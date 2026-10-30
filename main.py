import asyncio
import argparse
import copy
import json
import os
import posixpath
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
import webbrowser
from contextlib import closing, suppress
from pathlib import Path
from urllib.parse import quote, urlsplit

# A windowed frozen executable has no console streams, but dependencies may
# still use them for logging.
if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

import asyncssh
import uvicorn
from asyncssh.constants import FILEXFER_TYPE_DIRECTORY
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE
CONFIG_PATH = DATA_DIR / "config.toml"
if getattr(sys, "frozen", False) and not CONFIG_PATH.exists():
    try:
        with CONFIG_PATH.open("xb") as config_file:
            config_file.write((BASE / "config.toml").read_bytes())
    except FileExistsError:
        pass
with CONFIG_PATH.open("rb") as file:
    CONFIG = tomllib.load(file)

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
DATABASE = DATA_DIR / "connections.sqlite3"
# A random capability identifies each SSH session to the browser.
SSH_SESSIONS = {}
HOST_KEY_CHALLENGES = {}
SESSION_GRACE_SECONDS = 60
OUTPUT_BUFFER_LIMIT = 1024 * 1024
CONFIG_LOCK = threading.Lock()

# Run as an SSH exec channel, not in the interactive terminal. No remote files
# or shell startup settings are changed. Refuse ambiguous process matches.
REMOTE_CWD_PROBE = r'''
import json, os

def probe():
    connection = os.environ.get("SSH_CONNECTION")
    if not connection or not os.path.isdir("/proc"):
        return {"error": "服务器未提供 SSH_CONNECTION 或 Linux /proc"}
    expected = ("SSH_CONNECTION=" + connection).encode()
    uid = os.getuid()
    processes = {}
    candidates = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        base = "/proc/" + name
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(base + "/stat", "r") as f:
                info = f.read()
            fields = info[info.rfind(")") + 2:].split()
            pid, parent, tty = int(name), int(fields[1]), int(fields[4])
            processes[pid] = parent
            if not tty:
                continue
            with open(base + "/comm", "r") as f:
                command = f.read().strip().lstrip("-")
            if command not in {"bash", "sh", "dash", "zsh", "fish", "ksh", "mksh", "ash"}:
                continue
            with open(base + "/environ", "rb") as f:
                env = f.read().split(b"\0")
            if expected in env and any(item.startswith(b"SSH_TTY=") for item in env):
                candidates[pid] = (parent, tty, int(fields[2]), int(fields[5]))
        except (OSError, ValueError, IndexError):
            continue
    if not candidates:
        return {"error": "无法读取当前 SSH 终端的 shell 进程"}
    if len({item[1] for item in candidates.values()}) != 1:
        return {"error": "发现多个终端，无法确定当前目录"}
    ancestors = set()
    for pid in candidates:
        parent = candidates[pid][0]
        seen = set()
        while parent and parent not in seen:
            seen.add(parent)
            if parent in candidates:
                ancestors.add(parent)
            parent = processes.get(parent, 0)
    leaves = set(candidates) - ancestors
    if len(leaves) != 1:
        return {"error": "发现多个 shell，无法确定当前目录"}
    try:
        shell_pid = leaves.pop()
        path = os.readlink("/proc/%s/cwd" % shell_pid)
        if not path.startswith("/") or path.endswith(" (deleted)"):
            return {"error": "终端当前目录不可用"}
        try:
            with open("/proc/%s/wchan" % shell_pid, "r") as f:
                wchan = f.read().strip()
        except OSError:
            wchan = ""
        waiting = wchan in {"wait_woken", "n_tty_read", "tty_read", "do_select", "do_sys_poll", "poll_schedule_timeout", "poll_schedule_timeout.constprop.0"}
        return {"path": path, "shell_ready": candidates[shell_pid][2] == candidates[shell_pid][3] and waiting}
    except OSError:
        return {"error": "没有权限读取终端当前目录"}

print("MXSSH_CWD:" + json.dumps(probe(), ensure_ascii=True))
'''


