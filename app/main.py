"""Stash web server (Starlette). Run: python -m app"""
import json
import os
import re
import sys
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import analyze, db, importer, media, sync

STATIC = Path(__file__).parent / "static"
SORTS = {
    "saved": "COALESCE(p.saved_at, p.added_at) DESC, p.id DESC",
    "saved_old": "COALESCE(p.saved_at, p.added_at) ASC, p.id ASC",
    "posted": "p.posted_at DESC NULLS LAST, p.id DESC",
    "random": "RANDOM()",
}


def err(status, msg):
    return JSONResponse({"error": msg}, status_code=status)


def fts_query(q):
    # every word must match (prefix search), so "neon tok" finds "neon tokyo"
    words = re.findall(r"[\w#@'-]+", q, flags=re.UNICODE)
    return " ".join('"' + w.replace('"', "").lstrip("#@") + '"*' for w in words if w.lstrip("#@"))


# ---------- library ----------

async def list_posts(request):
    qp = request.query_params
    where, args, joins = [], [], []
    status = qp.get("status", "ready")
    if qp.get("trashed"):
        where.append("p.deleted_at IS NOT NULL AND p.status!='removed'")
    else:
        where.append("p.deleted_at IS NULL")
        if status != "all":
            where.append("p.status=?")
            args.append(status)
    if qp.get("q"):
        fq = fts_query(qp["q"])
        if fq:
            joins.append("JOIN search s ON s.rowid=p.id")
            where.append("search MATCH ?")
            args.append(fq)
    if qp.get("collection"):
        where.append("p.id IN (SELECT pc.post_id FROM post_collections pc JOIN collections x "
                     "ON x.id=pc.collection_id WHERE x.name=?)")
        args.append(qp["collection"])
    if qp.get("uncollected"):
        where.append("p.id NOT IN (SELECT post_id FROM post_collections)")
    if qp.get("tag"):
        where.append("p.id IN (SELECT pt.post_id FROM post_tags pt JOIN tags t ON t.id=pt.tag_id WHERE t.name=?)")
        args.append(qp["tag"])
    if qp.get("creator"):
        where.append("p.username=?")
        args.append(qp["creator"])
    kind = qp.get("kind")
    if kind == "video":
        where.append("EXISTS (SELECT 1 FROM media m WHERE m.post_id=p.id AND m.kind='video')")
    elif kind == "image":
        where.append("NOT EXISTS (SELECT 1 FROM media m WHERE m.post_id=p.id AND m.kind='video')")
    if qp.get("favorite"):
        where.append("p.favorite=1")
    # look filters (computed from the pixels of the cover image)
    look = {k: qp.get(k) for k in ("tone", "sat", "temp", "contrast", "ar") if qp.get(k)}
    if look:
        joins.append("JOIN media cm ON cm.post_id=p.id AND cm.idx=0 JOIN analysis a ON a.media_id=cm.id")
        col = {"tone": "a.tone", "sat": "a.sat", "temp": "a.temp", "contrast": "a.contrast", "ar": "a.ar_label"}
        for k, v in look.items():
            vals = v.split(",")
            where.append(f"{col[k]} IN ({','.join('?' * len(vals))})")
            args.extend(vals)
    limit = min(int(qp.get("limit", 60)), 200)
    offset = int(qp.get("offset", 0))
    # ranked searches: by color, by similarity to a post, or by an uploaded image
    ranked = None
    if qp.get("similar"):
        v = analyze.vec_for_post(int(qp["similar"]))
        ranked = analyze.nearest(v, exclude=int(qp["similar"]), k=300) if v is not None else []
    elif qp.get("simimg"):
        res = analyze.image_query(qp["simimg"])
        if res is None:
            return err(410, "That image search has expired. Drop the image in again.")
        ranked = analyze.nearest(res["vec"], k=300)
    if ranked is not None or qp.get("color"):
        base = (f"SELECT p.id, (SELECT a2.palette FROM media m2 JOIN analysis a2 ON a2.media_id=m2.id "
                f"WHERE m2.post_id=p.id AND m2.idx=0) AS pal FROM posts p {' '.join(joins)} "
                f"{'WHERE ' + ' AND '.join(where) if where else ''}")
        cands = {r[0]: r[1] for r in db.conn().execute(base, args)}
        if ranked is not None:
            ids = [i for i in ranked if i in cands]
        else:
            ids = list(cands)
        if qp.get("color"):
            target = analyze.hex_to_lab(qp["color"])
            scored = [(analyze.color_score(cands[i], target), i) for i in ids]
            if ranked is not None:   # keep similarity order, just drop non-matching colors
                ids = [i for s_, i in scored if s_ >= 0.08]
            else:
                ids = [i for s_, i in sorted(scored, key=lambda t: -t[0]) if s_ >= 0.08]
        c = db.conn()
        page = ids[offset:offset + limit]
        rows = {r["id"]: r for r in c.execute(f"SELECT * FROM posts WHERE id IN ({','.join('?' * len(page))})", page)} if page else {}
        items = [db.post_dict(c, rows[i]) for i in page if i in rows]
        return JSONResponse({"items": items, "more": offset + limit < len(ids), "total": len(ids) if offset == 0 else None})
    order = SORTS.get(qp.get("sort", "saved"), SORTS["saved"])
    if qp.get("sort") == "random" and qp.get("seed"):
        # stable shuffle across pages
        seed = int(qp["seed"]) % 100003
        order = f"(p.id * 2654435761 + {seed}) % 1000003"
    sql = (f"SELECT p.* FROM posts p {' '.join(joins)} "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY {order} LIMIT ? OFFSET ?")
    c = db.conn()
    rows = c.execute(sql, args + [limit + 1, offset]).fetchall()
    items = [db.post_dict(c, r) for r in rows[:limit]]
    total = None
    if offset == 0:
        total = c.execute(f"SELECT COUNT(*) FROM posts p {' '.join(joins)} "
                          f"{'WHERE ' + ' AND '.join(where) if where else ''}", args).fetchone()[0]
    return JSONResponse({"items": items, "more": len(rows) > limit, "total": total})


async def get_post(request):
    c = db.conn()
    r = c.execute("SELECT * FROM posts WHERE id=?", (request.path_params["id"],)).fetchone()
    if not r:
        return err(404, "Not found")
    return JSONResponse(db.post_dict(c, r, full=True))


async def update_post(request):
    pid = request.path_params["id"]
    body = await request.json()
    with db.tx() as c:
        if "notes" in body:
            c.execute("UPDATE posts SET notes=? WHERE id=?", (str(body["notes"])[:20000], pid))
        if "favorite" in body:
            c.execute("UPDATE posts SET favorite=? WHERE id=?", (1 if body["favorite"] else 0, pid))
        for name in body.get("add_tags", []):
            if name.strip():
                tid = db.get_or_create(c, "tags", name.strip().lstrip("#"))
                c.execute("INSERT OR IGNORE INTO post_tags VALUES (?,?)", (pid, tid))
        for name in body.get("remove_tags", []):
            c.execute("DELETE FROM post_tags WHERE post_id=? AND tag_id=(SELECT id FROM tags WHERE name=?)",
                      (pid, name))
        for name in body.get("add_collections", []):
            if name.strip():
                db.add_to_collection(c, pid, name.strip(), source="local")
        for name in body.get("remove_collections", []):
            c.execute("DELETE FROM post_collections WHERE post_id=? AND collection_id="
                      "(SELECT id FROM collections WHERE name=?)", (pid, name))
        c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM post_tags)")
        db.reindex(c, pid)
        r = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        return JSONResponse(db.post_dict(c, r, full=True))


