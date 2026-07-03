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
    # auto-create-notebook is exercised by its own tests with stubs; keep it off by
    # default so the other tests stay notebook-less unless they opt in.
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", False)
    # Figures default ON in prod, but captioning shells to vision + network — keep it
    # off for the generic tests; the figure test opts in with stubs.
    monkeypatch.setattr(api, "AUTO_FIGURES", False)
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


def test_skills_exposes_answer_formats(client):
    r = client.get("/api/skills").json()
    keys = [f["key"] for f in r["answer_formats"]]
    assert {"", "one-sentence", "claim-map", "claim-map-pragmatic",
            "feature-map"} <= set(keys)                    # default + presets
    assert all("label" in f for f in r["answer_formats"])


def test_chat_passes_answer_format_through(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: seen.update(k) or {"answer": "ok", "model": "m"})
    tab = client.post("/api/tabs", json={"name": "T"}).json()
    client.post(f"/api/tabs/{tab['id']}/chat",
                json={"question": "map the claims", "answer_format": "claim-map"})
    assert seen["answer_format"] == "claim-map"


def test_build_prompt_claim_map_replaces_style_line():
    p = claude_bridge.build_prompt("q", answer_format="claim-map")
    assert "ONE-LINE CLAIM MAP" in p
    assert "ANSWER STYLE" not in p                      # format replaces the style line
    # unknown / empty key falls back to the normal style instruction
    assert "ANSWER STYLE" in claude_bridge.build_prompt("q", answer_format="")
    assert "ANSWER STYLE" in claude_bridge.build_prompt("q", answer_format="bogus")


def test_build_prompt_claim_map_pragmatic():
    p = claude_bridge.build_prompt("q", answer_format="claim-map-pragmatic")
    assert "PRAGMATIC CLAIM MAP" in p
    assert "standard design practice" in p                 # the obviousness verdict
    assert "ANSWER STYLE" not in p                         # preset replaces the style line


def test_build_prompt_feature_map():
    p = claude_bridge.build_prompt("A power supply circuit…", answer_format="feature-map")
    assert "INTERLINEAR FEATURE MAP" in p
    assert "ANSWER STYLE" not in p                      # preset replaces the style line


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


def test_ask_notebook_fans_out_across_rollover_siblings(client, monkeypatch):
    """Candidates beyond the source cap live in sibling notebooks (auto-rollover).
    A query must hit ALL of them, not just the connected one — otherwise patents in
    the siblings are unreachable (the EP3087655 bug)."""
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": [
        {"id": "nb-3", "title": "Cands (3)"}, {"id": "nb-2", "title": "Cands (2)"}]})
    tab = client.post("/api/tabs", json={"name": "Roll"}).json()
    # connected notebook = the latest; an earlier candidate was exported to a sibling
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-3", "notebook_title": "Cands (3)", "source_ids": []})
    db.add_text_document(tab["id"], "EP3087655", "Old candidate", "body",
                         nlm_source_notebook="nb-2")
    r = client.post(f"/api/tabs/{tab['id']}/ask-notebook", json={"question": "ports?"}).json()
    text = r["messages"][0]["text"]
    assert "nlm[nb-3]" in text and "nlm[nb-2]" in text          # both notebooks queried
    titles = [p["title"] for p in r["messages"][0]["participants"]]
    assert titles == ["Cands (3)", "Cands (2)"]                 # connected first


def test_chat_ask_notebook_fans_out_to_claude(client, monkeypatch):
    """The /chat path feeds EVERY sibling notebook's answer to Claude for synthesis."""
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: seen.update(sources=k.get("sources")) or
                        {"answer": "synthesized", "model": "claude-fable-5"})
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": [
        {"id": "nb-3", "title": "Cands (3)"}, {"id": "nb-2", "title": "Cands (2)"}]})
    tab = client.post("/api/tabs", json={"name": "Roll2"}).json()
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-3", "notebook_title": "Cands (3)", "source_ids": []})
    db.add_text_document(tab["id"], "EP3087655", "Old", "body", nlm_source_notebook="nb-2")
    client.post(f"/api/tabs/{tab['id']}/chat",
                json={"question": "ports?", "ask_notebook": True}).json()
    assert {s["title"] for s in seen["sources"]} == {"Cands (3)", "Cands (2)"}


def test_chat_corrects_citation_for_non_focused_candidate(client, monkeypatch):
    """The model cites a candidate that ISN'T in focus (number pulled from the
    conversation) with a wrong [00NN]. The API-layer verifier loads that candidate's
    full text from the DB and corrects the locator. Regression for EP3087655 [0029]."""
    tab = client.post("/api/tabs", json={"name": "Cite"}).json()
    db.add_text_document(tab["id"], "EP3087655", "Power supply system",
                         "[0024] wattmeter 72 connected.\n\n"
                         "[0025] By control of the switches 62 and 63, outputs of the PVPCSs 6 and 16 "
                         "can be stopped.\n\n[0029] the BATPCS 81 and the PVPCSs 6 and 16 are started.")
    monkeypatch.setattr(claude_bridge, "chat", lambda *a, **k: {
        "answer": 'In EP3087655 [0029]: "outputs of the PVPCSs 6 and 16 can be stopped".',
        "model": "claude-sonnet-4-6"})
    r = client.post(f"/api/tabs/{tab['id']}/chat", json={"question": "ports?"}).json()
    c_msg = next(m for m in r["messages"] if m["role"] == "c")
    assert "[0025]" in c_msg["text"] and "[0029]" not in c_msg["text"]


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
    db.update_document(docs["EP3667902A1"]["id"], score=7, score_note="old", scored_at=9_999_999_999, score_model="claude-opus-4-8")
    read = []
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: (read.append(d["number"]), {"verdict": f"MATCH SCORE: 5 for {d['number']}"})[1])
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "ranking"})
    client.post(f"/api/tabs/{tid}/deep-compare",
                json={"skip_scored": True, "reading_model": "claude-sonnet-4-6"})
    _wait_read(client, tid)
    assert read == ["CN114853847"]                      # only the unread one was read
    after = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert after["EP3667902A1"]["score"] == 7           # already-read one untouched
    assert after["CN114853847"]["score"] == 5           # newly read
    assert after["CN114853847"]["score_model"] == "claude-sonnet-4-6"   # records which model read it


def test_continue_is_model_aware_upgrades_weaker_reads(client, monkeypatch):
    """The interrupted-sonnet-over-221 case: Continue with a STRONGER reading model
    re-reads candidates still on a weaker model (haiku) but skips ones already read by
    that model or stronger (sonnet/opus). This is what resumes an upgrade-read on just
    the leftovers across token-budget windows without re-reading the strong-read ones."""
    tab = client.post("/api/tabs", json={"name": "Upgrade"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"text": "EP3667902A1 CN114853847B CN114547092 CN117241689"})
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    # prior batch: one read by sonnet (done at this level), one by opus (stronger),
    # one by haiku (WEAKER — must be upgraded). The 4th is unread.
    db.update_document(docs["EP3667902A1"]["id"], score=8, scored_at=9_999_999_999, score_model="claude-sonnet-4-6")
    db.update_document(docs["CN114853847"]["id"], score=7, scored_at=9_999_999_999, score_model="claude-opus-4-8")
    db.update_document(docs["CN114547092"]["id"], score=3, scored_at=9_999_999_999, score_model="claude-haiku-4-5")
    read = []
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: read.append(d["number"]) or {"verdict": f"MATCH SCORE: 6 for {d['number']}"})
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "ranking"})
    client.post(f"/api/tabs/{tid}/deep-compare",
                json={"skip_scored": True, "reading_model": "claude-sonnet-4-6"})
    _wait_read(client, tid)
    # the haiku-read one is upgraded, the unread one is read; sonnet/opus ones skipped
    assert set(read) == {"CN114547092", "CN117241689"}
    after = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert after["EP3667902A1"]["score"] == 8           # sonnet read untouched
    assert after["CN114853847"]["score"] == 7           # opus read untouched (no downgrade)
    assert after["CN114547092"]["score_model"] == "claude-sonnet-4-6"   # upgraded to sonnet


def test_rerank_reuses_stored_reads_without_rereading(client, monkeypatch):
    """The core demand: once read ANYWHERE, an assessment is reused EVERYWHERE. A
    Continue/re-rank with everything already read must do ZERO reads yet rank the
    WHOLE corpus — including a legacy doc that has only a score (no stored verdict)."""
    tab = client.post("/api/tabs", json={"name": "Reuse"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1 CN114547092"})
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    # one fully-read (verdict stored), one LEGACY read (score only, verdict NULL)
    db.update_document(docs["EP3667902A1"]["id"], verdict="MATCH SCORE: 8\n[0012]: foo",
                       score=8, scored_at=9_999_999_999, score_model="claude-sonnet-4-6")
    db.update_document(docs["CN114547092"]["id"], score=4, score_note="legacy note",
                       scored_at=9_999_999_999, score_model="claude-opus-4-8")
    reads, ranked = [], {}
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: reads.append(d["number"]) or {"verdict": "x"})
    monkeypatch.setattr(claude_bridge, "deep_reduce",
                        lambda q, bm, verdicts, **k: ranked.update(v=verdicts) or {"answer": "ranking"})
    r = client.post(f"/api/tabs/{tid}/deep-compare", json={"skip_scored": True}).json()
    assert r.get("started")
    _wait_read(client, tid)
    assert reads == []                                  # NOTHING was re-read
    nums = {v["number"] for v in ranked["v"]}
    assert nums == {"EP3667902A1", "CN114547092"}       # BOTH ranked (legacy one included)
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any(m["role"] == "q" and "Re-rank" in m["text"] for m in msgs)


def test_notebook_resync_retracks_and_finds_duplicates(client, monkeypatch):
    """Resync reconciles app↔NLM: a candidate physically in a notebook but untracked
    gets re-tracked, and a candidate present in TWO notebooks is reported as a duplicate."""
    tab = client.post("/api/tabs", json={"name": "Resync"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-a", "notebook_title": "A", "source_ids": []})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1 CN114853847B CN114547092"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": [
        {"id": "nb-a", "title": "A", "sources": 2}, {"id": "nb-b", "title": "B", "sources": 1}]})
    # EP3667902 is in BOTH notebooks (duplicate); CN114853847 only in B; CN114547092 in none
    srcs = {
        "nb-a": [{"id": "sa1", "title": "EP3667902A1 — foo"}],
        "nb-b": [{"id": "sb1", "title": "EP3667902 — foo (copy)"},
                 {"id": "sb2", "title": "CN114853847B — bar"}],
    }
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": srcs.get(nb, [])})
    # scan both notebooks explicitly
    r = client.post(f"/api/tabs/{tid}/notebook/resync",
                    json={"notebook_ids": ["nb-a", "nb-b"]}).json()
    assert r["ok"] and r["in_nlm"] == 2          # EP3667902 + CN114853847 now tracked
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert docs["EP3667902A1"]["nlm_source_notebook"] in ("nb-a", "nb-b")
    assert docs["CN114853847"]["nlm_source_notebook"] == "nb-b"
    assert docs["CN114547092"]["nlm_source_notebook"] is None     # genuinely not in NLM
    dups = {d["number"]: d for d in r["duplicates"]}
    assert "EP3667902A1" in dups and len(dups["EP3667902A1"]["locations"]) == 2


