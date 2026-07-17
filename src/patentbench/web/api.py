"""Patent Workbench FastAPI app — tabs, documents, chat, NotebookLM, lessons."""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import (citations, claude_bridge, db, extract, fetcher, figures, kgraph,
                lessons, nlm_bridge, patents)
from . import schemas

UPLOADS = os.environ.get("PB_UPLOADS", "/data/uploads")
FIGURES = os.environ.get("PB_FIGURES", "/data/figures")
PSA_DIR = os.environ.get("PB_PSA_DIR", "/data/psa")   # ⚖️ problem-solution methodology
AUTO_FIGURES = os.environ.get("PB_AUTO_FIGURES", "1") not in ("0", "", "false", "no")
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


def _files_hash(paths: list[str]) -> str | None:
    """A stable content signature for an uploaded file-set: sha256 over each file's
    own sha256, sorted so file order doesn't matter. Lets an identical re-upload (in
    any tab) be recognised and its OCR/transcription reused. None if unreadable."""
    digests = []
    for p in paths:
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            digests.append(h.hexdigest())
        except OSError:
            return None
    if not digests:
        return None
    return hashlib.sha256("".join(sorted(digests)).encode()).hexdigest()


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
    if not bm:
        return bm
    if bm.get("number"):
        bm["links"] = patents.links(bm["number"])
    # replace the heavy figures JSON with light counts for the state payload
    figs = json.loads(bm["figures"]) if bm.get("figures") else []
    bm["figures"] = bool(figs)
    bm["figures_total"] = len(figs)
    bm["figures_n"] = sum(1 for f in figs if f.get("caption"))
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
            "notebook": db.get_notebook_config(tab_id),
            "combi_motivations": db.get_combi_motivations(tab_id)}


# ---------- benchmark (the reference document, one per tab) ----------

def _fetch_benchmark(tab_id: int, model: str | None = None) -> None:
    bm = db.get_benchmark(tab_id)
    if not bm or not bm.get("number"):
        return
    res = fetcher.fetch_document(bm["number"])
    if "error" in res:
        db.update_benchmark(tab_id, status="error", error=res["error"])
    else:
        urls = res.pop("figure_urls", None) or []
        figs = json.dumps([{"n": i, "url": u} for i, u in enumerate(urls, 1)]) if urls else None
        db.update_benchmark(tab_id, status="ready", error=None, figures=figs, **res)
        # caption the benchmark's drawings BEFORE mirroring, so NotebookLM and every
        # reader can ground on the benchmark's figures too.
        if AUTO_FIGURES and urls:
            _process_benchmark_figures(tab_id, model)
        _mirror_benchmark_if_auto(tab_id)


def _process_benchmark_figures(tab_id: int, model: str | None = None,
                               force: bool = False) -> int:
    """Caption the (number-based) benchmark's drawing sheets and merge them into its
    description, so figures are groundable on the benchmark side as well. Upload-based
    benchmarks carry their drawings in the page transcription already, so they are
    skipped (no separate figure URLs)."""
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("text"):           # upload/feature benchmark — no figure URLs
        return 0
    meta = json.loads(bm["figures"]) if bm.get("figures") else []
    if meta and all(f.get("caption") for f in meta) and not force:
        return sum(1 for f in meta if f.get("caption"))
    urls = [f["url"] for f in meta if f.get("url")]
    if (force or not urls) and bm.get("number"):
        urls = fetcher.figure_urls(bm["number"]) or urls
    if not urls:
        return 0
    figs = figures.download(urls, os.path.join(FIGURES, f"bm-{tab_id}"))
    figures.caption_all(figs, model, context=figures.doc_context(bm))
    merged = figures.merge_into_description(bm.get("description"), figs)
    n = sum(1 for f in figs if f.get("caption"))
    db.update_benchmark(tab_id, figures=json.dumps(figs, ensure_ascii=False),
                        description=merged)
    return n


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
    # only page photos go through a model; a pure-PDF benchmark records no text_model
    had_image = any(f["kind"] != "pdf" for f in files)
    db.update_benchmark(tab_id, status="ready", text=text, progress=None,
                        text_model=(model or claude_bridge.TRANSCRIBE_MODEL) if had_image else None,
                        error="; ".join(errors) or None)
    _mirror_benchmark_if_auto(tab_id)


@app.put("/api/tabs/{tab_id}/benchmark")
def benchmark_set_number(tab_id: int, body: schemas.BenchmarkSet, bg: BackgroundTasks):
    """Set the benchmark by patent number (or a link containing one)."""
    _tab_or_404(tab_id)
    nums = patents.extract_candidates(body.text)
    if not nums:
        raise HTTPException(400, "no plausible patent number found")
    # NOT clear_benchmark: set_benchmark replaces the row itself and carries the
    # written features across — clearing first would delete them (the whole point).
    for f in db.benchmark_files(tab_id):       # replacing: drop previous uploads
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
    features = [{"name": f.name.strip(), "weight": f.weight,
                 "kind": (f.kind if f.kind in ("M", "A") else "M"), "sl": f.sl}
                for f in body.features if f.name.strip()]
    # Re-weighting/editing the features of a DOCUMENT benchmark annotates it — it must
    # not silently replace the fetched document with a spec. To swap a document out for
    # a feature combination, remove the document first (or write a free-form spec).
    bm = db.get_benchmark(tab_id)
    if features and bm and bm.get("source") not in (None, "features"):
        db.set_benchmark_feature_list(tab_id, features)
        return {"ok": True, "benchmark": _benchmark_view(tab_id)}
    if features:
        spec = _compose_feature_spec(features)
    else:
        spec = (body.spec or "").strip()
        if len(spec) < 10:
            raise HTTPException(400, "describe the feature combination, or add features one by one")
    for f in db.benchmark_files(tab_id):       # replacing: drop previous uploads
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
    add never discards it.

    Only MANDATORY (kind=='M') features compose the base benchmark — the established score
    must not move when an ADDITIONAL (kind=='A') feature is absent. A-features are stored
    separately (features_json) and assessed by the ➕ additional read, which only adds to
    the score. If the user marked everything 'A', fall back to all so the spec isn't empty."""
    mand = [f for f in features if f.get("kind", "M") == "M"] or features
    lines = [
        _FEATURE_SPEC_HEADER + " (importance weight 1–5 in brackets; the more, "
        "the more decisive):",
        "",
    ]
    for i, f in enumerate(mand, 1):
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
    context. Creates a fresh feature benchmark if none exists yet.

    When the benchmark is a DOCUMENT the features annotate it instead of replacing it:
    only the feature list is written, so the fetched document stays put."""
    _tab_or_404(tab_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "empty feature")
    bm = db.get_benchmark(tab_id)
    features = list((bm.get("features") if bm else None) or [])
    features.append({"name": name, "weight": body.weight,
                     "kind": (body.kind if body.kind in ("M", "A") else "M"), "sl": body.sl})
    if bm and bm.get("source") not in (None, "features"):
        db.set_benchmark_feature_list(tab_id, features)   # keep the document itself
        return {"ok": True, "benchmark": _benchmark_view(tab_id)}
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


@app.post("/api/tabs/{tab_id}/benchmark/decompose")
def benchmark_decompose_ep(tab_id: int, body: schemas.DecomposeRequest):
    """🔬 PROPOSE a split of the claimed invention into its separable ELEMENTS.

    PROPOSES ONLY — nothing is stored and nothing is scored here. The caller shows the
    elements for review/edit and saves them through the normal features path, so a bad
    split can never silently poison the whole candidate list.

    Why it exists: coverage is judged per element. A claim held as ONE monolithic feature
    can never be combination-analysed — 🧩 combi needs both documents to contribute an
    element the other lacks, which is impossible with a single feature, so it silently
    finds nothing. Decomposition is what makes 2-document coverage meaningful."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    feats = (bm or {}).get("features") or []
    add_feats: list[dict] = []
    keep: list[dict] = []          # features passed through untouched (not re-split)
    if body.source == "text":
        text = (body.text or "").strip()
        if len(text) < 20:
            raise HTTPException(400, "paste the claim text to decompose")
    elif body.source == "additional":
        # Split ONLY the additional features; mandatory elements are already granular and
        # re-cutting them would discard wording the user reviewed and accepted.
        add_feats = [f for f in feats if (f.get("kind") or "M") == "A"]
        if not add_feats:
            raise HTTPException(400, "this benchmark has no additional features to decompose")
        text = ""
        keep = [f for f in feats if (f.get("kind") or "M") != "A"]
    elif body.source == "features":
        if not feats:
            raise HTTPException(400, "this benchmark has no features to decompose")
        text = "\n\n".join(f["name"] for f in feats if (f.get("kind") or "M") != "A")
        # ADDITIONAL features are monolithic for the same reason the claim was, and they are
        # what differentiates the documents that all cover the mandatory elements — so split
        # them too. Each is done separately so its own stretch level rides onto its elements.
        add_feats = [f for f in feats if (f.get("kind") or "M") == "A"]
        if not text.strip() and not add_feats:
            raise HTTPException(400, "no features to decompose")
    else:                                     # the benchmark document's own claims
        if not bm:
            raise HTTPException(400, "set a benchmark first")
        text = (bm.get("claims") or bm.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "the benchmark has no claims/text to decompose")
    model = _read_model(body.model)
    elements: list[dict] = list(keep)
    models: list[str] = []
    if text.strip():
        res = claude_bridge.decompose_claim(text, model=model)
        if "error" in res:
            raise HTTPException(400, f"decomposition failed: {res['error']}")
        elements += res["elements"]
        if res.get("model"):
            models.append(res["model"])
    # one call per A feature, in parallel — each element inherits THAT feature's stretch level
    if add_feats:
        lock = threading.Lock()
        out: list[tuple[int, list[dict]]] = []

        def one(pair):
            i, f = pair
            r = claude_bridge.decompose_claim(f["name"], model=model)
            if "error" in r:
                return                        # a failed A split just leaves it whole
            els = [{**e, "kind": "A", "sl": int(f.get("sl", 5)),
                    "weight": int(e.get("weight", 1))} for e in r["elements"]]
            with lock:
                out.append((i, els))
                if r.get("model"):
                    models.append(r["model"])

        with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
            list(ex.map(one, list(enumerate(add_feats))))
        done = {i for i, _ in out}
        for _, els in sorted(out):
            elements += els
        # never silently drop an A feature whose split failed — keep it whole
        elements += [f for i, f in enumerate(add_feats) if i not in done]
    if not elements:
        raise HTTPException(400, "decomposition produced no elements")
    return {"ok": True, "elements": elements, "model": models[0] if models else None,
            "source": body.source, "source_chars": len(text),
            "mandatory": len([e for e in elements if (e.get("kind") or "M") != "A"]),
            "additional": len([e for e in elements if (e.get("kind") or "M") == "A"])}


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
    # replacing the benchmark: drop previous uploaded files from disk. Read them without
    # clearing — set_benchmark replaces the row and carries the written features across.
    for f in db.benchmark_files(tab_id):
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    db.set_benchmark(tab_id, source="pdf" if "pdf" in kinds else "images", files=saved)
    chash = _files_hash([f["path"] for f in saved])
    if chash:
        db.update_benchmark(tab_id, content_hash=chash)
    # If this exact file-set was already transcribed in another tab, don't re-OCR —
    # offer the stored text for reuse ("ask each time"). Transcription only starts
    # once the user declines, via POST /benchmark/transcribe.
    src = db.find_reusable_by_hash(chash, exclude_tab_id=tab_id) if chash else None
    if src:
        return {"ok": True, "benchmark": _benchmark_view(tab_id),
                "reuse": {"tab_name": src.get("tab_name"),
                          "text_model": src.get("text_model"),
                          "chars": len(src.get("description") or "")}}
    bg.add_task(_extract_benchmark_files, tab_id, _read_model(reading_model))
    return {"ok": True, "benchmark": _benchmark_view(tab_id)}


@app.post("/api/tabs/{tab_id}/benchmark/reuse")
def benchmark_reuse(tab_id: int):
    """Accept the offered cross-tab transcription for the just-uploaded benchmark
    file-set instead of re-OCR'ing it. Copies the stored text + its model."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm or not bm.get("content_hash"):
        raise HTTPException(400, "no benchmark upload to reuse for")
    src = db.find_reusable_by_hash(bm["content_hash"], exclude_tab_id=tab_id)
    if not src:
        raise HTTPException(404, "no reusable transcription found")
    db.update_benchmark(tab_id, status="ready", progress=None, error=None,
                        text=src.get("description") or src.get("text"),
                        title=bm.get("title") or src.get("title"),
                        text_model=src.get("text_model"))
    _mirror_benchmark_if_auto(tab_id)
    return {"ok": True, "benchmark": _benchmark_view(tab_id)}


@app.post("/api/tabs/{tab_id}/benchmark/transcribe")
def benchmark_transcribe(tab_id: int, bg: BackgroundTasks,
                         body: schemas.BenchmarkTranscribe | None = None):
    """Decline the reuse offer and OCR the uploaded benchmark pages from scratch."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm or not (bm.get("files") or []):
        raise HTTPException(400, "no uploaded benchmark files to transcribe")
    db.update_benchmark(tab_id, status="pending", error=None, progress=None)
    model = _read_model(body.reading_model) if body else None
    bg.add_task(_extract_benchmark_files, tab_id, model)
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


