#!/usr/bin/env python3
"""core_rescue_agreement — score a live core-rescue pass against opus, for free.

t10's 2049 documents all carry an opus feature grid, so for every document the rescue pass asks
about we ALREADY know whether it owns the core. That makes the live pass self-validating: no
extra queries, no reads. The question it answers is the one the whole architecture rests on —
can NotebookLM answer a CORE question (2-3 features, directional) reliably, given that its
exact-GRADE agreement with opus is only 39% while its DIRECTIONAL agreement is 86%?

A core test is directional by construction, so it should sit in NLM's reliable range. This
measures whether it actually does.

  docker exec patent-bench python3 /data/core_rescue_agreement.py [--tab 10]
"""
import argparse, json, os, sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--tab", type=int, default=10)
a = ap.parse_args()
T = a.tab
AUD = "/data/audits"

prog = f"{AUD}/core_rescue_t{T}.progress.json"
resc = f"{AUD}/core_rescue_t{T}.rescued.json"
if not os.path.exists(prog):
    print("no chunk has completed yet"); raise SystemExit(0)
asked = set(json.load(open(prog)))
rescued = set(json.load(open(resc))) if os.path.exists(resc) else set()

cands = [c for c in json.load(open("/data/core-of-invention-candidates.json"))[str(T)]
         if c.get("recommended")]
cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
ok = lambda v: v in ("yes", "partial")

truth, scores = {}, {}
for num, fs, sc in cx.execute(
        """select number, feature_scores, score from documents where tab_id=? and status='fetched'
           and feature_scores is not null""", (T,)):
    if num not in asked:
        continue
    g = {f["name"]: (f.get("status") or "").lower() for f in json.loads(fs)}
    truth[num] = any(all(ok(g.get(m)) for m in c["features"]) for c in cands)
    scores[num] = sc

both = [n for n in asked if n in truth]
tp = sum(1 for n in both if truth[n] and n in rescued)
fp = sum(1 for n in both if not truth[n] and n in rescued)
fn = sum(1 for n in both if truth[n] and n not in rescued)
tn = len(both) - tp - fp - fn
print(f"t{T} CORE-RESCUE AGREEMENT vs opus grids — {len(asked)} asked, {len(both)} scoreable")
print(f"  NLM rescued: {len(rescued)}   opus says own the core: {sum(truth.values())}")
print(f"\n                    opus: owns core   opus: does not")
print(f"  NLM rescued            {tp:>6}          {fp:>6}")
print(f"  NLM did not            {fn:>6}          {tn:>6}")
if tp + fn:
    print(f"\n  RECALL    {tp}/{tp+fn} = {100*tp/(tp+fn):.0f}%   "
          f"(of documents opus says own the core, how many NLM rescued — the number that matters:"
          f" a miss here loses the document forever)")
if tp + fp:
    print(f"  PRECISION {tp}/{tp+fp} = {100*tp/(tp+fp):.0f}%   "
          f"(cheap error — a false rescue costs one slow-lane read)")
if both:
    print(f"  AGREEMENT {(tp+tn)}/{len(both)} = {100*(tp+tn)/len(both):.0f}%")
champs = [n for n in both if (scores.get(n) or 0) >= 4]
if champs:
    got = [n for n in champs if n in rescued]
    print(f"\n  CHAMPIONS in what has been asked so far: {len(champs)} "
          f"(opus>=4, screen-rejected) — NLM rescued {len(got)}")
    for n in champs:
        print(f"     {'RESCUED' if n in rescued else 'MISSED ':8} opus {scores[n]:.1f}  {n}"
              f"   (opus core-ownership: {'yes' if truth[n] else 'no'})")
