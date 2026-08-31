#!/usr/bin/env python3
"""probe_text_audit — separate a COMPREHENSION failure from a DATA failure.

User point, 2026-08-31: if NotebookLM says "no fan disclosed" about a document that plainly has
one, that may not be the model failing to understand — the fan may simply not be in the text we
staged (bad OCR, a truncated description, a translation that dropped a section, claims-only
extraction). Those are opposite diagnoses with opposite fixes, and only the stored text can tell
them apart.

For every document in a probe set, report what the text we ACTUALLY SENT contains, so each of
NLM's per-document reasons can be scored as:
  - comprehension failure : the term is in the text, NLM said it is absent
  - data failure          : the term is genuinely not in the text we sent
  - correct               : NLM's reason matches what the text supports

  docker exec patent-bench python3 /data/probe_text_audit.py --tab 12 --docs-file <list.json>
"""
import argparse, json, re, sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--tab", type=int, default=12)
ap.add_argument("--docs-file", required=True)
a = ap.parse_args()

TERMS = {
    "fan":          r"\bfans?\b|\bblower|\bimpeller|\bventilat",
    "water-cool":   r"water[- ]cool|liquid[- ]cool|coolant|chilled water|water jacket",
    "condensation": r"condensat|dew\b|dew point|moisture|humid",
    "chiller":      r"\bchiller|refrigerat|cooling tower|heat exchanger",
    "jig":          r"\bjig\b|\btray\b|fixture|contact block|formation rack",
}
cx = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
nums = json.load(open(a.docs_file))
print("WHAT THE STAGED TEXT ACTUALLY CONTAINS  (tab %d, %d docs)" % (a.tab, len(nums)))
print("  doc               opus  chars   " + "  ".join(f"{k:>12}" for k in TERMS))
for num in nums:
    r = cx.execute("""select score, title, abstract, claims, description, digest
                      from documents where tab_id=? and number=?""", (a.tab, num)).fetchone()
    if not r:
        print("  %-16s NOT IN DB" % num); continue
    text = " ".join(x or "" for x in r[1:]).lower()
    hits = []
    for k, pat in TERMS.items():
        n = len(re.findall(pat, text))
        hits.append(f"{n:>12}" if n else f"{'-':>12}")
    flag = ""
    if len(text) < 4000:
        flag = "  <- THIN TEXT"
    if not re.search(r"[a-z]{4,}\s+[a-z]{4,}\s+[a-z]{4,}", text):
        flag += "  <- NOT PROSE (extraction may have failed)"
    print("  %-16s %-5s %6d  %s%s" % (num, r[0] if r[0] is not None else "-",
                                      len(text), "  ".join(hits), flag))
print("\n  counts are occurrences in the text we SENT to NotebookLM.")
print("  '-' means the concept is absent from our text: an NLM 'not disclosed' is then CORRECT,")
print("  and the real defect is upstream in fetching/extraction, not in the model.")