def test_notebook_distribute_fills_across_notebooks(client, monkeypatch):
    """Auto-split fills the first notebook to its cap, then spills the rest into the next."""
    tab = client.post("/api/tabs", json={"name": "Split"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1 CN114853847B CN114547092 CN117241689"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": [
        {"id": "nb-a", "title": "A", "sources": 0}, {"id": "nb-b", "title": "B", "sources": 0}]})
    store, cap = {"nb-a": 0, "nb-b": 0}, {"nb-a": 2, "nb-b": 5}
    def fake_add(nb, title, text):
        if store[nb] >= cap[nb]:
            return {"error": "full", "full": True}
        store[nb] += 1
        return {"ok": True}
    monkeypatch.setattr(nlm_bridge, "add_source_text", fake_add)
    ids = [d["id"] for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]]
    r = client.post(f"/api/tabs/{tid}/notebook/distribute",
                    json={"doc_ids": ids, "notebook_ids": ["nb-a", "nb-b"]}).json()
    assert r["ok"] and r["placed"] == 4 and r["remaining"] == 0
    placed = {p["notebook_id"]: p["added"] for p in r["placements"]}
    assert placed == {"nb-a": 2, "nb-b": 2}          # filled A to cap (2), spilled 2 into B
    docs = {d["number"]: d["nlm_source_notebook"]
            for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert sum(1 for v in docs.values() if v == "nb-a") == 2
    assert sum(1 for v in docs.values() if v == "nb-b") == 2


def test_notebook_source_delete_clears_tracking(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "Del"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1"})
    doc = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    db.update_document(doc["id"], nlm_source_notebook="nb-x", nlm_source_id="src-9")
    deleted = {}
    monkeypatch.setattr(nlm_bridge, "delete_source",
                        lambda ids, notebook_id=None: deleted.update(ids=ids, nb=notebook_id) or {"ok": True, "deleted": len(ids)})
    r = client.post(f"/api/tabs/{tid}/notebook/source-delete",
                    json={"notebook_id": "nb-x", "source_ids": ["src-9"]}).json()
    assert r["ok"] and r["deleted"] == 1 and r["cleared"] == 1
    assert deleted == {"ids": ["src-9"], "nb": "nb-x"}
    after = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    assert after["nlm_source_notebook"] is None      # tracking cleared after deletion


def test_nlm_shortlist_matches_candidates(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "Short"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    # benchmark as a feature combination (the spec is the question to NLM)
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) a fuel-gauge IC.\nB) a thermistor via voltage divider.",
                      "title": "gauge + thermistor"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689", "US10395648B1"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    # NLM names two of the three candidates (one with a different kind code) + a stranger
    monkeypatch.setattr(nlm_bridge, "query", lambda nb, q, source_ids=None: {
        "answer": "SHORTLIST: EP4340163 (kind-insensitive), CN117241689, "
                  "and also WO2022239372 which is not in the pool. BEST: EP4340163."})
    r = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={}).json()
    assert r["ok"] is True
    assert set(r["matched"]) == {"EP4340163A1", "CN117241689"}    # kind-code-insensitive match
    assert "US10395648B1" not in r["matched"]
    assert "WO2022239372" in r["unmatched"]                       # named but not a candidate
    assert len(r["shortlist_ids"]) == 2
    # NLM's reasoning is posted to chat for the user to review
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any(m["role"] == "c" and "EP4340163" in m["text"] for m in msgs)
    # the picks are PERSISTED (shortlisted=1) so 🧺 Consolidate reuses them after a reload
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    sl = {d["number"]: d["shortlisted"] for d in docs}
    assert sl["EP4340163A1"] == 1 and sl["CN117241689"] == 1 and sl["US10395648B1"] == 0
    # NLM's best-first ORDER is persisted as nlm_rank (1,2…) → the consensus tie-breaker
    rk = {d["number"]: d["nlm_rank"] for d in docs}
    assert rk["EP4340163A1"] == 1 and rk["CN117241689"] == 2 and rk["US10395648B1"] is None
    # coverage is disclosed: none of these candidates are NLM sources here, so the
    # summary must say so rather than implying it ranked over the whole pool
    summ = [m for m in msgs if m["role"] == "s" and "Coverage" in m["text"]]
    assert summ and "0 of 3 candidate(s) are NLM sources" in summ[0]["text"]
    assert "3 are NOT in NLM" in summ[0]["text"]


def test_nlm_shortlist_ranks_best_with_feature_map(client, monkeypatch):
    """Shortlist now also returns the ranked best + second-best (merged-in best-match)."""
    tab = client.post("/api/tabs", json={"name": "Rank"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) a fuel-gauge IC.\nB) a thermistor via voltage divider.",
                      "title": "gauge + thermistor"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689", "US10395648B1"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    calls = []
    def fake_q(nb, q, source_ids=None):
        calls.append(q)
        return {"answer": "SHORTLIST: EP4340163, CN117241689. BEST: EP4340163 — A) YES, B) YES. "
                          "SECOND-BEST: CN117241689 — A) YES, B) NO."}
    monkeypatch.setattr(nlm_bridge, "query", fake_q)
    r = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={}).json()
    assert r["ok"] is True
    assert set(r["matched"]) == {"EP4340163A1", "CN117241689"}
    assert len(calls) == 1                                       # one fan-out query, not per-candidate
    assert "BEST" in calls[0] and "FEATURE MAP" in calls[0]      # prompt asks for ranking + per-feature map


def test_notebook_delete_account_disconnects_tabs(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(nlm_bridge, "delete_notebook",
                        lambda nb: deleted.append(nb) or {"ok": True})
    tab = client.post("/api/tabs", json={"name": "Del"}).json()
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-doomed", "notebook_title": "NB", "source_ids": [],
                     "auto_add": False})
    r = client.delete("/api/notebooks/nb-doomed")
    assert r.status_code == 200 and deleted == ["nb-doomed"]
    # the tab connected to it is disconnected (no dangling notebook to query)
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    assert not (st["notebook"] and st["notebook"].get("notebook_id"))


def test_notebook_create_at_cap_returns_helpful_error(client, monkeypatch):
    # nlm_bridge maps the cryptic INVALID_ARGUMENT into an actionable cap message
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda t: {"limit": True, "error": "NotebookLM refused to create the "
                                   "notebook — your account has 100 notebooks (caps at ~100). "
                                   "Delete some old notebooks to free a slot, then try again."})
    tab = client.post("/api/tabs", json={"name": "Cap"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/notebook/create", json={"title": "X"})
    assert r.status_code == 400 and "100" in r.json()["detail"]


def test_notebook_consolidate_creates_and_copies(client, monkeypatch):
    added = []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: added.append((nb, title)) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda t: {"id": "nb-consol", "title": t})
    tab = client.post("/api/tabs", json={"name": "Cons"}).json()
    tid = tab["id"]
    # candidates already live in another notebook (auto_add off so the pipeline doesn't mirror)
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": [],
                     "auto_add": False})
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1 CN114547092"})
    ids = [d["id"] for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]]
    r = client.post(f"/api/tabs/{tid}/notebook/consolidate",
                    json={"title": "Best picks — Cons", "doc_ids": ids,
                          "include_benchmark": True}).json()
    assert r["ok"] is True and not r["full"]
    assert r["added"] == len(ids) + 1                       # candidates + benchmark
    assert r["notebook"]["notebook_id"] == "nb-consol"      # tab now connected to the new one
    assert all(nb == "nb-consol" for nb, _ in added)        # everything copied into it
    assert any("BENCHMARK" in t for _, t in added)


def test_nlm_shortlist_scoped_to_one_notebook(client, monkeypatch):
    """notebook_id restricts the query to a single (e.g. consolidated) notebook."""
    tab = client.post("/api/tabs", json={"name": "Scoped"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-consol", "notebook_title": "C", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) x.\nB) y.", "title": "feats"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    seen = []
    monkeypatch.setattr(nlm_bridge, "query", lambda nb, q, source_ids=None: (
        seen.append(nb) or {"answer": "BEST: EP4340163. SECOND-BEST: CN117241689."}))
    r = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={"notebook_id": "nb-consol"}).json()
    assert r["ok"] is True
    assert seen == ["nb-consol"]                       # queried ONLY the consolidated notebook
    assert set(r["matched"]) == {"EP4340163A1", "CN117241689"}


def test_nlm_query_cache_avoids_rerun(client, monkeypatch):
    """An identical NotebookLM query is served from the persistent cache — no re-run."""
    tab = client.post("/api/tabs", json={"name": "Cache"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) a fuel-gauge IC.\nB) a thermistor via voltage divider.",
                      "title": "gauge + thermistor"})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4340163A1"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    calls = []
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: calls.append(1) or {"answer": "BEST: EP4340163."})
    r1 = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={"notebook_id": "nb-1"}).json()
    r2 = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={"notebook_id": "nb-1"}).json()
    assert r1["ok"] and r2["ok"]
    assert len(calls) == 1                      # second identical query came from cache


def test_nlm_challenge_debates_finalists_both_sides(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "Chal"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) a fuel-gauge IC.\nB) a thermistor via voltage divider.",
                      "title": "feats"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    # NLM finalist = the persisted shortlist (what NotebookLM picked, in its notebook)
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    _db.set_shortlisted(tid, [docs[0]["id"]])
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "add_source_text", lambda *a, **k: {"ok": True})
    nlm_calls, claude_calls = [], []
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: nlm_calls.append(q) or {
                            "answer": f"{docs[0]['number']}: A) YES, B) PARTIAL."})
    debate_models = []
    def fake_debate(blocks, finals, nlm, model=None):
        claude_calls.append((finals, nlm)); debate_models.append(model)
        return {"answer": "Consensus: agree on A, dispute B.", "model": model}
    monkeypatch.setattr(claude_bridge, "debate", fake_debate)
    r = client.post(f"/api/tabs/{tid}/nlm-challenge", json={}).json()
    assert r["ok"] is True
    assert r["finalists"] == [docs[0]["number"]]          # debates NLM's finalist, not Claude's picks
    assert len(nlm_calls) == 1 and len(claude_calls) == 1  # one prompt per side
    assert docs[0]["number"] in nlm_calls[0]              # finalist named to NLM
    assert docs[0]["number"] in claude_calls[0][0]        # finalist digest given to Claude
    assert debate_models[0] == "claude-opus-4-8"         # reconciliation runs on opus, not haiku
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any(m["role"] == "c" and "NotebookLM" in m["text"] for m in msgs)
    assert any(m["role"] == "c" and "Claude" in m["text"] for m in msgs)


def test_pipeline_runs_all_steps_and_reports_done(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "Pipe"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) a fuel-gauge IC.\nB) a thermistor via voltage divider.",
                      "title": "feats"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    ids = [d["id"] for d in docs]
    by_num = {d["number"]: d["id"] for d in docs}
    # Claude's #1 = CN117241689 (higher score); NLM will pick EP4340163A1 → genuine divergence
    _db.update_document(by_num["CN117241689"], score=9, scored_at=1, score_model="claude-sonnet-4-6")
    _db.update_document(by_num["EP4340163A1"], score=5, scored_at=1, score_model="claude-sonnet-4-6")
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "create_notebook", lambda t: {"id": "nb-pipe", "title": t})
    monkeypatch.setattr(nlm_bridge, "wait_sources_ready", lambda *a, **k: {"ready": True, "processed": 1, "total": 1})
    monkeypatch.setattr(nlm_bridge, "add_source_text", lambda nb, ti, tx: {"ok": True})
    deleted = []
    monkeypatch.setattr(nlm_bridge, "delete_notebook", lambda nb: deleted.append(nb) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": [{"id": "s1", "title": "EP4340163A1"}]})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "SHORTLIST: EP4340163A1. "
                                                        "BEST: EP4340163A1. A) YES."})
    debated = []
    monkeypatch.setattr(claude_bridge, "debate",
                        lambda *a, **k: debated.append(1) or {"answer": "reconciled",
                                                              "model": "claude-opus-4-8"})
    r = client.post(f"/api/tabs/{tid}/pipeline",
                    json={"title": "Best picks — Pipe", "doc_ids": ids, "include_benchmark": True}).json()
    assert r["started"] is True
    # the job runs in a daemon thread; poll status until it finishes
    import time as _t
    for _ in range(50):
        s = client.get(f"/api/tabs/{tid}/pipeline/status").json()
        if s.get("phase") == "done":
            break
        _t.sleep(0.1)
    s = client.get(f"/api/tabs/{tid}/pipeline/status").json()
    assert s["phase"] == "done"
    cfg = client.get(f"/api/tabs/{tid}/state").json()["notebook"]
    assert cfg["notebook_id"] == "nb-pipe"          # consolidate step connected the tab
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any("Consolidated" in m["text"] for m in msgs)
    assert any("diverge" in m["text"] for m in msgs)        # divergence gate fired
    assert debated                                          # → opus reconciliation ran
    assert any(m["role"] == "c" and "NotebookLM" in m["text"] for m in msgs)   # debate posted


