#!/usr/bin/env python3
"""Ranking-integrity audit — verifies the displayed ranking corresponds to the most
relevant documents, independently of the app's own scoring code.

Born from two real 2026-08-18 bugs:
  A. Must-sort orphans (fix 8066543): rewording benchmark features orphaned every
     prior per-element read (element identity = NAME) — docs with the best real
     coverage counted un-assessed and silently sank in the 🎯 Must sort.
  B. Batch-scoped "BEST FIT" (fix 7168afd): a compile over a small batch crowned
     its local best as corpus champion; fixed with a deterministic corpus-top block.

Runs INSIDE the patent-bench container, read-only (sqlite mode=ro, GET-only HTTP):
  docker exec -i patent-bench python3 - [--tab N] [--json] < scripts/audit_ranking.py

Checks (per tab with a ready benchmark):
  C1 orphaned-reads   — every stored per-element verdict must key to a current
                        element name, directly or via the norm/positional remap.
                        Covers feature_scores, additional_scores AND combi_coverage
                        (the remap fix does NOT re-key combi_coverage — watch it).
  C2 rank-encoding    — /state rank.key must equal the documented composite formula
                        recomputed from its own components; benchmark rank = None;
                        no doc with raw yes/partial coverage may have rank = null.
  C3 corpus-top block — the latest compiled ranking message's 📌 top-10 must equal
                        a fresh recompute from stored scores as of that message's
                        timestamp (scores landed later = INFO, not a violation).
  C4 buried champion  — a doc in the corpus top-5 by holistic score whose 🎯 Must
                        rank position is far below gets flagged with a likely cause
                        (legacy wording / conflicts / un-assessed). Heuristic → WARN.

Exit code: 0 = all PASS, 1 = warnings only, 2 = failures.
"""
import argparse
import json
import re
import sqlite3
import sys
import urllib.request

DB = "file:/data/workbench.db?mode=ro"
API = "http://127.0.0.1:8000"
_REFNUM_RE = re.compile(r"\s*\(\s*\d[\d\s,./-]*\s*\)")   # mirrors api._REFNUM_RE intent


def feat_norm(name):
    return re.sub(r"\s+", " ", _REFNUM_RE.sub("", name or "")).strip().lower()


