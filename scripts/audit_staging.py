#!/usr/bin/env python3
"""Staging-completeness audit (F3c truncation + F4 rolling-notebook confound).

Born from two measured 2026-08-20 failures:
  F3c — nlm_bridge clips every staged source at 120_000 BYTES; CN116508192
        (280KB) lost its decisive paragraph [0193] at byte ~143k and was missed
        by the sweep. t13: 392/2058 docs staged truncated, 309 of them with NO
        deep read either (= "blind tails": content no instrument ever saw).
  F4  — an NLM answer was interpreted while the rolling staging notebook no
        longer held the relevant sources (rotation) — always verify the live
        source inventory before interpreting an answer.

Runs INSIDE the patent-bench container, read-only on the DB (sqlite mode=ro);
writes ONLY its verdict files under /data/audits/:
  docker exec -i patent-bench python3 - [--tab N] [--json] [--no-live] \
      [--deploy-head H] < scripts/audit_staging.py

Checks (per tab with an nlm_claims sweep state or nlm-screen history):
  S1 truncation census — recompose each fetched doc's staged blob exactly like
     api._doc_source_text (number/title + ABSTRACT + CLAIMS + DESCRIPTION +
     DIGEST) and measure UTF-8 BYTES vs the 120_000 clip.
     truncated AND never deep-read (score is null) = BLIND TAILS -> FAIL;
     truncated but deep-read (full text seen by a model)          -> WARN.
  S2 cut-point report — which section the clip lands in (INFO, top examples).
  S3 live inventory — the sweep state's notebook must currently hold exactly
     the last roster's sources (+ benchmark); any answer interpreted against a
     rotated notebook is void. Needs nlm_bridge (skipped under --no-live;
     list failure / quota -> WARN, never a crash).
  S4 claims-within-audited guard — every claimed doc id must be inside the
     audited prefix of the sweep queue (parser/state regression guard).

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
CLIP = 120_000
SCHEMA = 1
SCRIPT_VERSION = "2026-08-20.1"


def jload(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


def compose_blob(num, title, ab, cl, de, dg):
    """Byte-exact mirror of api._doc_source_text()."""
    return "\n\n".join(filter(None, [
        f"{num} — {title or ''}",
        ("ABSTRACT:\n" + ab) if ab else None,
        ("CLAIMS:\n" + cl) if cl else None,
        ("DESCRIPTION:\n" + de) if de else None,
        ("FULL-TEXT DIGEST:\n" + dg) if dg else None]))


def cut_section(blob):
    """Which section the 120KB byte-clip lands in."""
    b = blob.encode("utf-8")[:CLIP]
    head = b.decode("utf-8", "ignore")
    for marker, name in (("FULL-TEXT DIGEST:", "DIGEST"), ("DESCRIPTION:", "DESCRIPTION"),
                         ("CLAIMS:", "CLAIMS"), ("ABSTRACT:", "ABSTRACT")):
        if marker in head:
            return name
    return "TITLE"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, tab, check, msg, data=None):
        self.rows.append({"level": level, "tab": tab, "check": check, "msg": msg,
                          **({"data": data} if data is not None else {})})

    def worst(self):
        lv = [r["level"] for r in self.rows]
        return 2 if "FAIL" in lv else (1 if "WARN" in lv else 0)


def sweep_state(tab):
    p = f"/data/.nlm_claims_{tab}.json"
    if os.path.exists(p):
        try:
            with open(p) as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return None
    return None


def anchors(cx, tab):
    bm = cx.execute("select updated_at from benchmark where tab_id=?", (tab,)).fetchone()
    mx = cx.execute("select max(scored_at), count(*) from documents "
                    "where tab_id=? and status='fetched'", (tab,)).fetchone()
    nc = cx.execute("select max(ts), count(*) from nlm_claims where tab_id=?",
                    (tab,)).fetchone()
    return {"benchmark_updated_at": bm[0] if bm else None,
            "max_scored_at": mx[0], "fetched_docs": mx[1],
            "max_claims_ts": nc[0], "claims_rounds": nc[1]}


def audit_tab(cx, rep, tab, no_live):
    docs = [dict(zip(("id", "number", "score", "title", "ab", "cl", "de", "dg"), r))
            for r in cx.execute(
                "select id, number, score, title, abstract, claims, description, digest "
                "from documents where tab_id=? and status='fetched'", (tab,))]
    if not docs:
        return

    # ---- S1 + S2: truncation census over the composed staged blob ------------
    trunc, blind, cuts = [], [], {}
    for d in docs:
        blob = compose_blob(d["number"], d["title"], d["ab"], d["cl"], d["de"], d["dg"])
        n = len(blob.encode("utf-8"))
        if n > CLIP:
            sec = cut_section(blob)
            cuts[sec] = cuts.get(sec, 0) + 1
            trunc.append((d["number"], n, sec))
            if d["score"] is None:
                blind.append(d["number"])
    if blind:
        rep.add("FAIL", tab, "S1-blind-tails",
                f"{len(blind)} doc(s) staged TRUNCATED at {CLIP} bytes AND never "
                f"deep-read — their tails were never seen by ANY instrument. "
                f"E.g. {blind[:5]}",
                data={"blind": len(blind), "truncated": len(trunc)})
    if trunc and len(blind) < len(trunc):
        rep.add("WARN", tab, "S1-truncated-read",
                f"{len(trunc) - len(blind)} truncated doc(s) do have a full-length "
                f"deep read (tails seen by a model, invisible to NLM only)",
                data={"truncated_with_read": len(trunc) - len(blind)})
    if not trunc:
        rep.add("PASS", tab, "S1-truncation",
                f"no fetched doc exceeds the {CLIP}-byte staging clip")
    if trunc:
        ex = sorted(trunc, key=lambda t: -t[1])[:3]
        rep.add("INFO", tab, "S2-cut-points",
                f"clip lands in: {cuts}; largest: "
                + ", ".join(f"{n} ({b} B, cut in {s})" for n, b, s in ex))

    # ---- S3: live notebook inventory vs last roster --------------------------
    st = sweep_state(tab)
    if st:
        nb = st.get("notebook_id")
        roster = st.get("roster") or []
        if nb and roster and not no_live:
            try:
                sys.path.insert(0, "/app/src")
                from patentbench import nlm_bridge  # noqa: PLC0415
                # the tab's pinned NLM account — listing with the wrong profile
                # returns PERMISSION_DENIED (t10 lives on a per-tab account)
                prow = cx.execute("select nlm_profile from tabs where id=?",
                                  (tab,)).fetchone()
                prof = prow[0] if prow else None
                res = nlm_bridge.list_sources(nb, force=True, profile=prof)
                if res.get("error"):
                    rep.add("WARN", tab, "S3-live-inventory",
                            f"could not list sources of {nb}: {res['error'][:120]} — "
                            "do NOT interpret answers from this notebook until verified")
                else:
                    titles = [s.get("title") or "" for s in (res.get("sources") or [])]
                    nums_live = {t.split(" — ")[0].split(" (part")[0].strip()
                                 for t in titles if not t.startswith("🎯")}
                    by_id = {d["id"]: d["number"] for d in docs}
                    roster_nums = {by_id.get(i) for i in roster if by_id.get(i)}
                    missing = sorted(roster_nums - nums_live)
                    extra = sorted(nums_live - roster_nums)
                    if missing:
                        rep.add("FAIL", tab, "S3-live-inventory",
                                f"notebook {nb} is MISSING {len(missing)} of the last "
                                f"roster's sources (rotation happened): {missing[:5]} — "
                                "any answer interpreted now is VOID for those docs")
                    elif extra:
                        rep.add("WARN", tab, "S3-live-inventory",
                                f"notebook holds {len(extra)} source(s) beyond the last "
                                f"roster: {extra[:5]}")
                    else:
                        rep.add("PASS", tab, "S3-live-inventory",
                                f"notebook sources == last roster ({len(roster_nums)} docs)")
            except Exception as e:  # noqa: BLE001 — audit reports, never crashes
                rep.add("WARN", tab, "S3-live-inventory", f"live check failed: {e}")
        elif nb and roster:
            rep.add("INFO", tab, "S3-live-inventory", "skipped (--no-live)")

        # ---- S4: claims ⊆ audited prefix of the queue ------------------------
        queue, cursor = st.get("queue") or [], int(st.get("cursor", 0))
        audited = set(queue[:cursor]) if cursor else set(queue)
        claims = st.get("claims") or {}
        outside = [k for k in claims if int(k) not in audited]
        if outside:
            rep.add("FAIL", tab, "S4-claims-guard",
                    f"{len(outside)} claimed doc id(s) are OUTSIDE the audited queue "
                    f"prefix (parser/state regression): {outside[:5]}")
        elif claims:
            rep.add("PASS", tab, "S4-claims-guard",
                    f"all {len(claims)} claimed ids lie inside the audited prefix "
                    f"({len(audited)} docs)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--deploy-head", default=None)
    args = ap.parse_args()
    rep = Report()
    exit_code = None
    try:
        cx = sqlite3.connect(DB, uri=True)
        tabs = ([args.tab] if args.tab else
                [r[0] for r in cx.execute(
                    "select tab_id from benchmark where status='ready' order by tab_id")])
        anch = {}
        for t in tabs:
            audit_tab(cx, rep, t, args.no_live)
            anch[str(t)] = anchors(cx, t)
    except Exception as e:  # noqa: BLE001
        rep.add("FAIL", 0, "S0-audit-crash", f"audit could not complete: {e}")
        anch = {}
        exit_code = 3
    verdict = {"schema": SCHEMA, "audit": "staging", "script_version": SCRIPT_VERSION,
               "ts": int(time.time()), "args": vars(args),
               "deploy_head": args.deploy_head,
               "worst": ["PASS", "WARN", "FAIL"][rep.worst()] if exit_code != 3 else "INCOMPLETE",
               "rows": rep.rows, "anchors": anch}
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        with open(os.path.join(AUDIT_DIR, "audit_staging.json"), "w") as fh:
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
