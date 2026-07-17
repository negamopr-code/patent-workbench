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

import json
import os
import re
import shutil
import subprocess
import time

from . import citations

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Latest first — Fable 5 is the default; the UI offers the rest as a dropdown.
MODELS = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
# Default chat/compile model: sonnet is the cost/quality optimum for routine
# Q&A and deep-compare reduces — pick fable-5 from the dropdown for the final,
# decisive ranking only (it weighs hardest on the subscription session limit).
CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-sonnet-4-6")
EXTRACT_MODEL = os.environ.get("CLAUDE_EXTRACT_MODEL", "claude-haiku-4-5")
# The UI's "reading model" dropdown default. It assesses each candidate in
# deep-compare (a RANKING decision) and also transcribes pages, so it defaults to
# SONNET, not the cheapest model: haiku was caught inverting the candidate ranking
# (2026-06-27 forensics — see DIGEST_MODEL below), and the doctrine that came out
# of it is "bulk-read on sonnet, final reduce on opus". The user can still pick
# haiku per-run from the dropdown for cheap bulk transcription; photo number-OCR
# is decoupled onto OCR_MODEL regardless. Override with PB_READ_MODEL.
READ_MODEL = os.environ.get("PB_READ_MODEL", "claude-sonnet-4-6")
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
# Defaults to SONNET, not the cheapest model: this read is a RANKING decision, and
# haiku was caught inverting it (2026-06-27 DB forensics) — it scored generic
# keyword-matches 7/10 while the architecturally-correct winners landed 4-6/10,
# a distinction NLM's full read + opus debate caught. Bulk page transcription
# stays cheap on READ_MODEL (volume dominates, context self-corrects); the
# analytical read does not, so quality wins here. Override with PB_DIGEST_MODEL.
DIGEST_MODEL = os.environ.get("PB_DIGEST_MODEL", "claude-sonnet-4-6")
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
MAX_XTALK_CHARS = 80_000    # total budget for cross-tab chat exchanges pulled into the prompt
MAX_METHOD_CHARS = 120_000  # the user's problem-solution methodology document, verbatim
PSA_TIMEOUT = float(os.environ.get("PB_PSA_TIMEOUT", "900"))
MIN_DOC_CHARS = 1200        # floor below which a candidate block stops being useful
MAX_FULLTEXT_CHARS = 400_000  # full document fed to the digest/deep-map model
MAX_FOCUS_CHARS = 600_000     # total budget for user-SELECTED candidates loaded in full.
# Sized so a normal hand-selected set NEVER clips a full patent: an average patent
# is ~80k chars, so 600k keeps ~7 focused docs fully unclipped (was 300k → clipped a
# long doc's tail at ~[0113] once ~6 were selected — the model then honestly reported
# it could not quote late paragraphs; the text was in the DB, the PROMPT dropped it).

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
    "• DRAWINGS: a document block marked 'DRAWINGS NOT READ' has had NO "
    "vision-read of its figure sheets — treat its figures as UNKNOWN. Never "
    "infer figure content from the surrounding text; if figures could matter "
    "to the answer, say so explicitly and tell the user to run 🖼 Read figures "
    "on that document for a detailed check including the drawings. Only a "
    "document whose text carries a merged DRAWINGS block ([FIG. N] captions) "
    "has readable figures.\n"
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


# Figures are opt-in (vision-captioning costs real tokens), so a document may be
# read text-only. That is FINE — as long as the deficiency is loud: the model must
# know the drawings are unknown, and the user must know which button closes the gap.
_DRAWINGS_NOT_READ = (
    "(DRAWINGS NOT READ — this document's figure sheets were never vision-captioned, "
    "so nothing about its drawings appears in the text above. Figure content is "
    "UNKNOWN: do not infer it. If figures could matter, say so and tell the user to "
    "run 🖼 Read figures on this document for a detailed check including drawings.)")


def _figures_unread(doc: dict) -> bool:
    """True when the doc's drawing sheets were never vision-captioned. figures_n is
    None until 🖼/ingest captioning runs (0 = ran, no drawings; >0 = captioned)."""
    return doc.get("figures_n") is None


# Canonical marker of the vision-read captions block that figures.py merges into a
# stored description. Lives HERE (not in figures.py) because figures imports
# claude_bridge — the reverse import would be circular.
DRAWINGS_HEADER = "========== DRAWINGS (figure descriptions, vision-read) =========="


def _split_drawings(text: str) -> tuple[str, str]:
    """Split a stored description into (scraped body, merged DRAWINGS block). The
    captions are merged at the TAIL of the text — exactly what a budget clip eats
    first — so prompt blocks render them as their OWN field ahead of the
    description; a clip then truncates prose, never the figures. Seen live
    2026-07-03: EP4338618's 16 captions started at char 200 002 of a 215 838-char
    description and every focus slice under that lost ALL figures."""
    i = (text or "").find(DRAWINGS_HEADER)
    if i < 0:
        return text, ""
    return text[:i].rstrip(), text[i:].strip()


