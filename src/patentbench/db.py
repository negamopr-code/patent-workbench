"""SQLite persistence for the workbench: tabs, documents, messages, notebook config.

WAL + busy_timeout makes concurrent gunicorn workers safe; one short-lived
connection per operation. Schema is created idempotently on first touch.
"""
from __future__ import annotations

import json
import os
import re
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
-- 🏆 cross-tab scan memory: which other-tab documents were already digest-checked
-- against THIS tab's benchmark (fingerprinted), so repeat Best-match clicks only
-- scan what's new. matched=1 rows were imported as candidates; matched=0 are the
-- negatives we never want to re-pay for.
CREATE TABLE IF NOT EXISTS cross_scan(
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  number TEXT NOT NULL,
  bm_fp TEXT NOT NULL,
  matched INTEGER NOT NULL,
  checked_at INTEGER NOT NULL,
  PRIMARY KEY(tab_id, number, bm_fp));
CREATE TABLE IF NOT EXISTS combi_motivation(
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  combinable INTEGER NOT NULL,
  reason TEXT,
  model TEXT,
  ts INTEGER NOT NULL,
  PRIMARY KEY(tab_id, a_id, b_id));
-- Cross-tab knowledge graph. GLOBAL (not tab-scoped): one taxonomy that every tab's
-- features attach to. Hierarchy = kg_node.parent_id (field ‹ block ‹ function ‹ option);
-- non-hierarchical cross-links (⇄ related: MCU, gauge…) = kg_edge; which tabs/docs
-- disclose a node = kg_feature. Two features "linked across tabs" ≡ same kg_node.
CREATE TABLE IF NOT EXISTS kg_node(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                 -- 'field' | 'block' | 'function' | 'option'
  name TEXT NOT NULL,
  parent_id INTEGER REFERENCES kg_node(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kg_edge(
  id INTEGER PRIMARY KEY,
  src_id INTEGER NOT NULL REFERENCES kg_node(id) ON DELETE CASCADE,
  dst_id INTEGER NOT NULL REFERENCES kg_node(id) ON DELETE CASCADE,
  rel TEXT NOT NULL DEFAULT 'related',
  ts INTEGER NOT NULL,
  UNIQUE(src_id, dst_id, rel));
CREATE TABLE IF NOT EXISTS kg_feature(
  id INTEGER PRIMARY KEY,
  node_id INTEGER NOT NULL REFERENCES kg_node(id) ON DELETE CASCADE,
  tab_id INTEGER REFERENCES tabs(id) ON DELETE CASCADE,
  doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,   -- NULL = a benchmark target feature
  feature_name TEXT NOT NULL,
  status TEXT,                        -- yes/partial/no/benchmark
  note TEXT,
  ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_tab ON messages(tab_id, id);
CREATE INDEX IF NOT EXISTS idx_documents_tab ON documents(tab_id, id);
CREATE INDEX IF NOT EXISTS idx_kg_node_parent ON kg_node(parent_id);
CREATE INDEX IF NOT EXISTS idx_kg_feature_node ON kg_feature(node_id);
CREATE INDEX IF NOT EXISTS idx_kg_feature_tabdoc ON kg_feature(tab_id, doc_id);
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
        if "digest_model" not in dcols:    # which model produced the digest (for cross-tab reuse label)
            con.execute("ALTER TABLE documents ADD COLUMN digest_model TEXT")
        if "digest_error" not in dcols:    # why the digest is missing — a dropped digest makes a
            con.execute("ALTER TABLE documents ADD COLUMN digest_error TEXT")  # doc invisible to
            # every digest-based tool (➕ additional read, ♻️ re-check, 🧩 combi); never lose the reason
        if "text_model" not in dcols:      # which model OCR'd/transcribed the body (NULL = Google fetch)
            con.execute("ALTER TABLE documents ADD COLUMN text_model TEXT")
        if "content_hash" not in dcols:    # sha256 of the source upload, for hash-based cross-tab reuse
            con.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_documents_number ON documents(number)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)")
        if "figures" not in dcols:         # drawing sheets: JSON [{n,url,path,caption}]
            con.execute("ALTER TABLE documents ADD COLUMN figures TEXT")
        if "figures_n" not in dcols:       # count of captioned figures (cheap for the list view)
            con.execute("ALTER TABLE documents ADD COLUMN figures_n INTEGER")
        if "origin_tab_id" not in dcols:   # cross-tab import: which tab this copy came from
            con.execute("ALTER TABLE documents ADD COLUMN origin_tab_id INTEGER")
        bmcols = {r[1] for r in con.execute("PRAGMA table_info(benchmark)")}
        if "nlm_source_notebook" not in bmcols:   # benchmark mirrored into which notebook
            con.execute("ALTER TABLE benchmark ADD COLUMN nlm_source_notebook TEXT")
        if "text_model" not in bmcols:      # which model transcribed the benchmark's page photos
            con.execute("ALTER TABLE benchmark ADD COLUMN text_model TEXT")
        if "content_hash" not in bmcols:    # sha256 of the uploaded file-set, for hash-based reuse
            con.execute("ALTER TABLE benchmark ADD COLUMN content_hash TEXT")
        if "figures" not in bmcols:         # benchmark drawing sheets: JSON [{n,url,path,caption}]
            con.execute("ALTER TABLE benchmark ADD COLUMN figures TEXT")
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
            "id, tab_id, number, title, status, error, source, origin_tab_id, "
            "added_at, fetched_at, "
            "score, score_note, scored_at, score_model, feature_scores, "
            "digest_model, text_model, figures_n, "
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


# ---------- cross-tab reuse (a document OCR'd/fetched once benefits every tab) ----------

_REUSE_COLS = ("number, title, abstract, claims, description, digest, source, "
               "digest_model, text_model, content_hash")


def _best_reusable(rows: list[sqlite3.Row]) -> dict | None:
    """Pick the richest already-processed copy: prefer one WITH a digest, then the
    one with the longest body. Rows must already carry a `tab_name`."""
    def rank(r):
        body = len((r["abstract"] or "") + (r["claims"] or "") + (r["description"] or ""))
        return (1 if r["digest"] else 0, body)
    best = max(rows, key=rank, default=None)
    return dict(best) if best else None


def find_reusable_by_number(number: str, exclude_tab_id: int | None = None) -> dict | None:
    """The best already-fetched copy of this exact number in ANOTHER tab, with its
    body + digest + the models that produced them, so a new tab can reuse the work
    instead of re-fetching/re-OCR'ing. None if nobody has a usable copy yet."""
    if not number:
        return None
    with _conn() as c:
        rows = c.execute(
            f"SELECT d.id AS src_doc_id, d.tab_id AS src_tab_id, t.name AS tab_name, "
            f"{_REUSE_COLS} FROM documents d JOIN tabs t ON t.id = d.tab_id "
            "WHERE d.number=? AND d.status='fetched' AND d.tab_id IS NOT ? "
            "AND (d.description IS NOT NULL OR d.claims IS NOT NULL OR d.abstract IS NOT NULL)",
            (number, exclude_tab_id)).fetchall()
    return _best_reusable(rows)


def find_reusable_by_hash(content_hash: str, exclude_tab_id: int | None = None) -> dict | None:
    """The best already-transcribed content for an uploaded file-set, matched by its
    content hash — across BOTH candidate documents and benchmarks. Lets an identical
    upload reuse a prior OCR run even when no patent number is known."""
    if not content_hash:
        return None
    rows: list[dict] = []
    with _conn() as c:
        for r in c.execute(
                f"SELECT d.id AS src_doc_id, d.tab_id AS src_tab_id, t.name AS tab_name, "
                f"{_REUSE_COLS} FROM documents d JOIN tabs t ON t.id = d.tab_id "
                "WHERE d.content_hash=? AND d.tab_id IS NOT ? "
                "AND (d.description IS NOT NULL OR d.claims IS NOT NULL OR d.abstract IS NOT NULL)",
                (content_hash, exclude_tab_id)).fetchall():
            rows.append(dict(r))
        for r in c.execute(
                "SELECT b.tab_id AS src_tab_id, t.name AS tab_name, b.number, b.title, "
                "b.abstract, b.claims, b.description, b.text, b.text_model, b.content_hash "
                "FROM benchmark b JOIN tabs t ON t.id = b.tab_id "
                "WHERE b.content_hash=? AND b.status='ready' AND b.text IS NOT NULL "
                "AND b.tab_id IS NOT ?", (content_hash, exclude_tab_id)).fetchall():
            d = dict(r)
            # benchmark carries its full body in `text`; expose it as `description`
            if d.get("text") and not d.get("description"):
                d["description"] = d["text"]
            d.update(src_doc_id=None, digest=None, digest_model=None, source="benchmark")
            rows.append(d)
    if not rows:
        return None
    def rank(r):
        return len((r.get("abstract") or "") + (r.get("claims") or "")
                   + (r.get("description") or ""))
    return max(rows, key=rank)


def copy_into_document(doc_id: int, src: dict) -> None:
    """Write a reusable copy's body + digest + provenance into a (pending) document,
    marking it fetched. `src` is a row from find_reusable_by_*."""
    with _conn() as c:
        c.execute(
            "UPDATE documents SET status='fetched', error=NULL, fetched_at=?, "
            "title=COALESCE(title, ?), abstract=?, claims=?, description=?, digest=?, "
            "digest_model=?, text_model=?, content_hash=COALESCE(?, content_hash) "
            "WHERE id=?",
            (_now(), src.get("title"), src.get("abstract"), src.get("claims"),
             src.get("description"), src.get("digest"), src.get("digest_model"),
             src.get("text_model"), src.get("content_hash"), doc_id))


def documents_disclosing_feature(name: str, exclude_tab_id: int | None = None) -> list[dict]:
    """Every document in ANY tab whose feature verdicts include a feature matching
    `name` (case-insensitive) with status YES/PARTIAL — the cross-tab answer to
    'who else discloses this feature?'. Scans both mandatory (feature_scores) and
    additional (additional_scores) verdicts; returns one entry per (doc, match)."""
    target = (name or "").strip().lower()
    if not target:
        return []
    out: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            "SELECT d.id, d.tab_id, t.name AS tab_name, d.number, d.title, "
            "d.feature_scores, d.additional_scores FROM documents d "
            "JOIN tabs t ON t.id = d.tab_id "
            "WHERE (d.feature_scores IS NOT NULL OR d.additional_scores IS NOT NULL) "
            "AND d.tab_id IS NOT ?", (exclude_tab_id,)).fetchall()
    for r in rows:
        for col, kind in (("feature_scores", "M"), ("additional_scores", "A")):
            try:
                arr = json.loads(r[col]) if r[col] else None
            except (ValueError, TypeError):
                arr = None
            if not isinstance(arr, list):
                continue
            for e in arr:
                if not isinstance(e, dict) or (e.get("name") or "").strip().lower() != target:
                    continue
                status = (e.get("status") or "").strip().lower()
                # M-features: yes/partial disclose. A-features: present/stretch disclose.
                full = status in ("yes", "present")
                partial = status in ("partial", "stretch") or status.startswith("part")
                if not (full or partial):
                    continue
                out.append({"id": r["id"], "tab_id": r["tab_id"], "tab_name": r["tab_name"],
                            "number": r["number"], "title": r["title"], "kind": kind,
                            "status": "partial" if partial else "yes",
                            "note": e.get("note") or e.get("evidence") or ""})
    return out


def _number_base(number: str) -> str:
    """A publication number without its trailing kind code (A1/B2/…) — so a kindless
    reference 'EP4338618' matches a stored 'EP4338618A1'."""
    return re.sub(r"[A-Za-z]\d?$", "", (number or "").strip())


def cross_tab_reference(number: str, exclude_tab_id: int | None = None) -> dict | None:
    """Everything known about a patent NUMBER from ANY other tab — its title, digest
    and (richest) deep-read verdict — so a benchmark/chat that merely *names* it
    (e.g. 'overlapping section like in EP4338618') can pull that document's stored
    arguments as context without the number living in this tab. Matches kind-code-
    insensitively. None if unknown."""
    if not number:
        return None
    base = _number_base(number) or number
    with _conn() as c:
        rows = c.execute(
            "SELECT d.id AS doc_id, d.tab_id, t.name AS tab_name, d.number, d.title, "
            "d.digest, d.verdict, d.score, d.score_note, "
            "length(COALESCE(d.description,'') || COALESCE(d.claims,'') || "
            "COALESCE(d.abstract,'')) AS body_len "
            "FROM documents d JOIN tabs t ON t.id = d.tab_id "
            "WHERE d.number GLOB ? AND d.tab_id IS NOT ?",
            (base + "*", exclude_tab_id)).fetchall()
    # GLOB base+* over-matches (EP4338618 → EP43386189); keep only true kind-variants
    rows = [r for r in rows if _number_base(r["number"]) == base]
    if not rows:
        return None
    # richest copy: prefer one carrying a verdict, then a digest, then the longest body
    def rank(r):
        return (1 if r["verdict"] else 0, 1 if r["digest"] else 0, r["body_len"] or 0)
    r = max(rows, key=rank)
    if not (r["digest"] or r["verdict"]):
        return None
    return {"number": r["number"], "title": r["title"], "tab_id": r["tab_id"],
            "tab_name": r["tab_name"], "doc_id": r["doc_id"], "digest": r["digest"],
            "verdict": r["verdict"], "score": r["score"], "score_note": r["score_note"]}


def cross_tab_discussions(number: str, exclude_tab_id: int | None = None,
                          max_exchanges: int = 12) -> list[dict]:
    """Full chat EXCHANGES from OTHER tabs that mention this patent number — so
    'write here everything we discussed about EP4338618 in other tabs' returns the
    actual conversation, not just the document's stored digest. Match is kind-code-
    and separator-insensitive ('EP 4338618 A1' finds 'EP4338618B1'). An exchange =
    a user question plus every reply until the next question; overlapping hits
    merge. Grouped per tab, most recent exchanges kept, newest last."""
    base = _number_base(number) or (number or "").strip()
    if not base:
        return []
    key = re.sub(r"[\s/.\- ]", "", base).upper()

    def _norm(s: str) -> str:
        return re.sub(r"[\s/.\- ]", "", s or "").upper()

    out = []
    budget = max_exchanges
    with _conn() as c:
        tabs = c.execute("SELECT id, name FROM tabs WHERE id IS NOT ? "
                         "ORDER BY position, id", (exclude_tab_id,)).fetchall()
        for t in tabs:
            if budget <= 0:
                break
            rows = c.execute(
                "SELECT id, role, text, ts FROM messages WHERE tab_id=? "
                "AND role IN ('q','c','a') ORDER BY id", (t["id"],)).fetchall()
            hits = [i for i, r in enumerate(rows) if key in _norm(r["text"])]
            if not hits:
                continue
            spans: list[tuple[int, int]] = []
            for i in hits:
                s = i
                while s > 0 and rows[s]["role"] != "q":
                    s -= 1
                e = i + 1
                while e < len(rows) and rows[e]["role"] != "q":
                    e += 1
                if spans and s < spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], e))
                else:
                    spans.append((s, e))
            exchanges = [[{"role": rows[j]["role"], "text": rows[j]["text"],
                           "ts": rows[j]["ts"]} for j in range(s, e)]
                         for s, e in spans[-budget:]]
            budget -= len(exchanges)
            out.append({"tab_id": t["id"], "tab_name": t["name"], "number": number,
                        "exchanges": exchanges})
    return out


