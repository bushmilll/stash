"""SQLite library: posts, media files, collections, tags, full-text search."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("STASH_DATA", Path.home() / "Stash")).expanduser()
THUMB_DIR = DATA_DIR / "thumbs"        # previews + video posters: always on this Mac
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "stash.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
DEFAULT_MEDIA_DIR = DATA_DIR / "media"

for d in (DATA_DIR, THUMB_DIR, EXPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _initial_media_dir():
    """Originals can live elsewhere (Dropbox, an external drive). Read that choice before anything else starts."""
    try:
        chosen = json.loads(SETTINGS_PATH.read_text()).get("media_dir") if SETTINGS_PATH.exists() else ""
    except Exception:
        chosen = ""
    if chosen:
        return Path(chosen).expanduser()
    DEFAULT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_MEDIA_DIR


MEDIA_DIR = _initial_media_dir()
MEDIA_MOVING_TO = None                 # set while originals are being moved, so files are found in either place


def media_available():
    return MEDIA_DIR.exists()


def media_path(rel):
    """Where an original actually is right now (handles a move in progress)."""
    p = MEDIA_DIR / rel
    if MEDIA_MOVING_TO is not None and not p.exists():
        alt = MEDIA_MOVING_TO / rel
        if alt.exists():
            return alt
    return p


def media_roots():
    return [r for r in (MEDIA_DIR, MEDIA_MOVING_TO) if r is not None]

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    shortcode TEXT UNIQUE NOT NULL,
    url TEXT,
    username TEXT DEFAULT '',
    caption TEXT DEFAULT '',
    posted_at INTEGER,
    saved_at INTEGER,
    added_at INTEGER,
    status TEXT DEFAULT 'pending',      -- pending | ready | failed | gone
    error TEXT,
    attempts INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    favorite INTEGER DEFAULT 0,
    kind TEXT DEFAULT '',               -- image | video | carousel
    deleted_at INTEGER                  -- set = in trash
);
CREATE INDEX IF NOT EXISTS posts_saved ON posts(saved_at DESC);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS posts_user ON posts(username);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- image | video
    path TEXT NOT NULL,                 -- relative to MEDIA_DIR
    thumb TEXT,                         -- relative to THUMB_DIR
    width INTEGER, height INTEGER, duration REAL,
    UNIQUE(post_id, idx)
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE,
    source TEXT DEFAULT 'local'         -- instagram | local
);
CREATE TABLE IF NOT EXISTS post_collections (
    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, collection_id)
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS post_tags (
    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, tag_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    username, caption, notes, tags, collections,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS techniques (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    tag TEXT NOT NULL, score REAL, t REAL DEFAULT 0, source TEXT NOT NULL,
    PRIMARY KEY (media_id, tag)
);
CREATE INDEX IF NOT EXISTS techniques_tag ON techniques(tag, source);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY, q TEXT NOT NULL, post_id INTEGER NOT NULL, idx INTEGER DEFAULT 0, t REAL DEFAULT 0,
    signal TEXT NOT NULL, weight REAL NOT NULL, at INTEGER
);
CREATE INDEX IF NOT EXISTS feedback_q ON feedback(q);
CREATE TABLE IF NOT EXISTS feeds (
    url TEXT PRIMARY KEY, complete INTEGER DEFAULT 0, cursor TEXT, seen INTEGER DEFAULT 0, updated INTEGER
);
CREATE TABLE IF NOT EXISTS boards (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    bg TEXT DEFAULT '#eceae5',
    width INTEGER DEFAULT 1600,
    gap INTEGER DEFAULT 12,
    items TEXT DEFAULT '[]',
    settings TEXT DEFAULT '{}',
    created_at INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS analysis (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    palette TEXT, tone TEXT, sat TEXT, temp TEXT, contrast TEXT,
    ar REAL, ar_label TEXT, crop TEXT, lum REAL, chroma REAL, vec BLOB, version INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS analysis_filters ON analysis(tone, sat, temp, contrast, ar_label);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    t REAL NOT NULL,             -- seconds into the video (0 for images)
    vec BLOB NOT NULL            -- CLIP embedding, float16
);
CREATE INDEX IF NOT EXISTS frames_media ON frames(media_id);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT, status TEXT, message TEXT,
    done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
    started INTEGER, finished INTEGER
);
"""

