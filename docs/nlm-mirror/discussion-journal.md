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

### REJECTS AUDIT RESULT (2026-08-12): discovery is NOT loss-free

2 of 50 randomly sampled rejected docs are opus champions: **JP2022548488 = 5.0**
(two-piece inner/outer pole, welded pole connection, clamping groove) and
**CN222927625 = 4.0**. FN rate 4%, Wilson 95% CI [1.1%, 13.5%] → **~39 champion-grade
docs expected among the 986 unscored fetched rejects (CI 11–133)**. The earlier claim
"discovery is sound because all 66 known champions graduated" was survivorship bias —
champions could only be seen among docs opus had read. A mid-run transient NLM network
error (round 6, "peer closed connection") was resumed manually; noted as a deferred
improvement (auto-retry for network-class errors — quota pauses already self-heal).

### USER: did you really understand the problem? How to choose the 349 graduates RELIABLY and be sure the ~2000 rejected contain no doc covering all MUST features completely?

Answer given: the running claims audit fixes RANKING only; it cannot recover docs that
never graduated. Discovery leaks because it is single-elimination with a noisy judge
(one similarity top-10 judgment per doc, one appearance, no evidence). Initial proposal:
per-MUST-feature discovery question corpus-wide + K≥2 redundant passes + acceptance
sampling of rejects as standing QA; absolute certainty impossible without opus reading
everything, but the miss probability becomes measurable and boundable. The running
audit doubles as the calibration (per-pass sensitivity over the 66 known champions).

### USER refinement: graded coverage IS the redundancy

USER (essence): false negatives are absorbed by the per-feature grading — if NLM
wrongly misses ONE feature the doc still stands at 6/7, high in the ranking; and with
few good documents, lower grades simply flow into the "worth checking by opus" pool.
Agreed and adopted: a true 7/7 doc has 7 independent-ish chances per pass; rejecting it
requires missing all 7 (≈0.02% at sensitivity 0.7). K full passes are unnecessary —
the remaining FN channel is doc-level correlated failure (doc never "looked at"), which
shows up as ZERO-claim docs; only those need a cheap second pass in reshuffled batches.
The opus pool becomes adaptive: descend the coverage ladder (7/7 → 6/7 → …) until the
opus budget fills — the cut is relative to what exists, not a fixed rank.

### USER inversion: per-FEATURE database notebook — "run once, it is not wasted"

USER (essence): assess per feature, not per document — a second NLM notebook (same
account, separate folder) where each SOURCE is one MUST feature holding all claiming
documents + their quotations, populated as a byproduct of the same single pass; at the
end we own a per-feature database we can pick any doc from — even rejected docs'
evidence is preserved. Adopted with two mechanics: (1) SQLite (nlm_claims table) stays
the source of truth, the "📚 Features" notebook is a synced VIEW rebuilt by
delete+re-add (source ops cost no Q&A quota) — same pattern as the journal mirror;
(2) one round answer cannot carry BOTH a top-10 judgment AND quoted claims for 39 docs
— but the top-10 similarity judgment is obsolete anyway: graded coverage subsumes the
ranking, so the claims sweep is the single pass. Bonus: the feature notebook directly
answers gap-filler/combination queries ("who discloses feature 5?") — the H2 use case.
Immediate payoff: seed the notebook for the 349 from the audit's claims table, zero
extra NLM queries.

### Early instrument reading (10/30 rounds, 120 docs audited)

372 claims, **86.3% quote-verified** (164 exact + 157 fuzzy), 51 unverifiable zeroed by
the code guard — NLM backs most claims with real text; the hallucination guard is
earning its keep. Crown feature scarce as expected (7 claims); broad MUSTs (F6, F7)
claim widely.

### Mid-run events (2026-08-12 afternoon)

- Tabs 13+14 opus deep-reads COMPLETED cleanly (t13: all 153 read, 410 assessed total;
  t14: all 81 read, 326 assessed, combi auto-judged) — both EP tabs now hold full opus
  ground truth, ready for the same NLM-vs-opus validation as t11.
- t11 claims audit QUOTA-PAUSED at round 21/30 (252/349 audited); watchdog auto-resumes.
  Deploy window used: M/A/W letter badges on feature chips shipped (7e123ff) — USER asked
  to clearly differentiate mandatory vs additional features in the verdict list.
- **Preliminary comparison at 252/349** (provisional): claims-top-49 holds 19/47 audited
  champions (~40% vs old funnel 27%); adaptive ladder much stronger — pool@80 = 62%,
  pool@120 = 70%. Buried champions surfaced: CN220652168 (opus 6.0, old ledger pos 207)
  → position 3 with verified crown claim + 7/7 MUST; CN109863626 → position 7. Tail of
  ~6 champions at positions 181–248 (NLM claimed nothing verifiable) — per-feature
  agreement matrix at completion will show which features NLM cannot see.
- work2 re-authenticated by USER via nlm-keeper noVNC; CLI profile healthy after the
  keeper's cookie snapshot.

### USER: "we now know how to find right docs from 349, but what about the choice of 349 from ~2000? We should have the same mechanism, right?"

