from patentbench import patents
from patentbench.fetcher import _publication_variants, _looks_truncated


def test_looks_truncated_detects_mid_sentence_cut():
    body = "".join(f"[{i:04d}] The apparatus does a thing. " for i in range(1, 60))
    # a properly-ended description is NOT flagged
    assert not _looks_truncated(body + "This concludes the description.")
    # the same body cut mid-clause (no terminator) IS flagged
    assert _looks_truncated(body + "the controller then proceeds to recalcul")
    # short fragments (abstract-sized) are never flagged, even without a terminator
    assert not _looks_truncated("a brief unterminated stub")
    # CJK sentence terminator counts as a proper ending
    assert not _looks_truncated(body + "これで終わり。")


def test_publication_variants_prefer_grant_for_same_number_offices():
    # kind-less EP/GB/CN → try the granted B1 first, fall back to the application
    assert _publication_variants("EP3087655") == ["EP3087655B1", "EP3087655"]
    assert _publication_variants("CN114853847") == ["CN114853847B1", "CN114853847"]
    # already kind-coded → use verbatim (caller asked for that exact publication)
    assert _publication_variants("EP3087655B1") == ["EP3087655B1"]
    assert _publication_variants("CN203205735U") == ["CN203205735U"]
    # different-number-grant / no-grant offices → never kind-substitute
    assert _publication_variants("US20160156193") == ["US20160156193"]
    assert _publication_variants("WO2022243179") == ["WO2022243179"]


def test_fetch_recovers_truncated_description_from_pdf(monkeypatch):
    from patentbench import fetcher

    # a scrape whose ONLY paragraph is body-sized but cut mid-sentence (no terminator)
    cut = "the controller then proceeds to recalcul" * 30   # >800, unterminated
    html = f'<html><meta name="DC.title" content="T">' \
           f'<description-paragraph num="0001">{cut}</description-paragraph></html>'

    class _Resp:
        status_code = 200
        text = html

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(fetcher.httpx, "Client", _Client)
    monkeypatch.setattr(fetcher, "_pdf_url", lambda soup, raw: "http://x/p.pdf")
    full = "[0001] The controller recalculates and rechecks the value. " * 60  # long, terminated
    monkeypatch.setattr(fetcher, "_description_from_pdf", lambda url: full)

    res = fetcher._fetch_publication("US20240225122A1")
    # the fuller PDF body replaced the truncated scrape
    assert "rechecks the value" in res["description"]
    assert res["description"].rstrip().endswith(".")


def test_fetch_keeps_good_description_over_shorter_pdf(monkeypatch):
    from patentbench import fetcher

    good = "".join(f'<description-paragraph num="{i:04d}">Para {i} ends cleanly. '
                   f'</description-paragraph>' for i in range(1, 80))
    html = f'<html><meta name="DC.title" content="T">{good}</html>'

    class _Resp:
        status_code = 200
        text = html

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(fetcher.httpx, "Client", _Client)
    # even if a PDF exists, a properly-terminated scrape must NOT be flagged/replaced
    called = {"pdf": False}
    monkeypatch.setattr(fetcher, "_pdf_url",
                        lambda soup, raw: called.__setitem__("pdf", True) or "http://x/p.pdf")
    monkeypatch.setattr(fetcher, "_description_from_pdf", lambda url: "short")

    res = fetcher._fetch_publication("US1")
    assert "[0001]" in res["description"] and "[0079]" in res["description"]
    assert called["pdf"] is False          # guard never even looked at the PDF


def test_extract_bare_numbers():
    text = "US10395648B1 and EP3667902A1, also CN114853847B."
    # trailing bare-letter kind codes are stripped by design (patent-wiki doctrine:
    # Google resolves the bare number to its newest kind-code variant)
    assert patents.extract_candidates(text) == ["US10395648B1", "EP3667902A1", "CN114853847"]


