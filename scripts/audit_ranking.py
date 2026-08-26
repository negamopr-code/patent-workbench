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
  C5 closure gate     — deterministic canary-control (2026-08-20 lesson: the false
                        t13 closure's negative aggregates were computed over an
                        orphan-keyed store in which the CANARY itself showed 0/9
                        MUST): recompute the MUST-coverage aggregates under the
                        CURRENT keys and assert the registered canary/known-positive
                        registers. Canary dark → closure_claims_permitted: NONE.
  C6 falsification    — every top-K doc by holistic score must hold a deep read
                        keyed to the CURRENT wording (post-re-decompose); an unread
                        or stale-keyed top band forbids closure statements (F2).
  C7 DONE divergence  — a claims-audit DONE message listing "Top by claim score"
                        without the deterministic 📌 stored-score corpus-top block
                        invites reading claim-weight as relevance (measured
                        non-predictive) → WARN.

Verdict file: /data/audits/audit_ranking.json (+ history.jsonl) with per-tab
data-watermark anchors; --baselines takes the approved-known-baselines JSON from
docs/failure-registry.md (unregistered FAILs, or growth over a registered count,
gate regardless of prose); --registry takes docs/controls-registry.json.

Exit code: 0 = all PASS, 1 = warnings only, 2 = failures, 3 = incomplete.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

DB = "file:/data/workbench.db?mode=ro"
API = "http://127.0.0.1:8000"
AUDIT_DIR = "/data/audits"
SCHEMA = 1
SCRIPT_VERSION = "2026-08-20.2"
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
        self.rows = []          # (level, tab, check, message[, data])

    def add(self, level, tab, check, msg, data=None):
        self.rows.append({"level": level, "tab": tab, "check": check, "msg": msg,
                          **({"data": data} if data is not None else {})})

    def worst(self):
        lv = [r["level"] for r in self.rows]
        return 2 if "FAIL" in lv else (1 if "WARN" in lv else 0)


def anchors(cx, tab):
    bm = cx.execute("select updated_at from benchmark where tab_id=?", (tab,)).fetchone()
    mx = cx.execute("select max(scored_at), count(*) from documents "
                    "where tab_id=? and status='fetched'", (tab,)).fetchone()
    nc = cx.execute("select max(ts), count(*) from nlm_claims where tab_id=?",
                    (tab,)).fetchone()
    # 2026-08-26 (supervisor finding): screen rounds write nlm_screened_at /
    # nlm_screen_state only — without this anchor a verdict stayed FRESH while
    # hundreds of docs were screened and rosters rotated underneath it.
    sc = cx.execute("select max(nlm_screened_at), count(nlm_screened_at) from documents "
                    "where tab_id=?", (tab,)).fetchone()
    return {"benchmark_updated_at": bm[0] if bm else None,
            "max_scored_at": mx[0], "fetched_docs": mx[1],
            "max_claims_ts": nc[0], "claims_rounds": nc[1],
            "max_screened_at": sc[0], "screened_docs": sc[1]}


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


