"""Media handling: probing, thumbnails, stills, shot extraction, clips and GIFs (ffmpeg)."""
import json
import time
import shutil
import subprocess
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from . import db

THUMB_WIDTH = 520
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}

import os
import re


def _find_ffmpeg():
    """Bundled ffmpeg (inside the Mac app, via imageio-ffmpeg) first, then one installed on the system."""
    if os.environ.get("STASH_FFMPEG"):
        return os.environ["STASH_FFMPEG"]
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for p in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if p and Path(p).exists():
            return p
    return "ffmpeg"


FFMPEG = _find_ffmpeg()


def run(cmd, timeout=600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffmpeg failed").strip().splitlines()[-1][:300])
    return p.stdout


def kind_of(path: Path):
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return None


def probe(path: Path):
    """Return (width, height, duration) for a file."""
    if kind_of(path) == "image":
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                return im.width, im.height, None
        except Exception:
            pass
    # read size/duration from ffmpeg's own header dump (no ffprobe needed)
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60)
    info = p.stderr
    w = h = dur = None
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if m:
        dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})", info)
    if m:
        w, h = int(m[1]), int(m[2])
        if re.search(r"rotation of -?90", info):
            w, h = h, w
    return w, h, dur


def make_thumb(src: Path, name: str):
    """Create a ~520px wide JPEG thumbnail. Returns filename relative to THUMB_DIR."""
    out = db.THUMB_DIR / f"{name}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind_of(src) == "video":
        tmp = out.with_suffix(".tmp.jpg")
        try:
            run([FFMPEG, "-y", "-v", "error", "-ss", "0.8", "-i", str(src), "-frames:v", "1",
                 "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", str(tmp)], timeout=120)
        except RuntimeError:  # very short video: take the first frame
            run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
                 "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", str(tmp)], timeout=120)
        tmp.replace(out)
    else:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 3))
            im.save(out, "JPEG", quality=82, optimize=True)
    return out.relative_to(db.THUMB_DIR).as_posix()


def make_poster(src: Path, name: str):
    """Full-resolution still for a video (what the grid shows when tiles are big or the screen is Retina)."""
    out = db.THUMB_DIR / f"{name}_poster.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    for seek in (["-ss", "0.8"], []):   # very short clips: fall back to the first frame
        try:
            run([FFMPEG, "-y", "-v", "error", *seek, "-i", str(src), "-frames:v", "1",
                 "-vf", "scale='min(1920,iw)':-2", "-q:v", "2", str(tmp)], timeout=120)
            tmp.replace(out)
            return out.relative_to(db.THUMB_DIR).as_posix()
        except RuntimeError:
            continue
    return None


def ingest_file(c, post_id, shortcode, idx, path: Path):
    """Register a downloaded media file (already inside MEDIA_DIR) for a post."""
    k = kind_of(path)
    if not k:
        return
    w, h, dur = probe(path)
    thumb = make_thumb(path, f"{shortcode}/{idx:02d}")
    poster = make_poster(path, f"{shortcode}/{idx:02d}") if k == "video" else None
    rel = path.relative_to(db.MEDIA_DIR).as_posix()
    c.execute(
        "INSERT INTO media (post_id, idx, kind, path, thumb, poster, width, height, duration) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(post_id, idx) DO UPDATE SET kind=excluded.kind, path=excluded.path, thumb=excluded.thumb, "
        "poster=excluded.poster, width=excluded.width, height=excluded.height, duration=excluded.duration",
        (post_id, idx, k, rel, thumb, poster, w, h, dur))


def refresh_post_kind(c, post_id):
    kinds = [r[0] for r in c.execute("SELECT kind FROM media WHERE post_id=? ORDER BY idx", (post_id,))]
    kind = "carousel" if len(kinds) > 1 else (kinds[0] if kinds else "")
    c.execute("UPDATE posts SET kind=? WHERE id=?", (kind, post_id))


# ---------- exports ----------

def _export_path(stem, ext):
    name = f"{stem}_{uuid.uuid4().hex[:6]}.{ext}"
    return db.EXPORT_DIR / name


def media_row(media_id):
    r = db.conn().execute(
        "SELECT m.*, p.shortcode, p.username FROM media m JOIN posts p ON p.id=m.post_id WHERE m.id=?",
        (media_id,)).fetchone()
    if not r:
        raise FileNotFoundError("media not found")
    return r


def nice_stem(r):
    user = (r["username"] or "ig").replace("/", "_")
    return f"{user}_{r['shortcode']}_{r['idx'] + 1}"


