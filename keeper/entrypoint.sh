#!/bin/sh
# nlm-keeper entrypoint: Xvfb + openbox + VNC/noVNC + one Chromium per account
# (CDP ports 9301, 9302, … in accounts.conf order) + the keeper daemon.
set -eu

PROFILES_DIR=/home/app/chrome-profiles
ACCOUNTS_FILE="$PROFILES_DIR/accounts.conf"
mkdir -p "$PROFILES_DIR"
[ -f "$ACCOUNTS_FILE" ] || printf 'work2\n' > "$ACCOUNTS_FILE"

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
  chromium \
    --no-sandbox --disable-dev-shm-usage --disable-gpu \
    --user-data-dir="$PROFILES_DIR/$name" \
    --remote-debugging-port="$port" \
    --no-first-run --no-default-browser-check --start-maximized \
    "https://notebooklm.google.com" >/dev/null 2>&1 &
  echo "keeper: launched chromium for account '$name' on CDP :$port"
  i=$((i + 1))
done < "$ACCOUNTS_FILE"

exec /opt/venv/bin/python -u /opt/keeper/keeper_daemon.py
