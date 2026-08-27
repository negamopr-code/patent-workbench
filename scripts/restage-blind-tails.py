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
import json, os, re, sqlite3, subprocess, sys, time
TAB, LIST = int(sys.argv[1]), sys.argv[2]
LOG=f"/data/.restage_t{TAB}.log"; PROG=f"/data/audits/restage_t{TAB}.progress.json"
EV="/data/audits/restage"; LEDGER="/data/audits/restage_ledger.jsonl"
os.makedirs(EV, exist_ok=True)
# Heartbeat for the NLM Slot Manager. This runner is an OUT-OF-BAND NLM job: the
# slot manager only polls patent-bench's app job routes (claims-audit, nlm-screen,
# pipeline, cross-tab-scan), so without this the card says "quota idle" while we
# are burning that account's Q&A quota (observed 2026-08-27). The heartbeats dir
# in the nlm-profile volume is read by the slot manager alongside its bind store.
HB_DIR="/home/app/.notebooklm-mcp-cli/heartbeats"
def heartbeat(state, summary, **counts):
    try:
        os.makedirs(HB_DIR, exist_ok=True)
        tmp=f"{HB_DIR}/.patent-restage-t{TAB}.tmp"
        with open(tmp,"w") as f:
            json.dump({"job":f"patent-bench restage blind tails — tab {TAB}",
                       "account":PROFILE,"state":state,"summary":summary,
                       "counts":counts,
                       "updatedAt":time.strftime("%Y-%m-%dT%H:%M:%S.000Z",time.gmtime())},f)
        os.replace(tmp,f"{HB_DIR}/patent-restage-t{TAB}.json")
    except Exception:
        pass   # a heartbeat must never break the job
def log(m):
    with open(LOG,"a") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ",time.gmtime())+m+"\n")
try:
    _cx=sqlite3.connect("file:/data/workbench.db?mode=ro",uri=True)
    PROFILE=(_cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",(TAB,)).fetchone() or ["default"])[0]
except Exception:
    PROFILE="default"
docs=json.load(open(LIST)); done=set(json.load(open(PROG))) if os.path.exists(PROG) else set()
todo=[d for d in docs if d not in done]
log(f"armed tab={TAB} docs={len(docs)} remaining={len(todo)}")
heartbeat("running", f"armed — {len(todo)} of {len(docs)} blind-tail docs left",
          docs=len(docs), remaining=len(todo), credited=len(done))
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
                _broad=((res.get("answers") or {}).get("_broad") or "")
                broad_ok=(not res.get("broad_failed")) and bool(_broad.strip()) \
                         and '"status": "error"' not in _broad
                _cons=((res.get("answers") or {}).get("_consolidated") or "")
                # a reply that rebuilds its own checklist answered a different question
                if ("not explicitly provided" in _cons) or ("reconstructed checklist" in _cons):
                    broad_ok=False
                    log(f"chunk {i//10+1}: REJECTED credit — reply used an invented checklist")
                inv=set(res.get("source_inventory") or [])
                def _addressed(n, txt):
                    # the doc must carry its OWN grid, not merely be name-dropped in
                    # another document's justification (auditor 2026-08-27)
                    # real shape: "**NUM**: **F1=NO** **F2=NO** ..." — allow markdown
                    # and any leading feature index, but the grid must follow the
                    # number within a short window, not merely mention it
                    m = re.search(rf"\**{re.escape(n)}\**\s*[:\-—]", txt or "", re.IGNORECASE)
                    if not m:
                        return False
                    return bool(re.search(r"\**F\s*\d+\s*\**\s*=\s*\**\s*(YES|NO|PARTIAL)",
                                          (txt or "")[m.end():m.end() + 200], re.IGNORECASE))
                def _full(n):
                    p=po.get(n) or {}
                    if not p.get("want") or p.get("ok")!=p.get("want"): return False
                    if inv:   # every part title of this doc must be in the live inventory
                        hits=[t for t in inv if t and t.startswith(n)]
                        if len(hits)<p["want"]: return False
                    return True
                answered={k:v for k,v in (res.get("answers") or {}).items()
                          if broad_ok and k not in ("_broad","_consolidated")
                          and v and v!="QUOTA-ABORT" and _full(k) and _addressed(k, v)}
                with open(LEDGER,"a") as f:
                    for num in chunk:
                        f.write(json.dumps({"tab":TAB,"number":num,"ts":ts,
                                            "notebook":res.get("notebook"),"mode":"compact",
                                            "answered":num in answered,
                                            "parts_ok":(res.get("parts_ok") or {}).get(num),
                                            "inventory_seen":bool(res.get("source_inventory")),
                                            "evidence":f"{EV}/t{TAB}_{ts}.json"})+"\n")
        if r.returncode==2:
            log(f"chunk {i//10+1}: QUOTA -> sleep 3600")
            heartbeat("quota_exhausted",
                      f"account {PROFILE} out of NLM Q&A quota (empty-answer symptom) — retrying hourly; "
                      f"{len(done)}/{len(docs)} credited",
                      docs=len(docs), credited=len(done), chunk=i//10+1)
            time.sleep(3600); continue
        if r.returncode==3 and "100 notebooks" in (r.stderr or "")+(r.stdout or ""):
            # account at NotebookLM's ~100-notebook cap: RETRY the same chunk later,
            # never skip it (skipping silently dropped all of t11/t13 on 2026-08-27)
            log(f"chunk {i//10+1}: NOTEBOOK-CAP -> sleep 1800, retry same chunk")
            heartbeat("blocked", f"account {PROFILE} at NotebookLM's ~100-notebook cap — retrying every 30 min",
                      docs=len(docs), credited=len(done), chunk=i//10+1)
            time.sleep(1800); continue
        log(f"chunk {i//10+1}: exit={r.returncode} {(r.stderr or '')[-200:].strip()!r}")
        if r.returncode in (0,1):
            # only docs with real, credited evidence count as done — a QUOTA-ABORT
            # or a partially-ingested doc must come back on the next pass
            ok=[n for n in chunk if res and n in answered] if res else []
            done.update(ok); json.dump(sorted(done),open(PROG,"w"))
            log(f"chunk {i//10+1}: persisted {len(ok)}/{len(chunk)} answers")
            heartbeat("running", f"chunk {i//10+1} done — {len(done)}/{len(docs)} credited (account {PROFILE})",
                      docs=len(docs), credited=len(done), chunk=i//10+1)
        else: time.sleep(600)
        break
heartbeat("done", f"finished — {len(done)}/{len(docs)} blind-tail docs restaged in full and credited",
          docs=len(docs), credited=len(done))
log("done")
