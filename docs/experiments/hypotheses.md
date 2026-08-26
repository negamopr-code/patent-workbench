# Hypothesis register — NLM v2 screen viability (owner: hypothesis-driver agent)

Status: OPEN · SUPPORTED · REFUTED · SCOPED (true only under stated scope) · PARKED

Last update: 2026-08-26 cycle 4 (15:05–15:40 UTC). All conclusions PRE-AUDIT: ranking
audit re-ran 14:57 08-26 (defa67e, worst FAIL), recall/staging 17:27 08-25 (STALE-by-screen
per supervisor 14:55), full-doc 16:51 08-25 (pre-defa67e); the pipeline-integrity-supervisor
has gated nothing below. Cycle-4 constraints: no NLM jobs, no champion/closure claims,
≤20 opus reads outside the parent's 306-graduate manifest. Cycle-4 reads launched by me:
**12** (t11 unscored v2 rejects, ledger job 26). Parent-launched 15:12: 306 unread v2
graduates (t10 91 / t12 119 / t13 96, /data/audits/graduate_reads_2026-08-26.json) —
PENDING, counted as incoming H10/H5 evidence, not waited for.

Scope conventions: "v2-screened" = `nlm_screened_at ≥ 2026-08-23 19:00` (full multi-part
staging re-screen, over-clip scope); states with earlier dates belong to the v1 (truncated)
screen and are NOT evidence about v2. "add_failed" = never indexed in NotebookLM (H7).
opus-read ⇔ `score_model LIKE '%opus%' AND score IS NOT NULL`. "Stale" verdict = opus
`scored_at < benchmark.updated_at` of its tab (t10 08-18 08:13, t13 08-17 21:06; t11/t12/t14
have no stale opus verdicts). Graduate "ordinal" = `ledger[number][0]` in
`/data/.nlm_screen_{t}.json` (survivors are re-ranked each round, so a persistent survivor's
ordinal is its latest-round rank). Screen progress at cycle 4: t10 r29 863/1459 (212 grads,
483 v2 rejects, 170 add_failed), t11 r8 113/188 paused (86/27/0), t12 r19 225/363 (156/25/44),
t13 r23 319/458 (279/40/0), t14 r3 91/337 paused (47/7/37).

