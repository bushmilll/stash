"""Where the originals live: inside the library folder (default), Dropbox, another cloud folder, or an external drive.

The database, previews and video posters always stay on this Mac, so browsing never waits on the cloud.
Moving is done folder by folder; on the same disk (e.g. into Dropbox) each move is an instant rename.
If the app closes mid-move it picks up where it left off next launch.
"""
import os
import shutil
from pathlib import Path

from . import db

FOLDER_NAME = "Stash Media"


def candidates():
    """Places worth offering: Dropbox, Google Drive, iCloud Drive, OneDrive, external drives."""
    home = Path.home()
    out = []

    def add(label, base):
        if base.exists() and os.access(base, os.W_OK):
            out.append({"label": label, "path": str(base / FOLDER_NAME)})

    cloud = home / "Library" / "CloudStorage"
    if cloud.exists():
        for p in sorted(cloud.iterdir()):
            n = p.name
            if n.startswith("Dropbox"):
                add("Dropbox" + (n[7:].replace("-", " ").strip() and f" ({n[7:].strip('-').strip()})"), p)
            elif n.startswith("GoogleDrive"):
                add("Google Drive", p / "My Drive" if (p / "My Drive").exists() else p)
            elif n.startswith("OneDrive"):
                add("OneDrive", p)
    if (home / "Dropbox").exists() and not any(c["label"].startswith("Dropbox") for c in out):
        add("Dropbox", home / "Dropbox")
    add("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    vols = Path("/Volumes")
    if vols.exists():
        for v in sorted(vols.iterdir()):
            if v.is_dir() and not v.is_symlink() and v.name not in ("Macintosh HD", "Recovery", "Preboot"):
                add(f"Drive: {v.name}", v)
    return out


def status():
    s = db.load_settings()
    return {
        "media_dir": str(db.MEDIA_DIR),
        "is_default": db.MEDIA_DIR.resolve() == db.DEFAULT_MEDIA_DIR.resolve(),
        "available": db.media_available(),
        "moving_to": s.get("media_move_to") or None,
        "candidates": candidates(),
        "default_path": str(db.DEFAULT_MEDIA_DIR),
    }


def sizes():
    local = db.dir_size(db.THUMB_DIR) + sum(f.stat().st_size for f in db.DATA_DIR.glob("stash.db*") if f.is_file())
    originals = sum(db.dir_size(r) for r in db.media_roots() if r.exists())
    return {"local": local, "originals": originals}


def prepare_move(target):
    """Validate a destination and record the move; the job queue does the actual moving."""
    target = Path(target).expanduser()
    if not target.is_absolute():
        raise ValueError("Pick a full folder path.")
    current = db.MEDIA_DIR.resolve()
    if target.resolve() == current:
        raise ValueError("Your originals are already there.")
    if current in target.resolve().parents:
        raise ValueError("Pick a folder outside the current originals folder.")
    if not target.parent.exists():
        raise ValueError(f"{target.parent} doesn't exist. Is the drive plugged in?")
    target.mkdir(exist_ok=True)
    probe = target / ".stash-write-test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError:
        raise ValueError("Stash can't write to that folder.")
    same_disk = os.stat(target).st_dev == os.stat(current).st_dev if current.exists() else False
    if not same_disk:
        need = sizes()["originals"]
        free = shutil.disk_usage(target).free
        if need > free * 0.95:
            raise ValueError(f"Not enough space there: needs {need / 1e9:.1f} GB, has {free / 1e9:.1f} GB free.")
    db.save_settings({"media_move_from": str(db.MEDIA_DIR), "media_move_to": str(target)})
    return str(target)


def run_move(jid, update, check_cancel):
    s = db.load_settings()
    src, dst = Path(s.get("media_move_from") or db.MEDIA_DIR), Path(s.get("media_move_to") or "")
    if not s.get("media_move_to"):
        return "Nothing to move"
    if not dst.exists():
        raise RuntimeError(f"{dst} isn't reachable. Plug the drive in or check the cloud app is running.")
    db.MEDIA_DIR, db.MEDIA_MOVING_TO = src, dst
    items = sorted(src.iterdir()) if src.exists() else []
    moved = 0
    for i, item in enumerate(items):
        check_cancel()
        target = dst / item.name
        if item.is_dir() and target.exists():          # resumed move: merge what's left
            for f in item.iterdir():
                if not (target / f.name).exists():
                    shutil.move(str(f), str(target / f.name))
            shutil.rmtree(item, ignore_errors=True)
        else:
            shutil.move(str(item), str(target))
        moved += 1
        if i % 10 == 0:
            update(jid, done=i, total=len(items), message=f"Moving originals: {i + 1} of {len(items)} posts")
    db.MEDIA_DIR, db.MEDIA_MOVING_TO = dst, None
    db.save_settings({"media_dir": "" if dst.resolve() == db.DEFAULT_MEDIA_DIR.resolve() else str(dst),
                      "media_move_from": "", "media_move_to": ""})
    try:
        if src.exists() and src != db.DEFAULT_MEDIA_DIR and not any(src.iterdir()):
            src.rmdir()
    except OSError:
        pass
    return f"Moved {moved} posts' originals to {dst}"