@app.post("/api/tabs/{tab_id}/benchmark/figures")
def benchmark_figures(tab_id: int, bg: BackgroundTasks, force: bool = False):
    """Caption the benchmark's drawing sheets (number-based benchmark) on demand."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm:
        raise HTTPException(404, "no benchmark set")
    if bm.get("text"):
        raise HTTPException(400, "this benchmark's drawings are already in its page transcription")
    bg.add_task(_process_benchmark_figures, tab_id, None, force)
    return {"ok": True}


@app.get("/api/tabs/{tab_id}/benchmark/figure/{idx}")
def benchmark_figure_image(tab_id: int, idx: int):
    """Serve one stored benchmark drawing-sheet image (1-based)."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    figs = json.loads(bm["figures"]) if bm and bm.get("figures") else []
    if idx < 1 or idx > len(figs):
        raise HTTPException(404, "figure not found")
    path = figs[idx - 1].get("path")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "figure image missing")
    return FileResponse(path)


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
# Bulk digest passes (➕ additional read, ♻️ re-check): one call per this many candidates.
# Sized like the cross-tab scan. Batching is what lets those tools span ALL documents —
# a single call over hundreds of digests would blow the prompt budget.
BULK_DIGEST_BATCH = int(os.environ.get("PB_BULK_DIGEST_BATCH", "25"))


def _fetch_into_db(doc_id: int) -> None:
    doc = db.get_document(doc_id)
    if not doc:
        return
    res = fetcher.fetch_document(doc["number"])
    if "error" in res:
        db.update_document(doc_id, status="error", error=res["error"])
    else:
        # figure_urls isn't a column — stash the drawing URLs as a pending figures
        # skeleton so _process_figures can download+caption them (no re-scrape).
        urls = res.pop("figure_urls", None) or []
        figs = json.dumps([{"n": i, "url": u} for i, u in enumerate(urls, 1)]) if urls else None
        db.update_document(doc_id, status="fetched", error=None,
                           fetched_at=db._now(), figures=figs, **res)


def _digest_doc(doc_id: int, model: str | None = None) -> None:
    """Cheap-model pass over the candidate's FULL text → stored digest, so the
    chat is description-aware for every candidate from the get-go.

    A FAILED digest is RECORDED, never dropped: every digest-based tool (➕ additional
    read, ♻️ re-check, 🧩 combi) silently skips a document without one, so a swallowed
    error takes the document out of scope invisibly — and the run still reports 'all'."""
    doc = db.get_document(doc_id)
    if not doc or doc["status"] != "fetched" or doc.get("digest"):
        return
    fulltext = "\n\n".join(filter(None, [doc.get("abstract"), doc.get("claims"),
                                         doc.get("description")]))
    if not fulltext:
        db.update_document(doc_id, digest_error="no primary text to digest")
        return
    res = claude_bridge.digest_document(doc["number"], doc.get("title") or "", fulltext,
                                        model=model)
    if "digest" in res:
        db.update_document(doc_id, digest=res["digest"], digest_error=None,
                           digest_model=model or claude_bridge.DIGEST_MODEL)
    else:
        db.update_document(doc_id, digest_error=(res.get("error") or "digest failed")[:300])


def _process_figures(doc_id: int, model: str | None = None, force: bool = False) -> int:
    """Download + vision-caption a candidate's drawing sheets and merge the figure
    descriptions into its stored text, so chat/deep-compare/NLM can cite figures the
    way they cite [00NN] paragraphs. Returns the number of figures captioned. Uses
    the figure URLs stashed at fetch; falls back to re-scraping for older documents."""
    doc = db.get_document(doc_id)
    if not doc or doc["status"] != "fetched":
        return 0
    meta = json.loads(doc["figures"]) if doc.get("figures") else []
    if meta and all(f.get("caption") for f in meta) and not force:
        return sum(1 for f in meta if f.get("caption"))     # already done
    urls = [f["url"] for f in meta if f.get("url")]
    # force = the user distrusts the stored result — re-scrape the URLs too, they
    # may be the very problem (stored thumbnails instead of full sheets, 2026-07-03)
    if (force or not urls) and doc.get("number"):
        urls = fetcher.figure_urls(doc["number"]) or urls
    if not urls:
        db.update_document(doc_id, figures_n=0)
        return 0
    figs = figures.download(urls, os.path.join(FIGURES, str(doc_id)))
    figures.caption_all(figs, model, context=figures.doc_context(doc))
    merged = figures.merge_into_description(doc.get("description"), figs)
    n = sum(1 for f in figs if f.get("caption"))
    # n==0 while sheets EXIST = the vision runs failed (rate limit/outage), not
    # "this document has no drawings". figures_n=0 would falsely read as the
    # latter everywhere (_figures_unread) — keep it None so the ⚠ DRAWINGS-NOT-
    # READ deficiency stays visible and a re-run heals it. Seen live 2026-07-03:
    # a 23-doc sweep hit failures from doc 16 on and stamped 8 docs 'no drawings'.
    db.update_document(doc_id, figures=json.dumps(figs, ensure_ascii=False),
                       figures_n=(n if (n or not figs) else None), description=merged)
    return n


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
    # Caption drawings BEFORE the digest so the digest (and every later read) is
    # figure-aware. Captioning is vision-per-sheet and slow, so it's gated by env.
    if AUTO_FIGURES:
        with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
            list(ex.map(lambda i: _process_figures(i, model), doc_ids))
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
    # Cross-tab reuse: any inserted number already fetched+digested in another tab is
    # held back (left pending) and surfaced so the UI can ASK before re-doing the work.
    reusable, to_fetch = [], []
    for doc_id in res["inserted"]:
        d = db.get_document(doc_id)
        src = db.find_reusable_by_number(d["number"], exclude_tab_id=tab_id) if d else None
        if src:
            reusable.append({"doc_id": doc_id, "number": d["number"],
                             "tab_name": src.get("tab_name"),
                             "digest_model": src.get("digest_model"),
                             "text_model": src.get("text_model"),
                             "has_digest": bool(src.get("digest"))})
        else:
            to_fetch.append(doc_id)
    if to_fetch:
        bg.add_task(_process_documents, to_fetch, _read_model(body.reading_model))
    res["reusable"] = reusable
    return res


@app.post("/api/tabs/{tab_id}/documents/{doc_id}/reuse")
def document_reuse(tab_id: int, doc_id: int, bg: BackgroundTasks):
    """Accept a cross-tab copy for a held-back document: copy its body + digest from
    the richest existing copy in another tab instead of re-fetching/re-OCR'ing it."""
    doc = db.get_document(doc_id)
    if not doc or doc["tab_id"] != tab_id:
        raise HTTPException(404, "document not found")
    src = db.find_reusable_by_number(doc["number"], exclude_tab_id=tab_id)
    if not src:
        raise HTTPException(404, "no reusable copy found")
    db.copy_into_document(doc_id, src)
    # mirror into the connected notebook like a freshly-fetched candidate would be
    bg.add_task(_auto_export_docs, tab_id, [doc_id])
    return {"ok": True, "reused_from": src.get("tab_name")}


@app.get("/api/tabs/{tab_id}/documents")
def documents_list(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_documents(tab_id)
    for d in docs:
        d["links"] = _doc_links(d)
    return {"documents": docs}


@app.get("/api/tabs/{tab_id}/feature-xref")
def feature_xref(tab_id: int, name: str):
    """Cross-tab feature lookup: every document in OTHER tabs that discloses (YES or
    PARTIAL) a feature with this name. Powers the 'In other tabs' section of the
    feature modal, so a feature assessed once is visible everywhere it was checked."""
    _tab_or_404(tab_id)
    rows = db.documents_disclosing_feature(name, exclude_tab_id=tab_id)
    return {"name": name, "documents": rows}


# ---------- knowledge graph (cross-tab feature taxonomy) + global search ----------

@app.get("/api/kg")
def kg_tree():
    """The whole cross-tab knowledge graph (field›block›function›option forest with
    per-node tab/doc references and ⇄ related cross-links)."""
    return db.kg_tree()


@app.get("/api/search")
def global_search(q: str, limit: int = 40):
    """Global cross-tab search over graph nodes, documents and chat messages."""
    return db.kg_search(q, limit=min(max(limit, 1), 100))


@app.get("/api/tabs/{tab_id}/refs")
def resolve_refs(tab_id: int, text: str):
    """Patent numbers named in `text` (e.g. a benchmark feature 'overlapping section
    like in EP4338618') that are NOT in this tab but ARE stored in another — resolved
    to that tab's document + its digest/verdict, so it can be pulled in as context."""
    _tab_or_404(tab_id)
    here = {d["number"] for d in db.list_documents(tab_id)}
    out = []
    seen = set()
    for num in patents.extract_candidates(text or ""):
        if num in here or num in seen:
            continue
        seen.add(num)
        ref = db.cross_tab_reference(num, exclude_tab_id=tab_id)
        if ref:
            out.append(ref)
    return {"refs": out}


@app.post("/api/kg/classify")
def kg_classify(body: schemas.KgClassifyRequest):
    """LLM draft classification for a feature + existing nodes it could link to.
    Powers the 'Looks like … [Link] [New]' suggestion when a feature is added."""
    cls = kgraph.classify_feature(body.feature_name, model=body.model)
    if "error" in cls:
        return {"error": cls["error"], "candidates": db.kg_candidate_nodes(body.feature_name)}
    return {"classification": cls, "candidates": db.kg_candidate_nodes(body.feature_name)}


@app.post("/api/kg/attach")
def kg_attach(body: schemas.KgAttachRequest):
    """Confirm a classification: link the feature to an existing node, or create the
    field›block›function›option path. Returns the resulting node breadcrumb."""
    if body.node_id and db.kg_path(body.node_id):
        db.kg_attach_feature(body.node_id, body.feature_name, tab_id=body.tab_id,
                             doc_id=body.doc_id, status=body.status, note=body.note)
        return {"node_id": body.node_id, "path": db.kg_path(body.node_id)}
    if not (body.field or body.option):
        raise HTTPException(400, "need node_id or at least a field/option to create")
    cls = {"field": body.field or "", "block": body.block or "",
           "function": body.function or "", "option": body.option or "",
           "related_blocks": body.related_blocks, "matched_option_id": None}
    res = kgraph.apply_classification(cls, body.feature_name, tab_id=body.tab_id,
                                      doc_id=body.doc_id, status=body.status, note=body.note)
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


@app.patch("/api/kg/node/{node_id}")
def kg_node_patch(node_id: int, body: schemas.KgNodePatch):
    if not db.kg_path(node_id):
        raise HTTPException(404, "node not found")
    if body.name is not None:
        db.kg_rename_node(node_id, body.name)
    if body.reparent:
        db.kg_reparent_node(node_id, body.parent_id)
    return {"path": db.kg_path(node_id)}


@app.delete("/api/kg/node/{node_id}")
def kg_node_delete(node_id: int):
    return {"deleted": db.kg_delete_node(node_id)}


@app.post("/api/kg/edge")
def kg_edge_add(body: schemas.KgEdgeRequest):
    if not (db.kg_path(body.src_id) and db.kg_path(body.dst_id)):
        raise HTTPException(404, "node not found")
    db.kg_add_edge(body.src_id, body.dst_id, body.rel)
    return {"ok": True}


@app.delete("/api/kg/edge/{edge_id}")
def kg_edge_delete(edge_id: int):
    return {"deleted": db.kg_delete_edge(edge_id)}


def _kg_feature_sources(tab_id: int | None) -> list[dict]:
    """Every feature occurrence to classify: benchmark target features + each
    document's per-feature verdicts, across the given tab (or all tabs)."""
    tab_ids = [tab_id] if tab_id else [t["id"] for t in db.list_tabs()]
    out = []
    for tid in tab_ids:
        bm = db.get_benchmark(tid)
        for f in (bm or {}).get("features", []) or []:
            if f.get("name"):
                out.append({"tab_id": tid, "doc_id": None, "name": f["name"],
                            "status": "benchmark", "note": ""})
        for d in db.list_documents(tid, full=False):
            for arr in (d.get("feature_scores") or [], d.get("additional_scores") or []):
                for e in (arr or []):
                    nm = (e or {}).get("name")
                    st = ((e or {}).get("status") or "").lower()
                    if nm and st in ("yes", "partial", "present", "stretch"):
                        out.append({"tab_id": tid, "doc_id": d["id"], "name": nm,
                                    "status": st, "note": e.get("note") or e.get("evidence") or ""})
    return out


@app.post("/api/kg/rebuild")
def kg_rebuild(body: schemas.KgRebuildRequest):
    """Batch-classify every feature (benchmark targets + document verdicts) across a
    tab, or all tabs, into the graph. Deduplicates identical feature names within the
    run so the LLM is called once per distinct feature."""
    ok, why = claude_bridge.available()
    if not ok:
        raise HTTPException(503, why)
    if body.clear:
        db.kg_clear()
    sources = _kg_feature_sources(body.tab_id)
    cache: dict[str, dict] = {}
    attached = 0
    failed = 0
    for s in sources:
        key = s["name"].strip().lower()
        cls = cache.get(key)
        if cls is None:
            cls = kgraph.classify_feature(s["name"], model=body.model)
            cache[key] = cls
        if "error" in cls:
            failed += 1
            continue
        res = kgraph.apply_classification(
            cls, s["name"], tab_id=s["tab_id"], doc_id=s["doc_id"],
            status=s["status"], note=s["note"])
        if "error" not in res:
            attached += 1
    return {"attached": attached, "failed": failed,
            "distinct_features": len(cache), "nodes": db.kg_tree()["node_count"]}


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


@app.post("/api/tabs/{tab_id}/documents/{doc_id}/figures")
def document_figures(tab_id: int, doc_id: int, bg: BackgroundTasks, force: bool = False,
                     reading_model: str | None = None):
    """Download + caption this candidate's drawing sheets and merge them into its
    text (so figures become groundable). Runs in the background; poll the doc list
    for figures_n. `force=1` re-reads even if already captioned."""
    doc = db.get_document(doc_id)
    if not doc or doc["tab_id"] != tab_id:
        raise HTTPException(404, "document not found")
    if doc["status"] != "fetched":
        raise HTTPException(400, "fetch the document first")
    bg.add_task(_process_figures, doc_id, _read_model(reading_model), force)
    return {"ok": True}


@app.get("/api/tabs/{tab_id}/documents/{doc_id}/figure/{idx}")
def document_figure_image(tab_id: int, doc_id: int, idx: int):
    """Serve one stored drawing sheet image (1-based index) for the viewer."""
    doc = db.get_document(doc_id)
    if not doc or doc["tab_id"] != tab_id:
        raise HTTPException(404, "document not found")
    figs = json.loads(doc["figures"]) if doc.get("figures") else []
    if idx < 1 or idx > len(figs):
        raise HTTPException(404, "figure not found")
    path = figs[idx - 1].get("path")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "figure image missing")
    return FileResponse(path)


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


