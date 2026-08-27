# Hypothesis register — NLM v2 screen viability (owner: hypothesis-driver agent)

Status: OPEN · SUPPORTED · REFUTED · SCOPED (true only under stated scope) · UNRESOLVABLE
(named evidence would settle it) · PARKED

Last update: **2026-08-27 cycle 6 — FINAL CLOSING CYCLE** (16:15–17:10 UTC; ANALYSIS-ONLY:
user paused all deep-compare reads at 16:10, the graduate driver is stopped
(`/data/.opus_grad_driver.stop`), the three watchdog resume files are parked
`.PAUSED-BY-USER`, three NLM restage runners (t11/t13 default, t14 work2) are live and were
NOT touched. **0 opus reads, 0 NLM jobs launched this cycle.**) Every hypothesis below is
now terminal. Closing thesis: `docs/experiments/thesis_2026-08-27.md`.

All conclusions PRE-AUDIT in the sense of the gate matrix: the three auditors ran at
07:05–07:12 UTC today on deploy head 09f61a9 and all three returned worst = FAIL
(recall R1 t13 7/21 + R2 corridor everywhere; staging S1 blind tails 211→ now t11 30 / t13 53 /
t14 10 and S5 add_failed 119 over a registered baseline of 13; ranking C1 t13 330 orphans,
C6 t3). No pipeline-integrity-supervisor pass has gated the numbers below, and the
screen-completion evidence (five finished screens) is invisible to the deterministic
recall backend because **the five finished screens wrote ZERO `nlm_claims` rows** (open
F7-class control gap). Read every recall line with its stated denominator.

Scope conventions (unchanged): "v2-screened" = `nlm_screened_at ≥ 1787511600` (2026-08-23
19:00 UTC, the full multi-part staging re-screen); earlier stamps are v1 (truncated staging)
and are NOT evidence about v2. `add_failed` = never indexed in NotebookLM (F3c-ns).
opus-read ⇔ `score_model LIKE '%opus%' AND score IS NOT NULL`. Champion = opus ≥ 4.0.
Graduate "ordinal" = `ledger[key][0]` in `/data/.nlm_screen_{t}.json`.

**Terminal state of the five screens (all `step=done`, cursor = queue length):**
t10 r56 1459/1459 · t11 r15 188/188 · t12 r31 363/363 · t13 r34 458/458 · t14 r23 337/337.

**v2 accounting at close (DB, 2026-08-27 16:18 UTC):**

| tab | v2 screened | graduate | rejected | add_failed | grad % | v2 champions grad/rej/add_failed | rejects opus-read | graduates opus-read |
|---|---|---|---|---|---|---|---|---|
| t10 | 1291 | 509 | 767 | 15 | 39 % | 7 / 2 / 0 | 169/767 | 488/509 |
| t11 | 188 | 89 | 72 | 27 | 47 % | 2 / 0 / 1 | 21/72 | 89/89 |
| t12 | 319 | 260 | 33 | 26 | 82 % | 0 / 0 / 0 | **33/33** | **260/260** |
| t13 | 458 | 368 | 66 | 24 | 80 % | 3 / 0 / 2 | **66/66** | 317/368 |
| t14 | 300 | 207 | 66 | 27 | 69 % | 19 / 1 / 7 | 10/66 | 136/207 |
| **all** | **2556** | **1433** | **1004** | **119** | **56 %** | **31 / 3 / 10** | 299/1004 | 1290/1433 |

