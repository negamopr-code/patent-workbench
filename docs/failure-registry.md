# Failure registry — assessment-integrity failure classes and their controls

Living registry of every measured failure class, its controlling agent, trigger,
and status. **Known-baseline governance:** an audit FAIL may be treated as KNOWN
(non-gating) ONLY if it has a dated, user-approved entry in the baselines block
below. `audit_status.py` receives that block via `--baselines` and treats any
unregistered FAIL — or growth over a registered count — as gating, regardless of
what any auditor's prose says. This closes the F1 escape (the 08-18 false t13
closure was computed while a FAIL was being waved through as "known baseline").

| # | Failure class (measured 2026-08-20) | Controlling agent | Deterministic backend | Trigger | Status |
|---|---|---|---|---|---|
| F1 | False corpus-wide closure claims over defective stores (canary showed 0/9 in the very store used) | ranking-integrity-auditor | audit_ranking C5-det | before ANY corpus-wide negative claim | control shipped |
| F2 | Feature re-decompose orphans name-keyed reads; top band not re-read (t13: 401 orphans; 13 sunk re-read 08-20 → verify C2 clean) | ranking-integrity-auditor | audit_ranking C1/C2/C6 | after rewording; before champion report | baseline below |
| F3a | Sweep batch-35 answer-budget competition (roster-35 → 0/9 vs roster-10 → 7/9, proven by repro) | recall-integrity-auditor | audit_recall R2 | after every sweep | fix: batch ≤12 corridor |
| F3b | No follow-up questions (per-doc follow-up = near-opus verdicts, free) | nlm-followup-verifier | nlm_followup.py + audit_recall R6 | non-empty R6 queue | fix: in-sweep follow-up stage |
| F3c | 120KB staging clip (t13: 392 truncated / 309 blind tails; t10: 689 / 0) | staging-completeness-auditor | audit_staging S1/S2 | after staging; before interpreting notebook answers | fix: multi-part staging |
| F3c-ns | **Not staged** (`documents.nlm_screen_state='add_failed'`: source never indexed in NotebookLM, doc silently dropped from the screen and counted as "rejected"; 2026-08-25: t10 173 / t11 40 / t12 47 / t13 13 / t14 37 = 310 docs, 138 oversized; mechanism candidates: 50-source-cap mid-doc rollover + ingestion timeout — under test, hypotheses.md H7) | staging-completeness-auditor (+ recall R1 labels GT docs in this pool "unstaged", not "miss") | audit_staging S5 (TO PATCH: scripts do not yet partition on `nlm_screen_state`) | after staging; before ANY reject-pool negative | registered 2026-08-25 (user approved); every recall/coverage line carries "among docs that reached NotebookLM" + the add_failed count; fix: re-queue add_failed instead of terminal mark |
| F3d | Verbatim canary ≠ recall control | recall-integrity-auditor | audit_recall R3/R4 | every sweep | paraphrased canaries NOT yet planted (standing WARN) |
| F3e | No post-sweep recall measurement (t13 0/14, t10 7/12 found only manually) | recall-integrity-auditor | audit_recall R1 | after every sweep, before results posted | control shipped |
| F4 | Rolling-notebook confound (answer interpreted after source rotation) | staging-completeness-auditor | audit_staging S3 | before interpreting ANY notebook answer | control shipped |
| F5 | Lane blind stripes (embed claims-only → champion rank 1778/2058; lexical desc[:20k]) | recall-integrity-auditor | audit_recall R5 | after every lane run | fix: lane windows |
| F6 | Claim-weight lists presented as relevance (claim-count non-predictive; top claimants ≤3.0 opus) | ranking-integrity-auditor | audit_ranking C7 | DONE messages, any claim-list report | fix: DONE corpus-top block |
| F7 | Meta: gates rely on session discipline | pipeline-integrity-supervisor | audit_status + verdict files + pending-trigger flag | post-sweep, pre-report, pre-closure, post-deploy, post-rewording, session start | residual without harness hook (user chose protocol+memory) |

Ops/infra failures (host wedges, stale locks, watcher re-arm, 8099 relay) are
OUT of scope here by user decision — covered by sweep-watcher + the recovery
recipe in `incident_crash_2026-08-18_container_stop_sweep.md`.

## Approved known baselines (machine-readable — the ONLY source audit_status accepts)