def document_counts_by_tab() -> list[dict]:
    """Per tab: how many documents it holds and how many are fetched (have readable,
    already-OCR'd content). Feeds the chat's cross-tab coverage confirmation."""
    with _conn() as c:
        rows = c.execute(
            "SELECT t.id AS tab_id, t.name AS tab_name, "
            "COUNT(d.id) AS total, "
            "SUM(CASE WHEN d.status='fetched' THEN 1 ELSE 0 END) AS fetched "
            "FROM tabs t LEFT JOIN documents d ON d.tab_id=t.id "
            "GROUP BY t.id ORDER BY t.position, t.id").fetchall()
    return [{"tab_id": r["tab_id"], "tab_name": r["tab_name"],
             "total": r["total"] or 0, "fetched": r["fetched"] or 0} for r in rows]


def documents_across_tabs(exclude_tab_id: int | None = None) -> list[dict]:
    """Every FETCHED document in OTHER tabs, with the already-computed digest/verdict/
    score — so a chat can reason about (and combine) documents from any tab without
    re-fetching or re-OCR'ing. Richest copy per number (verdict ▷ digest ▷ body)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT d.id AS doc_id, d.tab_id, t.name AS tab_name, d.number, d.title, "
            "d.digest, d.verdict, d.score, d.score_note, "
            "length(COALESCE(d.description,'') || COALESCE(d.claims,'') || "
            "COALESCE(d.abstract,'')) AS body_len "
            "FROM documents d JOIN tabs t ON t.id=d.tab_id "
            "WHERE d.status='fetched' AND d.tab_id IS NOT ?", (exclude_tab_id,)).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        d = {"doc_id": r["doc_id"], "tab_id": r["tab_id"], "tab_name": r["tab_name"],
             "number": r["number"], "title": r["title"], "digest": r["digest"],
             "verdict": r["verdict"], "score": r["score"], "score_note": r["score_note"],
             "body_len": r["body_len"] or 0, "tabs": [r["tab_name"]]}
        prev = best.get(r["number"])
        if not prev:
            best[r["number"]] = d
            continue
        if r["tab_name"] not in prev["tabs"]:
            prev["tabs"].append(r["tab_name"])                       # same patent in several tabs
        cur_rank = (1 if r["verdict"] else 0, 1 if r["digest"] else 0, r["body_len"] or 0)
        prev_rank = (1 if prev["verdict"] else 0, 1 if prev["digest"] else 0, prev["body_len"])
        if cur_rank > prev_rank:
            d["tabs"] = prev["tabs"]
            best[r["number"]] = d
    out = list(best.values())
    out.sort(key=lambda x: (x["score"] if x["score"] is not None else -1), reverse=True)
    return out


def import_document_copy(tab_id: int, src_doc_id: int,
                         feature_scores: list[dict] | None = None,
                         score_note: str | None = None) -> int | None:
    """Copy a fetched document from ANOTHER tab into `tab_id` as a first-class
    candidate: full content (text, digest, figures) travels — zero re-fetch/re-OCR.
    What does NOT travel: score/verdict/nlm_* — they were computed against the OTHER
    tab's benchmark/notebooks and would poison this tab's ranking; this tab's own
    deep-read assesses the copy from scratch. `feature_scores`/`score_note` seed the
    cross-tab-scan's per-feature indication. None if the number already exists here."""
    with _conn() as c:
        src = c.execute("SELECT * FROM documents WHERE id=?", (src_doc_id,)).fetchone()
        if not src or src["status"] != "fetched":
            return None
        try:
            cur = c.execute(
                "INSERT INTO documents(tab_id, number, title, abstract, claims, "
                "description, digest, digest_model, text_model, content_hash, "
                "figures, figures_n, status, source, origin_tab_id, "
                "feature_scores, score_note, added_at, fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'fetched','cross-tab',?,?,?,?,?)",
                (tab_id, src["number"], src["title"], src["abstract"], src["claims"],
                 src["description"], src["digest"], src["digest_model"],
                 src["text_model"], src["content_hash"], src["figures"],
                 src["figures_n"], src["tab_id"],
                 json.dumps(feature_scores, ensure_ascii=False) if feature_scores else None,
                 score_note, _now(), _now()))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def cross_scan_checked(tab_id: int, bm_fp: str) -> set[str]:
    """Numbers already digest-checked against this benchmark fingerprint (any verdict)."""
    with _conn() as c:
        rows = c.execute("SELECT number FROM cross_scan WHERE tab_id=? AND bm_fp=?",
                         (tab_id, bm_fp)).fetchall()
    return {r[0] for r in rows}


