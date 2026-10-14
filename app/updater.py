"""Keeps Stash up to date: checks GitHub for a newer release and, on request, swaps the app in place.

Flow when the user clicks "Update":
  download the right .dmg (Apple Silicon or Intel) -> mount it -> copy the new Stash.app next to the running one
  -> hand off to a tiny shell script that waits for this app to quit, swaps the bundles and reopens Stash.
The library (~/Stash, or wherever the user moved the originals) is never touched.
"""
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

from . import __version__, db

log = logging.getLogger("stash")
REPO = "bushmilll/stash"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 30 * 60

_cache = {"at": 0, "info": None}
_state = {"status": "idle", "progress": 0, "message": "", "error": None}
_lock = threading.Lock()


def _ver(s):
    try:
        return tuple(int(x) for x in str(s).lstrip("v").split("."))
    except ValueError:
        return (0,)


def arch():
    """Which build fits this Mac. An Intel build running under Rosetta on an M-series Mac gets the Apple Silicon one."""
    if platform.machine() == "arm64":
        return "apple-silicon"
    try:
        out = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=3).stdout.strip()
        if out == "1":
            return "apple-silicon"
    except Exception:
        pass
    return "intel"


def app_bundle():
    """Path of the running Stash.app, or None when not running as the packaged Mac app."""
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def can_replace():
    app = app_bundle()
    if not app:
        return False
    # macOS runs apps straight from a download in a read-only shadow copy ("translocation") until they're moved
    return "AppTranslocation" not in str(app) and os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)


def check(force=False):
    if not force and _cache["info"] and time.time() - _cache["at"] < CHECK_EVERY:
        return _cache["info"]
    info = {"current": __version__, "latest": None, "available": False, "notes": "", "url": None, "asset": None}
    try:
        r = requests.get(API, timeout=8, headers={"Accept": "application/vnd.github+json", "User-Agent": "Stash-updater"})
        r.raise_for_status()
        rel = r.json()
        latest = rel.get("tag_name", "").lstrip("v")
        want = f"Stash-{arch()}.dmg"
        asset = next((a for a in rel.get("assets", []) if a.get("name") == want), None)
        info.update(latest=latest, notes=(rel.get("body") or "")[:2000], url=rel.get("html_url"),
                    asset=asset and {"name": asset["name"], "url": asset["browser_download_url"], "size": asset.get("size")},
                    available=bool(asset) and _ver(latest) > _ver(__version__))
    except Exception as e:
        info["error"] = f"Couldn't check for updates ({e.__class__.__name__})"
    _cache.update(at=time.time(), info=info)
    return info


def status(force=False):
    info = dict(check(force))
    info.update(state=dict(_state), can_install=bool(app_bundle()), in_place=can_replace(), arch=arch())
    return info


def _set(status, message="", progress=None, error=None):
    _state.update(status=status, message=message, error=error)
    if progress is not None:
        _state["progress"] = progress


def start_install():
    with _lock:
        if _state["status"] in ("downloading", "installing", "restarting"):
            return False
        _set("downloading", "Starting download", 0)
    threading.Thread(target=_install, daemon=True).start()
    return True


def _download(url, dest, size):
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or size or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    _set("downloading", f"Downloading {done >> 20} of {total >> 20} MB", round(done / total, 3))


def _install():
    try:
        info = check(force=True)
        if not info.get("asset"):
            raise RuntimeError(info.get("error") or "No download found for this Mac")
        work = db.DATA_DIR / ".update"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        dmg = work / info["asset"]["name"]
        _download(info["asset"]["url"], dmg, info["asset"].get("size"))

        app = app_bundle()
        if not app or not can_replace():
            # can't swap it ourselves (not in a writable folder): open the disk image so it's a simple drag
            subprocess.run(["open", str(dmg)], check=False)
            _set("manual", "The new version is open in Finder. Quit Stash, then drag Stash into Applications and choose Replace.", 1)
            return

        _set("installing", "Installing", 1)
        mnt = Path(tempfile.mkdtemp(prefix="stash-dmg-"))
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-noautoopen", "-readonly", "-mountpoint", str(mnt), str(dmg)],
                       check=True, capture_output=True, timeout=180)
        staged = app.parent / ".Stash-update.app"
        try:
            shutil.rmtree(staged, ignore_errors=True)
            subprocess.run(["ditto", str(mnt / "Stash.app"), str(staged)], check=True, capture_output=True, timeout=600)
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt), "-force"], capture_output=True, timeout=60)
        if not (staged / "Contents" / "MacOS" / "Stash").exists():
            raise RuntimeError("The download looks incomplete")

        old = app.parent / ".Stash-old.app"
        script = work / "swap.sh"
        script.write_text(f"""#!/bin/bash
# wait for the running Stash to quit, swap in the new version, reopen it
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done
rm -rf "{old}"
if mv "{app}" "{old}" && mv "{staged}" "{app}"; then
  rm -rf "{old}"
else
  [ -d "{app}" ] || mv "{old}" "{app}"
fi
xattr -dr com.apple.quarantine "{app}" 2>/dev/null
open "{app}"
""")
        _set("restarting", "Restarting Stash", 1)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                         stdout=open(work / "swap.log", "w"), stderr=subprocess.STDOUT)
        log.info("Update to %s staged; restarting", info.get("latest"))
        time.sleep(1.5)   # lets the window show "Restarting"
        os._exit(0)
    except Exception as e:
        log.exception("Update failed")
        _set("error", "", error=str(e) or e.__class__.__name__)