def test_pipeline_consensus_skips_debate(client, monkeypatch):
    """When Claude's #1 and NotebookLM's #1 agree, the pipeline posts agreement and does
    NOT spend an opus reconciliation."""
    tab = client.post("/api/tabs", json={"name": "Agree"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    ids = [d["id"] for d in docs]
    by_num = {d["number"]: d["id"] for d in docs}
    _db.update_document(by_num["EP4340163A1"], score=9, scored_at=1, score_model="claude-sonnet-4-6")
    _db.update_document(by_num["CN117241689"], score=4, scored_at=1, score_model="claude-sonnet-4-6")
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "create_notebook", lambda t: {"id": "nb-agree", "title": t})
    monkeypatch.setattr(nlm_bridge, "wait_sources_ready", lambda *a, **k: {"ready": True, "processed": 1, "total": 1})
    monkeypatch.setattr(nlm_bridge, "add_source_text", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "delete_notebook", lambda nb: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": [{"id": "s1", "title": "EP4340163A1"}]})
    monkeypatch.setattr(nlm_bridge, "query",   # NLM agrees: EP4340163A1 is best
                        lambda nb, q, source_ids=None: {"answer": "SHORTLIST: EP4340163A1. BEST: EP4340163A1."})
    debated = []
    monkeypatch.setattr(claude_bridge, "debate", lambda *a, **k: debated.append(1) or {"answer": "x"})
    client.post(f"/api/tabs/{tid}/pipeline",
                json={"title": "Agree", "doc_ids": ids, "include_benchmark": True})
    import time as _t
    for _ in range(50):
        if client.get(f"/api/tabs/{tid}/pipeline/status").json().get("phase") == "done":
            break
        _t.sleep(0.1)
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any("Independent agreement" in m["text"] for m in msgs)
    assert not debated                                      # no opus reconciliation when they agree


def test_pipeline_funnel_auto_selects_top_n(client, monkeypatch):
    """With no doc_ids, the pipeline funnels Claude's top_n best-scored candidates."""
    tab = client.post("/api/tabs", json={"name": "Funnel"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689", "US10395648B1"], "source": "image"})
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    by_num = {d["number"]: d["id"] for d in docs}
    _db.update_document(by_num["EP4340163A1"], score=9, scored_at=1, score_model="claude-sonnet-4-6")
    _db.update_document(by_num["CN117241689"], score=7, scored_at=1, score_model="claude-sonnet-4-6")
    _db.update_document(by_num["US10395648B1"], score=2, scored_at=1, score_model="claude-sonnet-4-6")
    copied = []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "create_notebook", lambda t: {"id": "nb-fn", "title": t})
    monkeypatch.setattr(nlm_bridge, "wait_sources_ready", lambda *a, **k: {"ready": True, "processed": 1, "total": 1})
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, ti, tx: copied.append(ti) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "delete_notebook", lambda nb: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": []})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "SHORTLIST: EP4340163A1. BEST: EP4340163A1."})
    monkeypatch.setattr(claude_bridge, "debate", lambda *a, **k: {"answer": "x", "model": "claude-opus-4-8"})
    # no doc_ids → auto-pick the top 2 by score (US10395648B1 score=2 excluded)
    r = client.post(f"/api/tabs/{tid}/pipeline", json={"title": "Funnel", "top_n": 2}).json()
    assert r["started"] is True and r["funnel_n"] == 2
    import time as _t
    for _ in range(50):
        if client.get(f"/api/tabs/{tid}/pipeline/status").json().get("phase") == "done":
            break
        _t.sleep(0.1)
    titles = " ".join(copied)
    assert "EP4340163A1" in titles and "CN117241689" in titles   # top 2 copied
    assert "US10395648B1" not in titles                          # the low-scorer was NOT funneled


def test_pipeline_deletes_rollover_notebooks(client, monkeypatch):
    """After consolidating into one notebook, the rollover notebooks the candidates were
    spread across are deleted and their refs cleared."""
    tab = client.post("/api/tabs", json={"name": "Roll"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    ids = [d["id"] for d in docs]
    # the candidates currently live in two OLD rollover notebooks
    _db.update_document(ids[0], nlm_source_notebook="nb-old-A", score=9, scored_at=1, score_model="x")
    _db.update_document(ids[1], nlm_source_notebook="nb-old-B", score=5, scored_at=1, score_model="x")
    deleted, cleared = [], []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "create_notebook", lambda t: {"id": "nb-new", "title": t})
    monkeypatch.setattr(nlm_bridge, "wait_sources_ready", lambda *a, **k: {"ready": True, "processed": 1, "total": 1})
    monkeypatch.setattr(nlm_bridge, "add_source_text", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "delete_notebook", lambda nb: deleted.append(nb) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": []})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "SHORTLIST: EP4340163A1. BEST: EP4340163A1."})
    monkeypatch.setattr(claude_bridge, "debate", lambda *a, **k: {"answer": "x", "model": "claude-opus-4-8"})
    client.post(f"/api/tabs/{tid}/pipeline", json={"title": "Roll", "doc_ids": ids})
    import time as _t
    for _ in range(50):
        if client.get(f"/api/tabs/{tid}/pipeline/status").json().get("phase") == "done":
            break
        _t.sleep(0.1)
    assert set(deleted) == {"nb-old-A", "nb-old-B"}     # both rollovers deleted
    assert _db.tab_notebook_ids(tid) == ["nb-new"]      # only the consolidated notebook remains


def test_pipeline_frees_slots_before_create(client, monkeypatch):
    """At the 100-notebook cap, the funnel must delete the tab's rollovers BEFORE creating
    the consolidated notebook — otherwise create fails before any slot is freed."""
    tab = client.post("/api/tabs", json={"name": "Cap"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    import patentbench.db as _db
    ids = [d["id"] for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]]
    _db.update_document(ids[0], nlm_source_notebook="nb-old-A", score=9, scored_at=1, score_model="x")
    _db.update_document(ids[1], nlm_source_notebook="nb-old-B", score=5, scored_at=1, score_model="x")
    order = []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "delete_notebook", lambda nb: order.append(("del", nb)) or {"ok": True})
    # create fails while at the cap (2 notebooks still alive), succeeds once a slot is freed
    def fake_create(t):
        live = 2 - sum(1 for o in order if o[0] == "del")
        order.append(("create", live))
        if live >= 2:
            return {"limit": True, "error": "account has 100 notebooks (caps at ~100)"}
        return {"id": "nb-new", "title": t}
    monkeypatch.setattr(nlm_bridge, "create_notebook", fake_create)
    monkeypatch.setattr(nlm_bridge, "wait_sources_ready", lambda *a, **k: {"ready": True, "processed": 1, "total": 1})
    monkeypatch.setattr(nlm_bridge, "add_source_text", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": []})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "SHORTLIST: EP4340163A1. BEST: EP4340163A1."})
    monkeypatch.setattr(claude_bridge, "debate", lambda *a, **k: {"answer": "x", "model": "claude-opus-4-8"})
    client.post(f"/api/tabs/{tid}/pipeline", json={"title": "Cap", "doc_ids": ids})
    import time as _t
    for _ in range(50):
        if client.get(f"/api/tabs/{tid}/pipeline/status").json().get("phase") == "done":
            break
        _t.sleep(0.1)
    # both deletes happened, and the (successful) create came AFTER them — not before
    assert [o for o in order if o[0] == "del"] == [("del", "nb-old-A"), ("del", "nb-old-B")]
    assert order[-1][0] == "create" and order[-1][1] == 0       # created only once slots were free
    assert _db.tab_notebook_ids(tid) == ["nb-new"]


def test_pipeline_funnel_needs_scored_candidates(client, monkeypatch):
    """No explicit finalists and nothing scored yet → 400 (run deep-compare first)."""
    tab = client.post("/api/tabs", json={"name": "PipeNo"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    assert client.post(f"/api/tabs/{tid}/pipeline", json={"title": "X"}).status_code == 400


def test_pipeline_consolidate_only_stops_before_query(client, monkeypatch):
    """consolidate_only=True copies the finalists into one notebook and STOPS — no NLM
    shortlist query, no debate. The user drives those steps manually afterwards."""
    tab = client.post("/api/tabs", json={"name": "Only"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    by_num = {d["number"]: d["id"] for d in docs}
    _db.update_document(by_num["EP4340163A1"], score=9, scored_at=1, score_model="x")
    _db.update_document(by_num["CN117241689"], score=4, scored_at=1, score_model="x")
    copied = []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "create_notebook", lambda t: {"id": "nb-only", "title": t})
    monkeypatch.setattr(nlm_bridge, "add_source_text", lambda nb, ti, tx: copied.append(ti) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "delete_notebook", lambda nb: {"ok": True})
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {"sources": []})
    queried = []
    monkeypatch.setattr(nlm_bridge, "query", lambda *a, **k: queried.append(1) or {"answer": "x"})
    debated = []
    monkeypatch.setattr(claude_bridge, "debate", lambda *a, **k: debated.append(1) or {"answer": "x"})
    r = client.post(f"/api/tabs/{tid}/pipeline",
                    json={"title": "Only", "top_n": 49, "consolidate_only": True}).json()
    assert r["started"] is True
    import time as _t
    for _ in range(50):
        if client.get(f"/api/tabs/{tid}/pipeline/status").json().get("phase") == "done":
            break
        _t.sleep(0.1)
    assert copied                                # documents were put into the notebook
    assert not queried                           # but NLM was NEVER queried
    assert not debated                           # and no debate ran
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any("nothing was queried" in m["text"] for m in msgs)


def test_wait_sources_ready_blocks_until_ingested(monkeypatch):
    """The ingestion gate returns only once EVERY source reports content — not while any is
    still 'empty' (un-processed). It probes via source_content (no chat quota)."""
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": [{"id": "s1", "title": "a"},
                                                             {"id": "s2", "title": "b"}]})
    # s1 ready immediately; s2 only on its 2nd probe → the gate must loop once
    calls = {"s2": 0}
    def fake_content(sid):
        if sid == "s1":
            return {"content": "ready"}
        calls["s2"] += 1
        return {"content": "ready"} if calls["s2"] >= 2 else {"error": "empty source content"}
    monkeypatch.setattr(nlm_bridge, "source_content", fake_content)
    slept = []
    rd = nlm_bridge.wait_sources_ready("nb", timeout=100, poll=5, _sleep=lambda s: slept.append(s))
    assert rd == {"ready": True, "processed": 2, "total": 2}
    assert slept == [5]                         # waited exactly one poll for s2 to finish ingesting


def test_wait_sources_ready_times_out_when_stuck(monkeypatch):
    """If a source never ingests, the gate gives up at the deadline and reports the shortfall
    (so the pipeline can warn rather than hang forever)."""
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "list_sources",
                        lambda nb, force=False: {"sources": [{"id": "s1", "title": "a"},
                                                             {"id": "s2", "title": "b"}]})
    monkeypatch.setattr(nlm_bridge, "source_content",
                        lambda sid: {"content": "ok"} if sid == "s1" else {"error": "empty source content"})
    # fake clock: advances past the deadline after a couple of polls
    ticks = iter([0.0, 5.0, 50.0, 200.0, 400.0])
    rd = nlm_bridge.wait_sources_ready("nb", timeout=100, poll=5,
                                       _sleep=lambda s: None, _now=lambda: next(ticks))
    assert rd["ready"] is False and rd["processed"] == 1 and rd["total"] == 2


def test_shortlist_rejects_truncated_answer(client, monkeypatch):
    """A structureless 'thinking' preamble is not cached, is retried, and its stray numbers
    are NOT scraped — the bug that silently dropped DE202022102539."""
    tab = client.post("/api/tabs", json={"name": "Trunc"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4340163A1", "CN117241689"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    calls = []
    # mimics the live dud: names a doc in passing, announces 'next', no verdict markers
    monkeypatch.setattr(nlm_bridge, "query", lambda nb, q, source_ids=None: calls.append(1) or {
        "answer": "Confirming source availability. I'm cross-referencing the texts; EP4340163A1 "
                  "is listed. I'm going to proceed to evaluate CN117241689 next."})
    r = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={"notebook_id": "nb-1"}).json()
    assert r.get("incomplete") is True
    assert r["matched"] == []                       # stray numbers NOT scraped as an assessment
    assert len(calls) == 2                          # retried once before giving up
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any("truncated" in m["text"] for m in msgs)   # warned, not silently accepted
    # and a second run must re-query (the dud was never cached)
    r2 = client.post(f"/api/tabs/{tid}/nlm-shortlist", json={"notebook_id": "nb-1"}).json()
    assert len(calls) == 4


def test_nlm_challenge_includes_claude_top_picks(client, monkeypatch):
    """Claude's high-scored picks are challenged by NLM too (union with the finalists),
    and they get added into the connected notebook so NLM can actually judge them."""
    tab = client.post("/api/tabs", json={"name": "Both"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"spec": "A) a fuel-gauge IC.\nB) a thermistor.", "title": "feats"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4340163A1", "CN117241689", "US10395648B1"], "source": "image"})
    import patentbench.db as _db
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    by_num = {d["number"]: d for d in docs}
    _db.set_shortlisted(tid, [by_num["EP4340163A1"]["id"]])          # NLM finalist
    _db.update_document(by_num["CN117241689"]["id"], score=8.0, score_note="strong")  # Claude pick
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    added = []
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, ti, tx: added.append(ti) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "block-by-block…"})
    monkeypatch.setattr(claude_bridge, "debate",
                        lambda *a, **k: {"answer": "consensus", "model": "claude-opus-4-8"})
    r = client.post(f"/api/tabs/{tid}/nlm-challenge", json={}).json()
    assert r["ok"] is True
    assert "EP4340163A1" in r["finalists"] and "CN117241689" in r["finalists"]  # both sides debated
    assert any("CN117241689" in t for t in added)     # Claude's pick was added into the notebook


def test_nlm_challenge_needs_finalists(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "ChalNo"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": "A) x.\nB) y.", "title": "f"})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4340163A1"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    assert client.post(f"/api/tabs/{tid}/nlm-challenge", json={}).status_code == 400


def test_nlm_shortlist_requires_benchmark(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "ShortNoBm"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": []})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4340163A1"], "source": "image"})
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    assert client.post(f"/api/tabs/{tid}/nlm-shortlist", json={}).status_code == 400


def test_deep_compare_pause_stops_and_continue_finishes(client, monkeypatch):
    import threading, time
    tab = client.post("/api/tabs", json={"name": "Pause"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B", "EP4340163A1"], "source": "image"})
    # gate deep_map so the job is mid-flight when we ask it to pause
    gate = threading.Event()
    seen = []

    def slow_map(bm, d, model=None, features=None):
        seen.append(d["number"])
        gate.wait(2)
        return {"verdict": f"MATCH SCORE: 5 for {d['number']}"}
    monkeypatch.setattr(claude_bridge, "deep_map", slow_map)
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "ranking"})
    monkeypatch.setattr(api, "DIGEST_WORKERS", 1)        # one at a time → pause leaves some un-assessed
    assert client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()["started"] is True
    for _ in range(50):                                   # wait until the first map call is in flight
        if seen:
            break
        time.sleep(0.02)
    p = client.post(f"/api/tabs/{tid}/deep-compare/pause").json()
    assert p["paused"] is True
    gate.set()                                            # let in-flight finish; job pauses after
    _wait_read(client, tid)
    after = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assessed = [n for n, d in after.items() if d["score"] is not None]
    assert 0 < len(assessed) < 3                          # paused mid-way: some done, some not
    # a paused run posts a ⏸ status and compiles NO ranking
    msgs = client.get(f"/api/tabs/{tid}/state").json()["messages"]
    assert any(m["role"] == "s" and "Paused" in m["text"] for m in msgs)
    assert not any(m["role"] == "c" and "ranking" in m["text"] for m in msgs)
    # Continue assesses the rest
    gate.set()
    client.post(f"/api/tabs/{tid}/deep-compare", json={"skip_scored": True})
    _wait_read(client, tid)
    final = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert all(d["score"] is not None for d in final.values())   # all assessed now


def test_deep_compare_honors_chosen_reading_model(client, monkeypatch):
    """The model picked for 'best match' must be the one that assesses each
    candidate AND gets recorded as score_model — and status reports it live."""
    tab = client.post("/api/tabs", json={"name": "Mdl"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1", "CN114853847B"], "source": "image"})
    used = []
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: (used.append(model),
                                                   {"verdict": f"MATCH SCORE: 6 for {d['number']}"})[1])
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "ranking"})
    client.post(f"/api/tabs/{tid}/deep-compare", json={"reading_model": "claude-haiku-4-5"})
    _wait_read(client, tid)
    assert used and all(m == "claude-haiku-4-5" for m in used)     # haiku assessed every candidate
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["score_model"] == "claude-haiku-4-5" for d in docs)  # recorded per row


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


