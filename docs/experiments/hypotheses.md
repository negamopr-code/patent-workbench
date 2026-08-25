# Hypothesis register — NLM v2 screen viability (owner: hypothesis-driver agent)

Status: OPEN · SUPPORTED · REFUTED · SCOPED (true only under stated scope) · PARKED

Last update: 2026-08-25 cycle 3 (16:55 UTC). All conclusions PRE-AUDIT: the staging,
recall, ranking and full-doc auditors re-ran at 16:49 UTC on deploy 911d1b3 (all `worst:
FAIL`, /data/audits/audit_*.json) and the pipeline-integrity-supervisor has not gated
anything below. Cycle-3 launched 0 new reads (the 5-doc t13 leftover job launched 16:48 by
the parent landed 16:50, all 5 ≤1). Today's reads (user-approved, ledger jobs 12–20):
t10 121, t11 18, t12 103, t13 118, t14 8 = 368 reads.

Scope conventions: "v2-screened" = `nlm_screened_at ≥ 2026-08-23 19:00` (full multi-part
staging re-screen, over-clip scope); states with earlier dates belong to the v1 (truncated)
screen and are NOT evidence about v2. "add_failed" = never indexed in NotebookLM (H7).
opus-read ⇔ `score_model LIKE '%opus%' AND score IS NOT NULL`. "Stale" verdict = opus
`scored_at < benchmark.updated_at` of its tab (t10 08-18 08:13, t13 08-17 21:06; t11/t12/t14
have no stale opus verdicts). Graduate "ordinal" = `ledger[id][0]` in
`/data/.nlm_screen_{t}.json` (survivors are re-ranked each round, so a persistent survivor's
ordinal is its latest-round rank).

