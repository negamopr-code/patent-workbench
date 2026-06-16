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
    # auto-create-notebook is exercised by its own tests with stubs; keep it off by
    # default so the other tests stay notebook-less unless they opt in.
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", False)
    return TestClient(api.app)


def _wait_read(client, tid, tries=100):
    """Block until the background deep-read job for a tab has finished."""
    import time
    for _ in range(tries):
        if not client.get(f"/api/tabs/{tid}/deep-compare/status").json()["running"]:
            return
        time.sleep(0.05)


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
                    files=[("files", ("list.txt", b"US10395648B1\nEP3667902A1", "text/plain"))]).json()
    assert r["numbers"] == ["US10395648B1", "EP3667902A1"]
    # confirm-add the extracted numbers
    r2 = client.post(f"/api/tabs/{tab['id']}/documents",
                     json={"numbers": r["numbers"], "source": "image"}).json()
    assert len(r2["inserted"]) == 2


def test_upload_many_files_aggregates_and_dedupes(client):
    tab = client.post("/api/tabs", json={"name": "Bulk"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/upload", files=[
        ("files", ("a.txt", b"US10395648B1\nEP3667902A1", "text/plain")),
        ("files", ("b.txt", ("EP3667902A1\nCN114853847B"), "text/plain")),  # 1 dup, 1 new
    ]).json()
    # union across files, first-seen order, deduped (CN…B canonicalizes to CN…)
    assert r["numbers"] == ["US10395648B1", "EP3667902A1", "CN114853847"]
    assert len(r["files"]) == 2
    assert {f["name"] for f in r["files"]} == {"a.txt", "b.txt"}


def test_ocr_model_floors_weak_to_strong():
    # cheap reading dropdown (haiku) must NOT be used for digit OCR — floored to
    # the strong OCR_MODEL; an explicitly stronger choice is honoured; None too.
    assert api._ocr_model("claude-haiku-4-5") == claude_bridge.OCR_MODEL
    assert api._ocr_model(None) == claude_bridge.OCR_MODEL
    assert api._ocr_model("bogus") == claude_bridge.OCR_MODEL
    assert api._ocr_model("claude-opus-4-8") == "claude-opus-4-8"
    assert claude_bridge.OCR_MODEL not in claude_bridge.WEAK_OCR_MODELS


def test_image_upload_uses_strong_ocr_model(client, monkeypatch):
    from patentbench import extract
    seen = {}

    def fake(path, model=None):
        seen["model"] = model
        return {"numbers": ["US10395648B1"], "uncertain": []}

    monkeypatch.setattr(extract, "numbers_from_image", fake)
    tab = client.post("/api/tabs", json={"name": "Img"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/upload",
                    files=[("files", ("photo.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg"))],
                    data={"reading_model": "claude-haiku-4-5"}).json()
    assert seen["model"] == claude_bridge.OCR_MODEL    # haiku floored to strong
    assert r["model"] == claude_bridge.OCR_MODEL
    assert r["numbers"] == ["US10395648B1"]


def test_auto_create_notebook_on_first_candidate(client, monkeypatch):
    added = []
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", True)
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda title: {"id": "nb-auto", "title": title})
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: (added.append((nb, title)), {"ok": True})[1])
    tab = client.post("/api/tabs", json={"name": "Proj-X"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    cfg = client.get(f"/api/tabs/{tab['id']}/state").json()["notebook"]
    assert cfg and cfg["notebook_id"] == "nb-auto"
    assert cfg["auto_add"]
    assert cfg["notebook_title"] == "Patent candidates — Proj-X"
    assert any(nb == "nb-auto" for nb, _ in added)          # candidate was exported
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["nlm_source_notebook"] == "nb-auto"


def test_auto_export_rolls_over_when_full(client, monkeypatch):
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", True)
    nb_seq = iter(["nb-1", "nb-2"])
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda title: {"id": next(nb_seq), "title": title})
    calls = []

    def fake_add(nb, title, text):
        calls.append(nb)
        return {"error": "full", "full": True} if nb == "nb-1" else {"ok": True}

    monkeypatch.setattr(nlm_bridge, "add_source_text", fake_add)
    tab = client.post("/api/tabs", json={"name": "Roll"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"numbers": ["US10395648B1"], "source": "image"})
    assert "nb-1" in calls and "nb-2" in calls               # full on nb-1 → rolled to nb-2
    cfg = client.get(f"/api/tabs/{tab['id']}/state").json()["notebook"]
    assert cfg["notebook_id"] == "nb-2"


def test_no_auto_create_when_disabled(client, monkeypatch):
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", False)
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda title: (_ for _ in ()).throw(AssertionError("should not create")))
    tab = client.post("/api/tabs", json={"name": "NoAuto"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    assert client.get(f"/api/tabs/{tab['id']}/state").json()["notebook"] is None


def test_nlm_rate_scores_candidates(client, monkeypatch):
    import time
    from patentbench import patents
    # connect a notebook + benchmark so the rating run has a target
    tab = client.post("/api/tabs", json={"name": "Rate"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B"], "source": "image"})
    # mark both candidates as living in nb-1 (export step normally does this)
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        db.update_document(d["id"], nlm_source_notebook="nb-1")
    # the notebook reports its sources (titles start with the patent number)
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": [
        {"id": "s-ep", "title": "EP3667902A1 — x"}, {"id": "s-cn", "title": "CN114853847 — y"}]})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "MATCH SCORE: 6\nKEY FEATURES: widget",
                                                        "sources_used": []})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    assert client.post(f"/api/tabs/{tid}/nlm-rate", json={}).json()["started"] is True
    # the rating runs in a daemon thread — wait briefly for it to finish
    for _ in range(50):
        if not client.get(f"/api/tabs/{tid}/nlm-rate/status").json()["running"]:
            break
        time.sleep(0.1)
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["nlm_score"] == 6 for d in docs)
    assert all(d["nlm_score_note"] == "widget" for d in docs)