def still(media_id, t: float):
    """Full-resolution PNG of a single video frame at time t (seconds)."""
    r = media_row(media_id)
    src = db.MEDIA_DIR / r["path"]
    out = _export_path(f"{nice_stem(r)}_{t:.2f}s".replace(".", "-"), "png")
    # -ss after -i = frame-accurate (decodes up to t; fine for social-length clips)
    run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-ss", f"{t:.3f}", "-frames:v", "1", str(out)])
    return out


def shots(media_id, threshold=0.3):
    """Extract one full-res frame per detected shot (scene change). Returns a zip path."""
    r = media_row(media_id)
    src = db.MEDIA_DIR / r["path"]
    work = db.EXPORT_DIR / f"shots_{uuid.uuid4().hex[:8]}"
    work.mkdir()
    # first frame + every frame where the scene changes
    vf = f"select='eq(n\\,0)+gt(scene\\,{threshold})'"
    run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-vf", vf, "-fps_mode", "vfr",
         str(work / "shot_%03d.png")], timeout=900)
    zpath = _export_path(f"{nice_stem(r)}_shots", "zip")
    shutil.make_archive(str(zpath.with_suffix("")), "zip", work)
    shutil.rmtree(work, ignore_errors=True)
    return zpath


def clip(media_id, start: float, end: float, fmt="mp4", width=None, fps=None):
    """Export a section of a video as mp4 (high quality) or gif (palette-optimized)."""
    r = media_row(media_id)
    src = db.MEDIA_DIR / r["path"]
    start = max(0.0, float(start))
    dur = max(0.1, float(end) - start)
    stem = f"{nice_stem(r)}_{start:.1f}-{start + dur:.1f}".replace(".", "-")
    if fmt == "gif":
        fps = int(fps or 15)
        width = int(width or 640)
        out = _export_path(stem, "gif")
        vf = (f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
              f"[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a")
        run([FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-vf", vf, "-loop", "0", str(out)])
    else:
        out = _export_path(stem, "mp4")
        vf = ["-vf", f"scale={int(width)}:-2:flags=lanczos"] if width else []
        head = [FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src), *vf]
        tail = ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
        try:
            run(head + ["-c:v", "libx264", "-crf", "14", "-preset", "slow"] + tail)
        except RuntimeError:
            # ffmpeg builds without x264: use the Mac's hardware encoder at a high bitrate instead
            run(head + ["-c:v", "h264_videotoolbox", "-b:v", "30M"] + tail)
    return out


def post_zip(post_id):
    c = db.conn()
    p = c.execute("SELECT shortcode, username, caption, url FROM posts WHERE id=?", (post_id,)).fetchone()
    work = db.EXPORT_DIR / f"post_{uuid.uuid4().hex[:8]}"
    work.mkdir()
    for m in c.execute("SELECT idx, path FROM media WHERE post_id=? ORDER BY idx", (post_id,)):
        src = db.MEDIA_DIR / m["path"]
        shutil.copy2(src, work / f"{m['idx'] + 1:02d}{src.suffix}")
    (work / "caption.txt").write_text(f"@{p['username']}\n{p['url']}\n\n{p['caption'] or ''}\n")
    zpath = _export_path(f"{p['username'] or 'ig'}_{p['shortcode']}", "zip")
    shutil.make_archive(str(zpath.with_suffix("")), "zip", work)
    shutil.rmtree(work, ignore_errors=True)
    return zpath


def cleanup_exports(max_age_hours=24):
    import time
    cutoff = time.time() - max_age_hours * 3600
    for f in db.EXPORT_DIR.iterdir():
        try:
            if f.stat().st_mtime < cutoff:
                shutil.rmtree(f) if f.is_dir() else f.unlink()
        except OSError:
            pass


# ---------- batch export (collections / selections) ----------

def _safe(name, default="export"):
    keep = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in (name or "")).strip(" .")
    return keep[:60] or default


