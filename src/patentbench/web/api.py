"""Patent Workbench FastAPI app — tabs, documents, chat, NotebookLM, lessons."""
from __future__ import annotations

import glob
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
from datetime import datetime, timedelta, timezone

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
# After a batch deep read finishes, the 🧩 motivation-to-combine judge runs on the
# coverage matrix's anchor+partner pairs AUTOMATICALLY (only never-judged pairs —
# stored verdicts are never re-billed). The read already paid for the per-element
# coverage the pairs are computed from, so this is the one cheap step that turns it
# into a finished combination analysis.
AUTO_COMBI = os.environ.get("PB_AUTO_COMBI", "1") not in ("0", "", "false", "no")
# Digest-on-intake is OFF by default: adding numbers must cost ZERO model tokens
# (fetch is a plain scrape; the finalists get a FULL advanced-model read later
# anyway). Digests for the cheap bulk tools are generated on demand via
# 🔁 digest-backfill, or per add with the 🧠 checkbox.
AUTO_DIGEST = os.environ.get("PB_AUTO_DIGEST", "0") not in ("0", "", "false", "no")
MAX_UPLOAD = 25 * 1024 * 1024
# 🧪 automatic EPC sanity pass over argumentation-class answers (tech-effect,
# ⚖ PSA, 123(2) check): a second model call that repairs methodology violations
# (problem containing the solution, hindsight, unverified basis) before the
# answer reaches the user. Costs roughly one extra answer-sized call per run.
EPC_SANITY = os.environ.get("PB_EPC_SANITY", "1") not in ("0", "", "false", "no")
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


def _epc_sanitized(answer: str, model: str) -> tuple[str, str, str | None]:
    """🧪 Run the EPC sanity pass over an argumentation answer.
    Returns (final_answer, chip_title, correction_notes|None). On checker
    failure the ORIGINAL answer is kept — the pass never loses content."""
    if not EPC_SANITY:
        return answer, "", None
    res = claude_bridge.epc_sanity(answer, model=model)
    if res.get("error"):
        return answer, f"🧪 EPC sanity: unavailable ({res['error'][:80]})", None
    if res.get("clean"):
        return answer, "🧪 EPC sanity: clean", None
    n = len([ln for ln in res["notes"].splitlines() if ln.strip()])
    return res["answer"], f"🧪 EPC sanity: {n} correction(s)", res["notes"]


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


# ---------- ✎ answer-format instructions (editable in place, no upload) ----------

def _answer_format_or_404(key: str) -> dict:
    f = claude_bridge._FORMAT_BY_KEY.get(key)
    if not f or not f.get("instruction"):
        raise HTTPException(404, f"'{key}' is not an editable answer format")
    return f


@app.get("/api/answer-format/{key}")
def answer_format_get(key: str):
    """The ACTIVE instruction text of a 📐 preset (user override if saved, else
    the built-in default) — what the ✎ editor shows."""
    f = _answer_format_or_404(key)
    override = claude_bridge.format_override(key)
    return {"key": key, "label": f["label"],
            "text": override or f["instruction"],
            "default": f["instruction"], "overridden": bool(override)}


