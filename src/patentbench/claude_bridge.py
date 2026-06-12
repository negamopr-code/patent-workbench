"""Headless `claude -p` chat for the Patent Workbench.

Adapted from antimg-web's claude_bridge: the image bakes the Claude Code CLI;
deploy/entrypoint.sh seeds OAuth credentials from a READ-ONLY /seed mount of the
operator's ~/.claude into the container's own CLAUDE_CONFIG_DIR (copy, not mount).

Pure text-in/text-out, stateless per turn: the API rebuilds one prompt from the
tab's persisted history + stored patent documents + selected skill doctrines +
optional NotebookLM answers. Degrades gracefully when the CLI or credentials are
missing — document management and NotebookLM Q&A keep working.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Latest first — Fable 5 is the default; the UI offers the rest as a dropdown.
MODELS = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-fable-5")
EXTRACT_MODEL = os.environ.get("CLAUDE_EXTRACT_MODEL", "claude-haiku-4-5")
# Reading patent numbers off a photo is high-stakes (one wrong digit = a WRONG but
# existing patent gets fetched silently) → strongest model; it's a single call.
OCR_MODEL = os.environ.get("PB_OCR_MODEL", "claude-fable-5")
# Page transcription is bulk work (one call per page) but feeds the benchmark text
# the whole comparison anchors on → sonnet as the cost/quality middle ground.
TRANSCRIBE_MODEL = os.environ.get("PB_TRANSCRIBE_MODEL", "claude-sonnet-4-6")
CHAT_TIMEOUT = float(os.environ.get("CLAUDE_CHAT_TIMEOUT", "240"))
SKILLS_DIR = os.environ.get("CLAUDE_SKILLS_DIR", os.path.expanduser("~/.claude/skills"))

MAX_HISTORY = 24            # turns kept in the prompt
MAX_TURN_CHARS = 4000       # each history turn clipped
MAX_SKILL_CHARS = 16_000    # each skill doctrine clipped
MAX_DOC_CHARS = 6000        # each stored document clipped (abstract+claims first)
MAX_DOCS_CHARS = 60_000     # total document budget per prompt

_PREAMBLE = (
    "You are the assistant of a Patent Workbench — a multi-tab patent project app. "
    "Each tab holds ONE BENCHMARK document (the reference — a patent number or an "
    "uploaded document/page photos) and a pool of CANDIDATE documents; the typical "
    "task is to find which candidate(s) best fit the benchmark, comparing features, "
    "claims and embodiments. A NotebookLM notebook may back the tab as well. "
    "Ground every claim about a document in its actual stored text and CITE the "
    "publication number (e.g. US10395648B1) when you do. Be precise about claim "
    "language vs description language. Honestly flag gaps: if a document's text "
    "was not fetched or a question goes beyond the provided material, say so "
    "instead of guessing. This is analytical assistance, not legal advice."
)

MAX_BENCHMARK_CHARS = 40_000   # the benchmark gets a larger budget than candidates

_LESSON_INSTRUCTION = (
    "SELF-IMPROVEMENT: if (and ONLY if) this exchange surfaced a durable, "
    "generalizable lesson that future patent analyses should follow (a method "
    "correction, a pitfall, a guideline clarification — not case-specific facts), "
    "end your answer with a separate final line per lesson, formatted EXACTLY:\n"
    "LESSON[<skill-name>]: <one concise paragraph>\n"
    "where <skill-name> is one of the skills listed above. Most answers need none."
)

_ROLE = {"q": "User", "a": "Notebook", "c": "Claude", "s": "System"}

LESSON_RE = re.compile(r"^LESSON\[([\w][\w-]*)\]:\s*(.+)$", re.MULTILINE)


def available() -> tuple[bool, str]:
    if shutil.which(CLAUDE_BIN) is None and not os.path.exists(CLAUDE_BIN):
        return False, (f"claude CLI not found ({CLAUDE_BIN}) — rebuild the container "
                       "with scripts/serve.sh")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    if not os.path.exists(os.path.join(cfg, ".credentials.json")):
        return False, ("claude credentials not seeded — the container needs the "
                       "read-only /seed mount of the host ~/.claude (scripts/serve.sh)")
    return True, ""


def _skill_description(path: str) -> str:
    """Short description for the UI: the frontmatter `description:` value if the
    SKILL.md starts with a YAML block, else the first non-empty line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(8000).splitlines()
    except OSError:
        return ""
    if lines and lines[0].strip() == "---":
        for line in lines[1:40]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip().strip("\"'")[:160]
    for line in lines:
        line = line.strip()
        if line and line != "---":
            return line.lstrip("#").strip()[:160]
    return ""


