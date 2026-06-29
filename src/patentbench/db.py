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
  digest TEXT,
  score REAL,
  score_note TEXT,
  scored_at INTEGER,
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
  auto_add INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS nlm_query_cache(
  key TEXT PRIMARY KEY,
  notebook_id TEXT,
  question TEXT,
  answer TEXT,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS combi_motivation(
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  combinable INTEGER NOT NULL,
  reason TEXT,
  model TEXT,
  ts INTEGER NOT NULL,
  PRIMARY KEY(tab_id, a_id, b_id));
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
        # migrations for DBs created before these columns existed
        cols = {r[1] for r in con.execute("PRAGMA table_info(benchmark)")}
        if "progress" not in cols:
            con.execute("ALTER TABLE benchmark ADD COLUMN progress TEXT")
        if "features_json" not in cols:    # weighted target features [{name,weight}]
            con.execute("ALTER TABLE benchmark ADD COLUMN features_json TEXT")
        dcols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        if "digest" not in dcols:
            con.execute("ALTER TABLE documents ADD COLUMN digest TEXT")
        if "verdict" not in dcols:  # full deep-map assessment vs benchmark — the
            con.execute("ALTER TABLE documents ADD COLUMN verdict TEXT")  # reusable read artifact
        if "feature_scores" not in dcols:  # per-target-feature verdict [{name,status}]
            con.execute("ALTER TABLE documents ADD COLUMN feature_scores TEXT")
        if "score" not in dcols:
            con.execute("ALTER TABLE documents ADD COLUMN score REAL")
            con.execute("ALTER TABLE documents ADD COLUMN score_note TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN scored_at INTEGER")
        if "score_model" not in dcols:      # which model did the last full-text read
            con.execute("ALTER TABLE documents ADD COLUMN score_model TEXT")
        if "nlm_source_notebook" not in dcols:
            con.execute("ALTER TABLE documents ADD COLUMN nlm_source_notebook TEXT")
        if "nlm_source_id" not in dcols:   # which NLM source this doc was imported from
            con.execute("ALTER TABLE documents ADD COLUMN nlm_source_id TEXT")
        if "nlm_score" not in dcols:        # NotebookLM's own match rating (vs Claude's score)
            con.execute("ALTER TABLE documents ADD COLUMN nlm_score REAL")
            con.execute("ALTER TABLE documents ADD COLUMN nlm_score_note TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN nlm_scored_at INTEGER")
        if "shortlisted" not in dcols:      # last 📓 NLM shortlist's picks — persists the
            con.execute("ALTER TABLE documents ADD COLUMN shortlisted INTEGER NOT NULL DEFAULT 0")
        if "nlm_rank" not in dcols:         # NLM's best-first ordering within that shortlist
            con.execute("ALTER TABLE documents ADD COLUMN nlm_rank INTEGER")
        if "additional_scores" not in dcols:   # ➕ additional-read verdict on the A-features
            con.execute("ALTER TABLE documents ADD COLUMN additional_scores TEXT")
        bmcols = {r[1] for r in con.execute("PRAGMA table_info(benchmark)")}
        if "nlm_source_notebook" not in bmcols:   # benchmark mirrored into which notebook
            con.execute("ALTER TABLE benchmark ADD COLUMN nlm_source_notebook TEXT")
        ncols = {r[1] for r in con.execute("PRAGMA table_info(tab_notebook_config)")}
        if "auto_add" not in ncols:
            con.execute("ALTER TABLE tab_notebook_config ADD COLUMN auto_add INTEGER NOT NULL DEFAULT 0")
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


def add_text_document(tab_id: int, number: str, title: str | None, content: str,
                      nlm_source_id: str | None = None,
                      nlm_source_notebook: str | None = None) -> int | None:
    """Insert a non-patent, text-only document (e.g. imported raw from a NotebookLM
    source) as an already-'fetched' row whose body lives in `description`, so it
    participates in chat + deep-compare like any candidate. Returns the new id, or
    None if a row with the same number already exists in the tab."""
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO documents(tab_id, number, title, description, status, source, "
                "nlm_source_id, nlm_source_notebook, added_at, fetched_at) "
                "VALUES(?,?,?,?,'fetched','notebook-text',?,?,?,?)",
                (tab_id, number, title, content, nlm_source_id, nlm_source_notebook,
                 _now(), _now()))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def imported_source_ids(tab_id: int) -> set[str]:
    """NLM source ids already imported into this tab (for idempotent re-import)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT nlm_source_id FROM documents WHERE tab_id=? AND nlm_source_id IS NOT NULL",
            (tab_id,)).fetchall()
    return {r[0] for r in rows}


def list_documents(tab_id: int, full: bool = False) -> list[dict]:
    cols = ("*" if full else
            "id, tab_id, number, title, status, error, source, added_at, fetched_at, "
            "score, score_note, scored_at, score_model, feature_scores, "
            "nlm_score, nlm_score_note, nlm_scored_at, "
            "nlm_source_notebook, shortlisted, nlm_rank, additional_scores, "
            "length(abstract) AS abstract_len, length(claims) AS claims_len, "
            "length(description) AS description_len, length(digest) AS digest_len, "
            "length(verdict) AS verdict_len")
    with _conn() as c:
        rows = c.execute(f"SELECT {cols} FROM documents WHERE tab_id=? ORDER BY id",
                         (tab_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["feature_scores"] = json.loads(d["feature_scores"]) if d.get("feature_scores") else None
            d["additional_scores"] = json.loads(d["additional_scores"]) if d.get("additional_scores") else None
            out.append(d)
        return out


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


def set_shortlisted(tab_id: int, doc_ids: list[int]) -> None:
    """Persist the latest 📓 NLM shortlist's picks for a tab, IN ORDER: doc_ids is best-first
    (BEST, SECOND-BEST, then the rest NLM named), so we store nlm_rank = 1,2,3… It marks these
    ids shortlisted=1 and clears the flag (and rank) on all others, so the picks AND NLM's
    ordering survive reloads / tab switches and feed the consensus tie-break."""
    with _conn() as c:
        c.execute("UPDATE documents SET shortlisted=0, nlm_rank=NULL "
                  "WHERE tab_id=? AND (shortlisted=1 OR nlm_rank IS NOT NULL)", (tab_id,))
        for rank, did in enumerate(doc_ids, 1):     # best-first → rank 1, 2, 3…
            c.execute("UPDATE documents SET shortlisted=1, nlm_rank=? WHERE tab_id=? AND id=?",
                      (rank, tab_id, did))


def clear_nlm_refs(tab_id: int, notebook_id: str) -> int:
    """Forget that any of a tab's documents / benchmark live in a notebook — used after
    that notebook is DELETED, so tab_notebook_ids() stops listing a dead notebook (which
    would otherwise be queried and error). Returns documents cleared."""
    with _conn() as c:
        cur = c.execute("UPDATE documents SET nlm_source_notebook=NULL, nlm_source_id=NULL "
                        "WHERE tab_id=? AND nlm_source_notebook=?", (tab_id, notebook_id))
        c.execute("UPDATE benchmark SET nlm_source_notebook=NULL "
                  "WHERE tab_id=? AND nlm_source_notebook=?", (tab_id, notebook_id))
        return cur.rowcount


def top_scored_documents(tab_id: int, limit: int) -> list[int]:
    """The tab's `limit` best-scored FETCHED candidates, highest Claude score first
    (id as a stable tiebreak). The funnel's stage-1 output: the few worth handing to
    NotebookLM for an independent second opinion."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM documents WHERE tab_id=? AND status='fetched' AND score IS NOT NULL "
            "ORDER BY score DESC, id ASC LIMIT ?", (tab_id, limit)).fetchall()
        return [r["id"] for r in rows]