_local = threading.local()
_write_lock = threading.RLock()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init():
    with _write_lock:
        c = conn()
        c.executescript(SCHEMA)
        # light migrations for libraries created by older versions
        cols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        if "deleted_at" not in cols:
            c.execute("ALTER TABLE posts ADD COLUMN deleted_at INTEGER")
        if "fav_idx" not in cols:   # which slide of a carousel you favorited
            c.execute("ALTER TABLE posts ADD COLUMN fav_idx INTEGER DEFAULT 0")
        if "saved_src" not in cols:
            # where the "saved" time came from: 'export' = real date from Instagram's data export,
            # 'feed' = estimated from the order of the collection feed. Older rows: an export date is
            # well before the moment the post was added; a sync-time stamp is about the same moment.
            c.execute("ALTER TABLE posts ADD COLUMN saved_src TEXT")
            c.execute("UPDATE posts SET saved_src = CASE WHEN saved_at IS NOT NULL AND added_at IS NOT NULL "
                      "AND saved_at < added_at - 3600 THEN 'export' ELSE 'feed' END")
        mcols = {r[1] for r in c.execute("PRAGMA table_info(media)")}
        if "poster" not in mcols:
            c.execute("ALTER TABLE media ADD COLUMN poster TEXT")      # full-res still for videos
        if "hq" not in mcols:
            c.execute("ALTER TABLE media ADD COLUMN hq INTEGER DEFAULT 0")  # 1 = best quality Instagram offers
        bcols = {r[1] for r in c.execute("PRAGMA table_info(boards)")}
        if "settings" not in bcols:
            c.execute("ALTER TABLE boards ADD COLUMN settings TEXT DEFAULT '{}'")   # layout, title, captions, tile shape…
        c.execute("CREATE INDEX IF NOT EXISTS posts_deleted ON posts(deleted_at)")
        acols = {r[1] for r in c.execute("PRAGMA table_info(analysis)")}
        for col, typ in (("content", "TEXT"), ("content_conf", "REAL"), ("clip", "BLOB"),
                         ("ai_version", "INTEGER"), ("content_user", "TEXT"), ("scan_version", "INTEGER"),
                         ("tech_version", "INTEGER")):
            if col not in acols:
                c.execute(f"ALTER TABLE analysis ADD COLUMN {col} {typ}")
        c.commit()


class tx:
    """Serialized write transaction (SQLite allows one writer anyway)."""
    def __enter__(self):
        _write_lock.acquire()
        return conn()

    def __exit__(self, exc_type, *a):
        try:
            if exc_type:
                conn().rollback()
            else:
                conn().commit()
        finally:
            _write_lock.release()


def now() -> int:
    return int(time.time())


# ---------- posts ----------

def upsert_post(c, shortcode, **fields):
    """Insert a post if new; fill in fields that are empty. Returns post id."""
    row = c.execute("SELECT * FROM posts WHERE shortcode=?", (shortcode,)).fetchone()
    if row is None:
        fields.setdefault("url", f"https://www.instagram.com/p/{shortcode}/")
        fields.setdefault("added_at", now())
        cols = ["shortcode"] + list(fields)
        c.execute(
            f"INSERT INTO posts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [shortcode] + list(fields.values()),
        )
        return c.execute("SELECT id FROM posts WHERE shortcode=?", (shortcode,)).fetchone()[0]
    updates = {}
    for k, v in fields.items():
        if v in (None, ""):
            continue
        if k == "saved_at":
            # keep the earliest known save time
            if not row["saved_at"] or v < row["saved_at"]:
                updates[k] = v
        elif k in ("status", "kind", "error", "attempts"):
            updates[k] = v
        elif not row[k]:
            updates[k] = v
    if updates:
        c.execute(
            f"UPDATE posts SET {','.join(f'{k}=?' for k in updates)} WHERE id=?",
            list(updates.values()) + [row["id"]],
        )
    return row["id"]


