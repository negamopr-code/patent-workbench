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
