#!/usr/bin/env python3
"""nlm login via an external CDP Chrome, tolerant of the notebook.google.com rebrand.

Google now redirects notebooklm.google.com -> notebook.google.com ("Gemini
Notebook") for (at least some) accounts. The stock `nlm login` waits for the
tab URL to be on notebooklm.google.com, so it times out with "Login timeout"
even though the user IS signed in. Everything downstream works on the
rebranded page — Network.getAllCookies still returns the HttpOnly OSID
cookies (for BOTH domains: the redirect chain sets them on the old one too),
and the CSRF/session/build-label tokens are the same batchexecute fields.
So: widen the domain check, then reuse the CLI's own extract + save path.

Run from the claude dev container (uses the patent-wiki-analyzer venv):

    /home/node/patent-wiki-analyzer/.venv/bin/python scripts/nlm-login-via-cdp.py work2 http://192.168.65.254:9223

Prereqs (Windows side): debug Chrome running with --remote-debugging-port=9222
on an isolated --user-data-dir, signed into the target account only;
`netsh portproxy` 0.0.0.0:9223 -> 127.0.0.1:9222 + firewall rule `nlm-cdp`.
Use the raw gateway IP (host.docker.internal is rejected by Chrome's Host
header check). Then seed into patent-bench: ./scripts/reseed-nlm-profile.sh <profile>
"""

import sys

import notebooklm_tools.utils.cdp as cdp
from notebooklm_tools.core.auth import AuthManager

profile = sys.argv[1] if len(sys.argv) > 1 else "work2"
cdp_url = sys.argv[2] if len(sys.argv) > 2 else "http://192.168.65.254:9223"

_orig = cdp._is_notebooklm_url
cdp._is_notebooklm_url = lambda url: _orig(url) or "notebook.google.com" in (url or "")

result = cdp.extract_cookies_via_existing_cdp(
    cdp_url=cdp_url,
    wait_for_login=True,
    login_timeout=90,
)

cookies = result["cookies"]
osid = [(c["domain"], c["name"]) for c in cookies if "OSID" in c.get("name", "")]
print("cookies:", len(cookies))
print("email:", result.get("email"))
print("csrf:", bool(result.get("csrf_token")), "| session_id:", bool(result.get("session_id")),
      "| build_label:", bool(result.get("build_label")))
print("OSID cookies:", osid)
if not osid:
    sys.exit("ABORT: no OSID cookies extracted — not saving. Is the account signed in on the NotebookLM tab?")

auth = AuthManager(profile)
auth.save_profile(
    cookies=cookies,
    csrf_token=result.get("csrf_token", ""),
    session_id=result.get("session_id", ""),
    email=result.get("email", ""),
    force=True,
    build_label=result.get("build_label", ""),
)
print(f"SAVED to profile {profile}:", auth.profile_dir)
print(f"Next: ./scripts/reseed-nlm-profile.sh {profile}")