def get_or_create(c, table, name, **extra):
    name = name.strip()
    row = c.execute(f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()
    if row:
        return row[0]
    cols = ["name"] + list(extra)
    c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
              [name] + list(extra.values()))
    return c.execute(f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()[0]


def add_to_collection(c, post_id, name, source="local"):
    cid = get_or_create(c, "collections", name, source=source)
    c.execute("INSERT OR IGNORE INTO post_collections VALUES (?,?)", (post_id, cid))


def reindex(c, post_id):
    p = c.execute("SELECT username, caption, notes FROM posts WHERE id=?", (post_id,)).fetchone()
    if not p:
        c.execute("DELETE FROM search WHERE rowid=?", (post_id,))
        return
    tags = " ".join(r[0] for r in c.execute(
        "SELECT t.name FROM tags t JOIN post_tags pt ON pt.tag_id=t.id WHERE pt.post_id=?", (post_id,)))
    cols = " ".join(r[0] for r in c.execute(
        "SELECT x.name FROM collections x JOIN post_collections pc ON pc.collection_id=x.id WHERE pc.post_id=?",
        (post_id,)))
    c.execute("DELETE FROM search WHERE rowid=?", (post_id,))
    c.execute("INSERT INTO search(rowid, username, caption, notes, tags, collections) VALUES (?,?,?,?,?,?)",
              (post_id, p["username"] or "", p["caption"] or "", p["notes"] or "", tags, cols))


def post_dict(c, row, full=False):
    d = dict(row)
    media = [dict(m) for m in c.execute(
        "SELECT m.id, m.idx, m.kind, m.path, m.thumb, m.poster, m.width, m.height, m.duration, m.hq, "
        "a.palette, a.tone, a.sat, a.temp, a.contrast, a.ar, a.ar_label, a.crop, "
        "COALESCE(a.content_user, a.content) AS content, a.content_user IS NOT NULL AS content_fixed "
        "FROM media m LEFT JOIN analysis a ON a.media_id=m.id WHERE m.post_id=? ORDER BY m.idx",
        (row["id"],))]
    for m in media:
        m["src"] = "/files/media/" + m["path"]
        m["thumb_src"] = "/files/thumbs/" + m["thumb"] if m["thumb"] else None
        m["poster_src"] = "/files/thumbs/" + m["poster"] if m.get("poster") else None
        for k in ("palette", "crop"):
            try:
                m[k] = json.loads(m[k]) if m[k] else None
            except Exception:
                m[k] = None
    d["media"] = media if full else media[:1]
    d["media_count"] = len(media)
    d["collections"] = [r[0] for r in c.execute(
        "SELECT x.name FROM collections x JOIN post_collections pc ON pc.collection_id=x.id "
        "WHERE pc.post_id=? ORDER BY x.name", (row["id"],))]
    if full:
        d["tags"] = [r[0] for r in c.execute(
            "SELECT t.name FROM tags t JOIN post_tags pt ON pt.tag_id=t.id WHERE pt.post_id=? ORDER BY t.name",
            (row["id"],))]
    return d


# ---------- settings ----------

DEFAULT_SETTINGS = {
    "instagram_username": "",
    "cookies_source": "chrome",          # chrome | firefox | safari | edge | brave | file
    "cookies_file": "",
    "collections": [],                   # [{"name": ..., "url": ...}]
    "include_uncollected": False,        # also bring in saves that aren't in any collection (All posts feed + data export)
    "auto_sync_hours": 6,                # 0 = off
    "stop_after_known": 30,              # incremental sync: stop a feed after this many already-known posts in a row
    "backfill_per_run": 150,             # max export-only posts to fetch per backfill run
    "request_delay": [6, 12],            # seconds between Instagram API requests (random in range)
    "ai_enabled": False,                 # on-device AI tags (model downloaded on first use)
    "rank_version": 0,                   # 1 = save order has been rebuilt from the collection feeds
    "media_dir": "",                     # where originals live; empty = inside the library folder
    "media_move_from": "",               # a move in progress (resumes if the app was closed)
    "media_move_to": "",
    "quality_check": 0,                  # 1 = every video was re-checked for a sharper version once
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text()))
        except Exception:
            pass
    return s


def save_settings(s):
    merged = load_settings()
    merged.update({k: v for k, v in s.items() if k in DEFAULT_SETTINGS})
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2))
    return merged


# ---------- backups ----------

BACKUP_DIR = DATA_DIR / "backups"
KEEP_BACKUPS = 7


def backup_now():
    """Consistent snapshot of the database (safe while the app is running) + settings."""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    dest = BACKUP_DIR / f"stash-{stamp}.db"
    out = sqlite3.connect(dest)
    try:
        conn().backup(out)
    finally:
        out.close()
    if SETTINGS_PATH.exists():
        (BACKUP_DIR / f"settings-{stamp}.json").write_bytes(SETTINGS_PATH.read_bytes())
    for pattern in ("stash-*.db", "settings-*.json"):
        for old in sorted(BACKUP_DIR.glob(pattern))[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    return dest


def list_backups():
    if not BACKUP_DIR.exists():
        return []
    return [{"name": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)}
            for f in sorted(BACKUP_DIR.glob("stash-*.db"), reverse=True)]


def dir_size(path: Path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
