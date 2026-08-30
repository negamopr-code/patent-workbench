#!/usr/bin/env python3
"""core-rescue — the "don't discard" checkpoint (user directive, 2026-08-30).

Runs over a tab's REJECTED pile, which carries no feature grid, and asks NotebookLM ONE
question: does this document disclose the CORE OF INVENTION? Anything that does is pulled back
into a keep-alive basket for the slow lane. It never rejects and never scores — its only
output is a rescue list.

Why it is cheap: the core is 2-3 features, not the benchmark's 11-27, so the question is ~500
chars instead of ~2200 and a full roster fits per query. It is a screen-speed pass.

Wording is BROADENED here on purpose, and ONLY here. Measured 2026-08-30: in three of four tabs
the LITERAL inventive step is unsatisfiable by the corpus (t10's "different frequencies" rescues
3/5; t14's inflection-angle-vs-reference is owned by 2 docs of 489 and zero champions), so the
workable core is its immediate genus. A false positive costs one slow-lane read; a false
negative loses the document forever — over-crediting is the cheap error in a rescue lane and
the expensive one in a verdict lane, which is why verbatim stays everywhere a verdict is formed.

Staging is FULL multi-part (truncation NO-GO), so a roster shrinks below the nominal size on
oversized docs exactly as the screen's does.

  docker exec -d patent-bench python3 /data/core-rescue.py <tab> [--roster 24] [--limit N]
Progress /data/audits/core_rescue_t<tab>.progress.json · evidence /data/audits/core_rescue/
Log /data/.core_rescue_t<tab>.log
"""
import argparse, json, os, re, sqlite3, sys, time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

AUD, EV = "/data/audits", "/data/audits/core_rescue"
CANDIDATES = "/data/core-of-invention-candidates.json"
GENUS_MAPS = "/data/genus_maps.json"
CLIP = 118_000
REFNUM = re.compile(r"\s*\(\s*\d+[A-Za-z]?(?:\s*,\s*\d+[A-Za-z]?)*\s*\)")

ap = argparse.ArgumentParser()
ap.add_argument("tab", type=int)
ap.add_argument("--roster", type=int, default=24)
ap.add_argument("--limit", type=int, default=0, help="stop after N docs (validation runs)")
ap.add_argument("--only-file", default=None,
                help="restrict to the publication numbers in this JSON list — controlled re-runs")
ap.add_argument("--tag", default="", help="namespace progress/evidence so an experiment does "
                                          "not pollute the real run")
ap.add_argument("--keep-notebook", action="store_true")
ap.add_argument("--per-feature", action="store_true",
                help="ask ONE question per core member and intersect locally, instead of asking "
                     "NotebookLM to evaluate the conjunction. NLM's DIRECTIONAL agreement with "
                     "opus is 86% but its exact-grade agreement is 39%; a two-feature "
                     "conjunction compounds the directional error to ~74%, which is what a "
                     "1-of-2 recall looks like. Asking each feature separately keeps NLM on the "
                     "question it is reliable at and does the AND in code. Costs one extra query "
                     "per core member per chunk.")
a = ap.parse_args()
TAB = a.tab
TAG = ("_" + a.tag) if a.tag else ""
LOG = f"/data/.core_rescue_t{TAB}{TAG}.log"
PROG = f"{AUD}/core_rescue_t{TAB}{TAG}.progress.json"
os.makedirs(EV, exist_ok=True)


def log(m):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + m + "\n")


# Heartbeat for the NLM Slot Manager. Without this the account card reads "quota idle" while an
# out-of-band job burns that account's quota — the exact defect fixed for the restage runner in
# 591c036 (2026-08-27) and re-introduced here by writing a new runner from scratch (caught by
# the user, 2026-08-30). Any new lane MUST publish one.
HB_DIR = "/home/app/.notebooklm-mcp-cli/heartbeats"