def _document_block(doc: dict, budget: int, clipped: bool = True) -> str:
    """One stored document as a prompt block. Fields are LABELLED by provenance so
    the model can obey the GROUNDING rules: abstract/claims/description are PRIMARY
    (quotable, citable); the digest is a DERIVED summary (never quotable). When
    `clipped`, the per-doc budget truncates the lower-priority fields — the model
    must treat a clipped candidate as NOT full-text."""
    head = f"[{doc.get('number', '?')} — {doc.get('title') or 'no title fetched'}]"
    body_parts = []
    desc_body, drawings = _split_drawings(doc.get("description") or "")
    values = {"abstract": doc.get("abstract"), "claims": doc.get("claims"),
              "drawings": drawings, "description": desc_body,
              "digest": doc.get("digest")}
    # primary text first when we have the full budget (focus block); digest first
    # only matters in the tight clipped path where description rarely fits anyway.
    # DRAWINGS (a few KB of vision-read captions) come BEFORE the description so a
    # budget clip truncates prose, never the figures.
    fields = (("Abstract (PRIMARY)", "abstract"),
              ("Claims (PRIMARY — quotable verbatim, cite as 'claim N')", "claims"),
              ("DRAWINGS (PRIMARY — vision-read figure captions; cite as 'Fig. N')", "drawings"),
              ("Description (PRIMARY — quotable verbatim; cite [00NN] markers)", "description"),
              ("DIGEST (DERIVED summary — NOT primary text, do NOT quote or cite paragraphs from it)", "digest"))
    if clipped:  # keep the old ordering when space is tight: abstract, digest, then primary
        fields = (fields[0], fields[4], fields[1], fields[2], fields[3])
    for label, key in fields:
        text = (values.get(key) or "").strip()
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
    # Focus blocks claim "FULL primary text" — if the drawings were never read, that
    # claim must be qualified loudly (the clipped roster already disclaims fullness).
    elif not clipped and _figures_unread(doc):
        body_parts.append(_DRAWINGS_NOT_READ)
    return head + "\n" + "\n".join(body_parts)


def _benchmark_figures_unread(bm: dict) -> bool:
    """True when a NUMBER-based benchmark's drawing sheets were never captioned.
    Upload-based benchmarks (text/photos) are exempt — their transcription IS the
    document as given. A merged DRAWINGS block in the description counts as read."""
    if not bm.get("number") or bm.get("text"):
        return False
    if "[FIG." in (bm.get("description") or ""):
        return False
    figs = bm.get("figures")
    if isinstance(figs, str):
        try:
            figs = json.loads(figs)
        except (ValueError, TypeError):
            figs = None
    return not (figs and any(f.get("caption") for f in figs))


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
        # DRAWINGS captions are merged at the description's tail — far beyond the
        # benchmark budget for any long document; surface them as their own field
        # ahead of the description or they are ALWAYS clipped away here.
        desc_body, drawings = _split_drawings(bm.get("description") or "")
        vals = {"abstract": bm.get("abstract"), "claims": bm.get("claims"),
                "drawings": drawings, "description": desc_body}
        for label, key in (("Abstract", "abstract"), ("Claims", "claims"),
                           ("Drawings (vision-read figure captions)", "drawings"),
                           ("Description", "description")):
            t = (vals.get(key) or "").strip()
            if t and budget > 100:
                chunk = f"{label}: {t[:budget]}"
                body.append(chunk)
                budget -= len(chunk)
    if not body:
        body.append(f"(content not ready — status: {bm.get('status', '?')}"
                    + (f", error: {bm['error']}" if bm.get("error") else "") + ")")
    elif _benchmark_figures_unread(bm):
        body.append(_DRAWINGS_NOT_READ)
    return head + "\n" + "\n".join(body)


def _focus_block(doc: dict) -> str:
    """A user-selected candidate rendered with the FULL primary-text budget (no
    abstract/digest-first clipping) — this is the uncl­ipped text the chat needs to
    quote real paragraphs/claims."""
    per = max(MIN_DOC_CHARS, min(MAX_FULLTEXT_CHARS, MAX_FOCUS_CHARS))
    return _document_block(doc, per, clipped=False)


def _discussions_body(discussions: list[dict]) -> str:
    """Chat exchanges (from db.cross_tab_discussions) rendered as prompt text,
    budget-capped at MAX_XTALK_CHARS. Shared by the chat block and the ⚖️ run."""
    blocks, used = [], 0
    for d in discussions:
        for ex in d.get("exchanges", []):
            lines = [f"{_ROLE.get(m.get('role', ''), 'User')}: "
                     f"{(m.get('text') or '')[:MAX_TURN_CHARS]}" for m in ex]
            when = (time.strftime("%Y-%m-%d", time.localtime(ex[0]["ts"]))
                    if ex and ex[0].get("ts") else "?")
            block = (f"[tab «{d.get('tab_name', '?')}» — {d.get('number', '?')} — "
                     f"{when}]\n" + "\n".join(lines))
            if used + len(block) > MAX_XTALK_CHARS:
                blocks.append(f"(…more discussion of {d.get('number', '?')} in tab "
                              f"«{d.get('tab_name', '?')}» did not fit the budget)")
                used = MAX_XTALK_CHARS
                break
            blocks.append(block)
            used += len(block)
        if used >= MAX_XTALK_CHARS:
            break
    return "\n\n".join(blocks)


