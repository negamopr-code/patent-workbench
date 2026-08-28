#!/usr/bin/env python3
"""ab-wording-test — PERSISTED A/B of feature wording on one NotebookLM notebook.

Why: 2026-08-27 a follow-up run observed the same document flip NO/NO/NO -> YES/YES/YES
when only the QUESTION WORDING changed (benchmark vocabulary vs a genus expansion). The
answers were never persisted (notebook deleted, stdout discarded), so the supervisor
refused to register the proposed failure class F3f on it. This script re-runs the test
and PERSISTS everything: both roundsvs the same sources, in one notebook, per doc.

Round A = the tab's MUST checklist verbatim (the wording the sweep/screen uses).
Round B = the same features, each restated as a GENUS + synonyms, reference numerals
stripped, for every feature answered NO in round A on weight >= MINW.

Runs INSIDE patent-bench:
  docker cp scripts/ab-wording-test.py patent-bench:/data/ &&
  docker exec -d patent-bench python3 /data/ab-wording-test.py --tab 10 --docs NUM1,NUM2
Output: /data/audits/ab_wording_t<tab>_<ts>.json  (sources inventory + both rounds verbatim)
Uses ONE account (the tab's own). Deletes its notebook at the end unless --keep-notebook.
"""
import argparse, json, os, re, sqlite3, sys, time
sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

CLIP = 118_000
AUD = "/data/audits"

# genus expansions for this benchmark's terms of art; keyed by a distinctive token of
# the MUST feature text. Deliberately conservative: a superset phrasing, never a hint
# that the answer should be YES.
GENUS = {
    "wireless power supply": "wireless/contactless power transfer of any kind — microwave, RF, "
                             "inductive, magnetic-resonance or capacitive power feeding, including "
                             "'power transmission', 'energy harvesting from a transmitted field', "
                             "'RF-to-DC rectenna supply' and equivalents",
    "microwave": "microwave or radio-frequency carrier used to carry power (any band described as "
                 "RF, UHF, SHF, ISM, 2.4 GHz, 5.8 GHz, 900 MHz or 'radio waves')",
    "wireless communication link": "any bidirectional or unidirectional radio data link between the "
                                   "two named units — BLE, Zigbee, Wi-Fi, LoRa, NFC, proprietary "
                                   "sub-GHz, 'radio telemetry', 'wireless signal transmission'",
    "stroke position": "position/displacement/travel/extension of a moving member (piston, rod, "
                       "spool, plunger, slide, actuator shaft), whether reported as absolute "
                       "position, stroke length, displacement or travel",
    "magnetic sensor": "any magnetically-sensing element — Hall element, magnetoresistive (AMR/GMR/TMR), "
                       "reed, fluxgate, magnetostrictive or inductive position sensor",
}


def compose(num, title, ab, cl, de, dg):
    return "\n\n".join(filter(None, [f"{num} — {title or ''}",
                                     ("ABSTRACT:\n" + ab) if ab else None,
                                     ("CLAIMS:\n" + cl) if cl else None,
                                     ("DESCRIPTION:\n" + de) if de else None,
                                     ("FULL-TEXT DIGEST:\n" + dg) if dg else None]))


def parts(num, title, blob):
    data = blob.encode("utf-8")
    if len(data) <= CLIP:
        return [(f"{num} — {(title or '')[:120]}", blob)]
    out, i = [], 0
    while i < len(data):
        ch = data[i:i + CLIP].decode("utf-8", "ignore")
        out.append(ch)
        i += len(ch.encode("utf-8"))
    n = len(out)
    return [(f"{num} (part {j+1}/{n}) — {(title or '')[:100]}", t) for j, t in enumerate(out)]


