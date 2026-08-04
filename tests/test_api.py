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
            "feature-map", "tech-effect"} <= set(keys)     # default + presets
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
    # guideline: relations in words, not signs; figures cited alongside [00NN]
    assert "PLAIN WORDS, NO SYMBOL SHORTHAND" in p
    assert "'→'" in p and "'='" in p
    assert "cite the figure(s)" in p


def test_chat_retries_shrunken_prompt_when_too_long(monkeypatch):
    calls = []
    def fake_run(prompt, model, extra_args=None, cwd=None, timeout=None):
        calls.append(len(prompt))
        if len(calls) == 1:
            return {"error": "API Error: 400 Prompt is too long"}
        return {"answer": "ok", "model": model}
    monkeypatch.setattr(claude_bridge, "_run_claude", fake_run)
    res = claude_bridge.chat("q", history=[{"role": "user", "text": "x" * 5000}] * 30)
    assert res["answer"].startswith("ok")
    assert "auto-trimmed" in res["answer"]           # the trim is disclosed
    assert len(calls) == 2 and calls[1] < calls[0]   # retried with a smaller prompt
    # a non-length error is NOT retried
    calls.clear()
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda *a, **k: calls.append(1) or {"error": "boom"})
    assert claude_bridge.chat("q")["error"] == "boom"
    assert len(calls) == 1


def test_chat_timeout_scales_with_prompt_size(monkeypatch):
    """A grounded prompt on a 1000+-doc tab (500-800k chars) needs more wall-clock
    than the flat CHAT_TIMEOUT — every big-tab chat died with 'claude chat timed
    out' (bit 2026-07-28). The timeout must grow with the prompt, capped."""
    seen = []
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda p, m, extra_args=None, cwd=None, timeout=None:
                        seen.append((len(p), timeout)) or {"answer": "ok", "model": m})
    claude_bridge.chat("q")
    small_len, small_to = seen[0]
    claude_bridge.chat("q", focus=[{"number": "X", "claims": "c" * 500_000}])
    big_len, big_to = seen[1]
    assert small_to >= claude_bridge.CHAT_TIMEOUT
    assert big_to > small_to                      # grows with the prompt
    assert big_to == claude_bridge._chat_timeout(big_len)
    assert claude_bridge._chat_timeout(10_000_000) == 1200.0   # capped


def test_answer_format_edit_roundtrip(client, tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "FMT_OVERRIDE_DIR", str(tmp_path / "fmt"))
    r = client.get("/api/answer-format/feature-map").json()
    assert r["overridden"] is False
    assert r["text"] == r["default"] and "INTERLINEAR FEATURE MAP" in r["text"]
    # save an edited version → it replaces the built-in in the prompt
    r = client.put("/api/answer-format/feature-map",
                   json={"text": "MY CUSTOM MAP RULES"}).json()
    assert r["overridden"] is True and r["text"] == "MY CUSTOM MAP RULES"
    p = claude_bridge.build_prompt("q", answer_format="feature-map")
    assert "MY CUSTOM MAP RULES" in p and "INTERLINEAR FEATURE MAP" not in p
    # empty text resets back to the built-in
    r = client.put("/api/answer-format/feature-map", json={"text": "  "}).json()
    assert r["overridden"] is False
    assert "INTERLINEAR FEATURE MAP" in claude_bridge.build_prompt(
        "q", answer_format="feature-map")
    # the default style and unknown keys are not editable
    assert client.get("/api/answer-format/bogus").status_code == 404


def test_tet_roundtrip_and_injection(client, tmp_path, monkeypatch):
    """📄 TET: the pasted example is stored globally and the tech-effect answer
    format carries the LIVE template into the prompt (wrapper + example)."""
    monkeypatch.setattr(claude_bridge, "FMT_OVERRIDE_DIR", str(tmp_path / "fmt"))
    r = client.get("/api/tet").json()
    assert r["overridden"] is False
    assert "TECHNICAL EFFECT TEMPLATE" in r["text"]        # built-in skeleton
    # the format injects wrapper + skeleton and replaces the default style line
    p = claude_bridge.build_prompt("q", answer_format="tech-effect")
    assert "TECHNICAL EFFECT ARGUMENTATION" in p
    assert "distinguishing technical feature" in p
    assert "ANSWER STYLE" not in p
    # a pasted example replaces the skeleton on the very next prompt
    r = client.put("/api/tet", json={"text": "MY WORKED TET EXAMPLE"}).json()
    assert r["overridden"] is True
    p = claude_bridge.build_prompt("q", answer_format="tech-effect")
    assert "MY WORKED TET EXAMPLE" in p
    assert "distinguishing technical feature" not in p     # skeleton gone
    assert "TECHNICAL EFFECT ARGUMENTATION" in p           # wrapper stays
    # empty text resets back to the skeleton
    r = client.put("/api/tet", json={"text": " "}).json()
    assert r["overridden"] is False
    assert "distinguishing technical feature" in claude_bridge.build_prompt(
        "q", answer_format="tech-effect")


def test_tet_supporting_docs_crud_and_chat_wiring(client, monkeypatch):
    """📄 per-tab TET supporting documents: paste/list/get/delete, kind
    validation, and the chat wiring — tech-effect answers carry them, other
    formats do not."""
    tab = client.post("/api/tabs", json={"name": "T"}).json()
    tid = tab["id"]
    # bad kind is rejected
    r = client.post(f"/api/tabs/{tid}/tet-docs/text",
                    json={"kind": "bogus", "text": "x" * 100})
    assert r.status_code == 400
    # paste an amended set of claims
    r = client.post(f"/api/tabs/{tid}/tet-docs/text",
                    json={"kind": "amended-claims",
                          "text": "1. An amended widget with a flange."}).json()
    assert r["kind"] == "amended-claims" and r["chars"] > 0
    doc_id = r["id"]
    lst = client.get(f"/api/tabs/{tid}/tet-docs").json()
    assert [d["id"] for d in lst["docs"]] == [doc_id]
    assert "amended-claims" in lst["kinds"] and "search-report" in lst["kinds"]
    assert "text" not in lst["docs"][0]              # list is metadata-only
    full = client.get(f"/api/tabs/{tid}/tet-docs/{doc_id}").json()
    assert "flange" in full["text"]
    # chat: the case docs ride along on EVERY answer, whatever the format —
    # "build on the amended claims" must work without the tech-effect preset
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: seen.update(k) or {"answer": "ok", "model": "m"})
    client.post(f"/api/tabs/{tid}/chat",
                json={"question": "argue", "answer_format": "tech-effect"})
    assert [d["id"] for d in seen["tet_docs"]] == [doc_id]
    client.post(f"/api/tabs/{tid}/chat", json={"question": "argue"})
    assert [d["id"] for d in seen["tet_docs"]] == [doc_id]
    # prompt injection: block header + governing rule + the document text
    p = claude_bridge.build_prompt(
        "q", answer_format="tech-effect",
        tet_docs=[{"kind": "amended-claims", "name": "claims-v2.pdf",
                   "text": "1. An amended widget with a flange."}])
    assert "TET SUPPORTING DOCUMENTS" in p
    assert "[Amended set of claims — claims-v2.pdf]" in p and "flange" in p
    # delete
    assert client.delete(f"/api/tabs/{tid}/tet-docs/{doc_id}").json()["ok"] is True
    assert client.get(f"/api/tabs/{tid}/tet-docs").json()["docs"] == []
    assert client.delete(f"/api/tabs/{tid}/tet-docs/{doc_id}").status_code == 404


def test_tet_scanned_pdf_upload_goes_through_ocr(client, monkeypatch):
    """📄 a scanned (no-text-layer) TET PDF is NOT rejected: it lands as a
    ⏳ pending row, the background vision OCR fills it in, and only READY rows
    reach the tech-effect prompt."""
    tid = client.post("/api/tabs", json={"name": "T"}).json()["id"]
    monkeypatch.setattr(api.extract, "text_from_pdf",
                        lambda p: {"error": "no text layer"})
    def fake_ocr(doc_id, pdf_path):
        db.update_tet_doc(doc_id, text="OCR-TRANSCRIBED CLAIMS " * 5,
                          status="ready", progress=None, error=None)
    monkeypatch.setattr(api, "_transcribe_tet_doc", fake_ocr)
    r = client.post(f"/api/tabs/{tid}/tet-docs",
                    data={"kind": "amended-claims"},
                    files={"file": ("scan.pdf", b"%PDF-1.4 fake",
                                    "application/pdf")}).json()
    assert r["pending"] is True and r["status"] == "pending"
    # TestClient runs background tasks on response completion → already ready
    docs = client.get(f"/api/tabs/{tid}/tet-docs").json()["docs"]
    assert docs[0]["status"] == "ready" and docs[0]["chars"] > 0
    # a row still in OCR never reaches the prompt (ready_only filter)
    db.add_tet_doc(tid, "other", "still-scanning.pdf", "", status="pending")
    seen = {}
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: seen.update(k) or {"answer": "ok", "model": "m"})
    client.post(f"/api/tabs/{tid}/chat",
                json={"question": "argue", "answer_format": "tech-effect"})
    assert [d["name"] for d in seen["tet_docs"]] == ["scan.pdf"]
    assert all(d["status"] == "ready" for d in seen["tet_docs"])


def test_tet_esop_citations_extraction(client):
    """📄 D-labeled and bare patent numbers are pulled out of a search-report
    TET doc (labeled first, own/benchmark number excluded); the literal
    /citations path does not collide with the /{doc_id} route."""
    tid = client.post("/api/tabs", json={"name": "T"}).json()["id"]
    db.set_benchmark(tid, source="number", number="EP4444444A1")
    esop = ("EXTENDED EUROPEAN SEARCH REPORT for EP 4444444 A1.\n"
            "Category X: D1: US 2016/0123456 A1 (SMITH) claims 1-5.\n"
            "Category Y: D2 EP 1234567 B1 (JONES).\n"
            "Also of interest: WO 2019/055123 A1.\n")
    client.post(f"/api/tabs/{tid}/tet-docs/text",
                json={"kind": "search-report", "text": esop})
    cits = client.get(f"/api/tabs/{tid}/tet-docs/citations").json()["citations"]
    by_num = {c["number"]: c["label"] for c in cits}
    assert by_num.get("US20160123456A1") == "D1"
    assert by_num.get("EP1234567B1") == "D2"
    assert "WO2019055123A1" in by_num and by_num["WO2019055123A1"] is None
    assert "EP4444444A1" not in by_num                 # the case's own number
    assert [c["label"] for c in cits[:2]] == ["D1", "D2"]   # labeled lead
    # a non-search-report doc contributes nothing
    client.post(f"/api/tabs/{tid}/tet-docs/text",
                json={"kind": "applicant-arguments",
                      "text": "We disagree with the citation of US 9999999 B2 entirely."})
    cits2 = client.get(f"/api/tabs/{tid}/tet-docs/citations").json()["citations"]
    assert all(c["number"] != "US9999999B2" for c in cits2)


def test_tet_123_check(client, monkeypatch):
    """⚖ Art. 123(2): needs amended claims + at least one 'as filed' document;
    passes the right doc sets to the bridge; the analysis lands in the chat."""
    tid = client.post("/api/tabs", json={"name": "T"}).json()["id"]
    r = client.post(f"/api/tabs/{tid}/tet-123check", json={})
    assert r.status_code == 400 and "Amended set of claims" in r.json()["detail"]
    client.post(f"/api/tabs/{tid}/tet-docs/text",
                json={"kind": "amended-claims",
                      "text": "1. A widget comprising a flange and a seal."})
    r = client.post(f"/api/tabs/{tid}/tet-123check", json={})
    assert r.status_code == 400 and "Initial" in r.json()["detail"]
    client.post(f"/api/tabs/{tid}/tet-docs/text",
                json={"kind": "initial-description",
                      "text": "The widget may comprise a flange [0007]."})
    seen = {}
    monkeypatch.setattr(claude_bridge, "tet_123_check",
                        lambda amended, init_cl, init_de, model=None:
                        seen.update(a=amended, c=init_cl, d=init_de, m=model)
                        or {"answer": "OK: flange has basis in [0007]."})
    r = client.post(f"/api/tabs/{tid}/tet-123check", json={}).json()
    assert seen["a"][0]["kind"] == "amended-claims"
    assert seen["c"] == [] and seen["d"][0]["kind"] == "initial-description"
    assert r["messages"][0]["text"].startswith("OK: flange")
    titles = [p["title"] for p in r["messages"][0]["participants"]]
    assert "⚖ Art. 123(2) check" in titles
    # 💾 the result is stored as a system TET doc, so later chat answers reuse
    # the established basis instead of re-running the check; latest run wins
    stored = [d for d in client.get(f"/api/tabs/{tid}/tet-docs").json()["docs"]
              if d["kind"] == "123-check"]
    assert len(stored) == 1
    assert "flange" in client.get(
        f"/api/tabs/{tid}/tet-docs/{stored[0]['id']}").json()["text"]
    client.post(f"/api/tabs/{tid}/tet-123check", json={})       # re-run
    stored2 = [d for d in client.get(f"/api/tabs/{tid}/tet-docs").json()["docs"]
               if d["kind"] == "123-check"]
    assert len(stored2) == 1                                    # replaced, not stacked
    # ...and the check itself never feeds on its own stored result
    assert all(d["kind"] != "123-check"
               for d in seen["a"] + seen["c"] + seen["d"])


