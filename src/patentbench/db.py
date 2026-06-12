"""SQLite persistence for the workbench: tabs, documents, messages, notebook config.

WAL + busy_timeout makes concurrent gunicorn workers safe; one short-lived
connection per operation. Schema is created idempotently on first touch.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("PB_DB", "/data/workbench.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tabs(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY,
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  number TEXT NOT NULL,
  title TEXT, abstract TEXT, claims TEXT, description TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  added_at INTEGER NOT NULL,
  fetched_at INTEGER,
  UNIQUE(tab_id, number));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  model TEXT,
  participants TEXT,
  ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tab_notebook_config(
  tab_id INTEGER PRIMARY KEY REFERENCES tabs(id) ON DELETE CASCADE,
  notebook_id TEXT,
  notebook_title TEXT,
  selected_source_ids TEXT,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS benchmark(
  tab_id INTEGER PRIMARY KEY REFERENCES tabs(id) ON DELETE CASCADE,
  number TEXT,
  title TEXT, abstract TEXT, claims TEXT, description TEXT,
  text TEXT,
  files TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  source TEXT,
  progress TEXT,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY,
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  name TEXT,
  kind TEXT,
  ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_tab ON messages(tab_id, id);
CREATE INDEX IF NOT EXISTS idx_documents_tab ON documents(tab_id, id);
"""

MAX_TEXT = 20_000  # message text cap


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.executescript(_SCHEMA)
        # migration for DBs created before the column existed
        cols = {r[1] for r in con.execute("PRAGMA table_info(benchmark)")}
        if "progress" not in cols:
            con.execute("ALTER TABLE benchmark ADD COLUMN progress TEXT")
        yield con
        con.commit()
    finally:
        con.close()


def _now() -> int:
    return int(time.time())


# ---------- tabs ----------

def list_tabs() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM tabs ORDER BY position, id").fetchall()
        return [dict(r) for r in rows]


def create_tab(name: str) -> dict:
    with _conn() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM tabs").fetchone()[0]
        cur = c.execute("INSERT INTO tabs(name, position, created_at, updated_at) VALUES(?,?,?,?)",
                        (name.strip() or "Untitled", pos, _now(), _now()))
        return {"id": cur.lastrowid, "name": name.strip() or "Untitled", "position": pos}


def rename_tab(tab_id: int, name: str) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE tabs SET name=?, updated_at=? WHERE id=?",
                        (name.strip() or "Untitled", _now(), tab_id))
        return cur.rowcount > 0


def delete_tab(tab_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM tabs WHERE id=?", (tab_id,))
        return cur.rowcount > 0


def tab_exists(tab_id: int) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM tabs WHERE id=?", (tab_id,)).fetchone() is not None


# ---------- documents ----------

def add_documents(tab_id: int, numbers: list[str], source: str = "manual") -> dict:
    """Dedupe-insert canonical numbers as pending rows. Returns {inserted:[ids], skipped:[numbers]}."""
    inserted, skipped = [], []
    with _conn() as c:
        for n in numbers:
            try:
                cur = c.execute(
                    "INSERT INTO documents(tab_id, number, source, added_at) VALUES(?,?,?,?)",
                    (tab_id, n, source, _now()))
                inserted.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                skipped.append(n)
    return {"inserted": inserted, "skipped": skipped}


def list_documents(tab_id: int, full: bool = False) -> list[dict]:
    cols = ("*" if full else
            "id, tab_id, number, title, status, error, source, added_at, fetched_at, "
            "length(abstract) AS abstract_len, length(claims) AS claims_len, "
            "length(description) AS description_len")
    with _conn() as c:
        rows = c.execute(f"SELECT {cols} FROM documents WHERE tab_id=? ORDER BY id",
                         (tab_id,)).fetchall()
        return [dict(r) for r in rows]


def get_document(doc_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else None


def update_document(doc_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id))


def set_document_number(tab_id: int, doc_id: int, number: str) -> dict:
    """Edit a document's number (e.g. fixing an OCR-damaged one) and reset it to
    pending. Returns {ok} | {error} on duplicate within the tab."""
    with _conn() as c:
        try:
            cur = c.execute(
                "UPDATE documents SET number=?, status='pending', error=NULL, title=NULL, "
                "abstract=NULL, claims=NULL, description=NULL, fetched_at=NULL "
                "WHERE id=? AND tab_id=?", (number, doc_id, tab_id))
        except sqlite3.IntegrityError:
            return {"error": f"{number} is already in this tab"}
        if cur.rowcount == 0:
            return {"error": "document not found"}
    return {"ok": True}


def delete_document(tab_id: int, doc_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM documents WHERE id=? AND tab_id=?", (doc_id, tab_id))
        return cur.rowcount > 0


# ---------- messages ----------

def append_message(tab_id: int, role: str, text: str, model: str | None = None,
                   participants: list | None = None) -> dict:
    row = {"tab_id": tab_id, "role": role, "text": (text or "")[:MAX_TEXT],
           "model": model,
           "participants": json.dumps(participants, ensure_ascii=False) if participants else None,
           "ts": _now()}
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages(tab_id, role, text, model, participants, ts) VALUES(?,?,?,?,?,?)",
            (row["tab_id"], row["role"], row["text"], row["model"], row["participants"], row["ts"]))
        row["id"] = cur.lastrowid
    row["participants"] = participants
    return row


def list_messages(tab_id: int, limit: int = 500) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM messages WHERE tab_id=? ORDER BY id DESC LIMIT ?",
                         (tab_id, limit)).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["participants"] = json.loads(d["participants"]) if d["participants"] else None
        out.append(d)
    return out


