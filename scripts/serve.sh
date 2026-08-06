#!/bin/sh
# Build + (re)deploy the Patent Workbench container on host port 8099.
# Works both from the host and from inside the claude dev container (docker
# socket is mounted; the build context is streamed by the docker CLI, while the
# -v bind mounts below are HOST paths resolved by the daemon).
#
# NLM profile lives in the NAMED VOLUME `nlm-profile` (NOT a host bind): on this
# Docker-Desktop-on-WSL2 host, /root/... bind paths materialize as
# docker-desktop-bind-mounts dirs that are WIPED on Docker Desktop/host restarts
# (bit 2026-08-06: mega-screen round 13 died on a root-owned empty profile).
# Named volumes survive those restarts (patent-bench-data proved it same day).
# First-time seed / cookie refresh: scripts/reseed-nlm-profile.sh.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker build -f "$ROOT/deploy/Dockerfile" -t patent-bench "$ROOT"
docker rm -f patent-bench 2>/dev/null || true
docker run -d --name patent-bench --restart unless-stopped -p 8099:8000 \
  -e PB_AUTO_FIGURES="${PB_AUTO_FIGURES:-0}" \
  -e PB_REDUCE_TIMEOUT="${PB_REDUCE_TIMEOUT:-1800}" \
  -v patent-bench-data:/data \
  -v /root/.claude:/seed:ro \
  -v /root/.claude/skills:/skills-rw \
  -v nlm-profile:/home/app/.notebooklm-mcp-cli \
  patent-bench

echo "Patent Workbench: http://localhost:8099/"