def cross_scan_mark(tab_id: int, bm_fp: str, results: dict[str, bool]) -> None:
    """Persist scan verdicts ({number: matched}) so repeat scans skip them."""
    now = _now()
    with _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO cross_scan(tab_id, number, bm_fp, matched, checked_at) "
            "VALUES(?,?,?,?,?)",
            [(tab_id, n, bm_fp, 1 if m else 0, now) for n, m in results.items()])


# ---------- knowledge graph (cross-tab feature taxonomy) ----------

KG_KINDS = ("field", "block", "function", "option")
_KG_PARENT_KIND = {"block": "field", "function": "block", "option": "function"}


def _kg_find_node(c, kind: str, name: str, parent_id: int | None):
    """An existing node with this kind + case-insensitive name under this parent."""
    return c.execute(
        "SELECT * FROM kg_node WHERE kind=? AND lower(name)=lower(?) "
        "AND parent_id IS ?", (kind, (name or "").strip(), parent_id)).fetchone()


def kg_get_or_create_node(kind: str, name: str, parent_id: int | None = None) -> dict:
    """Idempotent node creation — reuse an existing (kind,name,parent) so the graph
    doesn't grow duplicates when the same field/block/option is classified again."""
    kind = kind if kind in KG_KINDS else "option"
    name = (name or "").strip() or "(unnamed)"
    with _conn() as c:
        row = _kg_find_node(c, kind, name, parent_id)
        if row:
            return dict(row)
        now = _now()
        cur = c.execute(
            "INSERT INTO kg_node(kind, name, parent_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?)", (kind, name, parent_id, now, now))
        return {"id": cur.lastrowid, "kind": kind, "name": name,
                "parent_id": parent_id, "created_at": now, "updated_at": now}


