# Patent Benchmark Match Project — discussion journal

One-to-one record of what was discussed and discovered, session by session. This file is
the canonical copy; it is mirrored as a source into the NotebookLM notebook
"patent benchmark match project" (id 35690175-d37d-4de8-ac92-8254017063b5, profile: default).
Update rule: append new discussion below, then re-sync with scripts/sync-nlm-mirror.sh
(replaces the NLM source with this file's current content). Never rewrite history entries.

---

## Session 2026-08-12 — t11 (40_387_2026): opus read of all 349 graduates + NLM-funnel verdict

### Context
Experiment defined 2026-08-11: seriously validate the NLM mega-screen funnel on tab 11
(40_387_2026). NLM screened 1954 candidates in 51 rounds → 349 graduates → shortlist of 49
(all 49 opus-read). Question: does opus-5, reading ALL 349 graduates at full text, agree
with NLM's 49? Baseline snapshot: 49 opus-read, 134 sonnet-read, 166 unread.

### USER: "let us proceed with 40_387_2026 further, other two we will continue when I tell you, ok?"

Found state: the run was already mid-flight (256/349 opus-read). The user's /login had
rotated the Claude OAuth token at 05:47, killing all three running tab reads with
"Not logged in"; the auth watchdog (commit f35e49a) parked them and auto-resumed all three
at 05:49 once the reseed daemon delivered a fresh token — zero re-reads. Per the user's
directive, tabs 13 (EP25828490, 161 docs left) and 14 (EP25845081, 93 left) were PAUSED via
/deep-compare/pause (graceful, resume-safe); only tab 11 continued. A monitor was armed
(progress / stall / completion / tabs-13-14-resume warnings).

Prep discovery while waiting: the 51 per-round NLM answers are NOT persisted anywhere —
the mega-screen keeps only the parsed ledger (per doc: best in-round rank + round number)
in .nlm_screen_11.json, plus the finalize shortlist answer in chat. So NLM's per-round
reasoning cannot be mined after the fact. Marked as a funnel-observability gap.
NLM's finalize logic confirmed: CN220306367 ranked BEST as the benchmark's own sibling
filing (identical spec), CN117039286 SECOND-BEST as an independent invention.

### USER: "but in the tool it does not show me usual progress with 'pause' button, meaning that you run it in 40_387_2026 in background?"

Answer: the deep-read always runs server-side inside the patent-bench container; the
browser strip is only a status viewer. The page had stopped polling when the run briefly
died at the token rotation (pollRead does not reschedule once it sees running=false), so
the watchdog's server-side auto-resume was invisible to the open page. Reloading the page
or re-selecting the tab re-attaches the progress strip. Identified as a small real UI gap.

### USER: "yes, patch the pollRead heartbeat after the run completes"

Patched app.js: both idle branches of pollRead now keep a 30-second heartbeat so an open
page picks up a watchdog auto-resume by itself. Deployed after the run finished
(rebuild via scripts/serve.sh), commit 721045e, pushed to GitHub.

### RESULTS (run completed 06:15 UTC — 349/349 graduates opus-read)

**The verdict: NLM's shortlist and opus's champions disagree badly.**

- Opus top tier over all 349 (score >= 4.0, tie-aware) = 66 docs; only 18 of NLM's 49 are
  in it. 31 NLM picks scored <= 3.0; 48 opus champions were never shortlisted. Spearman
  rank correlation within the 49: 0.389 (weak).
- Biggest find: CN118156696 scored 7.0 — tying the two best known docs (CN117039286,
  CN220652165). Crown-feature YES, sonnet had it at 2.0, NLM ledger position 186/349.
  Completely invisible before this run.
- Five more 6.0-scorers NLM missed: CN220652168, CN110323367, CN109863626, CN209071393,
  CN220553513.

**H1 (feature lens) — CONFIRMED.** Crown MUST feature "Outer pole (112) injection-molded
with the cover body (100)" (weight 5): 21 YES-holders among the 349; NLM shortlisted only
6 of them. Across all 7 MUST features the opus top-66 has ~2-3x the YES coverage of
NLM's 49 (e.g. crown y6 vs y19; "inner pole connected to outer pole" y14 vs y34; "current
collector disk" y5 vs y13). NLM rewarded overall similarity over MUST coverage.

**H2 (gap-fillers) — different outcome than feared.** No feature lacks a YES inside the
opus top-66, and the fresh combi recompute over all 969 assessed docs returns an EMPTY
pairs list because CN117039286 covers all 7 MUST elements solo at YES (plus 13 additional
at YES) — a novelty-grade single reference, unchanged as champion. Second solo:
CN219553754 (6 YES + crown at partial). The run did not dethrone the champion; it widened
the crown-feature evidence pool from 6 known holders to 21.

**Mechanism of the misses:** the shortlist is cut by best in-round rank, so each doc
competes only against its own 39-doc round. Kept docs: median in-round rank 8; missed
champions: median 14 → round bias + rank-cut. Examples: US20220158279 (opus 5.0, crown
YES) was rank 15 in round 1; CN118156696 was rank 14 in round 44.

**Model-tier signal:** opus re-scored the 134 sonnet-read docs strictly upward — mean
+1.31, median +1.0, 114 higher / 20 equal / 0 lower. Sonnet screening scores
systematically underestimate near the cut. Extreme: CN118156696 2.0 → 7.0.

**Four funnel-adaptation proposals (for discussion, NOT yet built):**
1. Persist per-round NLM answers (observability).
2. MUST-aware shortlist cut — reserve slots for crown/MUST-feature claims, not pure
   in-round rank.
3. Cross-round normalization — e.g. a playoff round for near-winners, fixing round bias.
4. Opus for the border zone — don't let sonnet scores decide who is near the cut.

Artifacts: memory/session_logs/2026-08-12-t11-comparison{.md,-report.json},
-t11-nlm-shortlist-answer.md, -t11-nlm-ledger.json; analysis tools persisted in the
container as /data/.t11_{tick,compare,why}.py + /data/.t11_grad_ids.txt.

### USER: "write all this text in nlm source doc and all following chat also, and whatever we discussing here should be one-to-one written in this one single doc in nlm. This will help us in future to analyse what was discussed and discovered. Skill and lesson learned for this current project should be also in this nlm. Call this nlm as patent benchmark match project. Inside you can also create a source doc where you list the future implementation deffered list of feature."

Created NotebookLM notebook "patent benchmark match project"
(35690175-d37d-4de8-ac92-8254017063b5, profile default) with three sources:
1. This discussion journal (this doc — updated after every substantive exchange).
2. Skill & lessons learned for the patent-workbench project.
3. Deferred features / future implementation list.
Canonical copies live in the repo at docs/nlm-mirror/; scripts/sync-nlm-mirror.sh
replaces the NLM sources with the current file contents.

### USER: "based on experiment we just made, think how the flow should be updated in order to nlm to find out the same champions as opus 5 does"

Design analysis (plan mode). Two decisive data points sharpened the diagnosis:
1. **Discovery is NOT broken** — every one of the 66 opus champions DID graduate; the
   funnel failed only at the ranking/cut stage (349 → 49).
2. **Re-cutting the existing ledger cannot fix it** — recall of the 66 champions under
   the funnel's own ordering: @49→18, @100→23, @150→37, @250→51. Champion ledger
   positions median 139/349; best in-round ranks scatter 1–31. The per-round
   similarity-ranking signal itself is nearly uninformative; keeping more survivors or
   raising the cut is useless.

Design chosen: keep discovery untouched; add a separate **claims-audit stage** that
re-screens the graduates with a MANDATORY-feature checklist question and cuts the
shortlist by weighted MUST-claim score (absolute, cross-round-comparable → immune to
round bias).

USER decisions during planning:
- Rejects audit: "Yes, sample 50 rejects" — opus reads 50 random discovery-rejected
  docs to measure the discovery-stage false-negative rate (its one untested assumption).
- Rollout: "T11-style experiments only" — the default mega-screen flow stays unchanged;
  claims audit is a manually-triggered endpoint.
- USER (verbatim): "must-feature should be also backed by nlm with exact quotations
  which can be easily assesed and found by opus afterwards" → every claim must carry a
  verbatim quotation; quotes are verified IN CODE against the stored document text
  (substring after normalization + 4-gram shingle fuzzy fallback) — hallucinated quotes
  score 0 and opus later verifies pre-located passages instead of hunting.

### Implementation (2026-08-12, commit 2e982dd, deployed)

- `POST /api/tabs/{id}/claims-audit` (+ /status /pause /stop): background job over the
  graduates, rounds of 12 docs + benchmark in a dedicated "🧾 Claims" notebook,
  NLM_CLAIMS_PROMPT (MUST features only, weight-ordered, quote-mandatory), resume-safe
  state file, NLM-quota watchdog + boot re-arm — same self-healing as the mega-screen.
- Raw round answers persist in the new `nlm_claims` DB table (the mega-screen discards
  its answers — that's why the t11 post-mortem couldn't mine NLM's reasoning).
- Scoring: score = Σ weight of MUST features whose quote verified (fuzzy counts,
  unverified = 0); per-doc score+quotes land in nlm_score/nlm_score_note; dry-run by
  default, apply=true rewrites shortlisted/nlm_rank.
- 6 unit tests for the parser + quote verifier; full suite 331 passed.
- **Validation launched (in flight)**: claims-audit dry-run over the 349 t11 graduates
  (~30 rounds, zero Claude tokens) + opus deep-read of 50 seeded-random rejected docs
  (seed 40387, sample of the 986 unscored fetched rejects). Success metric: recall@49
  of the opus top-66 vs the baseline 18/66; plus quote-verification rate and the
  per-feature NLM-vs-opus agreement matrix; plus discovery FN rate from the 50 rejects.
