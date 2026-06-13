"""API smoke tests — external bridges (claude / nlm / Google Patents) are stubbed."""
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
    monkeypatch.setattr(claude_bridge, "digest_document",
                        lambda n, t, x, model=None: {"digest": f"digest of {n}"})
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None: {"verdict": f"MATCH SCORE: 7 for {d['number']}"})
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
    return TestClient(api.app)


def test_health(client):
    r = client.get("/api/health").json()
    assert r["ok"] is True


def test_tab_documents_flow(client):
    tab = client.post("/api/tabs", json={"name": "Test"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "US10395648B1 and https://patents.google.com/patent/CN120638382A/en"}).json()
    assert len(r["inserted"]) == 2
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert {d["number"] for d in docs} == {"US10395648B1", "CN120638382"}
    assert all(d["status"] == "fetched" for d in docs)  # TestClient runs bg tasks inline
    assert docs[0]["links"]["google"].startswith("https://patents.google.com/patent/")


def test_chat_with_notebook(client):
    tab = client.post("/api/tabs", json={"name": "Chat"}).json()
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": ["s1"]})
    r = client.post(f"/api/tabs/{tab['id']}/chat",
                    json={"question": "q?", "ask_notebook": True}).json()
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["a", "c"]
    assert "nlm[nb-1]:['s1']" in r["messages"][0]["text"]
    state = client.get(f"/api/tabs/{tab['id']}/state").json()
    assert [m["role"] for m in state["messages"]] == ["q", "a", "c"]


