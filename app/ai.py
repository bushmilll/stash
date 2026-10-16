"""On-device AI (optional): visual search and content tags. Nothing leaves the Mac.

Models (ONNX, run with onnxruntime, downloaded once from Hugging Face when AI is turned on):
  * SigLIP 2 (Google, base): the current model.
  * CLIP ViT-B/16: the earlier one. Libraries scanned with it keep working on it while SigLIP 2 scans
    everything in the background, then switch over. Its scan stays on disk, so "Previous model" in
    Settings switches back instantly.

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

CLIP, SIGLIP = "clip-vit-b16", "siglip2-b16"
# Per model: where it comes from, how pictures and words are prepared for it, and how its scores read.
# "scale" turns a similarity into the model's own logits (CLIP 100; SigLIP learns its own).
# "th" are similarity cut-offs that mean the same thing for each model (matched on real photos):
#   near   two frames of the same shot          proto   close enough to count as "looks like your example"
#   q_same two searches that mean the same      coll    a search that names one of your collections
#   dedupe consecutive video frames that are practically identical (only one is kept)
MODELS = {
    CLIP: dict(label="CLIP", repo="https://huggingface.co/Xenova/clip-vit-base-patch16/resolve/main/",
               files={"vision": ("onnx/vision_model_quantized.onnx", "vision.onnx"), "text": ("onnx/text_model_quantized.onnx", "text.onnx"),
                      "tokenizer": ("tokenizer.json", "tokenizer.json")},
               mb=150, mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711],
               pad=None, lower=False, scale=100.0,
               th=dict(near=0.84, proto=0.86, q_same=0.9, coll=0.93, dedupe=1.01, drop=0.12)),
    SIGLIP: dict(label="SigLIP 2", repo="https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX/resolve/main/",
                 files={"vision": ("onnx/vision_model.onnx", "vision.onnx"), "text": ("onnx/text_model_int8.onnx", "text.onnx"),
                        "tokenizer": ("tokenizer.json", "tokenizer.json")},
                 mb=655, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5],
                 pad=64, lower=True, scale=110.0,
                 th=dict(near=0.84, proto=0.86, q_same=0.9, coll=0.93, dedupe=0.97, drop=0.12)),
}
TARGET = SIGLIP          # the model new libraries use, and older ones move to
OLD_MODEL_DIRS = [db.DATA_DIR / "models" / "clip-vit-b32"]


def model_dir(name):
    if os.environ.get("STASH_MODEL_DIR"):
        return Path(os.environ["STASH_MODEL_DIR"]) / name
    return db.DATA_DIR / "models" / name


def active():
    """The model search and tags use right now."""
    m = db.load_settings().get("ai_model")
    if m in MODELS:
        return m
    # libraries from before the upgrade were scanned with CLIP: keep using it until SigLIP has caught up
    if installed(CLIP) or db.conn().execute("SELECT 1 FROM frames LIMIT 1").fetchone():
        return CLIP
    return TARGET


def upcoming():
    """The model being scanned in the background to take over, or None."""
    s = db.load_settings()
    if s.get("ai_model_pinned") or not s.get("ai_enabled"):
        return None
    return TARGET if active() != TARGET else None


def cfg(name=None):
    return MODELS[name or active()]


def th(key, name=None):
    return cfg(name)["th"][key]
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
_state = {"downloading": False, "progress": 0.0, "error": None, "model": None}
_sessions = {}           # model -> (vision, text, tokenizer)


# ---------- model files ----------

def installed(name=None):
    name = name or active()
    return all((model_dir(name) / local).exists() for _, local in MODELS[name]["files"].values())


def ready():
    return bool(db.load_settings().get("ai_enabled")) and installed()


def _download_all(name=None):
    name = name or (upcoming() or active())
    m = MODELS[name]
    try:
        d = model_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        done = 0
        _state["model"] = name
        for key, (remote, local) in m["files"].items():
            dest = d / local
            if dest.exists():
                continue
            tmp = dest.with_suffix(dest.suffix + ".part")
            with requests.get(m["repo"] + remote, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        _state["progress"] = min(0.99, done / (m["mb"] * 1e6))
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


def start_download(name=None):
    name = name or (upcoming() or active())
    if installed(name):
        db.save_settings({"ai_enabled": True})
        return
    if _state["downloading"]:
        return
    _state.update(downloading=True, progress=0.0, error=None)
    threading.Thread(target=_download_all, args=(name,), daemon=True).start()


def ensure_current_model():
    """Fetch whatever model is missing: the one in use, then the one taking over."""
    if not db.load_settings().get("ai_enabled"):
        return
    if not installed(active()):
        start_download(active())
    elif upcoming() and not installed(upcoming()):
        start_download(upcoming())


# ---------- running the model ----------

def _load(name=None):
    name = name or active()
    with _lock:
        if name not in _sessions:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 3          # leave the Mac responsive
            d = model_dir(name)
            vis = ort.InferenceSession(str(d / "vision.onnx"), opts, providers=["CPUExecutionProvider"])
            txt = ort.InferenceSession(str(d / "text.onnx"), opts, providers=["CPUExecutionProvider"])
            _sessions[name] = (vis, txt, Tokenizer.from_file(str(d / "tokenizer.json")))
    return _sessions[name]


def _pick_output(sess, outputs, want):
    names = [o.name for o in sess.get_outputs()]
    for w in (want, "pooler_output"):
        for i, n in enumerate(names):
            if w in n:
                return outputs[i]
    for o in outputs:                 # first 2-D output (the pooled embedding, not per-token states)
        if np.asarray(o).ndim == 2:
            return o
    return outputs[0]


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


def _to_tensor(sq, name=None):
    m = cfg(name)
    sq = sq.resize((224, 224), Image.BICUBIC)
    a = (np.asarray(sq, dtype=np.float32) / 255.0 - np.array(m["mean"], np.float32)) / np.array(m["std"], np.float32)
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


def embed_batch(tensors, name=None):
    """tensors: list of (3,224,224) arrays -> (n, D) unit vectors."""
    vis = _load(name)[0]
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


def embed_image(path, crop=None, name=None):
    """Single vector for an image (crops averaged): used for content tags and tests."""
    im = _open(path, crop)
    m = embed_batch([_to_tensor(c, name) for c in _square_crops(im)], name).mean(axis=0)
    return m / (np.linalg.norm(m) + 1e-8)


def embed_text(text, name=None):
    m = cfg(name)
    _, txt, tok = _load(name)
    if m["lower"]:
        text = text.lower()
    ids = tok.encode(text).ids
    if m["pad"]:      # SigLIP reads exactly 64 tokens, padded
        pad_id = tok.token_to_id("<pad>") or 0
        ids = (ids + [pad_id] * m["pad"])[:m["pad"]]
        mask = [1 if i != pad_id else 0 for i in ids]
    else:
        ids = ids[:77]
        mask = [1] * len(ids)
    arr = np.array([ids], dtype=np.int64)
    feed = {}
    for inp in txt.get_inputs():
        if "input_ids" in inp.name:
            feed[inp.name] = arr
        elif "attention" in inp.name:
            feed[inp.name] = np.array([mask], dtype=np.int64)
        elif "position" in inp.name:
            feed[inp.name] = np.arange(arr.shape[1], dtype=np.int64)[None]
    out = txt.run(None, feed)
    v = np.asarray(_pick_output(txt, out, "text_embeds"), dtype=np.float32).reshape(-1)
    return v / (np.linalg.norm(v) + 1e-8)


def embed_query(q, name=None):
    """A few phrasings averaged: makes one-word searches like "flower" much more reliable."""
    q = q.strip()
    vs = [embed_text(t, name) for t in (q, f"a photo of {q}", f"a film still with {q}", f"a close-up of {q}")]
    m = np.mean(vs, axis=0)
    return m / (np.linalg.norm(m) + 1e-8)


# ---------- content tags ----------

_prompt_cache = {}


def _prompt_matrix():
    key = active()
    if key not in _prompt_cache:
        keys, vecs = [], []
        for k, prompts in PROMPTS.items():
            for p in prompts:
                keys.append(k)
                vecs.append(embed_text(p))
        _prompt_cache[key] = (keys, np.stack(vecs))
    return _prompt_cache[key]


_examples = {"n": -1, "vecs": None, "labels": None}


def _corrections():
    """Covers you've re-labelled by hand, as examples for the rest of the library."""
    c = db.conn()
    n = c.execute("SELECT COUNT(*) FROM analysis WHERE content_user IS NOT NULL").fetchone()[0]
    if (n, active()) != _examples["n"]:
        vecs, labels = [], []
        for mid, label in c.execute("SELECT media_id, content_user FROM analysis WHERE content_user IS NOT NULL"):
            rows = c.execute("SELECT vec FROM frames WHERE media_id=? AND model=? ORDER BY t LIMIT 3", (mid, active())).fetchall()
            if rows:
                v = np.mean([np.frombuffer(r[0], np.float16).astype(np.float32) for r in rows], axis=0)
                vecs.append(v / (np.linalg.norm(v) + 1e-8)); labels.append(label)
        _examples.update(n=(n, active()), vecs=np.stack(vecs) if vecs else None, labels=labels)
    return _examples["vecs"], _examples["labels"]


