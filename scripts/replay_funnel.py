#!/usr/bin/env python3
"""Offline replay of funnel-ranking variants against opus-labeled ground truth.

Runs INSIDE patent-bench (read-only: /data/workbench.db + a stored claims-state
JSON) — never touches the running server:

    docker exec -i patent-bench python3 - <TAB> <CLAIMS_JSON> < scripts/replay_funnel.py

Measures, for the CURRENT ranking (total claimed weight) vs the HEAVY-FIRST
ranking (fix #1: w>=HEAVY_W features rank, light weight tie-break):
  - each opus champion's (score >= CHAMP_MIN) position in the read queue
  - the minimal queue prefix that recalls ALL champions
  - top-band contamination (opus-read docs in the band scoring < CHAMP_MIN)
And replays the translation guard (fix #3): CJK-origin docs whose UNVERIFIED
(quote-failed) claim weight is heavy but verified weight died.
"""
import json
import sqlite3
import sys

HEAVY_W = 4          # doctrine: features w>=4 are "heavy"
CHAMP_MIN = 4.0      # opus score >= 4.0 == champion (t12 ladder convention)
CJK = ("KR", "CN", "JP", "TW")
GUARD_UNVER_MIN = 4  # guard fires when quote-killed weight alone is heavy
GUARD_VER_MAX = 2    # ... and the surviving verified weight is noise-tier

OK = ("verified", "fuzzy", "claimed")


def main(tab_id: int, claims_path: str, db_path: str = "/data/workbench.db") -> None:
    st = json.load(open(claims_path))
    must = st.get("must") or []
    weights = {str(i): w for i, (_n, w) in enumerate(must, 1)}
    heavy_keys = {k for k, w in weights.items() if w >= HEAVY_W}
    claims = st.get("claims") or {}

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    docs = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, number, score, score_model FROM documents WHERE tab_id=?",
        (tab_id,))}
    champs = {d["id"]: d for d in docs.values()
              if d["score"] is not None and d["score"] >= CHAMP_MIN
              and "opus" in (d["score_model"] or "")}

    rows = []
    for did_s, feats in claims.items():
        did = int(did_s)
        if did not in docs:
            continue
        ok = {k for k, sv in feats.items() if sv[0] in OK}
        heavy = sum(weights.get(k, 0) for k in ok & heavy_keys)
        light = sum(weights.get(k, 0) for k in ok - heavy_keys)
        unver = sum(weights.get(k, 0) for k, sv in feats.items()
                    if sv[0] == "unverified")
        rows.append({"id": did, "number": docs[did]["number"], "heavy": heavy,
                     "light": light, "score": heavy + light, "unver": unver,
                     "crown": "1" in ok, "n_ok": len(ok)})

    def report(name, order):
        pos = {r["id"]: i + 1 for i, r in enumerate(order)}
        cpos = sorted(((pos.get(cid), champs[cid]["number"]) for cid in champs),
                      key=lambda t: (t[0] is None, t[0] or 0, t[1]))
        missing = [n for p, n in cpos if p is None]
        k_all = max((p for p, _n in cpos if p is not None), default=0)
        print(f"\n== {name} ==  ({len(order)} ranked docs)")
        print("champion positions:",
              ", ".join(f"{n}@{p if p else 'MISS'}" for p, n in cpos))
        if missing:
            print(f"⚠ NOT IN RANKING at all ({len(missing)}): {missing}")
        print(f"queue prefix for full champion recall: K = {k_all}"
              + (f"  (of ranked; +{len(missing)} unreachable)" if missing else ""))
        for band in (20, 44):
            top = order[:band]
            read = [r for r in top if docs[r["id"]]["score"] is not None
                    and "opus" in (docs[r["id"]]["score_model"] or "")]
            bad = [r for r in read if docs[r["id"]]["score"] < CHAMP_MIN]
            got = [r for r in top if r["id"] in champs]
            print(f"top-{band}: {len(got)}/{len(champs)} champions; "
                  f"contamination {len(bad)}/{len(read)} of opus-read"
                  f" = {100 * len(bad) / len(read):.0f}%" if read else
                  f"top-{band}: {len(got)}/{len(champs)} champions; no opus reads")

    report("CURRENT (total weight)",
           sorted(rows, key=lambda r: (-r["score"], not r["crown"],
                                       -r["n_ok"], r["id"])))
    report("FIX #1 (heavy-first, light tie-break)",
           sorted(rows, key=lambda r: (-r["heavy"], -r["light"],
                                       not r["crown"], -r["n_ok"], r["id"])))

    print("\n== FIX #3 translation-guard replay ==")
    flagged = [r for r in rows
               if r["number"][:2] in CJK and r["unver"] >= GUARD_UNVER_MIN
               and r["score"] <= GUARD_VER_MAX]
    ch_flag = [r for r in flagged if r["id"] in champs]
    print(f"flagged (CJK, unverified w>={GUARD_UNVER_MIN}, verified w<="
          f"{GUARD_VER_MAX}): {len(flagged)} doc(s) -> opus probes")
    for r in sorted(flagged, key=lambda r: -r["unver"]):
        d = docs[r["id"]]
        tag = (f"opus={d['score']}" if d["score"] is not None
               and "opus" in (d["score_model"] or "") else "unread")
        print(f"  {r['number']:>16}  unver={r['unver']:>2} verified={r['score']:>2}"
              f"  {tag}{'  🏆' if r['id'] in champs else ''}")
    print(f"guard recall: {len(ch_flag)} champion(s) saved"
          f" ({', '.join(d['number'] for d in ch_flag)})" if ch_flag else
          "guard recall: 0 champions in flagged set")
    n_champ_cjk_killed = [c["number"] for cid, c in champs.items()
                          if c["number"][:2] in CJK
                          and not any(r["id"] == cid and r["score"] > GUARD_VER_MAX
                                      for r in rows)
                          and not any(r["id"] == cid for r in flagged)]
    if n_champ_cjk_killed:
        print(f"⚠ CJK champions quote-killed but NOT caught by guard: "
              f"{n_champ_cjk_killed}")


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2],
         *( [sys.argv[3]] if len(sys.argv) > 3 else [] ))