async def close_ssh_session(token):
    session = SSH_SESSIONS.pop(token, None)
    if not session:
        return
    for task in (session.get("expiry"), session.get("reader")):
        if task and task is not asyncio.current_task():
            task.cancel()
    for websocket in tuple(session["websockets"]):
        with suppress(Exception):
            await websocket.send_json({"type": "session_closed"})
            await websocket.close()
    session["process"].close()
    session["connection"].close()
    with suppress(Exception):
        await session["connection"].wait_closed()


async def expire_session(token, session):
    try:
        await asyncio.sleep(SESSION_GRACE_SECONDS)
        if SSH_SESSIONS.get(token) is session and not session["websockets"]:
            await close_ssh_session(token)
    except asyncio.CancelledError:
        pass


async def read_ssh_output(token, session):
    try:
        while chunk := await session["process"].stdout.read(32768):
            async with session["send_lock"]:
                session["buffer"].extend(chunk)
                if len(session["buffer"]) > OUTPUT_BUFFER_LIMIT:
                    del session["buffer"][:-OUTPUT_BUFFER_LIMIT]
                for websocket in tuple(session["websockets"]):
                    with suppress(Exception):
                        await websocket.send_bytes(chunk)
    except (asyncssh.Error, OSError, asyncio.CancelledError):
        pass
    finally:
        await close_ssh_session(token)


@app.post("/api/session/disconnect")
async def disconnect_session(request: Request):
    check_origin(request)
    token = request.headers.get("x-mxssh-session", "")
    await close_ssh_session(token)
    return {"disconnected": True}


async def get_sftp(request: Request):
    token = request.headers.get("x-mxssh-session", "")
    session = SSH_SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=404, detail="SSH 会话已断开")
    async with session["lock"]:
        if SSH_SESSIONS.get(token) is not session:
            raise HTTPException(status_code=404, detail="SSH 会话已断开")
        if session["sftp"] is None:
            try:
                session["sftp"] = await session["connection"].start_sftp_client()
            except (asyncssh.Error, OSError) as exc:
                raise HTTPException(status_code=502, detail=f"无法启动 SFTP：{exc}") from exc
        return session["sftp"]


@app.get("/api/files/cwd")
async def get_terminal_cwd(request: Request):
    session = SSH_SESSIONS.get(request.headers.get("x-mxssh-session", ""))
    if not session:
        raise HTTPException(status_code=404, detail="SSH 会话已断开")
    try:
        result = await session["connection"].run(
            "python3 -B -c " + shlex.quote(REMOTE_CWD_PROBE), timeout=8, check=False
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith("MXSSH_CWD:")]
        if not lines:
            raise ValueError("无法执行目录检测（需要 Linux 和 python3）")
        data = json.loads(lines[-1][len("MXSSH_CWD:"):])
        if not isinstance(data, dict) or not isinstance(data.get("path"), str):
            raise ValueError(data.get("error", "无法确定终端当前目录") if isinstance(data, dict) else "目录检测结果无效")
        return {"path": data["path"], "shell_ready": data.get("shell_ready") is True}
    except (asyncssh.Error, OSError, ValueError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=f"无法定位终端目录：{exc}") from exc


class TerminalDirectoryInput(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


@app.post("/api/session/cd")
async def change_terminal_directory(item: TerminalDirectoryInput, request: Request):
    check_origin(request)
    token = request.headers.get("x-mxssh-session", "")
    session = SSH_SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=404, detail="SSH 会话已断开")
    if not item.path.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in item.path):
        raise HTTPException(status_code=400, detail="路径无效或包含控制字符")
    if session["input_pending"]:
        raise HTTPException(status_code=409, detail="终端中可能有尚未提交的输入，请先处理")
    version = session["input_version"]
    details = await get_terminal_cwd(request)
    if SSH_SESSIONS.get(token) is not session or session["input_version"] != version or session["input_pending"]:
        raise HTTPException(status_code=409, detail="终端状态已改变，请重试")
    if details["path"] == item.path:
        return {"sent": False, "already_there": True}
    if not details["shell_ready"]:
        raise HTTPException(status_code=409, detail="无法确认 shell 正在等待输入，请先回到命令提示符")
    session["process"].stdin.write(("cd -- " + shlex.quote(item.path) + "\r").encode("utf-8"))
    session["input_version"] += 1
    return {"sent": True}


