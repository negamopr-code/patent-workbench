#!/usr/bin/env python3
"""Per-doc NLM follow-up interrogation (F3b) — the free "opus-lite" verify loop.

Proven 2026-08-20: a per-doc follow-up question in a small-roster notebook
produced near-opus per-feature YES/PARTIAL/NO verdicts with citations, for a
document the batch-35 sweep had claimed for ZERO features. This script is the
mechanics behind the nlm-followup-verifier agent: it re-stages up to 10 docs
into a DEDICATED follow-up notebook (never the rolling sweep notebook — F4),
splitting any >120KB doc into parts so nothing is clipped (F3c), asks the
sweep's checklist question once, then one follow-up per doc, and appends every
round to /data/audits/followup_ledger.jsonl. Writes notebooks + ledger ONLY —
never the DB (keeps the instrument distinction clean: follow-up answers are
NLM evidence, not scores).

Run INSIDE patent-bench:
  docker exec -i patent-bench python3 - --tab N --docs NUM1,NUM2,... \
      [--keep-notebook] [--json] < scripts/nlm_followup.py
Exit: 0 ok, 1 partial (some steps failed), 2 quota/abort, 3 could not run.
"""
import argparse
import json
import os
import sqlite3
import sys
import time

DB = "file:/data/workbench.db?mode=ro"
AUDIT_DIR = "/data/audits"
CLIP = 118_000          # stay under nlm_bridge's 120_000-byte clip per part
MAX_DOCS = 10
GENUS_MAPS = "/data/genus_maps.json"

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402


def compose_blob(num, title, ab, cl, de, dg):
    return "\n\n".join(filter(None, [
        f"{num} — {title or ''}",
        ("ABSTRACT:\n" + ab) if ab else None,
        ("CLAIMS:\n" + cl) if cl else None,
        ("DESCRIPTION:\n" + de) if de else None,
        ("FULL-TEXT DIGEST:\n" + dg) if dg else None]))


