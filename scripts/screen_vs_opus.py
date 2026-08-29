#!/usr/bin/env python3
"""screen_vs_opus — score a live or finished mega-screen against opus ground truth.

The t10 run of 2026-08-29 is the best calibration the project has: all 773 queued documents
already carry an opus verdict the screen cannot see, so every graduate/reject decision is
immediately checkable. It cannot discover a champion (everything is already read) — its value
is measuring the instrument.

Counts ONLY documents from this run's queue, and only up to the cursor: seeded survivors carry
ledger entries from the previous tournament and would otherwise be scored as finds (they made
the round-2 numbers look like 100% recall when not one ground-truth doc had been reached yet).

  docker exec patent-bench python3 /data/screen_vs_opus.py [--tab 10]
"""
import argparse, json, sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--tab", type=int, default=10)
a = ap.parse_args()

st = json.load(open(f"/data/.nlm_screen_{a.tab}.json"))
queue, cursor = st["queue"], int(st.get("cursor", 0))
led = st.get("ledger") or {}
offered = set(queue[:cursor])                     # only these have had a chance
named = {int(k) for k, v in led.items() if v[1] > 0 and int(k) in offered}

cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
rows = cx.execute(
    "select id, number, score, title from documents where id in (%s)"
    % ",".join(str(i) for i in offered)).fetchall() if offered else []

by_score = {}
for did, num, sc, ti in rows:
    by_score.setdefault(sc, []).append((did, num, ti))

print(f"t{a.tab}: round {st.get('round')}, {cursor}/{len(queue)} offered, "
      f"{len(named)} of them named  (phase={st.get('step')})")
print(f"\n  opus score | offered | named by screen | rate")
tp = fp = fn = 0
for sc in sorted(by_score, reverse=True):
    docs = by_score[sc]
    hit = sum(1 for did, _, _ in docs if did in named)
    print(f"  {str(sc):>10} | {len(docs):>7} | {hit:>15} | {100*hit/len(docs):>4.0f}%")
    if sc is not None and sc >= 4:
        tp += hit; fn += len(docs) - hit
    else:
        fp += hit

gt_total = tp + fn
if gt_total:
    print(f"\n  RECALL on ground truth (opus>=4): {tp}/{gt_total} = {100*tp/gt_total:.0f}%")
else:
    print("\n  no opus>=4 document has been offered to the screen yet — no recall to report")
if named:
    print(f"  PRECISION: {tp}/{len(named)} = {100*tp/len(named):.1f}% of what the screen "
          f"named is opus>=4")
    print(f"  (the screen is a recall pre-filter: low precision is by design, "
          f"a missed GT doc is the expensive error)")

still = [q for q in queue[cursor:]]
if still:
    up = cx.execute("select count(*) from documents where id in (%s) and score>=4"
                    % ",".join(str(i) for i in still)).fetchone()[0]
    print(f"\n  {len(still)} docs not yet offered, holding {up} more ground-truth doc(s)")
