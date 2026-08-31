#!/usr/bin/env python3
"""core-probe — find the question that makes NotebookLM recognise a known champion.

Controlled probe with a KNOWN answer (user design, 2026-08-31). Stage N hard distractors plus
one document opus scored highly, then ask the same corpus several differently-framed questions
and see which framing surfaces the champion. Finally ask NLM, per document, for a ONE-LINE
reason it was not chosen — the rejection reasons are the diagnostic, not the pick.

Why t12: it is the tab where NLM's own per-document rating has ZERO correlation with opus
(champions average 3.40, junk 3.21) and where the screen rejected KR20260033205 at opus 8.0.
Its corpus is homogeneous — every document is battery charge/discharge equipment with cooling —
so comparison-based instruments have nothing to separate on.

The framings test one hypothesis each:
  COMPONENT  the component list ("a fan inside a water-cooled power supply")  <- what we asked before
  PURPOSE    the purpose relationship ("cold air reused to PREVENT CONDENSATION")
  CONTRAST   asks NLM to distinguish, not to match: what makes ONE of these different

  docker exec -d patent-bench python3 /data/core-probe.py --docs-file /data/audits/probe_t12_core.json --tab 12
"""
import argparse, json, os, re, sqlite3, sys, time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tab", type=int, default=12)
ap.add_argument("--docs-file", required=True)
ap.add_argument("--target", default="KR20260033205")
a = ap.parse_args()
CLIP = 118_000
OUT = f"/data/audits/core_probe_t{a.tab}_{int(time.time())}.json"
LOG = f"/data/.core_probe_t{a.tab}.log"


def log(m):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + m + "\n")


cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
prof = (cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",
                   (a.tab,)).fetchone() or ["default"])[0]
nums = json.load(open(a.docs_file))

FRAMINGS = {
 "COMPONENT": (
   "Which of the attached candidate documents disclose a charging and discharging apparatus in "
   "which a FAN is located INSIDE a WATER-COOLED POWER SUPPLY PART, and blows air onto the "
   "charging/discharging jig?\n"
   "Reply with exactly one line: PICK: <publication numbers, or NONE>"),
 "PURPOSE": (
   "One of the attached candidate documents solves this specific problem: inside a water-cooled "
   "power supply of a battery charger/discharger, cold surfaces cause CONDENSATION, which damages "
   "the electronics. Its solution REUSES the already-chilled air from inside that power supply — "
   "blowing it out to the charging jig — so that the power supply interior stays dry AND the jig "
   "gets cooled at the same time. The point is the PURPOSE: preventing condensation, with jig "
   "cooling as the by-product.\n"
   "Which candidate document is that? Reply with exactly one line: PICK: <publication number>"),
 "CONTRAST": (
   "All the attached candidates are battery charging/discharging equipment with cooling. Exactly "
   "ONE of them differs from the others in HOW it uses the cooling air: it takes air that has "
   "already been chilled inside the power-supply enclosure and redirects it, rather than simply "
   "blowing ambient or externally-cooled air. Identify that one and say in one line what makes it "
   "different from the rest.\n"
   "Reply: PICK: <publication number> — <one line>"),
}

def fresh_notebook(label):
    """A NEW notebook per question.

    CONFOUND FIXED 2026-08-31 (caught by the user): the first version asked all framings in ONE
    notebook, and NotebookLM keeps chat history within a notebook — so every question after the
    first could see that an earlier one had already named the target. Only the FIRST answer of
    such a run is independent evidence. That is failure class F4 (rolling-notebook confound)
    reappearing inside a control experiment. Each framing now gets its own notebook and its own
    staging, so the answers are independent at the cost of re-staging.
    """
    res = nlm_bridge.create_notebook(f"🧪 {label} — tab {a.tab}", profile=prof)
    nb = res.get("id") or (res.get("notebook") or {}).get("id")
    if not nb:
        log(f"notebook create failed: {res}"); sys.exit(3)
    return nb


def stage_all(nb):
    staged = []
    for num in nums:
        r = cx.execute("""select number,title,abstract,claims,description,digest from documents
                          where tab_id=? and number=?""", (a.tab, num)).fetchone()
        if not r:
            continue
        blob = "\n\n".join(filter(None, [f"{r[0]} — {r[1] or ''}",
                                         ("ABSTRACT:\n" + r[2]) if r[2] else None,
                                         ("CLAIMS:\n" + r[3]) if r[3] else None,
                                         ("DESCRIPTION:\n" + r[4]) if r[4] else None,
                                         ("FULL-TEXT DIGEST:\n" + r[5]) if r[5] else None]))
        data = blob.encode("utf-8")
        parts = ([(f"{num} — {(r[1] or '')[:120]}", blob)] if len(data) <= CLIP else
                 [(f"{num} (part {i+1}) — {(r[1] or '')[:100]}",
                   data[i*CLIP:(i+1)*CLIP].decode("utf-8", "ignore"))
                  for i in range((len(data)+CLIP-1)//CLIP)])
        if all(nlm_bridge.add_source_text(nb, t, x, profile=prof).get("ok") for t, x in parts):
            staged.append(num)
    nlm_bridge.wait_sources_ready(nb, timeout=600, profile=prof)
    return staged


out = {"tab": a.tab, "target": a.target, "independent_notebooks": True, "answers": {}}
for name, q in FRAMINGS.items():
    nb = fresh_notebook(name)
    staged = stage_all(nb)
    r = nlm_bridge.query(nb, q, profile=prof)
    ans = r.get("answer") or r.get("error") or ""
    out["answers"][name] = ans
    out.setdefault("staged", {})[name] = len(staged)
    hit = bool(re.search(re.escape(a.target), ans, re.IGNORECASE))
    log(f"{name} [own notebook, {len(staged)} docs]: target "
        f"{'FOUND' if hit else 'MISSED'} | {ans[:100].splitlines()[0] if ans else '(empty)'}")
    nlm_bridge.delete_notebook(nb, profile=prof)

# the diagnostic: why was each document NOT the answer?
nb = fresh_notebook("REASONS")
stage_all(nb)
q = ("For EACH attached candidate document, give ONE line stating why it is NOT a charger/"
     "discharger that reuses already-chilled air from inside its water-cooled power supply to "
     "prevent condensation there. If a document DOES do that, say so explicitly instead.\n"
     "Reply one line per document: <publication number>: <reason>")
r = nlm_bridge.query(nb, q, profile=prof)
out["answers"]["REASONS"] = r.get("answer") or r.get("error") or ""
log("reasons collected")
json.dump(out, open(OUT, "w"), indent=1)
nlm_bridge.delete_notebook(nb, profile=prof)
log(f"done -> {OUT}")
