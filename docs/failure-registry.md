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
- 2026-08-26 ~19:45 UTC container clock (session-start after the 14:35 + ~15:42 Docker-wide
  restarts; deploy head 7874470 docs-only, code head 38492c8 = defa67e app + new screen anchor
  in the 4 audit scripts): ACCOUNT GATE PASS 19:38 (A1 five bindings; A2 one job each on
  drawnformula t10 / default t13 / work2 t12; t11 r8 / t14 r3 "⏸ paused", error null, stop
  false). EVIDENCE: audit_staging 08-25 17:28 + audit_recall 08-25 17:27 + audit_ranking
  08-26 14:57 (all defa67e) — ALL THREE STALE on t10–t14: (a) written before 38492c8, so no
  max_screened_at/screened_docs anchor is recorded (the 14:55 watermark gap, now closed in
  code, is what flips them); (b) real drift: reads landed 15:07–15:09 on t10/t11/t12/t13
  (job 26 + graduate batch, all BEFORE the user pause 16:20/16:25 — no read after it),
  screens rotated (max nlm_screened_at t10 19:24 / t12 19:36 / t13 19:18; t10 1033 screened,
  t12/t13 fully screened once, requeues in flight). audit_full_staging 08-25 16:51
  pre-defa67e FAIL unchanged → every "assessed in full"/coverage claim stays gated.
  pending_trigger none. Gate matrix (audit_status 38492c8): post_sweep_results,
  champion_report, closure_claim BLOCKED on all five. Baselines: add_failed t10 170 ≤173,
  t12 31 ≤47 (117 t12 docs re-screened today — add_failed requeue works), t14 37 = 37,
  t11/t13 0; R2 t10 59 = 59, t13 119 = 119 (no claims round since 08-25, max_claims_ts
  unchanged); C1 t13 330 ≤401. UNREGISTERED gating FAILs unchanged: S1-blind-tails t11 52 /
  t12 115 / t13 219 / t14 166 (08-25 numbers, to be re-measured); R1 t13 7/20; R2 t11 46 /
  t12 61 / t14 80; the t12 S1 113→115 anomaly of 08-25 is still unexplained (staging not
  re-run). ORPHANED STATE after the two restarts: none active — locks .nlm_screen_{10,12,13}
  0–6 min old = the three running screens, no lock for t11/t14; processes = 2 gunicorn +
  one nlm CLI child of a screen; opus driver stop-flag 08-24, no deep-compare in flight (max
  scored_at 15:09 < pause). Latent: /data/.auto_refetch.lock created 15:43 by the boot sweep
  (O_EXCL, never removed in api.py) → the NEXT restart's auto-refetch will silently skip;
  harmless now (no 'pending' docs on t10–t14; the 87/150/90/132/162 'error' docs are the
  standing fetch-failed set). DRIVER CYCLE-4 FLAG: sync-nlm-mirror wrote 1 source to
  35690175-d37d-4de8-ac92-8254017063b5 (mirror notebook) at ~15:07 while t13 screened on
  default; t13's rolling notebook is 22f46fa9-dc19-495a-b505-6934f15dfd35 — a different
  notebook, so NO content contamination; the t13 round in flight (15:01→15:28) landed 14
  docs (13 graduate / 1 rejected, unmatched unchanged ['LM7805'], requeued 0) — no add
  loss observed. Classified as an A2 process breach (second write on a screening account =
  the F3c timed-out-add mechanism) with no measured data effect; sync-nlm-mirror must go
  through audit_accounts.py like any NLM write (caller's script edit). Note: tabs.nlm_profile
  is NULL for t11/t13 and audit_accounts reads NULL as 'default' — consistent with the
  registry, recorded for the record. AUDITORS DUE: ranking-integrity-auditor +
  recall-integrity-auditor now (DB-only; t11/t14 will stay FRESH, t10/t12/t13 re-stale as
  the screens rotate — say so); staging-completeness-auditor now for t11/t14 (paused,
  static) and for t10/t12/t13 only after their screens finish (S3-live-inventory reads the
  rotating notebook); full-doc-staging-auditor after the screens finish;
  nlm-followup-verifier (R6 t10 [US20120007441, CN103683526], t13 [CN218958581,
  CN220510820, CN120073105, CN120433348]) = NLM job on drawnformula/default → A2-blocked
  while t10/t13 screen AND user-blocked (no NLM jobs this session). C5: t13 SCOPED (9/9),
  others none → negatives scoped only. Verdict: BLOCKED.
- 2026-08-26 ~21:10 UTC container clock (session-start after the 20:57:01 Docker-wide
  restart #5; git HEAD 72e0235 NOT deployed, container still defa67e — no rebuild):
  supervisor run. Step 0 audit_accounts 21:02 PASS (A1 t10 drawnformula / t11,t13 default /
  t12,t14 work2; A2 one NLM job per account: t10, t12, t13 nlm-screen). Restart forensics:
  watchdog POSTed exactly ONE nlm-screen resume per tab (t10 20:57:17, t13 :18, t12 :18) —
  no double-resume; screen locks single; t13 accounting closes exactly (queue 458 = ledger
  392 + 66 DB-rejected; survivors+roster inside the ledger, all 12 marked graduate 21:01:18
  by the finalize step — "staging round 35: 3/37" is finalize, not a lost round); t10
  round 39 staging (roster 21, requeued 168 ≤ S5 baseline 173), t12 round 28 asking (roster
  8, requeued 44); no in-flight round lost its source list. /data/.auto_refetch.lock was
  present with mtime 20:58:04 = THIS boot's sweep (startup 20:57:05 + 60 s), i.e. the boot
  sweep DID run and the lock was not stale; because the 72e0235 fix is undeployed it would
  have silenced the NEXT boot's sweep, so the supervisor removed it (rm) at 21:03 — the
  entrypoint's "cleared stale background-job locks" does not cover this file (code-fix
  candidate: add it there; WEB_CONCURRENCY=1 makes the O_EXCL sibling rationale moot).
  Deep-compare: t12 (57 ids) and t13 (44 ids) opus-5 reads are RUNNING since 21:03:34/37
  (POST from 127.0.0.1) — NOT orphans: user 21:08 (session-log time) "resume the 101
  graduate reads", relaunched from the manifest; ids == graduate_reads_remaining, 0
  duplicates, 0 already opus-scored before launch; 15/16 landed by 21:06. These reads are
  in flight → t12/t13 recall/ranking watermarks will move legitimately. Freshness vs
  defa67e: t10/t13 staging+recall+ranking STALE (data: t10 screened 19:52 after the 19:51
  verdicts; t13 12 docs screened 21:01 + finalize; opus scores landing); t11/t12/t14
  recall+ranking FRESH; staging STALE(deploy) on t11/t12/t14 is a LABEL mismatch (verdict
  records 38492c8, container is defa67e, procedure HEAD is 72e0235; no audit script changed
  since 38492c8) — not overridden, disclosed; recall/ranking record deploy_head=None and are
  exempt from that check (inconsistency = code-fix candidate: one head label sourced from
  the running container). full-doc-staging verdict still 08-25 16:51 → STALE. No pending
  trigger. Cross-checks: t13 C6 PASS vs R1 FAIL 7/21 is NOT a contradiction (R1 misses are
  opus-read GT by definition); S1 blind shrink t11 52→40 / t12 115→90 / t13 219→174 /
  t14 166→166 matches reads that landed (job 26 = 12 on t11; 62/52 graduate reads on
  t12/t13; none on t14) — no shrink-without-reads. Baselines: t13 C1 330≤401, R2 119≤119;
  t10 R2 59≤59; S5 t10 170≤173 / t12 31≤47 / t14 37≤37 / t11,t13 0 — no growth, no new
  unregistered class. Still-gating unregistered FAILs: S1 t11 40 / t12 90 / t13 174 / t14
  166; R1 t13 7/21; R2 t11 46 / t12 61 / t14 80; R5 t10 lexical lane. C5: t13 SCOPED (9/9
  canary), others no canary → negatives scoped only. R6 queue non-empty: t10
  [US20120007441, CN103683526], t13 [CN218958581, AU2022338850, CN220510820, CN120073105,
  CN120433348]. Heads-up: t13 screen is in finalize → default account frees → watchdog
  series rule will resume t11 r8 = a new NLM launch; run audit_accounts around it.
  Verdict: BLOCKED.
- **2026-08-26 21:20 UTC — supervisor session-start pass after WSL reboot #6 (~21:15; uptime 213 s
  at 21:19).** Gate `audit_accounts` PASS at 21:19 (A1 t10 drawnformula / t11,t13 default /
  t12,t14 work2; A2 exactly one NLM job per account: t13 / t10 / t12 screens; t11 r8 and t14
  r3 correctly parked per series rule). No double-resume: one `.nlm_screen_{10,12,13}.lock`
  each (21:17–21:18), screens report a single runner per tab (t10 r38 staging, t12 r28→29,
  t13 r34→35), no nlm_claims rows since boot (screens are staging, not finalizing).
  Stale `/data/.auto_refetch.lock` (0 B, 21:17 = this boot; 72e0235 still undeployed on
  defa67e) REMOVED as in the 21:02 pass. Deep-compare launches cross-checked against DB
  (`documents.score_model/scored_at`, boot ≈ 21:16:40): t12 lock 12 ids == remaining manifest,
  0 opus-scored before launch, 9 landed by 21:19, 3 in flight (6002/6003/6006); t13 lock 4 ids,
  0 pre-scored, 4/4 landed; batch A′ (t12 27 / t13 49) has ZERO overlap with the in-flight
  locks and its waiter is alive (pid 235), driver alive (pid 229) — no duplicate launches.
  Disclosure: the driver's t10 batch (100 ids) contains 92 docs already scored by
  claude-sonnet-4-6 (pre-boot) — that is the intended opus-5 re-read of graduates
  (skip_scored skips opus-scored only); 8 opus scores landed post-boot, all opus, none
  overwritten twice. Freshness vs procedure HEAD da0463f: t10/t12/t13 staging+recall+ranking
  STALE by DATA (opus scores landing 21:18–21:19 + screens staging) — legitimately moving,
  NOT overridden; t11/t14 recall+ranking FRESH, staging STALE(deploy) is still the label
  mismatch (verdict 38492c8 / container defa67e / HEAD da0463f; `git diff 38492c8..HEAD`
  touches only src/patentbench/web/api.py, no audit script) — disclosed, not overridden.
  full-doc-staging verdict still 08-25 16:51 → STALE (FAIL on t11–t14 assessed_truncated
  75/231/271/209). No pending trigger. Cross-checks unchanged from 21:02 (no new verdicts).
  Baselines: no growth, no new class; still-gating unregistered FAILs unchanged (S1 t11 40 /
  t12 90 / t13 174 / t14 166; R1 t13 7/21; R2 t11 46 / t12 61 / t14 80; R5 t10). R6 queue
  non-empty: t10 [US20120007441, CN103683526]; t13 [CN218958581, AU2022338850, CN220510820,
  CN120073105, CN120433348]. Re-run guidance: auditors on t10/t12/t13 should wait for the
  t12 3 in-flight + t10 100-batch opus reads to land (t10 driver batch ≈ 92 remaining) —
  re-running now only produces another STALE set. Verdict: BLOCKED.