async def bulk(request):
    """Apply a tag/collection change to many posts at once."""
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])][:5000]
    with db.tx() as c:
        for pid in ids:
            for name in body.get("add_tags", []):
                tid = db.get_or_create(c, "tags", name.strip().lstrip("#"))
                c.execute("INSERT OR IGNORE INTO post_tags VALUES (?,?)", (pid, tid))
            for name in body.get("add_collections", []):
                db.add_to_collection(c, pid, name.strip(), source="local")
            for name in body.get("remove_collections", []):
                c.execute("DELETE FROM post_collections WHERE post_id=? AND collection_id="
                          "(SELECT id FROM collections WHERE name=?)", (pid, name))
            db.reindex(c, pid)
    return JSONResponse({"ok": True, "count": len(ids)})


async def sidebar(request):
    c = db.conn()
    cols = [dict(r) for r in c.execute(
        "SELECT x.name, x.source, COUNT(p.id) AS n FROM collections x "
        "LEFT JOIN post_collections pc ON pc.collection_id=x.id "
        "LEFT JOIN posts p ON p.id=pc.post_id AND p.status='ready' AND p.deleted_at IS NULL "
        "GROUP BY x.id ORDER BY x.name COLLATE NOCASE")]
    tags = [dict(r) for r in c.execute(
        "SELECT t.name, COUNT(*) AS n FROM tags t JOIN post_tags pt ON pt.tag_id=t.id "
        "GROUP BY t.id ORDER BY n DESC, t.name LIMIT 300")]
    creators = [dict(r) for r in c.execute(
        "SELECT username AS name, COUNT(*) AS n FROM posts WHERE status='ready' AND deleted_at IS NULL AND username!='' "
        "GROUP BY username ORDER BY n DESC LIMIT 40")]
    counts = dict(c.execute(
        "SELECT 'ready', COUNT(*) FROM posts WHERE status='ready' AND deleted_at IS NULL UNION ALL "
        "SELECT 'pending', COUNT(*) FROM posts WHERE status IN ('pending','failed') AND deleted_at IS NULL UNION ALL "
        "SELECT 'gone', COUNT(*) FROM posts WHERE status='gone' AND deleted_at IS NULL UNION ALL "
        "SELECT 'favorite', COUNT(*) FROM posts WHERE status='ready' AND deleted_at IS NULL AND favorite=1 UNION ALL "
        "SELECT 'trash', COUNT(*) FROM posts WHERE deleted_at IS NOT NULL AND status!='removed' UNION ALL "
        "SELECT 'uncollected', COUNT(*) FROM posts WHERE status='ready' AND deleted_at IS NULL AND id NOT IN "
        "(SELECT post_id FROM post_collections)").fetchall())
    boards = [dict(r) for r in c.execute(
        "SELECT id, name, items FROM boards ORDER BY updated_at DESC")]
    for b in boards:
        try:
            b["n"] = len(json.loads(b.pop("items") or "[]"))
        except Exception:
            b["n"] = 0
    return JSONResponse({"collections": cols, "tags": tags, "creators": creators, "counts": counts, "boards": boards})


