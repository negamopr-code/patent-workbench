#!/bin/sh
# sweep-watcher — re-arms interrupted claims-audit sweeps (P4: watchdog dies without re-arm).
# Runs INSIDE patent-bench (survives Claude-session crashes):
#   docker cp scripts/sweep-watcher.sh patent-bench:/data/sweep-watcher.sh
#   docker exec -d patent-bench sh /data/sweep-watcher.sh 10 13
# Log: /data/.sweep_watcher.log
API=http://127.0.0.1:8000
LOG=/data/.sweep_watcher.log
TABS="${*:-10 13}"
echo "$(date -u +%FT%TZ) watcher armed for tabs: $TABS" >> "$LOG"
while :; do
  for t in $TABS; do
    st=$(curl -s --max-time 20 "$API/api/tabs/$t/claims-audit/status") || continue
    running=$(printf %s "$st" | grep -o '"running":[a-z]*' | cut -d: -f2)
    resumable=$(printf %s "$st" | grep -o '"resumable":[a-z]*' | cut -d: -f2)
    phase=$(printf %s "$st" | grep -o '"phase":"[^"]*"' | cut -d'"' -f4)
    if [ "$running" != "true" ] && [ "$resumable" = "true" ]; then
      r=$(curl -s --max-time 30 -X POST "$API/api/tabs/$t/claims-audit" \
            -H 'Content-Type: application/json' -d '{"resume":true}')
      echo "$(date -u +%FT%TZ) tab $t phase=$phase → resume: $(printf %s "$r" | head -c 200)" >> "$LOG"
    fi
  done
  sleep 300
done
