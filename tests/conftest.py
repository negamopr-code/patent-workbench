"""Shared test fixtures. The `client` fixture stubs EVERY external bridge function
(claude / nlm / Google Patents) — the anti-leak rule: any NEW bridge function must be
stubbed HERE the moment it exists, or pytest spawns the real `claude`/`nlm` binaries
(bit us live: a test run burned real quota). Verify with
CLAUDE_BIN=/nonexistent NLM_BIN=/nonexistent pytest."""
import pytest
from fastapi.testclient import TestClient

import patentbench.db as db
from patentbench import claude_bridge, fetcher, nlm_bridge
from patentbench.web import api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setattr(fetcher, "fetch_document",
                        lambda n: {"title": f"T:{n}", "abstract": "abs",
                                   "claims": "1. A method.", "description": "desc"})
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: {"answer": "claude says hi", "model": "claude-fable-5"})
    # 🧪 The EPC sanity pass is a SECOND claude call made by the API on top of
    # the (stubbed) psa/chat/tet_123_check answer — without this stub every
    # PSA / 123-check / tech-effect test spawns the real `claude -p` binary.
    # The sanity tests override this with their own monkeypatch.
    monkeypatch.setattr(claude_bridge, "epc_sanity",
                        lambda answer, model=None: {"clean": True})
    monkeypatch.setattr(claude_bridge, "digest_document",
                        lambda n, t, x, model=None: {"digest": f"digest of {n}"})
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: {"verdict": f"MATCH SCORE: 7 for {d['number']}"})
    monkeypatch.setattr(claude_bridge, "deep_reduce",
                        lambda *a, **k: {"answer": "ranking: best is X"})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": f"nlm[{nb}]:{source_ids}",
                                                        "sources_used": []})
    monkeypatch.setattr(nlm_bridge, "list_notebooks",
                        lambda force=False: {"notebooks": [{"id": "nb-1", "title": "NB", "sources": 2}]})
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": [{"id": "s1", "title": "doc1"},
                                                             {"id": "s2", "title": "doc2"}]})
    # the 🔬 mega-screen's staging path — stubbed so no test ever shells out to `nlm`
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "delete_source",
                        lambda ids, nb=None: {"ok": True, "deleted": len(ids or [])})
    monkeypatch.setattr(nlm_bridge, "wait_sources_ready",
                        lambda nb, timeout=0, poll=0, known_ready=None: {"ready": True,
                                                                         "processed": 0, "total": 0})
    monkeypatch.setattr(nlm_bridge, "source_content",
                        lambda sid: {"content": "text"})
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda title: {"id": "nb-new", "title": title})
    monkeypatch.setattr(nlm_bridge, "delete_notebook",
                        lambda nb: {"ok": True})
    # auto-create-notebook is exercised by its own tests with stubs; keep it off by
    # default so the other tests stay notebook-less unless they opt in.
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", False)
    # Figures default ON in prod, but captioning shells to vision + network — keep it
    # off for the generic tests; the figure test opts in with stubs.
    monkeypatch.setattr(api, "AUTO_FIGURES", False)
    return TestClient(api.app)
