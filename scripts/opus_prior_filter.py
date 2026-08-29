#!/usr/bin/env python3
"""opus_prior_filter — rank the never-read documents by how much they look like the documents
opus ALREADY scored highly, so a tiny opus budget is spent where it can actually pay.

Why this and not the old lexical lane: `lex_lane_t13.py` ranked documents by TF-IDF similarity
to the BENCHMARK TEXT. It had no idea what opus rewards, and it failed its own R5 control
(a known-positive landed at rank 1696 of a 200-deep queue), so its queue was never trustable
as coverage. We now hold 4366 opus-read documents with 109 scored >= 4. That is a labelled set,
so the lane can be SUPERVISED and, more importantly, VALIDATED: hold each known positive out,
rank it against every known negative, and see where it lands. A lane that cannot re-find the
positives it was trained on must not be trusted to find new ones.

Signals (all computed from text that exists BEFORE any read — title/abstract/claims/description;
never from verdict, score_note or feature_scores, which are read OUTPUTS and would leak):
  lo   log-odds of each term appearing in a high-scoring doc vs a low-scoring one, within the tab
  bm   cosine to the benchmark text + MUST feature names (the old lane's signal, kept as a check)
Both are per-tab: vocabularies and benchmarks do not transfer between tabs.

Read-only, pure stdlib, zero NLM quota, zero Claude tokens.
  docker exec patent-bench python3 /data/opus_prior_filter.py --validate
  docker exec patent-bench python3 /data/opus_prior_filter.py --emit 3
"""
import argparse, json, math, re, sqlite3, sys
from collections import Counter, defaultdict

DB = "file:/data/workbench.db?mode=ro"
TABS = (12, 13, 14)          # t10 is fully opus-read: nothing to filter there
POS, NEG = 4.0, 2.0          # score >= POS is a positive; <= NEG a clear negative; between = ignored
STOP = set(("the a an and or of to in for with is are be by on at as from that this it its said "
            "wherein claim claims comprising comprises device method system first second one "
            "plurality least according invention embodiment present disclosure may can which "
            "such each other when than has have been also thereof therein said configured "
            "unit part member portion means module apparatus").split())


def tok(s):
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", (s or "").lower()) if w not in STOP]


def doc_terms(ab, cl, de, ti):
    """Presence-set of terms. Claims and title are weighted by repetition: a term in the claims
    is what the document actually protects, which is what a prior-art read turns on."""
    t = tok(ti) * 3 + tok(ab) * 2 + tok(cl) * 3 + tok((de or "")[:40000])
    return t


def load(cx, tab):
    rows = cx.execute("""select number, title, abstract, claims, description, score,
                                (verdict is not null and verdict<>'') as read_
                         from documents where tab_id=? and status='fetched'""", (tab,)).fetchall()
    docs = {}
    for num, ti, ab, cl, de, sc, read_ in rows:
        terms = doc_terms(ab, cl, de, ti)
        if not terms:
            continue
        docs[num] = {"tf": Counter(terms), "n": len(terms), "score": sc,
                     "read": bool(read_), "title": ti or ""}
    return docs


def log_odds(docs, pos_ids, neg_ids, alpha=1.0):
    """Per-term log-odds of appearing in a positive vs a negative document (presence, not count —
    with ~10-60 positives a raw-count model is dominated by one long document)."""
    dfp, dfn = Counter(), Counter()
    for n in pos_ids:
        dfp.update(set(docs[n]["tf"]))
    for n in neg_ids:
        dfn.update(set(docs[n]["tf"]))
    P, N = len(pos_ids), len(neg_ids)
    w = {}
    for term in set(dfp) | set(dfn):
        if dfp[term] < 2:            # a term seen in one positive only is noise, not signal
            continue
        p = (dfp[term] + alpha) / (P + 2 * alpha)
        q = (dfn[term] + alpha) / (N + 2 * alpha)
        w[term] = math.log(p / q)
    return w