- 2026-08-27 ~05:50 UTC container clock (supervisor pass after deploy 05:42 — container
  recreated from 17b74ab tree, src head da0463f; scripts head now 2d55ba7 = restage runner
  committed 05:43, copied to /data; /api/version does not exist (404), /api/health ok).
  Gate audit_accounts: A1 PASS all five bindings, A2 "no running NLM jobs" — BUT two NLM
  jobs ARE running (restage-blind-tails t11 pid 489 → nlm_followup default profile; t14
  pid 498 → nlm_followup work2 profile) that the gate cannot see (it reads the app's
  screen/claims status routes only). A2 verified by hand from /proc cmdlines: one runner
  per tab, no duplicates, default=t11 only, work2=t14 only, drawnformula free. Opus driver
  re-armed (pid 29) — .auto_refetch.lock + .claude_read_10/13/14.lock are its live reads
  (started 05:42:19–26), not stale. No pending_trigger. Freshness (audit_status on
  2d55ba7): t10–t14 staging/recall/ranking all STALE — staging (05:40, defa67e) already
  overtaken by 33/16/12 new reads on t10/t13/t14; recall+ranking (08-26 19:51) overtaken
  by 201/21/159/109/21 reads on t10/t11/t12/t13/t14. Cross-checks: S1 blind shrink
  (t11 40→30, t12 90→0, t13 174→82, t14 166→149) is backed by those reads — no
  contradiction; live nlm_screen_state add_failed = verdict S5 rows (15/27/26/24/27).
  Baselines: S5 t13 24 > registered 13 = GROWTH (gates; t11 27 ≤ 40, t10 15 ≤ 173,
  t12 26 ≤ 47, t14 27 = 37 ok); C1 t13 330 ≤ 401; R2 t10 59 / t13 119 = registered.
  Unregistered gating FAILs: S1 t11 30 / t13 82 / t14 149 (user chose restage over
  registering); R1 t13 7/21; R2 t11 46 / t12 61 / t14 80; R5 t10 lexical lane. R6 queue
  non-empty: t10 [US20120007441, CN103683526]; t13 [CN218958581, AU2022338850,
  CN220510820, CN120073105, CN120433348]. Restage evidence defect: nlm_followup emits
  the per-doc YES/PARTIAL/NO answers on stdout only and deletes the notebook (no
  --keep-notebook); restage-blind-tails captures stdout and DISCARDS it — only a ledger
  line (docs + deleted notebook id) survives, and audit_staging S1 never consults the
  ledger. As launched the restage cannot turn S1 green and leaves no auditable answer.
  F4: dedicated per-chunk notebooks, never the rolling one — no F4 confound. Open
  coverage question: t10 has 758 fetched docs with nlm_screen_state NULL (screened 1291
  of 2049) although the t10 screen is reported "phase done". audit_full_staging.json
  still 08-25 16:51 FAIL (assessed_truncated t11 75 / t12 231 / t13 271 / t14 209) —
  stale, gates any "assessed in full" claim. Verdict: BLOCKED.

### 2026-08-27 06:20 — caller follow-up on the post-deploy supervisor pass (17b74ab / scripts 2d55ba7)

- **Item 7 (restage evidence defect) FIXED and re-run.** `restage-blind-tails.py` now passes
  `--keep-notebook`, persists the full nlm_followup result JSON to `/data/audits/restage/t<tab>_<ts>.json`
  and appends one row per doc to `/data/audits/restage_ledger.jsonl` (`{tab, number, ts, notebook,
  answered, evidence}`). `audit_staging.py` S1 gained `restaged_full(tab)`: a doc with an ANSWERED
  ledger row leaves the blind pool and is counted in the new `restage_verified_full` data key.
  The 40 docs restaged before the fix (t11 30 = all 3 chunks, t14 10 = chunk 1) produced NO
  retrievable evidence (notebooks deleted, stdout discarded) → their progress files were reset and
  both tabs relaunched from zero at 06:18 (t11 30, t14 149). Quota cost of the redo is accepted.
- **Item 10 (t10 758 fetched docs with `nlm_screen_state` NULL) EXPLAINED — not a coverage gap.**
  All 758 were fetched 07-27…08-18, i.e. BEFORE the screen started 08-23 20:40, and all 758 already
  carry a deep-read score (748 opus-5, 10 opus-4-8, 87 no model recorded but scored; 4 of them ≥4.0).
  The screen queue (1459) is the unscored remainder, so the screen's "done" is consistent: 1459
  screened + 758 already deep-read + (2049 total) — no document is unassessed. Disclosure retained
  because the auditors' anchors count screened docs only.
- **Item 1 (A2 gate blind to restage runners) STANDS as a live risk.** `audit_accounts.py` sees only
  the app's screen/claims routes; the two `restage-blind-tails.py` runners (t11 → default,
  t14 → work2) are invisible to it. Until the gate learns to read `/data/.restage_t*.log` +
  `/proc`, nothing else may be launched on default (t13 claims, t13 R6 follow-up, sync-nlm-mirror)
  or work2 (t12) while they run — verified by hand via `/proc/*/cmdline` at 06:18: exactly one
  runner per tab, drawnformula free.

### 2026-08-27 ~07:10 UTC — recall-integrity audit, ALL tabs t10–t14 (deploy head 09f61a9)

Run: `docker exec -i -w /app patent-bench python3 - --json --deploy-head 09f61a9 --registry "$(cat
docs/controls-registry.json)" --lane-report /data/audits/lane_lexical_10.json < scripts/audit_recall.py`
→ `/data/audits/audit_recall.json`, worst **FAIL**. DB stable: max scored_at 05:48 UTC (opus
deep-compare parked by the app watchdog until 10:20 UTC); verdict holds for that window.

- **Measured claims-sweep recall vs opus ≥4.0 GT (canary excluded): t10 11/13 (85%) · t11 90/94 (96%) ·
  t12 14/14 (100%) · t13 7/21 (33%) FAIL · t14 60/60 (100%) — aggregate 182/202 (90%).** Moves since the
  08-26 19:51 verdict come from exactly 2 new GT docs out of 549 new opus reads (t11 CN117352927 4.0,
  t14 KR102760076 5.0, both claimed); t10/t12/t13 numerically unchanged.
- **t13 7/21 decomposed by round kind: `must` (the sweep) 2/21 = 10%; the other 5 hits came only from
  the `must-followup` instrument.** Only CN116508192 was found by must-rounds alone. Concrete F3a
  evidence: AU2022338850 (opus 4.0) sat in t13 round 33, roster 35, and the whole round's answer is
  `FEATURE 1: NONE … FEATURE 9: NONE` (143 chars for 35 docs). All 14 t13 misses were staged in
  roster-35 rounds — none is a staging gap; 0 never-staged on every tab.
- **The five overnight-FINISHED screens contribute ZERO rows to `nlm_claims`** (latest claims ts:
  t10 08-23 10:01, t11 08-15, t12 08-23, t13 08-23 17:32, t14 08-15). R1–R6 therefore measure the
  legacy claims sweep only — screen completion changes none of the recall numbers above. F7-class
  control gap first logged 2026-08-24, still open.
- **Screen-lane recall (auditor cross-check, GT among docs that reached NotebookLM): t10 7/9 · t11 68/93 ·
  t12 8/14 · t13 17/20 · t14 47/53 = 147/189 (78%); 42 opus ≥4 docs were explicitly REJECTED by a
  finished screen** — including the registered t12 champion control KR20260033205 (opus 8.0, screen
  `rejected`), t12 CN119833811 (5.0, rejected) and t10 EP3849091 (4.0, rejected); t13 CN116508192 (5.0)
  is `add_failed` (F3c-ns, never reached the notebook). Bias disclosure: opus reads every graduate, so
  this figure is optimistically biased and reject-pool reads are not a random sample. Hit rate among
  opus-read docs, graduate vs rejected: t10 1.5%/1.2%, t11 16.4%/15.3%, t12 1.9%/3.3%, t13 2.5%/1.0%,
  t14 11.7%/10.5% — on 4 of 5 tabs the graduate label shows no measurable lift.
- FAILs: R2-batch-corridor t10 59/216 (registered 59) · t11 46/78 · t12 61/118 · t13 119/168
  (registered 119) · t14 80/139 (t11/t12/t14 remain UNREGISTERED, unchanged counts); R1 t13 7/21;
  R5 t10 lexical lane (US20230337972 rank 1696/2041 vs max 200; CN113924787 1907 vs 500 — lane report
  08-20, stale). WARNs: R3 no verbatim canary on t10/t11/t12/t14 (corpus-wide negatives scoped only);
  R6 queues t10 [US20120007441, CN103683526] (both `nlm_screen_state` NULL = the pre-screen 758 cohort,
  so no screen can rescue them) and t13 [CN218958581, AU2022338850, CN220510820, CN120073105,
  CN120433348 (add_failed)]. R4 PASS on all five tabs — but t13's PASS rests on the follow-up stage
  (CN205265271 claimed by `must-followup` only).
- **Registry staleness (NEW, needs user approval to fix — not edited here):** the approved-baseline
  scope texts still quote "recall: 7/12" (t10) and "recall: 1/14" (t13); measured today they are 11/13
  and 7/21. R5 is unexercised on t11–t14 and for the embed lane everywhere (no lane reports exist);
  the audit silently skips tabs without one. Data hygiene: `followup_ledger.jsonl` gained a t14 row at
  06:22 with `"docs": []`.
- F6 hand-off: treating the finished screens as if they changed sweep coverage/clearance is
  discovery-presented-as-relevance → ranking-integrity-auditor C7, not judged here.
- Verdict: **VIOLATIONS** — no tab is cleared; t12/t14/t11 100%/100%/96% recall figures describe the
  legacy claims sweep only.

### 2026-08-27 ~07:06 UTC — staging-completeness audit, ALL tabs (deploy head 09f61a9, LIVE)