def split_parts(num, title, blob):
    """[(source_title, text)] — one part if it fits, else byte-safe parts so the
    120KB clip never removes content (F3c)."""
    data = blob.encode("utf-8")
    if len(data) <= CLIP:
        return [(f"{num} — {(title or '')[:120]}", blob)]
    parts, i, k = [], 0, 1
    while i < len(data):
        chunk = data[i:i + CLIP].decode("utf-8", "ignore")
        parts.append((None, chunk))
        i += len(chunk.encode("utf-8"))
        k += 1
    n = len(parts)
    return [(f"{num} (part {j + 1}/{n}) — {(title or '')[:100]}", t)
            for j, (_, t) in enumerate(parts)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, required=True)
    ap.add_argument("--docs", required=True, help="comma-separated doc numbers")
    ap.add_argument("--keep-notebook", action="store_true")
    ap.add_argument("--compact", action="store_true",
                    help="one CONSOLIDATED per-doc question instead of one query per doc "
                         "(2 NLM queries per notebook instead of 1+N) — quota doctrine "
                         "2026-08-27: big batches, few well-designed questions")
    ap.add_argument("--genus", action="store_true",
                    help="F3f vocabulary floor: broaden each term of art IN PLACE using "
                         "scripts/genus_maps.json for this tab. Adopted forward-only "
                         "2026-08-29 — evidence ab_clean_t10_1787900564.json (same sources, "
                         "verbatim wording NO/NO/NO vs genus wording YES/YES/YES).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    nums = [n.strip() for n in args.docs.split(",") if n.strip()][:MAX_DOCS]
    cx = sqlite3.connect(DB, uri=True)
    # the tab's pinned NLM account — creating/querying with the wrong profile
    # fails (t10 lives on a per-tab account); mirror of the S3 audit fix
    prow = cx.execute("select nlm_profile from tabs where id=?", (args.tab,)).fetchone()
    prof = prow[0] if prow else None

    st_path = f"/data/.nlm_claims_{args.tab}.json"
    must = []
    if os.path.exists(st_path):
        with open(st_path) as fh:
            must = (json.load(fh).get("must") or [])
    if not must:
        bm = cx.execute("select features_json from benchmark where tab_id=?",
                        (args.tab,)).fetchone()
        feats = json.loads(bm[0]) if bm and bm[0] else []
        must = [[f["name"], f.get("weight", 3)] for f in feats
                if (f.get("kind") or "M").upper() == "M"]
    if not must:
        print("no MUST features for this tab", file=sys.stderr)
        sys.exit(3)
    # strip reference numerals from the QUESTION (F-wording lesson 2026-08-20:
    # "(10)"-style numerals trigger false NOs when a doc's own numbering differs)
    import re as _re
    _refnum = _re.compile(r"\s*\(\s*\d+[A-Za-z]?(?:\s*,\s*\d+[A-Za-z]?)*\s*\)")
    def _clean(name):
        return _re.sub(r"[ ]+", " ", _refnum.sub("", name)).strip()

    genus_map, genus_hits = {}, 0
    if args.genus:
        try:
            with open(GENUS_MAPS) as fh:
                genus_map = (json.load(fh) or {}).get(str(args.tab)) or {}
        except Exception as e:  # noqa: BLE001
            print(f"genus map unreadable ({e}) — refusing to run a MIXED-wording "
                  "round; fix the map or drop --genus", file=sys.stderr)
            sys.exit(3)
        if not genus_map:
            print(f"no genus map for tab {args.tab} — refusing to run --genus with an "
                  "empty vocabulary (it would silently be the verbatim arm)", file=sys.stderr)
            sys.exit(3)

    def _genus(name):
        """Broaden the terms of art IN PLACE, keeping every structural element of the
        feature. Matches are computed against the ORIGINAL text and inserted
        right-to-left, so an expansion can never be re-matched by a later key
        (the nested-insert bug fixed in ab-wording-test.py on 2026-08-28)."""
        nonlocal genus_hits
        hits = []
        for key, expansion in genus_map.items():
            m = _re.search(_re.escape(key), name, _re.IGNORECASE)
            if m:
                hits.append((m.end(), expansion))
        if not hits:
            return name
        genus_hits += 1
        out = name
        for pos, expansion in sorted(hits, reverse=True):
            out = out[:pos] + f" [read broadly: {expansion}]" + out[pos:]
        return out

    spec = "\n".join(
        f"{i}. {(_genus(_clean(name)) if genus_map else _clean(name))} (importance {w}/5)"
        for i, (name, w) in enumerate(must, 1))

    docs = []
    for n in nums:
        r = cx.execute("select number, title, abstract, claims, description, digest "
                       "from documents where tab_id=? and number=? "
                       "and status='fetched'", (args.tab, n)).fetchone()
        if r:
            docs.append(r)
    if not docs:
        print("no fetched docs matched", file=sys.stderr)
        sys.exit(3)

    res = nlm_bridge.create_notebook(f"🔁 follow-up — tab {args.tab}", profile=prof)
    nb = res.get("id") or (res.get("notebook") or {}).get("id")
    if not nb:
        print(f"notebook create failed: {res}", file=sys.stderr)
        sys.exit(3)
    partial = False
    parts_ok = {}          # doc -> {"want": K, "ok": k}  (F3c: credit needs ALL parts)
    for num, title, ab, cl, de, dg in docs:
        want = split_parts(num, title, compose_blob(num, title, ab, cl, de, dg))
        parts_ok[num] = {"want": len(want), "ok": 0}
        for src_title, text in want:
            r = nlm_bridge.add_source_text(nb, src_title, text, profile=prof)
            if r.get("ok"):
                parts_ok[num]["ok"] += 1
            else:
                partial = True
    nlm_bridge.wait_sources_ready(nb, timeout=600, profile=prof)

    results = {"tab": args.tab, "notebook": nb, "ts": int(time.time()),
               "docs": [d[0] for d in docs], "answers": {}, "parts_ok": parts_ok,
               "wording": "genus" if genus_map else "verbatim",
               "genus_features": genus_hits, "spec": spec}
    # post-ingest source inventory: the ONLY re-verifiable evidence that the doc
    # reached the notebook in full, since the notebook is deleted afterwards
    try:
        inv = nlm_bridge.list_sources(nb, profile=prof)
        results["source_inventory"] = [s.get("title") for s in (inv.get("sources") or [])]
    except Exception as e:  # noqa: BLE001
        results["source_inventory_error"] = str(e)
    broad = ("For EACH numbered feature below, name every candidate source document "
             "that discloses it — explicitly or as an implicit realisation (a "
             "document that physically does it without the literal words). Reply "
             "with EXACTLY one line per feature: FEATURE <k>: <numbers or NONE>."
             "\n\n=== FEATURES ===\n" + spec)
    r = nlm_bridge.query(nb, broad, profile=prof)
    if nlm_bridge.is_quota_error(r):
        if not args.keep_notebook:      # never leak a notebook slot on abort
            try:
                nlm_bridge.delete_notebook(nb, profile=prof)
            except Exception:  # noqa: BLE001
                pass
        print("quota exhausted — aborting", file=sys.stderr)
        sys.exit(2)
    _b = r.get("answer") or r.get("error") or ""
    results["answers"]["_broad"] = _b
    # The compact consolidated question says "the checklist above" — if the broad
    # question did not actually land, NotebookLM invents its own feature list and
    # answers a DIFFERENT question (observed 2026-08-27 t14 chunk 1787848428: broad
    # died on a transport error, reply began "the checklist ... was not explicitly
    # provided ... we have ... designated F1 through F9"). Such a chunk must not be
    # asked, let alone credited.
    _bad = (not _b.strip()) or '"status": "error"' in _b or _b.lstrip().startswith("Query failed")
    if _bad:
        results["broad_failed"] = True
        partial = True
    if args.compact and results.get("broad_failed"):
        print("broad question failed — not asking the consolidated question "
              "(it would be answered against an invented checklist)", file=sys.stderr)
        for num, *_ in docs:
            results["answers"][num] = None
        docs_iter = []
    elif args.compact:
        listing = ", ".join(d[0] for d in docs)
        fu = ("Now go through EACH of these documents one by one: " + listing +
              ". For EVERY one of them, output exactly one block:\n"
              "<NUMBER>: F<k>=YES|PARTIAL|NO for every numbered feature of the "
              "checklist above, then ' | ' and a short justification citing where "
              "in that document (section/claim) the decisive disclosure is — or "
              "'no disclosure found' if none. Do not skip a document; if a document "
              "says nothing about a feature, still write F<k>=NO.")
        r = nlm_bridge.query(nb, fu, profile=prof)
        if nlm_bridge.is_quota_error(r):
            partial = True
            for num, *_ in docs:
                results["answers"][num] = "QUOTA-ABORT"
        else:
            ans = r.get("answer") or r.get("error") or ""
            results["answers"]["_consolidated"] = ans
            for num, *_ in docs:
                # a doc counts as answered only if the reply actually addresses it
                results["answers"][num] = ans if num in ans else None
                if num not in ans:
                    partial = True
        docs_iter = []
    else:
        docs_iter = docs
    for num, *_ in docs_iter:
        fu = (f"Now check ONE document specifically: {num}. For EACH numbered "
              "feature of the checklist above, state whether this document "
              "discloses it (YES / PARTIAL / NO) with a one-line justification "
              "citing where in the document.")
        r = nlm_bridge.query(nb, fu, profile=prof)
        if nlm_bridge.is_quota_error(r):
            partial = True
            results["answers"][num] = "QUOTA-ABORT"
            break
        results["answers"][num] = r.get("answer") or r.get("error")

    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(os.path.join(AUDIT_DIR, "followup_ledger.jsonl"), "a") as fh:
        fh.write(json.dumps({"tab": args.tab, "ts": results["ts"], "notebook": nb,
                             "mode": "compact" if args.compact else "per-doc",
                             "wording": "genus" if genus_map else "verbatim",
                             "docs": [d[0] for d in docs
                                      if results["answers"].get(d[0])
                                      not in (None, "QUOTA-ABORT")]},
                            ensure_ascii=False) + "\n")
    if not args.keep_notebook:
        nlm_bridge.delete_notebook(nb, profile=prof)
        results["notebook_deleted"] = True
    print(json.dumps(results, ensure_ascii=False, indent=1) if args.json
          else "\n\n".join(f"=== {k} ===\n{v}" for k, v in results["answers"].items()))
    sys.exit(1 if partial else 0)


main()
