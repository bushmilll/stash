"""Technique tags, in the spirit of visual technique libraries: framing, angle, look and camera movement.

  * Framing / angle / look come from the AI model, comparing the frames already scanned for search with
    short descriptions of each technique (nothing new is decoded for these).
  * Camera movement is measured from the video itself: a quick low-res pass at ~6 fps, tracking how the
    picture shifts between frames (phase correlation in a grid of blocks). Locked-off, pan, tilt,
    zoom/push, whip pan and handheld are reliable enough; dolly vs zoom and truck vs pan are not, so
    those are folded together rather than guessed.
  * Learned: when you add a tag by hand, shots that look like your examples get it too ("learned").
    Removing a tag ("no") keeps it off that post for good and counts against look-alikes.

Stored in `techniques` (media_id, tag, score, t, source). source: ai / motion / learned / user / no.
"""
import json
import subprocess
import threading
import time
import traceback

import numpy as np

from . import db

TECH_VERSION = 4

# key: (label, group). Frame Size, Shot Type and Composition use ShotDeck's names.
TAGS = {
    "ews": ("Extreme Wide", "Frame Size"), "ws": ("Wide", "Frame Size"), "mws": ("Medium Wide", "Frame Size"),
    "ms": ("Medium", "Frame Size"), "mcu": ("Medium Close Up", "Frame Size"), "cu": ("Close Up", "Frame Size"),
    "ecu": ("Extreme Close Up", "Frame Size"),
    "aerial": ("Aerial", "Shot Type"), "overhead": ("Overhead", "Shot Type"), "high_angle": ("High angle", "Shot Type"),
    "low_angle": ("Low angle", "Shot Type"), "dutch": ("Dutch angle", "Shot Type"), "establishing": ("Establishing shot", "Shot Type"),
    "ots": ("Over the shoulder", "Shot Type"), "single": ("Clean single", "Shot Type"), "two_shot": ("2 shot", "Shot Type"),
    "three_shot": ("3 shot", "Shot Type"), "group": ("Group shot", "Shot Type"), "insert": ("Insert", "Shot Type"),
    "center": ("Center", "Composition"), "left_heavy": ("Left heavy", "Composition"), "right_heavy": ("Right heavy", "Composition"),
    "balanced": ("Balanced", "Composition"), "symmetrical": ("Symmetrical", "Composition"), "short_side": ("Short side", "Composition"),
    "locked": ("Locked-off", "Movement"), "pan": ("Pan", "Movement"), "tilt": ("Tilt", "Movement"),
    "zoom": ("Zoom / push", "Movement"), "whip": ("Whip pan", "Movement"), "handheld": ("Handheld", "Movement"),
    "silhouette": ("Silhouette", "Look"), "shallow": ("Shallow focus", "Look"), "fisheye": ("Fisheye", "Look"),
    "ultra_wide": ("Ultra wide", "Look"), "underwater": ("Underwater", "Look"), "haze": ("Haze", "Look"),
    "hard_light": ("Hard light", "Look"), "halation": ("Halation", "Look"), "reflections": ("Reflections", "Look"),
    "spotlight": ("Spotlight", "Look"), "night_vision": ("Night vision", "Look"), "thermal": ("Thermal", "Look"),
    "motion_blur": ("Motion blur", "Look"), "double_exposure": ("Double exposure", "Look"),
    "split_screen": ("Split screen", "Look"), "typography": ("Typography", "Look"), "tilt_shift": ("Tilt shift", "Look"),
    "vintage": ("Vintage", "Look"),
}
GROUPS = ["Frame Size", "Shot Type", "Composition", "Movement", "Look"]
# tags from earlier versions -> today's names (your own tags carry over)
RENAMED = {"closeup": "cu", "wide": "ws", "two_shot": "two_shot", "central": "center"}

