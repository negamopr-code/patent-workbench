#!/usr/bin/env python3
"""opus-graduate-driver — standing opus-5 blind reads of NEW NLM-screen GRADUATES (user 2026-08-26 21:15:
"as new graduates appear on t10-t14, read them with opus 5, staying in the doctrine").
Runs INSIDE patent-bench:  docker cp scripts/opus-graduate-driver.py patent-bench:/data/ &&
  docker exec -d patent-bench python3 /data/opus-graduate-driver.py 10 11 12 13 14
Every POLL s, per tab: graduates = keys of the screen state's ledger (/data/.nlm_screen_{t}.json,
{doc_id: [ordinal, round]}) minus docs that already carry an opus verdict -> if the tab's claude-read
lock is free (one deep-compare per tab), POST deep-compare (reading_model opus-5, blind, skip_scored)
on up to BATCH of them. Opus reads go through the Claude bridge, never NLM quota (A2 untouched).
Token-limit hits are handled by the app's own watchdog. Log: /data/.opus_grad_driver.log
Stop: touch /data/.opus_grad_driver.stop . Dies with a patent-bench restart -> re-arm.
"""
import json, os, sqlite3, sys, time, urllib.request
TABS=[int(a) for a in sys.argv[1:]] or [10,11,12,13,14]
POLL=600; BATCH=100; TTL=1200
API="http://127.0.0.1:8000"; LOG="/data/.opus_grad_driver.log"; STOP="/data/.opus_grad_driver.stop"
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+m+"\n")
def lock_live(t):
    p=f"/data/.claude_read_{t}.lock"
    return os.path.exists(p) and time.time()-os.path.getmtime(p)<TTL
def pending(t):
    p=f"/data/.nlm_screen_{t}.json"
    if not os.path.exists(p): return []
    ids=[int(k) for k in (json.load(open(p)).get("ledger") or {})]
    if not ids: return []
    c=sqlite3.connect("/data/workbench.db")
    q=",".join("?"*len(ids))
    done=set(r[0] for r in c.execute(f"select id from documents where id in ({q}) and score_model like '%opus%' and score is not null",ids))
    ok=set(r[0] for r in c.execute(f"select id from documents where id in ({q}) and status='fetched'",ids))
    c.close()
    return [i for i in ids if i in ok and i not in done]
log(f"graduate driver armed tabs={TABS} poll={POLL}s batch={BATCH}")
while not os.path.exists(STOP):
    for t in TABS:
        try:
            st=json.loads(urllib.request.urlopen(f"{API}/api/tabs/{t}/deep-compare/status",timeout=30).read())
            if st.get("running") or lock_live(t) or os.path.exists(f"/data/.claude_read_{t}.resume.json"): continue
            ids=pending(t)
            if not ids: continue
            body=json.dumps({"reading_model":"claude-opus-5","skip_scored":True,"doc_ids":ids[:BATCH]}).encode()
            req=urllib.request.Request(f"{API}/api/tabs/{t}/deep-compare",data=body,headers={"Content-Type":"application/json"})
            r=json.loads(urllib.request.urlopen(req,timeout=60).read())
            log(f"t{t}: launched {min(len(ids),BATCH)} of {len(ids)} unread graduates -> started={r.get('started')} total={r.get('total')}")
        except Exception as e:
            log(f"t{t}: ERROR {e!r}")
    time.sleep(POLL)
log("graduate driver stopped (stop flag)")