def kg_ensure_path(field: str, block: str = "", function: str = "",
                   option: str = "") -> dict:
    """Get-or-create the whole field›block›function›option chain; returns each level's
    node id and the deepest node. Empty levels are skipped."""
    out: dict = {}
    parent = None
    for kind, name in (("field", field), ("block", block),
                       ("function", function), ("option", option)):
        if not (name or "").strip():
            continue
        node = kg_get_or_create_node(kind, name, parent)
        out[kind] = node["id"]
        parent = node["id"]
    out["node_id"] = parent
    return out


def kg_rename_node(node_id: int, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE kg_node SET name=?, updated_at=? WHERE id=?",
                        (name, _now(), node_id))
        return cur.rowcount > 0


def kg_reparent_node(node_id: int, parent_id: int | None) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE kg_node SET parent_id=?, updated_at=? WHERE id=?",
                        (parent_id, _now(), node_id))
        return cur.rowcount > 0


def kg_delete_node(node_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM kg_node WHERE id=?", (node_id,))
        return cur.rowcount > 0


def kg_add_edge(src_id: int, dst_id: int, rel: str = "related") -> None:
    if src_id == dst_id:
        return
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO kg_edge(src_id, dst_id, rel, ts) "
                  "VALUES(?,?,?,?)", (src_id, dst_id, rel or "related", _now()))


