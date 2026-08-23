#!/usr/bin/env python3
"""Fix #2 validation: does a tier-2.5 'FUNCTION' question separate t12's opus
champions from the measured top-band contamination (words≠meaning class)?

Runs INSIDE patent-bench but NEVER touches the server — drives the nlm CLI
directly on the idle work2 profile:

    docker exec -i -e NLM_BIN=/opt/nlmvenv/bin/nlm -e PYTHONPATH=/app/src \
        patent-bench python3 - < scripts/validate_function_question.py

Design: take t12's quoted-MUST audit; pick the top-band docs that HAVE opus
ground truth — champions (opus>=4) and contaminated (opus<=2) with verified
weight >=6; stage them + the benchmark into ONE throwaway work2 notebook; ask
one function question per doc x verified-feature; score FUNCTION-weight per
doc; report separation vs the opus labels. State saved to
/data/.t25_validation_2026-08-18.json; the notebook is deleted at the end.
"""
import json
import re
import sqlite3
import sys
import time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

TAB = 12
PROFILE = "work2"
CLAIMS = "/data/.nlm_claims_12.json.must"
OUT = "/data/.t25_validation_2026-08-18.json"
N_PER_CLASS = 6
MIN_W = 6          # verified weight band where contamination lives
CHAMP_MIN, CONTAM_MAX = 4.0, 2.0


def doc_source_text(doc):
    return "\n\n".join(filter(None, [
        f"{doc['number']} — {doc.get('title') or ''}",
        ("ABSTRACT:\n" + doc["abstract"]) if doc.get("abstract") else None,
        ("CLAIMS:\n" + doc["claims"]) if doc.get("claims") else None,
        ("DESCRIPTION:\n" + doc["description"]) if doc.get("description") else None]))


