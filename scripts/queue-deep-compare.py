#!/usr/bin/env python3
"""queue-deep-compare — one-shot: wait until a tab's deep-compare is free, then POST a blind opus-5
read of the ids in a manifest. Runs INSIDE patent-bench:
  docker cp scripts/queue-deep-compare.py patent-bench:/data/ &&
  docker exec -d patent-bench python3 /data/queue-deep-compare.py /data/audits/<manifest>.json <label>
Manifest = {"<tab>": [doc ids], ...}. Log /data/.queue_deep_compare.log. Dies with a restart -> re-arm.
"""
import json, os, sys, time, urllib.request
MAN, LABEL = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else os.path.basename(sys.argv[1]))
API="http://127.0.0.1:8000"; LOG="/data/.queue_deep_compare.log"; TTL=1200
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+f"[{LABEL}] "+m+"\n")
todo={k:v for k,v in json.load(open(MAN)).items() if v}
log(f"armed: {{ {', '.join(f't{k}:{len(v)}' for k,v in todo.items())} }}")
while todo:
    for t in list(todo):
        try:
            st=json.loads(urllib.request.urlopen(f"{API}/api/tabs/{t}/deep-compare/status",timeout=30).read())
            lock=f"/data/.claude_read_{t}.lock"
            if st.get("running") or os.path.exists(f"/data/.claude_read_{t}.resume.json") \
               or (os.path.exists(lock) and time.time()-os.path.getmtime(lock)<TTL): continue
            body=json.dumps({"reading_model":"claude-opus-5","skip_scored":True,"doc_ids":todo[t]}).encode()
            r=json.loads(urllib.request.urlopen(urllib.request.Request(f"{API}/api/tabs/{t}/deep-compare",data=body,
                headers={"Content-Type":"application/json"}),timeout=60).read())
            log(f"t{t}: launched {len(todo[t])} -> started={r.get('started')} total={r.get('total')}"); del todo[t]
        except Exception as e:
            log(f"t{t}: ERROR {e!r}")
    time.sleep(60)
log("done")
