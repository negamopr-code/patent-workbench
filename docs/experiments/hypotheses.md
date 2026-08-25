# Hypothesis register — NLM v2 screen viability (owner: hypothesis-driver agent)

Status: OPEN · SUPPORTED · REFUTED · SCOPED (true only under stated scope) · PARKED

Last update: 2026-08-25 cycle 2 (closed 06:30 UTC). All conclusions PRE-AUDIT until the
pipeline-integrity-supervisor + recall/ranking auditors gate them (supervisor running in
parallel this session). Cycle-2 reads: 52 (cap 60), all landed 05:59–06:06 UTC.

Scope conventions: "v2-screened" = `nlm_screened_at ≥ 2026-08-23 19:00` (full multi-part
staging re-screen, over-clip scope); states with earlier dates belong to the v1 (truncated)
screen and are NOT evidence about v2. "add_failed" = never indexed in NotebookLM (H7).
opus-read ⇔ `score_model LIKE '%opus%' AND score IS NOT NULL`. Verdicts dated before
2026-08-18 08:13 (t10 benchmark feature update) are "pre-update" (H8/H9).

| id | hypothesis | evidence | status | next test |
|---|---|---|---|---|
| H1 | The screen's miss class = old (pre-~2008), non-CJK disclosures lacking a modern claim skeleton | t10: 30/30 profile-matched non-graduates ≤2. t14: the 2 profile "misses" (US5686815, DE10158062) are add_failed | REFUTED on t10; confounded by H7 on t14 | none (closed cycle 1) |
| H2 | t10/t12 over-clip subsets contain no unread champions | t10: 96/216 v2 rejects read, 0 ≥3; 84/84 grads read, 2 at 3, 0 ≥4 post-update. t12: 9/9 v2 rejects 0 ≥3; 15/15 v2 grads 1 at 3; **32/32 add_failed 0 ≥3, max 1.0** (cycle 2 record) | SUPPORTED — t12 all three buckets; t10 scoped to the 376 docs that were opus-read (97 add_failed unread) | none (marginal 0) |
| H3 | Stage-2 citation follow-up ≈ opus on graduates | followup_ledger.jsonl: 3 rows, doc lists only; fu_t14_b1.json "running", answers {} since 08-24 21:44; no per-doc stage-2 verdict journaled | OPEN (blocked on verifier output, not reads) | three-way table when per-doc verdicts exist; 0 reads |
| H4 | The t14 miss rate generalises to t11/t13 (default account) | t11 v2: one answered round (08-23 22:18: 19 grad / 19 rej, 25 add_failed), t13 v2: 0 rounds; default account 0 rounds in >24 h | OPEN (quota-blocked; no reads spent) | at ~150 v2-screened docs per tab |
| H5 | NLM graduation rank carries no relevance ordering | unchanged (only ≥3 t10 graduates sit at ordinal 4+; the 4 valid GT controls ranked 1,2,3,x in round 1 — a counter-signal on n=4) | SUPPORTED (weak counter-signal noted) | none |
| H6 | The screen keys on claim boilerplate (F27) not weight-5 features | t14 split confounded (reject side 31/37 add_failed) | SCOPED (t14, n=7 true rejects) | recompute when t11/t13 answer; 0 reads |
| H7 | Reject-bucket champions are STAGING FAILURES (`add_failed`), a per-round systemic loss | **Loss size:** t10 173/473 v2-screened (37 %; 4–18 per round of 39), t11 40 (25+15 in the two round-1 attempts), t12 47 (15/39 + 32/32), t13 13/13, t14 37/78. **Mechanism = 50-source cap overflow (H7b, cycle 2, analytic):** per round the screen stages 39 new docs + 10 survivors + benchmark; every doc >118 KB is split into parts, so `Σparts + survivors + 1 − 50` slots cannot be added. Predicted overflow vs observed add_failed on t10 rounds 1–12: 4/4, 11/12, 13/15, 17/17, 17/17, 18/14, 18/18, 14/15, 17/15, 19/17, 12/13, 9/11 (ρ ≈ 0.95). On t12/t14 (all 39 docs multi-part) predicted overflow 42/33/47 exceeds add_failed 15/13/24 because part 1 may land while tails are evicted → the auditor's "blind tails" (49/53, 49/52 …). The 60 s second-chance wait in `_screen_stage` (H7a, cycle-2 pre-launch) is secondary. **Champion cost:** t10 5 of 9 current opus-≥4 docs are add_failed (US20230337972 5.0, US20220221016 5.0, US10996236, EP3849091, CA2552849 — all re-read blind 08-25, 4 of 5 single-part); t14 6 of 17; t12 0/47; t11 0/40 (cycle 2: 0/31, 3 at 3); t13 0/13 (cycle 2: 0/7). With population ≥4 rates t10 0.4 %, t11 4.8 %, t12 0.8 %, t13 0.7 %, t14 3.2 %, the observed champion losses are consistent with **value-neutral (random) eviction at the per-round loss fraction** — no tab shows a champion-biased or champion-sparing loss | SUPPORTED (mechanism H7b + value-neutral cost); champion cost SCOPED per tab (t10 5/9, t14 6/17, t11/t12/t13 0) | none within cap. t10 97 unread add_failed: expected ≥4 ≈ 0–1 at population rate → "needs user approval" (below) |
| H8 | The t10 GT set (12 "opus ≥4", 08-21 calibration) is partly STALE under the 08-18 features | cycle 2 blind re-read (8 reads, user-approved, snapshot gt_snapshot_2026-08-25.json): EP3970350 4, JP2019221076 4, US20070021140 4, CN113924787 4 **hold**; WO2020026413 4→3; US20200021142 1, CN106104969 1 (sonnet 1.0 before, never opus ≥4); US20230337972 6→5 (not in the 12). With cycle 1: EP3005248 1, EP2417690 1, US20090108997 2, US10027187 2, US9831029 1. **Valid GT = 4/12.** Recomputed GT-recall: 4/4 among valid controls that reached NLM (ranks 1, 2, 3, ≤10 in round 1); adding the current-≥4 champion US20230337972 (add_failed): 4/5 = 80 % pipeline, miss = staging | SUPPORTED (8/12 controls invalid) | none; GT set must be re-registered as {EP3970350, JP2019221076, US20070021140, CN113924787} + champions {US20230337972, US20220221016, US10996236, EP3849091, CA2552849} |
| H9 | t10's add_failed champions (6 docs, 08-17 verdicts) are stale too | cycle 2: US20220221016 6→5, US10996236 4, EP3849091 4, CA2552849 4 hold; TW201717523 4→3, CN113287245 4→3 | REFUTED (4/6 valid) → t10's staging champion loss is real | none |
| H10 | **NEW (cycle 2, from recall-integrity audit).** The v2 screen has a non-zero JUDGED-miss rate (rejects an opus champion it actually saw) | Audit's judged misses are all v1-screen states: t11 CN223245862/CN115051084/CN220420731/CN223471682 screened 08-07/08-08; t12 KR20260033205/CN119833811 screened 08-06; t13 CN114690685/CN101639686 08-08/08-09 (truncated staging, NO-GO'd). v2-screened rejects opus-read with ≥4: t10 0/96, t11 0/2, t12 0/9, t14 0/7 = **0/114**; ≥3: t10 0, t14 3/7 | SCOPED: 0/114 on v2 (pre-audit); the v1 judged-miss class is real but out of v2 scope | t11/t13 v2 rejects when quota returns (H4) |