def main():
    st = json.load(open(CLAIMS))
    must = st["must"]
    weights = {str(i): w for i, (_t, w) in enumerate(must, 1)}
    texts = {str(i): t for i, (t, _w) in enumerate(must, 1)}
    claims = st["claims"]

    con = sqlite3.connect("/data/workbench.db")
    con.row_factory = sqlite3.Row
    docs = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM documents WHERE tab_id=?", (TAB,))}
    bm = con.execute("SELECT * FROM benchmark WHERE tab_id=?", (TAB,)).fetchone()

    cand = []
    for did_s, feats in claims.items():
        did = int(did_s)
        d = docs.get(did)
        if not d or d["score"] is None or "opus" not in (d["score_model"] or ""):
            continue
        ok = {k: sv for k, sv in feats.items() if sv[0] in ("verified", "fuzzy")}
        w = sum(weights.get(k, 0) for k in ok)
        if w >= MIN_W and ok:
            cand.append({"id": did, "number": d["number"], "w": w, "ok": ok,
                         "opus": d["score"]})
    champs = sorted((c for c in cand if c["opus"] >= CHAMP_MIN),
                    key=lambda c: -c["w"])[:N_PER_CLASS]
    contam = sorted((c for c in cand if c["opus"] <= CONTAM_MAX),
                    key=lambda c: -c["w"])[:N_PER_CLASS]
    picked = champs + contam
    print(f"validation set: {len(champs)} champions / {len(contam)} contaminated "
          f"(verified w>={MIN_W}, opus-labeled)")
    for c in picked:
        print(f"  {c['number']:>16} w={c['w']:>2} opus={c['opus']} "
              f"feats={sorted(c['ok'])}")

    nb = nlm_bridge.create_notebook("t2.5 function validation (temp)",
                                    profile=PROFILE)
    if "error" in nb:
        sys.exit(f"notebook create failed: {nb['error']}")
    nbid = nb["id"]
    print("notebook:", nbid)
    try:
        bmd = dict(bm)
        bm_text = bmd.get("text") or "\n\n".join(filter(None, [
            f"BENCHMARK {bmd.get('number') or ''} — {bmd.get('title') or ''}",
            bmd.get("abstract") and "ABSTRACT:\n" + bmd["abstract"],
            bmd.get("claims") and "CLAIMS:\n" + bmd["claims"],
            bmd.get("description") and "DESCRIPTION:\n" + bmd["description"]]))
        r = nlm_bridge.add_source_text(
            nbid, "🎯 BENCHMARK — " + (bmd.get("number") or "benchmark"),
            bm_text, profile=PROFILE)
        if not r.get("ok"):
            sys.exit(f"benchmark add failed: {r.get('error')}")
        for i, c in enumerate(picked):
            d = docs[c["id"]]
            # stage in parts — truncation NO-GO (2026-08-23): >118KB docs must
            # never lose their tail to the CLI's single-source clip
            text = doc_source_text(d)
            data = text.encode("utf-8")
            base = f"{d['number']} — {(d['title'] or '')[:120]}"
            parts, j = [], 0
            while j < len(data):
                chunk = data[j:j + 118_000].decode("utf-8", "ignore")
                if not chunk:
                    break
                parts.append(chunk)
                j += len(chunk.encode("utf-8"))
            for k, p in enumerate(parts):
                title = (base if k == 0 else
                         f"{d['number']} (part {k + 1}/{len(parts)}) — {(d['title'] or '')[:100]}")
                r = nlm_bridge.add_source_text(nbid, title, p, profile=PROFILE)
                if not r.get("ok"):
                    sys.exit(f"source add {d['number']} failed: {r.get('error')}")
            print(f"staged {i + 1}/{len(picked)}", flush=True)
        w = nlm_bridge.wait_sources_ready(nbid, timeout=900, profile=PROFILE)
        print("ingest:", w)
        if not w.get("ready"):
            sys.exit("sources never became ready — aborting before wasting a query")

        def build_question(group):
            feat_lines = "\n".join(f"F{k}: {texts[k][:180]}"
                                   for k in sorted(weights, key=int))
            audit_lines = "\n".join(
                f"- {c['number']}: " + ", ".join(
                    f"F{k}" for k in sorted(c["ok"], key=int))
                for c in group)
            return (
                "Earlier keyword screening matched the listed feature numbers in "
                "each candidate document below. The 🎯 BENCHMARK source defines "
                "the reference system. Vocabulary presence is NOT enough: for "
                "EACH document and EACH of its listed features, judge whether the "
                "document's matching disclosure, read in context, actually "
                "PERFORMS THE FEATURE'S FUNCTION in the same role the benchmark "
                "assigns it (same kind of system, same purpose). Answer EXACTLY "
                "one line per document×feature:\n"
                "FUNCTION <publication number> | F<n> | YES or PARTIAL or NO | "
                "one-sentence reason grounded in that document.\n"
                "=== MANDATORY FEATURES ===\n" + feat_lines +
                "\n=== AUDIT LIST ===\n" + audit_lines)

        ans = ""
        groups = [picked]
        while groups:
            g = groups.pop(0)
            res = nlm_bridge.query(nbid, build_question(g), profile=PROFILE)
            err = str(res.get("error") or "")
            if "INVALID_ARGUMENT" in err and len(g) > 3:
                groups = [g[:len(g) // 2], g[len(g) // 2:]] + groups
                print(f"query too large — splitting {len(g)} docs")
                continue
            if "error" in res:
                print(f"⚠ query failed (notebook KEPT for retry: {nbid}): {err}")
                sys.exit(1)
            ans += "\n" + (res.get("answer") or "")
        verdicts = {}
        for m in re.finditer(
                r"FUNCTION\s+([A-Z]{2}[0-9A-Z/]+)\s*\|\s*F(\d+)\s*\|\s*"
                r"(YES|PARTIAL|NO)", ans, re.I):
            verdicts.setdefault(m.group(1).upper().replace("/", ""), {})[
                m.group(2)] = m.group(3).upper()

        print("\n== function-weight vs opus label ==")
        rows = []
        for c in picked:
            v = verdicts.get(c["number"].upper(), {})
            fw = sum(weights.get(k, 0) for k, verdict in v.items()
                     if verdict in ("YES", "PARTIAL"))
            label = "CHAMP" if c["opus"] >= CHAMP_MIN else "contam"
            rows.append({"number": c["number"], "label": label, "opus": c["opus"],
                         "w_quote": c["w"], "w_function": fw, "verdicts": v})
            print(f"  {c['number']:>16} {label} opus={c['opus']} "
                  f"quote-w={c['w']:>2} -> function-w={fw:>2}  {v}")
        json.dump({"notebook": nbid, "picked": rows, "answer": ans},
                  open(OUT, "w"), indent=1)
        print("saved ->", OUT)
        ch = [r for r in rows if r["label"] == "CHAMP"]
        co = [r for r in rows if r["label"] == "contam"]
        if ch and co:
            ch_min = min(r["w_function"] for r in ch)
            co_max = max(r["w_function"] for r in co)
            print(f"\nseparation: champions function-w min={ch_min}, "
                  f"contaminated max={co_max} -> "
                  + ("CLEAN SPLIT ✅" if ch_min > co_max else "overlap ⚠"))
        d = nlm_bridge.delete_notebook(nbid, profile=PROFILE)
        print("cleanup notebook:", d)
    except SystemExit:
        raise
    except Exception:
        print(f"⚠ crashed — notebook KEPT for inspection: {nbid}")
        raise


if __name__ == "__main__":
    main()
