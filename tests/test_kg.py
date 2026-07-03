"""Cross-tab knowledge graph + global search + cross-tab reference tests.

The LLM classifier (kgraph.classify_feature) is stubbed — these exercise the pure
DB/graph logic and the API wiring, not the model.
"""
import pytest
from fastapi.testclient import TestClient

import patentbench.db as db
from patentbench import claude_bridge, kgraph
from patentbench.web import api


@pytest.fixture
def dbfile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    return db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "UPLOADS", str(tmp_path / "uploads"))
    return TestClient(api.app)


# ---------- pure graph model ----------

def test_ensure_path_dedupes(dbfile):
    a = db.kg_ensure_path("Aerosol devices", "Battery", "Temp measurement",
                          "Thermistor voltage divider")
    b = db.kg_ensure_path("aerosol devices", "battery", "Temp measurement",
                          "Thermistor voltage divider")   # different case
    assert a["node_id"] == b["node_id"]                    # reused, not duplicated
    assert db.kg_tree()["node_count"] == 4                 # field/block/function/option once


def test_attach_feature_moves_not_duplicates(dbfile):
    t = db.create_tab("A")
    n1 = db.kg_ensure_path("F", "B", "Fn", "Opt1")["node_id"]
    n2 = db.kg_ensure_path("F", "B", "Fn", "Opt2")["node_id"]
    db.kg_attach_feature(n1, "thermistor reading", tab_id=t["id"], status="benchmark")
    db.kg_attach_feature(n2, "thermistor reading", tab_id=t["id"], status="benchmark")
    tree = db.kg_tree()
    feats = []

    def collect(node):
        feats.extend(node["features"])
        for c in node["children"]:
            collect(c)
    for r in tree["nodes"]:
        collect(r)
    # the occurrence moved from Opt1 to Opt2 — exactly one copy exists
    named = [f for f in feats if f["feature_name"] == "thermistor reading"]
    assert len(named) == 1
    assert named[0]["node_id"] == n2


def test_tree_rolls_up_counts_and_related(dbfile):
    t = db.create_tab("A")
    opt = db.kg_ensure_path("Aerosol", "Battery", "Temp meas", "Thermistor")["node_id"]
    mcu = db.kg_ensure_path("Aerosol", "MCU")["node_id"]
    db.kg_attach_feature(opt, "thermistor", tab_id=t["id"], status="benchmark")
    db.kg_add_edge(opt, mcu, "involves")
    tree = db.kg_tree()
    field = tree["nodes"][0]
    assert field["name"] == "Aerosol"
    assert field["total_features"] == 1          # rolled up from the option
    # find the option node and check its related edge points at MCU
    battery = next(c for c in field["children"] if c["name"] == "Battery")
    fn = battery["children"][0]
    option = fn["children"][0]
    assert any(r["name"] == "MCU" and r["rel"] == "involves" for r in option["related"])


def test_delete_node_cascades(dbfile):
    ids = db.kg_ensure_path("F", "B", "Fn", "Opt")
    db.kg_attach_feature(ids["node_id"], "x", status="benchmark")
    assert db.kg_delete_node(ids["field"])       # deleting the field
    assert db.kg_tree()["node_count"] == 0        # cascades all descendants


def test_candidate_nodes_by_word_overlap(dbfile):
    db.kg_ensure_path("Aerosol", "Battery", "Temp", "Thermistor voltage divider")
    cands = db.kg_candidate_nodes("thermistor divider circuit")
    assert cands and cands[0]["name"] == "Thermistor voltage divider"
    assert cands[0]["path"][0]["name"] == "Aerosol"


# ---------- cross-tab reference ----------

def test_cross_tab_reference(dbfile):
    ta = db.create_tab("A")
    tb = db.create_tab("B")
    res = db.add_documents(ta["id"], ["EP4338618A1"])
    doc_id = res["inserted"][0]
    db.update_document(doc_id, status="fetched", description="body",
                       digest="a digest about overlapping section",
                       verdict="MATCH SCORE: 8 — discloses the overlapping section")
    ref = db.cross_tab_reference("EP4338618A1", exclude_tab_id=tb["id"])
    assert ref and ref["tab_name"] == "A" and "overlapping" in ref["verdict"]
    # excluded from its own tab
    assert db.cross_tab_reference("EP4338618A1", exclude_tab_id=ta["id"]) is None
    # unknown number → None
    assert db.cross_tab_reference("US9999999B2", exclude_tab_id=tb["id"]) is None


# ---------- search ----------

