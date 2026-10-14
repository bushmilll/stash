"""Import saved posts + collections from Instagram's "Download your information" export (JSON format).

Instagram changes the export layout every so often, so this parser doesn't rely on exact paths or keys.
It looks for any JSON file whose name contains "saved", walks it, and:
  * any object containing an instagram.com/p/ or /reel/ link  -> a saved post
  * in collection files, an object with a name but no post link -> the start of a new collection
"""
import io
import json
import re
import zipfile

from . import db

POST_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})")


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _timestamps(obj):
    out = []
    for o in _walk(obj):
        ts = o.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 1_000_000_000:
            out.append(int(ts))
    return out


def _fix_mojibake(s):
    # Instagram exports encode UTF-8 bytes as latin-1 escapes ("Ã©" instead of "é")
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _entries(data):
    """Top-level list of entries in an export file, whatever its wrapper key is."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _post_from_entry(e):
    for s in _strings(e):
        m = POST_RE.search(s)
        if m:
            ts = _timestamps(e)
            user = e.get("title") if isinstance(e.get("title"), str) else ""
            if not user:
                smd = e.get("string_map_data") or {}
                for v in smd.values():
                    if isinstance(v, dict) and isinstance(v.get("value"), str) and not POST_RE.search(v["value"]):
                        user = v["value"]
                        break
            return m.group(1), _fix_mojibake(user or ""), (min(ts) if ts else None)
    return None


def _collection_name(e):
    smd = e.get("string_map_data") or {}
    name = smd.get("Name") or smd.get("name")
    if isinstance(name, dict) and isinstance(name.get("value"), str):
        return _fix_mojibake(name["value"])
    if isinstance(e.get("title"), str) and e["title"]:
        return _fix_mojibake(e["title"])
    return None


def parse_files(files):
    """files: {filename: bytes}. Returns (posts {code: (user, saved_at)}, memberships [(code, collection)])."""
    posts, memberships = {}, []
    for fname, raw in files.items():
        low = fname.lower()
        if not low.endswith(".json") or "saved" not in low:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        is_collections = "collection" in low
        current = None
        for e in _entries(data):
            if not isinstance(e, dict):
                continue
            p = _post_from_entry(e)
            if p:
                code, user, ts = p
                old = posts.get(code)
                if not old or (ts and (not old[1] or ts < old[1])) or (user and not old[0]):
                    posts[code] = (user or (old[0] if old else ""), ts or (old[1] if old else None))
                if is_collections and current:
                    memberships.append((code, current))
            elif is_collections:
                name = _collection_name(e)
                if name:
                    current = name
    return posts, memberships


def read_upload(filename, raw: bytes):
    """Accept the export .zip, or individual JSON files."""
    if filename.lower().endswith(".zip"):
        files = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for n in z.namelist():
                if n.lower().endswith(".json") and "saved" in n.lower():
                    files[n] = z.read(n)
        return files
    return {filename: raw}


def import_export(files):
    posts, memberships = parse_files(files)
    new = 0
    with db.tx() as c:
        for code, (user, ts) in posts.items():
            existed = c.execute("SELECT 1 FROM posts WHERE shortcode=?", (code,)).fetchone()
            pid = db.upsert_post(c, code, username=user, saved_at=ts)
            if not existed:
                new += 1
        for code, name in memberships:
            pid = c.execute("SELECT id FROM posts WHERE shortcode=?", (code,)).fetchone()[0]
            db.add_to_collection(c, pid, name, source="instagram")
        touched = {code for code in posts}
        for code in touched:
            pid = c.execute("SELECT id FROM posts WHERE shortcode=?", (code,)).fetchone()[0]
            db.reindex(c, pid)
    collections = sorted({n for _, n in memberships})
    return {"posts_found": len(posts), "new_posts": new, "collections": collections,
            "collection_links": len(memberships)}
