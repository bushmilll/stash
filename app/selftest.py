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
    for path in ("/api/sidebar", "/api/posts", "/api/facets", "/api/boards", "/api/library", "/api/update", "/api/techniques"):
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
        # camera movement: a slow pan across a test pattern, and a locked-off shot
        from app import techniques
        pan = tmp / "pan.mp4"
        subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=4", "-vf",
                        "crop=640:360:x='t*120':y=180,format=yuv420p", str(pan)], check=True, timeout=120)
        mt = techniques.analyze_motion(pan, 4)
        check("movement: pan detected", "pan" in mt, str(mt))
        mt2 = techniques.analyze_motion(vid, 2)
        check("movement: no pan on a still shot", "pan" not in mt2 and "whip" not in mt2, str(mt2))
        # composition is measured from the picture: a figure on the left reads as left heavy
        from PIL import ImageDraw
        comp = Image.new("RGB", (960, 540), (70, 70, 80)); cd = ImageDraw.Draw(comp)
        cd.ellipse([190, 170, 250, 230], fill=(10, 10, 10)); cd.rectangle([165, 225, 275, 540], fill=(10, 10, 10))
        comp.save(tmp / "comp.png")
        ct, cx = techniques.composition(tmp / "comp.png")
        check("composition: left heavy", "left_heavy" in ct, f"{sorted(ct)} weight at {cx:.2f}")
        # color profiles: a small red accent on a grey frame is found; a video's color is found mid-clip
        from app import analyze
        acc = Image.new("RGB", (900, 600), (120, 120, 125)); ImageDraw.Draw(acc).rectangle([600, 100, 690, 220], fill=(200, 20, 30))
        acc.save(tmp / "accent.png")
        with Image.open(tmp / "accent.png") as im_:
            cols = analyze._frame_colors(im_)
        red = [c_ for c_ in cols if c_[1] > 40]
        check("color: small accent kept in the breakdown", bool(red), str(cols[:4]))
        cv = tmp / "colorclip.mp4"
        subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x404040:s=320x180:d=2",
                        "-f", "lavfi", "-i", "color=c=0x1fb5ad:s=320x180:d=2", "-filter_complex", "[0][1]concat=n=2:v=1",
                        "-pix_fmt", "yuv420p", str(cv)], check=True, timeout=120)
        moments = analyze._video_moments(cv, 4)
        teal = [t for t, im_ in moments if any(abs(c_[1] + 30) < 15 and abs(c_[2] + 8) < 15 for c_ in analyze._frame_colors(im_))]
        check("color: teal found in the second half of a clip", bool(teal) and min(teal) >= 1.5, f"{len(moments)} moments, teal at {teal}")
        # moodboard video: one still layer + one looping tile
        from app import boardvideo
        Image.new("RGB", (640, 360), (236, 234, 229)).save(tmp / "layer0.png")
        seg_ok = True
        try:
            boardvideo._cut(pan, 0.5, 1.5, 200, 120, True, "#dddddd", tmp / "seg.mp4")
        except Exception as e:
            seg_ok = False
            check("moodboard video: cut a loop", False, repr(e))
        if seg_ok:
            out = subprocess.run([media.FFMPEG, "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-i", str(tmp / "layer0.png"),
                                  "-stream_loop", "-1", "-i", str(tmp / "seg.mp4"), "-filter_complex",
                                  "[0:v]format=rgba[b];[1:v]format=rgba,rotate=0.05:c=none:ow=rotw(0.05):oh=roth(0.05)[r];[b][r]overlay=40:40,format=yuv420p[o]",
                                  "-map", "[o]", "-t", "3", "-c:v", "libx264", "-crf", "14", str(tmp / "board.mp4")], timeout=180)
            check("moodboard video export", out.returncode == 0 and (tmp / "board.mp4").stat().st_size > 1000)
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
            check("AI model in use", ai.active() == ai.TARGET, ai.active())
            check("AI image embedding", v1.ndim == 1 and v1.shape[0] >= 256 and abs(float(np.linalg.norm(v1)) - 1) < 1e-3, str(v1.shape))
            tv = ai.embed_text("a photo of a red car")
            check("AI text embedding matches image size", tv.shape == v1.shape, f"{tv.shape} vs {v1.shape}")
            l1, s1 = ai.classify(v1, "")
            l2, s2 = ai.classify(v2, "")
            check("AI: text graphic is not a film frame", l1 != "film", f"{l1} {s1}")
            check("AI: text graphic scores more text than film", s1["text"] > s1["film"], f"{s1}")
            check("AI: film frame is a film frame", l2 == "film", f"{l2} {s2}")
            l3, _ = ai.classify(v2, "my new camera gear review, lens unboxing")
            print(f"  (film frame with a gear caption -> {l3})", flush=True)
            if on_ci:
                print(f"::notice title=AI tags::text card -> {l1}, film frame -> {l2}, film frame + gear caption -> {l3}", flush=True)
            # visual search: the word FLOWER appears only between 0:02 and 0:04 of a 6 second video
            plain = fim.resize((720, 1280))
            word = Image.new("RGB", (720, 1280), (245, 240, 230)); wd = ImageDraw.Draw(word)
            wd.text((60, 560), "FLOWER", fill=(20, 20, 20), font=ImageFont.load_default(size=150))
            plain.save(tmp / "a.png"); word.save(tmp / "b.png")
            vid = tmp / "hit.mp4"
            subprocess.run([media.FFMPEG, "-v", "error", "-y", "-loop", "1", "-t", "2", "-i", str(tmp / "a.png"),
                            "-loop", "1", "-t", "2", "-i", str(tmp / "b.png"), "-loop", "1", "-t", "2", "-i", str(tmp / "a.png"),
                            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p", "-r", "10", str(vid)], check=True, timeout=120)
            fdir = tmp / "frames"; fdir.mkdir()
            frames = ai._video_frames(vid, 6.0, str(fdir))
            tensors, times = [], []
            for tt, im in frames:
                for sq in ai._square_crops(im):
                    tensors.append(ai._to_tensor(sq)); times.append(tt)
            # real photos: a red and a blue square read as red / blue (checks colors and words line up for this model)
            red = Image.new("RGB", (400, 400), (200, 25, 30)); blue = Image.new("RGB", (400, 400), (30, 60, 210))
            red.save(tmp / "red.png"); blue.save(tmp / "blue.png")
            vr, vb = ai.embed_image(tmp / "red.png"), ai.embed_image(tmp / "blue.png")
            qr, qb = ai.embed_query("red"), ai.embed_query("blue")
            check("AI: words and pictures line up (red vs blue)", float(vr @ qr) > float(vb @ qr) and float(vb @ qb) > float(vr @ qb),
                  f"red img: red {float(vr @ qr):.3f} blue {float(vr @ qb):.3f}; blue img: red {float(vb @ qr):.3f} blue {float(vb @ qb):.3f}")
            vecs = ai.embed_batch(tensors)
            q = ai.embed_query("flower")
            sims = vecs @ q
            best_t = times[int(np.argmax(sims))]
            check("AI search finds the moment in a video", 2.0 <= best_t < 4.0, f"best match at {best_t}s, frames {sorted(set(times))}")
            # technique tags: the silhouette frame should read as a silhouette; the text card as typography
            from app import techniques
            # a stand-in "library": the fixture frames plus random crops of the test images, so tags are judged relative to it
            lib = [ai.embed_image(pth) for pth in (tmp / "card.jpg", tmp / "film.jpg", tmp / "a.png", tmp / "b.png")]
            rng = np.random.default_rng(1)
            for k in range(60):
                arr = (rng.random((240, 320, 3)) * 255).astype(np.uint8) if k % 3 == 0 else np.asarray(fim.resize((320, 180)).rotate(k * 7))
                Image.fromarray(arr).save(tmp / f"lib{k}.jpg")
                lib.append(ai.embed_image(tmp / f"lib{k}.jpg"))
            L = np.stack(lib)
            raw = techniques._raw(techniques._norm(L))
            techniques._stats.update(n=10 ** 9, v={k: (float(a.mean()), float(a.std()) + 1e-6) for k, a in raw.items()})
            tf = techniques.frame_tags([0.0], v2[None])
            tc = techniques.frame_tags([0.0], v1[None])
            techniques._stats.update(n=-1)
            check("AI technique tags run", isinstance(tf, dict) and isinstance(tc, dict))
            kept = {k for k in tc if k in techniques.ANY_CONTENT} if l1 in techniques.NOT_FOOTAGE else set(tc)
            check("AI: text card gets no camera/look tags", not (kept - techniques.ANY_CONTENT), f"{l1}: {sorted(kept)}")
            if on_ci:
                print(f"::notice title=Technique tags::silhouette frame -> {sorted(tf)}; text card -> {sorted(tc)}", flush=True)
                fw = ai.embed_image(tmp / "b.png")
                plainv = ai.embed_image(tmp / "a.png")
                for name, vv in (("silhouette", v2), ("textcard", v1), ("word", fw), ("plain", plainv)):
                    sc = techniques.frame_scores(vv)
                    print(f"::notice title=Scores {name}::" + " ".join(f"{k}={x}" for k, x in sorted(sc.items(), key=lambda kv: -kv[1])), flush=True)
            sc_word, sc_film = float(ai.embed_image(tmp / "b.png") @ q), float(ai.embed_image(tmp / "film.jpg") @ q)
            check("AI search ranks the matching image higher", sc_word > sc_film, f"{sc_word:.3f} vs {sc_film:.3f}")
            if on_ci:
                print(f"::notice title=AI search::'flower' best moment {best_t}s; image scores {sc_word:.3f} vs {sc_film:.3f}", flush=True)
        except Exception as e:
            import traceback
            check("AI tags", False, repr(e) + " " + traceback.format_exc()[-600:])

    print(f"\n{len(fails)} failed" if fails else "\nall checks passed", flush=True)
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run(int(sys.argv[1]) if len(sys.argv) > 1 else 8765) else 1)
