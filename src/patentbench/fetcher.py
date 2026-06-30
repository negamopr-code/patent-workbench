"""Fetch patent documents (title/abstract/claims/description) from Google Patents.

Python port of patent-wiki-analyzer's src/lib/fetchers/google.ts (HTML scrape
with the same selectors) plus its pdftotext fallback for the description when
the HTML lacks one. Throttled like nlm_bridge: process-wide lock + minimum gap.
Per scraper-pro doctrine the curl_cffi-impersonate fallback is NOT wired until a
403 actually shows up — the error message says what to do if it does.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time

import httpx
from bs4 import BeautifulSoup

MIN_GAP = float(os.environ.get("PB_FETCH_GAP", "1.2"))
TIMEOUT = float(os.environ.get("PB_FETCH_TIMEOUT", "30"))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "Chrome/120.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_FIELD = 200_000   # hard cap per stored field

_lock = threading.Lock()
_last = 0.0


def _gap_wait() -> None:
    global _last
    g = MIN_GAP - (time.monotonic() - _last)
    if g > 0:
        time.sleep(g)
    _last = time.monotonic()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _with_para_num(el, text: str) -> str:
    """Prefix a description paragraph with its Google-Patents paragraph number as
    a groundable marker, e.g. '[0035] ...'. The number lives in the element's
    `num` attribute — pure digits on US/EP `<description-paragraph>` (num="0035"),
    but PREFIXED on the CN-style `<p>` fallback (num="n0001"). Strip the prefix so
    both yield a marker. Use ONLY `num` (the real paragraph number); never the
    `id`, which is DOM position incl. headings and would mis-number paragraphs.
    Headings carry no `num`, so they get no marker — correct. This is what lets
    the chat cite real paragraphs instead of inventing them."""
    digits = re.sub(r"\D", "", (el.get("num") or "").strip())
    if not digits:
        return text
    marker = f"[{int(digits):04d}]"
    return text if text.startswith(marker) else f"{marker} {text}"


def _pdf_url(soup: BeautifulSoup, raw_html: str) -> str | None:
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and (meta.get("content") or "").startswith("http"):
        return meta["content"].strip()
    a = soup.select_one('a[href*="patentimages.storage.googleapis.com"]')
    if a and re.search(r"\.pdf($|\?)", a.get("href", ""), re.IGNORECASE):
        href = a["href"].strip()
        return f"https:{href}" if href.startswith("//") else href
    m = re.search(r"https://patentimages\.storage\.googleapis\.com/[A-Za-z0-9./_\-]+\.pdf",
                  raw_html)
    return m.group(0) if m else None


_IMG_EXT = r"(?:png|tif|tiff|jpg|jpeg|gif)"
_IMG_URL_RE = re.compile(
    r"https?:[^\s\"'<>]*patentimages\.storage\.googleapis\.com/[A-Za-z0-9./_\-]+\." + _IMG_EXT,
    re.IGNORECASE)
# A patentimages filename that denotes a DRAWING SHEET. EP/WO figures are imgfNNNN
# (imgbNNNN = inline body math — EXCLUDED); US sheets are …-DNNNNN; CN/JP often a
# bare numeric. _US_REPR (…-D00000) is the representative thumbnail — dropped when
# real sheets exist.
_FIG_NAME_RE = re.compile(
    r"(?:imgf\d+|[-_]D\d{3,}|^\d{8,})\." + _IMG_EXT + r"$", re.IGNORECASE)
_US_REPR_RE = re.compile(r"[-_]D0+\." + _IMG_EXT + r"$", re.IGNORECASE)


def _figure_urls(soup: BeautifulSoup, raw_html: str) -> list[str]:
    """Ordered, de-duplicated URLs of the drawing sheets. Google Patents serves
    figures on patentimages with office-specific names (EP `imgfNNNN`, US
    `…-DNNNNN`, CN a bare numeric). Each can appear under two storage paths, so we
    dedupe by filename and keep document order (= figure order). The US `D00000`
    representative thumbnail is dropped when real sheets exist (it duplicates FIG.1).
    EP inline-math images (`imgbNNNN`) are not matched."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(src: str) -> None:
        src = (src or "").strip()
        if not src or "patentimages.storage.googleapis.com" not in src:
            return
        name = src.split("?")[0].rsplit("/", 1)[-1]
        if not _FIG_NAME_RE.search(name):
            return
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            return
        if name.lower() in seen:
            return
        seen.add(name.lower())
        urls.append(src)

    for img in soup.select('img[src*="patentimages"]'):
        add(img.get("src") or "")
    for m in _IMG_URL_RE.finditer(raw_html):
        add(m.group(0))
    # drop the US representative thumbnail (…-D00000) unless it's the only image
    sheets = [u for u in urls if not _US_REPR_RE.search(u.rsplit("/", 1)[-1])]
    return sheets or urls


def figure_urls(number: str) -> list[str]:
    """Scrape ONLY the drawing-sheet URLs for a publication (for backfilling figures
    onto a document fetched before figures were captured). Tries the same grant-
    preferred variants as fetch_document. [] if none / unreachable."""
    for variant in _publication_variants(number):
        url = f"https://patents.google.com/patent/{variant}/en"
        with _lock:
            _gap_wait()
            try:
                with httpx.Client(timeout=TIMEOUT, headers=HEADERS,
                                  follow_redirects=True) as cl:
                    resp = cl.get(url)
            except httpx.HTTPError:
                return []
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            figs = _figure_urls(soup, resp.text)
            if figs:
                return figs
        elif resp.status_code != 404:
            break
    return []


