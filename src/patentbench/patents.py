"""Patent publication-number utilities: extraction, canonicalization, links.

Extraction regex + plausibility filter and the two OCR-recovery normalisers are
ports of the proven TypeScript versions in patent-wiki-analyzer
(src/app/api/notebooks/[id]/ocr-patents/route.ts and src/lib/fetchers/router.ts).
"""
from __future__ import annotations

import re
from urllib.parse import unquote

# Separators inside a number are space/slash/dot/dash but NEVER a newline:
# with \s the match absorbs the next line's list index ("…AU 2020/192686\n3." →
# AU20201926863, one digit too long → 404 on Google Patents). Seen live 2026-06-12.
# Case-insensitive so a hand-typed lowercase "cn120200454" is accepted just like
# "CN120200454" — canonicalize() uppercases the match anyway.
NUMBER_RE = re.compile(r"\b([A-Za-z]{2}[ ]?\d[\d /.-]*[A-Za-z]?\d*)\b")

# Patent numbers embedded in URLs (Google Patents path, Espacenet publication path
# or pn= query) — matched case-insensitively because query strings are often
# lowercase (e.g. ?q=pn%3Dcn120638382a).
_URL_NUMBER_RES = [
    re.compile(r"patents\.google\.com/patent/([A-Za-z0-9]+)"),
    re.compile(r"espacenet\.com/[^\s\"']*?/publication/([A-Za-z0-9]+)"),
    re.compile(r"[?&]q=pn(?:%3D|=)([A-Za-z0-9]+)", re.IGNORECASE),
]


def normalize(raw: str) -> str:
    return re.sub(r"[\s/.-]", "", raw).upper()


def is_plausible(n: str) -> bool:
    if len(n) < 6 or len(n) > 20:
        return False
    if not re.match(r"^[A-Z]{2}\d", n):
        return False
    return bool(re.search(r"\d{4,}", n))


# OCR commonly drops the leading 0 of the 7-digit serial in US application
# publication numbers — "US 2023/0278430 A1" gets read as "US2023278430".
# Google Patents requires the canonical US{YYYY}0{6digits} form. Pattern:
# US + EXACTLY 10 digits (optionally a kind code) → inject `0` after the year.
def canonicalize_us_app_number(raw: str) -> str:
    m = re.match(r"^US(\d{10})([A-Z]\d?)?$", raw)
    if not m:
        return raw
    digits, kind = m.group(1), m.group(2) or ""
    return f"US{digits[:4]}0{digits[4:]}{kind}"


# OCR also drops the digit suffix of multi-character kind codes —
# "JP4034816B2" gets read as "JP4034816B"; the partial `...B` form 404s on
# Google Patents. Strip a trailing single letter NOT followed by a digit.
def strip_partial_kind_code(raw: str) -> str:
    m = re.match(r"^([A-Z]{2,3}\d+)([A-Z])$", raw)
    return m.group(1) if m else raw


def canonicalize(raw: str) -> str:
    """Compose all OCR-recovery normalisers (strip partial kind code first)."""
    return canonicalize_us_app_number(strip_partial_kind_code(normalize(raw)))


def extract_candidates(text: str) -> list[str]:
    """Every plausible, canonicalized patent number in the text, ordered, deduped.
    Catches both bare numbers and numbers embedded in Google Patents / Espacenet URLs."""
    text = unquote(text or "")
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        # A space between country code and digits is only credible for long
        # serials ("US 2023/0278430"); "IN 2023"-style prose matches are noise.
        # Case-insensitive to also filter lowercase prose ("in 2023") now that the
        # number regex matches either case.
        if re.match(r"^[A-Za-z]{2}\s", raw) and len(re.sub(r"\D", "", raw)) < 6:
            return
        n = canonicalize(raw)
        if is_plausible(n) and n not in seen:
            seen.add(n)
            out.append(n)

    for rx in _URL_NUMBER_RES:
        for m in rx.finditer(text):
            add(m.group(1))
    for m in NUMBER_RE.finditer(text):
        add(m.group(1))
    return out


def links(number: str) -> dict:
    return {
        "google": f"https://patents.google.com/patent/{number}/en",
        "espacenet": f"https://worldwide.espacenet.com/patent/search?q=pn%3D{number}",
    }
