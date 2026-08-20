#!/usr/bin/env python3
"""Lexical recall lane — TF-IDF cosine of every fetched doc vs the benchmark.

Complements the NLM sweep (wording-biased, measured recall ~0-58%) and the
embedding lane. F5 fix baked in: FULL description tokens (the first version's
desc[:20k] clip was a blind stripe). Writes the R5 lane report to
/data/audits/lane_lexical_<tab>.json and prints the C5-style control block:
the registered canary AND the paraphrased champions are ranked out of
competition — if the controls sink, the lane's queue must not be trusted
(measured 2026-08-20: this lane ranks the verbatim canary #2 but the
paraphrased champions 155-620 — same wording bias as NLM, so its queue
COMPLEMENTS the embed lane, never replaces it).

Run INSIDE patent-bench (read-only DB, writes only /data/audits):
  docker exec -i patent-bench python3 - --tab N \
      [--registry "$(cat docs/controls-registry.json)"] [--top 30] \
      < scripts/lex_recall_lane.py
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter

DB = "file:/data/workbench.db?mode=ro"
AUDIT_DIR = "/data/audits"

STOP = set(("the a an and or of to in for with is are be by on at as from that this "
            "it its said wherein claim claims comprising comprises device method system "
            "first second one plurality least 所述 一种 用于 according invention "
            "embodiment present disclosure may can").split())


def tok(s):
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", (s or "").lower())
            if w not in STOP]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, required=True)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    reg = {}
    if args.registry:
        try:
            reg = (json.loads(args.registry).get("tabs") or {}).get(str(args.tab)) or {}
        except ValueError:
            pass
    c = sqlite3.connect(DB, uri=True)
    bm = c.execute("select title, abstract, claims, description, text, features_json "
                   "from benchmark where tab_id=?", (args.tab,)).fetchone()
    if not bm:
        print("no benchmark", file=sys.stderr)
        sys.exit(3)
    feats = json.loads(bm[5]) if bm[5] else []
    query = (" ".join(x or "" for x in bm[:5]) + " "
             + " ".join((f.get("name") or "") * (3 if (f.get("kind") or "M").upper() == "M" else 1)
                        for f in feats))

    docs = []
    for did, num, ab, cl, de, score in c.execute(
            "select id, number, abstract, claims, description, score from documents "
            "where tab_id=? and status='fetched'", (args.tab,)):
        t = tok((ab or "") + " " + (cl or "") + " " + (de or ""))   # FULL description
        if len(t) > 50:
            docs.append((did, num, score, Counter(t)))
    n = len(docs)
    df = Counter()
    for _, _, _, tf in docs:
        df.update(tf.keys())
    idf = {w: math.log(n / (1 + d)) for w, d in df.items()}

    def vec(tf):
        v = {w: (1 + math.log(f)) * idf.get(w, 0.0) for w, f in tf.items()}
        return v, (math.sqrt(sum(x * x for x in v.values())) or 1.0)

    qv, qn = vec(Counter(tok(query)))
    ranked = []
    for did, num, score, tf in docs:
        dv, dn = vec(tf)
        small, big = (qv, dv) if len(qv) < len(dv) else (dv, qv)
        dot = sum(val * big.get(w, 0.0) for w, val in small.items())
        ranked.append((dot / (qn * dn), did, num, score))
    ranked.sort(reverse=True)
    rank_of = {num: i + 1 for i, (_, _, num, _) in enumerate(ranked)}

    controls = ([reg.get("verbatim_canary")] if reg.get("verbatim_canary") else []) \
        + [cc["number"] for cc in (reg.get("champion_controls") or [])]
    print(f"=== control ranks (of {n}) ===")
    for cn in controls:
        print(f"  {cn}: rank {rank_of.get(cn, '∅')}")
    print(f"=== top-{args.top} UNREAD (score is null) ===")
    queue = []
    for i, (s, did, num, score) in enumerate(ranked):
        if score is None and len(queue) < args.top:
            queue.append({"id": did, "number": num, "sim": round(s, 4),
                          "lane": "lexical"})
            print(f"  rank {i + 1:4}  sim {s:.4f}  id {did}  {num}")
    os.makedirs(AUDIT_DIR, exist_ok=True)
    rp = os.path.join(AUDIT_DIR, f"lane_lexical_{args.tab}.json")
    with open(rp, "w") as fh:
        json.dump({"lane": "lexical", "tab": args.tab, "total": n,
                   "ranks": rank_of, "queue": queue}, fh)
    print(f"lane report -> {rp}")


main()
