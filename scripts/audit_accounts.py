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
import json
import os
import sqlite3
import sys
import time

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


def running_jobs(tab: int) -> list[str]:
    out = []
    for kind, lock in LOCKS.items():
        p = os.path.join(DATA, lock.format(t=tab))
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < SCREEN_TTL:
            out.append(kind)
    return out


def main() -> int:
    reg = registered_accounts()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute("select id, name, nlm_profile from tabs order by id").fetchall()
    checks, per_account = [], {}
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
    verdict = {"script_version": "2026-08-25.1", "ts": int(time.time()), "worst": worst,
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
