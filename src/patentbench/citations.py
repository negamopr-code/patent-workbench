"""Correct LLM-emitted [00NN] paragraph locators against the source text.

The reading/chat models quote candidate text VERBATIM but sometimes tag the quote
with the wrong [00NN] paragraph number (seen live 2026-06-17: a real EP3087655 B1
quote from [0025] was cited as [0029]). The fix is to never trust the model's
number: find the quoted span in the candidate's actual text and rewrite the
locator to the paragraph the quote really sits in. The QUOTE is the source of
truth; the number is derived. Quotes that can't be found verbatim are optionally
flagged so an invented quote is visible rather than dressed up with a locator.
"""
import re

# A locator (single [00NN] or an [00NN]–[00MM] range) bound TIGHTLY to a quoted
# span — only whitespace / a ':' / a dash may sit between them, so we never couple
# a marker to an unrelated quote later in the sentence.
_CITE = re.compile(
    r'(?P<loc>\[(?P<num>\d{3,4})\](?:\s*[–—-]\s*\[\d{3,4}\])?)'
    r'\s*[:：．.\-–—]?\s*'
    r'(?P<oq>["“«])(?P<q>.+?)(?P<cq>["”»])',
    re.S)

_FLAG = " ⚠"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _paragraphs(text: str) -> list[tuple[str, str]]:
    """[(marker, normalised body)] for each [00NN] paragraph in `text`."""
    parts = re.split(r"\[(\d{3,4})\]", text or "")
    out = []
    for i in range(1, len(parts), 2):
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out.append((parts[i], _norm(body)))
    return out


def _true_marker(quote: str, paras: list[tuple[str, str]]) -> str | None:
    """The marker of the paragraph that contains `quote` (verbatim, whitespace- and
    case-insensitive), or None. Ellipsis-elided quotes match on their longest run."""
    segs = [s for s in re.split(r"\.\.\.|…", quote) if s.strip()]
    candidates = [max(segs, key=len)] if segs else []
    if quote not in candidates:
        candidates.append(quote)
    for seg in candidates:
        q = _norm(seg)
        if len(q) < 8:                       # too short to attribute safely
            continue
        for marker, body in paras:
            if q in body:
                return marker
    return None


def verify(answer: str, sources: list[dict], flag_unfound: bool = False) -> dict:
    """Rewrite [00NN] locators in `answer` to the paragraph their quoted text
    actually occupies, searched across `sources` (= [{number, text}], full primary
    text). A locator is corrected only when the quote is found and the cited
    number(s) don't already cover the true paragraph. With `flag_unfound`, a quote
    found in NO source gets a ⚠ (use only when every cited source's full text is
    present — else legitimate quotes from un-loaded docs would be falsely flagged).
    Returns {answer, corrections:[(old_locator, new, number|None)]}."""
    src = [(s.get("number") or "", _paragraphs(s.get("text") or "")) for s in sources]
    corrections: list[tuple] = []

    def repl(m: re.Match) -> str:
        loc = m.group("loc")
        for number, paras in src:
            tm = _true_marker(m.group("q"), paras)
            if tm:
                nums = [int(x) for x in re.findall(r"\d{3,4}", loc)]
                if not (min(nums) <= int(tm) <= max(nums)):
                    corrections.append((loc, f"[{tm}]", number))
                    return m.group(0).replace(loc, f"[{tm}]", 1)
                return m.group(0)                       # already correct / in range
        if flag_unfound and not m.group(0).endswith(_FLAG):
            corrections.append((loc, "⚠", None))
            return m.group(0) + _FLAG
        return m.group(0)

    return {"answer": _CITE.sub(repl, answer), "corrections": corrections}
