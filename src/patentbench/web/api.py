"""Patent Workbench FastAPI app — tabs, documents, chat, NotebookLM, lessons."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import claude_bridge, db, extract, fetcher, lessons, nlm_bridge, patents
from . import schemas

UPLOADS = os.environ.get("PB_UPLOADS", "/data/uploads")
MAX_UPLOAD = 25 * 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

app = FastAPI(title="Patent Workbench")


@app.middleware("http")
async def _no_store_api(request: Request, call_next):
    """API responses must never be cached by the browser. Otherwise a request
    made in the seconds while the container is restarting (empty/elsewhere) can
    be remembered — e.g. an empty /api/tabs, leaving the user without their
    tab until they manually clear the cache."""
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": f"{exc.__class__.__name__}: {exc}"})


def _tab_or_404(tab_id: int) -> None:
    if not db.tab_exists(tab_id):
        raise HTTPException(404, "tab not found")


def _read_model(m: str | None) -> str | None:
    """Validated reading-model override; None falls back to the cheap default."""
    return m if m in claude_bridge.MODELS else None


def _ocr_model(m: str | None) -> str:
    """Model for pulling patent NUMBERS off a photo. The cheap reading dropdown
    (haiku) reads digits unstably on screen photos, so floor it to the strong
    OCR_MODEL — but honour an explicitly-chosen stronger model (sonnet/opus)."""
    chosen = _read_model(m)
    if chosen is None or chosen in claude_bridge.WEAK_OCR_MODELS:
        return claude_bridge.OCR_MODEL
    return chosen


# ---------- health / meta ----------

@app.get("/api/health")
def health():
    nlm_ok, nlm_why = nlm_bridge.available()
    cl_ok, cl_why = claude_bridge.available()
    ls_ok, ls_why = lessons.available()
    return {"ok": True,
            "nlm": {"available": nlm_ok, "reason": nlm_why},
            "claude": {"available": cl_ok, "reason": cl_why},
            "lessons": {"available": ls_ok, "reason": ls_why}}


@app.get("/api/skills")
def skills():
    return {"skills": claude_bridge.list_skills(),
            "models": claude_bridge.MODELS,
            "default_model": claude_bridge.CHAT_MODEL,
            "default_read_model": claude_bridge.READ_MODEL}


# ---------- tabs ----------

@app.get("/api/tabs")
def tabs_list():
    return {"tabs": db.list_tabs()}


@app.post("/api/tabs")
def tabs_create(body: schemas.TabCreate):
    return db.create_tab(body.name)


@app.patch("/api/tabs/{tab_id}")
def tabs_patch(tab_id: int, body: schemas.TabPatch):
    if body.name is not None:
        if not db.rename_tab(tab_id, body.name):
            raise HTTPException(404, "tab not found")
    return {"ok": True}


@app.delete("/api/tabs/{tab_id}")
def tabs_delete(tab_id: int):
    if not db.delete_tab(tab_id):
        raise HTTPException(404, "tab not found")
    return {"ok": True}


def _benchmark_view(tab_id: int) -> dict | None:
    bm = db.get_benchmark(tab_id, full=False)
    if bm and bm.get("number"):
        bm["links"] = patents.links(bm["number"])
    return bm


def _doc_links(d: dict) -> dict | None:
    """Patent docs get Google/Espacenet links; text-only NLM imports have none."""
    return None if d.get("source") == "notebook-text" else patents.links(d["number"])


@app.get("/api/tabs/{tab_id}/state")
def tab_state(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_documents(tab_id)
    for d in docs:
        d["links"] = _doc_links(d)
    return {"benchmark": _benchmark_view(tab_id),
            "documents": docs,
            "messages": db.list_messages(tab_id),
            "notebook": db.get_notebook_config(tab_id)}


# ---------- benchmark (the reference document, one per tab) ----------

def _fetch_benchmark(tab_id: int) -> None:
    bm = db.get_benchmark(tab_id)
    if not bm or not bm.get("number"):
        return
    res = fetcher.fetch_document(bm["number"])
    if "error" in res:
        db.update_benchmark(tab_id, status="error", error=res["error"])
    else:
        db.update_benchmark(tab_id, status="ready", error=None, **res)
        _mirror_benchmark_if_auto(tab_id)


TRANSCRIBE_WORKERS = int(os.environ.get("PB_TRANSCRIBE_WORKERS", "4"))


def _extract_benchmark_files(tab_id: int, model: str | None = None) -> None:
    """Background: build the benchmark's text from its uploaded files —
    pdftotext for PDFs, Claude haiku page transcription for pictures.
    Pages are transcribed CONCURRENTLY and progress is written to the DB so the
    UI can show 'page 12/30' instead of a bare 'pending'."""
    bm = db.get_benchmark(tab_id)
    if not bm:
        return
    files = bm.get("files") or []
    total = len(files)
    done = 0
    lock = threading.Lock()
    db.update_benchmark(tab_id, progress=f"0/{total}")

    def one(f: dict) -> tuple[dict, dict]:
        nonlocal done
        res = (extract.text_from_pdf(f["path"]) if f["kind"] == "pdf"
               else extract.text_from_image(f["path"], model=model))
        with lock:
            done += 1
            db.update_benchmark(tab_id, progress=f"{done}/{total}")
        return f, res

    chunks, errors = [], []
    with ThreadPoolExecutor(max_workers=TRANSCRIBE_WORKERS) as ex:
        for f, res in ex.map(one, files):       # ex.map preserves page order
            if "error" in res:
                errors.append(f"{f['name']}: {res['error']}")
            else:
                chunks.append(f"--- {f['name']} ---\n{res['text']}")
    text = "\n\n".join(chunks)
    if not text:
        db.update_benchmark(tab_id, status="error", progress=None,
                            error="; ".join(errors) or "no text extracted")
        return
    db.update_benchmark(tab_id, status="ready", text=text, progress=None,
                        error="; ".join(errors) or None)
    _mirror_benchmark_if_auto(tab_id)


@app.put("/api/tabs/{tab_id}/benchmark")
def benchmark_set_number(tab_id: int, body: schemas.BenchmarkSet, bg: BackgroundTasks):
    """Set the benchmark by patent number (or a link containing one)."""
    _tab_or_404(tab_id)
    nums = patents.extract_candidates(body.text)
    if not nums:
        raise HTTPException(400, "no plausible patent number found")
    for f in db.clear_benchmark(tab_id):       # replacing: drop previous uploads
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    db.set_benchmark(tab_id, source="number", number=nums[0])
    bg.add_task(_fetch_benchmark, tab_id)
    return {"ok": True, "benchmark": _benchmark_view(tab_id)}


@app.post("/api/tabs/{tab_id}/benchmark/upload")
async def benchmark_upload(tab_id: int, bg: BackgroundTasks,
                           files: list[UploadFile] = File(...),
                           reading_model: str | None = Form(None)):
    """Set the benchmark from uploaded files: one PDF, or a BUNCH of page photos.
    All files land in the benchmark's own directory under the tab's uploads."""
    _tab_or_404(tab_id)
    bm_dir = os.path.join(UPLOADS, str(tab_id), "benchmark")
    os.makedirs(bm_dir, exist_ok=True)
    saved = []
    for uf in files:
        data = await uf.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{uf.filename}: too large (25 MB max)")
        name = os.path.basename(uf.filename or "page")
        ext = os.path.splitext(name)[1].lower()
        if ext not in IMAGE_EXT and ext != ".pdf":
            raise HTTPException(400, f"{name}: only PDF or images allowed for the benchmark")
        path = os.path.join(bm_dir, f"{uuid.uuid4().hex[:8]}-{name}")
        with open(path, "wb") as fh:
            fh.write(data)
        saved.append({"path": path, "name": name,
                      "kind": "pdf" if ext == ".pdf" else "image"})
    if not saved:
        raise HTTPException(400, "no files received")
    # browsers send multi-selected files in arbitrary order — natural-sort by name
    # so "page (2)" precedes "page (10)" and the transcription reads in page order
    def natkey(f):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", f["name"])]
    saved.sort(key=natkey)
    kinds = {f["kind"] for f in saved}
    if kinds == {"pdf"} and len(saved) > 1:
        raise HTTPException(400, "upload ONE PDF, or multiple pictures — not several PDFs")
    if "pdf" in kinds and "image" in kinds:
        raise HTTPException(400, "upload either a PDF or pictures, not a mix")
    # replacing the benchmark: drop previous uploaded files from disk
    for f in db.clear_benchmark(tab_id):
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    db.set_benchmark(tab_id, source="pdf" if "pdf" in kinds else "images", files=saved)
    bg.add_task(_extract_benchmark_files, tab_id, _read_model(reading_model))
    return {"ok": True, "benchmark": _benchmark_view(tab_id)}


@app.get("/api/tabs/{tab_id}/benchmark/full")
def benchmark_full(tab_id: int):
    """Full benchmark content (fetched fields / transcribed text) for the viewer —
    so the user can verify WHAT was actually stored, not just the title."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm:
        raise HTTPException(404, "no benchmark set")
    return bm


