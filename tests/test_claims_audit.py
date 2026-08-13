"""🧾 Claims audit: the pure functions — FEATURE-block parsing, the code-side
quote-verification trust layer and the stage-2b pairs helpers. No NLM bridge,
no Claude, no network."""
from patentbench.web.api import (_claims_pairs_round, _claims_pairs_spec,
                                 _claims_parse, _quote_norm, _quote_verify)


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


def test_parse_quotes_free_lines():
    # stage-1/2a mode: numbers only, no '::' — parser yields empty quotes
    ans = ("FEATURE 1: CN117039286, CN118156696A, US20220158279\n"
           "FEATURE 2: NONE\n"
           "FEATURE 3: EP4340163A1\n")
    claims, unmatched = _claims_parse(ans, KEY_MAP, n_must=7)
    assert set(claims[1].keys()) == {1} and claims[1][1] == ""
    assert set(claims) == {1, 2, 3, 4}
    assert claims[4] == {3: ""}
    assert unmatched == []


def test_quote_hallucination_rejected():
    assert _quote_verify(
        "a completely different sentence about steel welding processes and ferrite",
        HAY) == "unverified"
    assert _quote_verify("", HAY) == "unverified"
    assert _quote_verify("short", HAY) == "unverified"


MUST_A = [["insulating layer between poles", 5], ["current collector disk", 3],
          ["clamping groove", 1]]
DOCS = {1: {"number": "CN117039286"}, 2: {"number": "CN118156696"},
        3: {"number": "US20220158279"}}
PAIRS = {1: ["1", "2"], 2: ["2"], 3: ["3"]}


def test_pairs_spec_lists_only_roster_claims():
    spec = _claims_pairs_spec(MUST_A, PAIRS, [1, 2], DOCS)
    # feature 1: only doc 1 claims it; feature 2: docs 1+2; feature 3: doc 3
    # is NOT in the roster → the whole block is omitted
    assert "1. insulating layer between poles (importance 5/5) — check ONLY: " \
           "CN117039286" in spec
    assert "2. current collector disk (importance 3/5) — check ONLY: " \
           "CN117039286, CN118156696" in spec
    assert "clamping groove" not in spec


def test_pairs_round_verdicts_replace_claims():
    hay = {1: HAY, 2: HAY}
    parsed = {1: {1: "an insulating layer is arranged between the inner pole "
                     "and the current collector disk"},
              2: {2: "NO"}}                    # NLM refuses doc 2's feature-2 claim
    out = _claims_pairs_round(parsed, PAIRS, [1, 2], hay)
    assert out["1"]["1"][0] == "verified"
    # asked but unanswered pair (doc 1 × feature 2) → the 2a claim dies
    assert out["1"]["2"][0] == "unverified"
    # ':: NO' is far under the 10-char quote floor → unverified, quote kept as-is
    assert out["2"]["2"][0] == "unverified"
    # roster-scoped: doc 3 was not in this round → untouched
    assert "3" not in out