def list_skills() -> list[dict]:
    """Available skill doctrines: [{name, description}] — directories with a SKILL.md."""
    out = []
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return out
    for name in names:
        if name.startswith((".", "_")):
            continue
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        out.append({"name": name, "description": _skill_description(path)})
    return out


def load_skill(name: str) -> str | None:
    """SKILL.md content for one skill; None if unknown. Name validated against the
    real directory listing — no path components accepted."""
    if name not in {s["name"] for s in list_skills()}:
        return None
    try:
        with open(os.path.join(SKILLS_DIR, name, "SKILL.md"),
                  encoding="utf-8", errors="replace") as fh:
            return fh.read()[:MAX_SKILL_CHARS]
    except OSError:
        return None


def _document_block(doc: dict, budget: int) -> str:
    """One stored document as a prompt block — abstract+claims prioritized over
    description inside the per-doc budget."""
    head = f"[{doc.get('number', '?')} — {doc.get('title') or 'no title fetched'}]"
    body_parts = []
    for label, key in (("Abstract", "abstract"), ("Claims", "claims"),
                       ("Description", "description")):
        text = (doc.get(key) or "").strip()
        if not text:
            continue
        room = budget - sum(len(p) for p in body_parts) - len(head)
        if room <= 100:
            break
        body_parts.append(f"{label}: {text[:room]}")
    if not body_parts:
        body_parts.append(f"(text not fetched — status: {doc.get('status', '?')}"
                          + (f", error: {doc['error']}" if doc.get("error") else "") + ")")
    return head + "\n" + "\n".join(body_parts)


def _benchmark_block(bm: dict) -> str:
    """The benchmark document as a prompt block — number-based (fetched fields) or
    upload-based (transcribed/extracted text), within MAX_BENCHMARK_CHARS."""
    head = "[BENCHMARK"
    if bm.get("number"):
        head += f" — {bm['number']}"
    if bm.get("title"):
        head += f" — {bm['title']}"
    head += "]"
    body = []
    if bm.get("text"):
        body.append(bm["text"][:MAX_BENCHMARK_CHARS])
    else:
        budget = MAX_BENCHMARK_CHARS
        for label, key in (("Abstract", "abstract"), ("Claims", "claims"),
                           ("Description", "description")):
            t = (bm.get(key) or "").strip()
            if t and budget > 100:
                chunk = f"{label}: {t[:budget]}"
                body.append(chunk)
                budget -= len(chunk)
    if not body:
        body.append(f"(content not ready — status: {bm.get('status', '?')}"
                    + (f", error: {bm['error']}" if bm.get("error") else "") + ")")
    return head + "\n" + "\n".join(body)


