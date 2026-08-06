# Patent Workbench web image — same proven shape as antimg-web:
# python-slim + Claude Code CLI (node) + nlm CLI baked into its own venv + poppler.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      nodejs npm poppler-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN python -m venv /opt/nlmvenv && /opt/nlmvenv/bin/pip install --no-cache-dir notebooklm-mcp-cli

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY src/ ./src/
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONPATH=/app/src \
    NLM_BIN=/opt/nlmvenv/bin/nlm \
    CLAUDE_BIN=claude \
    CLAUDE_CONFIG_DIR=/home/app/.claude \
    CLAUDE_SKILLS_DIR=/seed/skills \
    SKILLS_RW_DIR=/skills-rw \
    DISABLE_AUTOUPDATER=1 \
    PB_DB=/data/workbench.db \
    PB_UPLOADS=/data/uploads \
    PORT=8000 \
    WEB_CONCURRENCY=2

# .notebooklm-mcp-cli is created HERE so the nlm-profile named volume inherits
# app:app ownership on first use (docker copies mountpoint ownership into a
# fresh volume) — a root-owned profile dir kills every NLM call with a raw
# PermissionError (2026-08-06).
RUN useradd -m -u 1000 app && mkdir -p /data /home/app/.notebooklm-mcp-cli \
    && chown -R app:app /data /home/app
VOLUME /data
EXPOSE 8000
USER app
CMD ["/entrypoint.sh"]
