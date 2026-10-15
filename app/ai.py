"""On-device AI (optional): visual search and content tags. Nothing leaves the Mac.

Model: CLIP ViT-B/16 (ONNX, quantized) run with onnxruntime. Downloaded once (~150 MB) from Hugging Face
when the user turns AI on.

What it does
  * Scans every image (all carousel items) and about one frame per second of every video. Tall and wide
    frames are looked at as two overlapping squares, so nothing at the edges is missed.
  * Search: a typed query ("flower", "red neon at night") is compared with every scanned frame; each post
    is scored by its best frame, and the moment of that frame is remembered so the tile can show it.
  * Content tags for the cover: film / talking / bts / gear / text, nudged by caption hints.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageOps

from . import db

AI_VERSION = 3          # content tags (bump when prompts/model change)
SCAN_VERSION = 1        # frame scanning (bump when sampling/model changes)
MODEL_NAME = "clip-vit-b16"
REPO = "https://huggingface.co/Xenova/clip-vit-base-patch16/resolve/main/"
FILES = {
    "vision": ("onnx/vision_model_quantized.onnx", "vision.onnx"),
    "text": ("onnx/text_model_quantized.onnx", "text.onnx"),
    "tokenizer": ("tokenizer.json", "tokenizer.json"),
}
MODEL_DIR = Path(os.environ["STASH_MODEL_DIR"]) if os.environ.get("STASH_MODEL_DIR") else db.DATA_DIR / "models" / MODEL_NAME
OLD_MODEL_DIRS = [db.DATA_DIR / "models" / "clip-vit-b32"]
MAX_FRAMES = 90          # per video; longer videos are sampled more sparsely

LABELS = {
    "film": "Film frames",
    "talking": "Talking to camera",
    "bts": "Behind the scenes",
    "gear": "Gear & products",
    "text": "Text & graphics",
}
PROMPTS = {
    "film": [
        "a cinematic film still", "a frame from a movie", "a beautifully lit cinematic shot",
        "a still from a music video", "a moody photograph with dramatic lighting", "a wide establishing shot of a landscape",
        "a film still of an actor in a scene", "a fashion photograph", "an architectural interior photograph",
    ],
    "talking": [
        "a person talking to the camera", "a vlogger speaking to the camera", "a youtuber filming themselves in their room",
        "a podcast interview with microphones", "a talking head video", "a person explaining something to the camera",
        "a selfie video of someone speaking",
    ],
    "bts": [
        "behind the scenes on a film set", "a film crew working with a cinema camera", "a camera operator filming a scene",
        "a director on set talking to the crew", "lighting equipment and stands on a film set", "a camera rig on a dolly",
    ],
    "gear": [
        "a photo of a camera body on a table", "a product shot of a camera lens", "camera equipment laid out",
        "a review of camera gear", "a cinema camera with accessories", "a product photo of an electronic device",
    ],
    "text": [
        "a screenshot with text", "an infographic", "a slide with lots of text", "a quote in large text",
        "a chart with numbers", "a user interface screenshot", "a text post with a plain background",
        "a list of tips written in text", "words and numbers on a plain background", "an instagram carousel slide with text",
        "a page of written instructions", "a graphic with a numbered list",
    ],
}
CAPTION_HINTS = {
    "bts": r"\b(bts|behind[- ]the[- ]scenes|on set|set life|crew|gaffer|grip|dop on set)\b",
    "gear": r"\b(gear|camera bag|lens(es)?|review|unboxing|specs?|sensor|fx3|fx6|fx9|a7s|komodo|alexa|red raptor|blackmagic|bmpcc|gimbal|rig)\b",
    "talking": r"\b(tips|tutorial|how (i|to)|podcast|interview|story ?time|let me tell|advice|lesson|masterclass)\b",
    "text": r"\b(cheat ?sheet|infographic|thread|save this|swipe)\b",
}

_lock = threading.Lock()
_state = {"downloading": False, "progress": 0.0, "error": None, "sessions": None, "tokenizer": None}


# ---------- model files ----------

def installed():
    return all((MODEL_DIR / local).exists() for _, local in FILES.values())


def ready():
    return bool(db.load_settings().get("ai_enabled")) and installed()


def _download_all():
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        done = 0
        for key, (remote, local) in FILES.items():
            dest = MODEL_DIR / local
            if dest.exists():
                continue
            tmp = dest.with_suffix(dest.suffix + ".part")
            with requests.get(REPO + remote, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        _state["progress"] = min(0.99, done / 160e6)
            tmp.replace(dest)
        _state["progress"] = 1.0
        db.save_settings({"ai_enabled": True})
        for old in OLD_MODEL_DIRS:          # free the space used by the previous, smaller model
            shutil.rmtree(old, ignore_errors=True)
    except Exception as e:
        traceback.print_exc()
        _state["error"] = f"Couldn't download the AI model: {e}"
    finally:
        _state["downloading"] = False


def start_download():
    if installed():
        db.save_settings({"ai_enabled": True})
        return
    if _state["downloading"]:
        return
    _state.update(downloading=True, progress=0.0, error=None)
    threading.Thread(target=_download_all, daemon=True).start()


def ensure_current_model():
    """People who turned AI on with the previous model get the sharper one automatically."""
    if db.load_settings().get("ai_enabled") and not installed():
        start_download()


# ---------- running the model ----------

def _load():
    with _lock:
        if _state["sessions"] is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 3          # leave the Mac responsive
            vis = ort.InferenceSession(str(MODEL_DIR / "vision.onnx"), opts, providers=["CPUExecutionProvider"])
            txt = ort.InferenceSession(str(MODEL_DIR / "text.onnx"), opts, providers=["CPUExecutionProvider"])
            _state["sessions"] = (vis, txt)
            _state["tokenizer"] = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    return _state["sessions"]


def _pick_output(sess, outputs, want):
    names = [o.name for o in sess.get_outputs()]
    for i, n in enumerate(names):
        if want in n:
            return outputs[i]
    return outputs[0]


MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


def _square_crops(im):
    """One centered square for normal frames; two overlapping squares for tall (reels) or wide (2.39) frames."""
    w, h = im.size
    r = w / h
    if r < 0.72:          # tall: top and bottom
        s = w
        return [im.crop((0, 0, s, s)), im.crop((0, h - s, s, h))]
    if r > 1.45:          # wide: left and right
        s = h
        return [im.crop((0, 0, s, s)), im.crop((w - s, 0, w, s))]
    s = min(w, h)
    l, t = (w - s) // 2, (h - s) // 2
    return [im.crop((l, t, l + s, t + s))]


def _to_tensor(sq):
    sq = sq.resize((224, 224), Image.BICUBIC)
    a = (np.asarray(sq, dtype=np.float32) / 255.0 - MEAN) / STD
    return a.transpose(2, 0, 1)


def _open(path, crop=None):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
    if crop:   # drop letterbox bars found by the look analysis
        w, h = im.size
        box = (int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h))
        if box[2] - box[0] > 16 and box[3] - box[1] > 16:
            im = im.crop(box)
    return im


def embed_batch(tensors):
    """tensors: list of (3,224,224) arrays -> (n, 512) unit vectors."""
    vis, _ = _load()
    name = vis.get_inputs()[0].name
    out_rows = []
    for i in range(0, len(tensors), 16):
        chunk = np.stack(tensors[i:i + 16]).astype(np.float32)
        try:
            out = vis.run(None, {name: chunk})
            rows = np.asarray(_pick_output(vis, out, "image_embeds"), dtype=np.float32).reshape(len(chunk), -1)
        except Exception:          # model exported without a batch dimension: one at a time
            rows = np.concatenate([np.asarray(_pick_output(vis, vis.run(None, {name: c[None]}), "image_embeds"),
                                              dtype=np.float32).reshape(1, -1) for c in chunk])
        out_rows.append(rows)
    m = np.concatenate(out_rows)
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-8)


def embed_image(path, crop=None):
    """Single vector for an image (crops averaged): used for content tags and tests."""
    im = _open(path, crop)
    m = embed_batch([_to_tensor(c) for c in _square_crops(im)]).mean(axis=0)
    return m / (np.linalg.norm(m) + 1e-8)


def embed_text(text):
    _, txt = _load()
    ids = _state["tokenizer"].encode(text).ids[:77]
    arr = np.array([ids], dtype=np.int64)
    feed = {}
    for inp in txt.get_inputs():
        if "input_ids" in inp.name:
            feed[inp.name] = arr
        elif "attention" in inp.name:
            feed[inp.name] = np.ones_like(arr)
    out = txt.run(None, feed)
    v = np.asarray(_pick_output(txt, out, "text_embeds"), dtype=np.float32).reshape(-1)
    return v / (np.linalg.norm(v) + 1e-8)


def embed_query(q):
    """A few phrasings averaged: makes one-word searches like "flower" much more reliable."""
    q = q.strip()
    vs = [embed_text(t) for t in (q, f"a photo of {q}", f"a film still with {q}", f"a close-up of {q}")]
    m = np.mean(vs, axis=0)
    return m / (np.linalg.norm(m) + 1e-8)


# ---------- content tags ----------

_prompt_cache = {}


def _prompt_matrix():
    if "m" not in _prompt_cache:
        keys, vecs = [], []
        for k, prompts in PROMPTS.items():
            for p in prompts:
                keys.append(k)
                vecs.append(embed_text(p))
        _prompt_cache["m"] = (keys, np.stack(vecs))
    return _prompt_cache["m"]


_examples = {"n": -1, "vecs": None, "labels": None}


def _corrections():
    """Covers you've re-labelled by hand, as examples for the rest of the library."""
    c = db.conn()
    n = c.execute("SELECT COUNT(*) FROM analysis WHERE content_user IS NOT NULL").fetchone()[0]
    if n != _examples["n"]:
        vecs, labels = [], []
        for mid, label in c.execute("SELECT media_id, content_user FROM analysis WHERE content_user IS NOT NULL"):
            rows = c.execute("SELECT vec FROM frames WHERE media_id=? ORDER BY t LIMIT 3", (mid,)).fetchall()
            if rows:
                v = np.mean([np.frombuffer(r[0], np.float16).astype(np.float32) for r in rows], axis=0)
                vecs.append(v / (np.linalg.norm(v) + 1e-8)); labels.append(label)
        _examples.update(n=n, vecs=np.stack(vecs) if vecs else None, labels=labels)
    return _examples["vecs"], _examples["labels"]


