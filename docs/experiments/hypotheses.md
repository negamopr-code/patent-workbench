# Hypothesis register — NLM v2 screen viability (owner: hypothesis-driver agent)

Status: OPEN · SUPPORTED · REFUTED · SCOPED (true only under stated scope) · PARKED

Last update: 2026-08-26 cycle 5 (19:40–20:10 UTC; ANALYSIS-ONLY — user paused opus reads 16:20 and
revoked the driver's read allowance 16:25: 0 reads, 0 NLM jobs, no mirror sync this cycle).
All conclusions PRE-AUDIT: ranking audit 14:57 08-26 (defa67e, worst FAIL), recall/staging
17:27 08-25 (STALE-by-screen), full-doc 16:51 08-25; the pipeline-integrity-supervisor has
gated nothing below. Cycle-5 inputs: the 176 landed graduate verdicts of job 25 (reconciled
from the DB, see "Read budget"), screen rounds t10 r29→37 (cursor 863→1031), t12 r19→27
(225→332), t13 r23→32 (319→444); t11 r8 113/188 and t14 r3 91/337 still paused. Docker
restarts 14:35 and ~15:42 today; no opus verdict landed after 15:09:18 UTC.

Scope conventions: "v2-screened" = `nlm_screened_at ≥ 2026-08-23 19:00` (full multi-part
staging re-screen, over-clip scope); states with earlier dates belong to the v1 (truncated)
screen and are NOT evidence about v2. "add_failed" = never indexed in NotebookLM (H7).
opus-read ⇔ `score_model LIKE '%opus%' AND score IS NOT NULL`. "Stale" verdict = opus
`scored_at < benchmark.updated_at` of its tab (t10 08-18 08:13, t13 08-17 21:06; t11/t12/t14
have no stale opus verdicts). Graduate "ordinal" = `ledger[number][0]` in
`/data/.nlm_screen_{t}.json` (survivors are re-ranked each round, so a persistent survivor's
ordinal is its latest-round rank). Screen progress at cycle 5: t10 r37 1031/1459 (314 grads, 549 v2 rejects, 170 add_failed),
t11 r8 113/188 paused (86/27/0), t12 r27 332/363 (257/31/31), t13 r32 444/458 (381/63/0),
t14 r3 91/337 paused (47/7/37). Note: t12 add_failed 44→31 (13 tail copies reached, all recovered).

| id | hypothesis | evidence | status | next test |
|---|---|---|---|---|
| H1 | The screen's miss class = old (pre-~2008), non-CJK disclosures lacking a modern claim skeleton | t10: 30/30 profile-matched non-graduates ≤2. t14: the 2 profile "misses" (US5686815, DE10158062) are add_failed. **Cycle 4:** the first v2 judged miss (EP3849091, 2021 EP, modern claim skeleton) does not fit the profile | REFUTED on t10; confounded by H7 on t14 | none (closed cycle 1) |
| H2 | t10/t12 over-clip subsets contain no unread champions | t10: 98/549 v2 rejects read → 1 ≥4 (EP3849091, H10); v2 grads 183 opus-read, 5 ≥4 (4 GT + US20230337972); the 131 remaining t10 v2 grads all carry sonnet ≤1 (job 25 skipped them, skip_scored). t12: 13/31 v2 rejects read 0 ≥3 (18 unscored); **125/257 v2 grads opus-read → 0 ≥4, 2 at 3** (job 25 added 62, all ≤2); 75 unscored + 57 sonnet-only grads remain; add_failed 31 | REFUTED on t10 (as cycle 4); SUPPORTED on t12 within the read part (0 ≥3 in 138 v2 reads) — t12 still champion-free in scope | t12: 75 unscored grads (job 25 remainder 57 + 18 new) + 18 unscored rejects — PROPOSED, needs user approval |
| H3 | Stage-2 citation follow-up ≈ opus on graduates | unchanged: followup_ledger.jsonl still 5 rows (last 06:12 08-25) — NLM accounts occupied by screens all day. Three-way candidates now 8: + **t13 AU2022338850** (job 25, opus 4.0, ordinal 15) and **t13 CN120433348** (the GT canary: reached at cursor ~430, GRADUATED 18:56 08-26 at ordinal 12) | SUPPORTED on 7/7 (small n; pre-audit) | 0 reads: nlm-followup-verifier (F3b) when default/drawnformula are free; EP3849091 first |
| H4 | The t14 miss rate generalises to t11/t13 (default account) | t11 27/27 v2 rejects read, 0 ≥4 (2 at 3). t13 v2 rejects 40→**63** (23 new since 14:59; 4 have sonnet 0–1, 19 unscored) → 17 opus-read 0 ≥4, 2 at 3, **46 unscored**. t14 7/7, 0 ≥4, 3 at 3. Judged champions on t13 now 5/5 graduated (CN116130803, CN115166523, CN116508192, AU2022338850, canary CN120433348) | SCOPED: 0/51 judged misses among t11+t13+t14 rejects read (27+17+7); t13 5/5 champions graduated — but 46 t13 rejects unread, so the t13 line is a lower bound only | t13 46 + t12 18 unscored rejects = PROPOSED batch A′ (64 reads, was 44) — needs user approval |
| H5 | NLM graduation rank carries no relevance ordering | **cycle 5, pooled v2 graduates opus-read n=701 (was 493), ordinal (id-keyed ledger) × opus:** top-3: 9 ≥4 / 8 at 3 / 8 ≤2 (36 % champions); 4–10: 3 / 12 / 30 (7 %); 11+: 12 / 33 / 586 (1.9 %). Per tab (≥4/3/≤2): t10 1-3: 4/1/0, 4-10: 0/1/9, 11+: 1/4/163; t11 1/1/3, 0/6/5, 0/8/62; t12 0/2/4, 0/0/9, 0/0/110; t13 2/1/1, 0/2/6, 3/8/237; t14 2/3/0, 3/3/1, 8/13/14. **Job 25's 176 tail reads: 1 ≥4 (AU2022338850, ordinal 15), 1 at 3 (TW201528644), 174 ≤2 → 0.6 % champion rate in the ordinal-11+ tail, consistent with the prior 2.3 % estimate.** 12/24 champions still at ordinal 11+ | REFUTED as stated (top-3 enriched ~19× vs 11+, pre-audit); SCOPED corollary unchanged: rank is a triage order, not a recall cut-off | none; marginal gain of further tail reads ≈ 0 for H5 (line stopped) |
| H6 | The screen keys on claim boilerplate (F27) not weight-5 features | **Cycle 4, first positive class (n=1):** in EP3849091's round (27 docs staged 18:33:37 08-25, 7 graduated), EP3849091 has the HIGHEST lexical overlap with the t10 benchmark text (Jaccard 0.296 on ≥4-letter tokens of claims+abstract; the 7 graduates 0.142–0.195; round median 0.165), and its opus features: weight-2 base/remote devices YES, weight-3 links 1 YES / 1 NO, weight-4/5 power-supply features PARTIAL, weight-5 microwave frequency NO. All 7 graduates of that round score ≤1 (sonnet/opus). So the rejection is neither boilerplate-driven nor heavy-feature-driven — the round's graduation set looks unordered w.r.t. both signals | REFUTED for the one judged miss (boilerplate would have favoured EP3849091); replaced by H13 | none from me; re-evaluate if job 26 / parent batch adds judged misses |
| H7 | Reject-bucket champions are STAGING FAILURES (`add_failed`), a per-round systemic loss | Mechanism = 50-source cap overflow (H7b). **Cycle 5: t12 cursor 332 has entered the re-queued tail (starts at 319): 13/44 tail copies reached → 13/13 recovered (9 graduate, 4 rejected, all model-scored ≤2), 0 `add_failed` recurrence.** t10 add_failed 170 unchanged (tail from 1291, cursor 1031, ~7 rounds away); the 3 t10 add_failed champions (US20220221016 5.0, US10996236, CA2552849) have no position in the current queue (ids) other than the tail copies. t14 37 (paused) | SUPPORTED (mechanism); remedy VERIFIED on t12 (13/13), pending on t10 | 0 reads: watch the 3 t10 champions when the tail is reached (~round 44) |
| H8 | Stale opus verdicts (scored under pre-update features) are unreliable | unchanged from cycle 3 (10/114 stale 3.0 verdicts crossed to ≥4 on re-read; 3.0 tier clean on t10/t13; stale 2.0 tier 397 docs unread) | SUPPORTED | stale-2.0 sample (20 reads) still needs user approval — not launched (cap reserved for H4) |
| H9 | t10's add_failed champions (6 docs, 08-17 verdicts) are stale too | REFUTED (4/6 valid); EP3849091 was one of the 4 valid ones (re-read 06:01 08-25 → 4.0), which is what makes its 18:33 rejection a clean v2 judged miss | REFUTED, CLOSED | none |
| H10 | The v2 screen has a non-zero JUDGED-miss rate (rejects an opus champion it actually saw) | Cycle 4: first v2 judged miss = t10 EP3849091 (opus 4.0). **Cycle 5 detector re-run (rejected ∧ stored opus ≥3, all tabs): no new rows** — t10 EP3849091 4.0 + TW201717523 3.0; t11 CN108428823, CN217334362 (3.0); t13 US20160327612, CN115911602 (3.0); t14 KR100221047, WO2023085906, CN119487405 (3.0) — all pre-cycle-4 stamps. Second detector (round clusters whose graduates all ≤1 and a reject ≥3): only the same two t10 rounds. v2 rejects opus-read: t10 98, t11 27, t12 13, t13 17, t14 7 = **1/162 ≥4**. Judged champions on v2: t10 6 (5 grad / 1 rej), t11 1/1, t13 **5/5** (+AU2022338850, +canary CN120433348 graduated at ordinal 12), t14 13/13, t12 0 → **25/26 = 96.2 % judged recall**, pre-audit, lane-selected read sets. Unread rejects: t10 451 (sonnet ≤1), t12 18, t13 46 (unscored) | SUPPORTED (rate non-zero, 1/162 on read rejects; the EP3849091 class did NOT recur in ~3 h of new rounds on t10/t12/t13 — but the new t12/t13 rejects are mostly unscored, so recurrence there is untested) | PROPOSED batch A′ (t13 46 + t12 18); t10 sonnet-≤1 pool still low prior |
| H11 | The v2 re-screen scope (1554 over-clip docs) excludes champions, and the scope is not "multi-part" | unchanged (t10: 4 out-of-queue champions; 255 of 845 out-of-scope docs >118 KB). No new evidence this cycle | OPEN (analytic, 0 reads) | recall auditor / user to state the selection rule |
| H12 | `nlm_bridge` global lock serialises the three concurrent screens | app logs did not survive the 14:35 Docker-wide restart (`docker logs` has no screen lines), so no lock timing could be taken; t13 was "waiting for ingest" while t12 staged at 14:58 again | OPEN (0 reads) | lock timing after the next uninterrupted hour of logs |
| H13 | Within a round, NotebookLM's graduate set is not ordered by relevance when the round has no strong anchor doc: it graduates a fixed-ish number (7–10) of docs regardless of quality, so a lone champion in a weak round can be dropped | **Cycle 5 cluster test (nlm_screened_at 5-s buckets, survivor re-stamps included): graduates per round are NOT near-constant.** t10 rounds up to 08-25 20:41: 4–10 graduates of 17–28 (median 5), max stored score 1–3 except two rounds with a champion; t10 rounds after 08-26 14:58: 6–23 of 17–31 (median 11), max stored score 1.0 in 7 of 8 rounds. t12: 5–16 of 11–20; t13: 5–13 of 12–14. Graduate count varies 2–4× while max score stays ≤2, so the count is not quality-driven either | PARTLY REFUTED: the "fixed number" mechanism is wrong; the "ordering ≈ noise in weak rounds" half stands (all rounds with max ≤1 still graduate 5–23 docs). Replaced/extended by H15 | 0 reads: needs per-round rosters + NLM answers persisted (improvement 8) |
| H14 | The screen's rejection rate is tab-dependent and on t12/t13 it forwards almost everything, so stage-1 saves little opus budget there | graduation rate among v2-screened docs at cycle 5: t10 314/863 = 36 % (was 25 %), t11 86/113 = 76 %, t12 257/288 = 89 % (was 69 %), t13 381/444 = 86 %, t14 47/91 = 52 %. Split at the 14:35 restart (stamps): t10 pre 191/668 = 0.29 → post 123/195 = **0.63**; t12 0.85 → 0.95; t13 0.87 → 0.83 | SUPPORTED by counts (pre-audit; survivor re-stamps inflate n) — and t10 has now drifted toward the t12/t13 regime (H15) | 0 reads: ranking auditor to confirm with round rosters; remedy = per-round graduate cap or stricter prompt |
| H15 | **NEW (cycle 5, observation).** The t10 screen's selectivity changed after the 14:35 08-26 Docker restart: rounds now graduate ~2× as many docs (all model-scored ≤1) as before, i.e. the watchdog-resumed screen is running in a looser regime (candidates: leftover sources in the live notebook after the restart, larger effective rounds 17–31 vs 17–28, or the survivor set re-seeded from a different state) | t10 per-round graduates: 08-25 rounds 4–10 (median 5) of ~24; 08-26 post-restart rounds 11, 11, 12, 8, 23, 6, 11, 17 of 17–31; graduation rate 0.29 → 0.63; max stored score of the new graduates 1.0 in 7/8 rounds; t12 shows the same direction (0.85 → 0.95), t13 does not (0.87 → 0.83) | OPEN (analytic, 0 reads) | supervisor: compare notebook source counts before/after 14:35 (t10 notebook 50f703e7) and the roster sizes in app logs; if the loose regime persists, the 123 post-restart t10 graduates are the cheapest H14 remedy test (they should be re-judged under a graduate cap, not opus-read) |

## What we now believe (cycle 5 close, 08-26 ~20:05 UTC)

Analysis-only cycle (0 reads, 0 NLM jobs). Job 25 is reconciled from the DB, not the
counter: 176 opus verdicts (t10 62, t12 62, t13 52) landed 15:00:29–15:09:18 UTC and none
after, so the 15:42 restart cut no landed read and nothing was relaunched (all
deep-compare status endpoints idle). The t10 side is in fact COMPLETE, not paused at 62/91:
the other 29 manifest docs already carried sonnet 0.0 verdicts from 07-28 and the job ran
with skip_scored=true; the true remainder is t12 57 + t13 44 = 101 unscored graduates, not
130. The tail reads paid for what H5 predicted: 1 champion in 176 (t13 AU2022338850,
ordinal 15) and 1 borderline (t10 TW201528644) — a 0.6 % hit rate that changes no status
and stops the graduate-tail line. The judged-miss detector, re-run over ~3 h of new rounds
(t10 r29→37, t12 r19→27, t13 r23→32), found no new EP3849091-class row on any tab; the
judged-recall line is now 25/26 champions (96.2 %) because t13 gained two graduated
champions, one of them the GT canary CN120433348 (graduated at ordinal 12 in the 18:56
round). The caveat is power: 46 of t13's 63 v2 rejects and 18 of t12's 31 are unscored,
so "no recurrence" on those tabs is a lower bound. Two mechanism findings came free: the
cap-aware re-queue is verified on t12 (13/13 tail copies recovered, 0 add_failed
recurrence), and H13's "fixed graduate count" story is wrong — per-round graduate counts
vary 4–23 while max score stays ≤1, and on t10 the count doubled after the 14:35 restart
(graduation rate 0.29 → 0.63, H15). The t12 S1 +2 anomaly is consistent with the cycle-4
reading: US20160057394 and CN103059118 (id 6004/6005) are both `rejected` at 17:22:42
08-25, 206 KB each, never deep-read. All of this is pre-audit.

## Proposed improvements (not implemented — user decides)

1. **Cap-aware batch fill** (H7b) — DEPLOYED as 911d1b3 (cap-aware roster + re-queue);
   keep: verify with the 168 t10 re-queued docs that no `add_failed` recurs.
2. **Re-queue add_failed** — DEPLOYED (911d1b3). Remaining ask: audit gate treats
   `add_failed` as F3c-ns with its own denominator (done in registry fc6e576).
3. **Second-chance wait** (H7a): unchanged proposal.
4. **GT / verdict hygiene** (H8): re-read every opus verdict ≥2 after a benchmark feature
   update, or at least tag them `stale` in the UI; keep a score-history table (today's
   re-reads overwrote 114 verdicts — the pre-overwrite values are only in
   `stale3_snapshot_t{10,13}_2026-08-25.json`).
5. **Audit partitioning** (H10/H11): partition on `nlm_screen_state`, v1/v2 epoch AND
   in-queue/out-of-queue; report the v2 scope rule explicitly so out-of-scope champions
   (t10: 4, t11: 91, t12: 14, t13: 17, t14: 33 opus-≥4 docs outside the v2 queue) are not
   charged to the screen.
6. **Rank use** (H5): surface ordinal in the graduate list as a triage order for stage-2
   (top-3 first), never as a cut-off.
7. **Per-account bridge locks** (H12): only after the lock-timing test.
8. **Round-strength guard** (H13/H10): when a round's answer graduates docs the model
   lane scores ≤1 while rejecting a doc with a stored verdict ≥3, re-ask that round once
   with the survivors removed (or reduce the fixed graduate count); persist per-round
   rosters + NLM answers (`nlm_query_cache` holds no screen answers) so judged misses can be
   replayed instead of inferred from `nlm_screened_at` clusters.
9. **Queue-order invariant** (H7): keep `queue[cursor:]` = exactly the unscreened docs;
   re-staged docs should either be in `requeued` or have their position updated, so that
   audits can use position < cursor as "screened".

## Needs user approval (over cap or low prior)
- **PROPOSED batch A′ — H4/H10 reject pools (64 reads, supersedes batch A):** t13 46
  unscored v2 rejects + t12 18 unscored v2 rejects (chars not re-measured this cycle;
  cycle-4 proxy ≈ 0.16 M chars/doc → ~10 M chars ≈ 2.6 M input tokens). Decides whether
  the judged-miss class seen on t10 exists on the default/work2 tabs at cursors t13
  444/458, t12 332/363 — i.e. it closes both reject pools at near-complete screens. This
  is the single cheapest decisive test left; NOT launched (allowance revoked 16:25). Ids: run the query
  `nlm_screen_state='rejected' AND nlm_screened_at>=1787511600 AND score IS NULL` per tab
  (deterministic; manifests can be written on approval).
- **PROPOSED batch B — H10 t10 unread rejects (385, sonnet ≤1):** NOT recommended now;
  low prior (sonnet 0/1 on all 385). If the parent wants a bound, a seeded 40-doc sample
  would bound the sonnet-≤1 → opus-≥4 rate below ~7 %; ~4 M chars.
- H8 stale-2.0 sample (20 reads, t10 10 + t13 10, seeded): unchanged, not launched.

## Read budget discipline
Cycle 5: 0 reads launched; ledger gets one reconciliation line for job 25 (176 measured;
t10 62 = complete under skip_scored, t12 62/119, t13 52/96; remainder 101 unscored).
Every launched read names the hypothesis it serves and the expected information gain;
stop a line when a batch changes no status (marginal ≈ 0). Ledger:
docs/experiments/read_ledger.jsonl — cycle 4 adds job 26 (t11, 12 reads, 1.88 M chars ≈
0.47 M input tokens proxy; expected gain: closes the t11 v2 reject pool 27/27 so H4-on-t11
is measured, not sampled) and records the parent's job 25 (306 graduates, PENDING).
Cycle-4 marginal gain without any new read of mine: H10 flipped SCOPED → SUPPORTED, H2
REFUTED on t10, H6 REFUTED for n=1, H13 born — all from re-reading the DB against the
screen states (0 tokens).