def test_tet_123_check_prompt_assembly(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    claude_bridge.tet_123_check(
        amended=[{"name": "claims-v2", "text": "1. A widget with a flange."}],
        initial_claims=[],
        initial_desc=[{"name": "as-filed", "text": "A flange is optional [0007]."}])
    p = captured["p"]
    assert "ARTICLE 123(2) EPC CHECK" in p
    assert "AMENDED SET OF CLAIMS" in p and "INITIAL DESCRIPTION" in p
    assert "no initial set of claims was provided" in p     # the missing-doc note
    assert "directly and unambiguously derivable" in p
    assert "HOUSE STYLE" in p                                # house style rides along
    # ready-to-file closing paragraph + explicit overall conclusion
    assert "Basis for amendments (Art. 123(2), 76(1) EPC)" in p
    assert "whether basis for the amendments is there" in p


def test_epc_sanity_parsing(monkeypatch):
    """🧪 the checker's three outcomes: clean, corrected (answer + notes split
    on the marker), unparseable (treated as error so the caller keeps the
    original answer)."""
    outs = {}

    def fake_run(prompt, model, extra_args=None, cwd=None, timeout=None):
        outs["p"] = prompt
        return {"answer": outs["next"], "model": model}
    monkeypatch.setattr(claude_bridge, "_run_claude", fake_run)
    outs["next"] = "CLEAN"
    assert claude_bridge.epc_sanity("some answer")["clean"] is True
    outs["next"] = ("The objective technical problem is how to choose power "
                    "independently.\n---CORRECTIONS---\n1: problem named separate "
                    "frequencies; reformulated from the effect alone.")
    r = claude_bridge.epc_sanity("bad answer")
    assert r["clean"] is False
    assert r["answer"].startswith("The objective technical problem")
    assert "reformulated" in r["notes"]
    outs["next"] = "some rambling with no verdict"
    assert "error" in claude_bridge.epc_sanity("whatever")
    # the checklist itself carries the load-bearing rules
    assert "PROBLEM WITHOUT SOLUTION" in outs["p"]
    assert "COULD-WOULD" in outs["p"] and "EX POST FACTO" in outs["p"]
    assert "123(2)" in outs["p"]


def test_epc_sanity_repairs_tech_effect_chat(client, monkeypatch):
    """🧪 a tech-effect chat answer is replaced by the checker's corrected
    version, the chip lands in participants and the notes in a system message;
    a checker FAILURE keeps the original answer."""
    tid = client.post("/api/tabs", json={"name": "T"}).json()["id"]
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: {"answer": "problem with solution inside",
                                         "model": "m"})
    monkeypatch.setattr(claude_bridge, "epc_sanity",
                        lambda answer, model=None:
                        {"clean": False, "answer": "problem from the effect alone",
                         "notes": "1: fixed"})
    r = client.post(f"/api/tabs/{tid}/chat",
                    json={"question": "build", "answer_format": "tech-effect"}).json()
    texts = [m["text"] for m in r["messages"]]
    assert any("effect alone" in t for t in texts)
    assert not any("solution inside" in t for t in texts)
    assert any("🧪 EPC sanity check corrected" in t for t in texts)
    chips = [p["title"] for m in r["messages"] for p in (m.get("participants") or [])]
    assert any(c.startswith("🧪 EPC sanity: 1") for c in chips)
    # checker failure → original answer survives, chip says unavailable
    monkeypatch.setattr(claude_bridge, "epc_sanity",
                        lambda answer, model=None: {"error": "boom"})
    r = client.post(f"/api/tabs/{tid}/chat",
                    json={"question": "build", "answer_format": "tech-effect"}).json()
    assert any("solution inside" in m["text"] for m in r["messages"])
    chips = [p["title"] for m in r["messages"] for p in (m.get("participants") or [])]
    assert any("unavailable" in c for c in chips)
    # plain chat (no tech-effect format) never invokes the checker
    monkeypatch.setattr(claude_bridge, "epc_sanity",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    r = client.post(f"/api/tabs/{tid}/chat", json={"question": "hi"}).json()
    assert any("solution inside" in m["text"] for m in r["messages"])


def test_house_style_roundtrip_and_injection(client, tmp_path, monkeypatch):
    """🖋 the ONE global formatting space: editable, persisted, and injected into
    EVERY answer path — chat prompts, the deep-compare reduce, and ⚖️ PSA runs."""
    monkeypatch.setattr(claude_bridge, "FMT_OVERRIDE_DIR", str(tmp_path / "fmt"))
    r = client.get("/api/house-style").json()
    assert r["overridden"] is False
    assert "HOUSE STYLE" in r["text"]
    assert "additional features" in r["text"].lower()
    # the problem-never-contains-the-solution rule (recurring bug 2026-08-03)
    assert "THE PROBLEM NEVER CONTAINS THE SOLUTION" in r["text"]
    # default reaches the chat prompt path
    assert "HOUSE STYLE (BINDING)" in claude_bridge.build_prompt("q")
    assert "THE PROBLEM NEVER CONTAINS THE SOLUTION" in claude_bridge.build_prompt("q")
    # edited version replaces the default everywhere; empty resets
    r = client.put("/api/house-style", json={"text": "MY GLOBAL STYLE RULES"}).json()
    assert r["overridden"] is True
    assert "MY GLOBAL STYLE RULES" in claude_bridge.build_prompt("q")
    r = client.put("/api/house-style", json={"text": ""}).json()
    assert r["overridden"] is False and "HOUSE STYLE" in r["text"]


def test_house_style_reaches_deep_reduce(tmp_path, monkeypatch):
    # deliberately NO `client` fixture: it stubs deep_reduce, which would make this
    # test assert against the stub instead of the real prompt assembly
    monkeypatch.setattr(claude_bridge, "FMT_OVERRIDE_DIR", str(tmp_path / "fmt"))
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    claude_bridge.deep_reduce("q", {"number": "EP1", "claims": "1. x"},
                              [{"number": "D1", "title": "t", "verdict": "MATCH SCORE: 5"}])
    assert "HOUSE STYLE (BINDING)" in captured["p"]


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


def test_upload_scanned_pdf_named_by_number_costs_no_tokens(client, monkeypatch):
    """The Espacenet 'ITMI20090714A1.pdf' case (2026-07-22): image-only PDF used to
    error out — now the filename resolves the number with ZERO model calls."""
    from types import SimpleNamespace
    from patentbench import extract
    monkeypatch.setattr(extract.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(extract, "numbers_from_image",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no OCR for a number-named scan")))
    tab = client.post("/api/tabs", json={"name": "Scan"}).json()
    r = client.post(f"/api/tabs/{tab['id']}/upload",
                    files=[("files", ("ITMI20090714A1.pdf", b"%PDF-1.4 image only",
                                      "application/pdf"))]).json()
    assert r["numbers"] == ["ITMI20090714A1"]
    assert not r.get("error")


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


def test_deep_read_aborts_on_dead_auth_token(client, monkeypatch):
    """A revoked OAuth token fails EVERY call identically — after one worker-window
    of pure auth errors the batch must ABORT, write nothing, and tell the user how
    to recover, instead of grinding through all N earning nothing while the UI sits
    on 'assessing 0/N' (bit 2026-07-28: 577 × 401 after a container recreate)."""
    tab = client.post("/api/tabs", json={"name": "Auth"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    nums = " ".join(f"CN11485{i:04d}" for i in range(40))
    client.post(f"/api/tabs/{tid}/documents", json={"text": nums})
    calls = []
    monkeypatch.setattr(
        claude_bridge, "deep_map",
        lambda bm, d, model=None, features=None: calls.append(d["number"]) or
        {"error": "Failed to authenticate. API Error: 401 OAuth access token has been revoked."})
    reduced = []
    monkeypatch.setattr(claude_bridge, "deep_reduce",
                        lambda *a, **k: reduced.append(1) or {"answer": "ranking"})
    client.post(f"/api/tabs/{tid}/deep-compare", json={})
    _wait_read(client, tid)
    assert len(calls) < 40                                  # aborted early, not ground through
    assert not reduced                                      # no ranking over nothing
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["score"] is None for d in docs)            # nothing written
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    note = [m for m in msgs if "ABORTED" in m]
    assert note and "AUTHENTICATION" in note[0] and "reseed" in note[0].lower()
    assert "Continue" in note[0]                            # recovery path named


def test_deep_compare_batch_reads_top_N_by_prior_score(client, monkeypatch):
    """Best match's batching: cap the run to the top-N still needing a read, MOST-PROMISING
    first (by prior score), so 50-at-a-time spends on the best candidates and reports how
    many remain for the next launch."""
    tab = client.post("/api/tabs", json={"name": "Batch"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    client.post(f"/api/tabs/{tid}/documents",
                json={"text": "EP3667902A1 CN114853847B CN114547092 CN117241689"})
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    # give distinct prior scores so ordering is unambiguous (all STALE → all need re-read)
    for num, sc in [("EP3667902A1", 9), ("CN114853847", 3), ("CN114547092", 7), ("CN117241689", 1)]:
        db.update_document(docs[num]["id"], score=sc, scored_at=1, score_model="claude-haiku-4-5")
    read = []
    monkeypatch.setattr(claude_bridge, "deep_map",
                        lambda bm, d, model=None, features=None: read.append(d["number"]) or {"verdict": f"MATCH SCORE: 6 for {d['number']}"})
    monkeypatch.setattr(claude_bridge, "deep_reduce", lambda *a, **k: {"answer": "ranking"})
    r = client.post(f"/api/tabs/{tid}/deep-compare", json={"batch": 2}).json()
    assert r["batch_size"] == 2 and r["remaining_after"] == 2
    _wait_read(client, tid)
    assert set(read) == {"EP3667902A1", "CN114547092"}   # the two HIGHEST prior scores (9, 7)
    # a second launch reads the next two
    read.clear()
    client.post(f"/api/tabs/{tid}/deep-compare", json={"batch": 2, "skip_scored": True})
    _wait_read(client, tid)
    assert set(read) == {"CN114853847", "CN117241689"}   # the remaining lower-scored pair


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
    assert debate_models[0] == "claude-opus-5"           # reconciliation runs on opus, not haiku
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


def test_digest_at_intake_is_opt_in(client, monkeypatch):
    """Adding numbers costs ZERO tokens by default: fetch is a plain scrape and the
    digest only runs when the 🧠 checkbox (digest: true) asked for it."""
    calls = []
    monkeypatch.setattr(claude_bridge, "digest_document",
                        lambda n, t, x, model=None: calls.append(n)
                        or {"digest": f"digest of {n}"})
    tab = client.post("/api/tabs", json={"name": "Dg"}).json()
    client.post(f"/api/tabs/{tab['id']}/documents", json={"text": "US10395648B1"})
    docs = client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]
    assert docs[0]["status"] == "fetched" and not docs[0]["digest_len"]
    assert calls == []                                   # ← no model call happened
    tab2 = client.post("/api/tabs", json={"name": "Dg2"}).json()
    client.post(f"/api/tabs/{tab2['id']}/documents",
                json={"text": "EP3667902A1", "digest": True})
    docs2 = client.get(f"/api/tabs/{tab2['id']}/documents").json()["documents"]
    assert docs2[0]["digest_len"] and calls == ["EP3667902A1"]
    full = client.get(f"/api/tabs/{tab2['id']}/documents/{docs2[0]['id']}").json()
    assert full["digest"] == "digest of EP3667902A1"


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
                json={"text": "US10395648B1", "reading_model": "claude-sonnet-4-6",
                      "digest": True})
    assert seen["digest"] == "claude-sonnet-4-6"
    client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP3667902A1"})
    client.post(f"/api/tabs/{tab['id']}/deep-compare",
                json={"reading_model": "claude-sonnet-4-6"})
    _wait_read(client, tab["id"])
    assert seen["map"] == "claude-sonnet-4-6"
    # invalid model name falls back to the cheap default (None -> DIGEST_MODEL)
    seen.clear()
    client.post(f"/api/tabs/{tab['id']}/documents",
                json={"text": "CN114547092", "reading_model": "gpt-9", "digest": True})
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


def test_build_prompt_focus_roster_budget_is_hard_cap():
    """The MIN_DOC_CHARS floor must NOT multiply past the roster budget: on a tab
    with 1000+ assessed candidates the roster block used to reach ~1.4M chars and
    every shrink-retry still overflowed the model window (bit 2026-07-28). Bodies
    go to the highest-scored candidates within budget; the rest stay listed
    header-only, and the block says so."""
    focus = [{"id": 0, "number": "CN111", "title": "f", "claims": "1. c",
              "description": "[0001] focused body"}]
    n = 1200
    others = [{"id": i + 1, "number": f"CN{i:07d}A", "title": f"t{i}",
               "verdict": f"ASSESSMENT-{i} " + "v" * 3000, "score": (i % 100) / 10}
              for i in range(n)]
    p = claude_bridge.build_prompt("q", focus=focus, documents=others)
    bodies = p.count("ASSESSMENT vs benchmark")
    assert bodies == claude_bridge.MAX_ROSTER_CHARS // claude_bridge.MIN_DOC_CHARS
    assert p.count("• CN") == n                       # every candidate still listed
    assert "did not fit this prompt" in p             # trimmed ones flagged in place
    assert "highest-scored candidates carry" in p     # …and the block-level note
    # top-scored keep their body, low-scored do not
    top = max(others, key=lambda d: d["score"])
    assert f"ASSESSMENT-{top['id'] - 1} " in p
    # the whole block stays within budget + headers (order of magnitude, not 1.4M)
    assert len(p) < claude_bridge.MAX_ROSTER_CHARS + n * 200 + 50_000
    # shrink-retry now genuinely shrinks the roster too
    p_small = claude_bridge.build_prompt("q", focus=focus, documents=others, scale=0.25)
    assert p_small.count("ASSESSMENT vs benchmark") < bodies


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
    # the real "Prompt is too long" case: a BIG roster of long verdicts PLUS skills and full
    # chat history all stacking on top — this is what overflowed sonnet's context.
    verdicts = [{"number": f"US{i}", "title": "t", "verdict": "X" * 8000}
                for i in range(298)]
    skills = [{"name": "patent-analyzer", "content": "S" * 20000},
              {"name": "patent-search-pipeline", "content": "S" * 6000}]
    history = [{"role": "user", "text": "H" * 4000} for _ in range(24)]
    claude_bridge.deep_reduce("rank them", {"text": "BENCH"}, verdicts,
                              skills=skills, history=history)
    assert captured["t"] == claude_bridge.REDUCE_TIMEOUT and captured["t"] >= 600
    # EVERY candidate still appears (full card or compact one-liner) — none silently dropped
    for i in range(298):
        assert f"US{i}" in captured["p"]
    # ...yet the WHOLE prompt (cards + skills + history + benchmark + instructions) stays
    # comfortably inside a 200k-token context (~800k chars); 700k leaves real headroom.
    assert len(captured["p"]) < 700_000


def test_deep_reduce_compact_tail_when_roster_exceeds_budget(monkeypatch):
    """A roster too big for full cards keeps the top ones full and demotes the rest to
    compact one-liners — so the prompt is bounded but nothing vanishes from the ranking."""
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, timeout=None: captured.update(p=prompt) or {"answer": "r"})
    # 400 long verdicts: too many to all be full cards within the budget → a real compact tail
    verdicts = [{"number": f"US{i}", "title": "t",
                 "verdict": f"MATCH SCORE: {i % 10}\nlong detail " + "X" * 6000} for i in range(400)]
    claude_bridge.deep_reduce("rank", {"text": "B"}, verdicts)
    p = captured["p"]
    assert "ADDITIONAL CANDIDATES (full verdict omitted" in p    # the compact section exists
    assert "X" * 1000 in p                                       # early candidates keep a full body
    assert all(f"US{i}" in p for i in range(400))               # all 400 still ranked
    assert len(p) < 700_000                                      # ...and the prompt is bounded


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


def test_benchmark_add_feature_annotates_document_benchmark(client):
    """Features on a DOCUMENT benchmark rank candidates against it, so adding one must
    KEEP the document rather than replace it with a feature spec."""
    tab = client.post("/api/tabs", json={"name": "AddFeat3"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    assert client.post(f"/api/tabs/{tid}/benchmark/features/add",
                       json={"name": "x", "weight": 1}).status_code == 200
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert bm["source"] == "number" and bm["number"] == "US10395648B1"   # document kept
    assert [f["name"] for f in bm["features"]] == ["x"]


def test_written_features_survive_adding_a_document_benchmark(client):
    """THE regression: features written FIRST, document benchmark added AFTER. The
    features are the user's own input — what a match must disclose — so replacing the
    benchmark with a document must never silently delete them."""
    tab = client.post("/api/tabs", json={"name": "KeepFeat"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features/add",
                json={"name": "a stacked battery pack", "weight": 5})
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert bm["source"] == "number" and bm["number"] == "US10395648B1"
    assert [f["name"] for f in bm["features"]] == ["a stacked battery pack"]
    assert bm["features"][0]["weight"] == 5


def test_editing_features_of_a_document_benchmark_keeps_the_document(client):
    """Re-weighting via the feature editor (POST /benchmark/features) annotates a
    document benchmark instead of swapping the fetched document out for a spec."""
    tab = client.post("/api/tabs", json={"name": "EditFeat"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "US10395648B1"})
    r = client.post(f"/api/tabs/{tid}/benchmark/features",
                    json={"features": [{"name": "a busbar", "weight": 3}]})
    assert r.status_code == 200
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert bm["source"] == "number" and bm["number"] == "US10395648B1"
    assert bm["features"][0]["weight"] == 3


def test_clearing_the_benchmark_drops_features_too(client):
    """The explicit 'remove the benchmark' action is the ONE place features do go."""
    tab = client.post("/api/tabs", json={"name": "ClrFeat"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features/add",
                json={"name": "a busbar", "weight": 2})
    client.delete(f"/api/tabs/{tid}/benchmark")
    assert client.get(f"/api/tabs/{tid}/state").json()["benchmark"] is None


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
                        lambda bm, pairs, model=None, mode="must": {"results": {"1": {"combinable": True,
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


def test_combi_motivation_batches_all_pairs_past_single_call_cap(client, monkeypatch):
    """One click judges EVERY partner even past a single call's size (set-cover can surface
    many) — batched, per-chunk indexed. A blank matrix row must mean 'not yet judged', never
    'not combinable', so no partner is silently left unjudged."""
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "CombiBatch"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "x", "weight": 1}], "title": "x"})
    anchor = _feature_doc(db, tid, "US9000000A",
                          [{"name": "x", "weight": 1, "status": "yes", "note": "a"}])
    n = 25                                        # > the per-call batch (10) → several batches
    ids = [_feature_doc(db, tid, f"US80000{i:02d}A",
                        [{"name": "x", "weight": 1, "status": "no", "note": ""}]) for i in range(n)]
    # Fake judges every pair in whatever chunk it receives, 1-based WITHIN the chunk.
    monkeypatch.setattr(claude_bridge, "combi_motivation",
        lambda bm, pairs, model=None, mode="must": {"results": {str(i + 1): {"combinable": True, "reason": "ok"}
                                                   for i in range(len(pairs))}, "model": "m"})
    r = client.post(f"/api/tabs/{tid}/combi/motivation",
                    json={"pairs": [{"a_id": anchor, "b_id": b, "a_features": ["x"],
                                     "b_features": []} for b in ids]}).json()
    assert r["ok"] and len(r["results"]) == n     # ALL 25 judged, none dropped by a 12-cap


def test_combi_motivation_additional_mode_uses_anchor_framing(client, monkeypatch):
    """In additional mode the prompt must NOT claim 'A and B together cover the mandatory
    features' (the anchor covers them alone) — it must frame B as adding OPTIONAL features to
    the anchor A. Otherwise the judge says NO to everything (its false premise). We assert the
    mode reaches the bridge and drives the prompt head."""
    from patentbench import claude_bridge as cb
    seen = {}
    orig = cb.COMBI_MOTIVATION_PROMPT

    def fake(bm, pairs, model=None, mode="must"):
        seen["mode"] = mode
        return {"results": {"1": {"combinable": False, "reason": "different field"}}, "model": "m"}
    monkeypatch.setattr(cb, "combi_motivation", fake)
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "AddMode"}).json(); tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "x", "weight": 1}], "title": "x"})
    a = _feature_doc(db, tid, "US7000000A", [{"name": "x", "weight": 1, "status": "yes", "note": "a"}])
    b = _feature_doc(db, tid, "US7000001A", [{"name": "x", "weight": 1, "status": "no", "note": ""}])
    client.post(f"/api/tabs/{tid}/combi/motivation",
                json={"mode": "additional",
                      "pairs": [{"a_id": a, "b_id": b, "a_features": ["x"], "b_features": ["y"]}]})
    assert seen["mode"] == "additional"
    # the two heads are genuinely different framings
    assert cb._COMBI_MOTIV_HEAD["additional"] != cb._COMBI_MOTIV_HEAD["must"]
    assert "already" in cb._COMBI_MOTIV_HEAD["additional"].lower()