@app.delete("/api/tabs/{tab_id}/benchmark")
def benchmark_clear(tab_id: int):
    _tab_or_404(tab_id)
    for f in db.clear_benchmark(tab_id):
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    return {"ok": True}


# ---------- documents ----------

DIGEST_WORKERS = int(os.environ.get("PB_DIGEST_WORKERS", "4"))


def _fetch_into_db(doc_id: int) -> None:
    doc = db.get_document(doc_id)
    if not doc:
        return
    res = fetcher.fetch_document(doc["number"])
    if "error" in res:
        db.update_document(doc_id, status="error", error=res["error"])
    else:
        db.update_document(doc_id, status="fetched", error=None,
                           fetched_at=db._now(), **res)


def _digest_doc(doc_id: int, model: str | None = None) -> None:
    """Cheap-model pass over the candidate's FULL text → stored digest, so the
    chat is description-aware for every candidate from the get-go."""
    doc = db.get_document(doc_id)
    if not doc or doc["status"] != "fetched" or doc.get("digest"):
        return
    fulltext = "\n\n".join(filter(None, [doc.get("abstract"), doc.get("claims"),
                                         doc.get("description")]))
    if not fulltext:
        return
    res = claude_bridge.digest_document(doc["number"], doc.get("title") or "", fulltext,
                                        model=model)
    if "digest" in res:
        db.update_document(doc_id, digest=res["digest"])


MAX_FOCUS_DOCS = 5     # cap auto+manual focused candidates so the prompt stays tight
_DIGIT_RUN_RE = re.compile(r"\d{3,}")


def _auto_focus_ids(question: str, docs: list[dict]) -> list[int]:
    """Candidates the QUESTION explicitly refers to → load their FULL text without
    the user having to select them. Matches a full publication number anywhere in
    the text, OR a 3+ digit run that is a suffix of the number (users alias
    'CN113964850' as '850'). Conservative on length to avoid stray-number noise."""
    qnorm = re.sub(r"[^0-9a-z]", "", question.lower())
    qruns = {r for r in _DIGIT_RUN_RE.findall(question) if len(r) >= 3}
    out = []
    for d in docs:
        num = d.get("number") or ""
        if not num:
            continue
        bare = re.sub(r"[^0-9a-z]", "", num.lower())
        digits = re.sub(r"\D", "", num)
        if (bare and bare in qnorm) or any(digits.endswith(r) for r in qruns):
            out.append(d["id"])
    return out


def _doc_source_text(doc: dict) -> str:
    """The candidate as a NotebookLM text source — FULL primary text (abstract +
    claims + description with [00NN] markers) first so it's never clipped out by
    the derived digest; nlm_bridge clips the tail to ~100k chars."""
    return "\n\n".join(filter(None, [
        f"{doc['number']} — {doc.get('title') or ''}",
        ("ABSTRACT:\n" + doc["abstract"]) if doc.get("abstract") else None,
        ("CLAIMS:\n" + doc["claims"]) if doc.get("claims") else None,
        ("DESCRIPTION:\n" + doc["description"]) if doc.get("description") else None,
        ("FULL-TEXT DIGEST:\n" + doc["digest"]) if doc.get("digest") else None]))


def _add_doc_to_notebook(doc_id: int) -> dict:
    """Mirror one fetched candidate into the tab's connected notebook.
    {ok} | {error, full?} | {skip: reason}."""
    doc = db.get_document(doc_id)
    if not doc or doc["status"] != "fetched":
        return {"skip": "not fetched"}
    cfg = db.get_notebook_config(doc["tab_id"])
    if not cfg or not cfg.get("notebook_id"):
        return {"skip": "no notebook connected"}
    if doc.get("nlm_source_notebook") == cfg["notebook_id"]:
        return {"skip": "already added"}
    title = f"{doc['number']} — {(doc.get('title') or '')[:120]}"
    res = nlm_bridge.add_source_text(cfg["notebook_id"], title, _doc_source_text(doc))
    if res.get("ok"):
        db.update_document(doc_id, nlm_source_notebook=cfg["notebook_id"])
    return res


def _add_benchmark_to_notebook(tab_id: int) -> dict:
    """Mirror the tab's benchmark into the connected notebook as a text source.
    {ok} | {error, full?} | {skip: reason}."""
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        return {"skip": "benchmark not ready"}
    cfg = db.get_notebook_config(tab_id)
    if not cfg or not cfg.get("notebook_id"):
        return {"skip": "no notebook connected"}
    if bm.get("nlm_source_notebook") == cfg["notebook_id"]:
        return {"skip": "already added"}
    label = bm.get("number") or f"{len(bm.get('files') or [])} file(s)"
    title = f"🎯 BENCHMARK — {label}"
    res = nlm_bridge.add_source_text(cfg["notebook_id"], title, _benchmark_fulltext(bm))
    if res.get("ok"):
        db.update_benchmark(tab_id, nlm_source_notebook=cfg["notebook_id"])
    return res