def kg_delete_edge(edge_id: int) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM kg_edge WHERE id=?", (edge_id,)).rowcount > 0


def kg_attach_feature(node_id: int, feature_name: str, tab_id: int | None = None,
                      doc_id: int | None = None, status: str | None = None,
                      note: str | None = None) -> dict:
    """Attach a feature occurrence (a benchmark feature or a document's verdict) to a
    node. One occurrence = one node: re-attaching the same (tab,doc,feature) MOVES it,
    so re-classification never leaves a feature on two nodes."""
    with _conn() as c:
        c.execute(
            "DELETE FROM kg_feature WHERE tab_id IS ? AND doc_id IS ? "
            "AND lower(feature_name)=lower(?)", (tab_id, doc_id, (feature_name or "").strip()))
        cur = c.execute(
            "INSERT INTO kg_feature(node_id, tab_id, doc_id, feature_name, status, note, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (node_id, tab_id, doc_id, (feature_name or "").strip(),
             status, (note or "")[:2000], _now()))
        return {"id": cur.lastrowid, "node_id": node_id}


def kg_detach_feature(feature_id: int) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM kg_feature WHERE id=?", (feature_id,)).rowcount > 0


def kg_clear() -> None:
    """Wipe the whole graph (for a full 🔄 rebuild)."""
    with _conn() as c:
        c.execute("DELETE FROM kg_node")   # cascades to edges + features