async def create_collection(request):
    name = ((await request.json()).get("name") or "").strip()[:120]
    if not name:
        return err(400, "Give the collection a name.")
    with db.tx() as c:
        db.get_or_create(c, "collections", name, source="local")
    return JSONResponse({"ok": True, "name": name})


async def rename_collection(request):
    body = await request.json()
    old, new = body.get("old", ""), (body.get("new") or "").strip()
    with db.tx() as c:
        if not new:
            # delete the collection (posts stay in the library)
            ids = [r[0] for r in c.execute(
                "SELECT post_id FROM post_collections WHERE collection_id=(SELECT id FROM collections WHERE name=?)",
                (old,))]
            c.execute("DELETE FROM collections WHERE name=?", (old,))
        else:
            ids = [r[0] for r in c.execute(
                "SELECT post_id FROM post_collections WHERE collection_id=(SELECT id FROM collections WHERE name=?)",
                (old,))]
            target = c.execute("SELECT id FROM collections WHERE name=? AND name!=?", (new, old)).fetchone()
            if target:  # merge into existing
                c.execute("UPDATE OR IGNORE post_collections SET collection_id=? WHERE collection_id="
                          "(SELECT id FROM collections WHERE name=?)", (target[0], old))
                c.execute("DELETE FROM collections WHERE name=?", (old,))
            else:
                c.execute("UPDATE collections SET name=? WHERE name=?", (new, old))
        for pid in ids:
            db.reindex(c, pid)
    return JSONResponse({"ok": True})


# ---------- media tools ----------