# Auto-export: a brand-new project tab starts notebook-less, so the first content
# added to it (a candidate or the benchmark) auto-creates a NotebookLM notebook
# named after the project, connects it with auto-add ON, and rolls over to a
# follow-up notebook when the source cap is hit — so candidates land in NLM with
# no manual "connect a notebook" step. Disable with PB_AUTO_CREATE_NOTEBOOK=0.
AUTO_CREATE_NOTEBOOK = os.environ.get("PB_AUTO_CREATE_NOTEBOOK", "1") != "0"
_notebook_lock = threading.Lock()


def _tab_name(tab_id: int) -> str:
    for t in db.list_tabs():
        if t["id"] == tab_id:
            return t["name"]
    return f"tab {tab_id}"


def _rollover_title(title: str | None) -> str:
    """Next notebook in a series: 'X' -> 'X (2)', 'X (2)' -> 'X (3)'."""
    title = title or "Patent candidates"
    m = re.search(r" \((\d+)\)$", title)
    if m:
        return re.sub(r" \(\d+\)$", f" ({int(m.group(1)) + 1})", title)
    return f"{title} (2)"


def _create_and_connect(tab_id: int, title: str) -> dict | None:
    res = nlm_bridge.create_notebook(title)
    if not res.get("id"):
        return None
    db.set_notebook_config(tab_id, res["id"], res["title"], [], auto_add=True)
    return db.get_notebook_config(tab_id)


def _ensure_tab_notebook(tab_id: int) -> dict | None:
    """The tab's connected notebook config, AUTO-CREATING one (named after the
    project, auto-add ON) if none is connected yet. Returns the existing config
    untouched when one is already connected; None if NLM is unavailable / create
    failed or auto-create is disabled and nothing is connected."""
    cfg = db.get_notebook_config(tab_id)
    if cfg and cfg.get("notebook_id"):
        return cfg
    if not AUTO_CREATE_NOTEBOOK:
        return None
    with _notebook_lock:                              # avoid two batches racing to create
        cfg = db.get_notebook_config(tab_id)
        if cfg and cfg.get("notebook_id"):
            return cfg
        cfg = _create_and_connect(tab_id, f"Patent candidates — {_tab_name(tab_id)}")
        if cfg:
            db.append_message(tab_id, "s",
                              f"Auto-created NotebookLM notebook «{cfg['notebook_title']}» and "
                              "connected this project to it (auto-export on).")
        return cfg


def _rollover_notebook(tab_id: int, current: dict | None) -> dict | None:
    """The current notebook hit the source cap — connect a fresh follow-up notebook
    and return its config. Lock + re-check so concurrent fulls roll over only once."""
    with _notebook_lock:
        cfg = db.get_notebook_config(tab_id)
        cur_id = (current or {}).get("notebook_id")
        if cfg and cfg.get("notebook_id") and cfg["notebook_id"] != cur_id:
            return cfg                                # another thread already rolled over
        cfg = _create_and_connect(tab_id, _rollover_title((cfg or current or {}).get("notebook_title")))
        if cfg:
            # put the benchmark into the new notebook too, so candidates landing here
            # can be rated with a TINY grounded query instead of an embedded summary.
            _add_benchmark_to_notebook(tab_id)
            db.append_message(tab_id, "s",
                              f"Previous notebook was full — rolled over to «{cfg['notebook_title']}» "
                              "and kept exporting.")
        return cfg


def _auto_export_docs(tab_id: int, doc_ids: list[int]) -> None:
    """Mirror freshly-processed candidates into the tab's notebook, auto-creating it
    on first use and rolling over to a follow-up notebook at the source cap. Honours
    an explicit auto-add=OFF on an already-connected notebook (user opted out)."""
    cfg = db.get_notebook_config(tab_id)
    if not (cfg and cfg.get("notebook_id")):
        cfg = _ensure_tab_notebook(tab_id)
        if not cfg:
            return
    if not cfg.get("auto_add"):
        return                                        # connected but auto-export turned off
    for doc_id in doc_ids:                            # nlm_bridge serializes internally
        res = _add_doc_to_notebook(doc_id)
        if res.get("full"):
            cfg = _rollover_notebook(tab_id, cfg)
            if not cfg:
                db.append_message(tab_id, "s", "Notebook full and could not create a "
                                  "follow-up — remaining candidates not exported.")
                break
            _add_doc_to_notebook(doc_id)              # retry into the fresh notebook


def _mirror_benchmark_if_auto(tab_id: int) -> None:
    """After the benchmark becomes ready, export it — auto-creating/ rolling over the
    notebook like candidates do (the benchmark is the reference the notebook holds)."""
    cfg = db.get_notebook_config(tab_id)
    if not (cfg and cfg.get("notebook_id")):
        cfg = _ensure_tab_notebook(tab_id)
        if not cfg:
            return
    if not cfg.get("auto_add"):
        return
    res = _add_benchmark_to_notebook(tab_id)
    if res.get("full"):
        cfg = _rollover_notebook(tab_id, cfg)
        if cfg:
            _add_benchmark_to_notebook(tab_id)


def _process_documents(doc_ids: list[int], model: str | None = None) -> None:
    """Background pipeline for a batch: fetch each (throttled by the fetcher's
    own gap), digest all fetched docs concurrently, then mirror them into the
    connected NotebookLM notebook (auto-creating it on first use) — so the notebook
    stays a Claude-quota-independent fallback brain for the tab."""
    for doc_id in doc_ids:
        _fetch_into_db(doc_id)
    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(lambda i: _digest_doc(i, model), doc_ids))
    first = db.get_document(doc_ids[0]) if doc_ids else None
    if first:
        _auto_export_docs(first["tab_id"], doc_ids)


@app.post("/api/tabs/{tab_id}/documents")
def documents_add(tab_id: int, body: schemas.DocumentsAdd, bg: BackgroundTasks):
    _tab_or_404(tab_id)
    if body.numbers:
        nums = [patents.canonicalize(n) for n in body.numbers]
        nums = [n for n in dict.fromkeys(nums) if patents.is_plausible(n)]
    else:
        nums = patents.extract_candidates(body.text or "")
    if not nums:
        return {"inserted": [], "skipped": [], "error": "no plausible patent numbers found"}
    res = db.add_documents(tab_id, nums, source=body.source)
    if res["inserted"]:
        bg.add_task(_process_documents, res["inserted"], _read_model(body.reading_model))
    return res


