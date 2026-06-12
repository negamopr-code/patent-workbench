"""Extract patent publication numbers from user inputs: text, image, PDF.

Image path: Claude haiku reads the picture directly with its Read tool (the
antimg `extract_construction_from_image` pattern) using the OCR prompt proven in
patent-wiki-analyzer — NO NotebookLM quota is spent. PDF path: pdftotext first,
Claude-over-text fallback only when the regex finds nothing.
"""
from __future__ import annotations

import subprocess

from . import claude_bridge, patents

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
    passes = []
    for _ in range(2):
        res = claude_bridge.run_extract(prompt, allow_read=True,
                                        model=model or claude_bridge.OCR_MODEL)
        if "error" in res:
            return res
        passes.append(patents.extract_candidates(res["answer"]))
    first, second = set(passes[0]), set(passes[1])
    ordered = list(dict.fromkeys(passes[0] + passes[1]))
    return {"numbers": ordered, "uncertain": sorted(first ^ second)}


def numbers_from_pdf(path: str) -> dict:
    """{numbers: [...]} | {error}. pdftotext + regex; Claude haiku over the text
    only if the regex comes up empty (scanned PDFs etc. get a second chance)."""
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
        return {"error": "no extractable text in the PDF (scanned image-only PDF?) — "
                         "try uploading page screenshots as images instead"}
    res = claude_bridge.run_extract(
        OCR_PROMPT.replace("the source image", "the text below") + "\n\nTEXT:\n" + text[:60_000])
    if "error" in res:
        return res
    return {"numbers": patents.extract_candidates(res["answer"])}
