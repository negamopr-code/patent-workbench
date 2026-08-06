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

import re

NLM_BIN = os.environ.get("NLM_BIN", "nlm")
MIN_GAP = float(os.environ.get("NLM_MIN_GAP", "1.5"))        # seconds between calls
LIST_TIMEOUT = float(os.environ.get("NLM_LIST_TIMEOUT", "60"))
QUERY_TIMEOUT = float(os.environ.get("NLM_QUERY_TIMEOUT", "150"))
LIST_TTL = float(os.environ.get("NLM_LIST_TTL", "300"))      # notebook-list cache, seconds

_lock = threading.Lock()
_last_call = 0.0
_list_cache: dict[str, tuple[float, list[dict]]] = {}      # profile → (ts, notebooks)
_sources_cache: dict[str, tuple[float, list[dict]]] = {}   # notebook_id → (ts, sources)

# Per-tab accounts: a tab may pin a named auth profile (a separate Google login
# with its OWN quota pool and ~100-notebook cap). profile=None everywhere means
# the CLI's default profile — the pre-multi-account behavior, byte-for-byte.
DEFAULT_PROFILE = "default"


def _prof(profile: str | None) -> str:
    return profile or DEFAULT_PROFILE


def _with_profile(cmd: list[str], profile: str | None) -> list[str]:
    """Append --profile only for explicitly-pinned non-default profiles, so
    default-account tabs keep the exact CLI invocation that is proven live."""
    if profile and profile != DEFAULT_PROFILE:
        return cmd + ["--profile", profile]
    return cmd


def list_profiles() -> list[dict]:
    """Auth profiles present in the mounted profile dir: [{name, authed}].
    `authed` = cookies.json exists (liveness is only known at call time)."""
    root = os.path.join(os.path.expanduser("~/.notebooklm-mcp-cli"), "profiles")
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for n in names:
        if os.path.isdir(os.path.join(root, n)):
            out.append({"name": n,
                        "authed": os.path.isfile(os.path.join(root, n, "cookies.json"))})
    return out


def available(profile: str | None = None) -> tuple[bool, str]:
    """Is the nlm CLI reachable AND authenticated in this deployment? Returns
    (ok, reason-if-not). The profile check exists because a wiped/root-owned
    profile dir otherwise surfaces only as a raw PermissionError traceback in
    the MIDDLE of a job (mega-screen round 13, 2026-08-06) instead of an
    actionable refusal at start."""
    if shutil.which(NLM_BIN) is None and not os.path.exists(NLM_BIN):
        return False, (f"nlm CLI not found ({NLM_BIN}). Rebuild the container with "
                       "scripts/serve.sh — the image bakes notebooklm-mcp-cli and "
                       "needs the nlm-profile mount.")
    prof_dir = os.path.expanduser("~/.notebooklm-mcp-cli")
    cookies = os.path.join(prof_dir, "profiles", _prof(profile), "cookies.json")
    if not os.path.isfile(cookies):
        return False, ("NLM auth profile missing (" + cookies + "). Run "
                       "scripts/reseed-nlm-profile.sh from the claude dev "
                       "container, then retry.")
    if not os.access(prof_dir, os.W_OK):
        return False, (f"NLM profile dir not writable ({prof_dir}) — ownership "
                       "broken (expected uid 1000). Re-run "
                       "scripts/reseed-nlm-profile.sh, which fixes ownership.")
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


def list_notebooks(force: bool = False, profile: str | None = None) -> dict:
    """All notebooks in the account: {notebooks: [{id,title,sources}], error?}. Cached."""
    ok, why = available(profile)
    if not ok:
        return {"notebooks": [], "error": why}
    hit = _list_cache.get(_prof(profile))
    if not force and hit and time.monotonic() - hit[0] < LIST_TTL:
        return {"notebooks": hit[1]}
    try:
        proc = _run(_with_profile([NLM_BIN, "notebook", "list"], profile), LIST_TIMEOUT)
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
    _list_cache[_prof(profile)] = (time.monotonic(), nbs)
    return {"notebooks": nbs}


def list_sources(notebook_id: str, force: bool = False, profile: str | None = None) -> dict:
    """Files/sources inside one notebook: {sources: [{id,title}], error?}. Cached per notebook."""
    ok, why = available(profile)
    if not ok:
        return {"sources": [], "error": why}
    hit = _sources_cache.get(notebook_id)
    if not force and hit and time.monotonic() - hit[0] < LIST_TTL:
        return {"sources": hit[1]}
    try:
        proc = _run(_with_profile([NLM_BIN, "source", "list", notebook_id, "--json"], profile),
                    LIST_TIMEOUT)
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


def _notebook_count(profile: str | None = None) -> int:
    """Best-effort count of notebooks in the account (-1 if it can't be read)."""
    try:
        return len(list_notebooks(force=True, profile=profile).get("notebooks") or [])
    except Exception:
        return -1