def _file_response(path: Path, download=True):
    return FileResponse(path, filename=path.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


async def download_media(request):
    try:
        r = media.media_row(request.path_params["id"])
    except FileNotFoundError:
        return err(404, "Not found")
    src = db.MEDIA_DIR / r["path"]
    return FileResponse(src, filename=media.nice_stem(r) + src.suffix, content_disposition_type="attachment")


async def tool(request):
    mid = request.path_params["id"]
    action = request.path_params["action"]
    body = await request.json() if request.method == "POST" else {}
    try:
        if action == "still":
            out = await run_in_threadpool(media.still, mid, float(body.get("t", 0)))
        elif action == "shots":
            out = await run_in_threadpool(media.shots, mid, float(body.get("threshold", 0.3)))
        elif action == "clip":
            out = await run_in_threadpool(media.clip, mid, body["start"], body["end"],
                                          body.get("format", "mp4"), body.get("width"), body.get("fps"))
        else:
            return err(404, "Unknown tool")
    except FileNotFoundError:
        return err(404, "Not found")
    except Exception as e:
        return err(500, str(e))
    return JSONResponse({"url": f"/exports/{out.name}", "name": out.name, "size": out.stat().st_size})


async def post_zip(request):
    out = await run_in_threadpool(media.post_zip, request.path_params["id"])
    return _file_response(out)


async def export_file(request):
    name = Path(request.path_params["name"]).name
    p = db.EXPORT_DIR / name
    if not p.is_file():
        raise HTTPException(404)
    return _file_response(p)


# ---------- import / sync / settings ----------

async def upload_export(request):
    form = await request.form()
    files = {}
    for _, up in form.multi_items():
        if hasattr(up, "read"):
            raw = await up.read()
            files.update(importer.read_upload(up.filename or "upload.json", raw))
    if not files:
        return err(400, "No saved-posts JSON found. Make sure you requested the export in JSON format.")
    result = await run_in_threadpool(importer.import_export, files)
    if result["posts_found"] == 0:
        return err(400, "Couldn't find any saved posts in that file. Use the JSON export, "
                        "and include 'Saved' in the information you request.")
    return JSONResponse(result)


async def settings(request):
    if request.method == "PUT":
        return JSONResponse(db.save_settings(await request.json()))
    s = db.load_settings()
    s["_gallery_dl"] = sync.HAVE_GDL
    s["_ffmpeg"] = media.ffmpeg_ok()
    s["_desktop"] = os.environ.get("STASH_DESKTOP") == "1"
    from . import __version__
    s["_version"] = __version__
    s["_data_dir"] = str(db.DATA_DIR)
    return JSONResponse(s)


async def jobs(request):
    if request.method == "POST":
        body = await request.json()
        if body.get("action") == "cancel":
            sync.cancel()
            return JSONResponse({"ok": True})
        kinds = body.get("kinds") or ["sync"]
        return JSONResponse({"ids": [sync.enqueue(k) for k in kinds if k in ("sync", "backfill")]})
    rows = db.conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
    return JSONResponse([dict(r) for r in rows])



async def search_image(request):
    import tempfile
    form = await request.form()
    up = next((v for _, v in form.multi_items() if hasattr(v, "read")), None)
    if up is None:
        return err(400, "Drop an image to search with.")
    raw = await up.read()
    def work():
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            f.write(raw)
            path = f.name
        try:
            return analyze.remember_image_query(path)
        finally:
            import os
            os.unlink(path)
    try:
        token, res = await run_in_threadpool(work)
    except Exception:
        return err(400, "Couldn't read that image. Try a JPG or PNG.")
    return JSONResponse({"token": token, "palette": res["palette"], "ar_label": res["ar_label"]})


async def facets(request):
    """Counts per look filter, for the filter menus."""
    c = db.conn()
    out = {}
    for k, col in (("tone", "tone"), ("sat", "sat"), ("temp", "temp"), ("contrast", "contrast"), ("ar", "ar_label")):
        out[k] = {r[0]: r[1] for r in c.execute(
            f"SELECT a.{col}, COUNT(*) FROM posts p JOIN media m ON m.post_id=p.id AND m.idx=0 "
            f"JOIN analysis a ON a.media_id=m.id WHERE p.deleted_at IS NULL AND p.status='ready' AND a.{col} IS NOT NULL "
            f"GROUP BY a.{col}")}
    out["pending"] = len(analyze.pending(100000))
    return JSONResponse(out)


# ---------- desktop helpers (the Mac app's window can't download files on its own) ----------

def _downloads_dir():
    d = Path.home() / "Downloads"
    d.mkdir(exist_ok=True)
    return d


def _unique(dest: Path):
    if not dest.exists():
        return dest
    for i in range(2, 1000):
        cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not cand.exists():
            return cand
    return dest


def _resolve_download(url):
    """Map one of the app's download URLs to (source file, nice filename)."""
    url = url.split("?")[0]
    if m_ := re.fullmatch(r"/exports/([^/]+)", url):
        p = db.EXPORT_DIR / Path(m_[1]).name
        return p, p.name
    if m_ := re.fullmatch(r"/api/media/(\d+)/download", url):
        r = media.media_row(int(m_[1]))
        src = db.MEDIA_DIR / r["path"]
        return src, media.nice_stem(r) + src.suffix
    if m_ := re.fullmatch(r"/api/posts/(\d+)/zip", url):
        out = media.post_zip(int(m_[1]))
        return out, out.name
    raise FileNotFoundError(url)


async def save_to_downloads(request):
    import shutil
    body = await request.json()
    try:
        src, name = await run_in_threadpool(_resolve_download, body.get("url", ""))
    except FileNotFoundError:
        return err(404, "That file isn't available anymore.")
    dest = _unique(_downloads_dir() / name)
    await run_in_threadpool(shutil.copy2, src, dest)
    return JSONResponse({"path": str(dest), "name": dest.name})


async def save_blob(request):
    name = Path(request.query_params.get("name", "stash-export.png")).name
    dest = _unique(_downloads_dir() / name)
    dest.write_bytes(await request.body())
    return JSONResponse({"path": str(dest), "name": dest.name})


def _open(target, reveal=False):
    try:
        _open_inner(target, reveal)
    except Exception:
        pass


def _open_inner(target, reveal):
    import subprocess
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", target] if reveal else ["open", target])
    elif sys.platform.startswith("win"):
        os.startfile(os.path.dirname(target) if reveal else target)
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(target) if reveal else target])