Run: `docker exec -i -w /app patent-bench python3 - --json --deploy-head 09f61a9 < scripts/audit_staging.py`
→ `/data/audits/audit_staging.json` (14 tabs, `no_live=false`, 55 rows), worst **FAIL**. DB stable:
max scored_at 05:48 UTC (opus deep-compare parked by the app watchdog until 10:20 UTC) and all five
screens finished — the verdict holds for that window only.

- **S1 blind tails (FAIL): t11 30 · t13 53 · t14 128 = 211 docs whose tails no instrument has ever
  seen.** t10 and t12 have 0 (S1 PASS). Independently recomputed from sqlite (`mode=ro`), identical to
  the script. Largest per tab: t11 `WO2023273660` 350 415 B (230 415 B = 65.8% lost, clip inside
  DESCRIPTION, first lost text `[0067] 在其中一实施方式中…`); t13 `WO2022264760` 322 909 B (62.8% lost,
  DESCRIPTION); t14 `WO2016098802` 329 640 B (63.6% lost, DESCRIPTION, lost from `[0051] 図４Ａ…`).
- **S1 MOVED vs the 05:40 run: t13 82 → 53 (−29), t14 149 → 128 (−21), t11 30 → 30.** The movement is
  real, not a live/no-live artefact (no-live recompute gives the same 30/53/128): all 50 docs that left
  the pool received a deep-read score at 05:43–05:48 UTC (e.g. t13 `CN121756950` 3.0, t14 `KR102760076`
  5.0). ⇒ `/data/audits/blind_t13.json` (82) and `blind_t14.json` (149) are STALE inputs — the running
  restage jobs would spend NLM quota on ~50 already-read docs.
- **S1 restage credit = 0 docs on every tab (`restage_verified_full: 0`).** `restage_ledger.jsonl` has
  10 rows, all t14, all `answered:false` (the chunk's consolidated query returned `QUOTA-ABORT`). No
  doc is currently credited out of the blind pool by the new `restaged_full()` path. Soundness gaps in
  the credit (NEW): (a) `answered` = the doc number appears as a substring of one consolidated NLM
  reply — no check that all K parts were ingested; `nlm_followup` sets a global `partial` flag on
  `add_source_text` failure and `restage-blind-tails` treats exit 1 (partial) as success; (b) the
  follow-up notebook is deleted immediately and the evidence JSON records no source inventory, so the
  credit is unfalsifiable afterwards; (c) `restage-blind-tails.py` marks a doc done in
  `restage_t14.progress.json` when its key exists in `answers` — `QUOTA-ABORT` counts, so
  `DE102017110483` is already excluded from retry while still blind.
- **t11's 30 blind tails were restaged at 05:46–06:09 (3 chunks, `followup_ledger.jsonl` notebooks
  347ffd01/f54128e3/17bd5a24) under the PRE-fix runner, which discarded the answers** — that ledger
  records doc numbers only and the notebooks were deleted. The audit's refusal to credit them is
  correct: no evidence content exists.
- **S5 add_failed (FAIL): t10 15 · t11 27 · t12 26 · t13 24 · t14 27 = 119 docs NotebookLM never saw.**
  Unchanged vs 05:42; registered baseline is 13 ⇒ still GROWTH, still gating.
- S1 WARN (truncated but deep-read): t10 792 · t11 110 · t12 253 · t13 340 · t14 116. `parts_verified_full`
  (live notebooks): t10 18 · t11 21 · t12 33 · t13 27 · t14 18 — these reduced the truncated count, none
  of them reduced a blind count. **NEW caveat: "full-length deep read" is not literal for 16 docs**
  (t12 15 / t13 1) whose abstract+claims+description exceeds `claude_bridge.MAX_FULLTEXT_CHARS = 400_000`
  (largest `KR20140094482` 410 852 chars, 2.7% unread); `deep_map` clips at that cap.
- S2 cut points: t10–t14 clip lands in DESCRIPTION for 1 797 of the 1 822 truncated docs on t10–t14 (DIGEST 5); CLAIMS for 20
  (t12 18, t13 2 — e.g. `KR20230036637` 717 334 B cut inside CLAIMS).
- **S3 live inventory PASS on all five tabs** (2 notebooks listed per tab, no listing failure at the
  ~100-notebook cap): t10 12 · t11 8 · t12 12 · t13 12 · t14 10 sources == last roster. F4 is clear for
  answers from the LAST round only; answers from any earlier round remain void. S4 PASS everywhere
  (t10 237/240, t11 1/8, t12 7/132, t13 20/120, t14 150/150).
- Hygiene (NEW, minor): `audit_staging.py` still reports `SCRIPT_VERSION = "2026-08-25.1"` after the
  08-27 restage feature, so `history.jsonl` cannot distinguish restage-aware runs; the previous
  `audit_staging.json` on disk (06:17) was a `--tab 11 --no-live` run, which degrades the supervisor's
  all-tab freshness view.
- Verdict: **VIOLATIONS** — no "assessed in full" claim is supportable on t11/t13/t14 (211 blind tails)
  and none on any of t10–t14 while 119 add_failed docs never reached NotebookLM.

### 2026-08-27 ~07:30 UTC — full-document staging audit (truncation NO-GO), t10–t14 (deploy head 09f61a9, LIVE)

Checker: `scripts/audit_full_staging.py`, five per-tab `--live` runs piped in via
`docker exec -i -w /app patent-bench python3 - --tab N --live`; results merged into
`/data/audits/audit_full_staging.json` (scope t10–t14 per the standing scope rule; t1–t9 not run).
DB stable: all five NLM screens are `step=done`, opus deep-compare reads parked by the token-limit
watchdog until 10:20 UTC — **this verdict holds for that window only.**

| tab | docs | oversized (>118 000 B) | assessed_truncated (pre-epoch) | live part-presence problems |
|-----|------|------------------------|--------------------------------|------------------------------|
| 10  | 2049 | 833 | 0 (was 0)   | 1 |
| 11  | 1954 | 188 | 0 (was 75)  | 1 |
| 12  | 1824 | 319 | 0 (was 231) | 0 |
| 13  | 2058 | 458 | 0 (was 271) | 2 |
| 14  | 1835 | 300 | 0 (was 209) | 1 |

- **assessed_truncated is now 0 on every tab** (08-25 16:51 baseline: 75/231/271/209 on t11–t14). The
  overnight screen rounds re-screened every oversized doc that carries a verdict, so no oversized doc's
  only verdict predates `FULL_STAGING_EPOCH` any more. **This clears the WARN gate but proves nothing
  about coverage**: a post-epoch timestamp only means the verdict is younger than the multi-part deploy,
  not that all K parts were live when the question ran (all five docs below are counter-examples).
- **VERDICT FAIL — 5 live blind tails at part granularity, all in the lane screen notebooks, all
  `graduate`, all screened POST-epoch:**
  - t10 `JP7753930` 261 974 B, K=3, present `[1,2]` — nb `50f703e7` (🔁 Screen — Exam_2_478_2026), screened 08-26 19:52
  - t11 `CN116345029` 123 137 B, K=2, present `[1]` — nb `23cd3326`, screened 08-26 23:59
  - t13 `CN115552761` 246 638 B, K=3, present `[1,2]` — nb `22f46fa9`, screened 08-25 20:54
  - t13 `CN119153812` 145 090 B, K=2, present `[1]` — nb `22f46fa9`, screened 08-26 16:22 (score NULL ⇒ no deep read either: tail seen by NO instrument)
  - t14 `JP2023095746` 274 948 B, K=3, present `[1]` — nb `4cd50494`, screened **08-27 04:07** (~157 KB, 2 of 3 parts, never reached the notebook that graduated it)
- **Mechanism = the 50-source cap, not the splitter.** All four affected notebooks hold exactly
  `nlm_bridge.SOURCE_LIMIT = 50` sources (t10's holds 34 distinct docs though the last want-set was
  3 roster + 10 survivors). Residual code hole in `_screen_stage` step 4b: `_restage_missing_parts()`
  calls `nlm_bridge.add_source_text(nb, t, x, profile=prof)` **without checking `res["ok"]`**, and the
  post-repair re-verify (`have = {_shortlist_key(n) for n in num_map}`) is per-NUMBER again — so a tail
  part rejected at the cap leaves the doc "present", not `add_failed`, and it is questioned blind.
- Splitter math validated: for all 2 098 oversized docs on t10–t14 the checker's `ceil(size/118 000)`
  equals the real `_doc_source_parts` chunk count (0 mismatches) ⇒ no undercount of expected parts.
- Positive spot-check (t10 nb `50f703e7`): `WO2020026413` 336 909 B K=3 → parts 1/2/3 all present;
  `JP2019221076` 315 709 B K=3 → 1/2/3 present; `CN107534323` 152 186 B K=2 → 1/2 present. Full coverage.
- Coverage-gating counts unchanged from the 07:06 staging audit: **211 S1 blind tails (t11 30 · t13 53 ·
  t14 128)** and **119 add_failed docs NotebookLM never saw** (t10 15 · t11 27 · t12 26 · t13 24 · t14 27).
  Oversized docs with no verdict at all: t10 305.
- **Restage ledger is NOT sufficient evidence for the NO-GO invariant.** `restage_ledger.jsonl` currently
  holds 10 rows, all t14, all `answered:false` (`QUOTA-ABORT`); `restage_verified_full = 0` on every tab.
  Even for a future `answered:true` row, the throwaway notebook is deleted right after the query and no
  source inventory is persisted, so part-completeness is unfalsifiable; and the corpus notebook the tab's
  own verdict came from still holds the clipped doc. A restage row can retire the *"no instrument ever
  saw this tail"* claim; it cannot retire *"the notebook that produced this doc's verdict was complete"*.
- Verdict: **FAIL** — the truncation NO-GO invariant does not currently hold on t10, t11, t13, t14
  (t12 PASS on live part presence). No "assessed in full" claim is supportable for any of t10–t14.

### 2026-08-27 ~07:05 UTC — ranking-integrity audit, ALL tabs (deploy head 09f61a9, LIVE)

Run: `docker exec -i -w /app patent-bench python3 - --json --deploy-head 09f61a9 --registry "$(cat
docs/controls-registry.json)" --baselines "<fenced block above>" < scripts/audit_ranking.py`
→ `/data/audits/audit_ranking.json` (14 tabs, 47 rows), worst **FAIL**, exit 2. DB stable: max
scored_at 05:48 UTC, all five screens finished, opus deep-compare parked by the ⏳ watchdog until
10:20 UTC — **the verdict holds for that window only**.

