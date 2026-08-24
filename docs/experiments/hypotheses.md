# Hypothesis register — NLM v2 screen viability (owner: hypothesis-driver agent)

Status: OPEN · SUPPORTED · REFUTED · SCOPED (true only under stated scope) · PARKED

Last update: 2026-08-24 21:50 UTC container clock (cycle 1). All conclusions pre-audit until the
pipeline-integrity-supervisor + recall/ranking auditors gate them.

| id | hypothesis | evidence (2026-08-24 cycle 1) | status | next test |
|---|---|---|---|---|
| H1 | The screen's miss class = old (pre-~2008), non-CJK disclosures lacking a modern processor/memory/CRM claim skeleton | t10: all 30 profile-matched non-graduates now opus-read (14 earlier + 16 in the 21:41 job) — 30/30 ≤ 2. t14: the 2 profile "misses" (US5686815 1997, DE10158062 2003) are `add_failed` docs — they never reached NotebookLM (see H7), so they are not evidence of a judgement miss | REFUTED on t10; on t14 the evidence is confounded by H7 (no screened old-Western doc ≥4 was rejected: the 7 true rejects are CN×3, KR, WO, US, DE… with max 3) | none — line closed (marginal ≈ 0) |
| H2 | t10/t12 over-clip subsets contain no unread champions (earlier funnels already found them) | t10: 94/216 true rejects opus-read, 0 ≥3; 79 graduates read, 1 at 3 (US9991741), 0 ≥4; wave-2 + 21:41 job added 0 ≥4. t12: 9/9 true rejects read 0 ≥3; 15/15 grads read, 1 at 3 (KR20250094125) | SUPPORTED (scope: docs that actually reached NLM; the add_failed pool is H7) | none |
| H3 | Stage-2 citation follow-up ≈ opus on graduates (near-opus at zero token cost) | ledger has doc lists only (t10 10 docs at 21:35: EP3005248 EP2417690 US20090108997 US10027187 US8217782 CN107078561 US11316378 EP3281272 CN106233559 US11005308); per-feature verdicts not yet journaled by the verifiers; t14→t12 verifier running | OPEN | three-way table when the verifier agents journal per-doc verdicts — no opus reads needed (all 10 t10 docs and all t14 graduates already opus-read) |
| H4 | The t14 miss rate generalises to the default-account tabs (t11/t13) | default account has answered 0 rounds in 24 h (t11 0/188, t13 0/458). Note both already show add_failed marks (t11 15, t13 13 this run) before any answer | OPEN (blocked on quota) | at ~150 screened docs per tab: the true-reject pool (not add_failed) + 30-doc random control |
| H5 | NLM graduation rank carries no relevance ordering | unchanged; 21:41 job: the only ≥3 graduate (US9991741) had ledger ordinal 4 of round 10, the rank-1..3 graduates of that round are 1.0–2.0 | SUPPORTED | none |
| H6 | The screen keys on claim boilerplate (F27-type features), not the weight-5 inventive features | t14 feature split unchanged (F27 0.74 vs 0.15). Caveat: the "reject" side of that split was 31/37 add_failed docs, so the comparison is graduates vs *unscreened* docs, not graduates vs judged rejects | SCOPED (t14, needs re-computation against the 7 true rejects only — too few for a rate) | recompute on t11/t13 when they answer; no opus reads needed |
| H7 | **NEW.** The reject bucket's opus champions are STAGING FAILURES (`nlm_screen_state='add_failed'` = source never indexed in NotebookLM), not screen-judgement misses; add_failed is a per-round systemic loss of ~30–45 % of every batch | t14 this run: 78 screened = 34 grad + 7 rejected + **37 add_failed**; all 6 non-graduates with opus ≥4 (CN107431369, US5686815, EP3930140, DE10158062, CN105723559, US11397216) and 8 of the 11 ≥3 are add_failed; true rejects: 0/7 ≥4, 3/7 at 3 (KR100221047, WO2023085906, CN119487405). t10: every round loses 11–18 of 39 (168/468 = 36 %); t12 round 1 15/39, round 2 attempt 32/32 (marked 17:44, being re-staged now). add_failed size ≠ >120 KB only (t10 38/168 over 120 KB, median 70 KB) → not the multi-part path alone; whole-round clusters point at ingestion timeout / index-probe misses | SUPPORTED (t14 exact counts; t10/t12 structural counts) | t12: opus-read the 32 add_failed docs that nobody has scored (launched 21:49, ≤60 cap) → does staging loss hide champions on a second tab? If t12's re-stage of round 2 succeeds, the same 32 give a direct screen-vs-opus comparison |
| H8 | **NEW.** The t10 ground-truth set (12 "opus ≥4" docs from the 08-21 calibration) is partly STALE: 5 of the 12 re-read blind today score 1–2 under the current (08-18) feature set | EP3005248 1.0, EP2417690 1.0, US20090108997 2.0, US10027187 2.0, US9831029 1.0 (all opus-5, 20:49–21:04, detailed feature-check: 0 YES). Their earlier ≥4 verdicts predate or coincide with the 08-18 08:13 benchmark feature update (EP3970350/JP2019221076/US20070021140/CN113924787 = 07-28; US20230337972 = 07-28). The "GT-recall 9/12" line therefore mixes valid and stale controls | SUPPORTED for 5/12; remaining 7 unverified | **needs user approval** (overwrites registered GT verdicts): blind re-read of the 7 remaining GT docs + US20230337972 (07-28 6.0) = 8 reads. Not launched by the driver |

## What we now believe (cycle 1, 21:50 UTC)

The dominant defect is not NotebookLM's judgement but the staging step: on every tab a
third to a half of each 39-doc round is marked `add_failed` (source never indexed) and
silently dropped — those docs are never questioned, never re-queued, and were counted as
"rejects" by the 20:44 experiment. Re-partitioning t14 by that flag: the screen rejected
only 7 judged docs, none of which opus scores ≥4, while all six ≥4 "misses" sit in the
add_failed pool. So the screen's judged recall on t14 is 4+/4+ at ≥4 among what it actually
saw (tiny denominator, pre-audit), and the pipeline's recall is capped by the staging
success rate (~55–65 %). H1's "old non-CJK" profile was an artefact of which docs failed
to stage. On t10/t12 the over-clip slices are noise for both graduates and rejects
(H2). Separately, the t10 ground truth is partly stale under the 08-18 features (H8),
which weakens the banked "9/12 GT-recall" line until the GT set is re-verified.

## Proposed improvements (not implemented — user decides)

1. **Re-queue add_failed docs** (H7): the screen should append add_failed ids back to the
   queue (or a retry pass at the end) instead of marking them terminal; the audit gate
   matrix should treat `add_failed` as F3c-class "not staged" (blind doc), not as screened.
2. **Diagnose the loss mechanism**: log per-part add results + the strict index probe
   result for each failed doc; check whether `wait_sources_ready` timeout or
   `_notebook_source_index(strict=True)` title-key mismatch drives the whole-round
   clusters (11–18/39 every round on t10).
3. **GT hygiene** (H8): after any benchmark feature update, re-read the registered
   controls before using them as canaries; keep a score history (the wave-1 read
   overwrote the 4.0 verdicts with no trace in the DB).
4. Recall lines must state the denominator: "X/Y among docs that reached NotebookLM" and
   "Z docs add_failed (unassessed)" separately.

## Read budget discipline
Every launched read names the hypothesis it serves and the expected information gain;
stop a line when a batch changes no status (marginal ≈ 0). Ledger:
docs/experiments/read_ledger.jsonl.