def classify(img_vec, caption=""):
    keys, mat = _prompt_matrix()
    logits = 100.0 * mat @ img_vec
    p = np.exp(logits - logits.max())
    p /= p.sum()
    scores = {k: 0.0 for k in PROMPTS}
    for k, v in zip(keys, p):
        scores[k] += float(v)
    cap = (caption or "").lower()
    for k, rx in CAPTION_HINTS.items():
        if re.search(rx, cap):
            scores[k] += 0.25           # a nudge, not an override
    vecs, labels = _corrections()
    if vecs is not None:          # posts that look like ones you've corrected follow your call
        sims = vecs @ img_vec
        for j in np.argsort(-sims)[:5]:
            if sims[j] >= 0.84:
                scores[labels[j]] = scores.get(labels[j], 0) + 0.6 * (float(sims[j]) - 0.8) / 0.2
    total = sum(scores.values())
    scores = {k: v / total for k, v in scores.items()}
    best = max(scores, key=scores.get)
    return best, scores


# ---------- scanning (background) ----------

def _video_frames(src, duration, tmpdir):
    """About one frame a second (sparser for long videos). Returns [(t, PIL image)]."""
    from . import media
    dur = float(duration or 0) or 1.0
    fps = 1.0 if dur <= MAX_FRAMES else MAX_FRAMES / dur
    pattern = os.path.join(tmpdir, "%04d.jpg")
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(src), "-vf", f"fps={fps:.4f},scale=360:-2",
                    "-q:v", "4", pattern], capture_output=True, timeout=900)
    files = sorted(Path(tmpdir).glob("*.jpg"))
    if not files:   # very short or odd clip: take the first frame
        subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", "scale=360:-2",
                        os.path.join(tmpdir, "0001.jpg")], capture_output=True, timeout=120)
        files = sorted(Path(tmpdir).glob("*.jpg"))
    out = []
    for i, f in enumerate(files):
        with Image.open(f) as im:
            out.append((round(i / fps, 2), im.convert("RGB").copy()))
    return out