def _source_number(title: str) -> str:
    """The patent number a notebook source title starts with — titles are written as
    '<NUMBER> — <name>' (or '🎯 BENCHMARK — …'). '' when none is present."""
    m = re.match(r"^\s*([A-Z]{2}\d+[A-Z]?\d*)", title or "")
    return m.group(1) if m else ""


@app.post("/api/tabs/{tab_id}/notebook/resync")
def notebook_resync(tab_id: int, body: schemas.NotebookResync):
    """Reconcile the app's record of which candidates are in NotebookLM with NLM's
    ACTUAL sources. Scans the tab's notebooks (or an explicit/whole-account set), maps
    each source title to a candidate (kind-code-insensitive), and updates
    documents.nlm_source_notebook / nlm_source_id. Surfaces candidates that were in NLM
    but untracked, and DUPLICATES (one candidate in >1 notebook) so they can be deleted
    to free the 50-source cap. Read-only against NLM (no AI quota)."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    if body.scan_all:
        nb_ids = [n["id"] for n in (nlm_bridge.list_notebooks().get("notebooks") or [])]
    elif body.notebook_ids:
        nb_ids = list(dict.fromkeys(body.notebook_ids))
    else:
        nb_ids = db.tab_notebook_ids(tab_id)
    account = {n["id"]: n["title"]
               for n in (nlm_bridge.list_notebooks().get("notebooks") or [])}
    titles = dict(account)
    cands = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    by_key = {}
    for d in cands:                                   # first candidate per key wins
        by_key.setdefault(_shortlist_key(d["number"]), d)
    # key → list of {notebook_id, source_id} where a matching source physically exists
    locations: dict[str, list[dict]] = {}
    scan_errors, scanned_ok = [], set()
    for nb in nb_ids:
        res = nlm_bridge.list_sources(nb, force=True)
        if res.get("error"):
            scan_errors.append(f"{titles.get(nb, nb)}: {str(res['error'])[:80]}")
            continue
        scanned_ok.add(nb)
        for s in res.get("sources") or []:
            key = _shortlist_key(_source_number(s.get("title") or ""))
            if key and key in by_key:
                locations.setdefault(key, []).append({"notebook_id": nb, "source_id": s.get("id")})
    # update each candidate's tracked home (keep an existing valid home, else first found)
    retracked = 0
    for key, locs in locations.items():
        d = by_key[key]
        homes = [l["notebook_id"] for l in locs]
        cur = d.get("nlm_source_notebook")
        home = cur if cur in homes else locs[0]["notebook_id"]
        sid = next((l["source_id"] for l in locs if l["notebook_id"] == home), None)
        if d.get("nlm_source_notebook") != home or d.get("nlm_source_id") != sid:
            db.update_document(d["id"], nlm_source_notebook=home, nlm_source_id=sid)
            retracked += 1
    # Clear stale tracking ONLY when it's safe: the tracked notebook is gone from the
    # account entirely (deleted), or it was scanned cleanly yet no longer holds the
    # candidate. A transient scan error never clears (avoids false "not in NLM").
    cleared = 0
    for d in cands:
        nb = d.get("nlm_source_notebook")
        if not nb:
            continue
        gone = nb not in account
        scanned_clean = nb in scanned_ok and _shortlist_key(d["number"]) not in locations
        if gone or scanned_clean:
            db.update_document(d["id"], nlm_source_notebook=None, nlm_source_id=None)
            cleared += 1
    duplicates = [
        {"number": by_key[k]["number"],
         "locations": [{"notebook_id": l["notebook_id"],
                        "notebook_title": titles.get(l["notebook_id"], l["notebook_id"]),
                        "source_id": l["source_id"]} for l in locs]}
        for k, locs in locations.items() if len(locs) > 1]
    in_nlm = sum(1 for d in db.list_documents(tab_id)
                 if d["status"] == "fetched" and d.get("nlm_source_notebook"))
    dup_copies = sum(len(d["locations"]) - 1 for d in duplicates)
    summary = (f"♻️ Resynced with NotebookLM across {len(nb_ids)} notebook(s): "
               f"{in_nlm} of {len(cands)} candidate(s) are in NLM "
               f"({retracked} re-tracked, {cleared} cleared). "
               + (f"{len(duplicates)} candidate(s) are DUPLICATED across notebooks "
                  f"({dup_copies} extra copy/copies — delete them to free the 50-source cap). "
                  if duplicates else "No duplicates found. ")
               + ("; ".join(scan_errors) if scan_errors else ""))
    db.append_message(tab_id, "s", summary)
    return {"ok": True, "scanned": len(nb_ids), "in_nlm": in_nlm, "total": len(cands),
            "retracked": retracked, "cleared": cleared, "duplicates": duplicates,
            "dup_copies": dup_copies, "errors": scan_errors}


def _notebook_free(nb_id: str) -> int:
    """Free source slots in a notebook right now (SOURCE_LIMIT − live source count)."""
    res = nlm_bridge.list_sources(nb_id, force=True)
    if res.get("error"):
        return 0
    return max(0, nlm_bridge.SOURCE_LIMIT - len(res.get("sources") or []))


@app.post("/api/tabs/{tab_id}/notebook/distribute")
def notebook_distribute(tab_id: int, body: schemas.NotebookDistribute):
    """Auto-split candidates across several notebooks' FREE space: fill the first
    notebook to its 50-source cap, spill the rest into the next, and so on — using
    notebooks that already have room instead of creating new ones (which fails at the
    ~100-notebook account cap). Default targets = the tab's own notebooks with space
    (most-free first); pass notebook_ids for a manual ordered split."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available()
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    cands = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    if body.doc_ids:
        want = set(body.doc_ids)
        docs = [d for d in cands if d["id"] in want]
    else:
        docs = [d for d in cands if not d.get("nlm_source_notebook")]   # the not-in-NLM set
    docs.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)        # best first if space runs out
    titles = {n["id"]: n["title"]
              for n in (nlm_bridge.list_notebooks().get("notebooks") or [])}
    if body.notebook_ids:
        nb_ids = [n for n in dict.fromkeys(body.notebook_ids) if n in titles]
    else:                                          # tab's notebooks that have room, most-free first
        nb_ids = sorted((n for n in db.tab_notebook_ids(tab_id)),
                        key=_notebook_free, reverse=True)
        nb_ids = [n for n in nb_ids if _notebook_free(n) > 0]
    if not nb_ids:
        raise HTTPException(400, "no notebook with free space — 🗑 delete duplicate sources "
                            "(♻️ Resync finds them) or delete a notebook, then retry")
    remaining = list(docs)
    placements, errors = [], []
    for nb in nb_ids:
        added_here = 0
        if body.include_benchmark and not placements:    # benchmark into the first target only
            _add_benchmark_to_notebook(tab_id, nb)
        while remaining:
            d = remaining[0]
            res = _add_doc_to_notebook(d["id"], nb)
            if res.get("full"):
                break                                    # this notebook is full → next one
            remaining.pop(0)                             # consumed (added, or skipped as already-in)
            if res.get("ok"):
                added_here += 1
            elif res.get("error"):
                errors.append(f"{d['number']}: {res['error']}")
        if added_here:
            placements.append({"notebook_id": nb, "notebook_title": titles.get(nb, nb),
                               "added": added_here})
    placed = sum(p["added"] for p in placements)
    db.append_message(
        tab_id, "s",
        f"📚 Distributed {placed} candidate(s) across {len(placements)} notebook(s): "
        + (", ".join(f"{p['added']}→«{p['notebook_title']}»" for p in placements) or "none")
        + (f"; {len(remaining)} still not placed (all notebooks full — delete duplicates to free space)"
           if remaining else "")
        + (f"; errors: {'; '.join(errors[:3])}" if errors else "") + ".")
    return {"ok": True, "placed": placed, "placements": placements,
            "remaining": len(remaining), "errors": errors[:5]}


@app.post("/api/tabs/{tab_id}/notebook/source-delete")
def notebook_source_delete(tab_id: int, body: schemas.NotebookSourceDelete):
    """Permanently delete chosen sources from a notebook (dedup / free space), then
    clear the app's tracking for any candidate whose tracked source was deleted."""
    _tab_or_404(tab_id)
    res = nlm_bridge.delete_source(body.source_ids, notebook_id=body.notebook_id)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    deleted = set(body.source_ids)
    cleared = 0
    for d in db.list_documents(tab_id, full=True):
        if d.get("nlm_source_id") in deleted:
            db.update_document(d["id"], nlm_source_notebook=None, nlm_source_id=None)
            cleared += 1
    return {"ok": True, "deleted": res.get("deleted", 0), "cleared": cleared}


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
PIPELINE_INGEST_TIMEOUT = float(os.environ.get("PB_INGEST_TIMEOUT", "300"))  # max wait for NLM to process sources
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
                # DELETE the tab's rollover notebooks FIRST, then create the consolidated one.
                # The funnel is net-negative on notebook count (−many +1), so freeing the slots
                # up front means the create can't fail at the ~100-notebook account cap (which is
                # exactly when you most need to consolidate). Safe to delete before copying: the
                # copy below re-uploads each finalist from our STORED full text, not from the old
                # NLM source. Refs/caches are cleared so a now-dead notebook is never re-queried.
                cleaned = 0
                for old in db.tab_notebook_ids(tab_id):
                    _pipeline_set(tab_id, status_text="🗑 freeing slots: removing rollover notebooks…")
                    if nlm_bridge.delete_notebook(old).get("ok"):
                        cleaned += 1
                    db.clear_nlm_refs(tab_id, old)
                    db.nlm_cache_clear(old)
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
                                  f"«{created['title']}» and connected the tab"
                                  + (f"; 🗑 deleted {cleaned} other notebook(s) to free slots" if cleaned else "")
                                  + ".")
            else:                                       # resume: the notebook already exists, reuse it
                db.set_notebook_config(tab_id, nb, st.get("notebook_title") or nb, [], auto_add=True)
            if st.get("consolidate_only"):              # STOP here — caller wants the docs in NLM only
                db.append_message(tab_id, "s", "📥 The documents are in NotebookLM — nothing was "
                                  "queried. Click 📓 NLM shortlist or ⚖️ Debate when you're ready.")
                _pipeline_set(tab_id, step="done", status_text="✅ documents in NotebookLM (no query)",
                              error=None)
                return
            st = _pipeline_set(tab_id, step="shortlist")
        if st.get("step") == "shortlist":
            # WAIT for NotebookLM to finish ingesting the freshly-copied sources before the ONE
            # query fires — adding a source is instant but querying a half-processed notebook
            # returns a truncated 'full text isn't present' answer and wastes the query. The probe
            # costs no chat quota.
            nb = st.get("notebook_id")
            _pipeline_set(tab_id, status_text="⏳ waiting for NotebookLM to ingest the sources…")
            rd = nlm_bridge.wait_sources_ready(nb, timeout=PIPELINE_INGEST_TIMEOUT)
            if not rd.get("ready"):
                db.append_message(tab_id, "s",
                    f"⚠️ NotebookLM was still ingesting ({rd.get('processed', 0)}/{rd.get('total', 0)} "
                    f"sources ready) after {int(PIPELINE_INGEST_TIMEOUT)}s — asking anyway. If the "
                    "answer looks truncated, re-run 📓 shortlist in a minute (the sources will be ready).")
            _pipeline_set(tab_id, status_text="📓 picking best + second-best…")
            sl = nlm_shortlist(tab_id, schemas.NlmShortlistRequest(notebook_id=nb))
            nlm_top_id = (sl.get("shortlist_ids") or [None])[0]
            st = _pipeline_set(tab_id, step="debate", nlm_top_id=nlm_top_id)
        if st.get("step") == "debate":
            # TWO INDEPENDENT ROOTS, compared. NLM judged the funnel set with NO knowledge that
            # Claude preselected it; Claude ranked it without NLM's input. Debate ONLY when their
            # #1 picks diverge — agreement needs no opus reconciliation.
            claude_top, nlm_top = st.get("claude_top_id"), st.get("nlm_top_id")
            def _num(i):
                d = db.get_document(i) if i else None
                return d["number"] if d else None
            if nlm_top and claude_top and nlm_top == claude_top:
                _pipeline_set(tab_id, status_text="✅ both roots agree — no debate needed")
                db.append_message(tab_id, "s",
                    f"✅ Independent agreement: Claude (full-text ranking) and NotebookLM (grounded "
                    f"read of the same set, blind to Claude's scores) both pick «{_num(nlm_top)}» as "
                    "the best match. No divergence to debate.")
            else:
                _pipeline_set(tab_id, status_text="⚖️ roots diverge — Claude ↔ NotebookLM debating…")
                ids = [i for i in (nlm_top, claude_top) if i]
                db.append_message(tab_id, "s",
                    "⚖️ The two roots diverge — Claude's best is "
                    f"«{_num(claude_top) or '—'}», NotebookLM's is «{_num(nlm_top) or '—'}». "
                    "Reconciling on opus.")
                nlm_challenge(tab_id, schemas.NlmChallengeRequest(doc_ids=ids or None))
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
    # The funnel: explicit finalists if given, else AUTO-pick Claude's top_n by score. The
    # picks go into ONE notebook so NotebookLM can give a single, independent second opinion.
    ids = body.doc_ids or db.top_scored_documents(tab_id, body.top_n)
    if not ids:
        raise HTTPException(400, "no scored candidates to funnel — run 🏆 deep-compare first "
                                 "so Claude has ranked them")
    if not body.title.strip():
        raise HTTPException(400, "name the consolidated notebook")
    # Claude's #1 = highest-scored among the chosen finalists (for the divergence gate later).
    score_of = {d["id"]: (d.get("score") or 0) for d in db.list_documents(tab_id, full=True)}
    claude_top_id = max(ids, key=lambda i: (score_of.get(i, 0), -i))
    _pipeline_set(tab_id, step="consolidate", title=body.title.strip(), doc_ids=ids,
                  claude_top_id=claude_top_id, include_benchmark=bool(body.include_benchmark),
                  consolidate_only=bool(body.consolidate_only),
                  error=None, notebook_id=None, notebook_title=None, status_text="queued…")
    threading.Thread(target=_run_pipeline, args=(tab_id,), daemon=True).start()
    return {"started": True, "running": True, "funnel_n": len(ids)}


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
                      source_ids: list[str] | None = None, force: bool = False,
                      accept=None, retries: int = 0) -> dict:
    """nlm_bridge.query with a PERSISTENT answer cache keyed on
    (notebook, source-restriction, question, source-set signature). Identical queries
    return the stored answer for free — no NotebookLM call, no quota — and survive
    rebuilds (the cache lives in the /data DB). Stale automatically when sources change.
    Only successful answers are cached. {answer, cached?} | {error}.

    `accept(answer) -> bool` rejects a *substantively incomplete* reply (e.g. NotebookLM
    cut off in its 'thinking' preamble before producing the shortlist). A rejected answer
    is NOT cached (so it can't poison future runs), is retried up to `retries` times with a
    forced fresh query, and — if still rejected — is returned tagged {incomplete: True} so
    the caller can warn instead of silently scraping a non-answer."""
    sig = _notebook_signature(notebook_id)
    raw = "|".join([notebook_id, ",".join(source_ids or []), sig, question])
    key = hashlib.sha256(raw.encode()).hexdigest()
    if not force:
        hit = db.nlm_cache_get(key)
        if hit is not None and (accept is None or accept(hit)):
            return {"answer": hit, "cached": True}
    res = {}
    for attempt in range(retries + 1):
        res = nlm_bridge.query(notebook_id, question, source_ids=source_ids)
        ans = res.get("answer")
        if not ans:
            return res
        if accept is None or accept(ans):
            db.nlm_cache_put(key, notebook_id, question, ans)
            return res
        # incomplete: do NOT cache; retry a fresh query if budget remains
    res["incomplete"] = True
    return res