def test_deep_read_auto_judges_combinability(client, monkeypatch):
    """After a batch deep read the 🧩 motivation judge runs by itself on the matrix's
    anchor+partner pairs — and a second read never re-bills the stored verdicts."""
    judged_batches = []
    monkeypatch.setattr(claude_bridge, "combi_motivation",
                        lambda bm, pairs, model=None, mode="must":
                        judged_batches.append(list(pairs)) or
                        {"results": {str(i): {"combinable": True, "reason": "same field"}
                                     for i in range(1, len(pairs) + 1)},
                         "model": "m"})
    # deep read yields per-element coverage: US1111111 covers f1 only (anchor),
    # US2222222 covers f2 only (the gap-filling partner)
    def fake_map(bm_text, d, model=None, features=None):
        if d["number"] == "US1111111":
            return {"verdict": "MATCH SCORE: 8\nFEATURE 1: YES — a\nFEATURE 2: NO"}
        return {"verdict": "MATCH SCORE: 4\nFEATURE 1: NO\nFEATURE 2: YES — b"}
    monkeypatch.setattr(claude_bridge, "deep_map", fake_map)
    tab = client.post("/api/tabs", json={"name": "AutoCombi"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "f1", "weight": 3}, {"name": "f2", "weight": 2}],
                      "title": "f1+f2"})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "US1111111 US2222222"})
    assert client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()["started"]
    _wait_read(client, tid)
    st = client.get(f"/api/tabs/{tid}/state").json()
    assert len(st["combi_motivations"]) == 1                 # the anchor+partner pair
    assert next(iter(st["combi_motivations"].values()))["combinable"] is True
    assert any(m["role"] == "s" and "Combinability auto-judged" in m["text"]
               for m in st["messages"])
    # second read: coverage unchanged → the pair is already judged → NO new billing
    n_before = len(judged_batches)
    assert client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()["started"]
    _wait_read(client, tid)
    assert len(judged_batches) == n_before


def test_deep_read_reduce_carries_app_rank_and_alignment_rule(client, monkeypatch):
    """The reduce model receives the app's OWN order (APP RANK n/total on every card,
    same unified key as the matrix) plus the mandatory-alignment rule, so the chat
    ranking can no longer silently contradict the coverage matrix."""
    seen = {}
    monkeypatch.setattr(claude_bridge, "deep_reduce",
                        lambda q, bm, verdicts, skills=None, model=None, history=None,
                        rank_rule=None: seen.update(v=verdicts, rule=rank_rule)
                        or {"answer": "ranked"})
    def fake_map(bm_text, d, model=None, features=None):
        if d["number"] == "US1111111":
            return {"verdict": "MATCH SCORE: 2\nFEATURE 1: YES — a\nFEATURE 2: PARTIAL — b"}
        return {"verdict": "MATCH SCORE: 9\nFEATURE 1: NO\nFEATURE 2: NO"}
    monkeypatch.setattr(claude_bridge, "deep_map", fake_map)
    tab = client.post("/api/tabs", json={"name": "Rank"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "f1", "weight": 3}, {"name": "f2", "weight": 2}],
                      "title": "f"})
    client.post(f"/api/tabs/{tid}/documents", json={"text": "US1111111 US2222222"})
    assert client.post(f"/api/tabs/{tid}/deep-compare", json={}).json()["started"]
    _wait_read(client, tid)
    # coverage dominates the holistic score: US1111111 (2/10 but covers elements)
    # must be APP RANK 1, US9-scored-but-zero-coverage US2222222 rank 2
    cov = {v["number"]: v["coverage"] for v in seen["v"]}
    assert cov["US1111111"].startswith("APP RANK 1/2")
    assert cov["US2222222"].startswith("APP RANK 2/2")
    assert "MUST 1✓+1~/2" in cov["US1111111"]
    assert "DEVIATION from app rank" in seen["rule"]
    assert "measures something DIFFERENT" in seen["rule"]


def test_combi_auto_judge_endpoint_judges_current_matrix_pairs(client, monkeypatch):
    """POST /combi/auto-judge fills verdicts for the CURRENT matrix pairs on demand
    (for matrices whose partner set shifted after the last automatic run)."""
    monkeypatch.setattr(claude_bridge, "combi_motivation",
                        lambda bm, pairs, model=None, mode="must":
                        {"results": {str(i): {"combinable": True, "reason": "fits"}
                                     for i in range(1, len(pairs) + 1)}, "model": "m"})
    import json as _j
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "AJ"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "f1", "weight": 3}, {"name": "f2", "weight": 2}],
                      "title": "f"})
    a = _feature_doc(db, tid, "US1111111",
                     [{"name": "f1", "weight": 3, "status": "yes", "note": ""},
                      {"name": "f2", "weight": 2, "status": "no", "note": ""}])
    b = _feature_doc(db, tid, "US2222222",
                     [{"name": "f1", "weight": 3, "status": "no", "note": ""},
                      {"name": "f2", "weight": 2, "status": "yes", "note": ""}])
    r = client.post(f"/api/tabs/{tid}/combi/auto-judge").json()
    assert r["ok"] and r["judged"] == 1
    lo, hi = sorted((a, b))
    assert client.get(f"/api/tabs/{tid}/state").json()["combi_motivations"][f"{lo}-{hi}"]["combinable"]
    # idempotent: everything already judged → zero new calls
    assert client.post(f"/api/tabs/{tid}/combi/auto-judge").json()["judged"] == 0
    # no features → 400
    t2 = client.post("/api/tabs", json={"name": "AJ2"}).json()
    assert client.post(f"/api/tabs/{t2['id']}/combi/auto-judge").status_code == 400


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


def _add_read_tab(client, n_docs, batch=None, monkeypatch=None):
    """A tab with one A-feature and `n_docs` fetched candidates that all have a digest,
    scored DESCENDING (doc i scores 10-i) so the low ones fall outside any top-N."""
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": f"AddAll{n_docs}"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "core divider", "weight": 5, "kind": "M", "sl": 5},
        {"name": "detachable pack", "weight": 4, "kind": "A", "sl": 7},
    ]})
    nums = [f"EP430000{i}" for i in range(n_docs)]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for i, d in enumerate(client.get(f"/api/tabs/{tid}/documents").json()["documents"]):
        _db.update_document(d["id"], digest=f"digest of {d['number']}",
                            score=max(1, 10 - i), scored_at=1, score_model="opus")
    if batch is not None:
        monkeypatch.setattr(api, "BULK_DIGEST_BATCH", batch)
    return tid, nums


def test_additional_read_all_docs_covers_every_candidate(client, monkeypatch):
    """➕ additional read (ALL): every candidate with a digest is assessed — including the
    low-scored ones the top-N never reaches, which is the whole point (an A-feature bonus
    can only lift a document that was actually looked at)."""
    from patentbench import claude_bridge as cb
    tid, nums = _add_read_tab(client, 12)
    seen = []
    def fake_add(a_features, docs, model=None):
        seen.extend(d["number"] for d in docs)
        return {"results": {d["number"]: [{"name": "detachable pack", "weight": 4, "sl": 7,
                                           "status": "present", "evidence": "e"}] for d in docs},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "additional_read", fake_add)

    # default scope: only the top-N by score — the tail is untouched
    r = client.post(f"/api/tabs/{tid}/additional-read", json={"top_n": 3}).json()
    assert r["assessed"] == 3 and len(seen) == 3

    seen.clear()
    r = client.post(f"/api/tabs/{tid}/additional-read", json={"all_docs": True}).json()
    assert r["ok"] and r["assessed"] == 12 and r["requested"] == 12
    assert sorted(seen) == sorted(nums)                      # EVERY candidate, none skipped
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["additional_scores"] for d in docs)         # ...and every one persisted


