"""Look at each image and describe it the way ShotDeck / FrameSet filters do — no AI, just the pixels.

Per media item (works from its thumbnail, so videos are covered by their poster frame):
  palette     5 dominant colors with their share of the frame
  tone        low key / mid key / high key     (Lab lightness)
  sat         b&w / muted / saturated          (Lab chroma)
  temp        warm / neutral / cool            (Lab b*)
  contrast    low / normal / high              (spread of lightness)
  ar          true aspect ratio after removing letterbox / pillarbox bars, plus a label (2.39:1, 16:9, 4:5 …)
  crop        the content box inside the bars, so the grid can trim them off
  vec         a small layout+color fingerprint used for "Similar shots" and "Search by image"
"""
import functools
import json
import threading
from pathlib import Path
import time
import traceback

import numpy as np
from PIL import Image, ImageOps

from . import db

VEC_W, VEC_H = 8, 6

AR_LABELS = [  # (min ratio, label) checked top-down
    (2.30, "2.39:1"), (2.05, "2.20:1"), (1.92, "2:1"), (1.80, "1.85:1"), (1.70, "16:9"),
    (1.45, "3:2"), (1.28, "4:3"), (1.12, "5:4"), (0.92, "1:1"), (0.76, "4:5"), (0.62, "2:3"), (0.0, "9:16"),
]


# ---------- color math ----------

def _srgb_to_lab(rgb):
    """rgb: float array (..., 3) in 0..1 -> Lab (..., 3)."""
    c = np.where(rgb > 0.04045, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


@functools.lru_cache(maxsize=4096)
def hex_to_lab(hx):
    hx = hx.lstrip("#")
    rgb = np.array([int(hx[i:i + 2], 16) for i in (0, 2, 4)], dtype=float) / 255
    return _srgb_to_lab(rgb)


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


# ---------- letterbox ----------

def _content_box(L):
    """Find black (or near-uniform dark) bars on the edges. L: lightness array (h, w). Returns (x0, y0, x1, y1)."""
    h, w = L.shape

    def is_bar(strip):
        return strip.mean() < 9 and strip.std() < 6

    top = 0
    while top < h * 0.3 and is_bar(L[top]):
        top += 1
    bot = h
    while bot > h * 0.7 and is_bar(L[bot - 1]):
        bot -= 1
    left = 0
    while left < w * 0.3 and is_bar(L[top:bot, left]):
        left += 1
    right = w
    while right > w * 0.7 and is_bar(L[top:bot, right - 1]):
        right -= 1
    # only trust bars that are clearly there and roughly symmetric (a dark sky isn't a letterbox)
    if top < h * 0.03 or bot > h * 0.97 or abs(top - (h - bot)) > h * 0.04:
        top, bot = 0, h
    if left < w * 0.03 or right > w * 0.97 or abs(left - (w - right)) > w * 0.04:
        left, right = 0, w
    return left, top, right, bot


def ar_label(r):
    for lo, label in AR_LABELS:
        if r >= lo:
            return label
    return "9:16"


# ---------- main ----------

def analyze_file(path):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((360, 360))
        arr = np.asarray(im, dtype=np.float32) / 255.0
    lab = _srgb_to_lab(arr)
    h, w = lab.shape[:2]
    x0, y0, x1, y1 = _content_box(lab[..., 0])
    content = lab[y0:y1, x0:x1]
    crgb = arr[y0:y1, x0:x1]
    L, A, B = content[..., 0], content[..., 1], content[..., 2]
    chroma = np.sqrt(A ** 2 + B ** 2)

    meanL, stdL, meanC = float(L.mean()), float(L.std()), float(chroma.mean())
    tone = "low" if meanL < 32 else "high" if meanL > 62 else "mid"
    p95C = float(np.percentile(chroma, 95))
    sat = "bw" if meanC < 4 and p95C < 12 else "muted" if meanC < 16 else "saturated"
    colorful = chroma > 6
    warmth = float((B[colorful].mean() + 0.35 * A[colorful].mean())) if colorful.any() else 0.0
    temp = "neutral" if sat == "bw" else "warm" if warmth > 9 else "cool" if warmth < -2 else "neutral"
    contrast = "low" if stdL < 14 else "high" if stdL > 26 else "normal"

    # palette: median-cut on the content area
    cim = Image.fromarray((crgb * 255).astype(np.uint8))
    q = cim.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[:18]
    counts = sorted(q.getcolors() or [], reverse=True)
    total = sum(c for c, _ in counts) or 1
    palette = [[_hex(pal[i * 3:i * 3 + 3]), round(c / total, 3)] for c, i in counts[:6] if c / total >= 0.02][:5]

    ratio = (x1 - x0) / max(1, (y1 - y0))
    crop = [round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)]

    # fingerprint: coarse Lab layout (composition + color), lightness weighted up
    small = Image.fromarray((crgb * 255).astype(np.uint8)).resize((VEC_W, VEC_H), Image.BILINEAR)
    slab = _srgb_to_lab(np.asarray(small, dtype=np.float32) / 255.0)
    slab[..., 0] *= 1.4
    vec = slab.astype(np.float32).ravel()

    return {"palette": palette, "tone": tone, "sat": sat, "temp": temp, "contrast": contrast,
            "ar": round(ratio, 3), "ar_label": ar_label(ratio), "crop": crop,
            "lum": round(meanL, 1), "chroma": round(meanC, 1), "vec": vec}


