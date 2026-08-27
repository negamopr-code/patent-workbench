#!/usr/bin/env python3
"""restage-blind-tails — restage S1 blind-tail docs (staged truncated at 120KB and never deep-read)
in FULL via nlm_followup.py, 10 docs per dedicated follow-up notebook, in series on the tab's own
NLM account. Runs INSIDE patent-bench:
  docker cp scripts/nlm_followup.py patent-bench:/data/ && docker cp scripts/restage-blind-tails.py patent-bench:/data/ &&
  docker exec -d patent-bench python3 /data/restage-blind-tails.py <tab> /data/audits/blind_t<tab>.json
Exit 2 (quota) from nlm_followup -> sleep 1h and retry the same chunk. Log /data/.restage_t<tab>.log.
Progress /data/audits/restage_t<tab>.progress.json (done chunks) -> idempotent re-arm after a restart.
"""
import json, os, subprocess, sys, time
TAB, LIST = int(sys.argv[1]), sys.argv[2]
LOG=f"/data/.restage_t{TAB}.log"; PROG=f"/data/audits/restage_t{TAB}.progress.json"
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+m+"\n")
docs=json.load(open(LIST)); done=set(json.load(open(PROG))) if os.path.exists(PROG) else set()
todo=[d for d in docs if d not in done]
log(f"armed tab={TAB} docs={len(docs)} remaining={len(todo)}")
for i in range(0,len(todo),10):
    chunk=todo[i:i+10]
    while True:
        r=subprocess.run(["python3","/data/nlm_followup.py","--tab",str(TAB),"--docs",",".join(chunk),"--json"],capture_output=True,text=True)
        if r.returncode==2:
            log(f"chunk {i//10+1}: QUOTA -> sleep 3600"); time.sleep(3600); continue
        log(f"chunk {i//10+1}: exit={r.returncode} {(r.stderr or '')[-200:].strip()!r}")
        if r.returncode in (0,1): done.update(chunk); json.dump(sorted(done),open(PROG,"w"))
        else: time.sleep(600)
        break
log("done")
