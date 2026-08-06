#!/bin/sh
# Re-seed a NotebookLM auth profile into the patent-bench container.
#
#   ./reseed-nlm-profile.sh              # seed the default profile (as always)
#   ./reseed-nlm-profile.sh work2        # seed a SECOND account as profile 'work2'
#
# WHY THIS EXISTS
# --------------
# patent-bench keeps the NLM profiles in the NAMED VOLUME `nlm-profile`
# (serve.sh). The volume starts EMPTY, so each profile must be seeded ONCE from
# the canonical profile dir the claude dev container sees at
# /home/node/.notebooklm-mcp-cli — and re-seeded only when the cookies expire
# (nlm starts failing with "Profile '<name>' not found" or auth errors).
#
# PER-TAB ACCOUNTS (2026-08-06): a tab can pin a named profile (its own Google
# account → its own quota pool + ~100-notebook cap). To seed a second account:
#   1. In the claude dev container: nlm login --profile work2   (log into the
#      OTHER Google account; --clear first if Chrome clings to the old one)
#   2. ./reseed-nlm-profile.sh work2
#   3. Pick the account in the tab's 🔗 Notebook dialog (visible once 2+
#      profiles exist; locked after the tab's first notebook/screen).
#
# History: until 2026-08-06 this was a /root/claude-sandbox/... host bind. On
# Docker-Desktop-on-WSL2 such binds materialize as docker-desktop-bind-mounts
# dirs that are WIPED (root-owned, empty) on Docker Desktop/host restarts —
# which killed the mega-screen mid-round with a raw PermissionError. The named
# volume survives those restarts; this script is now first-seed + cookie-refresh
# only, not a recurring recovery step.
#
# Run from INSIDE the claude dev container (docker socket is mounted).
set -eu

PROFILE="${1:-default}"
SRC="/home/node/.notebooklm-mcp-cli/profiles/$PROFILE"
CT="patent-bench"

[ -f "$SRC/cookies.json" ] || { echo "ERROR: no source profile at $SRC — log in with 'nlm login --profile $PROFILE' first."; exit 1; }
docker ps --format '{{.Names}}' | grep -qx "$CT" || { echo "ERROR: container '$CT' is not running."; exit 1; }

cd "$SRC"
tar c cookies.json metadata.json | docker exec -u 0 -i -e PROFILE="$PROFILE" "$CT" sh -c '
  mkdir -p "/home/app/.notebooklm-mcp-cli/profiles/$PROFILE" &&
  tar x -C "/home/app/.notebooklm-mcp-cli/profiles/$PROFILE" &&
  chown -R 1000:1000 /home/app/.notebooklm-mcp-cli &&
  chmod 700 /home/app/.notebooklm-mcp-cli /home/app/.notebooklm-mcp-cli/profiles "/home/app/.notebooklm-mcp-cli/profiles/$PROFILE" &&
  chmod 600 "/home/app/.notebooklm-mcp-cli/profiles/$PROFILE"/*'

echo "Re-seeded NLM profile '$PROFILE' into $CT. Validate with:"
echo "  docker exec $CT /opt/nlmvenv/bin/nlm notebook list --profile $PROFILE"
