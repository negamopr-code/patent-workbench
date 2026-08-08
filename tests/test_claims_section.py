"""🔬 _claims_section — a full-document benchmark decomposes from its CLAIMS section,
not from 35k chars of description (slow, timeout-prone, noisy). Extraction is
line-anchored (prose MENTIONING claims never triggers) and falls back to None so the
caller can use the full text when no section is found."""
from patentbench.web.api import _claims_section

_CN_DOC = (
    "— page 1 —\n(12) 按照专利合作条约所公布的国际申请\n\n"
    "需要声明的是，本申请说明书和权利要求书中的术语，其目的仅是为了描述特定实施例。\n\n"
    + "描述正文。\n" * 40
    + "— page 19 —\n权利要求书\n\n"
    "1. 一种电池触发设备，其特征在于，包括：触发处理器。\n\n"
    + "2. 根据权利要求1所述的设备，其特征在于，还包括无线通信模块。\n" * 10
    + "— page 26 —\nTRANSLATION\n\nINTERNATIONAL SEARCH REPORT\n\n"
    "| X | CN 115166523 A | Relevant to claim No. 1 |\n"
)

_US_DOC = (
    "TITLE\nBattery apparatus\n\nDESCRIPTION\n" + "The invention relates to widgets.\n" * 30
    + "WHAT IS CLAIMED IS:\n\n1. An apparatus comprising a processor.\n"
    + "2. The apparatus of claim 1, further comprising a memory unit for storage.\n" * 8
    + "ABSTRACT\nAn apparatus is disclosed.\n"
)


def test_extracts_cn_claims_section_between_heading_and_search_report():
    sec = _claims_section(_CN_DOC)
    assert sec is not None
    assert sec.startswith("1. 一种电池触发设备")
    assert "INTERNATIONAL SEARCH REPORT" not in sec
    assert "描述正文" not in sec                      # description stays out
    assert "说明书和权利要求书中的术语" not in sec      # prose mention never triggers


def test_extracts_us_claims_until_abstract():
    sec = _claims_section(_US_DOC)
    assert sec is not None
    assert sec.startswith("1. An apparatus")
    assert "ABSTRACT" not in sec and "widgets" not in sec


def test_no_heading_or_tiny_section_falls_back_to_none():
    assert _claims_section("just a short text without any claims heading") is None
    assert _claims_section("") is None
    # heading present but section implausibly short → None (caller uses full text)
    assert _claims_section("CLAIMS\n1. x.\nABSTRACT\ndone") is None


def test_decompose_whole_uses_description_and_keeps_existing_features(client, monkeypatch):
    """🏅 source='whole': the REST of the document — description minus the claims
    section — becomes W bonus elements (comparison points for close calls); existing
    features ride through untouched and the claims stay out of the prompt."""
    import patentbench.db as db
    from patentbench import claude_bridge as cb

    sent = {}

    def fake_dec(text, model=None, claims=False):
        sent["text"] = text
        return {"elements": [{"name": "cooling fins near the core", "weight": 2}],
                "model": "m"}

    monkeypatch.setattr(cb, "decompose_claim", fake_dec)
    tid = client.post("/api/tabs", json={"name": "DecW"}).json()["id"]
    client.post(f"/api/tabs/{tid}/benchmark/features", json={"title": "t", "features": [
        {"name": "claim-derived M", "weight": 5, "kind": "M", "sl": 5}]})
    full = ("INTRO description of the invention core. " * 10
            + "\nCLAIMS\n1. An apparatus with a processor.\n"
            + "2. The apparatus of claim 1 with a memory unit.\n" * 8
            + "\nABSTRACT\nshort abstract.")
    db.update_benchmark(tid, text=full, status="ready")

    r = client.post(f"/api/tabs/{tid}/benchmark/decompose", json={"source": "whole"}).json()
    assert "An apparatus with a processor" not in sent["text"]     # claims stayed out
    assert "INTRO description" in sent["text"]                     # description went in
    assert [e["name"] for e in r["elements"]][0] == "claim-derived M"   # kept, first
    w = [e for e in r["elements"] if e["kind"] == "W"]
    assert [e["name"] for e in w] == ["cooling fins near the core"]
    assert r["whole"] == 1 and r["mandatory"] == 1