def create_notebook(title: str, profile: str | None = None) -> dict:
    """Create a notebook: {id, title} | {error, limit?}. NotebookLM caps an account at
    ~100 notebooks; over that, create fails with a cryptic 'API error (code 3):
    INVALID_ARGUMENT' — we translate that into an actionable message (delete some)."""
    ok, why = available(profile)
    if not ok:
        return {"error": why}
    try:
        proc = _run(_with_profile([NLM_BIN, "notebook", "create", title, "--json"], profile),
                    LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm notebook create: timeout"}
    # the CLI may signal failure via returncode OR a {"status":"error"} JSON payload
    data = {}
    try:
        data = _json_after(proc.stdout, "{")
        if isinstance(data, dict):
            data = data.get("value", data)
    except Exception:
        data = {}
    nb_id = data.get("id") or data.get("notebook_id") if isinstance(data, dict) else None
    if nb_id:
        _list_cache.pop(_prof(profile), None)   # the notebook list changed
        return {"id": nb_id, "title": data.get("title") or title}
    err = (data.get("error") if isinstance(data, dict) else "") or ""
    if not err:
        err = (proc.stderr or proc.stdout).strip()[:400]
    # the cap error's shape has drifted: code 3 INVALID_ARGUMENT (2026-06), then
    # code 8 RESOURCE_EXHAUSTED (seen live 2026-08-05) — treat either as the cap
    if ("INVALID_ARGUMENT" in err or "RESOURCE_EXHAUSTED" in err
            or "limit" in err.lower() or "quota" in err.lower()):
        n = _notebook_count(profile)
        return {"limit": True,
                "error": ("NotebookLM refused to create the notebook"
                          + (f" — your account has {n} notebooks" if n >= 0 else "")
                          + " (NotebookLM caps an account at ~100). Delete some old notebooks "
                          "(🗑 in the 🔗 Notebook list) to free a slot, then try again.")}
    return {"error": err or "nlm notebook create failed"}


def delete_notebook(notebook_id: str, profile: str | None = None) -> dict:
    """Delete a notebook permanently: {ok} | {error}. Frees a slot toward the ~100 cap."""
    ok, why = available(profile)
    if not ok:
        return {"error": why}
    try:
        proc = _run(_with_profile([NLM_BIN, "notebook", "delete", notebook_id, "-y"], profile),
                    LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm notebook delete: timeout"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "delete failed"}
    try:                                    # also catch a {"status":"error"} payload
        data = _json_after(proc.stdout, "{")
        val = data.get("value", data) if isinstance(data, dict) else {}
        if isinstance(val, dict) and val.get("status") == "error":
            return {"error": str(val.get("error") or "delete failed")[:400]}
    except Exception:
        pass
    _list_cache.pop(_prof(profile), None)
    _sources_cache.pop(notebook_id, None)
    return {"ok": True}


def _clip_bytes(text: str, limit: int = 120_000) -> str:
    """Clip to a UTF-8 BYTE budget (the kernel argv cap MAX_ARG_STRLEN is 128 KiB
    of BYTES, not characters — a CJK patent is ~3 B/char, so a 100k-CHAR clip
    overflows argv and makes `nlm source add` hang). Truncate on a char boundary."""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:limit].decode("utf-8", "ignore")


def add_source_text(notebook_id: str, title: str, text: str,
                    profile: str | None = None) -> dict:
    """Add a text source to a notebook: {ok} | {error, full?}. Text is clipped to
    a UTF-8 BYTE budget (kernel MAX_ARG_STRLEN caps a single argv entry at 128 KiB
    — byte-clipping, not char-clipping, is what keeps CJK sources from hanging)."""
    ok, why = available(profile)
    if not ok:
        return {"error": why}
    srcs = list_sources(notebook_id, force=True, profile=profile)
    if not srcs.get("error") and len(srcs.get("sources") or []) >= SOURCE_LIMIT:
        return {"error": f"notebook is full ({SOURCE_LIMIT} sources)", "full": True}
    cmd = _with_profile([NLM_BIN, "source", "add", notebook_id,
                         "--text", _clip_bytes(text), "--title", title[:200]], profile)
    try:
        proc = _run(cmd, QUERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm source add: timeout"}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()[:400] or "source add failed"
        return {"error": err, "full": "limit" in err.lower() or "full" in err.lower()}
    _sources_cache.pop(notebook_id, None)   # the source list changed
    return {"ok": True}


def delete_source(source_ids: list[str], notebook_id: str | None = None,
                  profile: str | None = None) -> dict:
    """Permanently delete one or more sources (dedup / free the 50-source cap):
    {ok, deleted} | {error}. notebook_id, when given, drops that notebook's source
    cache so a re-list reflects the deletion immediately."""
    ok, why = available(profile)
    if not ok:
        return {"error": why}
    ids = [s for s in (source_ids or []) if s]
    if not ids:
        return {"ok": True, "deleted": 0}
    cmd = _with_profile([NLM_BIN, "source", "delete", *ids, "-y"], profile)
    try:
        proc = _run(cmd, LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "nlm source delete: timeout"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "source delete failed"}
    if notebook_id:
        _sources_cache.pop(notebook_id, None)
    return {"ok": True, "deleted": len(ids)}


def source_content(source_id: str, profile: str | None = None) -> dict:
    """Raw text content of ONE source inside a notebook (no AI processing):
    {content} | {error}. Used to import a non-patent source into the workbench."""
    ok, why = available(profile)
    if not ok:
        return {"error": why}
    try:
        proc = _run(_with_profile([NLM_BIN, "source", "content", source_id, "--json"], profile),
                    QUERY_TIMEOUT)
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


def wait_sources_ready(notebook_id: str, timeout: float = 240.0, poll: float = 8.0,
                       known_ready: set[str] | None = None,
                       profile: str | None = None,
                       _sleep=time.sleep, _now=time.monotonic) -> dict:
    """Block until EVERY source in a notebook is ingested (i.e. queryable), or until
    `timeout` seconds elapse. NotebookLM accepts `source add` instantly but then needs
    time to PROCESS the text; querying before that finishes yields the truncated
    'the full text isn't explicitly present' answers and wastes the query. source_content()
    returns text only once a source is processed and costs NO Gemini chat quota, so we use
    it as the readiness probe (each source is probed once, then remembered).
    `known_ready` seeds the confirmed set — sources that stayed in the notebook
    from a previous round (benchmark, survivors) don't need re-probing. Returns
    {ready, processed, total}."""
    ok, why = available(profile)
    if not ok:
        return {"ready": False, "processed": 0, "total": 0, "error": why}
    deadline = _now() + timeout
    ready_ids: set[str] = set(known_ready or ())
    total = 0
    while True:
        srcs = list_sources(notebook_id, force=True, profile=profile).get("sources") or []
        total = len(srcs)
        for s in srcs:                              # probe only the not-yet-confirmed ones
            if s["id"] not in ready_ids and "content" in source_content(s["id"], profile=profile):
                ready_ids.add(s["id"])
        if total and len(ready_ids) >= total:
            return {"ready": True, "processed": len(ready_ids), "total": total}
        if _now() >= deadline:
            return {"ready": False, "processed": len(ready_ids), "total": total}
        _sleep(poll)


# Q&A-endpoint quota exhaustion (account-scoped, resets 6-12h — verified in the
# patent-wiki-analyzer project 2026-05-21): every `notebook query` fails while
# source add/delete/list KEEP WORKING, so a paused job may still stage sources.
_QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|rate.?limit|quota|\b429\b", re.IGNORECASE)


def is_quota_error(res: dict) -> bool:
    """True when a bridge result means the Gemini Q&A quota is exhausted — either
    the explicit marker in the error text, or the empty-answer symptom (the CLI
    returns success with no answer once the quota is gone)."""
    return bool(res.get("quota") or res.get("quota_suspect"))


def _err_result(err: str) -> dict:
    out = {"error": err}
    if _QUOTA_RE.search(err or ""):
        out["quota"] = True
    return out


def query(notebook_id: str, question: str, source_ids: list[str] | None = None,
          profile: str | None = None) -> dict:
    """Ask one notebook (optionally restricted to EXACT source files inside it).
    Returns {answer, sources_used} or {error, quota?, quota_suspect?}."""
    ok, why = available(profile)
    if not ok:
        return {"error": why}
    cmd = _with_profile([NLM_BIN, "notebook", "query", notebook_id, question,
                         "--json", "--timeout", str(int(QUERY_TIMEOUT))], profile)
    if source_ids:
        cmd += ["--source-ids", ",".join(source_ids)]
    try:
        proc = _run(cmd, QUERY_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        return {"error": "NotebookLM query timed out"}
    if proc.returncode != 0:
        return _err_result((proc.stderr or proc.stdout).strip()[:400] or "nlm query failed")
    try:
        data = _json_after(proc.stdout, "{")
    except Exception as exc:
        return {"error": f"parse nlm output: {exc}"}
    # CLI ≤0.6.x wraps the payload in {"value": {...}}; newer versions return it top-level.
    val = data.get("value", data) if isinstance(data, dict) else {}
    if isinstance(val, dict) and val.get("status") == "error":
        return _err_result(str(val.get("error") or "nlm query failed")[:400])
    answer = (val.get("answer") or "").strip() if isinstance(val, dict) else ""
    if not answer:
        return {"error": "empty answer from NotebookLM (quota exhausted?)",
                "quota_suspect": True}
    return {"answer": answer,
            "sources_used": val.get("sources_used") or val.get("sources") or []}
