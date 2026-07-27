"""Extract patent publication numbers from user inputs: text, image, PDF.

Image path: Claude haiku reads the picture directly with its Read tool (the
antimg `extract_construction_from_image` pattern) using the OCR prompt proven in
patent-wiki-analyzer — NO NotebookLM quota is spent. PDF path: pdftotext first,
Claude-over-text fallback only when the regex finds nothing.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from . import claude_bridge, patents

# Page cap for vision-OCR'ing a scanned PDF for candidate NUMBERS. Number-list
# scans are a few pages; a full scanned patent has its number on page 1 anyway.
OCR_PDF_MAX_PAGES = int(os.environ.get("PB_PDF_OCR_MAX_PAGES", "30"))

# Proven prompt from patent-wiki-analyzer ocr-patents/route.ts.
OCR_PROMPT = (
    "List every patent publication number visible in the source image. "
    "Patent numbers follow formats like US10395648B1, EP1300964B1, EP3667902A1, WO2020123456A1, "
    "CN114853847B, JP6489547B2, AU2020374889A1, US20110102159A1. "
    "Output ONLY the numbers — one per line, no commentary, no bullets, no explanations. "
    "Preserve the order in which they appear. If the same number appears multiple times, list it once."
)


TRANSCRIBE_PROMPT = (
    "Transcribe ALL text visible in the image, preserving its structure "
    "(headings, paragraph numbering like [0007], claim numbering, tables as plain "
    "text). Output ONLY the transcription — no commentary, no summaries. If a "
    "region is unreadable, mark it as [unreadable]."
)


def text_from_image(path: str, model: str | None = None) -> dict:
    """Full-text transcription of one page photo: {text} | {error}."""
    prompt = (f"First read the image at {path} with the Read tool. "
              f"Then do the following based on its content.\n\n{TRANSCRIBE_PROMPT}")
    res = claude_bridge.run_extract(prompt, allow_read=True,
                                    model=model or claude_bridge.TRANSCRIBE_MODEL)
    if "error" in res:
        return res
    return {"text": res["answer"]}


def text_from_pdf(path: str) -> dict:
    """Plain text of a PDF via pdftotext: {text} | {error}."""
    try:
        proc = subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return {"error": "pdftotext not installed in this image"}
    except subprocess.TimeoutExpired:
        return {"error": "pdftotext timed out"}
    text = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not text:
        return {"error": "no extractable text in the PDF (scanned image-only PDF?) — "
                         "upload the pages as pictures instead"}
    return {"text": text}


def text_from_scanned_pdf(path: str, model: str | None = None, workers: int = 4,
                          progress=None) -> dict:
    """Vision fallback for an image-only PDF: render the pages (pdftoppm) and
    transcribe each with the vision model — the same engine as photo pages and
    the ⚖️ PSA scanned fallback. `progress(done, total)` is called per page.
    {text} | {error}."""
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(["pdftoppm", "-r", "150", "-png", path,
                            os.path.join(td, "pg")],
                           check=True, timeout=300, capture_output=True)
        except (subprocess.SubprocessError, OSError) as e:
            return {"error": f"scanned PDF, and pdftoppm failed to render it: {e}"}
        pages = sorted(os.listdir(td))
        if not pages:
            return {"error": "scanned PDF rendered to no page images"}
        texts: list[str] = [""] * len(pages)
        page_errors: list[str] = []
        done = 0

        def one(ip):
            i, p = ip
            r = text_from_image(os.path.join(td, p), model=model)
            return i, (r.get("text") or ""), r.get("error")

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for i, t, err in ex.map(one, enumerate(pages)):
                texts[i] = t
                if err:
                    page_errors.append(err)
                done += 1
                if progress:
                    progress(done, len(pages))
        text = "\n\n".join(f"— page {i + 1} —\n{t.strip()}"
                           for i, t in enumerate(texts) if t.strip()).strip()
    if len(text) < 50:
        # Surface the REAL failure — swallowing per-page errors once masked a revoked
        # OAuth token (401) as "yielded almost no text" (2026-07-27, amended_478.pdf).
        if page_errors:
            return {"error": f"scanned PDF: {len(page_errors)}/{len(pages)} page "
                             f"transcription(s) failed — {page_errors[0][:200]}"}
        return {"error": "scanned PDF: vision transcription yielded almost no text"}
    return {"text": text}


def numbers_from_text(text: str) -> dict:
    return {"numbers": patents.extract_candidates(text)}


def numbers_from_image(path: str, model: str | None = None) -> dict:
    """{numbers: [...], uncertain: [...]} | {error}. The headless CLI reads the
    image with its Read tool — the only tool allowed — so the model actually sees
    the pixels. TWO independent passes are run and numbers that differ between
    them are flagged `uncertain` — digit misreads on noisy photos (screen moiré)
    are inconsistent run-to-run, so disagreement is the cheapest error signal.
    A wrong digit can silently fetch a real-but-WRONG patent, hence the paranoia
    (it also keeps cheap reading models safe to use by default)."""
    prompt = (f"First read the image at {path} with the Read tool. Read carefully, "
              "digit by digit — photos of screens have moiré noise.\n"
              f"Then do the following based on its content.\n\n{OCR_PROMPT}")

    def one_pass(_: int) -> dict:
        return claude_bridge.run_extract(prompt, allow_read=True,
                                         model=model or claude_bridge.OCR_MODEL)

    # The two independent passes are run CONCURRENTLY — each is a slow `claude -p`
    # subprocess, so running them in parallel roughly halves the wait the user sees.
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(one_pass, range(2)))
    for res in results:
        if "error" in res:
            return res
    passes = [patents.extract_candidates(res["answer"]) for res in results]
    first, second = set(passes[0]), set(passes[1])
    ordered = list(dict.fromkeys(passes[0] + passes[1]))
    return {"numbers": ordered, "uncertain": sorted(first ^ second)}


def _numbers_from_scanned_pdf(path: str, name: str | None = None) -> dict:
    """Image-only PDF (no text layer) → candidate numbers. Cheapest source first:
    the FILENAME — Espacenet/Google downloads are named by publication number
    ("ITMI20090714A1.pdf"), which is deterministic and costs ZERO model tokens.
    Only when the name yields nothing: render pages (pdftoppm, same move as the
    ⚖️ PSA scanned fallback) and run the 2-pass photo OCR per page."""
    from_name = patents.extract_candidates(name or "")
    if from_name:
        return {"numbers": from_name, "uncertain": [], "source": "filename"}
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(["pdftoppm", "-r", "150", "-png", path,
                            os.path.join(td, "pg")],
                           check=True, timeout=180, capture_output=True)
        except (subprocess.SubprocessError, OSError) as e:
            return {"error": f"scanned PDF, and pdftoppm failed to render it: {e}"}
        pages = sorted(os.listdir(td))
        if not pages:
            return {"error": "scanned PDF rendered to no page images"}
        skipped = max(0, len(pages) - OCR_PDF_MAX_PAGES)
        pages = pages[:OCR_PDF_MAX_PAGES]
        # numbers_from_image already runs 2 concurrent passes per page — keep the
        # page fan-out at 2 so one scanned PDF holds at most 4 claude sessions.
        with ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(
                lambda p: numbers_from_image(os.path.join(td, p)), pages))
    numbers: list[str] = []
    seen: set[str] = set()
    uncertain: set[str] = set()
    errors = [r["error"] for r in results if "error" in r]
    for r in results:
        for n in r.get("numbers", []):
            if n not in seen:
                seen.add(n)
                numbers.append(n)
        uncertain.update(r.get("uncertain", []))
    if not numbers and errors:
        return {"error": f"scanned PDF: page OCR failed — {errors[0]}"}
    out = {"numbers": numbers, "uncertain": sorted(uncertain), "source": "page-ocr"}
    if skipped:
        out["note"] = (f"OCR capped at {OCR_PDF_MAX_PAGES} pages — "
                       f"{skipped} page(s) not read")
    return out


def numbers_from_pdf(path: str, name: str | None = None) -> dict:
    """{numbers: [...]} | {error}. pdftotext + regex; Claude haiku over the text
    only if the regex comes up empty. A PDF with NO text layer (scan) falls back
    to filename/page-image extraction instead of erroring out."""
    try:
        proc = subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return {"error": "pdftotext not installed in this image"}
    except subprocess.TimeoutExpired:
        return {"error": "pdftotext timed out"}
    text = proc.stdout if proc.returncode == 0 else ""
    nums = patents.extract_candidates(text)
    if nums:
        return {"numbers": nums}
    if not text.strip():
        return _numbers_from_scanned_pdf(path, name=name)
    # regex-empty TEXT pdf: the filename is still cheaper than a model call
    from_name = patents.extract_candidates(name or "")
    if from_name:
        return {"numbers": from_name, "uncertain": [], "source": "filename"}
    res = claude_bridge.run_extract(
        OCR_PROMPT.replace("the source image", "the text below") + "\n\nTEXT:\n" + text[:60_000])
    if "error" in res:
        return res
    return {"numbers": patents.extract_candidates(res["answer"])}
