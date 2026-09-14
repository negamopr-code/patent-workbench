#!/usr/bin/env python3
"""NLM account gate — deterministic, read-only.

Two hard rules (user 2026-08-23 / 2026-08-25):
  A1  a tab's NLM account is FIXED for the life of the project: tabs.nlm_profile
      must equal the registered binding in docs/controls-registry.json
      ("nlm_accounts"), and every live lane notebook of the tab must have been
      created under that profile;
  A2  ONE running NLM job per account at a time (screens, claims audits,
      lanes run in series per account — t13 before t11 on default, t12 before
      t14 on work2).

Run BEFORE any resume/launch/rebind:
  docker exec -i patent-bench python3 - --registry "$(cat docs/controls-registry.json)" < scripts/audit_accounts.py
Exit 0 = PASS, 2 = FAIL (do not launch). Writes /data/audits/audit_accounts.json.
"""
import hashlib
import json
import os
import sqlite3
import sys
import time

# A0 (added 2026-09-13): two DIFFERENTLY-NAMED registered profiles must not
# share one cookie jar. A byte-identical pair means they're the same real
# Google account/quota pool wearing two tab labels — this gate used to be
# name-deep (compared strings, never the actual jars) and passed clean while
# t11/t13's "default" was silently work2's session
# (incident_nlm_account_gate_name_deep.md, 2026-09-05). Root cause: the NLM
# CLI's internal token-cache sync force-overwrites the "default" profile on
# every OTHER profile's token refresh unless NLM_PROFILE is set per
# subprocess call — fixed 2026-09-13 in nlm_bridge.py's _run() and
# keeper/keeper_daemon.py's cli_probe(); this check re-verifies live rather
# than trusting that the fix holds forever.

DB = os.environ.get("PB_DB", "/data/workbench.db")
DATA = os.path.dirname(DB)
REGISTRY = os.environ.get("PB_CONTROLS", "/app/docs/controls-registry.json")
SCREEN_TTL = 1200          # api.py: lock mtime older than this = job dead

JOB_FILES = {"nlm-screen": ".nlm_screen_{t}.json", "claims-audit": ".nlm_claims_{t}.json"}
LOCKS = {"nlm-screen": ".nlm_screen_{t}.lock", "claims-audit": ".nlm_claims_{t}.lock"}


def registered_accounts() -> dict[str, str]:
    if "--registry" in sys.argv:                 # JSON text, as audit_status takes --baselines
        return (json.loads(sys.argv[sys.argv.index("--registry") + 1]).get("nlm_accounts") or {})
    for p in (REGISTRY, "/workspace/docs/controls-registry.json"):
        if os.path.exists(p):
            with open(p) as f:
                return (json.load(f).get("nlm_accounts") or {})
    return {}


def out_of_band_jobs(tab: int) -> list[str]:
    """NLM jobs that do NOT go through the app's locks and are therefore invisible
    to the lock census — currently scripts/restage-blind-tails.py + the nlm_followup
    child it drives (F7: a gate that only sees the app relies on session discipline).
    Detected from /proc, so it works inside the container where `ps` is absent."""
    out = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "ignore").replace("\0", " ")
        except OSError:
            continue
        if not cmd.strip():
            continue
        args = cmd.split()
        if "restage-blind-tails.py" in cmd and str(tab) in args:
            out.append(f"restage[{pid}]")
        elif "ab-wording-test.py" in cmd and "--tab" in args:
            try:
                if args[args.index("--tab") + 1] == str(tab):
                    out.append(f"ab-wording[{pid}]")
            except (ValueError, IndexError):
                pass
        elif "nlm_followup.py" in cmd and "--tab" in args:
            try:
                if args[args.index("--tab") + 1] == str(tab):
                    out.append(f"followup[{pid}]")
            except (ValueError, IndexError):
                pass
    return out


def profile_cookie_hash(prof: str) -> str | None:
    path = os.path.expanduser(f"~/.notebooklm-mcp-cli/profiles/{prof}/cookies.json")
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def identity_checks(reg: dict[str, str]) -> list[dict]:
    checks = []
    seen: dict[str, str] = {}
    for name in sorted(set(reg.values())):
        h = profile_cookie_hash(name)
        if h is None:
            checks.append({"check": "A0-identity", "account": name, "level": "FAIL",
                           "msg": f"{name}: cookies.json missing/unreadable"})
        elif h in seen:
            checks.append({"check": "A0-identity", "account": name, "level": "FAIL",
                           "msg": f"{name} and {seen[h]}: byte-identical cookie jars "
                                  f"(md5 {h}) — same real account, not two"})
        else:
            seen[h] = name
            checks.append({"check": "A0-identity", "account": name, "level": "PASS",
                           "msg": f"{name}: distinct jar (md5 {h[:12]})"})
    return checks


def running_jobs(tab: int) -> list[str]:
    out = []
    for kind, lock in LOCKS.items():
        p = os.path.join(DATA, lock.format(t=tab))
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < SCREEN_TTL:
            out.append(kind)
    oob = out_of_band_jobs(tab)
    # a restage runner and the nlm_followup child it drives are ONE logical job
    if any(j.startswith("restage[") for j in oob):
        oob = [j for j in oob if not j.startswith("followup[")]
    out += oob
    return out


def main() -> int:
    reg = registered_accounts()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute("select id, name, nlm_profile from tabs order by id").fetchall()
    checks, per_account = identity_checks(reg), {}
    for tid, name, prof in rows:
        prof = prof or "default"
        want = reg.get(str(tid))
        if want is not None and want != prof:
            checks.append({"check": "A1-binding", "tab": tid, "level": "FAIL",
                           "msg": f"t{tid} {name}: bound to '{prof}', registered '{want}'"})
        elif want is not None:
            checks.append({"check": "A1-binding", "tab": tid, "level": "PASS",
                           "msg": f"t{tid}: '{prof}' == registered"})
        for kind in running_jobs(tid):
            per_account.setdefault(prof, []).append(f"t{tid}:{kind}")
    for prof, jobs in sorted(per_account.items()):
        lvl = "FAIL" if len(jobs) > 1 else "PASS"
        checks.append({"check": "A2-one-job-per-account", "account": prof, "level": lvl,
                       "msg": f"{prof}: {len(jobs)} running job(s) {jobs}"})
    if not per_account:
        checks.append({"check": "A2-one-job-per-account", "level": "PASS", "msg": "no running NLM jobs"})
    worst = "FAIL" if any(x["level"] == "FAIL" for x in checks) else "PASS"
    verdict = {"script_version": "2026-09-13.1", "ts": int(time.time()), "worst": worst,
               "registered": reg, "checks": checks}
    os.makedirs(os.path.join(DATA, "audits"), exist_ok=True)
    with open(os.path.join(DATA, "audits", "audit_accounts.json"), "w") as f:
        json.dump(verdict, f, indent=1)
    for x in checks:
        print(f"{x['level']:4} {x['check']:24} {x['msg']}")
    print(f"VERDICT: {worst}")
    return 0 if worst == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