@app.get("/api/files")
async def list_remote_files(request: Request, path: str = "."):
    sftp = await get_sftp(request)
    try:
        directory = await sftp.realpath(path)
        entries = []
        async for item in sftp.scandir(directory):
            if item.filename in (".", ".."):
                continue
            attrs = item.attrs
            is_directory = (attrs.type == FILEXFER_TYPE_DIRECTORY or
                            (attrs.permissions is not None and stat.S_ISDIR(attrs.permissions)))
            entries.append({"name": item.filename, "directory": is_directory, "size": attrs.size})
        entries.sort(key=lambda entry: (not entry["directory"], entry["name"].casefold()))
        return {"path": directory, "entries": entries}
    except (asyncssh.Error, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"读取目录失败：{exc}") from exc


@app.post("/api/files/upload")
async def upload_remote_file(request: Request, path: str):
    check_origin(request)
    sftp = await get_sftp(request)
    created = False
    try:
        async with sftp.open(path, "xb") as remote:
            created = True
            async for chunk in request.stream():
                if chunk:
                    await remote.write(chunk)
        return {"uploaded": True}
    except BaseException as exc:
        if created:
            with suppress(Exception):
                await sftp.remove(path)
        if isinstance(exc, (asyncssh.Error, OSError, ValueError)):
            raise HTTPException(status_code=400, detail=f"上传失败（目标文件不能已存在）：{exc}") from exc
        raise


@app.get("/api/files/download")
async def download_remote_file(request: Request, path: str):
    sftp = await get_sftp(request)
    try:
        remote = await sftp.open(path, "rb")
        attrs = await remote.stat()
    except (asyncssh.Error, OSError, ValueError) as exc:
        if 'remote' in locals():
            with suppress(Exception):
                await remote.close()
        raise HTTPException(status_code=400, detail=f"下载失败：{exc}") from exc

    async def content():
        try:
            while chunk := await remote.read(128 * 1024):
                yield chunk
        finally:
            await remote.close()

    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(posixpath.basename(path), safe="")}
    if attrs.size is not None:
        headers["Content-Length"] = str(attrs.size)
    return StreamingResponse(content(), media_type="application/octet-stream", headers=headers)


def database():
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("""CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
        port INTEGER NOT NULL, username TEXT NOT NULL, password TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS trusted_host_keys (
        host TEXT NOT NULL, port INTEGER NOT NULL, public_key TEXT NOT NULL,
        PRIMARY KEY (host, port)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS connection_commands (
        id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
        name TEXT NOT NULL, command TEXT NOT NULL,
        FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS connection_order (
        connection_id TEXT PRIMARY KEY, position INTEGER NOT NULL,
        FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE CASCADE
    )""")
    return db


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(default="", max_length=4096)

    @field_validator("name", "host", "username", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class ConnectionMoveInput(BaseModel):
    direction: str | None = Field(default=None, pattern="^(up|down)$")
    target_id: str | None = None
    placement: str | None = Field(default=None, pattern="^(before|after)$")


class ConnectionTestInput(ConnectionInput):
    connection_id: str | None = None


class ShortcutCommandInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=1000)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("命令必须为非空单行文本，不能包含控制字符")
        return value


class HostKeyConfirmation(BaseModel):
    challenge: str


def get_trusted_key(host, port):
    with closing(database()) as db, db:
        row = db.execute("SELECT public_key FROM trusted_host_keys WHERE host=? AND port=?",
                         (host.lower(), port)).fetchone()
        return row["public_key"] if row else None


class HostKeyVerifier(asyncssh.SSHClient):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.untrusted = None

    def validate_host_public_key(self, host, addr, port, key):
        public_key = key.export_public_key("openssh").decode("ascii").strip()
        trusted = get_trusted_key(self.host, self.port)
        if trusted == public_key:
            return True
        previous = []
        if trusted:
            previous.append(asyncssh.import_public_key(trusted).get_fingerprint())
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        if known_hosts.is_file():
            try:
                keys = asyncssh.match_known_hosts(str(known_hosts), host, addr, port if port != 22 else None)[0]
                previous.extend(item.get_fingerprint() for item in keys)
            except (OSError, ValueError, asyncssh.Error):
                pass
        self.untrusted = {"host": self.host, "port": self.port, "public_key": public_key,
                          "algorithm": key.get_algorithm(), "fingerprint": key.get_fingerprint(),
                          "previous": list(dict.fromkeys(previous)), "expected_key": trusted}
        return False