@app.get("/api/tabs/{tab_id}/documents")
def documents_list(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_documents(tab_id)
    for d in docs:
        d["links"] = _doc_links(d)
    return {"documents": docs}


@app.get("/api/tabs/{tab_id}/documents/{doc_id}")
def document_full(tab_id: int, doc_id: int):
    """Full stored text of one candidate (title/abstract/claims/description)."""
    doc = db.get_document(doc_id)
    if not doc or doc["tab_id"] != tab_id:
        raise HTTPException(404, "document not found")
    doc["links"] = _doc_links(doc)
    return doc


@app.patch("/api/tabs/{tab_id}/documents/{doc_id}")
def document_edit_number(tab_id: int, doc_id: int, body: schemas.DocumentNumberEdit,
                         bg: BackgroundTasks):
    """Fix an OCR-damaged number; the document is refetched under the new number."""
    n = patents.canonicalize(body.number)
    if not patents.is_plausible(n):
        raise HTTPException(400, f"not a plausible patent number: {n}")
    res = db.set_document_number(tab_id, doc_id, n)
    if "error" in res:
        raise HTTPException(400, res["error"])
    bg.add_task(_process_documents, [doc_id])
    return {"ok": True, "number": n}


@app.post("/api/tabs/{tab_id}/documents/{doc_id}/refetch")
def document_refetch(tab_id: int, doc_id: int, bg: BackgroundTasks):
    doc = db.get_document(doc_id)
    if not doc or doc["tab_id"] != tab_id:
        raise HTTPException(404, "document not found")
    db.update_document(doc_id, status="pending", error=None)
    bg.add_task(_process_documents, [doc_id])
    return {"ok": True}


@app.delete("/api/tabs/{tab_id}/documents/{doc_id}")
def document_delete(tab_id: int, doc_id: int):
    if not db.delete_document(tab_id, doc_id):
        raise HTTPException(404, "document not found")
    return {"ok": True}


# ---------- upload (photos / PDF / txt → candidate numbers) ----------

UPLOAD_FILE_WORKERS = int(os.environ.get("PB_UPLOAD_WORKERS", "3"))
MAX_UPLOAD_FILES = int(os.environ.get("PB_MAX_UPLOAD_FILES", "40"))


def _extract_one(f: dict, ocr_model: str) -> tuple[str, dict]:
    """Pull patent numbers out of ONE saved file by its kind."""
    ext = f["ext"]
    if ext in IMAGE_EXT:
        return "image", extract.numbers_from_image(f["path"], ocr_model)
    if ext == ".pdf":
        return "pdf", extract.numbers_from_pdf(f["path"])
    try:
        with open(f["path"], "rb") as fh:
            return "text", extract.numbers_from_text(fh.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return "text", {"error": f"could not read file as text: {exc}"}


def _extract_numbers_from_files(saved: list[dict], ocr_model: str) -> dict:
    """Extract+aggregate numbers from a batch of files CONCURRENTLY. Numbers are
    unioned across files (deduped, first-seen order); uncertain flags are unioned;
    per-file results + errors are returned so the UI can show which photo gave what."""
    workers = max(1, min(UPLOAD_FILE_WORKERS, len(saved)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda f: _extract_one(f, ocr_model), saved))
    numbers: list[str] = []
    seen: set[str] = set()
    uncertain: set[str] = set()
    per_file, errors = [], []
    any_image = False
    for f, (kind, res) in zip(saved, results):
        any_image = any_image or kind == "image"
        if "error" in res:
            errors.append(f"{f['name']}: {res['error']}")
            per_file.append({"name": f["name"], "error": res["error"]})
            continue
        nums = res.get("numbers", [])
        for n in nums:
            if n not in seen:
                seen.add(n)
                numbers.append(n)
        uncertain.update(res.get("uncertain", []))
        per_file.append({"name": f["name"], "numbers": nums,
                         "uncertain": res.get("uncertain", [])})
    return {"numbers": numbers, "uncertain": sorted(uncertain),
            "files": per_file, "errors": errors,
            "model": ocr_model if any_image else None}


@app.post("/api/tabs/{tab_id}/upload")
async def upload(tab_id: int, files: list[UploadFile] = File(...),
                 reading_model: str | None = Form(None)):
    """Accept ONE OR MANY files (drop a whole stack of document-list photos at
    once) and aggregate every patent number found across all of them."""
    _tab_or_404(tab_id)
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, f"too many files ({len(files)}); max {MAX_UPLOAD_FILES}")
    tab_dir = os.path.join(UPLOADS, str(tab_id))
    os.makedirs(tab_dir, exist_ok=True)
    saved = []
    for uf in files:
        data = await uf.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{uf.filename}: too large (25 MB max)")
        name = os.path.basename(uf.filename or "upload")
        ext = os.path.splitext(name)[1].lower()
        path = os.path.join(tab_dir, f"{uuid.uuid4().hex[:8]}-{name}")
        with open(path, "wb") as fh:
            fh.write(data)
        kind = "image" if ext in IMAGE_EXT else ("pdf" if ext == ".pdf" else "text")
        db.record_upload(tab_id, path, name, kind)
        saved.append({"path": path, "name": name, "ext": ext})
    if not saved:
        raise HTTPException(400, "no files received")

    # Number extraction (image OCR especially) shells out to blocking `claude -p`
    # subprocesses that can run for minutes — and a batch fans that out across
    # files. This endpoint is async, so the whole batch runs in a worker thread to
    # keep the event loop (chat, manual number entry, tab switches) responsive.
    ocr_model = _ocr_model(reading_model)
    res = await run_in_threadpool(_extract_numbers_from_files, saved, ocr_model)
    if not res["numbers"] and res["errors"]:
        return {"error": "; ".join(res["errors"]), "numbers": [], "files": res["files"]}
    return res


# ---------- NotebookLM ----------

@app.get("/api/notebooks")
def notebooks(force: bool = False):
    return nlm_bridge.list_notebooks(force=force)


@app.get("/api/sources")
def sources(notebook_id: str, force: bool = False):
    return nlm_bridge.list_sources(notebook_id, force=force)


@app.put("/api/tabs/{tab_id}/notebook")
def notebook_set(tab_id: int, body: schemas.NotebookConfig):
    _tab_or_404(tab_id)
    db.set_notebook_config(tab_id, body.notebook_id, body.notebook_title, body.source_ids,
                           auto_add=body.auto_add)
    return {"ok": True, "notebook": db.get_notebook_config(tab_id)}


@app.post("/api/tabs/{tab_id}/notebook/sync")
def notebook_sync(tab_id: int):
    """Bulk-mirror the benchmark + every fetched candidate into the connected
    notebook. Stops at the source cap and reports it so the UI can propose a
    follow-up notebook."""
    _tab_or_404(tab_id)
    cfg = db.get_notebook_config(tab_id)
    if not cfg or not cfg.get("notebook_id"):
        raise HTTPException(400, "no notebook connected to this tab")
    added, errors, full = 0, [], False
    # benchmark first — it's the reference the notebook should always hold
    bm_res = _add_benchmark_to_notebook(tab_id)
    if bm_res.get("ok"):
        added += 1
    elif bm_res.get("full"):
        full = True
    elif bm_res.get("error"):
        errors.append(f"benchmark: {bm_res['error']}")
    docs = db.list_documents(tab_id)
    pending = [d for d in docs if d["status"] == "fetched"
               and d.get("nlm_source_notebook") != cfg["notebook_id"]]
    if not full:
        for d in pending:
            res = _add_doc_to_notebook(d["id"])
            if res.get("ok"):
                added += 1
            elif res.get("full"):
                full = True
                break
            elif res.get("error"):
                errors.append(f"{d['number']}: {res['error']}")
    remaining = sum(1 for d in pending
                    if db.get_document(d["id"]).get("nlm_source_notebook") != cfg["notebook_id"])
    return {"added": added, "remaining": remaining, "full": full,
            "errors": errors[:5], "total_fetched": len([d for d in docs
                                                        if d["status"] == "fetched"])}


@app.post("/api/tabs/{tab_id}/notebook/import")
def notebook_import(tab_id: int, bg: BackgroundTasks):
    """Pull the connected notebook's sources INTO the workbench (the other half of
    the bidirectional mirror). A source whose title/content names a patent number
    becomes a real candidate (re-fetched from Google Patents); any other source is
    imported as a raw text-only document via `nlm source content`. Idempotent:
    sources already imported (by id) or patents already in the tab are skipped."""
    _tab_or_404(tab_id)
    cfg = db.get_notebook_config(tab_id)
    if not cfg or not cfg.get("notebook_id"):
        raise HTTPException(400, "no notebook connected to this tab")
    nb_id = cfg["notebook_id"]
    listing = nlm_bridge.list_sources(nb_id, force=True)
    if listing.get("error"):
        raise HTTPException(400, f"could not list notebook sources: {listing['error']}")
    sources = listing.get("sources") or []
    already = db.imported_source_ids(tab_id)

    patent_numbers: list[str] = []           # number -> add as fetched candidate
    patent_src: dict[str, str] = {}          # canonical number -> originating source id
    text_added, text_errors, skipped = 0, [], 0
    for s in sources:
        sid, title = s["id"], (s.get("title") or "").strip()
        if sid in already:
            skipped += 1
            continue
        nums = patents.extract_candidates(title)          # patent number in the title?
        if nums:
            n = nums[0]
            patent_numbers.append(n)
            patent_src.setdefault(n, sid)
            continue
        # non-patent source → import its raw content as a text-only document
        content = nlm_bridge.source_content(sid)
        if content.get("error"):
            text_errors.append(f"{title or sid}: {content['error']}")
            continue
        new_id = db.add_text_document(
            tab_id, number=(title or f"NLM source {sid}")[:120], title=title,
            content=content["content"], nlm_source_id=sid, nlm_source_notebook=nb_id)
        if new_id:
            text_added += 1
        else:
            skipped += 1

    # patents: dedupe-insert, then fetch+digest in the background. Mark them as
    # already-in-this-notebook so the export side won't push them straight back.
    ins = db.add_documents(tab_id, list(dict.fromkeys(patent_numbers)), source="notebook")
    for doc_id in ins["inserted"]:
        doc = db.get_document(doc_id)
        sid = patent_src.get(doc["number"]) if doc else None
        db.update_document(doc_id, nlm_source_notebook=nb_id, nlm_source_id=sid)
    if ins["inserted"]:
        bg.add_task(_process_documents, ins["inserted"], None)
    patents_added, patents_skipped = len(ins["inserted"]), len(ins["skipped"])

    db.append_message(
        tab_id, "s",
        f"📥 Imported from «{cfg.get('notebook_title') or nb_id}»: "
        f"{patents_added} patent candidate(s) (fetching full text), {text_added} text source(s)"
        + (f"; skipped {skipped + patents_skipped} already present" if (skipped or patents_skipped) else "")
        + (f"; errors: {'; '.join(text_errors[:3])}" if text_errors else ""))
    return {"patents_added": patents_added, "text_added": text_added,
            "skipped": skipped + patents_skipped, "errors": text_errors[:5],
            "total_sources": len(sources)}


@app.post("/api/tabs/{tab_id}/notebook/create")
def notebook_create(tab_id: int, body: schemas.NotebookCreate):
    """Create a fresh notebook (e.g. when the current one is full) and connect
    the tab to it, keeping auto-add on."""
    _tab_or_404(tab_id)
    res = nlm_bridge.create_notebook(body.title)
    if "error" in res:
        raise HTTPException(400, res["error"])
    db.set_notebook_config(tab_id, res["id"], res["title"], [], auto_add=True)
    db.append_message(tab_id, "s", f"Created notebook «{res['title']}» and connected "
                                   "the tab to it (auto-add on).")
    return {"ok": True, "notebook": db.get_notebook_config(tab_id)}


@app.post("/api/tabs/{tab_id}/ask-notebook")
def ask_notebook(tab_id: int, body: schemas.AskNotebookRequest):
    _tab_or_404(tab_id)
    cfg = db.get_notebook_config(tab_id)
    if not cfg or not cfg.get("notebook_id"):
        raise HTTPException(400, "no notebook connected to this tab")
    db.append_message(tab_id, "q", body.question)
    res = nlm_bridge.query(cfg["notebook_id"], body.question,
                           source_ids=cfg.get("selected_source_ids") or None)
    if "error" in res:
        msg = db.append_message(tab_id, "s", f"NotebookLM error: {res['error']}")
        return {"messages": [msg], "error": res["error"]}
    msg = db.append_message(
        tab_id, "a", res["answer"],
        participants=[{"kind": "notebook", "title": cfg.get("notebook_title") or cfg["notebook_id"],
                       "sources_restricted": bool(cfg.get("selected_source_ids"))}])
    return {"messages": [msg]}


# ---------- chat ----------

@app.post("/api/tabs/{tab_id}/chat")
def chat(tab_id: int, body: schemas.ChatRequest):
    _tab_or_404(tab_id)
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    history = db.list_messages(tab_id, limit=claude_bridge.MAX_HISTORY)
    db.append_message(tab_id, "q", body.question)
    out_messages = []
    participants = []

    nlm_sources = None
    if body.ask_notebook:
        cfg = db.get_notebook_config(tab_id)
        if cfg and cfg.get("notebook_id"):
            res = nlm_bridge.query(cfg["notebook_id"], body.question,
                                   source_ids=cfg.get("selected_source_ids") or None)
            title = cfg.get("notebook_title") or cfg["notebook_id"]
            if "error" in res:
                out_messages.append(db.append_message(tab_id, "s",
                                                      f"NotebookLM error: {res['error']}"))
            else:
                nlm_sources = [{"title": title, "answer": res["answer"]}]
                participants.append({"kind": "notebook", "title": title})
                out_messages.append(db.append_message(
                    tab_id, "a", res["answer"],
                    participants=[{"kind": "notebook", "title": title}]))
        else:
            out_messages.append(db.append_message(
                tab_id, "s", "Ask-notebook was on, but no notebook is connected to this tab."))

    documents = db.list_documents(tab_id, full=True) if body.use_documents else None
    benchmark = db.get_benchmark(tab_id) if body.use_documents else None
    # FOCUS = candidates loaded with FULL primary text (uncl­ipped) so the model can
    # quote real claims/[00NN] paragraphs. Sources, priority order: (1) the ones the
    # user checked; (2) candidates the CURRENT question names (full number or alias
    # like "850"); (3) STICKY — candidates named in the recent conversation, so a
    # follow-up that doesn't re-type the number keeps that candidate fully loaded.
    focus = None
    if documents:
        recent_q = " ".join(h.get("text", "") for h in history
                            if h.get("role") == "q")[-2000:]
        ordered = list(dict.fromkeys(
            (body.focus_ids or [])
            + _auto_focus_ids(body.question, documents)      # current turn (highest priority)
            + _auto_focus_ids(recent_q, documents)))         # sticky from recent questions
        if ordered:
            keep = set(ordered[:MAX_FOCUS_DOCS])
            focus = [d for d in documents if d["id"] in keep]
            documents = [d for d in documents if d["id"] not in keep]
    skill_blocks = []
    for name in body.skills:
        content = claude_bridge.load_skill(name)
        if content:
            skill_blocks.append({"name": name, "content": content})
            participants.append({"kind": "skill", "title": name})

    res = claude_bridge.chat(body.question, history=history, documents=documents,
                             sources=nlm_sources, skills=skill_blocks, model=model,
                             benchmark=benchmark, focus=focus, full=body.full)
    if "error" in res:
        out_messages.append(db.append_message(tab_id, "s", f"Claude error: {res['error']}"))
        return {"messages": out_messages, "error": res["error"]}

    participants.insert(0, {"kind": "model", "title": model})
    if benchmark:
        participants.append({"kind": "benchmark",
                             "title": benchmark.get("number")
                             or f"{len(benchmark.get('files') or [])} file(s)"})
    if focus:
        participants.append({"kind": "documents",
                             "title": f"{len(focus)} focused (full text)"})
    if body.use_documents and documents:
        participants.append({"kind": "documents", "title": f"{len(documents)} candidates"})
    out_messages.append(db.append_message(tab_id, "c", res["answer"], model=model,
                                          participants=participants))

    for les in res.get("lessons", []):
        saved = lessons.append_lesson(les["skill"], les["lesson"])
        note = (f"Lesson auto-appended to skill /{les['skill']} (references/lessons.md)."
                if saved.get("ok") else
                f"Lesson for /{les['skill']} NOT saved: {saved.get('error')}\n\n{les['lesson']}")
        out_messages.append(db.append_message(tab_id, "s", note))
    return {"messages": out_messages}


# ---------- NotebookLM rating (palmares: NLM score vs Claude score) ----------

# NLM rates ONE source per query, and candidates are spread across several
# notebooks (the 50-source cap rolls over), so a rating run sweeps EVERY notebook
# the tab uses. nlm_bridge already serialises calls with a min-gap, so a single
# background thread is the right shape — it never starves non-NLM web traffic.
# Job state lives in a FILE LOCK (not memory): gunicorn runs multiple workers, so
# an in-process flag would let a second worker start a duplicate run (doubling
# quota) and would mis-report status from the worker that isn't running the job.
_NLM_RATE_LOCK_TTL = 2 * 3600             # a lock older than this is treated as stale (job died)


def _nlm_rate_lock_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".nlm_rate_{tab_id}.lock")