@app.put("/api/answer-format/{key}")
def answer_format_put(key: str, body: schemas.AnswerFormatEdit):
    """Save an edited instruction for this preset (kept in the data volume,
    shared by ALL tabs). Empty text — or text identical to the built-in —
    resets back to the default."""
    f = _answer_format_or_404(key)
    text = body.text.strip()
    path = os.path.join(claude_bridge.FMT_OVERRIDE_DIR, f"{key}.txt")
    if not text or text == f["instruction"].strip():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:
        os.makedirs(claude_bridge.FMT_OVERRIDE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return answer_format_get(key)


# ---------- 🖋 house style: the ONE global formatting space ----------

@app.get("/api/house-style")
def house_style_get():
    """The ACTIVE global style rules injected into EVERY answer (chat, deep-compare
    ranking, ⚖️ problem-solution) — what the 🖋 editor shows."""
    override_path = os.path.join(claude_bridge.FMT_OVERRIDE_DIR,
                                 claude_bridge.HOUSE_STYLE_FILE)
    overridden = os.path.exists(override_path)
    return {"text": claude_bridge.house_style(),
            "default": claude_bridge.HOUSE_STYLE_DEFAULT, "overridden": overridden}


@app.put("/api/house-style")
def house_style_put(body: schemas.AnswerFormatEdit):
    """Save the edited global style (data volume, shared by ALL tabs and ALL answer
    paths). Empty text — or text identical to the built-in — resets to the default."""
    text = body.text.strip()
    path = os.path.join(claude_bridge.FMT_OVERRIDE_DIR, claude_bridge.HOUSE_STYLE_FILE)
    if not text or text == claude_bridge.HOUSE_STYLE_DEFAULT.strip():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:
        os.makedirs(claude_bridge.FMT_OVERRIDE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return house_style_get()


# ---------- 📄 TET: the global technical effect template ----------

@app.get("/api/tet")
def tet_get():
    """The ACTIVE technical effect template — the user's pasted example that the
    📐 'Technical effect argumentation' format adapts to the chosen documents."""
    override_path = os.path.join(claude_bridge.FMT_OVERRIDE_DIR,
                                 claude_bridge.TET_FILE)
    return {"text": claude_bridge.tet_template(),
            "default": claude_bridge.TET_DEFAULT,
            "overridden": os.path.exists(override_path)}


@app.put("/api/tet")
def tet_put(body: schemas.AnswerFormatEdit):
    """Save the pasted example (data volume, shared by ALL tabs). Empty text —
    or text identical to the built-in skeleton — resets to the skeleton."""
    text = body.text.strip()
    path = os.path.join(claude_bridge.FMT_OVERRIDE_DIR, claude_bridge.TET_FILE)
    if not text or text == claude_bridge.TET_DEFAULT.strip():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:
        os.makedirs(claude_bridge.FMT_OVERRIDE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return tet_get()


# ---------- 📄 TET supporting documents (per tab: amended claims, arguments …) ----------

def _tet_kind_or_400(kind: str) -> str:
    if kind not in claude_bridge.TET_DOC_KINDS:
        raise HTTPException(400, f"unknown TET document kind '{kind}' — one of: "
                                 + ", ".join(claude_bridge.TET_DOC_KINDS))
    return kind


@app.get("/api/tabs/{tab_id}/tet-docs")
def tet_docs_list(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_tet_docs(tab_id)
    # A container rebuild kills an in-flight OCR thread and leaves its row
    # 'pending' forever — flip long-dead ones to an actionable error.
    for d in docs:
        if d["status"] == "pending" and time.time() - (d["added_at"] or 0) > 7200:
            db.update_tet_doc(d["id"], status="error",
                              error="OCR interrupted (app restarted) — delete and re-upload")
            d["status"], d["error"] = "error", "OCR interrupted (app restarted) — delete and re-upload"
    return {"docs": docs, "kinds": claude_bridge.TET_DOC_KINDS}


def _transcribe_tet_doc(doc_id: int, pdf_path: str) -> None:
    """Background: a SCANNED (image-only) TET supporting PDF → page PNGs
    (pdftoppm) → vision transcription per page (same engine as the benchmark
    photo pages and ⚖️ PSA scans) → the tet_doc row's text. Progress/errors are
    written to the row so the 📄 manager can poll them."""
    pages_dir = pdf_path + "-pages"
    shutil.rmtree(pages_dir, ignore_errors=True)
    os.makedirs(pages_dir, exist_ok=True)
    try:
        try:
            subprocess.run(["pdftoppm", "-r", "150", "-png", pdf_path,
                            os.path.join(pages_dir, "pg")],
                           check=True, timeout=180, capture_output=True)
        except (subprocess.SubprocessError, OSError) as e:
            db.update_tet_doc(doc_id, status="error", error=f"pdftoppm failed: {e}")
            return
        pages = sorted(os.listdir(pages_dir))
        if not pages:
            db.update_tet_doc(doc_id, status="error",
                              error="the PDF produced no page images")
            return
        db.update_tet_doc(doc_id, progress=f"0/{len(pages)}")
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
                db.update_tet_doc(doc_id, progress=f"{done}/{len(pages)}")
        text = "\n\n".join(f"— page {i + 1} —\n{t.strip()}"
                           for i, t in enumerate(texts) if t.strip()).strip()
        if len(text) < 50:
            db.update_tet_doc(doc_id, status="error",
                              error="vision transcription yielded almost no text")
            return
        db.update_tet_doc(doc_id, text=text, status="ready", progress=None, error=None)
    finally:
        shutil.rmtree(pages_dir, ignore_errors=True)
        try:
            os.remove(pdf_path)
        except OSError:
            pass


@app.post("/api/tabs/{tab_id}/tet-docs")
async def tet_doc_upload(tab_id: int, bg: BackgroundTasks, kind: str = Form(...),
                         file: UploadFile = File(...)):
    """Upload a TET supporting document (PDF, TXT or MD) into THIS tab. A
    scanned image-only PDF is vision-OCR'd page by page in the background — the
    row appears immediately as ⏳ pending and flips to ready when transcribed."""
    _tab_or_404(tab_id)
    _tet_kind_or_400(kind)
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "too large (25 MB max)")
    name = os.path.basename(file.filename or kind)
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        os.makedirs(UPLOADS, exist_ok=True)
        tmp = os.path.join(UPLOADS, f"tet-{uuid.uuid4().hex}.pdf")
        with open(tmp, "wb") as fh:
            fh.write(data)
        res = extract.text_from_pdf(tmp)
        text = (res.get("text") or "").strip()
        if "error" in res or len(text) < 50:
            # scanned image-only PDF — no text layer. Same answer as the ⚖️ PSA
            # and benchmark photo paths: vision-transcribe in the background.
            doc = db.add_tet_doc(tab_id, kind, name, "", status="pending")
            bg.add_task(_transcribe_tet_doc, doc["id"], tmp)
            return {**doc, "pending": True}
        try:
            os.remove(tmp)
        except OSError:
            pass
    elif ext in (".txt", ".md"):
        text = data.decode("utf-8", errors="replace").strip()
    else:
        raise HTTPException(400, f"{name}: only PDF, TXT or MD accepted")
    if len(text) < 20:
        raise HTTPException(400, "the document yielded almost no text")
    return db.add_tet_doc(tab_id, kind, name, text)


@app.post("/api/tabs/{tab_id}/tet-docs/text")
def tet_doc_paste(tab_id: int, body: schemas.TetDocText):
    """Add a TET supporting document from pasted text."""
    _tab_or_404(tab_id)
    _tet_kind_or_400(body.kind)
    name = (body.name or "").strip() \
        or f"{claude_bridge.TET_DOC_KINDS[body.kind]} (pasted)"
    return db.add_tet_doc(tab_id, body.kind, name[:200], body.text.strip())


# NOTE: registered BEFORE /tet-docs/{doc_id} so the literal path wins routing.
@app.get("/api/tabs/{tab_id}/tet-docs/citations")
def tet_doc_citations(tab_id: int):
    """Patent numbers cited in the tab's READY initial search report(s), with
    their D-labels where the report names them — offered in the 📐 roles popup
    as direct D1/D2 picks. The application's own (benchmark) number is excluded."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id, full=False) or {}
    own = patents.normalize(bm.get("number") or "") or None
    labeled: dict[str, str] = {}
    order: list[str] = []
    dnum_re = re.compile(r"\bD(\d{1,2})\b[\s:=–—-]{0,3}" + patents.NUMBER_RE.pattern)
    for doc in db.list_tet_docs(tab_id, full=True, ready_only=True):
        if doc["kind"] != "search-report":
            continue
        text = doc.get("text") or ""
        for m in dnum_re.finditer(text):
            n = patents.normalize(m.group(2))
            if patents.is_plausible(n):
                labeled.setdefault(n, f"D{m.group(1)}")
        for m in patents.NUMBER_RE.finditer(text):
            n = patents.normalize(m.group(1))
            if patents.is_plausible(n) and n != own and n not in order:
                order.append(n)
    pos = {n: i for i, n in enumerate(order)}
    cits = [{"number": n, "label": labeled.get(n)} for n in order[:30]]
    cits.sort(key=lambda c: (c["label"] is None,
                             int(c["label"][1:]) if c["label"] else pos[c["number"]]))
    return {"citations": cits}


@app.get("/api/tabs/{tab_id}/tet-docs/{doc_id}")
def tet_doc_get(tab_id: int, doc_id: int):
    _tab_or_404(tab_id)
    doc = db.get_tet_doc(tab_id, doc_id)
    if not doc:
        raise HTTPException(404, "TET document not found")
    return doc


@app.delete("/api/tabs/{tab_id}/tet-docs/{doc_id}")
def tet_doc_delete(tab_id: int, doc_id: int):
    _tab_or_404(tab_id)
    if not db.delete_tet_doc(tab_id, doc_id):
        raise HTTPException(404, "TET document not found")
    return {"ok": True}


@app.post("/api/tabs/{tab_id}/tet-123check")
def tet_123_check_run(tab_id: int, body: schemas.Tet123Request):
    """⚖ Art. 123(2) check: diff the amended set of claims against the initial
    one, then verify EVERY amended feature has a direct and unambiguous basis in
    the application as filed (initial claims + initial description). Uses the
    tab's READY TET documents; the analysis lands in the tab's chat."""
    _tab_or_404(tab_id)
    by: dict[str, list[dict]] = {}
    for d in db.list_tet_docs(tab_id, full=True, ready_only=True):
        by.setdefault(d["kind"], []).append(d)
    amended = by.get("amended-claims") or []
    init_cl = by.get("initial-claims") or []
    init_de = by.get("initial-description") or []
    if not amended:
        raise HTTPException(400, "no ready «Amended set of claims» among this tab's "
                                 "TET documents — upload it first (📄 TET)")
    if not (init_cl or init_de):
        raise HTTPException(400, "nothing filed to check against — upload the "
                                 "«Initial set of claims» and/or «Initial "
                                 "description» (application as filed) first (📄 TET)")
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    res = claude_bridge.tet_123_check(amended, init_cl, init_de, model=model)
    out = []
    if "error" in res:
        out.append(db.append_message(tab_id, "s", f"Claude error: {res['error']}"))
        return {"messages": out, "error": res["error"]}
    participants = [{"kind": "model", "title": model},
                    {"kind": "psa", "title": "⚖ Art. 123(2) check"}]
    participants += [{"kind": "documents",
                      "title": f"{claude_bridge.TET_DOC_KINDS[k]}: "
                               + ", ".join(d["name"] for d in v)}
                     for k, v in (("amended-claims", amended),
                                  ("initial-claims", init_cl),
                                  ("initial-description", init_de)) if v]
    # 🧪 EPC sanity pass BEFORE storing/showing — the stored basis must already
    # be methodology-clean (verbatim basis, no invented citations).
    answer, chip, notes = _epc_sanitized(res["answer"], model)
    if chip:
        participants.append({"kind": "psa", "title": chip})
    # 💾 persist the result as a system TET document (latest run wins) — it then
    # rides along on every chat answer, so «Build argumentation» reuses the
    # established Basis-for-amendments instead of re-running the check, and the
    # basis survives history clipping/scrolling.
    for d in db.list_tet_docs(tab_id):
        if d["kind"] == "123-check":
            db.delete_tet_doc(tab_id, d["id"])
    db.add_tet_doc(tab_id, "123-check",
                   f"⚖ 123(2) check ({model.replace('claude-', '')})",
                   answer)
    out.append(db.append_message(tab_id, "c", answer,
                                 model=model, participants=participants))
    if notes:
        out.append(db.append_message(
            tab_id, "s", "🧪 EPC sanity check corrected the answer above:\n" + notes))
    return {"messages": out}


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


def _attach_ranks(tab_id: int, docs: list[dict]) -> None:
    """Attach the unified Must-dominant `rank` — THE key the matrix's '① best' uses —
    to a documents payload. EVERY endpoint that returns the documents list must go
    through here: on 2026-07-27 the /documents refresh path (used after deep reads)
    returned docs WITHOUT `rank`, so the palmares silently lost its 🎯 sort, fell back
    to the ⚖ blended sort, and showed a different #1 than the matrix."""
    bm = db.get_benchmark(tab_id)
    elements = _combi_elements(bm)
    if not elements:
        return
    bnorm = _norm_num((bm or {}).get("number"))
    full_by_id = {d["id"]: d for d in db.list_documents(tab_id, full=True)}
    for d in docs:
        if bnorm and _norm_num(d.get("number")) == bnorm:
            d["rank"] = None            # the benchmark itself is not a candidate
            continue
        src = full_by_id.get(d["id"])
        u = _unified_score(elements, src) if src else None
        d["rank"] = u if (u and u["assessed"]) else None


@app.get("/api/tabs/{tab_id}/state")
def tab_state(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_documents(tab_id)
    _attach_ranks(tab_id, docs)
    for d in docs:
        d["links"] = _doc_links(d)
    return {"benchmark": _benchmark_view(tab_id),
            "documents": docs,
            "messages": db.list_messages(tab_id),
            "notebook": db.get_notebook_config(tab_id),
            "nlm_profile": _tab_profile(tab_id),
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
    vision_used = False                        # any page photo OR scanned-PDF fallback
    db.update_benchmark(tab_id, progress=f"0/{total}")

    def one(f: dict) -> tuple[dict, dict]:
        nonlocal done, vision_used
        if f["kind"] == "pdf":
            res = extract.text_from_pdf(f["path"])
            if "error" in res and "no extractable text" in res["error"]:
                # SCANNED (image-only) PDF — same vision fallback the ⚖️ PSA upload
                # got on 07-22; the benchmark path used to hard-fail here with
                # "upload the pages as pictures instead" (bit: amended_478.pdf).
                with lock:
                    vision_used = True
                res = extract.text_from_scanned_pdf(
                    f["path"], model=model, workers=TRANSCRIBE_WORKERS,
                    progress=lambda p, n: db.update_benchmark(
                        tab_id, progress=f"{f['name']}: page {p}/{n} (vision OCR)"))
        else:
            with lock:
                vision_used = True
            res = extract.text_from_image(f["path"], model=model)
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
    # a model was involved only for page photos / scanned-PDF vision fallback
    had_image = vision_used
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
                 "kind": (f.kind if f.kind in ("M", "A", "W") else "M"), "sl": f.sl}
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
                     "kind": (body.kind if body.kind in ("M", "A", "W") else "M"), "sl": body.sl})
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
    text_kind = "M"                # kind to tag the text-derived elements with
    claims_mode = False            # claim-structure-aware: claim 1 → M, dependent claims → A
    if body.source == "text":
        text = (body.text or "").strip()
        if len(text) < 20:
            raise HTTPException(400, "paste the claim text to decompose")
        claims_mode = True
    elif body.source == "additional":
        # Split ONLY the additional features; mandatory elements are already granular and
        # re-cutting them would discard wording the user reviewed and accepted.
        add_feats = [f for f in feats if _kind(f) == "A"]
        if not add_feats:
            raise HTTPException(400, "this benchmark has no additional features to decompose")
        text = ""
        keep = [f for f in feats if _kind(f) != "A"]     # keep M and W untouched
    elif body.source == "features":
        if not feats:
            raise HTTPException(400, "this benchmark has no features to decompose")
        text = "\n\n".join(f["name"] for f in feats if _kind(f) == "M")
        # ADDITIONAL features are monolithic for the same reason the claim was, and they are
        # what differentiates the documents that all cover the mandatory elements — so split
        # them too. Each is done separately so its own stretch level rides onto its elements.
        add_feats = [f for f in feats if _kind(f) == "A"]
        keep = [f for f in feats if _kind(f) == "W"]     # whole-doc bonus stays as-is
        if not text.strip() and not add_feats:
            raise HTTPException(400, "no features to decompose")
    elif body.source == "whole":
        # WHOLE-DOCUMENT features: decompose the BENCHMARK DOCUMENT's own claims into elements
        # and tag them W — a bonus pool that boosts (never gates) a candidate's ranking.
        if not bm:
            raise HTTPException(400, "set a benchmark first")
        text = (bm.get("claims") or bm.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "the benchmark document has no claims/text to decompose")
        text_kind = "W"
    else:                       # the benchmark document's own claims: claim 1 → M, deps → A
        if not bm:
            raise HTTPException(400, "set a benchmark first")
        text = (bm.get("claims") or bm.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "the benchmark has no claims/text to decompose")
        claims_mode = True
    model = _read_model(body.model)
    elements: list[dict] = list(keep)
    models: list[str] = []
    if text.strip():
        res = claude_bridge.decompose_claim(text, model=model, claims=claims_mode)
        if "error" in res:
            raise HTTPException(400, f"decomposition failed: {res['error']}")
        # W elements carry a generous default stretch (bonus reading), like additional ones.
        new_els = res["elements"]
        if text_kind == "W":
            new_els = [{**e, "kind": "W", "sl": int(e.get("sl", 5) or 5)} for e in new_els]
        elements += new_els
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
            "mandatory": len([e for e in elements if _kind(e) == "M"]),
            "additional": len([e for e in elements if _kind(e) == "A"]),
            "whole": len([e for e in elements if _kind(e) == "W"])}


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
    res = nlm_bridge.add_source_text(nb, title, _doc_source_text(doc),
                                     profile=_tab_profile(doc["tab_id"]))
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
    res = nlm_bridge.add_source_text(nb, title, _benchmark_fulltext(bm),
                                     profile=_tab_profile(tab_id))
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


def _tab_profile(tab_id: int) -> str | None:
    """The tab's pinned NLM account (auth profile); None = default account. Every
    tab-scoped nlm_bridge call goes through this so a tab's whole NLM life —
    notebooks, sources, queries, screens — stays inside ONE Google account."""
    try:
        return db.get_tab_nlm_profile(tab_id)
    except Exception:
        return None


def _rollover_title(title: str | None) -> str:
    """Next notebook in a series: 'X' -> 'X (2)', 'X (2)' -> 'X (3)'."""
    title = title or "Patent candidates"
    m = re.search(r" \((\d+)\)$", title)
    if m:
        return re.sub(r" \(\d+\)$", f" ({int(m.group(1)) + 1})", title)
    return f"{title} (2)"


def _create_and_connect(tab_id: int, title: str) -> dict | None:
    res = nlm_bridge.create_notebook(title, profile=_tab_profile(tab_id))
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


def _process_documents(doc_ids: list[int], model: str | None = None,
                       read_figures: bool | None = None,
                       digest: bool | None = None) -> None:
    """Background pipeline for a batch: fetch each (throttled by the fetcher's
    own gap — a token-free scrape), optionally caption figures / digest, then
    mirror them into the connected NotebookLM notebook (auto-creating it on first
    use) — so the notebook stays a Claude-quota-independent fallback brain."""
    for doc_id in doc_ids:
        _fetch_into_db(doc_id)
    # Caption drawings BEFORE the digest so the digest (and every later read) is
    # figure-aware. Captioning is vision-per-sheet — a claude session per sheet, the
    # single most expensive intake step — so it's opt-in per add (UI checkbox),
    # falling back to the deploy-level PB_AUTO_FIGURES default.
    if AUTO_FIGURES if read_figures is None else read_figures:
        with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
            list(ex.map(lambda i: _process_figures(i, model), doc_ids))
    # Digest is one model call per document — opt-in exactly like figures, so a
    # plain "add these numbers" costs zero tokens; 🔁 digest-backfill recovers
    # the digests later when a digest-based tool actually needs them.
    if AUTO_DIGEST if digest is None else digest:
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
        bg.add_task(_process_documents, to_fetch, _read_model(body.reading_model),
                    body.read_figures, body.digest)
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
    _attach_ranks(tab_id, docs)     # same rank as /state — see _attach_ranks docstring
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


# ---------- 🔁 stuck-pending recovery ----------
# _process_documents runs as FastAPI BackgroundTasks / a request-scoped thread — a
# container restart kills it silently and its docs sit ⏳ pending forever (bit live:
# 656 tab-11 docs after a rebuild; the workaround was a hand-rolled drain script).
# Now: a bulk per-tab endpoint + a boot-time sweep re-queue them. Reuse-held
# pendings (a fetched copy exists in another tab → the UI must ASK reuse-vs-refetch)
# are pending BY DESIGN and are never auto-fetched.
AUTO_REFETCH = os.environ.get("PB_AUTO_REFETCH", "1") not in ("0", "", "false", "no")
STALE_PENDING_S = float(os.environ.get("PB_STALE_PENDING_MIN", "15")) * 60


def _pending_stale_docs(tab_id: int) -> list[dict]:
    """A tab's status='pending' docs older than the staleness window, excluding
    reuse-held ones."""
    now = db._now()
    out = []
    for d in db.list_documents(tab_id):
        if d["status"] != "pending":
            continue
        if now - (d.get("added_at") or 0) < STALE_PENDING_S:
            continue                     # a live add may still be working on it
        if db.find_reusable_by_number(d["number"], exclude_tab_id=tab_id):
            continue                     # held back for the reuse question, not lost
        out.append(d)
    return out


@app.post("/api/tabs/{tab_id}/documents/refetch-pending")
def documents_refetch_pending(tab_id: int):
    """Bulk re-queue every stuck-pending document of this tab (fetch is a token-free
    scrape; figures/digest follow the deploy defaults, i.e. off)."""
    _tab_or_404(tab_id)
    docs = _pending_stale_docs(tab_id)
    if not docs:
        return {"requeued": 0, "numbers": []}
    ids = [d["id"] for d in docs]
    for i in ids:
        db.update_document(i, error=None)
    threading.Thread(target=_process_documents, args=(ids,), daemon=True).start()
    db.append_message(tab_id, "s",
                      f"🔁 Re-queued {len(ids)} stuck pending fetch(es) — they were "
                      "orphaned by a restart. Fetching now (token-free).")
    return {"requeued": len(ids), "numbers": [d["number"] for d in docs][:50]}


def _auto_refetch_sweep() -> None:
    """Boot-time sweep: after the container settles, re-queue every tab's stuck
    pendings. O_EXCL lock so only ONE gunicorn worker sweeps."""
    time.sleep(float(os.environ.get("PB_AUTO_REFETCH_DELAY", "60")))
    lock = os.path.join(os.path.dirname(db.DB_PATH) or ".", ".auto_refetch.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except (FileExistsError, OSError):
        return
    try:
        for t in db.list_tabs():
            docs = _pending_stale_docs(t["id"])
            if not docs:
                continue
            ids = [d["id"] for d in docs]
            for i in ids:
                db.update_document(i, error=None)
            db.append_message(t["id"], "s",
                              f"🔁 Auto-resume after restart: {len(ids)} pending "
                              "fetch(es) had been orphaned — re-queued (token-free "
                              "fetch; reuse-held documents untouched).")
            _process_documents(ids)      # serial per tab — the fetcher throttles itself
    except Exception:
        pass                             # a failed sweep must never take the app down


# ---------- upload (photos / PDF / txt → candidate numbers) ----------

UPLOAD_FILE_WORKERS = int(os.environ.get("PB_UPLOAD_WORKERS", "3"))
MAX_UPLOAD_FILES = int(os.environ.get("PB_MAX_UPLOAD_FILES", "40"))


def _extract_one(f: dict, ocr_model: str) -> tuple[str, dict]:
    """Pull patent numbers out of ONE saved file by its kind."""
    ext = f["ext"]
    if ext in IMAGE_EXT:
        return "image", extract.numbers_from_image(f["path"], ocr_model)
    if ext == ".pdf":
        return "pdf", extract.numbers_from_pdf(f["path"], name=f["name"])
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
def notebooks(force: bool = False, profile: str | None = None):
    """Notebooks of ONE account: the default one, or `?profile=` (the UI passes the
    active tab's pinned account so the picker lists the right notebooks)."""
    return nlm_bridge.list_notebooks(force=force, profile=profile)


@app.get("/api/sources")
def sources(notebook_id: str, force: bool = False, profile: str | None = None):
    return nlm_bridge.list_sources(notebook_id, force=force, profile=profile)


@app.get("/api/nlm/profiles")
def nlm_profiles():
    """Auth profiles (Google accounts) available to bind tabs to: [{name, authed}].
    Seed a new one with scripts/reseed-nlm-profile.sh <name> from the dev container."""
    return {"profiles": nlm_bridge.list_profiles(), "default": nlm_bridge.DEFAULT_PROFILE}


def _tab_nlm_in_use(tab_id: int) -> str | None:
    """Why the tab's NLM account can no longer be changed (None = still free).
    STICKY rule: once any NLM artifact exists under the current account, switching
    would orphan it (notebooks are not portable between Google accounts)."""
    cfg = db.get_notebook_config(tab_id)
    if cfg and cfg.get("notebook_id"):
        return "a notebook is connected to this tab"
    if _screen_read(tab_id):
        return "a mega-screen (state) exists for this tab"
    for d in db.list_documents(tab_id):
        if d.get("nlm_source_notebook"):
            return "documents are already mirrored into a notebook"
    bm = db.get_benchmark(tab_id)
    if bm and bm.get("nlm_source_notebook"):
        return "the benchmark is already mirrored into a notebook"
    return None


@app.get("/api/tabs/{tab_id}/nlm-profile")
def tab_nlm_profile_get(tab_id: int):
    _tab_or_404(tab_id)
    locked_why = _tab_nlm_in_use(tab_id)
    return {"profile": _tab_profile(tab_id), "locked": bool(locked_why),
            "locked_why": locked_why}


@app.put("/api/tabs/{tab_id}/nlm-profile")
def tab_nlm_profile_set(tab_id: int, body: schemas.TabNlmProfile):
    """Pin the tab to an NLM account (auth profile). Allowed only while the tab has
    no NLM artifacts — once chosen and used, it stays (notebooks don't move between
    accounts). Clearing back to default follows the same rule."""
    _tab_or_404(tab_id)
    prof = (body.profile or "").strip() or None
    if prof == nlm_bridge.DEFAULT_PROFILE:
        prof = None
    if prof == _tab_profile(tab_id):
        return {"ok": True, "profile": prof}
    locked_why = _tab_nlm_in_use(tab_id)
    if locked_why:
        raise HTTPException(409, f"this tab's NLM account is locked — {locked_why}. "
                            "Disconnect/remove the NLM artifacts first (or use a new tab).")
    if prof and not any(p["name"] == prof and p["authed"]
                        for p in nlm_bridge.list_profiles()):
        raise HTTPException(400, f"profile '{prof}' is not seeded/authenticated — run "
                            "scripts/reseed-nlm-profile.sh " + prof + " first")
    db.set_tab_nlm_profile(tab_id, prof)
    return {"ok": True, "profile": prof}


@app.delete("/api/notebooks/{notebook_id}")
def notebook_delete_account(notebook_id: str, profile: str | None = None):
    """Delete a notebook permanently from the NotebookLM account (frees a slot toward
    the ~100-notebook cap). Any tab connected to it is disconnected so it doesn't try
    to query a notebook that no longer exists."""
    res = nlm_bridge.delete_notebook(notebook_id, profile=profile)
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
    prof = _tab_profile(tab_id)
    listing = nlm_bridge.list_sources(nb_id, force=True, profile=prof)
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
        content = nlm_bridge.source_content(sid, profile=prof)
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
    res = nlm_bridge.create_notebook(body.title, profile=_tab_profile(tab_id))
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
              for n in (nlm_bridge.list_notebooks(profile=_tab_profile(tab_id))
                        .get("notebooks") or [])}
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
    prof = _tab_profile(tab_id)
    ok, why = nlm_bridge.available(prof)
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    if body.scan_all:
        nb_ids = [n["id"] for n in (nlm_bridge.list_notebooks(profile=prof)
                                    .get("notebooks") or [])]
    elif body.notebook_ids:
        nb_ids = list(dict.fromkeys(body.notebook_ids))
    else:
        nb_ids = db.tab_notebook_ids(tab_id)
    account = {n["id"]: n["title"]
               for n in (nlm_bridge.list_notebooks(profile=prof).get("notebooks") or [])}
    titles = dict(account)
    cands = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    by_key = {}
    for d in cands:                                   # first candidate per key wins
        by_key.setdefault(_shortlist_key(d["number"]), d)
    # key → list of {notebook_id, source_id} where a matching source physically exists
    locations: dict[str, list[dict]] = {}
    scan_errors, scanned_ok = [], set()
    for nb in nb_ids:
        res = nlm_bridge.list_sources(nb, force=True, profile=prof)
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


def _notebook_free(nb_id: str, profile: str | None = None) -> int:
    """Free source slots in a notebook right now (SOURCE_LIMIT − live source count)."""
    res = nlm_bridge.list_sources(nb_id, force=True, profile=profile)
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
    prof = _tab_profile(tab_id)
    ok, why = nlm_bridge.available(prof)
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
              for n in (nlm_bridge.list_notebooks(profile=prof).get("notebooks") or [])}
    if body.notebook_ids:
        nb_ids = [n for n in dict.fromkeys(body.notebook_ids) if n in titles]
    else:                                          # tab's notebooks that have room, most-free first
        nb_ids = sorted((n for n in db.tab_notebook_ids(tab_id)),
                        key=lambda n: _notebook_free(n, prof), reverse=True)
        nb_ids = [n for n in nb_ids if _notebook_free(n, prof) > 0]
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
    res = nlm_bridge.delete_source(body.source_ids, notebook_id=body.notebook_id,
                                   profile=_tab_profile(tab_id))
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
    ok, why = nlm_bridge.available(_tab_profile(tab_id))
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
    created = nlm_bridge.create_notebook(body.title, profile=_tab_profile(tab_id))
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
                    if nlm_bridge.delete_notebook(old, profile=_tab_profile(tab_id)).get("ok"):
                        cleaned += 1
                    db.clear_nlm_refs(tab_id, old)
                    db.nlm_cache_clear(old)
                _pipeline_set(tab_id, status_text="🧺 creating notebook & copying finalists…")
                created = nlm_bridge.create_notebook(st["title"], profile=_tab_profile(tab_id))
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
            rd = nlm_bridge.wait_sources_ready(nb, timeout=PIPELINE_INGEST_TIMEOUT,
                                               profile=_tab_profile(tab_id))
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
    ok, why = nlm_bridge.available(_tab_profile(tab_id))
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