def build_prompt(question: str, history: list[dict] | None = None,
                 documents: list[dict] | None = None,
                 sources: list[dict] | None = None,
                 skills: list[dict] | None = None,
                 benchmark: dict | None = None) -> str:
    parts = [_PREAMBLE]
    if skills:
        blocks = "\n\n".join(f"[Skill /{s['name']}]\n{s['content']}" for s in skills)
        parts.append(
            "USER-SELECTED SKILL DOCTRINES — apply their rules, methods and "
            "anti-patterns; if two skills conflict, flag it explicitly:\n" + blocks)
        parts.append(_LESSON_INSTRUCTION)
    if benchmark:
        parts.append(
            "BENCHMARK DOCUMENT — the reference document of this tab. Candidates "
            "are compared AGAINST this; when ranking or matching, anchor on its "
            "claims and technical solution:\n\n" + _benchmark_block(benchmark))
    if documents:
        blocks, used = [], 0
        skipped = 0
        for d in documents:
            if used >= MAX_DOCS_CHARS:
                skipped += 1
                continue
            b = _document_block(d, min(MAX_DOC_CHARS, MAX_DOCS_CHARS - used))
            blocks.append(b)
            used += len(b)
        note = (f"\n\n(NOTE: {skipped} more document(s) did not fit the context "
                "budget — say so if they may matter.)" if skipped else "")
        parts.append("CANDIDATE DOCUMENTS of this tab (fetched and stored locally — "
                     "this is your primary evidence; cite publication numbers):\n\n"
                     + "\n\n".join(blocks) + note)
    if history:
        lines = []
        for h in history[-MAX_HISTORY:]:
            role = _ROLE.get(h.get("role", ""), "User")
            lines.append(f"{role}: {(h.get('text') or '')[:MAX_TURN_CHARS]}")
        parts.append("CONVERSATION HISTORY of this tab (user questions, NotebookLM "
                     "answers, your previous answers):\n" + "\n\n".join(lines))
    if sources:
        blocks = "\n\n".join(
            f"[Notebook «{s.get('title', '?')}»]\n{(s.get('answer') or '')[:MAX_TURN_CHARS]}"
            for s in sources)
        parts.append(
            "NOTEBOOKLM ANSWERS to the CURRENT question (each notebook only sees "
            "its own corpus):\n" + blocks
            + "\n\nCompile these with the stored documents into one full picture; "
            "merge what agrees, flag contradictions and gaps, attribute key claims "
            "to their notebook. Do not invent anything beyond the provided material.")
    parts.append("USER QUESTION:\n" + question)
    return "\n\n---\n\n".join(parts)


def _run_claude(prompt: str, model: str, extra_args: list[str] | None = None,
                cwd: str | None = None, timeout: float | None = None) -> dict:
    ok, why = available()
    if not ok:
        return {"error": why}
    try:
        proc = subprocess.run([CLAUDE_BIN, "-p", "--model", model, *(extra_args or [])],
                              input=prompt, capture_output=True, text=True,
                              timeout=timeout or CHAT_TIMEOUT, cwd=cwd)
    except subprocess.TimeoutExpired:
        return {"error": "claude chat timed out"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "claude failed"}
    answer = proc.stdout.strip()
    if not answer:
        return {"error": "empty answer from claude"}
    return {"answer": answer, "model": model}


def chat(question: str, history: list[dict] | None = None,
         documents: list[dict] | None = None, sources: list[dict] | None = None,
         skills: list[dict] | None = None, model: str | None = None,
         benchmark: dict | None = None) -> dict:
    """One stateless turn. Returns {answer, model, lessons:[(skill, text)]} | {error}.
    No tools — pure text; benchmark + documents arrive pre-fetched from the local DB."""
    prompt = build_prompt(question, history, documents, sources, skills,
                          benchmark=benchmark)
    res = _run_claude(prompt, model or CHAT_MODEL)
    if "error" in res:
        return res
    lessons = LESSON_RE.findall(res["answer"])
    if lessons:
        res["answer"] = LESSON_RE.sub("", res["answer"]).strip()
        res["lessons"] = [{"skill": s, "lesson": t.strip()} for s, t in lessons]
    return res


def run_extract(prompt: str, allow_read: bool = False, model: str | None = None) -> dict:
    """One-shot extraction run (optionally with the Read tool so the model can
    open an image/file). Returns {answer, model} | {error}."""
    extra = ["--allowedTools", "Read"] if allow_read else None
    return _run_claude(prompt, model or EXTRACT_MODEL, extra_args=extra)