def _nlm_rate_running(tab_id: int) -> bool:
    p = _nlm_rate_lock_path(tab_id)
    try:
        return (time.time() - os.path.getmtime(p)) < _NLM_RATE_LOCK_TTL
    except OSError:
        return False


def _nlm_rate_scope_ids(tab_id: int) -> list[int] | None:
    """The doc-id subset of the running job (None = all), read from the lock file."""
    try:
        with open(_nlm_rate_lock_path(tab_id)) as f:
            return json.load(f).get("ids")
    except (OSError, ValueError):
        return None

# Preferred: TINY query — both benchmark and candidate are sources in the notebook,
# so NotebookLM compares them grounded, with no benchmark text in the message.
NLM_RATE_PROMPT_GROUNDED = (
    "Two sources are attached: the one titled '🎯 BENCHMARK' is the reference "
    "invention; the other is a candidate patent. Rate how closely the CANDIDATE "
    "matches the BENCHMARK's technical solution. Reply with EXACTLY two lines:\n"
    "MATCH SCORE: <0-10>\n"
    "KEY FEATURES: <one short line, or none>"
)

# Fallback: only when the candidate's notebook does NOT hold the benchmark source;
# embeds a SHORT benchmark summary (kept small to save quota / speed up NotebookLM).
NLM_RATE_PROMPT = (
    "The attached source is ONE candidate patent. Rate how closely it matches the "
    "BENCHMARK invention below (judge the technical solution, not just the field). "
    "Reply with EXACTLY two lines:\n"
    "MATCH SCORE: <0-10>\n"
    "KEY FEATURES: <one short line, or none>\n\n"
    "BENCHMARK:\n{benchmark}"
)


