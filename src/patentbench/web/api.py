"""Patent Workbench FastAPI app — tabs, documents, chat, NotebookLM, lessons."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import citations, claude_bridge, db, extract, fetcher, lessons, nlm_bridge, patents
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
            "default_read_model": claude_bridge.READ_MODEL,
            "answer_formats": [{"key": f["key"], "label": f["label"]}
                               for f in claude_bridge.ANSWER_FORMATS]}


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


@app.post("/api/tabs/{tab_id}/benchmark/features")
def benchmark_set_features(tab_id: int, body: schemas.BenchmarkFeatures):
    """Set the benchmark as a TARGET FEATURE COMBINATION spec instead of a
    document. The spec text becomes the benchmark verbatim — every downstream
    comparison (chat focus, deep-compare map-reduce, NLM rating, mirror) reads it
    through the same _benchmark_fulltext path, so no fetch/transcription runs."""
    _tab_or_404(tab_id)
    # Two input shapes: a weighted feature LIST (added one by one) wins; otherwise
    # the free-form spec window. The list is composed into the benchmark text so all
    # downstream readers (chat, deep-compare, NLM, mirror) work unchanged, and the
    # weights are stored separately to drive the candidate ranking.
    features = [{"name": f.name.strip(), "weight": f.weight}
                for f in body.features if f.name.strip()]
    if features:
        spec = _compose_feature_spec(features)
    else:
        spec = (body.spec or "").strip()
        if len(spec) < 10:
            raise HTTPException(400, "describe the feature combination, or add features one by one")
    for f in db.clear_benchmark(tab_id):       # replacing: drop previous uploads
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    title = (body.title or "").strip() or "🧩 Feature combination"
    db.set_benchmark_features(tab_id, spec, title, features=features or None)
    _mirror_benchmark_if_auto(tab_id)
    return {"ok": True, "benchmark": _benchmark_view(tab_id)}


_FEATURE_SPEC_HEADER = ("TARGET FEATURE COMBINATION — a matching document should "
                        "disclose these features")


def _compose_feature_spec(features: list[dict], extra: str = "") -> str:
    """Render a weighted feature list into the benchmark text a matching document
    must satisfy. Weights are shown so the reading model knows which features carry
    the most importance, but the decisive weighting is applied in code (ranking).
    `extra` = free-form text the user already wrote; preserved so an incremental
    add never discards it."""
    lines = [
        _FEATURE_SPEC_HEADER + " (importance weight 1–5 in brackets; the more, "
        "the more decisive):",
        "",
    ]
    for i, f in enumerate(features, 1):
        lines.append(f"{i}. [weight {f['weight']}] {f['name']}")
    lines.append("")
    lines.append("IMPLICIT MATCHES COUNT: if a document physically realizes a "
                 "feature above without using the literal wording, treat it as a match.")
    if extra.strip():
        lines += ["", "ADDITIONAL CONTEXT (kept from the free-form description):", extra.strip()]
    return "\n".join(lines)


@app.post("/api/tabs/{tab_id}/benchmark/features/add")
def benchmark_add_feature(tab_id: int, body: schemas.FeatureItem):
    """APPEND one weighted feature to the benchmark, non-destructively. Existing
    weighted features are kept; any free-form text already written is preserved as
    context. Creates a fresh feature benchmark if none exists yet."""
    _tab_or_404(tab_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "empty feature")
    bm = db.get_benchmark(tab_id)
    if bm and bm.get("source") not in (None, "features"):
        raise HTTPException(400, "the current benchmark is a document — remove it "
                                 "before defining the benchmark by features")
    features = list((bm.get("features") if bm else None) or [])
    features.append({"name": name, "weight": body.weight})
    # preserve free-form text the user already wrote (a features benchmark with text
    # but no weighted list yet) so adding a feature never throws it away
    extra = ""
    if bm and bm.get("source") == "features" and not (bm.get("features")):
        prior = (db.get_benchmark(tab_id, full=True) or {}).get("text") or ""
        if prior and _FEATURE_SPEC_HEADER not in prior:
            extra = prior
    title = (bm.get("title") if bm else "") or "🧩 Feature combination"
    db.set_benchmark_features(tab_id, _compose_feature_spec(features, extra), title,
                              features=features)
    _mirror_benchmark_if_auto(tab_id)
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


def _verify_citations(tab_id: int, answer: str) -> str:
    """Correct [00NN] paragraph locators in a model answer to the paragraph their
    quoted text actually occupies. Runs at the API layer (not in claude_bridge) so
    it can load the FULL text of ANY candidate the answer names — the model often
    cites candidates that weren't in the prompt's focus panel, pulling a stale
    number from the running conversation (seen live: EP3087655 [0029] for a [0025]
    quote, with EP3087655 not in focus). Only candidates actually mentioned are
    loaded, so the cost is bounded."""
    if not answer or "[" not in answer:
        return answer
    named = set(patents.extract_candidates(answer))
    if not named:
        return answer
    sources = [{"number": d["number"],
                "text": "\n\n".join(filter(None, [d.get("abstract"), d.get("claims"),
                                                  d.get("description")]))}
               for d in db.list_documents(tab_id, full=True)
               if d["number"] in named and d["status"] == "fetched"]
    bm = db.get_benchmark(tab_id)
    if bm:
        sources.append({"number": bm.get("number"),
                        "text": bm.get("text") or "\n\n".join(filter(
                            None, [bm.get("abstract"), bm.get("claims"), bm.get("description")]))})
    if not sources:
        return answer
    return citations.verify(answer, sources)["answer"]


def _add_doc_to_notebook(doc_id: int, notebook_id: str | None = None) -> dict:
    """Mirror one fetched candidate into a notebook — the tab's connected one by
    default, or an explicit `notebook_id` (the destination the user chose).
    {ok} | {error, full?} | {skip: reason}."""
    doc = db.get_document(doc_id)
    if not doc or doc["status"] != "fetched":
        return {"skip": "not fetched"}
    nb = notebook_id
    if not nb:
        cfg = db.get_notebook_config(doc["tab_id"])
        nb = cfg.get("notebook_id") if cfg else None
    if not nb:
        return {"skip": "no notebook connected"}
    if doc.get("nlm_source_notebook") == nb:
        return {"skip": "already added"}
    title = f"{doc['number']} — {(doc.get('title') or '')[:120]}"
    res = nlm_bridge.add_source_text(nb, title, _doc_source_text(doc))
    if res.get("ok"):
        db.update_document(doc_id, nlm_source_notebook=nb)
    return res


def _add_benchmark_to_notebook(tab_id: int, notebook_id: str | None = None) -> dict:
    """Mirror the tab's benchmark into a notebook (connected by default, or an
    explicit `notebook_id`). {ok} | {error, full?} | {skip: reason}."""
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        return {"skip": "benchmark not ready"}
    nb = notebook_id
    if not nb:
        cfg = db.get_notebook_config(tab_id)
        nb = cfg.get("notebook_id") if cfg else None
    if not nb:
        return {"skip": "no notebook connected"}
    if bm.get("nlm_source_notebook") == nb:
        return {"skip": "already added"}
    label = (bm.get("number") or bm.get("title")
             or f"{len(bm.get('files') or [])} file(s)")
    title = f"🎯 BENCHMARK — {label}"
    res = nlm_bridge.add_source_text(nb, title, _benchmark_fulltext(bm))
    if res.get("ok"):
        db.update_benchmark(tab_id, nlm_source_notebook=nb)
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


@app.delete("/api/notebooks/{notebook_id}")
def notebook_delete_account(notebook_id: str):
    """Delete a notebook permanently from the NotebookLM account (frees a slot toward
    the ~100-notebook cap). Any tab connected to it is disconnected so it doesn't try
    to query a notebook that no longer exists."""
    res = nlm_bridge.delete_notebook(notebook_id)
    if "error" in res:
        raise HTTPException(400, res["error"])
    db.nlm_cache_clear(notebook_id)                   # drop its now-orphan cached answers
    for t in db.list_tabs():
        cfg = db.get_notebook_config(t["id"])
        if cfg and cfg.get("notebook_id") == notebook_id:
            db.set_notebook_config(t["id"], None, None, [], auto_add=False)
    return {"ok": True}


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


@app.post("/api/tabs/{tab_id}/notebook/add-selected")
def notebook_add_selected(tab_id: int, body: schemas.NotebookAddSelected):
    """Push a CHOSEN subset of this tab's documents (optionally the benchmark) into
    the connected notebook. Stops at the source cap and reports `full` + how many of
    the requested items are still missing, so the UI can offer a follow-up notebook
    and re-call with the same payload (the rollover then re-adds them there)."""
    _tab_or_404(tab_id)
    nb = body.notebook_id                            # the destination the user chose
    if not nb:
        cfg = db.get_notebook_config(tab_id)
        nb = cfg.get("notebook_id") if cfg else None
    if not nb:
        cfg = _ensure_tab_notebook(tab_id)          # none connected → auto-create one
        nb = cfg.get("notebook_id") if cfg else None
    if not nb:
        raise HTTPException(400, "no notebook connected and auto-create is disabled")
    added, errors, full = 0, [], False
    if body.include_benchmark:
        res = _add_benchmark_to_notebook(tab_id, nb)
        if res.get("ok"):
            added += 1
        elif res.get("full"):
            full = True
        elif res.get("error"):
            errors.append(f"benchmark: {res['error']}")
    if not full:
        for doc_id in body.doc_ids:
            res = _add_doc_to_notebook(doc_id, nb)
            if res.get("ok"):
                added += 1
            elif res.get("full"):
                full = True
                break
            elif res.get("error"):
                d = db.get_document(doc_id)
                errors.append(f"{(d or {}).get('number', doc_id)}: {res['error']}")
            # a {skip} (not fetched / already in this notebook) is silently ignored
    remaining = 0
    if body.include_benchmark:
        bm = db.get_benchmark(tab_id)
        if bm and bm.get("status") == "ready" and bm.get("nlm_source_notebook") != nb:
            remaining += 1
    for doc_id in body.doc_ids:
        d = db.get_document(doc_id)
        if d and d["status"] == "fetched" and d.get("nlm_source_notebook") != nb:
            remaining += 1
    titles = {n["id"]: n["title"]
              for n in (nlm_bridge.list_notebooks().get("notebooks") or [])}
    return {"added": added, "remaining": remaining, "full": full, "errors": errors[:5],
            "notebook_id": nb, "notebook_title": titles.get(nb, nb)}


@app.post("/api/tabs/{tab_id}/notebook/consolidate")
def notebook_consolidate(tab_id: int, body: schemas.NotebookConsolidate):
    """Create ONE new notebook (the name the user chose) and copy a chosen set of the
    tab's candidates (+ benchmark) into it, then connect the tab to it. The point: the
    candidates are normally spread across rollover notebooks (50-source cap), so no
    single NotebookLM query can compare them all — consolidating a focused set into one
    notebook lets 🏆 best-match pick a single global winner. Reports how many didn't fit."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    fetched = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    if body.doc_ids:
        idset = set(body.doc_ids)
        docs = [d for d in fetched if d["id"] in idset]
    else:
        docs = fetched
    if not docs:
        raise HTTPException(400, "no fetched candidates to consolidate")
    created = nlm_bridge.create_notebook(body.title)
    if "error" in created or not created.get("id"):
        raise HTTPException(400, created.get("error") or "create failed")
    nb = created["id"]
    db.set_notebook_config(tab_id, nb, created["title"], [], auto_add=True)
    added, errors, full = 0, [], False
    if body.include_benchmark:
        res = _add_benchmark_to_notebook(tab_id, nb)
        if res.get("ok"):
            added += 1
        elif res.get("full"):
            full = True
        elif res.get("error"):
            errors.append(f"benchmark: {res['error']}")
    if not full:
        for d in docs:
            res = _add_doc_to_notebook(d["id"], nb)
            if res.get("ok"):
                added += 1
            elif res.get("full"):
                full = True
                break
            elif res.get("error"):
                errors.append(f"{d['number']}: {res['error']}")
    remaining = sum(1 for d in docs
                    if (db.get_document(d["id"]) or {}).get("nlm_source_notebook") != nb)
    db.append_message(
        tab_id, "s",
        f"🧺 Consolidated {added} document(s) into new notebook «{created['title']}» and connected "
        "the tab to it"
        + (f"; {remaining} didn't fit (NotebookLM's 50-source cap) — consolidate a smaller, "
           "best-only set" if full and remaining else "")
        + (f"; errors: {'; '.join(errors[:3])}" if errors else "") + ".")
    return {"ok": True, "added": added, "remaining": remaining, "full": full,
            "errors": errors[:5], "notebook": db.get_notebook_config(tab_id)}