def _query_tab_series(tab_id: int, question: str, accept=None, retries: int = 0) -> list[dict]:
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
        res = _nlm_query_cached(nb, question, source_ids=sids, accept=accept, retries=retries)
        entry = {"notebook_id": nb, "title": titles.get(nb, nb)}
        entry["error" if "error" in res else "answer"] = res.get("error") or res["answer"]
        if res.get("incomplete"):
            entry["incomplete"] = True
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


# ---------- ⚖️ problem-solution approach ----------

# Two GLOBAL documents drive every ⚖️ run, both kept in the data volume forever
# (they are doctrine, not tab data): `method` = the steps to follow, `format` =
# how the answer must be structured. Same storage/upload/OCR machinery for both.
PSA_KINDS = ("method", "format")


def _psa_doc(kind: str) -> dict | None:
    """The uploaded PSA document of this kind: {name, chars, uploaded_at, text}
    or None."""
    meta_path = os.path.join(PSA_DIR, f"{kind}.json")
    txt_path = os.path.join(PSA_DIR, f"{kind}.txt")
    if not (os.path.exists(meta_path) and os.path.exists(txt_path)):
        return None
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(txt_path, encoding="utf-8") as fh:
        meta["text"] = fh.read()
    return meta


def _psa_pending(kind: str) -> dict | None:
    p = os.path.join(PSA_DIR, f"{kind}-pending.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _write_psa_pending(kind: str, d: dict | None) -> None:
    p = os.path.join(PSA_DIR, f"{kind}-pending.json")
    if d is None:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        return
    os.makedirs(PSA_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)


def _kind_or_404(kind: str) -> None:
    if kind not in PSA_KINDS:
        raise HTTPException(404, f"unknown PSA document kind '{kind}' "
                                 f"(expected one of {', '.join(PSA_KINDS)})")


def _transcribe_psa_doc(kind: str, name: str) -> None:
    """Background: a SCANNED (image-only) PSA PDF → page PNGs (pdftoppm) → vision
    transcription per page (same engine as benchmark photo pages) → {kind}.txt.
    Progress/errors live in {kind}-pending.json so the UI can poll."""
    pdf = os.path.join(PSA_DIR, f"{kind}.pdf")
    pages_dir = os.path.join(PSA_DIR, f"pages-{kind}")
    shutil.rmtree(pages_dir, ignore_errors=True)
    os.makedirs(pages_dir, exist_ok=True)
    try:
        subprocess.run(["pdftoppm", "-r", "150", "-png", pdf,
                        os.path.join(pages_dir, "pg")],
                       check=True, timeout=180, capture_output=True)
    except (subprocess.SubprocessError, OSError) as e:
        _write_psa_pending({"name": name, "error": f"pdftoppm failed: {e}"})
        return
    pages = sorted(os.listdir(pages_dir))
    if not pages:
        _write_psa_pending({"name": name, "error": "the PDF produced no page images"})
        return
    _write_psa_pending(kind, {"name": name, "total": len(pages), "done": 0})
    texts: list[str] = [""] * len(pages)
    done = 0
    def one(ip):
        i, p = ip
        r = extract.text_from_image(os.path.join(pages_dir, p))
        return i, (r.get("text") or "")
    with ThreadPoolExecutor(max_workers=TRANSCRIBE_WORKERS) as ex:
        for i, t in ex.map(one, enumerate(pages)):
            texts[i] = t
            done += 1
            _write_psa_pending(kind, {"name": name, "total": len(pages), "done": done})
    text = "\n\n".join(f"— page {i + 1} —\n{t.strip()}"
                       for i, t in enumerate(texts) if t.strip()).strip()
    if len(text) < 50:
        _write_psa_pending(kind, {"name": name,
                                  "error": "vision transcription yielded almost no text"})
        return
    with open(os.path.join(PSA_DIR, f"{kind}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    meta = {"name": name, "chars": len(text), "uploaded_at": int(time.time())}
    with open(os.path.join(PSA_DIR, f"{kind}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    _write_psa_pending(kind, None)


@app.get("/api/psa/{kind}")
def psa_doc_status(kind: str):
    _kind_or_404(kind)
    pend = _psa_pending(kind)
    if pend:
        if pend.get("error"):
            return {"ok": False, "name": pend.get("name"), "error": pend["error"]}
        return {"ok": False, "pending": True, "name": pend.get("name"),
                "progress": f"{pend.get('done', 0)}/{pend.get('total') or '?'}"}
    m = _psa_doc(kind)
    if not m:
        return {"ok": False}
    return {"ok": True, "name": m["name"], "chars": m["chars"],
            "uploaded_at": m["uploaded_at"]}


@app.post("/api/psa/{kind}")
async def psa_doc_upload(kind: str, bg: BackgroundTasks, file: UploadFile = File(...)):
    """Upload/replace a global ⚖️ document (PDF/TXT/MD): `method` = the steps,
    followed VERBATIM step by step; `format` = the answer's structure, applied in
    combination with the steps. A scanned (image-only) PDF is vision-OCR'd page
    by page in the background."""
    _kind_or_404(kind)
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "too large (25 MB max)")
    name = os.path.basename(file.filename or kind)
    ext = os.path.splitext(name)[1].lower()
    os.makedirs(PSA_DIR, exist_ok=True)
    if ext == ".pdf":
        raw_path = os.path.join(PSA_DIR, f"{kind}.pdf")
        with open(raw_path, "wb") as fh:
            fh.write(data)
        res = extract.text_from_pdf(raw_path)
        if "error" in res or len((res.get("text") or "").strip()) < 50:
            # scanned image-only PDF — no text layer. Same answer as the benchmark
            # photo path: render pages and vision-transcribe them, in the background.
            for stale in (f"{kind}.txt", f"{kind}.json"):
                try:
                    os.remove(os.path.join(PSA_DIR, stale))
                except FileNotFoundError:
                    pass
            _write_psa_pending(kind, {"name": name, "total": 0, "done": 0})
            bg.add_task(_transcribe_psa_doc, kind, name)
            return {"ok": True, "pending": True, "name": name}
        text = res["text"]
    elif ext in (".txt", ".md"):
        text = data.decode("utf-8", errors="replace")
    else:
        raise HTTPException(400, f"{name}: only PDF, TXT or MD accepted")
    text = (text or "").strip()
    if len(text) < 50:
        raise HTTPException(400, "the document yielded almost no text — is it a "
                                 "scan? Provide a text PDF / TXT / MD")
    with open(os.path.join(PSA_DIR, f"{kind}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    meta = {"name": name, "chars": len(text), "uploaded_at": int(time.time())}
    with open(os.path.join(PSA_DIR, f"{kind}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    return {"ok": True, **meta}


def _psa_invention(benchmark: dict | None,
                   body: schemas.PsaRequest) -> tuple[dict | None, str]:
    """Resolve what the ⚖️ run assesses, from the basis the user PICKED — never guessed.
    Returns (invention | None, label): None = the benchmark document (the default), so
    the caller sends it as before; otherwise {'label','text'} replaces it entirely.
    The label is what the run message shows, so the basis is always readable back."""
    if body.basis == "text":
        text = (body.basis_text or "").strip()
        if len(text) < 20:
            raise HTTPException(400, "paste the text the approach should assess into the "
                                     "chat box first — it is the claimed invention of "
                                     "this run (basis: ✍️ text)")
        return ({"label": "text supplied by the user for this run", "text": text},
                f"✍️ pasted text ({len(text)} chars)")
    if body.basis == "features":
        feats = (benchmark or {}).get("features") or []
        if not feats:
            raise HTTPException(400, "this benchmark has no target features — define "
                                     "them first, or run with basis: 🎯 benchmark")
        return ({"label": "the benchmark's target feature combination",
                 "text": _compose_feature_spec(feats)},
                f"🧩 benchmark features ({len(feats)})")
    if not benchmark or not (benchmark.get("text") or benchmark.get("claims")
                             or benchmark.get("description")):
        raise HTTPException(400, "set a benchmark (with content) first — it is "
                                 "the claimed invention the approach assesses")
    return None, f"🎯 benchmark {benchmark.get('number') or benchmark.get('title') or ''}".strip()


@app.post("/api/tabs/{tab_id}/psa")
def psa_run(tab_id: int, body: schemas.PsaRequest):
    """⚖️ Run the uploaded methodology STRICTLY step-by-step over the CLAIMED INVENTION
    (the chosen basis) + the two selected candidates as D1/D2. Appends to the tab's chat.

    The basis is EXPLICIT, never inferred: the benchmark document (default), the
    benchmark's features, or text the user pasted — which replaces the benchmark
    entirely. Whichever it is, it is named on the run message, so a past run can always
    be read back to see what it assessed."""
    _tab_or_404(tab_id)
    method = _psa_doc("method")
    if not method:
        raise HTTPException(400, "no methodology uploaded yet — upload the "
                                 "problem-solution document first (📋)")
    fmt = _psa_doc("format")   # optional: the answer's structure, if uploaded
    benchmark = db.get_benchmark(tab_id)
    invention, basis_label = _psa_invention(benchmark, body)
    if invention:
        benchmark = None       # the chosen basis IS the invention — send nothing else
    docs = []
    for did in body.doc_ids:
        d = db.get_document(did)
        if not d or d["tab_id"] != tab_id:
            raise HTTPException(404, f"document {did} not found in this tab")
        if d["status"] != "fetched":
            raise HTTPException(400, f"{d.get('number') or did} is not fetched yet")
        docs.append(d)
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    # 💬 prior findings: every exchange in EVERY tab's chat (this one included)
    # that mentions D1 or D2 — deduped when one exchange names both documents.
    discussions = None
    if body.use_discussions:
        discussions, seen_ex = [], set()
        for d in docs:
            for grp in db.cross_tab_discussions(d.get("number") or "",
                                                exclude_tab_id=None):
                fresh = []
                for ex in grp["exchanges"]:
                    key = (grp["tab_id"], ex[0]["ts"], ex[0]["text"][:80])
                    if key not in seen_ex:
                        seen_ex.add(key)
                        fresh.append(ex)
                if fresh:
                    discussions.append({**grp, "exchanges": fresh})
    nums = " + ".join(d.get("number") or "?" for d in docs)
    n_ex = sum(len(g["exchanges"]) for g in discussions) if discussions else 0
    head = ("🪄 Argumentation stretch (problem-solution approach)" if body.stretch
            else "⚖️ Problem-solution approach")
    # Name the basis on the run itself: 'what was this based on?' must be answerable
    # from the chat months later, without re-deriving it from the tab's current state.
    db.append_message(tab_id, "q", f"{head} on {nums} "
                                   f"(basis: {basis_label}, method: {method['name']}"
                                   + (f", format: {fmt['name']}" if fmt else "")
                                   + (f", 💬 {n_ex} prior exchange(s)" if n_ex else "")
                                   + ")")
    res = claude_bridge.psa(method["text"], benchmark, docs, model=model,
                            format_text=fmt["text"] if fmt else None,
                            discussions=discussions or None, stretch=body.stretch,
                            invention=invention)
    out = []
    if "error" in res:
        out.append(db.append_message(tab_id, "s", f"Claude error: {res['error']}"))
        return {"messages": out, "error": res["error"]}
    participants = [{"kind": "model", "title": model},
                    {"kind": "psa", "title": f"basis: {basis_label}"},
                    {"kind": "psa", "title": method["name"]}]
    if body.stretch:
        participants.append({"kind": "psa", "title": "🪄 argumentation stretch"})
    if fmt:
        participants.append({"kind": "psa", "title": f"format: {fmt['name']}"})
    for g in discussions or []:
        participants.append({"kind": "xtalk",
                             "title": f"{g['number']} — чат «{g['tab_name']}» "
                                      f"({len(g['exchanges'])})"})
    participants += [{"kind": "documents", "title": f"D{i} {d.get('number') or '?'}"}
                     for i, d in enumerate(docs, 1)]
    out.append(db.append_message(tab_id, "c", _verify_citations(tab_id, res["answer"]),
                                 model=model, participants=participants))
    return {"messages": out}


# ---------- chat ----------

def _resolve_xrefs(tab_id: int, question: str, benchmark: dict | None,
                   documents: list[dict] | None, limit: int = 4) -> list[dict]:
    """Cross-tab reference docs named in the question or benchmark that live in ANOTHER
    tab. Numbers already present as candidates in THIS tab are skipped (they're loaded
    directly). Returns at most `limit`, richest-first (verdict beats digest)."""
    here = {(d.get("number") or "") for d in (documents or [])}
    text = question or ""
    if benchmark:
        text += "\n" + (benchmark.get("text") or "") + "\n" + (benchmark.get("title") or "")
        for f in benchmark.get("features", []) or []:
            text += "\n" + (f.get("name") or "")
    out, seen = [], set()
    for num in patents.extract_candidates(text):
        if num in here or num in seen:
            continue
        seen.add(num)
        ref = db.cross_tab_reference(num, exclude_tab_id=tab_id)
        if ref:
            out.append(ref)
        if len(out) >= limit:
            break
    return out


def _resolve_discussions(tab_id: int, question: str, history: list[dict],
                         limit_numbers: int = 3) -> list[dict]:
    """Chat exchanges about a named patent from OTHER tabs' conversations — so
    'what did we discuss about EP4338618 in the other tabs' surfaces the actual
    exchange here. Numbers come from the current question first, then recent
    questions (sticky, like focus) so follow-ups keep the discussion loaded.
    Unlike xrefs, numbers present in THIS tab are NOT skipped — the discussion
    still lives elsewhere."""
    recent_q = " ".join(h.get("text", "") for h in (history or [])
                        if h.get("role") == "q")[-2000:]
    nums = list(dict.fromkeys(patents.extract_candidates(question or "")
                              + patents.extract_candidates(recent_q)))[:limit_numbers]
    out = []
    for num in nums:
        out.extend(db.cross_tab_discussions(num, exclude_tab_id=tab_id))
    return out


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
            # Duplicate copies of ONE patent (kindless + A1) both match a question
            # naming it — the poorer copy halves the focus budget and injects a
            # DRAWINGS-NOT-READ disclaimer right next to the figures-read one.
            # Keep the richest copy; the dropped duplicate stays in the roster.
            best: dict[str, tuple] = {}
            for d in focus:
                base = db._number_base(d.get("number") or "") or f"#{d['id']}"
                rank = (d.get("figures_n") is not None,
                        len(d.get("description") or ""))
                if base not in best or rank > best[base][0]:
                    best[base] = (rank, d)
            if len(best) < len(focus):
                chosen = {v[1]["id"] for v in best.values()}
                focus = [d for d in focus if d["id"] in chosen]
                keep = chosen
            documents = [d for d in documents if d["id"] not in keep]
    skill_blocks = []
    for name in body.skills:
        content = claude_bridge.load_skill(name)
        if content:
            skill_blocks.append({"name": name, "content": content})
            participants.append({"kind": "skill", "title": name})

    # Cross-tab references: a patent named in the question OR the benchmark that isn't
    # in this tab but IS stored elsewhere → pull its digest/verdict in as context, so
    # "overlapping section like in EP4338618" can actually see EP4338618's arguments.
    xrefs = _resolve_xrefs(tab_id, body.question, benchmark, documents)
    for x in xrefs:
        participants.append({"kind": "xref", "title": f"{x['number']} (tab «{x['tab_name']}»)"})

    # Cross-tab DISCUSSIONS: what the user and the assistants already said about a
    # named patent in OTHER tabs' chats — the full exchanges, so "write here
    # everything we had in other tabs concerning EP4338618" actually works.
    discussions = _resolve_discussions(tab_id, body.question, history)
    for d in discussions:
        participants.append({"kind": "xtalk",
                             "title": f"{d['number']} — чат «{d['tab_name']}» "
                                      f"({len(d['exchanges'])})"})

    # 🌐 All tabs: give the model every OTHER tab's already-fetched (OCR'd) documents
    # as a cross-tab roster, so it can find/combine documents that live in any tab.
    other_docs = coverage = None
    if body.all_tabs and body.use_documents:
        other_docs = db.documents_across_tabs(exclude_tab_id=tab_id)
        coverage = db.document_counts_by_tab()
        for c in coverage:
            participants.append({"kind": "tab-docs",
                                 "title": f"«{c['tab_name']}» {c['fetched']}/{c['total']} docs"})

    res = claude_bridge.chat(body.question, history=history, documents=documents,
                             sources=nlm_sources, skills=skill_blocks, model=model,
                             benchmark=benchmark, focus=focus, full=body.full,
                             answer_format=body.answer_format, xrefs=xrefs,
                             other_docs=other_docs, coverage=coverage,
                             discussions=discussions)
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
    # Deterministic cross-tab coverage confirmation — exact per-tab fetched/total
    # counts (not the model's own count), so "considered N in tab A, M in tab B" is
    # authoritative. `documents` here already excludes the focused set, so add it back.
    if coverage:
        here = len(documents or []) + len(focus or [])
        parts = [f"«{c['tab_name']}» {c['fetched']}/{c['total']}"
                 + (" (this tab)" if c["tab_id"] == tab_id else "") for c in coverage]
        cross = sum(c["fetched"] for c in coverage if c["tab_id"] != tab_id)
        out_messages.append(db.append_message(
            tab_id, "s", f"🌐 Considered documents across all tabs — "
            + "; ".join(parts) + f".  (fetched/total; {cross} cross-tab fetched docs "
            "were available to this answer.)"))
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


def _shortlist_answer_complete(answer: str) -> bool:
    """True when a NotebookLM shortlist reply actually produced the structured assessment
    the prompt demands (SHORTLIST / BEST / FEATURE MAP sections). A truncated 'thinking'
    preamble that cuts off before the shortlist (seen live 2026-06-28: a 502-char reply
    that named 3 docs in passing and stopped at 'proceed to evaluate … next', silently
    dropping the other 12 sources incl. DE202022102539) reaches a DECISION for none of the
    sources — so it carries none of these markers, and we must not treat its stray publication
    numbers as an assessment. A genuine reply (even a terse 'BEST: X' or a 'SHORTLIST: None')
    always reaches at least one."""
    a = (answer or "").upper()
    return any(m in a for m in ("SHORTLIST", "FEATURE MAP", "BEST"))


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
        qres = _nlm_query_cached(body.notebook_id, question, source_ids=None,
                                 accept=_shortlist_answer_complete, retries=1)
        e = {"notebook_id": body.notebook_id,
             "title": titles.get(body.notebook_id, body.notebook_id)}
        e["error" if "error" in qres else "answer"] = qres.get("error") or qres["answer"]
        if qres.get("incomplete"):
            e["incomplete"] = True
        series = [e]
    else:
        series = _query_tab_series(tab_id, question,
                                   accept=_shortlist_answer_complete, retries=1)
    if not series:
        raise HTTPException(400, "no notebook connected — connect/export to a notebook first")
    # A truncated / structureless reply did NOT assess its notebook's sources — don't scrape
    # its stray numbers (that's how DE202022102539 silently vanished). Warn about it instead.
    answers = [e for e in series if e.get("answer") and not e.get("incomplete")]
    incomplete = [e for e in series if e.get("incomplete")]
    if not answers:
        if incomplete:                                   # every notebook returned a non-answer
            bits = []
            for e in incomplete:
                nb = e.get("notebook_id")
                n_src = sum(1 for d in cands if d.get("nlm_source_notebook") == nb)
                bits.append(f"«{e.get('title') or nb}» ({n_src} source(s))")
            msg = db.append_message(tab_id, "s",
                "⚠️ NotebookLM returned a truncated / structureless answer for " + ", ".join(bits)
                + " — those sources were NOT assessed (retried once). Re-run 📓 shortlist to try "
                "again, or consolidate to a single notebook first.")
            return {"ok": False, "shortlist_ids": [], "matched": [], "unmatched": [],
                    "incomplete": True, "messages": [msg], "error": "incomplete answer"}
        errs = "; ".join(e.get("error", "?") for e in series)
        msg = db.append_message(tab_id, "s", f"NotebookLM shortlist failed: {errs}")
        return {"ok": False, "shortlist_ids": [], "matched": [], "unmatched": [],
                "messages": [msg], "error": errs}

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
    # COVERAGE: NLM can only name documents that are SOURCES in its notebooks. Candidates
    # not yet exported are invisible to this shortlist — disclose that so a small/biased
    # shortlist isn't mistaken for "the best of all candidates".
    in_nlm = sum(1 for d in cands if d.get("nlm_source_notebook"))
    not_in_nlm = len(cands) - in_nlm
    coverage = (f"Coverage: {in_nlm} of {len(cands)} candidate(s) are NLM sources"
                + (f"; {not_in_nlm} are NOT in NLM and were invisible to this shortlist — "
                   "📭 filter + 📓➕ add them to a notebook to include them"
                   if not_in_nlm else ""))
    summary = (f"📓 NotebookLM shortlisted {len(matched)} candidate(s)"
               + (f" — best first: {', '.join(d['number'] for d in matched)}" if matched else "")
               + f". ({coverage}.) The answer above ranks the best + second-best with a per-feature "
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
    if incomplete:
        # name each notebook NLM didn't finish + how many of its sources went unassessed, so a
        # short shortlist isn't mistaken for a complete read (the silent-drop bug, made loud).
        bits = []
        for e in incomplete:
            nb = e.get("notebook_id")
            n_src = sum(1 for d in cands if d.get("nlm_source_notebook") == nb)
            bits.append(f"«{e.get('title') or nb}» ({n_src} source(s))")
        summary += ("\n\n⚠️ NotebookLM returned a truncated / structureless answer for "
                    + ", ".join(bits) + " — those sources were NOT assessed (retried once). "
                    "Re-run 📓 shortlist to try again, or consolidate to a single notebook first.")
    out.append(db.append_message(tab_id, "s", summary))
    return {"ok": True, "shortlist_ids": [d["id"] for d in matched],
            "matched": [d["number"] for d in matched], "unmatched": unmatched,
            "incomplete": bool(incomplete), "total": len(cands), "messages": out}


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


# ---------- 🏆 best-match cross-tab scan ----------
# "Best match" must consider EVERY relevant document, not just this tab's list: any
# other-tab candidate that covers even ONE of this benchmark's features is pulled in
# as a first-class candidate here (full content copied — no re-fetch), with the
# covered features indicated. Digest-based (cheap, like ♻️ re-check); negatives are
# cached per benchmark fingerprint so repeat Best-match clicks only scan what's new.
XSCAN_BATCH = int(os.environ.get("PB_XSCAN_BATCH", "25"))
_XSCAN_STATE: dict[int, dict] = {}          # tab_id → live progress (in-memory job)
_XSCAN_NUM_RE = re.compile(r"^[A-Z]{1,3}\d[\dA-Z]*$")   # scannable publication numbers


def _benchmark_fingerprint(bm: dict) -> str:
    """Identity of the CURRENT benchmark for the scan cache — changes when the
    benchmark document or its feature list changes, so stale verdicts don't stick."""
    feats = [(f.get("name"), f.get("weight")) for f in (bm.get("features") or [])]
    basis = json.dumps([bm.get("number"), bm.get("content_hash"),
                        len(bm.get("text") or ""), len(bm.get("description") or ""),
                        feats], ensure_ascii=False, sort_keys=True)
    return hashlib.md5(basis.encode()).hexdigest()


def _seed_feature_scores(hits: list[dict], features: list[dict]) -> list[dict] | None:
    """Align scan hits onto the full benchmark feature list (unmatched → 'no'), the
    same shape deep-read stores — so the feature chips indicate coverage at once.
    The next full read overwrites this seed with rigorous full-text verdicts."""
    if not features:
        return None
    by_name = {h["name"]: h for h in hits}
    return [{"name": f.get("name", ""), "weight": int(f.get("weight", 1)),
             "status": by_name.get(f.get("name", ""), {}).get("status", "no"),
             "note": by_name.get(f.get("name", ""), {}).get("note", "")}
            for f in features]


def _run_cross_scan(tab_id: int, bm: dict, fp: str, todo: list[dict],
                    model: str | None) -> None:
    st = _XSCAN_STATE[tab_id]
    features = bm.get("features") or []
    by_num = {d["number"]: d for d in todo}
    try:
        batches = [todo[i:i + XSCAN_BATCH] for i in range(0, len(todo), XSCAN_BATCH)]

        def one(batch: list[dict]) -> None:
            res = claude_bridge.cross_tab_scan(bm, features, batch, model=model)
            marks: dict[str, bool] = {}
            if "error" not in res:
                for num, r in (res.get("results") or {}).items():
                    d = by_num.get(num)
                    if not d:
                        continue
                    matched = bool(r["features"] or r["covers"])
                    marks[num] = matched
                    if matched:
                        covers = (", ".join(h["name"] for h in r["features"])
                                  or r["covers"] or "")
                        note = (f"↪ pulled from tab «{d['tab_name']}» — digest "
                                f"pre-check: covers {covers}")[:300]
                        new_id = db.import_document_copy(
                            tab_id, d["doc_id"],
                            feature_scores=_seed_feature_scores(r["features"], features),
                            score_note=note)
                        if new_id:
                            st["imported"].append({"id": new_id, "number": num,
                                                   "from": d["tab_name"],
                                                   "covers": covers})
                db.cross_scan_mark(tab_id, fp, marks)   # per-batch: a kill loses little
            else:
                st["errors"] += 1
            st["done"] += len(batch)

        with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
            list(ex.map(one, batches))
        if st["imported"]:
            names = ", ".join(f"{i['number']} (from «{i['from']}»)"
                              for i in st["imported"][:12])
            more = len(st["imported"]) - 12
            db.append_message(tab_id, "s",
                f"🏆 Cross-tab scan: pulled {len(st['imported'])} document(s) from other "
                f"tabs into this one — each covers ≥1 benchmark feature (digest "
                f"pre-check; the deep read will assess them in full): {names}"
                + (f" … +{more} more" if more > 0 else "") + ".")
    finally:
        st["running"] = False


@app.post("/api/tabs/{tab_id}/cross-tab-scan")
def cross_tab_scan_ep(tab_id: int, bg: BackgroundTasks,
                      body: schemas.CrossTabScanRequest | None = None):
    """Start the cross-tab scan for this tab's 🏆 Best match. Returns immediately;
    poll GET /cross-tab-scan/status until running=false."""
    _tab_or_404(tab_id)
    st = _XSCAN_STATE.get(tab_id)
    if st and st.get("running"):
        raise HTTPException(409, "a cross-tab scan is already running for this tab")
    bm = db.get_benchmark(tab_id, full=True)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    fp = _benchmark_fingerprint(bm)
    here = {d["number"] for d in db.list_documents(tab_id)}
    checked = db.cross_scan_checked(tab_id, fp)
    others = db.documents_across_tabs(exclude_tab_id=tab_id)
    todo = [d for d in others
            if d["number"] not in here and d["number"] not in checked
            and (d.get("digest") or d.get("verdict"))
            and _XSCAN_NUM_RE.match(d["number"] or "")]
    unscannable = sum(1 for d in others if d["number"] not in here
                      and not _XSCAN_NUM_RE.match(d["number"] or ""))
    _XSCAN_STATE[tab_id] = {"running": bool(todo), "total": len(todo), "done": 0,
                            "imported": [], "errors": 0,
                            "cached_skipped": len(checked & {d["number"] for d in others}),
                            "unscannable": unscannable}
    if todo:
        bg.add_task(_run_cross_scan, tab_id, bm, fp, todo,
                    _read_model(body.model if body else None))
    return {"started": bool(todo), **_XSCAN_STATE[tab_id]}


@app.get("/api/tabs/{tab_id}/cross-tab-scan/status")
def cross_tab_scan_status(tab_id: int):
    _tab_or_404(tab_id)
    return _XSCAN_STATE.get(tab_id) or {"running": False, "total": 0, "done": 0,
                                        "imported": [], "errors": 0}


@app.post("/api/tabs/{tab_id}/digest-rescore")
def digest_rescore_ep(tab_id: int, body: schemas.DigestRescoreRequest):
    """♻️ RE-CHECK. After a benchmark change, re-score candidates against the CURRENT benchmark
    from their STORED DIGESTS — no full-text re-read, no downgrade from a slow full pass. Scope
    is the top-N or EVERY candidate with a digest (all_docs), batched so hundreds of digests
    cannot blow the prompt budget. Updates score/score_note (score_model tagged '·digest' so
    it's clear these are the cheap digest-based scores, not a fresh full read)."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    fetched = [d for d in db.list_documents(tab_id, full=True)
               if d["status"] == "fetched" and (d.get("digest") or "").strip()]
    by_id = {d["id"]: d for d in fetched}
    if body.all_docs:                                   # EVERY candidate with a digest
        chosen = sorted(fetched, key=lambda d: -(d.get("score") or 0))
    elif body.doc_ids:
        chosen = [by_id[i] for i in body.doc_ids if i in by_id]
    else:
        chosen = [by_id[i] for i in db.top_scored_documents(tab_id, body.top_n) if i in by_id]
        if not chosen:                                  # nothing scored yet → any fetched-with-digest
            chosen = fetched[:body.top_n]
    if not chosen:
        raise HTTPException(400, "no candidates with a stored digest — run a 🏆 deep-compare / "
                                 "full read once so there are digests to re-check against")
    model = _read_model(body.model) or claude_bridge.DIGEST_MODEL
    batches = [chosen[i:i + BULK_DIGEST_BATCH] for i in range(0, len(chosen), BULK_DIGEST_BATCH)]
    results: dict = {}
    errors: list[str] = []
    used_model: list[str] = []
    now = int(time.time())
    lock = threading.Lock()

    def one(batch: list[dict]) -> None:
        res = claude_bridge.digest_rescore(bm, batch, model=model)
        if "error" in res:
            with lock:
                errors.append(res["error"])       # a failed batch loses only its own docs
            return
        got = res.get("results") or {}
        tag = f"{res.get('model') or model}·digest"
        for d in batch:                           # persist per batch: a kill loses little
            r = got.get(d["number"])
            if r and r.get("score") is not None:
                db.update_document(d["id"], score=r["score"], score_note=r.get("note") or None,
                                   scored_at=now, score_model=tag)
        with lock:
            results.update(got)
            if res.get("model"):
                used_model.append(res["model"])

    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(one, batches))
    updated = sum(1 for d in chosen
                  if (results.get(d["number"]) or {}).get("score") is not None)
    if not updated:
        raise HTTPException(400, f"re-check failed: {errors[0] if errors else 'no scores parsed'}")
    # Say plainly when batches failed — a partial run must never read as full coverage.
    missed = len(chosen) - updated
    note = (f" ⚠ {missed} candidate(s) not re-checked ({len(errors)} batch(es) failed) — re-run "
            "to fill them in." if missed else "")
    db.append_message(tab_id, "s",
        f"♻️ Re-checked {updated} candidate(s){' — ALL with a digest' if body.all_docs else ''} "
        f"against the current benchmark from their stored digests "
        f"({used_model[0] if used_model else model}) in {len(batches)} bulk pass(es) — NO "
        "full-text re-read. Scores are tagged ·digest (cheap re-check); run a 🏆 full "
        f"deep-compare when you want the rigorous opus read back.{note}")
    return {"ok": True, "updated": updated, "requested": len(chosen), "batches": len(batches),
            "failed_batches": len(errors), "results": results}


@app.get("/api/tabs/{tab_id}/digest-gap")
def digest_gap_ep(tab_id: int):
    """How many candidates are OUT OF SCOPE of every digest-based tool (➕ additional read,
    ♻️ re-check, 🧩 combi) because they have no stored digest. Surfaced in the UI so the gap
    is never invisible: a run over 'all documents' silently means 'all WITH a digest'."""
    _tab_or_404(tab_id)
    fetched = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    missing = [d for d in fetched if not (d.get("digest") or "").strip()]
    return {"ok": True, "fetched": len(fetched), "with_digest": len(fetched) - len(missing),
            "missing": len(missing),
            "docs": [{"id": d["id"], "number": d.get("number"),
                      "error": d.get("digest_error")} for d in missing[:200]]}


@app.post("/api/tabs/{tab_id}/digest-backfill")
def digest_backfill_ep(tab_id: int, body: schemas.DigestBackfillRequest):
    """🔁 Generate the MISSING digests, so 'all documents' finally means all of them. One
    cheap call per candidate (that is what a digest is), run concurrently. Explicitly
    user-triggered: it costs one call per missing document, so it never fires on its own."""
    _tab_or_404(tab_id)
    fetched = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    missing = [d for d in fetched if not (d.get("digest") or "").strip()]
    if not missing:
        return {"ok": True, "backfilled": 0, "still_missing": 0,
                "note": "every fetched candidate already has a digest"}
    model = _read_model(body.model)
    ids = [d["id"] for d in missing]
    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(lambda i: _digest_doc(i, model), ids))
    after = {d["id"]: d for d in db.list_documents(tab_id, full=True)}
    done = [i for i in ids if (after.get(i, {}).get("digest") or "").strip()]
    failed = [after[i] for i in ids if i not in done and i in after]
    why = ", ".join(sorted({(d.get("digest_error") or "?")[:60] for d in failed})[:3])
    db.append_message(tab_id, "s",
        f"🔁 Digest backfill: {len(done)} of {len(ids)} missing digest(s) generated — those "
        f"candidates are now in scope for ➕ additional read, ♻️ re-check and 🧩 combi."
        + (f" ⚠ {len(failed)} still failed ({why}) — re-run to retry them." if failed else ""))
    return {"ok": True, "backfilled": len(done), "still_missing": len(failed), "why": why}


@app.post("/api/tabs/{tab_id}/additional-read")
def additional_read_ep(tab_id: int, body: schemas.AdditionalReadRequest):
    """➕ ADDITIONAL READ. Check the benchmark's ADDITIONAL (A) features against candidates
    in bulk calls over their STORED DIGESTS — no full-text re-read, so it's cheap. Scope is
    the displayed top-N, or EVERY candidate with a digest (all_docs). Stores per-doc
    additional_scores; the UI turns those into a bonus that only RAISES the score (absence
    never lowers it)."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    a_features = [f for f in ((bm or {}).get("features") or []) if f.get("kind") == "A"]
    if not a_features:
        raise HTTPException(400, "no additional (A) features — mark some benchmark features 'A' first")
    fetched = [d for d in db.list_documents(tab_id, full=True)
               if d["status"] == "fetched" and (d.get("digest") or "").strip()]
    by_id = {d["id"]: d for d in fetched}
    if body.all_docs:                                   # EVERY candidate with a digest
        chosen = sorted(fetched, key=lambda d: -(d.get("score") or 0))
    elif body.doc_ids:                                  # the UI's exact top-N, in ranked order
        chosen = [by_id[i] for i in body.doc_ids if i in by_id]
    else:                                               # fall back to Claude's top_n by score
        chosen = [by_id[i] for i in db.top_scored_documents(tab_id, body.top_n) if i in by_id]
    if not chosen:
        raise HTTPException(400, "no candidates with a stored digest — run a 🏆 deep-compare / "
                                 "full read first so there's material to check against")
    model = _read_model(body.model) or claude_bridge.DIGEST_MODEL
    batches = [chosen[i:i + BULK_DIGEST_BATCH] for i in range(0, len(chosen), BULK_DIGEST_BATCH)]
    results: dict = {}
    errors: list[str] = []
    used_model: list[str] = []
    lock = threading.Lock()

    def one(batch: list[dict]) -> None:
        res = claude_bridge.additional_read(a_features, batch, model=model)
        if "error" in res:
            with lock:
                errors.append(res["error"])       # a failed batch loses only its own docs
            return
        got = res.get("results") or {}
        for d in batch:                           # persist per batch: a kill loses little
            feats = got.get(d["number"])
            if feats is not None:
                db.update_document(d["id"], additional_scores=json.dumps(feats, ensure_ascii=False))
        with lock:
            results.update(got)
            if res.get("model"):
                used_model.append(res["model"])

    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(one, batches))
    stored = sum(1 for d in chosen if results.get(d["number"]) is not None)
    if not stored:
        raise HTTPException(400, f"additional read failed: {errors[0] if errors else 'no verdicts parsed'}")
    names = ", ".join(f["name"] for f in a_features[:6]) + (" …" if len(a_features) > 6 else "")
    # Say plainly when batches failed — a partial run must never read as full coverage.
    missed = len(chosen) - stored
    note = (f" ⚠ {missed} candidate(s) not assessed ({len(errors)} batch(es) failed) — re-run to "
            "fill them in." if missed else "")
    db.append_message(tab_id, "s",
        f"➕ Additional read ({used_model[0] if used_model else 'sonnet'}) over {stored} "
        f"candidate(s){' — ALL with a digest' if body.all_docs else ''} in "
        f"{len(batches)} bulk pass(es) for {len(a_features)} additional feature(s): {names}. "
        "Present/stretched features now ADD to each doc's score (absence never lowers it) — "
        f"see the 🟢/🟡 chips and the +bonus in the score.{note}")
    return {"ok": True, "assessed": stored, "requested": len(chosen), "batches": len(batches),
            "failed_batches": len(errors), "a_features": len(a_features), "results": results}


# ---------- 🔎 combi investigation: the TOOL finds the 2-document combination ----------
# Deliberately independent of every other score in the app: its own per-element verdicts
# (documents.combi_coverage), its own pair rating. It never reads or moves score /
# feature_scores / additional_scores, so a pair's standing here says nothing about, and is
# unaffected by, how either document ranks on its own.

COMBI_SCREEN_BATCH = int(os.environ.get("PB_COMBI_SCREEN_BATCH", "40"))
# The rigorous coverage pass is FAR heavier per document than a re-score: a full digest each,
# times every element, with evidence. At 25 it timed out (300s) and lost the whole batch, so
# it gets its own smaller size — and a failed batch is split and retried rather than dropped.
COMBI_SCAN_BATCH = int(os.environ.get("PB_COMBI_SCAN_BATCH", "8"))
# A-feature bonus, mirroring the ➕ additional read's scale (app.js ADD_UNIT/ADD_CAP): a
# present additional element RAISES a pair's rating, its absence never lowers it.
ADD_UNIT, ADD_CAP = 0.3, 1.0
# screen → digest → full. A pair is only as trustworthy as its weaker document.
_DEPTH_RANK = {"screen": 0, "digest": 1, "full": 2}


def _combi_elements(bm: dict | None) -> list[dict]:
    """Every feature the coverage pass judges — MANDATORY *and* ADDITIONAL.

    The additional ones ride along on purpose: a combination that also brings the bonus
    element is a better combination, and it costs nothing to ask for it in the same pass.
    They are kept apart when RATING (see _combi_pairs): only mandatory elements decide
    whether a pair covers the invention."""
    return list((bm or {}).get("features") or [])


def _combi_mandatory(bm: dict | None) -> list[dict]:
    return [f for f in ((bm or {}).get("features") or []) if (f.get("kind") or "M") != "A"]


def _cov_records(doc: dict) -> dict:
    """{element name: stored verdict record}. Element identity is its NAME, which is what
    makes re-assessment incremental: adding elements leaves the existing ones matched."""
    try:
        return {c["name"]: c for c in json.loads(doc.get("combi_coverage") or "[]")}
    except (ValueError, TypeError, KeyError):
        return {}


def _cov_map(doc: dict) -> dict:
    return {n: r.get("status", "no") for n, r in _cov_records(doc).items()}


def _missing_for(doc: dict, elements: list[dict], want_depth: str) -> list[dict]:
    """The elements this document still needs judged AT `want_depth` — i.e. never judged,
    or judged only by a weaker stage. Everything already assessed at this depth or better
    is reused, so adding elements (e.g. splitting the additional feature) re-reads ONLY the
    new ones instead of paying for the whole element list again."""
    have = _cov_records(doc)
    want = _DEPTH_RANK[want_depth]
    return [e for e in elements
            if _DEPTH_RANK.get((have.get(e["name"]) or {}).get("depth") or "", -1) < want]


def _merge_cov(doc: dict, judged: list[dict], verdicts: list[dict], depth: str,
               elements: list[dict]) -> tuple[str, str]:
    """Fold fresh verdicts into the document's stored coverage, keeping any existing record
    that was made at a BETTER depth. Returns (coverage json, doc-level depth = the weakest
    depth among the current benchmark's elements — a document is only as verified as its
    least-verified element)."""
    have = _cov_records(doc)
    by_name = {v["name"]: v for v in verdicts}
    for e in judged:
        v = by_name.get(e["name"])
        if v is None:
            continue
        prev = have.get(e["name"])
        if prev and _DEPTH_RANK.get(prev.get("depth") or "", -1) > _DEPTH_RANK[depth]:
            continue                      # never downgrade a stronger read
        have[e["name"]] = {**v, "depth": depth}
    keep = [have[e["name"]] for e in elements if e["name"] in have]
    weakest = min((r.get("depth") or "screen" for r in keep),
                  key=lambda d: _DEPTH_RANK.get(d, 0), default="screen")
    return json.dumps(keep, ensure_ascii=False), weakest


def _rigorous(doc: dict, elements: list[dict]) -> bool:
    """Has every MANDATORY element of this document been judged at digest depth or better?

    The 🩺 screen is deliberately generous — it over-includes on purpose, because its only
    job is to rank who deserves a real look. Letting those verdicts through would publish
    'covers everything' findings built from a guess, so nothing screen-only ever reaches
    the pairs or the solo list. Additional elements may still be screen-level: they are a
    bonus and never decide a finding."""
    have = _cov_records(doc)
    mand = [e for e in elements if (e.get("kind") or "M") != "A"]
    if not mand:
        return False
    return all(_DEPTH_RANK.get((have.get(e["name"]) or {}).get("depth") or "", -1)
               >= _DEPTH_RANK["digest"] for e in mand)


def _combi_solo(elements: list[dict], docs: list[dict], limit: int = 20) -> list[dict]:
    """Documents that cover EVERY mandatory element ON THEIR OWN.

    Strictly stronger than any combination — one document disclosing the whole invention is
    a novelty-grade hit, where a pair is only an obviousness argument that still needs a
    motivation to combine. _combi_pairs deliberately drops any pair where one document
    subsumes the other, so without this list a solo full-coverer would vanish from the
    results entirely — the strongest finding, invisible. Ordered by additional coverage,
    which is what separates them once they all cover the mandatory elements."""
    mand = [e for e in elements if (e.get("kind") or "M") != "A"]
    add = [e for e in elements if (e.get("kind") or "M") == "A"]
    if not mand:
        return []
    out = []
    for d in docs:
        cov = _cov_map(d)
        if any(cov.get(e["name"], "no") != "yes" for e in mand):
            continue
        bonus, add_full, add_part = 0.0, 0, 0
        for e in add:
            s = cov.get(e["name"], "no")
            unit = (int(e.get("weight", 1)) / 5) * ADD_UNIT
            if s == "yes":
                bonus += unit
                add_full += 1
            elif s == "partial":
                bonus += unit * 0.5
                add_part += 1
        out.append({"id": d["id"], "number": d.get("number"),
                    "mand_total": len(mand),
                    # add_cov = full+partial (kept for compat); add_full / add_partial split
                    # them so a '9/9' that is mostly stretch reads as weaker than a '7 full'
                    "add_cov": add_full + add_part, "add_full": add_full,
                    "add_partial": add_part, "add_total": len(add),
                    "add_bonus": round(min(ADD_CAP, bonus), 2),
                    "depth": d.get("combi_depth") or "screen"})
    out.sort(key=lambda s: (-s["add_bonus"], -s["add_cov"], s["number"] or ""))
    return out[:limit]


def _combi_pairs(elements: list[dict], docs: list[dict], limit: int) -> list[dict]:
    """Every GENUINE 2-document combination, computed in code (free, no model call).

    MANDATORY elements decide coverage: a pair is COMPLETE when the union discloses (YES)
    every one of them. ADDITIONAL elements never affect completeness — their presence adds
    a bonus that RAISES the rating (same scale as the ➕ additional read), their absence
    costs nothing. So a pair that also brings the bonus element outranks an equal pair
    that doesn't.

    A pair only counts as a combination when BOTH documents uniquely contribute a
    mandatory element the other lacks — otherwise one document simply subsumes the other
    and there is nothing to combine."""
    mand = [e for e in elements if (e.get("kind") or "M") != "A"]
    add = [e for e in elements if (e.get("kind") or "M") == "A"]
    total_w = sum(int(e.get("weight", 1)) for e in mand) or 1
    cov = {d["id"]: _cov_map(d) for d in docs}
    out = []
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            A, B = docs[i], docs[j]
            ca, cb = cov[A["id"]], cov[B["id"]]

            def best(name):
                sa, sb = ca.get(name, "no"), cb.get(name, "no")
                return ("yes" if "yes" in (sa, sb)
                        else "partial" if "partial" in (sa, sb) else "no"), sa, sb

            w = 0.0
            complete = True
            only_a, only_b = [], []
            for e in mand:
                u, sa, sb = best(e["name"])
                if u == "yes":
                    w += int(e.get("weight", 1))
                elif u == "partial":
                    w += int(e.get("weight", 1)) * 0.5
                    complete = False
                else:
                    complete = False
                if sa == "yes" and sb != "yes":
                    only_a.append(e["name"])
                elif sb == "yes" and sa != "yes":
                    only_b.append(e["name"])
            if not only_a or not only_b:
                continue                      # not a combination: one subsumes the other
            # ADDITIONAL: bonus only — never part of `complete`, never a penalty.
            bonus, add_full, add_part = 0.0, 0, 0
            for e in add:
                u, _, _ = best(e["name"])
                unit = (int(e.get("weight", 1)) / 5) * ADD_UNIT
                if u == "yes":
                    bonus += unit
                    add_full += 1
                elif u == "partial":
                    bonus += unit * 0.5
                    add_part += 1
            bonus = min(ADD_CAP, bonus)
            depth = min((A.get("combi_depth") or "screen", B.get("combi_depth") or "screen"),
                        key=lambda d: _DEPTH_RANK.get(d, 0))
            out.append({
                "a_id": A["id"], "b_id": B["id"],
                "a": A.get("number"), "b": B.get("number"),
                "complete": complete,
                # rating = MANDATORY coverage only, on its own 0–10 scale. The additional
                # bonus is deliberately NOT added in: complete pairs already sit at 10, so
                # adding it there would saturate and hide the very thing it measures. It
                # ranks instead (below), and is reported separately — an honest metric plus
                # a visible bonus beats one number that quietly means two things.
                "rating": round(10.0 * w / total_w, 1),
                "add_bonus": round(bonus, 2), "add_cov": add_full + add_part,
                "add_full": add_full, "add_partial": add_part, "add_total": len(add),
                "covered_w": round(w, 1), "total_w": total_w,
                "a_only": only_a[:12], "b_only": only_b[:12],
                "depth": depth,
            })
    # Additional coverage breaks ties: between two pairs that cover the invention equally,
    # the one that ALSO brings the bonus element is the better combination.
    out.sort(key=lambda p: (-p["complete"], -p["rating"], -p["add_bonus"],
                            -len(p["a_only"]) - len(p["b_only"])))
    return out[:limit]


@app.get("/api/tabs/{tab_id}/combi-results")
def combi_results_ep(tab_id: int, top_pairs: int = 20):
    """The LAST investigation's findings, re-derived from STORED coverage — so a page reload
    doesn't lose them. The panel is otherwise pure client state (a scan's response held in
    memory), which a refresh wipes even though every verdict is safely in the DB. This lets
    the UI rehydrate the panel on load. Nothing is computed by a model here."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    elements = _combi_elements(bm)
    fresh = [d for d in db.list_documents(tab_id, full=True) if _rigorous(d, elements)]
    if not fresh:
        return {"ok": True, "has_results": False, "pairs": [], "solo": [], "elements": len(elements)}
    pairs = _combi_pairs(elements, fresh, top_pairs)
    solo = _combi_solo(elements, fresh)
    depth = "full" if fresh and all(d.get("combi_depth") == "full" for d in fresh) else "digest"
    return {"ok": True, "has_results": True, "assessed": len(fresh),
            "elements": len(elements), "complete": len([p for p in pairs if p["complete"]]),
            "pairs": pairs, "solo": solo, "depth": depth}


@app.post("/api/tabs/{tab_id}/combi-screen")
def combi_screen_ep(tab_id: int, body: schemas.CombiScreenRequest):
    """🩺 STAGE 0 — the FAST cut. Rank every digested candidate by how many elements it could
    PLAUSIBLY disclose, then hand back the top N for the rigorous pass.

    Deliberately cheap and generous: cheapest model, a short digest extract, terse output
    (element numbers, no evidence). Judging 284 candidates × 12 elements at full rigour is
    the slow part — this decides WHO deserves that in a fraction of the time. Recall is what
    matters here, so it stretches: what this stage drops is never looked at again."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    elements = _combi_elements(bm)
    if not elements:
        raise HTTPException(400, "the benchmark has no features to screen against")
    docs = [d for d in db.list_documents(tab_id, full=True)
            if d["status"] == "fetched" and (d.get("digest") or "").strip()]
    if not docs:
        raise HTTPException(400, "no candidates with a stored digest — 🔁 backfill first")
    model = _read_model(body.model) or claude_bridge.SCREEN_MODEL
    # INCREMENTAL, like the scan: only ask about elements this document hasn't been judged
    # on yet. Re-running after splitting the additional feature screens the NEW elements
    # only — everything already assessed is reused.
    todo: dict[tuple, list[dict]] = {}
    for d in docs:
        miss = _missing_for(d, elements, "screen")
        if miss:
            todo.setdefault(tuple(e["name"] for e in miss), []).append(d)
    reused = len(docs) - sum(len(v) for v in todo.values())
    by_name = {e["name"]: e for e in elements}
    batches: list[tuple[list[dict], list[dict]]] = []
    for names, group in todo.items():
        subset = [by_name[n] for n in names]
        for i in range(0, len(group), COMBI_SCREEN_BATCH):
            batches.append((subset, group[i:i + COMBI_SCREEN_BATCH]))
    hits: dict[str, list] = {}
    errors: list[str] = []
    lock = threading.Lock()

    def one(job: tuple[list[dict], list[dict]]) -> None:
        subset, batch = job
        res = claude_bridge.combi_fast_screen(subset, batch, model=model)
        if "error" in res:
            with lock:
                errors.append(res["error"])
            return
        subset, batch = job
        got = res.get("results") or {}
        for d in batch:
            idxs = got.get(d["number"])
            if idxs is None:
                continue
            # Screen verdicts are coarse and generous — stored at depth 'screen' so nothing
            # downstream mistakes them for a rigorous read. Merged, so an element already
            # judged at digest/full depth keeps its stronger verdict.
            v = [{"name": e["name"], "weight": int(e.get("weight", 1)),
                  "status": "yes" if i in idxs else "no", "evidence": ""}
                 for i, e in enumerate(subset)]
            cov, depth = _merge_cov(d, subset, v, "screen", elements)
            db.update_document(d["id"], combi_coverage=cov, combi_depth=depth)
            with lock:
                hits[d["number"]] = [subset[i]["name"] for i in idxs]

    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(one, batches))
    if not hits and not reused:
        raise HTTPException(400, f"combi screen failed: {errors[0] if errors else 'nothing parsed'}")
    # Rank from STORED coverage, not this run's hits: with the incremental pass a document's
    # verdicts may come partly from an earlier run, and the ranking must see all of them.
    mand = [e for e in elements if (e.get("kind") or "M") != "A"]
    scored = []
    for d in db.list_documents(tab_id, full=True):
        if not d.get("combi_coverage"):
            continue
        cov = _cov_map(d)
        hit = [e for e in mand if cov.get(e["name"]) in ("yes", "partial")]
        scored.append({"id": d["id"], "number": d.get("number"),
                       "hits": len([1 for e in elements if cov.get(e["name"]) in ("yes", "partial")]),
                       "mand_hits": len(hit),
                       "weight": sum(int(e.get("weight", 1)) for e in hit)})
    scored.sort(key=lambda x: (-x["weight"], -x["mand_hits"]))
    keep = scored[:body.top_n]
    missed = len(docs) - len(hits) - reused
    why = "; ".join(sorted({e[:80] for e in errors})[:2])
    note = (f" ⚠ {missed} candidate(s) NOT screened ({len(errors)} batch(es) failed: {why}) — "
            "they cannot reach the shortlist until you re-run." if missed > 0 else "")
    db.append_message(tab_id, "s",
        f"🩺 Fast screen (stage 0, {model}) over {len(hits) + reused} candidate(s) in "
        f"{len(batches)} pass(es) against {len(elements)} element(s)"
        + (f" — {len(hits)} newly screened, {reused} already assessed and REUSED rather than "
           "re-read" if reused else "")
        + f": shortlisted the top {len(keep)} for a closer look. Generous by design "
        "(broad/implicit readings included) — this only decides WHO gets the rigorous "
        f"pass, it is not a verdict.{note}")
    return {"ok": True, "screened": len(hits), "reused": reused, "requested": len(docs),
            "batches": len(batches), "failed_batches": len(errors),
            "elements": len(elements), "shortlist": keep, "dropped": len(scored) - len(keep)}


@app.post("/api/tabs/{tab_id}/combi-scan")
def combi_scan_ep(tab_id: int, body: schemas.CombiScanRequest):
    """🔎 STAGE 1. Map which ELEMENTS each candidate discloses, from stored digests, across
    EVERY candidate that has one — then derive, in code, the pairs that TOGETHER cover
    everything. The tool investigates; nothing here depends on the user picking D1/D2.

    Cheap + batched on purpose: the pair that covers everything may rank nowhere near the
    top, so the scan must span the whole corpus, not a top-N."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    elements = _combi_elements(bm)
    if len(_combi_mandatory(bm)) < 2:
        raise HTTPException(400, "combination analysis needs at least TWO mandatory elements — "
                                 "one monolithic feature cannot be split between two documents. "
                                 "Use 🔬 Decompose to split the claim into its elements first.")
    docs = [d for d in db.list_documents(tab_id, full=True)
            if d["status"] == "fetched" and (d.get("digest") or "").strip()]
    if body.doc_ids:                       # the 🩺 screen's shortlist — the usual path
        keep = set(body.doc_ids)
        docs = [d for d in docs if d["id"] in keep]
    if len(docs) < 2:
        raise HTTPException(400, "need at least two candidates with a stored digest — "
                                 "🔁 backfill the missing digests first")
    model = _read_model(body.model) or claude_bridge.DIGEST_MODEL
    # INCREMENTAL: ask each document only about the elements it still needs at this depth.
    # Documents are grouped by their missing set so one bulk call serves a whole group —
    # after splitting the additional feature, every document is missing exactly the new A
    # elements, so the re-run costs those alone instead of the entire element list again.
    todo: dict[tuple, list[dict]] = {}
    for d in docs:
        miss = _missing_for(d, elements, "digest")
        if miss:
            todo.setdefault(tuple(e["name"] for e in miss), []).append(d)
    reused = len(docs) - sum(len(v) for v in todo.values())
    by_name = {e["name"]: e for e in elements}
    batches: list[tuple[list[dict], list[dict]]] = []
    for names, group in todo.items():
        subset = [by_name[n] for n in names]
        for i in range(0, len(group), COMBI_SCAN_BATCH):
            batches.append((subset, group[i:i + COMBI_SCAN_BATCH]))
    errors: list[str] = []
    done_ids: set[int] = set()
    lock = threading.Lock()

    def one(job: tuple[list[dict], list[dict]]) -> None:
        """Run a batch; on failure SPLIT it and retry the halves. Timeouts scale with batch
        size, so halving usually turns one dead batch into two that land — far better than
        losing 25 documents to a single slow call."""
        subset, batch = job
        res = claude_bridge.combi_coverage_digests(subset, batch, model=model)
        if "error" in res:
            if len(batch) > 1:
                half = len(batch) // 2
                one((subset, batch[:half]))
                one((subset, batch[half:]))
            else:
                with lock:
                    errors.append(f"{batch[0].get('number')}: {res['error']}")
            return
        got = res.get("results") or {}
        for d in batch:
            v = got.get(d["number"])
            if v is not None:
                cov, depth = _merge_cov(d, subset, v, "digest", elements)
                db.update_document(d["id"], combi_coverage=cov, combi_depth=depth)
                with lock:
                    done_ids.add(d["id"])

    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(one, batches))
    # Findings come ONLY from rigorously-assessed documents. Counting every row that has any
    # coverage conflated the 🩺 screen's guesses with real verdicts — and made `missed` go
    # negative (50 shortlisted minus 284 "scanned").
    fresh = [d for d in db.list_documents(tab_id, full=True) if _rigorous(d, elements)]
    scanned = reused + len(done_ids)
    if not scanned:
        raise HTTPException(400, f"combi scan failed: {errors[0] if errors else 'no verdicts parsed'}")
    pairs = _combi_pairs(elements, fresh, body.top_pairs)
    solo = _combi_solo(elements, fresh)
    complete = [p for p in pairs if p["complete"]]
    missed = len(docs) - scanned
    # Name the actual error: "11 batches failed" without a reason leaves the user staring at
    # results drawn from a fraction of the corpus with no idea why.
    why = "; ".join(sorted({e[:80] for e in errors})[:2])
    note = (f" ⚠ {missed} of {len(docs)} shortlisted candidate(s) NOT assessed ({len(errors)} "
            f"failed after splitting and retrying: {why}) — findings below are drawn only from "
            f"the {scanned} that were, and a pair or solo hit among the rest cannot be found "
            "until you re-run." if missed > 0 else "")
    db.append_message(tab_id, "s",
        f"🔎 Combi scan (stage 1, {model}) in {len(batches)} bulk pass(es) against "
        f"{len(elements)} element(s) of the claimed invention"
        + (f" — {reused} candidate(s) already assessed at this depth were REUSED, not re-read"
           if reused else "") + f". {len(solo)} document(s) cover EVERY mandatory element "
        f"ALONE (stronger than any combination), {len(complete)} pair(s) cover them together. "
        "Verdicts are from DIGESTS (summaries) — run stage 2 to confirm the finalists against "
        "full text. Independent of every other score in the app." + note)
    return {"ok": True, "scanned": scanned, "requested": len(docs), "batches": len(batches),
            "failed_batches": len(errors), "elements": len(elements),
            "complete": len(complete), "pairs": pairs, "solo": solo, "depth": "digest"}


@app.post("/api/tabs/{tab_id}/combi-verify")
def combi_verify_ep(tab_id: int, body: schemas.CombiVerifyRequest):
    """🔎 STAGE 2. Re-read the shortlisted finalists' FULL primary text against the elements,
    REPLACING their stage-1 digest verdicts with citable ones, then recompute the pairs.

    Stage 1 judges from summaries, so it can miss an element the full text does disclose —
    this is where a shortlisted pair is actually confirmed (or falls away)."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    elements = _combi_elements(bm)
    if len(_combi_mandatory(bm)) < 2:
        raise HTTPException(400, "combination analysis needs at least TWO mandatory elements — "
                                 "use 🔬 Decompose first")
    chosen = []
    for did in body.doc_ids:
        d = db.get_document(did)
        if not d or d["tab_id"] != tab_id:
            raise HTTPException(404, f"document {did} not found in this tab")
        if d["status"] == "fetched":
            chosen.append(d)
    if not chosen:
        raise HTTPException(400, "no fetched documents to verify")
    model = _read_model(body.model) or claude_bridge.CHAT_MODEL
    errors: list[str] = []
    lock = threading.Lock()

    def one(d: dict) -> None:
        res = claude_bridge.combi_coverage_full(elements, d, model=model)
        if "error" in res:
            with lock:
                errors.append(f"{d.get('number')}: {res['error']}")
            return
        v = (res.get("results") or {}).get(d["number"])
        if v is None:                       # model didn't echo the number → don't guess
            with lock:
                errors.append(f"{d.get('number')}: no verdict block returned")
            return
        cov, depth = _merge_cov(d, elements, v, "full", elements)
        db.update_document(d["id"], combi_coverage=cov, combi_depth=depth)

    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(one, chosen))
    fresh = [d for d in db.list_documents(tab_id, full=True) if d.get("combi_coverage")]
    verified = [d for d in fresh if d.get("combi_depth") == "full"]
    pairs = _combi_pairs(elements, fresh, body.top_pairs)
    solo = _combi_solo(elements, fresh)
    complete = [p for p in pairs if p["complete"]]
    note = (f" ⚠ {len(errors)} full read(s) failed ({'; '.join(errors[:2])}) — those keep their "
            "digest-based verdict." if errors else "")
    db.append_message(tab_id, "s",
        f"🔎 Combi verify (stage 2, {model}): full primary text re-read for "
        f"{len(chosen) - len(errors)} finalist(s); {len(verified)} candidate(s) now carry a "
        f"citable element map. {len(complete)} pair(s) cover EVERY element. A stage-2 verdict "
        "REPLACES the digest guess, so a shortlisted pair can legitimately fall away here."
        + note)
    return {"ok": True, "verified": len(chosen) - len(errors), "failed": len(errors),
            "elements": len(elements), "complete": len(complete), "pairs": pairs,
            "solo": solo, "depth": "full"}


@app.post("/api/tabs/{tab_id}/combi/motivation")
def combi_motivation_ep(tab_id: int, body: schemas.CombiMotivationRequest):
    """🧩 COMBI. For each candidate PAIR the UI found (two docs that TOGETHER cover all the
    benchmark's mandatory features), judge in ONE bulk LLM pass over their STORED DIGESTS whether
    the two are genuinely combinable (real §103-style motivation). Persists each verdict so it
    survives reload; never touches single-document scores. The coverage math itself is done in the
    UI (free, from stored feature verdicts) — this only adds the motivation judgment."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    # Coverage is computed from the weighted feature list, which a DOCUMENT benchmark may
    # also carry — what combi needs is the features, not a particular benchmark source.
    if not bm or not (bm.get("features") or []):
        raise HTTPException(400, "combi needs benchmark features — define them first")
    by_id = {d["id"]: d for d in db.list_documents(tab_id, full=True)
             if d["status"] == "fetched"}
    pairs, keys = [], []
    for p in body.pairs[:12]:                         # cap the bulk call to the top dozen pairs
        a, b = by_id.get(p.a_id), by_id.get(p.b_id)
        if not a or not b:
            continue
        pairs.append({"a": a, "b": b, "a_features": p.a_features, "b_features": p.b_features})
        keys.append((p.a_id, p.b_id))
    if not pairs:
        raise HTTPException(400, "no valid fetched document pairs to judge")
    res = claude_bridge.combi_motivation(bm, pairs, model=_read_model(body.model))
    if "error" in res:
        raise HTTPException(400, f"combi motivation failed: {res['error']}")
    results = res.get("results") or {}
    out = {}
    for i, (a_id, b_id) in enumerate(keys, 1):
        v = results.get(str(i))
        if not v:
            continue
        db.set_combi_motivation(tab_id, a_id, b_id, v["combinable"], v.get("reason") or "",
                                res.get("model"))
        lo, hi = sorted((a_id, b_id))
        out[f"{lo}-{hi}"] = {"combinable": v["combinable"], "reason": v.get("reason") or "",
                             "model": res.get("model")}
    db.append_message(tab_id, "s",
        f"🧩 Judged combinability of {len(out)} document pair(s) ({res.get('model') or 'sonnet'}) — "
        "each pair's two references TOGETHER cover all mandatory benchmark features. Verdicts are a "
        "hint of what a 2-reference combination achieves; they do NOT change any single document's score.")
    return {"ok": True, "results": out, "model": res.get("model")}


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
        # INTERLEAVE the two sides so Claude's top picks are always challenged too —
        # never crowded out by a long finalist list when the set is capped.
        seen, subject = set(), []
        for a, b in itertools.zip_longest(finalists, claude_top):
            for d in (a, b):
                if d and d["id"] not in seen:
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


def _model_rank(m: str | None) -> int:
    """Reading-model strength: LOWER index in MODELS = stronger. Unknown/None →
    weakest, so an explicit upgrade re-reads such candidates."""
    try:
        return claude_bridge.MODELS.index(m)
    except (ValueError, TypeError):
        return len(claude_bridge.MODELS)


def _assessed_at_least(d: dict, read_model: str, since: float = 0) -> bool:
    """True if this candidate already has a CURRENT full-text assessment from `read_model`
    OR a stronger one. Two gates:
    - model: never re-read what a stronger model already did (no downgrade) — this makes
      Continue resume an interrupted upgrade-read on exactly the leftovers.
    - freshness: the read must be NEWER than `since` (the benchmark's last change). A read
      done against an OLDER benchmark is STALE — it never checked a feature added afterwards —
      so staleness beats model strength and it IS re-read, even if that means a weaker model
      re-reads a once-opus doc. (since=0 → freshness gate off, e.g. selected re-reads.)"""
    if not _has_assessment(d):
        return False
    if since and (d.get("scored_at") or 0) < since:        # read predates the benchmark change
        return False
    return _model_rank(d.get("score_model")) <= _model_rank(read_model)


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
    # (skip_scored) is MODEL-AWARE: it reads only candidates not yet assessed by the
    # chosen 📖 reading model or a stronger one, so an interrupted upgrade-read (e.g.
    # sonnet over 221, killed at the token limit) resumes on exactly the leftovers
    # without re-reading what the strong model already did. Deep-read all / selected =
    # (re-)read the targeted set fresh. When nothing needs reading but assessments
    # exist, RE-RANK from them (zero reads) — work done anywhere is reused everywhere.
    read_model = _read_model(body.reading_model) or claude_bridge.DIGEST_MODEL
    # reads older than the benchmark's last change are STALE (they predate any feature you
    # added) → Continue re-reads them regardless of which model did them.
    bm_at = (db.get_benchmark(tab_id) or {}).get("updated_at") or 0
    to_read = ([d for d in docs if not _assessed_at_least(d, read_model, since=bm_at)]
               if body.skip_scored else list(docs))
    corpus = [d for d in all_docs if _has_assessment(d)]
    if not to_read and not corpus:
        raise HTTPException(400, "no fetched candidate documents"
                            + (" among the selected ones" if body.doc_ids else ""))
    if _claude_read_running(tab_id):
        return {"started": False, "running": True, **_claude_read_counts(tab_id)}
    target_ids = [d["id"] for d in to_read]
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    question = (body.question or "").strip() or DEEP_DEFAULT_QUESTION
    to_read.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
    if to_read:
        scope = (f"{len(to_read)} of {len(all_docs)}"
                 if len(to_read) != len(all_docs) else f"all {len(to_read)}")
        head = (f"[Deep {'read · continue' if body.skip_scored else 'compare'} — {scope} "
                f"candidates at full text ({read_model}): "
                f"{', '.join(d['number'] for d in to_read[:30])}]")
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
