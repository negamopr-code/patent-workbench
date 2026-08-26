#!/bin/sh
# Mirror docs/nlm-mirror/*.md into the NotebookLM notebook "patent benchmark match
# project" (profile: default). For each doc: delete the old source with the same
# title (if any), then add the current file content as a fresh source — NLM sources
# are immutable, so "update" = replace. Run from anywhere with the docker socket;
# the content is piped into patent-bench, which holds the NLM profile volume.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTEBOOK="${NLM_MIRROR_NOTEBOOK:-35690175-d37d-4de8-ac92-8254017063b5}"

# NLM account gate (feedback_nlm_account_gate, 2026-08-25): this is a WRITE on the
# default account; a second concurrent writer while t11/t13 screen is the F3c
# timed-out-add mechanism (A2 breach logged 2026-08-26 15:07). Refuse unless the
# gate passes; NLM_MIRROR_FORCE=1 overrides deliberately.
if [ "${NLM_MIRROR_FORCE:-0}" != "1" ]; then
  if ! docker exec -w /app patent-bench python3 - < "$ROOT/scripts/audit_accounts.py"; then
    echo "sync-nlm-mirror: account gate FAILED — not writing to NotebookLM (NLM_MIRROR_FORCE=1 to override)" >&2
    exit 2
  fi
  # gate passes when no job runs on the default account; an active default-account
  # screen is still a concurrent writer for us, so check that separately
  busy=$(docker exec patent-bench python3 -c '
import urllib.request, json
for t in (11, 13):
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:8000/api/tabs/{t}/nlm-screen/status", timeout=10))
    if d.get("running"): print(t)
' 2>/dev/null)
  if [ -n "$busy" ]; then
    echo "sync-nlm-mirror: default-account screen running on t$busy — refusing concurrent write (NLM_MIRROR_FORCE=1 to override)" >&2
    exit 2
  fi
fi

sync_one() {
  file="$1"; title="$2"
  docker exec -i -e NB="$NOTEBOOK" -e TITLE="$title" patent-bench python3 -c '
import sys, os
sys.path.insert(0, "/app/src")
from patentbench import nlm_bridge as nb
nbid, title = os.environ["NB"], os.environ["TITLE"]
text = sys.stdin.read()
srcs = (nb.list_sources(nbid, force=True, profile="default") or {}).get("sources") or []
old = [s["id"] for s in srcs if (s.get("title") or "").strip() == title]
if old:
    print("  replacing", title, old, nb.delete_source(old, notebook_id=nbid, profile="default"))
r = nb.add_source_text(nbid, title, text, profile="default")
print("  added:", r)
' < "$file"
}

sync_one "$ROOT/docs/nlm-mirror/discussion-journal.md" "Discussion journal"
sync_one "$ROOT/docs/nlm-mirror/skill-lessons.md"      "Skill and lessons learned"
sync_one "$ROOT/docs/nlm-mirror/deferred-features.md"  "Deferred features list"
echo "synced -> notebook $NOTEBOOK (patent benchmark match project)"