def test_global_search(dbfile):
    ta = db.create_tab("Alpha")
    res = db.add_documents(ta["id"], ["US10395648B1"])
    db.update_document(res["inserted"][0], status="fetched",
                       title="Aerosol heater", digest="thermistor voltage divider")
    db.append_message(ta["id"], "q", "how is the thermistor wired?")
    db.append_message(ta["id"], "c", "via a voltage divider into the MCU")
    node = db.kg_ensure_path("Aerosol", "Battery", "Temp", "Thermistor divider")
    db.kg_attach_feature(node["node_id"], "thermistor", tab_id=ta["id"], status="benchmark")

    r = db.kg_search("thermistor")
    assert any("Thermistor" in n["name"] for n in r["nodes"])
    assert any(d["number"] == "US10395648B1" for d in r["documents"])
    assert any("thermistor" in m["snippet"].lower() for m in r["messages"])
    assert db.kg_search("")["nodes"] == []


# ---------- API ----------

def test_api_classify_and_attach(client, monkeypatch):
    monkeypatch.setattr(kgraph, "classify_feature", lambda name, model=None: {
        "field": "Aerosol devices", "block": "Battery",
        "function": "Temperature measurement", "option": "Thermistor voltage divider",
        "related_blocks": ["MCU", "Battery gauge"], "matched_option_id": None,
        "confidence": 0.9})
    tab = client.post("/api/tabs", json={"name": "A"}).json()
    r = client.post("/api/kg/classify",
                    json={"feature_name": "temp via thermistor", "tab_id": tab["id"]})
    assert r.status_code == 200
    cls = r.json()["classification"]
    assert cls["option"] == "Thermistor voltage divider"

    a = client.post("/api/kg/attach", json={
        "feature_name": "temp via thermistor", "field": cls["field"],
        "block": cls["block"], "function": cls["function"], "option": cls["option"],
        "related_blocks": cls["related_blocks"], "tab_id": tab["id"], "status": "benchmark"})
    assert a.status_code == 200
    path = a.json()["path"]
    assert [p["name"] for p in path] == ["Aerosol devices", "Battery",
                                         "Temperature measurement", "Thermistor voltage divider"]
    # graph now reachable + related MCU edge wired
    tree = client.get("/api/kg").json()
    field = tree["nodes"][0]
    assert field["total_features"] == 1
    opt = field["children"][0]["children"][0]["children"][0]
    assert any(rel["name"] == "MCU" for rel in opt["related"])


def test_api_attach_to_existing_node(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "A"}).json()
    # seed a node the DB way, then attach by node_id
    ids = db.kg_ensure_path("F", "B", "Fn", "Opt")
    r = client.post("/api/kg/attach", json={
        "feature_name": "some feature", "node_id": ids["node_id"], "tab_id": tab["id"]})
    assert r.status_code == 200 and r.json()["node_id"] == ids["node_id"]