def nlm_cache_get(key: str) -> str | None:
    """A previously-stored NotebookLM answer for this exact (notebook+sources+question)
    key, or None. Persisted so identical queries don't re-hit NotebookLM (quota) across
    rebuilds; the key embeds a source-set signature so it auto-misses when sources change."""
    with _conn() as c:
        r = c.execute("SELECT answer FROM nlm_query_cache WHERE key=?", (key,)).fetchone()
        return r["answer"] if r else None


def nlm_cache_put(key: str, notebook_id: str, question: str, answer: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO nlm_query_cache(key, notebook_id, question, answer, "
                  "created_at) VALUES(?,?,?,?,?)", (key, notebook_id, question, answer, _now()))


def nlm_cache_clear(notebook_id: str | None = None) -> int:
    """Drop cached NotebookLM answers (all, or just one notebook's). Returns rows removed."""
    with _conn() as c:
        if notebook_id:
            cur = c.execute("DELETE FROM nlm_query_cache WHERE notebook_id=?", (notebook_id,))
        else:
            cur = c.execute("DELETE FROM nlm_query_cache")
        return cur.rowcount


def set_document_number(tab_id: int, doc_id: int, number: str) -> dict:
    """Edit a document's number (e.g. fixing an OCR-damaged one) and reset it to
    pending. Returns {ok} | {error} on duplicate within the tab."""
    with _conn() as c:
        try:
            cur = c.execute(
                "UPDATE documents SET number=?, status='pending', error=NULL, title=NULL, "
                "abstract=NULL, claims=NULL, description=NULL, digest=NULL, fetched_at=NULL, "
                "score=NULL, score_note=NULL, scored_at=NULL, feature_scores=NULL "
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


def tab_notebook_ids(tab_id: int) -> list[str]:
    """Every NotebookLM notebook this tab's content is spread across — the
    connected notebook FIRST (most-recent candidates), then any sibling notebooks
    the auto-rollover exported earlier documents/benchmark into. Querying must hit
    ALL of them: candidates beyond the per-notebook source cap live in the
    siblings and are otherwise unreachable. De-duplicated, order preserved."""
    ids: list[str] = []
    cfg = get_notebook_config(tab_id)
    if cfg and cfg.get("notebook_id"):
        ids.append(cfg["notebook_id"])
    with _conn() as c:
        rows = c.execute(
            "SELECT nlm_source_notebook FROM documents "
            "WHERE tab_id=? AND nlm_source_notebook IS NOT NULL "
            "UNION SELECT nlm_source_notebook FROM benchmark "
            "WHERE tab_id=? AND nlm_source_notebook IS NOT NULL",
            (tab_id, tab_id)).fetchall()
    for r in rows:
        nb = r[0]
        if nb and nb not in ids:
            ids.append(nb)
    return ids


def set_notebook_config(tab_id: int, notebook_id: str | None, notebook_title: str | None,
                        source_ids: list[str] | None, auto_add: bool = False) -> None:
    with _conn() as c:
        if not notebook_id:
            c.execute("DELETE FROM tab_notebook_config WHERE tab_id=?", (tab_id,))
            return
        c.execute(
            "INSERT INTO tab_notebook_config(tab_id, notebook_id, notebook_title, selected_source_ids, auto_add, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(tab_id) DO UPDATE SET notebook_id=excluded.notebook_id, "
            "notebook_title=excluded.notebook_title, selected_source_ids=excluded.selected_source_ids, "
            "auto_add=excluded.auto_add, updated_at=excluded.updated_at",
            (tab_id, notebook_id, notebook_title,
             json.dumps(source_ids or [], ensure_ascii=False), int(auto_add), _now()))


# ---------- benchmark (one reference document per tab) ----------

def get_benchmark(tab_id: int, full: bool = True) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM benchmark WHERE tab_id=?", (tab_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["files"] = json.loads(d["files"]) if d["files"] else []
    d["features"] = json.loads(d["features_json"]) if d.get("features_json") else []
    d.pop("features_json", None)
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


def set_benchmark_features(tab_id: int, spec: str, title: str,
                           features: list[dict] | None = None) -> None:
    """Set the benchmark from a TARGET FEATURE COMBINATION (no document to
    fetch/transcribe). The composed `spec` IS the benchmark text, so it is ready
    at once; `features` (a weighted [{name,weight}] list) is stored alongside and
    drives the candidate ranking when present."""
    with _conn() as c:
        c.execute("DELETE FROM benchmark WHERE tab_id=?", (tab_id,))
        c.execute(
            "INSERT INTO benchmark(tab_id, title, text, files, status, source, "
            "features_json, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (tab_id, title, spec, json.dumps([]), "ready", "features",
             json.dumps(features or [], ensure_ascii=False) if features else None, _now()))


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


# ---------- combi (two-document combination) motivation verdicts ----------

def set_combi_motivation(tab_id: int, a_id: int, b_id: int, combinable: bool,
                         reason: str, model: str | None) -> None:
    """Persist the LLM 'motivation to combine' verdict for a document PAIR. Stored with the
    ids sorted so (a,b) and (b,a) collapse to one row (the combination is order-independent)."""
    lo, hi = sorted((int(a_id), int(b_id)))
    with _conn() as c:
        c.execute(
            "INSERT INTO combi_motivation(tab_id, a_id, b_id, combinable, reason, model, ts) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(tab_id, a_id, b_id) DO UPDATE SET "
            "combinable=excluded.combinable, reason=excluded.reason, model=excluded.model, ts=excluded.ts",
            (tab_id, lo, hi, 1 if combinable else 0, reason, model, _now()))


def get_combi_motivations(tab_id: int) -> dict:
    """All stored pair verdicts for a tab, keyed 'lo-hi' (ids sorted) → {combinable, reason, model, ts}."""
    with _conn() as c:
        rows = c.execute("SELECT a_id, b_id, combinable, reason, model, ts FROM combi_motivation "
                         "WHERE tab_id=?", (tab_id,)).fetchall()
    return {f"{r['a_id']}-{r['b_id']}": {"combinable": bool(r["combinable"]),
                                         "reason": r["reason"] or "", "model": r["model"],
                                         "ts": r["ts"]} for r in rows}


# ---------- uploads ----------

def record_upload(tab_id: int, path: str, name: str, kind: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO uploads(tab_id, path, name, kind, ts) VALUES(?,?,?,?,?)",
                        (tab_id, path, name, kind, _now()))
        return cur.lastrowid
