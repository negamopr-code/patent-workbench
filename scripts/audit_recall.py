#!/usr/bin/env python3
"""Recall-integrity audit (F3a batch competition, F3b follow-up coverage,
F3d canary semantics, F3e recall measurement, F5 lane blind stripes).

Born from measured 2026-08-20 failures: the t13 claims-audit sweep's recall
against the opus >=4.0 ground truth was 0/14 (t10: 7/12) while the verbatim
canary scored 9/9 — proven mechanisms: roster-35 answer-budget competition
(same doc + same question: roster-35 -> 0/9 claimed, roster-10 -> 7/9) and the
120KB staging clip. A per-doc follow-up question produced near-opus verdicts
for free but the sweep never asks follow-ups. The embed lane ranked a champion
at 1778/2058 (claims-text-only window).

Runs INSIDE the patent-bench container, read-only on the DB; writes ONLY its
verdict files under /data/audits/:
  docker exec -i patent-bench python3 - [--tab N] [--json] \
      [--registry "$(cat docs/controls-registry.json)"] \
      [--lane-report /data/audits/lane_<name>_<tab>.json ...] \
      [--gt-min 4.0] [--max-batch 12] [--deploy-head H] < scripts/audit_recall.py

Checks (per tab with nlm_claims rounds):
  R1 recall join      — sweep claims vs opus ground truth (score >= gt-min,
                        opus models, verbatim canary excluded). FAIL < 0.5,
                        WARN < 0.8. The "recall: X/Y" line MUST accompany any
                        sweep conclusion.
  R2 batch corridor   — per-round roster sizes from the nlm_claims table;
                        any round > max-batch (proven competition boundary)
                        -> FAIL for recall purposes (discovery-only otherwise).
  R3 verbatim canary  — claimed by the sweep? PASS labeled "plumbing only" —
                        NEVER evidence of recall.
  R4 paraphrased canaries — from the controls registry; none planted ->
                        standing WARN; planted but UNclaimed -> FAIL.
  R5 lane controls    — every lane report must rank the verbatim canary AND
                        every champion control within expect_lane_rank_max.
                        Lane report JSON: {"lane": name, "tab": N,
                        "ranks": {"<number>": rank}, "total": M}.
  R6 follow-up coverage — flagged docs (quiet ground-truth docs, blind-tail
                        restages) vs /data/audits/followup_ledger.jsonl;
                        emits the nlm-followup-verifier work queue.

Exit: 0 PASS, 1 WARN, 2 FAIL, 3 audit could not complete (= missing evidence).
"""
import argparse
import json
import os
import sqlite3
import sys
import time

DB = "file:/data/workbench.db?mode=ro"
AUDIT_DIR = "/data/audits"
SCHEMA = 1
SCRIPT_VERSION = "2026-08-20.1"
OPUS_MODELS = {"claude-fable-5", "claude-opus-5", "claude-opus-4-8"}


