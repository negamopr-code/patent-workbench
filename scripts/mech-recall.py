"""Per-lane champion recall for every live mechanism lane.

A lane is (tab, tag[, states[, unread_only]]) and mirrors LANES in mech-watchdog.py exactly —
same pile definition, same ledger files — so recall is measured over the documents the lane
actually walks. Unread-only lanes have no opus score by construction (`score is null`), so for
them there is no champion set to recall: their yield is new picks, reported as such.
"""
import json, os, sqlite3

LANES = ((12, ""), (14, "grad", "graduate", True), (10, "v2"), (13, "v2"), (14, "v2"),
         (10, "v3"), (13, "v3"))

cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)


def parts(lane):
    return (lane[0], lane[1], (lane[2] if len(lane) > 2 else "rejected"),
            bool(lane[3]) if len(lane) > 3 else False)


for lane in LANES:
    tab, tag, states, unread = parts(lane)
    sfx = "_" + tag if tag else ""
    pg = f"/data/audits/mech_t{tab}{sfx}.progress.json"
    pk = f"/data/audits/mech_t{tab}{sfx}.picks.json"
    asked = set(json.load(open(pg))) if os.path.exists(pg) else set()
    picks = [p["number"] for p in (json.load(open(pk)) if os.path.exists(pk) else [])]
    st = [x.strip() for x in states.split(",") if x.strip()]
    ph = ",".join("?" * len(st))
    pile = cx.execute(
        "select count(*) from documents where tab_id=? and status='fetched' "
        "and nlm_screen_state in (%s)%s" % (ph, " and score is null" if unread else ""),
        (tab, *st)).fetchone()[0]
    scope = states + (" unread-only" if unread else "")
    print("\n== t%d%s [%s]  asked %d/%d  picks %d" % (tab, sfx, scope, len(asked), pile, len(picks)))
    if unread:
        print("   no opus scores in this pile — yield is %d new picks: %s"
              % (len(picks), ", ".join(picks) or "-"))
        continue
    champs = [(n, s) for n, s in cx.execute(
        "select number,score from documents where tab_id=? and score>=4 "
        "and nlm_screen_state in (%s) order by score desc" % ph, (tab, *st))]
    hit = [n for n, _ in champs if n in picks]
    seen = [n for n, _ in champs if n in asked]
    print("   champions %d | asked %d | RECOVERED %d -> %s"
          % (len(champs), len(seen), len(hit), ", ".join(hit) or "-"))
    for n, s in champs:
        print("     %-16s opus %-4s %s%s" % (n, s, "asked" if n in asked else "NOT ASKED",
                                             "  <-- PICKED" if n in picks else ""))