# words people type that mean a tag (search "whip pan" -> whip)
SYNONYMS = {
    "extreme wide": "ews", "extreme wide shot": "ews", "ews": "ews", "extreme long shot": "ews",
    "wide": "ws", "wide shot": "ws", "ws": "ws", "long shot": "ws", "full shot": "ws",
    "medium wide": "mws", "medium wide shot": "mws", "cowboy shot": "mws", "medium long shot": "mws",
    "medium": "ms", "medium shot": "ms", "mid shot": "ms",
    "medium close up": "mcu", "medium close-up": "mcu", "mcu": "mcu",
    "close up": "cu", "close-up": "cu", "closeup": "cu", "cu": "cu",
    "extreme close up": "ecu", "extreme close-up": "ecu", "ecu": "ecu", "macro": "ecu",
    "aerial": "aerial", "drone": "aerial", "drone shot": "aerial",
    "overhead": "overhead", "top down": "overhead", "birds eye": "overhead", "bird's eye": "overhead",
    "high angle": "high_angle", "low angle": "low_angle", "dutch": "dutch", "dutch angle": "dutch", "canted": "dutch",
    "establishing": "establishing", "establishing shot": "establishing",
    "over the shoulder": "ots", "ots": "ots", "clean single": "single", "single": "single",
    "two shot": "two_shot", "2 shot": "two_shot", "three shot": "three_shot", "3 shot": "three_shot",
    "group shot": "group", "group": "group", "crowd": "group", "insert": "insert", "insert shot": "insert",
    "center": "center", "centered": "center", "centre": "center", "left heavy": "left_heavy", "right heavy": "right_heavy",
    "balanced": "balanced", "symmetrical": "symmetrical", "symmetry": "symmetrical", "short side": "short_side",
    "locked off": "locked", "locked-off": "locked", "static shot": "locked", "fixed cam": "locked", "tripod": "locked",
    "pan": "pan", "panning": "pan", "tilt": "tilt", "tilt up": "tilt", "tilt down": "tilt",
    "zoom": "zoom", "push in": "zoom", "push": "zoom", "crash zoom": "zoom", "dolly in": "zoom",
    "whip pan": "whip", "whip": "whip", "handheld": "handheld", "hand held": "handheld", "shaky": "handheld",
    "silhouette": "silhouette", "shallow focus": "shallow", "shallow depth of field": "shallow", "bokeh": "shallow",
    "fisheye": "fisheye", "ultra wide": "ultra_wide", "underwater": "underwater",
    "haze": "haze", "fog": "haze", "hard light": "hard_light", "halation": "halation", "reflection": "reflections",
    "reflections": "reflections", "spotlight": "spotlight", "night vision": "night_vision", "thermal": "thermal",
    "motion blur": "motion_blur", "double exposure": "double_exposure", "split screen": "split_screen",
    "typography": "typography", "tilt shift": "tilt_shift", "vintage": "vintage",
}


def tag_for_query(q):
    k = " ".join(q.lower().replace("’", "'").split())
    return SYNONYMS.get(k) or next((t for t, (label, _) in TAGS.items() if label.lower() == k), None)


# ---------- AI tags (from scanned frames) ----------

