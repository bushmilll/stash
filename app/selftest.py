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

    import os as _os
    on_ci = bool(_os.environ.get("GITHUB_ACTIONS"))

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
        if not ok:
            fails.append(name)
            if on_ci:   # shows up as a readable note on the build, even without opening the log
                msg = f"{name}: {detail}".replace("%", "%25").replace("\r", "").replace("\n", " | ")[:900]
                print(f"::error title=Self-test failed::{msg}", flush=True)

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

    # on-device AI tags (needs the model download, so only when asked: the build does)
    import os
    if os.environ.get("STASH_AI_TEST"):
        try:
            import numpy as np
            from PIL import Image, ImageDraw, ImageFont
            from app import ai
            if not ai.installed():
                ai._download_all()
            check("AI model downloaded", ai.installed(), ai._state.get("error") or "")
            tmp = Path(tempfile.mkdtemp())
            card = Image.new("RGB", (1080, 1350), (250, 250, 248)); d = ImageDraw.Draw(card)
            font = ImageFont.load_default(size=64)
            for i, line in enumerate(["5 CAMERA SETTINGS", "EVERY FILMMAKER", "SHOULD KNOW", "", "1. Shutter at 180°", "2. Native ISO", "3. Log profile", "4. White balance", "5. Frame rate"]):
                d.text((80, 120 + i * 110), line, fill=(15, 15, 15), font=font)
            card.save(tmp / "card.jpg")
            y = np.linspace(0, 1, 720)[:, None, None]
            film = (np.array([20, 30, 60]) * (1 - y) + np.array([230, 120, 50]) * y) * np.ones((720, 1280, 3))
            fim = Image.fromarray(film.astype(np.uint8)); fd = ImageDraw.Draw(fim)
            fd.rectangle([0, 470, 1280, 720], fill=(8, 8, 12)); fd.ellipse([610, 300, 670, 360], fill=(8, 8, 12)); fd.rectangle([600, 350, 680, 600], fill=(8, 8, 12))
            fim.save(tmp / "film.jpg")
            v1, v2 = ai.embed_image(tmp / "card.jpg"), ai.embed_image(tmp / "film.jpg")
            check("AI image embedding", v1.shape == (512,) and abs(float(np.linalg.norm(v1)) - 1) < 1e-3, str(v1.shape))
            l1, s1 = ai.classify(v1, "")
            l2, s2 = ai.classify(v2, "")
            check("AI tags a text card as text", l1 == "text", f"{l1} {s1}")
            check("AI doesn't tag a film frame as text", l2 != "text", f"{l2} {s2}")
            l3, _ = ai.classify(v2, "my new camera gear review, lens unboxing")
            print(f"  (film frame with a gear caption -> {l3})", flush=True)
            if on_ci:
                print(f"::notice title=AI tags::text card -> {l1}, film frame -> {l2}, film frame + gear caption -> {l3}", flush=True)
        except Exception as e:
            import traceback
            check("AI tags", False, repr(e) + " " + traceback.format_exc()[-600:])

    print(f"\n{len(fails)} failed" if fails else "\nall checks passed", flush=True)
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run(int(sys.argv[1]) if len(sys.argv) > 1 else 8765) else 1)
