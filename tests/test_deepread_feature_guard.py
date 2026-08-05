"""⚠️ no-features deep-read guard — the tab-11 double-spend lesson: a read started
before the feature list was accepted produces holistic-only verdicts (feature_scores
NULL) and must be repeated once features exist; the API now says so BEFORE spending."""
import time

import patentbench.db as db


def _tab_with_docs(client, name="Guard"):
    tab = client.post("/api/tabs", json={"name": name}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP3667902A1"], "source": "image"})
    return tid


def _wait_read(client, tid, tries=100):
    for _ in range(tries):
        if not client.get(f"/api/tabs/{tid}/deep-compare/status").json()["running"]:
            return
        time.sleep(0.05)


def test_deep_compare_warns_without_features(client):
    tid = _tab_with_docs(client)
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    r = client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()
    assert r["started"] is True and r["features_missing"] is True
    _wait_read(client, tid)
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert any("NO accepted feature list" in t for t in msgs)


def test_deep_compare_no_warning_with_features(client):
    tid = _tab_with_docs(client, "Guard2")
    db.set_benchmark_features(tid, "1. widget (importance 5/5)", "spec",
                              features=[{"name": "widget", "weight": 5}])
    r = client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()
    assert r["started"] is True and r["features_missing"] is False
    _wait_read(client, tid)
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert not any("NO accepted feature list" in t for t in msgs)
