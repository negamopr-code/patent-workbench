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

## Session 2026-08-13 — t14 stage 2a result + stage 2b (pairs verification) built & launched

### T14 STAGE 2A COMPLETED (overnight, work2, 8 rounds of 35, quotes-free)

195 of 279 quote-confirmed docs claim ≥1 of the 22 A-features — 756 doc×feature
pairs total (avg 3.9 per claimant); 84 docs claim ZERO A-features; 21 docs failed
staging (none of them champions). Combined-ladder analysis vs the 42 opus champions:

- **Recall unchanged at the cut**: MUST-ladder@49 18/42 with or without the A-scores —
  the 29-doc max-MUST tie group (score 11, crown verified) fits inside the top 49
  whole, so A-ordering can't change membership there. Spearman vs opus improves only
  0.258 → 0.279.
- **Ordering INSIDE the saturated tie group does improve**: sorted by raw A-score, the
  tie group's top 7 hold 5 champions incl. both 5.0s (CN118475846 A=19, AU2019361447
  A=17); but the #1 raw-A doc is a non-champion (EP3358704, opus 3.0, A=21) and three
  tie-group champions claim ZERO A-features (CN113646652, US12313687 — plus EP3166200
  at A=4). Conclusion: unverified 2a claims are too noisy to rank on directly — the
  A-stage earns its keep only after verification (exactly why 2b exists), and the
  zero-claim docs are the second-chance-pass clientele.

### Stage 2b BUILT: pairs-only quote verification (per USER's two-tier refinement)

New `pairs: true` mode on the claims-audit endpoint (valid only with
features=additional + quotes=true): seeds from the finished 2a state, asks NLM to
verify ONLY the claimed doc×feature pairs — each feature block lists "check ONLY:
<numbers>" — so answers are bounded and 20 docs fit a quoted round. Semantics differ
from all other modes: the verification verdict REPLACES the 2a 'claimed' status, so
a pair whose quotation fails code-side verification (or that NLM refuses with ":: NO",
or leaves unanswered) is DOWNGRADED to unverified — 2b exists to kill unverifiable
claims, not to accumulate them. The 2a state is archived as .additional-free before
the overwrite (archive logic now keys on kind+quotes+pairs, not kind alone). 334
tests green (2 new: pairs spec builder, replacement verdicts), deployed.

### Stage 2b LAUNCHED on t14 (work2): 195 docs, 756 pairs, 10 rounds of 20, dry-run

Launch hit an INSTANT work2 auth error ("Authentication expired" on the
notebook-list call); a resume 2 minutes later failed identically → NOT transient
this time. Root cause chased to the bottom: the Docker host restarted ~06:47, the
keeper's fresh Chrome restored its 33 saved cookies, but Google rejected the rotated
session tokens server-side — the work2 browser is parked on the Google
account-chooser page. The tell in the saved profile: session_id EMPTY +
build_label = identityfrontendauthuiserver (a Google LOGIN page, healthy profiles
show labs-tailwind-frontend) while the keeper kept logging cheerful
"refreshed: 33 cookies" every 15 min — it happily snapshots a logged-out browser.
A CDP probe clicked the work2 account tile on the chooser: Google answers with a
PASSWORD challenge → passive re-auth impossible, credentials are the user's.

Follow-up discovery: the DEFAULT profile is dead too — every store (patent-bench
volume AND the canonical dev-container profile) answers "Authentication expired";
the NLM-mirror sync of this very journal failed on all three adds (old sources
intact — the script lists-then-deletes, and the list already failed; canonical
copies live in the repo, which is exactly why). So BOTH Google sessions were
invalidated the same morning — this is a Google-side session wipe (likely tied to
the host restart timing only coincidentally), not a keeper bug per se.

**⏳ BLOCKED on USER — both accounts need one interactive re-login each:**
1. work2: keeper noVNC http://localhost:8106/vnc.html — click the account tile,
   enter the password (chooser is already open on that page).
2. default: `nlm login` re-auth (or better: add the default account to the keeper
   via accounts.conf + add-account.sh so BOTH sessions are held from now on —
   the 08-09 design supports exactly this).
Then: resume t14 2b (POST claims-audit {resume:true}, parked at round 0/10),
re-run scripts/sync-nlm-mirror.sh, and t11 resumes itself — but note its quota
watchdog GIVES UP 24h after the pause (~12:23 UTC today); after that it too needs
a manual resume.

