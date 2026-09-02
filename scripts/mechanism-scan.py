#!/usr/bin/env python3
"""mechanism-scan — rescan a tab's REJECTED pile by asking what the invention DOES.

Proven 2026-08-31 by dose-response: a prose description of the inventive MECHANISM finds a
champion the production screen rejected, at every roster size from 10 to 30, and answers NONE
correctly when no candidate qualifies. A ranked checklist over weighted feature names does not —
that is what put KR20260033205 (opus 8.0) in t12's discard pile, and what limited the earlier
core rescue to 25%.

Roster 30 is used deliberately: roster size was FALSIFIED as the cause, so there is no reason to
pay 3x the queries for small rosters. Staging is full multi-part (truncation NO-GO) and any doc
whose tail does not land is excluded rather than asked about.

  docker exec -d patent-bench python3 /data/mechanism-scan.py <tab> [--roster 30] [--only-file f]
Progress /data/audits/mech_t<tab>.progress.json · picks /data/audits/mech_t<tab>.picks.json
Evidence /data/audits/mechanism/ · log /data/.mech_t<tab>.log
"""
import argparse, json, os, re, sqlite3, sys, time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("tab", type=int)
ap.add_argument("--roster", type=int, default=30)
ap.add_argument("--only-file", default=None)
ap.add_argument("--tag", default="")
a = ap.parse_args()
TAB, TAG = a.tab, ("_" + a.tag if a.tag else "")
AUD, EV = "/data/audits", "/data/audits/mechanism"
LOG = f"/data/.mech_t{TAB}{TAG}.log"
PROG = f"{AUD}/mech_t{TAB}{TAG}.progress.json"
PICKS = f"{AUD}/mech_t{TAB}{TAG}.picks.json"
CLIP = 118_000
HB = "/home/app/.notebooklm-mcp-cli/heartbeats"
os.makedirs(EV, exist_ok=True)


def log(m):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + m + "\n")


cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
PROFILE = (cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",
                      (TAB,)).fetchone() or ["default"])[0]
spec = json.load(open("/data/mechanism-questions.json")).get(str(TAB))
if not spec:
    log("no mechanism question for this tab"); sys.exit(3)
QUESTION = spec["question"]


