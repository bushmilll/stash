"""Logged-in sync with Instagram via gallery-dl, plus a small background job runner.

Design notes (keeping your account safe):
  * One job at a time, and Instagram's API is only ever asked one thing at a time.
  * gallery-dl waits a random 6-12s between Instagram API requests (configurable).
  * The media files themselves (from Instagram's file servers, not its API) download a few at once,
    the way a browser loads a page of videos, while the next request waits its turn.
  * Feeds are newest-first, so a routine sync stops after N already-downloaded posts in a row
    instead of paging through all 10k+ saves every time.
  * If Instagram pushes back (login challenge, rate limit), the job stops instead of retrying.
"""
import os
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback

import requests

from . import db, media, storage

try:
    from gallery_dl import config as gdl_config, extractor as gdl_extractor, exception as gdl_exc
    from gallery_dl.extractor.message import Message
    HAVE_GDL = True
except ImportError:  # app still works for browsing without it
    HAVE_GDL = False

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


class StopSync(Exception):
    """Instagram pushed back; stop everything."""


class Cancelled(Exception):
    pass


DL_WORKERS = 3   # media files downloading at once (file servers only; API requests stay one at a time)


class _Downloads:
    """Runs _save_post calls a few at a time; the caller keeps reading the feed meanwhile."""
    def __init__(self, on_done):
        self.ex = ThreadPoolExecutor(DL_WORKERS, thread_name_prefix="stash-dl")
        self.pending = []
        self.on_done = on_done   # on_done(tag, exception_or_None)

    def submit(self, tag, fn, *a, **k):
        while len(self.pending) >= DL_WORKERS * 2:   # don't run far ahead of the downloads
            self.reap(block=True)
        self.pending.append((tag, self.ex.submit(fn, *a, **k)))

    def reap(self, block=False):
        if block and self.pending:
            self.pending[0][1].exception()   # wait for the oldest
        still = []
        for tag, f in self.pending:
            if f.done():
                self.on_done(tag, f.exception())
            else:
                still.append((tag, f))
        self.pending = still

    def finish(self):
        try:
            while self.pending:
                self.reap(block=True)
        finally:
            self.ex.shutdown(wait=True)

    def abort(self):
        self.ex.shutdown(wait=True, cancel_futures=True)


