#!/usr/bin/env python3
"""mech-watchdog — keep the mechanism scans alive across quota walls and restarts.

Standing rule in this project: a job interrupted by a quota limit must restart itself when the
limit lifts. Two gaps this closes:
  1. a lane armed BEFORE the empty-streak back-off shipped will grind through its whole pile on
     empty answers and credit nothing (t13, armed 21:10, fix deployed 21:19)
  2. no lane survives a patent-bench restart, and nothing re-arms them

Every 20 minutes: for each tab with work left, if no scan process is alive for it AND its
account answers a trivial probe, re-arm it. Progress files make re-arming free — nothing is
re-asked. One lane per account at a time (A2).

  docker exec -d patent-bench python3 /data/mech-watchdog.py
"""
import json, os, subprocess, sqlite3, sys, time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

LOG = "/data/.mech_watchdog.log"
# A lane is (tab, tag). The tag selects the QUESTION VARIANT ("" = the tab's original question,
# "v2" = the 2026-09-03 re-pitch) and gives the run its own progress/picks/log files, so two
# wordings over the same pile never share a ledger.
LANES = ((12, ""), (10, "v2"), (13, "v2"), (14, "v2"))


def log(m):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + m + "\n")


def alive(tab, tag):
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            c = open(f"/proc/{p}/cmdline", "rb").read().decode("utf8", "ignore").split("\0")
        except OSError:
            continue
        c = [x for x in c if x]
        if len(c) >= 3 and c[1].endswith("mechanism-scan.py") and c[2] == str(tab):
            running_tag = c[c.index("--tag") + 1] if "--tag" in c[:-1] else ""
            if running_tag == tag:
                return True
    return False


def quota_ok(prof):
    try:
        r = nlm_bridge.list_notebooks(profile=prof)
        nbs = [n for n in (r.get("notebooks") or r.get("items") or [])
               if (n.get("title") or "").startswith(("🔁 Screen", "🧾 Claims"))]
        if not nbs:
            return True
        res = nlm_bridge.query(nbs[0]["id"], "Reply with exactly the word: OK", profile=prof)
        return bool(res.get("answer"))
    except Exception:                                        # noqa: BLE001
        return False


log("watchdog armed")
while True:
    cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
    def profile_of(t):
        return (cx.execute("select coalesce(nlm_profile,'default') from tabs where id=?",
                           (t,)).fetchone() or ["default"])[0]
    busy_accounts = set()
    for t, tag in LANES:
        if alive(t, tag):
            busy_accounts.add(profile_of(t))
    for t, tag in LANES:
        if alive(t, tag):
            continue
        pile = cx.execute("""select count(*) from documents where tab_id=? and status='fetched'
                             and nlm_screen_state='rejected'""", (t,)).fetchone()[0]
        sfx = f"_{tag}" if tag else ""
        pg = f"/data/audits/mech_t{t}{sfx}.progress.json"
        asked = len(json.load(open(pg))) if os.path.exists(pg) else 0
        if asked >= pile:
            continue                                          # finished
        prof = profile_of(t)
        if prof in busy_accounts:
            continue                                          # A2: one job per account
        if not quota_ok(prof):
            log(f"t{t}: {pile-asked} left but {prof} still out of quota — waiting")
            continue
        cmd = ["python3", "/data/mechanism-scan.py", str(t), "--roster", "30"]
        if tag:
            cmd += ["--tag", tag]
        subprocess.Popen(cmd)
        busy_accounts.add(prof)
        log(f"t{t}{sfx}: re-armed on {prof} ({pile-asked} docs left, {asked} already asked)")
    time.sleep(1200)
