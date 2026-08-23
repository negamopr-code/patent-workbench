#!/usr/bin/env python3
"""Deterministic full-document staging audit — the truncation NO-GO invariant
(user directive 2026-08-23): every candidate document, in every tab, must reach
NotebookLM in FULL, never clipped.

Run inside the app container (read-only on the DB):

    docker exec -e PYTHONPATH=/app/src patent-bench \
        python3 /app/scripts/audit_full_staging.py [--tab N] [--live]

Checks, per tab with fetched docs:
  A. SIZE CENSUS — how many docs exceed STAGE_PART_BYTES (need multi-part
     staging) and their expected part counts.
  B. LIVE NOTEBOOKS (--live; costs source-list calls, no Q&A quota) — for every
     notebook a lane is currently bound to (mega-screen state, claims-audit
     state), raw-list its sources, group parts by document number, and compare
     the present part count against the expected count for every staged doc.
     Any doc with fewer parts than expected = a LIVE BLIND TAIL → FAIL.
  C. ASSESSMENT PROVENANCE — oversized docs whose screen verdict
     (nlm_screen_state/nlm_screened_at) predates the multi-part-staging deploy
     (FULL_STAGING_EPOCH) were assessed truncated → listed as needing re-screen.

Output: human summary on stdout + JSON verdict at
/data/audits/audit_full_staging.json (the only write; DB is opened read-only).
Exit 0 = PASS (no live blind tails; pre-epoch list is informational),
exit 1 = FAIL (live blind tail or a lane staged a clipped source).
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402
from patentbench import db as pbdb  # noqa: E402

STAGE_PART_BYTES = 118_000
# multi-part staging entered the staging lanes at commit d4d8691 (2026-08-20
# 21:20 UTC); screen verdicts recorded before this moment saw clipped docs.
# (9dda5f1/bca8e72 on 08-23 closed the residual fallback/rotation/part gaps.)
FULL_STAGING_EPOCH = 1787261400   # 2026-08-20 21:30 UTC


def composed_size(row) -> int:
    parts = [f"{row['number']} — {row['title'] or ''}"]
    for label, col in (("ABSTRACT", "abstract"), ("CLAIMS", "claims"),
                       ("DESCRIPTION", "description"), ("FULL-TEXT DIGEST", "digest")):
        if row[col]:
            parts.append(f"{label}:\n{row[col]}")
    return len("\n\n".join(parts).encode("utf-8"))


def expected_parts(size: int) -> int:
    return 1 if size <= STAGE_PART_BYTES else math.ceil(size / STAGE_PART_BYTES)


def state_file(kind: str, tab_id: int) -> dict | None:
    p = os.path.join(os.path.dirname(pbdb.DB_PATH) or ".", f".nlm_{kind}_{tab_id}.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


PART_RE = re.compile(r"\(part\s+(\d+)/(\d+)\)")
NUM_RE = re.compile(r"^([A-Z]{2}[0-9A-Z]+)")


def audit_live_notebook(nb: str, prof: str | None, docs_by_num: dict) -> list[dict]:
    """Group a notebook's raw sources by doc number; flag missing tail parts."""
    raw = nlm_bridge.list_sources(nb, force=True, profile=prof)
    if raw.get("error"):
        return [{"notebook": nb, "error": raw["error"]}]
    by_num: dict[str, set[int]] = {}
    declared: dict[str, int] = {}
    for s in raw.get("sources") or []:
        t = (s.get("title") or "").strip()
        if t.startswith("🎯 BENCHMARK"):
            continue
        m = NUM_RE.match(t)
        if not m:
            continue
        num = m.group(1)
        pm = PART_RE.search(t)
        k, total = (int(pm.group(1)), int(pm.group(2))) if pm else (1, None)
        by_num.setdefault(num, set()).add(k)
        if total:
            declared[num] = max(declared.get(num, 0), total)
    problems = []
    for num, ks in by_num.items():
        row = docs_by_num.get(num)
        want = expected_parts(composed_size(row)) if row is not None else declared.get(num, 1)
        want = max(want, declared.get(num, 1))
        have = len(ks)
        if have < want or set(range(1, want + 1)) - ks:
            problems.append({"notebook": nb, "number": num,
                             "parts_present": sorted(ks), "parts_expected": want})
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, default=None)
    ap.add_argument("--live", action="store_true",
                    help="also raw-list the lanes' live notebooks (source-list calls)")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{pbdb.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tabs = [args.tab] if args.tab else [r["tab_id"] for r in con.execute(
        "select distinct tab_id from documents where status='fetched' order by tab_id")]

    report = {"ran_at": time.time(), "epoch": FULL_STAGING_EPOCH, "tabs": {}}
    live_fail = False
    for tid in tabs:
        rows = list(con.execute(
            "select id, number, title, abstract, claims, description, digest, "
            "nlm_screened_at, nlm_screen_state from documents "
            "where tab_id=? and status='fetched'", (tid,)))
        over, pre_epoch = [], []
        docs_by_num = {}
        for r in rows:
            docs_by_num[r["number"]] = r
            sz = composed_size(r)
            if sz > STAGE_PART_BYTES:
                over.append(r["number"])
                if r["nlm_screened_at"] and r["nlm_screened_at"] < FULL_STAGING_EPOCH \
                        and r["nlm_screen_state"] not in (None, "", "add_failed"):
                    pre_epoch.append(r["number"])
        entry = {"docs": len(rows), "oversized": len(over),
                 "assessed_truncated": len(pre_epoch),
                 "assessed_truncated_numbers": pre_epoch}
        if args.live:
            problems = []
            prof_row = con.execute("select nlm_profile from tabs where id=?", (tid,)).fetchone()
            prof = prof_row["nlm_profile"] if prof_row else None
            for kind in ("screen", "claims"):
                st = state_file(kind, tid)
                nb = (st or {}).get("notebook_id")
                if nb:
                    problems += audit_live_notebook(nb, prof, docs_by_num)
            entry["live_problems"] = problems
            if any("number" in p for p in problems):
                live_fail = True
        report["tabs"][str(tid)] = entry
        print(f"tab {tid:3}: {len(rows):5} docs · {len(over):4} oversized · "
              f"{len(pre_epoch):4} assessed-truncated (pre-epoch)"
              + (f" · live problems: {len(entry.get('live_problems') or [])}"
                 if args.live else ""))

    verdict = "FAIL" if live_fail else "PASS"
    report["verdict"] = verdict
    os.makedirs("/data/audits", exist_ok=True)
    with open("/data/audits/audit_full_staging.json", "w") as f:
        json.dump(report, f, indent=1)
    print(f"VERDICT: {verdict}  (report: /data/audits/audit_full_staging.json)")
    return 1 if live_fail else 0


if __name__ == "__main__":
    sys.exit(main())
