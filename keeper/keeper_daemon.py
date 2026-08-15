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

BOOT-QUARANTINE (deferred #13, shutdown test 2026-08-14): at boot the
entrypoint launches every Chromium blocked from *.google.com with a
<name>.quarantine marker. While the marker exists the daemon does NOT drive
the browser at all — it probes the saved CLI profile (`nlm notebook list`,
free) instead. CLI alive → nothing to do, audits run login-free. CLI dead
(auth-class error, or 3 straight probe failures) → lift_quarantine: relaunch
the browser unblocked so it can auto-recover from gracefully-flushed cookies
or serve a human login. A <name>.wake file lifts quarantine on demand — with
the browser's Google cookies WIPED first, because presenting stale rotating
cookies next to a LIVE CLI session kills the whole session family.

The notebook.google.com rebrand widening mirrors scripts/nlm-login-via-cdp.py
(f8e9a66): Google redirects some accounts off notebooklm.google.com, which the
stock URL check would misread as "not logged in".
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

import notebooklm_tools.utils.cdp as cdp
from notebooklm_tools.core.auth import AuthManager

PB_URL = os.environ.get("PB_URL", "http://host.docker.internal:8099")
REFRESH_SECS = int(os.environ.get("REFRESH_SECS", "900"))
PROFILES_DIR = "/home/app/chrome-profiles"
ACCOUNTS_FILE = PROFILES_DIR + "/accounts.conf"
NOVNC_HINT = "http://localhost:8106/vnc.html"
NLM_BIN = "/opt/venv/bin/nlm"
# "bubu:default" — every snapshot of profile 'bubu' is also saved as 'default':
# same Google account under two profile names (tabs bound to the old name keep
# working without a separate login).
PROFILE_ALIASES = dict(kv.split(":", 1)
                       for kv in os.environ.get("PROFILE_ALIASES", "").split(",")
                       if ":" in kv)


def _graceful_exit(signum, frame):
    """A SIGKILLed Chromium loses its final cookie-DB flushes, so the next boot
    starts logged out — the root of the login-after-every-restart pain. On
    container stop, SIGTERM every Chromium (this daemon is PID 1, they are its
    children) and give them a moment to flush before exiting."""
    print("keeper: SIGTERM — shutting Chromiums down cleanly for cookie flush…")
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if "chrom" in comm:
                os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    time.sleep(5)
    sys.exit(0)


signal.signal(signal.SIGTERM, _graceful_exit)

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
    cdp_url = f"http://127.0.0.1:{port}"
    # Visit the OLD domain first: the CLI talks to notebooklm.google.com, but a
    # login completed on the rebranded notebook.google.com alone never mints
    # OSID cookies there — only riding the redirect chain does (bit 2026-08-09:
    # fresh work2 login saved 58 cookies yet patent-bench still got "auth
    # expired"). The navigation is also the reload that keeps Google rotating
    # the session server-side.
    try:
        page = cdp.find_or_create_notebooklm_page_by_cdp_url(cdp_url)
        ws = cdp._normalize_ws_url(page.get("webSocketDebuggerUrl"))
        cdp.navigate_to_url(ws, "https://notebooklm.google.com")
        time.sleep(6)
    except Exception:
        pass  # extraction below surfaces the real error
    result = cdp.extract_cookies_via_existing_cdp(
        cdp_url=cdp_url, wait_for_login=False, login_timeout=1)
    cookies = result["cookies"]
    # A usable profile needs BOTH layers: the notebooklm.google.com OSID
    # (service session) AND the central .google.com SID/SAPISID family — the
    # batchexecute API authenticates with a SAPISID hash, and a service-only
    # login (OSID present, SAPISID absent) still gets "Authentication expired"
    # (bit 2026-08-09 twice).
    has_nlm_osid = any("OSID" in c.get("name", "")
                       and "notebooklm.google.com" in c.get("domain", "") for c in cookies)
    has_central = any(c.get("name") in ("SAPISID", "__Secure-1PSID", "SID")
                      and c.get("domain", "").lstrip(".") == "google.com" for c in cookies)
    if not (has_nlm_osid and has_central):
        print(f"[{name}] LOGIN NEEDED (nlm_osid={has_nlm_osid} central={has_central}) — "
              f"sign in once at {NOVNC_HINT}")
        return False
    # A logged-out browser still yields a cookie dump that passes the layer checks
    # (stale-but-present OSID/SAPISID) — the reliable tell is the page itself: only
    # the real app serves the labs-tailwind frontend. Snapshotting a sign-in page
    # would overwrite the last-known-good profile with dead state (bit 2026-08-13:
    # a whole day of cheerful "refreshed" logs while the session was gone).
    build = result.get("build_label") or ""
    if "tailwind" not in build:
        print(f"[{name}] LOGIN NEEDED (logged-out page: build={build[:40] or '?'}) — "
              f"keeping the last good snapshot; sign in once at {NOVNC_HINT}")
        return False
    for prof in [name] + ([PROFILE_ALIASES[name]] if name in PROFILE_ALIASES else []):
        AuthManager(prof).save_profile(
            cookies=cookies,
            csrf_token=result.get("csrf_token", ""),
            session_id=result.get("session_id", ""),
            email=result.get("email", ""),
            force=True,
            build_label=result.get("build_label", ""),
        )
    print(f"[{name}] refreshed: {len(cookies)} cookies, email={result.get('email')}"
          + (f" (mirrored to '{PROFILE_ALIASES[name]}')" if name in PROFILE_ALIASES else ""))
    return True


def probe_logged_in(name, port):
    """Ride to the app and see where the browser lands. True only when the page
    settles on the app itself (not accounts.google.com / a signin interstitial)."""
    try:
        page = cdp.find_or_create_notebooklm_page_by_cdp_url(f"http://127.0.0.1:{port}")
        ws = cdp._normalize_ws_url(page.get("webSocketDebuggerUrl"))
        cdp.navigate_to_url(ws, "https://notebook.google.com")
        time.sleep(8)
        url = cdp.get_current_url(ws) or ""
        return ("accounts.google" not in url and "signin" not in url
                and ("notebook.google.com" in url or "notebooklm.google.com" in url))
    except Exception as e:
        print(f"[{name}] logged-in probe failed: {type(e).__name__}: {e}")
        return False


def boot_restore(name, port):
    """Chrome's own disk profile is the ONLY source of a browser session after a
    restart. NEVER inject the saved snapshot into the browser: presenting
    restored rotating tokens from a restarted browser makes Google invalidate
    the whole session FAMILY — killing the still-valid CLI profile too (bit
    2026-08-13 twice: over a live session AND over a logged-out one). The
    snapshot's only safe consumer is the CLI; a logged-out browser needs a
    human login, and until then the untouched snapshot keeps serving the CLI."""
    if probe_logged_in(name, port):
        print(f"[{name}] session survived the restart")
        return
    print(f"[{name}] LOGIN NEEDED after restart — sign in once at {NOVNC_HINT} "
          f"(saved profile untouched and still serving the CLI)")


def inject_profile_cookies(name, port):
    """Recovery-only restore (see boot_restore): push the last-saved cookie set
    into the freshly started Chromium. Some central Google cookies are
    session-scoped in this browser, so they don't survive a restart on disk."""
    from pathlib import Path
    f = Path.home() / ".notebooklm-mcp-cli" / "profiles" / name / "cookies.json"
    if not f.exists():
        return
    payload = []
    for c in json.load(open(f)):
        k = {"name": c["name"], "value": c["value"], "domain": c["domain"],
             "path": c.get("path", "/"), "secure": c.get("secure", False),
             "httpOnly": c.get("httpOnly", False)}
        if c.get("expires", -1) and c.get("expires", -1) > 0:
            k["expires"] = c["expires"]
        if c.get("sameSite"):
            k["sameSite"] = c["sameSite"]
        payload.append(k)
    try:
        from websocket import create_connection
        page = cdp.find_or_create_notebooklm_page_by_cdp_url(f"http://127.0.0.1:{port}")
        ws_url = cdp._normalize_ws_url(page.get("webSocketDebuggerUrl"))
        ws = create_connection(ws_url, timeout=10, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Storage.setCookies",
                            "params": {"cookies": payload}}))
        ws.recv()
        ws.close()
        print(f"[{name}] restored {len(payload)} saved cookies into the fresh browser")
    except Exception as e:
        print(f"[{name}] cookie restore failed: {type(e).__name__}: {e}")