```json
{
  "13": {
    "C1-orphaned-reads": {"count": 401, "approved": "2026-08-18 user; 2026-08-20 accepted as scoped end-state (Claude recommendation, user 'go ahead') — all probes/lanes at marginal≈0 in the ≤2.0 orphan tail; re-read only on explicit user request. Closure wording stays 'among current-key reads'.", "scope": "pre-v2 feature_scores keyed to v1 names; top band + canary + sunk-13 re-read 08-20 (387 remain, all in the weak tail)"},
    "R2-batch-corridor": {"count": 119, "approved": "2026-08-20 user 'go ahead'", "scope": "the COMPLETED legacy v2 sweep ran roster-35 before the corridor doctrine — its results are discovery-only forever and every conclusion carries recall: 1/14; future sweeps run roster ≤12 + follow-up stage"},
    "S5-not-staged-add_failed": {"count": 13, "approved": "2026-08-25 user", "scope": "F3c-ns, see tab 10"},
    "R2-screen-roster39": {"count": 39, "approved": "2026-08-25 user", "scope": "see tab 10"}
  },
  "10": {
    "R2-batch-corridor": {"count": 59, "approved": "2026-08-20 user 'go ahead'", "scope": "the COMPLETED legacy sweep ran roster-35 — discovery-only forever, conclusions carry recall: 7/12; future sweeps run roster ≤12 + follow-up stage"},
    "S5-not-staged-add_failed": {"count": 173, "approved": "2026-08-25 user 'register F3c … approved'", "scope": "F3c-ns: docs never indexed in NotebookLM; NOT screened, NOT rejected — reported as a separate 'not staged' class; every recall/coverage line states the reached-NotebookLM denominator + this count; 7 of the 12 t10 GT docs (both 6.0 champions) are in this pool"},
    "R2-screen-roster39": {"count": 39, "approved": "2026-08-25 user 'register … the roster-39 baseline, approved'", "scope": "the v2 SCREEN stage runs roster 39 by design on every tab (t10–t14); it is a discovery/graduation pass, not the claims sweep — the ≤12 corridor (F3a) binds the claims/must-rounds and the follow-up stage only. Screen verdicts are discovery-only; per-doc relevance comes from follow-up or opus"}
  },
  "11": {
    "S5-not-staged-add_failed": {"count": 40, "approved": "2026-08-25 user", "scope": "F3c-ns, see tab 10"},
    "R2-screen-roster39": {"count": 39, "approved": "2026-08-25 user", "scope": "see tab 10"}
  },
  "12": {
    "S5-not-staged-add_failed": {"count": 47, "approved": "2026-08-25 user", "scope": "F3c-ns, see tab 10; 32 of these opus-read 08-24/25: 0 ≥3"},
    "R2-screen-roster39": {"count": 39, "approved": "2026-08-25 user", "scope": "see tab 10"}
  },
  "14": {
    "S5-not-staged-add_failed": {"count": 37, "approved": "2026-08-25 user", "scope": "F3c-ns, see tab 10; all 6 opus ≥4 non-graduates on t14 are in this pool"},
    "R2-screen-roster39": {"count": 39, "approved": "2026-08-25 user", "scope": "see tab 10"}
  }
}
```

## Verification log

- 2026-08-20: registry seeded with the session's measured numbers (see
  docs/nlm-mirror/discussion-journal.md entries of 2026-08-20 for the full
  experimental record: recall joins, roster-size repro, truncation census).
- 2026-08-20 (post-deploy d4d8691, pre-report t13/t10): supervisor run.
  All three verdicts FRESH on d4d8691 (staging/recall/ranking, /data/audits/
  21:27); no pending_trigger. Baselines: t13 C1 = 387 ≤ 401 registered (within
  baseline, shrinking — top-band re-read); t13 C4 RESOLVED (canary CN223926581
  now registers 9/9 full MUST under current keys, C5 PASS SCOPED, zero-full
  elements 0). Unregistered gating FAILs: t10 C6 15/15 top-band stale-keyed
  (no current-key deep reads — closure/champion forbidden until re-read);
  t10 R2 59/59 rounds roster>12; t13 R1 recall 1/14 (7%); t13 R2 119/119
  rounds roster>12. Disclosures: t13 S1 blind-tails 344 (grew from 309 as
  sweep staged more rounds — legitimate movement, not shrinkage anomaly);
  t10 S3 notebook 5e1c98dd PERMISSION_DENIED (sources unverifiable — do not
  interpret its answers until verified). R6 queues non-empty: t13 13 docs,
  t10 5 docs → nlm-followup-verifier. No cross-file contradictions found
  (t13 R1 misses are opus-read but sweep-unclaimed, coherent with C6 PASS).
  VERDICT: BLOCKED (t10 C6/R2; t13 R1/R2); t13 champion report would be
  PERMITTED+DISCLOSE, t13 closure SCOPED-ONLY.
