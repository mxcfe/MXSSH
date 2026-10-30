"""Build the Windows single-file executable with the same icon as the tray."""

import os
import subprocess
import sys
from pathlib import Path

from tray_icon import create_icon


def main():
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 上执行此打包脚本。")
    root = Path(__file__).resolve().parent
    build_dir = root / "build"
    build_dir.mkdir(exist_ok=True)
    icon_path = build_dir / "mxssh.ico"
    create_icon().save(icon_path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
        "--name", "MXSSH", "--icon", str(icon_path),
        "--add-data", f"{root / 'static'}{os.pathsep}static",
        "--add-data", f"{root / 'config.toml'}{os.pathsep}.",
        "--hidden-import", "pystray._win32", "--collect-submodules", "uvicorn",
        "--collect-submodules", "asyncssh", str(root / "main.py"),
    ]
    subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