def classify(img_vec, caption=""):
    keys, mat = _prompt_matrix()
    logits = cfg()["scale"] * mat @ img_vec
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
            near = th("near")
            if sims[j] >= near:
                scores[labels[j]] = scores.get(labels[j], 0) + 0.6 * min(1.0, (float(sims[j]) - (near - 0.04)) / (1.04 - near))
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
    from . import activity
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(src), "-vf", f"fps={fps:.4f},scale=360:-2",
                    "-q:v", "4", pattern], capture_output=True, timeout=900, **activity.low_priority())
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


def scan_pending(limit=20, name=None):
    """Media not yet scanned with this model (the one in use, or the one taking over)."""
    name = name or active()
    if name == active():
        cond, args = "COALESCE(a.scan_version, 0) < ?", (SCAN_VERSION,)
    else:
        cond, args = "COALESCE(a.scan_next, '') != ?", (f"{name}:{SCAN_VERSION}",)
    return db.conn().execute(
        "SELECT m.id, m.kind, m.path, m.thumb, m.duration, m.idx, a.crop FROM media m JOIN posts p ON p.id=m.post_id "
        "LEFT JOIN analysis a ON a.media_id=m.id "
        f"WHERE p.deleted_at IS NULL AND p.status='ready' AND m.thumb IS NOT NULL AND {cond} "
        "ORDER BY COALESCE(p.saved_at, p.added_at) DESC LIMIT ?", (*args, limit)).fetchall()