- 2026-08-24 (session-start + pre-conclusion check, opus-parallel experiment;
  deploy head 76d3b09): supervisor run. audit_status: EVERY tab BLOCKED on all
  three gates. Verdict files are pre-campaign vintage — audit_ranking +
  audit_staging 08-20 21:45 on head 7322871 (STALE(deploy)), audit_recall
  08-21 tab-10-only (MISSING(tab) for 11-14), audit_full_staging 08-23 19:28
  (before the v2 re-screens launched; verdict PASS but assessed_truncated
  t11 188 / t12 319 / t13 458 / t14 300 = the re-screen scope, not yet
  re-measured). pending_trigger claims-audit-done t10 (08-23 19:14) never
  cleared — the post-sweep audits for the stopped v3 claims audit never ran.
  CONTROL GAP (F7-class): audit_recall/audit_staging/audit_ranking read only
  .nlm_claims_* state; the v2 .nlm_screen_* lanes (t10 1291 / t11 188 /
  t12 319 / t13 458 / t14 300, batch_size 39) are invisible to R1/R2/R3/R4/
  R6/S3 — no deterministic backend can currently gate the campaign or the
  opus-parallel conclusion. DOCTRINE CONFLICT: screens run roster 39 while the
  registered R2 corridor (t10/t13 baselines) says "future sweeps run roster
  ≤12 + follow-up stage"; the 08-23 v2 revert is user-approved in memory but
  NOT recorded here — needs a dated entry (screen-mode exemption or new
  corridor) before any screen result is admissible. Controls: t10 champion
  controls US20230337972 (queue pos 542), US20220221016 (1131) and canary
  EP3849091 (743) not yet reached (cursor 468); t12 has ZERO registered
  controls in its re-screen queue (recall unmeasurable), t14 only
  CN103457003 (survived), t11 none, t13 CN115166523 (pos 1) + CN116508192
  (pos 110). Journal "t10 GT-recall 9/12" exists in no verdict file. Opus
  reads today: t10 205 / t12 38 / t14 59 (claude-opus-5), deep_map verified
  blind (title/abstract/claims/description only). Account rules: bindings
  intact (t10 drawnformula, t11/t13 default, t12/t14 work2); but
  nlm-followup-verifier t10 runs on drawnformula concurrently with the t10
  priority screen, and t11 re-screen shares default with t13 (08-22
  exclusivity/HELD rule; default has answered 0/646 in 24 h) — both need
  explicit user confirmation or stop. Opus coverage driver (pid live, batch
  150, poll 600 s) will keep max_scored_at moving → all audits STALE until it
  is paused; followup_ledger.jsonl unchanged since 08-20 (verifier output not
  yet landed). VERDICT: BLOCKED — experiment conclusion inadmissible until
  screens are auditable and the corridor conflict is registered.
