#!/bin/sh
# Build + (re)deploy the nlm-keeper container on host port 8106 (noVNC).
#
# One persistent Chromium per Google account keeps the NotebookLM session
# alive; the daemon writes fresh cookies straight into the nlm-profile named
# volume (shared with patent-bench) and auto-resumes auth-interrupted
# mega-screens. Chrome profiles live in the named volume nlm-keeper-chrome —
# both volumes survive Docker Desktop / host restarts (host binds do NOT,
# see scripts/serve.sh history).
#
# ONE-TIME per account: open http://localhost:8106/vnc.html and sign in.
# ADD an account:
#   docker exec nlm-keeper sh -c 'echo work3 >> /home/app/chrome-profiles/accounts.conf'
#   docker restart nlm-keeper       # new Chromium appears; sign in via noVNC
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker build -t nlm-keeper "$ROOT/keeper"
docker rm -f nlm-keeper 2>/dev/null || true
docker run -d --name nlm-keeper --restart unless-stopped \
  -p 8106:6080 --shm-size=1g \
  -e PB_URL="${PB_URL:-http://host.docker.internal:8099}" \
  -e REFRESH_SECS="${REFRESH_SECS:-900}" \
  -v nlm-profile:/home/app/.notebooklm-mcp-cli \
  -v nlm-keeper-chrome:/home/app/chrome-profiles \
  nlm-keeper

echo "nlm-keeper: sign in / watch the browsers at http://localhost:8106/vnc.html"
echo "daemon log: docker logs -f nlm-keeper"
