#!/usr/bin/env python3
"""Pipeline-integrity status — the supervisor's deterministic core (F7).

Reads every verdict file under /data/audits/, recomputes the live data-watermark
anchors, and prints (a) a freshness table and (b) the GATE MATRIX verdict for
the actions the session may want to take. Evidence-based: a verdict file exists
iff its audit script actually ran — journal claims don't count.

Freshness is by DATA WATERMARK, not mtime: a verdict is STALE the moment any
live anchor (benchmark.updated_at, max scored_at, doc count, max nlm_claims.ts,
round count) differs from the one recorded in it — this natively encodes the
post-sweep / post-rewording / post-new-reads triggers. Deploys: pass
--deploy-head "$(git -C /workspace log -1 --format=%h -- src scripts)"; any
verdict recorded under a different head is STALE.

Gate matrix (BLOCKED reasons name the exact red gates):
  post_sweep_results   needs FRESH staging(S1,S3,S4) + recall(R1,R2,R3)
  champion_report      needs FRESH ranking(C1,C2,C6,C7) + recall(R5 if lanes ran)
  closure_claim        needs FRESH ranking C5 closure_claims_permitted != NONE,
                       C6 PASS, staging S1 blind-tails disclosed, recall R1
                       measured (scoped claims allowed when SCOPED)
  resume_after_deploy  every verdict must carry the current --deploy-head

Baseline governance: --baselines (the fenced JSON in docs/failure-registry.md)
is the ONLY source of KNOWN; any unregistered FAIL, or growth over a registered
count, gates regardless of any auditor's prose.

Run:
  docker exec -i patent-bench python3 - [--tab N] [--json] \
      [--baselines "$JSON"] [--deploy-head H] < scripts/audit_status.py
Exit: 0 = all requested gates green, 1 = warnings/scoped only, 2 = blocked.
"""
import argparse
import json
import os
import sqlite3
import sys
import time

DB = "file:/data/workbench.db?mode=ro"
AUDIT_DIR = "/data/audits"
AUDITS = ("staging", "recall", "ranking")