def _notebook_signature(notebook_id: str, profile: str | None = None) -> str:
    """A short fingerprint of a notebook's current source SET — so a cached answer is
    reused only while the sources are unchanged, and auto-misses once a source is
    added/removed (the answer would otherwise be stale)."""
    srcs = nlm_bridge.list_sources(notebook_id, profile=profile).get("sources") or []
    ids = ",".join(sorted(s["id"] for s in srcs))
    return hashlib.sha256(ids.encode()).hexdigest()[:16]


def _nlm_query_cached(notebook_id: str, question: str,
                      source_ids: list[str] | None = None, force: bool = False,
                      accept=None, retries: int = 0,
                      profile: str | None = None) -> dict:
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
    sig = _notebook_signature(notebook_id, profile)
    raw = "|".join([notebook_id, ",".join(source_ids or []), sig, question])
    key = hashlib.sha256(raw.encode()).hexdigest()
    if not force:
        hit = db.nlm_cache_get(key)
        if hit is not None and (accept is None or accept(hit)):
            return {"answer": hit, "cached": True}
    res = {}
    for attempt in range(retries + 1):
        res = nlm_bridge.query(notebook_id, question, source_ids=source_ids,
                               profile=profile)
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
    prof = _tab_profile(tab_id)
    titles = {n["id"]: n["title"]
              for n in (nlm_bridge.list_notebooks(profile=prof).get("notebooks") or [])}
    out: list[dict] = []
    for nb in db.tab_notebook_ids(tab_id):
        sids = (cfg.get("selected_source_ids") or None) if nb == connected else None
        res = _nlm_query_cached(nb, question, source_ids=sids, accept=accept,
                                retries=retries, profile=prof)
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