def _kg_all(c):
    nodes = [dict(r) for r in c.execute("SELECT * FROM kg_node ORDER BY kind, name")]
    edges = [dict(r) for r in c.execute("SELECT * FROM kg_edge")]
    feats = [dict(r) for r in c.execute(
        "SELECT f.*, t.name AS tab_name, d.number AS doc_number, d.title AS doc_title "
        "FROM kg_feature f LEFT JOIN tabs t ON t.id=f.tab_id "
        "LEFT JOIN documents d ON d.id=f.doc_id")]
    return nodes, edges, feats


def kg_tree() -> dict:
    """The whole graph as a nested field›block›function›option forest. Each node
    carries its attached feature occurrences (tab/doc references) and its ⇄ related
    cross-links. `total_features` rolls child counts up so a collapsed node still
    shows how much lives under it."""
    with _conn() as c:
        nodes, edges, feats = _kg_all(c)
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        n["children"] = []
        n["features"] = []
        n["related"] = []
    feats_by_node: dict[int, list] = {}
    for f in feats:
        feats_by_node.setdefault(f["node_id"], []).append({
            "id": f["id"], "node_id": f["node_id"],
            "tab_id": f["tab_id"], "tab_name": f["tab_name"],
            "doc_id": f["doc_id"], "number": f["doc_number"], "doc_title": f["doc_title"],
            "feature_name": f["feature_name"], "status": f["status"], "note": f["note"]})
    for nid, arr in feats_by_node.items():
        if nid in by_id:
            by_id[nid]["features"] = arr
    for e in edges:
        s, d = by_id.get(e["src_id"]), by_id.get(e["dst_id"])
        if s and d:
            s["related"].append({"edge_id": e["id"], "id": d["id"], "name": d["name"],
                                 "kind": d["kind"], "rel": e["rel"]})
            d["related"].append({"edge_id": e["id"], "id": s["id"], "name": s["name"],
                                 "kind": s["kind"], "rel": e["rel"]})
    roots = []
    for n in nodes:
        p = by_id.get(n["parent_id"])
        if p:
            p["children"].append(n)
        else:
            roots.append(n)

    def roll(n):
        tot = len(n["features"])
        for ch in n["children"]:
            tot += roll(ch)
        n["total_features"] = tot
        return tot
    for r in roots:
        roll(r)
    roots.sort(key=lambda n: (-n["total_features"], n["name"].lower()))
    return {"nodes": roots, "node_count": len(nodes)}


