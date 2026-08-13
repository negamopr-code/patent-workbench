# Patent Benchmark Match Project — skill & lessons learned

The working method (skill) and the hard-won lessons of the patent-workbench project
(multi-tab patent prior-art app: NotebookLM mega-screen funnel + Claude deep reads).
Canonical copy: docs/nlm-mirror/skill-lessons.md in the patent-workbench repo; mirrored
into the NotebookLM notebook "patent benchmark match project".

## The method (skill)

1. **Funnel shape**: NotebookLM mega-screen (zero Claude tokens) over the full candidate
   corpus → rounds of 39 through one rotating 50-source notebook, survivors carry forward
   → graduates → finalize query ranks the top ~49 into the shortlist → Claude deep-read
   (Verify) of the shortlist at full text → per-feature YES/PARTIAL/NO verdicts vs the
   accepted benchmark feature list → weighted ranking, solo coverers, 2-doc combinations.
2. **Feature list first**: accept the benchmark's decomposed feature list (kinds M/A,
   weights) BEFORE any deep read. Reads started without features are holistic-only,
   cannot feed the weighted ranking or combi, and must be redone (tab-11 double-spend
   lesson). The API now warns loudly at start.
3. **Model tiers**: sonnet for cheap breadth is UNRELIABLE near the cut — t11 proved opus
   re-scores sonnet reads strictly upward (mean +1.31, 0 downgrades; one doc 2.0→7.0).
   Use opus for anything that decides a shortlist boundary; reading-model ranking guards
   already skip docs read by an equal-or-stronger model (fable outranks opus).
4. **Anticipation standard**: a solo document "covers" a MUST element at YES or PARTIAL —
   a partial/implicit disclosure can still meet a limitation; only NO is a real gap.
   Strict all-YES stays the badge for a clean single reference.
5. **Combination doctrine**: a pair only counts when BOTH docs uniquely contribute a MUST
   element; the best second document is often a weaker doc holding the missing feature,
   not the next-strongest overall — which is exactly what a rank-cut shortlist drops.
6. **Everything resume-safe**: deep reads, mega-screens and verifies park on quota/auth
   errors and auto-resume (auth watchdog + reseed daemon + boot-time re-arm). Continue
   mode never re-reads. Verify is skip_scored-safe.

## Lessons learned (chronological, each cost real time or tokens)

- **Don't restart patent-bench with a read in flight** — the in-flight candidate dies;
  everything already scored survives (DB volume). Check /data/.claude_read_*.lock first.
- **Tab-11 double spend**: a 108-doc read ran twice because it started before the feature
  list was accepted → holistic-only verdicts, feature_scores NULL. Features FIRST.
- **"Not loaded" disclaimers from NLM chat are not failure** — grounding differs from
  refusal; don't re-run on sight of a caveat.
- **"Feature disappeared" panics**: benchmark re-decompose wipes feature_scores;
  "everything's gone" was browser cache. Check the DB before re-running anything.
- **pytest spawns the REAL claude CLI** — always CLAUDE_BIN=/nonexistent in tests.
- **NLM profile must live in a named volume** (nlm-profile): host bind paths under
  Docker Desktop/WSL2 get wiped on restarts; a root-owned empty profile killed mega-screen
  round 13 (2026-08-06).
- **Central SAPISID cookie layer is mandatory** for NLM accounts; service-only logins are
  garbage sessions. nlm-keeper (noVNC :8106) holds sessions, saves cookies every 15 min.
- **Token rotation (/login) kills running reads** with "Not logged in" — by design the
  auth watchdog parks and auto-resumes them (proved live 2026-08-12, zero re-reads).
- **UI poller vs server-side resumes**: a page already open on the tab missed the
  watchdog auto-resume because pollRead stopped rescheduling at running=false. Fixed with
  a 30 s idle heartbeat (721045e). Lesson: any server-side self-healing needs a client
  that keeps looking.
- **Mega-screen observability gap**: per-round NLM answers are not persisted — only the
  parsed ledger [best in-round rank, round]. Post-hoc "why did NLM rank X low" analysis
  is impossible until round answers are stored (deferred feature #1).
- **Round bias is real (t11, 2026-08-12)**: cutting the shortlist by best in-round rank
  makes a doc's fate depend on which 39-doc round it landed in. Median in-round rank of
  kept docs 8 vs 14 for the champions it missed. Combined with sonnet underestimation,
  the funnel missed a 7.0 (CN118156696) and five 6.0s among 349 graduates.
- **NLM ranks by overall similarity, not MUST coverage (t11 H1)**: of 21 crown-feature
  YES-holders, NLM shortlisted 6. If MUST coverage is what matters, the cut must be
  MUST-aware (deferred feature #2).
- **Quotes-free recall claims are ~1/3 noise (t14 2b, 2026-08-13)**: of 756 stage-2a
  claims, 31.2% died under quote verification. Never rank directly on unverified
  claims; the two-tier 2a→2b design is load-bearing, not polish.
- **Quote verification has a translation blind spot**: a KR champion (opus 4.0, raw-2a
  #1) verified to ZERO — NLM quotes across a translation can't match the stored text.
  Fallback shipped (a4c4658): failed quotes on non-English-origin docs soften to
  'claimed' (uncertain), opus adjudicates on full text.
- **NEVER inject saved cookies into a restarted browser** (2026-08-13, killed two live
  sessions): Google sees downgraded/rotated-back tokens and invalidates the whole
  session FAMILY — including the CLI's still-valid copy. The snapshot's only safe
  consumer is the CLI. Boot = probe-and-report; a logged-out browser waits for a human.
- **A session-keeper must refuse to snapshot a logged-out browser**: the reliable tell
  is the page's build label (only the real app serves labs-tailwind; login pages serve
  identityfrontend). File freshness and cookie counts LIE — a dead session snapshots
  "successfully" every 15 min while everything is broken.
- **Chromium needs a clean SIGTERM on container stop** — `docker rm -f` SIGKILLs it and
  loses the final cookie flush, which is why sessions never survived restarts. serve.sh
  stops with grace first; the daemon TERMs its Chromiums and waits before exiting.
- **Auth/network errors are transient for a resume-safe job**: the claims audit now
  parks (auth_paused) and probes with a FREE list call every 3 min instead of demanding
  a manual resume. Proved live twice on launch day.
- **No keeper restarts while a session is live** (standing rule, user-confirmed): deploys
  wait for a logged-out window or explicit approval.
