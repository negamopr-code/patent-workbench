#!/bin/sh
# Re-seed the NotebookLM auth profile into the patent-bench container.
#
# WHY THIS EXISTS
# --------------
# patent-bench keeps the NLM profile in the NAMED VOLUME `nlm-profile`
# (serve.sh). The volume starts EMPTY, so it must be seeded ONCE from the
# canonical profile the claude dev container sees at
# /home/node/.notebooklm-mcp-cli — and re-seeded only when the cookies expire
# (nlm starts failing with "Profile 'default' not found" or auth errors).
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

SRC="/home/node/.notebooklm-mcp-cli/profiles/default"
CT="patent-bench"

[ -f "$SRC/cookies.json" ] || { echo "ERROR: no source profile at $SRC — log in with nlm first."; exit 1; }
docker ps --format '{{.Names}}' | grep -qx "$CT" || { echo "ERROR: container '$CT' is not running."; exit 1; }

cd "$SRC"
tar c cookies.json metadata.json | docker exec -u 0 -i "$CT" sh -c '
  mkdir -p /home/app/.notebooklm-mcp-cli/profiles/default &&
  tar x -C /home/app/.notebooklm-mcp-cli/profiles/default &&
  chown -R 1000:1000 /home/app/.notebooklm-mcp-cli &&
  chmod 700 /home/app/.notebooklm-mcp-cli /home/app/.notebooklm-mcp-cli/profiles /home/app/.notebooklm-mcp-cli/profiles/default &&
  chmod 600 /home/app/.notebooklm-mcp-cli/profiles/default/*'

echo "Re-seeded NLM profile into $CT. Validate with:"
echo "  docker exec $CT /opt/nlmvenv/bin/nlm notebook query <notebook-id> 'ping' --json"