- 2026-08-25 (session-start, pre hypothesis-driver cycle 2; deploy head
  76d3b09): supervisor run. Gate matrix ALL RED, every tab, every gate.
  Freshness t10 staging/recall/ranking STALE; t12 staging STALE (32-read
  add_failed batch moved max_scored_at to 08-25 00:59), recall MISSING,
  ranking STALE; t11/t13/t14 staging FRESH, recall MISSING(tab), ranking
  STALE / STALE(deploy). Pending trigger t10 claims-audit-done (08-23 19:14)
  still unresolved — recall (08-21) and ranking (08-20, head 7322871) verdicts
  predate it. audit_full_staging.json 08-24 21:44 = FAIL (t14 graduates
  CN103457003/CN110679056/JP2020036393 confirmed assessed-truncated 1/N
  parts; live blind tails t11 JP7332073, t12 KR20150138127, t13 x3) and now
  stale vs t12/t10 watermarks. Baselines: t13 C1 387 <= 401 registered OK;
  t10 R2 recall verdict PASS covers only the since_ts window — screen rounds
  at roster 39 remain UNREGISTERED vs the corridor baseline (08-24 finding,
  still no dated entry). add_failed pools (DB, nlm_screen_state): t10 173,
  t11 40, t12 47, t13 13, t14 37 = 310 docs. NO audit script distinguishes
  add_failed: audit_staging pools status='fetched' (add_failed >120 KB land
  in S1 blind-tails as if staged), audit_recall counts an add_failed GT doc
  as a plain miss, audit_ranking ignores state, audit_full_staging merely
  excludes add_failed from assessed-truncated. Failure class F3c ("not
  staged, never indexed") is UNREGISTERED and UNGATED — the 310 docs are
  silently folded into "screened, not claimed". t12 H7 outcome (32/32 opus
  0 >=3, max 1.0) is discovery only. H8: 5/12 t10 GT docs re-read 1-2 under
  08-18 features — the recall verdict's R1 12/12 was computed on a GT set
  that has since changed; blind re-read of the 7 remaining + US20230337972
  overwrites documents.score = the R1 GT set itself; prior scores must be
  snapshotted in the ledger before launch. VERDICT: BLOCKED — cycle-2 opus
  reads may proceed (discovery), no conclusion reportable until all
  auditors re-run on 76d3b09 and F3c is registered.
- 2026-08-25 (session, deploy head 76d3b09, HEAD 85b824b) — REGISTRY UPDATE, user
  approved verbatim: "register F3c and the roster-39 baseline, approved".
  (1) New class F3c-ns "not staged" (`nlm_screen_state='add_failed'`) with per-tab
  baselines `S5-not-staged-add_failed` t10 173 / t11 40 / t12 47 / t13 13 / t14 37.
  Scripts still emit no S5 row (audit_staging/audit_recall/audit_ranking do not
  partition on the flag) — the baseline is documentary until they are patched;
  until then every recall/coverage line must hand-state the reached-NotebookLM
  denominator + the add_failed count. (2) `R2-screen-roster39` = 39 on t10–t14:
  the v2 screen runs roster 39 by design; the ≤12 corridor binds claims/must-rounds
  and follow-up only. NOT registered (not approved): the unregistered nlm_claims R2
  counts t11 46/78, t12 61/118, t14 80/139; t7 EP3282551 rank=null; t3 C6 gaps;
  t11 R1 89/93 (4 judged misses) — all still gating.
- 2026-08-25 (post hypothesis-driver cycle 2, pre-thesis report; deploy head
  76d3b09, HEAD b3762f1): supervisor run. audit_status consumes the fc6e576
  baselines syntactically, but NO verdict row carries `S5-not-staged-add_failed`
  or `R2-screen-roster39` (staging/recall scripts 2026-08-20.1 emit S1–S4 /
  R1–R6 only) — both baselines are inert/documentary; t12/t14 FRESH gates are
  unchanged by them. Live add_failed = registered exactly (t10 173 / t11 40 /
  t12 47 / t13 13 / t14 37, no growth). Freshness: t10/t11/t13 staging+recall+
  ranking ALL STALE — verdicts written 05:58:42–05:59:04 UTC, driver reads
  landed 05:59–06:07 (max_scored_at t10 06:02:43 +14 docs, t11 06:06:59 +31,
  t13 06:01:03 +7); ranking verdict also has deploy_head=null (run without
  --deploy-head). t12/t14 FRESH: post_sweep BLOCKED (R2-batch-corridor
  unregistered t12 61/118, t14 80/139), champion PERMITTED+DISCLOSE (S1),
  closure SCOPED-ONLY. Pending trigger t10 claims-audit-done (08-23 19:14)
  STILL unresolved after two audit cycles. audit_full_staging FAIL (05:59):
  live blind tails t11 JP7332073, t12 KR20150138127, t13 CN115166523/
  CN116130803/WO2024077056, t14 x3; assessed-truncated t11 110 / t12 248 /
  t13 445 / t14 222 — now stale vs t10/t11/t13 watermarks too. Cross-file:
  t10 recall R1 "12/12" was computed on the pre-overwrite GT; the H8 re-read
  changed the live GT set to score>=4 = 9 docs (5 of them add_failed:
  US20230337972, US20220221016, US10996236, EP3849091, CA2552849) — the R1 row
  is invalid by construction, not merely stale. t13 R1 5/14 FAIL (msg lists 6
  missed, data lists 9 — script inconsistency) and R6 queue t11 x4 / t13 x3
  still WARN in the verdict although followup_ledger.jsonl shows both queues
  worked 06:04/06:09 — recall must re-run to consume them. t11 full_staging
  assessed_truncated=110 vs staging S1 truncated=186 — different denominators,
  not a contradiction. Thesis admissibility: H7 mechanism (cap-overflow
  4/4…9/11) rests on screen-state files + code inspection, no auditor
  instrument measures it — discovery only; H8 (valid GT 4/12) and H9/H7-cost
  (t10 5/9 champions add_failed) are DB-verifiable and match live state but
  their recall lines are unaudited until recall re-runs on the new GT; H10
  (v2 judged-miss 0/114) is a partition of the STALE recall verdict's misses
  (t11 4 / t13 9 all v1-epoch) — reportable only as "among v2 rejects opus-read
  so far, t11/t13 screens stalled at round 0, roster-39 discovery pass".
  VERDICT: BLOCKED — spawn recall-integrity-auditor, ranking-integrity-auditor
  (with --deploy-head), staging-completeness-auditor, full-doc-staging-auditor
  on t10/t11/t13; thesis may be circulated only as discovery with the F3c-ns
  denominator wording and no closure/coverage claim.
- 2026-08-25 07:05 UTC — DEPLOY 196f40e (src/patentbench/web/api.py `_screen_fill_roster`):
  cap-aware screen roster fill — the F3c-ns root cause (parts rolling over the
  50-source cap → tail parts dropped → index probe fails → add_failed). Every
  verdict in /data/audits is now STALE(deploy); staging-completeness +
  full-doc-staging auditors to re-run after the first cap-aware rounds on
  t11/t13 (resumed 07:08; first rosters 23 and 24 docs vs 39). Expected
  observable: zero new add_failed and screen notebooks at parts have/expected
  equal. Existing add_failed docs remain terminal (re-queue NOT built yet).
- 2026-08-25 07:55 UTC — DEPLOY 911d1b3: add_failed re-queue (one extra pass per doc,
  tracked in screen state `requeued`; on resume, default start, per round).
  scripts/audit_staging.py 2026-08-25.1: emits `S5-not-staged-add_failed` (gates
  against the baselines above) and part-aware S1 (blind tails = upper bound;
  docs verified full in live notebooks excluded). All five screens POST-resumed
  07:57 on 911d1b3: t10 r12 468/1459, t11 r7 100/188, t12 r1 39/334, t13 r7
  106/458, t14 r2 78/337 (totals grew by the re-queued add_failed docs). Live
  S5: t11 0 / t13 0 (their 08-23 round-0 failures were re-screened today) —
  baselines 40/13 stay as maxima. Deploy head for all verdicts: 911d1b3.
- 2026-08-25 ~11:51 UTC (session-start, after the 09:54 container restart;
  scope t10–t14 only): supervisor run. Step 0 audit_accounts PASS (A1 all five
  bindings match; A2 no running NLM job; /data/audits/audit_accounts.json
  11:49). audit_status on 911d1b3: t10 staging/recall/ranking STALE (data
  watermark: documents t10 n=2136 vs anchor 2049, max_scored_at moved);
  t11–t14 STALE(deploy) — recall/ranking verdicts still carry 76d3b09
  (06:20/06:21), staging 196f40e (08:46); nothing has been re-run on 911d1b3.
  pending_trigger claims-audit-done t10 (08-23 19:14) still unconsumed.
  audit_full_staging.json 07:16 = FAIL (t12 KR20150138127 1/5 parts; t14
  CN103457003 1/3, CN110679056 1/2, JP2020036393 1/2; assessed_truncated
  t11 110 / t12 248 / t13 421 / t14 222). Baseline check: S5 t10 173 / t12
  47 / t14 37 = registered maxima; t13 C1 387 ≤ 401 KNOWN; t10/t13 R2 59/119
  registered. UNREGISTERED gating FAILs: S1-blind-tails t11 66 / t12 144 /
  t13 327 / t14 165; R1-recall t13 5/14; R2-batch-corridor t11 46 / t12 61 /
  t14 80; R5-lane-controls t10 (lexical lane); full-staging FAIL above.
  Cross-check: no verdict-vs-verdict contradiction found (R1 t10 9/9 vs C6
  t10 PASS consistent; S1 t10 blind=0 consistent with 757 truncated-but-read).
  Live state: screens t10 r14 / t12 r3 / t13 r8 died mid-round at 09:54
  (step=round, no lock; t11 r8 / t14 r3 paused per series rule). A t10 opus
  read batch IS ALREADY RUNNING: /data/.claude_read_10.lock started 11:49
  (50 ids, claude-opus-5) + 4 live `claude -p` processes since 11:50 — the
  caller must NOT launch a second batch. Verdict: BLOCKED for any reporting;
  screen resumes t10/t12/t13 cleared by the account gate only (default
  t13 first, work2 t12 first; t11/t14 stay paused).
- 2026-08-25 12:07:57 UTC — INCIDENT (F7-class, tooling): the new nlm-slot-manager watchdog
  restarted patent-bench because its "0 jobs running" probe set timed out and was counted
  as idle (fail-open). Killed t10/t12/t13 screens (auto-resumed by the same watchdog at
  12:09–12:10, rounds 16/5/10) and the in-flight t12 (86/99) / t13 (57/113) opus read
  batches (leftovers relaunched 12:14: t12 4, t13 50). Fix deployed in the slot manager:
  fail-closed job check (any unanswered probe → no restart), 2-cycle relay confirmation,
  serial probes with 30 s timeout, nlm-rate/status (17 s DB query) sampled once per 10 min.
  Root cause of the slow probes: nlm_bridge._run serialises EVERY NLM CLI call app-wide
  under one threading.Lock — three screens share one CLI pipe regardless of account (the
  11-min "waiting for ingest" on t12 was lock queueing, not a stall). Candidate fix: a
  per-profile lock (src change → deploy → needs a quiet window).
- 2026-08-25 ~17:00 UTC (post-deploy defa67e dedupe-rotation; cycle-3 b2ffa13):
  supervisor run. Step 0 account gate PASS (A1 t10 drawnformula / t11,t13 default /
  t12,t14 work2; A2 one job per account: t10, t12, t13 nlm-screen running). Verdict
  files 16:49–16:51 all on 911d1b3 → STALE(deploy) for t10/t11/t12/t14; t13 STALE by
  data (t13_b2 opus leftovers + screen rotation in flight — legitimate). Gate matrix:
  every gate red on every tab. Within baselines: S5 t10 172≤173 / t12 44≤47 / t14
  37=37 (t11, t13 no S5 row); t13 C1 344≤401; t10/t13 R2 59/119 registered. UNREGISTERED
  gating FAILs: S1-blind-tails t11 52 / t12 113 / t13 267 / t14 166 (t14 GREW 165→166,
  unexplained); R1-recall t13 7/20 (GT grew 14→20 = the six H8 crossers; F6: R1 counts
  must-followup hits as sweep hits — split pending); R2-batch-corridor t11 46 / t12 61 /
  t14 80; R6 queue t10 [US20120007441, CN103683526] + t13 [CN218958581, CN220510820,
  CN120073105, CN120433348] (= H8 crossers, consistent); full-doc FAIL: t12
  WO2025044604 parts [1]/2, t13 US10115302 parts [1]/2 (ghost-duplicate root cause,
  fix defa67e, not yet evidenced). No C5 canary on t10–t14 → corpus-wide negatives
  scoped only. Cross-checks: no contradictions between verdict files; thesis_2026-08-25.md
  still says "H5 SUPPORTED" while hypotheses.md cycle 3 says REFUTED — thesis not
  updated. Stale pending_trigger (t10 claims-audit-done 08-23 19:14) never cleared.
  Verdict: BLOCKED.
- 2026-08-25 ~17:11 UTC container clock (session-start after the 17:02:36 Docker-wide
  restart; watchdog auto-resumed t10/t12/t13 screens 17:02:49; deploy head defa67e):
  supervisor run, scope t10–t14. Step 0 account gate PASS (A1 all five bindings match;
  A2 one job per account: drawnformula t10, default t13, work2 t12; t11/t14 paused in
  series). No verdict file written since 16:49–16:51 (head 911d1b3) → nothing changed
  on the evidence side since the 17:00 entry; pending_trigger now consumed (file
  renamed .consumed_2026-08-25, t10 claims-audit-done 08-23) — resolved. Freshness:
  t12/t14 STALE(deploy) only (watermarks identical to anchors: max_scored_at, fetched,
  claims ts/rounds, benchmark updated_at all equal) → a plain re-run on defa67e
  refreshes them; t10/t11/t13 STALE by DATA — opus graduate reads landed after the
  anchors (t10 36, t11 19, t13 28 docs, t13 last at 17:10:43 = still landing) and
  of the 62 relaunched pending ids (t10 11 / t11 5 / t13 46) 33 already carry a
  post-anchor score (t10 11/11, t11 5/5, t13 17/46; 0 ≥4 so far), 29 t13 still
  in flight → re-audit only after the batch completes, else the verdicts go stale
  again in minutes. Gate matrix: all three gates red on every tab t10–t14. Baseline compare
  (verdict rows, not prose): WITHIN — t10 S5 172≤173, t12 S5 44≤47, t14 S5 37=37,
  t13 C1 344≤401, t10 R2-corridor 59=59, t13 R2-corridor 119=119. UNREGISTERED gating
  FAILs (unchanged from 17:00): S1-blind-tails t11 52 / t12 113 / t13 267 / t14 166;
  R1-recall t13 7/20; R2-batch-corridor t11 46 / t12 61 / t14 80; full-doc FAIL
  (audit_full_staging 16:51, epoch pre-defa67e) t12 WO2025044604 parts [1]/2, t13
  US10115302 parts [1]/2 — defa67e fix still unevidenced. R6 queues non-empty: t10
  [US20120007441, CN103683526], t13 [CN218958581, CN220510820, CN120073105,
  CN120433348]. C5: canary only on t13 (SCOPED); t10/t11/t12/t14 no verbatim canary →
  corpus-wide negatives scoped only. Cross-checks: S5 rows t11/t13 "no add_failed"
  agree with live nlm_screen_state (t11/t13 pools now empty) — registered S5 baselines
  t11 40 / t13 13 are over-registered, retire on next user approval; no
  contradiction between verdict files; the t14 S1 165→166 growth noted at 17:00 is
  not reconstructible (history.jsonl stores worst-only) — re-run will settle it.
  Caller-report "53 landed" vs DB 83 docs scored after the anchors = 53 + the 33
  relaunched ones already landed (consistent); caller's "17:12 relaunch" vs
  container clock 17:10 at audit time — timestamps from different clocks. Verdict: BLOCKED.
- 2026-08-25 17:13:19 UTC — SECOND Docker-wide restart of the evening (all 30 containers
  started the same second). Cause: openday-serve rendering TWO whole-period videos at once
  (SPY_sl0.25_tps0.5 + tps2 progress files written 17:12:53); its 3g cap did not protect the
  10 GB VM with ~5.3 GB baseline. Mitigation: `docker update --memory 2g --memory-swap 2g
  openday-serve` + serve.sh patched to 2g. Effects: the 17:12 graduate relaunch cut again
  (5/62 landed, all <4); 17:16 relaunched 57 (t10 11 / t11 5 / t13 41, ids in
  /data/audits/graduate_reads_pending2_2026-08-25.json). Watchdog auto-resumed t10/t12/t13
  screens 17:13:xx (t11/t14 paused); account gate A1/A2 PASS 17:15 (default t13, drawnformula
  t10, work2 t12). Deploy head defa67e unchanged → all verdict freshness unchanged (red).
- 2026-08-25 ~17:18 UTC container clock (session-start after the 17:13:19 Docker-wide
  restart #2; deploy head defa67e unchanged): supervisor run, scope t10–t14. Step 0
  account gate PASS (A1 five bindings match; A2 one job per account: drawnformula t10,
  default t13, work2 t12; t11/t14 paused, series order intact). Restart did NOT corrupt
  screen state: .nlm_screen_{t}.json/.lock all present, rounds monotonic vs earlier
  entries (t10 r14→r16→now r20 "staging round 21" lock 29 s old, cursor 671/1459;
  t12 r3→r5→now r7 cursor 88/363, unmatched 26, requeued 44 = live add_failed 44;
  t13 r8→r10→now r13 cursor 187/458; t11 r8 / t14 r3 still "⏸ paused"). t12/t13 locks
  17:13:32 (age ~4 min, TTL 1200 s) both "waiting for NotebookLM to ingest the batch" —
  not yet a stall, re-check if no progress by ~17:35. Screen notebooks unchanged (t12
  460a3ffe, t13 22f46fa9 = the ones named by audit_full_staging live_problems); claims
  jobs all step=done. Evidence side unchanged: audit_staging/recall/ranking ts 16:49
  head 911d1b3, audit_full_staging 16:51 — all pre-defa67e; pending_trigger none.
  Freshness: t12/t14 STALE(deploy) only; t10/t11/t13 STALE by DATA (reads landing).
  57-read relaunch (17:16, pending2 file) landing: t10 7/11, t11 3/5, t13 12/41 = 22/57
  scored after 17:16, 0 ≥4, rest in flight → re-audit ONLY after the batch completes.
  Gate matrix: all three gates red on every tab t10–t14. Baseline compare (verdict rows):
  WITHIN — t10 S5 172≤173, t12 S5 44≤47, t14 S5 37=37, t13 C1 344≤401, t10 R2 59=59,
  t13 R2 119=119. UNREGISTERED gating FAILs (unchanged): S1-blind-tails t11 52 / t12 113 /
  t13 267 / t14 166; R1-recall t13 7/20; R2-batch-corridor t11 46 / t12 61 / t14 80;
  full-doc FAIL t12 WO2025044604 [1]/2, t13 US10115302 [1]/2 (defa67e fix unevidenced
  until full-doc-staging-auditor re-runs on defa67e). R6 queues non-empty: t10
  [US20120007441, CN103683526], t13 [CN218958581, CN220510820, CN120073105, CN120433348].
  C5: canary only t13 (SCOPED, 9/9); t10/t11/t12/t14 none → negatives scoped only.
  Cross-checks: no contradiction — t13 C6 PASS vs R1 misses is consistent (all six misses
  hold opus reads 4.0–5.0, i.e. sweep misses of READ ground truth, not unread); live S5
  counts equal verdict rows on t10/t12/t14; t11/t13 no add_failed live (registered S5
  t11 40 / t13 13 remain over-registered — retire on user approval). Verdict: BLOCKED.
- 2026-08-26 ~14:55 UTC container clock (session-start after the 14:35 Docker-wide restart;
  watchdog auto-resume 14:36 died "could not list notebook sources", re-resumed 14:51):
  ACCOUNT GATE PASS 14:53 (A1 all five bindings match registry; A2 drawnformula t10 /
  default t13 / work2 t12 one job each; t11/t14 paused, series order intact). Screen state
  intact after restart: t10 r28 cursor 846/1459 requeued 168, t12 r18→"staging round 19
  3/11 added" cursor 225/363 unmatched 26 requeued 44, t13 r23 cursor 319/458; locks
  14:51–14:54 fresh; t11 r8 / t14 r3 "⏸ paused". Live add_failed: t10 170 (≤173), t12 44
  (≤47), t14 37 (=37), t11/t13 0 — WITHIN. Graduate batch EVIDENCED in DB (not journal):
  manifest ids t10 36 / t11 19 / t13 60 = 115 all scored 17:03–17:22 08-25, max score 3.0,
  0 ≥4. Evidence files: audit_recall 17:27:52 + audit_staging 17:28:18 (both defa67e,
  anchors == live on t10–t14 → audit_status prints FRESH); audit_ranking 16:49 head 911d1b3
  → STALE(deploy) t12/t14 and STALE-by-data t10/t11/t13 (115 reads landed after it);
  audit_full_staging 16:51, pre-defa67e, FAIL live_problems t12 WO2025044604 [1]/2 +
  t13 US10115302 [1]/2 — the defa67e fix is STILL unevidenced. pending_trigger none.
  WATERMARK GAP (named, not overridden): live_anchors() reads benchmark/scored_at/doc
  count/nlm_claims only; the screen writes documents.nlm_screened_at/nlm_screen_state
  and .nlm_screen_{t}.json, which no anchor covers. Since the 17:27 recall run: t10 185 /
  t12 134 / t13 129 docs newly screened (max nlm_screened_at 20:41/21:03/20:54 08-25) and
  rosters rotated → staging S3-live-inventory and the graduate sets are NOT what the
  FRESH label implies. Supervisor treats staging as STALE-by-screen on t10/t12/t13 until
  re-run after the screens finish; recommend adding max(nlm_screened_at) to
  live_anchors in scripts/audit_status.py (src/scripts edit = caller, not supervisor).
  Gate matrix (audit_status, defa67e): post_sweep_results t10/t13 PERMITTED (nominal —
  see gap), t11/t12/t14 BLOCKED R2-batch-corridor (unregistered 46/61/80);
  champion_report + closure_claim BLOCKED on all five (ranking stale). Baseline compare:
  t10 R2 59=59, t13 R2 119=119, t13 C1 344≤401, S5 within (above). UNREGISTERED gating
  FAILs unchanged: S1-blind-tails t11 52 / t12 115 / t13 219 / t14 166; R1 t13 7/20;
  R2 t11/t12/t14. Cross-checks: t13 S1 267→219 between 16:49 and 17:28 is consistent
  with the 60 t13 reads landing (truncated docs gaining a deep read); t12 S1 113→115 in
  the same window with ZERO new t12 reads (max scored_at 12:12 08-25) and no fetch
  change (1824) is UNEXPLAINED — staging auditor must name the two docs that joined on
  its next run (if it is screen-staged truncation it is an F-class regression of the
  08-23 multi-part invariant). t13 C6 PASS vs R1 misses still consistent (misses are
  READ GT). R6 queues non-empty: t10 [US20120007441, CN103683526], t13 [CN218958581,
  CN220510820, CN120073105, CN120433348] — nlm-followup-verifier is an NLM job on
  drawnformula/default, A2-blocked while those screens run. C5: t13 SCOPED (9/9),
  others none → negatives scoped only. Verdict: BLOCKED.