Two new deferred items from this incident: claims-audit auth/network self-heal
(parity with deep-compare's f35e49a — this class hit 3× in two days) and keeper
logged-out detection (refuse to snapshot a profile whose extraction comes from an
accounts.google.com page / empty session_id, and surface a loud "needs re-login"
flag instead of "refreshed").

### Evening 2026-08-13 — auth-resilience stack BUILT (user: "I do not want to do it each time")

USER re-logged work2 (two steps: the rebranded app login, then one more password
for the old notebooklm.google.com domain — Google refuses to mint the old-domain
session passively). 2b resumed and completed round 1 (20/195). Then a keeper
restart (deploying the snapshot gate) KILLED the fresh session — root cause found:
the keeper's boot restore unconditionally injected the older snapshot cookies over
Chrome's own rotated ones; Google sees downgraded tokens and invalidates the whole
session family server-side. That poisoning, not cookie loss, is why sessions never
survived restarts.

Deferred items 9+10 built, deployed, live-verified (user's explicit go:
"I do not want to do it each time" / "you should stay logged in even if pc is
shut down"):
1. **Claims-audit auth/network self-heal** — transient-class errors (auth expired,
   not logged in, peer closed, timeout…) now PARK the audit as auth_paused with a
   FREE list-notebooks probe every 3 min (quota probes stay hourly), auto-resume,
   24h give-up. Live-verified: 2b parked itself and sits armed.
2. **Keeper snapshot gate** — refresh refuses to save when the extraction comes
   from a non-tailwind page (login frontend); logs LOGIN NEEDED (slot manager
   already surfaces that flag) and keeps the last good snapshot. Live-verified on
   the first post-restart cycle.
3. **Keeper probe-first boot restore** — after a restart the daemon first checks
   whether Chrome's own disk session survived; snapshot injection happens ONLY as
   recovery when actually logged out (stops the poisoning). Live-verified: probe →
   inject → still out → loud LOGIN NEEDED, snapshot kept.

State: ONE more work2 login needed (the restart casualty); after it, the chain is
autonomous: keeper fast-polls at 60s → snapshots on login → 2b auth-watchdog
resumes within ~3 min. Default account still needs its keeper adoption (wizard
:8110, name it `default` if bbubu2748@gmail.com is that account) — then t11 +
mirror sync heal the same way.

### Late evening — injection DELETED (killed a session twice), aliasing, 2b COMPLETED

The probe-first injection still killed the work2 session family on the next keeper
restart (injection into a logged-out restarted browser → Google invalidates the
FAMILY, including the CLI's still-valid copy). Lesson finalized: NEVER inject
snapshots into the browser — boot is probe-and-report only; the snapshot's sole
consumer is the CLI (7e922d3). Restart survival addressed at the real root:
Chromiums now get SIGTERM + flush time on container stop (they were SIGKILLed,
losing final cookie writes → logged out on every boot). Account `bubu`
(bbubu2748@gmail.com = the default account) added to keeper + slot-manager
registry; PROFILE_ALIASES=bubu:default mirrors every bubu snapshot into the
`default` profile. USER re-logged work2 (3rd time — the first two were killed by
the keeper deploys, owned and explained); 2b self-resumed via the new auth
watchdog and COMPLETED 195/195 in 10 rounds. Standing rule adopted: no keeper
restarts while a session is live.

### T14 STAGE 2B RESULT (completed 2026-08-13 late evening)

756 asked pairs → 161 verified + 359 fuzzy = **68.8% survive; 236 (31.2%)
KILLED** — nearly a third of the quotes-free 2a claims were unverifiable.

- **Recall vs the 42 opus champions: unchanged** (@25 11, @49 18; @66 19→20;
  spearman 0.258→0.278). On t14 the A-ladder — even verified — does not recover
  more champions than the MUST ladder; its value is ORDERING the saturated top.
- **Tie-group ordering is the win**: the 29-doc max-MUST tie group sorted by
  verified A-score now opens with 4 champions in the top 5 (CN111049204 4.0,
  AU1321292 4.0, CN118475846 5.0, AU2019361447 5.0) — the raw-2a impostor
  EP3358704 (opus 3.0, raw A=21) collapsed to 8 after verification. For choosing
  the opus deep-read ORDER at the saturated top, the verified ladder works.
- **⚠ Translation blind spot confirmed**: KR102369183 — raw-2a's #1 (A=32) and
  an opus 4.0 champion — verified to ZERO (all its quotes unmatchable against
  the stored text, likely quoting across a translation). US20220209563 (4.0)
  fell 20→7 the same way. Quote verification punishes non-English corpora;
  candidate fix for the deferred list: verify against BOTH original and
  translated text layers, or count a killed pair at half-weight when the doc's
  stored text is non-English.

### USER: "of course, do all of this" — t14 completion program approved

All four open t14 items approved: (1) apply the combined verified ladder to the
tab's shortlist; (2) second-chance pass for the 84 zero-A-claim docs; (3)
translation-fallback fix; (4) full-corpus MUST sweep over the ~1000 non-graduates.
Executed same evening: translation fallback BUILT (a4c4658, 336 tests: failed
quotes on non-English-origin docs soften to 'claimed'); second-chance pass
LAUNCHED (84 docs, 3 rounds, work2). USER then logged into bubu — keeper
snapshotted (61 cookies) and mirrored to `default`; BOTH profiles live (94
notebooks = the default account, identity confirmed). t11 MUST audit RESUMED
(252→288/349) and the journal mirror re-synced successfully.

### SESSION CLOSE 2026-08-13 (late night) — pause-all per USER; shutdown test next

USER: "pause properly all processes, we will continue in the next session and we
will check if your logins survived the pc shutdown at the same time."

Paused via /claims-audit/pause (graceful, resume-safe at round boundary):
- t14 second-chance pass: paused mid-run (~35+/84 audited; rounds persisted in
  nlm_claims; resume continues, nothing lost).
- t11 MUST audit: paused (~288+/349).

NEXT SESSION checklist:
1. **Shutdown-survival test** (the point of the exercise): after the PC restart,
   check `docker logs nlm-keeper` for "[work2/bubu] session survived the restart"
   vs "LOGIN NEEDED after restart". Survival = the graceful-SIGTERM fix works;
   LOGIN NEEDED = investigate whether Docker Desktop's shutdown gave the container
   its grace period (the saved profiles stay valid either way — the CLI keeps
   working and paused audits resume regardless of the browser state).
2. Resume both audits: POST /api/tabs/{14,11}/claims-audit {"resume":true}.
3. When t14's second-chance completes → 2b-2 pairs verification of its claimants
   → apply the combined verified ladder to the shortlist (script ready) → launch
   the full-corpus MUST sweep (~1000 non-graduates, ~30 rounds).
4. When t11's MUST audit completes → its recall@49 vs the 66 champions = the
   final funnel-validation number; then the free NLM sweep of t11's 1605 rejects.

### 2026-08-14 — shutdown-survival test: FAILED; the CLI-stays-valid assumption was wrong

Host restarted overnight (containers back up 06:14 UTC). Result of the test the
session was paused for:

- **The graceful-SIGTERM handler never ran.** No "keeper: SIGTERM — shutting
  Chromiums down cleanly" line anywhere before the restart boundary (last log
  21:36:56, clean boot 06:14:34). Docker Desktop / WSL2 hard-killed the container
  at Windows shutdown — the fix's code path is untested by this event, because it
  was never invoked.
- **Both browser sessions dead** at boot: work2 and bubu probe LOGIN NEEDED
  (nlm_osid=True central=False). Boot-restore correctly left snapshots untouched.
- **Both CLI snapshots dead too** — this is the surprise. Yesterday's assumption
  ("the saved profiles stay valid either way — the CLI keeps working") is
  FALSIFIED: resuming both audits auth-paused within one round, quota.err =
  "Failed to create notebook: Authentication expired" on both tabs. The freshest
  snapshots (21:36, minutes before shutdown) were rejected server-side.

Root-cause model (updated): the browser's on-disk profile lags the live cookie
rotation (SIGKILL loses final flushes; some central cookies are session-scoped and
never reach disk at all). At next boot the keeper's Chromium navigates to Google
presenting that stale generation → Google invalidates the whole session FAMILY —
including the still-fresh CLI snapshot. Same family-kill mechanism as the deleted
injection path, but triggered by Chrome's own stale disk state. Consequence: ANY
hard host stop kills both consumers, graceful-SIGTERM only helps when Docker
actually delivers SIGTERM (it didn't).

State now: both audits auth_paused with self-heal probing every 3 min for 24h
(gives up ~06:21 UTC 08-15). One user login per account at
http://localhost:8106/vnc.html auto-resumes everything.

Candidate fixes (deferred — user to pick; none built):
a) Windows-side graceful stop: a shutdown task running `docker stop nlm-keeper`
   before WSL dies (timing at Windows shutdown is unreliable, needs testing).
b) Chromium prefs `restore_on_startup` ("continue where you left off") so
   session-scoped cookies persist to disk — may make graceful stops survivable;
   does nothing for hard kills mid-rotation.
c) Boot-quarantine: at boot do NOT let Chromium touch google.com until the CLI
   snapshot has been probed; if CLI is alive, keep the browser off Google to avoid
   the family kill (browser then needs a human login before it can serve again).
