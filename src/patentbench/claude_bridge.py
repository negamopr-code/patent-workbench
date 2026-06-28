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

from . import citations

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Latest first — Fable 5 is the default; the UI offers the rest as a dropdown.
MODELS = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
# Default chat/compile model: sonnet is the cost/quality optimum for routine
# Q&A and deep-compare reduces — pick fable-5 from the dropdown for the final,
# decisive ranking only (it weighs hardest on the subscription session limit).
CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-sonnet-4-6")
EXTRACT_MODEL = os.environ.get("CLAUDE_EXTRACT_MODEL", "claude-haiku-4-5")
# ALL document reading (page transcription, photo number-OCR, digests, deep-map
# full-text reads) defaults to the MOST AFFORDABLE model — the user picks a
# stronger one per task via the UI's separate "reading model" dropdown when
# quality demands it. Misread-digit risk on cheap OCR is mitigated by the
# two-pass disagreement check, which flags inconsistent numbers as uncertain.
READ_MODEL = os.environ.get("PB_READ_MODEL", "claude-haiku-4-5")
# Pulling patent NUMBERS off a photo is adversarial: one misread digit silently
# routes to a real-but-WRONG patent, and the two-pass disagreement check is no
# safety net when the model can't read the image at all — it just flags EVERY
# number uncertain (seen live 2026-06-16: haiku returned 30 garbage numbers, all
# ⚠). So number-OCR is decoupled from the cheap reading default and uses a strong
# vision model. Bulk full-text transcription stays on READ_MODEL — there context
# self-corrects and volume makes cost matter. Models too weak for digit OCR:
TRANSCRIBE_MODEL = os.environ.get("PB_TRANSCRIBE_MODEL", READ_MODEL)
OCR_MODEL = os.environ.get("PB_OCR_MODEL", "claude-fable-5")
WEAK_OCR_MODELS = {"claude-haiku-4-5"}
CHAT_TIMEOUT = float(os.environ.get("CLAUDE_CHAT_TIMEOUT", "240"))
SKILLS_DIR = os.environ.get("CLAUDE_SKILLS_DIR", os.path.expanduser("~/.claude/skills"))

# Model that reads each candidate's FULL text: at fetch time it writes the
# stored digest, and in deep-compare it judges one candidate vs the benchmark.
DIGEST_MODEL = os.environ.get("PB_DIGEST_MODEL", "claude-haiku-4-5")
DIGEST_TIMEOUT = float(os.environ.get("PB_DIGEST_TIMEOUT", "300"))
# The reduce phase compiles EVERY candidate's verdict in one call — far heavier
# than a normal chat turn — so it gets its own, much longer timeout. Its prompt
# is also kept bounded (per-verdict slice shrinks as the roster grows).
REDUCE_TIMEOUT = float(os.environ.get("PB_REDUCE_TIMEOUT", "900"))
REDUCE_PROMPT_BUDGET = int(os.environ.get("PB_REDUCE_PROMPT_BUDGET", "600000"))

MAX_HISTORY = 24            # turns kept in the prompt
MAX_TURN_CHARS = 4000       # each history turn clipped
MAX_SKILL_CHARS = 16_000    # each skill doctrine clipped
MAX_DOC_CHARS = 9000        # per-candidate ceiling (abstract+digest+claims first)
MAX_DOCS_CHARS = 400_000    # total candidate budget per prompt
MAX_ROSTER_CHARS = 240_000  # total budget for non-focused candidates' DIGESTS (focus path)
MIN_DOC_CHARS = 1200        # floor below which a candidate block stops being useful
MAX_FULLTEXT_CHARS = 400_000  # full document fed to the digest/deep-map model
MAX_FOCUS_CHARS = 300_000     # total budget for user-SELECTED candidates loaded in full

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