def _description_from_pdf(url: str) -> str:
    """Download the publication PDF and pdftotext it — fallback when HTML has no
    description (common for some jurisdictions)."""
    try:
        with httpx.Client(timeout=60, headers=HEADERS, follow_redirects=True) as cl:
            r = cl.get(url)
            r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(r.content)
            tmp = fh.name
        try:
            proc = subprocess.run(["pdftotext", "-layout", tmp, "-"],
                                  capture_output=True, text=True, timeout=120)
            return proc.stdout if proc.returncode == 0 else ""
        finally:
            os.unlink(tmp)
    except Exception:
        return ""


# Offices whose GRANT shares the application's number, differing only by kind code
# (EP A1→B1, GB A→B, CN A→B, …). For these a kind-less number must resolve to the
# GRANT — otherwise Google Patents serves the earlier A-publication, whose CLAIMS
# and [00NN] paragraph numbering differ from the granted B-document (the EP3087655
# bug: app stored the application claims + a paragraph numbering offset vs the B1).
# US/JP grants get a DIFFERENT number and WO/PCT has no grant, so we never
# kind-substitute those — a B-variant would 404 and waste a Google-Patents request.
_GRANT_SAME_NUMBER = re.compile(r"^(EP|GB|DE|CN|KR|FR|AT|CH|ES|IT|NL|SE|BE|DK|FI|PT)\d")
_HAS_KIND_CODE = re.compile(r"\d[A-Z]\d?$")


def _publication_variants(number: str) -> list[str]:
    """Publications to try, in order: prefer the grant (B1) for a kind-less number
    from a same-number-grant office, then fall back to the bare number (the
    application). A number that already carries a kind code is used verbatim — the
    caller asked for a specific publication. Note: B2/C grants (post-opposition,
    rare) aren't auto-tried; pass the explicit kind code for those."""
    if _HAS_KIND_CODE.search(number) or not _GRANT_SAME_NUMBER.match(number):
        return [number]
    return [f"{number}B1", number]


def fetch_document(number: str) -> dict:
    """Scrape one patent, preferring the granted publication. Returns
    {title, abstract, claims, description} | {error}."""
    last_err = {"error": "no publication yielded content"}
    for variant in _publication_variants(number):
        res = _fetch_publication(variant)
        if "error" not in res:
            return res
        last_err = res
        if res.get("status") != 404:        # 403/timeout/parse — stop, don't hammer Google
            break
    return {k: v for k, v in last_err.items() if k != "status"}


def _fetch_publication(number: str) -> dict:
    """Scrape ONE exact publication. {title, abstract, claims, description} |
    {error, status}. `status` lets fetch_document fall through to the next variant
    only on a 404 (publication doesn't exist)."""
    url = f"https://patents.google.com/patent/{number}/en"
    with _lock:
        _gap_wait()
        try:
            with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as cl:
                resp = cl.get(url)
        except httpx.HTTPError as exc:
            return {"error": f"fetch failed: {exc.__class__.__name__}: {exc}", "status": None}
    if resp.status_code == 404:
        return {"error": "not found on Google Patents (404) — check the number/kind code",
                "status": 404}
    if resp.status_code == 403:
        return {"error": "Google Patents 403 (TLS fingerprint block?) — needs the "
                         "curl_cffi impersonate fallback (see scraper-pro)", "status": 403}
    if resp.status_code != 200:
        return {"error": f"Google Patents HTTP {resp.status_code}", "status": resp.status_code}

    raw = resp.text
    soup = BeautifulSoup(raw, "lxml")

    meta = soup.find("meta", attrs={"name": "DC.title"})
    title = _clean(meta.get("content", "") if meta else "")

    abstract = ""
    for sel in ('section[itemprop="abstract"] .abstract', "abstract .abstract",
                '[itemprop="abstract"]'):
        el = soup.select_one(sel)
        if el:
            abstract = _clean(el.get_text())
            if abstract:
                break

    claims = []
    for el in soup.select("claim, .claim"):
        t = _clean(el.get_text())
        if t:
            claims.append(t)
    claims_text = "\n\n".join(dict.fromkeys(claims))  # dedupe nested duplicates, keep order

    paras = []
    for el in soup.select("description-paragraph, .description-paragraph, div.description-line"):
        t = _clean(el.get_text())
        if t:
            paras.append(_with_para_num(el, t))
    if not paras:
        for el in soup.select('section[itemprop="description"] p'):
            t = _clean(el.get_text())
            if t:
                paras.append(_with_para_num(el, t))
    description = "\n\n".join(paras)

    if not description:
        pdf = _pdf_url(soup, raw)
        if pdf:
            description = _description_from_pdf(pdf).strip()

    if not (title or abstract or claims_text or description):
        return {"error": "page fetched but no content parsed (layout change?)"}
    return {"title": title[:2000], "abstract": abstract[:MAX_FIELD],
            "claims": claims_text[:MAX_FIELD], "description": description[:MAX_FIELD],
            "figure_urls": _figure_urls(soup, raw)}
