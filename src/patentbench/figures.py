"""Patent drawing sheets → groundable text.

Figures are images, so a text-only read (chat / 🏆 deep-compare / NotebookLM
export) can't see them. We download each drawing sheet and have a VISION model
transcribe it into a `[FIG. N] …` caption that lists the reference numerals, then
merge those captions into the document's description as a DRAWINGS block — sitting
right beside the `[00NN]` paragraph markers. That is what lets every reader answer
questions about figures with concrete figure numbers and block/reference numerals,
exactly the way it already does for paragraphs.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import claude_bridge

FIG_WORKERS = int(os.environ.get("PB_FIGURE_WORKERS", "3"))
MAX_FIGURES = int(os.environ.get("PB_MAX_FIGURES", "40"))   # cap captioned sheets per doc
DL_TIMEOUT = float(os.environ.get("PB_FIGURE_DL_TIMEOUT", "30"))
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "Chrome/120.0 Safari/537.36")}

# A clearly-delimited, regeneratable block appended to the scraped description, so
# re-captioning never corrupts or duplicates the primary text. The marker string
# lives in claude_bridge (prompt building splits on it; importing figures from
# there would be circular) — re-exported here for the merge/strip helpers below.
DRAWINGS_HEADER = claude_bridge.DRAWINGS_HEADER
_BLOCK_RE = re.compile(r"\n*" + re.escape(DRAWINGS_HEADER) + r".*\Z", re.S)

CAPTION_PROMPT = (
    "This image is a patent DRAWING SHEET. Examine it carefully.\n"
    "1. Identify the figure number(s) PRINTED on the sheet (e.g. 'FIG. 3', 'FIG. 3A'). "
    "If none is printed, use 'FIG. ?'.\n"
    "2. Describe what the figure depicts — the apparatus / circuit / flowchart / graph "
    "and how its parts relate — technically and concisely.\n"
    "3. List EVERY reference numeral or block label visible and the component it marks.\n"
    "Output ONLY this, nothing else (one block per figure on the sheet):\n"
    "[FIG. N] <one-paragraph description>. Reference numerals: 10 = <part>, 12 = <part>, …"
)


def doc_context(doc: dict) -> str:
    """A short grounding header telling the vision captioner WHAT document the
    sheets belong to. Blind captioning guessed device identity from outlines alone
    (an aerosol-device PSU case read as 'clamshell phone', its chassis as a
    'firearm', 2026-07-03) — and those guessed names then poisoned downstream
    grounding ('DRAWINGS block is clearly mismatched'). '' when nothing is known."""
    head = " — ".join(b for b in (doc.get("number"), doc.get("title")) if b)
    abstract = (doc.get("abstract") or "").strip()
    return (head + ("\n" + abstract[:600] if abstract else "")).strip()


def download(urls: list[str], dest_dir: str) -> list[dict]:
    """Download drawing-sheet images into dest_dir. Returns [{n, url, path}] for the
    ones that fetched OK, numbered in document (= figure) order. Capped at MAX_FIGURES."""
    os.makedirs(dest_dir, exist_ok=True)
    out: list[dict] = []
    with httpx.Client(timeout=DL_TIMEOUT, headers=HEADERS, follow_redirects=True) as cl:
        for i, url in enumerate(urls[:MAX_FIGURES], 1):
            ext = os.path.splitext(url.split("?")[0])[1].lower() or ".png"
            path = os.path.join(dest_dir, f"fig{i:03d}{ext}")
            try:
                r = cl.get(url)
                r.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(r.content)
            except (httpx.HTTPError, OSError):
                continue
            out.append({"n": i, "url": url, "path": path})
    return out


def caption_one(path: str, model: str | None = None, context: str = "") -> str:
    """Vision-transcribe a single drawing sheet to a [FIG. N] caption. '' on failure.
    `context` (number/title/abstract) anchors WHAT the depicted apparatus is — never
    let the model name the device from its outline alone."""
    prompt = f"First read the image at {path} with the Read tool, then:\n\n"
    if context:
        prompt += ("DOCUMENT CONTEXT — this sheet belongs to the following patent. "
                   "Identify the depicted apparatus as what it is IN THIS DOCUMENT; "
                   "do NOT guess a generic consumer device from the outline:\n"
                   f"{context}\n\n")
    prompt += CAPTION_PROMPT
    res = claude_bridge.run_extract(prompt, allow_read=True,
                                    model=model or claude_bridge.TRANSCRIBE_MODEL)
    return "" if "error" in res else (res.get("answer") or "").strip()


def caption_all(figs: list[dict], model: str | None = None,
                workers: int | None = None, context: str = "") -> list[dict]:
    """Caption every downloaded sheet CONCURRENTLY (each is a slow vision `claude -p`).
    Sheets whose vision run came back empty get ONE retry pass (seen live: 24 of 40
    failed silently under concurrency). Mutates+returns figs, adding a `caption` to
    each (preserving order)."""
    if not figs:
        return figs
    w = max(1, min(workers or FIG_WORKERS, len(figs)))
    with ThreadPoolExecutor(max_workers=w) as ex:
        caps = list(ex.map(lambda f: caption_one(f["path"], model, context), figs))
    for f, cap in zip(figs, caps):
        f["caption"] = cap
    misses = [f for f in figs if not (f.get("caption") or "").strip()]
    if misses:
        with ThreadPoolExecutor(max_workers=max(1, w // 2)) as ex:
            caps = list(ex.map(lambda f: caption_one(f["path"], model, context), misses))
        for f, cap in zip(misses, caps):
            f["caption"] = cap
    return figs


def drawings_block(figs: list[dict]) -> str:
    """Render captioned figures into the DRAWINGS text block (the model's own
    [FIG. N] lines), or '' if nothing was captioned. Sheets that failed the vision
    read are flagged LOUDLY — a silent gap reads as 'no such figure', and the
    missing figure may be exactly the one the user asks about."""
    caps = [f.get("caption", "").strip() for f in figs if f.get("caption", "").strip()]
    if not caps:
        return ""
    block = DRAWINGS_HEADER + "\n" + "\n\n".join(caps)
    missing = [str(f.get("n", "?")) for f in figs if not (f.get("caption") or "").strip()]
    if missing:
        block += (f"\n\n(⚠ {len(missing)} of {len(figs)} sheets NOT captioned — vision "
                  f"read failed for sheet(s) {', '.join(missing)}; figures on those "
                  "sheets are UNKNOWN, not absent. Re-run 🖼 Read figures to fill them.)")
    return block


def strip_block(description: str | None) -> str:
    """Remove any previously-appended DRAWINGS block, leaving the scraped text alone."""
    return _BLOCK_RE.sub("", description or "").rstrip()


def merge_into_description(description: str | None, figs: list[dict]) -> str:
    """Scraped description with a FRESH DRAWINGS block appended (replacing any old
    one). Idempotent: re-captioning swaps the block instead of stacking copies."""
    base = strip_block(description)
    block = drawings_block(figs)
    if not block:
        return base
    return (base + "\n\n" + block) if base else block