_GROUNDING_INSTRUCTION = (
    "GROUNDING — non-negotiable, read before answering:\n"
    "• Each document block is labelled by what text it actually contains: "
    "PRIMARY (verbatim Claims and/or Description — quotable and citable) and/or "
    "DIGEST (a summary written by an indexing model). A DIGEST is NEVER primary "
    "text: do not quote it as claim or specification language and never cite a "
    "paragraph number taken from it. This rule exists because past answers "
    "quoted 'claim 2 / claim 7(c)' from a digest as if verified — that must not "
    "happen again.\n"
    "• Begin EVERY answer with ONE short line starting 'GROUNDING:' naming the "
    "documents you relied on and at what depth — e.g. 'GROUNDING: full "
    "claims+description for US10395648B1; digest-only for US9455593B2.' One line, "
    "no editorializing or 'notes on locators' after it.\n"
    "• CITE WITH ITS EXACT QUOTE: every [00NN]/claim locator you give MUST be "
    "accompanied by a SHORT exact supporting quote — the actual wording of that "
    "paragraph/claim (translated to English if needed), in quotation marks, kept "
    "brief (≤~20 words, the decisive phrase, not the whole paragraph). A bare "
    "locator or a loose paraphrase is not enough: the reader must see the words "
    "that back it. Format like: [0008]: \"the DCS sets up a two-way link with the "
    "storage system and transmits the generator power signal to it\". Use a "
    "paragraph marker [00NN] ONLY if it actually appears in the provided text, and "
    "a claim number only for a claim you were given. NEVER fabricate a locator or "
    "a quote — if the primary text is not in front of you, you do not have it.\n"
    "• REFUSE-TO-GUESS: if a correct answer would need primary claim/description "
    "text you were NOT given (a candidate present only as digest/abstract, or "
    "whose text was clipped away above), do NOT guess. State exactly what is "
    "missing and tell the user to run 🏆 Deep compare, or to narrow the tab to "
    "that specific candidate, for a verified answer. An ungrounded answer is "
    "worse than no answer.\n"
    "• LANGUAGE — ENGLISH ONLY, ZERO non-Latin characters: output NO "
    "Chinese/Japanese/Korean characters ANYWHERE — not in quotes, not as a "
    "parenthetical term gloss like '(测控装置)', not anywhere. The user cannot "
    "read them. Refer to every component by an English name (a romanized "
    "form or acronym is fine, e.g. 'measurement & control device (MCD)'). Quotes "
    "are REQUIRED with each cite (see above) but must be in ENGLISH — translate "
    "the original wording faithfully; never paste the original-language text.\n"
    "• ARGUE THE DISCLOSURE, don't just cite: a citation alone is not an "
    "argument. For each feature you assert is (or is not) disclosed, give the "
    "evidence→conclusion chain — the [00NN]/claim locator WITH its short exact "
    "quote, and then a clear explanation of WHY that wording reads on the specific "
    "claim element / benchmark feature (note when it is only implicit, or reached "
    "indirectly via another component). When arguing something is NOT disclosed, "
    "point to where it would appear and show it isn't.\n"
    "• KISS — short and readable beats exhaustive. Answer the question in plain "
    "English with the [00NN]/claim citation in brackets after each point, and "
    "STOP. Hard bans (these make answers unreadable): (a) reference/drawing "
    "numerals in ANY form — no 'element 12', '(12)', 'RTU (4)', 'storage system "
    "(12)', 'element 12 of figure 2'; name the part in plain words; (b) appendix "
    "sections like 'Process flow:', 'Coupled elements:', 'EMS-slot occupant:', or "
    "ASCII arrow diagrams; (c) a feature-by-feature TABLE or row-by-row mapping "
    "of the whole document unless the user explicitly asks for one; (d) jargon "
    "('difference-node', 'function-not-label read'). "
    "GOOD (the whole answer — each cite carries a short exact quote): 'Yes. In "
    "850 the storage system itself does the EMS's job — the DCS feeds it generator "
    "power [claim 1: \"the DCS...transmits the generator power signal to the "
    "storage system\"] and it sets charge/discharge as AGC-command minus that "
    "power [claim 5: \"the storage load command is the AGC instruction minus the "
    "generator output power; positive discharges, negative charges\"], the same "
    "two-inputs-minus operation as the benchmark EMS [0070: \"generates the "
    "control instruction based on the frequency instruction and the working "
    "parameter\"].' BAD: bare locators with no quote, OR the point exploded into a "
    "numbered element walkthrough with a process-flow diagram. Brief, but every "
    "cite is backed by its words."
)


# ---------------------------------------------------------------------------
# ANSWER-FORMAT PRESETS — selectable answer shapes for the chat. The "" preset
# keeps the default behaviour (concise, or 📝 Full when the toggle is on); any
# other preset's `instruction` REPLACES the style line and dictates structure
# (it still rides on top of GROUNDING — every locator carries its exact quote).
# To add a new option later: append one {key, label, instruction} entry here —
# the API and the chat dropdown pick it up automatically.
# ---------------------------------------------------------------------------
_FMT_CLAIM_MAP = (
    "ANSWER FORMAT — ONE-LINE CLAIM MAP (terse). Output ONE SENTENCE PER CLAIM "
    "GROUP and NOTHING else: no 'GROUNDING:' line, no bullets, no quotes, no "
    "intro, no 'Net:'/summary paragraph, no explanations. Group the claims "
    "under analysis (by default the BENCHMARK's claims; if the user names a "
    "different document's claims, use those) so claims sharing ONE technical "
    "focus sit together (an independent claim with its dependents, or a "
    "consecutive range). Each line MUST follow this template EXACTLY, in this "
    "order and wording:\n"
    "  Claims <range> are directed to <focus in 2-5 words>; <REFERENCE-NUMBER> "
    "discloses <the function in a few words> <expressly|implicitly> as per "
    "<bare locators>.\n"
    "BARE LOCATORS ONLY — paragraph markers like [0018], [0028]; claim numbers "
    "like 'claim 2'; figures like 'Fig.1' — comma-separated. DO NOT add a "
    "supporting quote, DO NOT paraphrase the wording, DO NOT justify WHY. Just "
    "the locators. (This format OVERRIDES the GROUNDING rule that normally asks "
    "for an exact quote after each locator — here locators stand alone.) Use "
    "'expressly' when the reference names it literally, 'implicitly' when only "
    "inferable. If more than one reference is relevant to a group, add a second "
    "clause in the SAME sentence: '..., while <REF2> discloses it expressly as "
    "per <locators>.'\n"
    "LITERAL TEMPLATE TO MATCH: 'Claims 2-3 are directed to grid measurement; "
    "EP3087655 discloses the metering function expressly as per [0018], [0028], "
    "claim 71, Fig.1.'\n"
    "Real locators only — never invent a paragraph/claim/figure absent from the "
    "provided text. If a group's focus is NOT disclosed, say so in the same "
    "one-line style, e.g. 'Claims 4-5 are directed to X; EP3087655 does not "
    "disclose this (no such teaching in the text).' NO tables, NO reference "
    "numerals like 'element 12', NO original-language text."
)

