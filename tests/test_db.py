import patentbench.db as db


def test_tab_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    t = db.create_tab("Project A")
    assert db.list_tabs()[0]["name"] == "Project A"
    assert db.rename_tab(t["id"], "Project B")
    assert db.list_tabs()[0]["name"] == "Project B"
    assert db.delete_tab(t["id"])
    assert db.list_tabs() == []


def test_documents_dedupe_and_cascade(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    t = db.create_tab("P")
    res = db.add_documents(t["id"], ["US1234567B1", "US1234567B1", "EP1111111A1"])
    assert len(res["inserted"]) == 2 and res["skipped"] == ["US1234567B1"]
    db.append_message(t["id"], "q", "hello")
    db.set_notebook_config(t["id"], "nb-1", "Notebook", ["s1", "s2"])
    assert db.get_notebook_config(t["id"])["selected_source_ids"] == ["s1", "s2"]
    db.delete_tab(t["id"])
    assert db.list_documents(t["id"]) == []
    assert db.list_messages(t["id"]) == []
    assert db.get_notebook_config(t["id"]) is None


def test_message_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    t = db.create_tab("P")
    db.append_message(t["id"], "c", "answer", model="claude-fable-5",
                      participants=[{"kind": "model", "title": "claude-fable-5"}])
    msgs = db.list_messages(t["id"])
    assert msgs[0]["participants"][0]["kind"] == "model"
    assert msgs[0]["model"] == "claude-fable-5"


def test_cross_tab_reuse_by_number(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    a = db.create_tab("A"); b = db.create_tab("B")
    [src_id] = db.add_documents(a["id"], ["US9999999B1"])["inserted"]
    db.update_document(src_id, status="fetched", abstract="abs", claims="c",
                       description="full desc here", digest="DIG",
                       digest_model="claude-sonnet-4-6")
    # another tab finds it; same tab is excluded
    assert db.find_reusable_by_number("US9999999B1", exclude_tab_id=a["id"]) is None
    hit = db.find_reusable_by_number("US9999999B1", exclude_tab_id=b["id"])
    assert hit and hit["tab_name"] == "A" and hit["digest"] == "DIG"
    assert hit["digest_model"] == "claude-sonnet-4-6"
    # copy into a pending doc in B
    [dst_id] = db.add_documents(b["id"], ["US9999999B1"])["inserted"]
    db.copy_into_document(dst_id, hit)
    got = db.get_document(dst_id)
    assert got["status"] == "fetched" and got["digest"] == "DIG"
    assert got["description"] == "full desc here"


def test_cross_tab_reuse_by_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    a = db.create_tab("A"); b = db.create_tab("B")
    [src_id] = db.add_documents(a["id"], ["EP1234567B1"])["inserted"]
    db.update_document(src_id, status="fetched", description="ocr body",
                       content_hash="HASHX", text_model="claude-fable-5")
    assert db.find_reusable_by_hash("nope", exclude_tab_id=b["id"]) is None
    hit = db.find_reusable_by_hash("HASHX", exclude_tab_id=b["id"])
    assert hit and hit["description"] == "ocr body" and hit["text_model"] == "claude-fable-5"


def test_documents_disclosing_feature(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    a = db.create_tab("A"); b = db.create_tab("B")
    [d1] = db.add_documents(a["id"], ["US1B1"])["inserted"]
    [d2] = db.add_documents(b["id"], ["US2B1"])["inserted"]
    db.update_document(d1, feature_scores=json.dumps(
        [{"name": "Widget", "status": "yes", "note": "para 12"}]))
    db.update_document(d2, feature_scores=json.dumps(
        [{"name": "widget", "status": "no"}]),
        additional_scores=json.dumps([{"name": "Widget", "status": "stretch"}]))
    # from tab A's view: B's doc discloses Widget (A-feature stretch counts), case-insensitive
    rows = db.documents_disclosing_feature("Widget", exclude_tab_id=a["id"])
    assert len(rows) == 1 and rows[0]["tab_name"] == "B" and rows[0]["kind"] == "A"
    # including all tabs: d1 (yes) shows too
    allrows = db.documents_disclosing_feature("widget")
    assert {r["number"] for r in allrows} == {"US1B1", "US2B1"}