# exclusive groups: one answer per frame. "_none" classes win when the group doesn't apply and are never tagged.
EXCLUSIVE = {
    "size": {
        "ews": ["an extreme wide shot, tiny distant figures in a vast landscape", "an extreme long shot of a huge open landscape",
                "a very distant view of a city or mountains"],
        "ws": ["a wide shot showing a person's whole body and the room around them", "a long shot of a full figure standing in a place",
               "a wide shot of a street with people"],
        "mws": ["a medium wide shot of a person from the knees up", "a cowboy shot framed from mid thigh up"],
        "ms": ["a medium shot of a person from the waist up", "a mid shot of someone sitting at a table"],
        "mcu": ["a medium close-up of a person from the chest up", "head and shoulders of a person"],
        "cu": ["a close-up of a person's face filling the frame", "a close-up shot of a face", "a close-up of an object"],
        "ecu": ["an extreme close-up of an eye", "an extreme close-up of lips", "a macro shot of a tiny detail or texture"],
    },
    "angle": {
        "overhead": ["a top-down overhead shot looking straight down", "a flat lay photographed from directly above"],
        "high_angle": ["a high angle shot looking down at a person", "a camera looking down on the subject from above"],
        "low_angle": ["a low angle shot looking up at a person", "a camera looking up at a towering subject"],
        "dutch": ["a dutch angle with a tilted horizon", "a canted, tilted camera angle"],
        "_none": ["an eye level shot", "a photo taken at eye level", "a straight-on shot"],
    },
    "people": {
        "single": ["one person alone in the frame", "a single actor in the shot"],
        "two_shot": ["two people in the frame", "a two shot of two actors"],
        "three_shot": ["three people in the frame", "a shot of three people together"],
        "group": ["a group of many people", "a crowd of people"],
        "_none": ["an empty scene with no people", "a landscape with nobody in it", "an object with no people", "a building"],
    },
}
# group -> (always pick one?, needs library-relative check?)
EXCLUSIVE_MODE = {"size": (True, False), "angle": (False, True), "people": (False, False)}
# yes / no pairs, judged independently
BINARY = {
    "aerial": (["an aerial drone shot looking down at the land", "a view from an airplane or drone high above"], ["a shot from the ground"]),
    "establishing": (["an establishing shot of a building exterior", "a wide establishing shot of a city or location"], ["a close shot of a person", "an interior detail"]),
    "ots": (["an over the shoulder shot, the back of a person's head and shoulder in the foreground", "a shot over someone's shoulder looking at another person"],
            ["a single person facing the camera with nothing in front of them"]),
    "insert": (["an insert shot, a close-up of hands doing something", "a detail shot of an object, a phone, a letter or a key"],
               ["a shot of a person's face", "a wide shot of a scene"]),
    "silhouette": (["a dark silhouette against a bright background", "a person in silhouette"], ["an evenly lit subject", "a person lit from the front"]),
    "shallow": (["a shallow depth of field with a very blurry background", "strong bokeh, background out of focus"], ["everything in sharp focus", "deep focus, sharp background"]),
    "fisheye": (["a fisheye lens photo with strong curved distortion", "a round fisheye image"], ["a normal lens photo with straight lines"]),
    "ultra_wide": (["an ultra wide angle lens with stretched edges", "a very wide lens with exaggerated perspective"], ["a normal lens", "a telephoto shot"]),
    "underwater": (["an underwater shot", "a person swimming underwater"], ["a shot on land", "a shot above water"]),
    "haze": (["a hazy, smoky atmosphere with light beams", "fog and haze in the air"], ["clear air", "a crisp clean image"]),
    "hard_light": (["hard light with sharp, crisp shadows", "harsh direct sunlight casting hard shadows"], ["soft diffused light", "an overcast soft lit scene"]),
    "halation": (["glowing red halation around bright highlights", "a dreamy glow blooming around lights"], ["clean highlights without glow"]),
    "reflections": (["a reflection in a mirror or window", "a person reflected in glass"], ["no reflections"]),
    "spotlight": (["a single spotlight on a dark stage", "a pool of light in darkness"], ["evenly lit room"]),
    "night_vision": (["a green night vision camera image", "infrared night vision footage"], ["a normal color image"]),
    "thermal": (["a thermal camera heat map image", "an infrared thermal image in false colors"], ["a normal color image"]),
    "motion_blur": (["heavy motion blur, streaks of movement", "a long exposure with motion blur"], ["a sharp frozen moment"]),
    "double_exposure": (["a double exposure with two images overlaid", "two images blended on top of each other"], ["a single clean image"]),
    "split_screen": (["a split screen with two separate images side by side", "a frame divided into panels"], ["a single full frame image"]),
    "typography": (["large bold typography text on screen", "a title card with big text"], ["an image without any text"]),
    "tilt_shift": (["a tilt shift miniature effect", "a scene that looks like a tiny model"], ["a normal photo"]),
    "vintage": (["old vintage film footage with grain and scratches", "a faded retro photograph from the 1970s"], ["a modern clean digital image"]),
}
FACING = (["a person facing left, looking toward the left side of the frame"], ["a person facing right, looking toward the right side of the frame"])
SHARE = 0.3         # a video gets a tag when this share of its frames show it

_prompts = {}