- **Champions (stored deep-read scores, 🎯 Must top verified to coincide):** t10 `US20230337972` 5.0
  now **tied** with `US20220221016` 5.0 (both re-read 08-26 14:00, 6.0→5.0) · t11 **3-way tie 7.0**
  `CN117039286` / `CN220652165` / `CN118156696` · t12 `KR20260033205` 8.0 (unchanged) · t13
  `CN223926581` 10.0 (unchanged) · t14 **3-way tie 6.0** `EP4152472` / `CN103457003` / `CN118318177`.
  ~549 new verdicts since the 08-26 19:57 audit (t10 214 · t12 159 · t13 123 · t14 32 · t11 21); the
  only one that entered a top-10 is t14 `KR102760076` 5.0.
- **t13 C1 orphaned reads FAIL — 330 docs [KNOWN, baseline 401, SHRANK from 401].** Verified raw:
  `CN115693859` stores 24 v1-keyed elements (`partial | message generation device comprises a wireless
  communication module and an expansion module`) against 9 current v2 M-names (`message generating
  device (120) comprises a wireless communication module (121) and a control module (122)`) — not
  exact, not numeral-stripped-norm, 24≠9 so not positional. **All 330 cap at holistic 2.0**
  (186×2.0 / 128×1.0 / 16×0.0) — no champion hides in the orphan pool, matching the registered scope.
- **C3 corpus-top blocks PASS on all five tabs** (block == stored top-10 as of its own ts). INFO: t14's
  block (05:36) predates `KR102760076` 5.0 (05:44) → displayed top-10 is one doc stale.
- **NEW / GATING — t12: the tab's LAST compiled message crowns a false leader.** Latest `role='c'`
  compile is msg **3092** (08-27 00:21, NLM mega-screen finalize): «**BEST**: KR20250094125,
  **SECOND-BEST**: WO2025225986» — both **opus 3.0** — with **no 📌 CURRENT CORPUS TOP-10 block**,
  while the corpus champion `KR20260033205` **8.0** is not in the shortlist at all
  (`nlm_screen_state='rejected'`). The same run wrote `nlm_rank=1/2` + ☑, so the shortlist surface also
  shows 3.0 above 8.0. **Systemic:** screen-finalize compiles carry no block on any tab (t10 3101, t11
  3095/3098, t12 3086/3092, t13 3077/3083, t14 3104); on t10/t11/t13/t14 a later ranking compile
  restored the block, on t12 it did not. **Audit blind spot:** C3 selects the newest *block-carrying*
  compile (t12 msg 3068, 20.9 h older) and only WARNs when no compile anywhere has a block ⇒ it cannot
  see a newer blockless crown. No baseline entry covers this (F6 covers claim-weight lists only).
- **NEW — screen-lane canary control fails ⇒ no negative claim from a screen is admissible:** t12
  `KR20260033205` (registered paraphrased canary, opus 8.0) `rejected`; t10 `EP3849091` (4.0 canary)
  `rejected`; t13 `CN116508192` (5.0 canary) `add_failed`. Msg 3092's «no candidate discloses all 17
  elements» is only survivable because its wording is scoped to "the provided sources".
- **Stale 6/6 echo (t10 2973/2979/2982) CONFIRMED and NOT code-fixed.** In msg 2979 block 1 (the
  deterministic prepend) lists `US20230337972 — 5`, block 2 lists `— 6`: the second block sits inside
  the reduce model's own answer, i.e. the model reproduced an older block from `history`. Last
  occurrence t11 msg **3051** (08-26 15:08); the 8 later block-carrying compiles are single-block, but
  nothing in `api.py` strips 📌 blocks from `res["answer"]`, so absence is luck, not a fix.
- **C4 WARNs both LEGAL (holistic ≠ Must), not sunk reads:** t11 `CN220306367` (6.0, pos 584/875) —
  all 48 stored names map to a kind, MUST 2/7 full, `mand_rating` 0.95, 581 docs strictly better; its
  19 ✓ are A-elements (sealing pin / injection hole family). t14 `CN118318177` (6.0, pos 127/475) —
  MUST 3F/2P of 5, `mand_rating` 7.27, `no_absent=True`, 103 docs strictly better (weighted rating,
  per-element weights explain the ordering).
- **C7:** WARN t11/t14 [as expected] — their latest claims-audit DONE messages are 08-15 16:28 /
  08-15 17:56, i.e. **pre-F6-fix artefacts**, not a live regression (the F6 `corpus_top` append is
  present in `api.py`; t10/t12/t13 DONE messages of 08-23 all carry it). PASS elsewhere.
- **C6 falsification PASS on all five tabs** — every top-15 holistic doc holds a current-key deep read.
- **C5 closure gate:** t13 **PASS**, canary `CN223926581` 9/9 MUST under current keys →
  `closure_claims_permitted: SCOPED`. t10/t11/t12/t14 **WARN** (no verbatim canary; benchmarks are
  uploaded claims PDFs — see `_verbatim_note` per tab). Surrogate known-positives DO register under
  current keys (t10 `US20220221016` 8F/11 · t11 `CN117039286` 7F/7 · t12 `KR20260033205` 6F/6 · t14
  `EP4152472` 5F/5), so the name-join is demonstrably healthy — claims are SCOPED, not FORBIDDEN.
  **Zero-full MUST elements = 0 on all five tabs**, so no 08-18-style «element X 0✓ corpus-wide» claim
  is live. Permitted scopes: t10 «among the 2049 current-key reads (of 2136)» · t11 «1092 of 2104» ·
  t12 «846 of 1914, and NOTHING from the screen lane» · t13 «659 of 2190, 330 v1-orphans excluded» ·
  t14 «480 of 1997». **FULL closure: nowhere.**
- Minor doc drift (NEW, needs user approval — not edited): `controls-registry.json` champion_controls
  still say t10 `US20230337972` / `US20220221016` are "opus 6.0"; both stored 5.0 since the 08-26 re-read.
- Verdict: **VIOLATIONS** — t13 C1 is KNOWN and shrinking, but t12 must not be quoted from its chat
  head: post no t12 "BEST" line until a ranking compile (or a code fix on the screen-finalize path)
  puts the deterministic 📌 block back as the tab's last word.

### 2026-08-27 ~16:40 UTC — supervisor gate on the FINAL hypothesis cycle (thesis_2026-08-27, HEAD fa6f6f7)

Trigger: pre-report gate on the hypothesis-driver's closing thesis (H1–H16 terminal), which
states itself that no supervisor pass has gated it. Scope t10–t14. Procedure HEAD for
src/scripts = **a2bb65f**; verdicts on disk carry 09f61a9 / f5de302 → STALE(deploy) everywhere.

- **Step 0 account gate PASS** (16:31 and re-run 16:36): A1 all five bindings == registry
  (t10 drawnformula · t11/t13 default · t12/t14 work2); A2 one job per account — at 16:31
  default `t11:restage[23219]`, work2 `t14:restage[23233]`; at 16:36 default `t11:restage`
  only (t14 finished). `chain-restage.py 13` (pid 24997) is parked and correctly
  fail-closed: `waiting: ['t11:24989','t11:24995']` — t13's restage will not start until
  default is free, so no A2 breach is in prospect. Runner scripts in `/data` md5-match git
  a2bb65f (`nlm_followup.py`, `restage-blind-tails.py`, `chain-restage.py`) → the
  quota-abort notebook-delete fix IS deployed on the restage lane.
- **Pause state verified as claimed:** `/data/.opus_grad_driver.stop` 16:10:29;
  `.claude_read_{10,13,14}.resume.json.PAUSED-BY-USER` 11:14; **no `.claude_read_*.lock`**;
  no deep-compare process. Global `max(scored_at) = 08-27 11:14:58`, i.e. **nothing has been
  unparked** — the read watermark has been static for 5.5 h. (Thesis §4 says the last verdict
  landed ~10:34; DB says 11:14:58 — minor, disclose.)
- **EVIDENCE DEFECT — the 07:06 all-tab staging verdict NO LONGER EXISTS.**
  `/data/audits/audit_staging.json` on disk is a **single-tab `--tab 14 --no-live` run at
  16:11 on f5de302** (`args {"tab":14,"json":true,"no_live":true}`, 6 rows, S3 "skipped").
  It overwrote the all-tab run. Consequence: `audit_status` reports staging **MISSING(tab)
  for t10–t13** and STALE(deploy) for t14. The 07:06 numbers (S1 t11 30 · t13 53 · t14 128;
  S5 119) survive only as prose in this file — per doctrine that is a journal claim, not
  evidence. **Everything the thesis draws from staging on t10–t13 is currently ungated.**
- **Freshness of the surviving verdicts.** `audit_recall` 07:05:33 and `audit_ranking`
  07:05:40, both head 09f61a9 → STALE(deploy) vs a2bb65f. STALE by DATA as well, but
  **quantified and immaterial for R1**: reads landed after their anchors on t10 (7), t13 (5),
  t14 (7), t11/t12 (0) — **0 of the 19 is a new GT (score ≥ 4)**, and screened counts /
  claims rounds / max_claims_ts are byte-identical to the anchors. So R1's 182/202 and the
  per-tab splits are numerically still true; the labels are stale, the numbers are not.
  `audit_full_staging` 07:12:44 (09f61a9) exists and is FAIL — the caller's belief that the
  ranking and full-doc auditors "were killed at 07:15 and never ran" is **wrong for both**:
  both wrote verdict files. What was actually lost is the *staging* all-tab verdict.
- **Gate matrix (audit_status, a2bb65f): post_sweep_results, champion_report and
  closure_claim BLOCKED on all five tabs.** No pending_trigger.
- **Baselines.** Live `add_failed` vs registered S5: t10 15 ≤ 173 · t11 27 ≤ 40 · t12 26 ≤ 47 ·
  **t13 24 > 13 = GROWTH → GATES** · t14 27 ≤ 37. t13 C1 330 ≤ 401 KNOWN. R2 corridor t10 59 =
  59, t13 119 = 119 registered; **t11 46 / t12 61 / t14 80 still UNREGISTERED → gate**.
  Registry scope texts remain stale ("recall 7/12" t10, "1/14" t13 vs measured 11/13, 7/21) —
  not edited, needs user approval.
- **Numbers independently re-derived from the DB and reproduced EXACTLY** (read-only URI,
  v2 epoch 1787511600): v2 champion table graduate 1433 / 31 champs · rejected 1004 / 3 ·
  `add_failed` **119 / 10** ⇒ judged 31/34 = 91.2 %, end-to-end **31/44 = 70.5 %**; per-tab
  splits 7·2·0 / 2·0·1 / 0·0·0 / 3·0·2 / 19·1·7 all match §6.2. The 3 judged misses
  (t10 EP3849091 4.0 rej 08-25 18:33, t10 CA2552849 4.0 rej 08-26 21:48, t14 CN115514036 4.0
  rej 08-27 03:48) confirmed. **39 v1 champion rejections** confirmed (t11 25 · t12 6 incl.
  KR20260033205 · t13 3 · t14 5; t10 0) — and 39 + 3 = the auditor's 42, so no contradiction.
  **t10 758 never-screened fetched docs, 4 with score ≥ 4** confirmed — and all 758 already
  carry a deep read, so no document there is unassessed.