def analyze_media(c, media_id):
    r = c.execute("SELECT path, thumb, kind FROM media WHERE id=?", (media_id,)).fetchone()
    if not r:
        return
    src = (db.THUMB_DIR / r["thumb"]) if r["thumb"] else (db.media_path(r["path"]))
    if r["kind"] == "image" and not r["thumb"]:
        src = db.media_path(r["path"])
    res = analyze_file(src)
    c.execute(
        "INSERT INTO analysis (media_id, palette, tone, sat, temp, contrast, ar, ar_label, crop, lum, chroma, vec, version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET palette=excluded.palette, tone=excluded.tone, "
        "sat=excluded.sat, temp=excluded.temp, contrast=excluded.contrast, ar=excluded.ar, ar_label=excluded.ar_label, "
        "crop=excluded.crop, lum=excluded.lum, chroma=excluded.chroma, vec=excluded.vec, version=excluded.version",
        (media_id, json.dumps(res["palette"]), res["tone"], res["sat"], res["temp"], res["contrast"], res["ar"],
         res["ar_label"], json.dumps(res["crop"]), res["lum"], res["chroma"], res["vec"].tobytes(), VERSION))


VERSION = 1


def pending(limit=200):
    return [r[0] for r in db.conn().execute(
        "SELECT m.id FROM media m LEFT JOIN analysis a ON a.media_id=m.id "
        "WHERE a.media_id IS NULL OR a.version < ? LIMIT ?", (VERSION, limit))]


def _backfill_posters(batch=20):
    """Videos saved by older versions only have a small thumbnail: make their full-res posters."""
    from . import media
    rows = db.conn().execute(
        "SELECT m.id, m.path, m.idx, p.shortcode FROM media m JOIN posts p ON p.id=m.post_id "
        "WHERE m.kind='video' AND m.poster IS NULL LIMIT ?", (batch,)).fetchall()
    for r in rows:
        src = db.media_path(r["path"])
        poster = media.make_poster(src, f"{r['shortcode']}/{r['idx']:02d}") if src.exists() else None
        with db.tx() as c:
            c.execute("UPDATE media SET poster=? WHERE id=?", (poster or "", r["id"]))


_turn = {"n": 0}