def build_prompt(question: str, history: list[dict] | None = None,
                 documents: list[dict] | None = None,
                 sources: list[dict] | None = None,
                 skills: list[dict] | None = None,
                 benchmark: dict | None = None,
                 focus: list[dict] | None = None,
                 full: bool = False, answer_format: str = "",
                 xrefs: list[dict] | None = None,
                 other_docs: list[dict] | None = None,
                 coverage: list[dict] | None = None,
                 discussions: list[dict] | None = None) -> str:
    parts = [_PREAMBLE]
    if documents or focus or benchmark:
        parts.append(_GROUNDING_INSTRUCTION)
    if xrefs:
        # Documents named in the benchmark/question that live in OTHER tabs — their
        # stored digest/verdict, pulled in as read-only context so the model can
        # locate the referenced feature and hunt it across this tab's candidates.
        blocks = []
        for x in xrefs:
            head = f"[{x.get('number', '?')}"
            if x.get("tab_name"):
                head += f" — from tab «{x['tab_name']}»"
            head += "]"
            body = (x.get("verdict") or x.get("digest") or "").strip()[:MAX_TURN_CHARS]
            blocks.append(f"{head}\n{body}")
        parts.append(
            "CROSS-TAB REFERENCES — documents the user named that are stored in other "
            "tabs of this workbench. Their assessment/digest is provided so you can "
            "understand what feature is being referenced and find it among THIS tab's "
            "candidates. Treat as background context (derived summaries — do not cite "
            "[00NN] from them as verified):\n\n" + "\n\n".join(blocks))
    if discussions:
        # Full chat exchanges from OTHER tabs that mention a document named in this
        # conversation — the actual discussion, so "what did we say about X in the
        # other tab" is answerable verbatim, and follow-ups can build on it.
        parts.append(
            "PRIOR DISCUSSIONS IN OTHER TABS — chat exchanges from elsewhere in this "
            "workbench that mention a document named in the current conversation. When "
            "the user asks what was discussed/said/concluded about that document in "
            "other tabs, reproduce it faithfully from here (say which tab each exchange "
            "comes from) and answer follow-ups against it. These are conversation "
            "records, NOT primary patent text — do not cite [00NN]/claims from them as "
            "verified:\n\n" + _discussions_body(discussions))
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
    if other_docs:
        # Every OTHER tab's already-fetched documents, as a compact per-tab roster of
        # digests/verdicts — enough to identify (and combine) documents across tabs
        # without reloading. Budget-capped: highest-score first, digests clipped.
        per = max(400, MAX_ROSTER_CHARS // max(1, len(other_docs)))
        per = min(per, MAX_DOC_CHARS)
        used, included, dropped = 0, [], 0
        for d in other_docs:
            summary = (d.get("verdict") or d.get("digest") or "").strip()
            if not summary:
                continue
            if used + min(len(summary), per) > MAX_ROSTER_CHARS:
                dropped += 1
                continue
            tabs = ", ".join(d.get("tabs") or [d.get("tab_name", "?")])
            head = f"• {d.get('number', '?')} — {d.get('title') or 'no title'}  [tab: {tabs}"
            if d.get("score") is not None:
                head += f" · {d['score']:g}/10"
            head += "]"
            clip = " …[clipped]" if len(summary) > per else ""
            included.append(f"{head}\n  {summary[:per]}{clip}")
            used += min(len(summary), per)
        if included:
            cov = ""
            if coverage:
                cov = "\n\nPER-TAB DOCUMENT COUNTS (every tab, authoritative): " + \
                    "; ".join(f"«{c['tab_name']}» {c['fetched']} fetched/"
                              f"{c['total']} total" for c in coverage)
            drop_note = (f"\n\n(+{dropped} more cross-tab documents not shown — budget; "
                         "they exist and are fetched.)" if dropped else "")
            parts.append(
                "DOCUMENTS IN OTHER TABS — already fetched and OCR'd elsewhere in this "
                "workbench, shown with their stored assessment/digest. You MAY use these "
                "to answer, and to assemble a COMBINATION of documents that spans tabs "
                "(they are reused, not re-fetched). Cite each by its publication number "
                "and name which tab it comes from. These are DERIVED summaries — to quote "
                "primary text/[00NN], the doc must be opened in its own tab.\n\n"
                + "\n\n".join(included) + drop_note + cov
                + "\n\nAT THE START of your answer, state the cross-tab coverage you "
                "considered — the per-tab document counts above (e.g. 'Considered «A» "
                "202 docs, «B» 100 docs, …').")
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
         full: bool = False, answer_format: str = "",
         xrefs: list[dict] | None = None,
         other_docs: list[dict] | None = None,
         coverage: list[dict] | None = None,
         discussions: list[dict] | None = None) -> dict:
    """One stateless turn. Returns {answer, model, lessons:[(skill, text)]} | {error}.
    No tools — pure text; benchmark + documents arrive pre-fetched from the local DB.
    `focus` = user-selected candidates loaded with FULL primary text; `full` =
    long-form answer (otherwise 1-2 sentence precise mode); `answer_format` = an
    ANSWER_FORMATS key that, when set, dictates the answer's structure; `xrefs` =
    cross-tab documents the text names, pulled in as read-only context; `other_docs`
    = every OTHER tab's fetched docs (cross-tab roster); `coverage` = per-tab counts;
    `discussions` = chat exchanges from OTHER tabs mentioning a named document."""
    prompt = build_prompt(question, history, documents, sources, skills,
                          benchmark=benchmark, focus=focus, full=full,
                          answer_format=answer_format, xrefs=xrefs,
                          other_docs=other_docs, coverage=coverage,
                          discussions=discussions)
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


# 🔬 Claim decomposition. A claim written as ONE block cannot be combination-analysed:
# coverage is per element, and two documents can only "together cover everything" if
# there are separable elements to split between them. This turns the block into that list.
_DECOMPOSE_PROMPT = (
    "Split the CLAIMED INVENTION below into its constituent ELEMENTS — the individual "
    "technical features that a prior-art document could disclose, or fail to disclose, "
    "INDEPENDENTLY of the others. This is the decomposition a novelty / inventive-step "
    "analysis works from.\n\n"
    "RULES:\n"
    "• One element per distinct technical feature, in the claim's OWN order.\n"
    "• Use the claim's OWN wording, condensed to a single line. Never invent a feature, "
    "never generalise beyond what the claim says, never narrow it.\n"
    "• Split ONLY where separate disclosure is genuinely possible. Do NOT split a single "
    "indivisible feature into fragments that no document would disclose apart (e.g. keep "
    "'X electrically connected to Y' together if the connection IS the feature).\n"
    "• Conversely, do NOT merge two features a document could plausibly disclose "
    "separately — a merged element makes a real 2-document combination invisible.\n"
    "• Keep the preamble/category (what the thing IS) as element 1 when the claim has one.\n"
    "• weight 1-5 = how decisive that element is for the invention: 5 = the characterising "
    "heart of it, 1 = routine/contextual.\n\n"
    "OUTPUT — one element per line and NOTHING else (no preamble, no commentary):\n"
    "<n> | <weight 1-5> | <the element, one line>\n\n"
    "CLAIMED INVENTION:\n\n{text}")

