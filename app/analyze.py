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
import json
import threading
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
    src = (db.THUMB_DIR / r["thumb"]) if r["thumb"] else (db.MEDIA_DIR / r["path"])
    if r["kind"] == "image" and not r["thumb"]:
        src = db.MEDIA_DIR / r["path"]
    res = analyze_file(src)
    c.execute(
        "INSERT OR REPLACE INTO analysis (media_id, palette, tone, sat, temp, contrast, ar, ar_label, crop, lum, chroma, vec, version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (media_id, json.dumps(res["palette"]), res["tone"], res["sat"], res["temp"], res["contrast"], res["ar"],
         res["ar_label"], json.dumps(res["crop"]), res["lum"], res["chroma"], res["vec"].tobytes(), VERSION))


VERSION = 1


def pending(limit=200):
    return [r[0] for r in db.conn().execute(
        "SELECT m.id FROM media m LEFT JOIN analysis a ON a.media_id=m.id "
        "WHERE a.media_id IS NULL OR a.version < ? LIMIT ?", (VERSION, limit))]


def _worker():
    """Analyze anything new in the background: on startup (whole backlog), and after each sync adds media."""
    while True:
        try:
            ids = pending()
            if not ids:
                time.sleep(20)
                continue
            for mid in ids:
                try:
                    with db.tx() as c:
                        analyze_media(c, mid)
                except Exception:
                    traceback.print_exc()
                    with db.tx() as c:  # don't retry a broken file forever
                        c.execute("INSERT OR REPLACE INTO analysis (media_id, version, tone) VALUES (?,?,?)", (mid, VERSION, None))
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


# ---------- color matching ----------

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