def _worker():
    """Analyze anything new in the background: on startup (whole backlog), and after each sync adds media."""
    while True:
        try:
            _backfill_posters()
            from . import activity
            activity.wait_idle()
            ids = pending()
            # color reading and AI scanning take turns, so both move along
            turn = _turn["n"] = _turn["n"] + 1
            if not ids and turn % 2 and color_batch():
                continue
            if not ids:
                from . import ai
                try:
                    tagged = ai.work_batch()
                except Exception:
                    traceback.print_exc()
                    tagged = 0
                if not tagged and not color_batch():
                    time.sleep(10)
                continue
            for mid in ids:
                activity.wait_idle()
                try:
                    with db.tx() as c:
                        analyze_media(c, mid)
                except Exception:
                    traceback.print_exc()
                    with db.tx() as c:  # don't retry a broken file forever
                        c.execute("INSERT INTO analysis (media_id, version) VALUES (?,?) ON CONFLICT(media_id) DO UPDATE SET version=excluded.version", (mid, VERSION))
        except Exception:
            traceback.print_exc()
            time.sleep(30)


def start():
    threading.Thread(target=_worker, daemon=True).start()


# ---------- similarity ----------

_cache = {"n": -1, "ids": None, "mat": None}


def _matrix():
    """All cover-image fingerprints, cached until the analysis table changes size."""
    c = db.conn()
    n = c.execute("SELECT COUNT(*) FROM analysis WHERE vec IS NOT NULL").fetchone()[0]
    if n != _cache["n"]:
        rows = c.execute(
            "SELECT p.id, a.vec FROM posts p JOIN media m ON m.post_id=p.id AND m.idx=0 "
            "JOIN analysis a ON a.media_id=m.id WHERE a.vec IS NOT NULL AND p.deleted_at IS NULL AND p.status='ready'").fetchall()
        _cache.update(n=n, ids=np.array([r[0] for r in rows]),
                      mat=np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else None)
    return _cache["ids"], _cache["mat"]


def nearest(vec, exclude=None, k=200):
    ids, mat = _matrix()
    if mat is None:
        return []
    d = np.sqrt(((mat - vec) ** 2).mean(axis=1))
    order = np.argsort(d)
    out = [int(ids[i]) for i in order if ids[i] != exclude]
    return out[:k]


def vec_for_post(post_id):
    r = db.conn().execute(
        "SELECT a.vec FROM media m JOIN analysis a ON a.media_id=m.id WHERE m.post_id=? AND m.idx=0", (post_id,)).fetchone()
    return np.frombuffer(r[0], dtype=np.float32) if r and r[0] else None


_image_queries = {}


def remember_image_query(path):
    """Analyze an image the user dropped in and keep its fingerprint (and palette) for this session."""
    res = analyze_file(path)
    token = str(int(time.time() * 1000))
    _image_queries[token] = res
    if len(_image_queries) > 20:
        _image_queries.pop(next(iter(_image_queries)))
    return token, res


def image_query(token):
    return _image_queries.get(token)


# ---------- color profiles ----------
# Every image, every carousel slide, and ~8 moments across every video get a detailed color breakdown
# (up to 24 colors each, down to half a percent of the frame), so small accents and colors that only show up
# partway through a clip can be found. Searching a color scores each moment by how much of it is close to that
# color, and the best moment wins: that's the slide / frame the tile shows.

COLOR_VERSION = 1
COLOR_FRAMES = 8


def _frame_colors(im):
    """PIL image -> [[L, a, b, share], ...] for its content area (letterbox bars removed)."""
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((200, 200))
    arr = np.asarray(im, dtype=np.float32) / 255.0
    lab = _srgb_to_lab(arr)
    x0, y0, x1, y1 = _content_box(lab[..., 0])
    if x1 - x0 < 8 or y1 - y0 < 8:
        x0, y0, x1, y1 = 0, 0, arr.shape[1], arr.shape[0]
    cim = Image.fromarray((arr[y0:y1, x0:x1] * 255).astype(np.uint8))
    q = cim.quantize(colors=24, method=Image.Quantize.MEDIANCUT)
    pal = np.array(q.getpalette()[:72], dtype=np.float32).reshape(-1, 3) / 255.0
    counts = q.getcolors() or []
    total = sum(c for c, _ in counts) or 1
    out = []
    for c, i in sorted(counts, reverse=True):
        share = c / total
        if share < 0.004 or i >= len(pal):
            continue
        L, a, b = (float(v) for v in _srgb_to_lab(pal[i]))
        out.append([round(L, 1), round(a, 1), round(b, 1), round(share, 4)])
    return out


