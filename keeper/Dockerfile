# nlm-keeper — always-on NotebookLM session keeper.
#
# WHY: Google kills fresh accounts' sessions server-side within ~40-45 min
# unless a real browser keeps rotating the cookies (bit work2 repeatedly,
# 2026-08-08/09). The old workaround — a debug Chrome on the Windows host +
# netsh portproxy + manual CDP re-extract — died on every host reboot and
# does not scale past one extra account.
#
# THIS container holds one persistent Chromium per Google account (profiles in
# the named volume nlm-keeper-chrome), shows them via noVNC for the ONE-TIME
# login, and runs a daemon that every REFRESH_SECS re-extracts cookies via CDP
# and writes them straight into the shared nlm-profile volume (the same volume
# patent-bench reads) — no reseed step — then auto-resumes any mega-screen that
# was interrupted by "Authentication expired".
#
# Add an account: echo NAME >> accounts.conf (in the nlm-keeper-chrome volume),
# docker restart nlm-keeper, sign in once at http://localhost:8106/vnc.html.
# See keeper/serve.sh.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc openbox novnc websockify \
      python3 python3-venv ca-certificates curl fonts-liberation procps \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir notebooklm-mcp-cli==0.7.2

RUN useradd -m -u 1000 app \
    && mkdir -p /home/app/chrome-profiles /home/app/.notebooklm-mcp-cli \
    && chown -R app:app /home/app

COPY entrypoint.sh keeper_daemon.py /opt/keeper/
RUN chmod +x /opt/keeper/entrypoint.sh

USER app
ENV HOME=/home/app
EXPOSE 6080
ENTRYPOINT ["/opt/keeper/entrypoint.sh"]