def kg_path(node_id: int) -> list[dict]:
    """Breadcrumb from the root field down to this node."""
    out = []
    with _conn() as c:
        cur = node_id
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            r = c.execute("SELECT id, kind, name, parent_id FROM kg_node WHERE id=?",
                          (cur,)).fetchone()
            if not r:
                break
            out.append({"id": r["id"], "kind": r["kind"], "name": r["name"]})
            cur = r["parent_id"]
    return list(reversed(out))


def kg_candidate_nodes(name: str, limit: int = 8) -> list[dict]:
    """Existing option/function nodes whose name overlaps the given feature text —
    the shortlist a link suggestion is drawn from (LLM then confirms/refines)."""
    target = (name or "").strip().lower()
    if not target:
        return []
    words = {w for w in re.split(r"[^a-z0-9]+", target) if len(w) >= 3}
    with _conn() as c:
        rows = c.execute(
            "SELECT id, kind, name FROM kg_node WHERE kind IN ('option','function')"
        ).fetchall()
    scored = []
    for r in rows:
        nm = r["name"].lower()
        nwords = {w for w in re.split(r"[^a-z0-9]+", nm) if len(w) >= 3}
        overlap = len(words & nwords)
        if nm in target or target in nm:
            overlap += 2
        if overlap:
            scored.append((overlap, dict(r)))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, node in scored[:limit]:
        node["path"] = kg_path(node["id"])
        out.append(node)
    return out


