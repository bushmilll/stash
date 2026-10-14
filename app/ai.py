"""On-device AI tags (optional): what kind of thing a save shows.

Uses CLIP (ViT-B/32, ONNX, quantized) running locally with onnxruntime. Nothing leaves the Mac; the model
(about 150 MB) is downloaded once from Hugging Face when the user turns AI tags on.

Each save's cover image is labelled as one of:
  film      a film/video frame or photograph used as visual reference
  talking   someone talking to camera: vlog, interview, podcast, tutorial
  bts       behind the scenes: crew, camera rigs, lighting setups, sets
  gear      camera equipment, lenses, products, spec sheets
  text      mostly text or graphics: screenshots, infographics, quotes
The caption nudges the result (e.g. "BTS", "gear review", "tutorial").
"""
import json
import re
import threading
import time
import traceback

import numpy as np
import requests
from PIL import Image, ImageOps

from . import db

AI_VERSION = 1
REPO = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
FILES = {
    "vision": ("onnx/vision_model_quantized.onnx", "vision.onnx"),
    "text": ("onnx/text_model_quantized.onnx", "text.onnx"),
    "tokenizer": ("tokenizer.json", "tokenizer.json"),
}
import os
from pathlib import Path
MODEL_DIR = Path(os.environ["STASH_MODEL_DIR"]) if os.environ.get("STASH_MODEL_DIR") else db.DATA_DIR / "models" / "clip-vit-b32"

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


def _download_all():
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        sizes = {}
        total_done = 0
        for key, (remote, local) in FILES.items():
            dest = MODEL_DIR / local
            if dest.exists():
                continue
            tmp = dest.with_suffix(dest.suffix + ".part")
            with requests.get(REPO + remote, stream=True, timeout=60) as r:
                r.raise_for_status()
                size = int(r.headers.get("content-length") or 0)
                sizes[key] = size
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        total_done += len(chunk)
                        _state["progress"] = min(0.99, total_done / 160e6)
            tmp.replace(dest)
        _state["progress"] = 1.0
        db.save_settings({"ai_enabled": True})
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


# ---------- running the model ----------

def _load():
    with _lock:
        if _state["sessions"] is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2          # leave the Mac responsive
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


def _pixels(path, crop=None):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if crop:   # drop letterbox bars found by the look analysis
            w, h = im.size
            box = (int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h))
            if box[2] - box[0] > 16 and box[3] - box[1] > 16:
                im = im.crop(box)
        w, h = im.size
        s = 224 / min(w, h)
        im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
        w, h = im.size
        l, t = (w - 224) // 2, (h - 224) // 2
        im = im.crop((l, t, l + 224, t + 224))
        a = (np.asarray(im, dtype=np.float32) / 255.0 - MEAN) / STD
    return a.transpose(2, 0, 1)[None]


def embed_image(path, crop=None):
    vis, _ = _load()
    feed = {vis.get_inputs()[0].name: _pixels(path, crop)}
    out = vis.run(None, feed)
    v = np.asarray(_pick_output(vis, out, "image_embeds"), dtype=np.float32).reshape(-1)
    return v / (np.linalg.norm(v) + 1e-8)


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
    total = sum(scores.values())
    scores = {k: v / total for k, v in scores.items()}
    best = max(scores, key=scores.get)
    return best, scores


# ---------- background tagging ----------

def pending(limit=50):
    return db.conn().execute(
        "SELECT m.id, m.thumb, a.crop, p.caption FROM media m JOIN posts p ON p.id=m.post_id "
        "JOIN analysis a ON a.media_id=m.id "
        "WHERE m.idx=0 AND m.thumb IS NOT NULL AND p.deleted_at IS NULL AND a.version >= 1 "
        "AND COALESCE(a.ai_version,0) < ? ORDER BY COALESCE(p.saved_at,p.added_at) DESC LIMIT ?", (AI_VERSION, limit)).fetchall()


def counts():
    c = db.conn()
    done = c.execute("SELECT COUNT(*) FROM analysis a JOIN media m ON m.id=a.media_id JOIN posts p ON p.id=m.post_id "
                     "WHERE m.idx=0 AND p.deleted_at IS NULL AND a.ai_version>=?", (AI_VERSION,)).fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM media m JOIN posts p ON p.id=m.post_id "
                      "WHERE m.idx=0 AND m.thumb IS NOT NULL AND p.deleted_at IS NULL").fetchone()[0]
    return done, total


def tag_batch():
    """Tag a batch of covers. Called from the analysis worker; returns how many were tagged."""
    if not (db.load_settings().get("ai_enabled") and installed()):
        return 0
    rows = pending()
    for r in rows:
        try:
            crop = json.loads(r["crop"]) if r["crop"] else None
            vec = embed_image(db.THUMB_DIR / r["thumb"], crop)
            label, scores = classify(vec, r["caption"])
            conf = scores[label]
        except Exception:
            traceback.print_exc()
            vec, label, conf = None, None, 0.0
        with db.tx() as c:
            c.execute("UPDATE analysis SET content=?, content_conf=?, clip=?, ai_version=? WHERE media_id=?",
                      (label, round(conf, 3), vec.astype(np.float16).tobytes() if vec is not None else None, AI_VERSION, r["id"]))
    return len(rows)


def status():
    done, total = counts()
    return {"installed": installed(), "enabled": bool(db.load_settings().get("ai_enabled")),
            "downloading": _state["downloading"], "progress": round(_state["progress"], 3),
            "error": _state["error"], "tagged": done, "total": total}