def _mark_scanned(c, media_id, name):
    c.execute("INSERT INTO analysis (media_id, version) VALUES (?, 0) ON CONFLICT(media_id) DO NOTHING", (media_id,))
    if name == active():
        c.execute("UPDATE analysis SET scan_version=? WHERE media_id=?", (SCAN_VERSION, media_id))
    else:
        c.execute("UPDATE analysis SET scan_next=? WHERE media_id=?", (f"{name}:{SCAN_VERSION}", media_id))


def scan_media(r, name=None):
    """Embed the sampled frames (and crops) of one media item with this model."""
    name = name or active()
    if r["kind"] == "video":
        src = db.media_path(r["path"])
        if not src.exists():
            raise FileNotFoundError(src)
        with tempfile.TemporaryDirectory() as tmp:
            frames = _video_frames(src, r["duration"], tmp)
    else:
        src = db.media_path(r["path"]) if r["path"] else None
        if not src or not src.exists():       # originals offline: the local preview is good enough here
            src = db.THUMB_DIR / r["thumb"]
        crop = json.loads(r["crop"]) if r["crop"] else None
        frames = [(0.0, _open(src, crop))]
    tensors, times = [], []
    for t, im in frames:
        im.thumbnail((640, 640))
        for sq in _square_crops(im):
            tensors.append(_to_tensor(sq, name))
            times.append(t)
    vecs = embed_batch(tensors, name) if tensors else np.zeros((0, 8), np.float32)
    # a held shot gives near-identical frames second after second: keep the first of each run
    dd = th("dedupe", name)
    if dd < 1 and len(vecs):
        keep, last = [], None
        tarr = np.array(times)
        for t in sorted(set(times)):
            sel = np.nonzero(tarr == t)[0]
            m = vecs[sel].mean(axis=0); m = m / (np.linalg.norm(m) + 1e-8)
            if last is None or float(m @ last) < dd:
                keep.extend(sel.tolist()); last = m
        times = [times[i] for i in keep]; vecs = vecs[keep]
    with db.tx() as c:
        c.execute("DELETE FROM frames WHERE media_id=? AND model=?", (r["id"], name))
        c.executemany("INSERT INTO frames (media_id, t, vec, model) VALUES (?,?,?,?)",
                      [(r["id"], t, v.astype(np.float16).tobytes(), name) for t, v in zip(times, vecs)])
        _mark_scanned(c, r["id"], name)
    if name == active():
        _index["dirty"] = True
    return len(times)


def scan_batch():
    """Scan a few media items: new saves with the model in use first, then the backlog for the model taking over.
    Returns how many were scanned."""
    if not ready():
        return 0
    from . import activity
    nxt = upcoming()
    rows = [(r, active()) for r in scan_pending()]
    if not rows and nxt and installed(nxt):
        rows = [(r, nxt) for r in scan_pending(name=nxt)]
        if not rows:
            switch_to(nxt)     # caught up: the new model takes over
            return 1
    for r, name in rows:
        activity.wait_idle()
        try:
            scan_media(r, name)
        except Exception as e:
            if not isinstance(e, FileNotFoundError):
                traceback.print_exc()
            if not db.media_available():
                return 0               # Dropbox / drive offline: try again later
            with db.tx() as c:         # unreadable: don't retry forever
                _mark_scanned(c, r["id"], name)
    return len(rows)