def scan_pending(limit=20):
    return db.conn().execute(
        "SELECT m.id, m.kind, m.path, m.thumb, m.duration, m.idx, a.crop FROM media m JOIN posts p ON p.id=m.post_id "
        "LEFT JOIN analysis a ON a.media_id=m.id "
        "WHERE p.deleted_at IS NULL AND p.status='ready' AND m.thumb IS NOT NULL "
        "AND COALESCE(a.scan_version, 0) < ? ORDER BY COALESCE(p.saved_at, p.added_at) DESC LIMIT ?",
        (SCAN_VERSION, limit)).fetchall()


def scan_media(r):
    """Embed every sampled frame (and crop) of one media item."""
    rows = []
    if r["kind"] == "video":
        src = db.media_path(r["path"])
        if not src.exists():
            raise FileNotFoundError(src)
        with tempfile.TemporaryDirectory() as tmp:
            frames = _video_frames(src, r["duration"], tmp)
    else:
        src = db.media_path(r["path"]) if r["path"] else None
        if not src or not src.exists():       # originals offline: the local preview is good enough for CLIP
            src = db.THUMB_DIR / r["thumb"]
        crop = json.loads(r["crop"]) if r["crop"] else None
        frames = [(0.0, _open(src, crop))]
    tensors, times = [], []
    for t, im in frames:
        im.thumbnail((640, 640))
        for sq in _square_crops(im):
            tensors.append(_to_tensor(sq))
            times.append(t)
    vecs = embed_batch(tensors) if tensors else np.zeros((0, 512), np.float32)
    with db.tx() as c:
        c.execute("DELETE FROM frames WHERE media_id=?", (r["id"],))
        c.executemany("INSERT INTO frames (media_id, t, vec) VALUES (?,?,?)",
                      [(r["id"], t, v.astype(np.float16).tobytes()) for t, v in zip(times, vecs)])
        c.execute("INSERT INTO analysis (media_id, version) VALUES (?, 0) ON CONFLICT(media_id) DO NOTHING", (r["id"],))
        c.execute("UPDATE analysis SET scan_version=? WHERE media_id=?", (SCAN_VERSION, r["id"]))
    _index["dirty"] = True
    return len(times)


