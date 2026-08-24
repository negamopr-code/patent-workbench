# Opus-parallel blind read vs NLM v2 full-staging screen — observer report

Experiment `opus_parallel_2026-08-24` (manifest `docs/experiments/opus_parallel_2026-08-24.json`,
seed 20260824, reading model claude-opus-5, blind deep_map: benchmark + features + doc text only).
Started 20:44 UTC, all 254 reads landed by 21:06 UTC (t12 38/38 ≈20:52, t14 59/59 ≈21:02,
t10 157/157 ≈21:06). Snapshot cursors: t10 351 / t12 39 / t14 78.
Opus jobs never stalled (lock age always < 60 s). Observer never wrote the DB or resumed a lane.

**Gate notice:** this is an observer's read-out. The conclusion is NOT final until
`recall-integrity-auditor` and `ranking-integrity-auditor` (via `pipeline-integrity-supervisor`)
have gated it and the baselines are recorded in `docs/failure-registry.md`.

## 1. Confusion matrices (NLM bucket at snapshot × blind opus verdict)

Champion threshold = opus ≥ 4 (t12 ladder convention); a secondary ≥ 3 cut is shown because
on t10/t12 nothing reaches 4 in either bucket.

| tab | thr | NLM-grad & opus≥thr | NLM-grad & opus<thr | NLM-rej & opus≥thr | NLM-rej & opus<thr | precision | reject-sample miss rate (Wilson 95%) |
|---|---|---|---|---|---|---|---|
| t10 | ≥4 | 0 | 57 | 0 | 100 | 0/57 = 0.00 | 0/100 = 0.000 [0.000, 0.037] |
| t10 | ≥3 | 1 | 56 | 0 | 100 | 1/57 = 0.02 | 0/100 = 0.000 [0.000, 0.037] |
| t12 | ≥4 | 0 | 15 | 0 | 23 | 0/15 = 0.00 | 0/23 = 0.000 [0.000, 0.143] |
| t12 | ≥3 | 1 | 14 | 0 | 23 | 1/15 = 0.07 | 0/23 = 0.000 [0.000, 0.143] |
| t14 | ≥4 | 4 | 18 | 2 | 35 | 4/22 = 0.18 | 2/37 = 0.054 [0.015, 0.177] |
| t14 | ≥3 | 16 | 6 | 11 | 26 | 16/22 = 0.73 | 11/37 = 0.297 [0.175, 0.458] |

Score distributions (mean opus): t10 grads 1.32 (0:3, 1:34, 2:19, 3:1) vs rejects 1.18
(0:7, 1:68, 2:25) · t12 grads 1.07 (0:4, 1:7, 2:3, 3:1) vs rejects 0.65 (0:10, 1:11, 2:2) ·
t14 grads 2.86 (1:1, 2:5, 3:12, 4:4) vs rejects 2.14 (0:3, 1:2, 2:21, 3:9, 4:2).

## 2. Implied corpus-level recall (fresh graduates vs full reject pool)

Rejects in pool at snapshot: t10 289, t12 24, t14 44. Estimated missed = pool × sample miss
rate; recall ≈ grad-hits / (grad-hits + missed), bounds from the Wilson CI of the miss rate.

| tab | thr | grad hits | est. missed in pool | recall estimate [95% band] |
|---|---|---|---|---|
| t10 | ≥4 | 0 | 0.0 (≤ 10.7) | undefined — no champions on either side |
| t10 | ≥3 | 1 | 0.0 (≤ 10.7) | 1.00 [0.09, 1.00] — one doc, uninformative |
| t12 | ≥4 | 0 | 0.0 (≤ 3.4) | undefined |
| t12 | ≥3 | 1 | 0.0 (≤ 3.4) | 1.00 [0.23, 1.00] — one doc, uninformative |
| t14 | ≥4 | 4 | 2.4 (0.7–7.8) | **0.63 [0.34, 0.86]** |
| t14 | ≥3 | 16 | 13.1 (7.7–20.2) | **0.55 [0.44, 0.68]** |

t14 with the 12 graduates that were already opus-read before the snapshot (skipped by the
manifest; opus 4.0 ×5: WO2012127775, EP3968486, JP2018147827, US10446885, CN113646652;
3.0 ×3; 2.0 ×1) plus the 10 live survivors (CN103457003 = 6.0, WO2022030912 / WO2024029839
= 4.0): full-graduate ≥4 count is 4 + 5 + 3 = 12 of 44 graduates (0.27) — but those 12
pre-read docs were chosen by earlier funnels (selection bias), so the unbiased precision
number is the fresh-graduate row (0.18 at ≥4, 0.73 at ≥3).

## 3. Champions found

Among NLM REJECTS (misses):
- t14 **US5686815** (opus 4.0; YES on F1, F2, F3, F4, F9 voltage acquisition, F17/F18 charge
  sections, F22–F26) — 1997 US patent.
- t14 **DE10158062** (opus 4.0; YES on F1, F4, F9, F17, F18, F22–F26) — 2003 DE.
- t14 opus 3.0 rejects: CN110854972, CN113193579, JP2019509593, CN104662770, KR100221047,
  CN119487405, JP2022044881, + 2 more (11 of 37 sampled ≥3).
- t10 / t12: none (max reject score 2).

Among NLM GRADUATES:
- t14 opus 4.0: CN109073712 (NLM rank 17, round 1), WO2022030912 (rank 3, r2), CN110199452
  (rank 5, r2), US20250096336 (rank 11, r1).