def score_lo(doc, w):
    """Mean log-odds over the document's distinct terms that the model knows. Mean, not sum, so a
    long specification cannot out-score a tight one just by being long."""
    hits = [w[t] for t in doc["tf"] if t in w]
    if not hits:
        return 0.0
    hits.sort(reverse=True)
    top = hits[:60]                  # the document's most positive evidence, capped
    return sum(top) / len(top)


def benchmark_vec(cx, tab, idf):
    bm = cx.execute("select title, abstract, claims, description, text, features_json "
                    "from benchmark where tab_id=?", (tab,)).fetchone()
    if not bm:
        return {}
    feats = json.loads(bm[5]) if bm[5] else []
    q = " ".join(x or "" for x in bm[:5]) + " " + " ".join(
        ((f.get("name") or "") + " ") * (3 if (f.get("kind") or "M").upper() == "M" else 1)
        for f in feats)
    tf = Counter(tok(q))
    return {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}


def cosine(a, b):
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) < len(b) else (b, a)
    dot = sum(v * large.get(t, 0.0) for t, v in small.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def must_coverage(cx, tab, docs):
    """Fraction of each HEAVY MUST feature's terms present in the document, averaged. The
    supervised log-odds signal alone is corruptible: it happily learns a tab's shared boilerplate
    (t13's top unread candidate scored on 'shuttles/rockets/spaceships' — Chinese field-of-use
    padding its positives also carry) or one positive's idiosyncratic vocabulary. Coverage of the
    words the CLAIM actually turns on is the corrective, and it cannot be gamed by boilerplate."""
    bm = cx.execute("select features_json from benchmark where tab_id=?", (tab,)).fetchone()
    feats = json.loads(bm[0]) if bm and bm[0] else []
    heavy = [set(tok(f["name"])) for f in feats
             if (f.get("kind") or "M").upper() == "M" and f.get("weight", 3) >= 4]
    heavy = [h for h in heavy if h]
    for d in docs.values():
        d["mc"] = (sum(len(h & set(d["tf"])) / len(h) for h in heavy) / len(heavy)) if heavy else 0.0


def build(cx, tab):
    docs = load(cx, tab)
    df = Counter()
    for d in docs.values():
        df.update(set(d["tf"]))
    Nd = len(docs)
    idf = {t: math.log(1 + Nd / c) for t, c in df.items()}
    for d in docs.values():
        d["vec"] = {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in d["tf"].items()}
    bvec = benchmark_vec(cx, tab, idf)
    for d in docs.values():
        d["bm"] = cosine(d["vec"], bvec)
    must_coverage(cx, tab, docs)
    labelled = [n for n, d in docs.items() if d["read"] and d["score"] is not None]
    pos = [n for n in labelled if docs[n]["score"] >= POS]
    neg = [n for n in labelled if docs[n]["score"] <= NEG]
    return docs, pos, neg


def validate(cx):
    """Leave-one-positive-out. For each known positive, rebuild the model WITHOUT it and rank it
    against every known negative. This is the control the old lexical lane failed."""
    print("LEAVE-ONE-POSITIVE-OUT VALIDATION  (rank of the held-out positive among the negatives)")
    print("tab   pos  neg    signal      median rank   top1%   top5%   top10%   worst")
    overall = defaultdict(list)
    for tab in TABS:
        docs, pos, neg = build(cx, tab)
        if len(pos) < 4:
            print(f" t{tab}: only {len(pos)} positives — skipped"); continue
        for signal in ("bm", "lo", "lo+bm", "lo+bm+mc"):
            ranks = []
            for held in pos:
                rest = [p for p in pos if p != held]
                w = log_odds(docs, rest, neg) if signal != "bm" else {}
                def sc(n):
                    d = docs[n]
                    if signal == "bm":
                        return d["bm"]
                    if signal == "lo":
                        return score_lo(d, w)
                    if signal == "lo+bm":
                        return score_lo(d, w) + 4.0 * d["bm"]
                    return score_lo(d, w) + 4.0 * d["bm"] + 2.0 * d["mc"]
                field = [(sc(n), n) for n in neg] + [(sc(held), held)]
                field.sort(key=lambda x: -x[0])
                ranks.append(next(i for i, (_, n) in enumerate(field, 1) if n == held))
            ranks.sort()
            m = len(neg) + 1
            med = ranks[len(ranks) // 2]
            t1 = sum(1 for r in ranks if r <= max(1, m // 100)) / len(ranks)
            t5 = sum(1 for r in ranks if r <= max(1, m // 20)) / len(ranks)
            t10 = sum(1 for r in ranks if r <= max(1, m // 10)) / len(ranks)
            print(f" t{tab:<4} {len(pos):<4} {len(neg):<6} {signal:<11} "
                  f"{med:<13} {t1:5.0%}   {t5:5.0%}   {t10:5.0%}    {ranks[-1]}")
            overall[signal] += [r / m for r in ranks]
    print("\npooled across tabs (rank as a fraction of the field — lower is better):")
    for signal, rs in overall.items():
        rs.sort()
        print(f"  {signal:<8} median {rs[len(rs)//2]:.3f}   "
              f"90th pct {rs[int(0.9*len(rs))-1]:.3f}   n={len(rs)}")


def emit(cx, k, signal="lo+bm, MUST-coverage gated"):
    print(f"UNREAD CANDIDATES ranked by '{signal}' (never opus-read, screen-rejected)\n")
    out = []
    for tab in TABS:
        docs, pos, neg = build(cx, tab)
        w = log_odds(docs, pos, neg)
        unread = [n for n, d in docs.items() if not d["read"]]
        scored = sorted(((score_lo(docs[n], w) + 4.0 * docs[n]["bm"], n) for n in unread),
                        key=lambda x: -x[0])
        # where do THIS tab's known positives sit on the same scale? a candidate that does not
        # reach the weakest of them is not worth a read
        pos_s = sorted(score_lo(docs[n], w) + 4.0 * docs[n]["bm"] for n in pos)
        # GATE, calibrated per tab: a candidate must cover the heavy MUST terms at least as well
        # as the weakest quartile of the documents opus actually scored >= 4 on this benchmark.
        # Ranking alone put a doc whose evidence was "shuttles / rockets / spaceships" first.
        mc_pos = sorted(docs[n]["mc"] for n in pos)
        gate = mc_pos[max(0, len(mc_pos)//4 - 1)]
        print(f"t{tab}: {len(unread)} unread | known-positive band "
              f"{pos_s[0]:.2f} .. {pos_s[-1]:.2f} (median {pos_s[len(pos_s)//2]:.2f})")
        kept = [(sc, n) for sc, n in scored if docs[n]["mc"] >= gate]
        blocked = len([1 for sc, n in scored[:k*4] if docs[n]["mc"] < gate])
        print(f"    MUST-coverage gate {gate:.0%} (25th pct of this tab's opus>=4 docs) — "
              f"{blocked} of the top {k*4} raw-ranked blocked as boilerplate matches")
        for s, n in kept[:k]:
            band = "ABOVE median positive" if s >= pos_s[len(pos_s) // 2] else \
                   ("within positive band" if s >= pos_s[0] else "below every positive")
            print(f"    {s:7.3f}  mc={docs[n]['mc']:4.0%}  {n:<16} "
                  f"{docs[n]['title'][:52]:<52} [{band}]")
            out.append({"tab": tab, "number": n, "score": round(s, 4), "band": band,
                        "must_coverage": round(docs[n]["mc"], 3),
                        "band_position": round((s - pos_s[0]) / max(1e-9, pos_s[-1] - pos_s[0]), 3),
                        "title": docs[n]["title"]})
        print()
    json.dump(out, open("/data/audits/opus_prior_filter.json", "w"), indent=1)
    print("-> /data/audits/opus_prior_filter.json")


def precision(cx, folds=5):
    """The decision metric. Rank the WHOLE labelled field (positives + negatives) with a k-fold
    model so no positive ever contributes to its own score, then ask what a small pick buys:
    of the top N, how many are documents opus actually scored >= 4?"""
    print("K-FOLD PRECISION ON THE LABELLED FIELD  (no positive scores itself)")
    print("tab   field   pos   p@1    p@3    p@5    p@10   recall@50  best-rank-of-a-positive")
    for tab in TABS:
        docs, pos, neg = build(cx, tab)
        if len(pos) < folds:
            print(f" t{tab}: too few positives"); continue
        scores = {}
        for f in range(folds):
            held = [p for i, p in enumerate(sorted(pos)) if i % folds == f]
            train = [p for p in pos if p not in held]
            w = log_odds(docs, train, neg)
            for n in held:
                scores[n] = score_lo(docs[n], w) + 4.0 * docs[n]["bm"] + 2.0 * docs[n]["mc"]
        w_all = log_odds(docs, pos, neg)          # negatives never train on themselves as positives
        for n in neg:
            scores[n] = score_lo(docs[n], w_all) + 4.0 * docs[n]["bm"] + 2.0 * docs[n]["mc"]
        field = sorted(scores.items(), key=lambda kv: -kv[1])
        ispos = {n: (n in set(pos)) for n, _ in field}
        def pat(k):
            return sum(1 for n, _ in field[:k] if ispos[n]) / k
        rec50 = sum(1 for n, _ in field[:50] if ispos[n]) / len(pos)
        best = next(i for i, (n, _) in enumerate(field, 1) if ispos[n])
        print(f" t{tab:<4} {len(field):<7} {len(pos):<5} {pat(1):<6.0%} {pat(3):<6.0%} "
              f"{pat(5):<6.0%} {pat(10):<6.0%} {rec50:<10.0%} {best}")


def explain(cx, tab, numbers):
    """What is actually driving a candidate's score, and does it touch the HEAVY MUST features?
    A lexical lane that scores high on generic vocabulary of the field is a trap — this makes
    that visible before an opus read is spent."""
    docs, pos, neg = build(cx, tab)
    w = log_odds(docs, pos, neg)
    must = cx.execute("select features_json from benchmark where tab_id=?", (tab,)).fetchone()
    feats = json.loads(must[0]) if must and must[0] else []
    heavy = [f for f in feats if (f.get("kind") or "M").upper() == "M" and f.get("weight", 3) >= 4]
    for num in numbers:
        d = docs.get(num)
        if not d:
            print(f"  {num}: not in tab {tab}"); continue
        hits = sorted(((w[t], t) for t in d["tf"] if t in w), reverse=True)[:12]
        print(f"\n  t{tab} {num} — {d['title'][:70]}")
        print(f"    top evidence terms: {', '.join(f'{t}({v:+.2f})' for v, t in hits)}")
        for f in heavy:
            terms = set(tok(f["name"]))
            present = sorted(t for t in terms if t in d["tf"])
            cov = len(present) / max(1, len(terms))
            flag = "OK " if cov >= 0.6 else ("weak" if cov >= 0.3 else "MISS")
            print(f"    [{flag}] w{f.get('weight')} {cov:4.0%} of terms: {f['name'][:78]}")


ap = argparse.ArgumentParser()
ap.add_argument("--validate", action="store_true")
ap.add_argument("--emit", type=int, metavar="K")
ap.add_argument("--precision", action="store_true")
ap.add_argument("--explain", nargs=2, metavar=("TAB", "NUMBERS"))
a = ap.parse_args()
cx = sqlite3.connect(DB, uri=True)
if a.validate:
    validate(cx)
if a.precision:
    precision(cx)
if a.explain:
    explain(cx, int(a.explain[0]), a.explain[1].split(","))
if a.emit:
    emit(cx, a.emit)
