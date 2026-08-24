#!/usr/bin/env python3
"""opus-coverage-driver — full blind opus-5 coverage of everything the NLM v2 screens process.
Runs INSIDE patent-bench:  docker cp scripts/opus-coverage-driver.py patent-bench:/data/ &&
  docker exec -d patent-bench python3 /data/opus-coverage-driver.py 10 12 14 11 13
Every POLL s, per tab: screened docs (queue[:cursor]) minus live survivors minus docs already
opus-read → if the tab's claude-read lock is free, POST deep-compare (reading_model opus-5,
blind) on up to BATCH of them. Log: /data/.opus_driver.log. Stop: touch /data/.opus_driver.stop
"""
import json, os, sqlite3, sys, time, urllib.request
TABS=[int(a) for a in sys.argv[1:]] or [10,12,14]
POLL=600; BATCH=150; TTL=1200
API="http://127.0.0.1:8000"; LOG="/data/.opus_driver.log"; STOP="/data/.opus_driver.stop"
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+m+"\n")
def lock_live(t):
    p=f"/data/.claude_read_{t}.lock"
    return os.path.exists(p) and time.time()-os.path.getmtime(p)<TTL
def todo(t):
    p=f"/data/.nlm_screen_{t}.json"
    if not os.path.exists(p): return []
    d=json.load(open(p)); q=d.get("queue") or []; cur=int(d.get("cursor") or 0)
    surv=set(int(x) for x in d.get("survivors") or [])
    ids=[int(x) for x in q[:cur] if int(x) not in surv]
    if not ids: return []
    c=sqlite3.connect("/data/workbench.db")
    done=set(r[0] for r in c.execute(
        f"select id from documents where id in ({','.join('?'*len(ids))}) and score_model like '%opus%' and score is not null",ids))
    c.close()
    return [i for i in ids if i not in done]
log(f"driver armed tabs={TABS} poll={POLL}s batch={BATCH}")
while not os.path.exists(STOP):
    for t in TABS:
        try:
            if lock_live(t): continue
            ids=todo(t)
            if not ids: continue
            body=json.dumps({"reading_model":"claude-opus-5","model":"claude-opus-5","skip_scored":False,"doc_ids":ids[:BATCH]}).encode()
            req=urllib.request.Request(f"{API}/api/tabs/{t}/deep-compare",data=body,headers={"Content-Type":"application/json"})
            r=json.loads(urllib.request.urlopen(req,timeout=60).read())
            log(f"t{t}: launched {min(len(ids),BATCH)} of {len(ids)} pending → {r.get('started')} running={r.get('running')}")
        except Exception as e:
            log(f"t{t}: ERROR {e!r}")
    time.sleep(POLL)
log("driver stopped (stop flag)")