@app.get("/api/psa/{kind}/text")
def psa_doc_text_get(kind: str):
    """The ACTIVE text of a ⚖️ document, for in-place ✎ editing. `format` has a
    built-in default (the user's 6-step problem-solution chain) that applies when
    nothing is uploaded; `method` has none (the methodology must be supplied)."""
    _kind_or_404(kind)
    default = claude_bridge.PSA_FORMAT_DEFAULT if kind == "format" else ""
    m = _psa_doc(kind)
    text = ""
    if m:
        try:
            with open(os.path.join(PSA_DIR, f"{kind}.txt"), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            pass
    return {"kind": kind, "name": (m or {}).get("name"),
            "text": text or default, "default": default,
            "overridden": bool(text.strip())}


@app.put("/api/psa/{kind}/text")
def psa_doc_text_put(kind: str, body: schemas.AnswerFormatEdit):
    """Save an in-place edit of a ⚖️ document — no re-upload needed. Empty text (or,
    for `format`, text identical to the built-in default) removes the stored doc;
    `format` then falls back to the built-in chain, `method` becomes unset."""
    _kind_or_404(kind)
    text = body.text.strip()
    default = claude_bridge.PSA_FORMAT_DEFAULT if kind == "format" else ""
    if not text or (default and text == default.strip()):
        for stale in (f"{kind}.txt", f"{kind}.json"):
            try:
                os.remove(os.path.join(PSA_DIR, stale))
            except FileNotFoundError:
                pass
        return psa_doc_text_get(kind)
    os.makedirs(PSA_DIR, exist_ok=True)
    with open(os.path.join(PSA_DIR, f"{kind}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    prev = _psa_doc(kind)
    meta = {"name": (prev or {}).get("name") or f"{kind} (edited in app)",
            "chars": len(text), "uploaded_at": int(time.time())}
    with open(os.path.join(PSA_DIR, f"{kind}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    return psa_doc_text_get(kind)


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
    # 🧪 EPC sanity pass: methodology violations (problem-with-solution,
    # hindsight, single-reference novelty) are repaired before display.
    answer, chip, notes = _epc_sanitized(res["answer"], model)
    if chip:
        participants.append({"kind": "psa", "title": chip})
    out.append(db.append_message(tab_id, "c", _verify_citations(tab_id, answer),
                                 model=model, participants=participants))
    if notes:
        out.append(db.append_message(
            tab_id, "s", "🧪 EPC sanity check corrected the answer above:\n" + notes))
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

    # 📄 the tab's TET supporting documents (amended claims, applicant
    # arguments, ESOP, as-filed texts) ride along on EVERY chat answer — a plain
    # question like "build on the ESOP and the amended claims" must see them
    # without requiring the 📐 tech-effect preset (bit 2026-08-03: the format
    # gate made exactly that question run blind).
    tet_docs = db.list_tet_docs(tab_id, full=True, ready_only=True) or None
    if tet_docs:
        participants.append({"kind": "documents",
                             "title": f"{len(tet_docs)} TET supporting doc(s)"})
    res = claude_bridge.chat(body.question, history=history, documents=documents,
                             sources=nlm_sources, skills=skill_blocks, model=model,
                             benchmark=benchmark, focus=focus, full=body.full,
                             answer_format=body.answer_format, xrefs=xrefs,
                             other_docs=other_docs, coverage=coverage,
                             discussions=discussions, tet_docs=tet_docs)
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
    # 🧪 EPC sanity pass on argumentation answers (tech-effect format only —
    # ordinary chat turns are not methodology documents and skip the extra call)
    answer = res["answer"]
    if body.answer_format == "tech-effect":
        answer, chip, sanity_notes = _epc_sanitized(answer, model)
        if chip:
            participants.append({"kind": "psa", "title": chip})
    else:
        sanity_notes = None
    out_messages.append(db.append_message(tab_id, "c", _verify_citations(tab_id, answer),
                                          model=model, participants=participants))
    if sanity_notes:
        out_messages.append(db.append_message(
            tab_id, "s", "🧪 EPC sanity check corrected the answer above:\n" + sanity_notes))

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


def _notebook_source_index(nb: str, profile: str | None = None,
                           strict: bool = False) -> tuple[dict[str, str], str | None]:
    """({patent-number -> source_id}, benchmark_source_id|None) for a notebook,
    read from its source titles ('CN1234 — …' / '🎯 BENCHMARK — …'). Knowing the
    benchmark's source id lets the rating query stay TINY — we ground NotebookLM on
    the benchmark source instead of pasting the whole benchmark into every question.
    strict=True: a failed source LIST must not read as an EMPTY notebook — the 🔬
    screen's rotation acted on that empty index and tried to add the benchmark into
    a genuinely full notebook (tab 11 round 14, 2026-08-07). Retry once, then raise
    so the job errors resumably instead of misdiagnosing."""
    res = nlm_bridge.list_sources(nb, force=True, profile=profile)
    if strict and res.get("error"):
        time.sleep(5)
        res = nlm_bridge.list_sources(nb, force=True, profile=profile)
        if res.get("error"):
            raise RuntimeError(f"could not list notebook sources: {res['error']}")
    m: dict[str, str] = {}
    bm_sid = None
    for s in (res.get("sources") or []):
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
        prof = _tab_profile(tab_id)
        notebooks = {d["nlm_source_notebook"] for d in docs}
        indexes = {nb: _notebook_source_index(nb, prof) for nb in notebooks}
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
            res = nlm_bridge.query(nb, q, source_ids=sids, profile=prof)
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
    ok, why = nlm_bridge.available(_tab_profile(tab_id))
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
# Google rejects the WHOLE question past ~5-6k chars (verified live 2026-08-07: 6708 and
# 6000 → INVALID_ARGUMENT code 3 on tab 11, whose feature spec alone filled the 6k cap).
# So the spec's slice must budget for the template around it, not just for itself.
NLM_QUERY_SAFE_TOTAL = 5000


def _nlm_question(template: str, spec: str, *, spec_key: str = "benchmark", **kw) -> str:
    """Render an NLM question with the feature spec sliced so the TOTAL question stays
    under NLM_QUERY_SAFE_TOTAL — capping the spec alone let a long template push the
    total past the ceiling and Google refused the query."""
    room = NLM_QUERY_SAFE_TOTAL - len(template.format(**{spec_key: ""}, **kw))
    return template.format(**{spec_key: (spec or "")[:max(0, room)]}, **kw)


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
    prof = _tab_profile(tab_id)
    ok, why = nlm_bridge.available(prof)
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    cands = [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"]
    if not cands:
        raise HTTPException(400, "no fetched candidate documents to shortlist")
    spec = _benchmark_feature_spec_for_nlm(bm)        # weighted feature names → spec → summary
    question = (body.question or "").strip() or _nlm_question(NLM_SHORTLIST_PROMPT, spec)
    if body.notebook_id:
        # one consolidated notebook → a single global best/second-best across all of them
        titles = {n["id"]: n["title"]
                  for n in (nlm_bridge.list_notebooks(profile=prof).get("notebooks") or [])}
        qres = _nlm_query_cached(body.notebook_id, question, source_ids=None,
                                 accept=_shortlist_answer_complete, retries=1,
                                 profile=prof)
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


# ---------- 🔬 NLM mega-screen: free rotation tournament over a huge pool ----------
# Evaluate 500-1500+ candidates with ZERO Claude tokens: rotate the pool through ONE
# dedicated screening notebook (free tier = 50 sources) in rounds of
# [1 benchmark + ≤survivor_cap carry-forward survivors + batch of fresh docs], ask ONE
# holistic ranking question per round (the affordable quota mode — no --source-ids),
# and let survivors compete against every new batch (the moving yardstick that makes
# 39 separate rounds globally comparable). Every doc ever NAMED in a round's answer
# lands in a graduates ledger; a finalize round refills the notebook with the ledger's
# best and ONE rich shortlist query writes the global ranking into the existing
# shortlisted/nlm_rank columns. The screening notebook is NOT the tab's mirror — we
# never touch documents.nlm_source_notebook (that column drives tab_notebook_ids()
# fan-out and would dangle after rotation deletes).
# Lessons baked in: losers are deleted only AFTER an answer passed the structural
# guard and state was persisted (the DE202022102539 silent-drop); the round question
# carries only the ≤6k feature spec (INVALID_ARGUMENT ~5-7k ceiling); quota
# exhaustion (Q&A-scoped, resets 6-12h, source add/delete keep working) auto-pauses
# the job and a watchdog probes hourly, mirroring the ⏳ token-limit watchdog.

SCREEN_TTL = float(os.environ.get("PB_SCREEN_TTL", "1200"))       # secs before job looks dead
SCREEN_QUOTA_PROBE_EVERY = float(os.environ.get("PB_SCREEN_QUOTA_PROBE", str(60 * 60)))
SCREEN_QUOTA_GIVE_UP = 24 * 3600          # stop auto-probing after a day of exhaustion
_screen_watchdogs: dict[int, threading.Thread] = {}
_screen_watchdogs_mu = threading.Lock()

NLM_SCREEN_PROMPT = (
    "Across ALL the candidate source documents provided (ignore the 🎯 BENCHMARK "
    "source itself), rank the TOP {top} candidates that best disclose the TARGET "
    "FEATURE COMBINATION below — best first. Treat surface-form synonyms and implicit "
    "realisations (a document that physically does the step without the literal word) "
    "as disclosure. Reply in this order:\n"
    "TOP: a numbered list of the {top} best candidates, each by its publication number "
    "(e.g. EP4340163A1, CN117241689) with one short line on what it covers.\n"
    "NEAR-MISSES: any other candidate that discloses MOST of the elements, by "
    "publication number.\n"
    "Only name documents actually among the sources; do not invent publication numbers."
    "\n\n=== TARGET FEATURE COMBINATION ===\n{benchmark}"
)


def _screen_answer_complete(answer: str) -> bool:
    """The round answer must reach its ranked decision — a truncated 'thinking'
    preamble carries neither marker and must never cost the batch (its unnamed
    docs would be silently 'rejected')."""
    a = (answer or "").upper()
    return "TOP" in a or "NEAR-MISS" in a


def _screen_state_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".nlm_screen_{tab_id}.json")


def _screen_pause_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".nlm_screen_{tab_id}.pause")


def _screen_lock_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".", f".nlm_screen_{tab_id}.lock")


def _screen_read(tab_id: int) -> dict | None:
    try:
        with open(_screen_state_path(tab_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _screen_set(tab_id: int, **kw) -> dict:
    st = _screen_read(tab_id) or {}
    st.update(kw)
    with open(_screen_state_path(tab_id), "w") as f:
        json.dump(st, f)
    return st


def _screen_running(tab_id: int) -> bool:
    try:
        return (time.time() - os.path.getmtime(_screen_lock_path(tab_id))) < SCREEN_TTL
    except OSError:
        return False


def _screen_status(tab_id: int) -> dict:
    st = _screen_read(tab_id)
    if not st:
        return {"present": False, "phase": "idle", "running": False, "resumable": False}
    running = _screen_running(tab_id)
    quota = st.get("quota") or {}
    if st.get("step") == "done":
        phase = "done"
    elif quota.get("paused"):
        phase = "quota_paused"
    elif st.get("error"):
        phase = "error"
    elif os.path.exists(_screen_pause_path(tab_id)) and not running:
        phase = "paused"
    elif running:
        phase = "running"
    else:
        phase = "interrupted"
    return {"present": True, "phase": phase, "running": phase == "running",
            "resumable": phase in ("error", "interrupted", "paused", "quota_paused"),
            "round": st.get("round", 0), "screened": st.get("cursor", 0),
            "total": len(st.get("queue") or []),
            "survivors": len(st.get("survivors") or []),
            "graduates": len(st.get("ledger") or {}),
            "status_text": st.get("status_text", ""), "error": st.get("error"),
            "quota_resume_at": quota.get("resume_at"),
            "notebook_id": st.get("notebook_id"),
            "notebook_title": st.get("notebook_title")}


def _screen_heartbeat(tab_id: int) -> None:
    try:
        os.utime(_screen_lock_path(tab_id), None)
    except OSError:
        pass


def _screen_parse_ranked(answer: str, key_map: dict[str, int]) -> tuple[list[int], list[str]]:
    """Publication numbers in mention order (NLM's best-first ranking), matched
    kind-code-insensitively against this round's roster∪survivors. Numbers outside
    that set (hallucinations, ghosts of deleted sources) are reported, never ranked."""
    ordered, seen, unmatched = [], set(), []
    for n in patents.extract_candidates(answer or ""):
        did = key_map.get(_shortlist_key(n))
        if did is not None and did not in seen:
            seen.add(did)
            ordered.append(did)
        elif did is None and n not in unmatched:
            unmatched.append(n)
    return ordered, unmatched


def _screen_notebook(tab_id: int, st: dict) -> tuple[str, str]:
    """The dedicated screening notebook (find by exact title, else create). Kept
    OUT of the tab's notebook config / mirror columns on purpose."""
    nb, title = st.get("notebook_id"), st.get("notebook_title")
    prof = _tab_profile(tab_id)
    want = f"🔁 Screen — {_tab_name(tab_id)}"[:100]
    if nb:
        if any(n["id"] == nb for n in (nlm_bridge.list_notebooks(force=True, profile=prof)
                                       .get("notebooks") or [])):
            return nb, title or want
        db.append_message(tab_id, "s", "🔬 The screening notebook disappeared — recreating it; "
                          "survivors will be re-staged from the ledger.")
    for n in (nlm_bridge.list_notebooks(force=True, profile=prof).get("notebooks") or []):
        if n.get("title") == want:
            _screen_set(tab_id, notebook_id=n["id"], notebook_title=want)
            return n["id"], want
    created = nlm_bridge.create_notebook(want, profile=prof)
    if not created.get("id"):
        raise RuntimeError(created.get("error") or "screening notebook create failed")
    _screen_set(tab_id, notebook_id=created["id"], notebook_title=created["title"])
    return created["id"], created["title"]


def _screen_stage(tab_id: int, st: dict, want_ids: list[int],
                  docs_by_id: dict[int, dict]) -> tuple[str, str | None, dict[str, int], list[int]]:
    """Make the notebook hold EXACTLY benchmark + `want_ids` (idempotent: deletes
    stale candidate sources, adds missing ones, re-adds once, marks ghosts
    add_failed). Returns (nb, bm_sid, key_map of what's really in, failed_ids)."""
    nb, _ = _screen_notebook(tab_id, st)
    prof = _tab_profile(tab_id)
    want_keys = {_shortlist_key(docs_by_id[i]["number"]): i for i in want_ids
                 if docs_by_id.get(i)}

    def index() -> tuple[dict[str, str], str | None]:
        return _notebook_source_index(nb, prof, strict=True)

    num_map, bm_sid = index()
    # 1. delete candidate sources that are neither benchmark nor wanted (previous
    #    round's losers — safe HERE because the previous answer was accepted and
    #    persisted before this round began; a crash between rounds re-runs this).
    stale = [sid for num, sid in num_map.items() if _shortlist_key(num) not in want_keys]
    if stale:
        _screen_set(tab_id, status_text=f"🗑 rotating out {len(stale)} source(s)…")
        nlm_bridge.delete_source(stale, nb, profile=prof)
        num_map, bm_sid = index()
    if not bm_sid:
        bm = db.get_benchmark(tab_id)
        label = (bm.get("number") or bm.get("title") or "benchmark")
        res = nlm_bridge.add_source_text(nb, f"🎯 BENCHMARK — {label}", _benchmark_fulltext(bm),
                                         profile=prof)
        if not res.get("ok"):
            raise RuntimeError(f"benchmark add failed: {res.get('error')}")
    # 2. add the missing wanted docs (skip what's already there — crash-safe re-entry)
    present = {_shortlist_key(n) for n in num_map}
    missing = [i for k, i in want_keys.items() if k not in present]
    added = 0
    for did in missing:
        d = docs_by_id[did]
        _screen_set(tab_id, status_text=f"📤 staging round {st.get('round', 0) + 1}: "
                                        f"{added}/{len(missing)} added…")
        _screen_heartbeat(tab_id)
        nlm_bridge.add_source_text(nb, f"{d['number']} — {(d.get('title') or '')[:120]}",
                                   _doc_source_text(d), profile=prof)
        added += 1
    # 3. wait for ingestion (probe costs no chat quota); carried-over sources are
    #    already confirmed — only the fresh adds need probing.
    _screen_set(tab_id, status_text="⏳ waiting for NotebookLM to ingest the batch…")
    known = set(num_map.values()) | ({bm_sid} if bm_sid else set())
    nlm_bridge.wait_sources_ready(nb, timeout=PIPELINE_INGEST_TIMEOUT, known_ready=known,
                                  profile=prof)
    # 4. verify: re-add once, then mark unindexable docs add_failed (ghost-source lesson)
    num_map, bm_sid = index()
    have = {_shortlist_key(n) for n in num_map}
    failed = []
    for k, did in want_keys.items():
        if k not in have:
            d = docs_by_id[did]
            nlm_bridge.add_source_text(nb, f"{d['number']} — {(d.get('title') or '')[:120]}",
                                       _doc_source_text(d), profile=prof)
    if any(k not in have for k in want_keys):
        nlm_bridge.wait_sources_ready(nb, timeout=60, known_ready=set(num_map.values()),
                                      profile=prof)
        num_map, bm_sid = index()
        have = {_shortlist_key(n) for n in num_map}
        failed = [did for k, did in want_keys.items() if k not in have]
        if failed:
            db.mark_screened(tab_id, failed, "add_failed")
    key_map = {_shortlist_key(n): want_keys.get(_shortlist_key(n)) for n in num_map
               if _shortlist_key(n) in want_keys}
    return nb, bm_sid, key_map, failed


def _screen_query(tab_id: int, nb: str, question: str) -> dict:
    """One guarded round query — NO cache (rotation changes the source-set signature
    every round, so the cache could never hit and would only store garbage)."""
    prof = _tab_profile(tab_id)
    res = nlm_bridge.query(nb, question, profile=prof)
    if "answer" in res and not _screen_answer_complete(res["answer"]):
        res = nlm_bridge.query(nb, question, profile=prof)  # one retry on truncation
        if "answer" in res and not _screen_answer_complete(res["answer"]):
            return {"incomplete": True, "answer": res["answer"]}
    return res


def _run_nlm_screen(tab_id: int) -> None:
    lock = _screen_lock_path(tab_id)
    try:
        st = _screen_read(tab_id)
        if not st or st.get("step") == "done":
            return
        bm = db.get_benchmark(tab_id)
        spec = _benchmark_feature_spec_for_nlm(bm)
        params = st.get("params") or {}
        s_cap = int(params.get("survivor_cap", 10))
        batch = int(params.get("batch_size", 39))
        question = _nlm_question(NLM_SCREEN_PROMPT, spec, top=s_cap)
        docs_by_id = {d["id"]: d for d in db.list_documents(tab_id, full=True)
                      if d["status"] == "fetched"}
        while st.get("step") == "round":
            if os.path.exists(_screen_pause_path(tab_id)):
                _screen_set(tab_id, status_text="⏸ paused")
                return
            if st.get("stop"):
                break                                     # finalize from the ledger so far
            queue, cursor = st.get("queue") or [], int(st.get("cursor", 0))
            if cursor >= len(queue):
                break
            roster = [i for i in queue[cursor:cursor + batch] if i in docs_by_id]
            survivors = [i for i in st.get("survivors") or [] if i in docs_by_id]
            st = _screen_set(tab_id, roster=roster,
                             status_text=f"round {st.get('round', 0) + 1}: staging…")
            nb, bm_sid, key_map, failed = _screen_stage(
                tab_id, st, survivors + roster, docs_by_id)
            _screen_set(tab_id, status_text=f"📓 round {st.get('round', 0) + 1}: asking NotebookLM…")
            _screen_heartbeat(tab_id)
            res = _screen_query(tab_id, nb, question)
            if nlm_bridge.is_quota_error(res):
                _screen_quota_pause(tab_id, res.get("error") or "quota exhausted")
                return
            if res.get("incomplete"):
                _screen_set(tab_id, error="round answer truncated twice — sources kept, "
                                          "▶️ Resume retries this round",
                            status_text="⚠️ truncated answer")
                db.append_message(tab_id, "s",
                    f"🔬 Mega-screen round {st.get('round', 0) + 1}: NotebookLM returned a "
                    "truncated/structureless answer twice — nothing was rejected or deleted. "
                    "▶️ Resume retries the same round.")
                return
            if "error" in res:
                _screen_set(tab_id, error=res["error"][:300], status_text="⚠️ query failed")
                db.append_message(tab_id, "s",
                    f"🔬 Mega-screen round {st.get('round', 0) + 1} query failed: "
                    f"{res['error'][:200]} — ▶️ Resume retries this round.")
                return
            # answer accepted → rank, ledger, bookkeeping, THEN advance the cursor.
            ordered, unmatched = _screen_parse_ranked(res["answer"], key_map)
            new_survivors = ordered[:s_cap]
            ledger = st.get("ledger") or {}
            rnd = int(st.get("round", 0)) + 1
            for rank, did in enumerate(ordered, 1):
                e = ledger.get(str(did))
                if not e or rank < e[0]:
                    ledger[str(did)] = [rank, rnd]
                else:
                    ledger[str(did)] = [e[0], rnd]
            named = set(ordered)
            db.mark_screened(tab_id, list(named), "graduate")
            db.mark_screened(tab_id, [i for i in roster
                                      if i not in named and i not in set(failed)], "rejected")
            st = _screen_set(tab_id, survivors=new_survivors, ledger=ledger,
                             cursor=cursor + len(queue[cursor:cursor + batch]), round=rnd,
                             unmatched=(st.get("unmatched") or []) + unmatched, error=None,
                             status_text=f"round {rnd} done — {len(ledger)} graduate(s) so far")
            _screen_heartbeat(tab_id)
        _screen_finalize(tab_id, _screen_set(tab_id, step="finalize"), docs_by_id)
    except Exception as exc:                              # keep the state file → resumable
        _screen_set(tab_id, error=str(exc)[:300], status_text=f"interrupted: {str(exc)[:120]}")
        db.append_message(tab_id, "s",
                          f"🔬 Mega-screen interrupted: {str(exc)[:200]} — ▶️ Resume to continue.")
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _screen_finalize(tab_id: int, st: dict, docs_by_id: dict[int, dict]) -> None:
    """Refill the notebook with the ledger's best (best-ever rank; later round wins
    ties — it faced stronger carry-forward competition) and run ONE rich shortlist
    query for the global ranking → the existing shortlisted/nlm_rank columns."""
    params = st.get("params") or {}
    target = int(params.get("target", 40))
    ledger = st.get("ledger") or {}
    ranked = sorted(((int(k), v) for k, v in ledger.items() if int(k) in docs_by_id),
                    key=lambda kv: (kv[1][0], -kv[1][1]))
    finalists = [k for k, _ in ranked[:min(target, 49)]]
    if not finalists:
        _screen_set(tab_id, step="done", status_text="✅ done — nothing graduated", error=None)
        db.append_message(tab_id, "s",
            "🔬 Mega-screen finished: NotebookLM named NO candidate in any round — "
            "nothing to shortlist. Check the benchmark features, or verify a sample "
            "with 🏆 deep-compare.")
        return
    _screen_set(tab_id, status_text=f"🏁 finalize: staging top {len(finalists)}…")
    nb, bm_sid, key_map, _failed = _screen_stage(tab_id, st, finalists, docs_by_id)
    bm = db.get_benchmark(tab_id)
    spec = _benchmark_feature_spec_for_nlm(bm)
    question = _nlm_question(NLM_SHORTLIST_PROMPT, spec)
    _screen_set(tab_id, status_text="🏁 finalize: asking for the global ranking…")
    res = nlm_bridge.query(nb, question, profile=_tab_profile(tab_id))
    if nlm_bridge.is_quota_error(res):
        _screen_quota_pause(tab_id, res.get("error") or "quota exhausted")
        return
    if "answer" in res and not _shortlist_answer_complete(res["answer"]):
        res = nlm_bridge.query(nb, question, profile=_tab_profile(tab_id))
    if "error" in res or not _shortlist_answer_complete(res.get("answer", "")):
        _screen_set(tab_id, error=(res.get("error") or "finalize answer truncated")[:300],
                    status_text="⚠️ finalize failed — ▶️ Resume retries")
        db.append_message(tab_id, "s", "🔬 Mega-screen finalize failed "
                          f"({(res.get('error') or 'truncated answer')[:160]}) — ▶️ Resume retries it.")
        return
    ordered, unmatched = _screen_parse_ranked(res["answer"], key_map)
    final_ids = ordered + [i for i in finalists if i not in set(ordered)]
    db.set_shortlisted(tab_id, final_ids)
    st = _screen_read(tab_id) or {}
    total, rounds = len(st.get("queue") or []), st.get("round", 0)
    participants = [{"kind": "benchmark", "title": _benchmark_label(bm)},
                    {"kind": "notebook", "title": st.get("notebook_title") or nb}]
    db.append_message(tab_id, "q",
                      f"[🔬 NLM mega-screen finalize — global ranking of the top "
                      f"{len(finalists)} graduate(s) from {rounds} round(s) over {total} candidate(s)]")
    db.append_message(tab_id, "c", _verify_citations(tab_id, res["answer"]),
                      model="notebooklm", participants=participants)
    stray = (st.get("unmatched") or []) + unmatched
    db.append_message(tab_id, "s",
        f"🔬 Mega-screen DONE: {total} candidate(s) screened in {rounds} round(s), "
        f"{len(st.get('ledger') or {})} graduated, top {len(final_ids)} written to the "
        f"shortlist (📓 rank + ☑ auto-checked) — now run 🏆 Verify shortlist for the "
        "precise Claude read. All of this cost ZERO Claude tokens."
        + (f"\n\n({len(set(stray))} number(s) NLM named are not in the pool: "
           f"{', '.join(sorted(set(stray))[:15])}…)" if stray else ""))
    _screen_set(tab_id, step="done", status_text="✅ done", error=None)


# --- quota watchdog (mirrors the ⏳ token-limit watchdog for Claude reads) ---

def _screen_quota_pause(tab_id: int, err: str) -> None:
    resume_at = time.time() + SCREEN_QUOTA_PROBE_EVERY
    _screen_set(tab_id, quota={"paused": True, "resume_at": resume_at,
                               "paused_at": time.time(), "err": (err or "")[:300]},
                status_text="😴 NLM quota exhausted — auto-resume armed")
    db.append_message(tab_id, "s",
        "😴 NotebookLM's Q&A quota is exhausted (typically resets in 6-12h; source "
        "staging still works). The mega-screen is PAUSED and will probe hourly and "
        f"auto-resume — nothing to click. ({(err or '')[:160]})")
    _spawn_screen_watchdog(tab_id)


def _spawn_screen_watchdog(tab_id: int) -> None:
    with _screen_watchdogs_mu:
        t = _screen_watchdogs.get(tab_id)
        if t and t.is_alive():
            return
        t = threading.Thread(target=_screen_watchdog_loop, args=(tab_id,), daemon=True)
        _screen_watchdogs[tab_id] = t
        t.start()


def _screen_watchdog_loop(tab_id: int) -> None:
    while True:
        st = _screen_read(tab_id)
        quota = (st or {}).get("quota") or {}
        if not st or not quota.get("paused") or st.get("step") == "done":
            return                                        # resumed manually / finished / gone
        if time.time() - (quota.get("paused_at") or 0) > SCREEN_QUOTA_GIVE_UP:
            _screen_set(tab_id, quota=None,
                        error="NLM quota still exhausted after 24h of probing",
                        status_text="⚠️ gave up probing — ▶️ Resume manually")
            db.append_message(tab_id, "s",
                "🔬 Mega-screen: the NotebookLM quota was still exhausted after ~24h of "
                "hourly probes — giving up on auto-resume. ▶️ Resume it manually later.")
            return
        wait_s = (quota.get("resume_at") or 0) - time.time()
        if wait_s > 0:
            time.sleep(min(wait_s, 300))                  # re-read every ≤5min (manual resume shifts state)
            continue
        if _screen_running(tab_id):
            time.sleep(300)
            continue
        nb = st.get("notebook_id")
        probe = (nlm_bridge.query(nb, "Reply with exactly: OK", profile=_tab_profile(tab_id))
                 if nb else {"error": "no notebook"})
        if nlm_bridge.is_quota_error(probe) or "error" in probe:
            _screen_set(tab_id, quota={**quota, "resume_at": time.time() + SCREEN_QUOTA_PROBE_EVERY})
            continue
        _screen_set(tab_id, quota=None, error=None, status_text="▶️ quota back — resuming…")
        db.append_message(tab_id, "s", "😴→▶️ Mega-screen: the NotebookLM quota is back — "
                                       "resuming the rotation where it left off.")
        _screen_launch(tab_id)
        return


def _screen_launch(tab_id: int) -> bool:
    """Start the job thread under the exclusive lock. False = already running."""
    lock = _screen_lock_path(tab_id)
    if _screen_running(tab_id):
        return False
    try:
        os.unlink(lock)                                   # clear a STALE lock (mtime past TTL)
    except OSError:
        pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return False
    try:
        os.unlink(_screen_pause_path(tab_id))
    except OSError:
        pass
    threading.Thread(target=_run_nlm_screen, args=(tab_id,), daemon=True).start()
    return True


@app.post("/api/tabs/{tab_id}/nlm-screen")
def nlm_screen_start(tab_id: int, body: schemas.NlmScreenRequest):
    """Start (or ▶️ Resume) the 🔬 NLM mega-screen rotation job."""
    _tab_or_404(tab_id)
    ok, why = nlm_bridge.available(_tab_profile(tab_id))
    if not ok:
        raise HTTPException(400, f"NotebookLM unavailable: {why}")
    stt = _screen_status(tab_id)
    if stt["running"]:
        return {"started": False, **stt}
    if body.resume:
        st = _screen_read(tab_id)
        if not st or st.get("step") not in ("round", "finalize"):
            raise HTTPException(400, "no interrupted mega-screen to resume")
        _screen_set(tab_id, error=None, quota=None, stop=False, status_text="▶️ resuming…")
        started = _screen_launch(tab_id)
        return {"started": started, "resumed": True, **_screen_status(tab_id)}
    bm = db.get_benchmark(tab_id)
    if not bm or bm.get("status") != "ready":
        raise HTTPException(400, "benchmark is not ready — set it first")
    fetched = [d for d in db.list_documents(tab_id) if d["status"] == "fetched"]
    cands = fetched
    if body.doc_ids:
        want = set(body.doc_ids)
        cands = [d for d in cands if d["id"] in want]
    if not body.include_screened:
        cands = [d for d in cands if not d.get("nlm_screened_at")]
    if not cands:
        raise HTTPException(400, "no fetched candidates to screen"
                            + ("" if body.include_screened
                               else " (all already screened — tick ↻ include screened)"))
    # 🏆 Champion seeding: a default run over newcomers carries the previous
    # tournament forward instead of isolating them — prior graduates enter the
    # ledger (so finalize ranks old+new together and set_shortlisted no longer
    # wipes a big run's result with a newcomers-only ranking) and the best of
    # them pre-fill the survivor pool, so a newcomer must BEAT the champions to
    # graduate. Carried rounds reset to 0: on equal best-rank the "later round
    # wins" tiebreak then favors the doc that earned it against the seeded
    # champions. Explicit doc_ids or ↻ include_screened = a deliberate fresh
    # tournament, no seeding.
    seed_ledger, seed_survivors = {}, []
    if not body.include_screened and not body.doc_ids:
        prev = _screen_read(tab_id) or {}
        fetched_ids = {d["id"] for d in fetched}
        seed_ledger = {k: [v[0], 0] for k, v in (prev.get("ledger") or {}).items()
                       if int(k) in fetched_ids}
        seed_survivors = [int(k) for k, v in sorted(seed_ledger.items(),
                          key=lambda kv: kv[1][0])[:body.survivor_cap]]
    _screen_set(tab_id, step="round", queue=[d["id"] for d in cands], cursor=0, round=0,
                roster=[], survivors=seed_survivors, ledger=seed_ledger, unmatched=[],
                params={"batch_size": body.batch_size, "survivor_cap": body.survivor_cap,
                        "target": body.target},
                started_at=db._now(), quota=None, stop=False, error=None,
                status_text="queued…")
    rounds = -(-len(cands) // body.batch_size)
    db.append_message(tab_id, "s",
        f"🔬 NLM mega-screen STARTED over {len(cands)} candidate(s): ~{rounds} round(s) of "
        f"{body.batch_size} through one rotating notebook, {body.survivor_cap} survivors "
        f"carry forward each round, finalize writes the top ~{body.target} to the shortlist. "
        "Free (zero Claude tokens); survives restarts; NLM quota pauses auto-resume."
        + (f"\n🏆 Seeded with the previous run: {len(seed_survivors)} champion(s) pre-fill "
           f"the survivor pool and {len(seed_ledger)} prior graduate(s) stay in the ledger — "
           "newcomers must beat the champions to graduate; finalize ranks old and new "
           "together." if seed_survivors else ""))
    started = _screen_launch(tab_id)
    return {"started": started, "rounds_estimate": rounds, **_screen_status(tab_id)}


@app.get("/api/tabs/{tab_id}/nlm-screen/status")
def nlm_screen_status(tab_id: int):
    """File+DB derived — correct from any gunicorn worker."""
    _tab_or_404(tab_id)
    return _screen_status(tab_id)


@app.post("/api/tabs/{tab_id}/nlm-screen/pause")
def nlm_screen_pause(tab_id: int):
    """Halt at the next round boundary (sources stay; ▶️ Resume continues)."""
    _tab_or_404(tab_id)
    with open(_screen_pause_path(tab_id), "w"):
        pass
    return {"pausing": True, **_screen_status(tab_id)}


@app.post("/api/tabs/{tab_id}/nlm-screen/stop")
def nlm_screen_stop(tab_id: int):
    """Stop rotating and finalize NOW from the graduates ledger accumulated so far."""
    _tab_or_404(tab_id)
    st = _screen_read(tab_id)
    if not st or st.get("step") == "done":
        raise HTTPException(400, "no mega-screen in progress")
    _screen_set(tab_id, stop=True)
    if not _screen_running(tab_id):                       # not running → finalize directly
        _screen_set(tab_id, step="finalize", error=None, quota=None)
        _screen_launch(tab_id)
    return {"stopping": True, **_screen_status(tab_id)}


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
def digest_rescore_ep(tab_id: int, body: schemas.DigestRescoreRequest, bg: BackgroundTasks):
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
    bg.add_task(_auto_judge_combi_safe, tab_id)
    return {"ok": True, "updated": updated, "requested": len(chosen), "batches": len(batches),
            "failed_batches": len(errors), "results": results}


@app.post("/api/tabs/{tab_id}/score-recalc")
def score_recalc_ep(tab_id: int, bg: BackgroundTasks):
    """🧮 RECALC (free, instant). Recompute every candidate's stored score from its
    ALREADY-STORED per-element verdicts under the benchmark's CURRENT feature kinds —
    no model call, no re-read, no digest pass.

    This is the button for 'the features were RELABELED after the reads ran' (e.g.
    dependent-claim features moved M → A): the per-element verdicts are still valid —
    they match by element NAME — but the 0-10 score frozen on each document was
    aggregated under the old kinds. Score becomes the weighted Must-rating (the same
    number the unified ranking uses); the A/W bonus rides in the note."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    els = (bm or {}).get("features") or []
    if not any(_kind(e) == "M" for e in els):
        raise HTTPException(400, "the benchmark has no mandatory (M) elements to score against")
    now = int(time.time())
    updated = no_verdicts = 0
    for d in db.list_documents(tab_id, full=True):
        if d["status"] != "fetched":
            continue
        u = _unified_score(els, d)
        if not u["assessed"]:            # nothing per-element stored → keep the old score
            no_verdicts += 1
            continue
        note = (f"recalc: M {u['mand_full']}✓ {u['mand_partial']}~ of {u['mand_total']}"
                + (f" · A-bonus {u['add_bonus']} ({u['add_full']}✓ {u['add_partial']}~ "
                   f"of {u['add_total']})" if u["add_total"] else "")
                + (f" · W-bonus {u['w_bonus']}" if u["w_total"] else ""))
        db.update_document(d["id"], score=u["mand_rating"], score_note=note,
                           scored_at=now, score_model="recalc·stored-verdicts")
        updated += 1
    if not updated:
        raise HTTPException(400, "no candidate has stored per-element verdicts yet — run a "
                                 "🏆 deep-compare / 🔎 combi / ➕ additional read first")
    db.append_message(tab_id, "s",
        f"🧮 Recalculated {updated} candidate score(s) from their STORED per-element verdicts "
        f"under the current M/A/W labels — zero model calls, nothing re-read. Score = weighted "
        f"Must-rating over the {sum(1 for e in els if _kind(e) == 'M')} mandatory element(s); "
        f"additional-feature coverage is a bonus in the note."
        + (f" ⚠ {no_verdicts} candidate(s) kept their old score — no per-element verdicts "
           "stored (only a holistic read); re-read those to refresh them." if no_verdicts else ""))
    bg.add_task(_auto_judge_combi_safe, tab_id)
    return {"ok": True, "updated": updated, "no_verdicts": no_verdicts}


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
def additional_read_ep(tab_id: int, body: schemas.AdditionalReadRequest, bg: BackgroundTasks):
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
    bg.add_task(_auto_judge_combi_safe, tab_id)
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
# Bonus scale, mirroring the ➕ additional read (app.js ADD_UNIT/ADD_CAP): a present
# bonus element RAISES the score, its absence never lowers it. ADDITIONAL (A) and
# WHOLE-DOCUMENT (W) each get their OWN capped pool, so neither can leap a document over
# one that covers more MANDATORY elements — Must always dominates.
ADD_UNIT, ADD_CAP = 0.3, 1.0
# screen → digest → full. A pair is only as trustworthy as its weaker document.
_DEPTH_RANK = {"screen": 0, "digest": 1, "full": 2}
# Combinability (motivation-to-combine) judging: bounded total, batched so one click judges
# EVERY matrix partner (set-cover can surface many) without one call carrying too many digests.
COMBI_MOTIV_MAX = int(os.environ.get("PB_COMBI_MOTIV_MAX", "60"))
COMBI_MOTIV_BATCH = int(os.environ.get("PB_COMBI_MOTIV_BATCH", "10"))


# Feature kinds: M = mandatory/core (the ONLY thing that decides coverage and the ranking
# tier), A = additional (user-curated bonus), W = whole-document (elements of the benchmark
# document itself — bonus). Everywhere that used to read "not A" as mandatory must read
# "== M" now, or a W element would silently be treated as mandatory.
def _kind(f: dict) -> str:
    k = (f.get("kind") or "M").upper()
    return k if k in ("M", "A", "W") else "M"


def _norm_num(s: str | None) -> str:
    """Normalize a publication number for identity comparison (strip spaces/hyphens/kind
    codes' punctuation, uppercase) so a candidate that IS the benchmark is recognised."""
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def _drop_benchmark(docs: list[dict], bm: dict | None) -> list[dict]:
    """Exclude the benchmark document itself from a candidate list — it trivially matches
    itself 11/11 and would always be the top 'coverer', which is meaningless."""
    bn = _norm_num((bm or {}).get("number"))
    if not bn:
        return docs
    return [d for d in docs if _norm_num(d.get("number")) != bn]


def _combi_elements(bm: dict | None) -> list[dict]:
    """Every feature the coverage pass judges — MANDATORY *and* the bonus kinds (A, W).

    The bonus ones ride along on purpose: a document/pair that also brings a bonus element
    is better, and it costs nothing to ask for it in the same pass. They are kept apart when
    RATING (see _combi_pairs / _unified_score): only mandatory (M) elements decide whether a
    document or pair covers the invention."""
    return list((bm or {}).get("features") or [])


def _combi_mandatory(bm: dict | None) -> list[dict]:
    return [f for f in ((bm or {}).get("features") or []) if _kind(f) == "M"]


# Fidelity of a per-element verdict — the HIGHER wins when both stores hold it. A full-text
# reading always beats a digest guess, so the score/matrix reflect the tokens actually spent:
#   • feature_scores  → the best-match DEEP READ, which reads abstract+claims+description in
#     full (deep_map), so it is full text (fidelity 'read'). Never written by ♻️ digest-rescore.
#   • combi_coverage  → the 🔎 combi scan, depth-tagged: full (stage-2) > digest > screen.
_FID = {"combi_full": 3, "read": 2, "combi_digest": 1, "combi_screen": 0}
_FID_LABEL = {3: "full", 2: "full", 1: "digest", 0: "screen"}


def _effective_coverage(doc: dict) -> dict:
    """Best available per-element verdict, merging the full-text deep read (feature_scores)
    with the combi scan (combi_coverage), preferring the higher-fidelity source — so a full
    read you already paid for is used instead of a digest guess. Reuses stored data only, no
    model call.

    CONFLICT is surfaced, not hidden: when the TWO full-text passes disagree on an element —
    the 🏆 best-match deep-read (holistic per-feature check) vs the 🔎 combi stage-2 verify
    (element-by-element, anticipation standard) — the winner is the higher-fidelity one, but
    `conflict=True` and `alt` carries the losing verdict so the matrix/list can mark it and
    the user can decide. A digest/screen verdict differing from a full read is NOT a conflict
    (it is simply lower fidelity, expected to be overridden).

    {name: {"status", "fid", "depth", "conflict", "alt"}}."""
    out: dict = {}
    for e in (doc.get("feature_scores") or []):        # deep_map = full text → fidelity 'read'
        if isinstance(e, dict) and e.get("name"):
            out[e["name"]] = {"status": e.get("status") or "no", "fid": _FID["read"],
                              "conflict": False, "alt": None}
    for n, r in _cov_records(doc).items():
        depth = r.get("depth") or "screen"
        fid = _FID.get("combi_" + depth, _FID["combi_digest"])
        cstat = r.get("status", "no")
        prev = out.get(n)
        if prev is None:
            out[n] = {"status": cstat, "fid": fid, "conflict": False, "alt": None}
            continue
        # Two FULL-TEXT verdicts (deep-read AND combi stage-2) that differ → a real conflict.
        conflict = (prev["fid"] >= _FID["read"] and depth == "full" and cstat != prev["status"])
        if fid >= prev["fid"]:                          # combi wins on fidelity
            out[n] = {"status": cstat, "fid": fid, "conflict": conflict,
                      "alt": prev["status"] if conflict else None}
        elif conflict:                                  # deep-read keeps the cell but flag it
            prev["conflict"] = True
            prev["alt"] = cstat
    for v in out.values():
        v["depth"] = _FID_LABEL.get(v["fid"], "digest")
    return out


def _element_status_map(doc: dict) -> dict:
    """Statuses only, full read preferred over digest (see _effective_coverage)."""
    return {n: v["status"] for n, v in _effective_coverage(doc).items()}


def _bonus_pool(elements: list[dict], cov: dict) -> dict:
    """Weighted bonus for one kind's elements: present=full unit, partial=half, absent=0,
    capped. Same scale as the ➕ additional read so the number means the same everywhere."""
    b, full, part = 0.0, 0, 0
    for e in elements:
        s = cov.get(e["name"], "no")
        unit = (int(e.get("weight", 1)) / 5) * ADD_UNIT
        if s == "yes":
            b += unit
            full += 1
        elif s == "partial":
            b += unit * 0.5
            part += 1
    return {"bonus": round(min(ADD_CAP, b), 2), "full": full, "partial": part,
            "total": len(elements)}


def _unified_score(elements: list[dict], doc: dict) -> dict:
    """THE single ranking used by the matrix, the list and the chat. Must (M) coverage is the
    dominant, tier-deciding term; Additional (A) and Whole-document (W) are separate capped
    bonus pools that only differentiate WITHIN a Must tier. Computed from stored coverage —
    no model call, so it re-ranks every already-assessed document for free.

    `key` is a single sortable number encoding the lexicographic order
    (covers-all-Must, weighted-Must-rating, A-bonus, W-bonus) so callers can sort by it
    directly; the component fields are returned for display."""
    mand = [e for e in elements if _kind(e) == "M"]
    add = [e for e in elements if _kind(e) == "A"]
    whole = [e for e in elements if _kind(e) == "W"]
    eff = _effective_coverage(doc)
    cov = {n: v["status"] for n, v in eff.items()}
    total_w = sum(int(e.get("weight", 1)) for e in mand) or 1
    cells = [cov.get(e["name"], "no") for e in mand]
    mand_full = sum(1 for s in cells if s == "yes")
    mand_part = sum(1 for s in cells if s == "partial")
    # Must elements where the two FULL-TEXT passes disagree (surfaced, not silently resolved).
    mand_conflicts = sum(1 for e in mand if eff.get(e["name"], {}).get("conflict"))
    # STRICT by user rule (2026-07-27): "alone"/covers-all = EVERY Must element a hard ✓.
    # A ~ is a stretch reading — a document needing one is not a clean single reference and
    # must not wear the badge or the top rank tier. `no_absent` keeps the looser fact
    # separately: nothing is MISSING (a ~ can't be filled by a partner, only strengthened),
    # which is what the matrix pivot and gap-filling care about.
    covers_all = bool(mand) and all(s == "yes" for s in cells)
    no_absent = bool(mand) and not any(s == "no" for s in cells)
    covered_w = sum(int(e.get("weight", 1)) * (1.0 if s == "yes" else 0.5)
                    for e, s in zip(mand, cells) if s in ("yes", "partial"))
    mand_rating = round(10.0 * covered_w / total_w, 2) if mand else 0.0
    a = _bonus_pool(add, cov)
    w = _bonus_pool(whole, cov)
    assessed = any(s != "no" for s in cells) or a["full"] or a["partial"] or w["full"] or w["partial"]
    # Composite key: covers_all (×1e9, STRICT all-✓) ≫ mand_rating (×1e6, ≤1e7) ≫ A-bonus
    # (×1e3, ≤1e3) ≫ W-bonus (×1, ≤1). No tier for no_absent: a 3✓+3~ doc ranks by its
    # rating (7.5) and sits BELOW a clean 5✓+1✗ doc (8.33) — per the same user rule.
    key = ((1e9 if covers_all else 0.0) + mand_rating * 1e6
           + a["bonus"] * 1e3 + w["bonus"]) if assessed else -1.0
    return {"covers_all": covers_all, "no_absent": no_absent,
            "mand_full": mand_full, "mand_partial": mand_part,
            "mand_total": len(mand), "mand_rating": mand_rating,
            "mand_conflicts": mand_conflicts,
            "add_bonus": a["bonus"], "add_full": a["full"], "add_partial": a["partial"],
            "add_total": a["total"], "w_bonus": w["bonus"], "w_full": w["full"],
            "w_partial": w["partial"], "w_total": w["total"],
            "assessed": bool(assessed), "key": round(key, 3)}


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
    mand = [e for e in elements if _kind(e) == "M"]
    if not mand:
        return False
    eff = _effective_coverage(doc)                     # combi coverage OR the full deep read
    return all(eff.get(e["name"], {}).get("fid", 0) >= _FID["combi_digest"] for e in mand)


def _combi_solo(elements: list[dict], docs: list[dict], limit: int = 20) -> list[dict]:
    """Documents that cover EVERY mandatory element ON THEIR OWN.

    Strictly stronger than any combination — one document disclosing the whole invention is
    a novelty-grade hit, where a pair is only an obviousness argument that still needs a
    motivation to combine. _combi_pairs deliberately drops any pair where one document
    subsumes the other, so without this list a solo full-coverer would vanish from the
    results entirely — the strongest finding, invisible.

    A document COVERS a mandatory element at YES *or* PARTIAL: a partial/implicit/pack-level
    disclosure can still MEET a limitation — that is the anticipation standard, not literal
    identity — so requiring every element at YES hid genuine single-reference anticipations
    (e.g. one strong on 9 limbs, pack-level on 2). Only a "no" means the document truly lacks
    the element. Literal coverers (more YES) rank first; the ✓/~ split stays visible.

    NOTE the split from `_unified_score.covers_all` (2026-07-27 user rule): the "alone"
    badge and the top rank tier are STRICT (all ✓). Membership in THIS list stays loose —
    it transparently reports the ✓/~ split and names the partial elements, so a ~-leaning
    near-anticipation is surfaced without being labeled a clean single reference."""
    mand = [e for e in elements if _kind(e) == "M"]
    add = [e for e in elements if _kind(e) == "A"]
    whole = [e for e in elements if _kind(e) == "W"]
    if not mand:
        return []
    out = []
    for d in docs:
        cov = _cov_map(d)
        statuses = [cov.get(e["name"], "no") for e in mand]
        if any(s == "no" for s in statuses):        # a real gap → not a single-ref coverer
            continue
        mand_full = sum(1 for s in statuses if s == "yes")
        mand_part = sum(1 for s in statuses if s == "partial")
        partial_names = [e["name"] for e, s in zip(mand, statuses) if s == "partial"]
        a = _bonus_pool(add, cov)
        w = _bonus_pool(whole, cov)
        out.append({"id": d["id"], "number": d.get("number"),
                    "mand_total": len(mand), "mand_full": mand_full,
                    "mand_partial": mand_part, "partial_names": partial_names[:12],
                    "add_cov": a["full"] + a["partial"], "add_full": a["full"],
                    "add_partial": a["partial"], "add_total": a["total"],
                    "add_bonus": a["bonus"],
                    "w_bonus": w["bonus"], "w_full": w["full"], "w_partial": w["partial"],
                    "w_total": w["total"],
                    "depth": d.get("combi_depth") or "screen"})
    # literal coverers first (most mandatory YES), then A bonus, then W bonus, then partials
    out.sort(key=lambda s: (-s["mand_full"], -s["add_bonus"], -s["w_bonus"],
                            -s["add_cov"], s["number"] or ""))
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
    mand = [e for e in elements if _kind(e) == "M"]
    add = [e for e in elements if _kind(e) == "A"]
    whole = [e for e in elements if _kind(e) == "W"]
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

            # A document COVERS an element at YES or PARTIAL (partial can still meet a
            # limitation — the anticipation standard). `complete` = the union leaves NO
            # mandatory element uncovered; mand_full / mand_partial record the quality, and
            # the rating (weighted, partial=half) separates a literal cover from a stretched
            # one so the two never look equal.
            def has(s):
                return s in ("yes", "partial")

            mw = 0.0
            mand_full = mand_part = 0
            complete = True
            only_a, only_b = [], []
            for e in mand:
                u, sa, sb = best(e["name"])
                if u == "yes":
                    mw += int(e.get("weight", 1))
                    mand_full += 1
                elif u == "partial":
                    mw += int(e.get("weight", 1)) * 0.5
                    mand_part += 1
                else:
                    complete = False          # a real gap in BOTH → not covered
                if has(sa) and not has(sb):
                    only_a.append(e["name"])
                elif has(sb) and not has(sa):
                    only_b.append(e["name"])
            if not only_a or not only_b:
                continue                      # not a combination: one subsumes the other
            # BONUS kinds (A, W): the UNION's best status per element, bonus only — never part
            # of `complete`, never a penalty. Each kind is its own capped pool.
            union_cov = {e["name"]: best(e["name"])[0] for e in (add + whole)}
            a = _bonus_pool(add, union_cov)
            w = _bonus_pool(whole, union_cov)
            bonus, add_full, add_part = a["bonus"], a["full"], a["partial"]
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
                "rating": round(10.0 * mw / total_w, 1),
                "mand_full": mand_full, "mand_partial": mand_part, "mand_total": len(mand),
                "add_bonus": round(bonus, 2), "add_cov": add_full + add_part,
                "add_full": add_full, "add_partial": add_part, "add_total": len(add),
                "w_bonus": w["bonus"], "w_full": w["full"], "w_partial": w["partial"],
                "w_total": w["total"],
                "covered_w": round(mw, 1), "total_w": total_w,
                "a_only": only_a[:12], "b_only": only_b[:12],
                "depth": depth,
            })
    # Bonus coverage breaks ties: between two pairs that cover the invention equally, the one
    # that ALSO brings the A/W bonus elements is the better combination.
    out.sort(key=lambda p: (-p["complete"], -p["rating"], -p["add_bonus"], -p["w_bonus"],
                            -len(p["a_only"]) - len(p["b_only"])))
    return out[:limit]


def _combi_matrix(elements: list[dict], docs: list[dict], limit: int = 10) -> dict:
    """Element × document coverage GRID over the MANDATORY elements — the raw material the
    user reads to judge combinations by eye (the pair list is no longer shown; this replaces
    it). Columns are the mandatory elements; each row is a document with its per-element
    verdict (yes / partial / no) plus the three standalone scores this app already computes:

      • mandatory coverage  — how many must-elements the document discloses on its own
        (covers_all = a single-reference full coverer, the same test as `_combi_solo`);
      • additional bonus    — the ➕ additional-read scale (weight/5 · ADD_UNIT, capped),
        each present A-feature adding points, absence never a penalty;
      • whole-benchmark match — the 🏆 best-match score already stored on the row (present
        only when the benchmark has been ranked; null otherwise).

    The grid PIVOTS: while the best document still misses a Must element, columns are the Must
    elements and partners fill Must gaps. Once the best document covers every Must element (and
    there are additional/whole-doc elements), the Must dimension is solved — showing ten more
    all-✓ Must rows is noise — so the columns switch to the ADDITIONAL (+ whole-doc) elements
    and partners become the documents that bring the additional features the anchor lacks, even
    if they don't cover Must (they combine with the anchor, which already does). Pure derivation
    from stored coverage; no model call."""
    mand = [e for e in elements if _kind(e) == "M"]
    bonus = [e for e in elements if _kind(e) in ("A", "W")]     # additional + whole-doc
    rows = []
    for d in docs:
        u = _unified_score(elements, d)              # the SAME score the list/chat use
        if not u["assessed"]:
            continue                                  # never scanned/read → not a matrix row
        eff = _effective_coverage(d)                  # full read preferred over digest
        m_fids = [eff.get(e["name"], {}).get("fid", 0) for e in mand]
        rows.append({
            "id": d["id"], "number": d.get("number"), "_eff": eff,
            "mand_full": u["mand_full"], "mand_partial": u["mand_partial"],
            "mand_total": u["mand_total"], "covers_all": u["covers_all"],
            "no_absent": u["no_absent"],
            "mand_rating": u["mand_rating"],
            "add_bonus": u["add_bonus"], "add_full": u["add_full"],
            "add_partial": u["add_partial"], "add_total": u["add_total"],
            "w_bonus": u["w_bonus"], "w_full": u["w_full"], "w_partial": u["w_partial"],
            "w_total": u["w_total"], "score": d.get("score"), "key": u["key"],
            "mand_conflicts": u["mand_conflicts"],
            # Row depth = the weakest fidelity among the MUST cells (the primary read depth),
            # so a document full-read against the elements shows 📖 full regardless of the grid.
            "depth": _FID_LABEL.get(min(m_fids), "screen") if m_fids else "screen",
        })
    rows.sort(key=lambda r: (-r["key"], r["number"] or ""))
    # PIVOT decision: Must solved by the best doc AND bonus elements exist → differentiate on
    # the additional dimension instead of repeating all-✓ Must rows.
    # Pivot on no_absent, not the strict covers_all: once the best doc has nothing ABSENT
    # (a ~ can't be filled by a partner), Must gap-filling is exhausted and the additional
    # dimension differentiates. The strict all-✓ test only gates the "alone" badge/tier.
    pivot = bool(rows) and rows[0]["no_absent"] and bool(bonus)
    active = bonus if pivot else mand
    mode = "additional" if pivot else "must"
    columns = [{"name": e["name"], "weight": int(e.get("weight", 1)), "kind": _kind(e)}
               for e in active]
    for r in rows:
        eff = r.pop("_eff")
        r["cells"] = [eff.get(e["name"], {}).get("status", "no") for e in active]
        # Per-cell CONFLICT: the losing full-text verdict when the two passes disagree, else
        # null — so the grid MARKS the contested cell instead of silently showing one side.
        r["cell_alt"] = [(eff.get(e["name"], {}).get("alt")
                          if eff.get(e["name"], {}).get("conflict") else None) for e in active]
    focus = _focus_combination(active, rows, limit)
    contested = sum(1 for row in focus["rows"] for a in row.get("cell_alt", []) if a)
    return {"columns": columns, "rows": focus["rows"], "gap_names": focus["gap_names"],
            "uncovered_gaps": focus.get("uncovered_gaps", []),
            "anchor": focus["anchor"], "covers_all_anchor": focus["covers_all_anchor"],
            "mode": mode, "total_ranked": len(rows), "contested": contested}


MATRIX_MIN_ROWS = 10        # always show at least this many (anchor + fillers), for choice
MATRIX_MAX_ROWS = 50        # never exceed — a runaway backstop


def _focus_combination(cols: list[dict], rows: list[dict], limit: int = MATRIX_MIN_ROWS,
                       hard_cap: int = MATRIX_MAX_ROWS) -> dict:
    """ANCHOR (best document) + the PARTNERS needed so EVERY coverable gap the anchor has in
    the ACTIVE dimension (`cols`) is filled by at least one shown row — a greedy SET COVER, not
    a flat top-N. The row count GROWS as needed (up to hard_cap) so a gap is never silently
    dropped: if some document discloses it, a row disclosing it IS shown. Columns that NO
    document in the pool covers are returned in `uncovered_gaps` and marked distinctly, so
    "genuinely absent from the corpus" is never confused with "hidden below the fold".

    After coverage is guaranteed, the list is topped up to `limit` with the next-best fillers
    for richness (choice per gap). `covers_all_anchor` means the anchor covers every ACTIVE
    element — nothing to fill — so the next-best rows are shown as leaders instead."""
    if not rows:
        return {"rows": [], "gap_names": [], "anchor": None, "covers_all_anchor": False,
                "uncovered_gaps": []}
    names = [e["name"] for e in cols]
    w_by_i = {i: int(cols[i].get("weight", 1)) for i in range(len(cols))}
    top = rows[0]
    others = rows[1:]
    gap_idx = [i for i, s in enumerate(top["cells"]) if s == "no"]
    anchor = {**top, "is_anchor": True, "fills": []}
    if not gap_idx:                                   # anchor covers every active element
        rest = [{**r, "is_anchor": False, "fills": []} for r in others[:limit]]
        return {"rows": [anchor] + rest, "gap_names": [], "anchor": anchor["number"],
                "covers_all_anchor": True, "uncovered_gaps": []}
    gap_names = [names[i] for i in gap_idx]
    # A gap is COVERABLE if any document in the pool discloses it; otherwise it is genuinely
    # absent from the searched corpus (a real prior-art finding, not a display limit).
    coverable = {i: any(r["cells"][i] in ("yes", "partial") for r in others) for i in gap_idx}
    uncovered_gaps = [names[i] for i in gap_idx if not coverable[i]]
    need = {i for i in gap_idx if coverable[i]}
    partners = []
    for r in others:
        fidx = {i for i in gap_idx if r["cells"][i] in ("yes", "partial")}
        if not fidx:
            continue                                  # brings nothing the anchor lacks
        fill_w = sum(w_by_i[i] * (1.0 if r["cells"][i] == "yes" else 0.5) for i in fidx)
        partners.append({**r, "is_anchor": False, "fills": [names[i] for i in sorted(fidx)],
                         "fill_w": round(fill_w, 1), "_fidx": fidx})
    # 1) GREEDY SET COVER — pick the partner covering the most still-uncovered weighted gaps
    #    until every coverable gap is represented (bounded by hard_cap).
    selected, avail, remaining = [], list(partners), set(need)
    while remaining and avail and len(selected) < hard_cap:
        best = max(avail, key=lambda p: (sum(w_by_i[i] for i in p["_fidx"] & remaining), p["key"]))
        if not (best["_fidx"] & remaining):
            break                                     # nothing left adds new coverage
        selected.append(best)
        avail.remove(best)
        remaining -= best["_fidx"]
    # 2) TOP UP to `limit` with the next-best fillers, for choice per gap.
    for p in sorted(avail, key=lambda p: (-p["fill_w"], -p["key"])):
        if len(selected) >= limit:
            break
        selected.append(p)
    selected.sort(key=lambda p: (-p["fill_w"], -p["key"]))
    for p in selected:
        p.pop("_fidx", None)
    return {"rows": [anchor] + selected, "gap_names": gap_names,
            "anchor": anchor["number"], "covers_all_anchor": False,
            "uncovered_gaps": uncovered_gaps}


@app.get("/api/tabs/{tab_id}/combi-results")
def combi_results_ep(tab_id: int, top_pairs: int = 20):
    """The LAST investigation's findings, re-derived from STORED coverage — so a page reload
    doesn't lose them. The panel is otherwise pure client state (a scan's response held in
    memory), which a refresh wipes even though every verdict is safely in the DB. This lets
    the UI rehydrate the panel on load. Nothing is computed by a model here."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    elements = _combi_elements(bm)
    fresh = _drop_benchmark([d for d in db.list_documents(tab_id, full=True) if _rigorous(d, elements)], bm)
    if not fresh:
        return {"ok": True, "has_results": False, "pairs": [], "solo": [],
                "matrix": {"columns": [], "rows": []}, "elements": len(elements),
                "ideal": _combi_ideal_payload(tab_id)}
    pairs = _combi_pairs(elements, fresh, top_pairs)
    solo = _combi_solo(elements, fresh)
    depth = "full" if fresh and all(d.get("combi_depth") == "full" for d in fresh) else "digest"
    return {"ok": True, "has_results": True, "assessed": len(fresh),
            "elements": len(elements), "complete": len([p for p in pairs if p["complete"]]),
            "pairs": pairs, "solo": solo, "matrix": _combi_matrix(elements, _drop_benchmark([d for d in db.list_documents(tab_id, full=True) if d['status'] == 'fetched'], bm)), "depth": depth,
            "ideal": _combi_ideal_payload(tab_id)}


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
    docs = _drop_benchmark([d for d in db.list_documents(tab_id, full=True)
            if d["status"] == "fetched" and (d.get("digest") or "").strip()], bm)
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
def combi_scan_ep(tab_id: int, body: schemas.CombiScanRequest, bg: BackgroundTasks):
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
    docs = _drop_benchmark([d for d in db.list_documents(tab_id, full=True)
            if d["status"] == "fetched" and (d.get("digest") or "").strip()], bm)
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
    fresh = _drop_benchmark([d for d in db.list_documents(tab_id, full=True) if _rigorous(d, elements)], bm)
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
           if reused else "") + f". {sum(1 for s in solo if not s['mand_partial'])} document(s) "
        f"cover EVERY mandatory element ALONE with a hard ✓ (single-reference grade), "
        f"{sum(1 for s in solo if s['mand_partial'])} more have no absent element but lean on "
        f"partial (~) readings, {len(complete)} pair(s) cover them together. "
        "Verdicts are from DIGESTS (summaries) — run stage 2 to confirm the finalists against "
        "full text. Independent of every other score in the app." + note)
    bg.add_task(_auto_judge_combi_safe, tab_id)
    return {"ok": True, "scanned": scanned, "requested": len(docs), "batches": len(batches),
            "failed_batches": len(errors), "elements": len(elements),
            "complete": len(complete), "pairs": pairs, "solo": solo,
            "matrix": _combi_matrix(elements, _drop_benchmark([d for d in db.list_documents(tab_id, full=True) if d['status'] == 'fetched'], bm)), "depth": "digest"}


@app.post("/api/tabs/{tab_id}/combi-verify")
def combi_verify_ep(tab_id: int, body: schemas.CombiVerifyRequest, bg: BackgroundTasks):
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
    fresh = _drop_benchmark([d for d in db.list_documents(tab_id, full=True) if d.get("combi_coverage")], bm)
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
    bg.add_task(_auto_judge_combi_safe, tab_id)
    return {"ok": True, "verified": len(chosen) - len(errors), "failed": len(errors),
            "elements": len(elements), "complete": len(complete), "pairs": pairs,
            "solo": solo, "matrix": _combi_matrix(elements, _drop_benchmark([d for d in db.list_documents(tab_id, full=True) if d['status'] == 'fetched'], bm)), "depth": "full"}


# ---------- 🏆 chat-grade ideal pair ----------
# Born from a real divergence (2026-07-27): the chat, grounded on the benchmark's full
# claims+description and every candidate's stored verdict card, concluded that
# CN109964136 + EP2088659 covers every dependent claim — while the matrix, derived from
# the stricter per-element stage-2 read, held EP2088659 at rank 13 with ~/✗ cells. The
# fix is NOT to bend one to the other by hand each time: this endpoint runs the SAME
# chat-grade assessment on demand and then writes it INTO the stores the matrix renders
# from, so both views come from one verdict.

def _combi_ideal_payload(tab_id: int) -> dict | None:
    """The stored 🏆 verdict, with the pair's publication numbers resolved (a doc may have
    been deleted since — the verdict is then reported without cells to point at)."""
    row = db.get_combi_ideal(tab_id)
    if not row:
        return None
    for key, out in (("a_id", "a_number"), ("b_id", "b_number")):
        d = db.get_document(row[key])
        row[out] = (d or {}).get("number")
    return row


def _union_of(elements: list[dict], cov_a: list[dict], cov_b: list[dict]) -> list[dict]:
    """Per-element UNION of the pair's verdicts: the better status wins; `by` names the
    supplier(s) so the pinned verdict can say who brings what."""
    rank = {"yes": 2, "partial": 1, "no": 0}
    by_a = {c["name"]: c for c in cov_a or []}
    by_b = {c["name"]: c for c in cov_b or []}
    out = []
    for e in elements:
        sa = (by_a.get(e["name"], {}).get("status") or "no").lower()
        sb = (by_b.get(e["name"], {}).get("status") or "no").lower()
        best = sa if rank.get(sa, 0) >= rank.get(sb, 0) else sb
        by = ("both" if rank.get(sa, 0) and rank.get(sa, 0) == rank.get(sb, 0)
              else "A" if rank.get(sa, 0) > rank.get(sb, 0)
              else "B" if rank.get(sb, 0) else "")
        out.append({"name": e["name"], "kind": _kind(e), "weight": int(e.get("weight", 1)),
                    "status": best, "by": by})
    return out


_STATUS_SYM = {"yes": "✓", "partial": "~", "no": "✗"}
_KIND_LABEL = {"M": "Must elements", "A": "Additional features", "W": "Whole-document features"}


def _ideal_mapping_text(a_num: str, b_num: str, elements: list[dict],
                        cov_a: list[dict], cov_b: list[dict]) -> str:
    """The DETAILED per-element mapping of the 🏆 pair, posted to the chat: every element
    with its union verdict, which document supplies it, and the full-text evidence cite
    the phase-2 read returned. Grouped Must / Additional / Whole-document with the same
    ME/AE/WE codes the matrix uses, so chat and grid speak one language."""
    by_a = {c["name"]: c for c in cov_a or []}
    by_b = {c["name"]: c for c in cov_b or []}
    rank = {"yes": 2, "partial": 1, "no": 0}
    prefix, ctr = {"M": "ME", "A": "AE", "W": "WE"}, {"M": 0, "A": 0, "W": 0}
    sections: dict[str, list[str]] = {"M": [], "A": [], "W": []}
    for e in elements:
        k = _kind(e)
        ctr[k] += 1
        code = f"{prefix[k]}{ctr[k]}"
        sa = (by_a.get(e["name"], {}).get("status") or "no").lower()
        sb = (by_b.get(e["name"], {}).get("status") or "no").lower()
        union = sa if rank.get(sa, 0) >= rank.get(sb, 0) else sb
        parts = []
        for letter, num, st, cov in (("A", a_num, sa, by_a), ("B", b_num, sb, by_b)):
            if st == "no":
                continue
            ev = (cov.get(e["name"], {}).get("evidence") or "").strip()
            parts.append(f"{letter} {num} {_STATUS_SYM[st]}"
                         + (f" ({ev})" if ev else ""))
        line = f"{_STATUS_SYM.get(union, '✗')} {code} — {e['name']}: "
        line += "; ".join(parts) if parts else "not disclosed by either document"
        sections[k].append(line)
    out = [f"🏆 Detailed feature mapping for {a_num} (A) + {b_num} (B), from the "
           "full-text read of both documents:"]
    for k in ("M", "A", "W"):
        if sections[k]:
            out.append(f"\n{_KIND_LABEL[k]}:\n" + "\n".join(sections[k]))
    return "\n".join(out)


@app.post("/api/tabs/{tab_id}/combi/ideal")
def combi_ideal_ep(tab_id: int, body: schemas.CombiIdealRequest, bg: BackgroundTasks):
    """🏆 Run the canonical ideal-pair question through the CHAT pipeline (phase 1 —
    identical grounding and model choice, so the conclusion IS what the chat would say),
    then full-read the two chosen documents against the elements following that
    conclusion's affirmative readings (phase 2), and store everything the matrix needs:
    per-document cells (combi_coverage, depth full), the pair's combinability verdict,
    and the pinned tab-level 🏆 card. Both the chat and the matrix then show ONE verdict."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm or not (bm.get("features") or []):
        raise HTTPException(400, "combi needs benchmark features — define them first")
    elements = _combi_elements(bm)
    if len(_combi_mandatory(bm)) < 2:
        raise HTTPException(400, "combination analysis needs at least TWO mandatory "
                                 "elements — 🔬 Decompose the claim first")
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    documents = db.list_documents(tab_id, full=True)
    pool = _drop_benchmark([d for d in documents if d["status"] == "fetched"], bm)
    if len(pool) < 2:
        raise HTTPException(400, "need at least two fetched candidates")
    # FOCUS = the matrix anchor (the ranked best document): its full text is what the
    # chat run that motivated this feature was grounded on; every other candidate rides
    # along as its stored verdict card, exactly like the chat roster.
    matrix = _combi_matrix(elements, pool)
    anchor_id = (matrix["rows"][0]["id"] if matrix.get("rows") else None)
    focus = [d for d in pool if d["id"] == anchor_id] or None
    roster = [d for d in pool if not focus or d["id"] != focus[0]["id"]]
    # STATELESS on purpose — no chat history. The first version passed the tab's
    # conversation in, and a re-run after fresh reads just echoed the PREVIOUS 🏆
    # verdict sitting in that history instead of re-deriving from the updated cards
    # (bit 2026-07-27: opus re-reads flipped the anchor, the button still answered
    # the old pair). Each run must be grounded ONLY on the current stored data.
    question = claude_bridge.IDEAL_COMBI_QUESTION
    db.append_message(tab_id, "q", f"🏆 Ideal pair (chat-grade, {model}, "
                                   f"stateless re-run on current data): {question}")
    res = claude_bridge.chat(question + claude_bridge._IDEAL_PAIR_TRAILER,
                             history=None, documents=roster, model=model,
                             benchmark=bm, focus=focus, full=True)
    out_messages = []
    if "error" in res:
        out_messages.append(db.append_message(tab_id, "s", f"Claude error: {res['error']}"))
        return {"messages": out_messages, "error": res["error"]}
    pair = claude_bridge.parse_ideal_pair(res["answer"])
    prose = claude_bridge.strip_ideal_trailer(res["answer"])
    participants = [{"kind": "model", "title": model},
                    {"kind": "psa", "title": "🏆 ideal pair (chat-grade)"},
                    {"kind": "benchmark", "title": _benchmark_label(bm)}]
    if focus:
        participants.append({"kind": "documents",
                             "title": f"anchor {focus[0].get('number')} (full text)"})
    out_messages.append(db.append_message(tab_id, "c", _verify_citations(tab_id, prose),
                                          model=model, participants=participants))

    def _fail(note: str) -> dict:
        out_messages.append(db.append_message(tab_id, "s", note))
        return {"messages": out_messages, "ok": False, "ideal": _combi_ideal_payload(tab_id)}

    if not pair:
        return _fail("🏆 The answer carried no machine-readable IDEAL PAIR line — the "
                     "matrix was NOT updated. Re-run, or read the verdict above.")
    by_base = {}
    for d in pool:
        by_base.setdefault(db._number_base(d.get("number") or "").upper(), d)
    docs_ab = [by_base.get(db._number_base(n).upper()) for n in pair]
    if not all(docs_ab):
        missing = [n for n, d in zip(pair, docs_ab) if not d]
        return _fail(f"🏆 The chat chose {' + '.join(pair)}, but "
                     f"{', '.join(missing)} is not among this tab's fetched candidates — "
                     "the matrix was NOT updated. Fetch it into the tab and re-run.")
    doc_a, doc_b = docs_ab
    # PHASE 2 — convert the prose verdict into per-element, per-document cells against
    # both FULL texts (the affirmative, stretch-allowed read the analysis argued for).
    ver = claude_bridge.combi_ideal_verify(elements, doc_a, doc_b, prose, model=model)
    if "error" in ver:
        return _fail(f"🏆 Pair chosen ({doc_a['number']} + {doc_b['number']}), but the "
                     f"full-text element read failed: {ver['error']} — matrix not updated.")
    results = ver.get("results") or {}
    cov_a = results.get(doc_a["number"]) or results.get(pair[0])
    cov_b = results.get(doc_b["number"]) or results.get(pair[1])
    if not cov_a or not cov_b:
        return _fail(f"🏆 Pair chosen ({doc_a['number']} + {doc_b['number']}), but the "
                     "element read returned no parsable verdict block for "
                     f"{'both documents' if not cov_a and not cov_b else (doc_a['number'] if not cov_a else doc_b['number'])} — matrix not updated.")
    for d, cov in ((doc_a, cov_a), (doc_b, cov_b)):
        db.update_document(d["id"], combi_coverage=json.dumps(cov, ensure_ascii=False),
                           combi_depth="full")
    db.set_combi_motivation(tab_id, doc_a["id"], doc_b["id"], bool(ver.get("combinable")),
                            ver.get("reason") or "🏆 chat-grade ideal pair", model)
    union = _union_of(elements, cov_a, cov_b)
    mand_u = [u for u in union if u["kind"] == "M"]
    m_yes = sum(1 for u in mand_u if u["status"] == "yes")
    m_part = sum(1 for u in mand_u if u["status"] == "partial")
    open_m = [u["name"] for u in mand_u if u["status"] == "no"]
    db.set_combi_ideal(tab_id, doc_a["id"], doc_b["id"],
                       {"answer": prose, "union": union,
                        "combinable": bool(ver.get("combinable")),
                        "reason": ver.get("reason") or "",
                        "mand_yes": m_yes, "mand_partial": m_part,
                        "mand_total": len(mand_u), "open": open_m}, model)
    # The DETAILED per-element mapping in the chat — the verdict must be readable there
    # in full (element, supplier, evidence cite), not only as matrix cells.
    out_messages.append(db.append_message(
        tab_id, "c",
        _ideal_mapping_text(doc_a["number"], doc_b["number"], elements, cov_a, cov_b),
        model=model, participants=[{"kind": "model", "title": model},
                                   {"kind": "psa", "title": "🏆 feature mapping"}]))
    out_messages.append(db.append_message(
        tab_id, "s",
        f"🏆 Ideal pair {doc_a['number']} + {doc_b['number']}: union covers "
        f"{m_yes}✓{f' +{m_part}~' if m_part else ''} of {len(mand_u)} Must element(s)"
        + (f"; still open: {'; '.join(open_m)}" if open_m else "")
        + f". {'Combinable' if ver.get('combinable') else '⛔ NOT combinable'}"
        + (f" — {ver['reason']}" if ver.get("reason") else "") + ". Both documents were "
        "re-read on FULL text following this verdict; their matrix cells and the pair "
        "pin now reflect it."))
    bg.add_task(_auto_judge_combi_safe, tab_id)
    fresh = _drop_benchmark([d for d in db.list_documents(tab_id, full=True)
                             if d.get("combi_coverage")], bm)
    return {"ok": True, "messages": out_messages, "ideal": _combi_ideal_payload(tab_id),
            "pairs": _combi_pairs(elements, fresh, 20), "solo": _combi_solo(elements, fresh),
            "matrix": _combi_matrix(elements, _drop_benchmark(
                [d for d in db.list_documents(tab_id, full=True) if d["status"] == "fetched"], bm)),
            "depth": "full"}


def _judge_combi_pairs(tab_id: int, bm: dict, pairs: list[dict], keys: list[tuple],
                       model: str | None = None, mode: str = "must") -> tuple[dict, list]:
    """Bulk motivation-to-combine judge shared by the ⚖️ button endpoint and the
    post-deep-read auto run. BATCHED so every pair is judged in one pass even past a
    single call's comfortable size (set-cover can surface many partners) — a blank
    row must never be mistaken for "not combinable"; it means "not yet judged".
    Persists each verdict; returns ({'lo-hi': verdict}, [batch errors])."""
    out, errors = {}, []
    lock = threading.Lock()

    def one(start: int) -> None:
        chunk = pairs[start:start + COMBI_MOTIV_BATCH]
        chunk_keys = keys[start:start + COMBI_MOTIV_BATCH]
        res = claude_bridge.combi_motivation(bm, chunk, model=model, mode=mode)
        if "error" in res:
            with lock:
                errors.append(res["error"])
            return
        results = res.get("results") or {}
        for i, (a_id, b_id) in enumerate(chunk_keys, 1):
            v = results.get(str(i))
            if not v:
                continue
            db.set_combi_motivation(tab_id, a_id, b_id, v["combinable"], v.get("reason") or "",
                                    res.get("model"))
            lo, hi = sorted((a_id, b_id))
            with lock:
                out[f"{lo}-{hi}"] = {"combinable": v["combinable"], "reason": v.get("reason") or "",
                                     "model": res.get("model")}

    with ThreadPoolExecutor(max_workers=DIGEST_WORKERS) as ex:
        list(ex.map(one, range(0, len(pairs), COMBI_MOTIV_BATCH)))
    return out, errors


def _auto_judge_combinability(tab_id: int, bm: dict | None) -> dict:
    """🧩 Run after a batch deep read: the read just paid for fresh per-element
    coverage, so the matrix's anchor+partner pairs are derivable for free — judge
    their motivation-to-combine right away instead of waiting for the ⚖️ click.
    INCREMENTAL like everything else: pairs with a stored verdict are skipped, so a
    re-read or the next batch only bills the genuinely new pairs."""
    els = _combi_elements(bm)
    if not els:
        return {"judged": 0}
    docs = _drop_benchmark([d for d in db.list_documents(tab_id, full=True)
                            if d["status"] == "fetched"], bm)
    matrix = _combi_matrix(els, docs)
    rows = matrix.get("rows") or []
    anchor = rows[0] if rows else None
    partners = [r for r in rows[1:] if r.get("fills")]
    if not anchor or not partners:
        return {"judged": 0}                 # solo coverer / nothing assessed → no pairs
    covered = [c["name"] for c, s in zip(matrix.get("columns") or [],
                                         anchor.get("cells") or [])
               if s in ("yes", "partial")]
    existing = db.get_combi_motivations(tab_id)
    by_id = {d["id"]: d for d in docs}
    pairs, keys = [], []
    for p in partners:
        lo, hi = sorted((anchor["id"], p["id"]))
        if f"{lo}-{hi}" in existing:
            continue
        a, b = by_id.get(anchor["id"]), by_id.get(p["id"])
        if not a or not b:
            continue
        pairs.append({"a": a, "b": b,
                      "a_features": covered or ["(covers the mandatory elements)"],
                      "b_features": p["fills"]})
        keys.append((anchor["id"], p["id"]))
    if not pairs:
        return {"judged": 0}
    mode = "additional" if matrix.get("mode") == "additional" else "must"
    out, errors = _judge_combi_pairs(tab_id, bm, pairs, keys, model=None, mode=mode)
    combinable = sum(1 for v in out.values() if v["combinable"])
    if out or errors:
        db.append_message(tab_id, "s",
            f"🧩 Combinability auto-judged after the read: {len(out)} new anchor pair(s) — "
            f"{combinable} genuinely combinable, {len(out) - combinable} not; already-judged "
            "pairs were skipped, verdicts are in the 🔎 coverage matrix."
            + (f" ⚠ {len(errors)} batch(es) failed — ⚖️ re-judge in the matrix retries them."
               if errors else ""))
    return {"judged": len(out), "combinable": combinable, "errors": len(errors)}


def _auto_judge_combi_safe(tab_id: int) -> None:
    """Judge fresh matrix pairs after ANY operation that changed coverage (deep read,
    ➕ additional read, ♻️ re-check, 🔎 scan/verify) — so the matrix's 🔗/⛔ badges are
    never stale just because the partner set shifted. Already-judged pairs cost
    nothing; never raises into the caller."""
    if not AUTO_COMBI:
        return
    try:
        _auto_judge_combinability(tab_id, db.get_benchmark(tab_id))
    except Exception as e:
        db.append_message(tab_id, "s", f"🧩 combinability auto-judge failed: {e} — "
                          "⚖️ Judge combinability in the matrix runs it manually.")


@app.post("/api/tabs/{tab_id}/combi/auto-judge")
def combi_auto_judge_ep(tab_id: int):
    """Judge the CURRENT matrix's unjudged anchor+partner pairs now (same incremental
    judge the automatic triggers use) — for a matrix showing ⚪ after operations that
    predate the auto-judge."""
    _tab_or_404(tab_id)
    bm = db.get_benchmark(tab_id)
    if not bm or not (bm.get("features") or []):
        raise HTTPException(400, "combi needs benchmark features — define them first")
    return {"ok": True, **_auto_judge_combinability(tab_id, bm)}


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
    for p in body.pairs[:COMBI_MOTIV_MAX]:            # bounded, but ALL matrix partners fit
        a, b = by_id.get(p.a_id), by_id.get(p.b_id)
        if not a or not b:
            continue
        pairs.append({"a": a, "b": b, "a_features": p.a_features, "b_features": p.b_features})
        keys.append((p.a_id, p.b_id))
    if not pairs:
        raise HTTPException(400, "no valid fetched document pairs to judge")
    model = _read_model(body.model)
    out, errors = _judge_combi_pairs(tab_id, bm, pairs, keys, model=model,
                                     mode=body.mode if body.mode in ("must", "additional") else "must")
    if not out and errors:
        raise HTTPException(400, f"combi motivation failed: {errors[0]}")
    combinable = sum(1 for v in out.values() if v["combinable"])
    note = f" ⚠ {len(errors)} batch(es) failed — re-run to judge the rest." if errors else ""
    db.append_message(tab_id, "s",
        f"🧩 Judged combinability of {len(out)} document pair(s) ({model or 'sonnet'}) — "
        f"{combinable} genuinely combinable, {len(out) - combinable} not (different field / no "
        "motivation to combine). Verdicts are a hint of what a 2-reference combination achieves; "
        f"they do NOT change any single document's score.{note}")
    return {"ok": True, "results": out, "model": model}


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
DEBATE_MODEL = os.environ.get("PB_DEBATE_MODEL", "claude-opus-5")


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
    ok, why = nlm_bridge.available(_tab_profile(tab_id))
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
    question = _nlm_question(NLM_DEBATE_PROMPT, spec, spec_key="spec", finalists=nums)
    if cfg.get("notebook_id"):
        titles = {n["id"]: n["title"]
                  for n in (nlm_bridge.list_notebooks(profile=_tab_profile(tab_id))
                            .get("notebooks") or [])}
        qres = _nlm_query_cached(cfg["notebook_id"], question, source_ids=None,
                                 force=bool(not_in_nb), profile=_tab_profile(tab_id))
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
    so a limited token budget is spent on the best candidates before it runs out.
    With no score at all, a doc the 🔬 mega-screen explicitly REJECTED ranks below a
    never-screened unknown (a graduate keeps the unknowns' rank — its real rank comes
    from nlm_rank once the finalize writes the shortlist)."""
    vals = [v for v in (d.get("score"), d.get("nlm_score")) if v is not None]
    if vals:
        return sum(vals) / len(vals)
    return -2.0 if d.get("nlm_screen_state") == "rejected" else -1.0


# ---------- ⏳ token-limit watchdog: auto-resume the read when the window resets ----------
# The subscription window can exhaust MID-read ("You've hit your session limit ·
# resets 8:30pm (UTC)"): every further call fails until the reset, so the batch
# used to grind to the end earning nothing and the user had to notice and click
# ▶️ Continue hours later. Now the read stops at the first full worker-window of
# limit errors, the request is persisted next to the DB (survives a container
# restart), and a watchdog thread wakes at the announced reset time (fallback:
# periodic probe), verifies the window is open with one tiny call, and relaunches
# the SAME deep-compare in Continue mode — already-read candidates are skipped;
# if everything was read and only the ranking failed, it re-ranks with zero
# re-reading. Manual ▶️ Continue before the reset stays possible and harmless.
_LIMIT_ERR = re.compile(r"(session|usage|5-hour|weekly)\s+limit", re.IGNORECASE)
_LIMIT_RESET = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(UTC\)", re.IGNORECASE)
_LIMIT_MARGIN = 180             # relaunch this many seconds after the announced reset
_LIMIT_PROBE_EVERY = 15 * 60    # re-probe cadence when no reset time is announced
_limit_watchdogs: dict[int, threading.Thread] = {}
_limit_watchdogs_mu = threading.Lock()


def _limit_resume_path(tab_id: int) -> str:
    return os.path.join(os.path.dirname(db.DB_PATH) or ".",
                        f".claude_read_{tab_id}.resume.json")


def _parse_limit_reset(err: str) -> float | None:
    """'… resets 8:30pm (UTC)' → epoch of the NEXT such UTC wall-clock time."""
    m = _LIMIT_RESET.search(err or "")
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    at = datetime.now(timezone.utc).replace(hour=h, minute=int(m.group(2) or 0),
                                            second=0, microsecond=0)
    if at <= datetime.now(timezone.utc):
        at += timedelta(days=1)
    return at.timestamp()


def _arm_limit_watchdog(tab_id: int, *, doc_ids: list[int], model: str, read_model: str,
                        skills: list[str], question: str, err: str) -> None:
    reset_ep = _parse_limit_reset(err)
    resume_at = (reset_ep + _LIMIT_MARGIN) if reset_ep else (time.time() + _LIMIT_PROBE_EVERY)
    state = {"tab_id": tab_id, "doc_ids": doc_ids, "model": model,
             "read_model": read_model, "skills": list(skills or []),
             "question": question, "resume_at": resume_at, "armed_at": db._now(),
             "err": (err or "")[:300]}
    with open(_limit_resume_path(tab_id), "w", encoding="utf-8") as f:
        json.dump(state, f)
    when = (datetime.fromtimestamp(resume_at, timezone.utc).strftime("%H:%M UTC")
            if reset_ep else f"~{_LIMIT_PROBE_EVERY // 60} min (no reset time announced; will probe)")
    db.append_message(
        tab_id, "s",
        f"⏳ Token-limit watchdog ARMED — will auto-resume this deep-read/ranking in "
        f"Continue mode around {when}. Already-read candidates are never re-read; "
        "nothing to click. Survives a container restart. A manual ▶️ Continue before "
        "then is fine — once the work completes, the watchdog stands down.")
    _spawn_limit_watchdog(tab_id)


def _disarm_limit_watchdog(tab_id: int) -> None:
    try:
        os.unlink(_limit_resume_path(tab_id))
    except OSError:
        pass


def _spawn_limit_watchdog(tab_id: int) -> None:
    with _limit_watchdogs_mu:
        t = _limit_watchdogs.get(tab_id)
        if t and t.is_alive():
            return
        t = threading.Thread(target=_limit_watchdog_loop, args=(tab_id,), daemon=True)
        _limit_watchdogs[tab_id] = t
        t.start()


def _limit_watchdog_loop(tab_id: int) -> None:
    path = _limit_resume_path(tab_id)
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except OSError:
            return                              # disarmed (completed or user deleted)
        except ValueError:
            os.unlink(path)
            return
        wait_s = state.get("resume_at", 0) - time.time()
        if wait_s > 0:
            time.sleep(min(wait_s, 300))        # re-read the file every ≤5 min (re-arms shift it)
            continue
        if _claude_read_running(tab_id):        # a manual run is in flight — let it finish
            time.sleep(300)
            continue
        # the window should be open — verify with one tiny call before relaunching
        probe = claude_bridge._run_claude("Reply with exactly: OK",
                                          claude_bridge.DIGEST_MODEL, timeout=180)
        if "error" in probe:
            nxt = _parse_limit_reset(probe["error"])
            state["resume_at"] = ((nxt + _LIMIT_MARGIN) if nxt
                                  else time.time() + _LIMIT_PROBE_EVERY)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state, f)
            except OSError:
                return
            continue
        _disarm_limit_watchdog(tab_id)          # relaunching now; a new limit hit re-arms
        db.append_message(tab_id, "s",
                          "⏳→▶️ Watchdog: token window is open again — resuming the "
                          "deep-read/ranking (Continue mode, already-read skipped).")
        body = schemas.DeepCompareRequest(
            model=state.get("model"), skills=state.get("skills") or [],
            question=state.get("question"), doc_ids=state.get("doc_ids") or None,
            reading_model=state.get("read_model"), skip_scored=True)
        try:
            deep_compare(tab_id, body)
        except HTTPException as e:
            db.append_message(tab_id, "s", f"⏳ Watchdog could not resume: {e.detail}")
        return


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
        # AUTH CIRCUIT BREAKER — a revoked/expired OAuth token fails EVERY call the
        # same way, so once a full worker-window has produced nothing but auth errors
        # the rest of the batch cannot fare better. Without this the pool ground
        # through all candidates earning nothing and the UI sat on "assessing 0/N"
        # (bit 2026-07-28: 577 × 401 after a container-recreate rotated the token).
        _auth_err = re.compile(r"401|OAuth|revoked|authenticat", re.IGNORECASE)
        verdicts, paused, auth_dead, limit_dead = [], False, False, False
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
                if (not any(v["ok"] for v in verdicts)
                        and len(verdicts) >= DIGEST_WORKERS
                        and all(_auth_err.search(v["verdict"] or "") for v in verdicts)):
                    auth_dead = True                  # nothing will succeed — stop the batch
                    for fut in pending:
                        verdicts.append(fut.result())
                    pending.clear()
                    break
                # LIMIT CIRCUIT BREAKER — the subscription window exhausted mid-run:
                # once a full worker-window of consecutive results are all limit
                # errors, the rest of the batch cannot fare better until the window
                # resets. Stop burning time; the watchdog below auto-resumes.
                tail = verdicts[-DIGEST_WORKERS:]
                if (len(tail) >= DIGEST_WORKERS
                        and all((not v["ok"]) and _LIMIT_ERR.search(v["verdict"] or "")
                                for v in tail)):
                    limit_dead = True                 # nothing succeeds until the reset
                    for fut in pending:
                        verdicts.append(fut.result())
                    pending.clear()
                    break
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
        if auth_dead:
            db.append_message(
                tab_id, "s",
                f"⛔ Deep-read ABORTED after {len(verdicts)} attempt(s): every call failed "
                "with an AUTHENTICATION error (the container's Claude token is revoked or "
                f"expired) — e.g. {verdicts[0]['verdict'][:160]}. Nothing was assessed or "
                "overwritten. Reseed the token "
                "(`bash ~/.claude/scripts/reseed-claude-containers.sh` on the host side), "
                "then ▶️ Continue deep-read — it resumes exactly these candidates.")
            return
        read = sum(1 for v in verdicts if v["ok"])
        failed = len(verdicts) - read
        if limit_dead:
            last_err = next((v["verdict"] for v in reversed(verdicts)
                             if not v["ok"] and _LIMIT_ERR.search(v["verdict"] or "")), "")
            db.append_message(
                tab_id, "s",
                f"⛔ Token window EXHAUSTED — assessed {read}/{len(docs)} this run "
                f"({read_model}); {len(docs) - read} left unread (their reads were NOT "
                f"wasted attempts — nothing was overwritten). Error: {last_err[:200]}")
            _arm_limit_watchdog(tab_id, doc_ids=doc_ids, model=model,
                                read_model=read_model, skills=skills,
                                question=question, err=last_err)
            return                                    # the watchdog finishes the job
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
        corpus = _drop_benchmark([d for d in db.list_documents(tab_id, full=True)
                  if d["status"] == "fetched" and _has_assessment(d)], bm)
        # Order + annotate by the SAME unified Must-first key the list/matrix use, so the chat
        # ranking cannot contradict the coverage grid. Each card carries its Must coverage,
        # and the reduce is told Must dominates (rank_rule below).
        els = _combi_elements(bm) if bm_features else []

        def _rank_key(d):
            return _unified_score(els, d)["key"] if els else _promise(d)

        def _cov_line(d):
            if not els:
                return None
            u = _unified_score(els, d)
            if not u["assessed"]:
                return None
            return (f"MUST {u['mand_full']}✓" + (f"+{u['mand_partial']}~" if u["mand_partial"] else "")
                    + f"/{u['mand_total']} (weighted {u['mand_rating']}/10, covers-all="
                    + ("YES" if u["covers_all"] else "no") + ")"
                    + (f"; +A{u['add_bonus']}" if u["add_total"] else "")
                    + (f"; +W{u['w_bonus']}" if u["w_total"] else ""))

        corpus.sort(key=lambda d: (_rank_key(d), d["id"]), reverse=True)
        # APP RANK stamped on every card: the reduce model gets the app's OWN order
        # (the same unified key the list and matrix use), so its output can be
        # checked against it line by line instead of drifting to a holistic order.
        n_corpus = len(corpus)
        reduce_verdicts = [{"number": d["number"], "title": d.get("title"),
                            "coverage": (f"APP RANK {i}/{n_corpus}"
                                         + (f"; {_cov_line(d)}" if _cov_line(d) else "")),
                            "verdict": _stored_assessment(d)}
                           for i, d in enumerate(corpus, 1) if _stored_assessment(d)]
        rank_rule = (
            "RANKING RULE — rank by MUST/CORE coverage FIRST. A document (or two-document "
            "combination) that discloses EVERY mandatory element outranks one that misses any, "
            "whatever their additional/whole-document features. Additional (A) and whole-"
            "document (W) features are BONUS only: they separate documents that are EQUAL on the "
            "mandatory elements; their absence never lowers a rank. Each card shows its MUST "
            "coverage — rank consistently with it, and state each finalist's mandatory coverage "
            "explicitly (e.g. '11 of 11' vs '10 of 11, one partial').\n"
            "ALIGNMENT IS MANDATORY: every card carries 'APP RANK n/total' — the app's own "
            "coverage-matrix order, computed from the same per-element verdicts you were given. "
            "Your final ranking must present the candidates in APP RANK order. You may deviate "
            "for a specific document ONLY by writing a line 'DEVIATION from app rank N: "
            "<reason>' citing the exact paragraph/claim that justifies it — a ranking that "
            "silently contradicts the matrix is WRONG. Note that a document's overall match "
            "score (0-10 vs the benchmark text) measures something DIFFERENT from element "
            "coverage; when they disagree, say one sentence explaining which one your rank "
            "follows and why." if els else None)
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
                                        model=model, history=history, rank_rule=rank_rule)
        if "error" in res:
            db.append_message(tab_id, "s", f"Claude error compiling the ranking: {res['error']}")
            if _LIMIT_ERR.search(res["error"] or ""):
                # reads are stored; only the ranking is missing → the watchdog's
                # Continue relaunch will re-rank from stored assessments (0 re-reads)
                _arm_limit_watchdog(tab_id, doc_ids=doc_ids, model=model,
                                    read_model=read_model, skills=skills,
                                    question=question, err=res["error"])
        else:
            _disarm_limit_watchdog(tab_id)     # ranking delivered — armed job (if any) is done
            db.append_message(tab_id, "c", _verify_citations(tab_id, res["answer"]),
                              model=model, participants=participants)
            for les in res.get("lessons", []):
                saved = lessons.append_lesson(les["skill"], les["lesson"])
                db.append_message(tab_id, "s",
                                  f"Lesson auto-appended to skill /{les['skill']} (references/lessons.md)."
                                  if saved.get("ok") else
                                  f"Lesson for /{les['skill']} NOT saved: {saved.get('error')}\n\n{les['lesson']}")
            # 🧩 The batch is read and ranked → finish the combination analysis too:
            # judge the fresh matrix pairs' combinability without waiting for the ⚖️
            # click. Skipped when the reduce failed (same quota would sink it) and
            # NEVER allowed to kill the read job it rides on.
            if bm_features:
                _auto_judge_combi_safe(tab_id)
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
    # most-promising first (by prior score), so a capped batch spends on the best candidates
    to_read.sort(key=lambda d: (_promise(d), d["id"]), reverse=True)
    remaining_after = 0
    if body.batch and len(to_read) > body.batch:       # this run = the top `batch` only
        remaining_after = len(to_read) - body.batch
        to_read = to_read[:body.batch]
    target_ids = [d["id"] for d in to_read]
    model = body.model if body.model in claude_bridge.MODELS else claude_bridge.CHAT_MODEL
    question = (body.question or "").strip() or DEEP_DEFAULT_QUESTION
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
    # ⚠️ no-features guard (tab-11 double-spend lesson: a 108-doc read ran twice
    # because it started BEFORE the feature list was accepted → verdicts holistic-only,
    # feature_scores NULL, 🧮 Recalc structurally impossible). Warn loudly at start —
    # the reads still run (holistic mode is legitimate), but the cost trade-off is
    # stated BEFORE the tokens are spent, not discovered after.
    features_missing = bool(to_read) and not (bm.get("features") or [])
    if features_missing:
        db.append_message(
            tab_id, "s",
            f"⚠️ The benchmark has NO accepted feature list — the {len(to_read)} read(s) "
            "starting now will be HOLISTIC-ONLY: no per-feature ✓/~/✗ verdicts, so the "
            "⚖ weighted ranking, 🧩 Combi and 🧮 Recalc cannot use them, and adding "
            "features later means RE-READING everything. If you want feature scoring, "
            "⏸ stop now, run 🔬 Decompose (accept the features), then ▶️ Continue.")
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
    return {"started": True, "running": True, "batch_size": len(target_ids),
            "remaining_after": remaining_after, "features_missing": features_missing,
            **_claude_read_counts(tab_id)}


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

# Re-arm any ⏳ token-limit watchdog that was pending when the container stopped —
# the state files live next to the DB, so a rebuild/restart resumes the wait
# instead of silently dropping the promised auto-continue.
for _p in glob.glob(os.path.join(os.path.dirname(db.DB_PATH) or ".",
                                 ".claude_read_*.resume.json")):
    _m = re.search(r"\.claude_read_(\d+)\.resume\.json$", _p)
    if _m:
        _spawn_limit_watchdog(int(_m.group(1)))

# Same for a 🔬 mega-screen that was quota-paused when the container stopped —
# its state file survives; the watchdog resumes the wait instead of dropping it.
for _p in glob.glob(os.path.join(os.path.dirname(db.DB_PATH) or ".", ".nlm_screen_*.json")):
    _m = re.search(r"\.nlm_screen_(\d+)\.json$", _p)
    if _m and ((_screen_read(int(_m.group(1))) or {}).get("quota") or {}).get("paused"):
        _spawn_screen_watchdog(int(_m.group(1)))

# 🔁 boot sweep for fetches orphaned by the restart (status='pending' with no worker)
if AUTO_REFETCH:
    threading.Thread(target=_auto_refetch_sweep, daemon=True).start()