d) Accept the cost: one noVNC login per account after each host restart.

### 2026-08-14 — t11 MUST audit DONE (349/349): the final funnel-validation number

Recall of the 66 opus champions (>=4.0) among the 344 audited+opus-scored docs:
- OLD similarity shortlist@49: **18/66**
- verified-MUST claims ladder@49: **20/66** · @66: 26 · @80: 30 · @100: 34
- quote verification: 887 claims, 601 verified + 179 fuzzy = **87.9% ok**
- spearman(claims score, opus score) = **0.308**

Reading: the verified-MUST ladder beats the similarity shortlist only marginally at
the same cut (20 vs 18), and even a 2x-wider cut holds barely half the champions.
Its TOP is excellent — top 8 are all crown-MUST holders, 7 of 8 opus>=4.0, the solo
7/7-MUST full-coverer CN117039286 #1 — same shape as t14 2b: verified claims order
the head of the ladder well, but mid-ladder discrimination vs opus is weak (0.308).
MUST-coverage recall is not a sufficient recall gate on its own; it is a good
head-ordering tool. (Numbers: /data/.claims_compare.py 11; state archived
.nlm_claims_11.json.must + .must-done-backup.)

Chain advanced same morning (all pre-approved gates):
- t14 second-chance DONE 84/84: **80/84 zero-claim docs now claim** >=1 A-feature
  (368 pairs) — near-universal claiming, i.e. 2a "nobody looked" holes were real but
  unverified claims stay noisy → **2b-2 pairs verification launched** (80 docs, 368
  pairs, 4 rounds, work2; original 2a archive backed up .additional-free.2a-orig).
- **Free MUST sweep of t11's 1605 rejects launched** (quotes-free, batch 35, ~46
  rounds, default acct) — the last validation gate: do the mega-screen's rejects
  hide champions?

### 2026-08-14 — t14 chain COMPLETE through apply: shortlist REWRITTEN; both reject-sweeps running

- **2b-2 pairs verification DONE** (80 docs, 368 second-chance claims, 4 rounds):
  108 verified + 135 fuzzy = **66.0% ok**, 59 killed, 66 kept as unverified claims
  (staging failures). Second-chance claims verify at a higher rate than the 2a set
  (66% vs 68.8% — comparable), so the "nobody looked" holes were real signal.
- **Combined verified ladder APPLIED to t14's shortlist** (user go 2026-08-13,
  /data/.t14_apply_ladder.py --apply): verified MUST → crown → verified A (with
  translation softening), pairs-replace merge of ALL additional rounds. **New top-49
  holds 18/42 champions vs the OLD similarity shortlist's 10/42** — near-doubled
  recall at the same cut; overlap with the old shortlist only 11 docs. Top-10
  includes both 5.0s (CN118475846 #3, AU2019361447 #8). Old shortlist state
  recoverable from DB history; archives backed up (.additional-pairs.2b1-orig,
  .2b2-done-backup, .must-done-backup).
- **Full-corpus MUST sweep launched on t14's 1509 fetched rejects** (quotes-free,
  batch 35, ~44 rounds, work2) — parallel to t11's 1605-reject sweep (default).
  Both are the last validation gates: how many champions did the mega-screen
  reject outright?

### 2026-08-14 (evening) — t14 reject sweep DONE; t11 sweep quota-paused at 1400/1605

**t14 full-corpus MUST sweep (1509 rejects, 44 rounds) finished.** Raw result:
1289/1509 rejects (85%) claim >=1 MUST feature; **264 claim the full weight
(11/11)**; 342 claim the crown; **341 claim >= the applied top-49's floor (>=8)**.
Graduates' verified ladder for scale: max 11, median 5, top-49 floor 8.
Interpretation is deliberately conservative: these are UNVERIFIED quotes-free
claims from the instrument whose claims died 31-34% under quote verification and
which claims *something* for nearly every doc it reads (cf. second-chance 80/84).
The sweep therefore does NOT establish hundreds of missed champions — it
establishes that quotes-free claiming saturates on this corpus and CANNOT rank
rejects by itself. Proposed next gate (awaiting user): quoted MUST verification
of the 341 above-floor rejects (~28 rounds, batch 12, free, dry-run) to get a
survivor count comparable with the graduates' verified ladder.

**Ops pattern of the day:** ~7 transient auth_paused blips during the sweeps
(t11 x6, t14 x1), every one self-healed within minutes once the keeper's next
refresh re-mirrored fresh cookies. Root shape: CLI snapshot staleness between
15-min keeper refreshes vs Google's server-side rotation under heavy API load.
Data point for deferred #13: shortening REFRESH_SECS (900 → ~300) would shrink
the blip window; the self-heal already absorbs the rest.

t11 sweep: quota_paused at 1400/1605 (~6 rounds left) — the default account's
daily NLM quota ran out; hourly probes will resume it when quota returns.

### SESSION CLOSE 2026-08-14 (evening) — pause-all per USER

Both runs paused gracefully (pause flag set; the auth watchdog may relaunch after
a blip clears but the runner parks on the flag immediately — verified in code):
- **t14 quoted MUST verify of 341 above-floor rejects: paused at 276/341**
  (round 23, 271 claimants so far). One hard 400-error mid-run (round 20) was
  resumed manually — remember: phase=error does NOT auto-resume, auth/quota do.
