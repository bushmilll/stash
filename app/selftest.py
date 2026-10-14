"""Checks a running Stash end to end. Used by the build (before and after packaging) so a broken app never ships."""
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


def run(port):
    base = f"http://127.0.0.1:{port}"
    fails = []

    def get(path):
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, r.read()

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
        if not ok:
            fails.append(name)

    st, body = get("/api/settings")
    s = json.loads(body)
    check("server answers", st == 200)
    check("sync engine (gallery-dl) bundled", s.get("_gallery_dl") is True)
    check("ffmpeg bundled", s.get("_ffmpeg") is True)
    for path in ("/", "/static/app.js", "/static/board.js", "/static/style.css", "/static/fonts/InterTight.ttf", "/static/fonts/IBMPlexMono-Regular.ttf"):
        try:
            st, b = get(path)
            check("serves " + path, st == 200 and len(b) > 100)
        except Exception as e:
            check("serves " + path, False, str(e))
    for path in ("/api/sidebar", "/api/posts", "/api/facets", "/api/boards", "/api/library"):
        try:
            st, _ = get(path)
            check("api " + path, st == 200)
        except Exception as e:
            check("api " + path, False, str(e))

    # media pipeline: make a short test video + image, probe, thumbnail, analyze, export a clip
    try:
        from app import analyze, media
        from gallery_dl import extractor
        check("instagram extractor loads", type(extractor.find("https://www.instagram.com/x/saved/y/123/")).__name__ == "InstagramCollectionExtractor")
        tmp = Path(tempfile.mkdtemp())
        vid = tmp / "t.mp4"
        subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=2", "-pix_fmt", "yuv420p", str(vid)], check=True, timeout=120)
        w, h, d = media.probe(vid)
        check("ffmpeg reads video", (w, h) == (640, 360) and d and d > 1.5, f"{w}x{h} {d}")
        thumb = media.make_thumb(vid, "selftest/00")
        check("thumbnail", (media.db.THUMB_DIR / thumb).exists())
        res = analyze.analyze_file(media.db.THUMB_DIR / thumb)
        check("image analysis", res["ar_label"] == "16:9" and len(res["palette"]) > 0, res["ar_label"])
        out = subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(vid), "-t", "1", "-vf", "fps=10,scale=320:-1,split[a][b];[a]palettegen[p];[b][p]paletteuse", str(tmp / "t.gif")], timeout=120)
        check("gif export", out.returncode == 0 and (tmp / "t.gif").exists())
        out = subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(vid), "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp / "c.mp4")], timeout=120)
        check("mp4 export (x264)", out.returncode == 0)
        # Instagram video formats: each must decode to a real (not black) frame and full-res poster
        import numpy as np
        from PIL import Image
        fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
        if not fixtures.exists():
            fixtures = Path.cwd() / "tests" / "fixtures"
        for f in sorted(fixtures.glob("*.mp4")):
            png = tmp / (f.stem + ".png")
            subprocess.run([media.FFMPEG, "-y", "-v", "error", "-i", str(f), "-ss", "1.000", "-frames:v", "1", str(png)], timeout=120)
            ok = png.exists()
            if ok:
                a = np.asarray(Image.open(png).convert("L"), dtype=np.float32)
                ok = a.mean() > 20 and a.std() > 10
            check(f"grab frame from {f.stem}", ok, "" if ok else "blank or unreadable")
            poster = media.make_poster(f, f"selftest/{f.stem}")
            check(f"poster from {f.stem}", bool(poster))
    except Exception as e:
        check("media pipeline", False, repr(e))

    print(f"\n{len(fails)} failed" if fails else "\nall checks passed", flush=True)
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run(int(sys.argv[1]) if len(sys.argv) > 1 else 8765) else 1)
