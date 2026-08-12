# Patent Benchmark Match Project — deferred features / future implementation list

Features proposed but NOT built — each waits for an explicit user go-ahead. Canonical
copy: docs/nlm-mirror/deferred-features.md in the patent-workbench repo; mirrored into
the NotebookLM notebook "patent benchmark match project". Status values:
PROPOSED (awaiting decision) · APPROVED (build when scheduled) · DONE (moved out with
commit ref) · REJECTED (kept for the record with the reason).

## Funnel adaptations from the t11 NLM-vs-opus experiment (2026-08-12) — all PROPOSED

1. **Persist per-round NLM mega-screen answers.** Today only the parsed ledger
   [best in-round rank, round] and the finalize answer survive; the 51 round answers are
   discarded, so post-hoc why-analysis of NLM's choices is impossible. Store each round's
   raw answer (DB table or files next to .nlm_screen_<tab>.json).
2. **MUST-aware shortlist cut.** The finalize cut is pure in-round rank. Reserve
   shortlist slots for docs whose round answers claim crown/MUST features, so
   gap-holders and crown-feature docs cannot be squeezed out by overall-similarity picks
   (t11: 15 of 21 crown-feature YES-holders were not shortlisted).
3. **Cross-round normalization / playoff.** A doc competes only inside its own 39-doc
   round → round bias (kept docs median in-round rank 8 vs 14 for missed champions).
   Add a playoff round over near-winners (e.g. ranks 8-20 of every round) before the cut,
   or calibrate cuts across rounds instead of using raw rank.
4. **Opus-tier border zone.** Sonnet scores systematically underestimate (t11: mean
   +1.31 on opus re-read, zero downgrades). Any doc whose score sits near a decision
   boundary (shortlist cut, graduate cut) should be (re)read by opus before the boundary
   is applied.

## Earlier deferred items (pre-2026-08-12)

5. **EPO Register link → auto-populate tab.** OPS works; the doclist endpoint is behind
   Cloudflare. (Deferred nice-to-have, first noted in project memory.)
6. **Warning "deep read without benchmark features".** Partially DONE server-side —
   the API posts a loud holistic-only warning at read start; a blocking UI confirm
   remains open. (Origin: tab-11 double-spend lesson.)
7. **NLM account manager layer.** Proposed 2026-08-09 after nlm-keeper shipped: one
   place that tracks per-account notebook counts, quota state and cookie freshness, and
   assigns accounts to tabs (today: manual accounts.conf + per-tab config).
8. **pii-redaction for own pre-filing texts.** Queued 2026-07-07: privacy-protect the
   Claude leg for the user's OWN unpublished invention texts (keep numbers/dates, redact
   names). The NLM leg is Google-side and out of scope.

## Bookkeeping

- 2026-08-12: list created as part of the "patent benchmark match project" notebook
  (items 1-4 straight from the t11 comparison results; 5-8 imported from project memory).
