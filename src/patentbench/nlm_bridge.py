"""Bridge to the NotebookLM CLI (`nlm` from notebooklm-mcp-cli).

Copied near-verbatim from antimg-web (the proven sibling). Design contract:
- Questions go VERBATIM to one notebook via `nlm notebook query <id> <q> --json`.
  The answer is computed by Gemini on Google's side → zero Anthropic tokens.
- Calls are SERIALIZED with a minimum gap (anti RESOURCE_EXHAUSTED) behind a
  process-wide lock; concurrency across gunicorn workers is acceptable (the
  account-level quota is the real limiter, not the gap).
- Deployment: the web image bakes `nlm` into /opt/nlmvenv (see deploy/Dockerfile)
  and the container mounts the shared auth profile at ~/.notebooklm-mcp-cli
  (host: /root/claude-sandbox/persistent/nlm-profile — one Google login reused
  by every project in this sandbox; the notebook UUID is the connection string).
- Degrades gracefully: if the CLI or the profile is missing this module reports
  `available=False` with a human reason instead of raising — the rest of the
  studio must keep working without NotebookLM.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

NLM_BIN = os.environ.get("NLM_BIN", "nlm")
MIN_GAP = float(os.environ.get("NLM_MIN_GAP", "1.5"))        # seconds between calls
LIST_TIMEOUT = float(os.environ.get("NLM_LIST_TIMEOUT", "60"))
QUERY_TIMEOUT = float(os.environ.get("NLM_QUERY_TIMEOUT", "150"))
LIST_TTL = float(os.environ.get("NLM_LIST_TTL", "300"))      # notebook-list cache, seconds

_lock = threading.Lock()
_last_call = 0.0
_list_cache: tuple[float, list[dict]] | None = None
_sources_cache: dict[str, tuple[float, list[dict]]] = {}   # notebook_id → (ts, sources)


def available() -> tuple[bool, str]:
    """Is the nlm CLI reachable in this deployment? Returns (ok, reason-if-not)."""
    if shutil.which(NLM_BIN) is None and not os.path.exists(NLM_BIN):
        return False, (f"nlm CLI not found ({NLM_BIN}). Rebuild the container with "
                       "scripts/serve.sh — the image bakes notebooklm-mcp-cli and "
                       "needs the nlm-profile mount.")
    return True, ""


def _gap_wait() -> None:
    global _last_call
    g = MIN_GAP - (time.monotonic() - _last_call)
    if g > 0:
        time.sleep(g)


def _json_after(s: str, brace: str):
    """nlm prints human chatter before the JSON payload — parse from the first brace."""
    i = s.find(brace)
    if i < 0:
        raise ValueError("no JSON in nlm output")
    return json.loads(s[i:])


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    global _last_call
    with _lock:
        _gap_wait()
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        finally:
            _last_call = time.monotonic()


def list_notebooks(force: bool = False) -> dict:
    """All notebooks in the account: {notebooks: [{id,title,sources}], error?}. Cached."""
    global _list_cache
    ok, why = available()
    if not ok:
        return {"notebooks": [], "error": why}
    if not force and _list_cache and time.monotonic() - _list_cache[0] < LIST_TTL:
        return {"notebooks": _list_cache[1]}
    try:
        proc = _run([NLM_BIN, "notebook", "list"], LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"notebooks": [], "error": "nlm notebook list: timeout"}
    if proc.returncode != 0:
        return {"notebooks": [],
                "error": (proc.stderr or proc.stdout).strip()[:400] or "nlm notebook list failed"}
    try:
        data = _json_after(proc.stdout, "[")
    except Exception as exc:
        return {"notebooks": [], "error": f"parse nlm output: {exc}"}
    nbs = [{"id": n["id"], "title": n.get("title") or n["id"],
            "sources": n.get("source_count")}
           for n in data if isinstance(n, dict) and n.get("id")]
    _list_cache = (time.monotonic(), nbs)
    return {"notebooks": nbs}


def list_sources(notebook_id: str, force: bool = False) -> dict:
    """Files/sources inside one notebook: {sources: [{id,title}], error?}. Cached per notebook."""
    ok, why = available()
    if not ok:
        return {"sources": [], "error": why}
    hit = _sources_cache.get(notebook_id)
    if not force and hit and time.monotonic() - hit[0] < LIST_TTL:
        return {"sources": hit[1]}
    try:
        proc = _run([NLM_BIN, "source", "list", notebook_id, "--json"], LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"sources": [], "error": "nlm source list: timeout"}
    if proc.returncode != 0:
        return {"sources": [],
                "error": (proc.stderr or proc.stdout).strip()[:400] or "nlm source list failed"}
    try:
        data = _json_after(proc.stdout, "[")
    except Exception as exc:
        return {"sources": [], "error": f"parse nlm output: {exc}"}
    srcs = [{"id": s["id"], "title": s.get("title") or s.get("name") or s["id"]}
            for s in data if isinstance(s, dict) and s.get("id")]
    _sources_cache[notebook_id] = (time.monotonic(), srcs)
    return {"sources": srcs}


# NotebookLM caps sources per notebook (50 on the standard plan — the yt2nlm
# project's auto-split at 50 confirmed this live). At the cap the workbench
# proposes rolling over to a fresh notebook instead of failing adds.
SOURCE_LIMIT = int(os.environ.get("NLM_SOURCE_LIMIT", "50"))


def create_notebook(title: str) -> dict:
    """Create a notebook: {id, title} | {error}."""
    ok, why = available()
    if not ok:
        return {"error": why}
    try:
        proc = _run([NLM_BIN, "notebook", "create", title, "--json"], LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm notebook create: timeout"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "create failed"}
    try:
        data = _json_after(proc.stdout, "{")
    except Exception as exc:
        return {"error": f"parse nlm output: {exc}"}
    data = data.get("value", data) if isinstance(data, dict) else {}
    nb_id = data.get("id") or data.get("notebook_id")
    if not nb_id:
        return {"error": "nlm notebook create returned no id"}
    global _list_cache
    _list_cache = None                      # the notebook list changed
    return {"id": nb_id, "title": data.get("title") or title}


def add_source_text(notebook_id: str, title: str, text: str) -> dict:
    """Add a text source to a notebook: {ok} | {error, full?}. Text is clipped to
    ~100k chars (kernel MAX_ARG_STRLEN caps a single argv entry at 128 KiB)."""
    ok, why = available()
    if not ok:
        return {"error": why}
    srcs = list_sources(notebook_id, force=True)
    if not srcs.get("error") and len(srcs.get("sources") or []) >= SOURCE_LIMIT:
        return {"error": f"notebook is full ({SOURCE_LIMIT} sources)", "full": True}
    cmd = [NLM_BIN, "source", "add", notebook_id,
           "--text", text[:100_000], "--title", title[:200]]
    try:
        proc = _run(cmd, QUERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm source add: timeout"}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()[:400] or "source add failed"
        return {"error": err, "full": "limit" in err.lower() or "full" in err.lower()}
    _sources_cache.pop(notebook_id, None)   # the source list changed
    return {"ok": True}


def source_content(source_id: str) -> dict:
    """Raw text content of ONE source inside a notebook (no AI processing):
    {content} | {error}. Used to import a non-patent source into the workbench."""
    ok, why = available()
    if not ok:
        return {"error": why}
    try:
        proc = _run([NLM_BIN, "source", "content", source_id, "--json"], QUERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm source content: timeout"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "source content failed"}
    # newer CLIs print JSON ({content|text|value}); older ones print raw text.
    out = proc.stdout
    try:
        data = _json_after(out, "{")
        val = data.get("value", data) if isinstance(data, dict) else {}
        content = (val.get("content") or val.get("text") or "") if isinstance(val, dict) else ""
    except Exception:
        content = out.strip()
    content = (content or "").strip()
    if not content:
        return {"error": "empty source content"}
    return {"content": content}


def query(notebook_id: str, question: str, source_ids: list[str] | None = None) -> dict:
    """Ask one notebook (optionally restricted to EXACT source files inside it).
    Returns {answer, sources_used} or {error}."""
    ok, why = available()
    if not ok:
        return {"error": why}
    cmd = [NLM_BIN, "notebook", "query", notebook_id, question,
           "--json", "--timeout", str(int(QUERY_TIMEOUT))]
    if source_ids:
        cmd += ["--source-ids", ",".join(source_ids)]
    try:
        proc = _run(cmd, QUERY_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        return {"error": "NotebookLM query timed out"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "nlm query failed"}
    try:
        data = _json_after(proc.stdout, "{")
    except Exception as exc:
        return {"error": f"parse nlm output: {exc}"}
    # CLI ≤0.6.x wraps the payload in {"value": {...}}; newer versions return it top-level.
    val = data.get("value", data) if isinstance(data, dict) else {}
    if isinstance(val, dict) and val.get("status") == "error":
        return {"error": str(val.get("error") or "nlm query failed")[:400]}
    answer = (val.get("answer") or "").strip() if isinstance(val, dict) else ""
    if not answer:
        return {"error": "empty answer from NotebookLM (quota exhausted?)"}
    return {"answer": answer,
            "sources_used": val.get("sources_used") or val.get("sources") or []}
