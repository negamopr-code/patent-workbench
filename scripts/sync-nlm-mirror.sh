#!/bin/sh
# Mirror docs/nlm-mirror/*.md into the NotebookLM notebook "patent benchmark match
# project" (profile: default). For each doc: delete the old source with the same
# title (if any), then add the current file content as a fresh source — NLM sources
# are immutable, so "update" = replace. Run from anywhere with the docker socket;
# the content is piped into patent-bench, which holds the NLM profile volume.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTEBOOK="${NLM_MIRROR_NOTEBOOK:-35690175-d37d-4de8-ac92-8254017063b5}"

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