_FMT_CLAIM_MAP_PRAGMATIC = (
    "ANSWER FORMAT — PRAGMATIC CLAIM MAP (examiner-style, terse). Like the "
    "one-line claim map, but for EACH claim group you decide disclosure "
    "feature-by-feature and, for any feature the reference does NOT disclose, "
    "give the pragmatic obviousness verdict in the SAME breath instead of just "
    "flagging the gap. Output one short block PER CLAIM GROUP and NOTHING else: "
    "no 'GROUNDING:' line, no bullets, no intro, no 'Net:'/summary paragraph. "
    "Group the claims under analysis (by default the BENCHMARK's claims; if the "
    "user names a different document's claims, use those) so claims sharing a "
    "technical focus sit together (an independent claim with its dependents, or "
    "a consecutive range).\n"
    "Each block follows this shape EXACTLY, in this order and wording:\n"
    "  Claims <range> are directed to <the features in a few words>. "
    "<REFERENCE-NUMBER> discloses <the disclosed feature(s)> as per <bare "
    "locators>. <For each NOT-disclosed feature:> <feature> is not disclosed in "
    "<REFERENCE-NUMBER>; however, it would constitute a standard design practice "
    "to use one without appeal to inventive step.\n"
    "LITERAL TEMPLATE TO MATCH: 'Claims 2-3 are directed to metering function "
    "and usage of a transformer. EP3087655 discloses the metering function as "
    "per [0018], [0028], claim 71, Fig.1. The transformer is not disclosed in "
    "EP3087655; however, it would constitute a standard design practice to use "
    "one without appeal to inventive step.'\n"
    "BARE LOCATORS ONLY for what IS disclosed — paragraph markers like [0018], "
    "[0028]; claim numbers like 'claim 2'; figures like 'Fig.1' — comma-"
    "separated; no quote, no paraphrase, no WHY (this OVERRIDES the GROUNDING "
    "quote rule — locators stand alone). Real locators only — never invent a "
    "paragraph/claim/figure absent from the provided text.\n"
    "OBVIOUSNESS WORDING: only call a missing feature obvious when it genuinely "
    "is a routine/notorious measure (a standard component, a well-known "
    "expedient, a trivial workshop choice). Default phrasing: '<feature> is not "
    "disclosed in <REF>; however, it would constitute a standard design practice "
    "to use one without appeal to inventive step.' Vary 'standard design "
    "practice' with 'a notorious/well-known measure' or 'a routine workshop "
    "modification' where it reads more naturally. If a missing feature is NOT "
    "plainly routine — it could carry an inventive contribution — do NOT pretend "
    "it is obvious; instead write: '<feature> is not disclosed in <REF> and is "
    "not a mere routine measure (may support inventive step).' Be honest, not "
    "automatically dismissive. NO tables, NO reference numerals like 'element "
    "12', NO original-language text."
)

_FMT_FEATURE_MAP = (
    "ANSWER FORMAT — INTERLINEAR FEATURE MAP. The user pasted a CLAIM (preamble "
    "+ a list of elements). Reproduce the claim text faithfully and IN ORDER, "
    "and after EACH element insert — right where it occurs — how the reference "
    "document under analysis discloses it. Map every meaningful phrase (each "
    "branch, port, connection and 'wherein' clause); do NOT summarise, reorder "
    "or drop wording. The pasted claim is the backbone; the parentheticals are "
    "the only thing you add.\n"
    "TYPOGRAPHY (markdown, REQUIRED): put the CLAIM's own text in **bold** and "
    "every disclosure parenthetical in *italic*, so the reader instantly tells "
    "claim-language from your mapping. Shape:\n"
    "  **A first branch … electrically connected to a first port** *(storage→"
    "inverter 84 path via switch 59 — [0021], Fig.1)*\n"
    "BE CONCISE — this is the main thing the user wants: each parenthetical is "
    "ONE short, fact-based clause that conveys the LOGIC of the match — name the "
    "part, give its reference numeral(s) and locators ([00NN], Fig.N), and a few "
    "words or a brief \"quote\" ONLY if they carry the argument. No "
    "multi-sentence parentheticals, no restating the claim inside them, no "
    "padding. Reference numerals (e.g. '59', 'inverter 84') are REQUIRED and "
    "this format OVERRIDES the GROUNDING ban on them (they tie each element to "
    "the drawings).\n"
    "WHICH DOCUMENT: map against the FOCUSED candidate (the one whose full text "
    "is loaded); if the user names a specific document, use that. Open with one "
    "short line, e.g. 'Mapping against EP3087655:'. State a partial match in a "
    "few words inside the parenthetical, e.g. *(partial: grid feed enters on a "
    "separate path, not as one of these branches)*; write *(not disclosed)* when "
    "a feature is absent. Real locators/numerals only — never invent one. NO "
    "tables, NO original-language text, NO separate 'GROUNDING:' line, NO "
    "trailing summary."
)