def test_benchmark_by_features(client):
    tab = client.post("/api/tabs", json={"name": "BMfeat"}).json()
    spec = ("TARGET FEATURE COMBINATION (ALL of A-B):\n"
            "A) a rechargeable battery.\nB) a fuel-gauge IC.")
    r = client.post(f"/api/tabs/{tab['id']}/benchmark/features",
                    json={"spec": spec, "title": "battery + gauge"}).json()
    assert r["ok"]
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    bm = st["benchmark"]
    assert bm["source"] == "features" and bm["status"] == "ready"
    assert bm["title"] == "battery + gauge"
    assert bm["number"] is None and bm["text"] is True   # slim view: presence flag
    assert "links" not in bm or bm.get("links") is None
    # full view returns the spec verbatim
    full = client.get(f"/api/tabs/{tab['id']}/benchmark/full").json()
    assert full["text"] == spec
    # chat participant carries the feature title (not "0 file(s)")
    parts = client.post(f"/api/tabs/{tab['id']}/chat",
                        json={"question": "best fit?"}).json()["messages"][-1]["participants"]
    assert any(p["kind"] == "benchmark" and p["title"] == "battery + gauge" for p in parts)
    # too-short spec is rejected by validation
    assert client.post(f"/api/tabs/{tab['id']}/benchmark/features",
                       json={"spec": "tiny"}).status_code == 422


def test_benchmark_features_default_title(client):
    tab = client.post("/api/tabs", json={"name": "BMfeat2"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/benchmark/features",
                    json={"spec": "A) something disclosed.\nB) something else."}).json()
    assert r["ok"]
    assert r["benchmark"]["title"] == "🧩 Feature combination"


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
    assert any(m["role"] == "s" and "ranking ALL 2 candidate(s)" in m["text"] for m in msgs)
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
    assert any(m["role"] == "s" and "Read 2 candidate(s) at FULL text this run" in m["text"]
               for m in msgs)
    assert any(p["title"] == "2 candidates · full text" for p in msgs[-1]["participants"])
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
                        lambda bm, d, model=None, features=None: seen.update(map=model)
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
                        lambda bm, d, model=None, features=None: {"verdict":
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


def test_notebook_add_selected_pushes_only_chosen(client, monkeypatch):
    added = []
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: added.append((nb, title)) or {"ok": True})
    tab = client.post("/api/tabs", json={"name": "AddSel"}).json()
    # connect with auto_add OFF so the pipeline does NOT pre-mirror candidates
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": [],
                     "auto_add": False})
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"text": "US10395648B1 EP3667902A1 CN114547092"})
    assert added == []                                   # auto-add off → nothing mirrored yet
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    chosen = [d["id"] for d in docs[:2]]
    r = client.post(f"/api/tabs/{tab['id']}/notebook/add-selected",
                    json={"doc_ids": chosen, "include_benchmark": False}).json()
    assert r["added"] == 2 and not r["full"] and r["remaining"] == 0
    assert len(added) == 2                               # only the two chosen, not the third
    # re-posting the same ids is idempotent (already in this notebook → skipped)
    added.clear()
    r2 = client.post(f"/api/tabs/{tab['id']}/notebook/add-selected",
                     json={"doc_ids": chosen, "include_benchmark": False}).json()
    assert r2["added"] == 0 and added == []


def test_notebook_add_selected_benchmark_first_then_full(client, monkeypatch):
    added, state = [], {"full_after": 1}
    def fake_add(nb, title, text):
        if len(added) >= state["full_after"]:
            return {"error": "notebook is full (50 sources)", "full": True}
        added.append((nb, title)); return {"ok": True}
    monkeypatch.setattr(nlm_bridge, "add_source_text", fake_add)
    tab = client.post("/api/tabs", json={"name": "AddBm"}).json()
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": [],
                     "auto_add": False})
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "EP3667902A1 CN114547092"})
    ids = [d["id"] for d in client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    r = client.post(f"/api/tabs/{tab['id']}/notebook/add-selected",
                    json={"doc_ids": ids, "include_benchmark": True}).json()
    # benchmark takes the only free slot → reports full, candidates still remaining
    assert r["added"] == 1 and r["full"] is True
    assert any("BENCHMARK" in t for _, t in added)
    assert r["remaining"] == len(ids)


def test_notebook_add_selected_auto_creates_when_none_connected(client, monkeypatch):
    added = []
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: added.append((nb, title)) or {"ok": True})
    monkeypatch.setattr(nlm_bridge, "create_notebook",
                        lambda t: {"id": "nb-made", "title": t})
    # disable the pipeline's own auto-create so the tab stays notebook-less until we add
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", False)
    tab = client.post("/api/tabs", json={"name": "Lonely"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    assert added == []                                   # no notebook → nothing mirrored
    doc_id = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"][0]["id"]
    # add-selected re-enables on its own only when AUTO_CREATE is on
    monkeypatch.setattr(api, "AUTO_CREATE_NOTEBOOK", True)
    r = client.post(f"/api/tabs/{tab['id']}/notebook/add-selected",
                    json={"doc_ids": [doc_id], "include_benchmark": False}).json()
    assert r["added"] == 1 and added and added[0][0] == "nb-made"
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    assert st["notebook"]["notebook_id"] == "nb-made"


def test_notebook_add_selected_to_explicit_notebook(client, monkeypatch):
    """An explicit notebook_id sends docs to THAT notebook, not the connected one."""
    added = []
    monkeypatch.setattr(nlm_bridge, "add_source_text",
                        lambda nb, title, text: added.append((nb, title)) or {"ok": True})
    tab = client.post("/api/tabs", json={"name": "Pick"}).json()
    # tab is connected to nb-1, but we add to a DIFFERENT notebook
    client.put(f"/api/tabs/{tab['id']}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB", "source_ids": [],
                     "auto_add": False})
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    doc_id = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"][0]["id"]
    r = client.post(f"/api/tabs/{tab['id']}/notebook/add-selected",
                    json={"doc_ids": [doc_id], "include_benchmark": False,
                          "notebook_id": "nb-other"}).json()
    assert r["added"] == 1 and r["notebook_id"] == "nb-other"
    assert added and added[0][0] == "nb-other"          # landed in the chosen notebook
    # the tab's connection is untouched
    st = client.get(f"/api/tabs/{tab['id']}/state").json()
    assert st["notebook"]["notebook_id"] == "nb-1"


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


def test_build_prompt_roster_carries_digest_when_focused():
    """With a focus, NON-focused candidates must still expose their already-computed
    digest + score (paid for at read time) — not collapse to bare number+title — so
    the model can reason about them instead of asking the user to reload full text."""
    focus = [{"id": 1, "number": "CN111", "title": "f", "claims": "1. c",
              "description": "[0001] focused body"}]
    others = [{"id": 2, "number": "EP3977876", "title": "Power supply unit",
               "digest": "discloses independent sensing and heating paths",
               "score": 1, "score_note": "thermistor voltage divider"},
              {"id": 4, "number": "CN117295425", "title": "Power supply unit",
               "verdict": "ASSESSMENT: [0015] independent heater path confirmed",
               "score": 2, "score_note": "independent paths"},
              {"id": 3, "number": "CN999", "title": "no digest yet"}]
    p = claude_bridge.build_prompt("q", focus=focus, documents=others)
    assert "discloses independent sensing and heating paths" in p   # digest present
    assert "1/10" in p and "thermistor voltage divider" in p        # score + note present
    assert "DERIVED summary" in p                                    # digest flagged not-quotable
    assert "[0015] independent heater path confirmed" in p          # stored verdict reused
    assert "ASSESSMENT vs benchmark" in p                           # verdict labelled citable
    assert "not yet read" in p                                       # un-read candidate flagged


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


def test_parse_feature_check_aligns_and_defaults():
    feats = [{"name": "battery", "weight": 5},
             {"name": "fuel-gauge IC", "weight": 3},
             {"name": "thermistor", "weight": 1}]
    verdict = ("MATCH SCORE: 6\n"
               "FEATURE 1: YES — [0012] a rechargeable cell\n"
               "FEATURE 2: PARTIAL — claim 3 mentions a gauge vaguely\n"
               # feature 3 deliberately omitted by the model -> defaults to 'no'
               "VERDICT: partial\n")
    out = claude_bridge.parse_feature_check(verdict, feats)
    assert [f["status"] for f in out] == ["yes", "partial", "no"]
    assert out[0]["weight"] == 5 and out[0]["name"] == "battery"
    assert out[0]["note"].startswith("[0012]")


def test_deep_map_prompt_carries_target_features(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, timeout=None: captured.update(p=prompt)
                        or {"answer": "MATCH SCORE: 4\nFEATURE 1: NO — not disclosed"})
    feats = [{"name": "uniquefeatureZ", "weight": 4}]
    claude_bridge.deep_map("BENCH TEXT", {"number": "US1", "title": "t",
                                          "description": "body"}, features=feats)
    assert "FEATURE CHECK" in captured["p"]
    assert "uniquefeatureZ" in captured["p"]


def test_focus_block_loads_full_long_description():
    # a large patent description (well beyond the 40k benchmark cap) must load IN
    # FULL when the candidate is FOCUSED — regression: it used to clip at ~40k.
    desc = "".join(f"[{i:04d}] paragraph body line {i}. " for i in range(1, 3000))
    assert len(desc) > 90_000   # well past the old 40k benchmark cap
    doc = {"number": "WO2022239361", "title": "Power supply",
           "abstract": "abs", "claims": "1. A unit.", "description": desc}
    prompt = claude_bridge.build_prompt("does it disclose Fig. 23?", focus=[doc])
    # the tail paragraph [2999] (Fig-23-region analogue) must be present, unclipped
    assert "[2999]" in prompt
    assert "CLIPPED" not in prompt


def test_focus_multiple_long_docs_not_clipped():
    # REGRESSION (the [0113] incident): selecting several full patents at once must
    # NOT clip any one of them below its full text. Before the 600k budget, six ~82k
    # docs each got 300k//6 = 50k → a long description was cut around [0113] and the
    # model could not quote late paragraphs like [0133]/[0134].
    def make(num):
        desc = "".join(f"[{i:04d}] paragraph body line {i}. " for i in range(1, 2000))
        return {"number": num, "title": "Power supply", "abstract": "abs",
                "claims": "1. A unit.", "description": desc}
    docs = [make(f"US2024022512{n}") for n in range(6)]   # six full-size patents
    assert len(docs[0]["description"]) > 60_000
    prompt = claude_bridge.build_prompt("does it disclose [1999]?", focus=docs)
    assert "[1999]" in prompt                       # every doc's tail paragraph survives
    assert "CLIPPED" not in prompt                  # none silently truncated


def test_focus_flags_drawings_not_read():
    # figures are OPT-IN — but a focused doc whose sheets were never vision-captioned
    # must carry a loud DRAWINGS NOT READ block so the model flags the gap instead of
    # the user discovering it at the last moment.
    # the note's unique wording (the grounding RULE always references the marker
    # name, so assertions must target the per-document note, not the phrase)
    NOTE = "never vision-captioned"
    doc = {"number": "US1", "title": "t", "abstract": "a", "claims": "1. A unit.",
           "description": "[0001] Body ends cleanly."}
    p = claude_bridge.build_prompt("q", focus=[doc])                 # figures_n absent
    assert "(DRAWINGS NOT READ" in p and NOTE in p
    assert "🖼 Read figures" in p                                    # names the fix
    p = claude_bridge.build_prompt("q", focus=[{**doc, "figures_n": 10}])
    assert NOTE not in p                                             # figures were read
    p = claude_bridge.build_prompt("q", focus=[{**doc, "figures_n": 0}])
    assert NOTE not in p                                             # ran: no drawings exist


def test_grounding_rule_mentions_drawings():
    doc = {"number": "US1", "title": "t", "description": "[0001] x."}
    p = claude_bridge.build_prompt("q", focus=[doc])
    assert "DRAWINGS:" in p                # the grounding instruction covers the case


def test_deep_map_appends_figures_caveat(monkeypatch):
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, timeout=None:
                        {"answer": "MATCH SCORE: 6\nKEY FEATURES: none"})
    doc = {"number": "US1", "title": "t", "description": "[0001] body."}
    out = claude_bridge.deep_map("BENCH", doc)                       # figures_n absent
    assert "⚠ DRAWINGS NOT READ" in out["verdict"]                   # code-stamped, not LLM
    assert "🖼 Read figures" in out["verdict"]
    # the stamp must not corrupt the structured verdict fields
    assert claude_bridge.parse_verdict(out["verdict"])["score"] == 6
    out = claude_bridge.deep_map("BENCH", {**doc, "figures_n": 3})
    assert "DRAWINGS NOT READ" not in out["verdict"]                 # figures were read
    out = claude_bridge.deep_map("BENCH", {**doc, "figures_n": 0})
    assert "DRAWINGS NOT READ" not in out["verdict"]                 # no drawings exist


