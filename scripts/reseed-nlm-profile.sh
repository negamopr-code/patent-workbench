#!/bin/sh
# Re-seed the NotebookLM auth profile into the patent-bench container.
#
# WHY THIS EXISTS
# --------------
# patent-bench mounts the NLM profile via:
#     -v /root/claude-sandbox/persistent/nlm-profile:/home/app/.notebooklm-mcp-cli
# but on this Docker-Desktop-on-WSL2 host that host path resolves (through the
# daemon's bind layer) to a DIFFERENT, empty directory than the populated
# profile the claude dev container sees at /home/node/.notebooklm-mcp-cli.
# Result: nlm inside patent-bench reports "Profile 'default' not found" and the
# in-app NotebookLM query crashes with a raw traceback at
#   notebooklm_tools/cli/commands/notebook.py:171  (get_client -> profile_exists).
#
# This script copies the working profile (cookies.json + metadata.json) from the
# canonical claude-container location into patent-bench and fixes ownership.
# The files land in patent-bench's real bind dir, which is keyed off the -v path
# string, so they SURVIVE `scripts/serve.sh` rebuilds. Re-run only when the
# cookies expire (NLM starts failing again with the same traceback).
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
