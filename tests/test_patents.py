from patentbench import patents


def test_extract_bare_numbers():
    text = "US10395648B1 and EP3667902A1, also CN114853847B."
    # trailing bare-letter kind codes are stripped by design (patent-wiki doctrine:
    # Google resolves the bare number to its newest kind-code variant)
    assert patents.extract_candidates(text) == ["US10395648B1", "EP3667902A1", "CN114853847"]


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