def kg_search(query: str, limit: int = 40) -> dict:
    """Global cross-tab search: graph nodes, documents and chat messages that match
    the query. Feeds the top-bar search panel."""
    q = (query or "").strip()
    if not q:
        return {"nodes": [], "documents": [], "messages": []}
    like = f"%{q}%"
    with _conn() as c:
        nrows = c.execute(
            "SELECT id, kind, name FROM kg_node WHERE name LIKE ? LIMIT ?",
            (like, limit)).fetchall()
        # also nodes reached via a feature-name match (the feature text often differs
        # from the option label the LLM chose)
        frows = c.execute(
            "SELECT DISTINCT node_id FROM kg_feature WHERE feature_name LIKE ? LIMIT ?",
            (like, limit)).fetchall()
        drows = c.execute(
            "SELECT d.id, d.tab_id, t.name AS tab_name, d.number, d.title, d.score "
            "FROM documents d JOIN tabs t ON t.id=d.tab_id "
            "WHERE d.number LIKE ? OR d.title LIKE ? OR d.digest LIKE ? OR d.verdict LIKE ? "
            "LIMIT ?", (like, like, like, like, limit)).fetchall()
        mrows = c.execute(
            "SELECT m.id, m.tab_id, t.name AS tab_name, m.role, m.text, m.ts "
            "FROM messages m JOIN tabs t ON t.id=m.tab_id "
            "WHERE m.text LIKE ? AND m.role IN ('q','c','a') "
            "ORDER BY m.id DESC LIMIT ?", (like, limit)).fetchall()
    node_ids = {r["id"] for r in nrows} | {r["node_id"] for r in frows}
    nodes = []
    for nid in list(node_ids)[:limit]:
        p = kg_path(nid)
        if p:
            nodes.append({"id": nid, "kind": p[-1]["kind"], "name": p[-1]["name"],
                          "path": p})
    docs = [dict(r) for r in drows]
    msgs = []
    for r in mrows:
        d = dict(r)
        t = d.get("text") or ""
        i = t.lower().find(q.lower())
        d["snippet"] = ("…" + t[max(0, i - 60):i + 120] + "…") if i >= 0 else t[:160]
        msgs.append(d)
    return {"nodes": nodes, "documents": docs, "messages": msgs}


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
    """Replace the tab's benchmark with a fresh pending one (number- or file-based).

    Written features SURVIVE the replacement. They are the user's OWN input — what a
    match must disclose — not something derived from the document, so adding a document
    benchmark must never silently delete them. `features_json` is orthogonal to `source`:
    the benchmark is a document (number/pdf/images) or a spec ('features'), and either
    may carry a weighted feature list that drives the ranking."""
    with _conn() as c:
        prev = c.execute("SELECT features_json FROM benchmark WHERE tab_id=?",
                         (tab_id,)).fetchone()
        feats = prev["features_json"] if prev else None
        c.execute("DELETE FROM benchmark WHERE tab_id=?", (tab_id,))
        c.execute(
            "INSERT INTO benchmark(tab_id, number, files, status, source, "
            "features_json, updated_at) VALUES(?,?,?,?,?,?,?)",
            (tab_id, number, json.dumps(files or [], ensure_ascii=False),
             "pending", source, feats, _now()))


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


def set_benchmark_feature_list(tab_id: int, features: list[dict] | None) -> None:
    """Write ONLY the weighted feature list, keeping the benchmark itself intact.

    Used when the benchmark is a DOCUMENT: the features annotate it (they drive the
    ranking) and must not replace it, so unlike `set_benchmark_features` this touches
    no other column — the fetched number/text/files stay exactly as they are."""
    with _conn() as c:
        c.execute("UPDATE benchmark SET features_json=?, updated_at=? WHERE tab_id=?",
                  (json.dumps(features, ensure_ascii=False) if features else None,
                   _now(), tab_id))


def update_benchmark(tab_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE benchmark SET {sets} WHERE tab_id=?", (*fields.values(), tab_id))


def clear_benchmark(tab_id: int) -> list[dict]:
    """Remove the benchmark ENTIRELY — written features included; returns its uploaded
    files so the caller can delete them. This is the explicit "remove the benchmark"
    action. To REPLACE a benchmark, use `benchmark_files` + `set_benchmark` instead, so
    the user's written features survive the swap."""
    bm = get_benchmark(tab_id)
    with _conn() as c:
        c.execute("DELETE FROM benchmark WHERE tab_id=?", (tab_id,))
    return (bm or {}).get("files") or []


def benchmark_files(tab_id: int) -> list[dict]:
    """The benchmark's uploaded files, WITHOUT removing the row — so a caller replacing
    the benchmark can unlink stale uploads while `set_benchmark` still sees the previous
    row and can carry its features across."""
    return (get_benchmark(tab_id) or {}).get("files") or []


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