- **H6 independently re-derived from `/data/audits/restage/t14_*.json` (12 clean chunks):**
  66 graduate / 52 rejected = 118 ✓; **exactly 1 of 118 discloses any weight-4/5 feature
  (JP2020119712, F1)** ✓ ⇒ 117/118 ✓; every weight-4 feature (F4–F11) 0.0 % on both sides ✓;
  permutation test (seed 42, 20 000 shuffles) **p = 1.0e-4** vs the thesis's 5e-5 (the floor
  for 20 001 draws), same order. Point estimates differ with parse method: my weighted yield
  is **4.35 vs 1.29 (3.4×)** against the thesis's 3.76 vs 1.03 (3.65×), and per-feature diffs
  drift ±2 pp (F22 +32.4 vs +34.3; F16 +12.1 vs +10.6). Direction and conclusion identical;
  the exact figures are not bit-reproducible from the persisted evidence.
- **NEW GATING DEFECT — restage credit can turn S1 green on an answer to a different
  question.** At **16:33:48 the t14 restage runner completed the last 10 docs**
  (`/data/audits/restage/t14_1787848428.json`, ledger now 148 rows, **128 t14 docs
  `answered:true`**). But that chunk's `_broad` failed with a transport error and its
  `_consolidated` opens verbatim: *"Because the specific **checklist of features (F1, F2,
  etc.)** was not explicitly provided in the query or the active workspace history, we have
  analyzed each of the 10 patents … (designated **F1 through F9** below)"* — NotebookLM
  **invented its own 9-feature list** instead of answering t14's 22-feature MUST checklist.
  All 10 docs were nevertheless credited `answered:true` with `parts_ok` and
  `inventory_seen:true`, because `restage-blind-tails.py` credits on
  `answers[num]` being non-empty and ≠ `QUOTA-ABORT` — it never checks that the reply
  addresses the tab's own feature list. `audit_staging.restaged_full()` consumes exactly that
  flag, so **the next staging run will report t14 S1 blind = 0** on 10 documents that were
  never asked the right question. This is the 2026-08-20 failure shape (a control reading
  green over a computation in which the target never registered). Required fix before any
  t14 "no blind tails" / "assessed in full" claim: assert the consolidated reply carries the
  tab's own F-index cardinality and contains no "checklist … not provided" disclaimer, and
  un-credit `t14_1787848428`. Same guard needed for `t14_1787834464` (already `answered:false`).
- **t11/t13 restage lanes have produced ZERO evidence.** `restage_ledger.jsonl` = 148 rows,
  **all t14**; distinct answered t11 0 · t13 0 · t14 128. Their 08-27 08:17–09:52 runs all
  exited 3 ("~100 notebooks" cap — the leak fixed in a2bb65f); the relaunched t11 runner hit
  `QUOTA -> sleep 3600` at 16:17:55 and re-armed 16:36:16 on the **default** account, whose
  Q&A quota the caller probed as exhausted at 16:20 (rc 0, `answer: ""`). t11's 30 and t13's
  53 blind tails therefore remain fully blind, with no staging verdict on disk for either tab.
- **H16 / F3f admissibility.** `followup_ledger.jsonl` corroborates the A/B *design and
  timing* — t10 07:07 `mode:"per-doc"` [US20120007441, CN103683526] nb 264d074c and t10 07:12
  `mode:"ab-wording"` [US20120007441] nb 49975ba7 with note *"round 2: RF-inclusive wording
  variant of F7/F8/F9 after round-1 NO on all three"*. **The answers themselves are not
  persisted anywhere** (both notebooks deleted; `nlm_followup` was pre-`--keep-notebook` on
  that path) — the NO/NO/NO → YES/YES/YES flip exists only in
  `docs/nlm-mirror/discussion-journal.md`. Under the standing doctrine that is a journal
  claim, not evidence. n = 1 document × 3 features. **F3f is NOT registered by this pass**
  (no user approval, and one unpersisted measurement cannot carry a class). Recommended
  shape if the user approves: a table row with **size UNMEASURED and no baseline count**,
  control = "re-ask every weight-4/5 NO once with a genus expansion", gated on a persisted
  re-run of the A/B on drawnformula (t10's account, which answers).
- **Cross-file contradictions:** none between `audit_recall` / `audit_ranking` /
  `audit_full_staging` (t13 C6 PASS vs R1 misses remains consistent — the misses are opus-read
  GT; ranking's 42 rejected champions == thesis's 39 v1 + 3 v2). Two thesis-vs-live drifts,
  both time-stamped after writing: §6.4/§8 "t14's blind pool is 10, not 0 … those 10 remain
  uncredited" was overtaken at 16:33; §8 "Three restage runners (t11, t13 on default; t14 on
  work2) were live throughout" is wrong — **t13's runner exited `done` at 09:52:54** and only
  the chain waiter is alive (had both been live on default it would have been an A2 breach).
- Standing carry-overs, unchanged: C5 canary only on t13 (SCOPED 9/9); t10/t11/t12/t14 have
  no verbatim canary ⇒ corpus-wide negatives scoped only. Ranking's t12 blockless-crown FAIL
  (msg 3092 crowns two opus-3.0 docs while the 8.0 champion is `rejected`) still stands — no
  t12 "BEST" line may be quoted. R6 queues non-empty: t10 [US20120007441, CN103683526];
  t13 [CN218958581, AU2022338850, CN220510820, CN120073105, CN120433348].
- **Verdict: BLOCKED.** The DB-derivable arithmetic of the thesis is sound and reproduces
  exactly; what is not gated is the staging evidence for t10–t13 (file overwritten), the
  restage credit on t14 (10 docs credited on a wrong-checklist reply), and H16 (journal-only).

### 2026-08-27 16:47 UTC — staging-completeness audit, ALL tabs RESTORED (deploy head 52efb22, `--no-live`)

- **Evidence restored.** `/data/audits/audit_staging.json` is again an all-tab verdict
  (`ts 1787849217`, `args {"tab": null, "json": true, "no_live": true, "deploy_head": "52efb22"}`,
  `script_version 2026-08-27.1`), with `anchors` for all 14 ready tabs — the 16:11 `--tab 14`
  probe that overwrote the 07:06 file is superseded, and `audit_status` can no longer read
  `MISSING(tab)` on t10–t13 (freshness is keyed on `anchors[str(tab)]`).
- **S3 SKIPPED ON EVERY TAB (`--no-live`), disclosed.** A2 gate PASS with two live out-of-band
  runners (`default` → `t11:restage[25412]`, `work2` → `t14:restage[25404]`); the `default`
  account's Q&A quota is exhausted. `audit_staging` has no per-tab live switch, and a
  `list_sources(force=True)` on a profile mid-chunk would contend with the remediation itself,
  so the whole run was `--no-live`. Comparability is not affected: the 07:06 entry records that
  the blind counts are live/no-live invariant ("no-live recompute gives the same 30/53/128") and
  `parts_verified_full` reduced no blind count then; this run reports `parts_verified_full: 0`
  and `live_notebooks_listed: 0` on every tab. **Consequence: no F4 clearance exists right now —
  no answer from any t10–t14 screen/claims notebook may be interpreted until a live S3 is run.**
- **Worst level per tab: t10–t14 all FAIL** (t1–t9 WARN, out of scope). Every t10–t14 FAIL is
  S5, plus S1 on t11/t13/t14. S4 PASS on all five (t10 237/240, t11 1/8, t12 7/132, t13 20/120,
  t14 150/150). S2: the clip lands in DESCRIPTION for 1 673 of 1 693 truncated docs; the rest in
  CLAIMS (20) or DIGEST (7).
- **S1 blind tails: t11 30 · t13 48 · t14 10 = 88** (t10 0, t12 0 — S1 WARN only). vs the 07:06
  prose (t11 30 · t13 53 · t14 128 = 211): **−123**, and every doc that left the pool is
  accounted for individually —
  - t11: the *identical set* of 30 (set equality against `/data/audits/blind_t11.json`).
  - t13: 53 → 48; the 5 leavers (`AU2020250299`, `CN101326796`, `CN112654325`, `CN113302890`,
    `US20120112536`) each gained a real `claude-opus-5` deep read at 11:14 UTC today
    (`scored_at` 1787829252–1787829298) — a read, not a bookkeeping change.
  - t14: 128 → 10; 118 left via `restage_verified_full` and the residual 10 are *exactly* the
    10 docs requeued from chunk `ts=1787848428` (set equality with the `uncredited_reason`
    rows). No new entrant on any tab.
- **Raw re-verification from sqlite (`mode=ro`), independent of the audit script** — the audit's
  `compose_blob` was first confirmed byte-exact against `web/api.py:_doc_source_text` and
  `CLIP=120_000` against `nlm_bridge._clip_bytes`:
  - t13 `CA3079164` — **187 087 B composed, 67 087 B (35 %) never staged**, clip lands in
    DESCRIPTION (NLM saw 103 455 of 158 160 description bytes = 65 %); `score` null.
  - t11 `US20090311607` — 166 628 B, 46 628 B (27 %) unstaged, cut in DESCRIPTION, `score` null.
  - t14 `US20060158947` — 139 204 B, 19 204 B (13 %) unstaged, cut in DESCRIPTION, `score` null.
- **S5 add_failed: t10 15 · t11 27 · t12 26 · t13 24 · t14 27 = 119** (unchanged from 07:06).
  Against the registered baselines (t10 173 / t11 40 / t12 47 / t13 13 / t14 37): all within
  baseline except **t13 24 > 13 = GROWTH, still gating** — KNOWN, first flagged in today's
  05:40/07:06 passes, still unregistered and unexplained.
- **RESTAGE CREDIT IS NOW SOUND for the 118 t14 docs — verified against raw evidence, not just
  the ledger flag.** Ledger = 148 rows, 118 credited / 30 not, all tab 14 (t11 and t13 have zero
  ledger rows, so their 30 + 48 blind tails carry no credit at all). Per credited doc: (a) `parts_ok`
  `want == ok == K` where K was **recomputed independently from the DB blob size** (105 docs K=2,
  13 docs K=3) — 0 disagreements; (b) the chunk's post-ingest `source_inventory` holds ≥K source
  titles for that number — 0 gaps; (c) the doc has its **own** `NUM: F1=…F22=…` grid line with a
  claim/section citation in that chunk's `_consolidated` reply — 0 missing (the apparent miss on
  `DE102017110483` is a streaming artefact: its grid is concatenated onto a truncated preceding
  line, the grid and its Claim-1 citation are intact). All 12 credited chunks answer the real
  22-item checklist; the invented-checklist pattern appears in **exactly one** file,
  `/data/audits/restage/t14_1787848428.json` (`"not explicitly provided"`, `"reconstructed
  checklist"`, F1–F9 only), and that file backs **no** credited row — its 10 docs are the 10 that
  remain blind. The other two uncredited chunks (`1787811878` empty, `1787834464` F1–F3 only) are
  likewise uncredited. **The 07:06 finding "restage credit = 0 docs, unfalsifiable" is resolved.**
