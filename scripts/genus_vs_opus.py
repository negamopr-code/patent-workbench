#!/usr/bin/env python3
"""genus_vs_opus — measure how far the F3f GENUS wording moves NotebookLM's per-feature
verdicts away from the opus ground truth, on the documents that happen to carry both.

Why it exists: the untreated lane (2026-08-29) ran the genus wording over the 92 add_failed
docs. 86 of those already hold a full opus verdict in documents.feature_scores, so the run
produced an accidental calibration set — the first chance to price the recall/precision
trade the genus wording makes. The number this prints decides whether the wording is safe on
t10's 758-doc backlog, where NO opus read exists to catch a false YES.

Read-only: DB opened ro, nothing written. Zero NLM quota, zero Claude tokens.
  docker exec patent-bench python3 /data/genus_vs_opus.py [--tab N]
"""
import argparse, collections, json, glob, os, re, sqlite3, sys

DB = "file:/data/workbench.db?mode=ro"
ARMS = {"genus": "/data/audits/untreated", "verbatim": "/data/audits/restage"}
RANK = {"yes": 2, "partial": 1, "no": 0}


def must_features(cx, tab):
    p = f"/data/.nlm_claims_{tab}.json"
    must = []
    if os.path.exists(p):
        must = (json.load(open(p)).get("must") or [])
    if not must:
        r = cx.execute("select features_json from benchmark where tab_id=?", (tab,)).fetchone()
        feats = json.loads(r[0]) if r and r[0] else []
        must = [[f["name"], f.get("weight", 3)] for f in feats
                if (f.get("kind") or "M").upper() == "M"]
    return must


def genus_grid(consolidated, number):
    """{feature_index: 'yes'|'partial'|'no'} parsed out of the doc's OWN block."""
    m = re.search(rf"\**{re.escape(number)}\**\s*[:\-—]", consolidated or "", re.IGNORECASE)
    if not m:
        return {}
    window = consolidated[m.end():m.end() + 900]
    nxt = re.search(r"\n\s*\**[A-Z]{2}\d{6,}", window)      # stop at the next doc block
    if nxt:
        window = window[:nxt.start()]
    return {int(k): v.lower() for k, v in
            re.findall(r"F\s*(\d+)\s*\**\s*=\s*\**\s*(YES|PARTIAL|NO)", window, re.IGNORECASE)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int)
    ap.add_argument("--arm", choices=("genus", "verbatim"), default="genus")
    a = ap.parse_args()
    cx = sqlite3.connect(DB, uri=True)
    cells = collections.Counter()
    per_tab = collections.defaultdict(collections.Counter)
    disagreements, compared_docs = [], 0

    for path in sorted(glob.glob(f"{ARMS[a.arm]}/t*_*.json")):
        tab = int(os.path.basename(path).split("_")[0][1:])
        if a.tab and tab != a.tab:
            continue
        ev = json.load(open(path))
        if ev.get("wording", "verbatim") != a.arm:
            continue
        cons = (ev.get("answers") or {}).get("_consolidated") or ""
        must = must_features(cx, tab)
        for num in ev.get("docs") or []:
            grid = genus_grid(cons, num)
            if not grid:
                continue
            row = cx.execute("select feature_scores from documents where tab_id=? and number=?",
                             (tab, num)).fetchone()
            if not row or not row[0]:
                continue                       # no opus verdict -> nothing to compare against
            opus = {f["name"]: (f.get("status") or "").lower()
                    for f in json.loads(row[0]) if f.get("name")}
            compared_docs += 1
            for i, (name, _w) in enumerate(must, 1):
                g, o = grid.get(i), opus.get(name)
                if g is None or o not in RANK:
                    continue
                key = f"{g}|{o}"
                cells[key] += 1
                per_tab[tab][key] += 1
                if RANK[g] > RANK[o]:
                    disagreements.append((tab, num, i, g, o, name[:70]))

    tot = sum(cells.values())
    if not tot:
        print("no comparable cells yet"); return 0
    agree = sum(v for k, v in cells.items() if k.split("|")[0] == k.split("|")[1])
    inflate = sum(v for k, v in cells.items()
                  if RANK[k.split("|")[0]] > RANK[k.split("|")[1]])
    deflate = tot - agree - inflate
    print(f"ARM={a.arm}   docs compared: {compared_docs}   cells: {tot}")
    print(f"  agree            {agree:5d}  {100*agree/tot:5.1f}%")
    print(f"  genus > opus     {inflate:5d}  {100*inflate/tot:5.1f}%   (over-credit — the F3f cost)")
    print(f"  genus < opus     {deflate:5d}  {100*deflate/tot:5.1f}%   (under-credit)")
    print("\nconfusion  genus|opus:")
    for g in ("yes", "partial", "no"):
        print("   " + "  ".join(f"{g}|{o}={cells.get(f'{g}|{o}', 0):4d}"
                                for o in ("yes", "partial", "no")))
    hard = [d for d in disagreements if d[3] == "yes" and d[4] == "no"]
    print(f"\nhardest over-credits (genus YES where opus said NO): {len(hard)}")
    for tab, num, i, g, o, name in hard[:15]:
        print(f"   t{tab} {num:<15} F{i:<3} {name}")
    if per_tab and not a.tab:
        print("\nper tab:")
        for tab in sorted(per_tab):
            c = per_tab[tab]; t = sum(c.values())
            inf = sum(v for k, v in c.items() if RANK[k.split("|")[0]] > RANK[k.split("|")[1]])
            ag = sum(v for k, v in c.items() if k.split("|")[0] == k.split("|")[1])
            print(f"   t{tab}: cells={t:4d} agree={100*ag/t:5.1f}%  over-credit={100*inf/t:5.1f}%")
    return 0


sys.exit(main())
