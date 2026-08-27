#!/usr/bin/env python3
"""restage-blind-tails — restage S1 blind-tail docs (staged truncated at 120KB and never deep-read)
in FULL via nlm_followup.py, 10 docs per dedicated follow-up notebook, in series on the tab's own
NLM account. Runs INSIDE patent-bench:
  docker cp scripts/nlm_followup.py patent-bench:/data/ && docker cp scripts/restage-blind-tails.py patent-bench:/data/ &&
  docker exec -d patent-bench python3 /data/restage-blind-tails.py <tab> /data/audits/blind_t<tab>.json
Exit 2 (quota) from nlm_followup -> sleep 1h and retry the same chunk. Log /data/.restage_t<tab>.log.
Progress /data/audits/restage_t<tab>.progress.json (done docs) -> idempotent re-arm after a restart.

EVIDENCE (fixed 2026-08-27 after the supervisor caught it): nlm_followup prints the per-doc
YES/PARTIAL/NO answers to stdout ONLY and this runner used to discard them. It now persists the
full result JSON to /data/audits/restage/t<tab>_<ts>.json (that file IS the evidence), and
appends one restage_ledger.jsonl row per doc {tab, number, ts, notebook, mode, answered}.
The notebook is DELETED after each chunk (nlm_followup default): NotebookLM caps an account at
~100 notebooks and the default account is already at the cap, so keeping them stalls the run
(hit 2026-08-27 06:25). --compact keeps it to 2 NLM queries per 10-doc chunk (quota doctrine).
audit_staging S1 credits a doc as staged-in-full when that row exists (--restage-ledger).
"""
import json, os, subprocess, sys, time
TAB, LIST = int(sys.argv[1]), sys.argv[2]
LOG=f"/data/.restage_t{TAB}.log"; PROG=f"/data/audits/restage_t{TAB}.progress.json"
EV="/data/audits/restage"; LEDGER="/data/audits/restage_ledger.jsonl"
os.makedirs(EV, exist_ok=True)
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+m+"\n")
docs=json.load(open(LIST)); done=set(json.load(open(PROG))) if os.path.exists(PROG) else set()
todo=[d for d in docs if d not in done]
log(f"armed tab={TAB} docs={len(docs)} remaining={len(todo)}")
for i in range(0,len(todo),10):
    chunk=todo[i:i+10]
    while True:
        r=subprocess.run(["python3","/data/nlm_followup.py","--tab",str(TAB),"--docs",",".join(chunk),
                          "--json","--compact"],capture_output=True,text=True)
        res=None
        if r.stdout.strip():
            try: res=json.loads(r.stdout)
            except Exception as e: log(f"chunk {i//10+1}: stdout not JSON ({e}) -> raw kept")
            ts=int(time.time())
            with open(f"{EV}/t{TAB}_{ts}.json","w") as f: f.write(r.stdout)
            if res:
                # credit requires BOTH: every part of the doc ingested (F3c) and the
                # reply actually addressing that doc. QUOTA-ABORT is never credit.
                po=res.get("parts_ok") or {}
                inv=set(res.get("source_inventory") or [])
                def _full(n):
                    p=po.get(n) or {}
                    if not p.get("want") or p.get("ok")!=p.get("want"): return False
                    if inv:   # every part title of this doc must be in the live inventory
                        hits=[t for t in inv if t and t.startswith(n)]
                        if len(hits)<p["want"]: return False
                    return True
                answered={k:v for k,v in (res.get("answers") or {}).items()
                          if k not in ("_broad","_consolidated") and v and v!="QUOTA-ABORT"
                          and _full(k)}
                with open(LEDGER,"a") as f:
                    for num in chunk:
                        f.write(json.dumps({"tab":TAB,"number":num,"ts":ts,
                                            "notebook":res.get("notebook"),"mode":"compact",
                                            "answered":num in answered,
                                            "parts_ok":(res.get("parts_ok") or {}).get(num),
                                            "inventory_seen":bool(res.get("source_inventory")),
                                            "evidence":f"{EV}/t{TAB}_{ts}.json"})+"\n")
        if r.returncode==2:
            log(f"chunk {i//10+1}: QUOTA -> sleep 3600"); time.sleep(3600); continue
        log(f"chunk {i//10+1}: exit={r.returncode} {(r.stderr or '')[-200:].strip()!r}")
        if r.returncode in (0,1):
            # only docs with real, credited evidence count as done — a QUOTA-ABORT
            # or a partially-ingested doc must come back on the next pass
            ok=[n for n in chunk if res and n in answered] if res else []
            done.update(ok); json.dump(sorted(done),open(PROG,"w"))
            log(f"chunk {i//10+1}: persisted {len(ok)}/{len(chunk)} answers")
        else: time.sleep(600)
        break
log("done")
