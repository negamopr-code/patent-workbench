"""Scanned-PDF number intake — filename first (zero tokens), page OCR only last.
The claude bridge and poppler binaries are stubbed; nothing shells out."""
from types import SimpleNamespace

from patentbench import extract


def _pdftotext_empty(cmd, **kw):
    assert cmd[0] == "pdftotext"
    return SimpleNamespace(returncode=0, stdout="")


def test_scanned_pdf_number_comes_from_filename_without_ocr(monkeypatch):
    monkeypatch.setattr(extract.subprocess, "run", _pdftotext_empty)
    monkeypatch.setattr(extract, "numbers_from_image",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("page OCR must not run — filename had the number")))
    res = extract.numbers_from_pdf("/up/8f-ITMI20090714A1.pdf", name="ITMI20090714A1.pdf")
    assert res == {"numbers": ["ITMI20090714A1"], "uncertain": [], "source": "filename"}


def test_text_pdf_with_no_regex_hits_tries_filename_before_model(monkeypatch):
    monkeypatch.setattr(extract.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=0,
                                                          stdout="lorem ipsum, no numbers"))
    monkeypatch.setattr(extract.claude_bridge, "run_extract",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("model call must not run — filename had the number")))
    res = extract.numbers_from_pdf("/up/x.pdf", name="US10395648B1.pdf")
    assert res["numbers"] == ["US10395648B1"] and res["source"] == "filename"


def test_scanned_pdf_without_filename_number_falls_back_to_page_ocr(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        if cmd[0] == "pdftotext":
            return SimpleNamespace(returncode=0, stdout="")
        assert cmd[0] == "pdftoppm"
        prefix = cmd[-1]                     # …/pg inside the tempdir
        for i in (1, 2):
            open(f"{prefix}-{i}.png", "wb").close()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    monkeypatch.setattr(extract, "numbers_from_image",
                        lambda p, model=None: calls.append(p) or
                        {"numbers": ["US10395648B1", "EP3667902A1"],
                         "uncertain": ["EP3667902A1"]})
    res = extract.numbers_from_pdf("/up/scan.pdf", name="scan.pdf")
    assert len(calls) == 2                   # one OCR per rendered page
    assert res["numbers"] == ["US10395648B1", "EP3667902A1"]   # union, deduped
    assert res["uncertain"] == ["EP3667902A1"]
    assert res["source"] == "page-ocr"


def test_scanned_pdf_ocr_failure_is_reported(monkeypatch):
    def fake_run(cmd, **kw):
        if cmd[0] == "pdftotext":
            return SimpleNamespace(returncode=0, stdout="")
        prefix = cmd[-1]
        open(f"{prefix}-1.png", "wb").close()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    monkeypatch.setattr(extract, "numbers_from_image",
                        lambda p, model=None: {"error": "claude quota exhausted"})
    res = extract.numbers_from_pdf("/up/scan.pdf", name="scan.pdf")
    assert "error" in res and "claude quota exhausted" in res["error"]
