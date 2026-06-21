#!/bin/sh
# Seed Claude CLI credentials from a READ-ONLY /seed mount of the operator's ~/.claude,
# then start the web server. Copying (not mounting) keeps the container's `claude` from
# ever writing back into the host's live config (same pattern as antimg-web).
# Without /seed the Claude chat reports "credentials not seeded"; documents +
# NotebookLM Q&A keep working.
set -eu

mkdir -p "${CLAUDE_CONFIG_DIR:?}"
if [ -f /seed/.credentials.json ]; then
  cp /seed/.credentials.json "$CLAUDE_CONFIG_DIR/.credentials.json" \
    && echo "entrypoint: seeded claude credentials" \
    || echo "entrypoint: WARN could not seed credentials (Claude chat unavailable)"
else
  echo "entrypoint: no /seed credentials — Claude chat unavailable (mount /root/.claude:ro at /seed)"
fi

# Background-job locks (deep-read assessment, NLM rating) track IN-MEMORY worker
# threads, which do NOT survive a container/process restart. A fresh start means
# no job is running by definition, so any lingering lock/pause file is stale —
# left behind by a job whose thread was killed by a rebuild. Clear them here, once,
# BEFORE gunicorn forks its workers: otherwise the app reports a phantom
# "running"/"paused" job (until the multi-hour lock TTL), refuses to start new
# work, and the UI is wedged showing no progress. Scores already earned are safe in
# the DB — ▶️ Continue resumes the rest.
DATA_DIR="$(dirname "${PB_DB:-/data/workbench.db}")"
rm -f "$DATA_DIR"/.claude_read_*.lock \
      "$DATA_DIR"/.claude_read_*.pause \
      "$DATA_DIR"/.nlm_rate_*.lock 2>/dev/null || true
echo "entrypoint: cleared stale background-job locks in $DATA_DIR"

exec gunicorn patentbench.web.api:app -k uvicorn.workers.UvicornWorker \
  -b "0.0.0.0:${PORT}" -w "${WEB_CONCURRENCY}" --timeout 900 \
  --access-logfile - --error-logfile -