- **Residual credit caveats (NEW, none invalidating the 118):**
  1. `restage-blind-tails.py` credits on `num in ans` — a *plain substring* test, and
     `nlm_followup --compact` copies the whole `_consolidated` blob under every doc key, so a doc
     merely name-dropped inside another doc's justification would be credited. It happened not to
     bite here (all 118 verified to carry their own grid), but the guard should be tightened to an
     anchored `NUM:\s*F1=` match.
  2. Every restage notebook is deleted after its chunk (`notebook_deleted: true`), so the credit is
     **not live-falsifiable**; the evidence JSON + its `source_inventory` is the whole record.
  3. Scope of the credit: the restage asked the tab's 22-item claims-state MUST checklist — the same
     one the sweep uses — not all 27 benchmark features. The 5 unasked ones are generic
     apparatus/processor/memory/instruction items. Not a defect, but "assessed in full" for those
     118 means *against the sweep checklist*, at full text.
- **No "assessed in full" claim is supportable on any of t10–t14.** 88 docs' tails were never seen
  by any instrument (t11 30 · t13 48 · t14 10) and 119 add_failed docs never reached NotebookLM at
  all (t10 15 · t11 27 · t12 26 · t13 24 · t14 27); t14 is the only tab where the blind pool is
  nearly closed, and even there the 10 residual docs plus 27 add_failed gate the claim. t10 and t12
  are S1-clean but S5-blocked (15 / 26).