_FMT_ONE_SENTENCE = (
    "ANSWER FORMAT — ONE SENTENCE. Answer in EXACTLY ONE sentence and nothing "
    "else: no 'GROUNDING:' line, no bullets, no preamble, no follow-up line. "
    "Lead with the direct answer; if a locator carries it, fold ONE short "
    "citation ([00NN]/claim N) into the sentence — no quote needed. If the "
    "answer genuinely cannot fit one sentence honestly, give the single most "
    "important sentence and stop. Never invent a locator."
)

# Ordered; first entry ("") is the default. `instruction=None` → fall back to
# the concise/Full style line. Exposed (key+label only) via /api/skills.
ANSWER_FORMATS = [
    {"key": "", "label": "Default answer", "instruction": None},
    {"key": "one-sentence", "label": "Concise — 1 sentence answer",
     "instruction": _FMT_ONE_SENTENCE},
    {"key": "claim-map", "label": "Claims grouped → 1-line disclosure",
     "instruction": _FMT_CLAIM_MAP},
    {"key": "claim-map-pragmatic",
     "label": "Claims grouped → disclosure + obviousness verdict",
     "instruction": _FMT_CLAIM_MAP_PRAGMATIC},
    {"key": "feature-map", "label": "Paste a claim → interlinear feature map",
     "instruction": _FMT_FEATURE_MAP},
]
_FORMAT_BY_KEY = {f["key"]: f for f in ANSWER_FORMATS}


def _style_instruction(full: bool) -> str:
    if full:
        return ("ANSWER STYLE: FULL analysis — still KISS. Cover the few decisive "
                "points only, each as a SHORT bullet: the point in plain English + "
                "its [00NN]/claim citation WITH a short exact English quote. Aim "
                "for ≤6 bullets. NO tables, NO process-flow/coupled-elements "
                "appendices, NO reference numerals ('element 12', 'RTU (4)'), NO "
                "original-language quotes. Depth = covering the key features, NOT "
                "an exhaustive element-by-element walkthrough. Obey GROUNDING.")
    return ("ANSWER STYLE: KISS — the body is at most 3–4 short sentences in plain "
            "English. Lead with the answer; after each point put its [paragraph]/"
            "claim citation WITH a short exact English quote (≤~20 words) and a "
            "brief WHY. NO tables, NO appendix sections, NO reference numerals, NO "
            "jargon, NO original-language quotes. If the full story needs more, end "
            "with one line: 'Ask for detail on X if useful.' The 'GROUNDING:' line "
            "doesn't count toward the limit.")


_ROLE = {"q": "User", "a": "Notebook", "c": "Claude", "s": "System"}

LESSON_RE = re.compile(r"^LESSON\[([\w][\w-]*)\]:\s*(.+)$", re.MULTILINE)

# Safety net: the user wants ZERO CJK in answers. Even with the prompt rule, a
# model may slip a term gloss like "(测控装置)" — strip it deterministically.
_CJK = "　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯"
_PAREN_CJK_RE = re.compile(r"\s*[(（][^()（）]*[" + _CJK + r"][^()（）]*[)）]")
_CJK_RUN_RE = re.compile("[" + _CJK + "]+")


def _strip_cjk(text: str) -> str:
    """Remove CJK characters from an answer: first drop whole parenthetical glosses
    that contain CJK, then any remaining CJK runs, then tidy emptied quotes/parens."""
    if not text:
        return text
    t = _PAREN_CJK_RE.sub("", text)
    t = _CJK_RUN_RE.sub("", t)
    t = re.sub(r'(["“”\'])\s*\1', "", t)       # emptied quote pairs
    t = re.sub(r"\(\s*\)", "", t)               # emptied parens
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r" +([.,;:)])", r"\1", t).strip()


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


def _document_block(doc: dict, budget: int, clipped: bool = True) -> str:
    """One stored document as a prompt block. Fields are LABELLED by provenance so
    the model can obey the GROUNDING rules: abstract/claims/description are PRIMARY
    (quotable, citable); the digest is a DERIVED summary (never quotable). When
    `clipped`, the per-doc budget truncates the lower-priority fields — the model
    must treat a clipped candidate as NOT full-text."""
    head = f"[{doc.get('number', '?')} — {doc.get('title') or 'no title fetched'}]"
    body_parts = []
    # primary text first when we have the full budget (focus block); digest first
    # only matters in the tight clipped path where description rarely fits anyway.
    fields = (("Abstract (PRIMARY)", "abstract"),
              ("Claims (PRIMARY — quotable verbatim, cite as 'claim N')", "claims"),
              ("Description (PRIMARY — quotable verbatim; cite [00NN] markers)", "description"),
              ("DIGEST (DERIVED summary — NOT primary text, do NOT quote or cite paragraphs from it)", "digest"))
    if clipped:  # keep the old ordering when space is tight: abstract, digest, then primary
        fields = (fields[0], fields[3], fields[1], fields[2])
    for label, key in fields:
        text = (doc.get(key) or "").strip()
        if not text:
            continue
        room = budget - sum(len(p) for p in body_parts) - len(head)
        if room <= 100:
            break
        # Always flag a real truncation — even in the focus path. The focus header
        # promises "FULL … unclipped" text; if a field actually overflows the budget
        # the model must NOT treat it as complete (status ≠ substance).
        clip_note = " …[CLIPPED — truncated here, NOT full text]" if len(text) > room else ""
        body_parts.append(f"{label}: {text[:room]}{clip_note}")
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