| id | hypothesis | evidence | status | next test |
|---|---|---|---|---|
| H1 | The screen's miss class = old (pre-~2008), non-CJK disclosures lacking a modern claim skeleton | t10: 30/30 profile-matched non-graduates ≤2. t14: the 2 profile "misses" (US5686815, DE10158062) are add_failed | REFUTED on t10; confounded by H7 on t14 | none (closed cycle 1) |
| H2 | t10/t12 over-clip subsets contain no unread champions | t10: 96/354 v2 rejects read, 0 ≥3; 85/121 v2 grads read, 5 ≥4 (the 4 GT controls + recovered US20230337972); 172/172 add_failed read, 4 ≥4. t12: 9/9 v2 rejects 0 ≥3; **35/35 v2 grads read (cycle 3: +20, all ≤3), 0 ≥4, 1 at 3**; 35/44 add_failed 0 ≥3 | SUPPORTED — t12 all three buckets (9 add_failed unread, in re-queue); t10 scoped to the read part (258 v2 rejects + 36 v2 grads unread) | none (marginal 0) |
| H3 | Stage-2 citation follow-up ≈ opus on graduates | unchanged from cycle 2: 7/7 champion-level agreement (t11 CN223245862 5.0, CN115051084, CN220420731, CN223471682 4.0; t13 CN206076985, CN104760550, CN224152886 4.0); 3 weight-5 single-feature disagreements. No new stage-2 rows today (followup_ledger.jsonl last row 06:12). New candidates for the three-way table: t13 CN116130803 (v2 grad ordinal 2 → opus 5.0), t14 WO2014110477 (v2 grad ordinal 22 → 4.0), t11 JP2022548488 (v2 grad ordinal 1 → 5.0), t13 CN115166523 (ordinal 1 → 6.0), CN116508192 (ordinal 11 → 5.0) | SUPPORTED on 7/7 (small n; pre-audit) | 0 reads: hand the 5 new v2 graduate champions to nlm-followup-verifier (F3b) |
| H4 | The t14 miss rate generalises to t11/t13 (default account) | **cycle 3:** both screens were resumed on cap-aware fill (911d1b3): t11 r8 113/188, 86 grads, 27 v2 rejects — 8 read, 0 ≥4, 2 at 3 (paused 16:55); t13 r13 187/458, 153 grads, 34 v2 rejects — 9 read, 0 ≥4, 2 at 3 (running). t14 for comparison: 7/7 v2 rejects read, 0 ≥4, 3 at 3. On the reject bucket t11/t13 look like t14 (0 ≥4, some 3s); on graduates t11 is champion-poor in the over-clip subset (1/67 ≥4: JP2022548488 5.0 at ordinal 1) because 91 of its 93 opus-≥4 docs are outside the v2 queue | SCOPED: no v2 judged miss on t11/t13 so far (0/17 read rejects); true-reject pool still small (17 read of 61) | after t11 resumes / t13 reaches ~300: read the remaining v2 rejects at 3 or unread (t11 19, t13 25 → 44 reads, under cap) |
| H5 | NLM graduation rank carries no relevance ordering | **cycle 3, pooled v2 graduates opus-read (n=341, all 5 tabs), ordinal × opus:** top-3: 9 ≥4 / 6 at 3 / 6 ≤2 (43 % champions); 4–10: 3 / 11 / 24 (8 %); 11+: 10 / 27 / 245 (3.5 %). Excluding t10 (whose 4 GT controls are persistent survivors re-ranked to 1–3 in round 19): top-3 5/16, 4–10 3/37, 11+ 9/214 — same ordering. But 10 of 22 champions sit at ordinal 11+ (t14: JP2018147827 14, CN109073712 17, US20250096336 11, EP3968486 13, US10446885 18, CN113646652 17, CN105823988 11, WO2014110477 22; t13 CN116508192 11; t10 US20230337972 11) | REFUTED as stated (rank is enriched ~12× top-3 vs 11+, pre-audit); SCOPED corollary: rank is NOT usable as a recall cut-off (45 % of champions are at 11+) | none — 0 reads; ranking auditor to confirm with its own ordinal source |
| H6 | The screen keys on claim boilerplate (F27) not weight-5 features | t14 split confounded (reject side 31/37 add_failed); t11/t13 true-reject pools now exist (27, 34) but only 17 read, none ≥4 → no positive class to split on | SCOPED (no v2 judged miss to characterise) | recompute if H4/H10 ever produce a v2 judged miss; 0 reads |
| H7 | Reject-bucket champions are STAGING FAILURES (`add_failed`), a per-round systemic loss | Mechanism = 50-source cap overflow (H7b, cycle 2). **Cycle 3 — remedy evidence:** cap-aware fill + re-queue (911d1b3) is live: t10 rounds 13–19 staged "6/28 added" style rosters (shrunk from 39); the 173rd add_failed champion **US20230337972 (5.0) was re-queued and graduated at ordinal 11 in round 16** — t10 add_failed 173→172 (4 ≥4 remain: US20220221016 5.0, US10996236, EP3849091, CA2552849; 168 in `requeued`, still pending). t12 add_failed 44 (15 re-queued), t14 37 (37 re-queued, paused). Champion cost unchanged: t10 4 (+1 recovered), t14 6/17, t11/t12/t13 0 | SUPPORTED (mechanism + value-neutral cost); remedy PARTIALLY VERIFIED (1 recovery) | 0 reads: count `requeued → graduate/rejected` transitions after 5 more t10 rounds; the 4 remaining t10 add_failed champions are the canary |
| H8 | Stale opus verdicts (scored under pre-update features) are unreliable | cycle 2: t10 GT set 4/12 valid. **Cycle 3 (H8-boundary, 114 stale 3.0 verdicts re-read blind):** t10 71 → 4 crossed to 4.0 (US20180351412, US11922243, US20120007441, CN103683526), 0 above 4, 67 stayed ≤3; t13 43 → 6 crossed: CN116130803 3→5.0 (old features = "battery triggering device" list, i.e. the t13 benchmark changed 08-17), CN218958581, CN115863801, CN220510820, CN120073105, CN120433348 →4.0. Crossing rate 10/114 = 8.8 % (t10 5.6 %, t13 14 %). Remaining stale opus verdicts: t10 88 at 0 / 264 at 1 / 200 at 2; t13 18 / 131 / 197 — none at 3 left | SUPPORTED and extended to t13; the 3.0 tier is now clean on both tabs | **Proposal (≤20 reads, not launched):** seeded 10+10 sample of stale 2.0 verdicts (t10/t13) to bound the 2→≥4 crossing rate; 0/20 bounds it below ~15 % (not decisive for the 397-doc tier → needs user approval to go further) |
| H9 | t10's add_failed champions (6 docs, 08-17 verdicts) are stale too | REFUTED (4/6 valid); pool 173/173 read, exact t10 add_failed reject-miss = 5 (now 4 after the US20230337972 recovery) | REFUTED, CLOSED | none |
| H10 | The v2 screen has a non-zero JUDGED-miss rate (rejects an opus champion it actually saw) | **Cycle 3 judged-miss count on v2 = 0.** v2 rejects opus-read: t10 96, t11 8, t12 9, t13 9, t14 7 = **0/129 ≥4** (≥3: t11 2, t13 2, t14 3). The 11 newly-≥4 docs of today, placed against the screen: t13 CN116130803 → v2 GRADUATE ordinal 2 (round 13); t14 WO2014110477 → v2 GRADUATE ordinal 22 (round 3); t10 US20180351412, US11922243, US20120007441, CN103683526 → NOT in the v2 queue (never screened by any recorded run; see H11); t13 CN218958581, CN115863801, CN220510820, CN120073105 → v1 graduates 08-08/09, not in the v2 queue; t13 CN120433348 → v1 graduate, in the v2 queue at position 425 (cursor 187, not yet reached). So 2 confirmed v2 graduations, 0 v2 rejections, 9 unseen-by-v2 | SCOPED: 0/129 on v2 (pre-audit); v1 judged misses (8 docs, roster-35 rounds) remain out of v2 scope | H4's 44-read reject sweep once t11/t13 progress; CN120433348 is a free canary (watch the t13 ledger when the cursor passes 425) |
| H11 | **NEW (cycle 3).** The v2 re-screen scope (1554 over-clip docs) excludes docs that are champions, and the scope is not "multi-part" | t10: 845 docs are neither v2-queued nor `nlm_screened_at`-tagged; 758 of them are opus-read → 4 ≥4 (the four H8-boundary crossers, 0.5 %) and 57 at 3. 255 of the 845 exceed 118 KB (multi-part under v2 staging) — e.g. US20120007441 (187 KB) — so "over-clip" ≠ ">118 KB". Corpus champions vs v2 queue: t10 9/13 in queue, t11 2/93, t12 0/14, t13 4/21, t14 26/59 | OPEN (analytic, 0 reads) — needs the scope definition from the recall auditor / user | 0 reads: recall auditor to state the 1554-doc selection rule and whether the 4 t10 out-of-scope champions were v1-screened (no record in `documents`) |
| H12 | **NEW (cycle 3, throughput).** `nlm_bridge` holds ONE global lock for all NLM CLI calls, so concurrent screens (t10/t12/t13) serialise; long "waiting for ingest" phases are lock queueing, not stalls | t12 round 7 waited 11 min for ingest while t10/t13 were staging (parent observation); t12 and t13 both show "waiting for NotebookLM to ingest the batch" at 16:50 while t10 stages "6/28 added" | OPEN (0 reads) | 0 reads: time-stamp `nlm_bridge` lock acquire/release per tab from the app log over one hour; if lock-wait > ingest time, propose per-account locks (accounts are already disjoint: t10 drawnformula, t11/t13 default, t12/t14 work2) |