def _video_moments(src, duration, n=COLOR_FRAMES):
    """~n frames spread across a video -> [(t, PIL image)]."""
    import os, subprocess, tempfile
    from . import activity, media
    dur = float(duration or 0)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        if dur > 1.5:
            fps = n / dur
            subprocess.run([media.FFMPEG, "-v", "error", "-y", "-ss", f"{min(0.3, dur * 0.03):.2f}", "-i", str(src),
                            "-vf", f"fps={fps:.5f},scale=200:-2", "-frames:v", str(n), "-q:v", "5",
                            os.path.join(tmp, "%03d.jpg")], capture_output=True, timeout=600, **activity.low_priority())
        files = sorted(Path(tmp).glob("*.jpg"))
        if not files:
            subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", "scale=200:-2",
                            os.path.join(tmp, "001.jpg")], capture_output=True, timeout=120, **activity.low_priority())
            files = sorted(Path(tmp).glob("*.jpg"))
        step = (dur / n) if dur > 1.5 else 0
        for i, f in enumerate(files):
            with Image.open(f) as im:
                out.append((round(min(0.3, dur * 0.03) + i * step, 2), im.copy()))
    return out


def color_profile(r):
    """r: media row (id, kind, path, thumb, poster, duration) -> [[t, colors], ...]."""
    if r["kind"] == "video":
        src = db.media_path(r["path"]) if r["path"] else None
        if src and src.exists():
            moments = _video_moments(src, r["duration"])
            if moments:
                return [[t, _frame_colors(im)] for t, im in moments]
        alt = db.THUMB_DIR / (r["poster"] or r["thumb"] or "")
        if not alt.is_file():
            raise FileNotFoundError(src)
        with Image.open(alt) as im:
            return [[0.0, _frame_colors(im)]]
    src = db.media_path(r["path"]) if r["path"] else None
    if not src or not src.exists():
        src = db.THUMB_DIR / (r["thumb"] or "")
    with Image.open(src) as im:
        return [[0.0, _frame_colors(im)]]


def color_pending(limit=12):
    return db.conn().execute(
        "SELECT m.id, m.kind, m.path, m.thumb, m.poster, m.duration FROM media m JOIN posts p ON p.id=m.post_id "
        "LEFT JOIN analysis a ON a.media_id=m.id WHERE p.deleted_at IS NULL AND p.status='ready' AND m.thumb IS NOT NULL "
        "AND COALESCE(a.color_version, 0) < ? ORDER BY COALESCE(p.saved_at, p.added_at) DESC LIMIT ?",
        (COLOR_VERSION, limit)).fetchall()


def color_batch():
    from . import activity
    rows = color_pending()
    for r in rows:
        activity.wait_idle()
        try:
            prof = color_profile(r)
        except Exception as e:
            if not isinstance(e, FileNotFoundError):
                traceback.print_exc()
            if not db.media_available():
                return 0          # originals offline: try again later
            prof = None
        with db.tx() as c:
            c.execute("INSERT INTO analysis (media_id, version) VALUES (?, 0) ON CONFLICT(media_id) DO NOTHING", (r["id"],))
            c.execute("UPDATE analysis SET colors=?, color_version=? WHERE media_id=?",
                      (json.dumps(prof, separators=(",", ":")) if prof else None, COLOR_VERSION, r["id"]))
        _colors["dirty"] = True
    return len(rows)


_colors = {"dirty": True, "n": -1}


