"""Per-tab NLM account (auth profile) binding.

Covers the three layers:
- bridge: --profile flag injection (only for non-default), per-profile caches
- api: /api/nlm/profiles listing, per-tab GET/PUT, the STICKY lock
- threading: a pinned tab's calls actually carry its profile
"""
import pytest

from patentbench import nlm_bridge
from patentbench.web import api


# ---------- bridge ----------

def test_with_profile_flag_only_for_non_default():
    cmd = ["nlm", "notebook", "list"]
    assert nlm_bridge._with_profile(cmd, None) == cmd
    assert nlm_bridge._with_profile(cmd, "default") == cmd
    assert nlm_bridge._with_profile(cmd, "work2") == ["nlm", "notebook", "list",
                                                      "--profile", "work2"]


def test_list_profiles_scans_dir(tmp_path, monkeypatch):
    root = tmp_path / ".notebooklm-mcp-cli" / "profiles"
    (root / "default").mkdir(parents=True)
    (root / "default" / "cookies.json").write_text("{}")
    (root / "work2").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    profs = {p["name"]: p["authed"] for p in nlm_bridge.list_profiles()}
    assert profs == {"default": True, "work2": False}


def test_available_checks_named_profile_cookies(tmp_path, monkeypatch):
    root = tmp_path / ".notebooklm-mcp-cli" / "profiles"
    (root / "default").mkdir(parents=True)
    (root / "default" / "cookies.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(nlm_bridge, "NLM_BIN", "/bin/sh")   # exists → CLI check passes
    ok, _ = nlm_bridge.available()
    assert ok
    ok2, why2 = nlm_bridge.available("work2")
    assert not ok2 and "work2" in why2


def test_list_cache_is_per_profile(monkeypatch):
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        class P:
            returncode = 0
            stdout = '[{"id": "nb-%d", "title": "T"}]' % len(calls)
            stderr = ""
        return P()

    monkeypatch.setattr(nlm_bridge, "_run", fake_run)
    monkeypatch.setattr(nlm_bridge, "available", lambda profile=None: (True, ""))
    monkeypatch.setattr(nlm_bridge, "_list_cache", {})
    a = nlm_bridge.list_notebooks()
    b = nlm_bridge.list_notebooks(profile="work2")
    assert len(calls) == 2                       # different profiles → no cache sharing
    assert "--profile" in calls[1] and "--profile" not in calls[0]
    assert a != b
    assert nlm_bridge.list_notebooks() == a      # cached, no third call
    assert len(calls) == 2


# ---------- api ----------

@pytest.fixture
def two_profiles(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_profiles",
                        lambda: [{"name": "default", "authed": True},
                                 {"name": "work2", "authed": True}])


def _mk_tab(client, name="T"):
    return client.post("/api/tabs", json={"name": name}).json()["id"]


def test_profiles_endpoint(client, two_profiles):
    r = client.get("/api/nlm/profiles").json()
    assert r["default"] == "default"
    assert [p["name"] for p in r["profiles"]] == ["default", "work2"]


def test_tab_profile_set_get_and_state(client, two_profiles):
    t = _mk_tab(client)
    assert client.get(f"/api/tabs/{t}/nlm-profile").json() == {
        "profile": None, "locked": False, "locked_why": None}
    r = client.put(f"/api/tabs/{t}/nlm-profile", json={"profile": "work2"})
    assert r.status_code == 200 and r.json()["profile"] == "work2"
    assert client.get(f"/api/tabs/{t}/nlm-profile").json()["profile"] == "work2"
    assert client.get(f"/api/tabs/{t}/state").json()["nlm_profile"] == "work2"
    # 'default' normalizes back to None
    r = client.put(f"/api/tabs/{t}/nlm-profile", json={"profile": "default"})
    assert r.json()["profile"] is None


def test_tab_profile_rejects_unseeded(client, two_profiles, monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_profiles",
                        lambda: [{"name": "default", "authed": True}])
    t = _mk_tab(client)
    r = client.put(f"/api/tabs/{t}/nlm-profile", json={"profile": "ghost"})
    assert r.status_code == 400


def test_tab_profile_locks_once_notebook_connected(client, two_profiles):
    t = _mk_tab(client)
    client.put(f"/api/tabs/{t}/notebook",
               json={"notebook_id": "nb-1", "notebook_title": "NB",
                     "source_ids": [], "auto_add": False})
    info = client.get(f"/api/tabs/{t}/nlm-profile").json()
    assert info["locked"] and "notebook" in info["locked_why"]
    r = client.put(f"/api/tabs/{t}/nlm-profile", json={"profile": "work2"})
    assert r.status_code == 409
    # same-value PUT stays a no-op even when locked
    r = client.put(f"/api/tabs/{t}/nlm-profile", json={"profile": "default"})
    assert r.status_code == 200


# ---------- threading: a pinned tab's calls carry its profile ----------

def test_pinned_tab_lists_notebooks_with_profile(client, two_profiles, monkeypatch):
    t = _mk_tab(client)
    client.put(f"/api/tabs/{t}/nlm-profile", json={"profile": "work2"})
    seen = {}
    monkeypatch.setattr(nlm_bridge, "list_notebooks",
                        lambda force=False, profile=None, **k:
                        seen.update(p=profile) or {"notebooks": []})
    client.get("/api/notebooks", params={"profile": "work2"})
    assert seen["p"] == "work2"
    # and the tab-scoped availability check resolves the pinned profile
    seen2 = {}
    monkeypatch.setattr(nlm_bridge, "available",
                        lambda profile=None: seen2.update(p=profile) or (False, "nope"))
    r = client.post(f"/api/tabs/{t}/nlm-rate", json={})
    assert r.status_code == 400
    assert seen2["p"] == "work2"
