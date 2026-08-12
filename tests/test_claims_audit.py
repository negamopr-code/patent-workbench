"""🧾 Claims audit: the pure functions — FEATURE-block parsing and the code-side
quote-verification trust layer. No NLM bridge, no Claude, no network."""
from patentbench.web.api import _claims_parse, _quote_verify, _quote_norm


KEY_MAP = {"CN117039286": 1, "CN118156696": 2, "US20220158279": 3, "EP4340163": 4}

ANSWER = """Here is the assessment of the mandatory features.

FEATURE 1:
- CN117039286 :: "the outer pole is injection-molded integrally with the cover body"
- CN118156696A :: "pole 112 formed in the cover plate by insert injection molding"
FEATURE 2:
- NONE
FEATURE 3:
- US20220158279 :: "an inner terminal separated from the outer terminal by a gap"
- CN999999999 :: "a hallucinated document number"
FEATURE 4:
- EP4340163A1
FEATURE 9:
- CN117039286 :: "feature index out of range must be ignored"
"""


def test_parse_blocks_and_kind_codes():
    claims, unmatched = _claims_parse(ANSWER, KEY_MAP, n_must=7)
    # kind-code-insensitive: CN118156696A matches stored CN118156696
    assert claims[1][1].startswith("the outer pole is injection-molded")
    assert claims[2][1].startswith("pole 112 formed")
    assert claims[3][3].startswith("an inner terminal")
    # a claim line without '::' still counts, with an empty quote
    assert claims[4][4] == ""
    # NONE lines claim nothing; out-of-range feature indices are dropped
    assert all(2 not in feats for feats in claims.values())
    assert all(9 not in feats for feats in claims.values())
    # hallucinated numbers are reported, never claimed
    assert "CN999999999" in unmatched


def test_parse_ignores_numbers_inside_quotes():
    ans = 'FEATURE 1:\n- CN117039286 :: "similar to the one in US20220158279"\n'
    claims, unmatched = _claims_parse(ans, KEY_MAP, n_must=7)
    assert 1 in claims[1]
    assert 3 not in claims          # the quoted US number must not become a claim
    assert unmatched == []


def test_parse_empty_and_garbage():
    assert _claims_parse("", KEY_MAP, 7) == ({}, [])
    assert _claims_parse("no structure at all", KEY_MAP, 7) == ({}, [])


HAY = _quote_norm(
    "A battery cover plate, wherein the outer pole (112) is injection-molded "
    "integrally with the cover body (100), and an insulating layer is arranged "
    "between the inner pole and the current collector disk.")


def test_quote_verified_exact_modulo_punctuation():
    assert _quote_verify(
        "the OUTER POLE (112), is injection-molded integrally with the cover body",
        HAY) == "verified"


def test_quote_fuzzy_survives_small_noise():
    # one word altered mid-quote → not a substring, but most 4-gram shingles hit
    q = ("the outer pole 112 is injection-moulded integrally with the cover "
         "body 100 and an insulating layer is arranged")
    assert _quote_verify(q, HAY) in ("verified", "fuzzy")


def test_quote_hallucination_rejected():
    assert _quote_verify(
        "a completely different sentence about steel welding processes and ferrite",
        HAY) == "unverified"
    assert _quote_verify("", HAY) == "unverified"
    assert _quote_verify("short", HAY) == "unverified"