def _keep_awake():
    """Stop the Mac from idle-sleeping while a job runs (closing the lid still sleeps it)."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None


# ---------- job runner ----------

_queue = queue.Queue()
_current = {"id": None, "cancel": False}
_last_auto = {"t": 0}


def enqueue(kind):
    with db.tx() as c:
        busy = c.execute("SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running')", (kind,)).fetchone()
        if busy:
            return busy[0]
        c.execute("INSERT INTO jobs (kind, status, message, started) VALUES (?, 'queued', 'Waiting…', ?)",
                  (kind, db.now()))
        jid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    _queue.put((jid, kind))
    return jid


def cancel():
    _current["cancel"] = True


def _update(jid, **kw):
    with db.tx() as c:
        c.execute(f"UPDATE jobs SET {','.join(f'{k}=?' for k in kw)} WHERE id=?", list(kw.values()) + [jid])


def _check_cancel():
    if _current["cancel"]:
        raise Cancelled()


def _worker():
    while True:
        jid, kind = _queue.get()
        _current.update(id=jid, cancel=False)
        _update(jid, status="running", message="Starting…")
        awake = _keep_awake()
        try:
            fn = {"sync": run_sync, "backfill": run_backfill, "upgrade": run_upgrade,
                  "move": lambda j: storage.run_move(j, _update, _check_cancel)}[kind]
            msg = fn(jid)
            _update(jid, status="done", message=msg, finished=db.now())
        except Cancelled:
            _update(jid, status="cancelled", message="Stopped", finished=db.now())
        except StopSync as e:
            _update(jid, status="error", message=str(e), finished=db.now())
        except Exception as e:
            traceback.print_exc()
            _update(jid, status="error", message=f"{type(e).__name__}: {e}"[:400], finished=db.now())
        finally:
            _current.update(id=None, cancel=False)
            if awake:
                awake.terminate()


NET_BLIPS = ("resolve", "nodename", "name or service", "connection", "timed out", "timeout", "network is unreachable",
             "temporarily unavailable", "max retries", "couldn't reach instagram")


def _backoff(job):
    """How long to wait after a failed run: Instagram pushing back = 2 hours; the Mac briefly offline = 5 minutes."""
    msg = (job[2] if len(job) > 2 else "") or ""
    low = msg.lower()
    if any(k in low for k in NET_BLIPS) and not _blocking(msg):
        return 5 * 60
    return 2 * 3600


def _scheduler():
    first = True
    while True:
        if not first:
            time.sleep(15)
        first = False
        try:
            backups = db.list_backups()
            if not backups or time.time() - backups[0]["mtime"] > 20 * 3600:
                db.backup_now()
            media.cleanup_exports()
        except Exception:
            traceback.print_exc()
        try:
            s = db.load_settings()
            if s.get("collections") or s.get("instagram_username"):
                busy = db.conn().execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                lu = db.conn().execute("SELECT COALESCE(finished, started), status, message FROM jobs WHERE kind='upgrade' ORDER BY id DESC LIMIT 1").fetchone()
                # straight on to the next round while there's work; back off two hours after a failed run
                up_ok = not lu or time.time() - (lu[0] or 0) > (_backoff(lu) if lu[1] == "error" else 30)
                todo = db.conn().execute(
                    "SELECT COUNT(*) FROM media m JOIN posts p ON p.id=m.post_id WHERE m.kind='video' AND COALESCE(m.hq,0)=0 "
                    "AND p.status='ready' AND p.deleted_at IS NULL").fetchone()[0]
                ls = db.conn().execute("SELECT COALESCE(finished, started), status, message FROM jobs WHERE kind='sync' ORDER BY id DESC LIMIT 1").fetchone()
                sync_ok = not ls or time.time() - (ls[0] or 0) > (_backoff(ls) if ls[1] == "error" else 30)
                want_sync = sync_ok and bool(feeds_incomplete())
                want_up = up_ok and bool(todo)
                if not busy and (want_sync or want_up):
                    # quality upgrades first (there are far fewer of them), then catching up on older saves
                    enqueue("upgrade" if want_up else "sync")
            hours = float(s.get("auto_sync_hours") or 0)
            if hours <= 0 or not (s.get("collections") or s.get("instagram_username")):
                continue
            row = db.conn().execute(
                "SELECT MAX(started) FROM jobs WHERE kind='sync'").fetchone()
            last = row[0] or 0
            if time.time() - last >= hours * 3600:
                enqueue("sync")
                enqueue("backfill")
                enqueue("upgrade")
        except Exception:
            traceback.print_exc()


def start():
    s0 = db.load_settings()
    if int(s0.get("quality_check") or 0) < 1:
        # earlier versions sometimes kept Instagram's small copy and called it the best: check every video again
        with db.tx() as c:
            c.execute("UPDATE media SET hq=0 WHERE kind='video'")
        db.save_settings({"quality_check": 1})
    # anything left "running" from a previous launch didn't finish
    with db.tx() as c:
        c.execute("UPDATE jobs SET status='cancelled', message='Interrupted (app closed)' "
                  "WHERE status IN ('queued','running')")
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    pending = db.load_settings().get("media_move_to")
    if pending:   # the app closed during a move: keep both places readable and finish it
        db.MEDIA_MOVING_TO = Path(pending)
        enqueue("move")


# ---------- gallery-dl setup ----------

def _configure(s):
    if not db.media_available():
        raise StopSync(f"Your originals folder isn't reachable ({db.MEDIA_DIR}). "
                       "Plug the drive in or check Dropbox is running, then sync again.")
    if not HAVE_GDL:
        raise StopSync("gallery-dl isn't installed. Run: pip install gallery-dl")
    gdl_config.clear()
    src = s.get("cookies_source") or "chrome"
    if src == "file":
        if not s.get("cookies_file"):
            raise StopSync("Cookies are set to 'file' but no cookies.txt path is set in Settings.")
        cookies = s["cookies_file"]
    else:
        cookies = [src]  # read the session straight from your browser
    gdl_config.set(("extractor",), "cookies", cookies)
    ig = ("extractor", "instagram")
    gdl_config.set(ig, "videos", True)             # include the DASH manifest so we can take the best streams
    delay = s.get("request_delay") or [6, 12]
    gdl_config.set(ig, "sleep-request", [float(delay[0]), float(delay[1])])


def _friendly(e):
    name = type(e).__name__
    text = str(e)
    low = text.lower()
    if HAVE_GDL and isinstance(e, (gdl_exc.AuthenticationError, gdl_exc.AuthorizationError)) or "login" in low:
        return ("Instagram didn't accept the login. Open instagram.com in the browser chosen in Settings, "
                "make sure you're logged in, then try again.")
    if "429" in text or "rate" in low or "challenge" in low or "checkpoint" in low:
        return ("Instagram is rate-limiting or asking for verification. Sync stopped to protect your account. "
                "Open Instagram, clear any prompt, and wait a few hours before syncing again.")
    return f"{name}: {text}"[:400]


def _feeds(s):
    feeds = []
    for col in s.get("collections") or []:
        url = (col.get("url") or "").strip()
        if url:
            feeds.append((url.split("?")[0], (col.get("name") or "").strip() or None))
    user = (s.get("instagram_username") or "").strip().lstrip("@")
    if s.get("include_uncollected") and user:
        feeds.append((f"https://www.instagram.com/{user}/saved/all-posts/", None))
    return feeds


# ---------- downloading ----------

def _download(url, dest, headers=None, tries=3):
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for attempt in range(tries):
        try:
            with requests.get(url, headers=headers or {"User-Agent": UA}, stream=True, timeout=60) as r:
                if r.status_code in (403, 404, 410):   # expired or deleted link: retrying won't help
                    r.raise_for_status()
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            if tmp.stat().st_size == 0:
                raise IOError("empty download")
            tmp.replace(dest)
            return
        except requests.HTTPError as e:
            last = e
            if e.response is not None and e.response.status_code in (403, 404, 410):
                break
        except (requests.RequestException, IOError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise last or IOError("download failed")


# ---------- best-quality video (Instagram's DASH streams) ----------

COMPAT = ("avc1", "avc3", "hvc1", "hev1")   # codecs every Mac plays natively


def _dash_streams(manifest):
    """Parse Instagram's DASH manifest -> (best video rep, best audio rep). Each rep: dict(url, w, h, bw, codecs)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(manifest)
    videos, audios = [], []
    for aset in root.iter():
        if not aset.tag.endswith("AdaptationSet"):
            continue
        a_mime = (aset.get("mimeType") or aset.get("contentType") or "")
        for rep in aset:
            if not rep.tag.endswith("Representation"):
                continue
            base = next((c.text for c in rep if c.tag.endswith("BaseURL") and c.text), None)
            if not base:
                continue
            mime = rep.get("mimeType") or a_mime
            r = {"url": base.strip(), "w": int(rep.get("width") or 0), "h": int(rep.get("height") or 0),
                 "bw": int(rep.get("bandwidth") or 0), "codecs": (rep.get("codecs") or aset.get("codecs") or "").lower()}
            if mime.startswith("video") or (r["w"] and r["h"]):
                videos.append(r)
            elif mime.startswith("audio") or "mp4a" in r["codecs"] or "opus" in r["codecs"]:
                audios.append(r)
    if not videos:
        raise ValueError("no video streams in manifest")
    best_v = max(videos, key=lambda r: (r["w"] * r["h"], r["codecs"].startswith(COMPAT), r["bw"]))
    # Instagram often has the same picture in H.264 too: take that only when it's the full resolution AND has the
    # bits to match (H.264 needs ~40% more data than VP9/AV1 for the same detail; HEVC is on par), so nothing
    # needs converting. Otherwise the sharper VP9/AV1 version is converted at a generous bitrate.
    top = best_v["w"] * best_v["h"]
    other = [r for r in videos if r["w"] * r["h"] >= top and not r["codecs"].startswith(COMPAT)]
    ref = max(other, key=lambda r: r["bw"]) if other else None     # the sharpest VP9/AV1 version, if any

    def _as_good(r):
        if not r["codecs"].startswith(COMPAT) or r["w"] * r["h"] < top:
            return False
        if not ref or not ref["bw"]:
            return True
        need = 1.0 if r["codecs"].startswith(("hvc1", "hev1")) else 1.4
        return (r["bw"] or 0) >= need * ref["bw"]
    compat = [r for r in videos if _as_good(r)]
    if ref and not compat:
        best_v = ref
    elif compat:
        best_v = max(compat, key=lambda r: (r["w"] * r["h"], r["bw"]))
    best_a = max(audios, key=lambda r: r["bw"]) if audios else None
    return best_v, best_a