def audit_tab(cx, rep, tab, reg_tab=None):
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

    # ---- C5: closure-claim gate (deterministic canary-control) ---------------
    # Recompute the corpus-wide MUST-coverage aggregate under CURRENT keys and
    # assert the registered known-positive registers. If the canary is dark in
    # this very computation, ANY corpus-wide negative derived from it is void.
    canary = (reg_tab or {}).get("verbatim_canary")
    m_names = names_by_kind["M"]
    if canary and m_names:
        full_by_elem = {n: 0 for n in m_names}
        canary_full = None
        for d in docs:
            arr = jload(d["feature_scores"], [])
            if not arr:
                continue
            by_name = {s.get("name"): s.get("status") for s in arr
                       if isinstance(s, dict)}
            fulls = sum(1 for n in m_names if by_name.get(n) == "yes")
            for n in m_names:
                if by_name.get(n) == "yes":
                    full_by_elem[n] += 1
            if d["number"] == canary:
                canary_full = fulls
        zero_elems = [n for n, c in full_by_elem.items() if c == 0]
        if canary_full is None or canary_full == 0:
            rep.add("FAIL", tab, "C5-closure-gate",
                    f"canary {canary} registers {canary_full or 0}/{len(m_names)} "
                    "full MUST in the CURRENT-key aggregate — the store is broken "
                    "for negative claims. closure_claims_permitted: NONE",
                    data={"closure_claims_permitted": "NONE",
                          "canary_full": canary_full or 0})
        else:
            scope = ("corpus-wide negatives allowed ONLY scoped to current-key "
                     "reads" if orphans["feature_scores"] else "aggregate healthy")
            rep.add("PASS", tab, "C5-closure-gate",
                    f"canary {canary} registers {canary_full}/{len(m_names)} full "
                    f"MUST under current keys; zero-full elements now: "
                    f"{len(zero_elems)} ({[feat_norm(z)[:40] for z in zero_elems[:3]]}) "
                    f"— {scope}",
                    data={"closure_claims_permitted":
                          "SCOPED" if orphans["feature_scores"] else "FULL",
                          "canary_full": canary_full,
                          "zero_full_elements": len(zero_elems)})
    elif m_names:
        rep.add("WARN", tab, "C5-closure-gate",
                "no verbatim canary registered — negative aggregates have no "
                "known-positive control; closure claims must stay scoped")

    # ---- C6: falsification coverage of the top band --------------------------
    # Every top-K holistic doc must hold a deep read keyed to the CURRENT wording
    # — the 08-18 false closure stood on a top band whose reads were stale-keyed.
    TOP_K = 15
    top_band = sorted((d for d in docs if d["score"] is not None),
                      key=lambda d: (-(d["score"] or 0), d["id"]))[:TOP_K]
    stale_band = []
    for d in top_band:
        arr = jload(d["feature_scores"], [])
        if not arr:
            stale_band.append(f"{d['number']} (no per-element read)")
            continue
        # remap-tolerant, mirroring C1: a legacy-keyed store the deployed remap
        # recovers (exact/norm/position) is NOT stale — only unremappable is
        # (t10's numeral-stripped keys are healthy; t13's structural v1
        # rewording was not).
        if names_recoverable(arr, all_names) is None:
            stale_band.append(f"{d['number']} (stale-keyed, unremappable)")
    if stale_band:
        rep.add("FAIL", tab, "C6-falsification",
                f"{len(stale_band)}/{len(top_band)} top-band doc(s) lack a "
                f"CURRENT-key deep read — closure statements are forbidden until "
                f"the top band is re-read: {stale_band[:5]}",
                data={"stale_top_band": len(stale_band)})
    elif top_band:
        rep.add("PASS", tab, "C6-falsification",
                f"all top-{len(top_band)} docs hold current-key deep reads "
                "(falsification attempt available)")

    # ---- C7: DONE-message divergence (claim-weight ≠ relevance) --------------
    done_msg = cx.execute(
        "select text from messages where tab_id=? and role='s' "
        "and text like '%Claims audit%DONE%' order by id desc limit 1",
        (tab,)).fetchone()
    if done_msg:
        if ("Top by claim score" in done_msg[0]
                and "CURRENT CORPUS TOP" not in done_msg[0]):
            rep.add("WARN", tab, "C7-done-divergence",
                    "latest claims-audit DONE message lists 'Top by claim score' "
                    "WITHOUT the stored-score corpus-top alongside — claim-weight "
                    "is measured non-predictive of relevance (top claimants ≤3.0 "
                    "opus); readers will mistake it for a relevance ranking")
        else:
            rep.add("PASS", tab, "C7-done-divergence",
                    "DONE message carries the stored-score corpus-top context")

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
    ap.add_argument("--registry", default=None,
                    help="contents of docs/controls-registry.json")
    ap.add_argument("--baselines", default=None,
                    help="approved-known-baselines JSON from docs/failure-registry.md")
    ap.add_argument("--deploy-head", default=None)
    args = ap.parse_args()
    rep = Report()
    exit_code = None
    anch = {}
    try:
        reg = jload(args.registry, {}) if args.registry else {}
        reg_tabs = reg.get("tabs") or {}
        baselines = jload(args.baselines, {}) if args.baselines else {}
        cx = sqlite3.connect(DB, uri=True)
        tabs = ([args.tab] if args.tab else
                [r[0] for r in cx.execute(
                    "select tab_id from benchmark where status='ready' order by tab_id")])
        for t in tabs:
            audit_tab(cx, rep, t, reg_tabs.get(str(t)))
            anch[str(t)] = anchors(cx, t)
        # baseline governance: annotate each FAIL as KNOWN (registered, no growth)
        # or GATING. A registered count is a CEILING — growth gates.
        for r in rep.rows:
            if r["level"] != "FAIL":
                continue
            bl = (baselines.get(str(r["tab"])) or {}).get(r["check"])
            count = None
            m = re.match(r"(\d+)", r["msg"])
            if m:
                count = int(m.group(1))
            if bl and (count is None or count <= int(bl.get("count", 0))):
                r["baseline"] = "KNOWN"
                r["baseline_note"] = bl.get("approved", "")
            else:
                r["baseline"] = "GATING"
    except Exception as e:  # noqa: BLE001 — audit reports, never crashes silently
        rep.add("FAIL", 0, "C0-audit-crash", f"audit could not complete: {e}")
        exit_code = 3
    verdict = {"schema": SCHEMA, "audit": "ranking", "script_version": SCRIPT_VERSION,
               "ts": int(time.time()),
               "args": {k: v for k, v in vars(args).items()
                        if k not in ("registry", "baselines")},
               "deploy_head": args.deploy_head,
               "worst": ["PASS", "WARN", "FAIL"][rep.worst()] if exit_code != 3 else "INCOMPLETE",
               "rows": rep.rows, "anchors": anch}
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        with open(os.path.join(AUDIT_DIR, "audit_ranking.json"), "w") as fh:
            json.dump(verdict, fh, ensure_ascii=False, indent=1)
        with open(os.path.join(AUDIT_DIR, "history.jsonl"), "a") as fh:
            fh.write(json.dumps({k: verdict[k] for k in
                                 ("audit", "ts", "worst", "deploy_head")}) + "\n")
    except OSError as e:
        print(f"⚠ verdict file not written: {e}", file=sys.stderr)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=1))
    else:
        for r in rep.rows:
            icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}[r["level"]]
            tag = f" [{r['baseline']}]" if r.get("baseline") else ""
            print(f"{icon} t{r['tab']:>2} {r['check']:<20}{tag} {r['msg']}")
        print(f"\nVERDICT: {verdict['worst']}")
    sys.exit(exit_code if exit_code is not None else rep.worst())


main()