async def reveal(request):
    body = await request.json()
    path = body.get("path") or ""
    if body.get("what") == "library":
        path = str(db.DATA_DIR)
    p = Path(path)
    # only ever reveal things in Downloads or the library
    if not p.exists() or not (p.is_relative_to(_downloads_dir()) or p.is_relative_to(db.DATA_DIR)):
        return err(404, "Couldn't find that file.")
    _open(str(p), reveal=p.is_file())
    return JSONResponse({"ok": True})


async def open_url(request):
    body = await request.json()
    url = body.get("url", "")
    if not re.match(r"^https://(www\.)?instagram\.com/|^https://github\.com/", url):
        return err(400, "Only Instagram and GitHub links open from here.")
    import webbrowser
    webbrowser.open(url)
    return JSONResponse({"ok": True})


# ---------- trash ----------

async def trash_posts(request):
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])][:20000]
    restore = bool(body.get("restore"))
    with db.tx() as c:
        for pid in ids:
            c.execute("UPDATE posts SET deleted_at=? WHERE id=?", (None if restore else db.now(), pid))
    return JSONResponse({"ok": True, "count": len(ids)})


async def purge_trash(request):
    """Permanently delete trashed posts (their files too). ids omitted = everything in the trash."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    def work():
        with db.tx() as c:
            if body.get("ids"):
                ids = [int(i) for i in body["ids"]]
                ids = [r[0] for r in c.execute(
                    f"SELECT id FROM posts WHERE deleted_at IS NOT NULL AND id IN ({','.join('?' * len(ids))})", ids)]
            else:
                ids = [r[0] for r in c.execute("SELECT id FROM posts WHERE deleted_at IS NOT NULL AND status!='removed'")]
            return len(ids), media.purge_posts(c, ids)
    n, freed = await run_in_threadpool(work)
    return JSONResponse({"ok": True, "count": n, "freed": freed})


# ---------- batch export ----------

async def export_batch(request):
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])]
    name = body.get("name") or "Stash export"
    if body.get("collection"):
        ids = [r[0] for r in db.conn().execute(
            "SELECT p.id FROM posts p JOIN post_collections pc ON pc.post_id=p.id "
            "JOIN collections x ON x.id=pc.collection_id WHERE x.name=? AND p.deleted_at IS NULL "
            "ORDER BY COALESCE(p.saved_at,p.added_at) DESC", (body["collection"],))]
        name = body["collection"]
    if not ids:
        return err(400, "Nothing to export.")
    try:
        out, files = await run_in_threadpool(
            media.export_batch, ids, name, body.get("images", True), body.get("videos", True), body.get("metadata", True))
    except RuntimeError as e:
        return err(400, str(e))
    return JSONResponse({"url": f"/exports/{out.name}", "name": out.name, "size": out.stat().st_size, "files": files})


# ---------- backups / library health ----------

async def library_info(request):
    def work():
        return {"size": db.dir_size(db.MEDIA_DIR) + db.dir_size(db.THUMB_DIR),
                "backups": db.list_backups(), "backup_dir": str(db.BACKUP_DIR), "ffmpeg": media.ffmpeg_ok()}
    return JSONResponse(await run_in_threadpool(work))


async def backup_now(request):
    dest = await run_in_threadpool(db.backup_now)
    return JSONResponse({"ok": True, "name": dest.name})


async def library_check(request):
    return JSONResponse(await run_in_threadpool(media.check_library))


# ---------- moodboards ----------

def _board_items(c, raw):
    """Hydrate stored board items with live media info; drop media that no longer exists."""
    out = []
    for it in json.loads(raw or "[]"):
        if it.get("type") == "text":
            out.append(it)
            continue
        m = c.execute("SELECT m.id, m.kind, m.path, m.thumb, m.width, m.height, p.username, p.shortcode, p.caption, p.id AS post_id "
                      "FROM media m JOIN posts p ON p.id=m.post_id WHERE m.id=? AND p.deleted_at IS NULL "
                      "AND p.status!='removed'", (it.get("media_id"),)).fetchone()
        if not m:
            continue
        it = dict(it, type="media", kind=m["kind"], src="/files/media/" + m["path"],
                  thumb_src="/files/thumbs/" + m["thumb"] if m["thumb"] else None,
                  mw=m["width"], mh=m["height"], username=m["username"], shortcode=m["shortcode"], post_id=m["post_id"],
                  caption=(m["caption"] or "")[:200])
        out.append(it)
    return out


def _board_dict(c, r, with_items=True):
    d = {"id": r["id"], "name": r["name"], "bg": r["bg"], "width": r["width"], "gap": r["gap"]}
    if with_items:
        d["items"] = _board_items(c, r["items"])
    return d


async def boards(request):
    c = db.conn()
    if request.method == "POST":
        body = await request.json()
        media_ids = [int(i) for i in body.get("media_ids", [])]
        for pid in body.get("post_ids", []):
            m = c.execute("SELECT id FROM media WHERE post_id=? ORDER BY idx LIMIT 1", (int(pid),)).fetchone()
            if m:
                media_ids.append(m[0])
        if body.get("collection"):
            lim = int(body.get("limit", 60))
            rows = c.execute(
                "SELECT (SELECT id FROM media WHERE post_id=p.id ORDER BY idx LIMIT 1) FROM posts p "
                "JOIN post_collections pc ON pc.post_id=p.id JOIN collections x ON x.id=pc.collection_id "
                "WHERE x.name=? AND p.deleted_at IS NULL AND p.status='ready' "
                "ORDER BY COALESCE(p.saved_at,p.added_at) DESC LIMIT ?", (body["collection"], lim)).fetchall()
            media_ids += [r[0] for r in rows if r[0]]
        items = [{"type": "media", "media_id": mid} for mid in dict.fromkeys(media_ids)]
        with db.tx() as w:
            w.execute("INSERT INTO boards (name, bg, items, created_at, updated_at) VALUES (?,?,?,?,?)",
                      ((body.get("name") or "Untitled moodboard").strip()[:120], "#eceae5", json.dumps(items), db.now(), db.now()))
            bid = w.execute("SELECT last_insert_rowid()").fetchone()[0]
        return JSONResponse({"id": bid, "count": len(items)})
    rows = c.execute("SELECT * FROM boards ORDER BY updated_at DESC").fetchall()
    return JSONResponse([_board_dict(c, r, with_items=False) for r in rows])


async def board_one(request):
    bid = request.path_params["id"]
    c = db.conn()
    r = c.execute("SELECT * FROM boards WHERE id=?", (bid,)).fetchone()
    if not r:
        return err(404, "Moodboard not found")
    if request.method == "GET":
        return JSONResponse(_board_dict(c, r))
    if request.method == "DELETE":
        with db.tx() as w:
            w.execute("DELETE FROM boards WHERE id=?", (bid,))
        return JSONResponse({"ok": True})
    body = await request.json()   # PUT
    keep = ("type", "media_id", "x", "y", "w", "z", "h", "text", "size", "color", "fit", "num", "font", "auto", "role")
    items = [{k: it[k] for k in keep if k in it} for it in body.get("items", [])][:500]
    with db.tx() as w:
        w.execute("UPDATE boards SET name=?, bg=?, width=?, gap=?, items=?, updated_at=? WHERE id=?",
                  ((body.get("name") or r["name"]).strip()[:120], body.get("bg", r["bg"]),
                   int(body.get("width", r["width"])), int(body.get("gap", r["gap"])), json.dumps(items), db.now(), bid))
    return JSONResponse({"ok": True})


async def board_add(request):
    bid = request.path_params["id"]
    body = await request.json()
    c = db.conn()
    r = c.execute("SELECT items FROM boards WHERE id=?", (bid,)).fetchone()
    if not r:
        return err(404, "Moodboard not found")
    items = json.loads(r["items"] or "[]")
    have = {i.get("media_id") for i in items if i.get("type") == "media"}
    media_ids = [int(i) for i in body.get("media_ids", [])]
    for pid in body.get("post_ids", []):
        m = c.execute("SELECT id FROM media WHERE post_id=? ORDER BY idx LIMIT 1", (int(pid),)).fetchone()
        if m:
            media_ids.append(m[0])
    added = 0
    for mid in dict.fromkeys(media_ids):
        if mid not in have:
            items.append({"type": "media", "media_id": mid})
            added += 1
    with db.tx() as w:
        w.execute("UPDATE boards SET items=?, updated_at=? WHERE id=?", (json.dumps(items), db.now(), bid))
    return JSONResponse({"ok": True, "added": added})


async def board_pdf(request):
    """Wrap the PNG the browser rendered into a PDF page (same size, no recompression of the image)."""
    from io import BytesIO
    from PIL import Image
    raw = await request.body()
    def work():
        im = Image.open(BytesIO(raw)).convert("RGB")
        name = _safe_name(request.query_params.get("name", "moodboard"))
        out = db.EXPORT_DIR / f"{name}_{int(time.time())}.pdf"
        im.save(out, "PDF", resolution=144.0, quality=95)
        return out
    out = await run_in_threadpool(work)
    return JSONResponse({"url": f"/exports/{out.name}", "name": out.name, "size": out.stat().st_size})


def _safe_name(n):
    return media._safe(n, "moodboard")


async def server_error(request, exc):
    import traceback
    traceback.print_exc()
    return JSONResponse({"error": f"Something went wrong on the server: {type(exc).__name__}. "
                                  "If it keeps happening, restart Stash."}, status_code=500)


async def index(request):
    return FileResponse(STATIC / "index.html")


def create_app():
    db.init()
    sync.start()
    analyze.start()
    routes = [
        Route("/", index),
        Route("/api/posts", list_posts),
        Route("/api/posts/bulk", bulk, methods=["POST"]),
        Route("/api/posts/trash", trash_posts, methods=["POST"]),
        Route("/api/trash/purge", purge_trash, methods=["POST"]),
        Route("/api/export", export_batch, methods=["POST"]),
        Route("/api/search-image", search_image, methods=["POST"]),
        Route("/api/facets", facets),
        Route("/api/desktop/save", save_to_downloads, methods=["POST"]),
        Route("/api/desktop/save-blob", save_blob, methods=["POST"]),
        Route("/api/desktop/reveal", reveal, methods=["POST"]),
        Route("/api/desktop/open-url", open_url, methods=["POST"]),
        Route("/api/library", library_info),
        Route("/api/library/backup", backup_now, methods=["POST"]),
        Route("/api/library/check", library_check, methods=["POST"]),
        Route("/api/boards", boards, methods=["GET", "POST"]),
        Route("/api/boards/pdf", board_pdf, methods=["POST"]),
        Route("/api/boards/{id:int}", board_one, methods=["GET", "PUT", "DELETE"]),
        Route("/api/boards/{id:int}/add", board_add, methods=["POST"]),
        Route("/api/posts/{id:int}", get_post),
        Route("/api/posts/{id:int}", update_post, methods=["PATCH"]),
        Route("/api/posts/{id:int}/zip", post_zip),
        Route("/api/sidebar", sidebar),
        Route("/api/collections", create_collection, methods=["POST"]),
        Route("/api/collections/rename", rename_collection, methods=["POST"]),
        Route("/api/media/{id:int}/download", download_media),
        Route("/api/media/{id:int}/{action}", tool, methods=["POST"]),
        Route("/api/import", upload_export, methods=["POST"]),
        Route("/api/settings", settings, methods=["GET", "PUT"]),
        Route("/api/jobs", jobs, methods=["GET", "POST"]),
        Route("/exports/{name}", export_file),
        Mount("/files/media", StaticFiles(directory=db.MEDIA_DIR), name="media"),
        Mount("/files/thumbs", StaticFiles(directory=db.THUMB_DIR), name="thumbs"),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, exception_handlers={Exception: server_error})


app = create_app()
