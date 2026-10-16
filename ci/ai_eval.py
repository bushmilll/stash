"""Compare Stash's current search model with candidates on real photos (COCO val2017 + captions).

Runs on GitHub's Mac runners (they can download models). Prints a report: search accuracy, speed,
and the score distributions needed to re-tune Stash's thresholds for a new model.
"""
import io, json, os, random, sys, time, zipfile, concurrent.futures as cf
from pathlib import Path
import numpy as np
import requests
from PIL import Image, ImageOps

N = int(os.environ.get("N_IMAGES", "1000"))
WORK = Path(os.environ.get("WORK", "/tmp/aieval")); WORK.mkdir(parents=True, exist_ok=True)
HF = "https://huggingface.co/"

def get(url, dest):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for ch in r.iter_content(1 << 20):
                f.write(ch)
        tmp.replace(dest)
    return dest

# ---------- data ----------
ann = get("http://images.cocodataset.org/annotations/annotations_trainval2017.zip", WORK / "ann.zip")
with zipfile.ZipFile(ann) as z:
    caps = json.loads(z.read("annotations/captions_val2017.json"))
by_img = {}
for a in caps["annotations"]:
    by_img.setdefault(a["image_id"], []).append(a["caption"].strip())
imgs = sorted(caps["images"], key=lambda r: r["id"])[:N]
def fetch(r):
    return get(r["coco_url"], WORK / "img" / r["file_name"])
with cf.ThreadPoolExecutor(16) as ex:
    paths = list(ex.map(fetch, imgs))
texts = [by_img[r["id"]][0] for r in imgs]
texts2 = [by_img[r["id"]][1] for r in imgs]
print(f"data: {len(paths)} images", flush=True)

# ---------- models ----------
MODELS = {
  "clip-b16-int8 (current)": dict(repo="Xenova/clip-vit-base-patch16", vision="onnx/vision_model_quantized.onnx", text="onnx/text_model_quantized.onnx",
                                   mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711], pad=None, lower=False),
  "siglip2-b16 vis-int8 txt-int8": dict(repo="onnx-community/siglip2-base-patch16-224-ONNX", vision="onnx/vision_model_int8.onnx", text="onnx/text_model_int8.onnx",
                                   mean=[0.5] * 3, std=[0.5] * 3, pad=64, lower=True),
  "siglip2-b16 vis-fp32 txt-int8": dict(repo="onnx-community/siglip2-base-patch16-224-ONNX", vision="onnx/vision_model.onnx", text="onnx/text_model_int8.onnx",
                                   mean=[0.5] * 3, std=[0.5] * 3, pad=64, lower=True),
  "siglip2-b16 vis-fp32 txt-fp32": dict(repo="onnx-community/siglip2-base-patch16-224-ONNX", vision="onnx/vision_model.onnx", text="onnx/text_model.onnx",
                                   mean=[0.5] * 3, std=[0.5] * 3, pad=64, lower=True),
}
only = os.environ.get("MODELS")
if only:
    MODELS = {k: v for k, v in MODELS.items() if any(s in k for s in only.split(","))}

import onnxruntime as ort
from tokenizers import Tokenizer
print("onnxruntime", ort.__version__, ort.get_available_providers(), flush=True)

def square_crops(im):
    w, h = im.size; r = w / h
    if r < 0.72: s = w; return [im.crop((0, 0, s, s)), im.crop((0, h - s, s, h))]
    if r > 1.45: s = h; return [im.crop((0, 0, s, s)), im.crop((w - s, 0, w, s))]
    s = min(w, h); l, t = (w - s) // 2, (h - s) // 2
    return [im.crop((l, t, l + s, t + s))]

def tensor(sq, mean, std):
    a = (np.asarray(sq.resize((224, 224), Image.BICUBIC), dtype=np.float32) / 255.0 - np.array(mean, np.float32)) / np.array(std, np.float32)
    return a.transpose(2, 0, 1)

def pick(sess, out, names):
    on = [o.name for o in sess.get_outputs()]
    for want in names:
        if want in on:
            return out[on.index(want)]
    # first 2-D output
    for i, o in enumerate(out):
        if np.asarray(o).ndim == 2:
            return o
    return out[0]

def unit(m):
    return m / (np.linalg.norm(m, axis=-1, keepdims=True) + 1e-8)