def test_additional_read_all_docs_batches_the_bulk_calls(client, monkeypatch):
    """'All documents' must not become one giant prompt: the scope is split into batches,
    so hundreds of digests stay inside the budget."""
    from patentbench import claude_bridge as cb
    tid, nums = _add_read_tab(client, 7, batch=2, monkeypatch=monkeypatch)
    sizes = []
    def fake_add(a_features, docs, model=None):
        sizes.append(len(docs))
        return {"results": {d["number"]: [{"name": "detachable pack", "weight": 4, "sl": 7,
                                           "status": "absent", "evidence": ""}] for d in docs},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "additional_read", fake_add)
    r = client.post(f"/api/tabs/{tid}/additional-read", json={"all_docs": True}).json()
    assert r["assessed"] == 7 and r["batches"] == 4          # 7 docs / batch 2 → 4 passes
    assert max(sizes) <= 2 and sum(sizes) == 7               # none oversized, none dropped


def test_additional_read_partial_batch_failure_keeps_the_rest(client, monkeypatch):
    """A failed batch must lose only its own documents — and the run must SAY so rather
    than reporting a partial pass as full coverage."""
    from patentbench import claude_bridge as cb
    tid, nums = _add_read_tab(client, 6, batch=2, monkeypatch=monkeypatch)
    calls = {"n": 0}
    lock = __import__("threading").Lock()
    def fake_add(a_features, docs, model=None):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            return {"error": "session limit"}
        return {"results": {d["number"]: [{"name": "detachable pack", "weight": 4, "sl": 7,
                                           "status": "present", "evidence": "e"}] for d in docs},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "additional_read", fake_add)
    r = client.post(f"/api/tabs/{tid}/additional-read", json={"all_docs": True}).json()
    assert r["assessed"] == 4 and r["requested"] == 6 and r["failed_batches"] == 1
    msg = client.get(f"/api/tabs/{tid}/state").json()["messages"][-1]["text"]
    assert "2 candidate(s) not assessed" in msg               # the gap is stated, not hidden


def _combi_tab(client, monkeypatch, elements=("packs", "bus", "comms")):
    """A tab whose benchmark has `elements` as mandatory features, and 3 digested docs."""
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": "Combi"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": e, "weight": 5, "kind": "M", "sl": 5} for e in elements]})
    nums = ["EP4400001", "EP4400002", "EP4400003"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        _db.update_document(d["id"], digest=f"digest of {d['number']}", score=5, scored_at=1)
    return tid, nums


def test_parse_coverage_maps_elements_and_defaults_silence_to_no():
    from patentbench import claude_bridge as cb
    els = [{"name": "packs", "weight": 5}, {"name": "bus", "weight": 3},
           {"name": "comms", "weight": 1}]
    out = cb.parse_coverage("=== EP4400001 ===\n1: YES — [0021] a plurality of packs\n"
                            "2: PARTIAL — implicit rail\n", els)
    got = out["EP4400001"]
    assert [c["status"] for c in got] == ["yes", "partial", "no"]   # unanswered → no
    assert got[0]["evidence"].startswith("[0021]")


def test_combi_scan_finds_the_pair_that_covers_everything(client, monkeypatch):
    """The TOOL finds the combination — no user-picked D1/D2. A and B each hold what the
    other lacks, so together they cover all three elements."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    def fake_cov(elements, docs, model=None):
        table = {   # A: packs+bus  B: comms  C: packs only (subsumed by A)
            "EP4400001": ["YES", "YES", "NO"],
            "EP4400002": ["NO", "NO", "YES"],
            "EP4400003": ["YES", "NO", "NO"],
        }
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i].lower(), "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert r["scanned"] == 3 and r["elements"] == 3
    complete = [p for p in r["pairs"] if p["complete"]]
    assert len(complete) == 1
    p = complete[0]
    assert {p["a"], p["b"]} == {"EP4400001", "EP4400002"}   # the only genuine full cover
    assert p["rating"] == 10.0 and p["depth"] == "digest"
    # C+B covers only packs+comms (no bus) → present but NOT complete
    assert any({q["a"], q["b"]} == {"EP4400003", "EP4400002"} and not q["complete"]
               for q in r["pairs"])


def test_unified_score_must_dominates_and_wholedoc_is_bonus():
    """The single ranking key: Must (M) coverage decides the tier; Additional (A) and
    Whole-document (W) are separate capped bonus pools that only differentiate WITHIN a
    Must tier — never lift a weaker-on-Must document above a stronger one."""
    from patentbench.web import api
    els = [{"name": "M1", "weight": 5, "kind": "M"}, {"name": "M2", "weight": 5, "kind": "M"},
           {"name": "A1", "weight": 5, "kind": "A"}, {"name": "W1", "weight": 5, "kind": "W"}]

    def doc(cov):
        return {"combi_coverage": __import__("json").dumps(
            [{"name": k, "status": v, "depth": "digest"} for k, v in cov.items()])}

    full = api._unified_score(els, doc({"M1": "yes", "M2": "yes"}))           # all Must, no bonus
    gap_big_bonus = api._unified_score(els, doc(                              # 1 Must + both bonuses
        {"M1": "yes", "M2": "no", "A1": "yes", "W1": "yes"}))
    assert full["covers_all"] and not gap_big_bonus["covers_all"]
    # Must dominates: the full coverer outranks the gap doc even though the gap doc has more bonus.
    assert full["key"] > gap_big_bonus["key"]
    # W is a real, separate bonus pool.
    assert gap_big_bonus["w_bonus"] > 0 and gap_big_bonus["add_bonus"] > 0
    # Whole-document elements are NOT counted as mandatory.
    assert full["mand_total"] == 2

    # Reuses the deep-read's feature_scores when combi_coverage is absent (no new tokens).
    fs_only = {"feature_scores": [{"name": "M1", "status": "yes"}, {"name": "M2", "status": "partial"}]}
    u = api._unified_score(els, fs_only)
    assert u["assessed"] and u["mand_full"] == 1 and u["mand_partial"] == 1


def test_combi_scan_returns_the_element_document_matrix(client, monkeypatch):
    """The 🔎 scan (and combi-results) return a MANDATORY element × document grid — the raw
    material the UI renders instead of the pair/solo lists. Columns are the mandatory
    elements; rows carry per-element cells plus the three standalone scores, full coverers
    first."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    def fake_cov(elements, docs, model=None):
        table = {   # A covers everything alone; B partial on one; C has a real gap
            "EP4400001": ["YES", "YES", "YES"],
            "EP4400002": ["YES", "PARTIAL", "YES"],
            "EP4400003": ["YES", "NO", "YES"],
        }
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i].lower(), "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    mx = r["matrix"]
    assert [c["name"] for c in mx["columns"]] == ["packs", "bus", "comms"]   # mandatory → columns
    rows = mx["rows"]
    assert len(rows) == 3
    # Full coverers rank first; only EP4400001/EP4400002 have no 'no', EP4400003 has a gap.
    assert rows[0]["number"] == "EP4400001" and rows[0]["covers_all"] is True
    assert rows[0]["cells"] == ["yes", "yes", "yes"]
    assert rows[-1]["number"] == "EP4400003" and rows[-1]["covers_all"] is False
    # Each row carries the three standalone scores.
    for row in rows:
        assert set(("mand_full", "mand_partial", "mand_total", "mand_rating",
                    "add_bonus", "score", "depth")) <= set(row)


def test_drop_benchmark_excludes_the_benchmark_document():
    """A candidate that IS the benchmark (any number formatting) is removed — it would match
    itself 11/11 and always be the top 'coverer', which is meaningless."""
    from patentbench.web import api
    docs = [{"number": "US-1234567-B2"}, {"number": "us 1234567 b2"}, {"number": "EP9999999"}]
    kept = api._drop_benchmark(docs, {"number": "US1234567B2"})
    assert [d["number"] for d in kept] == ["EP9999999"]


def test_effective_coverage_surfaces_full_text_conflict():
    """When the two FULL-TEXT passes disagree on an element — the deep-read (feature_scores)
    vs the combi stage-2 verify (combi_coverage depth=full) — the higher-fidelity verdict is
    shown, but the conflict is FLAGGED with the losing verdict, not silently hidden. A digest
    verdict differing from a full read is NOT a conflict (just lower fidelity)."""
    from patentbench.web import api
    import json
    doc = {
        "feature_scores": [{"name": "M1", "status": "partial"},   # deep-read: partial
                           {"name": "M2", "status": "yes"}],
        "combi_coverage": json.dumps([
            {"name": "M1", "status": "yes", "depth": "full"},      # combi verify: yes → CONFLICT
            {"name": "M2", "status": "no", "depth": "digest"}]),   # digest < full → NOT a conflict
    }
    eff = api._effective_coverage(doc)
    assert eff["M1"]["status"] == "yes" and eff["M1"]["conflict"] is True and eff["M1"]["alt"] == "partial"
    assert eff["M2"]["status"] == "yes" and eff["M2"]["conflict"] is False   # deep-read wins, no conflict
    u = api._unified_score([{"name": "M1", "weight": 5, "kind": "M"},
                            {"name": "M2", "weight": 5, "kind": "M"}], doc)
    assert u["mand_conflicts"] == 1


def test_combi_matrix_pivots_to_additional_when_must_is_saturated():
    """When the best document already covers every Must element and additional features exist,
    the grid switches its columns to the ADDITIONAL features and the partners become documents
    that bring the additional features the anchor lacks — even ones that don't cover Must (they
    combine with the anchor, which does)."""
    from patentbench.web import api
    import json
    els = [{"name": "M1", "weight": 5, "kind": "M"}, {"name": "M2", "weight": 5, "kind": "M"},
           {"name": "A1", "weight": 5, "kind": "A"}, {"name": "A2", "weight": 5, "kind": "A"}]

    def doc(i, num, cov):
        return {"id": i, "number": num, "status": "fetched",
                "combi_coverage": json.dumps([{"name": k, "status": v, "depth": "digest"}
                                              for k, v in cov.items()])}
    docs = [
        doc(1, "AAA", {"M1": "yes", "M2": "yes", "A1": "yes", "A2": "no"}),   # all Must, missing A2
        doc(2, "BBB", {"M1": "yes", "M2": "yes", "A1": "no", "A2": "no"}),    # all Must, no additional
        doc(3, "CCC", {"M1": "no", "M2": "no", "A1": "no", "A2": "yes"}),     # NO Must, brings A2
    ]
    mx = api._combi_matrix(els, docs)
    assert mx["mode"] == "additional"                       # Must saturated → pivot
    assert [c["name"] for c in mx["columns"]] == ["A1", "A2"]
    assert mx["anchor"] == "AAA" and mx["gap_names"] == ["A2"]
    nums = [r["number"] for r in mx["rows"]]
    assert nums[0] == "AAA"
    assert "CCC" in nums                                    # non-Must partner that brings A2
    assert "BBB" not in nums                                # brings no additional the anchor lacks
    ccc = next(r for r in mx["rows"] if r["number"] == "CCC")
    assert ccc["fills"] == ["A2"] and ccc["covers_all"] is False


def test_focus_combination_set_covers_every_coverable_gap_and_flags_absent():
    """The matrix guarantees COVERAGE, not a flat top-N: every gap the anchor has that SOME
    document fills is represented by a shown row (even if that needs more than the min rows),
    and a gap NO document covers is reported in uncovered_gaps — genuinely absent, not hidden."""
    from patentbench.web import api
    cols = [{"name": "G1", "weight": 1}, {"name": "G2", "weight": 1},
            {"name": "G3", "weight": 1}, {"name": "G4", "weight": 1}]

    def row(num, cells, key):
        return {"id": num, "number": num, "cells": cells, "key": key, "covers_all": False}
    # Anchor covers nothing; each partner fills a DIFFERENT single gap; G4 filled by nobody.
    rows = [
        row("ANCHOR", ["no", "no", "no", "no"], 100),
        row("P1", ["yes", "no", "no", "no"], 90),
        row("P2", ["no", "yes", "no", "no"], 80),
        row("P3", ["no", "no", "yes", "no"], 70),
    ]
    out = api._focus_combination(cols, rows, limit=2)     # min rows 2, but coverage needs more
    shown = [r["number"] for r in out["rows"]]
    assert shown[0] == "ANCHOR"
    # All THREE coverable gaps get a representative even though limit=2 — coverage beats the cap.
    for p in ("P1", "P2", "P3"):
        assert p in shown
    assert out["uncovered_gaps"] == ["G4"]                # nobody covers G4 → flagged absent