| id | hypothesis | evidence at close | final status |
|---|---|---|---|
| H1 | The screen's miss class = old (pre-~2008), non-CJK disclosures lacking a modern claim skeleton | t10 30/30 profile-matched non-graduates ≤2. The two t14 profile "misses" (US5686815, DE10158062) are `add_failed`, i.e. F3c-ns not judgment. The first judged v2 miss (EP3849091, 2021 EP, modern skeleton) contradicts the profile. **New, decisive:** today's t14 restage interrogated 118 previously-blind tails in FULL — a pool well stocked with exactly this class (37 of 118 non-CJK — DE/US/EP/WO/CA/AU; of the 52 whose number prefix carries a parsable year, 12 are pre-2008, oldest AU9338298 1993) — and **117 of 118 disclose no weight-4/5 feature at all**; the single exception (JP2020119712, one w5 YES) is a graduate. The "old non-CJK" pool is champion-free, not a hidden miss class | **REFUTED** |
| H2 | t10/t12 over-clip subsets contain no unread champions | t10: v2 rejects 169/767 opus-read → 2 ≥4 (EP3849091 4.0, CA2552849 4.0); v2 graduates 488/509 → 7 ≥4 ⇒ champions do sit in the t10 reject pool. t12: **the entire v2 scope is now opus-read** — 260/260 graduates, 33/33 rejects, 26/26 add_failed → **0 ≥4 anywhere**, max 3.0 | **SCOPED / CLOSED**: REFUTED on t10, SUPPORTED on t12 (measured, not sampled) |
| H3 | Stage-2 citation follow-up ≈ opus on graduates | Ledger `/data/audits/followup_ledger.jsonl` 25 rows. Calibration round 07:05 08-27 on the two t10 R6 GT docs (both opus 4.0): **CN103683526 — follow-up corroborates opus feature-for-feature** (agrees F1/F2/F3/F4, gives the same technical reason for the microwave NOs: inductive coupling, not radiated) and is one notch stronger on F4. **US20120007441 — follow-up returned NO on the whole microwave trio F7/F8/F9 (weights 4/5/5), a false negative verified against source text** (opus's [0172] citation found verbatim in the stored description at offset ~136 384). Agreement is therefore conditional on question wording (see H16). Instrument artefacts logged: NLM silently RENUMBERS the checklist (a stable permutation of the 11 MUST features) and returns SOURCE ORDINALS, not document numbers, in broad rounds | **SUPPORTED, SCOPED to wording**: follow-up ≈ opus when the feature wording is synonym-tolerant; it is not a safe NO-instrument otherwise |
| H4 | The t14 judged-miss rate generalises to t11/t13 (default account) | Judged-miss rate on opus-read v2 rejects: **t13 0/66 (pool complete)** · **t12 0/33 (pool complete)** · t11 0/21 of 72 · t10 2/169 of 767 · t14 1/10 of 66 (CN115514036 4.0, rejected 08-27 03:48). Batch A′ (76 unscored v2 rejects, launched 08-26 21:40 by the parent) landed 68 verdicts: 0 ≥4, 3 at 3 | **SCOPED**: 0 judged misses on the two tabs whose reject pools are exhaustively read (t12, t13); non-zero on t10 (1.2 %) and t14; t11 (51/72 unread) and t10/t14's unread remainder are the named residual |
| H5 | NLM graduation rank carries no relevance ordering | Pooled at close, opus-read graduates n = 1403 keyed by ledger ordinal: **ordinal 1–3: 10 ≥4 of 26 (38.5 %)** · 4–10: 3 of 49 (6.1 %) · **11+: 28 of 1328 (2.1 %)** — an 18× enrichment at the head. But **28 of 41 champions sit at ordinal 11+** | **REFUTED as stated** (rank does carry ordering); **SCOPED corollary**: rank is a triage order, never a recall cut-off |
| H6 | The screen keys on claim boilerplate, not on the weight-5 features | **Decisive test found in today's restage** (n = 118 t14 tails, independent full-text NLM interrogation of all 22 MUST features, graduates 66 vs rejects 52). Per-feature YES/PARTIAL-rate difference graduate−reject: **F22 Battery (w1) +34.3 pts · F18 charging device (w2) +21.1 · F20 method category (w1) +20.4 · F17 (w3) +20.0 · F21 system category (w1) +14.6 · F14/F15/F16 CC-CV boilerplate (w3) +10.6…+12.1 · every weight-4 feature +0.0 · every weight-5 feature +0.0/+1.5.** Aggregate weighted yield 3.76 (grad) vs 1.03 (rej), permutation p = 0.00005 | **SUPPORTED (revised)**: the screen separates documents on topical/category boilerplate. Scope caveat: this pool contains almost no heavy-feature disclosures, so the test proves what the screen keys ON, not that it would ignore a heavy feature if present |
| H7 | Reject-bucket champions are STAGING failures (`add_failed`), a per-round systemic loss | Mechanism (H7b, 50-source-cap overflow) confirmed; cap-aware re-queue DEPLOYED (911d1b3). Remedy now verified on two tabs: **t10 add_failed 170 → 15**, and all three formerly-unstaged t10 champions were re-screened — US20220221016 (5.0) GRADUATED, US10996236 (4.0) GRADUATED, CA2552849 (4.0) REJECTED; t12 13/13 tail copies recovered. Residual: **119 add_failed corpus-wide (t10 15 · t11 27 · t12 26 · t13 24 · t14 27) still holding 10 champions** (t14 7, t13 2 incl. the GT canary CN120433348, t11 1) — and t13 grew 13 → 24 over its registered baseline | **SUPPORTED**; remedy effective but incomplete — F3c-ns is still the single largest champion sink in v2 (10 of 44 v2 champions) |
| H8 | Stale opus verdicts (scored under pre-update features) are unreliable | Unchanged: 10/114 stale 3.0 verdicts crossed to ≥4 on blind re-read; 8 of 12 t10 GT controls were invalid under the 08-18 feature set (valid GT = 4). The 4 valid t10 GT controls all graduated in the 03:54 08-27 round → **t10 registered-control recall 4/4** | **SUPPORTED** |
| H9 | t10's add_failed champions (6 docs, 08-17 verdicts) are stale too | 4/6 valid on blind re-read | **REFUTED, CLOSED** |
| H10 | The v2 screen has a non-zero JUDGED-miss rate (rejects a champion it actually saw) | **v2 judged misses = 3 of 299 opus-read v2 rejects (1.0 %)**: t10 EP3849091 (4.0, rejected 08-25 18:33), t10 CA2552849 (4.0, rejected 08-26 21:48), t14 CN115514036 (4.0, rejected 08-27 03:48). **v2 judged recall 31/34 = 91.2 %.** Including the staging sink: **end-to-end v2 champion recall 31/44 = 70.5 %.** The corpus-wide auditor figure (147/189 = 78 %, 42 rejected champions) is dominated by the v1 truncated screen: **39 of the 42 rejected champions carry v1 timestamps** (t11 25 · t12 6 incl. the registered control KR20260033205 8.0 · t13 3 · t14 5 · t10 0) and **none of the 39 was ever re-examined by the v2 re-screen** | **SUPPORTED** (rate non-zero and now measured on complete pools for t12/t13) |
| H11 | The v2 re-screen scope excludes champions, and the scope rule is not "over-clip / multi-part" | Measured on t10: in-scope 1291 docs, mean 101 795 chars, 33.3 % over 118 kB; **out-of-scope 758 docs, mean 100 896 chars, 33.0 % over 118 kB** — the two populations are indistinguishable in size, so the queue is NOT the "over-clip" set. The out-of-scope cohort holds **4 opus champions** (US20180351412, US11922243, US20120007441, CN103683526, all `nlm_screen_state IS NULL`), and no v1-rejected champion on any tab was re-queued into v2 | **SUPPORTED**: the scope both excludes champions and is not the stated rule; every "the screen saw everything" line must name the v2 denominator |
| H12 | The `nlm_bridge` global lock serialises concurrent screens | Round cadence measured from `nlm_screened_at` round clusters (v2 epoch, gaps > 1 h dropped): **1 screen active — median 788 s (n=10) · 2 — 964 s (n=37) · 3 — 1410 s (n=68) · 4 — 1536 s (n=9) · 5 — 1516 s (n=5)**. A strict global lock predicts ≈ 3 × 788 = 2364 s at three concurrent screens; observed 1.8 × | **REFUTED as stated / SCOPED**: contention is real and sub-linear, not strict serialisation. Residual (would settle it exactly): per-round lock-acquire/release timestamps in the app log, which do not survive the Docker restarts |
| H13 | Within a round the graduate set is unordered and of near-fixed size, so a lone champion in a weak round is dropped | Graduate counts per round vary 4–23 while max stored score stays ≤1 ⇒ the count is neither fixed nor quality-driven. The ordering half is superseded by H6 (graduation tracks topical boilerplate) and quantified by H5 (top-3 enrichment 18×) | **REFUTED** (fixed-count mechanism); ordering half **absorbed into H5/H6** |
| H14 | Rejection rate is tab-dependent; on t12/t13 the screen forwards almost everything, so stage-1 saves little opus budget | Final graduation rates: **t10 39 % · t11 47 % · t12 82 % · t13 80 % · t14 69 %**; pooled v2 1433/2556 = 56 %. The screen therefore removes 39 % of the corpus from the read queue pooled (10 % on t12, 14 % on t13) — while consuming three NLM accounts for four days | **SUPPORTED** |
| H15 | t10's screen became looser after the 14:35 08-26 Docker restart (graduation rate doubled) | Rate change confirmed: pre-restart 185 graduates / 662 screened = 0.28; post-restart 324 / 614 = 0.53. **But the loose regime did not cost recall — it delivered 6 of t10's 7 v2 graduate champions**, including all 4 valid GT canaries (EP3970350, JP2019221076, US20070021140, CN113924787, one round at 03:54:04 08-27) and 2 of the 3 re-queued add_failed champions. The rate change is confounded with queue composition: the post-restart segment IS the re-queued add_failed/GT-rich tail | **SCOPED**: rate change real, recall cost zero, cause not isolated (per-round rosters and NLM answers are not persisted — improvement 8) |
| H16 | **NEW (cycle 6).** The instrument's recall floor is set by BENCHMARK VOCABULARY, not by document content: a feature phrased with a narrow genus term produces false NOs on documents that disclose the genus in other words | Controlled A/B, 07:12 08-27, document and features held constant, only wording changed: US20120007441 F7/F8/F9 **NO/NO/NO → YES/YES/YES** when "using a microwave" was widened to "any beamed/radiated RF wireless power transfer"; for F9 NLM returned the exact sentence opus had cited. Second, independent artefact from today's restage: in the 10-doc batch `t14_1787834464`, NLM scored F1/F2/F3 = YES on JP2021009830 by matching the *checklist labels* to literal tokens in the document text (`"检测电池状态的功能F1"`) — label collision, not disclosure. Both mechanisms sit upstream of every NO the screen, the sweep and the follow-up stage produce | **SUPPORTED** (n = 1 controlled flip + 1 label-collision class); registered as a new failure class candidate **F3f — feature-vocabulary false negative** |

## What we now believe (cycle 6 close, 2026-08-27 ~17:00 UTC)

The NLM screen is a **topicality filter with an 18× head enrichment and a ~1 % judged-miss
rate, whose real cost is not judgment but plumbing.** Three independent measurements now
say the same thing. (1) Judgment: across the 2556 v2-screened documents the screen kept 31
of the 34 champions it actually judged (91.2 %); the three it dropped are EP3849091,
CA2552849 (t10) and CN115514036 (t14), and on the two tabs where the reject pool is
*exhaustively* opus-read — t12 33/33 and t13 66/66 — it dropped **zero**. (2) Mechanism:
the only clean, unconfounded head-to-head we have (118 t14 blind tails, restaged in full
today and interrogated feature-by-feature by NotebookLM itself) shows graduates carry 3.6×
the weighted feature yield of rejects (3.76 vs 1.03, permutation p = 5e-5) — but the entire
separation is carried by weight-1…3 category features (Battery +34 pts, charging device
+21, method/system categories +20/+15, CC-CV boilerplate +11) and **not one weight-4 or
weight-5 feature contributes anything**. The screen sorts by subject matter, and it finds
champions because champions are on-subject. (3) Cost: of the 44 champions inside the v2
scope, 10 were lost before any judgment happened — they were never indexed in NotebookLM at
all (`add_failed`, 119 docs corpus-wide, still growing on t13) — so end-to-end v2 champion
recall is 31/44 = **70.5 %**, and the biggest single remedy is plumbing, not prompting. Two
further findings bound how much any NLM-only phase can be trusted: the v2 re-screen scope is
**not** the "over-clip" set it was described as (t10 in-scope and out-of-scope docs have
identical size distributions, and 4 t10 champions sit outside the queue entirely), and **no
v1-rejected champion — including t12's registered control KR20260033205, opus 8.0 — was
ever re-examined by v2**, so the 39 v1 rejections in the auditor's "42 rejected champions"
line are unretested, not overturned. Finally, the cheapest and most disturbing result of
the whole study is H16: with the document and the feature list held constant, rewording one
feature turned NO/NO/NO into YES/YES/YES on the two highest-weight features of the t10
checklist, with NLM quoting the exact sentence opus had cited. Every negative this pipeline
has ever produced is conditional on benchmark vocabulary. Marginal yield of further opus
reads is now measured and low: **568 opus reads landed since 21:00 08-26 produced 2
champions (0.35 %)**, and the 68 batch-A′ reject reads produced none — which is why the
read lines were stopped before the user's pause, and why the register closes here.

## Proposed improvements (not implemented — user decides)

1. **Synonym/glossary line per term-of-art feature (F3f, new, highest value).** Emit, with
   every MUST feature, an explicit genus expansion ("microwave = any beamed/radiated RF
   power transfer"), and re-ask any NO on a weight-4/5 feature once with the expansion
   before recording it. Evidence: H16's controlled flip. Cheap, free (NLM), and it attacks
   the recall floor of every lane at once.
2. **Never let a checklist label collide with document text.** Number features `Q1…Qn`
   or use hashes, not `F1…Fn` (evidence: JP2021009830's literal `功能F1`). Parse answers by
   *semantics*, never by position — NLM renumbers the checklist (t10 R6 round, stable
   permutation) and returns source ordinals, not document numbers.
3. **Close F3c-ns before spending another opus token.** 119 `add_failed` docs hold 10 of the
   44 v2 champions. Re-queue is deployed and works (t10 170→15, t12 13/13); finish it on
   t11/t12/t13/t14 and add a hard post-round assertion "every roster doc has a terminal
   state that is not `add_failed`".
4. **Re-screen the v1-rejected champions.** 39 champions were rejected by the truncated v1
   screen and never re-tested — including a registered control at opus 8.0. Any claim that
   v2 fixed truncation is untested until these 39 run through v2.
5. **State the v2 scope rule, or fix it.** The queue is not the over-clip set (H11);
   758 t10 docs with the same size profile — and 4 champions — were never screened.
6. **Persist per-round rosters and NLM answers** (`nlm_query_cache` holds no screen answers)
   so judged misses can be replayed instead of inferred from `nlm_screened_at` clusters, and
   so H15-class regime changes can be attributed.
7. **Make screen completion visible to the deterministic backend.** Five finished screens
   wrote zero `nlm_claims` rows; every audit still measures the legacy sweep (F7 gap).
8. **Rank use (H5)**: surface the graduation ordinal as a triage order (top-3 first) — never
   as a cut-off; 28 of 41 champions sit at ordinal 11+.
9. **Verdict hygiene (H8)**: re-read or tag every opus verdict after a benchmark feature
   update; keep a score-history table.
10. **Concurrency (H12)**: three concurrent screens cost 1.8× per round, not 3× — running
    three accounts in parallel is worth it; a per-account bridge lock would recover part of
    the remaining 0.8×.

## Needs user approval (not launched; listed for completeness)
- t11 51 unread v2 rejects + t14 56 unread v2 rejects (107 reads) — the only remaining
  measurement that would turn H4/H10's judged-miss rate from "measured on 2 tabs, sampled on
  3" into "measured on 4". Prior is low (0/33 and 0/66 on the two completed pools).
- t10 598 unread v2 rejects — NOT recommended (sonnet ≤1 on the bulk; a seeded 40-doc sample
  would bound the sonnet-≤1 → opus-≥4 rate under ~7 %).
- Re-screen of the 39 v1-rejected champions (improvement 4) — NLM work, no opus reads.

## Read budget discipline
Cycle 6: **0 reads launched, 0 NLM jobs.** Ledger `docs/experiments/read_ledger.jsonl` gains
three outcome/reconciliation rows (standing graduate driver, batch A′, t14 restage). Cumulative
ledger n_reads = 2036 launched; DB holds **4156 opus-scored documents** across t10–t14
(t10 1430 · t11 604 · t12 639 · t13 996 · t14 487). Measured character cost where recorded:
97.8 M chars ≈ 24.5 M input tokens (partial coverage — the standing-driver and batch-A′ jobs
were not char-measured; at the cycle-4 proxy of ~25 k tokens/read the 568 reads since 08-26
21:00 cost ≈ 14 M tokens). Marginal yield of the last 568 reads: 2 champions (0.35 %).
