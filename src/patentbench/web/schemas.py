"""Pydantic request models for the Patent Workbench API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TabCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TabPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class DocumentsAdd(BaseModel):
    text: str | None = None            # free text / pasted numbers / URLs
    numbers: list[str] | None = None   # pre-confirmed canonical numbers (upload flow)
    source: str = "manual"
    reading_model: str | None = None   # model for the full-text digest; None = cheapest


class DocumentNumberEdit(BaseModel):
    number: str = Field(min_length=4, max_length=40)


class BenchmarkSet(BaseModel):
    text: str = Field(min_length=1, max_length=2000)   # a number or a link containing one


class BenchmarkTranscribe(BaseModel):
    # Decline the cross-tab reuse offer and OCR the uploaded benchmark pages anew.
    reading_model: str | None = None


class FeatureItem(BaseModel):
    # One target feature added "one by one" with its own importance weight (1–5,
    # default 1). The weight feeds the candidate ranking: the primary rank key is
    # the sum of the weights a candidate discloses, broken by how many it matches.
    name: str = Field(min_length=1, max_length=4000)
    weight: int = Field(default=1, ge=1, le=5)
    # kind: "M" = mandatory/fundamental (drives the base score); "A" = additional (presence
    # raises the score, absence never lowers it). sl = stretch level 1-10 for A features:
    # how far the argument may be stretched (only meaningful when kind == "A").
    kind: str = Field(default="M")
    sl: int = Field(default=5, ge=1, le=10)


class BenchmarkFeatures(BaseModel):
    # The benchmark defined as a TARGET FEATURE COMBINATION. Two equivalent inputs:
    #   • spec     — one free-form window (everything together), no per-feature weights
    #   • features — a list of individually weighted features (added one by one)
    # When `features` is given it wins: the benchmark text is composed from the
    # weighted list and the weights drive scoring. Stored as the benchmark text and
    # read as-is by the matching/chat models; weights are kept separately.
    spec: str | None = Field(default=None, min_length=10, max_length=40000)
    features: list[FeatureItem] = []
    title: str | None = Field(default=None, max_length=200)


class NotebookConfig(BaseModel):
    notebook_id: str | None = None     # None/empty disconnects
    notebook_title: str | None = None
    source_ids: list[str] = []
    auto_add: bool = False             # mirror new candidates into the notebook


class NotebookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class NotebookAddSelected(BaseModel):
    """Push a CHOSEN subset of the tab's documents into a CHOSEN notebook (vs
    /notebook/sync which mirrors everything fetched into the connected one).
    notebook_id picks the destination explicitly; None falls back to the tab's
    connected notebook (auto-creating one if needed)."""
    doc_ids: list[int] = []
    include_benchmark: bool = True
    notebook_id: str | None = None


class NotebookResync(BaseModel):
    """Reconcile app↔NLM source membership. By default scans the tab's own notebooks
    (connected + rollover siblings); notebook_ids overrides with an explicit set, and
    scan_all scans EVERY notebook in the account (slower, finds stray placements)."""
    notebook_ids: list[str] | None = None
    scan_all: bool = False


class NotebookSourceDelete(BaseModel):
    """Permanently delete sources from a notebook (dedup / free the 50-source cap)."""
    notebook_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class NotebookDistribute(BaseModel):
    """Fill candidates ACROSS several notebooks' free space (auto-split). doc_ids empty
    = every fetched candidate not yet in NLM. notebook_ids None = the tab's own
    notebooks that have room (most-free first); pass an explicit ordered list to control
    which notebooks receive them and in what order (manual 40+37-style splits)."""
    doc_ids: list[int] = []
    notebook_ids: list[str] | None = None
    include_benchmark: bool = False


# A pasted patent excerpt / long instruction is a legitimate question — cap it
# generously (the prompt builder clips per-turn history downstream) rather than
# rejecting a long message with an opaque 422.
MAX_QUESTION = 500_000


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    model: str | None = None
    skills: list[str] = []
    use_documents: bool = True
    ask_notebook: bool = False
    full: bool = False                 # long-form answer; default = 1-2 sentence precise mode
    answer_format: str = ""            # ANSWER_FORMATS key; "" = default style
    focus_ids: list[int] = []          # selected candidates loaded with FULL primary text
    all_tabs: bool = False             # also give the model every OTHER tab's fetched docs


class AskNotebookRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)


class PsaRequest(BaseModel):
    doc_ids: list[int] = Field(min_length=2, max_length=2)   # exactly D1 and D2
    model: str | None = None           # answer model; None = chat default
    use_discussions: bool = True       # 💬 feed ALL chats' exchanges about D1/D2 into the run
    stretch: bool = False              # 🪄 advocacy mode: argue disclosed, silent on gaps
    # What the run assesses as the CLAIMED INVENTION. Explicit, never inferred — the
    # basis used is recorded on the run message so a past run can always be read back.
    #   'benchmark' (default, unchanged) — the benchmark document
    #   'features'  — the benchmark's weighted target features
    #   'text'      — basis_text verbatim (e.g. a feature pasted in the chat box); it
    #                 REPLACES the benchmark, which is then not sent at all
    basis: Literal["benchmark", "features", "text"] = "benchmark"
    basis_text: str | None = Field(default=None, max_length=MAX_QUESTION)


class DeepCompareRequest(BaseModel):
    model: str | None = None           # answer/compile model (chat dropdown)
    skills: list[str] = []
    question: str | None = Field(default=None, max_length=MAX_QUESTION)
    doc_ids: list[int] | None = None   # subset to analyze; None/empty = all candidates
    reading_model: str | None = None   # model that reads full texts; None = cheapest
    skip_scored: bool = False          # CONTINUE mode: only read candidates not yet full-read


class NlmRateRequest(BaseModel):
    force: bool = False                 # re-rate candidates that already have an NLM score
    doc_ids: list[int] | None = None   # subset to rate; None/empty = every fetched candidate


class NlmShortlistRequest(BaseModel):
    # Free, broad pre-filter: ONE NotebookLM fan-out question (grounded on the
    # sources, no token cost to us) returns which documents disclose the benchmark's
    # feature combination → narrows 100s of candidates to a handful before the
    # expensive opus verification. None question = build it from the benchmark.
    question: str | None = Field(default=None, max_length=MAX_QUESTION)
    # restrict the query to ONE notebook (e.g. the just-consolidated one → a single
    # global best/second-best pick); None = fan across every notebook the candidates live in
    notebook_id: str | None = None


class PipelineRequest(BaseModel):
    """Run consolidate → shortlist → debate as ONE resumable background job. resume=True
    continues an interrupted job from its last completed step (reusing the notebook it
    already created); otherwise title + doc_ids start a fresh run."""
    title: str = Field(default="", max_length=200)
    doc_ids: list[int] | None = None       # explicit finalists; None → auto-pick Claude's top_n
    top_n: int = Field(default=49, ge=1, le=49)   # funnel size when doc_ids is None (49+benchmark=50-cap)
    include_benchmark: bool = True
    consolidate_only: bool = False         # stop after copying the 49 in — no shortlist, no NLM query
    resume: bool = False


class CrossTabScanRequest(BaseModel):
    """🏆 Best-match cross-tab scan: digest-check every OTHER tab's fetched document
    against THIS tab's benchmark and import any that covers ≥1 target feature as a
    first-class candidate here (with the covered features indicated)."""
    model: str | None = None


class DigestRescoreRequest(BaseModel):
    """♻️ Re-check: re-score the top candidates against the CURRENT benchmark from their stored
    digests in ONE bulk pass — no full-text re-read. doc_ids = the UI's ranked top-N; else top_n
    by current score."""
    doc_ids: list[int] | None = None
    top_n: int = Field(default=49, ge=1, le=50)
    model: str | None = None


class AdditionalReadRequest(BaseModel):
    """➕ Additional read: check the benchmark's ADDITIONAL (A) features against candidates'
    stored digests in bulk passes (no full-text re-read). Scope, in priority order:
    all_docs → EVERY candidate with a digest; doc_ids → that exact set (the UI sends its
    top-N in ranked order); else top_n by Claude score."""
    doc_ids: list[int] | None = None
    top_n: int = Field(default=10, ge=1, le=50)
    # all_docs has no count cap on purpose — the endpoint batches, so the scope is bounded
    # by how many candidates actually have a digest, not by the prompt budget.
    all_docs: bool = False
    model: str | None = None


class CombiPair(BaseModel):
    """One candidate PAIR the UI asks to judge for combinability. a_features / b_features =
    the mandatory feature names each reference uniquely supplies (for the LLM's context)."""
    a_id: int
    b_id: int
    a_features: list[str] = Field(default_factory=list)
    b_features: list[str] = Field(default_factory=list)


class CombiMotivationRequest(BaseModel):
    """🧩 Combi: judge whether each given document PAIR is genuinely combinable (motivation to
    combine) to cover the benchmark. The UI sends its top complete pairs; one bulk LLM pass."""
    pairs: list[CombiPair] = Field(default_factory=list)
    model: str | None = None


class NotebookConsolidate(BaseModel):
    """Create ONE new notebook (user-named) and copy a chosen set of the tab's
    candidates (+ benchmark) into it, then connect the tab — so a single global
    🏆 best-match query can compare them all. doc_ids None = every fetched candidate."""
    title: str = Field(min_length=1, max_length=200)
    doc_ids: list[int] | None = None
    include_benchmark: bool = True


class NlmChallengeRequest(BaseModel):
    # Confront NotebookLM (grounded on the docs) with Claude's top picks in ONE query
    # and ask it to reconcile the divergence. doc_ids overrides "Claude's top picks".
    doc_ids: list[int] | None = None


class ReconcileRequest(BaseModel):
    model: str | None = None           # cheap model by default — this is one small call
    min_delta: float = 2.0             # only candidates whose two scores differ by >= this


class LessonCreate(BaseModel):
    skill: str
    lesson: str = Field(min_length=1, max_length=8000)


# ---------- knowledge graph ----------

class KgClassifyRequest(BaseModel):
    """Ask the LLM to place a feature in the cross-tab taxonomy and return an existing
    node to link to (if any) + a draft field›block›function›option path to confirm."""
    feature_name: str = Field(min_length=1, max_length=4000)
    tab_id: int | None = None
    doc_id: int | None = None
    model: str | None = None


class KgAttachRequest(BaseModel):
    """Confirm/persist a classification (the [Link]/[New] step). Either link to an
    existing node_id, or create the field›block›function›option chain from the draft."""
    feature_name: str = Field(min_length=1, max_length=4000)
    node_id: int | None = None                 # link to this existing node…
    field: str | None = Field(default=None, max_length=200)   # …or build this path
    block: str | None = Field(default=None, max_length=200)
    function: str | None = Field(default=None, max_length=200)
    option: str | None = Field(default=None, max_length=200)
    related_blocks: list[str] = []
    tab_id: int | None = None
    doc_id: int | None = None
    status: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class KgNodePatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    parent_id: int | None = None               # reparent (may be null → root)
    reparent: bool = False                     # explicit: apply parent_id even if null


class KgEdgeRequest(BaseModel):
    src_id: int
    dst_id: int
    rel: str = "related"


class KgRebuildRequest(BaseModel):
    tab_id: int | None = None                  # None = every tab
    model: str | None = None
    clear: bool = False                        # wipe the graph first
