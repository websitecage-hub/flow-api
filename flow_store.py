# -*- coding: utf-8 -*-
"""Persistence: sqlite jobs + media index, forever-log helpers.

DB: sessions/flow.db (git-ignored)
  jobs(id, kind, params, status, progress, result, error,
       created, updated, finished)
  media(uuid, prompt, seed, model, aspect, width, height, size,
        batch, file, created, updated)   -- local mirror of history
Logs: logs/flow-YYYYMMDD.jsonl (append-only, never auto-deleted).
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
DB_FILE = ROOT / "sessions" / "flow.db"
LOG_DIR = ROOT / "logs"
_lock = threading.Lock()


def _db():
    DB_FILE.parent.mkdir(exist_ok=True)
    cx = sqlite3.connect(str(DB_FILE), timeout=30)
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("""CREATE TABLE IF NOT EXISTS jobs(
      id TEXT PRIMARY KEY, kind TEXT, params TEXT, status TEXT,
      progress TEXT, result TEXT, error TEXT,
      created REAL, updated REAL, finished REAL)""")
    cx.execute("""CREATE TABLE IF NOT EXISTS media(
      uuid TEXT PRIMARY KEY, prompt TEXT, seed INTEGER, model TEXT,
      aspect TEXT, width INTEGER, height INTEGER, size INTEGER,
      batch TEXT, file TEXT, created TEXT, updated REAL)""")
    cx.execute("""CREATE TABLE IF NOT EXISTS submits(
      kind TEXT, ts REAL)""")
    cx.execute("""CREATE INDEX IF NOT EXISTS ix_submits ON submits(kind, ts)""")
    cx.execute("""CREATE TABLE IF NOT EXISTS workers(
      name TEXT PRIMARY KEY, last_seen REAL, info TEXT)""")
    return cx


def new_job(kind, params):
    import uuid as _uuid
    jid = f"{kind[:3]}-{int(time.time() * 1000):x}-{_uuid.uuid4().hex[:6]}"
    now = time.time()
    with _lock:
        cx = _db()
        cx.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (jid, kind, json.dumps(params), "queued", "[]", "", "",
                    now, now, 0))
        cx.commit()
        cx.close()
    return jid


def job_update(jid, status=None, event=None, result=None, error=None):
    with _lock:
        cx = _db()
        row = cx.execute("SELECT progress FROM jobs WHERE id=?", (jid,)).fetchone()
        prog = json.loads(row[0]) if row and row[0] else []
        if event is not None:
            prog.append({"t": time.time(), "e": event})
            prog = prog[-50:]
        sets, vals = ["progress=?", "updated=?"], [json.dumps(prog), time.time()]
        if status is not None:
            sets.append("status=?")
            vals.append(status)
            if status in ("done", "failed"):
                sets.append("finished=?")
                vals.append(time.time())
        if result is not None:
            sets.append("result=?")
            vals.append(json.dumps(result, default=str))
        if error is not None:
            sets.append("error=?")
            vals.append(str(error)[:500])
        vals.append(jid)
        cx.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)
        cx.commit()
        cx.close()


def job_get(jid):
    with _lock:
        cx = _db()
        r = cx.execute("SELECT id,kind,params,status,progress,result,error,"
                       "created,updated,finished FROM jobs WHERE id=?",
                       (jid,)).fetchone()
        cx.close()
    if not r:
        return None
    return {"id": r[0], "kind": r[1], "params": json.loads(r[2] or "{}"),
            "status": r[3], "progress": json.loads(r[4] or "[]"),
            "result": json.loads(r[5] or "null"), "error": r[6],
            "created": r[7], "updated": r[8], "finished": r[9]}


def jobs_list(limit=30):
    with _lock:
        cx = _db()
        rows = cx.execute("SELECT id,kind,status,created,finished FROM jobs"
                          " ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        cx.close()
    return [{"id": r[0], "kind": r[1], "status": r[2],
             "created": r[3], "finished": r[4]} for r in rows]


def media_upsert(items):
    if not items:
        return
    now = time.time()
    with _lock:
        cx = _db()
        for m in items:
            try:
                cx.execute(
                    "INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(uuid) DO UPDATE SET prompt=excluded.prompt,"
                    " seed=excluded.seed, width=excluded.width,"
                    " height=excluded.height, size=excluded.size,"
                    " batch=excluded.batch, file=excluded.file,"
                    " created=excluded.created, updated=excluded.updated",
                    (m.get("uuid"), m.get("prompt"), m.get("seed"),
                     m.get("model"), m.get("aspect"), m.get("width"),
                     m.get("height"), m.get("size"), m.get("batch"),
                     m.get("file"), m.get("created"), now))
            except Exception:
                pass
        cx.commit()
        cx.close()


def media_list(limit=100):
    with _lock:
        cx = _db()
        rows = cx.execute("SELECT uuid,prompt,seed,model,aspect,width,height,"
                          "size,batch,file,created FROM media"
                          " ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        cx.close()
    keys = ("uuid", "prompt", "seed", "model", "aspect", "width", "height",
            "size", "batch", "file", "created")
    return [dict(zip(keys, r)) for r in rows]


# ── Google-facing throttle state ─────────────────────────────
def submit_record(kind):
    with _lock:
        cx = _db()
        cx.execute("INSERT INTO submits VALUES(?,?)", (kind, time.time()))
        cx.execute("DELETE FROM submits WHERE ts < ?", (time.time() - 86400 * 2,))
        cx.commit()
        cx.close()


def submits_since(kind, seconds):
    with _lock:
        cx = _db()
        n = cx.execute("SELECT COUNT(*) FROM submits WHERE kind=? AND ts>?",
                       (kind, time.time() - seconds)).fetchone()[0]
        cx.close()
    return n


def last_submit_ts(kind):
    with _lock:
        cx = _db()
        r = cx.execute("SELECT MAX(ts) FROM submits WHERE kind=?", (kind,)).fetchone()
        cx.close()
    return r[0] if r and r[0] else 0.0


# ── storage backend: local disk now, S3-compatible when configured ──
def save_blob(name, data):
    """Save bytes; returns {'path': local_path, 'url': public_url_or_''}.
    With S3_ENDPOINT+S3_BUCKET+S3_KEY+S3_SECRET set, mirrors to bucket and
    returns its public URL (else url='')."""
    out = {"path": "", "url": ""}
    try:
        dest = ROOT / "outputs" / name
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(data)
        out["path"] = str(dest)
    except Exception:
        pass
    try:
        import os as _os
        ep, bucket = _os.environ.get("S3_ENDPOINT", ""), _os.environ.get("S3_BUCKET", "")
        key, secret = _os.environ.get("S3_KEY", ""), _os.environ.get("S3_SECRET", "")
        if ep and bucket and key and secret and out["path"]:
            import boto3 as _boto
            s3 = _boto.client("s3", endpoint_url=ep,
                              aws_access_key_id=key, aws_secret_access_key=secret)
            s3.upload_file(out["path"], bucket, f"flow/{name}",
                           ExtraArgs={"ContentType": "image/jpeg"})
            pub = _os.environ.get("S3_PUBLIC_BASE", "").rstrip("/")
            if pub:
                out["url"] = f"{pub}/flow/{name}"
    except Exception:
        pass
    return out


def jlog(event, **fields):
    """Append one JSON line to today's forever-log."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        rec = {"ts": time.time(), "event": event}
        rec.update(fields)
        with open(LOG_DIR / f"flow-{time.strftime('%Y%m%d')}.jsonl",
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def log_tail(n=100, day=None):
    """Last n lines of a daily log (default today)."""
    try:
        day = day or time.strftime("%Y%m%d")
        fp = LOG_DIR / f"flow-{day}.jsonl"
        if not fp.exists():
            return []
        lines = fp.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


# ── distributed worker queue ───────────────────────────────────
def claim_next_job(worker):
    """Atomically claim oldest queued job -> running. Returns job or None."""
    with _lock:
        cx = _db()
        r = cx.execute("SELECT id,kind,params FROM jobs WHERE status='queued'"
                       " ORDER BY created LIMIT 1").fetchone()
        if not r:
            cx.close()
            return None
        now = time.time()
        cx.execute("UPDATE jobs SET status='running',updated=? WHERE id=?",
                   (now, r[0]))
        cx.execute("INSERT INTO workers VALUES(?,?,?)"
                   " ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen,"
                   " info=excluded.info",
                   (worker, now, json.dumps({"job": r[0]})))
        cx.commit()
        cx.close()
    return {"id": r[0], "kind": r[1], "params": json.loads(r[2] or "{}")}


def worker_heartbeat(worker, info=None):
    with _lock:
        cx = _db()
        cx.execute("INSERT INTO workers VALUES(?,?,?)"
                   " ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen,"
                   " info=excluded.info",
                   (worker, time.time(), json.dumps(info or {})))
        cx.commit()
        cx.close()


def workers_online(max_age=120):
    with _lock:
        cx = _db()
        rows = cx.execute("SELECT name,last_seen,info FROM workers WHERE last_seen>?",
                          (time.time() - max_age,)).fetchall()
        cx.close()
    return [{"name": r[0], "last_seen": r[1], "info": json.loads(r[2] or "{}")}
            for r in rows]