def kind(e):
    k = (e.get("kind") or "M").upper()
    return k if k in ("M", "A", "W") else "M"


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def jload(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


class Report:
    def __init__(self):
        self.rows = []          # (level, tab, check, message)

    def add(self, level, tab, check, msg):
        self.rows.append({"level": level, "tab": tab, "check": check, "msg": msg})

    def worst(self):
        lv = [r["level"] for r in self.rows]
        return 2 if "FAIL" in lv else (1 if "WARN" in lv else 0)


def names_recoverable(stored, current_names):
    """Can this stored per-element list be keyed to the current wording?
    Mirrors the remap contract independently: exact → norm → positional."""
    stored_names = [s.get("name") for s in stored if isinstance(s, dict)]
    cur = set(current_names)
    if any(n in cur for n in stored_names):
        return "exact"
    by_norm = {feat_norm(n) for n in current_names}
    if any(feat_norm(n) in by_norm for n in stored_names):
        return "norm"
    if len(stored) == len(current_names):
        return "position"
    return None


def audit_tab(cx, rep, tab):
    bm = cx.execute("select features_json, number, updated_at from benchmark "
                    "where tab_id=? and status='ready'", (tab,)).fetchone()
    if not bm:
        return
    elements = jload(bm[0], [])
    if not elements:
        return
    names_by_kind = {k: [e["name"] for e in elements if kind(e) == k] for k in "MAW"}
    all_names = [e["name"] for e in elements]
    docs = cx.execute(
        "select id, number, score, score_model, scored_at, status, feature_scores,"
        " additional_scores, combi_coverage from documents where tab_id=?",
        (tab,)).fetchall()
    cols = ["id", "number", "score", "score_model", "scored_at", "status",
            "feature_scores", "additional_scores", "combi_coverage"]
    docs = [dict(zip(cols, d)) for d in docs]
    bm_num = re.sub(r"[^A-Za-z0-9]", "", bm[1] or "").upper()

    # ---- C1: orphaned per-element reads --------------------------------------
    orphans = {"feature_scores": [], "additional_scores": [], "combi_coverage": []}
    for d in docs:
        for col, cur in (("feature_scores", names_by_kind["M"]),
                         ("additional_scores", names_by_kind["A"]),
                         ("combi_coverage", all_names)):
            arr = jload(d[col], [])
            if not arr or not cur:
                continue
            has_value = any(isinstance(s, dict) and s.get("status") in ("yes", "partial")
                            for s in arr)
            if has_value and names_recoverable(arr, cur) is None:
                orphans[col].append(d["number"])
    for col, lost in orphans.items():
        if lost:
            level = "FAIL" if col != "combi_coverage" else "WARN"  # no remap exists for combi
            rep.add(level, tab, "C1-orphaned-reads",
                    f"{len(lost)} doc(s) hold yes/partial {col} verdicts that key to NO "
                    f"current element name and are NOT remappable (exact/norm/position) — "
                    f"their coverage is invisible to the ranking. E.g. {lost[:5]}")
    if not any(orphans.values()):
        rep.add("PASS", tab, "C1-orphaned-reads",
                "every stored per-element verdict keys to the current wording "
                "(directly or remappable)")

    # ---- C2: rank encoding + no sunk assessments (needs the live API) --------
    try:
        state = get(f"{API}/api/tabs/{tab}/state")
    except Exception as e:  # noqa: BLE001 — audit must report, not crash
        rep.add("WARN", tab, "C2-rank-encoding", f"/state unreachable: {e}")
        state = None
    if state:
        by_id = {d["id"]: d for d in docs}
        bad_key, sunk, bm_ranked = [], [], []
        for d in state["documents"]:
            num_norm = re.sub(r"[^A-Za-z0-9]", "", d.get("number") or "").upper()
            r = d.get("rank")
            if bm_num and num_norm == bm_num:
                if r is not None:
                    bm_ranked.append(d["number"])
                continue
            if r:
                expect = ((1e9 if r["covers_all"] else 0.0) + r["mand_rating"] * 1e6
                          + r["add_bonus"] * 1e3 + r["w_bonus"]) if r["assessed"] else -1.0
                if abs(round(expect, 3) - r["key"]) > 0.001:
                    bad_key.append(f"{d['number']} key={r['key']} expected={round(expect, 3)}")
            else:
                raw = by_id.get(d["id"])
                if raw:
                    vals = (jload(raw["feature_scores"], [])
                            + jload(raw["combi_coverage"], []))
                    if any(isinstance(s, dict) and s.get("status") in ("yes", "partial")
                           for s in vals):
                        sunk.append(d["number"])
        if bm_ranked:
            rep.add("FAIL", tab, "C2-rank-encoding",
                    f"benchmark itself carries a rank (must be None): {bm_ranked}")
        if bad_key:
            rep.add("FAIL", tab, "C2-rank-encoding",
                    f"rank.key diverges from its own components (encoding drift) on "
                    f"{len(bad_key)} doc(s): {bad_key[:3]}")
        if sunk:
            rep.add("FAIL", tab, "C2-rank-encoding",
                    f"{len(sunk)} doc(s) hold yes/partial verdicts in the DB but expose "
                    f"rank=null via /state — assessed coverage is SUNK (bug-A symptom): "
                    f"{sunk[:5]}")
        if not (bad_key or sunk or bm_ranked):
            rep.add("PASS", tab, "C2-rank-encoding",
                    f"{sum(1 for d in state['documents'] if d.get('rank'))} ranked docs: "
                    "key encoding exact, benchmark unranked, no sunk assessments")

    # ---- C3: corpus-top block vs stored scores as of the message ------------
    msg = cx.execute(
        "select text, ts from messages where tab_id=? and role='c' "
        "and text like '%CURRENT CORPUS TOP-10%' order by id desc limit 1",
        (tab,)).fetchone()
    newest_compile = cx.execute(
        "select ts from messages where tab_id=? and role='c' order by id desc limit 1",
        (tab,)).fetchone()
    if not msg:
        if newest_compile:
            rep.add("WARN", tab, "C3-corpus-top",
                    "no compiled message carries the 📌 corpus-top block (fix 7168afd "
                    "regressed, or no compile ran since deploy)")
        return
    listed = re.findall(r"^\s*\d+\.\s+(\S+)\s+—", msg[0], re.M)[:10]
    asof = [d for d in docs if d["score"] is not None
            and (d["scored_at"] or 0) <= msg[1]]
    asof.sort(key=lambda d: (-(d["score"] or 0), d["id"]))
    expect = [d["number"] for d in asof[:10]]
    if listed == expect:
        rep.add("PASS", tab, "C3-corpus-top",
                "latest 📌 corpus-top block equals the stored-score top-10 as of its "
                "own timestamp")
    else:
        # scores that landed AFTER the message legitimately change the live top
        live = sorted((d for d in docs if d["score"] is not None),
                      key=lambda d: (-(d["score"] or 0), d["id"]))[:10]
        if listed == [d["number"] for d in live]:
            rep.add("PASS", tab, "C3-corpus-top",
                    "block matches the live stored-score top-10 (as-of reconstruction "
                    "differs — re-scored docs)")
        else:
            rep.add("FAIL", tab, "C3-corpus-top",
                    f"📌 block diverges from stored scores: block={listed[:5]}… "
                    f"expected(as-of)={expect[:5]}… — a compile may have crowned a "
                    f"batch-local best (bug-B symptom)")

    # ---- C4: buried champion (heuristic) -------------------------------------
    if state:
        ranked = sorted((d for d in state["documents"] if d.get("rank")),
                        key=lambda d: -d["rank"]["key"])
        pos = {d["number"]: i + 1 for i, d in enumerate(ranked)}
        top5 = sorted((d for d in docs if d["score"] is not None),
                      key=lambda d: (-(d["score"] or 0), d["id"]))[:5]
        for d in top5:
            p = pos.get(d["number"])
            if p is None or p > 25:
                sdoc = next((x for x in state["documents"]
                             if x["number"] == d["number"]), {})
                cause = ("legacy wording" if sdoc.get("legacy_wording")
                         else "no per-element assessment"
                         if not sdoc.get("rank") else "low Must coverage")
                rep.add("WARN", tab, "C4-buried-champion",
                        f"{d['number']} (holistic score {d['score']}) sits at 🎯 "
                        f"position {p or '∅ (unranked)'} — cause: {cause}. Verify "
                        f"this is genuine (holistic≠Must is legal) and not a sunk read.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cx = sqlite3.connect(DB, uri=True)
    tabs = ([args.tab] if args.tab else
            [r[0] for r in cx.execute(
                "select tab_id from benchmark where status='ready' order by tab_id")])
    rep = Report()
    for t in tabs:
        audit_tab(cx, rep, t)
    if args.json:
        print(json.dumps({"worst": ["PASS", "WARN", "FAIL"][rep.worst()],
                          "rows": rep.rows}, ensure_ascii=False, indent=1))
    else:
        for r in rep.rows:
            icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[r["level"]]
            print(f"{icon} t{r['tab']:>2} {r['check']:<20} {r['msg']}")
        print(f"\nVERDICT: {['COMPLIANT', 'WARNINGS', 'VIOLATIONS'][rep.worst()]}")
    sys.exit(rep.worst())


main()
