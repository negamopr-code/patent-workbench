"""exit 0 only when every mechanism lane has asked its whole pile."""
import json, os, sqlite3, sys

LANES = ((12, ""), (14, "grad", "graduate", True), (10, "v2"), (13, "v2"), (14, "v2"),
         (10, "v3"), (13, "v3"))

cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
out, done_all = [], True
for lane in LANES:
    tab, tag = lane[0], lane[1]
    states = lane[2] if len(lane) > 2 else "rejected"
    unread = bool(lane[3]) if len(lane) > 3 else False
    sfx = "_" + tag if tag else ""
    pg = f"/data/audits/mech_t{tab}{sfx}.progress.json"
    asked = len(json.load(open(pg))) if os.path.exists(pg) else 0
    st = [x.strip() for x in states.split(",") if x.strip()]
    pile = cx.execute(
        "select count(*) from documents where tab_id=? and status='fetched' "
        "and nlm_screen_state in (%s)%s" % (",".join("?" * len(st)),
                                            " and score is null" if unread else ""),
        (tab, *st)).fetchone()[0]
    out.append(f"t{tab}{sfx} {asked}/{pile}")
    done_all &= asked >= pile
print(" | ".join(out))
sys.exit(0 if done_all else 1)
