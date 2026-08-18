#!/bin/sh
# nlm-keeper entrypoint: Xvfb + openbox + VNC/noVNC + one Chromium per account
# (CDP ports 9301, 9302, … in accounts.conf order) + the keeper daemon.
#
# BOOT-QUARANTINE (deferred #13; shutdown test 2026-08-14): a rebooted Chromium
# that touches Google with stale on-disk cookies makes Google invalidate the
# whole session FAMILY — including the still-valid CLI snapshot the audits run
# on. So every browser boots BLOCKED from Google (resolver rule) on about:blank
# with a .quarantine marker; the daemon probes the CLI snapshot instead and only
# "wakes" the browser (relaunch without the block) when the CLI is dead or a
# .wake file asks for it. While the CLI snapshot is alive, a host restart needs
# ZERO logins.
set -eu

PROFILES_DIR=/home/app/chrome-profiles
ACCOUNTS_FILE="$PROFILES_DIR/accounts.conf"
mkdir -p "$PROFILES_DIR"
[ -f "$ACCOUNTS_FILE" ] || printf 'work2\n' > "$ACCOUNTS_FILE"

# `docker restart` keeps the writable layer, so a killed Xvfb leaves its lock
# behind and every subsequent boot dies with "Server is already active for
# display 99" (3-day crash loop, 2026-08-15..18). Same class as the Chromium
# SingletonLock cleanup below: only one X server ever runs here, always safe.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp &
export DISPLAY=:99
sleep 1
openbox &
x11vnc -display :99 -forever -shared -nopw -localhost -quiet -bg
websockify --web /usr/share/novnc 6080 localhost:5900 &

i=0
while read -r name; do
  case "$name" in ''|'#'*) continue ;; esac
  port=$((9301 + i))
  # A previous container's hostname lives in SingletonLock and makes Chromium
  # abort with "profile in use on another computer" — only one Chromium per
  # profile runs here, so the stale locks are always safe to clear.
  rm -f "$PROFILES_DIR/$name/SingletonLock" \
        "$PROFILES_DIR/$name/SingletonSocket" \
        "$PROFILES_DIR/$name/SingletonCookie"
  # Persist session-scoped cookies to disk ("continue where you left off" +
  # clean exit_type): a gracefully-stopped browser then has a real chance of
  # still being logged in at the quarantine lift instead of guaranteed-dead.
  python3 - "$PROFILES_DIR/$name" <<'PY' || true
import json, os, sys
p = os.path.join(sys.argv[1], "Default", "Preferences")
try:
    prefs = json.load(open(p))
except (OSError, ValueError):
    sys.exit(0)
prefs.setdefault("session", {})["restore_on_startup"] = 1
prefs.setdefault("profile", {})["exit_type"] = "Normal"
tmp = p + ".tmp"
json.dump(prefs, open(tmp, "w"))
os.replace(tmp, p)
PY
  touch "$PROFILES_DIR/$name.quarantine"
  chromium \
    --no-sandbox --disable-dev-shm-usage --disable-gpu \
    --user-data-dir="$PROFILES_DIR/$name" \
    --remote-debugging-port="$port" \
    --host-resolver-rules="MAP *.google.com 127.0.0.1" \
    --no-first-run --no-default-browser-check --start-maximized \
    "about:blank" >/dev/null 2>&1 &
  echo "keeper: launched chromium for account '$name' on CDP :$port (QUARANTINED — Google blocked until CLI dies or .wake)"
  i=$((i + 1))
done < "$ACCOUNTS_FILE"

exec /opt/venv/bin/python -u /opt/keeper/keeper_daemon.py