def test_combi_matrix_focuses_on_anchor_plus_gap_fillers(client, monkeypatch):
    """The matrix is a 2-document combination finder: row ① is the best document (anchor);
    the rows below are ONLY documents that fill a Must element the anchor lacks — the closest
    partners for a pair. A document that brings nothing the anchor misses is dropped."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)          # 3 mandatory: packs, bus, comms
    def fake_cov(elements, docs, model=None):
        table = {   # A = best (has packs+bus, MISSING comms); B fills comms; C brings nothing new
            "EP4400001": ["YES", "YES", "NO"],
            "EP4400002": ["NO", "NO", "YES"],
            "EP4400003": ["YES", "NO", "NO"],
        }
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i].lower(), "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    mx = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()["matrix"]
    assert mx["anchor"] == "EP4400001" and mx["covers_all_anchor"] is False
    assert mx["gap_names"] == ["comms"]                  # the element the anchor lacks
    nums_shown = [r["number"] for r in mx["rows"]]
    assert nums_shown[0] == "EP4400001"                  # row ① = the anchor
    assert "EP4400002" in nums_shown                     # the gap-filler is shown
    assert "EP4400003" not in nums_shown                 # brings nothing new → dropped
    filler = next(r for r in mx["rows"] if r["number"] == "EP4400002")
    assert filler["fills"] == ["comms"]


def test_combi_scan_rejects_one_document_subsuming_another(client, monkeypatch):
    """A pair is only a COMBINATION when each side contributes something the other lacks —
    A ⊇ C is one document doing the work, not a combination."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    def fake_cov(elements, docs, model=None):
        table = {"EP4400001": ["YES", "YES", "YES"],    # covers everything alone
                 "EP4400002": ["YES", "NO", "NO"],      # ⊂ A
                 "EP4400003": ["YES", "YES", "NO"]}     # ⊂ A
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i].lower(), "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert r["pairs"] == []          # every pair is subsumption, none is a combination


def test_combi_scan_needs_at_least_two_elements(client, monkeypatch):
    """THE tab-36 case: one monolithic feature can never be split between two documents,
    so the analysis must say so loudly instead of silently returning nothing."""
    tid, nums = _combi_tab(client, monkeypatch, elements=("one monolithic claim",))
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={})
    assert r.status_code == 400
    assert "at least TWO mandatory elements" in r.json()["detail"]
    assert "Decompose" in r.json()["detail"]           # points at the fix


def test_combi_scoring_is_independent_of_every_other_score(client, monkeypatch):
    """The combi rating must not read from, or write to, score / feature_scores /
    additional_scores — it is its own investigation."""
    import patentbench.db as _db
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    before = {d["number"]: (d["score"], d["feature_scores"], d["additional_scores"])
              for d in docs}
    def fake_cov(elements, docs_, model=None):
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"], "status": "yes", "evidence": "e"}
            for e in elements] for d in docs_}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    client.post(f"/api/tabs/{tid}/combi-scan", json={})
    after = {d["number"]: (d["score"], d["feature_scores"], d["additional_scores"])
             for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert after == before                              # untouched
    assert _db.get_document(docs[0]["id"])["combi_coverage"]      # ...stored separately


def test_combi_results_rehydrate_from_stored_coverage(client, monkeypatch):
    """The findings survive a reload: GET /combi-results re-derives pairs+solo from stored
    coverage (no model call), so the panel isn't lost when the in-memory scan state is."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    # nothing scanned yet → no results
    r0 = client.get(f"/api/tabs/{tid}/combi-results").json()
    assert r0["has_results"] is False and r0["pairs"] == [] and r0["solo"] == []
    # run a scan, then fetch results WITHOUT re-running anything
    monkeypatch.setattr(cb, "combi_coverage_digests", lambda elements, docs, model=None: {
        "results": {d["number"]: [{"name": e["name"], "weight": e["weight"],
                                   "status": "yes", "evidence": "e"} for e in elements]
                    for d in docs}, "model": "m"})
    client.post(f"/api/tabs/{tid}/combi-scan", json={})
    r = client.get(f"/api/tabs/{tid}/combi-results").json()
    assert r["has_results"] is True and r["assessed"] == 3
    assert len(r["solo"]) == 3        # all three cover everything → all solo hits
    # a screen-only tab must NOT rehydrate findings (generous guess, not a verdict)
    tid2, _ = _combi_tab(client, monkeypatch)
    monkeypatch.setattr(cb, "combi_fast_screen", lambda features, docs, model=None: {
        "results": {d["number"]: [0, 1, 2] for d in docs}, "model": "haiku"})
    client.post(f"/api/tabs/{tid2}/combi-screen", json={"top_n": 3})
    r2 = client.get(f"/api/tabs/{tid2}/combi-results").json()
    assert r2["has_results"] is False   # screen-only → nothing rigorous to show


def test_combi_surfaces_documents_that_cover_everything_alone(client, monkeypatch):
    """A document disclosing the WHOLE invention is novelty-grade — strictly stronger than
    any combination. _combi_pairs drops subsumed pairs, so without the solo list the
    strongest finding would vanish from the results entirely."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    def fake_cov(elements, docs, model=None):
        table = {"EP4400001": ["yes", "yes", "yes"],   # covers everything ALONE
                 "EP4400002": ["yes", "no", "no"],
                 "EP4400003": ["no", "yes", "no"]}
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i], "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert [s["number"] for s in r["solo"]] == ["EP4400001"]
    assert r["solo"][0]["mand_total"] == 3
    # EP4400002+EP4400003 is still only a partial pair; the solo hit is the real answer
    assert not any(p["complete"] for p in r["pairs"])


def test_combi_solo_counts_partial_as_covering(client, monkeypatch):
    """The CN111181207 case: a document strong on most limbs but PARTIAL (implicit/pack-level)
    on a couple still anticipates — a partial can meet a limitation. It must appear as a solo
    coverer, flagged, not vanish because it wasn't literal-YES on every element. Only a real
    'no' disqualifies. Literal coverers rank above stretched ones."""
    from patentbench import claude_bridge as cb
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": "SoloP"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "preamble", "weight": 1, "kind": "M", "sl": 5},
        {"name": "packs", "weight": 5, "kind": "M", "sl": 5},
        {"name": "cell-voltage", "weight": 4, "kind": "M", "sl": 5},
    ]})
    nums = ["CN102122826", "CN111181207", "EPweak000"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        _db.update_document(d["id"], digest="d", score=5, scored_at=1)
    def fake_cov(elements, docs, model=None):
        table = {  #             preamble  packs   cell-voltage
            "CN102122826": ["yes", "yes", "yes"],       # literal on all 3
            "CN111181207": ["partial", "yes", "partial"],  # covers all, 2 partial (the real case)
            "EPweak000":   ["yes", "yes", "no"],        # a genuine gap → NOT a solo coverer
        }
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i], "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    solo = {s["number"]: s for s in r["solo"]}
    assert set(solo) == {"CN102122826", "CN111181207"}      # the partial one IS included
    assert "EPweak000" not in solo                          # the one with a real 'no' is not
    assert [s["number"] for s in r["solo"]] == ["CN102122826", "CN111181207"]  # literal first
    p = solo["CN111181207"]
    assert p["mand_full"] == 1 and p["mand_partial"] == 2
    assert sorted(p["partial_names"]) == ["cell-voltage", "preamble"]   # named to argue


def test_combi_pair_counts_partial_toward_covers_all(client, monkeypatch):
    """Pairs use the same standard: the union covering an element at YES *or* PARTIAL makes
    the pair complete, and the rating (partial=half) keeps a stretched cover below a literal
    one so they never read as equal."""
    from patentbench import claude_bridge as cb
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": "PairP"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "packs", "weight": 5, "kind": "M", "sl": 5},
        {"name": "bus", "weight": 5, "kind": "M", "sl": 5},
    ]})
    nums = ["EP4400001", "EP4400002"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        _db.update_document(d["id"], digest="d", score=5, scored_at=1)
    def fake_cov(elements, docs, model=None):
        table = {"EP4400001": ["yes", "no"],       # packs solid, no bus
                 "EP4400002": ["no", "partial"]}   # bus only partial
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i], "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert len(r["pairs"]) == 1
    p = r["pairs"][0]
    assert p["complete"] is True                    # union covers both (one via partial)
    assert p["mand_full"] == 1 and p["mand_partial"] == 1
    assert p["rating"] == 7.5                        # 5 + 5*0.5 = 7.5 of 10 — below a literal 10


def test_combi_solo_ranked_by_additional_coverage(client, monkeypatch):
    """Once several documents each cover the mandatory set, the ADDITIONAL elements are
    what separate them — that is the whole point of splitting them."""
    from patentbench import claude_bridge as cb
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": "Solo"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "packs", "weight": 5, "kind": "M", "sl": 5},
        {"name": "bus", "weight": 5, "kind": "M", "sl": 5},
        {"name": "a1", "weight": 5, "kind": "A", "sl": 10},
        {"name": "a2", "weight": 5, "kind": "A", "sl": 10},
    ]})
    nums = ["EP4400001", "EP4400002"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        _db.update_document(d["id"], digest="d", score=5, scored_at=1)
    def fake_cov(elements, docs, model=None):
        #                          packs  bus    a1     a2
        table = {"EP4400001": ["yes", "yes", "no", "no"],     # covers M alone, no additional
                 "EP4400002": ["yes", "yes", "yes", "yes"]}   # covers M alone AND both A
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i], "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert [s["number"] for s in r["solo"]] == ["EP4400002", "EP4400001"]   # A decides
    assert r["solo"][0]["add_cov"] == 2 and r["solo"][1]["add_cov"] == 0


def test_decompose_splits_the_additional_feature_too(client, monkeypatch):
    """The additional feature is monolithic for the same reason the claim was — split it,
    and its elements inherit ITS stretch level."""
    from patentbench import claude_bridge as cb
    calls = []
    def fake_dec(text, model=None, claims=False):
        calls.append(text[:20])
        if text.startswith("LOCKOUT"):
            return {"elements": [{"name": "wave-by-wave limiting", "weight": 4, "kind": "M", "sl": 5},
                                 {"name": "threshold forced to zero", "weight": 3, "kind": "M", "sl": 5}],
                    "model": "m"}
        return {"elements": [{"name": "packs", "weight": 5, "kind": "M", "sl": 5}], "model": "m"}
    monkeypatch.setattr(cb, "decompose_claim", fake_dec)
    tab = client.post("/api/tabs", json={"name": "DecA"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "a monolithic claim", "weight": 5, "kind": "M", "sl": 5},
        {"name": "LOCKOUT mechanism, one big block", "weight": 3, "kind": "A", "sl": 10},
    ]})
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "features"}).json()
    assert r["mandatory"] == 1 and r["additional"] == 2
    a_els = [e for e in r["elements"] if e["kind"] == "A"]
    assert [e["name"] for e in a_els] == ["wave-by-wave limiting", "threshold forced to zero"]
    assert all(e["sl"] == 10 for e in a_els)          # inherits the A feature's stretch level


def test_decompose_keeps_an_additional_feature_whose_split_failed(client, monkeypatch):
    """A failed A split must leave that feature WHOLE, never drop it silently."""
    from patentbench import claude_bridge as cb
    def fake_dec(text, model=None, claims=False):
        if text.startswith("LOCKOUT"):
            return {"error": "session limit"}
        return {"elements": [{"name": "packs", "weight": 5, "kind": "M", "sl": 5}], "model": "m"}
    monkeypatch.setattr(cb, "decompose_claim", fake_dec)
    tab = client.post("/api/tabs", json={"name": "DecA2"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "a monolithic claim", "weight": 5, "kind": "M", "sl": 5},
        {"name": "LOCKOUT mechanism", "weight": 3, "kind": "A", "sl": 10},
    ]})
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "features"}).json()
    a_els = [e for e in r["elements"] if e["kind"] == "A"]
    assert [e["name"] for e in a_els] == ["LOCKOUT mechanism"]     # kept whole, not lost


def test_combi_rescan_only_assesses_the_NEW_elements(client, monkeypatch):
    """Re-running after splitting the additional feature must NOT re-judge the mandatory
    elements already assessed — that is the whole token cost."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch, elements=("packs", "bus", "comms"))
    asked = []
    def fake_cov(elements, docs, model=None):
        asked.append([e["name"] for e in elements])
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"], "status": "yes", "evidence": "e"}
            for e in elements] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    client.post(f"/api/tabs/{tid}/combi-scan", json={})
    assert asked and set(asked[0]) == {"packs", "bus", "comms"}
    # now ADD an element (as splitting the additional feature does) and re-run
    asked.clear()
    client.post(f"/api/tabs/{tid}/benchmark/features/add",
                json={"name": "lockout", "weight": 3, "kind": "A", "sl": 10})
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert asked, "expected the new element to be assessed"
    assert all(set(a) == {"lockout"} for a in asked)     # ONLY the new one — nothing re-read
    # ...and the earlier mandatory verdicts survived the merge
    import patentbench.db as _db, json as _json
    rec = {c["name"]: c for c in _json.loads(
        [d for d in _db.list_documents(tid, full=True)][0]["combi_coverage"])}
    assert set(rec) == {"packs", "bus", "comms", "lockout"}
    assert rec["packs"]["status"] == "yes" and rec["packs"]["depth"] == "digest"


def test_combi_rescan_with_nothing_new_reuses_everything(client, monkeypatch):
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    calls = {"n": 0}
    def fake_cov(elements, docs, model=None):
        calls["n"] += 1
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"], "status": "yes", "evidence": "e"}
            for e in elements] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    client.post(f"/api/tabs/{tid}/combi-scan", json={})
    first = calls["n"]
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert calls["n"] == first          # zero new model calls
    assert r["solo"] or r["pairs"]      # ...and results still computed from stored coverage


def test_parse_screen_reads_terse_number_lines():
    from patentbench import claude_bridge as cb
    els = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    out = cb.parse_screen("EP4400001: 1,3\nEP4400002: NONE\nEP4400003: 2 3 99\n", els)
    assert out == {"EP4400001": [0, 2], "EP4400002": [], "EP4400003": [1, 2]}  # 99 ignored


def test_combi_screen_shortlists_the_top_candidates(client, monkeypatch):
    """🩺 stage 0: the fast cut. Judging every candidate at full rigour is the slow part —
    this decides who deserves it, and hands back only the shortlist."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    def fake_screen(features, docs, model=None):
        table = {"EP4400001": [0, 1, 2], "EP4400002": [0], "EP4400003": []}
        return {"results": {d["number"]: table[d["number"]] for d in docs}, "model": "haiku"}
    monkeypatch.setattr(cb, "combi_fast_screen", fake_screen)
    r = client.post(f"/api/tabs/{tid}/combi-screen", json={"top_n": 2}).json()
    assert r["screened"] == 3 and r["dropped"] == 1
    assert [s["number"] for s in r["shortlist"]] == ["EP4400001", "EP4400002"]  # best first
    # stored at depth 'screen' so nothing downstream mistakes it for a rigorous read
    import patentbench.db as _db
    d = [x for x in _db.list_documents(tid, full=True) if x["number"] == "EP4400001"][0]
    assert d["combi_depth"] == "screen"