def export_batch(post_ids, name, include_images=True, include_videos=True, metadata=True):
    """Zip the original files of many posts (+ a spreadsheet of where each came from)."""
    import csv
    import io
    import zipfile
    c = db.conn()
    out = _export_path(_safe(name), "zip")
    rows, files = [], 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for pid in post_ids:
            p = c.execute("SELECT * FROM posts WHERE id=? AND deleted_at IS NULL", (pid,)).fetchone()
            if not p:
                continue
            cols = ", ".join(r[0] for r in c.execute(
                "SELECT x.name FROM collections x JOIN post_collections pc ON pc.collection_id=x.id WHERE pc.post_id=?", (pid,)))
            tags = ", ".join(r[0] for r in c.execute(
                "SELECT t.name FROM tags t JOIN post_tags pt ON pt.tag_id=t.id WHERE pt.post_id=?", (pid,)))
            for m in c.execute("SELECT idx, kind, path FROM media WHERE post_id=? ORDER BY idx", (pid,)):
                if (m["kind"] == "video" and not include_videos) or (m["kind"] == "image" and not include_images):
                    continue
                src = db.MEDIA_DIR / m["path"]
                if not src.exists():
                    continue
                arc = f"{_safe(p['username'], 'ig')}_{p['shortcode']}_{m['idx'] + 1}{src.suffix.lower()}"
                z.write(src, arc)
                files += 1
                rows.append([arc, p["username"], p["url"], (p["caption"] or "").replace("\n", " "), cols, tags,
                             p["notes"] or "", time.strftime("%Y-%m-%d", time.localtime(p["saved_at"])) if p["saved_at"] else "",
                             time.strftime("%Y-%m-%d", time.localtime(p["posted_at"])) if p["posted_at"] else ""])
        if metadata and rows:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["file", "creator", "instagram_url", "caption", "collections", "tags", "notes", "saved", "posted"])
            w.writerows(rows)
            z.writestr("_list.csv", "﻿" + buf.getvalue())  # BOM so Excel/Numbers read emoji correctly
    if not files:
        out.unlink(missing_ok=True)
        raise RuntimeError("Nothing to export. These posts have no downloaded media of that type yet.")
    return out, files


# ---------- trash / repair ----------

def purge_posts(c, post_ids):
    """Permanently delete the files of these posts. The post row stays (status 'removed') so sync
    doesn't bring it back."""
    freed = 0
    for pid in post_ids:
        p = c.execute("SELECT shortcode FROM posts WHERE id=?", (pid,)).fetchone()
        if not p:
            continue
        for d in (db.MEDIA_DIR / p["shortcode"], db.THUMB_DIR / p["shortcode"]):
            if d.exists():
                freed += db.dir_size(d)
                shutil.rmtree(d, ignore_errors=True)
        c.execute("DELETE FROM media WHERE post_id=?", (pid,))
        c.execute("DELETE FROM post_collections WHERE post_id=?", (pid,))
        c.execute("DELETE FROM post_tags WHERE post_id=?", (pid,))
        c.execute("UPDATE posts SET status='removed', caption='', notes='', favorite=0, kind='' WHERE id=?", (pid,))
        c.execute("DELETE FROM search WHERE rowid=?", (pid,))
    return freed


def check_library():
    """Find media rows whose files vanished (moved/deleted outside the app) and queue those posts for
    re-download; rebuild missing thumbnails."""
    c = db.conn()
    missing_files = rebuilt = requeued = 0
    broken_posts = set()
    rows = c.execute("SELECT id, post_id, idx, path, thumb FROM media").fetchall()
    for r in rows:
        src = db.MEDIA_DIR / r["path"]
        if not src.exists():
            broken_posts.add(r["post_id"])
            missing_files += 1
            continue
        if not r["thumb"] or not (db.THUMB_DIR / r["thumb"]).exists():
            try:
                sc = c.execute("SELECT shortcode FROM posts WHERE id=?", (r["post_id"],)).fetchone()[0]
                thumb = make_thumb(src, f"{sc}/{r['idx']:02d}")
                with db.tx() as w:
                    w.execute("UPDATE media SET thumb=? WHERE id=?", (thumb, r["id"]))
                rebuilt += 1
            except Exception:
                pass
    with db.tx() as w:
        for pid in broken_posts:
            w.execute("DELETE FROM media WHERE post_id=? AND id IN (SELECT id FROM media WHERE post_id=?)", (pid, pid))
            w.execute("UPDATE posts SET status='pending', attempts=0, error='Files went missing, will re-download' "
                      "WHERE id=? AND deleted_at IS NULL", (pid,))
            requeued += 1
        # posts marked ready but with no media at all
        for r in w.execute("SELECT p.id FROM posts p WHERE p.status='ready' AND NOT EXISTS "
                           "(SELECT 1 FROM media m WHERE m.post_id=p.id)").fetchall():
            w.execute("UPDATE posts SET status='pending', attempts=0 WHERE id=?", (r[0],))
            requeued += 1
    return {"checked": len(rows), "missing_files": missing_files, "thumbs_rebuilt": rebuilt, "requeued": requeued}


def ffmpeg_ok():
    return Path(FFMPEG).exists() or shutil.which(FFMPEG) is not None
