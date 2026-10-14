import os
import threading
import webbrowser

import uvicorn

host = os.environ.get("STASH_HOST", "127.0.0.1")
port = int(os.environ.get("STASH_PORT", "8765"))

if __name__ == "__main__":
    if os.environ.get("STASH_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print(f"\n  Stash is running → http://localhost:{port}\n  (Ctrl+C to quit)\n")
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")