Confirmed: same mechanism corpus-wide — claims ladder over ALL fetched candidates,
"graduates" dissolve as a concept (the ladder IS the selection), zero-claim docs get one
second-chance pass in reshuffled batches, old mega-screen keeps running in parallel for
comparison. The endpoint already accepts doc_ids = whole corpus (no new code for the
sweep itself); small pending piece = merging claims across runs from the nlm_claims
table for the second-chance pass. Cost fork for t14: full corpus ~153 quoted rounds
(multi-day) vs graduates-only ~28 rounds (~1 day).

**USER decision: graduates-only for t14** (full-corpus sweep deferred). t14 claims audit
LAUNCHED on work2 (326 graduates, 28 rounds, dry-run) — first live use of the per-tab
account parallelism: t11 on default + t14 on work2, independent quotas.

### Discovery-filter recall measured (2026-08-12): survivor filter ≈ 45% (30–70%)

Full accounting of t11's 1954 fetched: 66 champions among 349 graduates (complete opus
knowledge) vs an estimated **~80 champions hiding in the 1605 rejects**: 2 confirmed in
the 50-doc opus sample (4%, CI 1.1–13.5% → ~37 in the 936 unscored) + ~42 estimated in
the 619 sonnet-scored rejects via calibration (sonnet 2.0 → 32% opus-champion rate,
17/53; the rejects hold 101 docs at sonnet 2.0). Verdict per USER's decision rule: the
survivor filter is NOT good enough as sole discovery — adaptation justified. Probes
proposed: opus read of the 101-doc sonnet-2.0 pocket (token cost, awaiting go) + free
NLM per-feature sweep of all 1605 rejects (queued behind quota).

### T14 CLAIMS AUDIT VERDICT (completed 2026-08-12 evening, work2, 28 rounds)

317/326 graduates claimed; vs 42 opus champions among the audited: OLD similarity
shortlist 10/42 (24%) — claims ladder@49 **18/42 (43%)**, @100 23/42. Only 12 docs
overlap between old and new top-49 (75% turnover): 37 newcomers incl. **12 champions
the old shortlist never ranked** (EP3166200, CN113646652, US20220209563, US12313687,
CN111049204, CN115514036, AU1321292, CN107431369, CN112601969, US20250047121,
CN116961149, EP4064412); 36 dropped of which only 4 were champions (old ranks 15/23/
35/41). Quote verification 71% (translation-heavy corpus, many fuzzy). **NEW LESSON —
MUST SATURATION**: t14's top-15 all tie at max claim score with crown verified (its 5
MUSTs are broad) → the MUST ladder stops discriminating at the top; ordering power for
such benchmarks lives in the ADDITIONAL features → stage 2a/2b is essential, not
polish. Dry-run: the tool's shortlist untouched.

### SONNET-2.0 POCKET RESULT (user-approved opus read, completed 2026-08-12 evening)

101/101 rejected sonnet-2.0 docs opus-read: **23 champions (22.8%)** — measured vs the
32% calibration, inside the CI. Top finds: CN220233407 6.0, CN119381711 6.0, five
5.0s. Updated discovery recall of the old similarity screen: 66 champions kept vs
~70 (23+2 measured + ~45 estimated remaining) discarded → **still ≈ 50%**. These 23+2
champions are the instrument-independent label set for validating the per-feature
discovery sweep (avoiding a second survivorship trap). ⚠ Ops lesson: the deploy after
the pocket read killed its final ranking-compile chat message (scores all safe in DB;
re-rank recoverable) — deploy gates must BLOCK on .claude_read_*.lock, not report it.

### Claims-audit engine extended + t14 stage 2a launched (commit b: feature-kind + quotes-free)

Added `features: must|additional` and `quotes: bool` to the claims audit (batch 35
quotes-free; 'claimed' status counts in recall mode; A-runs never touch nlm_score or
the shortlist; state of the finished MUST audit archived; nlm_claims rows carry kind).
332 tests green, deployed. **t14 stage 2a LAUNCHED**: 22 A-features, quotes-free, over
the 279 quote-confirmed docs, 8 rounds on work2 — first live run of the target flow's
stage 2a. Next: 2b = quotes only for the doc×feature pairs 2a claims.

### Quota economics + USER's target flow (agreed on paper 2026-08-12)

USER: per-feature approach makes the most sense — MUST-only sweep over ~3000 docs in
NLM, zero Claude tokens, NO survivor mode needed in parallel long-run; graduates
(= docs fulfilling the MUSTs) then get a second NLM round for additional features.
Clarified: quota is charged per QUERY not per answer length — quotes just shrink
docs-per-round (12 vs 35), so quotes-free stage 1 costs 86 rounds vs ~300 quoted for
3000 docs. Unquoted per-feature is STILL far more precise than the similarity screen
(absolute per-doc criterion, no top-10 cap, graded 0–10 coverage, 10 chances per doc);
quotes add verification, applied where trust is consumed (stage 1.5 on graduates).
Full spec + budget (~135 rounds core, 2–3 days one account) recorded in the deferred-
features doc as TARGET ARCHITECTURE, with validation gates. Adopted parameters:
adaptive-ladder graduation bar (not strict all-MUST) + second-chance pass included.
T14 transient work2 auth error at round 7 (keeper cookie snapshot mid-call) resumed
manually — auto-retry for network-class errors promoted on the deferred list.