report = {}
for name, cfg in MODELS.items():
    d = WORK / "models" / cfg["repo"].replace("/", "__")
    vp = get(HF + cfg["repo"] + "/resolve/main/" + cfg["vision"], d / cfg["vision"])
    tp = get(HF + cfg["repo"] + "/resolve/main/" + cfg["text"], d / cfg["text"])
    kp = get(HF + cfg["repo"] + "/resolve/main/tokenizer.json", d / "tokenizer.json")
    sizes = (vp.stat().st_size + tp.stat().st_size) / 1e6
    res = {"download_mb": round(sizes)}
    for prov in (["CPUExecutionProvider"], ["CoreMLExecutionProvider", "CPUExecutionProvider"]):
        if prov[0] not in ort.get_available_providers():
            continue
        try:
            o = ort.SessionOptions(); o.intra_op_num_threads = 3
            vs = ort.InferenceSession(str(vp), o, providers=prov)
            x = np.stack([tensor(Image.new("RGB", (224, 224), (100, 120, 140)), cfg["mean"], cfg["std"])] * 8)
            iname = vs.get_inputs()[0].name
            vs.run(None, {iname: x})
            t0 = time.time()
            for _ in range(3):
                vs.run(None, {iname: x})
            res[f"img_per_s_{prov[0][:6]}"] = round(24 / (time.time() - t0), 1)
        except Exception as e:
            res[f"img_per_s_{prov[0][:6]}"] = f"failed: {e}"[:120]
    o = ort.SessionOptions(); o.intra_op_num_threads = 3
    vs = ort.InferenceSession(str(vp), o, providers=["CPUExecutionProvider"])
    ts = ort.InferenceSession(str(tp), o, providers=["CPUExecutionProvider"])
    print(name, "vision io:", [(i.name, i.shape) for i in vs.get_inputs()], [(i.name, i.shape) for i in vs.get_outputs()], flush=True)
    print(name, "text io:", [(i.name, i.shape) for i in ts.get_inputs()], [(i.name, i.shape) for i in ts.get_outputs()], flush=True)
    tok = Tokenizer.from_file(str(kp))
    pad_id = tok.token_to_id("<pad>") or 0
    iname = vs.get_inputs()[0].name

    def embed_imgs(ims, aug=False):
        """-> list of (n_crops, D) per image, and mean vector per image"""
        allt, owner = [], []
        for k, p in enumerate(ims):
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            if aug:   # a slightly different copy: crop 8% off, a bit brighter
                w, h = im.size; im = im.crop((int(w * .08), int(h * .04), w, int(h * .96))).point(lambda v: min(255, int(v * 1.12)))
            im.thumbnail((640, 640))
            for sq in square_crops(im):
                allt.append(tensor(sq, cfg["mean"], cfg["std"])); owner.append(k)
        outs = []
        for i in range(0, len(allt), 16):
            chunk = np.stack(allt[i:i + 16]).astype(np.float32)
            try:
                outs.append(np.asarray(pick(vs, vs.run(None, {iname: chunk}), ["image_embeds", "pooler_output"]), np.float32).reshape(len(chunk), -1))
            except Exception:
                outs.append(np.concatenate([np.asarray(pick(vs, vs.run(None, {iname: c[None]}), ["image_embeds", "pooler_output"]), np.float32).reshape(1, -1) for c in chunk]))
        V = unit(np.concatenate(outs)); owner = np.array(owner)
        return V, owner

    def embed_txt(tt):
        out = []
        for t in tt:
            t = t.lower() if cfg["lower"] else t
            ids = tok.encode(t).ids
            if cfg["pad"]:
                ids = (ids + [pad_id] * cfg["pad"])[:cfg["pad"]]
            else:
                ids = ids[:77]
            arr = np.array([ids], np.int64)
            feed = {}
            for inp in ts.get_inputs():
                if "input_ids" in inp.name: feed[inp.name] = arr
                elif "attention" in inp.name: feed[inp.name] = np.ones_like(arr) if not cfg["pad"] else (arr != pad_id).astype(np.int64)
                elif "position" in inp.name: feed[inp.name] = np.arange(arr.shape[1], dtype=np.int64)[None]
            out.append(np.asarray(pick(ts, ts.run(None, feed), ["text_embeds", "pooler_output"]), np.float32).reshape(-1))
        return unit(np.stack(out))

    t0 = time.time(); V, owner = embed_imgs(paths); res["embed_s_per_img"] = round((time.time() - t0) / len(paths), 3)
    T = embed_txt(texts); T2 = embed_txt(texts2)
    # app-style scoring: best crop per image
    S = T @ V.T
    best = np.full((len(texts), len(paths)), -9, np.float32)
    for k in range(len(paths)):
        best[:, k] = S[:, owner == k].max(axis=1)
    ranks = np.array([(best[i] > best[i, i]).sum() for i in range(len(texts))])
    res.update({f"t2i_R@{k}": round(float((ranks < k).mean()) * 100, 1) for k in (1, 5, 10)})
    Vm = unit(np.stack([V[owner == k].mean(axis=0) for k in range(len(paths))]))
    S2 = Vm @ T.T
    r2 = np.array([(S2[i] > S2[i, i]).sum() for i in range(len(paths))])
    res.update({f"i2t_R@{k}": round(float((r2 < k).mean()) * 100, 1) for k in (1, 5)})
    # distributions for re-tuning thresholds
    pct = lambda a: {p: round(float(np.percentile(a, p)), 4) for p in (1, 10, 50, 90, 99, 99.9)}
    diag = best[np.arange(len(texts)), np.arange(len(texts))]
    off = best[~np.eye(len(texts), dtype=bool)]
    res["text_img_match"] = pct(diag); res["text_img_random"] = pct(off)
    rng = np.random.default_rng(0)
    a, b = rng.integers(0, len(paths), 4000), rng.integers(0, len(paths), 4000)
    keep = a != b
    res["img_img_random"] = pct((Vm[a] * Vm[b]).sum(1)[keep])
    Va, oa = embed_imgs(paths[:300], aug=True)
    Vam = unit(np.stack([Va[oa == k].mean(axis=0) for k in range(300)]))
    res["img_img_same_shot"] = pct((Vm[:300] * Vam).sum(1))
    res["txt_txt_same_photo"] = pct((T * T2).sum(1))
    ta, tb = rng.integers(0, len(texts), 4000), rng.integers(0, len(texts), 4000)
    res["txt_txt_random"] = pct((T[ta] * T[tb]).sum(1)[ta != tb])
    # cinematic prompts as a sanity check on what the shot-type prompts pick out
    probe = ["a close-up of a person's face", "a wide shot of a landscape", "an overhead shot looking straight down", "a photo of food on a plate",
             "a city street at night", "a person surfing a wave"]
    P = embed_txt(probe)
    top = {}
    for j, q in enumerate(probe):
        sc = (P[j] @ V.T)
        bestimg = {}
        for idx in np.argsort(-sc):
            k = int(owner[idx])
            if k not in bestimg:
                bestimg[k] = float(sc[idx])
            if len(bestimg) >= 5:
                break
        top[q] = [(texts[k][:70], round(s, 3)) for k, s in bestimg.items()]
    res["probe_top5"] = top
    report[name] = res
    print(json.dumps({name: res}, indent=1), flush=True)