# ---------- notebook config ----------

def get_notebook_config(tab_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM tab_notebook_config WHERE tab_id=?", (tab_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["selected_source_ids"] = json.loads(d["selected_source_ids"]) if d["selected_source_ids"] else []
    return d


def set_notebook_config(tab_id: int, notebook_id: str | None, notebook_title: str | None,
                        source_ids: list[str] | None) -> None:
    with _conn() as c:
        if not notebook_id:
            c.execute("DELETE FROM tab_notebook_config WHERE tab_id=?", (tab_id,))
            return
        c.execute(
            "INSERT INTO tab_notebook_config(tab_id, notebook_id, notebook_title, selected_source_ids, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(tab_id) DO UPDATE SET notebook_id=excluded.notebook_id, "
            "notebook_title=excluded.notebook_title, selected_source_ids=excluded.selected_source_ids, "
            "updated_at=excluded.updated_at",
            (tab_id, notebook_id, notebook_title,
             json.dumps(source_ids or [], ensure_ascii=False), _now()))


# ---------- benchmark (one reference document per tab) ----------

def get_benchmark(tab_id: int, full: bool = True) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM benchmark WHERE tab_id=?", (tab_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["files"] = json.loads(d["files"]) if d["files"] else []
    if not full:
        for k in ("abstract", "claims", "description", "text"):
            d[k] = bool(d[k])      # presence flags only — keep state payload small
    return d


def set_benchmark(tab_id: int, source: str, number: str | None = None,
                  files: list[dict] | None = None) -> None:
    """Replace the tab's benchmark with a fresh pending one (number- or file-based)."""
    with _conn() as c:
        c.execute("DELETE FROM benchmark WHERE tab_id=?", (tab_id,))
        c.execute(
            "INSERT INTO benchmark(tab_id, number, files, status, source, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (tab_id, number, json.dumps(files or [], ensure_ascii=False),
             "pending", source, _now()))


def update_benchmark(tab_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE benchmark SET {sets} WHERE tab_id=?", (*fields.values(), tab_id))


def clear_benchmark(tab_id: int) -> list[dict]:
    """Remove the benchmark; returns its uploaded files so the caller can delete them."""
    bm = get_benchmark(tab_id)
    with _conn() as c:
        c.execute("DELETE FROM benchmark WHERE tab_id=?", (tab_id,))
    return (bm or {}).get("files") or []


# ---------- uploads ----------

def record_upload(tab_id: int, path: str, name: str, kind: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO uploads(tab_id, path, name, kind, ts) VALUES(?,?,?,?,?)",
                        (tab_id, path, name, kind, _now()))
        return cur.lastrowid