def heartbeat(state, summary, **counts):
    try:
        os.makedirs(HB_DIR, exist_ok=True)
        tmp = f"{HB_DIR}/.patent-core-rescue-t{TAB}{TAG}.tmp"
        with open(tmp, "w") as f:
            json.dump({"job": f"patent-bench core rescue — tab {TAB}", "account": PROFILE,
                       "state": state, "summary": summary, "counts": counts,
                       "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}, f)
        os.replace(tmp, f"{HB_DIR}/patent-core-rescue-t{TAB}{TAG}.json")
    except Exception:
        pass                      # a heartbeat must never break the job


cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
PROFILE = (cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",
                      (TAB,)).fetchone() or ["default"])[0]

cands = json.load(open(CANDIDATES)).get(str(TAB)) or []
cands = [c for c in cands if c.get("recommended")] or cands
if not cands:
    log("no core candidates for this tab"); sys.exit(3)
genus = (json.load(open(GENUS_MAPS)) or {}).get(str(TAB), {})


def broaden(name):
    """Genus expansion IN PLACE — right-to-left against the ORIGINAL text so an expansion is
    never re-matched by a later key."""
    clean = re.sub(r"[ ]+", " ", REFNUM.sub("", name)).strip()
    hits = []
    for key, exp in genus.items():
        m = re.search(re.escape(key), clean, re.IGNORECASE)
        if m:
            hits.append((m.end(), exp))
    for pos, exp in sorted(hits, reverse=True):
        clean = clean[:pos] + f" [read broadly: {exp}]" + clean[pos:]
    return clean


def core_question():
    blocks = []
    for i, c in enumerate(cands, 1):
        members = "\n".join(f"   - {broaden(n)}" for n in c["features"])
        blocks.append(f"CORE {i} ({c.get('label','core')}) — a document qualifies only if it "
                      f"discloses ALL of:\n{members}")
    return ("For EACH candidate source document below, decide whether it discloses ANY ONE of "
            "the CORE combinations listed. Treat surface-form synonyms and implicit "
            "realisations as disclosure — a document that physically does the thing without "
            "using these words still qualifies. Answer ONLY with publication numbers.\n"
            "Reply in exactly this form, nothing else:\n"
            "QUALIFY: <comma-separated publication numbers, or NONE>\n\n"
            + "\n\n".join(blocks))


def feature_question(text):
    """One core MEMBER, asked alone — the directional question NLM is measured to be good at."""
    return ("For EACH candidate source document below, decide whether it discloses the single "
            "element described. Treat surface-form synonyms and implicit realisations as "
            "disclosure — a document that physically does the thing without using these words "
            "still qualifies. Answer ONLY with publication numbers.\n"
            "Reply in exactly this form, nothing else:\n"
            "DISCLOSE: <comma-separated publication numbers, or NONE>\n\n"
            "=== ELEMENT ===\n" + broaden(text))


def ask(nb, question, tries=3):
    """Ask, distinguishing a TRANSIENT empty answer from real quota exhaustion.

    nlm_bridge.is_quota_error() is true for `quota_suspect`, which the bridge sets on ANY empty
    answer — so a notebook that has not finished indexing 12-24 freshly staged multi-part
    documents reads as "quota exhausted" and costs an hour of backoff. Verified 2026-08-30:
    `default` reported QUOTA after ~4 queries against a ~68/day cap, while a probe on the same
    account answered instantly and this exact core question returned a clean
    "QUALIFY: CN120472728". Retry the empty answer; back off only on the EXPLICIT marker
    (RESOURCE_EXHAUSTED / 429 / "quota" in the error text).

    Returns the result dict, or None when the quota is genuinely gone.
    """
    for attempt in range(1, tries + 1):
        r = nlm_bridge.query(nb, question, profile=PROFILE)
        if r.get("quota"):                      # explicit marker — real exhaustion
            return None
        if not r.get("quota_suspect"):          # a real answer
            return r
        log(f"  empty answer (attempt {attempt}/{tries}) — waiting for ingestion, retrying")
        nlm_bridge.wait_sources_ready(nb, timeout=180, profile=PROFILE)
        time.sleep(20 * attempt)
    log("  empty answer persisted — chunk failed (NOT quota); it will retry on re-arm")
    return {"answer": ""}


QUESTION = core_question()
log(f"armed tab={TAB} profile={PROFILE} cores={len(cands)} question={len(QUESTION)}B "
    f"roster={a.roster}")

rows = cx.execute("""select number, title, abstract, claims, description, digest
                     from documents where tab_id=? and status='fetched'
                     and nlm_screen_state='rejected' order by number""", (TAB,)).fetchall()
done = set(json.load(open(PROG))) if os.path.exists(PROG) else set()
rescued = set(json.load(open(f"{AUD}/core_rescue_t{TAB}{TAG}.rescued.json"))) \
    if os.path.exists(f"{AUD}/core_rescue_t{TAB}{TAG}.rescued.json") else set()
if a.only_file:
    keep = set(json.load(open(a.only_file)))
    rows = [r for r in rows if r[0] in keep]
todo = [r for r in rows if r[0] not in done]
if a.limit:
    todo = todo[:a.limit]
log(f"rejected pile {len(rows)} | already asked {len(done)} | this run {len(todo)}")
heartbeat("running", f"core rescue armed — {len(todo)} of {len(rows)} rejected docs to ask",
          pile=len(rows), asked=len(done), rescued=len(rescued))


def parts(num, title, blob):
    data = blob.encode("utf-8")
    if len(data) <= CLIP:
        return [(f"{num} — {(title or '')[:120]}", blob)]
    out, i = [], 0
    while i < len(data):
        ch = data[i:i + CLIP].decode("utf-8", "ignore")
        out.append(ch); i += len(ch.encode("utf-8"))
    n = len(out)
    return [(f"{num} (part {j+1}/{n}) — {(title or '')[:100]}", t) for j, t in enumerate(out)]


for i in range(0, len(todo), a.roster):
    chunk = todo[i:i + a.roster]
    while True:
        res = nlm_bridge.create_notebook(f"🎯 core rescue — tab {TAB}", profile=PROFILE)
        nb = res.get("id") or (res.get("notebook") or {}).get("id")
        if not nb:
            if "100 notebooks" in str(res):
                log("NOTEBOOK-CAP -> sleep 1800"); time.sleep(1800); continue
            log(f"notebook create failed: {res}"); sys.exit(3)
        staged, ok_docs = 0, []
        for num, ti, ab, cl, de, dg in chunk:
            blob = "\n\n".join(filter(None, [f"{num} — {ti or ''}",
                                             ("ABSTRACT:\n" + ab) if ab else None,
                                             ("CLAIMS:\n" + cl) if cl else None,
                                             ("DESCRIPTION:\n" + de) if de else None,
                                             ("FULL-TEXT DIGEST:\n" + dg) if dg else None]))
            want = parts(num, ti, blob)
            got = 0
            for t, x in want:
                r = nlm_bridge.add_source_text(nb, t, x, profile=PROFILE)
                if not r.get("ok"):
                    r = nlm_bridge.add_source_text(nb, t, x, profile=PROFILE)
                got += 1 if r.get("ok") else 0
            # truncation NO-GO: a doc whose tail did not land is NOT asked about
            if got == len(want):
                ok_docs.append(num); staged += len(want)
            else:
                log(f"  {num}: {len(want)-got}/{len(want)} part(s) rejected — excluded, not asked")
        nlm_bridge.wait_sources_ready(nb, timeout=600, profile=PROFILE)
        inv = nlm_bridge.list_sources(nb, profile=PROFILE)
        if a.per_feature:
            # one question per DISTINCT core member; a doc qualifies if it satisfies every
            # member of ANY one core. The AND happens here, never inside NotebookLM.
            members = sorted({m for c in cands for m in c["features"]})
            per, quota = {}, False
            for mem in members:
                rq = ask(nb, feature_question(mem))
                if rq is None:
                    quota = True
                    break
                txt = rq.get("answer") or ""
                per[mem] = {n for n in ok_docs if re.search(re.escape(n), txt, re.IGNORECASE)}
            if quota:
                if not a.keep_notebook:
                    try:
                        nlm_bridge.delete_notebook(nb, profile=PROFILE)
                    except Exception:
                        pass
                heartbeat("quota_exhausted", f"account {PROFILE} out of NLM quota — retrying "
                          f"hourly; {len(rescued)} rescued of {len(done)} asked",
                          pile=len(rows), asked=len(done), rescued=len(rescued))
                log("QUOTA (explicit) -> sleep 3600")
                time.sleep(3600)
                continue
            r = {"answer": json.dumps({m: sorted(v) for m, v in per.items()}, indent=1),
                 "_per_feature": per}
        else:
            r = ask(nb, QUESTION)
            if r is None:
                if not a.keep_notebook:
                    try:
                        nlm_bridge.delete_notebook(nb, profile=PROFILE)
                    except Exception:
                        pass
                heartbeat("quota_exhausted", f"account {PROFILE} out of NLM quota — retrying "
                          f"hourly; {len(rescued)} rescued of {len(done)} asked",
                          pile=len(rows), asked=len(done), rescued=len(rescued))
                log("QUOTA (explicit) -> sleep 3600")
                time.sleep(3600)
                continue

        ans = r.get("answer") or r.get("error") or ""
        ts = int(time.time())
        json.dump({"tab": TAB, "tag": a.tag, "ts": ts, "notebook": nb, "wording": "genus-broadened", "mode": ("per-feature" if a.per_feature else "conjunction"),
                   "cores": [c.get("label") for c in cands], "question": QUESTION,
                   "asked": ok_docs, "answer": ans,
                   "source_inventory": [s.get("title") for s in (inv.get("sources") or [])]},
                  open(f"{EV}/t{TAB}{TAG}_{ts}.json", "w"), indent=1)
        bad = (not ans.strip()) or '"status": "error"' in ans
        if bad:
            log(f"chunk {i//a.roster+1}: question failed — chunk NOT credited, will retry on re-arm")
        else:
            if a.per_feature:
                per = r["_per_feature"]
                named = {n for n in ok_docs
                         if any(all(n in per.get(mem, set()) for mem in c["features"])
                                for c in cands)}
            else:
                named = set()
                m = re.search(r"QUALIFY:\s*(.+)", ans, re.IGNORECASE)
                pool = m.group(1) if m else ans
                for num in ok_docs:
                    if re.search(re.escape(num), pool, re.IGNORECASE):
                        named.add(num)
            rescued |= named
            done |= set(ok_docs)
            json.dump(sorted(done), open(PROG, "w"))
            json.dump(sorted(rescued), open(f"{AUD}/core_rescue_t{TAB}{TAG}.rescued.json", "w"))
            log(f"chunk {i//a.roster+1}: asked {len(ok_docs)} ({staged} sources) -> "
                f"RESCUED {len(named)} | running total {len(rescued)}/{len(done)}")
            heartbeat("running", f"asked {len(done)}/{len(rows)} rejected docs, "
                                 f"{len(rescued)} rescued so far",
                      pile=len(rows), asked=len(done), rescued=len(rescued),
                      chunk=i//a.roster+1)
        if not a.keep_notebook:
            try: nlm_bridge.delete_notebook(nb, profile=PROFILE)
            except Exception: pass
        break
heartbeat("done", f"core rescue finished — {len(rescued)} rescued of {len(done)} asked",
          pile=len(rows), asked=len(done), rescued=len(rescued))
log(f"done — {len(rescued)} rescued of {len(done)} asked")