def create_host_key_challenge(info):
    now = time.monotonic()
    for token, entry in list(HOST_KEY_CHALLENGES.items()):
        if entry["expires"] <= now:
            HOST_KEY_CHALLENGES.pop(token, None)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    HOST_KEY_CHALLENGES[token] = {**info, "expires": now + 120}
    return {"challenge": token, "host": info["host"], "port": info["port"],
            "algorithm": info["algorithm"], "fingerprint": info["fingerprint"],
            "previous": info["previous"]}


def confirm_host_key(token):
    info = HOST_KEY_CHALLENGES.pop(token, None)
    if not info or info["expires"] <= time.monotonic():
        raise ValueError("密钥确认已过期，请重新连接")
    with closing(database()) as db, db:
        current = db.execute("SELECT public_key FROM trusted_host_keys WHERE host=? AND port=?",
                             (info["host"].lower(), info["port"])).fetchone()
        current_key = current["public_key"] if current else None
        if current_key not in (info["expected_key"], info["public_key"]):
            raise ValueError("该主机的信任记录已变化，请重新连接并核对指纹")
        db.execute("INSERT INTO trusted_host_keys (host, port, public_key) VALUES (?, ?, ?) "
                   "ON CONFLICT(host, port) DO UPDATE SET public_key=excluded.public_key",
                   (info["host"].lower(), info["port"], info["public_key"]))


async def open_ssh(host, port, username, password):
    verifier = HostKeyVerifier(host, port)
    options = dict(host=host, port=port, username=username, password=password,
                   client_keys=None, agent_path=None, preferred_auth="password",
                   connect_timeout=10, login_timeout=10)
    if CONFIG.get("ssh", {}).get("verify_host_key", True):
        options.update(client_factory=lambda: verifier, server_host_key_algs="default")
        pinned = get_trusted_key(host, port)
        if pinned:
            def pinned_hosts(match_host, addr, match_port):
                known_hosts = Path.home() / ".ssh" / "known_hosts"
                matched = asyncssh.match_known_hosts(str(known_hosts), match_host, addr, match_port) if known_hosts.is_file() else ([], [], [], [], [], [], [])
                # A confirmed replacement supersedes system host keys, while
                # explicit revocations remain enforced.
                return ([asyncssh.import_public_key(pinned)], [], matched[2], [], matched[4], [], matched[6])
            options["known_hosts"] = pinned_hosts
    else:
        options["known_hosts"] = None
    try:
        return await asyncssh.connect(**options), None
    except asyncssh.HostKeyNotVerifiable:
        if verifier.untrusted:
            return None, create_host_key_challenge(verifier.untrusted)
        raise


def check_origin(request: Request):
    origin = request.headers.get("origin", "")
    if not origin or urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(status_code=403, detail="Invalid origin")


class ServerSettingsInput(BaseModel):
    port: int = Field(ge=1, le=65535, strict=True)


def read_config_text():
    text = CONFIG_PATH.read_bytes().decode("utf-8")
    return text, tomllib.loads(text)


def update_config_port(text, original, port):
    expected = copy.deepcopy(original)
    if "server" in expected and not isinstance(expected["server"], dict):
        raise ValueError("配置文件中的 server 不是表")
    expected.setdefault("server", {})["port"] = port
    newline = "\r\n" if "\r\n" in text else "\n"
    section = re.search(r"(?m)^[ \t]*\[server\][ \t]*(?:\#[^\r\n]*)?\r?$", text)
    if section:
        next_section = re.search(r"(?m)^[ \t]*\[", text[section.end():])
        end = section.end() + next_section.start() if next_section else len(text)
        body = text[section.end():end]
        entry = re.search(r"(?m)^([ \t]*port[ \t]*=[ \t]*)([^\r\n#]+?)([ \t]*(?:\#[^\r\n]*)?)(\r?)$", body)
        if entry:
            body = body[:entry.start()] + entry.group(1) + str(port) + entry.group(3) + entry.group(4) + body[entry.end():]
        else:
            body = newline + f"port = {port}" + body
        updated = text[:section.end()] + body + text[end:]
    elif "server" not in original:
        updated = text + ("" if not text or text.endswith("\n") else newline) + f"[server]{newline}port = {port}{newline}"
    else:
        raise ValueError("请先将 server 配置改为独立的 [server] 表")
    if tomllib.loads(updated) != expected:
        raise ValueError("无法安全更新此配置文件，请手动修改 server.port")
    return updated