- t12: KR20250094125 (opus 3.0, NLM rank 1 round 1) — the only ≥3 in the tab slice.
- t10: US12212161 (opus 3.0, NLM rank 7 round 11) — the only ≥3 in the slice.

NLM over-inclusion (graduate & opus ≤ 1): t10 37/57 (65%), t12 11/15 (73%), t14 1/22
(CA3217299, rank 16). On t10 the ≤1 graduates span ranks 6–20 and rounds 1–11 with no
pattern — every round's top-N is being filled with noise because the batch has nothing better.

## 4. Feature-level patterns

- **t10 and t12 are no-signal slices**: no feature has opus YES in more than ~13% of either
  bucket; the fresh graduates are indistinguishable from rejects (t10 1.32 vs 1.18). The
  screen's "graduate" label here means "least bad of 39", not "relevant". Both tabs' real
  champions (t10 US20230337972/US20220221016 = 6.0; t12 KR20260033205 = 8.0 etc.) were
  found in earlier rounds/chains, i.e. before the snapshot.
- **t14 (the only tab with material)**: NLM keys on claim-category boilerplate. YES frequency
  graduates vs rejects: F27 computer-readable-medium claim 0.74 vs 0.15, F26 apparatus 0.74
  vs 0.47, F1 0.58 vs 0.35, F2/F3 processor+memory 0.95 vs 0.68/0.56. The heavy discriminating
  features (F5, F7, F11, F12 — weight 5) are YES in ≈0% of BOTH buckets; F9 (weight 4) is
  YES in 11% vs 9%. So the screen separates "looks like the benchmark's claim skeleton" from
  "does not", not "discloses the inventive concept".
- **What NLM misses on t14**: both ≥4 misses are OLD non-CJK documents (1997 US, 2003 DE) with
  the substantive features (F9, F17, F18) but without the modern processor/memory/CRM claim
  skeleton (F27 NO on both). CJK share: rejects 26/37 CN+JP+KR vs graduates 13/22 — CJK is
  over-represented among rejects, but the 3.0-level misses are mixed (CN×4, JP×2, KR, DE, US).
- **NLM rank ≠ opus order inside the graduate set** (t14: 4.0s at ranks 3, 5, 11, 17; rank 2
  = 3.0; rank 16 = 1.0) — consistent with the standing "claimant-list ≠ relevance" finding.

## 5. Caveats (honest)

- Small reject samples on t12 (23) and t14 (37); the t14 ≥4 miss rate CI is 1.5–18%, so the
  corpus-recall band 0.34–0.86 is wide. t10's 100-sample gives a tight bound only because
  there is nothing to find in that slice.
- Live survivors (10/tab) were excluded — they hold the tabs' best material (t14 CN103457003
  6.0), so full-graduate precision is understated relative to what the user sees.
- Graduates already opus-read before the snapshot (t10 5, t14 12) were skipped: t14's
  pre-read set is selection-biased upward.
- Opus text-only reads (drawings not captioned) — scores could move ±1 with figures.
- Canaries: not separately identified in this sample; the t10 GT-recall lane
  (9/12 in round 1, journal 08-24) is a separate measurement.
- No control over batch position: the 39-doc batch composition (roster) determines who
  graduates; "reject" on t10/t12 often means "lost a 39-way competition of noise".

## 6. Wave-2 candidates (screened after the snapshot — NOT read, listed only)

- t10 graduates rounds 10–11 (13, all sonnet-scored 1.0/0.0 so far): CN112055927,
  WO2016045117, US9991741, US9954374, CN110537309, US11271611, US11626756, US12231187,
  US10574096, US11831174, US11707996, WO2014027710, WO2016111903.
- t10 rejects screened after snapshot: 65 (queue positions 351–429 minus graduates/survivors).
- t12, t14: no new screening since the snapshot (lanes stopped, see §7).

## 7. Lane status during the experiment

- t10 NLM lane: healthy the whole time (round 10 → 12, cursor 351 → 429 of 1291, ledger 62 →
  75).
- **t11, t12, t13, t14 lanes: STOPPED before 20:45 with non-quota network errors** —
  t11/t12 "could not list notebook sources" (httpx transport), t13/t14 "Failed to create
  notebook: [Errno -3] Temporary failure in name resolution". `running=false`,
  `resumable=true`, quota=None. The observer did not resume them (outside its remit); the
  in-app watchdog does not cover this error class. They need a manual POST resume.

## 8. Verdict (observer, pre-audit)

The NLM v2 full-staging screen is **not a reliable relevance filter on its own**: where the
slice contains real material (t14) it recovers roughly half to two-thirds of opus-grade
champions (recall ≈ 0.55–0.63, lower band 0.34–0.44) at a fresh-graduate precision of 0.18
(≥4) / 0.73 (≥3), and it misses old, non-boilerplate disclosures (US5686815, DE10158062)
while ranking by claim-skeleton similarity rather than by the weight-5 inventive features.
Where the slice is noise (t10, t12) it still graduates 13–15% of docs, all of which opus scores
≤3 — precision ≈ 0 with no recall loss, i.e. harmless but wasteful. Viability therefore rests on
NLM as a *recall pre-filter with mandatory opus (or opus-lite follow-up) verification of every
graduate*, plus a cheap second lane for the reject pool (lexical/embedding or NLM follow-up on
the reject set) to catch the ~5% ≥4 misses; the screen must not be used as the final ranking.
Subject to auditor gating.
