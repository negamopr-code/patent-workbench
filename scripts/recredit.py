#!/usr/bin/env python3
"""recredit — re-run the credit gate over PERSISTED chunk evidence, with no NLM call at all.

This is why the runners persist every answer. When the gate itself is found to be wrong — as on
2026-08-29, when NotebookLM switched to an enumerated reply format ("**1 (AU2016285501)**: ...")
and the anchor regex, which demanded the number immediately before the separator, threw away 10
genuine feature grids — the fix must not cost a second round of quota. Re-score the evidence on
disk and rebuild the progress files.

Rebuilds <lane>_t<tab>.progress.json from /data/audits/<lane>/t<tab>_*.json.
  docker exec patent-bench python3 /data/recredit.py --lane untreated [--apply]
Without --apply it only reports the delta.
"""
import argparse, glob, json, os, re, sys

AUD = "/data/audits"


def addressed(n, txt):
    m = re.search(rf"{re.escape(n)}" + r"\s*[)\]]?\s*\**\s*[:\-—]", txt or "", re.IGNORECASE)
    if not m:
        return False
    return bool(re.search(r"\**F\s*\d+\s*\**\s*=\s*\**\s*(YES|NO|PARTIAL)",
                          (txt or "")[m.end():m.end() + 200], re.IGNORECASE))


def index_map(ev):
    """NotebookLM sometimes heads each block with a bare ORDINAL instead of the publication
    number ("1: F1=NO F2=NO ..."), leaving the gate's number-anchor nothing to match — ten valid
    grids were discarded that way on t14 slowrej chunk 3 (2026-08-31) and on the paired test.

    The blocks are the documents in the order the question listed them, so the mapping is
    recoverable. It is only applied under a strict guard: the number of ordinal blocks must
    EQUAL the number of documents asked, and no block may already carry a publication number.
    Anything else returns {} and the chunk stays uncredited rather than risk mis-attributing a
    grid to the wrong document.
    """
    ans = (ev.get("answers") or {}).get("_consolidated") or ""
    docs = [d for d in (ev.get("docs") or ev.get("asked") or [])]
    if not docs:
        return {}
    if any(re.search(rf"\**{re.escape(n)}\**\s*[)\]]?\s*\**\s*[:\-—]", ans) for n in docs):
        return {}                       # already number-keyed; nothing to remap
    idx = sorted({int(m) for m in re.findall(r"^(\d+)\s*[:.]\s*\**F\s*1\s*=", ans, re.M)})
    if idx != list(range(1, len(docs) + 1)):
        return {}                       # not a clean 1..N enumeration — refuse to guess
    return {i: docs[i - 1] for i in idx}


def credited(ev):
    """Same rules as the runner: the broad question must have landed, the reply must not have
    rebuilt its own checklist, every part of the doc must be in the live inventory, and the doc
    must carry its own grid."""
    ans = ev.get("answers") or {}
    broad = ans.get("_broad") or ""
    ok = (not ev.get("broad_failed")) and bool(broad.strip()) and '"status": "error"' not in broad
    cons = ans.get("_consolidated") or ""
    if ("not explicitly provided" in cons) or ("reconstructed checklist" in cons):
        ok = False
    if not ok:
        return set(), "broad/checklist gate failed"
    po, inv = ev.get("parts_ok") or {}, set(ev.get("source_inventory") or [])
    out = set()
    # index-keyed recovery: attribute each ordinal block to the doc the question listed there
    imap = index_map(ev)
    for i, num in imap.items():
        m = re.search(rf"^{i}\s*[:.]\s*(.*)$", cons, re.M)
        if not m:
            continue
        if not re.search(r"F\s*\d+\s*=\s*(YES|NO|PARTIAL)", m.group(1), re.IGNORECASE):
            continue
        p = po.get(num) or {}
        if not p.get("want") or p.get("ok") != p.get("want"):
            continue
        if inv and len([t for t in inv if t and t.startswith(num)]) < p["want"]:
            continue
        out.add(num)
    for num, v in (ans or {}).items():
        if num.startswith("_") or not v or v == "QUOTA-ABORT":
            continue
        p = po.get(num) or {}
        if not p.get("want") or p.get("ok") != p.get("want"):
            continue
        if inv and len([t for t in inv if t and t.startswith(num)]) < p["want"]:
            continue
        if addressed(num, v):
            out.add(num)
    return out, "ok"


ap = argparse.ArgumentParser()
ap.add_argument("--lane", default="untreated")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

by_tab = {}
for path in sorted(glob.glob(f"{AUD}/{a.lane}/t*_*.json")):
    tab = int(os.path.basename(path).split("_")[0][1:])
    try:
        ev = json.load(open(path))
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! unreadable {path}: {e}"); continue
    got, why = credited(ev)
    by_tab.setdefault(tab, set()).update(got)
    if not got:
        print(f"  t{tab} {os.path.basename(path)}: 0 credited ({why}, "
              f"wording={ev.get('wording', 'verbatim')})")

print(f"\n{'tab':<5} {'was':<6} {'now':<6} {'recovered'}")
for tab in sorted(by_tab):
    prog = f"{AUD}/{a.lane}_t{tab}.progress.json"
    was = set(json.load(open(prog))) if os.path.exists(prog) else set()
    now = was | by_tab[tab]
    print(f"t{tab:<4} {len(was):<6} {len(now):<6} +{len(now) - len(was)}")
    if a.apply and now != was:
        json.dump(sorted(now), open(prog, "w"))
print("\n(applied)" if a.apply else "\n(dry run — pass --apply to write the progress files)")
