

def test_rotate_out_ids_drops_losers_and_duplicates():
    """Duplicate sources (same title staged twice after a timed-out add) must be
    rotated out like losers — they eat 50-cap slots and cost tail parts
    (t12 2026-08-25: JP5011645 ×3, WO2025044604 part 2 lost)."""
    from patentbench.web import api
    want = {api._shortlist_key("JP5011645"), api._shortlist_key("WO2025044604")}
    raw = [
        {"id": "b", "title": "🎯 BENCHMARK — x"},
        {"id": "b2", "title": "🎯 BENCHMARK — x"},                       # duplicate benchmark
        {"id": "j1", "title": "JP5011645 — Secondary battery"},
        {"id": "j1d", "title": "JP5011645 — Secondary battery"},         # duplicate
        {"id": "j2", "title": "JP5011645 (part 2/2) — Secondary battery"},
        {"id": "w1", "title": "WO2025044604 — Motor controller"},
        {"id": "w1d", "title": "WO2025044604 — Motor controller"},       # duplicate
        {"id": "l", "title": "US20230032979 — loser"},                   # not wanted
    ]
    ids, dups = api._rotate_out_ids(raw, want)
    assert set(ids) == {"b2", "j1d", "w1d", "l"}
    assert dups == 3
