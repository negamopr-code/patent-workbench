# The "core of invention" principle — user directive, 2026-08-30

## The idea, in the user's own example

> *"a housing with external terminals and batteries inside where the cells of batteries are
> configured dynamically to be put in series and always match the placement where it is
> introduced (same size, shape and so on)".*
>
> *"If you strip apart the core of invention, it would be really a controller to adapt cells
> connection in series to match the voltage of battery before. Everything else can be not
> shown and said explicitly in the claim, but it is very likely that everything has a housing
> and terminals and so on."*

So in that claim:

| feature | nature |
|---|---|
| housing | **inherent** — essentially every battery product has one |
| external terminals | **inherent** |
| cells inside | **inherent** |
| same size / shape / form factor as the battery it replaces | **contextual** — a design constraint, not the invention |
| **controller that dynamically reconfigures cell series connections to match the voltage of the battery being replaced** | **THE CORE** |

## What follows for scoring

A document disclosing **only** the core is closer prior art than a document disclosing every
inherent feature and not the core. Today's pipeline gets this backwards: coverage is
**additive and weighted**, so six inherent features outscore one inventive one. A candidate at
2/8 features can be the closest art in the corpus if the 2 include the core; a candidate at
6/8 is often just "another product in the field".

**Therefore the core should act as a GATE, not as a summand.**
- discloses the core → candidate, no matter how many inherent features are absent
- misses the core → not a match, no matter how many inherent features are present

## Why it needs an agent and not a formula (measured, 2026-08-30)

A statistical proxy was tried first: rank features by how rarely they appear as YES across the
corpus, on the theory that the inventive feature is the rare one. **It does not work.** Ground-
truth median rank was unchanged (t10 8 → 8, t13 14 → 15) and the biggest promotions went to
opus 1.0–2.0 documents. The reason: in a weakly-matched corpus *every* feature is rare, so
inverse-frequency cannot distinguish "rare because inventive" from "rare because irrelevant".

The core is a **semantic** property of the benchmark — what a skilled reader would call the
inventive step — so it must be derived by reading the benchmark itself, iteratively, not by
counting the corpus. Hence a dedicated agent.

## Evidence this is a live defect, not a theoretical one

- **t10** decomposes into 11 features. Its core is the pair *"second wireless power supply
  device using a microwave"* (w5) and *"the two supplies use DIFFERENT microwave frequencies"*
  (w5). Its inherent features are *"Wireless system"* (w1), *"Base wireless device"* (w2).
  Documents scoring on the inherent features alone were graduated by the screen; the
  ground-truth documents that own the core were rejected.
- **t12** rejected `KR20260033205` (opus 8.0, the highest-scoring document in the tab and a
  registered champion control) while its last re-screen nominated 168 documents of which
  **zero** scored opus ≥4 and 155 scored ≤1 — a textbook case of inherent-feature matching.
- The **paired test** (2026-08-30) showed the per-document lane's grids for rejected documents
  concentrating on F8/F9/F11 — *base wireless device, remote wireless device, wireless system*,
  i.e. the inherent three — while the weight-5 core features were NO almost everywhere.

## Open questions to develop (asked of the user 2026-08-30)

1. Is the core always a **single** feature, or can it be a small irreducible **combination**
   (e.g. core = dynamic series reconfiguration *plus* voltage matching, where either alone is
   known art)?
2. Should the core act as a hard gate (no core → rejected) or as a **large multiplier** that
   keeps a partial-core document alive for review?
3. Where does the core come from — the benchmark's independent claim 1 only, the claim plus
   the description's "problem to be solved" / "advantage of the invention" passages, or the
   examiner's own citation reasoning where available?
4. When a benchmark has several plausible cores, do we run the corpus against each
   independently and union the candidates?
5. Should the existing 1–5 feature weights be re-derived from the core analysis, or kept and
   supplemented with a separate `is_core` flag?