def scan_batch():
    """Scan a few media items. Called from the analysis worker; returns how many were scanned."""
    if not ready():
        return 0
    rows = scan_pending()
    for r in rows:
        try:
            scan_media(r)
        except Exception as e:
            if not isinstance(e, FileNotFoundError):
                traceback.print_exc()
            if not db.media_available():
                return 0               # Dropbox / drive offline: try again later
            with db.tx() as c:         # unreadable: don't retry forever
                c.execute("INSERT INTO analysis (media_id, version) VALUES (?, 0) ON CONFLICT(media_id) DO NOTHING", (r["id"],))
                c.execute("UPDATE analysis SET scan_version=? WHERE media_id=?", (SCAN_VERSION, r["id"]))
    return len(rows)


def tag_pending(limit=50):
    return db.conn().execute(
        "SELECT m.id, a.crop, p.caption FROM media m JOIN posts p ON p.id=m.post_id JOIN analysis a ON a.media_id=m.id "
        "WHERE m.idx=0 AND p.deleted_at IS NULL AND COALESCE(a.scan_version,0) >= 1 "
        "AND COALESCE(a.ai_version,0) < ? LIMIT ?", (AI_VERSION, limit)).fetchall()


def tag_batch():
    """Content tags for covers, from their first scanned frame. Returns how many were tagged."""
    if not ready():
        return 0
    rows = tag_pending()
    for r in rows:
        label, conf = None, 0.0
        try:
            vs = [np.frombuffer(v[0], dtype=np.float16).astype(np.float32) for v in db.conn().execute(
                "SELECT vec FROM frames WHERE media_id=? AND t=(SELECT MIN(t) FROM frames WHERE media_id=?)", (r["id"], r["id"]))]
            if vs:
                v = np.mean(vs, axis=0); v /= (np.linalg.norm(v) + 1e-8)
                label, scores = classify(v, r["caption"])
                conf = scores[label]
        except Exception:
            traceback.print_exc()
        with db.tx() as c:
            c.execute("UPDATE analysis SET content=?, content_conf=?, ai_version=? WHERE media_id=?",
                      (label, round(conf, 3), AI_VERSION, r["id"]))
    return len(rows)


def work_batch():
    """One unit of background AI work; returns True if it did something."""
    if scan_batch() or tag_batch():
        return True
    if not ready():
        return False
    from . import techniques
    return bool(techniques.work_batch())