def test_ask_notebook_only(client):
    tab = client.post("/api/tabs", json={"name": "NB"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/ask-notebook", json={"question": "q?"})
    assert r.status_code == 400  # no notebook connected yet
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    r = client.post(f"/api/tabs/{tab['id']}/ask-notebook", json={"question": "q?"}).json()
    assert r["messages"][0]["role"] == "a"


def test_tab_rename_delete(client):
    tab = client.post("/api/tabs", json={"name": "Old"}).json()
    client.patch(f"/api/tabs/{tab['id']}", json={"name": "New"})
    assert client.get("/api/tabs").json()["tabs"][0]["name"] == "New"
    client.delete(f"/api/tabs/{tab['id']}")
    assert client.get("/api/tabs").json()["tabs"] == []
    assert client.get(f"/api/tabs/{tab['id']}/state").status_code == 404


def test_upload_txt(client, tmp_path):
    tab = client.post("/api/tabs", json={"name": "Up"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/upload",
                    files={"file": ("list.txt", b"US10395648B1\nEP3667902A1", "text/plain")}).json()
    assert r["numbers"] == ["US10395648B1", "EP3667902A1"]
    # confirm-add the extracted numbers
    r2 = client.post(f"/api/tabs/{tab['id']}/documents",
                     json={"numbers": r["numbers"], "source": "image"}).json()
    assert len(r2["inserted"]) == 2


def test_benchmark_by_number(client):
    tab = client.post("/api/tabs", json={"name": "BM"}).json()
    r = client.put(f"/api/tabs/{tab['id']}/benchmark",
                   json={"text": "https://patents.google.com/patent/US10395648B1/en"}).json()
    assert r["ok"]
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    bm = st["benchmark"]
    assert bm["number"] == "US10395648B1"
    assert bm["status"] == "ready"            # bg fetch ran inline, stubbed fetcher
    assert bm["links"]["google"].endswith("/US10395648B1/en")
    # no plausible number → 400
    assert client.put(f"/api/tabs/{tab['id']}/benchmark",
                      json={"text": "hello world"}).status_code == 400


def test_benchmark_upload_images(client, monkeypatch):
    from patentbench import extract
    monkeypatch.setattr(extract, "text_from_image",
                        lambda p, model=None: {"text": f"page text of {p.rsplit('-', 1)[-1]}"})
    tab = client.post("/api/tabs", json={"name": "BMimg"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/benchmark/upload",
                    files=[("files", ("p1.png", b"x", "image/png")),
                           ("files", ("p2.png", b"y", "image/png"))]).json()
    assert r["ok"]
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    bm = st["benchmark"]
    assert bm["status"] == "ready" and bm["source"] == "images"
    assert [f["name"] for f in bm["files"]] == ["p1.png", "p2.png"]
    assert bm["text"] is True                  # presence flag in slim state view
    # mixing pdf and images rejected
    r2 = client.post(f"/api/tabs/{tab['id']}/benchmark/upload",
                     files=[("files", ("a.pdf", b"x", "application/pdf")),
                            ("files", ("b.png", b"y", "image/png"))])
    assert r2.status_code == 400


def test_chat_includes_benchmark_participant(client):
    tab = client.post("/api/tabs", json={"name": "BMchat"}).json()
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    r = client.post(f"/api/tabs/{tab['id']}/chat", json={"question": "best fit?"}).json()
    parts = r["messages"][-1]["participants"]
    assert any(p["kind"] == "benchmark" and p["title"] == "US10395648B1" for p in parts)


def test_benchmark_clear(client):
    tab = client.post("/api/tabs", json={"name": "BMdel"}).json()
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    assert client.delete(f"/api/tabs/{tab['id']}/benchmark").json()["ok"]
    assert client.get(f"/api/tabs/{tab['id']}/state").json()["benchmark"] is None


def test_document_full_and_edit_number(client):
    tab = client.post("/api/tabs", json={"name": "Fix"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["abstract_len"] == 3          # stubbed "abs"
    full = client.get(f"/api/tabs/{tab['id']}/documents/{docs[0]['id']}").json()
    assert full["claims"] == "1. A method."
    r = client.patch(f"/api/tabs/{tab['id']}/documents/{docs[0]['id']}",
                     json={"number": "US2023278430"}).json()
    assert r["number"] == "US20230278430"        # canonicalized (missing zero injected)
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["number"] == "US20230278430" and docs[0]["status"] == "fetched"


def test_benchmark_full_view(client, monkeypatch):
    from patentbench import extract
    monkeypatch.setattr(extract, "text_from_image", lambda p, model=None: {"text": "page one text"})
    tab = client.post("/api/tabs", json={"name": "BMview"}).json()
    client.post(f"/api/tabs/{tab['id']}/benchmark/upload",
                files=[("files", ("p1.png", b"x", "image/png"))])
    full = client.get(f"/api/tabs/{tab['id']}/benchmark/full").json()
    assert "page one text" in full["text"]


def test_benchmark_upload_natural_page_order(client, monkeypatch):
    from patentbench import extract
    monkeypatch.setattr(extract, "text_from_image", lambda p, model=None: {"text": "t"})
    tab = client.post("/api/tabs", json={"name": "Order"}).json()
    client.post(f"/api/tabs/{tab['id']}/benchmark/upload",
                files=[("files", (f"page ({i}).png", b"x", "image/png")) for i in (10, 2, 1)])
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    assert [f["name"] for f in st["benchmark"]["files"]] == \
        ["page (1).png", "page (2).png", "page (10).png"]


def test_digest_stored_at_fetch_time(client):
    tab = client.post("/api/tabs", json={"name": "Dg"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["digest_len"] and docs[0]["status"] == "fetched"
    full = client.get(f"/api/tabs/{tab['id']}/documents/{docs[0]['id']}").json()
    assert full["digest"] == "digest of US10395648B1"


def test_deep_compare(client):
    tab = client.post("/api/tabs", json={"name": "Deep"}).json()
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "EP3667902A1 CN114547092"})
    r = client.post(f"/api/tabs/{tab['id']}/deep-compare", json={}).json()
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["s", "c"]
    assert "2/2 candidates at FULL text" in r["messages"][0]["text"]
    assert r["messages"][1]["text"] == "ranking: best is X"
    parts = r["messages"][1]["participants"]
    assert any(p["title"].endswith("full text") for p in parts if p["kind"] == "documents")
    # no benchmark -> 400
    tab2 = client.post("/api/tabs", json={"name": "NoBM"}).json()
    assert client.post(f"/api/tabs/{tab2['id']}/deep-compare", json={}).status_code == 400


def test_deep_compare_subset(client):
    tab = client.post("/api/tabs", json={"name": "DeepSel"}).json()
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"text": "EP3667902A1 CN114547092 CN119134413"})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    pick = [d["id"] for d in docs[:2]]
    r = client.post(f"/api/tabs/{tab['id']}/deep-compare", json={"doc_ids": pick}).json()
    assert "2/2 candidates at FULL text" in r["messages"][0]["text"]
    parts = r["messages"][1]["participants"]
    assert any(p["title"] == "2 of 3 candidates · full text" for p in parts)
    state = client.get(f"/api/tabs/{tab['id']}/state").json()
    q = [m for m in state["messages"] if m["role"] == "q"][-1]
    assert "2 of 3 candidates" in q["text"]
    # unknown ids only -> 400
    assert client.post(f"/api/tabs/{tab['id']}/deep-compare",
                       json={"doc_ids": [99999]}).status_code == 400


def test_reading_model_plumbed(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "digest_document",
                        lambda n, t, x, model=None: seen.update(digest=model)
                        or {"digest": "d"})
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None: seen.update(map=model)
                        or {"verdict": "MATCH SCORE: 5"})
    tab = client.post("/api/tabs", json={"name": "RM"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"text": "US10395648B1", "reading_model": "claude-sonnet-4-6"})
    assert seen["digest"] == "claude-sonnet-4-6"
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP3667902A1"})
    client.post(f"/api/tabs/{tab['id']}/deep-compare",
                json={"reading_model": "claude-sonnet-4-6"})
    assert seen["map"] == "claude-sonnet-4-6"
    # invalid model name falls back to the cheap default (None -> DIGEST_MODEL)
    seen.clear()
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"text": "CN114547092", "reading_model": "gpt-9"})
    assert seen["digest"] is None  # noqa: E501 — invalid name rejected, default used


def test_deep_compare_stores_scores(client, monkeypatch):
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None: {"verdict":
                            f"MATCH SCORE: 8.5\nKEY FEATURES: AGC fan-out + ESS hierarchy\n"
                            f"OVERLAP: ...\nVERDICT: close for {d['number']}"})
    tab = client.post("/api/tabs", json={"name": "Score"}).json()
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "EP3667902A1"})
    client.post(f"/api/tabs/{tab['id']}/deep-compare", json={})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["score"] == 8.5
    assert docs[0]["score_note"] == "AGC fan-out + ESS hierarchy"
    assert docs[0]["scored_at"]


