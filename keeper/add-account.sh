#!/bin/sh
# Add a Google account to nlm-keeper: one command + one sign-in, ever.
#   ./keeper/add-account.sh work3
set -eu
NAME="${1:?usage: add-account.sh <profile-name>}"
docker exec nlm-keeper sh -c \
  "grep -qx '$NAME' /home/app/chrome-profiles/accounts.conf || echo '$NAME' >> /home/app/chrome-profiles/accounts.conf"
docker restart nlm-keeper >/dev/null
echo "Account '$NAME' added. Sign in ONCE at http://localhost:8106/vnc.html —"
echo "the daemon focuses the window that still needs a login and takes it from there."
