#!/usr/bin/env python3
"""roster-doseresponse — find the roster size at which NotebookLM stops seeing a known champion.

User design, 2026-08-31: the same question, the same champion, progressively larger rosters.
If the attention-budget hypothesis is right (failure class F3a: roster-35 -> 0/9 vs roster-10 ->
7/9), the champion is found at small N and lost somewhere above it. That crossing point is the
operating parameter for every lane in the project — currently guessed, never measured on a real
champion.

Champion KR20260033205 (t12, opus 8.0) is found at roster 10 by all three framings; the screen
rejected it at roster 20-39. This measures where in between it breaks.

  docker exec -d patent-bench python3 /data/roster-doseresponse.py --tab 12 --sizes 10,15,20,25,30
"""
import argparse, json, re, sqlite3, sys, time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tab", type=int, default=12)
ap.add_argument("--target", default="KR20260033205")
ap.add_argument("--sizes", default="10,15,20,25,30")
a = ap.parse_args()
SIZES = [int(x) for x in a.sizes.split(",")]
CLIP = 118_000
LOG = f"/data/.roster_dose_t{a.tab}.log"
OUT = f"/data/audits/roster_dose_t{a.tab}_{int(time.time())}.json"


def log(m):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + m + "\n")


cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
prof = (cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",
                   (a.tab,)).fetchone() or ["default"])[0]

# distractor pool: same character as the champion (charge/discharge + cooling) but opus-low,
# so every roster size is padded with GENUINELY similar documents, not easy filler
pool = [r[0] for r in cx.execute("""select number from documents where tab_id=? and status='fetched'
    and score is not null and score<=3 and number<>? order by score desc, number""",
    (a.tab, a.target))]
QUESTION = (
    "All the attached candidates are battery charging/discharging equipment with cooling. At most "
    "ONE of them takes air that has ALREADY been chilled inside its water-cooled power-supply "
    "enclosure and redirects that air to the charging jig, so that the enclosure is kept free of "
    "CONDENSATION and the jig is cooled as a by-product. The others cool by some different means.\n"
    "Identify it. If no attached document does this, answer exactly NONE.\n"
    "Reply on one line: PICK: <publication number or NONE> — <one line why>")


def stage(nb, num):
    r = cx.execute("""select number,title,abstract,claims,description,digest from documents
                      where tab_id=? and number=?""", (a.tab, num)).fetchone()
    if not r:
        return False
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
    return all(nlm_bridge.add_source_text(nb, t, x, profile=prof).get("ok") for t, x in parts)


results = {}
for n in SIZES:
    roster = [a.target] + pool[:n-1]           # champion always present
    res = nlm_bridge.create_notebook(f"🧪 roster {n} — tab {a.tab}", profile=prof)
    nb = res.get("id") or (res.get("notebook") or {}).get("id")
    if not nb:
        log(f"roster {n}: notebook create failed {res}"); continue
    ok = sum(1 for d in roster if stage(nb, d))
    nlm_bridge.wait_sources_ready(nb, timeout=600, profile=prof)
    r = nlm_bridge.query(nb, QUESTION, profile=prof)
    ans = (r.get("answer") or r.get("error") or "").strip()
    found = bool(re.search(re.escape(a.target), ans, re.IGNORECASE))
    results[n] = {"staged": ok, "found": found, "answer": ans}
    log(f"roster {n:>3}: staged {ok}/{n}  target {'FOUND' if found else 'LOST '}  | "
        f"{ans.splitlines()[0][:96] if ans else '(empty)'}")
    nlm_bridge.delete_notebook(nb, profile=prof)
json.dump({"tab": a.tab, "target": a.target, "question": QUESTION, "results": results},
          open(OUT, "w"), indent=1)
log(f"done -> {OUT}")