_DECOMPOSE_RE = re.compile(r"^\s*\d+\s*\|\s*([1-5])\s*\|\s*(.+?)\s*$", re.MULTILINE)


def parse_decomposition(text: str) -> list[dict]:
    """'<n> | <weight> | <element>' lines → weighted M features, ready for the editor."""
    out = []
    for m in _DECOMPOSE_RE.finditer(text or ""):
        name = m.group(2).strip()
        if name and not name.startswith("<"):        # skip an echoed format template
            out.append({"name": name[:4000], "weight": int(m.group(1)), "kind": "M", "sl": 5})
    return out


def decompose_claim(invention_text: str, model: str | None = None) -> dict:
    """🔬 PROPOSE a split of the claimed invention into separable elements. Proposes only —
    the caller shows them for review; nothing is scored against them until accepted.
    Returns {elements: [{name, weight, kind, sl}], model} | {error}."""
    if not (invention_text or "").strip():
        return {"error": "nothing to decompose"}
    res = _run_claude(_DECOMPOSE_PROMPT.format(text=invention_text[:MAX_BENCHMARK_CHARS]),
                      model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    els = parse_decomposition(res["answer"])
    if not els:
        return {"error": "the model returned no parsable elements — try a stronger model"}
    return {"elements": els, "model": res.get("model")}


# 🔎 COMBI INVESTIGATION — element-level coverage, judged INDEPENDENTLY of every other
# score in the app. Stage 1 reads digests (cheap, spans the whole corpus); stage 2 re-reads
# the finalists' FULL text. Same output shape for both, so one parser serves them and a
# stage-2 verdict simply overwrites the stage-1 one for that document.
_COVERAGE_RULES = (
    "For EACH element answer YES (the document discloses it), PARTIAL (it discloses "
    "something that reads on the element only in part, or only implicitly) or NO.\n"
    "• Judge each element INDEPENDENTLY — a document may disclose some and not others; "
    "that is the point, so never let one element's answer colour another's.\n"
    "• NEVER invent disclosure. If the material is silent, answer NO.\n"
    "• This assessment is SELF-CONTAINED: ignore any ranking or score the document may "
    "already carry elsewhere.\n\n"
    "=== ELEMENTS OF THE CLAIMED INVENTION ===\n{elements}\n\n")

COMBI_DIGEST_PROMPT = (
    "You are mapping which ELEMENTS of a claimed invention each candidate patent "
    "discloses, so that pairs of documents which TOGETHER cover every element can be "
    "found.\n\n"
    + _COVERAGE_RULES +
    "You are given each candidate's DIGEST (a faithful summary of its full text). Judge "
    "only from the digest. A digest is a SUMMARY: if it is silent on an element, answer "
    "NO — a later full-text pass will confirm the finalists.\n\n"
    "=== CANDIDATES (digests) ===\n{docs}\n\n"
    "OUTPUT — for EVERY candidate, in this EXACT format, nothing else:\n"
    "=== <PUBLICATION NUMBER> ===\n"
    "<element number>: YES|PARTIAL|NO — <≤15 words of evidence>\n"
    "(one line per element, numbered as listed above)")

COMBI_FULL_PROMPT = (
    "You are confirming, against FULL PRIMARY TEXT, which ELEMENTS of a claimed invention "
    "this document discloses. A cheap summary-based pass shortlisted it; your verdict "
    "REPLACES that one, so judge only from the primary text below.\n\n"
    + _COVERAGE_RULES +
    "Cite the passage that carries each YES/PARTIAL ([00NN] paragraph marker, claim "
    "number, or Fig.). An element with no citable passage is NO.\n\n"
    "=== DOCUMENT (full primary text) ===\n{docs}\n\n"
    "OUTPUT — in this EXACT format, nothing else:\n"
    "=== <PUBLICATION NUMBER> ===\n"
    "<element number>: YES|PARTIAL|NO — <≤20 words, with the [00NN]/claim/Fig. cite>\n"
    "(one line per element, numbered as listed above)")

_COV_HEADER_RE = re.compile(r"^===\s*([A-Z]{1,3}\d[\dA-Z]*)\s*===\s*$", re.MULTILINE)
_COV_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.\)]\s*(YES|PARTIAL|NO)\b[\s—:-]*(.*)$",
                          re.IGNORECASE)