## What we now believe (cycle 2 close, 06:30 UTC)

The pipeline's dominant defect on the v2 re-screen is deterministic: NotebookLM caps a
notebook at 50 sources, the screen fills 39 new docs + 10 survivors + benchmark, and every
multi-part doc consumes extra slots — so `Σparts − 39` docs (or tails) per round are
evicted and marked `add_failed`, never re-queued. The per-round prediction reproduces
t10's 12 observed add_failed counts almost exactly. The eviction is value-neutral: it
took 5 of t10's 9 current champions and 6 of t14's 17, but none on t11/t12/t13 where
champions are rarer, in line with each tab's base rate. Among docs that reached NotebookLM
the v2 screen has rejected no opus-≥4 doc so far (0/114 read v2 rejects) and graduated all
4 valid t10 GT controls at ranks 1–3+; the judged misses the recall audit lists are v1
(truncated-staging) verdicts. The t10 GT set was 8/12 invalid under the 08-18 features;
the surviving controls and the five re-verified add_failed champions are the new canary
set. H3/H4/H6 remain blocked on other agents/quota, not on reads; every remaining read
line is at marginal ≈ 0 within the cap → loop ends after this cycle.

## Proposed improvements (not implemented — user decides)

1. **Cap-aware batch fill** (H7b, `_screen_stage` / round builder ~api.py 2348/3703):
   compute `Σparts(roster) + Σparts(survivors) + 1` and shrink the roster until ≤ 50,
   carrying the overflow docs to the next round; never evict a tail part.
2. **Re-queue add_failed** instead of terminal marking; audit gate matrix treats
   `add_failed` as F3c "not staged" (blind doc); recall lines report it as a separate
   denominator.
3. **Second-chance wait** (H7a): give re-added sources the full `PIPELINE_INGEST_TIMEOUT`,
   seed `known_ready` from the post-re-add index, log `(wanted, missing-1st, missing-2nd)`.
4. **GT hygiene** (H8/H9): re-read registered controls after any feature update; keep a
   score history table (the 08-24 read overwrote the 4.0 verdicts without trace);
   re-register the t10 canary set as listed in H8.
5. **Audit partitioning:** audit_staging.py / audit_recall.py must partition on
   `nlm_screen_state` and on the v1/v2 epoch (`nlm_screened_at`), otherwise v1 judged
   misses are charged to v2.

## Needs user approval (over cap or low prior)
- t10: 97 unread add_failed docs (mean 83 k chars ≈ 8.1 M chars): expected ≥4 ≈ 0–1 at
  the population rate, but H9 showed the read part of that pool holds 5 champions
  (lane-selected, biased). Decisive only for the exact t10 reject-miss count.

## Read budget discipline
Every launched read names the hypothesis it serves and the expected information gain;
stop a line when a batch changes no status (marginal ≈ 0). Ledger:
docs/experiments/read_ledger.jsonl (9 jobs, 423 reads, proxy ≈ 45.0 M chars ≈ 11.2 M
input tokens; the bridge logs no usage).
