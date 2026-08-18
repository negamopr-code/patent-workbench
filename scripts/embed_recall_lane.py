#!/usr/bin/env python3
"""Fix #4 — embeddings recall lane (NLM blind spot #4: cross-family paraphrase
blindness). Ranks a tab's corpus by semantic similarity of claims text to the
benchmark, feeding the same top-tier read queue as the lexical lane.

Engine: quantized MiniLM-L6-v2 ONNX (int8, ~23 MB) + tokenizers — small enough
to run on CPU in a memory-starved VM. Designed for CHUNKED invocation so RSS is
freed between chunks and a ulimit -v cap makes THIS process fail first, never
pressuring the shared VM (the sweeps' container must survive us):

  embed a slice:   python embed_recall_lane.py embed corpus.json outdir START COUNT
  rank + report:   python embed_recall_lane.py rank  corpus.json outdir [CHAMP_MIN]

corpus.json: {"docs":[{id,number,title,abstract,claims,score,score_model}...],
              "benchmark":{number,title,abstract,claims,features_json}}
outdir accumulates emb_<START>.npz checkpoints; rank merges them, embeds the
benchmark + MUST features, and reports every opus-labeled doc's rank plus the
lane's read queue (top-N + seeded random control).
"""
import json
import os
import sys

import numpy as np

MODEL = os.environ.get("EMB_MODEL",
                       os.path.join(os.path.dirname(__file__), "model_quantized.onnx"))
TOKENIZER = os.environ.get("EMB_TOKENIZER",
                           os.path.join(os.path.dirname(__file__), "tokenizer.json"))
MAX_TOK = 256          # MiniLM effective window
CHUNKS_PER_DOC = 4     # first ~1000 tokens of claims text
BATCH = 8
TOP_N = 60             # lane read-queue size
RAND_N = 15            # seeded random control


def _session():
    import onnxruntime as ort
    from tokenizers import Tokenizer
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.inter_op_num_threads = 1
    opts.enable_cpu_mem_arena = False   # keep virtual footprint small under ulimit -v
    sess = ort.InferenceSession(MODEL, opts, providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(TOKENIZER)
    tok.enable_truncation(max_length=MAX_TOK)
    return sess, tok


def _embed_texts(sess, tok, texts):
    """Mean-pooled, L2-normalized sentence vectors for a list of strings."""
    out = []
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        enc = [tok.encode(t) for t in batch]
        n = max(len(e.ids) for e in enc)
        ids = np.zeros((len(enc), n), dtype=np.int64)
        att = np.zeros((len(enc), n), dtype=np.int64)
        for j, e in enumerate(enc):
            ids[j, :len(e.ids)] = e.ids
            att[j, :len(e.ids)] = e.attention_mask
        res = sess.run(None, {"input_ids": ids, "attention_mask": att,
                              "token_type_ids": np.zeros_like(ids)})[0]
        mask = att[:, :, None].astype(np.float32)
        vec = (res * mask).sum(1) / np.maximum(mask.sum(1), 1e-9)
        out.append(vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-9))
    return np.vstack(out) if out else np.zeros((0, 384), dtype=np.float32)


def _doc_chunks(d):
    text = " ".join(filter(None, [d.get("title"), d.get("abstract"),
                                  d.get("claims"), d.get("text")])) \
        or (d.get("number") or "")
    words = text.split()
    step = 180                      # ~230 tokens per 180 words, under MAX_TOK
    chunks = [" ".join(words[i:i + step]) for i in range(0, len(words),
                                                         step)][:CHUNKS_PER_DOC]
    return chunks or [d.get("number") or ""]


def embed(corpus_path, outdir, start, count):
    docs = json.load(open(corpus_path))["docs"][start:start + count]
    if not docs:
        print("nothing to do")
        return
    sess, tok = _session()
    vecs, ids = [], []
    for d in docs:
        cv = _embed_texts(sess, tok, _doc_chunks(d))
        v = cv.mean(0)
        vecs.append(v / max(np.linalg.norm(v), 1e-9))
        ids.append(d["id"])
    np.savez_compressed(os.path.join(outdir, f"emb_{start}.npz"),
                        ids=np.array(ids), vecs=np.vstack(vecs))
    print(f"embedded {len(ids)} docs [{start}..{start + len(ids)})")


def rank(corpus_path, outdir, champ_min=4.0):
    data = json.load(open(corpus_path))
    docs = {d["id"]: d for d in data["docs"]}
    bm = data["benchmark"]
    parts = sorted(f for f in os.listdir(outdir) if f.startswith("emb_"))
    ids, vecs = [], []
    for f in parts:
        z = np.load(os.path.join(outdir, f))
        ids.extend(z["ids"].tolist())
        vecs.append(z["vecs"])
    vecs = np.vstack(vecs)
    print(f"merged {len(ids)} embedded docs from {len(parts)} checkpoint(s)")

    sess, tok = _session()
    feats = [f.get("name") or f.get("text") or f.get("feature") or "" for f in
             (json.loads(bm.get("features_json") or "[]"))
             if f.get("kind", "M") == "M"]
    bm_texts = _doc_chunks(bm)
    bv = _embed_texts(sess, tok, bm_texts).mean(0)
    bv /= max(np.linalg.norm(bv), 1e-9)
    fv = _embed_texts(sess, tok, [f for f in feats if f]) if any(feats) else None

    sim_bm = vecs @ bv
    sim = sim_bm.copy()
    if fv is not None and len(fv):
        top3 = np.sort(vecs @ fv.T, axis=1)[:, -3:].mean(1)
        sim = 0.5 * sim_bm + 0.5 * top3
    order = np.argsort(-sim)
    rank_of = {ids[i]: r + 1 for r, i in enumerate(order)}

    print("\n== opus-labeled docs, embedding rank ==")
    labeled = [d for d in docs.values() if d.get("score") is not None
               and "opus" in (d.get("score_model") or "")]
    hits = 0
    for d in sorted(labeled, key=lambda d: -(d["score"] or 0)):
        r = rank_of.get(d["id"])
        mark = "🏆" if d["score"] >= champ_min else "  "
        if d["score"] >= champ_min and r and r <= TOP_N:
            hits += 1
        print(f"  {mark} {d['number']:>16} opus={d['score']:>4} -> rank "
              f"{r if r else 'unembedded'} / {len(ids)}")
    champs = [d for d in labeled if d["score"] >= champ_min]
    print(f"\nlane recall: {hits}/{len(champs)} champions inside top-{TOP_N} "
          f"({100 * hits / max(len(champs), 1):.0f}%)")

    rng = np.random.default_rng(20260818)
    queue = [{"id": ids[i], "number": docs[ids[i]]["number"],
              "sim": round(float(sim[i]), 4), "lane": "embed"}
             for i in order[:TOP_N]]
    rest = [i for i in order[TOP_N:]]
    ctrl = rng.choice(len(rest), size=min(RAND_N, len(rest)), replace=False)
    queue += [{"id": ids[rest[i]], "number": docs[ids[rest[i]]]["number"],
               "sim": round(float(sim[rest[i]]), 4), "lane": "control"}
              for i in ctrl]
    out = os.path.join(outdir, "embed_lane_queue.json")
    json.dump(queue, open(out, "w"), indent=1)
    print(f"read queue (top-{TOP_N} + {RAND_N} seeded control) -> {out}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "embed":
        embed(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    elif cmd == "rank":
        rank(sys.argv[2], sys.argv[3],
             *([float(sys.argv[4])] if len(sys.argv) > 4 else []))
    else:
        sys.exit("usage: embed_recall_lane.py embed|rank …")
