import json, os, sqlite3
cx=sqlite3.connect("file:/data/workbench.db?mode=ro",uri=True)
for tab,tag in ((12,""),(10,"v2"),(13,"v2")):
    sfx = "_"+tag if tag else ""
    pg=f"/data/audits/mech_t{tab}{sfx}.progress.json"; pk=f"/data/audits/mech_t{tab}{sfx}.picks.json"
    asked=set(json.load(open(pg))) if os.path.exists(pg) else set()
    picks=[p["number"] for p in (json.load(open(pk)) if os.path.exists(pk) else [])]
    champs=[(n,s) for n,s in cx.execute(
        "select number,score from documents where tab_id=? and score>=4 and nlm_screen_state='rejected' order by score desc",(tab,))]
    pile=cx.execute("select count(*) from documents where tab_id=? and status='fetched' and nlm_screen_state='rejected'",(tab,)).fetchone()[0]
    print("\n== t%d%s  asked %d/%d  picks %d" % (tab,sfx,len(asked),pile,len(picks)))
    hit=[n for n,_ in champs if n in picks]
    seen=[n for n,_ in champs if n in asked]
    print("   champions %d | asked %d | RECOVERED %d -> %s" % (len(champs), len(seen), len(hit), ", ".join(hit) or "-"))
    for n,s in champs:
        print("     %-16s opus %-4s %s%s" % (n, s, "asked" if n in asked else "NOT ASKED", "  <-- PICKED" if n in picks else ""))
