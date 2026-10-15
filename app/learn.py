"""Search that learns from you. Everything stays in the library database on this Mac.

Signals, per search:
  open        you opened a result                                  +1
  keep        you added a result to a moodboard or collection      +2
  skip        you opened something further down, past this one     -0.25
  notmatch    you said "Not a match"                               -3 (and it's hidden for that search)

How they're used when you search again (the same words, or something that means nearly the same):
  * the search itself is nudged toward what you picked and away from what you rejected,
  * the posts themselves move up or down a little.
Collections count too: searching for a word that names one of your collections ("cinematography")
leans toward what you put in it.
"""
import re
import threading
import time

import numpy as np

from . import db

SIGNALS = {"open": 1.0, "keep": 2.0, "skip": -0.25, "notmatch": -3.0}
_state = {"version": 0}
_qcache = {}
_lock = threading.Lock()


def norm_q(q):
    return " ".join(re.sub(r"[^\w\s'-]", " ", (q or "").lower()).split())


def version():
    return _state["version"]


def record(q, post_id, signal, idx=0, t=0.0):
    q = norm_q(q)
    if not q or signal not in SIGNALS:
        return
    with db.tx() as c:
        if signal in ("open", "keep", "notmatch"):   # one of each per post and search is enough
            c.execute("DELETE FROM feedback WHERE q=? AND post_id=? AND signal=?", (q, post_id, signal))
        c.execute("INSERT INTO feedback (q, post_id, idx, t, signal, weight, at) VALUES (?,?,?,?,?,?,?)",
                  (q, post_id, int(idx or 0), float(t or 0), signal, SIGNALS[signal], db.now()))
    _state["version"] += 1


def _qvec(q):
    from . import ai
    with _lock:
        if q not in _qcache:
            _qcache[q] = ai.embed_text(q)
            if len(_qcache) > 400:
                _qcache.pop(next(iter(_qcache)))
        return _qcache[q]


def _frame_vec(c, post_id, idx, t):
    r = c.execute("SELECT f.vec FROM frames f JOIN media m ON m.id=f.media_id WHERE m.post_id=? AND m.idx=? "
                  "ORDER BY ABS(f.t - ?) LIMIT 1", (post_id, idx, t)).fetchone()
    return np.frombuffer(r[0], np.float16).astype(np.float32) if r else None


def related(q):
    """{post_id: (net weight, [(weight, idx, t)])} from this search and searches that mean nearly the same."""
    c = db.conn()
    qs = [r[0] for r in c.execute("SELECT DISTINCT q FROM feedback")]
    if not qs:
        return {}, set()
    me = _qvec(q)
    weights = {}
    for other in qs:
        if other == q:
            weights[other] = 1.0
        else:
            sim = float(me @ _qvec(other))
            if sim >= 0.9:
                weights[other] = 0.5 * (sim - 0.9) / 0.1 + 0.25
    if not weights:
        return {}, set()
    out, hidden = {}, set()
    marks = ",".join("?" * len(weights))
    for fq, pid, idx, t, sig, w in c.execute(f"SELECT q, post_id, idx, t, signal, weight FROM feedback WHERE q IN ({marks})", list(weights)):
        ww = w * weights[fq]
        e = out.setdefault(pid, [0.0, []])
        e[0] += ww
        e[1].append((ww, idx, t))
        if sig == "notmatch" and weights[fq] >= 0.5:
            hidden.add(pid)
    return out, hidden


def collection_examples(q, limit=40):
    """Posts in a collection whose name matches the search ("cinematography" -> your Cinematography collection)."""
    c = db.conn()
    names = [r[0] for r in c.execute("SELECT name FROM collections")]
    me = _qvec(q)
    hits = [n for n in names if norm_q(n) == q or (len(q) > 3 and q in norm_q(n)) or float(me @ _qvec(norm_q(n))) >= 0.93]
    if not hits:
        return []
    marks = ",".join("?" * len(hits))
    return [r[0] for r in c.execute(
        f"SELECT DISTINCT pc.post_id FROM post_collections pc JOIN collections x ON x.id=pc.collection_id "
        f"WHERE x.name IN ({marks}) ORDER BY pc.post_id DESC LIMIT ?", (*hits, limit))]


def personalize(q, qv):
    """-> (adjusted query vector, {post_id: score bonus}, {post_ids to hide})"""
    q = norm_q(q)
    c = db.conn()
    fb, hidden = related(q)
    pos, neg, bonus = [], [], {}
    for pid, (net, marks) in fb.items():
        bonus[pid] = float(np.clip(net, -3, 3)) * 0.012
        w, idx, t = max(marks, key=lambda m: abs(m[0]))
        v = _frame_vec(c, pid, idx, t)
        if v is None:
            continue
        (pos if net > 0 else neg).append(v * min(abs(net), 3))
    coll = collection_examples(q)
    for pid in coll:
        bonus[pid] = bonus.get(pid, 0) + 0.015
    cv = [v for v in (_frame_vec(c, pid, 0, 0) for pid in coll[:30]) if v is not None]
    adj = qv.copy()
    if pos:
        adj += 0.5 * min(1.0, len(pos) / 3) * _unit(np.sum(pos, axis=0))
    if cv:
        adj += 0.25 * _unit(np.mean(cv, axis=0))
    if neg:
        adj -= 0.25 * min(1.0, len(neg) / 3) * _unit(np.sum(neg, axis=0))
    return _unit(adj), bonus, hidden


def _unit(v):
    return v / (np.linalg.norm(v) + 1e-8)


def stats():
    c = db.conn()
    return {"signals": c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
            "searches": c.execute("SELECT COUNT(DISTINCT q) FROM feedback").fetchone()[0],
            "tags": c.execute("SELECT COUNT(*) FROM techniques WHERE source IN ('user','no')").fetchone()[0]}


def forget():
    with db.tx() as c:
        c.execute("DELETE FROM feedback")
    _state["version"] += 1