def _prompt_vectors():
    if not _prompts:
        from . import ai
        for g, classes in EXCLUSIVE.items():
            keys = list(classes)
            _prompts[g] = (keys, np.stack([np.mean([ai.embed_text(p) for p in classes[k]], axis=0) for k in keys]))
        for k, (pos, neg) in BINARY.items():
            _prompts[k] = (np.mean([ai.embed_text(p) for p in pos], axis=0), np.mean([ai.embed_text(p) for p in neg], axis=0))
        _prompts["_facing"] = tuple(np.mean([ai.embed_text(p) for p in side], axis=0) for side in FACING)
    return _prompts


def _norm(m):
    return m / (np.linalg.norm(m, axis=-1, keepdims=True) + 1e-8)


def frame_scores(vec):
    """Every tag's probability for one frame (used to calibrate the cutoffs)."""
    v = _norm(np.asarray(vec, np.float32))[None]
    return {k: round(float(1 / (1 + np.exp(-a[0])) if k in BINARY else a[0]), 3) for k, a in _raw(v).items()}


# Each tag's prompts have their own bias (some score high on almost anything), so a frame only gets a look tag
# when it stands out from the rest of *your* library on that measure, as well as clearing a floor.
Z_BINARY, Z_EXCLUSIVE = 2.0, 1.5
FLOOR_BINARY, FLOOR_EXCLUSIVE = 0.9, 0.55
# framing / angle / look describe footage: text graphics, gear shots and talking heads only get these
ANY_CONTENT = {"typography", "split_screen"}
NOT_FOOTAGE = {"text", "gear", "talking"}
_stats = {"n": -1}


def _raw(vecs):
    """Per frame: binary tags as logit differences, exclusive classes as probabilities. -> {tag: array}"""
    P = _prompt_vectors()
    out = {}
    for g in EXCLUSIVE:
        keys, M = P[g]
        lg = 100.0 * vecs @ _norm(M).T
        p = np.exp(lg - lg.max(axis=1, keepdims=True)); p /= p.sum(axis=1, keepdims=True)
        for ci, k in enumerate(keys):
            if not k.startswith("_"):
                out[k] = p[:, ci]
    for k in BINARY:
        pos, neg = P[k]
        out[k] = 100.0 * (vecs @ _norm(pos) - vecs @ _norm(neg))
    return out


def library_stats():
    """Mean / spread of every tag's score over the library's frames (sampled), refreshed as the library grows."""
    from . import ai
    ix = ai._load_index()
    mat = ix["mat"]
    n = 0 if mat is None else len(mat)
    if _stats["n"] >= 0 and n < _stats["n"] * 1.1 + 50:
        return _stats.get("v")
    if n < 300:            # too few frames to know what's typical yet
        _stats.update(n=n, v=None)
        return None
    pick = np.random.default_rng(0).choice(n, size=min(n, 20000), replace=False)
    raw = _raw(_norm(mat[pick].astype(np.float32)))
    _stats.update(n=n, v={k: (float(a.mean()), float(a.std()) + 1e-6) for k, a in raw.items()})
    return _stats["v"]


def frame_tags(times, vecs):
    """vecs: (n, 512) frame vectors of one media item -> {tag: (score, first t)}."""
    out = {}
    if not len(vecs):
        return out
    vecs = _norm(np.asarray(vecs, np.float32))
    times = np.asarray(times, np.float32)
    stats = library_stats()
    raw = _raw(vecs)
    few = len(vecs) <= 2

    def keep(k, hit, prob):
        if hit.mean() >= SHARE or (few and hit.any()):
            out[k] = (float(prob[hit].mean()), float(times[hit].min()))

    for g, classes in EXCLUSIVE.items():
        always, relative = EXCLUSIVE_MODE[g]
        keys = [k for k in classes if not k.startswith("_")]
        if always:   # frame size: every frame has one; tag the sizes the video spends real time in
            P_ = np.stack([raw[k] for k in keys], axis=1)
            winner = P_.argmax(axis=1)
            for ci, k in enumerate(keys):
                keep(k, (winner == ci) & (P_[:, ci] >= 0.3), raw[k])
            continue
        for k in keys:
            prob = raw[k]
            hit = prob >= FLOOR_EXCLUSIVE
            if relative and stats and k in stats:
                mu, sd = stats[k]
                hit &= prob >= mu + Z_EXCLUSIVE * sd
            keep(k, hit, prob)
    for k in BINARY:
        a = raw[k]
        prob = 1 / (1 + np.exp(-a))
        if stats and k in stats:
            mu, sd = stats[k]
            hit = (prob >= FLOOR_BINARY) & (a >= mu + Z_BINARY * sd)
        else:   # new library: only very confident calls
            hit = prob >= 0.985
        keep(k, hit, prob)
    return out


