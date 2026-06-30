from patentbench import figures, fetcher
from bs4 import BeautifulSoup


def test_figure_url_parsing_per_office():
    html = """
    <img src="https://patentimages.storage.googleapis.com/aa/bb/US09455593-20160927-D00000.png">
    <img src="https://patentimages.storage.googleapis.com/cc/dd/US09455593-20160927-D00001.png">
    <img src="https://patentimages.storage.googleapis.com/ee/ff/US09455593-20160927-D00002.png">
    <img src="https://patentimages.storage.googleapis.com/11/22/imgf0001.png">
    <img src="https://patentimages.storage.googleapis.com/33/44/imgb0001.png">
    <img src="https://patentimages.storage.googleapis.com/55/66/imgf0001.png">
    """  # note: imgb = inline math (excluded); imgf0001 duplicated (deduped)
    soup = BeautifulSoup(html, "lxml")
    urls = fetcher._figure_urls(soup, html)
    names = [u.rsplit("/", 1)[-1] for u in urls]
    assert "imgf0001.png" in names
    assert "imgb0001.png" not in names                 # inline math excluded
    assert names.count("imgf0001.png") == 1            # deduped
    # US representative D00000 dropped because real sheets exist
    assert "US09455593-20160927-D00000.png" not in names
    assert "US09455593-20160927-D00001.png" in names


def test_drawings_block_merge_idempotent():
    figs = [{"n": 1, "caption": "[FIG. 1] A widget. Reference numerals: 10 = body."},
            {"n": 2, "caption": "[FIG. 2] A flow chart. Reference numerals: 20 = step."}]
    desc = "[0001] The invention.\n\n[0002] More text."
    merged = figures.merge_into_description(desc, figs)
    assert "[0001] The invention." in merged
    assert figures.DRAWINGS_HEADER in merged
    assert "[FIG. 1]" in merged and "[FIG. 2]" in merged
    # re-merging swaps the block, never stacks it
    again = figures.merge_into_description(merged, figs)
    assert again.count(figures.DRAWINGS_HEADER) == 1
    # stripping restores the original primary text
    assert figures.strip_block(again) == desc


def test_drawings_block_empty_when_no_captions():
    assert figures.drawings_block([{"n": 1, "caption": ""}]) == ""
    assert figures.merge_into_description("desc", [{"n": 1}]) == "desc"