def test_benchmark_block_flags_unread_figures():
    # number-based benchmark without captioned figures → the note; with a merged
    # DRAWINGS block or upload-based text → no note.
    NOTE = "never vision-captioned"      # unique to the per-document note
    bm = {"number": "EP1", "title": "t", "abstract": "a", "claims": "1. A unit.",
          "description": "[0001] Body."}
    p = claude_bridge.build_prompt("q", benchmark=bm)
    assert "(DRAWINGS NOT READ" in p and NOTE in p
    p = claude_bridge.build_prompt("q", benchmark={**bm, "description":
                                                   "[0001] Body.\n[FIG. 1] A pump."})
    assert NOTE not in p                                             # captions merged
    p = claude_bridge.build_prompt(
        "q", benchmark={**bm, "figures": '[{"n": 1, "caption": "A pump."}]'})
    assert NOTE not in p                                             # captioned (JSON)
    p = claude_bridge.build_prompt("q", benchmark={"text": "uploaded transcription",
                                                   "title": "t"})
    assert NOTE not in p                                             # upload-based


def test_focus_block_flags_clip_when_over_budget(monkeypatch):
    # if a focused doc genuinely exceeds the focus budget, the model must be TOLD
    # it is truncated — never silently presented as "full text".
    monkeypatch.setattr(claude_bridge, "MAX_FOCUS_CHARS", 5000)
    monkeypatch.setattr(claude_bridge, "MAX_FULLTEXT_CHARS", 5000)
    desc = "".join(f"[{i:04d}] x. " for i in range(1, 3000))
    doc = {"number": "US9", "title": "t", "description": desc}
    prompt = claude_bridge.build_prompt("q", focus=[doc])
    assert "CLIPPED" in prompt


def test_deep_reduce_uses_long_timeout_and_bounds_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, timeout=None: captured.update(p=prompt, t=timeout)
                        or {"answer": "ranking"})
    # a large roster: each verdict is long, so the per-verdict slice must shrink
    verdicts = [{"number": f"US{i}", "title": "t", "verdict": "X" * 8000}
                for i in range(200)]
    claude_bridge.deep_reduce("rank them", {"text": "BENCH"}, verdicts)
    # the reduce gets the dedicated long timeout, not the 240s chat default
    assert captured["t"] == claude_bridge.REDUCE_TIMEOUT
    assert captured["t"] >= 600
    # the assembled prompt stays bounded despite 200 long verdicts
    assert len(captured["p"]) < claude_bridge.REDUCE_PROMPT_BUDGET * 1.2


def test_benchmark_by_weighted_features(client):
    tab = client.post("/api/tabs", json={"name": "WF"}).json()
    feats = [{"name": "a rechargeable battery", "weight": 5},
             {"name": "a fuel-gauge IC", "weight": 2}]
    r = client.post(f"/api/tabs/{tab['id']}/benchmark/features",
                    json={"features": feats, "title": "battery + gauge"}).json()
    assert r["ok"]
    bm = client.get(f"/api/tabs/{tab['id']}/state").json()["benchmark"]
    assert bm["source"] == "features"
    assert [f["weight"] for f in bm["features"]] == [5, 2]
    assert bm["features"][0]["name"] == "a rechargeable battery"
    # the composed spec (full view) embeds both features + their weights
    full = client.get(f"/api/tabs/{tab['id']}/benchmark/full").json()
    assert "rechargeable battery" in full["text"] and "weight 5" in full["text"]
    # empty body (no features, no spec) is rejected
    assert client.post(f"/api/tabs/{tab['id']}/benchmark/features",
                       json={}).status_code == 400


def test_deep_compare_stores_weighted_feature_scores(client, monkeypatch):
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: {"verdict":
                            "MATCH SCORE: 7\n"
                            "FEATURE 1: YES — [0001] has a battery\n"
                            "FEATURE 2: NO — no gauge\n"
                            f"VERDICT: for {d['number']}"})
    tab = client.post("/api/tabs", json={"name": "WFscore"}).json()
    feats = [{"name": "a rechargeable battery", "weight": 5},
             {"name": "a fuel-gauge IC", "weight": 2}]
    client.post(f"/api/tabs/{tab['id']}/benchmark/features",
                json={"features": feats, "title": "b+g"})
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "EP3667902A1"})
    client.post(f"/api/tabs/{tab['id']}/deep-compare", json={})
    _wait_read(client, tab["id"])
    d = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"][0]
    assert d["score"] == 7
    fs = d["feature_scores"]
    assert [f["status"] for f in fs] == ["yes", "no"]
    assert fs[0]["weight"] == 5 and fs[0]["name"] == "a rechargeable battery"


def test_benchmark_add_feature_is_additive(client):
    tab = client.post("/api/tabs", json={"name": "AddFeat"}).json()
    tid = tab["id"]
    # start with two weighted features
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "a battery", "weight": 5},
                                   {"name": "a gauge", "weight": 2}], "title": "b+g"})
    # append a third — must NOT drop the first two
    r = client.post(f"/api/tabs/{tid}/benchmark/features/add",
                    json={"name": "a thermistor", "weight": 3}).json()
    assert r["ok"]
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert [f["name"] for f in bm["features"]] == ["a battery", "a gauge", "a thermistor"]
    assert [f["weight"] for f in bm["features"]] == [5, 2, 3]
    assert bm["title"] == "b+g"          # title preserved across the add


def test_benchmark_add_feature_preserves_freeform_text(client):
    tab = client.post("/api/tabs", json={"name": "AddFeat2"}).json()
    tid = tab["id"]
    prose = "A) a rechargeable cell with a unique guardian circuit described at length."
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"spec": prose})
    # adding a weighted feature to a free-form benchmark keeps the prose as context
    client.post(f"/api/tabs/{tid}/benchmark/features/add",
                json={"name": "a thermistor", "weight": 4})
    full = client.get(f"/api/tabs/{tid}/benchmark/full").json()
    assert "guardian circuit" in full["text"]      # original prose not discarded
    assert "a thermistor" in full["text"]          # new feature added
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert [f["name"] for f in bm["features"]] == ["a thermistor"]


def test_benchmark_add_feature_accepts_long_pasted_text(client):
    # a feature pasted from a patent claim can be a long paragraph — the paste-
    # friendly textarea allows up to 4000 chars, so the server must too (was 500).
    tab = client.post("/api/tabs", json={"name": "LongFeat"}).json()
    tid = tab["id"]
    long_name = ("a rechargeable battery cell " + ("with a guardian protection sub-circuit " * 40)).strip()
    assert len(long_name) > 500
    r = client.post(f"/api/tabs/{tid}/benchmark/features/add",
                    json={"name": long_name, "weight": 3})
    assert r.status_code == 200, r.text
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert bm["features"][0]["name"] == long_name


def test_benchmark_add_feature_rejects_document_benchmark(client):
    tab = client.post("/api/tabs", json={"name": "AddFeat3"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    assert client.post(f"/api/tabs/{tid}/benchmark/features/add",
                       json={"name": "x", "weight": 1}).status_code == 400


def test_benchmark_add_feature_creates_when_none(client):
    tab = client.post("/api/tabs", json={"name": "AddFeat4"}).json()
    tid = tab["id"]
    r = client.post(f"/api/tabs/{tid}/benchmark/features/add",
                    json={"name": "a battery", "weight": 5}).json()
    assert r["ok"]
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert bm["source"] == "features" and bm["features"][0]["name"] == "a battery"


def test_parse_combi_motivation_blocks():
    text = ("=== 1 ===\nCOMBINABLE: YES\nWHY: same field, A adds the gauge B lacks.\n"
            "=== 2 ===\nCOMBINABLE: NO\nWHY: unrelated domains, no motivation.")
    out = claude_bridge.parse_combi_motivation(text)
    assert out["1"]["combinable"] is True and "gauge" in out["1"]["reason"]
    assert out["2"]["combinable"] is False


def _feature_doc(db, tid, number, scores):
    """Insert a fetched candidate with the given per-feature verdicts."""
    import json as _j
    doc_id = db.add_documents(tid, [number])["inserted"][0]
    db.update_document(doc_id, status="fetched", digest=f"digest of {number}",
                       feature_scores=_j.dumps(scores))
    return doc_id


def test_combi_motivation_endpoint_persists_and_returns(client, monkeypatch):
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "Combi"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "a battery", "weight": 3},
                                   {"name": "a gauge", "weight": 2}], "title": "b+g"})
    # A discloses battery only; B discloses gauge only → together they cover all mandatory
    a_id = _feature_doc(db, tid, "US1111111A",
                        [{"name": "a battery", "weight": 3, "status": "yes", "note": "cell"},
                         {"name": "a gauge", "weight": 2, "status": "no", "note": ""}])
    b_id = _feature_doc(db, tid, "US2222222A",
                        [{"name": "a battery", "weight": 3, "status": "no", "note": ""},
                         {"name": "a gauge", "weight": 2, "status": "yes", "note": "fuel gauge"}])
    monkeypatch.setattr(claude_bridge, "combi_motivation",
                        lambda bm, pairs, model=None: {"results": {"1": {"combinable": True,
                                                       "reason": "same field; complementary"}},
                                                       "model": "claude-sonnet-4-6"})
    r = client.post(f"/api/tabs/{tid}/combi/motivation",
                    json={"pairs": [{"a_id": a_id, "b_id": b_id,
                                     "a_features": ["a battery"], "b_features": ["a gauge"]}]}).json()
    lo, hi = sorted((a_id, b_id))
    assert r["ok"] and r["results"][f"{lo}-{hi}"]["combinable"] is True
    # persisted → surfaced in state for the next page load
    cm = client.get(f"/api/tabs/{tid}/state").json()["combi_motivations"]
    assert cm[f"{lo}-{hi}"]["combinable"] is True and "complementary" in cm[f"{lo}-{hi}"]["reason"]