def parse_coverage(text: str, elements: list[dict]) -> dict:
    """'=== NUMBER ===' blocks of '<n>: YES|PARTIAL|NO — evidence' → {number: [{name,
    weight, status, evidence}]}, indexed back onto `elements`. Unanswered element → 'no'
    (silence is never disclosure)."""
    out: dict = {}
    parts = _COV_HEADER_RE.split(text or "")
    for i in range(1, len(parts) - 1, 2):
        num = parts[i].strip()
        by_idx: dict[int, tuple[str, str]] = {}
        for line in parts[i + 1].splitlines():
            m = _COV_LINE_RE.match(line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(elements):
                by_idx[idx] = (m.group(2).lower(), m.group(3).strip()[:200])
        if by_idx:
            out[num] = [{"name": e.get("name", ""), "weight": int(e.get("weight", 1)),
                         "status": by_idx.get(j, ("no", ""))[0],
                         "evidence": by_idx.get(j, ("no", ""))[1]}
                        for j, e in enumerate(elements)]
    return out


def _element_lines(elements: list[dict]) -> str:
    return "\n".join(f"{i}. {e['name']}  (importance {e.get('weight', 1)}/5)"
                     for i, e in enumerate(elements, 1))


def combi_coverage_digests(elements: list[dict], docs: list[dict],
                           model: str | None = None) -> dict:
    """🔎 STAGE 1 — element coverage for a BATCH of candidates from their stored digests.
    Cheap enough to span the whole corpus, which is the point: the pair that covers
    everything may sit far down the ranking. Returns {results, model} | {error}."""
    if not elements or not docs:
        return {"results": {}}
    doc_blocks = "\n\n".join(
        f"=== {d['number']} ===\n{(d.get('digest') or '(no digest available)')[:6000]}"
        for d in docs)
    res = _run_claude(COMBI_DIGEST_PROMPT.format(elements=_element_lines(elements),
                                                 docs=doc_blocks),
                      model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    return {"results": parse_coverage(res["answer"], elements), "model": res.get("model")}


def combi_coverage_full(elements: list[dict], doc: dict, model: str | None = None) -> dict:
    """🔎 STAGE 2 — re-read ONE finalist's FULL primary text against the elements. The
    stage-1 digest verdict is a summary-based approximation; this replaces it with a
    citable one. Returns {results, model} | {error}."""
    if not elements or not doc:
        return {"results": {}}
    block = f"=== {doc['number']} ===\n" + _document_block(doc, MAX_FULLTEXT_CHARS,
                                                           clipped=False)
    res = _run_claude(COMBI_FULL_PROMPT.format(elements=_element_lines(elements),
                                               docs=block),
                      model or CHAT_MODEL, timeout=PSA_TIMEOUT)
    if "error" in res:
        return res
    return {"results": parse_coverage(res["answer"], elements), "model": res.get("model")}


_PSA_INSTRUCTION = (
    "TASK — PROBLEM-SOLUTION APPROACH.\n"
    "The USER-SUPPLIED METHODOLOGY above is BINDING. Execute it STRICTLY, step by "
    "step, in the exact order it is written:\n"
    "• Work through EVERY step/point of the methodology — do not skip, merge, "
    "reorder, abbreviate or invent steps.\n"
    "• Head each part of your answer with the methodology's OWN step names/"
    "numbering, so the execution of every step is visible and checkable.\n"
    "• If a step cannot be executed with the provided material, say so explicitly "
    "under that step's heading (and what is missing), then continue with the next "
    "step — never silently drop it.\n"
    "• The CLAIMED INVENTION under assessment is given above under 'CLAIMED "
    "INVENTION UNDER ASSESSMENT' — assess THAT, and only that, whether it is a "
    "whole document or a single feature/claim the user supplied verbatim. The two "
    "selected documents D1 and D2 are the prior art the approach is based on "
    "(e.g. closest prior art and combination document — assign their roles as the "
    "methodology directs).\n"
    "• Ground every factual statement in the provided texts with [00NN]/claim/"
    "Fig. citations, per the grounding rules above.")


_PSA_STRETCH_INSTRUCTION = (
    "ADVOCACY MODE — ARGUMENTATION STRETCH. For THIS run only, the task above is "
    "modified: produce the STRONGEST argumentation the provided disclosures can "
    "honestly support — the reading most favorable to the case, as a party's "
    "advocate would draft it.\n"
    "• Argue AFFIRMATIVELY from what D1/D2 DISCLOSE. Where a feature is disclosed "
    "only implicitly, functionally, partially or via an equivalent, put forward "
    "the stretched interpretation — anchored to the exact passage ([00NN]/claim/"
    "Fig.) that carries it, phrased as the reading of that passage.\n"
    "• REMAIN SILENT about features that are NOT disclosed: do not enumerate "
    "gaps, do not volunteer weaknesses or counter-arguments, do not add "
    "'however/but X is missing' caveats. Simply build the argument from what is "
    "there.\n"
    "• Omission is allowed — misstatement is NOT. Never assert that a "
    "non-disclosed feature IS disclosed, never invent or paraphrase text beyond "
    "what a cited passage actually says, never cite a passage for more than it "
    "carries. Every sentence you write must remain TRUE and verifiable against "
    "the provided texts; the stretch lives in interpretation, emphasis and "
    "selection — not in facts.\n"
    "• Still execute the methodology step by step under its own headings (and "
    "the output format, if provided); within each step, present the advocacy "
    "version of that step's result.")


def psa(method_text: str, benchmark: dict | None, docs: list[dict],
        model: str | None = None, format_text: str | None = None,
        discussions: list[dict] | None = None, stretch: bool = False,
        invention: dict | None = None) -> dict:
    """⚖️ Problem-solution approach: run the user's uploaded methodology STRICTLY,
    step by step, over the CLAIMED INVENTION + two user-selected prior-art documents
    (full primary text). `invention` = {'label', 'text'} — an explicit basis (e.g. a
    feature the user pasted) that REPLACES the benchmark as the claimed invention;
    the benchmark is then not sent at all, so the run assesses exactly what the user
    chose and nothing else. Without it the benchmark document is the invention.
    `format_text` = the user's uploaded output-format document, applied in combination
    with the steps; `discussions` = ALL chats' exchanges about D1/D2, reused as prior
    findings; `stretch` = 🪄 advocacy mode (argue the disclosed, silent on gaps, facts
    stay true). Same return contract as chat()."""
    parts = [_PREAMBLE, _GROUNDING_INSTRUCTION]
    parts.append("USER-SUPPLIED METHODOLOGY (BINDING — the answer must follow it "
                 "verbatim, step by step):\n\n" + (method_text or "")[:MAX_METHOD_CHARS])
    if format_text:
        parts.append(
            "USER-SUPPLIED OUTPUT FORMAT (BINDING — the answer's STRUCTURE must "
            "follow this document exactly, in combination with the methodology "
            "steps above; where the two conflict on structure, this format "
            "document wins):\n\n" + format_text[:MAX_METHOD_CHARS])
    if invention:
        parts.append(
            f"CLAIMED INVENTION UNDER ASSESSMENT — {invention['label']}. The user "
            "chose THIS as the basis of the run; it is the invention the approach "
            "assesses, verbatim. No benchmark document accompanies it — do not ask "
            "for one and do not assess anything else:\n\n"
            + (invention.get("text") or "")[:MAX_BENCHMARK_CHARS])
    else:
        parts.append("CLAIMED INVENTION UNDER ASSESSMENT — the benchmark document:"
                     "\n\n" + _benchmark_block(benchmark or {}))
    per = max(MIN_DOC_CHARS, min(MAX_FULLTEXT_CHARS,
                                 MAX_FOCUS_CHARS // max(1, len(docs))))
    for i, d in enumerate(docs, 1):
        parts.append(f"D{i} — selected prior-art document {i} of {len(docs)} "
                     "(FULL primary text):\n\n" + _document_block(d, per, clipped=False))
    if discussions:
        parts.append(
            "PRIOR DISCUSSIONS ABOUT D1/D2 — every exchange from this workbench's "
            "chats (all tabs) that mentions the selected documents: the latest "
            "findings, arguments, feature mappings and conclusions already worked "
            "out about them. REUSE this prior analysis while executing the "
            "methodology — do not rediscover from scratch what is already "
            "established here, and flag where your step-by-step result contradicts "
            "an earlier conclusion. These are conversation records, NOT primary "
            "text — verify any [00NN]/claim citation you take from them against "
            "the D1/D2 full texts above before relying on it:\n\n"
            + _discussions_body(discussions))
    parts.append(_PSA_INSTRUCTION)
    if stretch:
        parts.append(_PSA_STRETCH_INSTRUCTION)
    res = _run_claude("\n\n---\n\n".join(parts), model or CHAT_MODEL,
                      timeout=PSA_TIMEOUT)
    if "error" in res:
        return res
    res["answer"] = _strip_cjk(res["answer"])
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
    if _figures_unread(doc):
        fulltext += "\n\n" + _DRAWINGS_NOT_READ
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
    # Deficiency stamp — appended in CODE, not left to the model (LLM caveats are
    # flaky; this one must survive verbatim). The verdict is persisted and reused by
    # the chat roster, 📊 re-rank and the reduce, so the gap stays visible everywhere
    # until the user opts into a figure read.
    if _figures_unread(doc):
        verdict += ("\n\n⚠ DRAWINGS NOT READ — this assessment covers the text only "
                    "(abstract/claims/description); the figure sheets were never "
                    "vision-captioned. For a check that includes the drawings, run "
                    "🖼 Read figures on this document, then re-read it.")
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


ADDITIONAL_PROMPT = (
    "You are checking whether each candidate patent discloses a set of ADDITIONAL features "
    "(bonus features). These are NOT the mandatory benchmark — their ABSENCE must not count "
    "against a document; you are only looking for whether each is PRESENT, can be reasonably "
    "STRETCHED to be present, or is ABSENT.\n\n"
    "Each additional feature has a STRETCH LEVEL (SL, 1–10): how generous a reading you may "
    "give it. SL 9–10 = accept a broad/implicit realisation; SL 5 = a fair reading; SL 1–2 = "
    "must be almost literal. Use STRETCH only when the disclosure supports it WITHIN that "
    "feature's SL; otherwise ABSENT.\n\n"
    "You are given each candidate's DIGEST (a faithful summary of its full text). Judge only "
    "from the digest; if the digest is silent, answer ABSENT (do not invent).\n\n"
    "=== ADDITIONAL FEATURES ===\n{features}\n\n"
    "=== CANDIDATES (digests) ===\n{docs}\n\n"
    "OUTPUT — for EVERY candidate, in this EXACT format, nothing else:\n"
    "=== <PUBLICATION NUMBER> ===\n"
    "<feature number>: PRESENT|STRETCH|ABSENT — <≤15 words of evidence>\n"
    "(one line per additional feature, numbered as listed above)")


def additional_read(a_features: list[dict], docs: list[dict], model: str | None = None) -> dict:
    """ONE bulk pass (default sonnet) over candidates' STORED DIGESTS — no full-text re-read —
    checking only the ADDITIONAL (A) features, honouring each feature's stretch level. Returns
    {results: {number: [{name, weight, sl, status, evidence}]}} | {error}. Cheap: digests are a
    few KB each and all candidates are judged in a single call."""
    if not a_features or not docs:
        return {"results": {}}
    feat_lines = "\n".join(
        f"{i}. {f['name']}  (importance {f.get('weight', 1)}/5, stretch level {f.get('sl', 5)}/10)"
        for i, f in enumerate(a_features, 1))
    doc_blocks = "\n\n".join(
        f"=== {d['number']} ===\n{(d.get('digest') or '(no digest available)')[:6000]}"
        for d in docs)
    prompt = ADDITIONAL_PROMPT.format(features=feat_lines, docs=doc_blocks)
    res = _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    return {"results": parse_additional(res["answer"], a_features), "model": res.get("model")}


_ADD_HEADER_RE = re.compile(r"^===\s*([A-Z]{1,3}\d[\dA-Z]*)\s*===\s*$", re.MULTILINE)
_ADD_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.\)]\s*(PRESENT|STRETCH|ABSENT)\b[\s—:-]*(.*)$",
                          re.IGNORECASE)


def parse_additional(text: str, a_features: list[dict]) -> dict:
    """Map the bulk additional-read output back onto {number: [{name,weight,sl,status,evidence}]}.
    Each '=== NUMBER ===' block holds 'N: STATUS — evidence' lines indexed to a_features."""
    out: dict = {}
    parts = _ADD_HEADER_RE.split(text or "")
    # split → ['', num1, body1, num2, body2, …]
    for i in range(1, len(parts) - 1, 2):
        num = parts[i].strip()
        body = parts[i + 1]
        feats: list[dict] = []
        for line in body.splitlines():
            m = _ADD_LINE_RE.match(line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if not (0 <= idx < len(a_features)):
                continue
            f = a_features[idx]
            feats.append({"name": f.get("name", ""), "weight": int(f.get("weight", 1)),
                          "sl": int(f.get("sl", 5)), "status": m.group(2).lower(),
                          "evidence": m.group(3).strip()[:200]})
        if feats:
            out[num] = feats
    return out


CROSS_TAB_SCAN_PROMPT = (
    "You screen patent documents from OTHER projects against THIS project's benchmark. "
    "For each candidate below, judge from its stored DIGEST (a faithful summary of its "
    "full text) whether it discloses ANY of the benchmark's target features — even ONE "
    "covered feature makes it worth pulling in. If the digest is silent on a feature, "
    "treat it as not shown (do not invent).\n\n"
    "{benchmark}\n\n"
    "{features}\n\n"
    "=== CANDIDATES (digests) ===\n{docs}\n\n"
    "OUTPUT — for EVERY candidate, in this EXACT format, nothing else:\n"
    "=== <PUBLICATION NUMBER> ===\n"
    "then EITHER one line per COVERED target feature (omit uncovered ones):\n"
    "FEATURE <n>: <YES|PARTIAL> — <≤15 words: what in the candidate covers it>\n"
    "OR, when target features are not numbered, a single line:\n"
    "COVERS: <≤20 words: which benchmark elements it discloses>\n"
    "OR, if it covers nothing of the benchmark:\n"
    "MATCHES: NONE")

_XSCAN_FEAT_RE = re.compile(
    r"FEATURE\s*(\d+)\s*:\s*(YES|PARTIAL)\b[\s—:-]*(.*)", re.IGNORECASE)
_XSCAN_COVERS_RE = re.compile(r"COVERS:\s*(.+)", re.IGNORECASE)


def cross_tab_scan(benchmark: dict, features: list[dict], docs: list[dict],
                   model: str | None = None) -> dict:
    """🏆 Best-match cross-tab screen: ONE bulk call (default sonnet) judging each
    OTHER-tab candidate's stored digest against THIS tab's benchmark. The caller
    batches; every input doc gets a verdict (empty = no coverage → negative-cache).
    Returns {results: {number: {features: [{name,weight,status,evidence}],
    covers: str|None}}} | {error}."""
    if not docs:
        return {"results": {}}
    feat_lines = ("TARGET FEATURES (numbered):\n" + "\n".join(
        f"{i}. {f.get('name', '')}" for i, f in enumerate(features, 1))
        if features else "TARGET FEATURES: not numbered — judge against the "
                         "benchmark's claims/technical solution as a whole.")
    doc_blocks = "\n\n".join(
        f"=== {d['number']} ===\n"
        f"{((d.get('digest') or d.get('verdict') or '') or '(no digest available)')[:3000]}"
        for d in docs)
    prompt = CROSS_TAB_SCAN_PROMPT.format(
        benchmark="BENCHMARK:\n\n" + _benchmark_block(benchmark),
        features=feat_lines, docs=doc_blocks)
    res = _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    return {"results": parse_cross_tab_scan(res["answer"], features),
            "model": res.get("model")}


def parse_cross_tab_scan(text: str, features: list[dict]) -> dict:
    """Map the bulk scan output back onto {number: {features, covers}}. A block with
    no FEATURE/COVERS line (e.g. 'MATCHES: NONE') yields an EMPTY entry — the caller
    needs the negatives too, to cache them."""
    out: dict = {}
    parts = _ADD_HEADER_RE.split(text or "")
    for i in range(1, len(parts) - 1, 2):
        num = parts[i].strip()
        body = parts[i + 1]
        feats: list[dict] = []
        for line in body.splitlines():
            m = _XSCAN_FEAT_RE.search(line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if not (0 <= idx < len(features)):
                continue
            f = features[idx]
            feats.append({"name": f.get("name", ""), "weight": int(f.get("weight", 1)),
                          "status": m.group(2).lower(),
                          "note": m.group(3).strip()[:200]})
        cov = _XSCAN_COVERS_RE.search(body)
        covers = cov.group(1).strip()[:200] if cov else None
        if covers and covers.lower() in ("none", "none.", "-"):
            covers = None
        out[num] = {"features": feats, "covers": covers}
    return out


DIGEST_RESCORE_PROMPT = (
    "Re-score each candidate patent against the BENCHMARK below, using ONLY the candidate's "
    "stored DIGEST (a faithful summary of its full text) — do NOT ask for or assume full text. "
    "This is a fast re-check after a benchmark change; judge from the digest. If the digest is "
    "silent on a benchmark element, treat it as not shown (do not invent).\n\n"
    "{benchmark}\n\n"
    "=== CANDIDATES (digests) ===\n{docs}\n\n"
    "OUTPUT — for EVERY candidate, in this EXACT format, nothing else:\n"
    "=== <PUBLICATION NUMBER> ===\n"
    "SCORE: <0-10>\n"
    "WHY: <≤20 words: the decisive matched/missing benchmark elements>")


def digest_rescore(benchmark: dict, docs: list[dict], model: str | None = None) -> dict:
    """Fast re-score (default sonnet) of candidates against the CURRENT benchmark using their
    STORED DIGESTS — one bulk call, NO full-text re-read. For when the benchmark changed and the
    user wants updated scores cheaply. Returns {results: {number: {score, note}}} | {error}."""
    if not docs:
        return {"results": {}}
    bm_block = _benchmark_block(benchmark)
    doc_blocks = "\n\n".join(
        f"=== {d['number']} ===\n{(d.get('digest') or '(no digest available)')[:6000]}"
        for d in docs)
    prompt = DIGEST_RESCORE_PROMPT.format(benchmark="BENCHMARK:\n\n" + bm_block, docs=doc_blocks)
    res = _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    return {"results": parse_digest_rescore(res["answer"]), "model": res.get("model")}


_RESCORE_SCORE_RE = re.compile(r"SCORE:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_RESCORE_WHY_RE = re.compile(r"WHY:\s*(.+)", re.IGNORECASE)


def parse_digest_rescore(text: str) -> dict:
    """Map the bulk digest-rescore output ('=== NUM ===' blocks with SCORE/WHY) back onto
    {number: {score, note}}."""
    out: dict = {}
    parts = _ADD_HEADER_RE.split(text or "")
    for i in range(1, len(parts) - 1, 2):
        num = parts[i].strip()
        body = parts[i + 1]
        m = _RESCORE_SCORE_RE.search(body)
        if not m:
            continue
        w = _RESCORE_WHY_RE.search(body)
        out[num] = {"score": min(10.0, max(0.0, float(m.group(1)))),
                    "note": (w.group(1).strip()[:200] if w else "")}
    return out


COMBI_MOTIVATION_PROMPT = (
    "You assess whether PAIRS of patent references are genuinely COMBINABLE (obviousness-style) to "
    "reach the BENCHMARK invention below. For each pair, reference A and reference B TOGETHER disclose "
    "the benchmark's mandatory features — each supplies the part the other lacks. A pair is only "
    "useful if a skilled person would have a REAL motivation/reason to combine them: same or adjacent "
    "technical field, compatible structures, or an explicit teaching/suggestion pointing to the "
    "combination. Features merely 'adding up' is NOT enough — if combining is far-fetched, say NO.\n\n"
    "{benchmark}\n\n"
    "=== REFERENCE PAIRS (judge from the digests) ===\n{pairs}\n\n"
    "OUTPUT — for EVERY pair, in this EXACT format, nothing else:\n"
    "=== <PAIR NUMBER> ===\n"
    "COMBINABLE: <YES or NO>\n"
    "WHY: <≤25 words: the concrete motivation to combine, or the obstacle if NO>")


def combi_motivation(benchmark: dict, pairs: list[dict], model: str | None = None) -> dict:
    """ONE bulk pass (default sonnet) judging, for each candidate PAIR, whether the two references
    are genuinely combinable (real motivation to combine) to reach the benchmark. `pairs` =
    [{a:{number,digest}, b:{number,digest}, a_features:[names], b_features:[names]}]. Returns
    {results: {pair_index_str: {combinable, reason}}, model} | {error}. Cheap: digests only, one call."""
    if not pairs:
        return {"results": {}}
    bm_block = _benchmark_block(benchmark)
    blocks = []
    for i, p in enumerate(pairs, 1):
        a, b = p["a"], p["b"]
        blocks.append(
            f"=== {i} ===\n"
            f"REFERENCE A = {a['number']} — supplies: {', '.join(p.get('a_features') or []) or '—'}\n"
            f"{(a.get('digest') or '(no digest available)')[:3000]}\n\n"
            f"REFERENCE B = {b['number']} — supplies: {', '.join(p.get('b_features') or []) or '—'}\n"
            f"{(b.get('digest') or '(no digest available)')[:3000]}")
    prompt = COMBI_MOTIVATION_PROMPT.format(benchmark="BENCHMARK:\n\n" + bm_block,
                                            pairs="\n\n".join(blocks))
    res = _run_claude(prompt, model or DIGEST_MODEL, timeout=DIGEST_TIMEOUT)
    if "error" in res:
        return res
    return {"results": parse_combi_motivation(res["answer"]), "model": res.get("model")}


_COMBI_IDX_RE = re.compile(r"^===\s*(\d+)\s*===\s*$", re.MULTILINE)
_COMBI_YN_RE = re.compile(r"COMBINABLE:\s*(YES|NO)", re.IGNORECASE)
_COMBI_WHY_RE = re.compile(r"WHY:\s*(.+)", re.IGNORECASE)


def parse_combi_motivation(text: str) -> dict:
    """Map the bulk combi output ('=== N ===' blocks with COMBINABLE/WHY) back onto
    {pair_index_str: {combinable: bool, reason}}."""
    out: dict = {}
    parts = _COMBI_IDX_RE.split(text or "")
    for i in range(1, len(parts) - 1, 2):
        idx = parts[i].strip()
        body = parts[i + 1]
        m = _COMBI_YN_RE.search(body)
        if not m:
            continue
        w = _COMBI_WHY_RE.search(body)
        out[idx] = {"combinable": m.group(1).upper() == "YES",
                    "reason": (w.group(1).strip()[:240] if w else "")}
    return out


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