def _focus_block(doc: dict) -> str:
    """A user-selected candidate rendered with the FULL primary-text budget (no
    abstract/digest-first clipping) — this is the uncl­ipped text the chat needs to
    quote real paragraphs/claims."""
    per = max(MIN_DOC_CHARS, min(MAX_FULLTEXT_CHARS, MAX_FOCUS_CHARS))
    return _document_block(doc, per, clipped=False)


def build_prompt(question: str, history: list[dict] | None = None,
                 documents: list[dict] | None = None,
                 sources: list[dict] | None = None,
                 skills: list[dict] | None = None,
                 benchmark: dict | None = None,
                 focus: list[dict] | None = None,
                 full: bool = False, answer_format: str = "") -> str:
    parts = [_PREAMBLE]
    if documents or focus or benchmark:
        parts.append(_GROUNDING_INSTRUCTION)
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
    if focus:
        # the FULL primary text of the candidate(s) the user selected — divide the
        # focus budget across them so each is as complete as possible.
        per = max(MIN_DOC_CHARS, min(MAX_FULLTEXT_CHARS, MAX_FOCUS_CHARS // len(focus)))
        fblocks = "\n\n".join(_document_block(d, per, clipped=False) for d in focus)
        parts.append(
            f"FOCUSED CANDIDATE(S) — the user selected these {len(focus)} document(s); "
            "their FULL primary text (claims + description with [00NN] paragraph "
            "markers) is loaded below, uncl­ipped. THIS is your verified primary "
            "source: quote and cite from here. Ground your answer in these:\n\n"
            + fblocks)
    if documents and focus:
        # The user focused on specific candidate(s): the answer is grounded in the
        # FULL focused text above. The rest collapse to a roster — but one that
        # CARRIES each candidate's already-computed DIGEST + score note (paid for at
        # full-read time), not just number+title. That keeps the prompt far smaller
        # than 400k chars of clipped primary bodies while letting the model REASON
        # about every candidate it already read, instead of going blind and telling
        # the user to "load full text" for something already digested. The digest is
        # DERIVED (not quotable); a candidate must still be SELECTED only when a
        # VERBATIM claim/[00NN] quote is needed.
        # Prefer the stored deep-compare VERDICT (a full-text assessment vs THIS
        # benchmark, with verified [00NN] evidence) over the generic digest — both
        # are already paid for; reuse whichever is richer.
        def _summary(d: dict) -> tuple[str, str]:
            v = (d.get("verdict") or "").strip()
            if v:
                return ("ASSESSMENT vs benchmark (full-text read; its [00NN]/claim "
                        "markers were verified — you MAY cite them)", v)
            dig = (d.get("digest") or "").strip()
            if dig:
                return ("DIGEST (DERIVED summary — do NOT quote/cite paragraphs from "
                        "it; SELECT this candidate to load full text for a verbatim quote)", dig)
            return ("", "")
        with_text = [d for d in documents if _summary(d)[1]]
        per = (min(MAX_DOC_CHARS, max(MIN_DOC_CHARS, MAX_ROSTER_CHARS // len(with_text)))
               if with_text else 0)
        lines = []
        for d in documents:
            head = f"• {d.get('number','?')} — {d.get('title') or 'no title'}"
            if d.get("score") is not None:
                note = (d.get("score_note") or "").strip()
                head += f"  [{d['score']:g}/10{(' — ' + note) if note else ''}]"
            label, body = _summary(d)
            if body:
                clip = " …[clipped]" if len(body) > per else ""
                head += f"\n  {label}: {body[:per]}{clip}"
            else:
                head += "  (not yet read; SELECT or Deep-compare to read it)"
            lines.append(head)
        parts.append(
            f"OTHER CANDIDATES in this tab ({len(documents)}) — each shown with its "
            "stored full-text ASSESSMENT (or digest) and match score, already computed "
            "at read time. Use these to REASON about and compare every candidate; you "
            "do NOT need the user to reload them. A candidate carrying an ASSESSMENT "
            "was read in full — rely on it; a digest-only candidate must be SELECTED "
            "for a verbatim claim/[00NN] quote:\n" + "\n".join(lines))
    elif documents:
        # EVERY candidate is always present: the per-candidate slice shrinks as
        # the list grows instead of dropping the tail (which is arbitrary —
        # insertion order says nothing about relevance). Only past the hard
        # floor (~330 candidates) does skipping start, loudly.
        per_doc = min(MAX_DOC_CHARS, max(MIN_DOC_CHARS, MAX_DOCS_CHARS // len(documents)))
        cap = max(1, MAX_DOCS_CHARS // MIN_DOC_CHARS)
        included, skipped = documents[:cap], len(documents[cap:])
        blocks = [_document_block(d, per_doc, clipped=True) for d in included]
        note = (f"\n\n(NOTE: {skipped} more document(s) did not fit even at the "
                "minimum slice — tell the user to use Deep compare for full "
                "coverage.)" if skipped else "")
        parts.append("CANDIDATE DOCUMENTS of this tab — ALL "
                     f"{len(included)} of them (fetched and stored locally; "
                     f"each CLIPPED to ~{per_doc} chars — this is NOT their full "
                     "text, the description is mostly truncated. To quote a "
                     "candidate's primary text or cite its paragraphs, the user "
                     "must SELECT it (loads it into the FOCUSED block above) or "
                     "run 🏆 Deep compare. Do not present clipped/digest content "
                     "as verified primary text); cite publication numbers:\n\n"
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
    fmt = _FORMAT_BY_KEY.get(answer_format or "")
    if fmt and fmt.get("instruction"):
        parts.append(fmt["instruction"])
    else:
        parts.append(_style_instruction(full))
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
         benchmark: dict | None = None, focus: list[dict] | None = None,
         full: bool = False, answer_format: str = "") -> dict:
    """One stateless turn. Returns {answer, model, lessons:[(skill, text)]} | {error}.
    No tools — pure text; benchmark + documents arrive pre-fetched from the local DB.
    `focus` = user-selected candidates loaded with FULL primary text; `full` =
    long-form answer (otherwise 1-2 sentence precise mode); `answer_format` = an
    ANSWER_FORMATS key that, when set, dictates the answer's structure."""
    prompt = build_prompt(question, history, documents, sources, skills,
                          benchmark=benchmark, focus=focus, full=full,
                          answer_format=answer_format)
    res = _run_claude(prompt, model or CHAT_MODEL)
    if "error" in res:
        return res
    res["answer"] = _strip_cjk(res["answer"])
    # NOTE: [00NN] locator correction happens at the API layer (api._verify_citations),
    # not here — the model often cites candidates that are NOT in `focus` (pulling a
    # number from the running conversation), and only the API has DB access to load
    # ANY mentioned candidate's full text to check the quote against.
    lessons = LESSON_RE.findall(res["answer"])
    if lessons:
        res["answer"] = LESSON_RE.sub("", res["answer"]).strip()
        res["lessons"] = [{"skill": s, "lesson": t.strip()} for s, t in lessons]
    return res


_DIGEST_PROMPT = (
    "You are indexing a patent document for later comparison work. Read the FULL "
    "text below and produce a dense, factual digest (600-900 words) with sections:\n"
    "1. Technical field & problem solved\n"
    "2. Core solution / mechanism (how it works)\n"
    "3. Key claim features (independent claims, characterizing parts)\n"
    "4. Embodiment details — every concrete component, system, protocol or term "
    "named anywhere in the description (e.g. DCS, AGC, PLC, specific sensors, "
    "control loops), even in passing; these matter for prior-art matching\n"
    "5. Anything unusual or distinguishing\n"
    "Be specific, cite paragraph/claim numbers where visible. No fluff, no "
    "introduction — start directly with section 1.\n\nFULL TEXT:\n"
)


def digest_document(number: str, title: str, fulltext: str,
                    model: str | None = None) -> dict:
    """One reading-model pass over the ENTIRE document at fetch time → stored
    digest. {digest} | {error}."""
    prompt = f"Document {number} — {title}\n\n" + _DIGEST_PROMPT + fulltext[:MAX_FULLTEXT_CHARS]
    res = _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    return {"digest": res["answer"]}


_DEEP_MAP_PROMPT = (
    "You compare ONE candidate patent against a BENCHMARK document, both given in "
    "FULL below. You are reading the candidate's COMPLETE primary text (claims + "
    "description, with [00NN] paragraph markers).\n"
    "SCORE BY FEATURE PRESENCE, NOT BY CLAIMED NOVELTY. The question is whether the "
    "candidate's full text DISCLOSES the benchmark's technical features ANYWHERE — in "
    "the claims, the detailed description, an embodiment, a figure description, or even "
    "the background/prior-art section — NOT whether the candidate's own *claimed "
    "invention* is the same as the benchmark's. A candidate that claims something else "
    "entirely but still describes the benchmark's feature combination (e.g. as shared "
    "background hardware common to a patent family) is a STRONG match and must score "
    "high. Do not down-rank a candidate merely because the feature is incidental to, or "
    "outside, what it claims as novel. Judge disclosure by substance, not by where it "
    "appears or how it is labelled.\n"
    "Output exactly:\n"
    "MATCH SCORE: <0-10>\n"
    "KEY FEATURES: the decisive SHARED features, 3-6 items of 1-4 words each, "
    "joined with ' + ' (e.g. 'AGC frequency signal + ESS hierarchy + droop "
    "control'); if nothing meaningful is shared, write 'none'\n"
    "OVERLAP: bullet list of features the candidate shares with the benchmark — "
    "for EACH, give the candidate's real locator (its [00NN] paragraph or "
    "'claim N') AND a concise ENGLISH statement of what that passage says; "
    "include description-level disclosure (embodiments, named components), not "
    "just claims\n"
    "CANDIDATE EVIDENCE: 2-5 decisive disclosures from THIS candidate, each as its "
    "exact [00NN]/claim locator + ONE English sentence of what it discloses — this "
    "is the evidence the final answer relies on. Write it in ENGLISH; if the "
    "patent is non-English, translate — do NOT output original-language text. Get "
    "the [00NN]/claim markers EXACTLY right (they are verified against the text "
    "you were given); never invent one.\n"
    "DIFFERENCES: bullet list of what the benchmark has that this candidate lacks "
    "(and vice versa where relevant)\n"
    "VERDICT: 2-3 sentences — how close is this candidate to the benchmark's "
    "technical solution and why\n"
    "Everything in ENGLISH. Ground every statement in the provided texts and tie "
    "it to a real [00NN]/claim locator; never invent a marker or a disclosure."
)


def _feature_check_block(features: list[dict]) -> str:
    """Instruction + numbered target list so the map model rates EACH weighted
    feature individually (the per-feature verdicts drive the weighted ranking)."""
    lines = [
        "FEATURE CHECK: for EACH numbered TARGET FEATURE below, output one line",
        "  FEATURE <n>: <YES|PARTIAL|NO> — <the candidate's [00NN]/claim locator and a",
        "  short English note, or 'not disclosed'>",
        "YES = the candidate clearly discloses it (literally or by clear equivalent);",
        "PARTIAL = related but incomplete/ambiguous; NO = absent. Judge by substance,",
        "not wording. TARGET FEATURES:",
    ]
    for i, f in enumerate(features, 1):
        lines.append(f"{i}. {f.get('name', '')}")
    return "\n".join(lines)


def deep_map(benchmark_text: str, doc: dict, model: str | None = None,
             features: list[dict] | None = None) -> dict:
    """Map phase of deep-compare: the reading model reads the candidate's FULL
    text vs the benchmark. When `features` is given (weighted feature-combination
    benchmark), the model also emits a per-feature YES/PARTIAL/NO check used for
    the weighted ranking. {verdict} | {error}."""
    fulltext = "\n\n".join(filter(None, [
        doc.get("abstract"), doc.get("claims"), doc.get("description")]))
    instructions = _DEEP_MAP_PROMPT
    if features:
        instructions = instructions + "\n" + _feature_check_block(features)
    prompt = (instructions
              + "\n\n===== BENCHMARK =====\n" + benchmark_text[:200_000]
              + f"\n\n===== CANDIDATE {doc.get('number')} — {doc.get('title') or ''} =====\n"
              + fulltext[:MAX_FULLTEXT_CHARS])
    res = _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    # Locators originate here and are trusted verbatim by deep_reduce — correct them
    # against the candidate's full text at the source. The whole text is present, so
    # a quote found nowhere is genuinely suspect → flag it.
    verdict = citations.verify(res["answer"],
                               [{"number": doc.get("number"), "text": fulltext}],
                               flag_unfound=True)["answer"]
    return {"verdict": verdict}


_SCORE_RE = re.compile(r"MATCH SCORE:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_FEATURES_RE = re.compile(r"KEY FEATURES:\s*(.+)", re.IGNORECASE)
_FEATURE_CHECK_RE = re.compile(
    r"FEATURE\s*(\d+)\s*:\s*(YES|PARTIAL|NO)\b[^\n]*", re.IGNORECASE)


def parse_feature_check(text: str, features: list[dict]) -> list[dict]:
    """Map a deep-map verdict's FEATURE CHECK lines back onto the benchmark's
    weighted features, by 1-based index. Returns [{name, weight, status, note}]
    aligned to `features`; any feature the model didn't rate defaults to 'no'."""
    found: dict[int, tuple[str, str]] = {}
    for m in _FEATURE_CHECK_RE.finditer(text or ""):
        idx = int(m.group(1))
        status = m.group(2).lower()
        line = m.group(0)
        note = line.split("—", 1)[1].strip()[:200] if "—" in line else ""
        found[idx] = (status, note)
    out = []
    for i, f in enumerate(features, 1):
        status, note = found.get(i, ("no", ""))
        out.append({"name": f.get("name", ""), "weight": int(f.get("weight", 1)),
                    "status": status, "note": note})
    return out


def parse_verdict(text: str) -> dict:
    """Pull the structured bits out of a deep-map verdict for the candidates
    column: {score: float|None, features: str|None}."""
    m = _SCORE_RE.search(text or "")
    score = min(10.0, max(0.0, float(m.group(1)))) if m else None
    f = _FEATURES_RE.search(text or "")
    features = f.group(1).strip()[:200] if f else None
    if features and features.lower() in ("none", "none.", "-"):
        features = None
    return {"score": score, "features": features}


def deep_reduce(question: str, benchmark: dict, verdicts: list[dict],
                skills: list[dict] | None = None, model: str | None = None,
                history: list[dict] | None = None) -> dict:
    """Reduce phase: the chat model compiles per-candidate FULL-TEXT verdicts into
    a final ranking/answer. Same return contract as chat()."""
    # Per-verdict slice: 8000 chars normally, but shrink it for a large roster so
    # the whole reduce prompt stays under REDUCE_PROMPT_BUDGET (prevents both the
    # timeout and blowing the model's context). Floor at 1500 so cards stay useful.
    per_cap = 8000
    if verdicts:
        per_cap = max(1500, min(8000, REDUCE_PROMPT_BUDGET // len(verdicts)))
    blocks = "\n\n".join(
        f"[{v['number']} — {v.get('title') or ''}]\n{v['verdict'][:per_cap]}"
        for v in verdicts)
    parts = [_PREAMBLE, _GROUNDING_INSTRUCTION]
    if skills:
        sk = "\n\n".join(f"[Skill /{s['name']}]\n{s['content']}" for s in skills)
        parts.append("USER-SELECTED SKILL DOCTRINES:\n" + sk)
        parts.append(_LESSON_INSTRUCTION)
    parts.append("BENCHMARK DOCUMENT:\n\n" + _benchmark_block(benchmark))
    parts.append(
        "FULL-TEXT VERDICTS — every candidate was read IN FULL (claims AND "
        "description, with [00NN] markers) by an analyst model. Each card carries "
        "OVERLAP locators and a CANDIDATE EVIDENCE block: [00NN]/claim markers "
        "each with an English statement of what it discloses — verified against "
        "the candidate's primary text, so you MAY rely on and cite them directly "
        "(e.g. 'CN…[0015]: the device forwards the load command'). Hard limit: "
        "only reproduce a [00NN]/claim marker that appears in a card below — never "
        "invent or extrapolate one; cite in ENGLISH, never paste original-language "
        "text. If a card lacks the specific paragraph the question needs, say so "
        "and tell the user to SELECT that candidate in a normal chat turn (loads "
        "its full text):\n\n" + blocks)
    if history:
        lines = [f"{_ROLE.get(h.get('role', ''), 'User')}: "
                 f"{(h.get('text') or '')[:MAX_TURN_CHARS]}" for h in history[-MAX_HISTORY:]]
        parts.append("CONVERSATION HISTORY:\n" + "\n\n".join(lines))
    parts.append(_style_instruction(True))
    parts.append("TASK:\n" + question)
    res = _run_claude("\n\n---\n\n".join(parts), model or CHAT_MODEL,
                      timeout=REDUCE_TIMEOUT)
    if "error" in res:
        return res
    res["answer"] = _strip_cjk(res["answer"])
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


RECONCILE_PROMPT = (
    "Two AI engines independently rated how closely each CANDIDATE patent matches "
    "the BENCHMARK invention (0–10): 🤖 Claude read each candidate's FULL text vs "
    "the benchmark; 📓 NotebookLM rated it grounded on its stored source. They "
    "disagree on the candidates below. Using ONLY the benchmark and the short "
    "rating notes given (do NOT ask for more text), explain CONCISELY where each "
    "gap comes from — typically: NotebookLM rewards shared FIELD/application or "
    "retrieval-surface wording, while Claude weighs the actual CLAIM-LEVEL "
    "mechanism; or one engine saw a feature the other missed.\n\n"
    "Output one line per candidate, then a final 1–2 sentence PATTERN:\n"
    "<number>: <why they differ> → more credible: <Claude|NLM|middle> (~<n>/10)\n"
    "PATTERN: <the systematic reason NLM and Claude diverge here>"
)


DEBATE_PROMPT = (
    "Reconcile two assessments of how well each FINALIST patent matches the benchmark, "
    "FUNCTIONAL BLOCK by functional block. NotebookLM read the FULL documents on its side "
    "(its reply is given). You are given each finalist's full-text DIGEST (and your earlier "
    "per-block verdicts where available). For EACH finalist and EACH target block output: "
    "YOUR verdict (YES/PARTIAL/NO from the digest), NLM's verdict (from its reply), AGREE? "
    "(yes/no), and a one-line reconciled conclusion (who is better supported and why). Treat "
    "an implicit realisation — doing the step without the literal word — as covered. Then give "
    "a short CONSENSUS ranking of the finalists both views support, and a DISPUTED list: each "
    "block where you still disagree + the specific evidence that would settle it. Be concise."
)


def debate(blocks_text: str, finalists_text: str, nlm_answer: str,
           model: str | None = None) -> dict:
    """ONE cheap call that has Claude argue per functional block against NotebookLM's
    grounded reply — Claude reasons from the finalists' DIGESTS (not full re-read), so it
    stays cheap. Returns {answer, model} | {error}."""
    prompt = (DEBATE_PROMPT
              + "\n\n=== TARGET FEATURE BLOCKS ===\n" + (blocks_text or "")[:4000]
              + "\n\n=== FINALIST DIGESTS (Claude side) ===\n" + (finalists_text or "")[:12000]
              + "\n\n=== NOTEBOOKLM'S GROUNDED REPLY ===\n" + (nlm_answer or "")[:6000])
    return _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)


def reconcile(benchmark_summary: str, items: list[dict], model: str | None = None) -> dict:
    """Explain — in ONE cheap call over the already-stored short notes (no full
    texts) — why Claude and NotebookLM disagree. items=[{number,title,score,
    score_note,nlm_score,nlm_score_note}]. Returns {answer, model} | {error}."""
    lines = []
    for it in items:
        lines.append(
            f"- {it['number']} \"{(it.get('title') or '')[:80]}\": "
            f"🤖 Claude {it.get('score')}/10 [{it.get('score_note') or '—'}] vs "
            f"📓 NLM {it.get('nlm_score')}/10 [{it.get('nlm_score_note') or '—'}]")
    prompt = (RECONCILE_PROMPT + "\n\nBENCHMARK:\n" + (benchmark_summary or "")[:4000]
              + "\n\nDISAGREEMENTS:\n" + "\n".join(lines))
    return _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