# ---------- consolidate → shortlist → debate, as a resumable background job ----------
# A dropped browser request no longer interrupts the work (it runs in a server thread);
# a container restart no longer loses it (step + created-notebook id are persisted to a
# /data file the entrypoint does NOT clear, so ▶️ Resume continues from the last step).
PIPELINE_TTL = float(os.environ.get("PB_PIPELINE_TTL", "1200"))   # secs before a job looks stale
_PIPELINE_STEPS = ("consolidate", "shortlist", "debate")


def _pipeline_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".pipeline_{tab_id}.json")


def _pipeline_read(tab_id: int) -> dict | None:
    try:
        with open(_pipeline_path(tab_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pipeline_set(tab_id: int, **kw) -> dict:
    st = _pipeline_read(tab_id) or {}
    st.update(kw)
    with open(_pipeline_path(tab_id), "w") as f:
        json.dump(st, f)
    return st


def _pipeline_fresh(tab_id: int) -> bool:
    try:
        return (time.time() - os.path.getmtime(_pipeline_path(tab_id))) < PIPELINE_TTL
    except OSError:
        return False


def _pipeline_status(tab_id: int) -> dict:
    st = _pipeline_read(tab_id)
    if not st:
        return {"present": False, "running": False, "resumable": False, "step": None}
    active = st.get("step") in _PIPELINE_STEPS
    fresh = _pipeline_fresh(tab_id)
    if st.get("error"):
        phase = "error"
    elif st.get("step") == "done":
        phase = "done"
    elif active and fresh:
        phase = "running"
    elif active:
        phase = "interrupted"           # crashed/restarted mid-step → resumable
    else:
        phase = "idle"
    return {"present": True, "phase": phase, "running": phase == "running",
            "resumable": phase in ("error", "interrupted"), "step": st.get("step"),
            "status_text": st.get("status_text", ""), "error": st.get("error"),
            "notebook_id": st.get("notebook_id"), "notebook_title": st.get("notebook_title")}


def _run_pipeline(tab_id: int) -> None:
    st = _pipeline_read(tab_id)
    if not st:
        return
    try:
        if st.get("step") == "consolidate":
            nb = st.get("notebook_id")
            if not nb:                                  # fresh: create + copy (idempotent on resume)
                _pipeline_set(tab_id, status_text="🧺 creating notebook & copying finalists…")
                created = nlm_bridge.create_notebook(st["title"])
                if not created.get("id"):
                    raise RuntimeError(created.get("error") or "notebook create failed")
                nb = created["id"]
                db.set_notebook_config(tab_id, nb, created["title"], [], auto_add=True)
                st = _pipeline_set(tab_id, notebook_id=nb, notebook_title=created["title"])
                if st.get("include_benchmark", True):
                    _add_benchmark_to_notebook(tab_id, nb)
                copied = 0
                for did in st.get("doc_ids", []):
                    if _add_doc_to_notebook(did, nb).get("ok"):
                        copied += 1
                    _pipeline_set(tab_id, status_text=f"🧺 copying finalists… {copied}")
                db.append_message(tab_id, "s", f"🧺 Consolidated {copied} document(s) into "
                                  f"«{created['title']}» and connected the tab.")
            else:                                       # resume: the notebook already exists, reuse it
                db.set_notebook_config(tab_id, nb, st.get("notebook_title") or nb, [], auto_add=True)
            st = _pipeline_set(tab_id, step="shortlist")
        if st.get("step") == "shortlist":
            _pipeline_set(tab_id, status_text="📓 picking best + second-best…")
            nlm_shortlist(tab_id, schemas.NlmShortlistRequest(notebook_id=st.get("notebook_id")))
            st = _pipeline_set(tab_id, step="debate")
        if st.get("step") == "debate":
            _pipeline_set(tab_id, status_text="⚖️ Claude ↔ NotebookLM debating block by block…")
            # doc_ids=None → debate the UNION of NLM's finalists + Claude's top picks (the
            # bidirectional set); the debate adds any of Claude's picks missing from the notebook.
            nlm_challenge(tab_id, schemas.NlmChallengeRequest())
            st = _pipeline_set(tab_id, step="done")
        _pipeline_set(tab_id, step="done", status_text="✅ done", error=None)
    except Exception as exc:                            # keep the file so the user can ▶️ Resume
        _pipeline_set(tab_id, error=str(exc)[:300], status_text=f"interrupted: {str(exc)[:120]}")
        db.append_message(tab_id, "s",
                          f"Pipeline step failed: {str(exc)[:200]} — ▶️ Resume to continue.")


@app.post("/api/tabs/{tab_id}/pipeline")
def pipeline_start(tab_id: int, body: schemas.PipelineRequest):
    """Start (or ▶️ Resume) the consolidate→shortlist→debate background job."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    stt = _pipeline_status(tab_id)
    if stt["running"]:
        return {"started": False, "running": True, **stt}
    if body.resume:
        st = _pipeline_read(tab_id)
        if not st or st.get("step") not in _PIPELINE_STEPS:
            raise HTTPException(400, "no interrupted pipeline to resume")
        _pipeline_set(tab_id, error=None, status_text="▶️ resuming…")
        threading.Thread(target=_run_pipeline, args=(tab_id,), daemon=True).start()
        return {"started": True, "running": True, "resumed": True}
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    ids = body.doc_ids or []
    if not ids:
        raise HTTPException(400, "no finalists selected for the pipeline")
    if not body.title.strip():
        raise HTTPException(400, "name the consolidated notebook")
    _pipeline_set(tab_id, step="consolidate", title=body.title.strip(), doc_ids=ids,
                  include_benchmark=bool(body.include_benchmark), error=None,
                  notebook_id=None, notebook_title=None, status_text="queued…")
    threading.Thread(target=_run_pipeline, args=(tab_id,), daemon=True).start()
    return {"started": True, "running": True}


@app.get("/api/tabs/{tab_id}/pipeline/status")
def pipeline_status_ep(tab_id: int):
    _tab_or_404(tab_id)
    return _pipeline_status(tab_id)


def _notebook_signature(notebook_id: str) -> str:
    """A short fingerprint of a notebook's current source SET — so a cached answer is
    reused only while the sources are unchanged, and auto-misses once a source is
    added/removed (the answer would otherwise be stale)."""
    srcs = nlm_bridge.list_sources(notebook_id).get("sources") or []
    ids = ",".join(sorted(s["id"] for s in srcs))
    return hashlib.sha256(ids.encode()).hexdigest()[:16]


def _nlm_query_cached(notebook_id: str, question: str,
                      source_ids: list[str] | None = None, force: bool = False) -> dict:
    """nlm_bridge.query with a PERSISTENT answer cache keyed on
    (notebook, source-restriction, question, source-set signature). Identical queries
    return the stored answer for free — no NotebookLM call, no quota — and survive
    rebuilds (the cache lives in the /data DB). Stale automatically when sources change.
    Only successful answers are cached. {answer, cached?} | {error}."""
    sig = _notebook_signature(notebook_id)
    raw = "|".join([notebook_id, ",".join(source_ids or []), sig, question])
    key = hashlib.sha256(raw.encode()).hexdigest()
    if not force:
        hit = db.nlm_cache_get(key)
        if hit is not None:
            return {"answer": hit, "cached": True}
    res = nlm_bridge.query(notebook_id, question, source_ids=source_ids)
    if res.get("answer"):
        db.nlm_cache_put(key, notebook_id, question, res["answer"])
    return res


def _query_tab_series(tab_id: int, question: str) -> list[dict]:
    """Fan a question across EVERY notebook the tab's candidates are spread over.
    Auto-rollover splits candidates across several notebooks at the per-notebook
    source cap, so querying only the connected notebook misses every candidate in
    the siblings. Returns one entry per notebook, connected one FIRST:
    {notebook_id, title, answer} | {notebook_id, title, error}. The connected
    notebook honours its selected_source_ids; siblings are queried over all their
    sources. NLM serialises calls internally, so these run back-to-back."""
    cfg = db.get_notebook_config(tab_id) or {}
    connected = cfg.get("notebook_id")
    titles = {n["id"]: n["title"]
              for n in (nlm_bridge.list_notebooks().get("notebooks") or [])}
    out: list[dict] = []
    for nb in db.tab_notebook_ids(tab_id):
        sids = (cfg.get("selected_source_ids") or None) if nb == connected else None
        res = _nlm_query_cached(nb, question, source_ids=sids)
        entry = {"notebook_id": nb, "title": titles.get(nb, nb)}
        entry["error" if "error" in res else "answer"] = res.get("error") or res["answer"]
        out.append(entry)
    return out


def _series_answer_text(answers: list[dict]) -> str:
    """One assistant message from the fan-out: a single notebook's answer bare,
    several answers stitched under per-notebook headers."""
    if len(answers) == 1:
        return answers[0]["answer"]
    return "\n\n".join(f"**📓 {e['title']}**\n{e['answer']}" for e in answers)


@app.post("/api/tabs/{tab_id}/ask-notebook")
def ask_notebook(tab_id: int, body: schemas.AskNotebookRequest):
    _tab_or_404(tab_id)
    series = _query_tab_series(tab_id, body.question)
    if not series:
        raise HTTPException(400, "no notebook connected to this tab")
    db.append_message(tab_id, "q", body.question)
    answers = [e for e in series if "answer" in e]
    if not answers:
        err = "; ".join(f"{e['title']}: {e['error']}" for e in series)
        msg = db.append_message(tab_id, "s", f"NotebookLM error: {err}")
        return {"messages": [msg], "error": err}
    cfg = db.get_notebook_config(tab_id) or {}
    msg = db.append_message(
        tab_id, "a", _series_answer_text(answers),
        participants=[{"kind": "notebook", "title": e["title"],
                       "sources_restricted": bool(e["notebook_id"] == cfg.get("notebook_id")
                                                  and cfg.get("selected_source_ids"))}
                      for e in answers])
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
        series = _query_tab_series(tab_id, body.question)
        if not series:
            out_messages.append(db.append_message(
                tab_id, "s", "Ask-notebook was on, but no notebook is connected to this tab."))
        else:
            answers = [e for e in series if "answer" in e]
            errors = [e for e in series if "error" in e]
            if errors:
                out_messages.append(db.append_message(
                    tab_id, "s", "NotebookLM error: "
                    + "; ".join(f"{e['title']}: {e['error']}" for e in errors)))
            if answers:
                nlm_sources = [{"title": e["title"], "answer": e["answer"]} for e in answers]
                for e in answers:
                    participants.append({"kind": "notebook", "title": e["title"]})
                out_messages.append(db.append_message(
                    tab_id, "a", _series_answer_text(answers),
                    participants=[{"kind": "notebook", "title": e["title"]} for e in answers]))

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
                             benchmark=benchmark, focus=focus, full=body.full,
                             answer_format=body.answer_format)
    if "error" in res:
        out_messages.append(db.append_message(tab_id, "s", f"Claude error: {res['error']}"))
        return {"messages": out_messages, "error": res["error"]}

    participants.insert(0, {"kind": "model", "title": model})
    if benchmark:
        participants.append({"kind": "benchmark",
                             "title": _benchmark_label(benchmark)})
    if focus:
        participants.append({"kind": "documents",
                             "title": f"{len(focus)} focused (full text)"})
    if body.use_documents and documents:
        participants.append({"kind": "documents", "title": f"{len(documents)} candidates"})
    out_messages.append(db.append_message(tab_id, "c", _verify_citations(tab_id, res["answer"]),
                                          model=model, participants=participants))

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


# ---------- funnel stage 1: NotebookLM shortlist (free, broad) ----------

NLM_SHORTLIST_PROMPT = (
    "Across ALL the source documents provided, assess which ones best disclose the "
    "TARGET FEATURE COMBINATION below. Treat the listed surface-form synonyms and any "
    "IMPLICIT realisation (a document that physically does the step without the literal "
    "word) as a match. Answer in this order:\n"
    "1. SHORTLIST — list EVERY document that discloses ALL of the elements, each by its "
    "publication number (e.g. EP4340163A1, CN117241689).\n"
    "2. BEST and SECOND-BEST — name the single closest document and the runner-up by "
    "publication number (use these even if none disclose every element).\n"
    "3. FEATURE MAP — for the BEST and the SECOND-BEST, go through the target features "
    "ONE BY ONE and mark each YES / PARTIAL / NO with a few words of evidence, then state "
    "the feature(s) the BEST document still does NOT cover.\n"
    "Only use documents actually among the sources; do not invent publication numbers."
    "\n\n=== TARGET FEATURE COMBINATION ===\n{benchmark}"
)

NLM_SHORTLIST_QUERY_CAP = 6000   # NotebookLM rejects very long questions (~5-7k char ceiling)


def _shortlist_key(number: str) -> str:
    """Join key that ignores the kind-code suffix so an NLM-named 'EP4340163A1'
    matches a stored 'EP4340163' (or vice-versa) — same publication for shortlisting."""
    m = re.match(r"^([A-Z]{2}\d+)", number or "")
    return m.group(1) if m else (number or "")


@app.post("/api/tabs/{tab_id}/nlm-shortlist")
def nlm_shortlist(tab_id: int, body: schemas.NlmShortlistRequest):
    """FUNNEL STAGE 1 (free, broad). Ask NotebookLM — in ONE fan-out question across
    every notebook the candidates live in — which documents disclose the benchmark's
    full feature combination. Parse the publication numbers it names, match them
    against this tab's candidates, and return that shortlist so the user can then run
    the expensive 🤖 opus verification on just those few (stage 2)."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    cands = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    if not cands:
        raise HTTPException(400, "no fetched candidate documents to shortlist")
    spec = _benchmark_feature_spec_for_nlm(bm)        # weighted feature names → spec → summary
    question = (body.question or "").strip() or NLM_SHORTLIST_PROMPT.format(
        benchmark=(spec or "")[:NLM_SHORTLIST_QUERY_CAP])
    if body.notebook_id:
        # one consolidated notebook → a single global best/second-best across all of them
        titles = {n["id"]: n["title"]
                  for n in (nlm_bridge.list_notebooks().get("notebooks") or [])}
        qres = _nlm_query_cached(body.notebook_id, question, source_ids=None)
        e = {"notebook_id": body.notebook_id,
             "title": titles.get(body.notebook_id, body.notebook_id)}
        e["error" if "error" in qres else "answer"] = qres.get("error") or qres["answer"]
        series = [e]
    else:
        series = _query_tab_series(tab_id, question)
    if not series:
        raise HTTPException(400, "no notebook connected — connect/export to a notebook first")
    answers = [e for e in series if e.get("answer")]
    if not answers:
        errs = "; ".join(e.get("error", "?") for e in series)
        msg = db.append_message(tab_id, "s", f"NotebookLM shortlist failed: {errs}")
        return {"ok": False, "shortlist_ids": [], "messages": [msg], "error": errs}

    # union of every publication number NotebookLM named, matched to our candidates
    named: list[str] = []
    for e in answers:
        named += patents.extract_candidates(e["answer"])
    by_key: dict[str, dict] = {}
    for d in cands:                                  # first candidate per key wins
        by_key.setdefault(_shortlist_key(d["number"]), d)
    matched, seen_ids, unmatched = [], set(), []
    for n in named:
        d = by_key.get(_shortlist_key(n))
        if d and d["id"] not in seen_ids:
            seen_ids.add(d["id"])
            matched.append(d)
        elif not d and n not in unmatched:
            unmatched.append(n)

    if matched:                                       # persist the picks so they survive reloads
        db.set_shortlisted(tab_id, [d["id"] for d in matched])
    participants = [{"kind": "benchmark", "title": _benchmark_label(bm)}]
    for e in answers:
        participants.append({"kind": "notebook", "title": e.get("title") or e["notebook_id"]})
    out = [db.append_message(tab_id, "q",
                             f"[📓 NLM shortlist — which of {len(cands)} candidates best disclose the "
                             f"benchmark's full feature combination? (ranked best + per-feature map)]")]
    out.append(db.append_message(tab_id, "c", _series_answer_text(answers),
                                 model="notebooklm", participants=participants))
    summary = (f"📓 NotebookLM shortlisted {len(matched)} of {len(cands)} candidate(s)"
               + (f" — best first: {', '.join(d['number'] for d in matched)}" if matched else "")
               + ". The answer above ranks the best + second-best with a per-feature "
               "YES/PARTIAL/NO map. Review them (auto-checked on the right), then 🤖 Verify "
               "shortlist to run the precise opus read on just these.")
    if unmatched:
        summary += (f"\n\n({len(unmatched)} number(s) NotebookLM named are NOT in your candidate "
                    f"pool — add them as candidates if you want them assessed: {', '.join(unmatched[:20])}"
                    + (" …" if len(unmatched) > 20 else "") + ")")
    if not matched:
        summary = ("📓 NotebookLM named no document already in your candidate pool. See its answer "
                   "above" + (f"; numbers it mentioned: {', '.join(unmatched[:20])}" if unmatched else "")
                   + ".")
    out.append(db.append_message(tab_id, "s", summary))
    return {"ok": True, "shortlist_ids": [d["id"] for d in matched],
            "matched": [d["number"] for d in matched], "unmatched": unmatched,
            "total": len(cands), "messages": out}


def _benchmark_feature_spec_for_nlm(bm: dict, limit: int = NLM_SHORTLIST_QUERY_CAP) -> str:
    """The benchmark rendered as a SHORT target-feature list for the single NLM
    shortlist question — the weighted feature names when defined that way, else the
    feature spec text, else a compact abstract+claim summary. Tiny on purpose: the
    candidate texts live in the notebook (read Gemini-side, no tokens to us), so the
    question only has to carry the features to check against."""
    feats = bm.get("features") or []
    if feats:
        return "\n".join(f"{i}. {f['name']} (importance {f['weight']}/5)"
                         for i, f in enumerate(feats, 1))[:limit]
    if bm.get("source") == "features" and bm.get("text"):
        return bm["text"][:limit]
    return _benchmark_summary_for_nlm(bm, limit=limit)


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


NLM_DEBATE_PROMPT = (
    "Assess ONLY these documents — {finalists} — which are all in the sources. Some were "
    "picked by you earlier, OTHERS were rated highly by an independent full-text analysis "
    "(Claude); judge them ALL on equal footing. For EACH, go through the TARGET FEATURE BLOCKS "
    "below ONE BY ONE and mark each YES / PARTIAL / NO with a few words of evidence FROM THE "
    "DOCUMENT (treat an implicit realisation — doing the step without the literal word — as "
    "covered). Then name the single best and the runner-up, and — importantly — for any "
    "document you rank LOW, state SPECIFICALLY which block(s) it misses or only partially "
    "discloses, so a disagreement with the other analysis is explained. Do not bring in "
    "documents outside this list.\n\n=== TARGET FEATURE BLOCKS ===\n{spec}"
)
NLM_CHALLENGE_MAX = 10       # union of both sides' picks; still fits one bulk prompt per side
CLAUDE_TOP_MIN = 7.0         # Claude's "good picks" worth challenging NotebookLM with
# the block-by-block reconciliation is the decisive call → run it on the strong model
DEBATE_MODEL = os.environ.get("PB_DEBATE_MODEL", "claude-opus-4-8")


def _claude_finalist_brief(d: dict) -> str:
    """One finalist's Claude-side material for the debate: its stored per-block verdicts
    (if Claude deep-read it) + a clipped digest — NO full-text re-read, so the call is cheap."""
    parts = [f"{d['number']} — {(d.get('title') or '')[:90]}"]
    fs = d.get("feature_scores")
    if fs:
        parts.append("Claude blocks: " + "; ".join(f"{f.get('name')}={f.get('status')}" for f in fs))
    elif d.get("score") is not None:
        parts.append(f"Claude score {d['score']:g}/10 — {(d.get('score_note') or '').strip()}")
    parts.append("DIGEST: " + (d.get("digest") or d.get("score_note") or "(no digest)")[:3000])
    return "\n".join(parts)


@app.post("/api/tabs/{tab_id}/nlm-challenge")
def nlm_challenge(tab_id: int, body: schemas.NlmChallengeRequest):
    """Bidirectional Claude ↔ NotebookLM debate over the FINALISTS (NotebookLM's own stored
    picks — they're guaranteed to be in its notebook, so it can actually evaluate them). NLM
    re-reads them block-by-block (one bulk grounded prompt); Claude then argues per block from
    the finalists' stored digests/verdicts (one cheap call) and reconciles into a consensus +
    a disputed list. No full-text re-read, one prompt per side — neither overwhelms NLM nor
    burns tokens. Complements 🔍 'Why the gap?' (Claude over stored NOTES only)."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    docs = db.list_documents(tab_id, full=True)
    fetched = [d for d in docs if d["status"] == "fetched"]
    # subject = BIDIRECTIONAL union: NotebookLM's finalists (its shortlist) AND Claude's own
    # high-scored picks — so NLM is challenged on Claude's choices too, not only its own.
    if body.doc_ids:
        idset = set(body.doc_ids)
        subject = [d for d in fetched if d["id"] in idset]
    else:
        finalists = [d for d in fetched if d.get("shortlisted")] or sorted(
            [d for d in fetched if d.get("nlm_score") is not None],
            key=lambda d: (d["nlm_score"], d["id"]), reverse=True)
        claude_top = sorted([d for d in fetched if (d.get("score") or 0) >= CLAUDE_TOP_MIN],
                            key=lambda d: (d["score"], d["id"]), reverse=True)
        if not claude_top:                             # fall back to Claude's best few if none ≥ min
            claude_top = sorted([d for d in fetched if d.get("score") is not None],
                                key=lambda d: (d["score"], d["id"]), reverse=True)[:5]
        seen, subject = set(), []
        for d in finalists + claude_top:               # finalists first, then Claude's picks, deduped
            if d["id"] not in seen:
                seen.add(d["id"])
                subject.append(d)
    if not subject:
        raise HTTPException(400, "nothing to debate yet — run 📓 NLM shortlist / 🏆 Deep compare "
                                 "first, or check the documents to debate")
    subject = subject[:NLM_CHALLENGE_MAX]
    spec = _benchmark_feature_spec_for_nlm(bm)
    nums = ", ".join(d["number"] for d in subject)
    cfg = db.get_notebook_config(tab_id) or {}
    # ensure EVERY subject document is a source in the connected notebook — otherwise NLM
    # answers "not present in the source set" for Claude's picks that were never consolidated.
    not_in_nb = []
    if cfg.get("notebook_id"):
        nb = cfg["notebook_id"]
        _add_benchmark_to_notebook(tab_id, nb)
        for d in subject:
            if d.get("nlm_source_notebook") != nb:
                res = _add_doc_to_notebook(d["id"], nb)
                if not res.get("ok") and not res.get("skip"):
                    not_in_nb.append(d["number"])
    # 1) NotebookLM round — one bulk grounded prompt, scoped to the connected notebook (now
    #    holding both sides' picks); cached so re-runs are free.
    question = NLM_DEBATE_PROMPT.format(finalists=nums, spec=(spec or "")[:NLM_SHORTLIST_QUERY_CAP])
    if cfg.get("notebook_id"):
        titles = {n["id"]: n["title"]
                  for n in (nlm_bridge.list_notebooks().get("notebooks") or [])}
        qres = _nlm_query_cached(cfg["notebook_id"], question, source_ids=None, force=bool(not_in_nb))
        series = [{"notebook_id": cfg["notebook_id"],
                   "title": titles.get(cfg["notebook_id"], cfg["notebook_id"]),
                   **({"error": qres["error"]} if "error" in qres else {"answer": qres["answer"]})}]
    else:
        series = _query_tab_series(tab_id, question)
    finalists = subject                                # name kept for the messages below
    answers = [e for e in series if e.get("answer")]
    if not answers:
        errs = "; ".join(e.get("error", "?") for e in series) or "no notebook connected"
        msg = db.append_message(tab_id, "s", f"📓 NotebookLM debate failed: {errs}")
        return {"ok": False, "messages": [msg], "error": errs}
    nlm_answer = _series_answer_text(answers)
    # 2) Claude round — ONE strong-model (opus by default) call: argue per block from the
    #    subject's digests + stored verdicts vs NLM's reply, and reconcile. Opus, not haiku,
    #    because this is the decisive reasoning step.
    finalists_text = "\n\n".join(_claude_finalist_brief(d) for d in finalists)
    debate_model = DEBATE_MODEL if DEBATE_MODEL in claude_bridge.MODELS else claude_bridge.DIGEST_MODEL
    cres = claude_bridge.debate(spec, finalists_text, nlm_answer, model=debate_model)
    # 3) post the two-sided debate to chat
    nb_parts = [{"kind": "benchmark", "title": _benchmark_label(bm)}]
    for e in answers:
        nb_parts.append({"kind": "notebook", "title": e.get("title") or e["notebook_id"]})
    out = [db.append_message(
        tab_id, "q",
        f"[⚖️ Debate — Claude ↔ NotebookLM reconcile {len(finalists)} pick(s) block by block "
        f"(both sides' choices): {nums}]")]
    if not_in_nb:
        out.append(db.append_message(tab_id, "s", "Note: couldn't add to the notebook (so NLM "
                   f"couldn't judge them): {', '.join(not_in_nb)} — the notebook may be at its "
                   "50-source cap."))
    out.append(db.append_message(tab_id, "c", "**📓 NotebookLM (grounded on the documents):**\n\n"
                                 + nlm_answer, model="notebooklm", participants=nb_parts))
    if cres.get("answer"):
        out.append(db.append_message(
            tab_id, "c", f"**🤖 Claude · {debate_model} (reconciling block by block):**\n\n" + cres["answer"],
            model=debate_model,
            participants=[{"kind": "model", "title": debate_model},
                          {"kind": "documents", "title": f"{len(finalists)} picks · digests"}]))
    else:
        out.append(db.append_message(tab_id, "s", f"Claude side failed: {cres.get('error')}"))
    out.append(db.append_message(
        tab_id, "s", "⚖️ Above: NotebookLM's grounded block-by-block read of BOTH sides' picks and "
        "Claude's (opus) reconciliation. Tick the agreed best and 🤖 Verify with opus."))
    return {"ok": True, "messages": out, "finalists": [d["number"] for d in finalists]}


# ---------- deep compare (full-text map-reduce) ----------

DEEP_DEFAULT_QUESTION = (
    "Rank ALL candidates by how closely they match the benchmark's technical "
    "solution, using the full-text verdicts. For each: score, the decisive "
    "overlapping features (claims AND description-level disclosure), and what "
    "disqualifies or weakens it. Name the single best fit and the runner-up, and "
    "state explicitly what evidence would change the ranking."
)


def _benchmark_label(bm: dict) -> str:
    """Short human label for a benchmark: its number, else a feature-spec title,
    else the uploaded-file count."""
    return (bm.get("number") or bm.get("title")
            or f"{len(bm.get('files') or [])} file(s)")


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


def _claude_read_pause_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".claude_read_{tab_id}.pause")


def _claude_read_paused(tab_id: int) -> bool:
    return os.path.exists(_claude_read_pause_path(tab_id))


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


def _has_assessment(d: dict) -> bool:
    """True if this candidate was EVER full-read — a stored deep-map verdict, OR a
    legacy read that left only a score (verdict column predates those reads)."""
    return bool((d.get("verdict") or "").strip()) or d.get("score") is not None


def _stored_assessment(d: dict) -> str:
    """The candidate's reusable full-text assessment for the reduce/ranking. Uses the
    rich stored verdict when present; for a legacy read (score but no verdict)
    synthesizes a block from score + key features + digest so it still ranks."""
    v = (d.get("verdict") or "").strip()
    if v:
        return v
    parts = []
    if d.get("score") is not None:
        parts.append(f"MATCH SCORE: {d['score']:g}")
    if (d.get("score_note") or "").strip():
        parts.append("KEY FEATURES: " + d["score_note"].strip())
    if (d.get("digest") or "").strip():
        parts.append(d["digest"].strip())
    return "\n".join(parts).strip()


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
        bm_features = bm.get("features") or []     # weighted feature-combination mode
        idset = set(doc_ids)
        docs = [d for d in db.list_documents(tab_id, full=True)
                if d["id"] in idset and d["status"] == "fetched"]
        docs.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
        history = db.list_messages(tab_id, limit=claude_bridge.MAX_HISTORY)

        def one(d: dict) -> dict:
            res = claude_bridge.deep_map(bm_text, d, model=read_model,
                                         features=bm_features or None)
            ok = "verdict" in res
            verdict = res.get("verdict") or f"(read failed: {res.get('error')})"
            if ok:                             # PERSIST the read artifact so EVERY feature
                parsed = claude_bridge.parse_verdict(verdict)   # (chat, re-rank, future
                fields = dict(verdict=verdict, # deep-compares) reuses it without re-reading
                              scored_at=db._now(), score_model=read_model)
                if parsed["score"] is not None:    # score + features land on the row too
                    fields.update(score=parsed["score"], score_note=parsed["features"])
                if bm_features:                # per-feature YES/PARTIAL/NO → weighted ranking
                    fs = claude_bridge.parse_feature_check(verdict, bm_features)
                    fields["feature_scores"] = json.dumps(fs, ensure_ascii=False)
                db.update_document(d["id"], **fields)
            os.utime(lock, None)               # heartbeat so the lock isn't seen as stale
            return {"number": d["number"], "title": d.get("title"), "verdict": verdict, "ok": ok}

        # Pausable sliding window: keep ≤ DIGEST_WORKERS in flight and check the
        # pause flag between completions. Scores land per-candidate live (in `one`),
        # so pausing loses nothing — the unassessed ones keep score=None and ▶️
        # Continue (skip_scored) picks them up, possibly with a different model.
        pause_path = _claude_read_pause_path(tab_id)
        verdicts, paused = [], False
        with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
            pending, nxt = {}, iter(docs)
            for _ in range(DIGEST_WORKERS):           # prime the window
                d = next(nxt, None)
                if d is None:
                    break
                pending[ex.submit(one, d)] = d
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    pending.pop(fut)
                    verdicts.append(fut.result())
                if os.path.exists(pause_path):        # stop launching new work; drain in-flight
                    paused = True
                    for fut in pending:
                        verdicts.append(fut.result())
                    pending.clear()
                    break
                for _ in range(DIGEST_WORKERS - len(pending)):   # refill
                    d = next(nxt, None)
                    if d is None:
                        break
                    pending[ex.submit(one, d)] = d
        read = sum(1 for v in verdicts if v["ok"])
        failed = len(verdicts) - read
        if paused:
            remaining = len(docs) - len(verdicts)
            db.append_message(
                tab_id, "s",
                f"⏸ Paused — assessed {read}/{len(docs)} against the benchmark ({read_model})"
                + (f", {failed} failed" if failed else "")
                + f"; {remaining + failed} still to assess. Change the 📖 reading model if "
                "this one is wrong, then ▶️ Continue deep-read (only the un-assessed ones run).")
            return                                    # no partial ranking on pause — resume to finish

        # REDUCE over the WHOLE corpus of stored assessments — every candidate that
        # was EVER full-read (this run or any earlier one) participates in the ranking,
        # so already-read documents are reused, never re-read. Reading above only
        # filled the gaps; the ranking is always whole-corpus.
        corpus = [d for d in db.list_documents(tab_id, full=True)
                  if d["status"] == "fetched" and _has_assessment(d)]
        corpus.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
        reduce_verdicts = [{"number": d["number"], "title": d.get("title"),
                            "verdict": _stored_assessment(d)} for d in corpus
                           if _stored_assessment(d)]
        reused = max(0, len(corpus) - read)
        if not reduce_verdicts:
            db.append_message(tab_id, "s",
                              f"No candidate could be full-read ({failed} failed, likely token "
                              "limit) — nothing to rank. ▶️ Continue deep-read once renewed.")
            return
        db.append_message(
            tab_id, "s",
            (f"Read {read} candidate(s) at FULL text this run ({read_model}); "
             if read else "")
            + (f"{failed} failed (likely token limit); " if failed else "")
            + f"ranking ALL {len(corpus)} candidate(s) with a stored full-text "
            f"assessment" + (f" ({reused} reused, no re-read)" if reused else "") + ".")

        skill_blocks = []
        participants = [{"kind": "model", "title": model},
                        {"kind": "benchmark", "title": _benchmark_label(bm)},
                        {"kind": "documents",
                         "title": f"{len(corpus)} candidates · full text"
                         + (f" ({reused} reused)" if reused else "")}]
        for name in skills:
            content = claude_bridge.load_skill(name)
            if content:
                skill_blocks.append({"name": name, "content": content})
                participants.append({"kind": "skill", "title": name})
        res = claude_bridge.deep_reduce(question, bm, reduce_verdicts, skills=skill_blocks,
                                        model=model, history=history)
        if "error" in res:
            db.append_message(tab_id, "s", f"Claude error compiling the ranking: {res['error']}")
        else:
            db.append_message(tab_id, "c", _verify_citations(tab_id, res["answer"]),
                              model=model, participants=participants)
            for les in res.get("lessons", []):
                saved = lessons.append_lesson(les["skill"], les["lesson"])
                db.append_message(tab_id, "s",
                                  f"Lesson auto-appended to skill /{les['skill']} (references/lessons.md)."
                                  if saved.get("ok") else
                                  f"Lesson for /{les['skill']} NOT saved: {saved.get('error')}\n\n{les['lesson']}")
    finally:
        for p in (lock, _claude_read_pause_path(tab_id)):
            try:
                os.unlink(p)
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
        if not docs:
            raise HTTPException(400, "no fetched candidate documents among the selected ones")
    else:
        docs = list(all_docs)
    # READING happens only for candidates that still need it; the RANKING always
    # reuses the whole corpus of stored assessments (see _run_claude_read). Continue
    # = read only the un-assessed; Deep-read all / selected = (re-)read the targeted
    # set fresh. When nothing needs reading but assessments exist, RE-RANK from them
    # (zero reads) — work done anywhere is reused everywhere.
    to_read = [d for d in docs if not _has_assessment(d)] if body.skip_scored else list(docs)
    corpus = [d for d in all_docs if _has_assessment(d)]
    if not to_read and not corpus:
        raise HTTPException(400, "no fetched candidate documents"
                            + (" among the selected ones" if body.doc_ids else ""))
    if _claude_read_running(tab_id):
        return {"started": False, "running": True, **_claude_read_counts(tab_id)}
    target_ids = [d["id"] for d in to_read]
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    question = (body.question or "").strip() or DEEP_DEFAULT_QUESTION
    read_model = _read_model(body.reading_model) or claude_bridge.DIGEST_MODEL
    to_read.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
    if to_read:
        scope = (f"{len(to_read)} of {len(all_docs)}"
                 if len(to_read) != len(all_docs) else f"all {len(to_read)}")
        head = (f"[Deep {'read · continue' if body.skip_scored else 'compare'} — {scope} "
                f"candidates at full text: {', '.join(d['number'] for d in to_read[:30])}]")
    else:                                          # reduce-only: re-rank from stored
        scope = f"all {len(all_docs)}"
        head = (f"[Re-rank — compiling the stored full-text assessments of "
                f"{len(corpus)} candidate(s), no re-reading]")
    db.append_message(tab_id, "q", f"{head}\n{question}")
    lock = _claude_read_lock_path(tab_id)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"ids": target_ids, "started": db._now(),
                                 "read_model": read_model}).encode())
        os.close(fd)
    except FileExistsError:
        return {"started": False, "running": True, **_claude_read_counts(tab_id)}
    try:                                       # drop any stale pause flag from a crashed run
        os.unlink(_claude_read_pause_path(tab_id))
    except OSError:
        pass
    threading.Thread(target=_run_claude_read,
                     args=(tab_id, target_ids, model, read_model, list(body.skills), question, scope),
                     daemon=True).start()
    return {"started": True, "running": True, **_claude_read_counts(tab_id)}


@app.get("/api/tabs/{tab_id}/deep-compare/status")
def deep_compare_status(tab_id: int):
    """DB-derived progress for the background deep-read — correct from any worker."""
    _tab_or_404(tab_id)
    running = _claude_read_running(tab_id)
    return {"running": running, "paused": running and _claude_read_paused(tab_id),
            "read_model": _claude_read_meta(tab_id).get("read_model") if running else None,
            **_claude_read_counts(tab_id)}


@app.post("/api/tabs/{tab_id}/deep-compare/pause")
def deep_compare_pause(tab_id: int):
    """Ask a running assessment to pause: it stops launching new candidates, lets
    the in-flight ones finish (their scores are saved), and waits. Already-assessed
    candidates keep their scores; ▶️ Continue re-runs only the un-assessed ones, so
    you can switch the 📖 reading model between pause and continue."""
    _tab_or_404(tab_id)
    if not _claude_read_running(tab_id):
        return {"paused": False, "running": False, **_claude_read_counts(tab_id)}
    open(_claude_read_pause_path(tab_id), "w").close()
    return {"paused": True, "running": True, **_claude_read_counts(tab_id)}


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
