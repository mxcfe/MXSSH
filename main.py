import asyncio
import json
import posixpath
import shlex
import sqlite3
import stat
import time
import tomllib
import uuid
from contextlib import closing, suppress
from pathlib import Path
from urllib.parse import quote, urlsplit

import asyncssh
import uvicorn
from asyncssh.constants import FILEXFER_TYPE_DIRECTORY
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE = Path(__file__).resolve().parent
with (BASE / "config.toml").open("rb") as file:
    CONFIG = tomllib.load(file)

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
DATABASE = BASE / "connections.sqlite3"
# A random capability identifies each SSH session to the browser.
SSH_SESSIONS = {}
HOST_KEY_CHALLENGES = {}
SESSION_GRACE_SECONDS = 60
OUTPUT_BUFFER_LIMIT = 1024 * 1024

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
    db.execute("""CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
        port INTEGER NOT NULL, username TEXT NOT NULL, password TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS trusted_host_keys (
        host TEXT NOT NULL, port INTEGER NOT NULL, public_key TEXT NOT NULL,
        PRIMARY KEY (host, port)
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


class ConnectionTestInput(ConnectionInput):
    connection_id: str | None = None


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
            "SELECT id, name, host, port, username FROM connections ORDER BY name COLLATE NOCASE, id"
        )]


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


if __name__ == "__main__":
    server = CONFIG.get("server", {})
    uvicorn.run(app, host=server.get("host", "127.0.0.1"), port=int(server.get("port", 8000)))