def test_api_node_rename_reparent_delete(client):
    ids = db.kg_ensure_path("F", "B", "Fn", "Opt")
    r = client.patch(f"/api/kg/node/{ids['option']}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["path"][-1]["name"] == "Renamed"
    # reparent the block under a different field root
    other = db.kg_ensure_path("G")["field"]
    r = client.patch(f"/api/kg/node/{ids['block']}",
                     json={"parent_id": other, "reparent": True})
    assert r.status_code == 200
    assert client.delete(f"/api/kg/node/{ids['field']}").json()["deleted"] is True


def test_api_refs_endpoint(client):
    ta = client.post("/api/tabs", json={"name": "A"}).json()
    tb = client.post("/api/tabs", json={"name": "B"}).json()
    res = db.add_documents(ta["id"], ["EP4338618A1"])
    db.update_document(res["inserted"][0], status="fetched",
                       digest="the overlapping section digest")
    r = client.get(f"/api/tabs/{tb['id']}/refs",
                   params={"text": "overlapping section like in EP4338618"})
    assert r.status_code == 200
    refs = r.json()["refs"]
    assert refs and refs[0]["number"] == "EP4338618A1" and refs[0]["tab_name"] == "A"


def test_api_search_endpoint(client):
    ta = client.post("/api/tabs", json={"name": "A"}).json()
    res = db.add_documents(ta["id"], ["US10395648B1"])
    db.update_document(res["inserted"][0], status="fetched",
                       title="thermistor thing", digest="d")
    r = client.get("/api/search", params={"q": "thermistor"})
    assert r.status_code == 200
    assert any(d["number"] == "US10395648B1" for d in r.json()["documents"])


def test_api_rebuild(client, monkeypatch):
    monkeypatch.setattr(claude_bridge, "available", lambda: (True, ""))
    calls = []

    def fake_classify(name, model=None):
        calls.append(name)
        return {"field": "Field", "block": "Block", "function": "Fn",
                "option": f"Opt-{name[:4]}", "related_blocks": [],
                "matched_option_id": None, "confidence": 0.8}
    monkeypatch.setattr(kgraph, "classify_feature", fake_classify)

    ta = client.post("/api/tabs", json={"name": "A"}).json()
    # a feature-combination benchmark → two target features
    client.post(f"/api/tabs/{ta['id']}/benchmark/features", json={
        "title": "bm", "features": [{"name": "alpha feature", "weight": 3, "kind": "M"},
                                    {"name": "beta feature", "weight": 2, "kind": "M"}]})
    r = client.post("/api/kg/rebuild", json={"tab_id": ta["id"], "clear": True})
    assert r.status_code == 200
    body = r.json()
    assert body["attached"] == 2 and body["distinct_features"] == 2
    assert set(calls) == {"alpha feature", "beta feature"}


def test_documents_across_tabs_and_counts(dbfile):
    ta = db.create_tab("A")
    tb = db.create_tab("B")
    ra = db.add_documents(ta["id"], ["US1111111B1", "US2222222B1"])
    db.update_document(ra["inserted"][0], status="fetched", digest="d1", score=7)
    db.update_document(ra["inserted"][1], status="pending")          # not fetched
    rb = db.add_documents(tb["id"], ["US3333333B1", "US1111111B1"])  # dup number across tabs
    db.update_document(rb["inserted"][0], status="fetched", verdict="v3", score=9)
    db.update_document(rb["inserted"][1], status="fetched", digest="d1b", verdict="v1", score=5)

    counts = {c["tab_name"]: c for c in db.document_counts_by_tab()}
    assert counts["A"]["total"] == 2 and counts["A"]["fetched"] == 1
    assert counts["B"]["total"] == 2 and counts["B"]["fetched"] == 2

    # from tab A's perspective: only tab B's fetched docs, deduped by number
    other = db.documents_across_tabs(exclude_tab_id=ta["id"])
    nums = {d["number"]: d for d in other}
    assert set(nums) == {"US3333333B1", "US1111111B1"}
    assert nums["US3333333B1"]["score"] == 9
    # highest-score first
    assert other[0]["number"] == "US3333333B1"


def test_chat_all_tabs_roster_and_coverage(client, monkeypatch):
    seen = {}

    def fake_chat(question, **kw):
        seen["other_docs"] = kw.get("other_docs")
        seen["coverage"] = kw.get("coverage")
        return {"answer": "ok", "model": "claude-fable-5"}
    monkeypatch.setattr(claude_bridge, "chat", fake_chat)

    ta = client.post("/api/tabs", json={"name": "A"}).json()
    tb = client.post("/api/tabs", json={"name": "B"}).json()
    rb = db.add_documents(tb["id"], ["US3333333B1"])
    db.update_document(rb["inserted"][0], status="fetched", digest="digest3", score=8)

    r = client.post(f"/api/tabs/{ta['id']}/chat", json={
        "question": "find a combination across tabs", "use_documents": True, "all_tabs": True})
    assert r.status_code == 200
    assert seen["other_docs"] and seen["other_docs"][0]["number"] == "US3333333B1"
    assert any(c["tab_name"] == "B" and c["fetched"] == 1 for c in seen["coverage"])
    # a deterministic coverage confirmation message is emitted
    texts = [m["text"] for m in r.json()["messages"]]
    assert any("Considered documents across all tabs" in t for t in texts)

    # OFF by default → no cross-tab roster
    seen.clear()
    client.post(f"/api/tabs/{ta['id']}/chat", json={"question": "q", "use_documents": True})
    assert seen["other_docs"] is None


def test_chat_injects_cross_tab_xref(client, monkeypatch):
    seen = {}

    def fake_chat(question, **kw):
        seen["xrefs"] = kw.get("xrefs")
        return {"answer": "ok", "model": "claude-fable-5"}
    monkeypatch.setattr(claude_bridge, "chat", fake_chat)

    ta = client.post("/api/tabs", json={"name": "A"}).json()
    tb = client.post("/api/tabs", json={"name": "B"}).json()
    res = db.add_documents(ta["id"], ["EP4338618A1"])
    db.update_document(res["inserted"][0], status="fetched",
                       verdict="discloses the overlapping section")
    client.post(f"/api/tabs/{tb['id']}/chat", json={
        "question": "find the overlapping section like in EP4338618",
        "use_documents": True})
    assert seen["xrefs"] and seen["xrefs"][0]["number"] == "EP4338618A1"
