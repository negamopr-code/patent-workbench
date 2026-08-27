#!/usr/bin/env python3
"""chain-restage — wait until an NLM account is free, then start a restage runner on it.
Enforces A2 (one NLM job per account, in series) for out-of-band restage jobs, which the app's
own scheduler knows nothing about. Runs INSIDE patent-bench:
  docker exec -d patent-bench python3 /data/chain-restage.py <tab> /data/audits/blind_t<tab>.json
Free = no restage/nlm_followup process bound to ANY tab sharing this tab's nlm_profile, and no
live app lock (screen/claims) on those tabs. Log /data/.chain_restage_t<tab>.log. Dies with a
patent-bench restart -> re-arm (the runner's progress file makes it idempotent).
"""
import json, os, sqlite3, subprocess, sys, time
TAB, LIST = int(sys.argv[1]), sys.argv[2]
LOG=f"/data/.chain_restage_t{TAB}.log"; TTL=1200
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+m+"\n")
cx=sqlite3.connect("file:/data/workbench.db?mode=ro",uri=True)
prof=lambda t:(cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",(t,)).fetchone() or ["default"])[0]
MINE=prof(TAB); SIBS=[r[0] for r in cx.execute("select id from tabs where coalesce(nlm_profile,'default')=?",(MINE,))]
def busy():
    hits=[]
    for pid in [p for p in os.listdir("/proc") if p.isdigit()]:
        try: cmd=open(f"/proc/{pid}/cmdline","rb").read().decode("utf-8","ignore").replace("\0"," ")
        except OSError: continue
        if ("restage-blind-tails.py" in cmd or "nlm_followup.py" in cmd) and pid!=str(os.getpid()):
            args=cmd.split()
            for t in SIBS:
                if str(t) in args and t!=TAB: hits.append(f"t{t}:{pid}")
                elif t==TAB and "restage-blind-tails.py" in cmd and str(t) in args: hits.append(f"t{t}:ALREADY-RUNNING:{pid}")
    for t in SIBS:
        for lock in (f"/data/.nlm_screen_{t}.lock", f"/data/.nlm_claims_{t}.lock"):
            if os.path.exists(lock) and time.time()-os.path.getmtime(lock)<TTL: hits.append(f"t{t}:{os.path.basename(lock)}")
    return hits
log(f"armed tab={TAB} profile={MINE} siblings={SIBS}")
while True:
    b=busy()
    if not b:
        p=subprocess.Popen(["python3","/data/restage-blind-tails.py",str(TAB),LIST])
        log(f"account free -> started restage pid={p.pid}"); break
    log(f"waiting: {b}"); time.sleep(300)
