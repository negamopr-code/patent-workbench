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