def heartbeat(state, summary, **counts):
    try:
        os.makedirs(HB, exist_ok=True)
        tmp = f"{HB}/.patent-mech-t{TAB}{TAG}.tmp"
        with open(tmp, "w") as f:
            json.dump({"job": f"patent-bench mechanism scan — tab {TAB}", "account": PROFILE,
                       "state": state, "summary": summary, "counts": counts,
                       "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}, f)
        os.replace(tmp, f"{HB}/patent-mech-t{TAB}{TAG}.json")
    except Exception:
        pass


rows = cx.execute("""select number,title,abstract,claims,description,digest from documents
                     where tab_id=? and status='fetched' and nlm_screen_state='rejected'
                     order by number""", (TAB,)).fetchall()
if a.only_file:
    keep = set(json.load(open(a.only_file)))
    rows = [r for r in rows if r[0] in keep]
done = set(json.load(open(PROG))) if os.path.exists(PROG) else set()
picks = json.load(open(PICKS)) if os.path.exists(PICKS) else []
todo = [r for r in rows if r[0] not in done]
log(f"armed tab={TAB} profile={PROFILE} roster={a.roster} pile={len(rows)} todo={len(todo)}")
log(f"  mechanism: {spec['label']}")
heartbeat("running", f"mechanism rescan armed — {len(todo)} of {len(rows)} rejected docs",
          pile=len(rows), asked=len(done), picks=len(picks))


def parts_of(num, ti, blob):
    data = blob.encode("utf-8")
    if len(data) <= CLIP:
        return [(f"{num} — {(ti or '')[:120]}", blob)]
    out, i = [], 0
    while i < len(data):
        ch = data[i:i + CLIP].decode("utf-8", "ignore")
        out.append(ch); i += len(ch.encode("utf-8"))
    return [(f"{num} (part {j+1}/{len(out)}) — {(ti or '')[:100]}", t) for j, t in enumerate(out)]


# Consecutive empty answers mean the ACCOUNT is out of quota, not that these particular
# documents are unanswerable. work2 signals exhaustion with an empty reply and no explicit
# marker (2026-09-01), so without this the lane stages 30 docs per chunk and grinds through the
# whole pile producing nothing. Back off exactly as for an explicit quota wall.
empty_streak = 0
for i in range(0, len(todo), a.roster):
    if empty_streak >= 2:
        heartbeat("quota_exhausted", f"{PROFILE} returning empty answers — backing off hourly; "
                  f"{len(picks)} picks of {len(done)} asked",
                  pile=len(rows), asked=len(done), picks=len(picks))
        log(f"{empty_streak} consecutive empty answers -> treating as quota, sleep 3600")
        time.sleep(3600)
        empty_streak = 0
    chunk = todo[i:i + a.roster]
    res = nlm_bridge.create_notebook(f"🔎 mechanism — tab {TAB}", profile=PROFILE)
    nb = res.get("id") or (res.get("notebook") or {}).get("id")
    if not nb:
        if "100 notebooks" in str(res):
            log("NOTEBOOK-CAP -> sleep 1800"); time.sleep(1800); continue
        log(f"notebook create failed: {res}"); break
    ok_docs = []
    for num, ti, ab, cl, de, dg in chunk:
        blob = "\n\n".join(filter(None, [f"{num} — {ti or ''}",
                                         ("ABSTRACT:\n" + ab) if ab else None,
                                         ("CLAIMS:\n" + cl) if cl else None,
                                         ("DESCRIPTION:\n" + de) if de else None,
                                         ("FULL-TEXT DIGEST:\n" + dg) if dg else None]))
        want = parts_of(num, ti, blob)
        got = 0
        for t, x in want:
            r = nlm_bridge.add_source_text(nb, t, x, profile=PROFILE)
            if not r.get("ok"):
                r = nlm_bridge.add_source_text(nb, t, x, profile=PROFILE)
            got += 1 if r.get("ok") else 0
        if got == len(want):
            ok_docs.append(num)
        else:
            log(f"  {num}: {len(want)-got} part(s) rejected — excluded, not asked")
    nlm_bridge.wait_sources_ready(nb, timeout=600, profile=PROFILE)
    # Retry an EMPTY answer instead of walking past it. t12's first run burned 27 chunks on
    # empty replies and credited nothing (2026-09-01) — the bridge sets quota_suspect on any
    # empty answer, so an un-ingested notebook and a real quota wall look identical. Retry the
    # empty; back off only on the explicit marker.
    r = None
    for attempt in range(1, 4):
        r = nlm_bridge.query(nb, QUESTION, profile=PROFILE)
        if r.get("quota") or not r.get("quota_suspect"):
            break
        log(f"  empty answer (attempt {attempt}/3) — waiting for ingestion, retrying")
        nlm_bridge.wait_sources_ready(nb, timeout=180, profile=PROFILE)
        time.sleep(20 * attempt)
    if r.get("quota"):
        try: nlm_bridge.delete_notebook(nb, profile=PROFILE)
        except Exception: pass
        heartbeat("quota_exhausted", f"{PROFILE} out of quota — retrying hourly",
                  pile=len(rows), asked=len(done), picks=len(picks))
        log("QUOTA (explicit) -> sleep 3600"); time.sleep(3600); continue
    ans = (r.get("answer") or r.get("error") or "").strip()
    ts = int(time.time())
    json.dump({"tab": TAB, "ts": ts, "notebook": nb, "mechanism": spec["label"],
               "question": QUESTION, "asked": ok_docs, "answer": ans},
              open(f"{EV}/t{TAB}{TAG}_{ts}.json", "w"), indent=1)
    if not ans or r.get("quota_suspect"):
        empty_streak += 1
        log(f"chunk {i//a.roster+1}: empty answer (streak {empty_streak}) — NOT credited, "
            f"retries on re-arm")
    else:
        hit = [n for n in ok_docs if re.search(re.escape(n), ans, re.IGNORECASE)]
        for n in hit:
            picks.append({"number": n, "answer": ans[:400], "ts": ts})
        done |= set(ok_docs)
        json.dump(sorted(done), open(PROG, "w"))
        json.dump(picks, open(PICKS, "w"), indent=1)
        empty_streak = 0
        log(f"chunk {i//a.roster+1}: asked {len(ok_docs)} -> "
            f"{('PICK ' + ', '.join(hit)) if hit else 'NONE'} | total picks {len(picks)}/{len(done)}")
        heartbeat("running", f"asked {len(done)}/{len(rows)}, {len(picks)} picked",
                  pile=len(rows), asked=len(done), picks=len(picks))
    try: nlm_bridge.delete_notebook(nb, profile=PROFILE)
    except Exception: pass
heartbeat("done", f"finished — {len(picks)} picks from {len(done)} asked",
          pile=len(rows), asked=len(done), picks=len(picks))
log(f"done — {len(picks)} picks of {len(done)} asked")