def test_nlm_rate_only_selected(client, monkeypatch):
    import time
    tab = client.post("/api/tabs", json={"name": "Sel"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B"], "source": "image"})
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    for d in docs:
        db.update_document(d["id"], nlm_source_notebook="nb-1")
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": [
        {"id": "s-ep", "title": "EP3667902A1 — x"}, {"id": "s-cn", "title": "CN114853847 — y"}]})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "MATCH SCORE: 8\nKEY FEATURES: gear"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    target = next(d for d in docs if d["number"] == "EP3667902A1")
    other = next(d for d in docs if d["number"] != "EP3667902A1")
    assert client.post(f"/api/tabs/{tid}/nlm-rate",
                       json={"doc_ids": [target["id"]]}).json()["started"] is True
    for _ in range(50):
        if not client.get(f"/api/tabs/{tid}/nlm-rate/status").json()["running"]:
            break
        time.sleep(0.1)
    after = {d["id"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert after[target["id"]]["nlm_score"] == 8        # selected one rated
    assert after[other["id"]]["nlm_score"] is None       # the other left untouched


def test_deep_compare_continue_skips_read_and_records_model(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "Cont"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B"], "source": "image"})
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    # pretend one was already full-read in a prior (interrupted) batch
    db.update_document(docs["EP3667902A1"]["id"], score=7, score_note="old", scored_at=1, score_model="claude-opus-4-8")
    read = []
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None: (read.append(d["number"]), {"verdict": f"MATCH SCORE: 5 for {d['number']}"})[1])
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "ranking"})
    client.post(f"/api/tabs/{tid}/deep-compare",
                json={"skip_scored": True, "reading_model": "claude-sonnet-4-6"})
    _wait_read(client, tid)
    assert read == ["CN114853847"]                      # only the unread one was read
    after = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert after["EP3667902A1"]["score"] == 7           # already-read one untouched
    assert after["CN114853847"]["score"] == 5           # newly read
    assert after["CN114853847"]["score_model"] == "claude-sonnet-4-6"   # records which model read it


