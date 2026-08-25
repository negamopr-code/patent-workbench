# Conclusion — hypothesis-driver loop, cycles 1–2 (2026-08-24 20:44 → 2026-08-25 06:30 UTC)

PRE-AUDIT. Every number below awaits the pipeline-integrity-supervisor and the
recall/ranking/staging auditors (gate matrix in docs/failure-registry.md). Register:
docs/experiments/hypotheses.md · ledger: docs/experiments/read_ledger.jsonl · thesis:
docs/experiments/thesis_2026-08-25.md.

## Per-hypothesis verdicts

| id | verdict | key numbers / patents |
|---|---|---|
| H1 old non-CJK miss class | REFUTED (t10), confounded (t14) | t10 30/30 profile non-graduates ≤2; t14 US5686815, DE10158062 were add_failed |
| H2 over-clip slices hold no unread champions | SUPPORTED (t12 all buckets; t10 among 376 read) | t12 add_failed 32/32: 0 ≥3, max 1.0 (KR20240056998, US20080253085, WO2011125505) |
| H3 stage-2 ≈ opus | OPEN | no per-doc stage-2 verdict journaled; three-way table impossible yet |
| H4 t14 miss rate on t11/t13 | OPEN | default account quota: t11 one v2 round, t13 none |
| H5 rank ≠ relevance | SUPPORTED (weak counter-signal: 4 valid GT ranked 1,2,3,≤10) | US9991741 ordinal 4 |
| H6 keys on F27 boilerplate | SCOPED (t14, n=7) | needs true-reject recomputation |
| H7 add_failed = staging loss, not judgement | SUPPORTED; mechanism = 50-source cap overflow (H7b) | t10 per-round predicted vs observed: 4/4 11/12 13/15 17/17 17/17 18/14 18/18 14/15 17/15 19/17 12/13 9/11 |
| H8 t10 GT stale | SUPPORTED, 8/12 invalid | valid: EP3970350, JP2019221076, US20070021140, CN113924787 (all 4.0 blind 08-25) |
| H9 t10 add_failed champions stale | REFUTED, 4/6 valid | US20220221016 5.0, US10996236, EP3849091, CA2552849 4.0; TW201717523, CN113287245 → 3 |
| H10 v2 judged-miss rate | SCOPED: 0/114 v2 rejects read ≥4 | audit's judged misses are v1 states (08-06…08-09) |

## Exact reject-miss counts (opus ≥4 among opus-read docs, by v2 screen state)

| tab | v2 rejected read / ≥4 | v2 graduate read / ≥4 | add_failed read / ≥4 (unread) | v2 scope note |
|---|---|---|---|---|
| t10 | 96/216 · **0** | 84/84 · 4 | 90/173 · **5** (83 unread → 97 incl. never-scored; see approval line) | GT: 4/4 valid controls graduated; pipeline 4/9 champions staged |
| t11 | 2/19 · 0 | 7/19 · 1 | 40/40 · **0** | one v2 round only |
| t12 | 9/9 · 0 | 15/15 · 0 | 47/47 · **0** | no registered controls → corpus recall unmeasured |
| t13 | 0/0 · – | 0/0 · – | 13/13 · **0** | no v2 round answered |
| t14 | 7/7 · 0 | 34/34 · 11 | 37/37 · **6** (CN107431369, US5686815, EP3930140, DE10158062, CN105723559, US11397216) | 52/52 staged GT recalled; 6/58 GT unstaged (auditor) |

Recall lines (screen among docs that reached NotebookLM, opus ≥4 as truth):
t10 4/4 (valid GT) · t14 11/11 among v2 grads+rejects read · t11 1/1 · t12 0 champions in
scope · t13 unmeasured. Pipeline recall incl. staging: t10 4/9 (44 %) · t14 11/17 (65 %).

## Three-way graduate table
Not available: stage-2 ledgers (t10 08-16 5 docs, t13 08-16 10 docs, t10 08-24 10 docs,
t14 fu_t14_b1 "running") contain doc lists only. Column 3 (blind opus) exists for all 25
listed docs; column 2 must come from the nlm-followup-verifier.

## Remedy proposals (not implemented)
1. Cap-aware batch fill: shrink the roster until Σparts(roster+survivors)+1 ≤ 50, carry
   overflow to the next round; never evict tails.
2. Re-queue add_failed; gate matrix treats it as F3c "not staged"; separate denominator in
   every recall line.
3. Second-chance ingest wait = full PIPELINE_INGEST_TIMEOUT; log per-round missing counts.
4. GT hygiene: re-read controls after feature updates; score-history table; re-register the
   t10 canary set (4 controls + 5 champions above).
5. Audits partition on nlm_screen_state and v1/v2 epoch.

## Admissibility caveats (report §8)
Fresh audit verdict files on the current head are outstanding; per-tab recall lines above
are against the *re-verified* controls, not yet REGISTERED ones (registry update needed);
t12 has no controls; the three-way table is missing; roster-39 mode and the account-sharing
decisions are not user-registered; all verdicts are opus-5 single reads (no repeat-read
variance measured); the token figures are a chars/4 proxy (bridge logs no usage).
