#!/usr/bin/env python3
"""opus_ranking — the corpus ranking derived from the reads we ALREADY paid for.

5470 of 9720 fetched documents (56%) carry a full opus verdict with a per-feature grid. That is
the strongest instrument in the project and its output was not being read correctly, for two
reasons this tool fixes:

1. ORPHANED GRIDS (failure class C1). A benchmark re-decomposed after its reads leaves every
   earlier grid keyed to names the current benchmark no longer has. t10: 11 orphan names over
   1206 documents, differing from the current ones ONLY by reference numerals ("Wireless system"
   vs "Wireless system (10)"). t13: 9 orphan names over 332 documents, a genuine rename
   (expansion module -> control module, control processor -> controller). Remapping is by EXACT
   match after numeral-stripping, never by fuzzy similarity: difflib maps t13's "a battery
   triggering device (device category)" onto "a teaching tool (device category)", which would
   silently invent coverage. What does not map after normalisation is reported and dropped.

2. RAW SCORE IS NOT A SCALE. It is whatever integer the reading model wrote in "MATCH SCORE: N".
   t10 tops out at 5, t12 at 8, t13 jumps 6 -> 10 with nothing between. score>=4 is the top 0.6%
   of t10 and the top 12% of t14, so raw score must never be compared across tabs. Ranking is by
   WEIGHTED FEATURE COVERAGE over all benchmark features (both kinds — t14 keeps 22 of its 27
   features, including every inflection-angle one, under kind 'A'), with the tab's own percentile
   as the cross-tab-comparable number.

Read-only, zero quota, zero tokens.
  docker exec patent-bench python3 /data/opus_ranking.py [--tab N] [--top K] [--remap-report]
"""
import argparse, json, os, re, sqlite3, sys
from collections import Counter

DB = "file:/data/workbench.db?mode=ro"
W = {"yes": 1.0, "partial": 0.5, "no": 0.0}
REFNUM = re.compile(r"\s*\(\s*\d+[A-Za-z]?(?:\s*,\s*\d+[A-Za-z]?)*\s*\)")
# documents planted as controls, never reportable as finds
CANARY = {"CN223926581"}


def norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", REFNUM.sub("", (s or "").lower()))


def norm_key(s):
    return " ".join(norm(s).split())


def benchmark(cx, tab):
    row = cx.execute("select features_json from benchmark where tab_id=?", (tab,)).fetchone()
    feats = json.loads(row[0]) if row and row[0] else []
    return [(f["name"], float(f.get("weight", 3))) for f in feats]


def remap(feats, grid_names):
    """orphan name -> current name, by exact match after numeral-stripping only."""
    by_key = {}
    for name, _w in feats:
        by_key.setdefault(norm_key(name), name)
    cur = {n for n, _ in feats}
    mapping, unmapped = {}, []
    for g in grid_names:
        if g in cur:
            continue
        hit = by_key.get(norm_key(g))
        if hit:
            mapping[g] = hit
        else:
            unmapped.append(g)
    return mapping, unmapped


def rank(cx, tab, report=False):
    feats = benchmark(cx, tab)
    if not feats:
        return [], [], 0
    total_w = sum(w for _, w in feats) or 1.0
    rows = cx.execute("""select number, title, score, feature_scores from documents
                         where tab_id=? and status='fetched' and feature_scores is not null""",
                      (tab,)).fetchall()
    seen = Counter()
    parsed = []
    for num, ti, sc, fs in rows:
        try:
            g = json.loads(fs)
        except Exception:                                    # noqa: BLE001
            continue
        seen.update(f.get("name") for f in g if f.get("name"))
        parsed.append((num, ti or "", sc, g))
    mapping, unmapped = remap(feats, list(seen))
    dropped = set(unmapped)
    unmapped_names = unmapped
    if report:
        print(f"t{tab}: {len(seen)} grid names, {len(mapping)} remapped by numeral-strip, "
              f"{len(unmapped)} unmapped and dropped")
        for u in unmapped:
            print(f"    DROPPED (no successor in the current benchmark): {u[:80]}")
    wmap = dict(feats)
    out = []
    for num, ti, sc, g in parsed:
        cov = 0.0
        hit = 0
        for f in g:
            name = mapping.get(f.get("name"), f.get("name"))
            if name in wmap:
                v = W.get((f.get("status") or "").lower(), 0.0)
                cov += wmap[name] * v
                if v > 0:
                    hit += 1
        # does this doc's grid key to names the current benchmark dropped?
        stale_doc = any(f.get("name") in dropped for f in g)
        out.append({"number": num, "title": ti, "score": sc,
                    "coverage": cov / total_w, "features_hit": hit,
                    "_stale": stale_doc})
    # A doc whose grid keys to a dropped decomposition cannot be scored on the current
    # feature space: dividing by the full weight silently caps it (t13's 330 docs top out
    # at 48.4% while current-key docs reach 100%) and co-ranking them buries them exactly
    # as C1 describes. Split them out and say so.
    stale_keys = set(unmapped_names)
    for d in out:
        d["stale_decomposition"] = d.pop("_stale", False)
    ranked = [d for d in out if not d["stale_decomposition"]]
    stale = [d for d in out if d["stale_decomposition"]]
    ranked.sort(key=lambda d: (-d["coverage"], -(d["score"] or 0)))
    stale.sort(key=lambda d: -(d["score"] or 0))
    out = ranked
    for i, d in enumerate(out):
        d["pct"] = 1.0 - i / max(1, len(out) - 1)
    return out, unmapped, len(parsed), stale


ap = argparse.ArgumentParser()
ap.add_argument("--tab", type=int)
ap.add_argument("--top", type=int, default=5)
ap.add_argument("--remap-report", action="store_true")
a = ap.parse_args()
cx = sqlite3.connect(DB, uri=True)
tabs = [a.tab] if a.tab else [10, 11, 12, 13, 14]
allout = {}
for t in tabs:
    out, unmapped, n, stale = rank(cx, t, report=a.remap_report)
    allout[t] = out
    print("=" * 96)
    print(f"t{t} — ranked by weighted opus feature coverage over {n} read documents")
    if stale:
        names = ", ".join("%s (opus %s)" % (d["number"], d["score"]) for d in stale[:3])
        print("   EXCLUDED from this ranking: %d document(s) whose grids key to a "
              "decomposition this benchmark dropped — they cannot be scored on the current "
              "feature space, and co-ranking them would cap them below every current-key doc "
              "(the C1 burial, relocated into this tool). Best by raw opus score: %s"
              % (len(stale), names))
    shown = 0
    for d in out:
        if shown >= a.top:
            break
        tag = "  [CANARY — planted control, not a find]" if d["number"] in CANARY else ""
        print(f"   {d['coverage']:6.1%}  raw {str(d['score']):>4}  {d['features_hit']:>2} feat  "
              f"{d['number']:<15} {d['title'][:46]:<46}{tag}")
        shown += 1
# merge-on-write: a --tab run used to truncate this file to the single key it computed
# (it held only "14" after my own --tab 14 run). Shared verdict artifacts are merged.
OUT = "/data/audits/opus_ranking.json"
prev = {}
if os.path.exists(OUT):
    try:
        prev = json.load(open(OUT))
    except Exception:                                        # noqa: BLE001
        prev = {}
prev.update({str(t): v[:50] for t, v in allout.items()})
json.dump(prev, open(OUT, "w"), indent=1)
print("\n-> /data/audits/opus_ranking.json")