def test_combi_motivation_requires_feature_benchmark(client):
    tab = client.post("/api/tabs", json={"name": "CombiDoc"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    r = client.post(f"/api/tabs/{tid}/combi/motivation",
                    json={"pairs": [{"a_id": 1, "b_id": 2}]})
    assert r.status_code == 400


def test_parse_additional_maps_status_by_index():
    """The bulk additional-read output ('=== NUM ===' blocks, 'N: STATUS — evidence' lines)
    parses back onto each candidate's A-feature verdicts, indexed to the feature list."""
    from patentbench import claude_bridge as cb
    a_features = [{"name": "detachable pack", "weight": 3, "sl": 7},
                  {"name": "fuel-gauge IC", "weight": 5, "sl": 4}]
    text = ("=== CN117241689 ===\n"
            "1: PRESENT — pack slides out [0030]\n"
            "2: STRETCH — gauge implied by IC 12\n"
            "=== EP4338615 ===\n"
            "1: ABSENT — integrated, not removable\n"
            "2: PRESENT — remaining-capacity meter IC 12\n")
    out = cb.parse_additional(text, a_features)
    assert set(out) == {"CN117241689", "EP4338615"}
    assert out["CN117241689"][0] == {"name": "detachable pack", "weight": 3, "sl": 7,
                                     "status": "present", "evidence": "pack slides out [0030]"}
    assert out["CN117241689"][1]["status"] == "stretch"
    assert out["EP4338615"][0]["status"] == "absent" and out["EP4338615"][1]["status"] == "present"


def test_features_persist_kind_and_sl(client):
    """Benchmark features round-trip their M/A kind + stretch level; only M features compose
    the base benchmark text (so an A feature's absence can't move the established score)."""
    tab = client.post("/api/tabs", json={"name": "MA"}).json()
    tid = tab["id"]
    r = client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "thermistor divider", "weight": 5, "kind": "M", "sl": 5},
        {"name": "detachable battery pack", "weight": 3, "kind": "A", "sl": 8},
    ]}).json()
    feats = r["benchmark"]["features"]
    assert feats[0]["kind"] == "M" and feats[1]["kind"] == "A" and feats[1]["sl"] == 8
    # base benchmark text contains the M feature, NOT the A feature
    full = client.get(f"/api/tabs/{tid}/benchmark?full=1")
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert any(f["name"] == "detachable battery pack" and f["kind"] == "A" for f in bm["features"])


def test_additional_read_endpoint(client, monkeypatch):
    """➕ additional read: one bulk call over digests, stores per-doc additional_scores; needs
    A-features and a digest, never re-reads full text."""
    from patentbench import claude_bridge as cb
    tab = client.post("/api/tabs", json={"name": "Add"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "core divider", "weight": 5, "kind": "M", "sl": 5},
        {"name": "detachable pack", "weight": 4, "kind": "A", "sl": 7},
    ]})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4338615"], "source": "image"})
    import patentbench.db as _db
    doc = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    _db.update_document(doc["id"], digest="A remaining-capacity meter IC with a removable battery pack.",
                        score=8, scored_at=1, score_model="opus")
    captured = {}
    def fake_add(a_features, docs, model=None):
        captured["a"] = [f["name"] for f in a_features]
        captured["n"] = [d["number"] for d in docs]
        captured["model"] = model
        return {"results": {"EP4338615": [{"name": "detachable pack", "weight": 4, "sl": 7,
                                           "status": "present", "evidence": "removable pack"}]},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "additional_read", fake_add)
    r = client.post(f"/api/tabs/{tid}/additional-read", json={}).json()
    assert r["ok"] and r["assessed"] == 1 and r["a_features"] == 1
    assert captured["a"] == ["detachable pack"]          # ONLY the A-feature checked
    assert captured["n"] == ["EP4338615"]
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    asc = docs[0]["additional_scores"]
    assert asc and asc[0]["status"] == "present" and asc[0]["name"] == "detachable pack"


def test_additional_read_needs_a_features(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "NoA"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "only mandatory", "weight": 5, "kind": "M", "sl": 5}]})
    assert client.post(f"/api/tabs/{tid}/additional-read", json={}).status_code == 400


def test_parse_digest_rescore():
    from patentbench import claude_bridge as cb
    text = ("=== CN117321873 ===\nSCORE: 8\nWHY: gauge divider + independent paths\n"
            "=== EP4212037 ===\nSCORE: 6.5\nWHY: divider but no battery gauge\n")
    out = cb.parse_digest_rescore(text)
    assert out["CN117321873"]["score"] == 8.0 and "independent" in out["CN117321873"]["note"]
    assert out["EP4212037"]["score"] == 6.5


def test_digest_rescore_endpoint_no_reread(client, monkeypatch):
    """♻️ re-check updates scores from digests in one bulk call, tags them ·digest, and never
    touches full text."""
    from patentbench import claude_bridge as cb
    tab = client.post("/api/tabs", json={"name": "Re"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "thermistor divider", "weight": 5, "kind": "M", "sl": 5}]})
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4338615"], "source": "image"})
    import patentbench.db as _db
    doc = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    _db.update_document(doc["id"], digest="thermistor + resistor voltage divider read by gauge",
                        score=8, scored_at=1, score_model="claude-opus-4-8")
    seen = {}
    def fake_rescore(bm, docs, model=None):
        seen["n"] = [d["number"] for d in docs]; seen["model"] = model
        return {"results": {"EP4338615": {"score": 7.0, "note": "divider present"}},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "digest_rescore", fake_rescore)
    r = client.post(f"/api/tabs/{tid}/digest-rescore", json={}).json()
    assert r["ok"] and r["updated"] == 1 and seen["n"] == ["EP4338615"]
    d = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    assert d["score"] == 7.0 and d["score_model"].endswith("·digest")   # tagged digest-based


def test_assessed_freshness_gate():
    """A read OLDER than the benchmark's last change is stale → counts as NOT assessed, so
    Continue re-reads it regardless of model strength; a newer read is kept."""
    from patentbench.web import api
    d = {"score": 8, "scored_at": 100, "score_model": "claude-opus-4-8", "verdict": "x"}
    assert api._assessed_at_least(d, "claude-sonnet-4-6", since=0) is True     # no benchmark change
    assert api._assessed_at_least(d, "claude-sonnet-4-6", since=50) is True    # read after the change
    assert api._assessed_at_least(d, "claude-sonnet-4-6", since=200) is False  # read BEFORE the change → stale


def test_continue_rereads_stale_after_benchmark_change(client, monkeypatch):
    """Adding a feature (benchmark change) makes a prior opus read STALE → Continue re-reads
    it even though opus is 'stronger' than the current model. Staleness beats model strength."""
    tab = client.post("/api/tabs", json={"name": "Stale"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "thermistor divider", "weight": 5, "kind": "M", "sl": 5}]})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "EP3667902A1 CN114547092"})
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    # both opus-read in the PAST (epoch) — before the benchmark we will now change
    db.update_document(docs["EP3667902A1"]["id"], verdict="v", score=8, scored_at=1, score_model="claude-opus-4-8")
    db.update_document(docs["CN114547092"]["id"], verdict="v", score=7, scored_at=1, score_model="claude-opus-4-8")
    # add a feature → benchmark.updated_at bumps to now, so both reads are stale
    client.post(f"/api/tabs/{tid}/benchmark/features/add",
                json={"name": "battery gauge", "weight": 4, "kind": "M", "sl": 5})
    read = []
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: read.append(d["number"]) or {"verdict": "x"})
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "r"})
    client.post(f"/api/tabs/{tid}/deep-compare", json={"skip_scored": True, "reading_model": "claude-sonnet-4-6"})
    _wait_read(client, tid)
    assert set(read) == {"EP3667902A1", "CN114547092"}   # BOTH stale opus reads re-read on sonnet


def test_cross_tab_document_reuse_flow(client):
    a = client.post("/api/tabs", json={"name": "A"}).json()
    b = client.post("/api/tabs", json={"name": "B"}).json()
    # add by number in A → bg fetch+digest run synchronously under TestClient
    client.post(f"/api/tabs/{a['id']}/documents", json={"numbers": ["US9999999B1"]})
    docs = client.get(f"/api/tabs/{a['id']}/documents").json()["documents"]
    assert docs[0]["status"] == "fetched"
    # same number in B is held back as reusable, NOT auto-fetched
    res = client.post(f"/api/tabs/{b['id']}/documents", json={"numbers": ["US9999999B1"]}).json()
    assert len(res["reusable"]) == 1
    ru = res["reusable"][0]
    assert ru["tab_name"] == "A" and ru["has_digest"]
    bdoc = client.get(f"/api/tabs/{b['id']}/documents").json()["documents"][0]
    assert bdoc["status"] == "pending"
    # accept reuse → body+digest copied, no re-fetch needed
    rr = client.post(f"/api/tabs/{b['id']}/documents/{ru['doc_id']}/reuse").json()
    assert rr["ok"] and rr["reused_from"] == "A"
    bdoc = client.get(f"/api/tabs/{b['id']}/documents").json()["documents"][0]
    assert bdoc["status"] == "fetched" and bdoc["digest_len"]


def test_feature_xref_endpoint(client):
    import json
    a = client.post("/api/tabs", json={"name": "A"}).json()
    b = client.post("/api/tabs", json={"name": "B"}).json()
    client.post(f"/api/tabs/{a['id']}/documents", json={"numbers": ["US7654321B1"]})
    did = client.get(f"/api/tabs/{a['id']}/documents").json()["documents"][0]["id"]
    db.update_document(did, feature_scores=json.dumps(
        [{"name": "Gizmo", "status": "yes", "note": "see claim 3"}]))
    # B asks who else discloses Gizmo → finds A's doc
    res = client.get(f"/api/tabs/{b['id']}/feature-xref", params={"name": "Gizmo"}).json()
    assert len(res["documents"]) == 1
    assert res["documents"][0]["tab_name"] == "A"
    assert res["documents"][0]["number"] == "US7654321B1"


def test_document_figures_backfill(client, monkeypatch):
    from patentbench import figures, fetcher as _f
    monkeypatch.setattr(figures, "download",
                        lambda urls, dest: [{"n": i + 1, "url": u, "path": f"/x/{i}.png"}
                                            for i, u in enumerate(urls)])
    monkeypatch.setattr(figures, "caption_all",
                        lambda figs, model=None, workers=None, context="":
                        [f.update(caption=f"[FIG. {f['n']}] stub") or f for f in figs])
    monkeypatch.setattr(_f, "figure_urls", lambda n: ["http://x/imgf0001.png",
                                                      "http://x/imgf0002.png"])
    t = client.post("/api/tabs", json={"name": "F"}).json()
    client.post(f"/api/tabs/{t['id']}/documents", json={"numbers": ["US7654321B1"]})
    did = client.get(f"/api/tabs/{t['id']}/documents").json()["documents"][0]["id"]
    assert client.post(f"/api/tabs/{t['id']}/documents/{did}/figures").json()["ok"]
    full = client.get(f"/api/tabs/{t['id']}/documents/{did}").json()
    assert "[FIG. 1] stub" in full["description"]
    assert figures.DRAWINGS_HEADER in full["description"]
    doc = client.get(f"/api/tabs/{t['id']}/documents").json()["documents"][0]
    assert doc["figures_n"] == 2


# ---------- 🏆 best-match cross-tab scan ----------