| id | hypothesis | evidence | status | next test |
|---|---|---|---|---|
| H1 | The screen's miss class = old (pre-~2008), non-CJK disclosures lacking a modern claim skeleton | t10: 30/30 profile-matched non-graduates ≤2. t14: the 2 profile "misses" (US5686815, DE10158062) are add_failed. **Cycle 4:** the first v2 judged miss (EP3849091, 2021 EP, modern claim skeleton) does not fit the profile | REFUTED on t10; confounded by H7 on t14 | none (closed cycle 1) |
| H2 | t10/t12 over-clip subsets contain no unread champions | t10: 98/483 v2 rejects read → **1 ≥4 (EP3849091, see H10)**; 121/212 v2 grads read, 5 ≥4 (4 GT controls + US20230337972); 170/170 add_failed read, 3 ≥4. t12: 9/25 v2 rejects read 0 ≥3; 56/156 v2 grads read 0 ≥4 (2 at 3); 35/44 add_failed 0 ≥3. Cycle 4 free pairs (docs screened since cycle 3 that already had opus verdicts): t12 29 graduates, 0 ≥4 | **REFUTED on t10 as stated** (an unread-at-cycle-3 champion existed in the reject bucket; it had been read 06:01 08-25 as add_failed and moved to rejected 18:33); SUPPORTED on t12 within the read part (16 rejects + 100 grads unread → 119 in the parent's batch) | t12: parent batch (119 grads) closes the graduate side; 16 unscored t12 rejects = PROPOSED (below) |
| H3 | Stage-2 citation follow-up ≈ opus on graduates | unchanged: 7/7 champion-level agreement, 3 weight-5 single-feature disagreements; followup_ledger.jsonl still 5 rows (last 06:12 08-25) — NLM jobs blocked all cycle (A2). Three-way candidates now 6: t13 CN116130803, CN115166523, CN116508192; t14 WO2014110477; t11 JP2022548488; **t10 EP3849091 (v1 claims-audit nlm_score 6.0 → v2 screen REJECT → opus 4.0)** — the only doc where two NLM instruments disagree with each other | SUPPORTED on 7/7 (small n; pre-audit) | 0 reads: nlm-followup-verifier (F3b) when default/drawnformula are free; EP3849091 first |
| H4 | The t14 miss rate generalises to t11/t13 (default account) | unchanged screen progress (t11 paused 113/188, t13 319/458 but 0 new t13 rejects opus-read since cycle 3 beyond 3 free pairs ≤1). v2 rejects: t11 27 (8 opus-read, 0 ≥4, 2 at 3; 7 sonnet ≤1; **12 unscored → launched cycle 4, job 26**); t13 40 (12 read, 0 ≥4, 2 at 3; 28 unscored); t14 7/7 read, 0 ≥4, 3 at 3 | SCOPED: 0/27 judged misses among t11+t13+t14 rejects read (n=27) — but t10 now shows the class exists (H10), so the null on default-account tabs is a power question: at t10's observed rate (1/98) the expected count in 27 reads is 0.3 | **job 26 (12 reads) closes the t11 pool 27/27 at cursor 113**; t13 28 unscored rejects = PROPOSED (over cap) |
| H5 | NLM graduation rank carries no relevance ordering | **cycle 4, pooled v2 graduates opus-read n=493 (was 341), ordinal × opus:** top-3: 9 ≥4 / 8 at 3 / 7 ≤2 (37 % champions); 4–10: 3 / 12 / 27 (7 %); 11+: 10 / 31 / 386 (2.3 %). Per tab (≥4 / 3 / ≤2): t10 1-3: 4/1/0, 4-10: 0/1/9, 11+: 1/3/102; t11 1/1/3, 0/6/5, 0/8/62; t12 0/2/3, 0/0/7, 0/0/44; t13 2/1/1, 0/2/5, 1/7/164; t14 2/3/0, 3/3/1, 8/13/14. Still 10/22 champions at ordinal 11+ | REFUTED as stated (top-3 enriched ~16× vs 11+, pre-audit); SCOPED corollary unchanged: not a recall cut-off | parent batch (306 grads, mostly ordinal 11+) will re-test the 2.3 % tail rate; 0 reads from me |
| H6 | The screen keys on claim boilerplate (F27) not weight-5 features | **Cycle 4, first positive class (n=1):** in EP3849091's round (27 docs staged 18:33:37 08-25, 7 graduated), EP3849091 has the HIGHEST lexical overlap with the t10 benchmark text (Jaccard 0.296 on ≥4-letter tokens of claims+abstract; the 7 graduates 0.142–0.195; round median 0.165), and its opus features: weight-2 base/remote devices YES, weight-3 links 1 YES / 1 NO, weight-4/5 power-supply features PARTIAL, weight-5 microwave frequency NO. All 7 graduates of that round score ≤1 (sonnet/opus). So the rejection is neither boilerplate-driven nor heavy-feature-driven — the round's graduation set looks unordered w.r.t. both signals | REFUTED for the one judged miss (boilerplate would have favoured EP3849091); replaced by H13 | none from me; re-evaluate if job 26 / parent batch adds judged misses |
| H7 | Reject-bucket champions are STAGING FAILURES (`add_failed`), a per-round systemic loss | Mechanism = 50-source cap overflow (H7b). Cycle 4: t10 add_failed 172→170; the `requeued` list (168) is appended as duplicate queue entries at positions 1291–1458 (cursor 863 → reached in ~11 rounds; all 168 still add_failed). **Anomaly:** EP3849091 (add_failed at cycle 3) is NOT in `requeued`, has a single queue position (743) and was screened at 18:33 08-25 → the app re-staged it in the normal sequence; likewise the 08-23 19:33 add_failed champions CA2552849 / US20220221016 sit at positions 1077/1131 (> cursor) and are not in `requeued` — queue order ≠ screening order, so "position < cursor ⇒ screened" does not hold on t10. t12 add_failed 44 (44 requeued, tail copies), t14 37 (37 requeued, paused). Champion cost: t10 3 remaining (US20220221016 5.0, US10996236, CA2552849), t14 6/17 | SUPPORTED (mechanism); remedy PARTIALLY VERIFIED (1 recovery, 1 recovery-then-judged-miss) | 0 reads: watch the 3 remaining t10 add_failed champions when the tail copies are reached (~round 40); supervisor to explain the queue-order anomaly |
| H8 | Stale opus verdicts (scored under pre-update features) are unreliable | unchanged from cycle 3 (10/114 stale 3.0 verdicts crossed to ≥4 on re-read; 3.0 tier clean on t10/t13; stale 2.0 tier 397 docs unread) | SUPPORTED | stale-2.0 sample (20 reads) still needs user approval — not launched (cap reserved for H4) |
| H9 | t10's add_failed champions (6 docs, 08-17 verdicts) are stale too | REFUTED (4/6 valid); EP3849091 was one of the 4 valid ones (re-read 06:01 08-25 → 4.0), which is what makes its 18:33 rejection a clean v2 judged miss | REFUTED, CLOSED | none |
| H10 | The v2 screen has a non-zero JUDGED-miss rate (rejects an opus champion it actually saw) | **Cycle 4: first v2 judged miss = t10 EP3849091 (opus 4.0, current-feature verdict 06:01 08-25).** Staged and answered in the 27-doc round of 18:33:37 08-25 (7 graduates, all ≤1); `unmatched` list empty, so it was judged, not lost. v2 rejects opus-read: t10 98, t11 8, t12 9, t13 12, t14 7 = **1/134 ≥4** (t10 1/98); ≥3: t10 1 (TW201717523), t11 2, t13 2, t14 3. Judged champions on v2 so far: t10 6 (5 graduated — 4 GT at ordinals 1–3, US20230337972 at 11 — 1 rejected), t11 1/1, t13 3/3, t14 13/13 graduated, t12 0 champions in scope → 22/23 = 95.7 % judged recall, pre-audit, lane-selected read sets. Canary CN120433348 (t13 pos 425) not reached (cursor 319) | **SUPPORTED** (rate non-zero; 1/134 on read rejects, but t10's 385 unread rejects are all sonnet ≤1 so the unread part is low-prior) | job 26 (t11 12) + PROPOSED t13 28 / t12 16; parent's 306-graduate batch tests the other side (graduates that are not champions do not change H10) |
| H11 | The v2 re-screen scope (1554 over-clip docs) excludes champions, and the scope is not "multi-part" | unchanged (t10: 4 out-of-queue champions; 255 of 845 out-of-scope docs >118 KB). No new evidence this cycle | OPEN (analytic, 0 reads) | recall auditor / user to state the selection rule |
| H12 | `nlm_bridge` global lock serialises the three concurrent screens | app logs did not survive the 14:35 Docker-wide restart (`docker logs` has no screen lines), so no lock timing could be taken; t13 was "waiting for ingest" while t12 staged at 14:58 again | OPEN (0 reads) | lock timing after the next uninterrupted hour of logs |
| H13 | **NEW (cycle 4).** Within a round, NotebookLM's graduate set is not ordered by relevance when the round has no strong anchor doc: it graduates a fixed-ish number (7–10) of docs regardless of quality, so a lone champion in a weak round can be dropped | EP3849091's round: 7/27 graduated, all 7 ≤1 by model score, lexical overlap of graduates 0.14–0.20 vs 0.30 for the rejected champion. Cross-check on all t10 v2 rounds needs per-round rosters (not persisted; only `nlm_screened_at` batches). Per-batch graduate counts from `nlm_screened_at` clusters can be computed for t10/t12/t13 (0 reads) | OPEN | 0 reads: cluster t10 v2 docs by `nlm_screened_at` second, count graduates per cluster and their max opus/sonnet score; if graduates-per-round is near-constant while max score varies, H13 is supported |
| H14 | **NEW (cycle 4, observation).** The screen's rejection rate is tab-dependent and on t12/t13 it forwards almost everything, so stage-1 saves little opus budget there | graduation rate among v2-screened docs: t10 212/863 = 25 %, t11 86/113 = 76 %, t12 156/225 = 69 %, t13 279/319 = 87 %, t14 47/91 = 52 %. t13 rounds graduate 8–14 of 12–14 docs (survivor re-stamps included; per-round rosters not persisted) | SUPPORTED by counts (pre-audit; survivor re-stamping inflates per-round n) | 0 reads: ranking auditor to confirm with the round rosters; if confirmed, the remedy is a per-round graduate cap or a stricter prompt on t12/t13 |

## What we now believe (cycle 4 close, 08-26 ~15:40 UTC)

The v2 screen does have a judged-miss class: EP3849091 (t10, opus 4.0 on the current
features, re-verified 06:01 08-25) was staged in a 27-doc round at 18:33 08-25, answered,
and rejected while seven ≤1 docs from the same round graduated. It was the doc with the
highest lexical overlap with the benchmark in that round, so neither the boilerplate story
(H6) nor a heavy-feature story explains it; the simplest reading is that NotebookLM
graduates roughly a fixed number of docs per round and in a weak round the ordering is
close to noise (H13, untested). Everything else moved incrementally: pooled ordinal × opus
on 493 read graduates keeps the same shape (top-3 37 % champions, 11+ 2.3 %, 10/22 champions
at 11+); the judged-recall line across the five tabs is 22/23 champions that reached a v2
judgement (pre-audit, read sets lane-selected); t12 remains champion-free in scope. The
cap-aware re-queue is working mechanically (168 t10 tail copies pending) but the queue
order no longer reflects screening order, which the supervisor should explain before
anyone uses "position < cursor" as a screened test. Stage-2 (H3) could not advance: every
NLM account is occupied by a screen. The t12 S1 +2 anomaly is a screen-staging event, not
a read: the 17:22 08-25 t12 round staged 13 docs, seven of them >120 KB and never
deep-read (CA2856061, US20160033194, US20190372449, US20160057394, CN103059118,
US20230032979, US20100015512); the two that the S1 counter picked up are most plausibly the
two REJECTED ones (US20160057394 206 KB, CN103059118 206 KB), whose parts leave the live
notebook on rejection and so cannot be "verified full" by the auditor — an auditor
artefact of the upper-bound design, not evidence of truncated staging; the staging auditor
should confirm by listing those two in its next run. All of this is pre-audit.

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
- **PROPOSED batch A — H4/H10 reject pools (44 reads):** t13 28 unscored v2 rejects
  (4.34 M chars ≈ 1.1 M input tokens proxy) + t12 16 unscored v2 rejects (2.96 M chars ≈
  0.74 M tokens). Decides whether the judged-miss class seen on t10 exists on the
  default/work2 tabs at the current cursors (t13 319/458, t12 225/363). Ids: run the query
  `nlm_screen_state='rejected' AND nlm_screened_at>=1787511600 AND score IS NULL` per tab
  (deterministic; manifests can be written on approval).
- **PROPOSED batch B — H10 t10 unread rejects (385, sonnet ≤1):** NOT recommended now;
  low prior (sonnet 0/1 on all 385). If the parent wants a bound, a seeded 40-doc sample
  would bound the sonnet-≤1 → opus-≥4 rate below ~7 %; ~4 M chars.
- H8 stale-2.0 sample (20 reads, t10 10 + t13 10, seeded): unchanged, not launched.

## Read budget discipline
Every launched read names the hypothesis it serves and the expected information gain;
stop a line when a batch changes no status (marginal ≈ 0). Ledger:
docs/experiments/read_ledger.jsonl — cycle 4 adds job 26 (t11, 12 reads, 1.88 M chars ≈
0.47 M input tokens proxy; expected gain: closes the t11 v2 reject pool 27/27 so H4-on-t11
is measured, not sampled) and records the parent's job 25 (306 graduates, PENDING).
Cycle-4 marginal gain without any new read of mine: H10 flipped SCOPED → SUPPORTED, H2
REFUTED on t10, H6 REFUTED for n=1, H13 born — all from re-reading the DB against the
screen states (0 tokens).
