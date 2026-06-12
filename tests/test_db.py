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
