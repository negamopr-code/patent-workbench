"""🔬 NLM mega-screen tests — the rotation tournament runs against a stateful fake
notebook (in-memory source store + scripted answers); no real `nlm`/`claude` ever runs
(verify: CLAUDE_BIN=/nonexistent NLM_BIN=/nonexistent pytest)."""
import time

import patentbench.db as db
from patentbench import nlm_bridge
from patentbench.web import api

NUMS = ["EP3667901A1", "EP3667902A1", "EP3667903A1",
        "EP3667904A1", "EP3667905A1", "EP3667906A1"]


class FakeNlm:
    """A stateful NotebookLM: notebooks hold {source_id: title}; queries pop
    scripted answers in order (a dict, or a callable receiving (fake, nb, q))."""

    def __init__(self, answers):
        self.notebooks = {}          # nb_id -> {sid: title}
        self.titles = {}             # nb_id -> notebook title
        self.answers = list(answers)
        self.queries = []
        self.deleted = []
        self.drop_titles = ()        # numbers to silently NOT index (ghost adds)
        self._n = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
        monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {
            "notebooks": [{"id": i, "title": t, "sources": len(self.notebooks[i])}
                          for i, t in self.titles.items()]})
        monkeypatch.setattr(nlm_bridge, "create_notebook", self._create)
        monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {
            "sources": [{"id": s, "title": t}
                        for s, t in self.notebooks.get(nb, {}).items()]})
        monkeypatch.setattr(nlm_bridge, "add_source_text", self._add)
        monkeypatch.setattr(nlm_bridge, "delete_source", self._delete)
        monkeypatch.setattr(nlm_bridge, "wait_sources_ready",
                            lambda nb, timeout=0, poll=0, known_ready=None:
                            {"ready": True, "processed": 0, "total": 0})
        monkeypatch.setattr(nlm_bridge, "source_content", lambda sid: {"content": "x"})
        monkeypatch.setattr(nlm_bridge, "query", self._query)

    def _create(self, title):
        self._n += 1
        nb = f"nb-screen-{self._n}"
        self.notebooks[nb] = {}
        self.titles[nb] = title
        return {"id": nb, "title": title}

    def _add(self, nb, title, text):
        if any(num in title for num in self.drop_titles):
            return {"ok": True}                       # ghost: accepted, never indexed
        self._n += 1
        self.notebooks.setdefault(nb, {})[f"s{self._n}"] = title
        return {"ok": True}

    def _delete(self, ids, nb=None):
        self.deleted += list(ids)
        for srcs in self.notebooks.values():
            for sid in ids:
                srcs.pop(sid, None)
        return {"ok": True, "deleted": len(ids)}

    def _query(self, nb, q, source_ids=None):
        self.queries.append((nb, q))
        a = self.answers.pop(0) if self.answers else {"answer": "TOP: none"}
        return a(self, nb, q) if callable(a) else a