def test_combi_scan_honours_the_screen_shortlist(client, monkeypatch):
    """The rigorous pass runs over the shortlist only — that is what makes it fast."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    seen = []
    def fake_cov(elements, docs, model=None):
        seen.extend(d["number"] for d in docs)
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"], "status": "yes", "evidence": "e"}
            for e in elements] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    ids = [d["id"] for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]][:2]
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={"doc_ids": ids}).json()
    assert r["scanned"] >= 2
    assert sorted(seen) == sorted(nums[:2])          # the third was never read


def test_combi_takes_the_additional_feature_into_account(client, monkeypatch):
    """A pair that ALSO brings the additional element must outrank an equal pair that
    doesn't — but the additional element never decides completeness, and its absence is
    never a penalty."""
    from patentbench import claude_bridge as cb
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": "CombiA"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "packs", "weight": 5, "kind": "M", "sl": 5},
        {"name": "bus", "weight": 5, "kind": "M", "sl": 5},
        {"name": "lockout", "weight": 5, "kind": "A", "sl": 10},
    ]})
    nums = ["EP4400001", "EP4400002", "EP4400003"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        _db.update_document(d["id"], digest=f"digest of {d['number']}", score=5, scored_at=1)
    def fake_cov(elements, docs, model=None):
        #                packs  bus    lockout(A)
        table = {"EP4400001": ["yes", "no",  "no"],
                 "EP4400002": ["no",  "yes", "yes"],   # brings the A element
                 "EP4400003": ["no",  "yes", "no"]}    # same M cover, no A
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i], "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    by = {frozenset([p["a"], p["b"]]): p for p in r["pairs"]}
    with_a = by[frozenset(["EP4400001", "EP4400002"])]
    without = by[frozenset(["EP4400001", "EP4400003"])]
    assert with_a["complete"] and without["complete"]      # A never decides completeness
    assert with_a["add_cov"] == 1 and without["add_cov"] == 0
    assert with_a["add_bonus"] > 0 and without["add_bonus"] == 0
    # rating stays MANDATORY coverage (both cover it fully) — the A element ranks instead,
    # because folding it into a rating that already sits at 10 would hide it entirely
    assert with_a["rating"] == without["rating"] == 10.0
    assert r["pairs"][0]["a_id"] == with_a["a_id"]         # the A-covering pair ranks FIRST
    assert r["pairs"][0]["b_id"] == with_a["b_id"]


def test_combi_additional_alone_never_makes_a_pair_complete(client, monkeypatch):
    """Only mandatory elements decide whether a pair covers the invention."""
    from patentbench import claude_bridge as cb
    import patentbench.db as _db
    tab = client.post("/api/tabs", json={"name": "CombiA2"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "packs", "weight": 5, "kind": "M", "sl": 5},
        {"name": "bus", "weight": 5, "kind": "M", "sl": 5},
        {"name": "third", "weight": 5, "kind": "M", "sl": 5},
        {"name": "lockout", "weight": 5, "kind": "A", "sl": 10},
    ]})
    nums = ["EP4400001", "EP4400002"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": nums, "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        _db.update_document(d["id"], digest="d", score=5, scored_at=1)
    def fake_cov(elements, docs, model=None):
        table = {"EP4400001": ["yes", "no", "no", "yes"],
                 "EP4400002": ["no", "yes", "no", "yes"]}   # 'third' covered by neither
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"],
             "status": table[d["number"]][i], "evidence": "e"}
            for i, e in enumerate(elements)] for d in docs}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert r["complete"] == 0
    assert r["pairs"][0]["add_cov"] == 1 and not r["pairs"][0]["complete"]


def test_screen_only_documents_never_produce_findings(client, monkeypatch):
    """The 🩺 screen is deliberately generous — it over-includes so nothing real is dropped
    before the rigorous pass. Those guesses must NEVER surface as pairs or solo hits: doing
    so publishes 'covers everything' built from a guess. Only digest+ verdicts count."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    # screen says every document plausibly discloses everything (the generous extreme)
    monkeypatch.setattr(cb, "combi_fast_screen", lambda features, docs, model=None: {
        "results": {d["number"]: [0, 1, 2] for d in docs}, "model": "haiku"})
    r = client.post(f"/api/tabs/{tid}/combi-screen", json={"top_n": 3}).json()
    assert r["screened"] == 3
    # ...yet the scan, with every rigorous batch failing, must report NO findings at all
    monkeypatch.setattr(cb, "combi_coverage_digests",
                        lambda elements, docs_, model=None: {"error": "claude chat timed out"})
    r2 = client.post(f"/api/tabs/{tid}/combi-scan", json={})
    assert r2.status_code == 400                       # loud, not a screen-built answer
    assert "timed out" in r2.json()["detail"]


def test_combi_scan_splits_and_retries_a_failed_batch(client, monkeypatch):
    """A timeout scales with batch size, so a dead batch is halved and retried rather than
    losing every document in it."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    monkeypatch.setattr(api, "COMBI_SCAN_BATCH", 3)
    sizes = []
    def fake_cov(elements, docs_, model=None):
        sizes.append(len(docs_))
        if len(docs_) > 1:                       # the big batch always times out
            return {"error": "claude chat timed out"}
        return {"results": {d["number"]: [
            {"name": e["name"], "weight": e["weight"], "status": "yes", "evidence": "e"}
            for e in elements] for d in docs_}, "model": "m"}
    monkeypatch.setattr(cb, "combi_coverage_digests", fake_cov)
    r = client.post(f"/api/tabs/{tid}/combi-scan", json={}).json()
    assert r["scanned"] == 3                     # all three landed via the split
    assert max(sizes) == 3 and sizes.count(1) == 3   # halved down to singletons


def test_combi_pair_depth_is_that_of_its_weaker_document(client, monkeypatch):
    """A pair is only as trustworthy as its weaker read — digest < full."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    # each document holds exactly ONE distinct element, so every pair is a genuine
    # combination (a document disclosing everything would subsume the others and no pair
    # would survive the filter at all)
    OWN = {"EP4400001": 0, "EP4400002": 1, "EP4400003": 2}
    def cov(elements, doc_nums):
        return {n: [{"name": e["name"], "weight": e["weight"],
                     "status": "yes" if i == OWN[n] else "no", "evidence": "e"}
                    for i, e in enumerate(elements)] for n in doc_nums}
    monkeypatch.setattr(cb, "combi_coverage_digests", lambda elements, docs_, model=None: {
        "results": cov(elements, [d["number"] for d in docs_]), "model": "m"})
    client.post(f"/api/tabs/{tid}/combi-scan", json={})       # all three at digest depth
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    # now upgrade TWO of the three to FULL depth; the third stays at digest
    monkeypatch.setattr(cb, "combi_coverage_full", lambda elements, doc, model=None: {
        "results": cov(elements, [doc["number"]]), "model": "m"})
    upgraded = [docs[0]["id"], docs[1]["id"]]
    r = client.post(f"/api/tabs/{tid}/combi-verify", json={"doc_ids": upgraded}).json()
    by = {(p["a_id"], p["b_id"]): p for p in r["pairs"]}
    both_up = [p for k, p in by.items() if set(k) == set(upgraded)]
    mixed = [p for k, p in by.items() if len(set(k) & set(upgraded)) == 1]
    assert both_up and all(p["depth"] == "full" for p in both_up)   # both sides verified
    assert mixed and all(p["depth"] == "digest" for p in mixed)     # weaker side wins


def test_combi_verify_full_text_replaces_the_digest_verdict(client, monkeypatch):
    """Stage 2 is where a shortlisted pair is confirmed — or legitimately falls away."""
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    monkeypatch.setattr(cb, "combi_coverage_digests", lambda elements, docs, model=None: {
        "results": {d["number"]: [{"name": e["name"], "weight": e["weight"],
                                   "status": "yes", "evidence": "e"} for e in elements]
                    for d in docs}, "model": "m"})
    client.post(f"/api/tabs/{tid}/combi-scan", json={})
    ids = [d["id"] for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]][:2]
    # the full read does NOT bear the digest out → coverage collapses
    monkeypatch.setattr(cb, "combi_coverage_full", lambda elements, doc, model=None: {
        "results": {doc["number"]: [{"name": e["name"], "weight": e["weight"],
                                     "status": "no", "evidence": ""} for e in elements]},
        "model": "m"})
    r = client.post(f"/api/tabs/{tid}/combi-verify", json={"doc_ids": ids}).json()
    assert r["verified"] == 2 and r["depth"] == "full"
    import json as _json
    import patentbench.db as _db
    row = _db.get_document(ids[0])
    assert row["combi_depth"] == "full"
    assert all(c["status"] == "no" for c in _json.loads(row["combi_coverage"]))


def test_combi_verify_failure_keeps_the_digest_verdict(client, monkeypatch):
    from patentbench import claude_bridge as cb
    tid, nums = _combi_tab(client, monkeypatch)
    monkeypatch.setattr(cb, "combi_coverage_digests", lambda elements, docs, model=None: {
        "results": {d["number"]: [{"name": e["name"], "weight": e["weight"],
                                   "status": "yes", "evidence": "e"} for e in elements]
                    for d in docs}, "model": "m"})
    client.post(f"/api/tabs/{tid}/combi-scan", json={})
    ids = [d["id"] for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]][:1]
    monkeypatch.setattr(cb, "combi_coverage_full",
                        lambda elements, doc, model=None: {"error": "session limit"})
    r = client.post(f"/api/tabs/{tid}/combi-verify", json={"doc_ids": ids}).json()
    assert r["verified"] == 0 and r["failed"] == 1
    import patentbench.db as _db
    assert _db.get_document(ids[0])["combi_depth"] == "digest"   # not silently downgraded
    msg = client.get(f"/api/tabs/{tid}/state").json()["messages"][-1]["text"]
    assert "full read(s) failed" in msg


def test_parse_decomposition_reads_weighted_element_lines():
    from patentbench import claude_bridge as cb
    out = cb.parse_decomposition(
        "1 | 5 | a plurality of battery packs\n"
        "2 | 4 | power conversion modules in one-to-one correspondence\n"
        "noise line that is not an element\n"
        "3 | 1 | a communication unit\n")
    assert [e["name"] for e in out] == ["a plurality of battery packs",
                                        "power conversion modules in one-to-one correspondence",
                                        "a communication unit"]
    assert [e["weight"] for e in out] == [5, 4, 1]
    assert all(e["kind"] == "M" for e in out)      # elements are mandatory by construction


def test_parse_decomposition_skips_an_echoed_template():
    from patentbench import claude_bridge as cb
    assert cb.parse_decomposition("<n> | <weight 1-5> | <the element, one line>") == []


def test_decompose_proposes_without_storing_anything(client, monkeypatch):
    """🔬 decompose PROPOSES only: a bad split must never silently poison 284 documents,
    so the benchmark is untouched until the user accepts."""
    from patentbench import claude_bridge as cb
    monkeypatch.setattr(cb, "_run_claude", lambda *a, **k: {
        "answer": "1 | 5 | a plurality of battery packs\n2 | 3 | a battery bus",
        "model": "claude-sonnet-4-6"})
    tab = client.post("/api/tabs", json={"name": "Dec"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features/add",
                json={"name": "one monolithic claim block", "weight": 5})
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "features"}).json()
    assert [e["name"] for e in r["elements"]] == ["a plurality of battery packs", "a battery bus"]
    # the benchmark still holds the ORIGINAL single feature — nothing was stored
    bm = client.get(f"/api/tabs/{tid}/state").json()["benchmark"]
    assert [f["name"] for f in bm["features"]] == ["one monolithic claim block"]


def test_parse_claims_decomposition_claim1_is_M_dependents_are_A():
    """Decomposing a CLAIM SET is claim-structure-aware: claim 1's elements are mandatory,
    a dependent claim's elements are ADDITIONAL — their absence must never gate a candidate."""
    from patentbench import claude_bridge as cb
    out = cb.parse_claims_decomposition(
        "1 | 1 | 5 | a plurality of battery packs\n"
        "1 | 2 | 4 | a battery bus\n"
        "noise line that is not an element\n"
        "2 | 3 | 2 | wave-by-wave current limiting\n"
        "5 | 4 | 1 | a display unit\n")
    assert [(e["name"], e["kind"], e["claim"]) for e in out] == [
        ("a plurality of battery packs", "M", 1),
        ("a battery bus", "M", 1),
        ("wave-by-wave current limiting", "A", 2),
        ("a display unit", "A", 5)]
    assert [e["weight"] for e in out] == [5, 4, 2, 1]
    assert all(e["sl"] == 5 for e in out)


def test_decompose_benchmark_claims_tags_dependent_elements_A(client, monkeypatch):
    """The default decompose of a benchmark's claim set must NOT flatten everything to
    mandatory — dependent-claim features come back as A (additional)."""
    from patentbench import claude_bridge as cb
    monkeypatch.setattr(cb, "_run_claude", lambda *a, **k: {
        "answer": ("1 | 1 | 5 | a plurality of battery packs\n"
                   "1 | 2 | 4 | a battery bus\n"
                   "3 | 3 | 2 | a lockout mechanism\n"),
        "model": "m"})
    tab = client.post("/api/tabs", json={"name": "DecCl"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "EP1111111A1"})
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "benchmark"}).json()
    assert r["mandatory"] == 2 and r["additional"] == 1
    a_els = [e for e in r["elements"] if e["kind"] == "A"]
    assert [e["name"] for e in a_els] == ["a lockout mechanism"]