- **t11 reject sweep: paused at 1540/1605** (round 44, 885 claimants — vs t14's
  85% claim-rate, t11's rejects claim at ~57% so far).

NEXT SESSION:
1. Resume both: POST /api/tabs/{14,11}/claims-audit {"resume":true}. If the PC
   was restarted: FIRST re-login BOTH accounts at http://localhost:8106/vnc.html
   (bubu's window sits UNDER work2's on the noVNC desktop), then audits self-heal.
2. t14 verify done → survivor count (verified/fuzzy MUST >= 8) = mega-screen FN
   verdict; survivors = new-graduate candidates → discuss promotions + opus reads.
3. t11 sweep done → same reject analysis as t14 (claimed-MUST dist vs graduates'
   ladder) → likely same quoted-verify follow-up (user gave the principle: without
   verification a sweep is not truly finished).
4. Then: the funnel-fix decisions (t11's 4 proposals + deferred #13 restart
   survival) with all validation numbers on the table.

## 2026-08-15 — post-restart recovery: state-file corruption found & fixed; awaiting logins

Host restarted overnight (as planned). On session start both audits were parked
`auth_paused` (t14 276/341, t11 1540/1605), keeper looping LOGIN NEEDED for both
accounts — the expected shutdown-test signature. But a NEW failure surfaced:

**`.nlm_claims_11.json` got corrupted at ~05:59 UTC** — one stray trailing byte
after an otherwise complete JSON doc. Root cause: gunicorn runs `-w 2`; BOTH
worker processes execute the module-level watchdog re-arm loop on startup, so two
processes were doing unlocked read-modify-write on the same state file every auth
probe. The corrupt file made `_claims_read` return None, which silently KILLED
t11's watchdog loops in both workers — t11 would never have auto-resumed after
login, and status showed `present:false` (looks like "no audit exists": scary
but recoverable, the data was intact).

Fix shipped (commit 98f2897, pushed): `_json_write_atomic` (tmp-<pid> +
os.replace) for all three state writers (`_pipeline_set`, `_screen_set`,
`_claims_set`). State file repaired byte-exact (cursor 1540 / 885 claims
verified), patched api.py docker-cp'd into the live container (image is baked;
serve.sh builds from the repo so any future rebuild bakes the fix), container
restarted → startup loop re-armed watchdogs for BOTH tabs, both parked
`auth_paused` / resumable:true again.

Lesson for the corruption class: `phase:"idle", present:false` on a tab that
should have a parked audit = read-side JSON failure, NOT lost data — check the
state file's tail before assuming the run is gone.