def test_extract_lowercase_numbers_accepted():
    # hand-typed lowercase must work the same as uppercase
    assert patents.extract_candidates("cn120200454") == ["CN120200454"]
    assert patents.extract_candidates("us10395648b1") == ["US10395648B1"]
    assert patents.extract_candidates("Cn 120200454 a") == ["CN120200454"]
    # but lowercase prose must still NOT be mistaken for a number
    assert patents.extract_candidates("in 2023 the 2024 report") == []


def test_extract_from_google_patents_url():
    url = "https://patents.google.com/patent/CN120638382A/en?oq=cn202510591293"
    nums = patents.extract_candidates(url)
    assert "CN120638382" in nums


def test_extract_from_espacenet_url():
    url = ("https://worldwide.espacenet.com/patent/search/family/096974018/"
           "publication/CN120638382A?q=pn%3DCN120638382A")
    nums = patents.extract_candidates(url)
    assert nums[0] == "CN120638382"
    assert nums.count("CN120638382") == 1  # deduped across URL paths and query


def test_us_app_leading_zero_canonicalization():
    assert patents.canonicalize("US2023278430") == "US20230278430"
    # grants and already-canonical app pubs untouched
    assert patents.canonicalize("US10395648B1") == "US10395648B1"
    assert patents.canonicalize("US20230278430A1") == "US20230278430A1"


def test_partial_kind_code_stripped():
    assert patents.canonicalize("JP4034816B") == "JP4034816"
    assert patents.canonicalize("JP6489547B2") == "JP6489547B2"


def test_spaces_and_separators_normalized():
    assert patents.canonicalize("US 2023/0278430 A1") == "US20230278430A1"


def test_implausible_rejected():
    assert patents.extract_candidates("AB12 short IN 2023 hello") == []


def test_links():
    l = patents.links("US10395648B1")
    assert l["google"] == "https://patents.google.com/patent/US10395648B1/en"
    assert "pn%3DUS10395648B1" in l["espacenet"]


def test_number_does_not_absorb_next_line_list_index():
    # regression: with \s in the separator class the match crossed the newline and
    # swallowed the next line's list numbering -> AU20201926863 (404). 2026-06-12.
    text = "6. US 2018/560640\n7. AU 2020/192686\n3. CN 114853847\n"
    nums = patents.extract_candidates(text)
    assert "US20180560640" in nums      # missing-zero rule now fires (10 digits)
    assert "AU2020192686" in nums
    assert "CN114853847" in nums
    assert not any(n.startswith("AU202019268") and len(n) > len("AU2020192686") for n in nums)


def test_two_pass_image_ocr_flags_disagreement(monkeypatch):
    from patentbench import claude_bridge, extract
    answers = iter(["US10395648B1\nCN119134413", "US10395648B1\nCN119334413"])
    monkeypatch.setattr(claude_bridge, "run_extract",
                        lambda *a, **k: {"answer": next(answers)})
    res = extract.numbers_from_image("/tmp/x.png")
    assert "US10395648B1" in res["numbers"]
    assert set(res["uncertain"]) == {"CN119134413", "CN119334413"}


def test_all_candidates_always_in_prompt():
    # regression: 34 docs x 9k per-doc cap > 260k total budget used to DROP the
    # last 5 candidates from chat ("5 did not fit the context budget"). 2026-06-12.
    from patentbench import claude_bridge as cb
    docs = [{"number": f"US{i:07}", "title": f"t{i}", "abstract": "a" * 20000}
            for i in range(60)]
    p = cb.build_prompt("q", documents=docs)
    for d in docs:
        assert d["number"] in p
    assert "did not fit" not in p
    assert "ALL 60 of them" in p


def test_parse_verdict():
    from patentbench import claude_bridge as cb
    v = cb.parse_verdict("MATCH SCORE: 9\nKEY FEATURES: A + B + C\nOVERLAP: x")
    assert v == {"score": 9.0, "features": "A + B + C"}
    assert cb.parse_verdict("MATCH SCORE: 7.5\nKEY FEATURES: none")["features"] is None
    assert cb.parse_verdict("no structure at all") == {"score": None, "features": None}
    assert cb.parse_verdict("MATCH SCORE: 55")["score"] == 10.0  # clamped