def jload(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


def live_anchors(cx, tab):
    bm = cx.execute("select updated_at from benchmark where tab_id=?", (tab,)).fetchone()
    mx = cx.execute("select max(scored_at), count(*) from documents "
                    "where tab_id=? and status='fetched'", (tab,)).fetchone()
    nc = cx.execute("select max(ts), count(*) from nlm_claims where tab_id=?",
                    (tab,)).fetchone()
    return {"benchmark_updated_at": bm[0] if bm else None,
            "max_scored_at": mx[0], "fetched_docs": mx[1],
            "max_claims_ts": nc[0], "claims_rounds": nc[1]}


def load_verdict(name):
    p = os.path.join(AUDIT_DIR, f"audit_{name}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def rows_for(verdict, tab, check_prefixes):
    out = []
    for r in (verdict or {}).get("rows", []):
        if tab is not None and r.get("tab") != tab:
            continue
        if any(r.get("check", "").startswith(p) for p in check_prefixes):
            out.append(r)
    return out


def gating_fail(rows, baselines, tab):
    """FAIL rows not covered by an approved baseline (or growing past it)."""
    out = []
    for r in rows:
        if r["level"] != "FAIL":
            continue
        if r.get("baseline") == "KNOWN":
            continue
        bl = (baselines.get(str(tab)) or {}).get(r.get("check"))
        if bl:
            import re as _re
            m = _re.match(r"(\d+)", r.get("msg", ""))
            if m and int(m.group(1)) <= int(bl.get("count", 0)):
                continue
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baselines", default=None)
    ap.add_argument("--deploy-head", default=None)
    args = ap.parse_args()
    baselines = jload(args.baselines, {}) if args.baselines else {}
    cx = sqlite3.connect(DB, uri=True)
    tabs = ([args.tab] if args.tab else
            [r[0] for r in cx.execute(
                "select tab_id from benchmark where status='ready' order by tab_id")])

    verdicts = {n: load_verdict(n) for n in AUDITS}
    pending = None
    pt = os.path.join(AUDIT_DIR, "pending_trigger.json")
    if os.path.exists(pt):
        try:
            with open(pt) as fh:
                pending = json.load(fh)
        except (OSError, ValueError):
            pending = {"raw": "unreadable"}

    report = {"ts": int(time.time()), "deploy_head": args.deploy_head,
              "pending_trigger": pending, "tabs": {}}
    worst = 0
    for tab in tabs:
        live = live_anchors(cx, tab)
        fresh = {}
        for name, v in verdicts.items():
            if v is None:
                fresh[name] = "MISSING"
                continue
            if v.get("worst") == "INCOMPLETE":
                fresh[name] = "INCOMPLETE"
                continue
            rec = (v.get("anchors") or {}).get(str(tab))
            if rec is None:
                fresh[name] = "MISSING(tab)"
            elif rec != live:
                fresh[name] = "STALE"
            elif args.deploy_head and v.get("deploy_head") not in (None, args.deploy_head):
                fresh[name] = "STALE(deploy)"
            else:
                fresh[name] = "FRESH"

        gates = {}
        # --- gate: post_sweep_results ---
        blockers = [n for n in ("staging", "recall") if fresh[n] != "FRESH"]
        red = []
        if not blockers:
            red += [r["check"] for r in gating_fail(
                rows_for(verdicts["staging"], tab, ("S3", "S4")), baselines, tab)]
            red += [r["check"] for r in gating_fail(
                rows_for(verdicts["recall"], tab, ("R2",)), baselines, tab)]
            if not rows_for(verdicts["recall"], tab, ("R1",)):
                red.append("R1-missing")
        gates["post_sweep_results"] = ("BLOCKED", blockers + red) if (blockers or red) \
            else ("PERMITTED", [])
        # --- gate: champion_report ---
        blockers = [n for n in ("ranking",) if fresh[n] != "FRESH"]
        red = []
        if not blockers:
            red += [r["check"] for r in gating_fail(
                rows_for(verdicts["ranking"], tab, ("C1", "C2", "C6")), baselines, tab)]
            r5 = rows_for(verdicts["recall"], tab, ("R5",)) if fresh.get("recall") == "FRESH" else []
            red += [r["check"] for r in r5 if r["level"] == "FAIL"]
            s1 = rows_for(verdicts["staging"], tab, ("S1-blind",)) if fresh.get("staging") == "FRESH" else []
            if any(r["level"] == "FAIL" for r in s1):
                red.append("S1-blind-tails(must-be-disclosed-alongside)")
        gates["champion_report"] = ("BLOCKED", blockers + red) if blockers or any(
            not x.startswith("S1-blind") for x in red) else (
            ("PERMITTED+DISCLOSE" if red else "PERMITTED"), red)
        # --- gate: closure_claim ---
        blockers = [n for n in AUDITS if fresh[n] != "FRESH"]
        red, scoped = [], False
        if not blockers:
            c5 = rows_for(verdicts["ranking"], tab, ("C5",))
            perm = (c5[0].get("data") or {}).get("closure_claims_permitted") if c5 else None
            if perm == "NONE" or not c5:
                red.append("C5-canary-dark")
            elif perm == "SCOPED":
                scoped = True
            red += [r["check"] for r in gating_fail(
                rows_for(verdicts["ranking"], tab, ("C6",)), baselines, tab)]
            r1 = rows_for(verdicts["recall"], tab, ("R1",))
            if not r1:
                red.append("R1-missing")
            elif any(r["level"] == "FAIL" for r in r1):
                scoped = True
            if any(r["level"] == "FAIL" for r in rows_for(
                    verdicts["staging"], tab, ("S1-blind",))):
                scoped = True
        if blockers or red:
            gates["closure_claim"] = ("BLOCKED", blockers + red)
        elif scoped:
            gates["closure_claim"] = ("SCOPED-ONLY", ["negatives must be scoped: "
                                                      "'among current-key reads'"])
        else:
            gates["closure_claim"] = ("PERMITTED", [])

        w = 2 if any(g[0] == "BLOCKED" for g in gates.values()) else (
            1 if any(g[0] in ("SCOPED-ONLY", "PERMITTED+DISCLOSE") for g in gates.values()) else 0)
        worst = max(worst, w)
        report["tabs"][str(tab)] = {"freshness": fresh, "gates":
                                    {k: {"verdict": v[0], "red": v[1]}
                                     for k, v in gates.items()}}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        if pending:
            print(f"⏰ PENDING TRIGGER: {pending}")
        for tab, t in report["tabs"].items():
            print(f"— tab {tab} —")
            print("  freshness:", ", ".join(f"{k}:{v}" for k, v in t["freshness"].items()))
            for g, gv in t["gates"].items():
                mark = {"PERMITTED": "🟢", "PERMITTED+DISCLOSE": "🟡",
                        "SCOPED-ONLY": "🟡", "BLOCKED": "🔴"}[gv["verdict"]]
                red = f" — red: {gv['red']}" if gv["red"] else ""
                print(f"  {mark} {g}: {gv['verdict']}{red}")
        print(f"\nSUPERVISOR: {'REPORTING PERMITTED' if worst == 0 else ('SCOPED/DISCLOSE ONLY' if worst == 1 else 'BLOCKED — spawn/refresh the auditors named above')}")
    sys.exit(worst)


main()