def settings_response(config):
    configured_port = int(config.get("server", {}).get("port", 8000))
    active_port = int(CONFIG.get("server", {}).get("port", 8000))
    return {"port": configured_port, "active_port": active_port, "restart_required": configured_port != active_port}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/settings")
def get_settings():
    try:
        with CONFIG_LOCK:
            _, config = read_config_text()
            return settings_response(config)
    except (OSError, ValueError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"读取配置失败：{exc}") from exc


@app.put("/api/settings")
def update_settings(item: ServerSettingsInput, request: Request):
    check_origin(request)
    temp_path = None
    try:
        with CONFIG_LOCK:
            text, config = read_config_text()
            updated = update_config_port(text, config, item.port)
            if updated != text:
                file_mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=CONFIG_PATH.parent,
                                                 prefix=".mxssh-config-", suffix=".tmp", delete=False) as temp:
                    temp_path = Path(temp.name)
                    temp.write(updated)
                os.chmod(temp_path, file_mode)
                os.replace(temp_path, CONFIG_PATH)
                temp_path = None
            return settings_response(tomllib.loads(updated))
    except (OSError, ValueError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"保存配置失败：{exc}") from exc
    finally:
        if temp_path:
            with suppress(OSError):
                temp_path.unlink()


@app.post("/api/host-keys/confirm")
def trust_host_key(item: HostKeyConfirmation, request: Request):
    check_origin(request)
    try:
        confirm_host_key(item.challenge)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"trusted": True}


@app.post("/api/connections/test")
async def test_connection(item: ConnectionTestInput, request: Request):
    check_origin(request)
    password = item.password
    if not password and item.connection_id:
        with closing(database()) as db, db:
            saved = db.execute("SELECT password FROM connections WHERE id=?", (item.connection_id,)).fetchone()
        if not saved:
            raise HTTPException(status_code=404, detail="连接不存在")
        password = saved["password"]
    if not password:
        raise HTTPException(status_code=422, detail="请输入密码")
    try:
        connection, host_key = await open_ssh(item.host, item.port, item.username, password)
        if host_key:
            return {"status": "host_key", "host_key": host_key}
        connection.close()
        await connection.wait_closed()
        return {"status": "success"}
    except (asyncssh.Error, OSError, ValueError, asyncio.TimeoutError) as exc:
        return {"status": "error", "message": str(exc) or "连接超时"}


@app.get("/api/connections")
def list_connections():
    with closing(database()) as db, db:
        return [dict(row) for row in db.execute(
            "SELECT c.id, c.name, c.host, c.port, c.username FROM connections c "
            "LEFT JOIN connection_order o ON o.connection_id=c.id "
            "ORDER BY o.position IS NULL, o.position, c.name COLLATE NOCASE, c.id"
        )]