def test_decompose_from_benchmark_claims(client, monkeypatch):
    from patentbench import claude_bridge as cb
    seen = {}
    def fake_run(prompt, model, **k):
        seen["p"] = prompt
        return {"answer": "1 | 5 | an element", "model": model}
    monkeypatch.setattr(cb, "_run_claude", fake_run)
    tab = client.post("/api/tabs", json={"name": "DecB"}).json()
    tid = tab["id"]
    client.put(f"/api/tabs/{tid}/benchmark", json={"text": "EP1111111A1"})
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "benchmark"})
    assert r.status_code == 200
    assert "1. A method." in seen["p"]              # the benchmark's claims were the source


def test_decompose_needs_something_to_decompose(client, monkeypatch):
    tab = client.post("/api/tabs", json={"name": "DecE"}).json()
    tid = tab["id"]
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "features"})
    assert r.status_code == 400 and "no features" in r.json()["detail"]


def test_decompose_surfaces_an_unparsable_answer(client, monkeypatch):
    from patentbench import claude_bridge as cb
    monkeypatch.setattr(cb, "_run_claude",
                        lambda *a, **k: {"answer": "I think the claim is about batteries.",
                                         "model": "m"})
    tab = client.post("/api/tabs", json={"name": "DecU"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features/add", json={"name": "x", "weight": 5})
    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "features"})
    assert r.status_code == 400 and "no parsable elements" in r.json()["detail"]


def test_digest_failure_is_recorded_not_dropped(client, monkeypatch):
    """A swallowed digest error takes a document out of scope of EVERY digest-based tool
    (➕ additional read, ♻️ re-check, 🧩 combi) invisibly — while runs still report 'all'.
    The failure must be recorded so the gap is findable."""
    from patentbench import claude_bridge as cb
    monkeypatch.setattr(cb, "digest_document",
                        lambda n, t, x, model=None: {"error": "session limit"})
    tab = client.post("/api/tabs", json={"name": "DigFail"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4338615"], "source": "image"})
    d = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    api._digest_doc(d["id"])
    import patentbench.db as _db
    row = _db.get_document(d["id"])
    assert not (row.get("digest") or "")
    assert "session limit" in (row.get("digest_error") or "")     # the reason survives
    gap = client.get(f"/api/tabs/{tid}/digest-gap").json()
    assert gap["missing"] == 1 and gap["with_digest"] == 0
    assert gap["docs"][0]["number"] == "EP4338615"


def test_digest_backfill_fills_the_gap(client, monkeypatch):
    """🔁 backfill puts the skipped candidates back in scope."""
    from patentbench import claude_bridge as cb
    monkeypatch.setattr(cb, "digest_document",
                        lambda n, t, x, model=None: {"error": "session limit"})
    tab = client.post("/api/tabs", json={"name": "DigBF"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/documents",
                json={"numbers": ["EP4338615", "EP4338616"], "source": "image"})
    for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]:
        api._digest_doc(d["id"])
    assert client.get(f"/api/tabs/{tid}/digest-gap").json()["missing"] == 2
    # the transient failure clears → backfill succeeds
    monkeypatch.setattr(cb, "digest_document",
                        lambda n, t, x, model=None: {"digest": f"digest of {n}"})
    r = client.post(f"/api/tabs/{tid}/digest-backfill", json={}).json()
    assert r["backfilled"] == 2 and r["still_missing"] == 0
    assert client.get(f"/api/tabs/{tid}/digest-gap").json()["missing"] == 0


def test_digest_backfill_reports_what_still_fails(client, monkeypatch):
    from patentbench import claude_bridge as cb
    monkeypatch.setattr(cb, "digest_document",
                        lambda n, t, x, model=None: {"error": "session limit"})
    tab = client.post("/api/tabs", json={"name": "DigBF2"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/documents", json={"numbers": ["EP4338615"], "source": "image"})
    d = client.get(f"/api/tabs/{tid}/documents").json()["documents"][0]
    api._digest_doc(d["id"])
    r = client.post(f"/api/tabs/{tid}/digest-backfill", json={}).json()
    assert r["backfilled"] == 0 and r["still_missing"] == 1
    msg = client.get(f"/api/tabs/{tid}/state").json()["messages"][-1]["text"]
    assert "still failed" in msg                     # not silently reported as done


def test_digest_rescore_all_docs_covers_every_digested_candidate(client, monkeypatch):
    """♻️ re-check (ALL): after a benchmark change the WHOLE list is stale, not just the
    documents that happened to rank on top."""
    from patentbench import claude_bridge as cb
    tid, nums = _add_read_tab(client, 7, batch=2, monkeypatch=monkeypatch)
    seen = []
    def fake_rescore(bm, docs, model=None):
        seen.extend(d["number"] for d in docs)
        return {"results": {d["number"]: {"score": 6, "note": "n"} for d in docs},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "digest_rescore", fake_rescore)
    r = client.post(f"/api/tabs/{tid}/digest-rescore", json={"all_docs": True}).json()
    assert r["updated"] == 7 and r["batches"] == 4        # batched, not one giant call
    assert sorted(seen) == sorted(nums)
    docs = client.get(f"/api/tabs/{tid}/documents").json()["documents"]
    assert all(d["score"] == 6 for d in docs)
    assert all("·digest" in (d["score_model"] or "") for d in docs)


def test_digest_rescore_all_partial_failure_is_reported(client, monkeypatch):
    from patentbench import claude_bridge as cb
    tid, nums = _add_read_tab(client, 6, batch=2, monkeypatch=monkeypatch)
    calls = {"n": 0}
    lock = __import__("threading").Lock()
    def fake_rescore(bm, docs, model=None):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            return {"error": "session limit"}
        return {"results": {d["number"]: {"score": 6, "note": "n"} for d in docs},
                "model": "claude-sonnet-4-6"}
    monkeypatch.setattr(cb, "digest_rescore", fake_rescore)
    r = client.post(f"/api/tabs/{tid}/digest-rescore", json={"all_docs": True}).json()
    assert r["updated"] == 4 and r["requested"] == 6 and r["failed_batches"] == 1
    msg = client.get(f"/api/tabs/{tid}/state").json()["messages"][-1]["text"]
    assert "2 candidate(s) not re-checked" in msg


def test_additional_read_all_docs_errors_when_every_batch_fails(client, monkeypatch):
    from patentbench import claude_bridge as cb
    tid, nums = _add_read_tab(client, 3)
    monkeypatch.setattr(cb, "additional_read", lambda *a, **k: {"error": "session limit"})
    r = client.post(f"/api/tabs/{tid}/additional-read", json={"all_docs": True})
    assert r.status_code == 400 and "session limit" in r.json()["detail"]


def test_score_recalc_reaggregates_stored_verdicts_under_current_kinds(client):
    """🧮 recalc: after relabeling features (M → A) the stored per-element verdicts are
    re-aggregated under the CURRENT kinds — zero model calls. A doc that missed only
    now-A features must jump to a 10/10 Must-rating."""
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "Recalc"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "cover body", "weight": 5, "kind": "M", "sl": 5},
        {"name": "outer pole", "weight": 5, "kind": "M", "sl": 5},
        {"name": "clamping structure", "weight": 3, "kind": "A", "sl": 5},   # was M once
    ]})
    # covers both (current) M elements, misses the now-A one; frozen score = old low note
    d1 = _feature_doc(db, tid, "EP5500001", [
        {"name": "cover body", "status": "yes"}, {"name": "outer pole", "status": "yes"},
        {"name": "clamping structure", "status": "no"}])
    db.update_document(d1, score=4.0, score_note="old all-M framing", score_model="opus")
    # no per-element verdicts at all → must keep its old holistic score untouched
    d2 = db.add_documents(tid, ["EP5500002"])["inserted"][0]
    db.update_document(d2, status="fetched", score=7.0, score_note="holistic only")
    r = client.post(f"/api/tabs/{tid}/score-recalc").json()
    assert r["updated"] == 1 and r["no_verdicts"] == 1
    docs = {d["number"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert docs["EP5500001"]["score"] == 10.0                    # both M ✓ → full Must-rating
    assert docs["EP5500001"]["score_model"] == "recalc·stored-verdicts"
    assert docs["EP5500002"]["score"] == 7.0                     # untouched, no verdicts stored
    assert docs["EP5500002"]["score_note"] == "holistic only"


def test_score_recalc_needs_mandatory_elements_and_verdicts(client):
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "RecalcE"}).json()
    tid = tab["id"]
    r = client.post(f"/api/tabs/{tid}/score-recalc")
    assert r.status_code == 400 and "mandatory" in r.json()["detail"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "cover body", "weight": 5, "kind": "M", "sl": 5}]})
    r = client.post(f"/api/tabs/{tid}/score-recalc")
    assert r.status_code == 400 and "per-element" in r.json()["detail"]


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
    client.post(f"/api/tabs/{a['id']}/documents",
                json={"numbers": ["US9999999B1"], "digest": True})
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
    client.post(f"/api/tabs/{a}/documents",
                json={"numbers": ["US7777777"], "source": "manual", "digest": True})
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
                json={"numbers": ["US5555555", "US6666666"], "source": "manual",
                      "digest": True})
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


def _psa_tab_with_docs(psa_client, name="Basis"):
    _upload_method(psa_client)
    tab = psa_client.post("/api/tabs", json={"name": name}).json()
    psa_client.put(f"/api/tabs/{tab['id']}/benchmark", json={"text": "EP1111111A1"})
    psa_client.post(f"/api/tabs/{tab['id']}/documents",
                    json={"text": "CN113964850 US11909216B2"})
    ids = [d["id"] for d in
           psa_client.get(f"/api/tabs/{tab['id']}/documents").json()["documents"]]
    return tab["id"], ids


def _capture_psa(monkeypatch):
    seen = {}
    def fake_psa(method_text, benchmark, docs, model=None, format_text=None,
                 discussions=None, stretch=False, invention=None):
        seen.update(benchmark=benchmark, invention=invention)
        return {"answer": "STEP 1: …", "model": model}
    monkeypatch.setattr(claude_bridge, "psa", fake_psa)
    return seen


def test_psa_basis_defaults_to_the_benchmark(psa_client, monkeypatch):
    """Unchanged default: no basis given → the benchmark document is the invention."""
    seen = _capture_psa(monkeypatch)
    tid, ids = _psa_tab_with_docs(psa_client)
    psa_client.post(f"/api/tabs/{tid}/psa", json={"doc_ids": ids})
    assert seen["invention"] is None                       # → bridge renders the benchmark
    assert seen["benchmark"]["number"] == "EP1111111A1"
    msgs = psa_client.get(f"/api/tabs/{tid}/state").json()["messages"]
    q = [m for m in msgs if m["role"] == "q"][-1]
    assert "basis: 🎯 benchmark EP1111111A1" in q["text"]   # the basis is on the record


def test_psa_basis_text_replaces_the_benchmark(psa_client, monkeypatch):
    """✍️ pasted text IS the claimed invention — the benchmark must NOT be sent, so the
    run assesses exactly what the user chose."""
    seen = _capture_psa(monkeypatch)
    tid, ids = _psa_tab_with_docs(psa_client)
    r = psa_client.post(f"/api/tabs/{tid}/psa", json={
        "doc_ids": ids, "basis": "text",
        "basis_text": "a hardware discharge-channel lockout during concurrent charging"})
    assert r.status_code == 200
    assert seen["benchmark"] is None                        # benchmark withheld
    assert seen["invention"]["text"].startswith("a hardware discharge-channel lockout")
    msgs = psa_client.get(f"/api/tabs/{tid}/state").json()["messages"]
    q = [m for m in msgs if m["role"] == "q"][-1]
    assert "basis: ✍️ pasted text (" in q["text"]           # readable back months later
    c = [m for m in msgs if m["role"] == "c"][-1]
    assert any(p["title"].startswith("basis: ✍️") for p in c["participants"])


def test_psa_basis_text_requires_actual_text(psa_client, monkeypatch):
    """Choosing ✍️ text with an empty chat box must fail LOUDLY, not silently fall back
    to the benchmark — a silent fallback is exactly the 'based on what?' confusion."""
    _capture_psa(monkeypatch)
    tid, ids = _psa_tab_with_docs(psa_client)
    r = psa_client.post(f"/api/tabs/{tid}/psa",
                        json={"doc_ids": ids, "basis": "text", "basis_text": "  "})
    assert r.status_code == 400 and "paste the text" in r.json()["detail"]


def test_psa_basis_features_uses_the_feature_spec(psa_client, monkeypatch):
    seen = _capture_psa(monkeypatch)
    tid, ids = _psa_tab_with_docs(psa_client, name="BasisF")
    # a document benchmark that ALSO carries features (the two now coexist)
    psa_client.post(f"/api/tabs/{tid}/benchmark/features/add",
                    json={"name": "a stacked battery pack", "weight": 5})
    r = psa_client.post(f"/api/tabs/{tid}/psa", json={"doc_ids": ids, "basis": "features"})
    assert r.status_code == 200
    assert seen["benchmark"] is None
    assert "a stacked battery pack" in seen["invention"]["text"]
    q = [m for m in psa_client.get(f"/api/tabs/{tid}/state").json()["messages"]
         if m["role"] == "q"][-1]
    assert "basis: 🧩 benchmark features (1)" in q["text"]


def test_psa_basis_features_needs_features(psa_client, monkeypatch):
    _capture_psa(monkeypatch)
    tid, ids = _psa_tab_with_docs(psa_client, name="BasisNoF")
    r = psa_client.post(f"/api/tabs/{tid}/psa", json={"doc_ids": ids, "basis": "features"})
    assert r.status_code == 400 and "no target features" in r.json()["detail"]


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
    assert "CLAIMED INVENTION UNDER ASSESSMENT — the benchmark document" in p
    assert "[BENCHMARK — EP1 — Bench]" in p       # ...rendered from the benchmark
    assert "D1 — selected prior-art document 1 of 2" in p
    assert "D2 — selected prior-art document 2 of 2" in p
    assert "do not skip, merge, reorder" in p
    assert "never silently drop it" in p


