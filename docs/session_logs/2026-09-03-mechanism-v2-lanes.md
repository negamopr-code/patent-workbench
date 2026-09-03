# 2026-09-03 — v2 mechanism lanes running; F3c-ns closed; t12 lane complete

## Closed this session

- **F3c-ns (`add_failed`) closed corpus-wide** (75c2af7). 120/120 read, 10 champions.
  The final 6 reads (t14's unstaged tail, the highest-prior batch in the corpus) returned
  max 3.0, no champion. t14's yield drops 7/21 → 7/27 and is front-loaded in the re-queued
  cohort. Discharges cycle-6 improvement 3.
- **t12 mechanism lane complete** (594c758). 1369/1369 asked, 1 pick (`KR20260033205` 8.0),
  all 7 rejected champions asked. **Precision 1/1, champion recall 1/7.**

## Running (survives this session — watchdog re-arms across quota walls and restarts)

`mech-watchdog.py` drives (tab, tag) lanes: `((12,""),(10,"v2"),(13,"v2"),(14,"v2"))`.
Accounts: t10 drawnformula · t13 default · t14 work2. One job per account (A2).

State at session end: **t10 450/1005 · t13 570/1304 · t14 300/1328.**

## ⚠ The v2 re-pitch is losing champions v1 caught — provisional, NOT yet final

Chunk partitions are identical between v1 and v2 (same pile order, same roster 30), so
each already-asked champion is a clean A/B on the same document.

| tab | champion | v1 | v2 |
|---|---|---|---|
| t10 | CA2552849 4.0 | picked | **picked** |
| t10 | CN103683526 4.0 | picked | **asked, NOT picked** |
| t10 | EP3849091 4.0 | missed | asked, not picked |
| t13 | CN117692268 4.0 | picked (component form) | **asked, NOT picked** |

Provisional net: t10 1/3 asked where v1 was 2/3 · t13 0/1 where v1 was 1/1. Pick rate is
also running ~1.0 % against v1's 0.6 %, i.e. looser AND less accurate so far.

Likely cause on t10: the v2 clause "the RELAYING node must itself be beam-powered" is a new
over-specification — `CN103683526` supplies a *field device* with a wireless module. On t13
the conversion-only wording may be too abstract to fire on a concrete CAN bridge.

**If v2 finishes below v1's recall, the 09-02 over-specification correction is falsified as
applied here and v1's wordings stand.** Do not write that conclusion until the lanes finish.

## How the next session reports net recall

```bash
docker exec patent-bench python3 /data/mech-lanes-done.py   # exit 0 when all three complete
docker exec patent-bench python3 /data/mech-recall.py       # per-lane champion recall + picks
docker exec patent-bench sh -c 'tail -3 /data/.mech_t{10,13,14}_v2.log'
```
Controls are free: 17 already-scored rejected champions (t10 5 · t13 4 · t14 8).
Unread picks so far, worth an opus batch once the lanes are done:
`CA3240249` (t13) · `CN111886752` (t14). `CN111148177` (t10) carries only a stale
sonnet-4-6 1.0.

If patent-bench restarted, re-arm with:
`docker exec -d patent-bench python3 /data/mech-watchdog.py`