def genus_for(name):
    """Broaden the TERMS OF ART **in place**, keeping every structural element of the
    feature (which device, to which device, the frequency-difference condition, ...).

    BUG FIXED 2026-08-28: this used to RETURN the expansion, i.e. replace the whole
    feature text. All three t10 weight-4/5 features matched the same key, so the genus
    arm asked one trivially-true question ("is there any wireless power transfer?")
    three times and scored YES/YES/YES — a strictly weaker question, not a rewording.
    That run (ab_clean_t10_1787900330.json) is VOID as evidence."""
    # all matches are computed against the ORIGINAL text and applied right-to-left,
    # so an expansion can never be matched again by a later key (nested-insert bug)
    hits = []
    for key, expansion in GENUS.items():
        m = re.search(re.escape(key), name, re.IGNORECASE)
        if m:
            hits.append((m.end(), expansion))
    if not hits:
        return None
    out = name
    for pos, expansion in sorted(hits, reverse=True):
        out = out[:pos] + f" [read broadly: {expansion}]" + out[pos:]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", type=int, required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--minw", type=int, default=4)
    ap.add_argument("--keep-notebook", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="single-variable arm: TWO notebooks with identical sources, the SAME "
                         "neutral question in both, differing only in feature wording (verbatim "
                         "vs genus). Removes the two confounds of the first run: the round-B "
                         "prompt also said 'judge the substance', and round B saw round A's NOs "
                         "in the same chat.")
    a = ap.parse_args()
    nums = [n.strip() for n in a.docs.split(",") if n.strip()]
    cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
    prof = (cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",
                       (a.tab,)).fetchone() or ["default"])[0]
    must = (json.load(open(f"/data/.nlm_claims_{a.tab}.json")).get("must") or [])
    if not must:
        print("no MUST features", file=sys.stderr); sys.exit(3)
    refnum = re.compile(r"\s*\(\s*\d+[A-Za-z]?(?:\s*,\s*\d+[A-Za-z]?)*\s*\)")
    clean = [(refnum.sub("", n).strip(), w) for n, w in must]

    docs = []
    for n in nums:
        r = cx.execute("select number,title,abstract,claims,description,digest from documents "
                       "where tab_id=? and number=? and status='fetched'", (a.tab, n)).fetchone()
        if r: docs.append(r)
    if not docs:
        print("no fetched docs", file=sys.stderr); sys.exit(3)

    out = {"tab": a.tab, "profile": prof, "ts": int(time.time()), "docs": [d[0] for d in docs],
           "must": clean, "rounds": {}, "design": "clean-two-notebook" if a.clean else "sequential"}

    def stage(title):
        """fresh notebook with the same sources; returns (nb, parts_ok, inventory)"""
        r = nlm_bridge.create_notebook(title, profile=prof)
        n = r.get("id") or (r.get("notebook") or {}).get("id")
        if not n:
            print(f"notebook create failed: {r}", file=sys.stderr); sys.exit(3)
        po = {}
        for num, title_, ab, cl, de, dg in docs:
            want = parts(num, title_, compose(num, title_, ab, cl, de, dg))
            po[num] = {"want": len(want), "ok": 0}
            for st, tx in want:
                if nlm_bridge.add_source_text(n, st, tx, profile=prof).get("ok"):
                    po[num]["ok"] += 1
        nlm_bridge.wait_sources_ready(n, timeout=600, profile=prof)
        iv = nlm_bridge.list_sources(n, profile=prof)
        return n, po, [x.get("title") for x in (iv.get("sources") or [])]

    def ask_on(nb_id, q, tag):
        r = nlm_bridge.query(nb_id, q, profile=prof)
        if nlm_bridge.is_quota_error(r):
            out["rounds"][tag] = "QUOTA-ABORT"; return None
        out["rounds"][tag] = {"notebook": nb_id, "question": q,
                              "answer": r.get("answer") or r.get("error")}
        return out["rounds"][tag]["answer"]

    if a.clean:
        # identical prompt in both arms; ONLY the feature text differs
        heavy = [(i, n, w) for i, (n, w) in enumerate(clean, 1) if w >= a.minw]
        specs = {
            "verbatim": "\n".join(f"F{i}. {n}" for i, n, w in heavy),
            "genus": "\n".join(f"F{i}. {genus_for(n) or n}" for i, n, w in heavy),
        }
        out["heavy_features"] = [{"index": i, "text": n, "weight": w} for i, n, w in heavy]
        out["specs"] = specs
        for arm, spec in specs.items():
            nb_a, po_a, inv_a = stage(f"🔬 A/B {arm} — tab {a.tab}")
            out.setdefault("arms", {})[arm] = {"notebook": nb_a, "parts_ok": po_a,
                                               "source_inventory": inv_a}
            for num, *_ in docs:
                ask_on(nb_a, f"Document {num}. For EACH feature below state F<k>=YES, "
                             f"F<k>=PARTIAL or F<k>=NO for this document, each with a one-line "
                             f"justification citing the claim/paragraph. Do not skip a feature."
                             f"\n\n=== FEATURES ===\n{spec}", f"{arm}::{num}")
            if not a.keep_notebook:
                nlm_bridge.delete_notebook(nb_a, profile=prof)
        os.makedirs(AUD, exist_ok=True)
        path = f"{AUD}/ab_clean_t{a.tab}_{out['ts']}.json"
        with open(path, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(path); return
    res = nlm_bridge.create_notebook(f"🔬 A/B wording — tab {a.tab}", profile=prof)
    nb = res.get("id") or (res.get("notebook") or {}).get("id")
    if not nb:
        print(f"notebook create failed: {res}", file=sys.stderr); sys.exit(3)
    out["notebook"] = nb
    ing = {}
    for num, title, ab, cl, de, dg in docs:
        want = parts(num, title, compose(num, title, ab, cl, de, dg))
        ing[num] = {"want": len(want), "ok": 0}
        for st, tx in want:
            if nlm_bridge.add_source_text(nb, st, tx, profile=prof).get("ok"):
                ing[num]["ok"] += 1
    out["parts_ok"] = ing
    nlm_bridge.wait_sources_ready(nb, timeout=600, profile=prof)
    inv = nlm_bridge.list_sources(nb, profile=prof)
    out["source_inventory"] = [s.get("title") for s in (inv.get("sources") or [])]

    def ask(q, tag):
        r = nlm_bridge.query(nb, q, profile=prof)
        if nlm_bridge.is_quota_error(r):
            out["rounds"][tag] = "QUOTA-ABORT"
            return None
        out["rounds"][tag] = {"question": q, "answer": r.get("answer") or r.get("error")}
        return out["rounds"][tag]["answer"]

    specA = "\n".join(f"F{i}. {n} (importance {w}/5)" for i, (n, w) in enumerate(clean, 1))
    for num, *_ in docs:
        ansA = ask(f"Document {num}. For EACH feature below state F<k>=YES, F<k>=PARTIAL or "
                   f"F<k>=NO for this document, each with a one-line justification citing the "
                   f"claim/paragraph. Do not skip a feature.\n\n=== FEATURES ===\n{specA}",
                   f"A::{num}")
        if ansA is None: break
        # round B: only the heavy features this wording said NO to
        nos = [i for i, (n, w) in enumerate(clean, 1)
               if w >= a.minw and re.search(rf"F\s*{i}\s*\**\s*=\s*\**\s*NO", ansA, re.IGNORECASE)]
        if not nos:
            out["rounds"][f"B::{num}"] = {"skipped": "no weight>=%d NO in round A" % a.minw}
            continue
        specB = "\n".join(
            f"F{i}. {clean[i-1][0]}  ||  read this feature in its BROADEST sense: {genus_for(clean[i-1][0]) or 'any equivalent realisation, however worded'}"
            for i in nos)
        ask(f"Same document {num}. Re-examine ONLY these features, which you answered NO to. "
            f"Judge the SUBSTANCE, not the wording: if the document realises the function by any "
            f"equivalent means, that is YES. Answer F<k>=YES/PARTIAL/NO with the citation.\n\n"
            f"=== FEATURES ===\n{specB}", f"B::{num}")
        out.setdefault("round_b_targets", {})[num] = nos

    os.makedirs(AUD, exist_ok=True)
    path = f"{AUD}/ab_wording_t{a.tab}_{out['ts']}.json"
    if not a.keep_notebook:
        nlm_bridge.delete_notebook(nb, profile=prof); out["notebook_deleted"] = True
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(path)


main()
