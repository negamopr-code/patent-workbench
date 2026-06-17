from patentbench import citations

# A miniature candidate text with the real shape of the EP3087655 B1 bug: the
# "can be stopped" sentence lives in [0025]; the model mis-cited it as [0029].
SRC = [{"number": "EP3087655", "text": (
    "[0024] An output of the wattmeter 72 is connected to the power storage device 4.\n\n"
    "[0025] By control of the switches 62 and 63, outputs of the PVPCSs 6 and 16 can be "
    "stopped. Note that the switches 62 and 63 are turned OFF.\n\n"
    "[0028] (a-1) When the system power is supplied to the power storage device 4.\n\n"
    "[0029] (a-3) Following that, the BATPCS 81 and the PVPCSs 6 and 16 are started.")}]


def test_corrects_wrong_paragraph_number():
    ans = 'switches 62/63 gate the ports — [0029]: "outputs of the PVPCSs 6 and 16 can be stopped".'
    out = citations.verify(ans, SRC)
    assert '[0025]: "outputs of the PVPCSs 6 and 16 can be stopped"' in out["answer"]
    assert "[0029]" not in out["answer"]
    assert out["corrections"] == [("[0029]", "[0025]", "EP3087655")]


def test_leaves_correct_citation_untouched():
    ans = '[0029]: "the BATPCS 81 and the PVPCSs 6 and 16 are started"'
    out = citations.verify(ans, SRC)
    assert out["answer"] == ans
    assert out["corrections"] == []


def test_range_kept_when_quote_inside_it():
    ans = '[0028]–[0029]: "the BATPCS 81 and the PVPCSs 6 and 16 are started"'
    out = citations.verify(ans, SRC)
    assert out["answer"] == ans          # true marker 0029 is within the range


def test_whitespace_and_case_insensitive_match():
    ans = '[0099]: "OUTPUTS of the   PVPCSs 6 and 16    can be stopped"'
    out = citations.verify(ans, SRC)
    assert '[0025]:' in out["answer"]


def test_flag_unfound_only_when_requested():
    ans = '[0030]: "this sentence appears in no paragraph whatsoever here"'
    # default: leave untouched (quote might be from an un-loaded doc)
    assert citations.verify(ans, SRC)["answer"] == ans
    # deep_map mode: the full text is present, so an unfindable quote is flagged
    flagged = citations.verify(ans, SRC, flag_unfound=True)
    assert flagged["answer"].endswith("⚠")


def test_ellipsis_quote_matches_longest_segment():
    ans = '[0001]: "By control of the switches 62 and 63 … turned OFF"'
    out = citations.verify(ans, SRC)
    assert "[0025]" in out["answer"]