def _benchmark_summary_for_nlm(bm: dict, limit: int = 4000) -> str:
    """Compact benchmark text to embed in the NLM rating question. NotebookLM
    REJECTS over-long questions (INVALID_ARGUMENT — the query string, not just
    argv, has a ceiling around ~5-7k chars), so keep the benchmark well under it;
    the abstract + independent claim carry the solution. Verified live 2026-06-16."""
    parts = [f"{bm.get('number') or ''} — {bm.get('title') or ''}".strip(" —")]
    if bm.get("text"):
        parts.append(bm["text"])
    else:
        parts += [bm.get("abstract") or "", bm.get("claims") or ""]
    return "\n\n".join(p for p in parts if p)[:limit]


def _notebook_source_index(nb: str) -> tuple[dict[str, str], str | None]:
    """({patent-number -> source_id}, benchmark_source_id|None) for a notebook,
    read from its source titles ('CN1234 — …' / '🎯 BENCHMARK — …'). Knowing the
    benchmark's source id lets the rating query stay TINY — we ground NotebookLM on
    the benchmark source instead of pasting the whole benchmark into every question."""
    m: dict[str, str] = {}
    bm_sid = None
    for s in (nlm_bridge.list_sources(nb, force=True).get("sources") or []):
        title = s.get("title") or ""
        if title.startswith("🎯 BENCHMARK"):
            bm_sid = s["id"]
        else:
            nums = patents.extract_candidates(title)
            if nums:
                m.setdefault(nums[0], s["id"])
    return m, bm_sid


