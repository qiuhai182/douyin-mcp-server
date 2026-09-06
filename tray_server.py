#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run the douyin-mcp-server WebUI as a Windows system-tray application.

No console window is shown: the FastAPI server runs on a background thread
and all of its output (stdout/stderr/uvicorn logs) is redirected to
logs/webui.log. Interact via the tray icon:

  - double-click (or menu "打开控制台界面") -> open the WebUI in a browser
  - menu "打开运行日志"                    -> open logs/webui.log
  - menu "退出"                            -> graceful server shutdown

Usage:
    .venv\\Scripts\\pythonw.exe tray_server.py
    (start.bat does this for you)

Windows only (pystray Win32 backend). To run the WebUI the classic way,
with a visible console, use:  python web/app.py
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "webui.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # simple cap: restart truncates oversized logs

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8080"))
URL = f"http://{HOST}:{PORT}"

# pythonw.exe has no console: sys.stdout/stderr are None, and any print /
# logging write would raise AttributeError. Redirect everything into a log
# file before importing anything that may log.
try:
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
        LOG_FILE.unlink()
    LOG_DIR.mkdir(exist_ok=True)
    sys.stdout = open(LOG_FILE, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
except Exception:
    # Even logging setup failing must not kill the process silently.
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = sys.stdout

import requests  # noqa: E402
import pystray  # noqa: E402
import uvicorn  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


_SESSION = requests.Session()
_SESSION.trust_env = False  # never route localhost checks through a proxy


def _already_running() -> bool:
    """True when another instance is already serving the WebUI."""
    try:
        return _SESSION.get(f"{URL}/", timeout=2).status_code == 200
    except Exception:
        return False


def _port_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.bind((HOST, PORT))
            return True
        except OSError:
            return False


def _load_webapp():
    """Import web/app.py by path (the web directory is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "douyin_webui", ROOT / "web" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["douyin_webui"] = module
    spec.loader.exec_module(module)
    return module


def _tray_image() -> Image.Image:
    """Bundled artwork when available, else a simple fallback badge."""
    try:
        return Image.open(ROOT / "douyin-video.png").convert("RGBA")
    except Exception:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(254, 44, 85, 255))
        d.text((32, 30), "抖", fill="white", anchor="mm")
        return img


def _open_console(icon=None, item=None):
    webbrowser.open(URL)


def _open_log(icon=None, item=None):
    os.startfile(LOG_FILE)  # noqa: S606 - default text editor on Windows


def _fatal(message: str):
    """Show a native error dialog (there is no console to print to)."""
    print(f"[FATAL] {message}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"{message}\n\n详细日志：\n{LOG_FILE}",
            "Douyin WebUI 启动失败", 0x10)
    except Exception:
        pass


def main():
    if _already_running():
        # Double launch: just reveal the existing console.
        _open_console()
        return

    if not _port_free():
        _fatal(f"端口 {PORT} 被其它程序占用，且不是本服务的响应。")
        return

    webapp = _load_webapp()
    config = uvicorn.Config(webapp.app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(
        target=server.run, name="uvicorn-server", daemon=True)
    server_thread.start()

    # Wait for startup; cold starts on Python 3.14 can take >15s.
    for _ in range(60):
        if server.started or _already_running():
            break
        if not server_thread.is_alive():
            _fatal("服务线程意外退出。")
            return
        time.sleep(0.5)
    else:
        _fatal(f"服务在 30 秒内未能启动（{URL}）。")
        return

    def _quit(icon=None, item=None):
        server.should_exit = True
        server_thread.join(timeout=10)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开控制台界面", _open_console, default=True),
        pystray.MenuItem("打开运行日志", _open_log),
        pystray.MenuItem("退出", _quit),
    )
    icon = pystray.Icon(
        "douyin-webui", _tray_image(), f"Douyin WebUI - {URL}", menu)
    print(f"[tray] running, console at {URL}, log at {LOG_FILE}")
    icon.run()


if __name__ == "__main__":
    main()
