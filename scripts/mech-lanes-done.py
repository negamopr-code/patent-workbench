"""exit 0 only when all three v2 mechanism lanes have asked their whole pile."""
import json, os, sqlite3, sys
cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
out, done_all = [], True
for tab in (10, 13, 14):
    pg = f"/data/audits/mech_t{tab}_v2.progress.json"
    asked = len(json.load(open(pg))) if os.path.exists(pg) else 0
    pile = cx.execute("""select count(*) from documents where tab_id=? and status='fetched'
                         and nlm_screen_state='rejected'""", (tab,)).fetchone()[0]
    out.append(f"t{tab} {asked}/{pile}")
    done_all &= asked >= pile
print(" | ".join(out))
sys.exit(0 if done_all else 1)