@app.post("/api/connections/{connection_id}/move")
def move_connection(connection_id: str, item: ConnectionMoveInput, request: Request):
    check_origin(request)
    with closing(database()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        ids = [row[0] for row in db.execute(
            "SELECT c.id FROM connections c LEFT JOIN connection_order o ON o.connection_id=c.id "
            "ORDER BY o.position IS NULL, o.position, c.name COLLATE NOCASE, c.id"
        )]
        if connection_id not in ids:
            raise HTTPException(status_code=404, detail="连接不存在")
        original = ids.copy()
        index = ids.index(connection_id)
        if item.direction and not item.target_id and not item.placement:
            target = index + (-1 if item.direction == "up" else 1)
            if 0 <= target < len(ids):
                ids[index], ids[target] = ids[target], ids[index]
        elif not item.direction and item.target_id and item.placement:
            if item.target_id not in ids:
                raise HTTPException(status_code=404, detail="目标连接不存在")
            if item.target_id != connection_id:
                ids.remove(connection_id)
                target = ids.index(item.target_id) + (item.placement == "after")
                ids.insert(target, connection_id)
        else:
            raise HTTPException(status_code=422, detail="排序参数无效")
        if ids != original:
            db.executemany(
                "INSERT INTO connection_order (connection_id, position) VALUES (?, ?) "
                "ON CONFLICT(connection_id) DO UPDATE SET position=excluded.position",
                ((id, position) for position, id in enumerate(ids)),
            )
    return {"moved": ids != original}


@app.get("/api/connections/{connection_id}/commands")
def list_shortcuts(connection_id: str):
    with closing(database()) as db, db:
        if not db.execute("SELECT 1 FROM connections WHERE id=?", (connection_id,)).fetchone():
            raise HTTPException(status_code=404, detail="连接不存在")
        return [dict(row) for row in db.execute(
            "SELECT id, name, command FROM connection_commands WHERE connection_id=? ORDER BY name COLLATE NOCASE, id",
            (connection_id,),
        )]


@app.post("/api/connections/{connection_id}/commands", status_code=201)
def create_shortcut(connection_id: str, item: ShortcutCommandInput, request: Request):
    check_origin(request)
    command_id = uuid.uuid4().hex
    with closing(database()) as db, db:
        if not db.execute("SELECT 1 FROM connections WHERE id=?", (connection_id,)).fetchone():
            raise HTTPException(status_code=404, detail="连接不存在")
        db.execute("INSERT INTO connection_commands (id, connection_id, name, command) VALUES (?, ?, ?, ?)",
                   (command_id, connection_id, item.name, item.command))
    return {"id": command_id}


@app.put("/api/connections/{connection_id}/commands/{command_id}")
def update_shortcut(connection_id: str, command_id: str, item: ShortcutCommandInput, request: Request):
    check_origin(request)
    with closing(database()) as db, db:
        result = db.execute("UPDATE connection_commands SET name=?, command=? WHERE id=? AND connection_id=?",
                            (item.name, item.command, command_id, connection_id))
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="快捷命令不存在")
    return {"id": command_id}


@app.delete("/api/connections/{connection_id}/commands/{command_id}")
def delete_shortcut(connection_id: str, command_id: str, request: Request):
    check_origin(request)
    with closing(database()) as db, db:
        if not db.execute("DELETE FROM connection_commands WHERE id=? AND connection_id=?",
                          (command_id, connection_id)).rowcount:
            raise HTTPException(status_code=404, detail="快捷命令不存在")
    return {"deleted": True}


@app.post("/api/connections", status_code=201)
def create_connection(item: ConnectionInput, request: Request):
    check_origin(request)
    if not item.password:
        raise HTTPException(status_code=422, detail="请输入密码")
    connection_id = uuid.uuid4().hex
    with closing(database()) as db, db:
        db.execute("INSERT INTO connections VALUES (?, ?, ?, ?, ?, ?)",
                   (connection_id, item.name, item.host, item.port, item.username, item.password))
    return {"id": connection_id}


@app.put("/api/connections/{connection_id}")
def update_connection(connection_id: str, item: ConnectionInput, request: Request):
    check_origin(request)
    with closing(database()) as db, db:
        if item.password:
            result = db.execute("UPDATE connections SET name=?, host=?, port=?, username=?, password=? WHERE id=?",
                                (item.name, item.host, item.port, item.username, item.password, connection_id))
        else:
            result = db.execute("UPDATE connections SET name=?, host=?, port=?, username=? WHERE id=?",
                                (item.name, item.host, item.port, item.username, connection_id))
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="连接不存在")
    return {"id": connection_id}


@app.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: str, request: Request):
    check_origin(request)
    with closing(database()) as db, db:
        if not db.execute("DELETE FROM connections WHERE id=?", (connection_id,)).rowcount:
            raise HTTPException(status_code=404, detail="连接不存在")
    return {"deleted": True}


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


def valid_size(value, default):
    try:
        return max(1, min(int(value), 500))
    except (TypeError, ValueError):
        return default