def _color_index():
    """Every color of every profiled moment, as flat arrays for fast matching."""
    c = db.conn()
    n = c.execute("SELECT COUNT(*) FROM analysis WHERE color_version>=? AND colors IS NOT NULL", (COLOR_VERSION,)).fetchone()[0]
    if not _colors["dirty"] and n == _colors["n"]:
        return _colors
    if "lab" in _colors and time.time() - _colors.get("at", 0) < 60:
        return _colors       # still profiling the library: refresh at most once a minute (newer ones use the cover palette meanwhile)
    lab, share, frame = [], [], []
    frames = []           # (post_id, idx, t)
    for pid, idx, data in c.execute(
            "SELECT m.post_id, m.idx, a.colors FROM analysis a JOIN media m ON m.id=a.media_id "
            "WHERE a.color_version>=? AND a.colors IS NOT NULL", (COLOR_VERSION,)):
        try:
            prof = json.loads(data)
        except Exception:
            continue
        for t, cols in prof:
            fi = len(frames)
            frames.append((pid, idx, t))
            for L, a, b, sh in cols:
                lab.append((L, a, b)); share.append(sh); frame.append(fi)
    _colors.update(dirty=False, n=n, at=time.time(),
                   lab=np.array(lab, np.float32).reshape(-1, 3), share=np.array(share, np.float32),
                   frame=np.array(frame, np.int64), frames=frames,
                   posts={f[0] for f in frames})
    return _colors


def color_matches(hex_color, candidates=None):
    """{post_id: (score, media idx, t)} for posts whose best moment features this color."""
    ix = _color_index()
    if not len(ix["share"]):
        return {}
    tL, ta, tb = (float(v) for v in hex_to_lab(hex_color))
    t_chroma = (ta ** 2 + tb ** 2) ** 0.5
    L, A, Bb = ix["lab"][:, 0], ix["lab"][:, 1], ix["lab"][:, 2]
    chroma = np.sqrt(A ** 2 + Bb ** 2)
    if t_chroma >= 10:
        # a color: hue and colorfulness matter most, lightness less (dark teal still counts as teal); greys never do
        d = np.sqrt((0.4 * (L - tL)) ** 2 + (A - ta) ** 2 + (Bb - tb) ** 2)
        w = np.clip(1 - d / 32, 0, 1) * (chroma >= 7)
        floor = 0.02
    else:
        # white / grey / black: neutral pixels near that lightness, and it has to be a real part of the frame
        d = np.sqrt((L - tL) ** 2 + (1.3 * chroma) ** 2)
        w = np.clip(1 - d / 28, 0, 1)
        floor = 0.12
    per_frame = np.bincount(ix["frame"], weights=ix["share"] * w, minlength=len(ix["frames"]))
    out = {}
    for fi in np.nonzero(per_frame >= floor)[0]:
        pid, idx, t = ix["frames"][fi]
        if candidates is not None and pid not in candidates:
            continue
        sc = float(per_frame[fi])
        if pid not in out or sc > out[pid][0]:
            out[pid] = (sc, idx, t)
    return out


def color_profiled(pid):
    return pid in _colors.get("posts", ())     # uses the index the last color_matches() built


# ---------- color matching (older, cover-only palettes) ----------

def color_score(palette_json, target_lab):
    """How strongly a frame features this color: share of the palette within reach of it."""
    try:
        pal = json.loads(palette_json or "[]")
    except Exception:
        return 0.0
    score = 0.0
    tL, ta, tb = target_lab
    t_chroma = (ta ** 2 + tb ** 2) ** 0.5
    for hx, share in pal:
        L, a, b = hex_to_lab(hx)
        if t_chroma > 12 and (a ** 2 + b ** 2) ** 0.5 < 8:
            continue  # asking for a color: greys and blacks don't count
        # hue and colorfulness matter most; lightness counts for less, so a darker magenta still matches magenta
        d = float(((0.45 * (L - tL)) ** 2 + (a - ta) ** 2 + (b - tb) ** 2) ** 0.5)
        if d < 40:
            score += share * (1 - d / 40)
    return score