def _mk_tab(client, n_docs=6, name="Screen"):
    tab = client.post("/api/tabs", json={"name": name}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark",
               json={"text": "https://patents.google.com/patent/US10395648B1/en"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": NUMS[:n_docs], "source": "image"})
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    return tid, {d["number"]: d["id"] for d in docs}


def _wait_screen(client, tid, until=("done", "error", "interrupted", "quota_paused", "paused"),
                 tries=200):
    for _ in range(tries):
        s = client.get(f"/api/tabs/{tid}/nlm-screen/status").json()
        if s.get("phase") in until:
            return s
        time.sleep(0.05)
    raise AssertionError(f"screen never reached {until}: {s}")


def test_screen_happy_path_rotates_and_writes_shortlist(client, monkeypatch):
    fake = FakeNlm([
        # round 1 (docs 1-5): top 2 survive, one near-miss also graduates
        {"answer": "TOP:\n1. EP3667902A1 — solid\n2. EP3667904A1 — ok\n"
                   "NEAR-MISSES: EP3667901A1"},
        # round 2 (doc 6 + survivors 902/904): newcomer wins
        {"answer": "TOP:\n1. EP3667906A1 — best\n2. EP3667902A1 — still good"},
        # finalize: rich shortlist over the 4 graduates
        {"answer": "SHORTLIST — EP3667906A1, EP3667902A1\nBEST: EP3667906A1\n"
                   "SECOND-BEST: EP3667902A1\nFEATURE MAP: 1. YES"},
    ])
    fake.install(monkeypatch)
    tid, ids = _mk_tab(client)
    r = client.post(f"/api/tabs/{tid}/nlm-screen",
                    json={"batch_size": 5, "survivor_cap": 2}).json()
    assert r["started"] is True and r["rounds_estimate"] == 2
    s = _wait_screen(client, tid, until=("done",))
    assert s["screened"] == 6 and s["round"] == 2 and s["graduates"] == 4
    docs = {d["number"]: d for d in
            client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert docs["EP3667906A1"]["nlm_rank"] == 1        # finalize order → rank
    assert docs["EP3667902A1"]["nlm_rank"] == 2
    assert docs["EP3667906A1"]["shortlisted"] == 1
    assert docs["EP3667903A1"]["nlm_screen_state"] == "rejected"
    assert docs["EP3667905A1"]["nlm_screen_state"] == "rejected"
    assert docs["EP3667901A1"]["nlm_screen_state"] == "graduate"   # near-miss graduated
    # Correction-A regression: the screening notebook never becomes the doc mirror
    assert all(d["nlm_source_notebook"] is None for d in docs.values())
    assert client.get(f"/api/tabs/{tid}/state").json()["notebook"] is None
    # round 2's staging rotated round 1's losers out of the notebook
    assert fake.deleted


def test_screen_quota_pause_then_manual_resume(client, monkeypatch):
    fake = FakeNlm([
        {"error": "API error: RESOURCE_EXHAUSTED", "quota": True},
        {"answer": "TOP:\n1. EP3667902A1\n2. EP3667901A1"},
        {"answer": "SHORTLIST — EP3667902A1\nBEST: EP3667902A1\nFEATURE MAP: 1. YES"},
    ])
    fake.install(monkeypatch)
    tid, _ = _mk_tab(client, n_docs=5)
    client.post(f"/api/tabs/{tid}/nlm-screen", json={"batch_size": 5, "survivor_cap": 2})
    s = _wait_screen(client, tid, until=("quota_paused",))
    assert s["resumable"] is True and s["screened"] == 0     # nothing lost, nothing advanced
    r = client.post(f"/api/tabs/{tid}/nlm-screen", json={"resume": True}).json()
    assert r["started"] is True
    s = _wait_screen(client, tid, until=("done",))
    assert s["screened"] == 5


def test_screen_empty_answer_is_quota_suspect_not_rejection(client, monkeypatch):
    fake = FakeNlm([{"error": "empty answer from NotebookLM (quota exhausted?)",
                     "quota_suspect": True}])
    fake.install(monkeypatch)
    tid, _ = _mk_tab(client, n_docs=5)
    client.post(f"/api/tabs/{tid}/nlm-screen", json={"batch_size": 5, "survivor_cap": 2})
    s = _wait_screen(client, tid, until=("quota_paused",))
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["nlm_screen_state"] is None for d in docs)  # nobody was "rejected" by an outage


def test_screen_truncated_answer_never_costs_the_batch(client, monkeypatch):
    fake = FakeNlm([
        {"answer": "Let me think about which documents to evaluate next…"},   # truncated
        {"answer": "I will now proceed to evaluate EP3667901A1 further…"},    # retry also bad
    ])
    fake.install(monkeypatch)
    tid, _ = _mk_tab(client, n_docs=5)
    client.post(f"/api/tabs/{tid}/nlm-screen", json={"batch_size": 5, "survivor_cap": 2})
    s = _wait_screen(client, tid, until=("error",))
    assert s["screened"] == 0 and s["resumable"] is True
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    # EP3667901A1 was named in the truncated preamble — it must NOT count as assessed
    assert all(d["nlm_screen_state"] is None for d in docs)
    assert not fake.deleted                                   # sources kept for the retry


def test_screen_ignores_hallucinated_numbers(client, monkeypatch):
    fake = FakeNlm([
        {"answer": "TOP:\n1. EP9999999A1 — invented\n2. EP3667902A1 — real"},
        {"answer": "SHORTLIST — EP3667902A1\nBEST: EP3667902A1\nFEATURE MAP: 1. YES"},
    ])
    fake.install(monkeypatch)
    tid, _ = _mk_tab(client, n_docs=5)
    client.post(f"/api/tabs/{tid}/nlm-screen", json={"batch_size": 5, "survivor_cap": 2})
    _wait_screen(client, tid, until=("done",))
    docs = {d["number"]: d for d in
            client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert docs["EP3667902A1"]["nlm_rank"] == 1
    assert all(d["nlm_rank"] in (None, 1) for d in docs.values())   # the ghost got no slot


def test_screen_ghost_add_marked_add_failed(client, monkeypatch):
    fake = FakeNlm([
        {"answer": "TOP:\n1. EP3667902A1\n2. EP3667901A1"},
        {"answer": "SHORTLIST — EP3667902A1\nBEST: EP3667902A1\nFEATURE MAP: 1. YES"},
    ])
    fake.drop_titles = ("EP3667903A1",)          # accepted but never indexed
    fake.install(monkeypatch)
    tid, _ = _mk_tab(client, n_docs=5)
    client.post(f"/api/tabs/{tid}/nlm-screen", json={"batch_size": 5, "survivor_cap": 2})
    _wait_screen(client, tid, until=("done",))
    docs = {d["number"]: d for d in
            client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert docs["EP3667903A1"]["nlm_screen_state"] == "add_failed"
    assert docs["EP3667904A1"]["nlm_screen_state"] == "rejected"


def test_screen_stop_finalizes_from_partial_ledger(client, monkeypatch):
    fake = FakeNlm([
        {"error": "RESOURCE_EXHAUSTED", "quota": True},     # round 2 quota-pauses
        {"answer": "SHORTLIST — EP3667902A1\nBEST: EP3667902A1\nFEATURE MAP: 1. YES"},
    ])
    fake.answers.insert(0, {"answer": "TOP:\n1. EP3667902A1\n2. EP3667904A1"})  # round 1 OK
    fake.install(monkeypatch)
    tid, _ = _mk_tab(client, n_docs=6)
    client.post(f"/api/tabs/{tid}/nlm-screen", json={"batch_size": 5, "survivor_cap": 2})
    _wait_screen(client, tid, until=("quota_paused",))
    client.post(f"/api/tabs/{tid}/nlm-screen/stop")
    s = _wait_screen(client, tid, until=("done",))
    assert s["graduates"] == 2                              # round 1's graduates only
    docs = {d["number"]: d for d in
            client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert docs["EP3667902A1"]["nlm_rank"] == 1


def test_screen_capacity_validator(client):
    tid, _ = _mk_tab(client, n_docs=5)
    r = client.post(f"/api/tabs/{tid}/nlm-screen",
                    json={"batch_size": 48, "survivor_cap": 20})
    assert r.status_code == 422                             # 1+20+48 > 50


def test_screen_needs_benchmark(client, monkeypatch):
    FakeNlm([]).install(monkeypatch)
    tab = client.post("/api/tabs", json={"name": "NoBM"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"numbers": [NUMS[0]], "source": "image"})
    r = client.post(f"/api/tabs/{tab['id']}/nlm-screen", json={})
    assert r.status_code == 400 and "benchmark" in r.json()["detail"]


def test_promise_ranks_screen_rejected_below_unknown():
    assert api._promise({"nlm_screen_state": "rejected"}) < api._promise({})
    assert api._promise({"score": 3, "nlm_screen_state": "rejected"}) == 3.0
    assert api._promise({"nlm_screen_state": "graduate"}) == api._promise({})


def test_quota_error_detection_unit():
    assert nlm_bridge.is_quota_error({"error": "x", "quota": True})
    assert nlm_bridge.is_quota_error({"error": "empty answer", "quota_suspect": True})
    assert not nlm_bridge.is_quota_error({"error": "network down"})
    assert not nlm_bridge.is_quota_error({"answer": "fine"})
    assert nlm_bridge._err_result("API error RESOURCE_EXHAUSTED").get("quota") is True
    assert nlm_bridge._err_result("HTTP 429 too many requests").get("quota") is True
    assert nlm_bridge._err_result("connection refused").get("quota") is None


def test_create_notebook_cap_detected_for_resource_exhausted(monkeypatch):
    # Google's ~100-notebook cap error drifted from code 3 INVALID_ARGUMENT to
    # code 8 RESOURCE_EXHAUSTED (live 2026-08-05) — both must yield the actionable
    # "delete some notebooks" message, not a raw error.
    import subprocess
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "_run", lambda cmd, timeout: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="API error (code 8): RESOURCE_EXHAUSTED"))
    monkeypatch.setattr(nlm_bridge, "_notebook_count", lambda: 100)
    r = nlm_bridge.create_notebook("X")
    assert r.get("limit") is True and "100" in r["error"] and "Delete" in r["error"]


def test_wait_sources_ready_known_ready_skips_probes(monkeypatch):
    probed = []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "list_sources", lambda nb, force=False: {
        "sources": [{"id": "s1", "title": "a"}, {"id": "s2", "title": "b"}]})
    monkeypatch.setattr(nlm_bridge, "source_content",
                        lambda sid: probed.append(sid) or {"content": "x"})
    r = nlm_bridge.wait_sources_ready("nb", timeout=5, poll=0.01, known_ready={"s1"})
    assert r["ready"] is True
    assert probed == ["s2"]                                 # s1 was pre-confirmed
