"""Headings: MediaWiki's == section == is WikiMark's top-level # heading."""
from mw2wm.convert import convert_text


def heads(wikitext):
    out = convert_text(wikitext, title="T")
    return [line for line in out.splitlines() if line.startswith("#")]


def test_section_levels_move_up_one():
    assert heads("Intro.\n\n== History ==\nA.\n\n=== Early ===\nB.\n\n==== Detail ====\nC.\n") == [
        "# History", "## Early", "### Detail"]


def test_level_one_is_also_top_level():
    assert heads("= Rare =\nA.\n\n== Usual ==\nB.\n") == ["# Rare", "# Usual"]


def test_deepest_level():
    assert heads("====== Six ======\nA.\n") == ["##### Six"]