def test_notebook_auto_add_and_sync(client, monkeypatch):
    added, state = [], {"full_after": 2}
    def fake_add(nb, title, text):
        if len(added) >= state["full_after"]:
            return {"error": "notebook is full (50 sources)", "full": True}
        added.append((nb, title))
        return {"ok": True}
    monkeypatch.setattr(nlm_bridge, "add_source_text", fake_add)
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda t: {"id": "nb-2", "title": t})
    tab = client.post("/api/tabs", json={"name": "NbSync"}).json()
    # auto-add on: new candidates mirror into the notebook during the pipeline
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": [],
                     "auto_add": True})
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["nlm_source_notebook"] == "nb-1"
    assert added[0][0] == "nb-1" and "US10395648B1" in added[0][1]
    # sync: adds until full, reports remaining
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"text": "EP3667902A1 CN114547092"})
    r = client.post(f"/api/tabs/{tab['id']}/notebook/sync").json()
    assert r["full"] is True and r["added"] == 0  # pipeline added 2nd already? no: full_after=2 hit during pipeline
    # create follow-up notebook and finish
    r2 = client.post(f"/api/tabs/{tab['id']}/notebook/create",
                     json={"title": "NB (2)"}).json()
    assert r2["notebook"]["notebook_id"] == "nb-2" and r2["notebook"]["auto_add"] == 1
    state["full_after"] = 99
    r3 = client.post(f"/api/tabs/{tab['id']}/notebook/sync").json()
    assert r3["added"] >= 1 and not r3["full"]


def test_notebook_export_includes_benchmark(client, monkeypatch):
    added = []
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: added.append((nb, title)) or {"ok": True})
    tab = client.post("/api/tabs", json={"name": "Exp"}).json()
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": [],
                     "auto_add": True})
    # setting the benchmark (bg fetch runs inline) mirrors it when auto-add is on
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    assert any("BENCHMARK" in t for _, t in added)
    # a new candidate auto-mirrors during its own pipeline (not the benchmark again)
    added.clear()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "CN114547092"})
    assert any("CN114547092" in t for _, t in added)
    assert not any("BENCHMARK" in t for _, t in added)
    # a manual sync now finds everything already mirrored → nothing to add
    r = client.post(f"/api/tabs/{tab['id']}/notebook/sync").json()
    assert r["added"] == 0 and r["remaining"] == 0


def test_notebook_import_patents_and_text(client, monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": [
                            {"id": "sp", "title": "US10395648B1 — a patent"},
                            {"id": "st", "title": "Meeting notes"}]})
    monkeypatch.setattr(nlm_bridge, "source_content",
                        lambda sid: {"content": "raw text body of the notes"})
    tab = client.post("/api/tabs", json={"name": "Imp"}).json()
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    r = client.post(f"/api/tabs/{tab['id']}/notebook/import").json()
    assert r["patents_added"] == 1 and r["text_added"] == 1
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    by_num = {d["number"]: d for d in docs}
    assert "US10395648B1" in by_num and by_num["US10395648B1"]["status"] == "fetched"
    assert by_num["US10395648B1"]["nlm_source_notebook"] == "nb-1"  # won't be re-exported
    txt = by_num["Meeting notes"]
    assert txt["source"] == "notebook-text" and txt["links"] is None
    # idempotent: re-import skips everything already present
    r2 = client.post(f"/api/tabs/{tab['id']}/notebook/import").json()
    assert r2["patents_added"] == 0 and r2["text_added"] == 0 and r2["skipped"] == 2