def _bg_run(cmd, timeout=900):
    """ffmpeg at low priority, so converting downloads never makes the app feel slow."""
    from . import activity
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **activity.low_priority())
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffmpeg failed").strip().splitlines()[-1][:300] if (p.stderr or "").strip() else "ffmpeg failed")


def _download_dash(manifest, dest, headers=None):
    """Download the sharpest video stream + best audio and combine them into one mp4 at dest."""
    v, a = _dash_streams(manifest)
    tmp_v = dest.with_name(dest.stem + ".v.part")
    tmp_a = dest.with_name(dest.stem + ".a.part")
    out = dest.with_name(dest.stem + ".mux.mp4")
    try:
        _download(v["url"], tmp_v, headers)
        if a:
            _download(a["url"], tmp_a, headers)
        cmd = [media.FFMPEG, "-y", "-v", "error", "-i", str(tmp_v)] + (["-i", str(tmp_a)] if a else [])
        cmd += ["-map", "0:v:0"] + (["-map", "1:a:0"] if a else [])
        if v["codecs"].startswith(COMPAT):
            media.run(cmd + ["-c", "copy", "-movflags", "+faststart", str(out)], timeout=900)
        else:
            # VP9 / AV1 only: convert to H.264 so it plays and exports everywhere (Resolve included).
            # The Mac's video chip does it in a few seconds at a generous bitrate (3x the VP9 stream, which is
            # ~2x as efficient as H.264, so the copy keeps all the detail); without it, a fast x264 pass.
            rate = int(min(60e6, max(8e6, 3 * (v["bw"] or 4e6))))
            tail = ["-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)]
            try:
                _bg_run(cmd + ["-c:v", "h264_videotoolbox", "-b:v", str(rate), "-maxrate", str(int(rate * 1.5)),
                               "-bufsize", str(rate * 2), "-profile:v", "high"] + tail)
            except Exception:
                _bg_run(cmd + ["-c:v", "libx264", "-crf", "15", "-preset", "veryfast"] + tail)
        out.replace(dest)
    finally:
        for p in (tmp_v, tmp_a, out):
            p.unlink(missing_ok=True)
    return v


def _quality(path):
    """(pixels, bitrate) of a video file, for comparing two versions of the same video."""
    try:
        w, h, d = media.probe(path)
        return (w or 0) * (h or 0), (path.stat().st_size * 8 / d) if d else 0
    except Exception:
        return 0, 0


def _keep_better(new, dest):
    """Replace dest with new only if new is sharper (more pixels, or same size at a clearly higher bitrate)."""
    if not dest.exists():
        new.replace(dest)
        return True
    (np_, nb), (op, ob) = _quality(new), _quality(dest)
    if np_ > op * 1.02 or (np_ >= op * 0.98 and nb > ob * 1.1):
        new.replace(dest)
        return True
    new.unlink(missing_ok=True)
    return False


def _full_size(path, f):
    """True if a single-file video is as large as the original Instagram reports (else a smaller copy was served)."""
    ow, oh = int(f.get("width_original") or 0), int(f.get("height_original") or 0)
    if not ow or not oh:
        return True
    try:
        w, h, _ = media.probe(path)
    except Exception:
        return False
    return (w or 0) * (h or 0) >= 0.9 * ow * oh


SIGNIN = {"ok": None, "at": 0}


def _save_post(post, files, collection=None, upgrade=False, saved_at=None):
    """post: gallery-dl metadata dict; files: [(url, filedict)]. Downloads media and records the post."""
    code = post.get("post_shortcode") or post.get("shortcode")
    if not code:
        return None
    folder = db.MEDIA_DIR / code
    folder.mkdir(parents=True, exist_ok=True)
    saved, best = [], set()
    for i, (url, f) in enumerate(files):
        _check_cancel()
        headers = f.get("_http_headers") or None
        is_video = url.startswith("ytdl:") or bool(f.get("video_url"))
        if upgrade and not is_video:
            # upgrading only touches videos; keep the photo we already have
            existing = sorted(folder.glob(f"{i + 1:02d}.*"))
            if existing:
                saved.append(existing[0])
                continue
        if url.startswith("ytdl:"):
            dest = folder / f"{i + 1:02d}.mp4"
            if dest.exists() and not upgrade:
                saved.append(dest); best.add(i)
                continue
            target = dest.with_name(dest.stem + ".new.mp4") if dest.exists() else dest
            try:
                # get the best version now, so it never needs a second pass later: a hiccup gets two more tries
                for attempt in range(3):
                    try:
                        _download_dash(f["_ytdl_manifest_data"], target, headers)
                        break
                    except (Cancelled, StopSync):
                        raise
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(3 * (attempt + 1))
                best.add(i)
            except (Cancelled, StopSync):
                raise
            except Exception as e:
                print(f"[stash] best-quality download failed for {code} #{i + 1}, using the standard file: {e}", flush=True)
                fb = f.get("_fallback") or ()
                if not fb:
                    if dest.exists():
                        saved.append(dest)
                    continue
                tmp = dest.with_name(dest.stem + ".fb.mp4")
                _download(fb[0], tmp, headers)
                tmp.replace(target)
            if target != dest:
                _keep_better(target, dest)   # never swap a video for a worse copy
            saved.append(dest)
            continue
        ext = (f.get("extension") or "jpg").lower()
        dest = folder / f"{i + 1:02d}.{ext}"
        if is_video and dest.exists() and upgrade:
            tmp = dest.with_name(dest.stem + ".new." + ext)
            _download(url, tmp, headers)
            if _full_size(tmp, f):
                best.add(i)
            _keep_better(tmp, dest)
        elif not dest.exists():
            _download(url, dest, headers)
            if is_video and _full_size(dest, f):
                best.add(i)   # no DASH offered, but this file is full size: the best Instagram has
        elif is_video:
            best.add(i)
        saved.append(dest)

    date = post.get("post_date") or post.get("date")
    posted_at = int(date.timestamp()) if hasattr(date, "timestamp") else None
    with db.tx() as c:
        pid = db.upsert_post(
            c, code,
            url=f"https://www.instagram.com/p/{code}/",
            username=post.get("username") or "",
            caption=post.get("description") or "",
            posted_at=posted_at,
            saved_at=saved_at or db.now(),
            saved_src="feed",
            status="ready", error="",
        )
        # a post's caption/author from the live feed beats what the export had
        c.execute("UPDATE posts SET caption=COALESCE(NULLIF(?, ''), caption), "
                  "username=COALESCE(NULLIF(?, ''), username), posted_at=COALESCE(?, posted_at) WHERE id=?",
                  (post.get("description") or "", post.get("username") or "", posted_at, pid))
        for i, path in enumerate(saved):
            media.ingest_file(c, pid, code, i, path)
            c.execute("UPDATE media SET hq=? WHERE post_id=? AND idx=?", (1 if i in best else (-1 if upgrade else 0), pid, i))
            if upgrade:   # new file: redo the look analysis
                c.execute("DELETE FROM analysis WHERE media_id=(SELECT id FROM media WHERE post_id=? AND idx=?)", (pid, i))
        media.refresh_post_kind(c, pid)
        if collection:
            db.add_to_collection(c, pid, collection, source="instagram")
        db.reindex(c, pid)
    return code


def _iter_posts(url, cursor=None, holder=None):
    """Yield (post_meta, [(file_url, file_meta), ...]) for each post in a feed, lazily.
    cursor: start from this bookmark instead of the top. holder["ex"] lets the caller read the live bookmark."""
    gdl_config.set(("extractor", "instagram"), "cursor", cursor or True)
    ex = gdl_extractor.find(url)
    if holder is not None:
        holder["ex"] = ex
    if ex is None:
        raise StopSync(f"That doesn't look like an Instagram saved/collection URL: {url}")
    post, files = None, []
    checked = False
    for msg in ex:
        if not checked:
            checked = True
            try:   # without a signed-in session Instagram only hands out smaller videos
                SIGNIN.update(ok=any(c.name == "sessionid" and "instagram" in (c.domain or "") for c in ex.cookies), at=time.time())
            except Exception:
                pass
        if msg[0] == Message.Directory:
            if post is not None:
                yield post, files
            post, files = msg[-1], []
        elif msg[0] == Message.Url:
            files.append((msg[1], msg[2]))
    if post is not None:
        yield post, files


def _blocking(msg):
    """True if this failure means Instagram is pushing back (stop everything) rather than a one-off."""
    low = msg.lower()
    return any(k in low for k in ("rate-limiting", "didn't accept the login", "challenge", "checkpoint", "429"))


def _status(code):
    r = db.conn().execute("SELECT id, status FROM posts WHERE shortcode=?", (code,)).fetchone()
    return (r["id"], r["status"]) if r else (None, None)


def _feed_state(url):
    r = db.conn().execute("SELECT complete, cursor, seen FROM feeds WHERE url=?", (url,)).fetchone()
    return {"complete": r[0], "cursor": r[1], "seen": r[2] or 0} if r else {"complete": 0, "cursor": None, "seen": 0}


def _save_feed(url, **kw):
    st = _feed_state(url)
    st.update(kw)
    with db.tx() as c:
        c.execute("INSERT INTO feeds (url, complete, cursor, seen, updated) VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                  "complete=excluded.complete, cursor=excluded.cursor, seen=excluded.seen, updated=excluded.updated",
                  (url, st["complete"], st["cursor"], st["seen"], db.now()))


DEEP_STRETCH = 30 * 60


def _has_upgrades():
    return bool(db.conn().execute(
        "SELECT 1 FROM media m JOIN posts p ON p.id=m.post_id WHERE m.kind='video' AND COALESCE(m.hq,0)=0 "
        "AND p.status='ready' AND p.deleted_at IS NULL LIMIT 1").fetchone())


def _oldest():
    return int(db.conn().execute("SELECT MIN(COALESCE(saved_at, added_at)) FROM posts").fetchone()[0] or db.now())


def feeds_incomplete():
    s = db.load_settings()
    return [u for u, _ in _feeds(s) if not _feed_state(u)["complete"]]


def run_sync(jid):
    s = db.load_settings()
    _configure(s)
    feeds = _feeds(s)
    if not feeds:
        raise StopSync("Nothing to sync yet. Add your Instagram username and/or collection links in Settings.")
    stop_after = int(s.get("stop_after_known") or 30)
    # Instagram doesn't say when something was saved, but feeds list newest saves first:
    # stamp new posts by their position so "Newest saves" really is newest first.
    rerank = int(s.get("rank_version") or 0) < 1     # one-time full pass to fix order from older versions
    run_start = db.now()
    ranked = set()
    counts = {"new": 0, "failed": 0}
    stop = {}

    def done(tag, e):
        code, uname = tag
        if e is None:
            counts["new"] += 1
            return
        if isinstance(e, (Cancelled, StopSync)):
            stop.setdefault("e", e)
            return
        msg = _friendly(e)
        if _blocking(msg):
            stop.setdefault("e", StopSync(msg))
            return
        # one bad post shouldn't stop the whole run: park it for the backlog fetcher
        counts["failed"] += 1
        with db.tx() as c:
            db.upsert_post(c, code, username=uname, status="failed", error=msg[:300])

    pool = _Downloads(done)
    try:
        return _run_sync(jid, s, feeds, stop_after, rerank, run_start, ranked, pool, counts, stop)
    except BaseException:
        pool.abort()
        raise


def _run_sync(jid, s, feeds, stop_after, rerank, run_start, ranked, pool, counts, stop):
    paused, linked = False, 0
    for fi, (url, name) in enumerate(feeds):
        label = name or ("All saved" if "all-posts" in url else url)
        st = _feed_state(url)
        # 1) the top of the feed: brand-new saves. Once Stash has read the whole collection, it can stop as soon
        #    as it reaches posts it already has. Until then it keeps going past them (see pass 2).
        passes = [("top", None)]
        if not st["complete"]:
            passes.append(("deep", st["cursor"]))
        for mode, cursor in passes:
            known_streak, seen = 0, 0
            floor = _oldest() - 1     # older saves found deeper in the feed sort below everything known so far
            holder = {}
            last_save = deep_started = time.time()
            try:
                for post, files in _iter_posts(url, cursor, holder):
                    _check_cancel()
                    seen += 1
                    code = post.get("post_shortcode")
                    col = name or post.get("collection_name")
                    pid, status = _status(code)
                    stamp = (run_start - seen) if mode == "top" else floor - seen
                    if rerank and pid and pid not in ranked:
                        ranked.add(pid)
                        with db.tx() as c:
                            c.execute("UPDATE posts SET saved_at=? WHERE id=? AND COALESCE(saved_src,'feed')='feed'", (stamp, pid))
                    if status in ("ready", "removed"):
                        known_streak += 1
                        if col and status == "ready":
                            with db.tx() as c:
                                before = c.total_changes
                                db.add_to_collection(c, pid, col, source="instagram")
                                if c.total_changes > before:
                                    linked += 1
                                    db.reindex(c, pid)
                        # the top pass stops at saves it already has; the catch-up pass (bookmarked) does the rest,
                        # including the one-time re-ordering, so a restart never sends it back to the start
                        if mode == "top" and known_streak >= stop_after:
                            break
                    else:
                        known_streak = 0
                        if code:   # note it first, so a post still downloading when the app closes gets fetched later
                            with db.tx() as c:
                                ppid = db.upsert_post(c, code, username=post.get("username") or "", saved_at=stamp, saved_src="feed", status="pending")
                                if col:
                                    db.add_to_collection(c, ppid, col, source="instagram")
                        pool.submit((code, post.get("username") or ""), _save_post, post, files, col, saved_at=stamp)
                    pool.reap()
                    if "e" in stop:
                        raise stop["e"]
                    if mode == "deep" and time.time() - deep_started > DEEP_STRETCH and _has_upgrades():
                        # take turns with the quality upgrades: bookmark and let them run, then carry on
                        _save_feed(url, cursor=getattr(holder.get("ex"), "_cursor", None) or st["cursor"], seen=st["seen"] + seen)
                        paused = True
                        break
                    if mode == "deep" and time.time() - last_save > 10:   # bookmark, so a closed app resumes here
                        _save_feed(url, cursor=getattr(holder.get("ex"), "_cursor", None) or st["cursor"], seen=st["seen"] + seen)
                        last_save = time.time()
                    where = "catching up on older saves" if mode == "deep" else "checking for new saves"
                    _update(jid, done=fi, total=len(feeds),
                            message=f"{label}: {where}, {st['seen'] + seen if mode == 'deep' else seen} checked, {counts['new']} new so far")
                else:
                    # reached the very end of the feed: from now on, syncs only need the top
                    if mode == "deep" or not st["complete"]:
                        _save_feed(url, complete=1, cursor=None, seen=(st["seen"] + seen) if mode == "deep" else seen)
                        st["complete"] = 1
            except (Cancelled, StopSync):
                if mode == "deep":
                    _save_feed(url, cursor=getattr(holder.get("ex"), "_cursor", None) or st["cursor"], seen=st["seen"] + seen)
                raise
            except Exception as e:
                if mode == "deep":
                    _save_feed(url, cursor=getattr(holder.get("ex"), "_cursor", None) or st["cursor"], seen=st["seen"] + seen)
                raise StopSync(f"{label}: {_friendly(e)}")
            if st["complete"]:
                break
    pool.finish()
    if "e" in stop:
        raise stop["e"]
    new_total, failed_posts = counts["new"], counts["failed"]
    if rerank and all(_feed_state(u)["complete"] for u, _ in feeds):   # re-ordered all the way down
        db.save_settings({"rank_version": 1})
    msg = f"Synced {len(feeds)} feed(s): {new_total} new post(s)"
    if paused:
        msg += ". Catching up continues after a round of quality upgrades"
    if linked:
        msg += f", {linked} collection link(s) updated"
    if failed_posts:
        msg += f", {failed_posts} couldn't be downloaded and will be retried"
    return msg


_wanted = []    # posts you asked to re-download now: they jump the queue


def upgrade_posts(post_ids):
    """Re-download these posts' videos now, keeping whichever version is sharper."""
    for i in post_ids[:50]:
        if int(i) not in _wanted:
            _wanted.append(int(i))
    return enqueue("upgrade")


def run_upgrade(jid):
    """Re-download videos that were saved before Stash grabbed the best-quality streams."""
    s = db.load_settings()
    _configure(s)
    limit = int(s.get("backfill_per_run") or 150)
    rows = list(db.conn().execute(
            "SELECT DISTINCT p.id, p.shortcode FROM posts p JOIN media m ON m.post_id=p.id "
            "WHERE m.kind='video' AND COALESCE(m.hq,0)=0 AND p.status='ready' AND p.deleted_at IS NULL "
            "ORDER BY COALESCE(p.saved_at,p.added_at) DESC LIMIT ?", (limit,)).fetchall())
    asked = [r for r in (db.conn().execute("SELECT id, shortcode FROM posts WHERE id=?", (pid,)).fetchone() for pid in list(_wanted)) if r]
    _wanted.clear()
    rows = asked + [r for r in rows if r["id"] not in {a["id"] for a in asked}]
    if not rows:
        return "All videos are already in the best quality Instagram offers"
    delay = s.get("request_delay") or [6, 12]
    ok = failed = misses = 0
    res = {"ok": 0, "failed": 0}

    def done(tag, e):
        if e is None:
            res["ok"] += 1
        elif not isinstance(e, (Cancelled, StopSync)):
            res["failed"] += 1
            print(f"[stash] upgrade download failed for {tag}: {e}", flush=True)
    pool = _Downloads(done)
    try:
        ok, failed = _upgrade_loop(jid, rows, delay, pool, res)
    except BaseException:
        pool.abort()
        raise
    pool.finish()
    ok, failed = res["ok"], failed + res["failed"]
    left = db.conn().execute(
        "SELECT COUNT(DISTINCT m.post_id) FROM media m JOIN posts p ON p.id=m.post_id "
        "WHERE m.kind='video' AND COALESCE(m.hq,0)=0 AND p.status='ready' AND p.deleted_at IS NULL").fetchone()[0]
    return f"Upgraded {ok} video post(s) to best quality" + (f", {failed} unavailable" if failed else "") + (f" · {left} to go" if left else "")


def _upgrade_loop(jid, rows, delay, pool, res):
    ok = failed = misses = 0
    for i, r in enumerate(rows):
        _check_cancel()
        pool.reap()
        _update(jid, done=i, total=len(rows), message=f"Upgrading video {i + 1} of {len(rows)}")
        try:
            for post, files in _iter_posts(f"https://www.instagram.com/p/{r['shortcode']}/"):
                pool.submit(r["shortcode"], _save_post, post, files, upgrade=True)
            misses = 0
        except (Cancelled, StopSync):
            raise
        except Exception as e:
            msg = _friendly(e)
            if _blocking(msg):
                raise StopSync(msg)
            failed += 1
            low = msg.lower()
            if any(k in low for k in ("404", "not found", "unavailable", "deleted", "private", "no longer")):
                with db.tx() as c:   # the post is gone: don't keep retrying it
                    c.execute("UPDATE media SET hq=-1 WHERE post_id=? AND kind='video'", (r["id"],))
                misses = 0
            else:   # couldn't reach Instagram: leave it for next time, and stop if it keeps happening
                misses += 1
                if misses >= 3:
                    raise StopSync("Couldn't reach Instagram to upgrade videos, will try again later (" + msg[:160] + ")")
        for _ in range(int(random.uniform(float(delay[0]), float(delay[1])) * 2)):
            _check_cancel()
            time.sleep(0.5)
    return ok, failed


def run_backfill(jid):
    """Fetch media for posts we only know about from the data export (one request per post, slowly)."""
    s = db.load_settings()
    _configure(s)
    limit = int(s.get("backfill_per_run") or 150)
    rows = db.conn().execute(
        "SELECT id, shortcode FROM posts WHERE status IN ('pending','failed') AND attempts < 3 AND deleted_at IS NULL "
        + ("" if s.get("include_uncollected") else "AND id IN (SELECT post_id FROM post_collections) ") +
        "ORDER BY saved_at DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "Nothing to backfill, every post has its media"
    delay = s.get("request_delay") or [6, 12]
    ok = gone = failed = 0
    for i, r in enumerate(rows):
        _check_cancel()
        _update(jid, done=i, total=len(rows), message=f"Fetching {i + 1} of {len(rows)} (export backlog)")
        try:
            got = False
            for post, files in _iter_posts(f"https://www.instagram.com/p/{r['shortcode']}/"):
                if _save_post(post, files):
                    got = True
            if got:
                ok += 1
            else:
                raise gdl_exc.NotFoundError("post")
        except Cancelled:
            raise
        except Exception as e:
            is_gone = HAVE_GDL and isinstance(e, gdl_exc.NotFoundError)
            msg = _friendly(e)
            with db.tx() as c:
                c.execute("UPDATE posts SET status=?, error=?, attempts=attempts+1 WHERE id=?",
                          ("gone" if is_gone else "failed", "Post deleted or private" if is_gone else msg, r["id"]))
            if is_gone:
                gone += 1
            else:
                failed += 1
                if "rate-limiting" in msg or "didn't accept the login" in msg:
                    raise StopSync(msg)
        # one request per post: pace them like a person scrolling
        for _ in range(int(random.uniform(float(delay[0]), float(delay[1])) * 2)):
            _check_cancel()
            time.sleep(0.5)
    left = db.conn().execute(
        "SELECT COUNT(*) FROM posts WHERE status IN ('pending','failed') AND attempts < 3").fetchone()[0]
    return f"Backfill: {ok} downloaded, {gone} unavailable, {failed} failed · {left} still waiting"