def test_parse_cross_tab_scan():
    feats = [{"name": "battery", "weight": 5}, {"name": "inverter", "weight": 3}]
    text = ("=== US1111111 ===\n"
            "FEATURE 1: YES — a battery pack 12 is disclosed\n"
            "FEATURE 2: NO — absent\n"
            "=== US2222222 ===\nMATCHES: NONE\n"
            "=== US3333333 ===\nCOVERS: grid-tie inverter with droop control")
    out = claude_bridge.parse_cross_tab_scan(text, feats)
    hits = out["US1111111"]["features"]
    assert [h["name"] for h in hits] == ["battery"]        # NO lines are not "coverage"
    assert hits[0]["status"] == "yes" and "battery pack" in hits[0]["note"]
    assert out["US2222222"] == {"features": [], "covers": None}   # negatives kept (for caching)
    assert out["US3333333"]["covers"].startswith("grid-tie")


def test_import_document_copy_copies_content_not_scores(client):
    # content (text/digest/figures) travels; score/verdict do NOT — they were judged
    # against the OTHER tab's benchmark and would poison this tab's ranking.
    a = client.post("/api/tabs", json={"name": "A"}).json()["id"]
    b = client.post("/api/tabs", json={"name": "B"}).json()["id"]
    client.post(f"/api/tabs/{a}/documents", json={"numbers": ["US7777777"], "source": "manual"})
    import patentbench.db as _db
    src = client.get(f"/api/tabs/{a}/documents").json()["documents"][0]
    _db.update_document(src["id"], verdict="MATCH SCORE: 9 vs A's benchmark", score=9,
                        figures='[{"n": 1, "caption": "[FIG. 1] pump"}]', figures_n=1)
    new_id = _db.import_document_copy(
        b, src["id"],
        feature_scores=[{"name": "f", "weight": 1, "status": "yes", "note": "n"}],
        score_note="↪ pulled from tab «A»")
    d = _db.get_document(new_id)
    assert d["tab_id"] == b and d["source"] == "cross-tab" and d["origin_tab_id"] == a
    assert d["status"] == "fetched" and d["description"] == "desc"   # content copied
    assert d["digest"] == "digest of US7777777" and d["figures_n"] == 1
    assert d["score"] is None and d["verdict"] is None               # judgements do not travel
    assert _db.import_document_copy(b, src["id"]) is None            # dup number → skipped


def test_cross_tab_scan_endpoint_imports_and_caches(client, monkeypatch):
    from patentbench import claude_bridge as cb
    a = client.post("/api/tabs", json={"name": "Origin"}).json()["id"]
    b = client.post("/api/tabs", json={"name": "Target"}).json()["id"]
    client.post(f"/api/tabs/{a}/documents",
                json={"numbers": ["US5555555", "US6666666"], "source": "manual"})
    client.post(f"/api/tabs/{b}/benchmark/features", json={"title": "t", "features": [
        {"name": "thermistor divider", "weight": 5, "kind": "M", "sl": 5}]})

    def fake_scan(bm, feats, docs, model=None):
        res = {}
        for d in docs:
            if d["number"] == "US5555555":      # covers ONE feature → must be pulled in
                res[d["number"]] = {"features": [{"name": "thermistor divider", "weight": 5,
                                                  "status": "partial", "note": "divider 12"}],
                                    "covers": None}
            else:                               # covers nothing → negative-cached
                res[d["number"]] = {"features": [], "covers": None}
        return {"results": res, "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "cross_tab_scan", fake_scan)

    r = client.post(f"/api/tabs/{b}/cross-tab-scan", json={}).json()
    assert r["started"] is True and r["total"] == 2
    s = client.get(f"/api/tabs/{b}/cross-tab-scan/status").json()
    assert s["running"] is False and len(s["imported"]) == 1
    assert s["imported"][0]["number"] == "US5555555" and s["imported"][0]["from"] == "Origin"

    docs_b = client.get(f"/api/tabs/{b}/documents").json()["documents"]
    imp = [d for d in docs_b if d["source"] == "cross-tab"]
    assert len(imp) == 1 and imp[0]["number"] == "US5555555"
    assert imp[0]["origin_tab_id"] == a                              # ↪ chip provenance
    fs = imp[0]["feature_scores"]                                    # covered features indicated
    assert fs and fs[0]["name"] == "thermistor divider" and fs[0]["status"] == "partial"
    assert "covers thermistor divider" in (imp[0]["score_note"] or "")
    assert imp[0]["score"] is None                                   # deep read still to come

    # second Best-match click: the match is now IN the tab, the non-match is cached →
    # nothing to scan, no repeat token spend.
    r2 = client.post(f"/api/tabs/{b}/cross-tab-scan", json={}).json()
    assert r2["started"] is False and r2["total"] == 0
    assert r2["cached_skipped"] >= 1


# ---------- cross-tab chat discussions ----------

def test_cross_tab_discussions_db(client):
    a = client.post("/api/tabs", json={"name": "Valves"}).json()["id"]
    b = client.post("/api/tabs", json={"name": "Pumps"}).json()["id"]
    # a full exchange in tab A about EP4338618A1, plus an unrelated one after it
    db.append_message(a, "q", "what does EP4338618A1 teach about the overlapping section?")
    db.append_message(a, "a", "NLM: EP4338618A1 discloses an overlap of blades")
    db.append_message(a, "c", "Claude: the overlap is in claim 3")
    db.append_message(a, "q", "unrelated question about something else")
    db.append_message(a, "c", "unrelated answer")

    # kind-code- and spacing-insensitive: ask with B1 and spaces, stored as A1
    out = db.cross_tab_discussions("EP 4338618 B1", exclude_tab_id=b)
    assert len(out) == 1 and out[0]["tab_name"] == "Valves"
    ex = out[0]["exchanges"]
    assert len(ex) == 1                                   # unrelated exchange NOT included
    assert [m["role"] for m in ex[0]] == ["q", "a", "c"]  # the WHOLE exchange, q→replies
    assert "claim 3" in ex[0][2]["text"]

    # the tab that holds the discussion is excluded from its own lookup
    assert db.cross_tab_discussions("EP4338618A1", exclude_tab_id=a) == []


def test_chat_pulls_cross_tab_discussions(client, monkeypatch):
    a = client.post("/api/tabs", json={"name": "Valves"}).json()["id"]
    b = client.post("/api/tabs", json={"name": "Pumps"}).json()["id"]
    # a real chat happened in tab A (stubbed claude): both q and c are stored
    client.post(f"/api/tabs/{a}/chat", json={"question": "is EP4338618A1 novel over D1?"})

    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *args, **k: (seen.update(k), {"answer": "ok", "model": "m"})[1])
    r = client.post(f"/api/tabs/{b}/chat",
                    json={"question": "write here everything we had in other tabs "
                                      "concerning EP4338618A1"}).json()
    disc = seen.get("discussions") or []
    assert disc and disc[0]["tab_name"] == "Valves"
    texts = " ".join(m["text"] for ex in disc[0]["exchanges"] for m in ex)
    assert "novel over D1" in texts and "claude says hi" in texts
    # the answer carries a 💬 participants chip naming the source tab
    chips = [p for m in r["messages"] if m.get("participants")
             for p in m["participants"] if p["kind"] == "xtalk"]
    assert chips and "Valves" in chips[0]["title"]

    # sticky: the follow-up does NOT re-type the number, discussions stay loaded
    seen.clear()
    client.post(f"/api/tabs/{b}/chat", json={"question": "and what was the conclusion?"})
    disc2 = seen.get("discussions") or []
    assert disc2 and disc2[0]["tab_name"] == "Valves"


def test_build_prompt_renders_discussions_block():
    disc = [{"tab_id": 1, "tab_name": "Valves", "number": "EP4338618A1",
             "exchanges": [[{"role": "q", "text": "about EP4338618A1?", "ts": 1750000000},
                            {"role": "c", "text": "the overlap is in claim 3", "ts": 1750000001}]]}]
    p = claude_bridge.build_prompt("what did we discuss?", discussions=disc)
    assert "PRIOR DISCUSSIONS IN OTHER TABS" in p
    assert "tab «Valves» — EP4338618A1" in p
    assert "User: about EP4338618A1?" in p and "Claude: the overlap is in claim 3" in p
    # conversation records are not citable primary text
    assert "NOT primary patent text" in p


# ---------- DRAWINGS captions must survive budget clips ----------