def _quar_path(name):
    return f"{PROFILES_DIR}/{name}.quarantine"


def _wake_path(name):
    return f"{PROFILES_DIR}/{name}.wake"


def cli_probe(name):
    """(ok, err): a real notebook listing against the SAVED CLI profile — no
    browser involved, no Q&A quota consumed. This is the quarantine health
    check: while it passes, patent-bench keeps working and no login is needed."""
    cmd = [NLM_BIN, "notebook", "list"]
    # mirror patent-bench's _with_profile: 'default' means no --profile flag
    if name != "default":
        cmd += ["--profile", name]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    if p.returncode == 0:
        return True, ""
    return False, ((p.stderr or p.stdout) or "").strip()[:200]


def _chromium_pid(port):
    """Main Chromium process for this account (owns the CDP port; renderers
    carry --type= and are torn down with it)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
        except OSError:
            continue
        if f"--remote-debugging-port={port}" in cmd and "--type=" not in cmd \
                and "chrom" in cmd:
            return int(pid)
    return None


def lift_quarantine(name, port, wipe_google_cookies=False):
    """Wake a quarantined browser: relaunch it WITHOUT the Google block so it can
    serve a human login (or, after a graceful stop with fresh on-disk cookies,
    auto-recover). wipe_google_cookies=True is for waking while the CLI session
    is still ALIVE (.wake): presenting stale rotating cookies to Google would
    kill the live session family, so the browser must arrive empty-handed."""
    pid = _chromium_pid(port)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(24):
            if _chromium_pid(port) is None:
                break
            time.sleep(0.5)
    prof = f"{PROFILES_DIR}/{name}"
    if wipe_google_cookies:
        for rel in ("Default/Cookies", "Default/Cookies-journal",
                    "Default/Network/Cookies", "Default/Network/Cookies-journal"):
            try:
                os.unlink(os.path.join(prof, rel))
            except OSError:
                pass
    for lockf in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.unlink(os.path.join(prof, lockf))
        except OSError:
            pass
    subprocess.Popen(
        ["chromium", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
         f"--user-data-dir={prof}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", "--start-maximized",
         "https://notebooklm.google.com"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for f in (_quar_path(name), _wake_path(name)):
        try:
            os.unlink(f)
        except OSError:
            pass
    print(f"[{name}] browser WOKEN (Google unblocked"
          + (", cookies wiped" if wipe_google_cookies else "")
          + ") — next cycle refreshes or asks for a login")
    time.sleep(15)  # let it paint before the next probe/refresh touches CDP


def bring_to_front(port):
    """Focus this account's Chrome window so the noVNC page shows the right
    sign-in screen without the user hunting through stacked windows."""
    try:
        from websocket import create_connection
        page = cdp.find_or_create_notebooklm_page_by_cdp_url(f"http://127.0.0.1:{port}")
        ws_url = cdp._normalize_ws_url(page.get("webSocketDebuggerUrl"))
        ws = create_connection(ws_url, timeout=5, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
        ws.recv()
        ws.close()
    except Exception:
        pass


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
for _name, _port in accounts():
    if os.path.exists(_quar_path(_name)):
        print(f"[{_name}] BOOT: browser quarantined (Google blocked) — probing "
              f"the CLI snapshot instead; login NOT needed while it lives")
    else:
        boot_restore(_name, _port)
ever_ok = set()
quar_fails = {}
while True:
    any_ok = False
    pending_ports = []
    for name, port in accounts():
        if os.path.exists(_quar_path(name)):
            if os.path.exists(_wake_path(name)):
                # human asked for the browser back while the CLI may be alive:
                # arrive at Google empty-handed so the live family is safe
                lift_quarantine(name, port, wipe_google_cookies=True)
                continue
            ok, err = cli_probe(name)
            if ok:
                quar_fails[name] = 0
                any_ok = True
                ever_ok.add(name)
                print(f"[{name}] quarantined: CLI session ALIVE, browser parked "
                      f"off Google — running login-free")
            else:
                quar_fails[name] = quar_fails.get(name, 0) + 1
                authish = any(w in err.lower()
                              for w in ("auth", "expired", "login", "sign in"))
                if authish or quar_fails[name] >= 3:
                    print(f"[{name}] quarantine LIFT: CLI snapshot dead "
                          f"({err[:100] or 'no error text'}) — waking the browser")
                    lift_quarantine(name, port, wipe_google_cookies=False)
                else:
                    print(f"[{name}] quarantine probe hiccup "
                          f"({quar_fails[name]}/3: {err[:100]}) — staying parked")
            continue
        try:
            if refresh(name, port):
                any_ok = True
                ever_ok.add(name)
            else:
                pending_ports.append(port)
        except Exception as e:
            print(f"[{name}] refresh failed: {type(e).__name__}: {e}")
    if pending_ports:
        bring_to_front(pending_ports[0])
    if any_ok:
        try:
            resume_auth_errored()
        except Exception as e:
            print(f"[resume] sweep failed: {type(e).__name__}: {e}")
    # Accounts still awaiting their one-time login poll fast, so a fresh
    # sign-in lands within a minute; settled accounts rotate every REFRESH_SECS.
    pending_login = {n for n, _ in accounts()} - ever_ok
    time.sleep(60 if pending_login else REFRESH_SECS)