def facing(vecs):
    """-1 facing left, +1 facing right, 0 unclear (for Short side)."""
    P = _prompt_vectors()
    L, R = P["_facing"]
    v = _norm(np.asarray(vecs, np.float32)).mean(axis=0)
    d = 100.0 * (float(_norm(L) @ v) - float(_norm(R) @ v))
    return -1 if d > 1.5 else 1 if d < -1.5 else 0


# ---------- composition (measured from the picture) ----------

def composition(path, crop=None):
    """Where the visual weight sits, and mirror symmetry. -> (tags set, weight centre x 0..1)"""
    from PIL import Image, ImageFilter, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("L")
    if crop:   # ignore letterbox bars
        w, h = im.size
        box = (int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h))
        if box[2] - box[0] > 32 and box[3] - box[1] > 32:
            im = im.crop(box)
    im.thumbnail((256, 256))
    L = np.asarray(im, np.float32) / 255.0
    soft = np.asarray(im.filter(ImageFilter.GaussianBlur(12)), np.float32) / 255.0
    edges = np.asarray(im.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(3)), np.float32) / 255.0
    # visual weight: things that stand out from their surroundings, plus detail
    weight = np.abs(L - soft) * 2.0 + edges + np.abs(L - L.mean()) * 0.6
    weight = np.asarray(Image.fromarray((weight / (weight.max() + 1e-6) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(6)), np.float32)
    weight = np.clip(weight - np.percentile(weight, 50), 0, None)   # keep what stands out, not the general texture
    total = weight.sum() + 1e-6
    h, w = weight.shape
    xs = np.arange(w)[None, :] / (w - 1)
    cx = float((weight * xs).sum() / total)
    cols = weight.sum(axis=0) / total
    left, mid, right = cols[: w // 3].sum(), cols[w // 3: 2 * w // 3].sum(), cols[2 * w // 3:].sum()
    flip = L[:, ::-1]
    a, b = L - L.mean(), flip - flip.mean()
    sym = float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6))
    tags = set()
    if sym >= 0.8 and L.std() > 0.06:
        tags.add("symmetrical")
    if mid >= 0.5 and abs(cx - 0.5) < 0.07:
        tags.add("center")
    elif cx < 0.42 and left > right * 1.6:
        tags.add("left_heavy")
    elif cx > 0.58 and right > left * 1.6:
        tags.add("right_heavy")
    elif abs(left - right) < 0.12 and mid < 0.5:
        tags.add("balanced")
    return tags, cx


# ---------- camera movement (from the video) ----------

MW, MH, MFPS, MAX_SECONDS = 160, 96, 6, 90