# ---------- search ----------

_index = {"dirty": True, "mat": None, "media": None, "t": None, "post": None, "idx": None}
_cache = OrderedDict()


def _load_index():
    if not _index["dirty"] and _index["mat"] is not None:
        return _index
    rows = db.conn().execute(
        "SELECT f.vec, f.media_id, f.t, m.post_id, m.idx FROM frames f JOIN media m ON m.id=f.media_id "
        "JOIN posts p ON p.id=m.post_id WHERE p.deleted_at IS NULL AND p.status='ready' ORDER BY f.media_id, f.t, f.id").fetchall()
    if rows:
        _index.update(
            mat=np.frombuffer(b"".join(r[0] for r in rows), dtype=np.float16).reshape(len(rows), -1),
            media=np.array([r[1] for r in rows]), t=np.array([r[2] for r in rows], dtype=np.float32),
            post=np.array([r[3] for r in rows]), idx=np.array([r[4] for r in rows]))
    else:
        _index.update(mat=None)
    _index["dirty"] = False
    _cache.clear()
    return _index


def search(q):
    """[(post_id, score, media_idx, t)] best first; broad, with looser matches at the bottom."""
    from . import learn
    ix = _load_index()
    key = (q.strip().lower(), learn.version())
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    if ix["mat"] is None:
        return []
    qv = embed_query(q).astype(np.float32)
    try:
        qv, bonus, hidden = learn.personalize(q, qv)
    except Exception:
        traceback.print_exc()
        bonus, hidden = {}, set()
    sims = np.empty(len(ix["mat"]), dtype=np.float32)
    for i in range(0, len(sims), 50000):
        sims[i:i + 50000] = ix["mat"][i:i + 50000].astype(np.float32) @ qv
    # best frame per post
    order = np.argsort(-sims)
    best = {}
    for j in order:
        pid = int(ix["post"][j])
        if pid not in best and pid not in hidden:
            best[pid] = (float(sims[j]) + bonus.get(pid, 0.0), int(j))
    scores = np.array([v[0] for v in best.values()])
    # broad cutoff: anything clearly above this library's typical score for the query
    cut = max(float(np.mean(scores) + 0.6 * np.std(scores)), float(np.max(scores)) - 0.12) if len(scores) > 5 else -1
    kept = sorted(((s, j, pid) for pid, (s, j) in best.items() if s >= cut), reverse=True)[:1500]
    out = []
    for s, j, pid in kept:
        # step back to where this matching stretch begins: the moment it appears
        mid = ix["media"][j]
        lo = j
        while lo > 0 and ix["media"][lo - 1] == mid:
            lo -= 1
        hi = j
        while hi + 1 < len(sims) and ix["media"][hi + 1] == mid:
            hi += 1
        per_t = {}                      # best score at each moment (tall/wide frames have two crops)
        for k in range(lo, hi + 1):
            tk = float(ix["t"][k])
            per_t[tk] = max(per_t.get(tk, -1.0), float(sims[k]))
        times = sorted(per_t)
        pos = times.index(float(ix["t"][j]))
        while pos > 0 and per_t[times[pos - 1]] >= float(sims[j]) - 0.03:
            pos -= 1
        out.append((pid, s, int(ix["idx"][j]), times[pos]))
    _cache[key] = out[:1500]
    if len(_cache) > 30:
        _cache.popitem(last=False)
    return _cache[key]


def counts():
    c = db.conn()
    scanned = c.execute("SELECT COUNT(*) FROM analysis a JOIN media m ON m.id=a.media_id JOIN posts p ON p.id=m.post_id "
                        "WHERE p.deleted_at IS NULL AND p.status='ready' AND a.scan_version>=?", (SCAN_VERSION,)).fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM media m JOIN posts p ON p.id=m.post_id "
                      "WHERE p.deleted_at IS NULL AND p.status='ready' AND m.thumb IS NOT NULL").fetchone()[0]
    return scanned, total


def status():
    done, total = counts()
    return {"installed": installed(), "enabled": bool(db.load_settings().get("ai_enabled")),
            "downloading": _state["downloading"], "progress": round(_state["progress"], 3),
            "error": _state["error"], "scanned": done, "total": total, "tagged": done}