def _run_nlm_rating(tab_id: int, force: bool, ids: list[int] | None) -> None:
    lock = _nlm_rate_lock_path(tab_id)
    try:
        bm = db.get_benchmark(tab_id)
        # short fallback summary — only used for notebooks that DON'T hold the
        # benchmark as a source (so the query can't ground on it). Small on purpose:
        # tiny messages = less quota burned and faster NotebookLM turnaround.
        bm_short = _benchmark_summary_for_nlm(bm, limit=1200) if bm else ""
        docs = [d for d in db.list_documents(tab_id, full=True)
                if d["status"] == "fetched" and d.get("nlm_source_notebook")]
        if ids:
            idset = set(ids)
            docs = [d for d in docs if d["id"] in idset]
        if not force:
            docs = [d for d in docs if d.get("nlm_score") is None]
        notebooks = {d["nlm_source_notebook"] for d in docs}
        indexes = {nb: _notebook_source_index(nb) for nb in notebooks}
        rated = 0
        for d in docs:
            nb = d["nlm_source_notebook"]
            number_map, bm_sid = indexes[nb]
            cand_sid = number_map.get(d["number"])
            if bm_sid and cand_sid:
                # TINY query: both the benchmark and the candidate are sources here,
                # so NotebookLM compares them grounded — no benchmark text in the prompt.
                q, sids = NLM_RATE_PROMPT_GROUNDED, [bm_sid, cand_sid]
            else:
                # this notebook lacks the benchmark source → embed the SHORT summary
                q = NLM_RATE_PROMPT.format(benchmark=bm_short)
                if not cand_sid:
                    q = f"(Find the source for patent {d['number']}.) " + q
                sids = [cand_sid] if cand_sid else None
            res = nlm_bridge.query(nb, q, source_ids=sids)
            parsed = claude_bridge.parse_verdict(res.get("answer", "")) if "answer" in res else {"score": None}
            if parsed.get("score") is not None:
                db.update_document(d["id"], nlm_score=parsed["score"],
                                   nlm_score_note=parsed.get("features"), nlm_scored_at=db._now())
                rated += 1
            os.utime(lock, None)              # heartbeat so the lock isn't seen as stale
        db.append_message(tab_id, "s",
                          f"NotebookLM rated {rated}/{len(docs)} candidate(s) across "
                          f"{len(notebooks)} notebook(s). Compare 🤖 Claude vs 📓 NLM in the "
                          "candidates ranking (Δ flags disagreements).")
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _nlm_rate_counts(tab_id: int) -> dict:
    """Counts for the status line — scoped to the running job's subset (if any)."""
    ids = _nlm_rate_scope_ids(tab_id) if _nlm_rate_running(tab_id) else None
    docs = [d for d in db.list_documents(tab_id)
            if d["status"] == "fetched" and d.get("nlm_source_notebook")]
    if ids:
        idset = set(ids)
        docs = [d for d in docs if d["id"] in idset]
    return {"total": len(docs),
            "rated": sum(1 for d in docs if d.get("nlm_score") is not None)}