## What we now believe (cycle 3 close, 16:55 UTC)

Nothing today produced a v2 judged miss: 129 v2 rejects opus-read across the five tabs, none
≥4, and the two champions that the v2 screen has actually seen (t13 CN116130803 5.0, t14
WO2014110477 4.0) were graduated — one at ordinal 2, one at ordinal 22. Graduation rank
does carry signal (top-3 ordinals are 43 % champions vs 3.5 % at 11+), so H5 as worded is
refuted, but almost half of the champions sit at ordinal 11+, so rank is a precision hint,
not a recall cut-off. The cap-aware fill / re-queue deploy already recovered one staging-lost
champion (US20230337972 → graduate, ordinal 11), which is the first direct evidence that
the H7 remedy works. The stale-verdict problem (H8) is broader than the t10 GT set: 10 of
114 stale 3.0 verdicts crossed to ≥4 on re-read, on both tabs whose benchmark changed;
the 3.0 tier is now clean but 397 stale 2.0 verdicts remain unread. The new open question
is scope (H11): the four t10 docs that crossed today were never in the v2 queue at all,
so the screen's recall on t10 is only measured inside the 1291-doc queue, and the queue is
not simply "the multi-part docs". Throughput (H12) is a lock-serialisation effect, not
a stall. All of this is pre-audit (auditors re-ran 16:49 on 911d1b3, all FAIL-gated).

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

## Needs user approval (over cap or low prior)
- H8 stale-2.0 sample (20 reads, t10 10 + t13 10, seeded): bounds the 2→≥4 crossing
  rate; a positive would imply re-reading 397 docs (~30 M chars proxy) — that follow-up is
  over cap and needs approval.
- H4/H10 reject sweep on t11/t13 (44 reads) — under cap, but wait until t11 is resumed and
  t13 passes ~300 so the pool is representative.

## Read budget discipline
Every launched read names the hypothesis it serves and the expected information gain;
stop a line when a batch changes no status (marginal ≈ 0). Ledger:
docs/experiments/read_ledger.jsonl — 20 jobs: 11 pre-today (520 reads, proxy ≈ 53.1 M
chars ≈ 13.3 M input tokens) + 9 today (368 reads landed, proxy ≈ 31.7 M chars from the
batch plan ≈ 7.9 M input tokens; the bridge logs no usage). Marginal gain today: the
H8-boundary batches changed H8 (extended) and created H11; the t14 batch changed H5
(ordinal-22 champion) ; the t11/t12/t13 H5/H10 batches changed no status (0 ≥4) but
tightened H10's denominator 114→129.