def jload(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


class Report:
    def __init__(self):
        self.rows = []

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


def ledger_docs(tab):
    """Doc numbers already covered by follow-up rounds (from the A4 ledger)."""
    p = os.path.join(AUDIT_DIR, "followup_ledger.jsonl")
    done = set()
    if os.path.exists(p):
        with open(p) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("tab") == tab:
                    done.update(e.get("docs") or [])
    return done


def audit_tab(cx, rep, tab, reg_tab, args, lane_reports):
    # --since-ts scopes the audit to ONE sweep generation — without it a re-sweep's
    # recall would be polluted by the previous sweep's claims rows.
    rounds = cx.execute("select round, roster, claims, kind from nlm_claims "
                        "where tab_id=? and ts>=? order by round",
                        (tab, args.since_ts or 0)).fetchall()
    if not rounds:
        rep.add("INFO", tab, "R0", "no nlm_claims rounds — nothing to audit")
        return
    claimed_ids = set()
    for _, _, cl, _ in rounds:
        claimed_ids.update(int(k) for k in jload(cl, {}))
    canary = (reg_tab or {}).get("verbatim_canary")
    gt_min = float((reg_tab or {}).get("ground_truth_min_score", args.gt_min))

    # ---- R1: recall vs opus ground truth -------------------------------------
    gt = [dict(zip(("id", "number", "score"), r)) for r in cx.execute(
        "select id, number, score from documents where tab_id=? and score>=? "
        "and score_model in (%s) order by score desc"
        % ",".join("?" * len(OPUS_MODELS)),
        (tab, gt_min, *OPUS_MODELS))]
    gt = [d for d in gt if d["number"] != canary]
    if gt:
        hit = [d for d in gt if d["id"] in claimed_ids]
        recall = len(hit) / len(gt)
        lvl = "FAIL" if recall < 0.5 else ("WARN" if recall < 0.8 else "PASS")
        missed = [d["number"] for d in gt if d["id"] not in claimed_ids]
        rep.add(lvl, tab, "R1-recall",
                f"sweep recall vs opus ≥{gt_min} ground truth: {len(hit)}/{len(gt)} "
                f"({recall:.0%}). A NON-claim clears NOTHING. Missed: {missed[:6]}",
                data={"recall": recall, "hit": len(hit), "gt": len(gt),
                      "missed": missed})
    else:
        rep.add("WARN", tab, "R1-recall",
                f"no opus ≥{gt_min} ground truth exists — recall UNMEASURABLE; "
                "sweep results must not be presented as clearance")

    # ---- R2: batch-size corridor --------------------------------------------
    sizes = [len(jload(ro, [])) for _, ro, _, _ in rounds]
    big = [s for s in sizes if s > args.max_batch]
    if big:
        rep.add("FAIL", tab, "R2-batch-corridor",
                f"{len(big)}/{len(sizes)} round(s) ran roster > {args.max_batch} "
                f"(max {max(big)}) — proven answer-budget competition zone "
                f"(roster-35 → 0/9 vs roster-10 → 7/9). These rounds are "
                "DISCOVERY-ONLY, never clearance.",
                data={"rounds_over": len(big), "max_roster": max(big)})
    else:
        rep.add("PASS", tab, "R2-batch-corridor",
                f"all {len(sizes)} round(s) within roster ≤ {args.max_batch}")

    # ---- R3: verbatim canary (plumbing only) --------------------------------
    if canary:
        crow = cx.execute("select id from documents where tab_id=? and number=?",
                          (tab, canary)).fetchone()
        if crow and crow[0] in claimed_ids:
            rep.add("PASS", tab, "R3-verbatim-canary",
                    f"{canary} claimed — PLUMBING VALID (worded in the benchmark's "
                    "own phrasing; says NOTHING about recall)")
        else:
            rep.add("FAIL", tab, "R3-verbatim-canary",
                    f"verbatim canary {canary} NOT claimed — the sweep plumbing "
                    "itself is broken (sources/answers/parser)")
    else:
        rep.add("WARN", tab, "R3-verbatim-canary",
                "no verbatim canary registered for this tab")

    # ---- R4: paraphrased canaries (the real recall control) ------------------
    para = (reg_tab or {}).get("paraphrased_canaries") or []
    if not para:
        rep.add("WARN", tab, "R4-paraphrased-canary",
                "NO paraphrased canaries planted — recall has no live control; "
                "plant known-relevant docs reworded away from the feature phrasing")
    else:
        nums = {num: did for did, num in cx.execute(
            "select id, number from documents where tab_id=?", (tab,))}
        dark = [p for p in para if nums.get(p) not in claimed_ids]
        if dark:
            rep.add("FAIL", tab, "R4-paraphrased-canary",
                    f"paraphrased canaries present but UNCLAIMED: {dark} — the "
                    "sweep is blind to paraphrase right now")
        else:
            rep.add("PASS", tab, "R4-paraphrased-canary",
                    f"all {len(para)} paraphrased canaries claimed")

    # ---- R5: lane controls ---------------------------------------------------
    controls = (reg_tab or {}).get("champion_controls") or []
    for lr in lane_reports:
        if lr.get("tab") != tab:
            continue
        ranks = lr.get("ranks") or {}
        lane = lr.get("lane", "?")
        bad = []
        if canary:
            cr = ranks.get(canary)
            if cr is None or cr > 30:
                bad.append(f"verbatim canary {canary} rank {cr}")
        for cc in controls:
            r = ranks.get(cc["number"])
            if r is None or r > cc.get("expect_lane_rank_max", 200):
                bad.append(f"{cc['number']} rank {r} (max "
                           f"{cc.get('expect_lane_rank_max', 200)})")
        if bad:
            rep.add("FAIL", tab, "R5-lane-controls",
                    f"lane '{lane}' fails its controls — its queue must NOT be "
                    f"trusted as coverage: {bad}")
        else:
            rep.add("PASS", tab, "R5-lane-controls",
                    f"lane '{lane}': canary + {len(controls)} champion control(s) "
                    "within expected ranks")

    # ---- R6: follow-up coverage → work queue --------------------------------
    done = ledger_docs(tab)
    quiet_gt = [d["number"] for d in gt if d["id"] not in claimed_ids
                and d["number"] not in done]
    queue = quiet_gt[:10]
    if queue:
        rep.add("WARN", tab, "R6-followup-queue",
                f"{len(quiet_gt)} quiet ground-truth doc(s) lack follow-up "
                f"verification — nlm-followup-verifier queue: {queue}",
                data={"queue": queue})
    elif gt:
        rep.add("PASS", tab, "R6-followup-queue",
                "every quiet ground-truth doc has follow-up coverage")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--lane-report", action="append", default=[])
    ap.add_argument("--gt-min", type=float, default=4.0)
    ap.add_argument("--max-batch", type=int, default=12)
    ap.add_argument("--since-ts", type=int, default=None,
                    help="only count nlm_claims rounds with ts >= this (scope to one sweep)")
    ap.add_argument("--deploy-head", default=None)
    args = ap.parse_args()
    rep = Report()
    exit_code = None
    anch = {}
    try:
        reg = jload(args.registry, {}) if args.registry else {}
        reg_tabs = reg.get("tabs") or {}
        lane_reports = []
        for p in args.lane_report:
            try:
                with open(p) as fh:
                    lane_reports.append(json.load(fh))
            except (OSError, ValueError) as e:
                rep.add("WARN", 0, "R5-lane-controls", f"lane report {p} unreadable: {e}")
        cx = sqlite3.connect(DB, uri=True)
        tabs = ([args.tab] if args.tab else
                [r[0] for r in cx.execute(
                    "select distinct tab_id from nlm_claims order by tab_id")])
        for t in tabs:
            audit_tab(cx, rep, t, reg_tabs.get(str(t)), args, lane_reports)
            anch[str(t)] = anchors(cx, t)
    except Exception as e:  # noqa: BLE001
        rep.add("FAIL", 0, "R0-audit-crash", f"audit could not complete: {e}")
        exit_code = 3
    verdict = {"schema": SCHEMA, "audit": "recall", "script_version": SCRIPT_VERSION,
               "ts": int(time.time()),
               "args": {k: v for k, v in vars(args).items() if k != "registry"},
               "deploy_head": args.deploy_head,
               "worst": ["PASS", "WARN", "FAIL"][rep.worst()] if exit_code != 3 else "INCOMPLETE",
               "rows": rep.rows, "anchors": anch}
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        with open(os.path.join(AUDIT_DIR, "audit_recall.json"), "w") as fh:
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
            print(f"{icon} t{r['tab']:>2} {r['check']:<22} {r['msg']}")
        print(f"\nVERDICT: {verdict['worst']}")
    sys.exit(exit_code if exit_code is not None else rep.worst())


main()