def test_psa_invention_replaces_the_benchmark_in_the_prompt(monkeypatch):
    """A chosen basis (e.g. a pasted feature) IS the claimed invention: it goes in under
    the same heading and the benchmark document is NOT sent at all — the run must assess
    exactly what the user picked and nothing else."""
    captured = {}
    monkeypatch.setattr(claude_bridge, "_run_claude",
                        lambda prompt, model, extra_args=None, cwd=None, timeout=None:
                        captured.update(p=prompt) or {"answer": "ok", "model": model})
    claude_bridge.psa("STEP 1: closest prior art.",
                      {"number": "EP1", "title": "Bench", "claims": "1. A thing."},
                      [{"number": "D1DOC", "title": "a", "claims": "1. x"},
                       {"number": "D2DOC", "title": "b", "claims": "1. y"}],
                      invention={"label": "text supplied by the user for this run",
                                 "text": "a hardware discharge-channel lockout"})
    p = captured["p"]
    assert "CLAIMED INVENTION UNDER ASSESSMENT — text supplied by the user" in p
    assert "a hardware discharge-channel lockout" in p
    assert "EP1" not in p and "[BENCHMARK" not in p    # benchmark NOT sent
    assert "D1 — selected prior-art document 1 of 2" in p   # prior art still there


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


def test_psa_text_edit_in_place(psa_client):
    """✎ the ⚖️ documents are editable as TEXT in place: `format` shows the built-in
    6-step chain when nothing is uploaded; saving persists without a re-upload;
    empty (or the untouched default) removes the stored doc again."""
    r = psa_client.get("/api/psa/format/text").json()
    assert r["overridden"] is False
    assert "objective technical problem is therefore" in r["text"]
    assert r["text"] == r["default"]
    r = psa_client.put("/api/psa/format/text", json={"text": "MY CHAIN v2"}).json()
    assert r["overridden"] is True and r["text"] == "MY CHAIN v2"
    assert psa_client.get("/api/psa/format").json()["ok"] is True    # doc now exists
    r = psa_client.put("/api/psa/format/text", json={"text": ""}).json()
    assert r["overridden"] is False and "readily combinable" in r["text"]
    assert psa_client.get("/api/psa/format").json()["ok"] is False   # back to built-in
    # method has NO built-in default — empty text until something is saved
    r = psa_client.get("/api/psa/method/text").json()
    assert r["text"] == "" and r["default"] == ""
    r = psa_client.put("/api/psa/method/text", json={"text": "STEP 1: x."}).json()
    assert r["overridden"] is True
    assert psa_client.get("/api/psa/method").json()["ok"] is True
    assert psa_client.get("/api/psa/bogus/text").status_code == 404


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
    assert "OUTPUT FORMAT (BINDING" in p
    assert "SECTION A: table first." in p
    assert "this format wins" in p
    # without a format doc the BUILT-IN 6-step problem-solution chain applies
    claude_bridge.psa("STEP 1: x.", {"number": "EP1", "claims": "1. A thing."},
                      [{"number": "D1DOC", "claims": "1. x"},
                       {"number": "D2DOC", "claims": "1. y"}])
    p2 = captured["p"]
    assert "OUTPUT FORMAT (BINDING" in p2
    assert "The objective technical problem is therefore" in p2
    assert "readily combinable" in p2
    assert "ADDITIONAL FEATURES section" in p2
    # the global house style rides along on every PSA run
    assert "HOUSE STYLE (BINDING)" in p2


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
                 discussions=None, stretch=False, invention=None):
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


# ---------- 🏆 chat-grade ideal pair ----------

def test_parse_ideal_pair_and_strip():
    text = ("The pair is CN109964136 with EP2088659 because …\n\n"
            "IDEAL PAIR: CN109964136 + EP2088659")
    assert claude_bridge.parse_ideal_pair(text) == ("CN109964136", "EP2088659")
    # the LAST match wins (the model may quote the requested format first), separators vary
    text2 = ("Format reminder: IDEAL PAIR: XX0000000 + YY0000000\n…analysis…\n"
             "IDEAL PAIR: US1111111 and EP2222222")
    assert claude_bridge.parse_ideal_pair(text2) == ("US1111111", "EP2222222")
    assert claude_bridge.parse_ideal_pair("no trailer here") is None
    stripped = claude_bridge.strip_ideal_trailer(text)
    assert "IDEAL PAIR" not in stripped and "CN109964136 with EP2088659" in stripped


def _ideal_tab(client, db):
    """Tab with a 2-mandatory-element benchmark and two fetched candidates."""
    tid = client.post("/api/tabs", json={"name": "Ideal"}).json()["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "a battery", "weight": 5},
                                   {"name": "a gauge", "weight": 3}], "title": "b+g"})
    ids = db.add_documents(tid, ["CN109964136A", "EP2088659A1"])["inserted"]
    for i in ids:
        db.update_document(i, status="fetched", claims="1. A method.", description="desc")
    return tid, ids


def test_combi_ideal_endpoint_writes_matrix_and_pins_verdict(client, monkeypatch):
    tid, (a_id, b_id) = _ideal_tab(client, db)
    seen = {}

    def _chat(*a, **k):
        seen.update(k)
        return {"answer": "A supplies X; B supplies Y.\n\n"
                          "IDEAL PAIR: CN109964136 + EP2088659",
                "model": "claude-opus-4-8"}
    monkeypatch.setattr(claude_bridge, "chat", _chat)
    monkeypatch.setattr(claude_bridge, "combi_ideal_verify",
        lambda els, da, dbdoc, rationale, model=None: {
            "results": {
                "CN109964136A": [{"name": "a battery", "weight": 5, "status": "yes", "evidence": "claim 1"},
                                 {"name": "a gauge", "weight": 3, "status": "no", "evidence": ""}],
                "EP2088659A1": [{"name": "a battery", "weight": 5, "status": "no", "evidence": ""},
                                {"name": "a gauge", "weight": 3, "status": "yes", "evidence": "[0067]"}]},
            "combinable": True, "reason": "same field, complementary teachings",
            "model": "claude-opus-4-8"})
    r = client.post(f"/api/tabs/{tid}/combi/ideal", json={"model": "claude-opus-4-8"}).json()
    # STATELESS: no chat history may reach phase 1 — a prior 🏆 verdict in the
    # conversation made re-runs echo the old pair instead of using fresh reads
    assert seen.get("history") is None
    assert r["ok"] and r["ideal"]["a_number"] == "CN109964136A"
    assert r["ideal"]["b_number"] == "EP2088659A1"
    assert r["ideal"]["mand_yes"] == 2 and r["ideal"]["mand_total"] == 2
    assert r["ideal"]["combinable"] is True and r["ideal"]["open"] == []
    # pair cells written at FULL depth → the matrix now renders the chat verdict
    da, dbb = db.get_document(a_id), db.get_document(b_id)
    assert da["combi_depth"] == "full" and dbb["combi_depth"] == "full"
    assert '"yes"' in dbb["combi_coverage"] and "0067" in dbb["combi_coverage"]
    # combinability persisted for the pair
    lo, hi = sorted((a_id, b_id))
    assert db.get_combi_motivations(tid)[f"{lo}-{hi}"]["combinable"] is True
    # survives reload: /combi-results rehydrates the pinned verdict
    cr = client.get(f"/api/tabs/{tid}/combi-results").json()
    assert cr["ideal"]["a_id"] == a_id and cr["ideal"]["union"][1]["by"] == "B"
    # the prose and the union summary landed in the chat
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert any("A supplies X" in m and "IDEAL PAIR" not in m for m in msgs)
    assert any("🏆 Ideal pair CN109964136A + EP2088659A1" in m for m in msgs)
    # the DETAILED per-element mapping is a chat message of its own, with codes,
    # supplier and the full-text evidence cites
    mapping = next(m for m in msgs if "Detailed feature mapping" in m)
    assert "ME1 — a battery: A CN109964136A ✓ (claim 1)" in mapping
    assert "ME2 — a gauge: B EP2088659A1 ✓ ([0067])" in mapping


def test_combi_ideal_pair_outside_tab_updates_nothing(client, monkeypatch):
    tid, (a_id, b_id) = _ideal_tab(client, db)
    monkeypatch.setattr(claude_bridge, "chat",
                        lambda *a, **k: {"answer": "Best is elsewhere.\n\n"
                                                   "IDEAL PAIR: CN109964136 + US9999999",
                                         "model": "claude-opus-4-8"})
    r = client.post(f"/api/tabs/{tid}/combi/ideal", json={}).json()
    assert r["ok"] is False and r["ideal"] is None
    assert db.get_document(a_id)["combi_depth"] is None      # nothing written
    msgs = [m["text"] for m in client.get(f"/api/tabs/{tid}/state").json()["messages"]]
    assert any("not among this tab's fetched candidates" in m for m in msgs)


def test_documents_endpoint_attaches_must_rank_like_state(client):
    """The /documents refresh path must carry the SAME unified Must rank as /state —
    when it didn't, the list silently lost its 🎯 sort after a deep read and showed a
    different #1 than the matrix (2026-07-27)."""
    import patentbench.db as db
    tab = client.post("/api/tabs", json={"name": "RankParity"}).json()
    tid = tab["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "a battery", "weight": 5},
                                   {"name": "a gauge", "weight": 3}], "title": "b+g"})
    did = _feature_doc(db, tid, "US3333333A",
                       [{"name": "a battery", "weight": 5, "status": "yes", "note": "cell"},
                        {"name": "a gauge", "weight": 3, "status": "partial", "note": "meter"}])
    st = {d["id"]: d for d in client.get(f"/api/tabs/{tid}/state").json()["documents"]}
    dl = {d["id"]: d for d in client.get(f"/api/tabs/{tid}/documents").json()["documents"]}
    assert st[did]["rank"] and dl[did]["rank"], "both payloads must carry the rank"
    assert dl[did]["rank"]["key"] == st[did]["rank"]["key"]
    assert dl[did]["rank"]["mand_full"] == 1 and dl[did]["rank"]["mand_partial"] == 1


def test_covers_all_is_strict_all_yes(client):
    """User rule 2026-07-27: 'alone' / covers-all = EVERY Must element a hard ✓.
    A doc with a ~ gets no_absent (nothing to fill) but NOT covers_all, no 1e9 tier —
    so a 3✓+3~ doc ranks below a clean 5✓/6 doc by plain rating."""
    import patentbench.db as db
    tid = client.post("/api/tabs", json={"name": "Strict"}).json()["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features",
                json={"features": [{"name": "a battery", "weight": 5},
                                   {"name": "a gauge", "weight": 5}], "title": "b+g"})
    all_yes = _feature_doc(db, tid, "US4444444A",
                           [{"name": "a battery", "weight": 5, "status": "yes", "note": ""},
                            {"name": "a gauge", "weight": 5, "status": "yes", "note": ""}])
    with_part = _feature_doc(db, tid, "US5555555A",
                             [{"name": "a battery", "weight": 5, "status": "yes", "note": ""},
                              {"name": "a gauge", "weight": 5, "status": "partial", "note": ""}])
    docs = {d["id"]: d for d in client.get(f"/api/tabs/{tid}/state").json()["documents"]}
    strict, loose = docs[all_yes]["rank"], docs[with_part]["rank"]
    assert strict["covers_all"] is True and strict["no_absent"] is True
    assert loose["covers_all"] is False and loose["no_absent"] is True
    assert strict["key"] >= 1e9 > loose["key"]


def test_benchmark_scanned_pdf_falls_back_to_vision_ocr(client, monkeypatch):
    """A scanned (image-only) benchmark PDF must be vision-transcribed page by page —
    it used to hard-fail with 'no extractable text … upload the pages as pictures
    instead' (bit 2026-07-27: amended_478.pdf). Same fallback the ⚖️ PSA upload has."""
    from patentbench import extract
    monkeypatch.setattr(extract, "text_from_pdf",
                        lambda p: {"error": "no extractable text in the PDF (scanned "
                                            "image-only PDF?) — upload the pages as "
                                            "pictures instead"})
    monkeypatch.setattr(extract, "text_from_scanned_pdf",
                        lambda p, model=None, workers=4, progress=None:
                        {"text": "— page 1 —\namended claim 1 wording from the scan"})
    tab = client.post("/api/tabs", json={"name": "Scan"}).json()
    client.post(f"/api/tabs/{tab['id']}/benchmark/upload",
                files=[("files", ("amended_478.pdf", b"%PDF-1.4 fake", "application/pdf"))])
    st = client.get(f"/api/tabs/{tab['id']}/state").json()["benchmark"]
    assert st["status"] == "ready" and not st.get("error")
    full = client.get(f"/api/tabs/{tab['id']}/benchmark/full").json()
    assert "amended claim 1 wording" in full["text"]
    assert st.get("text_model")        # vision was involved → the model is recorded


def test_run_claude_timeout_kills_whole_process_group(tmp_path, monkeypatch):
    """A timed-out CLI must not wedge the worker thread even when it left a
    grandchild holding stdout: subprocess.run()'s builtin timeout kills only the
    direct child and then blocks forever draining the pipe (bit 2026-08-04 — a
    hung read held a tab's read lock 13+ min past DIGEST_TIMEOUT, so ⏸ pause
    never engaged). _run_claude now SIGKILLs the whole process group."""
    import time as _time
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nsleep 300 &\nsleep 300\n")   # grandchild inherits stdout
    fake.chmod(0o755)
    monkeypatch.setattr(claude_bridge, "CLAUDE_BIN", str(fake))
    monkeypatch.setattr(claude_bridge, "available", lambda: (True, ""))
    t0 = _time.monotonic()
    res = claude_bridge._run_claude("hi", "claude-sonnet-4-6", timeout=1)
    assert _time.monotonic() - t0 < 10          # returns promptly, no pipe-drain hang
    assert res == {"error": "claude chat timed out"}
