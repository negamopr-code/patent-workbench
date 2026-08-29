#!/usr/bin/env python3
"""The truncation NO-GO invariant at PART granularity.

Before 2026-08-29 `_restage_missing_parts` discarded `add_source_text`'s result, so a tail part
rejected at NotebookLM's 50-source cap left the document looking present (part 1 keeps the
canonical title) and the screen questioned it with a blind tail — the exact failure the
full-doc-staging auditor kept reporting while the root cause stayed in the deployed code.

Runs with plain python3, no pytest and no app import: the functions under test are exec'd out
of the source against stubs, so this never opens the live DB or disturbs a running screen.
    python3 tests/test_restage_missing_parts.py
"""
import pathlib, re, sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src/patentbench/web/api.py"
text = SRC.read_text()


def grab(name):
    m = re.search(rf"^def {name}\(.*?(?=^def |^@app)", text, re.S | re.M)
    assert m, f"{name} not found in {SRC}"
    return m.group(0)


CLIP = int(re.search(r"^SOURCE_CLIP_BYTES\s*=\s*([\d_]+)", text, re.M).group(1).replace("_", "")) \
    if re.search(r"^SOURCE_CLIP_BYTES", text, re.M) else 118_000

env = {"__name__": "stub"}
for const in re.findall(r"^[A-Z_]+\s*=\s*[\d_]+$", text, re.M):
    exec(const, env)


class Bridge:
    def __init__(self, results):
        self.results, self.n = results, 0

    def add_source_text(self, nb, title, body, profile=None):
        r = self.results[self.n] if self.n < len(self.results) else True
        self.n += 1
        return {"ok": r}


env["nlm_bridge"] = Bridge([])
exec(grab("_doc_source_text"), env)
exec(grab("_doc_source_parts"), env)
exec(grab("_restage_missing_parts"), env)

DOC = {"number": "X1", "title": "t", "abstract": "a", "claims": "c",
       "description": "d" * 400_000, "digest": "", "figures": None}
parts = env["_doc_source_parts"](DOC)
assert len(parts) > 1, f"fixture must split into multiple parts, got {len(parts)}"
head = {parts[0][0]}                       # only part 1 present: the blind-tail shape
tail_n = len(parts) - 1

fails = []


def check(label, results, present, want):
    env["nlm_bridge"] = Bridge(results)
    got = env["_restage_missing_parts"]("nb", DOC, None, present)
    ok = want(got)
    print(f"  {'OK  ' if ok else 'FAIL'} {label:<46} -> {got}")
    if not ok:
        fails.append(label)


check("every part lands  -> positive count", [True] * 40, head, lambda n: n == tail_n > 0)
check("tail rejected     -> NEGATIVE, not success", [False] * 40, head,
      lambda n: n < 0 and abs(n) == tail_n)
check("transient reject  -> retry recovers it", [False, True] * 40, head,
      lambda n: n == tail_n > 0)
check("nothing missing   -> no-op, no calls", [], {t for t, _ in parts}, lambda n: n == 0)

print(f"\n{len(parts)} parts in fixture; {'ALL PASS' if not fails else 'FAILURES: ' + str(fails)}")
sys.exit(1 if fails else 0)