def test_nlm_rate_all_skips_already_rated(client, monkeypatch):
    import time
    tab = client.post("/api/tabs", json={"name": "Skip"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B"], "source": "image"})
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    for d in docs.values():
        db.update_document(d["id"], nlm_source_notebook="nb-1")
    db.update_document(docs["EP3667902A1"]["id"], nlm_score=9)   # already rated → must be skipped
    queried = []
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": [
        {"id": "s-ep", "title": "EP3667902A1 — x"}, {"id": "s-cn", "title": "CN114853847 — y"}]})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))

    def fake_query(nb, q, source_ids=None):
        queried.append(source_ids)
        return {"answer": "MATCH SCORE: 4\nKEY FEATURES: x"}

    monkeypatch.setattr(nlm_bridge, "query", fake_query)
    client.post(f"/api/tabs/{tid}/nlm-rate", json={})            # all, force=false
    for _ in range(50):
        if not client.get(f"/api/tabs/{tid}/nlm-rate/status").json()["running"]:
            break
        time.sleep(0.1)
    after = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert after["EP3667902A1"]["nlm_score"] == 9     # untouched (skipped)
    assert after["CN114853847"]["nlm_score"] == 4     # newly rated
    assert len(queried) == 1                          # only ONE query — the unrated one


def test_reconcile_explains_only_big_gaps(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "reconcile",
                        lambda bm, items, model=None: (seen.update(items=items), {"answer": "PATTERN: NLM scores on field"})[1])
    tab = client.post("/api/tabs", json={"name": "Rec"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B"], "source": "image"})
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    by_num = {d["number"]: d for d in docs}
    # one big gap (Δ5), one agreement (Δ0)
    db.update_document(by_num["EP3667902A1"]["id"], score=8, nlm_score=3)
    db.update_document(by_num["CN114853847"]["id"], score=6, nlm_score=6)
    r = client.post(f"/api/tabs/{tid}/reconcile", json={"min_delta": 2}).json()
    assert "messages" in r
    # only the disagreeing candidate is sent to the (cheap) reconcile call
    assert [it["number"] for it in seen["items"]] == ["EP3667902A1"]


def test_reconcile_when_no_disagreement(client):
    tab = client.post("/api/tabs", json={"name": "Agree"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/reconcile", json={"min_delta": 2}).json()
    assert "agree" in r["messages"][0]["text"].lower()


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
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1 CN114547092"})
    assert client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()["started"] is True
    _wait_read(client, tid)
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert msgs[-1]["role"] == "c" and msgs[-1]["text"] == "ranking: best is X"
    assert any(m["role"] == "s" and "2/2 candidates at FULL text" in m["text"] for m in msgs)
    parts = msgs[-1]["participants"]
    assert any(p["title"].endswith("full text") for p in parts if p["kind"] == "documents")
    # no benchmark -> 400 (validated before the job starts)
    tab2 = client.post("/api/tabs", json={"name": "NoBM"}).json()
    assert client.post(f"/api/tabs/{tab2['id']}/deep-compare", json={}).status_code == 400


def test_deep_compare_subset(client):
    tab = client.post("/api/tabs", json={"name": "DeepSel"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"text": "EP3667902A1 CN114547092 CN119134413"})
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    pick = [d["id"] for d in docs[:2]]
    assert client.post(f"/api/tabs/{tid}/deep-compare", json={"doc_ids": pick}).json()["started"]
    _wait_read(client, tid)
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any(m["role"] == "s" and "2/2 candidates at FULL text" in m["text"] for m in msgs)
    assert any(p["title"] == "2 of 3 candidates · full text" for p in msgs[-1]["participants"])
    q = [m for m in msgs if m["role"] == "q"][-1]
    assert "2 of 3 candidates" in q["text"]
    # unknown ids only -> 400
    assert client.post(f"/api/tabs/{tid}/deep-compare",
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
    _wait_read(client, tab["id"])
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
    _wait_read(client, tab["id"])
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


# ---------- grounding / focus / concise-vs-full / paragraph markers ----------

def test_paragraph_number_marker():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<description-paragraph num="0035">Body text</description-paragraph>', "lxml")
    el = soup.find("description-paragraph")
    assert fetcher._with_para_num(el, "Body text") == "[0035] Body text"
    # non-numeric / missing num → returned unchanged
    p = BeautifulSoup("<p>x</p>", "lxml").find("p")
    assert fetcher._with_para_num(p, "x") == "x"
    # idempotent: don't double-prefix
    assert fetcher._with_para_num(el, "[0035] Body text") == "[0035] Body text"


def test_build_prompt_grounding_and_concise_default():
    docs = [{"id": 1, "number": "US1", "title": "t", "abstract": "a",
             "digest": "DIG", "claims": "1. c", "description": "[0035] body"}]
    p = claude_bridge.build_prompt("q", documents=docs)          # full=False default
    assert "GROUNDING" in p and "REFUSE-TO-GUESS" in p
    assert "DERIVED summary" in p                                # digest labelled, not quotable
    assert "KISS" in p                                           # concise, plain, readable by default
    assert "ENGLISH ONLY" in p and "ARGUE THE DISCLOSURE" in p   # English-only + reasoned-citation rules
    assert "CITE WITH ITS EXACT QUOTE" in p                      # every cite carries its short exact quote
    assert "CLIPPED" in p                                        # candidate block warns it's not full text


def test_build_prompt_focus_full_text_and_full_style():
    docs = [{"id": 7, "number": "CN692", "title": "t", "abstract": "a",
             "digest": "DIG", "claims": "1. c", "description": "[0057] AGC stuff"}]
    p = claude_bridge.build_prompt("q", focus=docs, full=True)
    assert "FOCUSED CANDIDATE" in p and "FULL primary text" in p
    assert "[0057] AGC stuff" in p                               # full description present, uncl­ipped
    assert "FULL analysis" in p                                  # full style instruction


def test_chat_focus_and_full_plumbed(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: (seen.update(k), {"answer": "ok", "model": "m"})[1])
    tab = client.post("/api/tabs", json={"name": "T"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    did = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"][0]["id"]
    client.post(f"/api/tabs/{tab['id']}/chat",
                json={"question": "q", "full": True, "focus_ids": [did]})
    assert seen.get("full") is True
    assert seen.get("focus") and seen["focus"][0]["id"] == did
    # the focused doc is pulled OUT of the clipped documents list
    assert all(d["id"] != did for d in (seen.get("documents") or []))


def test_strip_cjk_removes_all_chinese():
    s = ('No. It interposes a measurement and control device (测控装置) between them; '
         'claim 5 / [0018] make the two-hop path explicit "硬接线" wiring.')
    out = claude_bridge._strip_cjk(s)
    assert not __import__("re").search(r"[一-鿿]", out)   # zero CJK
    assert "[0018]" in out and "claim 5" in out            # locators preserved
    assert "(测控装置)" not in out                          # gloss removed whole


def test_auto_focus_matches_number_and_alias():
    docs = [{"id": 1, "number": "CN113964850"}, {"id": 2, "number": "US11909216B2"},
            {"id": 3, "number": "CN120638382A"}]
    assert api._auto_focus_ids("does 850 disclose the difference node?", docs) == [1]      # short alias
    assert api._auto_focus_ids("compare CN113964850 vs US11909216B2", docs) == [1, 2]      # full numbers
    assert api._auto_focus_ids("what about the EMS generally?", docs) == []                # no reference
    assert 3 in api._auto_focus_ids("benchmark 382 vs 850", docs)                          # multiple aliases


def test_chat_auto_focuses_named_candidate(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: (seen.update(k), {"answer": "ok", "model": "m"})[1])
    tab = client.post("/api/tabs", json={"name": "AF"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "CN113964850 US11909216B2"})
    # ask about 850 WITHOUT selecting it → it must be auto-focused (full text)
    client.post(f"/api/tabs/{tab['id']}/chat", json={"question": "does 850 disclose X?"})
    foc = [d["number"] for d in (seen.get("focus") or [])]
    assert "CN113964850" in foc and "US11909216B2" not in foc


def test_chat_focus_is_sticky_across_turns(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: (seen.update(k), {"answer": "ok", "model": "m"})[1])
    tab = client.post("/api/tabs", json={"name": "Sticky"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "CN113964850 US11909216B2"})
    # turn 1 names 850 → focused
    client.post(f"/api/tabs/{tab['id']}/chat", json={"question": "tell me about 850"})
    assert "CN113964850" in [d["number"] for d in (seen.get("focus") or [])]
    # turn 2 does NOT name 850 → still focused via sticky (recent-question) match
    seen.clear()
    client.post(f"/api/tabs/{tab['id']}/chat", json={"question": "and its description paragraphs?"})
    assert "CN113964850" in [d["number"] for d in (seen.get("focus") or [])]