def _gray_frames(src, duration):
    from . import media
    secs = min(float(duration or MAX_SECONDS), MAX_SECONDS)
    cmd = [media.FFMPEG, "-v", "error", "-t", f"{secs:.2f}", "-i", str(src),
           "-vf", f"fps={MFPS},scale={MW}:{MH},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=600).stdout
    n = len(raw) // (MW * MH)
    return np.frombuffer(raw[:n * MW * MH], np.uint8).reshape(n, MH, MW).astype(np.float32)


_win = np.outer(np.hanning(32), np.hanning(32)).astype(np.float32)


def _shift(a, b):
    """Translation between two 32x32 blocks (phase correlation) -> (dx, dy, confidence)."""
    A = np.fft.fft2((a - a.mean()) * _win)
    B = np.fft.fft2((b - b.mean()) * _win)
    R = A * np.conj(B)
    R /= np.abs(R) + 1e-6
    r = np.fft.ifft2(R).real
    iy, ix = np.unravel_index(np.argmax(r), r.shape)
    dy = iy - 32 if iy > 16 else iy
    dx = ix - 32 if ix > 16 else ix
    return -dx, -dy, float(r.max())


# 3 rows x 4 columns of 32x32 blocks spread over the 160x96 frame
_BLOCKS = [(y, x) for y in (0, 32, 64) for x in (4, 44, 84, 124)]
_CENTER = np.array([[x + 16 - MW / 2, y + 16 - MH / 2] for y, x in _BLOCKS], np.float32)


def smear(frame):
    """How much a frame is streaked sideways: vertical detail far above horizontal detail = horizontal motion blur."""
    gx = float(np.abs(np.diff(frame, axis=1)).mean()) + 1e-3
    gy = float(np.abs(np.diff(frame, axis=0)).mean()) + 1e-3
    return gy / gx


def whip_moments(frames, steps):
    """Quick bursts of frames smeared sideways: what a whip pan leaves behind (too fast to track block by block)."""
    sm = [smear(f) for f in frames]
    usual = float(np.median(sm))          # some shots are full of vertical lines anyway: compare with the video itself
    hits = [sm[i + 1] >= max(1.8, 1.8 * usual) for i in range(len(steps))]
    out, run = [], 0
    for i, h_ in enumerate(hits + [False]):
        if h_:
            run += 1
        else:
            if 2 <= run <= 9:      # a whip is quick: a third of a second to ~1.5 s
                out.append((i - run) / MFPS)
            run = 0
    return out


def motion_steps(frames):
    """Per step between frames: (dx, dy, zoom, cut) in pixels of a 160-wide frame."""
    steps = []
    for a, b in zip(frames[:-1], frames[1:]):
        diff = float(np.abs(a - b).mean())
        vs, ok = [], []
        for y, x in _BLOCKS:
            dx, dy, conf = _shift(a[y:y + 32, x:x + 32], b[y:y + 32, x:x + 32])
            vs.append((dx, dy)); ok.append(conf > 0.25)   # film grain alone scores ~0.1-0.16
        v = np.array(vs, np.float32)
        good = np.array(ok)
        if good.sum() < 4:
            steps.append((0.0, 0.0, 0.0, diff > 40, False))
            continue
        med = np.median(v[good], axis=0)
        # zoom: blocks move away from (push in) or toward (pull out) the center
        rel = v[good] - med
        c = _CENTER[good]
        zoom = float((rel * c).sum() / ((c * c).sum() + 1e-6))
        steps.append((float(med[0]), float(med[1]), zoom, diff > 40, True))
    return steps


def motion_tags(steps):
    """Label stretches between cuts; a tag is kept when it covers enough of the video. -> {tag: (score, t)}"""
    if len(steps) < 6:
        return {}
    shots, cur = [], []
    for i, s in enumerate(steps):
        if s[3] and cur:
            shots.append(cur); cur = []
        else:
            cur.append((i, s))
    if cur:
        shots.append(cur)
    total = len(steps)
    found = {}
    for shot in shots:
        if len(shot) < 5:          # under a second: not enough to judge
            continue
        valid = [s for _, s in shot if s[4]]
        if len(valid) < 4:
            continue
        dx = np.array([s[0] for s in valid]); dy = np.array([s[1] for s in valid]); z = np.array([s[2] for s in valid])
        t0 = shot[0][0] / MFPS
        mx, my, mz = float(np.median(dx)), float(np.median(dy)), float(np.median(z))
        speed = np.hypot(dx, dy)
        jitter = float(np.median(np.hypot(np.diff(dx), np.diff(dy)))) if len(dx) > 2 else 0.0
        labels = []
        if np.median(speed) < 0.35 and jitter < 0.5 and abs(mz) < 0.004:
            labels.append(("locked", 0.9))
        elif jitter >= 0.9 and np.median(speed) < 4:
            labels.append(("handheld", min(1.0, jitter / 2)))
        if abs(mx) >= 0.6 and np.mean(np.sign(dx) == np.sign(mx)) >= 0.75 and abs(mx) > abs(my) * 1.5:
            labels.append(("pan", min(1.0, abs(mx) / 3)))
        if abs(my) >= 0.5 and np.mean(np.sign(dy) == np.sign(my)) >= 0.75 and abs(my) > abs(mx) * 1.5:
            labels.append(("tilt", min(1.0, abs(my) / 3)))
        if abs(mz) >= 0.006 and np.mean(np.sign(z) == np.sign(mz)) >= 0.7:
            labels.append(("zoom", min(1.0, abs(mz) / 0.02)))
        for tag, sc in labels:
            f = found.setdefault(tag, [0, sc, t0])
            f[0] += len(shot); f[1] = max(f[1], sc); f[2] = min(f[2], t0)
    return {t: (sc, t0) for t, (n, sc, t0) in found.items() if n / total >= 0.25 or n >= 18}


def analyze_motion(src, duration):
    frames = _gray_frames(src, duration)
    if len(frames) < 8:
        return {}
    steps = motion_steps(frames)
    out = motion_tags(steps)
    whips = whip_moments(frames, steps)
    if whips:
        out["whip"] = (0.8, whips[0])
    return out


# ---------- background work ----------

def pending(limit=10):
    return db.conn().execute(
        "SELECT m.id, m.kind, m.path, m.duration, m.thumb, m.poster, a.crop FROM media m JOIN posts p ON p.id=m.post_id JOIN analysis a ON a.media_id=m.id "
        "WHERE p.deleted_at IS NULL AND p.status='ready' AND COALESCE(a.scan_version,0) >= 1 "
        "AND COALESCE(a.tech_version,0) < ? ORDER BY COALESCE(p.saved_at, p.added_at) DESC LIMIT ?",
        (TECH_VERSION, limit)).fetchall()


def frame_matrix(c, media_id):
    rows = c.execute("SELECT t, vec FROM frames WHERE media_id=? ORDER BY t, id", (media_id,)).fetchall()
    if not rows:
        return [], np.zeros((0, 512), np.float32)
    return [r[0] for r in rows], np.stack([np.frombuffer(r[1], np.float16).astype(np.float32) for r in rows])


def tag_media(r):
    c = db.conn()
    times, vecs = frame_matrix(c, r["id"])
    found = frame_tags(times, vecs)
    label = c.execute("SELECT COALESCE(a.content_user, a.content) FROM media m JOIN media cm ON cm.post_id=m.post_id AND cm.idx=0 "
                      "JOIN analysis a ON a.media_id=cm.id WHERE m.id=?", (r["id"],)).fetchone()
    footage = not (label and label[0] in NOT_FOOTAGE)
    if not footage:
        found = {k: v for k, v in found.items() if k in ANY_CONTENT}
    else:
        pic = db.THUMB_DIR / (r["poster"] or r["thumb"] or "") if (r["poster"] or r["thumb"]) else None
        if pic and pic.is_file():
            try:
                comp, cx = composition(pic, json.loads(r["crop"]) if r["crop"] else None)
                for k in comp:
                    found[k] = (0.8, 0.0)
                # short side: subject off to one side, looking toward the near edge
                if any(k in found for k in ("single", "two_shot")) and (cx < 0.42 or cx > 0.58) and len(vecs):
                    f_ = facing(vecs)
                    if (cx < 0.42 and f_ < 0) or (cx > 0.58 and f_ > 0):
                        found["short_side"] = (0.6, 0.0)
            except Exception:
                traceback.print_exc()
    mot = {}
    if r["kind"] == "video":
        src = db.media_path(r["path"])
        if src.exists():
            try:
                mot = analyze_motion(src, r["duration"])
            except Exception:
                traceback.print_exc()
    with db.tx() as w:
        w.execute("DELETE FROM techniques WHERE media_id=? AND source IN ('ai','motion')", (r["id"],))
        held = {row[0] for row in w.execute("SELECT tag FROM techniques WHERE media_id=? AND source IN ('user','no')", (r["id"],))}
        w.executemany("INSERT OR REPLACE INTO techniques (media_id, tag, score, t, source) VALUES (?,?,?,?,?)",
                      [(r["id"], k, round(s, 3), round(t, 2), src_) for src_, d in (("ai", found), ("motion", mot))
                       for k, (s, t) in d.items() if k not in held])
        w.execute("UPDATE analysis SET tech_version=? WHERE media_id=?", (TECH_VERSION, r["id"]))


_migrated = {"done": False}


def _migrate():
    """Earlier versions used other tag names: carry your own tags over, drop the rest."""
    with db.tx() as w:
        for old, new in RENAMED.items():
            if old != new:
                w.execute("UPDATE OR REPLACE techniques SET tag=? WHERE tag=? AND source IN ('user','no')", (new, old))
        w.execute(f"DELETE FROM techniques WHERE tag NOT IN ({','.join('?' * len(TAGS))})", list(TAGS))
    _migrated["done"] = True


def work_batch():
    if not _migrated["done"]:
        _migrate()
    rows = pending()
    for r in rows:
        try:
            tag_media(r)
        except Exception:
            traceback.print_exc()
            with db.tx() as w:
                w.execute("UPDATE analysis SET tech_version=? WHERE media_id=?", (TECH_VERSION, r["id"]))
    if _relearn["due"] and time.time() >= _relearn["due"]:
        _relearn["due"] = 0
        try:
            relearn()
        except Exception:
            traceback.print_exc()
    return len(rows)


# ---------- learning from your own tags ----------

_relearn = {"due": 0}


def schedule_relearn(delay=3):
    _relearn["due"] = time.time() + delay


def _media_vecs(c, ids):
    out = {}
    for mid in ids:
        _, v = frame_matrix(c, mid)
        if len(v):
            out[mid] = _norm(v)
    return out


def relearn():
    """For every tag you've added by hand: give it to the shots that look most like your examples."""
    c = db.conn()
    by_tag = {}
    for mid, tag, src in c.execute("SELECT media_id, tag, source FROM techniques WHERE source IN ('user','no')"):
        by_tag.setdefault(tag, {"user": [], "no": []})[src].append(mid)
    with db.tx() as w:
        w.execute("DELETE FROM techniques WHERE source='learned'")
    if not by_tag:
        return
    from . import ai
    ix = ai._load_index()
    if ix["mat"] is None:
        return
    mat = ix["mat"]
    for tag, ex in by_tag.items():
        if not ex["user"]:
            continue
        pos = _media_vecs(c, ex["user"]); neg = _media_vecs(c, ex["no"])
        if not pos:
            continue
        # each example's frames are a prototype; a frame matches when it's very close to any of them
        P = _norm(np.concatenate([v.mean(axis=0, keepdims=True) for v in pos.values()] + [np.concatenate(list(pos.values()))[:200]]))
        sims = np.empty(len(mat), np.float32)
        for i in range(0, len(mat), 50000):
            sims[i:i + 50000] = (mat[i:i + 50000].astype(np.float32) @ P.T).max(axis=1)
        if neg:
            N = _norm(np.concatenate(list(neg.values()))[:200])
            nsims = np.empty(len(mat), np.float32)
            for i in range(0, len(mat), 50000):
                nsims[i:i + 50000] = (mat[i:i + 50000].astype(np.float32) @ N.T).max(axis=1)
        else:
            nsims = np.full(len(mat), -1, np.float32)
        cut = max(0.86, float(np.percentile(sims, 99.0)))
        best = {}
        for j in np.nonzero((sims >= cut) & (sims > nsims + 0.02))[0]:
            mid = int(ix["media"][j])
            if mid in best and best[mid][0] >= sims[j]:
                continue
            best[mid] = (float(sims[j]), float(ix["t"][j]))
        skip = set(ex["user"]) | set(ex["no"])
        with db.tx() as w:
            have = {row[0] for row in w.execute("SELECT media_id FROM techniques WHERE tag=? AND source IN ('ai','motion')", (tag,))}
            w.executemany("INSERT OR REPLACE INTO techniques (media_id, tag, score, t, source) VALUES (?,?,?,?, 'learned')",
                          [(mid, tag, round(s, 3), round(t, 2)) for mid, (s, t) in best.items() if mid not in skip and mid not in have])


def set_user_tag(media_id, tag, on):
    with db.tx() as w:
        w.execute("DELETE FROM techniques WHERE media_id=? AND tag=?", (media_id, tag))
        w.execute("INSERT INTO techniques (media_id, tag, score, t, source) VALUES (?,?,1,0,?)", (media_id, tag, "user" if on else "no"))
    schedule_relearn()