def switch_to(name, pinned=False):
    """Make this model the one search and tags use. Its scan becomes the live one; the other model's scan
    stays on disk, so switching back is instant."""
    old = active()
    if name == old:
        db.save_settings({"ai_model": name, "ai_model_pinned": bool(pinned)})
        return
    with db.tx() as c:
        c.execute("INSERT INTO analysis (media_id, version) SELECT DISTINCT media_id, 0 FROM frames WHERE true "
                  "ON CONFLICT(media_id) DO NOTHING")
        # the old model's scan becomes the "other" scan; the new model's scan becomes the live one
        c.execute("UPDATE analysis SET scan_next=CASE WHEN EXISTS (SELECT 1 FROM frames f WHERE f.media_id=analysis.media_id AND f.model=?) "
                  "THEN ? ELSE NULL END", (old, f"{old}:{SCAN_VERSION}"))
        c.execute("UPDATE analysis SET scan_version=CASE WHEN EXISTS (SELECT 1 FROM frames f WHERE f.media_id=analysis.media_id AND f.model=?) "
                  "THEN ? ELSE 0 END", (name, SCAN_VERSION))
        c.execute("UPDATE analysis SET ai_version=0")      # content labels are re-read with this model (your own labels stay)
    db.save_settings({"ai_model": name, "ai_model_pinned": bool(pinned)})
    _index["dirty"] = True
    _cache.clear(); _prompt_cache.clear(); _examples["n"] = -1
    from . import learn, techniques
    learn.model_changed()
    techniques.model_changed()


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
    name = active()
    for r in rows:
        label, conf = None, 0.0
        try:
            vs = [np.frombuffer(v[0], dtype=np.float16).astype(np.float32) for v in db.conn().execute(
                "SELECT vec FROM frames WHERE media_id=? AND model=? AND t=(SELECT MIN(t) FROM frames WHERE media_id=? AND model=?)",
                (r["id"], name, r["id"], name))]
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


_retry = {"at": 0}


def work_batch():
    """One unit of background AI work; returns True if it did something."""
    if time.time() - _retry["at"] > 600 and not _state["downloading"]:   # a model download that failed earlier (offline): try again
        _retry["at"] = time.time()
        try:
            ensure_current_model()
        except Exception:
            traceback.print_exc()
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
        "JOIN posts p ON p.id=m.post_id WHERE p.deleted_at IS NULL AND p.status='ready' AND f.model=? "
        "ORDER BY f.media_id, f.t, f.id", (active(),)).fetchall()
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
    key = (q.strip().lower(), learn.version(), active())
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
    cut = max(float(np.mean(scores) + 0.6 * np.std(scores)), float(np.max(scores)) - th("drop")) if len(scores) > 5 else -1
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


def upgrade_progress():
    """(scanned with the model taking over, total) or None."""
    nxt = upcoming()
    if not nxt:
        return None
    c = db.conn()
    done = c.execute("SELECT COUNT(*) FROM analysis a JOIN media m ON m.id=a.media_id JOIN posts p ON p.id=m.post_id "
                     "WHERE p.deleted_at IS NULL AND p.status='ready' AND a.scan_next=?", (f"{nxt}:{SCAN_VERSION}",)).fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM media m JOIN posts p ON p.id=m.post_id "
                      "WHERE p.deleted_at IS NULL AND p.status='ready' AND m.thumb IS NOT NULL").fetchone()[0]
    return done, total


def counts():
    c = db.conn()
    scanned = c.execute("SELECT COUNT(*) FROM analysis a JOIN media m ON m.id=a.media_id JOIN posts p ON p.id=m.post_id "
                        "WHERE p.deleted_at IS NULL AND p.status='ready' AND a.scan_version>=?", (SCAN_VERSION,)).fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM media m JOIN posts p ON p.id=m.post_id "
                      "WHERE p.deleted_at IS NULL AND p.status='ready' AND m.thumb IS NOT NULL").fetchone()[0]
    return scanned, total


def status():
    done, total = counts()
    up = upgrade_progress()
    others = [n for n in MODELS if n != active() and n != upcoming() and installed(n) and
              db.conn().execute("SELECT 1 FROM frames WHERE model=? LIMIT 1", (n,)).fetchone()]
    return {"installed": installed(), "enabled": bool(db.load_settings().get("ai_enabled")),
            "downloading": _state["downloading"], "progress": round(_state["progress"], 3),
            "downloading_model": MODELS.get(_state.get("model") or "", {}).get("label"),
            "error": _state["error"], "scanned": done, "total": total, "tagged": done,
            "model": active(), "model_label": cfg()["label"],
            "upgrade": {"model": upcoming(), "label": MODELS[upcoming()]["label"], "done": up[0], "total": up[1],
                        "installed": installed(upcoming())} if up else None,
            "switch_to": [{"model": n, "label": MODELS[n]["label"]} for n in others]}
