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
    "R2-batch-corridor": {"count": 119, "approved": "2026-08-20 user 'go ahead'", "scope": "the COMPLETED legacy v2 sweep ran roster-35 before the corridor doctrine — its results are discovery-only forever and every conclusion carries recall: 1/14; future sweeps run roster ≤12 + follow-up stage"}
  },
  "10": {
    "R2-batch-corridor": {"count": 59, "approved": "2026-08-20 user 'go ahead'", "scope": "the COMPLETED legacy sweep ran roster-35 — discovery-only forever, conclusions carry recall: 7/12; future sweeps run roster ≤12 + follow-up stage"}
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
