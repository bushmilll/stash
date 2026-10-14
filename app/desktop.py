"""Stash as a desktop app: runs the library server inside the app and shows it in its own window.

Double-clicking Stash.app starts this. No Terminal, no browser tab.
"""
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("STASH_DESKTOP", "1")
DATA = Path(os.environ.get("STASH_DATA", Path.home() / "Stash")).expanduser()
DATA.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("STASH_PORT", "8765"))

# when packaged there's no Terminal to print to, so keep a log in the library folder
if getattr(sys, "frozen", False) and not os.environ.get("STASH_SELFTEST"):
    log_file = open(DATA / "stash.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log_file
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stash")


def _is_stash(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=1.5) as r:
            return r.status == 200 and b"instagram_username" in r.read()
    except Exception:
        return False


def _free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _start_server():
    """Returns the port Stash is reachable on (reuses an already-running Stash instead of opening the library twice)."""
    if _is_stash(PORT):
        log.info("Stash is already running; opening another window")
        return PORT, False
    port = PORT if _free(PORT) else next(p for p in range(PORT + 1, PORT + 50) if _free(p))
    import uvicorn
    from app.main import app as asgi_app
    config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if _is_stash(port):
            break
        time.sleep(0.05)
    log.info("Stash library server on port %s, data in %s", port, DATA)
    return port, True


def main():
    port, _ = _start_server()
    if os.environ.get("STASH_SELFTEST"):
        # used by the build: check the packaged app works, then quit (no window)
        from app.selftest import run
        ok = run(port)
        sys.stdout.flush()
        os._exit(0 if ok else 1)
    url = f"http://127.0.0.1:{port}/?app=1"
    try:
        import webview  # pywebview: a native window (WebKit on macOS)
    except ImportError:
        import webbrowser
        webbrowser.open(url.replace("?app=1", ""))
        log.info("pywebview not available; opened in the browser instead")
        while True:
            time.sleep(3600)
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window("Stash", url, width=1440, height=900, min_size=(900, 600), background_color="#000000", text_select=True)
    storage = DATA / ".window"
    storage.mkdir(exist_ok=True)
    webview.start(private_mode=False, storage_path=str(storage))


if __name__ == "__main__":
    main()