@app.websocket("/ws")
async def terminal(websocket: WebSocket):
    # Browsers include Origin on WebSocket requests. Reject cross-origin pages.
    origin = websocket.headers.get("origin")
    if not origin or urlsplit(origin).netloc.lower() != websocket.headers.get("host", "").lower():
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session_token = None
    session = None
    created = False
    delivered = False
    explicit_disconnect = False
    launch_command = None
    try:
        data = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        if data.get("type") == "resume":
            session_token = data.get("session_token")
            session = SSH_SESSIONS.get(session_token)
            if not session:
                raise ValueError("会话已过期或断开，请重新连接")
        elif data.get("type") == "connect":
            if not data.get("connection_id"):
                raise ValueError("请选择已保存的连接")
            with closing(database()) as db, db:
                saved = db.execute("SELECT host, port, username, password FROM connections WHERE id=?",
                                   (data["connection_id"],)).fetchone()
                if data.get("command_id"):
                    command_row = db.execute("SELECT command FROM connection_commands WHERE id=? AND connection_id=?",
                                             (data["command_id"], data["connection_id"])).fetchone()
                    if not command_row:
                        raise ValueError("快捷命令不存在")
                    launch_command = command_row["command"]
            if saved is None:
                raise ValueError("连接不存在")
            host, port, username, password = saved
            if not host or not username or not isinstance(password, str) or not password or not 1 <= port <= 65535:
                raise ValueError("Host, username, password and a valid SSH port are required")
            connection, host_key = await open_ssh(host, port, username, password)
            if host_key:
                await websocket.send_json({"type": "host_key", "host_key": host_key})
                answer = await asyncio.wait_for(websocket.receive_json(), timeout=120)
                if answer.get("type") != "host_key_response" or not answer.get("trust"):
                    HOST_KEY_CHALLENGES.pop(host_key["challenge"], None)
                    raise ValueError("未信任服务器主机密钥")
                confirm_host_key(host_key["challenge"])
                connection, host_key = await open_ssh(host, port, username, password)
                if host_key:
                    HOST_KEY_CHALLENGES.pop(host_key["challenge"], None)
                    raise ValueError("服务器主机密钥发生变化，请重新连接并核对")
            try:
                process = await connection.create_process(
                    term_type="xterm-256color",
                    term_size=(valid_size(data.get("cols"), 80), valid_size(data.get("rows"), 24)),
                    encoding=None,
                )
            except BaseException:
                connection.close()
                await connection.wait_closed()
                raise
            session_token = uuid.uuid4().hex + uuid.uuid4().hex
            session = {"connection": connection, "process": process, "sftp": None,
                       "lock": asyncio.Lock(), "send_lock": asyncio.Lock(), "websockets": set(),
                       "buffer": bytearray(), "expiry": None, "reader": None,
                       "input_pending": False, "input_version": 0}
            SSH_SESSIONS[session_token] = session
            created = True
        else:
            raise ValueError("Expected a connection request")
        async with session["send_lock"]:
            if SSH_SESSIONS.get(session_token) is not session:
                raise ValueError("会话已过期或断开，请重新连接")
            if session["expiry"]:
                session["expiry"].cancel()
                session["expiry"] = None
            session["websockets"].add(websocket)
            session["process"].change_terminal_size(valid_size(data.get("cols"), 80),
                                                    valid_size(data.get("rows"), 24))
            await websocket.send_json({"type": "connected", "session_token": session_token})
            delivered = True
            if session["buffer"]:
                await websocket.send_bytes(bytes(session["buffer"]))
            if created:
                session["reader"] = asyncio.create_task(read_ssh_output(session_token, session))
                if launch_command is not None:
                    session["process"].stdin.write(launch_command.encode("utf-8") + b"\r")
                    session["input_version"] += 1
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                data = message["bytes"]
                if data:
                    session["input_version"] += 1
                    session["input_pending"] = data not in (b"\r", b"\n", b"\x03", b"\x15")
                    session["process"].stdin.write(data)
            elif message.get("text"):
                event = json.loads(message["text"])
                if event.get("type") == "resize":
                    session["process"].change_terminal_size(valid_size(event.get("cols"), 80),
                                                             valid_size(event.get("rows"), 24))
                elif event.get("type") == "disconnect":
                    explicit_disconnect = True
                    break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except (asyncssh.Error, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, asyncio.TimeoutError, RuntimeError) as exc:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(exc) or "Connection timed out"})
    finally:
        if session and SSH_SESSIONS.get(session_token) is session:
            if explicit_disconnect or (created and not delivered):
                await close_ssh_session(session_token)
            else:
                async with session["send_lock"]:
                    session["websockets"].discard(websocket)
                    if not session["websockets"] and not session["expiry"]:
                        session["expiry"] = asyncio.create_task(expire_session(session_token, session))
        with suppress(Exception):
            await websocket.close()


