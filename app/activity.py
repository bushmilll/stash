"""Keeps the app snappy while you use it: background work (color reading, AI scanning, tagging) steps aside
for a few seconds whenever you click, filter, search or scroll, then picks up again when you pause."""
import os
import time

_last = {"t": 0.0}
QUIET = 2.5          # seconds of no activity before background work resumes
QUIET_POLLS = ("/api/jobs", "/api/update", "/api/ai")   # the app's own status checks don't count as you


def touch(path=""):
    if path.startswith(QUIET_POLLS):
        return
    _last["t"] = time.time()


def busy():
    return time.time() - _last["t"] < QUIET


def wait_idle(max_wait=30):
    """Sleep while you're actively using the app (but never longer than max_wait)."""
    t0 = time.time()
    while busy() and time.time() - t0 < max_wait:
        time.sleep(0.25)


def low_priority():
    """For background ffmpeg runs: lower CPU priority so the app and your other apps come first."""
    if os.name == "posix":
        return {"preexec_fn": lambda: os.nice(15)}
    return {}