def _doc_with_tail_drawings(n=20000):
    return {"number": "EP4338618", "title": "Seal", "abstract": "A seal.",
            "claims": "1. A seal with overlap.",
            "description": ("[0001] prose " * (n // 13))
            + "\n\n" + claude_bridge.DRAWINGS_HEADER
            + "\n[FIG. 3] Overlapping section 42 between blades 40, 41.",
            "figures_n": 16}


def test_document_block_drawings_survive_focus_clip():
    doc = _doc_with_tail_drawings()
    out = claude_bridge._document_block(doc, budget=4000, clipped=False)
    # the description overflows the budget and is clipped…
    assert "…[CLIPPED" in out
    # …but the vision-read captions still made it in, BEFORE the description
    assert "[FIG. 3] Overlapping section 42" in out
    assert out.index("DRAWINGS") < out.index("Description (PRIMARY")
    assert "DRAWINGS NOT READ" not in out                 # figures ARE read


def test_benchmark_block_drawings_survive_clip(monkeypatch):
    monkeypatch.setattr(claude_bridge, "MAX_BENCHMARK_CHARS", 4000)
    bm = _doc_with_tail_drawings()
    block = claude_bridge._benchmark_block(bm)
    assert "[FIG. 3] Overlapping section 42" in block
    assert "DRAWINGS NOT READ" not in block


def test_chat_focus_prefers_figures_read_copy(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: (seen.update(k), {"answer": "ok", "model": "m"})[1])
    tab = client.post("/api/tabs", json={"name": "Dup"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "EP4338618 EP4338618A1"})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    kindless = next(d for d in docs if d["number"] == "EP4338618")
    db.update_document(kindless["id"], figures_n=16,
                       description="desc\n" + claude_bridge.DRAWINGS_HEADER + "\n[FIG. 1] x")
    # the question names the A1 copy — but the figures-read kindless copy must win;
    # duplicates must NOT split the focus budget between them
    client.post(f"/api/tabs/{tab['id']}/chat",
                json={"question": "what do EP4338618A1 figures show?"})
    foc = seen.get("focus") or []
    assert [d["id"] for d in foc] == [kindless["id"]]
    # the dropped duplicate is still present in the tab roster, not lost
    assert any(d["number"] == "EP4338618A1" for d in seen.get("documents") or [])


# ---------- ⚖️ problem-solution approach ----------

@pytest.fixture
def psa_client(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "PSA_DIR", str(tmp_path / "psa"))
    return client


def _upload_method(client, text="STEP 1: determine the closest prior art.\n"
                                "STEP 2: formulate the objective technical problem.\n"
                                "STEP 3: assess obviousness."):
    return client.post("/api/psa/method",
                       files={"file": ("epo-psa.txt", text.encode(), "text/plain")})


def test_psa_method_upload_and_status(psa_client):
    assert psa_client.get("/api/psa/method").json() == {"ok": False}
    r = _upload_method(psa_client)
    assert r.status_code == 200 and r.json()["ok"] is True
    s = psa_client.get("/api/psa/method").json()
    assert s["ok"] is True and s["name"] == "epo-psa.txt" and s["chars"] > 50


def test_psa_method_rejects_junk(psa_client):
    r = psa_client.post("/api/psa/method",
                        files={"file": ("m.docx", b"x" * 100, "application/x")})
    assert r.status_code == 400
    r = _upload_method(psa_client, text="too short")
    assert r.status_code == 400


def test_psa_requires_method_benchmark_and_two_docs(psa_client):
    tab = psa_client.post("/api/tabs", json={"name": "PSA"}).json()
    psa_client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "CN113964850 US11909216B2"})
    ids = [d["id"] for d in
           psa_client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    # no method yet
    r = psa_client.post(f"/api/tabs/{tab['id']}/psa", json={"doc_ids": ids})
    assert r.status_code == 400 and "methodology" in r.json()["detail"]
    _upload_method(psa_client)
    # no benchmark yet
    r = psa_client.post(f"/api/tabs/{tab['id']}/psa", json={"doc_ids": ids})
    assert r.status_code == 400 and "benchmark" in r.json()["detail"]
    # exactly two docs enforced by the schema
    psa_client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP1111111A1"})
    r = psa_client.post(f"/api/tabs/{tab['id']}/psa", json={"doc_ids": ids[:1]})
    assert r.status_code == 422


def test_psa_runs_method_over_benchmark_and_two_docs(psa_client, monkeypatch):
    seen = {}
    def fake_psa(method_text, benchmark, docs, model=None, format_text=None,
                 discussions=None, **kw):
        seen.update(method=method_text, benchmark=benchmark, docs=docs, model=model)
        return {"answer": "STEP 1: D1 is CN113964850 …", "model": model}
    monkeypatch.setattr(claude_bridge, "psa", fake_psa)
    _upload_method(psa_client)
    tab = psa_client.post("/api/tabs", json={"name": "PSA"}).json()
    psa_client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP1111111A1"})
    psa_client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "CN113964850 US11909216B2"})
    ids = [d["id"] for d in
           psa_client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    r = psa_client.post(f"/api/tabs/{tab['id']}/psa",
                        json={"doc_ids": ids, "model": "claude-fable-5"}).json()
    assert "STEP 1" in seen["method"]                      # methodology text passed
    assert [d["id"] for d in seen["docs"]] == ids          # both docs, full rows
    assert seen["benchmark"]["number"] == "EP1111111A1"
    # answer lands in the tab's chat with ⚖️ participants
    msgs = psa_client.get(f"/api/tabs/{tab['id']}/state").json()["messages"]
    q = [m for m in msgs if m["role"] == "q" and "Problem-solution" in m["text"]]
    assert q and "CN113964850 + US11909216B2" in q[0]["text"]
    c = [m for m in msgs if m["role"] == "c"][-1]
    kinds = {p["kind"] for p in c["participants"]}
    assert "psa" in kinds and "model" in kinds
    assert any(p["title"].startswith("D1 ") for p in c["participants"])
    assert r["messages"][-1]["text"].startswith("STEP 1")


def test_psa_prompt_is_strict_and_carries_all_parts(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    claude_bridge.psa("STEP 1: closest prior art.",
                      {"number": "EP1", "title": "Bench", "claims": "1. A thing."},
                      [{"number": "D1DOC", "title": "a", "claims": "1. x"},
                       {"number": "D2DOC", "title": "b", "claims": "1. y"}])
    p = captured["p"]
    assert "USER-SUPPLIED METHODOLOGY (BINDING" in p
    assert "STEP 1: closest prior art." in p
    assert "BENCHMARK DOCUMENT — the claimed invention" in p
    assert "D1 — selected prior-art document 1 of 2" in p
    assert "D2 — selected prior-art document 2 of 2" in p
    assert "do not skip, merge, reorder" in p
    assert "never silently drop it" in p


def test_psa_scanned_pdf_falls_back_to_vision_ocr(psa_client, monkeypatch):
    import os as _os
    monkeypatch.setattr(api.extract, "text_from_pdf",
                        lambda p: {"error": "no extractable text in the PDF"})
    def fake_transcribe(kind, name):
        text = "STEP 1: closest prior art. " * 10
        with open(_os.path.join(api.PSA_DIR, "method.txt"), "w") as fh:
            fh.write(text)
        with open(_os.path.join(api.PSA_DIR, "method.json"), "w") as fh:
            import json as _json, time as _time
            _json.dump({"name": name, "chars": len(text),
                        "uploaded_at": int(_time.time())}, fh)
        api._write_psa_pending("method", None)
    monkeypatch.setattr(api, "_transcribe_psa_doc", fake_transcribe)
    r = psa_client.post("/api/psa/method",
                        files={"file": ("scan.pdf", b"%PDF-1.4 fake scan",
                                        "application/pdf")})
    assert r.status_code == 200 and r.json().get("pending") is True
    # TestClient runs the background task inline → OCR has "finished" by now
    s = psa_client.get("/api/psa/method").json()
    assert s["ok"] is True and s["name"] == "scan.pdf"


def test_psa_method_status_reports_ocr_progress_and_error(psa_client):
    api._write_psa_pending("method", {"name": "scan.pdf", "total": 12, "done": 3})
    s = psa_client.get("/api/psa/method").json()
    assert s == {"ok": False, "pending": True, "name": "scan.pdf", "progress": "3/12"}
    api._write_psa_pending("method", {"name": "scan.pdf", "error": "pdftoppm failed"})
    s = psa_client.get("/api/psa/method").json()
    assert s["ok"] is False and "pdftoppm" in s["error"] and "pending" not in s
    api._write_psa_pending("method", None)
    assert psa_client.get("/api/psa/method").json() == {"ok": False}


def test_psa_format_doc_uploaded_and_combined(psa_client, monkeypatch):
    seen = {}
    def fake_psa(method_text, benchmark, docs, model=None, format_text=None,
                 discussions=None, **kw):
        seen.update(method=method_text, fmt=format_text)
        return {"answer": "formatted per spec", "model": model}
    monkeypatch.setattr(claude_bridge, "psa", fake_psa)
    _upload_method(psa_client)
    r = psa_client.post("/api/psa/format",
                        files={"file": ("report-format.txt",
                                        b"SECTION A: header table. SECTION B: reasoned "
                                        b"could-would analysis. SECTION C: conclusion.",
                                        "text/plain")})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert psa_client.get("/api/psa/format").json()["name"] == "report-format.txt"
    tab = psa_client.post("/api/tabs", json={"name": "PSAF"}).json()
    psa_client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP1111111A1"})
    psa_client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "CN113964850 US11909216B2"})
    ids = [d["id"] for d in
           psa_client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    psa_client.post(f"/api/tabs/{tab['id']}/psa", json={"doc_ids": ids})
    assert "SECTION A" in seen["fmt"]                      # format doc reached the run
    msgs = psa_client.get(f"/api/tabs/{tab['id']}/state").json()["messages"]
    c = [m for m in msgs if m["role"] == "c"][-1]
    assert any(p["title"] == "format: report-format.txt" for p in c["participants"])
    q = [m for m in msgs if m["role"] == "q"][-1]
    assert "format: report-format.txt" in q["text"]


def test_psa_unknown_kind_404(psa_client):
    assert psa_client.get("/api/psa/bogus").status_code == 404


def test_psa_prompt_format_block_binding(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    claude_bridge.psa("STEP 1: x.", {"number": "EP1", "claims": "1. A thing."},
                      [{"number": "D1DOC", "claims": "1. x"},
                       {"number": "D2DOC", "claims": "1. y"}],
                      format_text="SECTION A: table first.")
    p = captured["p"]
    assert "USER-SUPPLIED OUTPUT FORMAT (BINDING" in p
    assert "SECTION A: table first." in p
    assert "this format document wins" in p
    # without a format doc the block is absent
    claude_bridge.psa("STEP 1: x.", {"number": "EP1", "claims": "1. A thing."},
                      [{"number": "D1DOC", "claims": "1. x"},
                       {"number": "D2DOC", "claims": "1. y"}])
    assert "OUTPUT FORMAT" not in captured["p"]


def test_psa_reuses_prior_discussions_from_all_chats(psa_client, monkeypatch):
    seen = {}
    def fake_psa(method_text, benchmark, docs, model=None, format_text=None,
                 discussions=None, **kw):
        seen["disc"] = discussions
        return {"answer": "built on prior findings", "model": model}
    monkeypatch.setattr(claude_bridge, "psa", fake_psa)
    _upload_method(psa_client)
    # prior discussion about D1 in ANOTHER tab…
    other = psa_client.post("/api/tabs", json={"name": "Earlier"}).json()["id"]
    db.append_message(other, "q", "does CN113964850 disclose the seal overlap?")
    db.append_message(other, "c", "Yes — CN113964850 claim 3 covers it.")
    # …and about D2 in the PSA tab ITSELF
    tab = psa_client.post("/api/tabs", json={"name": "PSA"}).json()
    db.append_message(tab["id"], "q", "US11909216B2 argument recap?")
    db.append_message(tab["id"], "c", "US11909216B2 lacks the thermistor branch.")
    psa_client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP1111111A1"})
    psa_client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "CN113964850 US11909216B2"})
    ids = [d["id"] for d in
           psa_client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    psa_client.post(f"/api/tabs/{tab['id']}/psa",
                    json={"doc_ids": ids, "use_discussions": True})
    disc = seen["disc"]
    tabs_seen = {g["tab_name"] for g in disc}
    assert "Earlier" in tabs_seen and "PSA" in tabs_seen    # all chats, incl. own tab
    texts = " ".join(m["text"] for g in disc for ex in g["exchanges"] for m in ex)
    assert "claim 3 covers it" in texts and "lacks the thermistor branch" in texts
    # chip + q-line reflect the reuse
    msgs = psa_client.get(f"/api/tabs/{tab['id']}/state").json()["messages"]
    q = [m for m in msgs if m["role"] == "q" and "Problem-solution" in m["text"]][-1]
    assert "💬" in q["text"]
    c = [m for m in msgs if m["role"] == "c"][-1]
    assert any(p["kind"] == "xtalk" for p in c["participants"])
    # opt-out: checkbox off → no discussions gathered
    seen.clear()
    psa_client.post(f"/api/tabs/{tab['id']}/psa",
                    json={"doc_ids": ids, "use_discussions": False})
    assert seen["disc"] is None


def test_psa_prompt_discussions_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    disc = [{"tab_id": 1, "tab_name": "Earlier", "number": "CN113964850",
             "exchanges": [[{"role": "q", "text": "overlap?", "ts": 1750000000},
                            {"role": "c", "text": "claim 3 covers it", "ts": 1750000001}]]}]
    claude_bridge.psa("STEP 1: x.", {"number": "EP1", "claims": "1. A thing."},
                      [{"number": "D1DOC", "claims": "1. x"},
                       {"number": "D2DOC", "claims": "1. y"}], discussions=disc)
    p = captured["p"]
    assert "PRIOR DISCUSSIONS ABOUT D1/D2" in p
    assert "claim 3 covers it" in p
    assert "REUSE this prior analysis" in p
    assert "do not rediscover from scratch" in p


def test_psa_stretch_mode(psa_client, monkeypatch):
    seen = {}
    def fake_psa(method_text, benchmark, docs, model=None, format_text=None,
                 discussions=None, stretch=False):
        seen["stretch"] = stretch
        return {"answer": "advocacy draft", "model": model}
    monkeypatch.setattr(claude_bridge, "psa", fake_psa)
    _upload_method(psa_client)
    tab = psa_client.post("/api/tabs", json={"name": "Str"}).json()
    psa_client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP1111111A1"})
    psa_client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "CN113964850 US11909216B2"})
    ids = [d["id"] for d in
           psa_client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    psa_client.post(f"/api/tabs/{tab['id']}/psa", json={"doc_ids": ids, "stretch": True})
    assert seen["stretch"] is True
    msgs = psa_client.get(f"/api/tabs/{tab['id']}/state").json()["messages"]
    q = [m for m in msgs if m["role"] == "q" and "stretch" in m["text"].lower()][-1]
    assert q["text"].startswith("🪄 Argumentation stretch")
    c = [m for m in msgs if m["role"] == "c"][-1]
    assert any(p["title"] == "🪄 argumentation stretch" for p in c["participants"])
    # default remains strict
    psa_client.post(f"/api/tabs/{tab['id']}/psa", json={"doc_ids": ids})
    assert seen["stretch"] is False


def test_psa_prompt_stretch_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    args = ("STEP 1: x.", {"number": "EP1", "claims": "1. A thing."},
            [{"number": "D1DOC", "claims": "1. x"}, {"number": "D2DOC", "claims": "1. y"}])
    claude_bridge.psa(*args, stretch=True)
    p = captured["p"]
    assert "ADVOCACY MODE — ARGUMENTATION STRETCH" in p
    assert "REMAIN SILENT about features that are NOT disclosed" in p
    assert "Omission is allowed — misstatement is NOT" in p
    assert "TASK — PROBLEM-SOLUTION APPROACH" in p          # methodology still executes
    claude_bridge.psa(*args)
    assert "ADVOCACY MODE" not in captured["p"]             # strict default unchanged


def test_figures_all_captions_failed_keeps_unread_not_zero(client, monkeypatch):
    from patentbench import figures, fetcher as _f
    monkeypatch.setattr(figures, "download",
                        lambda urls, dest: [{"n": i + 1, "url": u, "path": f"/x/{i}.png"}
                                            for i, u in enumerate(urls)])
    # every vision call fails → zero captions, but sheets DO exist
    monkeypatch.setattr(figures, "caption_all",
                        lambda figs, model=None, workers=None, context="": figs)
    monkeypatch.setattr(_f, "figure_urls", lambda n: ["http://x/imgf0001.png"])
    t = client.post("/api/tabs", json={"name": "FF"}).json()
    client.post(f"/api/tabs/{t['id']}/documents", json={"numbers": ["US7654321B1"]})
    did = client.get(f"/api/tabs/{t['id']}/documents").json()["documents"][0]["id"]
    client.post(f"/api/tabs/{t['id']}/documents/{did}/figures")
    doc = client.get(f"/api/tabs/{t['id']}/documents").json()["documents"][0]
    assert doc["figures_n"] is None       # unread (re-run heals), NOT 'no drawings'