- **Verdict: VIOLATIONS.** Next action: keep the t11 runner going and **launch a t13 restage over
  its 48 blind tails** (t13's runner exited `done` at 09:52:54 with an empty ledger) — the t14 path
  is now demonstrably sound, and t13 is the tab with both the largest blind pool and the gating S5
  growth. Do not touch the shared verdict path with a `--tab` probe again.

### 2026-08-29 ~22:25 UTC — supervisor pass (session-start gate SKIPPED by the caller; scope t10–t14)

Context: the caller ran the whole session without a supervisor pass (violation of the
2026-08-24 standing rule) and asked for a retrospective audit of today's claims.
Procedure HEAD for src/scripts = **e02278b**.

- **Step 0 account gate PASS twice** (22:20:28 "no running NLM jobs"; 22:24:39
  `drawnformula: 1 running job ['t10:nlm-screen']`). A1 all five bindings == registry.
  **A t10 mega-screen was launched at 22:20:43** (`.nlm_screen_10.json` `started_at`
  1788042043, `params {batch_size:39, survivor_cap:10, target:49}`, notebook
  `50f703e7`): queue = **773 docs = the 758 never-screened backlog + the 15 t10
  add_failed**, all 773 already opus-scored. A2 holds (default/work2 idle). The gate
  is still blind to out-of-band runners (`nlm-untreated-lane.py`,
  `restage-blind-tails.py`) — 08-27 finding, unfixed.
- **Gate matrix (audit_status, e02278b): post_sweep_results, champion_report and
  closure_claim BLOCKED on all five tabs.** Freshness: t10 staging STALE(deploy),
  recall/ranking STALE; t11 all three STALE(deploy); t12 all three STALE; t13 staging
  STALE(deploy), recall/ranking STALE; t14 all three STALE. No pending_trigger.
  Verdict-file vintage: `audit_recall` + `audit_ranking` 08-27 07:05 (head 09f61a9),
  `audit_full_staging` 08-27 07:12 (09f61a9, **FAIL**), `audit_staging` 08-27 16:46
  (52efb22, `--no-live`). **Nothing has been audited in the 2 days and ~40 commits since.**
- **Claim "the agreed work is complete; 0 docs hold a truncation-affected verdict" —
  REFUTED as stated.** Independently recomputed from sqlite (`mode=ro`, composition
  byte-identical to `_doc_source_text`): t10–t14 hold **2 098 oversized docs (>118 000 B)**,
  of which **1 678** carry a screen verdict later than 9dda5f1 (the caller's 1 609 is not
  reproducible), **115** are `add_failed` (NotebookLM never indexed them — they hold no NLM
  verdict at all), and **305** (all t10) had no screen verdict until 22:20 today.
  `assessed_truncated = 0` is a **timestamp** metric; the 08-27 full-doc auditor already
  recorded that it "proves nothing about coverage". The part-presence instrument's last
  verdict is **FAIL** (5 live blind tails: t10 `JP7753930` [1,2]/3, t11 `CN116345029` [1]/2,
  t13 `CN115552761` [1,2]/3, t13 `CN119153812` [1]/2, t14 `JP2023095746` [1]/3 — all still
  `graduate` in the DB today), and its named root cause is **still in the deployed code**:
  `api.py:1338-1347 _restage_missing_parts()` calls `nlm_bridge.add_source_text()` without
  checking `res["ok"]`, and the post-repair re-verify at `api.py:4029` is per-NUMBER
  (`have = {_shortlist_key(n) for n in num_map}`), so a tail part rejected at
  `SOURCE_LIMIT = 50` leaves the doc "present" and it is questioned blind. The screen
  relaunched at 22:20 **reuses `50f703e7`, the very notebook the 08-27 audit found at the
  50-source cap.**
- **S1 blind tails now recompute to 0 on every tab (t10 0 · t11 0 · t12 0 · t13 0 · t14 0),
  and that is entirely restage credit, never audited.** `restage_ledger.jsonl` = 271 rows,
  **211 credited (t11 30 · t13 53 · t14 128)**; the t11/t13 rows were written 08-27
  17:54–21:20, i.e. **after** the last staging verdict (16:46). Supervisor spot-check of the
  10 t11/t13 evidence files: each consolidated reply carries per-doc grids at the tab's own
  MUST cardinality (t11 F1–F7 of 48 features, t13 F1–F9 of 24) and none shows the
  invented-checklist pattern — the credit is structurally sound, but **it retires only
  "no instrument ever saw this tail"; it does NOT retire "the notebook that produced this
  doc's screen verdict was complete"**, the notebooks are deleted (not live-falsifiable),
  and commit **383cb69 (today 20:14) LOOSENED the credit gate** with no auditor run since.
  A staging verdict must confirm S1=0 before any coverage claim rests on it.
- **Baselines.** Live `add_failed` vs registered S5: t10 15 ≤ 173 · t11 27 ≤ 40 · t12 26 ≤ 47
  · **t13 24 > 13 = GROWTH → GATES** (unregistered and unexplained since 08-27) · t14 27 ≤ 37;
  total **119 docs NotebookLM never saw**, of which only t10's 15 are in a running lane.
  `R2-screen-roster39` = 39 covers today's `batch_size 39` relaunch. Still-gating
  UNREGISTERED FAILs: R2-batch-corridor t11 46 / t12 61 / t14 80; R1 t13 7/21; R5 t10
  lexical lane. t13 C1 330 ≤ 401 KNOWN. **F3f is still NOT a registered class** (the 08-27
  pass declined it; today's section prices it but no table row and no user approval exists) —
  the genus wording was nevertheless run live on t10/t12/t13/t14 at 19:30–19:56 before being
  reverted (1c456a7). Mitigating and verified: `nlm-untreated-lane.py` opens the DB
  `mode=ro` and writes only `/data/audits/*_ledger.jsonl`, so **no genus verdict entered
  `documents`**.
- **New ground truth landed today → the recall verdict is invalid by construction, not merely
  stale.** Three opus-5 reads at 20:18–20:19 (`/data/audits/opus3_2026-08-29.json`):
  t12 `CN116190826` **4.0**, t14 `CA2142366` **4.0**, control t14 `CN115800429` 3.0 —
  **both new GT docs were `rejected` by the mega-screen**. R1's t12 14/14 and t14 60/60 were
  computed on a GT set that has since grown to 15 and 61. Live screen-lane recall
  recomputed by the supervisor: GT (opus ≥4, fetched) = **205** → `graduate` 147 ·
  **`rejected` 44** · `add_failed` 10 · never-screened 4 ⇒ **147/191 = 77 % among docs that
  reached the screen**; the reject-pool miss count grew 42 → 44 today off only 3 reads.
- **Discrimination, measured (discovery, no auditor instrument exists for it):** GT rate among
  opus-read docs, graduate vs rejected — t10 1.4 %/0.3 % · t11 16.4 %/3.8 % · t12 1.9 %/1.8 %
  · t13 2.5 %/1.0 % · t14 11.5 %/11.9 %; aggregate 6.0 % vs 2.0 % (2.94×). The sample is
  **not random** (opus reads every graduate; today's reject-pool reads were filter-selected),
  so neither "discriminates" nor "does not discriminate" is gate-backed. Both of the caller's
  successive public framings (69–86 % graduation "does not discriminate", then "the original
  runs graduated 10–24 %") are unsupported: live graduate share is t10 39.9 % · t11 21.5 % ·
  t12 23.9 % · t13 35.9 % · t14 26.5 %.
- **Evidence defect (repeat of the 08-27 `--tab` overwrite lesson, different file).**
  `/data/audits/opus_ranking.json` on disk contains **only key "14"** (50 rows): the last
  `opus_ranking.py --tab 14` run overwrote the shared path, so the claimed **1 206-grid t10
  recovery has no persisted artifact**. It also conflicts in wording with the ranking
  verdict, which records **t10 C1 PASS** ("every stored per-element verdict keys to the
  current wording, directly or remappable") — `audit_ranking.feat_norm` already strips
  reference numerals, so t10's grids were never orphaned in the auditor's sense; the only
  genuine orphan pool is t13's 330 (registered 401), which `opus_ranking.py` correctly drops.
- **Per-doc lane (v3) — no persisted evidence of finding a champion the screen misses.**
  The only head-to-head on disk is `audit_recall` t13: `must` rounds 2/21, `must-followup`
  contributed 5 of the 7 hits — that is the **legacy roster-35 claims sweep**, not the
  mega-screen. Today's 92-doc `add_failed` run cannot produce such evidence by construction
  (those docs were never screened), it wrote nothing to the DB, and 86 of the 92 already held
  opus verdicts. Both of today's new GT docs came from `opus_prior_filter.py`
  (`CN116190826` rank 0, `CA2142366` rank 9), not from the NLM lane.
- **Cross-file contradictions:** none between `audit_recall` / `audit_ranking` /
  `audit_full_staging` (t13 C6 PASS vs R1 misses stays consistent — the misses are opus-read
  GT). The S1 shrink 88 → 0 is fully explained by the 08-27 evening restage ledger rows, not
  by phantom reads (t11 max scored_at 08-27 05:38, t13 08-27 11:14, unchanged).
- **C5:** canary only on t13 (9/9, SCOPED). t10/t11/t12/t14 have no verbatim canary ⇒
  corpus-wide negatives scoped only. **R6 queues non-empty:** t10 [US20120007441,
  CN103683526] (both inside the 773 docs now being screened — wait for it), t13
  [CN218958581, AU2022338850, CN220510820, CN120073105, CN120433348].
- **Permanent scope limit to disclose:** with "no NLM quota on t11 ever again", t11's 27
  `add_failed` docs and its 4 R1 misses can never be closed by an NLM instrument; any t11
  coverage statement must say so.
- **Verdict: BLOCKED.** Nothing reported today was gate-backed. Auditors to spawn, in order:
  full-doc-staging-auditor (t11–t14 live now; t10 after its screen finishes) →
  staging-completeness-auditor (LIVE, not `--no-live`) → recall-integrity-auditor →
  ranking-integrity-auditor (`--deploy-head e02278b`) → nlm-followup-verifier (t13's queue on
  `default`, which is free; t10's two docs only after the drawnformula screen ends).

---

## 2026-08-29 — F3f priced: the genus wording buys recall and pays more in precision

**Context.** User decisions this session: (1) adopt the F3f genus wording from now on,
forward-only — never re-screen a document NotebookLM already answered on; (2) run NLM over the
`add_failed` documents, which NotebookLM never indexed; (3) never spend NLM quota on t11 again.
The untreated lane (`scripts/nlm-untreated-lane.py`, `nlm_followup --genus`,
`scripts/genus_maps.json`) was launched over the 92 `add_failed` docs of t10/t12/t13/t14.

**Correction to the premise.** `add_failed` means *NotebookLM never indexed it*, NOT *nothing
read it*. 86 of the 92 already carry a full opus verdict in `documents.feature_scores` and all
92 are ranked. Only 6 documents — all t14 (`CN106992561`, `JP2003207552`, `KR102344538`,
`KR20040066085`, `KR20260055095`, `WO2025036486`) — have never been read by any instrument.
That accident is what made the measurement below possible.

**Correction to the 08-27 confabulation reading.** The screen's `unmatched` list means "named but
not in THIS round's roster key_map", not "invented". Of t12's 26 unmatched numbers, **25 are real
documents in t12's own corpus** (mostly already graduates) — that is the F4 rolling-notebook
effect, NLM naming sources from earlier rounds. Genuine non-existent strings across all five tabs
are three: `OR802154` (t10), `LM7805` (t13, a voltage-regulator part number), and `EP239077233`
(t12, a digit-mangled form of the tab's own benchmark number EP23907723). Confabulation is real
but rare; the earlier "26 invented numbers" reading was wrong.

**The measurement** (`scripts/genus_vs_opus.py`, read-only, zero quota, zero tokens). Both arms
ran the SAME machinery — `nlm_followup` consolidated 10-doc question, dedicated notebook, full
multi-part staging — against the SAME reference, `documents.feature_scores` (opus). Only the
feature wording differs.

| arm | source | docs | cells | agree | NLM > opus (over-credit) | NLM < opus (under-credit) |
|---|---|---|---|---|---|---|
| verbatim | 08-27 restage lanes | 10 | 131 | **78.6%** | **0.0%** (0/131) | 21.4% |
| genus | 08-29 untreated lane | 30 | 210 | **55.2%** | **30.5%** (64/210) | 14.3% |

Genus confusion: `yes|partial=42`, `yes|no=21`, `no|partial=29`, `yes|yes=9`. Per tab the genus
over-credit is t12 16.7%, t13 48.9%.

**Both halves of F3f are confirmed, and they point opposite ways.**
- The vocabulary floor is REAL: the verbatim arm under-credits opus on 21.4% of cells
  (25 `no|partial`, 3 `no|yes`) — documents opus finds that the benchmark's own words hide.
- The cure costs more than the disease: the genus arm over-credits on 30.5% of cells, including
  21 hard `yes|no`, and its agreement with opus drops from 78.6% to 55.2%.

Worked example — t13 `CN116508192` (opus 5.0, rank 38): genus scored F1–F9 all YES; the opus read
calls it "a substantial partial overlap rather than a close match", because the benchmark's
defining data path (input device wirelessly delivering *modifiable target data* to a wireless
module inside a message generating device, which builds the CAN message) is absent — the
candidate's wireless link is a 125 kHz / 315 MHz keyless-entry authentication link. The
disagreement traces directly to the genus map broadening "wireless communication module" to
"any radio transceiver module … or an RF SoC performing the same role", which a key fob satisfies.

**Caveats.** The two arms ran over different document sets (verbatim = blind-tail restage docs on
t13/t14, genus = `add_failed` docs on t12/t13), so this is not a paired test, and the verbatim
side is small (10 docs). The reference is opus, the project's registered ground truth for R1, not
truth itself. A 0/131 vs 64/210 gap is nonetheless far too large for the doc-set difference alone.

**Doctrine that follows.** Over-credit and under-credit are not symmetric costs, and which one
hurts depends on the lane:
- **Discovery lanes** (surfacing candidates for reading, no opus coverage): a false YES is cheap —
  it buys a read. A false NO is fatal — the document is never seen again. **Use the genus wording.**
- **Verdict / clearance lanes** (champions, top-N, "the corpus is cleared"): a false YES is the
  expensive error. **Never let a genus YES stand as a verdict** — route it to verification.

Concretely: the genus wording is the right instrument for t10's 758 never-screened backlog
precisely *because* that lane's job is to nominate, not to clear. Its graduates must then be read.
No genus YES may enter a champion list, a top-N, or a closure claim without a deep read behind it.

**2026-08-29, later — the F3f numbers restated on 2x the data.** t10's 15 add_failed docs and
t14's first 10 completed under the reverted VERBATIM wording, all of them documents that already
carry an opus verdict, so the calibration set doubled. Restated:

| arm | docs | cells | agrees with opus | over-credits | of which hard YES-where-opus-said-NO |
|---|---|---|---|---|---|
| verbatim | 20 | 241 | 75.9% | **3.3%** | **0** |
| genus | 55 | 400 | 50.0% | **35.0%** | **45** |

Correction to the earlier entry: verbatim is NOT over-credit-free. On the wider sample it
over-credits 3.3% of cells (4 `yes|partial`, 4 `partial|no`) — the 0.0% came from a 10-document
sample. It still never produces a hard `yes|no`, where genus produces 45. The conclusion holds
and is stronger, but the clean-instrument framing was an artefact of sample size and should not
be repeated.

Worked example of the residual verbatim over-credit: t10 `JP4974243` ("Wireless power
distribution system") was answered YES on F1, F2 and F3 — the two weight-5 features and one
weight-4 — on a document opus scored 2.0. NLM's per-feature YES is not a match verdict even
under the validated wording; it is a nomination.

---

## 2026-08-30 — pre-fix staging baseline, and the t10 ground-truth result

**Baseline** (`/data/audits/audit_staging.json`, full scope, `--live`, head `7e62d08`, verdict FAIL on S5). Captured by running the script directly after the agent died on the session token limit — the audit costs zero Claude tokens, so the limit was irrelevant.

- **S3-live-inventory PASS on t10–t14.** First ever clearance of the F4 rolling-notebook confound; the 08-27 verdict ran `--no-live`, so none had existed.
- **S1-blind-tails = 0 on every tab, and this corroborates the masking finding.** This checker reads the DB; the full-doc-staging-auditor probing live notebooks found 4 blind tails the same day (`CN116345029`, `CN115552761`, `CN119153812`, `JP2023095746`), every one marked `graduate`. A lane credit clears the DB record while the tail stays absent from the notebook the screen actually questions. Two instruments, two views, one conclusion — see [[blind-tails-masked-not-fixed]].
- **t10 S5 now 0** (was 15): the running mega-screen requeued its `add_failed` docs, F3c-ns behaving as designed.
- **t13 S5 still 24 vs registered 13** — gating growth NOT explained by the untreated lane, which credited all 24. `add_failed` is a permanent stamp for what the SCREEN could not index; a later follow-up lane seeing the doc does not clear it.
- **NEW class, untracked: 20 documents are clipped inside CLAIMS** (t12 18, t13 2; worst `KR20230036637` at 717 KB). Claims are the operative text for a prior-art read, so these are the most damaging clips in the corpus and no check currently isolates them.

**The t10 ground-truth run — the recall measurement the 08-23 directive asked for.** 773 documents, all of them already opus-read, seeded with the previous tournament's 10 champions.

| doc | opus | screen |
|---|---|---|
| `US11922243` | 4.0 | graduate |
| `US20180351412` | 4.0 | rejected |
| `US20120007441` | 4.0 | rejected |
| `CN103683526` | 4.0 | rejected |

**1 of 4.** The two misses in bold above are *exactly* the two documents of the clean A/B (`ab_clean_t10_1787900564.json`), where benchmark-verbatim wording yields `F1/F2/F3 = NO/NO/NO` and genus wording yields YES. The screen runs verbatim. **F3f is vindicated as a diagnosis of why the screen misses, while remaining rejected as a cure** (35% over-credit, 45 hard `yes|no`).

Caveats that bound this: n=4; and the run was configured *harder* than the screens that produced the 77% corpus figure, because seeding 10 champions means a newcomer must beat opus 4.0–5.0 documents to be named, where the original runs started from an empty survivor pool. 25% and 77% are not measuring the same bar.

---

## 2026-08-30 — two rescue instruments tested against the rejected piles; both fail unassisted

**The problem is real.** 44 opus≥4 documents sit in the mega-screen's rejected piles across t10–t14, including t12's `KR20260033205` at opus 8.0, a registered champion control. The screen's recall is 8/13 (62%) on t10 measured against a fully opus-read corpus.

**Instrument 1 — NLM core-of-invention rescue. REJECTED on evidence.**
The concept validates offline: cores derived from independent claim 1 recover **22/22** lost champions at 6.5–18% keep-alive cost, and the deriving agent correctly rejected t14's weight-5 `F4` as inherent (62.4% of the tab owns it). But asking NotebookLM to evaluate a core recovered **1/4 (25%)** over 84 documents, and is **not reproducible**: `CA2552849` (opus 4.0) was FOUND in the roster-12 experiment and MISSED in the live run — same question, same roster size, differing only in which documents shared its chunk. Stopped after 27 queries rather than the planned 405.

**Instrument 2 — supervised filter, unassisted. FAILS to prospect.**
k-fold precision on the LABELLED field is excellent (p@3 100/100/100 with the core signal added, which lifted t14 from 67%). It does not transfer to the unread rejected pile:

| how the document was chosen | picks | opus result |
|---|---|---|
| filter ranking + abstract read before picking | CN116190826, CA2142366 | **4.0, 4.0** |
| deliberate low-ranked control | CN115800429 | 3.0 (predicted low) |
| **filter ranking alone** | KR20190012058, KR101488054, KR20230074001 | **2.0, 2.0, 3.0** |

The validation measured the filter on documents opus had already chosen to read. The rejected pile is a different distribution and the precision did not carry. **The abstract-reading step was doing real work that was being credited to the filter.**

**Working method, 2 for 2:** filter to narrow → read abstracts locally (free, no quota, no reads) → pick → opus confirms. What fails is skipping the middle step.

**Also settled: graduate queues are not worth gridding.** The slow lane ran t13 51/51 (1 document above 20% coverage) and t14 61 (3 above 20%). Two tabs, 112 graduates, 4 documents worth anything. The 122 "outstanding opus reads" are cancelled, not deferred.

**2026-08-31 — the slow lane cannot order a shortlist either.** t13's rejected pile looked rich: 38 of 100 filter-top documents above 40% MUST coverage, two at 100%, against 1 of 51 in its graduate pile. Three opus reads on the top of that pile returned **3.0, 3.0, 2.0**. Both 100%-coverage documents are 3.0.

The coverage score was measuring the BENCHMARK's looseness, not the documents' relevance. t13 decomposes into 9 loosely-coupled elements (wireless link, control module, BMS port); t12 has 6 tightly-coupled ones and t14 has 22 — and t12/t14's rejected piles returned **0 documents above 40%** against t13's 38. Coverage percentages are therefore not comparable across tabs and must never be read as a relevance ranking.

Cumulative record of automated candidate selection against the rejected piles:

| method | picks | opus results | champions |
|---|---|---|---|
| filter + human abstract read | CN116190826, CA2142366 | 4.0, 4.0 | **2/2** |
| filter ranking alone | 3 on t12 | 2.0, 2.0, 3.0 | 0/3 |
| slow-lane coverage ranking | 3 on t13 | 3.0, 3.0, 2.0 | 0/3 |

**0 of 6 unassisted, 2 of 2 assisted.** Neither NLM instrument nor the supervised filter can order a shortlist; the judgement step between narrowing and reading is doing the work. This is the same ceiling the calibration measured from the other direction (opus>=4 mean coverage 38.7% vs opus=3 38.8% — the instruments stop one level above the decision).

---

## 2026-08-31 — the screen loses champions because of its QUESTION, not its roster size

**Roster size is NOT the cause. Falsified by dose-response.** Champion `KR20260033205` (t12, opus 8.0, rejected by the production screen at roster 20–39) was found at **every** roster size tested — 10, 15, 20, 25, 30 — each in its own fresh notebook, one question each, padded with opus≤3 distractors that genuinely resemble it (all owning fan and/or water-cooling components).

| roster | 10 | 15 | 20 | 25 | 30 |
|---|---|---|---|---|---|
| champion | FOUND | FOUND | FOUND | FOUND | FOUND |

Controls: with the champion REMOVED, the same question at roster 10 answered `PICK: NONE` correctly, so it does not force a pick.

**The cause is the question.** The production screen asks *"rank the TOP 10 candidates that best disclose the TARGET FEATURE COMBINATION"* over a weighted 6-feature checklist — so a document whose whole claim to relevance is one weight-5 element loses to documents scoring partially across several. The probe asks for the inventive **mechanism in prose**: *"which one takes air ALREADY CHILLED inside its water-cooled power supply and redirects it to the charging jig, so the enclosure stays free of CONDENSATION and the jig is cooled as a by-product"*.

**This also explains why the core rescue failed at 25%.** The cores derived from claim 1 were correct — the delivery was not. The rescue asked a CONJUNCTION OVER FEATURE NAMES (*"discloses ALL of: [name1] AND [name2]"*), which is still a checklist. The probe describes what the invention DOES. Same core, different question form, 25% versus 5-for-5.

**Methodological correction (user-caught):** the first probe asked all framings in ONE notebook, and NotebookLM keeps chat history within a notebook — so only the FIRST answer of that run was independent. `core-probe.py` now uses a fresh notebook per framing. All dose-response results above are clean by construction.

**Also confirmed:** the reasons NotebookLM gives are expert-quality and independently checkable — *"uses an internal heat exchanger ... rather than reusing chilled air from a water-cooled power supply"*. Comprehension was never the bottleneck. And figures are a corpus-wide blind spot worth noting separately: only **19 of 9720** documents have any figures captured, so a disclosure that lives only in a drawing is invisible to every instrument here.

---

## 2026-09-02 — "purpose beats components" FALSIFIED; the variable is over-specification

The mechanism-question finding (2026-08-31) held: a prose description of what the invention does finds champions a weighted feature checklist rejects. But the follow-on hypothesis — that a question naming the invention's PURPOSE beats one naming its COMPONENTS — does not survive a controlled test.

**Test.** t13's rejected pile (1304 docs), same account, same roster 30, only the question changed.

| form | question named | picks | result |
|---|---|---|---|
| component | "radio in, wired CAN/LIN bus out, in one box" | 5 | recovered `CN117692268` (opus 4.0); 2 unread candidates read at **2.0 and 3.0** |
| purpose | "replace the tethered signal generator — operator commands from a phone instead of standing at the pack with a wired box" | **0 in 780** | **`CN117692268` was ASKED and REJECTED** |

The purpose form is strictly worse on the one champion we can score.

**The likely variable is SPECIFICITY, not purpose-vs-components.** The t12 question that works at every roster size 10-30 is mechanically specific but narratively neutral — *"takes air ALREADY chilled inside its enclosure and redirects it, so the enclosure stays free of condensation"*. No operator, no workflow, no product framing. The failed t13 purpose form embeds a user, a device and a workflow; `CN117692268` implements the mechanism without matching that story.

This is the same over-specification that made the first t10/t13/t14 questions return NONE correctly (they embedded the claim's narrowest limitation — t10's "different frequencies", which only 2 of 2049 documents disclose). Two different costumes, one error: **the question must describe the mechanism at the level of abstraction the corpus can actually satisfy, and no tighter.**

**Cumulative record of the mechanism scan on genuinely UNREAD documents:** `CN115997317` (t14) **5.0** · `IT202100015797` (t13) 2.0 · `US20130138857` (t13) 3.0. **One in three.** Eleven opus reads across the session produced exactly one document above 4.0, and **no tab's best answer changed**.

**2026-09-02 — the mechanism scan recovers t12's opus-8.0 champion in a LIVE run.** `KR20260033205` — the highest-scoring document in t12, a registered champion control, rejected by the production screen — was the **sole pick from 1050 documents** scanned at roster 30. Precision 1/1. Its stated reason is the mechanism, not a component list: *"utilizes a fan to redirect air chilled inside its water-cooled power supply to the charging jig, keeping the power supply free of condensation while cooling the jig as a by-product"*.

Everything else had failed on this document: the feature-checklist screen rejected it; NLM's own per-document MATCH SCORE has **−0.00** correlation with opus on t12; the supervised filter never surfaced it; the slow lane returned **0 of 110** t12 documents above 40% coverage.

This is the first live confirmation that the mechanism question generalises beyond the controlled probe. It does NOT rescue the session's other numbers — 2/5 recall on t10, 1-in-3 on unread candidates, and no tab's best answer improved — but it establishes that a correctly-pitched mechanism description can pull a tab's best document out of a discard pile of 1369 for ~35 queries and zero Claude tokens.

The working question form, for reference: mechanically specific, narratively neutral, ending in a NONE escape. Not the claim's narrowest limitation (over-specification, returns NONE correctly), not a user/workflow narrative (over-specification, missed t13's known champion), and not a list of components in a relationship (t13's component form: 5 picks, 1 champion, two unread candidates at opus 2.0 and 3.0).

---

## 2026-09-03 — F3c-ns (`add_failed`) is CLOSED corpus-wide: 120/120 read, 10 champions, tail empty

The staging sink named in H7 as "the single largest champion sink in v2" is now exhaustively
measured rather than sampled. Every `add_failed` document on t10–t14 carries an opus verdict.

| tab | add_failed | opus-read | opus ≥ 4 |
|---|---|---|---|
| t10 | 16 | 16 | 0 |
| t11 | 27 | 27 | 1 |
| t12 | 26 | 26 | 0 |
| t13 | 24 | 24 | 2 |
| t14 | 27 | 27 | **7** |
| **total** | **120** | **120** | **10** |

**The last 6 reads (t14 tail, 2026-09-03) returned 0 champions.** Scores: KR102344538 3.0 ·
KR20260055095 3.0 · WO2025036486 2.0 · CN106992561 2.0 · KR20040066085 2.0 · JP2003207552 2.0.
The two 3.0s are near misses on the right axis — KR20260055095 has the BOL (initial-state)
reference comparison and a strain/change-rate computation — but neither reaches 4.

**What this changes.** t14's `add_failed` hit rate was quoted as 7 of 21 read (33 %) and was the
highest prior of any pool in the corpus; it is now **7 of 27 (26 %)**, and the yield is entirely
front-loaded in the re-queued cohort. The tail — the documents that stayed unstaged longest —
contributed nothing. The pool is drained, not merely sampled, so `add_failed` can no longer be
cited as a place where unfound champions might still sit. Improvement 3 of cycle 6 ("close
F3c-ns before spending another opus token") is DISCHARGED.

**Read economics.** 6 reads, 0 champions. This was the highest-prior batch available anywhere in
the corpus by an order of magnitude, and it still returned nothing above 3.0 — a further datum
for the ceiling already recorded on 2026-09-02 (marginal yield of the last 568 reads: 2
champions, 0.35 %). The remaining unread bulk (t11 863 · t12 986 · t13 1005 · t14 1268 = 4122
rejected documents) has a strictly worse prior than the pool that just came up empty.

**Residual after this closure.** The 49 documents scoring opus ≥ 4 that the screen rejected
(t10 5 · t11 25 · t12 7 · t13 4 · t14 8) remain the live recall problem, and they are free
controls: any re-pitched mechanism question is validated against verdicts already paid for.