@app.post("/api/tabs/{tab_id}/nlm-rate")
def nlm_rate(tab_id: int, body: schemas.NlmRateRequest):
    """Rate fetched candidates with NotebookLM (across all the notebooks they live
    in), to compare against Claude's deep-compare score. With doc_ids, only that
    selection is rated; otherwise every fetched candidate."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    if _nlm_rate_running(tab_id):
        return {"started": False, "running": True, **_nlm_rate_counts(tab_id)}
    ids = body.doc_ids or None
    lock = _nlm_rate_lock_path(tab_id)
    try:                                       # create the lock atomically — loser bails
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"ids": ids}).encode())
        os.close(fd)
    except FileExistsError:
        return {"started": False, "running": True, **_nlm_rate_counts(tab_id)}
    threading.Thread(target=_run_nlm_rating, args=(tab_id, bool(body.force), ids),
                     daemon=True).start()
    return {"started": True, "running": True, **_nlm_rate_counts(tab_id)}


@app.get("/api/tabs/{tab_id}/nlm-rate/status")
def nlm_rate_status(tab_id: int):
    """DB-derived so it's correct from any gunicorn worker (the job may run in
    another). done == rated candidates; running == the lock file is fresh."""
    _tab_or_404(tab_id)
    counts = _nlm_rate_counts(tab_id)
    return {"running": _nlm_rate_running(tab_id), "done": counts["rated"], **counts}


RECONCILE_MAX_DOCS = 30      # cap the disagreement set so the single call stays cheap


@app.post("/api/tabs/{tab_id}/reconcile")
def reconcile_scores(tab_id: int, body: schemas.ReconcileRequest):
    """Explain — in ONE cheap call over the stored rating NOTES (no full texts) —
    why Claude and NotebookLM disagree on the candidates where they diverge most."""
    _tab_or_404(tab_id)
    docs = [d for d in db.list_documents(tab_id)
            if d.get("score") is not None and d.get("nlm_score") is not None
            and abs(d["score"] - d["nlm_score"]) >= body.min_delta]
    if not docs:
        msg = db.append_message(tab_id, "s",
                                "Claude and NotebookLM agree (no candidate differs by "
                                f"≥{body.min_delta:g}). Rate more candidates with 📓 to compare.")
        return {"messages": [msg]}
    docs.sort(key=lambda d: abs(d["score"] - d["nlm_score"]), reverse=True)
    capped = len(docs) > RECONCILE_MAX_DOCS
    docs = docs[:RECONCILE_MAX_DOCS]
    bm = db.get_benchmark(tab_id)
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.DIGEST_MODEL
    q = (f"[Explain 🤖 Claude vs 📓 NLM disagreements — {len(docs)} candidate(s), "
         f"Δ≥{body.min_delta:g}]")
    out = [db.append_message(tab_id, "q", q)]
    res = claude_bridge.reconcile(_benchmark_summary_for_nlm(bm) if bm else "", docs, model=model)
    if "error" in res:
        out.append(db.append_message(tab_id, "s", f"Reconcile failed: {res['error']}"))
        return {"messages": out, "error": res["error"]}
    note = ("" if not capped else
            f"\n\n(Showed the {RECONCILE_MAX_DOCS} biggest gaps; rate fewer or raise Δ for the rest.)")
    out.append(db.append_message(tab_id, "c", res["answer"] + note, model=model,
                                 participants=[{"kind": "model", "title": model},
                                               {"kind": "documents",
                                                "title": f"{len(docs)} disagreements · notes only"}]))
    return {"messages": out}


# ---------- deep compare (full-text map-reduce) ----------

DEEP_DEFAULT_QUESTION = (
    "Rank ALL candidates by how closely they match the benchmark's technical "
    "solution, using the full-text verdicts. For each: score, the decisive "
    "overlapping features (claims AND description-level disclosure), and what "
    "disqualifies or weakens it. Name the single best fit and the runner-up, and "
    "state explicitly what evidence would change the ranking."
)


def _benchmark_fulltext(bm: dict) -> str:
    if bm.get("text"):
        return bm["text"]
    return "\n\n".join(filter(None, [
        f"{bm.get('number') or ''} — {bm.get('title') or ''}",
        bm.get("abstract"), bm.get("claims"), bm.get("description")]))


# Claude deep-read = the full-text MAP + REDUCE, run as a RELOAD-SAFE BACKGROUND
# JOB (mirrors NLM rating): a file lock + DB-derived progress so a browser reload
# never interrupts it; per-doc scores land live; the compiled ranking is posted to
# chat at the end (so it survives reloads). Progress this pass = scored_at >= the
# job's start time, which works for both fresh re-reads and Continue.
def _claude_read_lock_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".claude_read_{tab_id}.lock")


def _claude_read_running(tab_id: int) -> bool:
    try:
        return (time.time() - os.path.getmtime(_claude_read_lock_path(tab_id))) < _NLM_RATE_LOCK_TTL
    except OSError:
        return False


def _claude_read_meta(tab_id: int) -> dict:
    try:
        with open(_claude_read_lock_path(tab_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _claude_read_counts(tab_id: int) -> dict:
    running = _claude_read_running(tab_id)
    meta = _claude_read_meta(tab_id) if running else {}
    ids, started = meta.get("ids"), meta.get("started")
    docs = [d for d in db.list_documents(tab_id) if d["status"] == "fetched"]
    if ids is not None:
        idset = set(ids)
        docs = [d for d in docs if d["id"] in idset]
    if running and started is not None:        # progress = candidates (re)scored THIS pass
        done = sum(1 for d in docs if (d.get("scored_at") or 0) >= started)
    else:
        done = sum(1 for d in docs if d.get("score") is not None)
    return {"total": len(docs), "done": done}


def _promise(d: dict) -> float:
    """Rank key: read the MOST PROMISING first (avg of any Claude/NLM score we have),
    so a limited token budget is spent on the best candidates before it runs out."""
    vals = [v for v in (d.get("score"), d.get("nlm_score")) if v is not None]
    return sum(vals) / len(vals) if vals else -1.0


def _run_claude_read(tab_id: int, doc_ids: list[int], model: str, read_model: str,
                     skills: list[str], question: str, scope_label: str) -> None:
    lock = _claude_read_lock_path(tab_id)
    try:
        bm = db.get_benchmark(tab_id)
        bm_text = _benchmark_fulltext(bm)
        idset = set(doc_ids)
        docs = [d for d in db.list_documents(tab_id, full=True)
                if d["id"] in idset and d["status"] == "fetched"]
        docs.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
        history = db.list_messages(tab_id, limit=claude_bridge.MAX_HISTORY)

        def one(d: dict) -> dict:
            res = claude_bridge.deep_map(bm_text, d, model=read_model)
            verdict = res.get("verdict") or f"(read failed: {res.get('error')})"
            parsed = claude_bridge.parse_verdict(verdict)
            if parsed["score"] is not None:    # score + features + WHICH model/WHEN land on the row
                db.update_document(d["id"], score=parsed["score"], score_note=parsed["features"],
                                   scored_at=db._now(), score_model=read_model)
            os.utime(lock, None)               # heartbeat so the lock isn't seen as stale
            return {"number": d["number"], "title": d.get("title"), "verdict": verdict}

        with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
            verdicts = list(ex.map(one, docs))
        failed = sum(1 for v in verdicts if v["verdict"].startswith("(read failed"))
        db.append_message(
            tab_id, "s", f"Read {len(docs) - failed}/{len(docs)} candidates at FULL text ({read_model})"
            + (f"; {failed} failed (likely token limit) — ▶️ Continue deep-read once renewed" if failed else ""))

        skill_blocks = []
        participants = [{"kind": "model", "title": model},
                        {"kind": "benchmark",
                         "title": bm.get("number") or f"{len(bm.get('files') or [])} file(s)"},
                        {"kind": "documents", "title": f"{scope_label} candidates · full text"}]
        for name in skills:
            content = claude_bridge.load_skill(name)
            if content:
                skill_blocks.append({"name": name, "content": content})
                participants.append({"kind": "skill", "title": name})
        res = claude_bridge.deep_reduce(question, bm, verdicts, skills=skill_blocks,
                                        model=model, history=history)
        if "error" in res:
            db.append_message(tab_id, "s", f"Claude error compiling the ranking: {res['error']}")
        else:
            db.append_message(tab_id, "c", res["answer"], model=model, participants=participants)
            for les in res.get("lessons", []):
                saved = lessons.append_lesson(les["skill"], les["lesson"])
                db.append_message(tab_id, "s",
                                  f"Lesson auto-appended to skill /{les['skill']} (references/lessons.md)."
                                  if saved.get("ok") else
                                  f"Lesson for /{les['skill']} NOT saved: {saved.get('error')}\n\n{les['lesson']}")
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


@app.post("/api/tabs/{tab_id}/deep-compare")
def deep_compare(tab_id: int, body: schemas.DeepCompareRequest):
    """Start a RELOAD-SAFE background full-text deep-read: a cheap model reads each
    candidate in full vs the benchmark (most-promising first, scores land live),
    then the chosen model compiles the ranking into chat. skip_scored = CONTINUE
    (only read candidates not yet full-read, so a renewed budget isn't wasted)."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    all_docs = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    if body.doc_ids:
        want = set(body.doc_ids)
        docs = [d for d in all_docs if d["id"] in want]
    else:
        docs = list(all_docs)
    if body.skip_scored:
        docs = [d for d in docs if d.get("score") is None]
    if not docs:
        if body.skip_scored:
            return {"started": False, "messages": [db.append_message(
                tab_id, "s", "Every candidate has already been full-read by Claude — nothing to "
                             "continue. Use 🤖 Claude deep-read all to re-read.")]}
        raise HTTPException(400, "no fetched candidate documents"
                            + (" among the selected ones" if body.doc_ids else ""))
    if _claude_read_running(tab_id):
        return {"started": False, "running": True, **_claude_read_counts(tab_id)}
    target_ids = [d["id"] for d in docs]
    scope = (f"{len(docs)} of {len(all_docs)}" if len(docs) != len(all_docs) else f"all {len(docs)}")
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    question = (body.question or "").strip() or DEEP_DEFAULT_QUESTION
    read_model = _read_model(body.reading_model) or claude_bridge.DIGEST_MODEL
    docs.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
    db.append_message(tab_id, "q",
                      f"[Deep {'read · continue' if body.skip_scored else 'compare'} — {scope} "
                      f"candidates at full text: {', '.join(d['number'] for d in docs[:30])}]\n{question}")
    lock = _claude_read_lock_path(tab_id)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"ids": target_ids, "started": db._now()}).encode())
        os.close(fd)
    except FileExistsError:
        return {"started": False, "running": True, **_claude_read_counts(tab_id)}
    threading.Thread(target=_run_claude_read,
                     args=(tab_id, target_ids, model, read_model, list(body.skills), question, scope),
                     daemon=True).start()
    return {"started": True, "running": True, **_claude_read_counts(tab_id)}


@app.get("/api/tabs/{tab_id}/deep-compare/status")
def deep_compare_status(tab_id: int):
    """DB-derived progress for the background deep-read — correct from any worker."""
    _tab_or_404(tab_id)
    return {"running": _claude_read_running(tab_id), **_claude_read_counts(tab_id)}


# ---------- lessons (manual save) ----------

@app.post("/api/lessons")
def lesson_create(body: schemas.LessonCreate):
    res = lessons.append_lesson(body.skill, body.lesson)
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


# ---------- static frontend ----------

class _RevalidatingStatic(StaticFiles):
    """Serve the SPA with `Cache-Control: no-cache` so the browser ALWAYS
    revalidates app.js/style.css/index.html against the ETag before using a
    cached copy. Without this, a redeploy can leave a stale app.js running
    against fresh HTML (e.g. an input it expects was moved) → blank page.
    `no-cache` still allows 304s, so it costs a conditional request, not a
    re-download, when nothing changed."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", _RevalidatingStatic(directory=_static, html=True), name="static")
