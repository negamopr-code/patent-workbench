#!/usr/bin/env python3
"""nlm-keeper daemon: rotate + persist NotebookLM sessions, auto-resume screens.

Every REFRESH_SECS (default 900), for each account in accounts.conf:
  1. CDP-extract cookies from that account's live Chromium (the extract path
     itself find-or-creates the NotebookLM tab and navigates it, which is also
     what keeps Google rotating the session server-side).
  2. Save them with the CLI's own AuthManager. HOME is /home/app and
     ~/.notebooklm-mcp-cli is the SAME named volume patent-bench mounts, so a
     save here IS the reseed — patent-bench picks it up on its next request.
Then, if at least one account refreshed, ask patent-bench for tabs whose
mega-screen sits in phase=error with an auth-expired message and resume them.

Not logged in yet (fresh account, or Google forced a re-login) is not an
error: the daemon logs a LOGIN NEEDED line pointing at the noVNC URL and
retries next cycle.

The notebook.google.com rebrand widening mirrors scripts/nlm-login-via-cdp.py
(f8e9a66): Google redirects some accounts off notebooklm.google.com, which the
stock URL check would misread as "not logged in".
"""
import json
import os
import time
import urllib.request

import notebooklm_tools.utils.cdp as cdp
from notebooklm_tools.core.auth import AuthManager

PB_URL = os.environ.get("PB_URL", "http://host.docker.internal:8099")
REFRESH_SECS = int(os.environ.get("REFRESH_SECS", "900"))
ACCOUNTS_FILE = "/home/app/chrome-profiles/accounts.conf"
NOVNC_HINT = "http://localhost:8106/vnc.html"

_orig = cdp._is_notebooklm_url
cdp._is_notebooklm_url = lambda url: _orig(url) or "notebook.google.com" in (url or "")


def accounts():
    out = []
    with open(ACCOUNTS_FILE) as f:
        i = 0
        for line in f:
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            out.append((name, 9301 + i))
            i += 1
    return out


def refresh(name, port):
    result = cdp.extract_cookies_via_existing_cdp(
        cdp_url=f"http://127.0.0.1:{port}", wait_for_login=False, login_timeout=1)
    cookies = result["cookies"]
    if not any("OSID" in c.get("name", "") for c in cookies):
        print(f"[{name}] LOGIN NEEDED — no OSID cookies; sign in once at {NOVNC_HINT}")
        return False
    AuthManager(name).save_profile(
        cookies=cookies,
        csrf_token=result.get("csrf_token", ""),
        session_id=result.get("session_id", ""),
        email=result.get("email", ""),
        force=True,
        build_label=result.get("build_label", ""),
    )
    print(f"[{name}] refreshed: {len(cookies)} cookies, email={result.get('email')}")
    return True


def api(path, payload=None):
    req = urllib.request.Request(
        PB_URL + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"} if payload is not None else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def resume_auth_errored():
    for tab in api("/api/tabs")["tabs"]:
        tid = tab["id"]
        try:
            st = api(f"/api/tabs/{tid}/nlm-screen/status")
        except Exception:
            continue
        err = (st.get("error") or "")
        if st.get("phase") == "error" and st.get("resumable") \
                and ("Authentication expired" in err or "auth" in err.lower()):
            try:
                r = api(f"/api/tabs/{tid}/nlm-screen", {"resume": True})
                print(f"[resume] tab {tid}: started={r.get('started')} "
                      f"round={r.get('round')} {r.get('screened')}/{r.get('total')}")
            except Exception as e:
                print(f"[resume] tab {tid}: FAILED — {e}")


print(f"keeper daemon up: accounts={[a for a, _ in accounts()]} "
      f"refresh every {REFRESH_SECS}s, patent-bench at {PB_URL}")
time.sleep(25)  # let the chromiums finish first paint
ever_ok = set()
while True:
    any_ok = False
    for name, port in accounts():
        try:
            if refresh(name, port):
                any_ok = True
                ever_ok.add(name)
        except Exception as e:
            print(f"[{name}] refresh failed: {type(e).__name__}: {e}")
    if any_ok:
        try:
            resume_auth_errored()
        except Exception as e:
            print(f"[resume] sweep failed: {type(e).__name__}: {e}")
    # Accounts still awaiting their one-time login poll fast, so a fresh
    # sign-in lands within a minute; settled accounts rotate every REFRESH_SECS.
    pending_login = {n for n, _ in accounts()} - ever_ok
    time.sleep(60 if pending_login else REFRESH_SECS)