def server_address():
    server = CONFIG.get("server", {})
    return str(server.get("host", "127.0.0.1")), int(server.get("port", 8000))


def browser_url(host, port):
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def publish_tray_status(path, status):
    status_file = Path(path)
    temporary = status_file.with_suffix(".tmp")
    temporary.write_text(status, encoding="utf-8")
    temporary.replace(status_file)


def run_windows_tray(status_path=None):
    """Run the HTTP server and tray icon in the detached Windows process."""
    import pystray
    from tray_icon import create_icon

    host, port = server_address()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=5))
    stopped = threading.Event()
    failures = []
    tray = None

    def report_status(status):
        if status_path:
            publish_tray_status(status_path, status)
        elif status.startswith("error:"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, status[6:], "MXSSH 启动失败", 0x10)

    def run_server():
        try:
            server.run()
        except BaseException as exc:
            failures.append("网页服务启动失败，请检查端口和监听地址。" if isinstance(exc, SystemExit) else (str(exc) or "网页服务启动失败。"))
        finally:
            stopped.set()
            if tray:
                tray.stop()

    thread = threading.Thread(target=run_server, name="mxssh-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    try:
        while not server.started:
            if stopped.is_set():
                raise RuntimeError(failures[0] if failures else "网页服务启动失败，请检查端口是否被占用。")
            if time.monotonic() >= deadline:
                raise RuntimeError("网页服务启动超时。")
            stopped.wait(0.05)

        icon_image = create_icon()

        def open_page(icon, item):
            webbrowser.open(browser_url(host, port))

        def exit_program(icon, item):
            server.should_exit = True
            icon.stop()

        tray = pystray.Icon("MXSSH", icon_image, "MXSSH", pystray.Menu(
            pystray.MenuItem("打开", open_page, default=True),
            pystray.MenuItem("退出程序", exit_program),
        ))

        def ready(icon):
            try:
                if stopped.is_set():
                    raise RuntimeError("网页服务已停止，请检查监听地址和端口。")
                icon.visible = True
                report_status("ready")
            except Exception as exc:
                with suppress(OSError):
                    report_status("error:" + (str(exc) or "无法显示托盘图标。"))
                threading.Thread(target=icon.stop, daemon=True).start()

        tray.run(setup=ready)
    except BaseException as exc:
        with suppress(OSError):
            report_status("error:" + (str(exc) or "托盘启动失败"))
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def start_windows_tray():
    """Launch a windowless child and report startup failures to the caller."""
    with tempfile.TemporaryDirectory(prefix="mxssh-tray-") as directory:
        status_file = Path(directory) / "status"
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--tray-worker", str(status_file)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if status_file.exists():
                status = status_file.read_text(encoding="utf-8")
                if status == "ready":
                    print("MXSSH 已启动，可通过系统托盘打开或退出。")
                    return
                if status.startswith("error:"):
                    raise SystemExit(status[6:])
            if process.poll() is not None:
                raise SystemExit("托盘启动失败，请检查依赖和网页服务端口。")
            time.sleep(0.05)
        process.terminate()
        raise SystemExit("托盘启动超时，请检查网页服务端口。")


if __name__ == "__main__":
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            run_windows_tray()
        except BaseException:
            # run_windows_tray has already shown the startup error.
            raise SystemExit(1)
    elif sys.platform == "win32" and len(sys.argv) == 3 and sys.argv[1] == "--tray-worker":
        try:
            run_windows_tray(sys.argv[2])
        except BaseException as exc:
            with suppress(OSError):
                publish_tray_status(sys.argv[2], "error:" + (str(exc) or "托盘启动失败"))
            raise
    else:
        parser = argparse.ArgumentParser(description="MXSSH 网页 SSH 服务")
        parser.add_argument("--tray", action="store_true", help="在 Windows 系统托盘中运行")
        args = parser.parse_args()
        if args.tray:
            if sys.platform != "win32":
                parser.error("--tray 仅支持 Windows")
            start_windows_tray()
        else:
            host, port = server_address()
            uvicorn.run(app, host=host, port=port)
