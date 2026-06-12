"""Patent Workbench FastAPI app — tabs, documents, chat, NotebookLM, lessons."""
from __future__ import annotations

import os
import uuid

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import claude_bridge, db, extract, fetcher, lessons, nlm_bridge, patents
from . import schemas

UPLOADS = os.environ.get("PB_UPLOADS", "/data/uploads")
MAX_UPLOAD = 25 * 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

app = FastAPI(title="Patent Workbench")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": f"{exc.__class__.__name__}: {exc}"})


def _tab_or_404(tab_id: int) -> None:
    if not db.tab_exists(tab_id):
        raise HTTPException(404, "tab not found")


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
            "default_model": claude_bridge.CHAT_MODEL}


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


@app.get("/api/tabs/{tab_id}/state")
def tab_state(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_documents(tab_id)
    for d in docs:
        d["links"] = patents.links(d["number"])
    return {"documents": docs,
            "messages": db.list_messages(tab_id),
            "notebook": db.get_notebook_config(tab_id)}


# ---------- documents ----------

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
    for doc_id in res["inserted"]:
        bg.add_task(_fetch_into_db, doc_id)
    return res


@app.get("/api/tabs/{tab_id}/documents")
def documents_list(tab_id: int):
    _tab_or_404(tab_id)
    docs = db.list_documents(tab_id)
    for d in docs:
        d["links"] = patents.links(d["number"])
    return {"documents": docs}


@app.post("/api/tabs/{tab_id}/documents/{doc_id}/refetch")
def document_refetch(tab_id: int, doc_id: int, bg: BackgroundTasks):
    doc = db.get_document(doc_id)
    if not doc or doc["tab_id"] != tab_id:
        raise HTTPException(404, "document not found")
    db.update_document(doc_id, status="pending", error=None)
    bg.add_task(_fetch_into_db, doc_id)
    return {"ok": True}


@app.delete("/api/tabs/{tab_id}/documents/{doc_id}")
def document_delete(tab_id: int, doc_id: int):
    if not db.delete_document(tab_id, doc_id):
        raise HTTPException(404, "document not found")
    return {"ok": True}


# ---------- upload (photo / PDF / txt → candidate numbers) ----------

@app.post("/api/tabs/{tab_id}/upload")
async def upload(tab_id: int, file: UploadFile = File(...)):
    _tab_or_404(tab_id)
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (25 MB max)")
    name = os.path.basename(file.filename or "upload")
    ext = os.path.splitext(name)[1].lower()
    tab_dir = os.path.join(UPLOADS, str(tab_id))
    os.makedirs(tab_dir, exist_ok=True)
    path = os.path.join(tab_dir, f"{uuid.uuid4().hex[:8]}-{name}")
    with open(path, "wb") as fh:
        fh.write(data)

    if ext in IMAGE_EXT:
        kind, res = "image", extract.numbers_from_image(path)
    elif ext == ".pdf":
        kind, res = "pdf", extract.numbers_from_pdf(path)
    else:
        kind = "text"
        try:
            res = extract.numbers_from_text(data.decode("utf-8", errors="replace"))
        except Exception as exc:
            res = {"error": f"could not read file as text: {exc}"}
    db.record_upload(tab_id, path, name, kind)
    if "error" in res:
        return {"kind": kind, "error": res["error"], "numbers": []}
    return {"kind": kind, "numbers": res["numbers"]}


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
    db.set_notebook_config(tab_id, body.notebook_id, body.notebook_title, body.source_ids)
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
    skill_blocks = []
    for name in body.skills:
        content = claude_bridge.load_skill(name)
        if content:
            skill_blocks.append({"name": name, "content": content})
            participants.append({"kind": "skill", "title": name})

    res = claude_bridge.chat(body.question, history=history, documents=documents,
                             sources=nlm_sources, skills=skill_blocks, model=model)
    if "error" in res:
        out_messages.append(db.append_message(tab_id, "s", f"Claude error: {res['error']}"))
        return {"messages": out_messages, "error": res["error"]}

    participants.insert(0, {"kind": "model", "title": model})
    if body.use_documents and documents:
        participants.append({"kind": "documents", "title": f"{len(documents)} docs"})
    out_messages.append(db.append_message(tab_id, "c", res["answer"], model=model,
                                          participants=participants))

    for les in res.get("lessons", []):
        saved = lessons.append_lesson(les["skill"], les["lesson"])
        note = (f"Lesson auto-appended to skill /{les['skill']} (references/lessons.md)."
                if saved.get("ok") else
                f"Lesson for /{les['skill']} NOT saved: {saved.get('error')}\n\n{les['lesson']}")
        out_messages.append(db.append_message(tab_id, "s", note))
    return {"messages": out_messages}


# ---------- lessons (manual save) ----------

@app.post("/api/lessons")
def lesson_create(body: schemas.LessonCreate):
    res = lessons.append_lesson(body.skill, body.lesson)
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


# ---------- static frontend ----------

_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=_static, html=True), name="static")
