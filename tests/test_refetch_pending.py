"""🔁 stuck-pending recovery — fetches orphaned by a container restart get re-queued
in bulk; STALE reuse-held pendings are auto-reused (the modal that would have asked
is long gone — its answer was lost to a closed browser or the wrong-tab 404 race —
and the alternative is pending-forever); FRESH pendings are always left alone."""
import time

import patentbench.db as db
from patentbench.web import api


def _add_pending(client, name, numbers, age_s=3600):
    tab = client.post("/api/tabs", json={"name": name}).json()
    tid = tab["id"]
    res = db.add_documents(tid, numbers, source="manual")
    for did in res["inserted"]:                      # backdate = orphaned long ago
        db.update_document(did, added_at=db._now() - age_s)
    return tid


def _wait_fetched(client, tid, tries=100):
    for _ in range(tries):
        docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
        if all(d["status"] == "fetched" for d in docs):
            return docs
        time.sleep(0.05)
    return client.get(f"/api/tabs/{tid}/documents").json()["documents"]


def test_refetch_pending_requeues_stale(client):
    tid = _add_pending(client, "Stuck", ["EP3667901A1", "EP3667902A1"])
    r = client.post(f"/api/tabs/{tid}/documents/refetch-pending").json()
    assert r["requeued"] == 2 and set(r["numbers"]) == {"EP3667901A1", "EP3667902A1"}
    docs = _wait_fetched(client, tid)
    assert all(d["status"] == "fetched" for d in docs)
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert any("Recovered 2 stuck pending" in t for t in msgs)


def test_refetch_pending_skips_fresh_and_reuses_stale_held(client, monkeypatch):
    # a FRESH pending (a live add may still be working on it) is left alone
    tid = _add_pending(client, "Fresh", ["EP3667903A1"], age_s=0)
    r = client.post(f"/api/tabs/{tid}/documents/refetch-pending").json()
    assert r["requeued"] == 0 and r["reused"] == 0
    # a STALE reuse-held pending (fetched copy in another tab, past the staleness
    # window) is auto-reused: the asking modal is gone, nobody will ever answer it
    src_tab = client.post("/api/tabs", json={"name": "Src"}).json()["id"]
    client.post(f"/api/tabs/{src_tab}/documents",
                json={"numbers": ["EP3667904A1"], "source": "image"})
    for d in db.list_documents(src_tab):             # give the copy a digest → reusable
        db.update_document(d["id"], digest="d")
    tid2 = _add_pending(client, "Held", ["EP3667904A1"])
    r = client.post(f"/api/tabs/{tid2}/documents/refetch-pending").json()
    assert r["requeued"] == 0 and r["reused"] == 1
    docs = client.get(f"/api/tabs/{tid2}/documents").json()["documents"]
    assert docs[0]["status"] == "fetched"            # copied from the src tab
    assert docs[0].get("digest_len")                 # digest came along with the copy


def test_boot_sweep_requeues_all_tabs_once(client, monkeypatch, tmp_path):
    tid = _add_pending(client, "Boot", ["EP3667905A1"])
    monkeypatch.setenv("PB_AUTO_REFETCH_DELAY", "0")
    api._auto_refetch_sweep()                        # first sweep takes the lock + fetches
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["status"] == "fetched" for d in docs)
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert sum("Auto-resume after restart" in t for t in msgs) == 1
    api._auto_refetch_sweep()                        # second worker: lock held → no-op
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert sum("Auto-resume after restart" in t for t in msgs) == 1