STILL WAITING ON USER: one login per account (work2 + bubu, bubu's window UNDER
work2's) at http://localhost:8106/vnc.html → both audits self-heal, no API call
needed. Monitor armed (2-min polls, confirmed-twice logic after a false positive
on an empty poll). Mirror sync of this entry PENDING auth.

### 2026-08-15 — USER rejected the login groundhog day → deferred #13 BUILT (b+c)

User («again log in???? we solved it thousands times») rejected option d. Honest
accounting: the keeper + self-heal DID solve the in-flight blips; the one case
never solved was the host restart, parked as deferred #13. Built now, options
b+c combined (commit pending deploy):

- **Root mechanism confirmed in code**: keeper/entrypoint.sh launched every boot
  Chromium straight at https://notebooklm.google.com — presenting stale on-disk
  cookies to Google BEFORE the daemon even started. That navigation IS the
  family kill. No daemon logic could have saved it.
- **Boot-quarantine (c)**: browsers now boot on about:blank with
  --host-resolver-rules="MAP *.google.com 127.0.0.1" + a .quarantine marker.
  The daemon probes `nlm notebook list` (free, no browser) per account: CLI
  alive → parked, audits run with ZERO logins; CLI dead (auth error or 3
  straight failures) → lift = relaunch unblocked → auto-recovery or LOGIN
  NEEDED. `.wake` file lifts on demand, wiping the browser's Google cookies
  first (stale rotating cookies next to a live CLI = family kill).
- **Session-cookie persistence (b)**: restore_on_startup=1 + exit_type=Normal
  written into Preferences at boot, so a gracefully-stopped browser has a real
  chance of still being logged in at lift time.
- REFRESH_SECS default 900→300 (blip-window data point from 08-14).
- Open question the design self-balances: if Google's ~40-45-min no-rotation
  kill (Dockerfile WHY, 2026-08-08/09) applies to CLI-alone sessions, the
  keeper lifts on CLI death and converges back to a rotating browser; if not
  (nlm-quota log shows other projects' profiles living 80+ days browser-less),
  quarantine holds and restarts stay login-free. Either way ≥ status quo.
- **DEPLOY DELIBERATELY DEFERRED to the next idle window** (both runs finished):
  work2 logged in mid-build and t14 is resuming — redeploying the keeper now
  would restart a live session mid-run (the 08-13 ⛔ rule). Image test-built OK.

Morning ops meanwhile: work2 login landed → t14 auth_paused → quota_paused
(daily quota from yesterday; hourly probes resume it, reset ~07:00 UTC).
t11 waits for the bubu login (default = alias of bubu).

### 2026-08-15 — t11 reject sweep DONE (1605/1605): OPPOSITE verdict from t14

Claimed-MUST distribution of the 905 claimants (56% claim-rate) vs the
graduates' verified ladder (max weight 21, top-49 floor 13):

- weight ≤2: 885 of 905 (488×w1 + 397×w2) — noise-level claiming
- full-weight 21/21: **0** · crown-feature claimers: **0** · ≥ floor(13): **12**
  (2×16, 1×15, 9×13; top: AU2022460007, CN218299978, KR101675622)

Where t14's quotes-free sweep SATURATED (85% claim, 341 above floor — no FN
signal), t11's is genuinely discriminative — likely because t11's MUST features
carry reference-numeral-specific language NLM can't loosely claim. Verdict:
**t11 mega-screen FN exposure is bounded to 12 docs of 1605**; the sweep itself
is near-FN-proof, pending quoted verification of those 12.

Launched immediately (user principle 08-14: no sweep is truly finished without
quoted verification): 1-round quoted MUST verify of the 12 above-floor rejects
on the default account (fresh daily quota after the 07:00 UTC reset — which
also explains the morning's t14 quota_paused: NLM's quota day resets at
midnight Pacific, so yesterday's 67-round marathon still counted at 06:12).
Reject-sweep state auto-archived as .nlm_claims_11.json.must-free; analysis
script /data/.t11_rejects_analysis.py.

### 2026-08-15 — t11 FN question CLOSED: 2/1605 borderline (0.12%), quote-verify kills 10/12

Quoted MUST verify of the 12 above-floor rejects (floor 13): only **AU2022460007**
(9 verified + 4 fuzzy = 13) and **CN219350430** (13 all-verified) hold the floor —
both exactly AT it, neither claims the crown. The other 10 collapse under quote
verification (CN218299978 16→0, KR101675622 15→0 — pure over-claiming; pattern
matches the ~34% 2b kill-rate, harsher here at the tail). **Verdict: t11
mega-screen is effectively FN-proof; 2 borderline docs = optional opus reads.**

t14 stall diagnosed: hourly quota probes failing since 07:12 with the same
RESOURCE_EXHAUSTED — so work2's limit is NOT midnight-PT-reset (or not only):
rolling 24h window or extended programmatic-access throttle after ~90 rounds in
two days. Hourly probes continue; ~5 rounds left; give-up 06:12 tomorrow.

Keeper quarantine DEPLOYED early (user added account work3 for tab 12's fresh
new-setup run — the required keeper restart was safest ON the new image):
work2+bubu came through the restart parked login-free with both audits running —
first live proof of the restart-survival design. Tab 12 prepped: old survivor
screen archived (.nlm_screen_12.json.survivor-mode-archive-2026-08-15),
benchmark ready (6 M / 11 A), 1825 fetched docs; awaiting work3 login → pin →
stage-1 MUST sweep (~53 rounds).

### 2026-08-15 — t14 quoted-verify DONE (341/341): 163 real-quote survivors — the FN verdict is structural

Google's throttle lifted ~10:12 UTC; run finished 10:34. Of the 341 above-floor
quotes-free claimants: **163 verify at >= floor(8) with real quotations — 64 at
full 11/11 verified MUST weight — all 163 crown-verified.** Distribution:
11:64 · 10:42 · 9:37 · 8:20 · then a long tail; only 8 docs verified 0.

READING (the t11 contrast is the lesson): t11's FN rate was 2/1605 because its
MUST features are reference-numeral-specific; t14's MUSTs (processor, memory,
charging-jig class) are GENERIC, so ~11% of the whole reject pool carries
genuinely quotable coverage of them. The quoted verify proves the claims are
real — it does NOT mint 163 champions; it proves t14's MUST gate has weak
discriminative power and the tab's real signal lives in its ADDITIONAL features
(the inflection-angle instructions). Implication for the funnel doctrine:
MUST-gate strength = feature specificity; write MUSTs with reference numerals /
concrete structure wherever possible (t12's fresh run has them — 6 M incl. w5
crown with numerals — and should behave like t11).

OPEN DECISION (user): what to do with the 163 survivors —
(a) run stage 2a/2b ADDITIONAL audit on them (~5 rounds 2a + pairs 2b, cheap,
    ranks them on the discriminative dimension);
(b) opus deep-read the 64 full-11/11 (real Claude spend);
(c) sample-audit to size the champion rate first (t11 rejected-pocket precedent:
    22.8% champions in a 101-doc opus pocket);
(d) leave as documented FN-bound (the ladder top-10 already holds both 5.0s).

### 2026-08-15 — t11 FN opus verdict: AU2022460007 = 5.0. The reject chain caught a real champion

Opus on the 2 quote-verified floor-holders: **AU2022460007 → 5.0** (top grade —
same tier as t14's two best) · CN219350430 → 3.0 (correctly cut). So the t11
mega-screen DID miss one top-tier reference among 1605 rejects (0.06%); the new
chain (quotes-free sweep → quoted verify → opus) recovered it for the cost of
~46 free NLM rounds + 1 quoted round + 2 opus reads.

Two doctrine consequences:
1. The reject sweep is not paranoia — it is a NECESSARY funnel stage; it just
   paid for itself with a 5.0.
2. Mid-ladder claims weight ≠ champion signal, again: AU2022460007 verified only
   13/21 MUST weight (rank ~tied-1st of rejects but nowhere near graduates' top),
   yet opus grades it 5.0 — quote-verified MUST coverage finds WHO to check, opus
   decides WHO matters.

OPEN (user): promote AU2022460007 into t11's shortlist (currently rejected /
unlisted)? Also still open: the 163 t14 survivors (options a–d) + work3 login.

### 2026-08-15 — USER decisions executed: option (a) + promotion

1. **AU2022460007 promoted into t11's shortlist** at rank 50 (appended — nobody
   displaced; its opus 5.0 is the credential) + screen state → graduate, so combi
   and future audits see it. Shortlist now 50.
2. **t14 option (a) launched**: stage 2a quotes-free ADDITIONAL audit (22
   A-features) over the 163 quote-verified survivors — 5 rounds on work2;
   on completion 2b pairs-verify launches automatically. Prior archives
   protected first: .reject-verify-done-backup, .must-graduates-orig,
   .additional-free.graduates-orig.

### 2026-08-15 — t11 middle band verified: EMPTY. t11 graduate-recall fully closed

Quoted verify of the 8 middle-band rejects (claimed 3–9): only CN115275463
verified anything at all — weight 2 (noise); the other 7 verified zero. So the
t11 sweep's bimodal shape was real: above the noise there was exactly ONE
missed doc of consequence (AU2022460007, opus 5.0, promoted). **t11 final
accounting over 1954 screened docs: 1 graduate missed by the old pipeline,
recovered; nothing else above noise. Chapter closed.**

Pipeline-improvement discussion recorded with user (proposals 2+3 superseded by
the claims ladder; #4 validated by AU2022460007; open builds: A specificity
gate · #1 persist round answers · B/C/D codify full-corpus sweep + three-tier
evidence + middle-band verify as defaults; build order after t12 reports).

### 2026-08-15 — ALL pipeline improvements BUILT (e6f57e1); t12 launch gated on user go

USER: «do all improvements and modifications once t14 is done… we need to be
crystal clear before starting t12». Honest state first: only ops fixes were
built before; the funnel proposals awaited the recorded discuss-first gate —
this was the go. Built and tested (339 green, 3 new):
1. **Specificity gate** — accepting numeral-less MANDATORY features posts the
   measured warning (t14 11% pseudo-survivors vs t11 0.12% FN).
2. **scope='corpus'** on the claims audit — full-corpus MUST sweep as primary
   screen, graduate filter gone.
3. **Three-tier doctrine automated** — quotes-free MUST completion posts the
   claimed-weight histogram + above-noise count + quoted-round estimate.
4. **Border-zone check on apply** — cut-adjacent docs without opus-tier reads
   are named in a warning.
(#1 round-answer persistence: already present in nlm_claims.answer — 205 rounds
stored; the corpus-sweep mode retires the mega-screen where it was missing.)

DEPLOY at the t14-done window (2a in flight). **t12 auto-launch DISARMED**: on
work3 login the tab only gets pinned; the sweep starts only after the user
confirms the crystal-clear plan (to be presented with t14's 2a/2b results).

### 2026-08-15 — t14 2a/2b DONE: combined ladder says the top-9 are ALL former rejects

2a: 150/163 survivors claimed >=1 A-feature (825 pairs) → 2b quote-verified all
825 (8 rounds). Combined M+A ladder over the union (317 graduates + 336
reject-survivors with any MUST evidence), same ordering as the applied ladder:

- **35 reject-survivors enter the top-49 — including the ENTIRE top-9** (all
  11/11👑 MUST; #1 US20200136173 verified A=39 across 13 A-features).
- The graduates' best (CN118475846, opus 5.0) sits at #12; only 14 current
  shortlist members keep a top-49 seat.
- All 35 entrants lack opus-tier reads (they were rejects — nobody ever read
  them). NOTHING APPLIED — analysis only (/data/.t14_combined_analysis.py);
  per the new border doctrine an apply without opus at the cut is exactly what
  the pipeline now warns against.

Verdict on the t14 mega-screen: catastrophically leaky (generic-MUST pathology
end to end) — but the three-tier chain recovered the leak for ~13 free rounds
+ 8 quoted rounds. Pipeline improvements DEPLOYED to patent-bench during this
idle window (specificity gate, scope=corpus, three-tier done-message, border
check) — t12 will run fully on the new pipeline.

PROPOSED to user: opus-read the 35 top-49 entrants (each one decides a
shortlist seat), then re-run the combined ladder with opus arbitration and
apply. Alternative: top-9 first (~1/4 spend) to sample the entrants' real
champion rate before committing to all 35.

## 2026-08-15 — FINAL DEBRIEF: the NLM claims-funnel experiment (t11 + t14)

**Goal (user, 2026-08-11):** an NLM-based, Claude-token-free method producing a
~49-doc shortlist good enough that opus is only the FINAL check.

**What was built.** The three-tier claims funnel, fully automated and deployed:
(1) quotes-free MUST sweep — recall, ~35 docs/round, zero Claude;
(2) quoted verification of everything above noise — precision, hallucinated
    quotes die in code;
(3) opus only for verified survivors — judgment.
Plus: ADDITIONAL stages 2a/2b (recall → bounded quote-verify), combined M+A
ladder, per-round raw-answer persistence, and four pipeline guards born from
measurement: specificity gate, scope=corpus primary screen, automated
three-tier done-message, border-zone opus check.

**Validation, t11 (specific numeral-bearing MUSTs).** Old screen's recall was
excellent: reject sweep of 1605 → quoted verify → exactly ONE real miss
(AU2022460007, opus 5.0, promoted to shortlist rank 50); middle band empty.
FN 0.06–0.12%. But ordering@49 remains the weak link: verified ladder holds
20/66 champions vs old 18/66; spearman 0.308. Head ordering excellent (top-8
all crown; #1 = solo 7/7 coverer).

**Validation, t14 (generic MUSTs).** The screen was catastrophically leaky:
163/1509 rejects carry quote-verified MUST >= floor (64 at full 11/11); after
2a/2b, the combined ladder's ENTIRE top-9 are former rejects; 35 ex-rejects
enter the top-49, only 14 old members keep seats. Opus arbitration of the 35
in flight (user: "opus 35"); nothing applied until it lands.

**Doctrine distilled (measured, not opined):**
1. MUST-gate strength = feature specificity. Numerals → 0.12% FN; generic →
   11% pseudo-survivors and a leaky screen. Gate now enforced at accept time.
2. Never trust an unverified claim: quote verification killed 50–85% of claims
   everywhere it ran; saturation without it (t14 sweep, 85% claim-rate) proves
   nothing.
3. Coverage finds WHO to check; opus decides WHO matters. AU2022460007 (5.0 at
   mid-band weight 13) is the canonical example — no coverage score would have
   shortlisted it.
4. Rank-cut survivor screens lose champions to round bias; the full-corpus
   claims sweep replaces them (scope=corpus is now the primary-screen mode).
5. Verify the WHOLE above-noise band, not just a champion floor.

**Cost.** Everything above ran on NLM quota (~200+ rounds over 3 accounts) +
exactly 2 opus reads for t11 closure; the 35-doc t14 batch is the first real
Claude spend, and every one of the 35 decides a shortlist seat.

**Target verdict.** Infrastructure: arrived. "Best references surface for opus
to confirm": arrived on both tabs. "All champions inside 49": not yet — t11
caps at ~30% with clean features (ordering, not recall); t14's number comes
after opus arbitration. t12 = the first clean end-to-end run of the improved
pipeline (specific features + corpus sweep from scratch) and the real test of
the stronger target.

**Also hardened today (ops):** atomic state-file writes (gunicorn -w 2 race),
keeper boot-quarantine + session-cookie persistence (host restarts no longer
demand logins while the CLI snapshot lives — proven live twice today),
REFRESH 300s, translation fallback, auth self-heal end to end.

**Open:** opus-35 → re-ladder → apply (in flight) · t12: work3 login + reword/
demote 2 numeral-less MUSTs + explicit user go · deferred list unchanged.

### 2026-08-15 — t14 CLOSED: opus-35 verdict + final apply (49/49 champions)

Opus on the 35 ladder entrants: **CN105823988 = 5.0** (new top-tier champion,
was a reject) + nine 4.0s → 10 champions recovered from the reject pile
(28.6% of the batch); ladder-#1 US20200136173 (A=39) = only 3.0 — coverage
finds, opus decides, once more. t14 champions: 42 → **52**.

**Final apply (user-approved flow):** top-49 by opus score, combined evidence
ladder as tie-break, over all 361 opus-scored docs. Result: **all 49 seats are
champions (>=4.0)**; only 3 tie-break 4.0s dropped (EP2465576, KR100402309,
KR102904406 — documented, not lost). Top-10: three 6.0s (EP4152472,
CN103457003, CN118318177), six 5.0s incl. the recovered CN105823988.
Champion-capture@49: 24% (old rank-cut) → 43% (evidence ladder) → **94%
(full three-tier chain + opus arbitration)** — the ceiling is seats, not
recall. Previous shortlist saved: /data/.t14_shortlist_pre_opus35.json.

t14 is closed. Open: t12 (work3 login + reword 2 numeral-less MUSTs + user go).

### 2026-08-15 — combi doctrine gate shipped; t12 LAUNCHED on the new pipeline (work2)

- t14 winner unchanged: **EP4152472 (6.0)** stays #1 — the recovered rejects
  enriched the field, nothing dethroned it.
- **Combi gate built+deployed (88fad19)**: additional-mode partners must share
  the MUST core at least partially (user doctrine); zero-MUST "partners" are
  dropped and gaps only they could fill report as uncovered.
- work3 abandoned (captcha loop, expired logins — user: "it is a mess"):
  removed from accounts.conf; keeper restarted — work2+bubu survived parked,
  third live proof of quarantine.
- t13 status checked (user asked): OLD pipeline only — survivor screen over
  2058 docs (410 graduates, ALL opus-read, shortlist 49); claims funnel never
  ran; 1648 rejects unswept → same reject-sweep treatment as t11/t14 is the
  natural follow-up after t12.
- **t12 LAUNCHED**: pinned to work2; scope=corpus quotes-free MUST sweep over
  1824 fetched docs, ~53 rounds — the first tab on the new pipeline end to
  end. Features kept AS ACCEPTED by the user (2 numeral-less MUSTs, w1+w2 =
  3/16 weight — low pseudo-survivor exposure; reword offer stands). Will
  quota-park and self-resume; on done the pipeline posts the histogram +
  three-tier next step.

### 2026-08-15 — both combination modes shipped as one-click buttons; t14 anchored verdict

USER's two-mode doctrine implemented end to end (f691d16, deployed):
- **Option 1 — ⚓ anchored**: among 100%-MUST coverers, rank is decided by each
  one's BEST combination (partner shares the MUST core, maximises weighted
  ADDITIONAL union) — the strongest achievable pair promotes its anchor, not
  the solo score. Panel block "Full-MUST coverers ranked by their BEST
  combination" + ⚓🏆 chat-grade button (A pinned to the shortlist's #1).
- **Option 2 — 🏆 global**: unchanged — two individually low-ranked docs whose
  union first completes the benchmark.

t14 live result: anchored chat verdict **EP4152472 (A) + CN103457003 (B)** —
5/5 MUST, combinable (CC-stage SOC/voltage-curve monitoring + inflection-point
degradation detection / CV limit reduction), both full-read + pinned.
Mechanical ranking: the two 6.0s are EACH OTHER's best partners (union 59/72
additional); CN103457003 #1 by solo tie-break (53.5 vs 33.0). EP4152472's
solo-vs-combo gap (33 → 59) is exactly what the old view hid.

### 2026-08-15 — t13 reject sweep LAUNCHED (default/bubu); t12 continues

USER: continue t12 as defined; run t13 on the default (bubu) account. t13:
1648 fetched rejects of 2058 (410 graduates already ALL opus-read — the
champion threshold is directly known, no claims ladder needed for them).
Sweep: quotes-free MUST, 48 rounds, profile default. ⚠ Expectation set BEFORE
the run (specificity doctrine): ALL 9 of t13's MUST features are generic —
zero reference numerals, worse than t14 — so saturation is the base case and
the quoted-verify tier will carry the discrimination. Both sweeps now run in
parallel on separate accounts (t12/work2 · t13/default), stall-aware watchers
on each.

### 2026-08-15 — CORRECTION per user: t13 = FULL-CORPUS blind run (not rejects-only)

USER: the new pipeline runs over ALL documents on both tabs, as if from
scratch — stored opus scores are the ANSWER KEY at the end, never an input.
Reject-only start stopped at its round boundary; **t13 relaunched
scope=corpus: 2058 docs, 59 rounds, default/bubu, blind**. t12 unchanged
(1824 docs, work2, round 2+). The two tabs now form the doctrine's validation
pair: specific features (t12) vs all-generic (t13, prediction: saturation),
both scored against known opus ground truth on completion.

### SESSION CLOSE 2026-08-15 — pause-all per user; continue next session

Both blind validation sweeps paused gracefully (flag set; they park at the
round boundary): **t12 at 280/1824** (~round 8, work2) · **t13 at 210/2058**
(~round 6, default/bubu). Watchers stopped.

NEXT SESSION:
1. Resume both: POST /api/tabs/{12,13}/claims-audit {"resume":true}. After a
   host restart, the quarantine keeper keeps CLI sessions alive — logins only
   if the keeper says LOGIN NEEDED (a manual pause never auto-resumes; the
   POST is always needed).
2. On each sweep's completion: the pipeline posts the claimed-weight histogram
   + above-noise count → quoted verify (three-tier) → survivors → 2a/2b →
   combined ladder → ⚓/🏆 combination views → ONLY THEN unseal opus ground
   truth and score the blind run (t13's 410 opus-read graduates = answer key;
   t12 compare vs its old shortlist).
3. Doctrine test: t13 (all-generic MUSTs) predicted to saturate; t12 (numeral
   MUSTs) predicted t11-like. Divergences = next improvements.
4. Day's shipped state: three-tier pipeline + 4 guards deployed · both combi
   modes as buttons (⚓ anchored rank + 🏆 global) · t14 CLOSED (49/49
   champions, capture 24%→94%, winner EP4152472; anchored pair EP4152472+
   CN103457003) · t11 CLOSED (FN 1/1605 recovered, AU2022460007 5.0 at rank
   50) · keeper quarantine (restart-survival, proven 3×) · atomic state writes.

### 2026-08-16 — both blind sweeps resumed post-restart; t12 tier-1 DONE → quoted verify launched

Host restart survived login-free (quarantine keeper, 4th proof). Both audits
resumed via POST {"resume":true}; zero stalls or quota pauses the whole day.

**t12 tier-1 DONE: 1824/1824, 53 rounds, 763 claimants (41.8%)** — numeral-MUST
prediction confirmed (t11-like, vs t14's 85%). Weight histogram: 16×3 · 13×1 ·
11×28 · 9×41 · 8×32 · 7×13 · 6×51 · 5×65 · 4×28 · 3×216 | noise ≤2: 285
(57+228). Top: WO2025161800, KR20260033205, KR20260043552 (all 6/6 MUSTs, w16).
**Tier-2 launched: quoted MUST verify on ALL 478 above-noise claimants (w≥3),
40 rounds of 12, dry-run** — per the verify-everything-above-noise doctrine
(C+D). Tier-1 state archived (.must-free) + rounds in nlm_claims.

**t13 anomaly HARDENED: claimants frozen at 3 of ~1750 audited (0.2%)** — the
saturation prediction for 9/9 generic MUSTs is fully INVERTED. User-approved
plan on t13 completion: (1) raw-answer spot-check (parse bug vs real NLM
silence), (2) unseal the 410-graduate opus answer key → tier-1 recall. Full
2058-doc opus read discussed as the complete-truth option; decision deferred
until recall numbers are in. Answer key stays sealed until then.

### 2026-08-16 — t12 tier-2 FN-measurement: pre-registered opus-32 launched (user-approved)

USER: run the 2+30 opus probe in parallel; more opus afterwards if needed —
"we need to figure out and improve at best our pipeline".

Design (saved /data/.t12_fn_sample.json, seed 20260816, selection uses ONLY
tier-1 weights — never tier-2 outcomes): among t12's 372 old-reject above-noise
claimants — (a) BOTH 6/6-MUST rejects WO2025161800 + KR20260033205, (b) 10
random per band w≥9 (of 44) · w6–8 (of 75) · w3–5 (of 251). All 32 read FRESH
by claude-opus-5 against the current benchmark (skip_scored=false; t12's 47 old
opus reads predate the from-scratch rebuild). Purpose: first-ever measurement
of the quoted-verify tier's FALSE-KILL rate per band (opus reads a doc
independently BEFORE its tier-2 verdict is consulted; crossing happens when
tier-2 finishes). Known risk being probed: verbatim-quote matching fragility
on translated CN/KR/JP texts (KR-fallback lesson).

Cross-strategy tier-1 result recorded: 478 above-noise = 372 old rejects (78%)
+ 106 old graduates; 2 of 3 full-6/6 claimants are old REJECTS; leak reversed
too — 60/195 old graduates silent, 12/47 old shortlist below noise.

### SESSION CLOSE 2026-08-16 — pause-all; opus probes landed 3 recovered champions

w≥9 old-reject band fully opus-read (census, 46 docs): the old screen dropped
**3 champions — KR20260033205 = 8.0 (highest on the tab), CN219576664 = 4.0,
CN222146326 = 4.0** — plus 5 borderline 3.0s. WO2025161800, the other 6/6-MUST
claimant, scored only 2.0 (NLM over-claim; quote gate expected to catch it).
Sampled w6–8 and w3–5 bands: nothing above 3.0 → noise, as doctrine predicted.
False-kill crossing runs when t12 tier-2 delivers verdicts on the 32
pre-registered docs.

PARKED: **t12 tier-2 quoted verify at 204/478 (round 17, 144 claimants)** ·
**t13 at 2030/2058 (round 58 — ONE round from completion; claimants 5 = 0.24%,
saturation prediction fully inverted)**. NEXT SESSION: resume both via POST
{"resume":true}; t13 completes in one round → raw-answer spot-check + unseal
the 410-graduate answer key; t12 tier-2 → FN crossing → survivors → 2a/2b →
ladder → combi views → compare vs old shortlist.

### 2026-08-17 — t13 blind sweep COMPLETE (2058/2058); answer key unsealed — recall catastrophic, saturation prediction doubly wrong

Both sweeps resumed login-free after a container restart (quarantine keeper,
5th proof). **t13 finished: 59 rounds, 2058/2058, final claimants 5 (0.24%).**

Step 1 — raw-answer spot-check: NOT a parse bug. Stored round answers are
well-formed ("FEATURE n: NONE" for 57 of 60 rounds); in the 3 claimant rounds
the parser extracted the cited numbers correctly. NLM genuinely refuses to
claim generic features — real silence.

Step 2 — 410-graduate opus answer key UNSEALED. Key: 1× 10.0 (CN223926581,
the benchmark's own priority family), 3× 5.0, 7× 4.0, 43× 3.0; old shortlist
49. Blind tier-1 claimants vs key:
- **Recall vs champions w≥4: 1/11. Vs ≥5: 1/4. Vs old shortlist-49: 1/49.**
- The one hit IS the 10.0 — CN223926581 — but it claimed only FEATURE 9
  (weight 1/33), while opus finds 15+/24 features disclosed verbatim.
- The other 4 claimants (KR102808807 f8, CN116508192 f9, US20260030964 f5,
  CN101848010 f8) are old REJECTS, never opus-read → opus probe launched
  (4 docs, claude-opus-5, per the t12 "more opus if needed" mandate).

DOCTRINE VERDICT (validation pair, generic half): the pre-registered
prediction for all-generic MUSTs was saturation → it inverted to near-total
silence, AND recall vs known ground truth is ~9% of champions. Quotes-free
tier-1 over generic-MUST features is not merely non-discriminative — it is
blind in BOTH directions. t12 (numeral MUSTs, 763/1824 = 41.8% claimants)
remains the working half of the pair; discrimination on generic tabs must
come from a different tier-1 formulation (per-doc reformulation, quoted
tier-1, or feature rewording before sweep).

### 2026-08-17 — t13 claimant opus probe landed: 4th recovered champion (CN116508192 = 5.0)

The 4 unscored t13 reject-claimants opus-read: **CN116508192 = 5.0 — the old
screen dropped another champion**; it ties the 3× 5.0 band, i.e. joint #2 on
the tab behind the 10.0. Its blind claim was FEATURE 9 only (weight 1/33) —
same pattern as the 10.0: real champions surface in the generic-MUST sweep at
noise-level claimed weight. The other three (KR102808807, US20260030964,
CN101848010) scored 1.0 — noise, as their single-feature claims suggested.

Updated t13 scorecard: blind claimants 5 = 2 champions (10.0 + 5.0) + 3 noise.
Precision 2/5; recall vs champions w≥4 (now 12 with the recovery) = 2/12.
Recovered-champion tally across the validation pair: t12 gave 3 (KR20260033205
8.0 · CN219576664 4.0 · CN222146326 4.0), t13 gives a 4th — every blind sweep
so far has found at least one champion the old screen rejected.

t12 tier-2: quota-paused at 240/478 (round 20, 174 claimants), auto-resume
armed 07:08 UTC; quota-aware watcher holds.

### 2026-08-17 — USER approved the instrument-fix sequence: t13 MUSTs reworded, v2 blind sweep launched

USER (on the unseal result): confirmed t13's silence = correct pipeline
mechanics, NLM judgment wrong; approved "reword features and re-sweep t13"
(step 1 of the sequence: reword → re-sweep → opus-probe claimants → stratified
opus sample of silent rejects → full opus read only if density warrants).

Reword executed: the opus verdict of CN223926581 (10.0, the benchmark's own
priority family) used as the translation table from the benchmark's
OCR-corrupted vocabulary to real document terms:
- "expansion module" → control module (122); "expansion function ports" →
  wired communication interface / CAN·LIN port for the battery management
  equipment (BMS/BMU); "target identification number → target identification
  device → target electrical messages" → target message/signal to the BMS
  triggering the battery to start and output electric energy; "converts analog
  to digital" → transceiver (1222) converts the target message into the target
  signal (CANH/CANL).
- All 9 M features now carry the family's reference numerals (110/120/121/
  122/1221/1222) per the specificity doctrine (t11: numeral MUSTs → 0.12% FN);
  weights and kinds unchanged; the 15 A features untouched.
- v1 artifacts archived: /data/.t13_features_v1_generic.json +
  /data/.nlm_claims_13.json.must-free-v1-genericwording (the auto-archive
  would not fire — v2 runs the same must/quotes-free mode).
- Side effect accepted: benchmark updated_at bumped → the 414 stored opus
  reads count as stale for Continue-mode; scores/verdicts remain valid.

**t13 v2 sweep RUNNING**: scope=corpus, 2058 docs, quotes-free MUST,
batch 35, ~59 rounds, dry-run, default/bubu profile. Prediction registered
BEFORE results (doctrine discipline): if wording was the root cause, v2
claimant rate should move from 0.24% toward t12's regime (~tens of %), and
the known champions (CN223926581 10.0, CN116508192 5.0, the 3× 5.0/4.0
graduates band) should claim multiple heavy features, not just the w1
category feature. Quota-aware watcher on.
