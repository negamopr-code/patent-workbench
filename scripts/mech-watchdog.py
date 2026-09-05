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

  docker exec -d patent-bench python3 /data/mech-watchdog.py [--only 10:v3]
"""
import argparse, hashlib, json, os, subprocess, sqlite3, sys, time

sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge  # noqa: E402

LOG = "/data/.mech_watchdog.log"
# A lane is (tab, tag). The tag selects the QUESTION VARIANT ("" = the tab's original question,
# "v2" = the 2026-09-03 re-pitch) and gives the run its own progress/picks/log files, so two
# wordings over the same pile never share a ledger.
# v3 (2026-09-04) applies the CORRECTED t14 lesson: relax incidentals, never the core relation.
# Order matters only for tie-breaks; the A2 one-job-per-account rule serialises each account.
# (tab, tag) or (tab, tag, states). states defaults to "rejected" — the discard pile. The
# t14 "grad" lane walks the 71 UNREAD GRADUATES instead: unread material is where this
# instrument's only measured yield comes from, and no other lane covers them while the
# standing graduate driver is paused (2026-09-04).
# Order = priority when an account frees up. The t14 grad lane (71 docs, ~3 chunks) runs BEFORE
# t14 v2's long tail: it is small, and it is the only lane covering t14's unread graduates.
LANES = ((12, ""), (14, "grad", "graduate", True), (10, "v2"), (13, "v2"), (14, "v2"),
         (10, "v3"), (13, "v3"))

PROFILES = "/home/app/.notebooklm-mcp-cli/profiles"

ap = argparse.ArgumentParser()
# Arm only a subset of LANES, e.g. --only 10:v3 or --only 10 (all of tab 10's lanes). Used when
# some accounts are unsafe to drive but others are intact, so the safe lanes still self-heal.
ap.add_argument("--only", default="")
ARGS = ap.parse_args()
ONLY = [x.strip() for x in ARGS.only.split(",") if x.strip()]


def selected(tab, tag):
    if not ONLY:
        return True
    return str(tab) in ONLY or f"{tab}:{tag}" in ONLY


def ambiguous_profiles():
    """Profiles whose cookie jar is byte-identical to another profile's.

    The account gate compares tabs.nlm_profile to a registered NAME; it never checks that two
    names are two accounts. On 2026-09-05 `default` and `work2` held the same jar, so t13's
    lanes ran on t14's account and A2's one-job-per-account serialisation — which keys on the
    name — let two lanes drain one real quota pool. Identity is checked here instead, and a
    profile that cannot be told apart from another is refused rather than driven blind.
    """
    seen, dupes = {}, set()
    try:
        names = sorted(os.listdir(PROFILES))
    except OSError:
        return dupes
    for n in names:
        f = os.path.join(PROFILES, n, "cookies.json")
        try:
            h = hashlib.md5(open(f, "rb").read()).hexdigest()
        except OSError:
            continue
        if h in seen:
            dupes.add(n)
            dupes.add(seen[h])
        else:
            seen[h] = n
    return dupes


def log(m):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + m + "\n")


def lane_parts(lane):
    """(tab, tag[, states[, unread_only]]) -> normalised 4-tuple."""
    tab, tag = lane[0], lane[1]
    return (tab, tag, (lane[2] if len(lane) > 2 else "rejected"),
            bool(lane[3]) if len(lane) > 3 else False)


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
    for lane in LANES:
        t, tag, _, _ = lane_parts(lane)
        if alive(t, tag):
            busy_accounts.add(profile_of(t))
    ambiguous = ambiguous_profiles()
    for lane in LANES:
        t, tag, states, unread_only = lane_parts(lane)
        if alive(t, tag) or not selected(t, tag):
            continue
        _st = [x.strip() for x in states.split(",") if x.strip()]
        pile = cx.execute(
            "select count(*) from documents where tab_id=? and status='fetched' "
            "and nlm_screen_state in (%s)%s"
            % (",".join("?" * len(_st)), " and score is null" if unread_only else ""),
            (t, *_st)).fetchone()[0]
        sfx = f"_{tag}" if tag else ""
        pg = f"/data/audits/mech_t{t}{sfx}.progress.json"
        asked = len(json.load(open(pg))) if os.path.exists(pg) else 0
        if asked >= pile:
            continue                                          # finished
        prof = profile_of(t)
        sfx_ = f"_{tag}" if tag else ""
        if prof in ambiguous:
            log(f"t{t}{sfx_}: REFUSED — profile {prof} shares a cookie jar with another "
                f"profile; re-seed it before this lane may run")
            continue
        if prof in busy_accounts:
            continue                                          # A2: one job per account
        if not quota_ok(prof):
            log(f"t{t}{sfx}: {pile-asked} left but {prof} still out of quota — waiting")
            continue
        cmd = ["python3", "/data/mechanism-scan.py", str(t), "--roster", "30"]
        if tag:
            cmd += ["--tag", tag]
        if states != "rejected":
            cmd += ["--states", states]
        if unread_only:
            cmd += ["--unread-only"]
        subprocess.Popen(cmd)
        busy_accounts.add(prof)
        log(f"t{t}{sfx}: re-armed on {prof} ({pile-asked} docs left, {asked} already asked)")
    time.sleep(1200)
