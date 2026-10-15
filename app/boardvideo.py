"""Moodboard → MP4. The app sends the board as still layers (PNG) with the looping video tiles between them,
in stacking order. Here each loop is cut and fitted to its tile, then everything is stacked with ffmpeg
and the loops repeat until the chosen length.
"""
import json
import math
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import db, media

FPS = 30
_jobs = {}


def _even(n):
    return max(2, int(round(n / 2)) * 2)


def _cut(src, vin, vout, w, h, fit, bg, out):
    """The tile's section, fitted to the tile, at 30 fps."""
    w, h = _even(w), _even(h)
    if fit:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg}"
    else:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    dur = None if vout is None else max(0.1, float(vout) - float(vin))
    cmd = [media.FFMPEG, "-y", "-v", "error", "-ss", f"{float(vin):.3f}"] + (["-t", f"{dur:.3f}"] if dur else []) + \
          ["-i", str(src), "-an", "-vf", f"{vf},fps={FPS},setsar=1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
           "-pix_fmt", "yuv420p", str(out)]
    media.run(cmd, timeout=900)
    return w, h


def render(job, spec, layer_files):
    work = Path(tempfile.mkdtemp(prefix="stash-boardvid-"))
    try:
        W, H, dur = _even(spec["width"]), _even(spec["height"]), float(spec["duration"])
        inputs, chain = [], []
        vids = [l for l in spec["layers"] if l["type"] == "video"]
        done = 0
        for l in spec["layers"]:
            if l["type"] == "png":
                inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(layer_files[l["file"]])]
                chain.append(("png", None))
            else:
                _jobs[job]["message"] = f"Cutting loop {done + 1} of {len(vids)}"
                r = media.media_row(l["media_id"])
                src = db.media_path(r["path"])
                if not src.exists():
                    raise RuntimeError("A video on this board isn't available (is your storage folder connected?)")
                seg = work / f"seg{done}.mp4"
                w, h = _cut(src, l.get("vin") or 0, l.get("vout"), l["w"], l["h"], l.get("fit"), l.get("bg", "#000000"), seg)
                inputs += ["-stream_loop", "-1", "-i", str(seg)]
                chain.append(("video", dict(l, w=w, h=h)))
                done += 1
        # stack: first layer is the opaque background; everything else goes on top in order
        parts, cur = [], "[0:v]"
        parts.append(f"{cur}scale={W}:{H},format=rgba[b0]")
        cur = "[b0]"
        for i, (kind, l) in enumerate(chain[1:], start=1):
            nxt = f"[b{i}]"
            if kind == "png":
                parts.append(f"{cur}[{i}:v]overlay=0:0:format=auto{nxt}")
            else:
                x, y, w, h, rot = l["x"], l["y"], l["w"], l["h"], float(l.get("rot") or 0)
                if rot:
                    a = math.radians(rot)
                    rw = abs(w * math.cos(a)) + abs(h * math.sin(a)); rh = abs(w * math.sin(a)) + abs(h * math.cos(a))
                    ox, oy = x + w / 2 - rw / 2, y + h / 2 - rh / 2
                    parts.append(f"[{i}:v]format=rgba,rotate={a:.5f}:c=none:ow=rotw({a:.5f}):oh=roth({a:.5f})[r{i}]")
                    parts.append(f"{cur}[r{i}]overlay={ox:.1f}:{oy:.1f}:format=auto{nxt}")
                else:
                    parts.append(f"{cur}[{i}:v]overlay={x:.1f}:{y:.1f}:format=auto{nxt}")
            cur = nxt
        parts.append(f"{cur}format=yuv420p[out]")
        name = media._safe(spec.get("name") or "moodboard", "moodboard")
        out = db.EXPORT_DIR / f"{name}_{int(time.time())}.mp4"
        script = work / "graph.txt"
        script.write_text(";\n".join(parts))
        base = [media.FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats", *inputs,
                "-filter_complex_script", str(script), "-map", "[out]", "-t", f"{dur:.3f}", "-r", str(FPS)]
        tail = ["-movflags", "+faststart", str(out)]
        encoders = [["-c:v", "libx264", "-preset", "medium", "-crf", "14", "-tune", "film"], ["-c:v", "h264_videotoolbox", "-b:v", "24M"]]
        last_err = ""
        for enc in encoders:
            _jobs[job]["message"] = "Rendering 0%"
            p = subprocess.Popen(base + enc + tail, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in p.stdout:
                if line.startswith("out_time_ms="):
                    try:
                        sec = int(line.split("=")[1]) / 1e6
                        _jobs[job]["message"] = f"Rendering {min(99, int(sec / dur * 100))}%"
                    except ValueError:
                        pass
            p.wait()
            if p.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                _jobs[job].update(status="done", url=f"/exports/{out.name}", name=out.name, message="Done")
                return
            last_err = (p.stderr.read() or "")[-600:]
        raise RuntimeError("ffmpeg couldn't make the video: " + last_err.strip().splitlines()[-1] if last_err.strip() else "ffmpeg failed")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _jobs[job].update(status="error", error=str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for f in layer_files:
            Path(f).unlink(missing_ok=True)


def start(spec, layer_bytes):
    job = uuid.uuid4().hex[:10]
    tmp = Path(tempfile.mkdtemp(prefix="stash-layers-"))
    files = []
    for i, b in enumerate(layer_bytes):
        f = tmp / f"layer{i}.png"
        f.write_bytes(b)
        files.append(f)
    _jobs[job] = {"status": "running", "message": "Starting", "started": time.time()}
    threading.Thread(target=render, args=(job, spec, files), daemon=True).start()
    if len(_jobs) > 20:
        for k in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:
            _jobs.pop(k, None)
    return job


def status(job):
    return _jobs.get(job)