# SigLIP's own scale/bias (turns similarities into probabilities): fit from the full model's logits
try:
    repo = "onnx-community/siglip2-base-patch16-224-ONNX"
    d = WORK / "models" / repo.replace("/", "__")
    mp = get(HF + repo + "/resolve/main/onnx/model_quantized.onnx", d / "onnx/model_quantized.onnx")
    ms = ort.InferenceSession(str(mp), providers=["CPUExecutionProvider"])
    print("full model io:", [(i.name, i.shape) for i in ms.get_inputs()], [(o.name, o.shape) for o in ms.get_outputs()], flush=True)
    tok = Tokenizer.from_file(str(d / "tokenizer.json")); pad_id = tok.token_to_id("<pad>") or 0
    ids = np.array([(tok.encode(t.lower()).ids + [pad_id] * 64)[:64] for t in texts[:6]], np.int64)
    px = np.stack([tensor(ImageOps.exif_transpose(Image.open(p)).convert("RGB"), [0.5] * 3, [0.5] * 3) for p in paths[:6]]).astype(np.float32)
    feed = {}
    for inp in ms.get_inputs():
        if "input_ids" in inp.name: feed[inp.name] = ids
        elif "pixel" in inp.name: feed[inp.name] = px
        elif "attention" in inp.name: feed[inp.name] = (ids != pad_id).astype(np.int64)
    outs = ms.run(None, feed); on = [o.name for o in ms.get_outputs()]
    L = np.asarray(outs[on.index("logits_per_image")]); ie = unit(np.asarray(outs[on.index("image_embeds")])); te = unit(np.asarray(outs[on.index("text_embeds")]))
    C = ie @ te.T
    A = np.stack([C.ravel(), np.ones(C.size)], 1); sol = np.linalg.lstsq(A, L.ravel(), rcond=None)[0]
    report["siglip_scale_bias"] = [round(float(sol[0]), 3), round(float(sol[1]), 3)]
    print("siglip logit scale / bias:", report["siglip_scale_bias"], flush=True)
except Exception as e:
    print("scale/bias fit failed:", e, flush=True)

lines = ["| model | MB | text→image R@1 | R@5 | R@10 | image→text R@1 | s/img | img/s CPU | img/s CoreML |", "|---|---|---|---|---|---|---|---|---|"]
for k, r in report.items():
    if not isinstance(r, dict):
        continue
    lines.append(f"| {k} | {r['download_mb']} | {r['t2i_R@1']} | {r['t2i_R@5']} | {r['t2i_R@10']} | {r['i2t_R@1']} | {r['embed_s_per_img']} | {r.get('img_per_s_CPUExe')} | {r.get('img_per_s_CoreML')} |")
print("\n".join(lines))
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write("\n".join(lines) + "\n\n```json\n" + json.dumps(report, indent=1)[:60000] + "\n```\n")
json.dump(report, open(WORK / "report.json", "w"), indent=1)
