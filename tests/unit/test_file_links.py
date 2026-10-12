"""[[File:...]] conversion."""
from mw2wm.convert import convert_text


def test_box_size_keeps_width_only():
    out = convert_text("[[File:Young Man.jpg|thumb|256x256px|A young man.]]", title="T")
    assert "width=256" in out and "height" not in out
    assert "(Young_Man.jpg)" in out  # original file name, relative


def test_plain_width():
    out = convert_text("[[File:Pic.png|thumb|300px|Cap]]", title="T")
    assert "width=300" in out and "height" not in out


def test_thumb_gets_its_own_paragraph_so_it_renders_as_a_captioned_figure():
    out = convert_text("Text before.\n[[File:Pic.jpg|thumb|300px|A caption.]]\nText after.", title="T")
    assert "\n\n![A caption.](Pic.jpg){.thumb width=300}\n\n" in out


def test_subpage_links_point_into_the_page_folder():
    out = convert_text("* [[/Chapter 1]]\n* [[/Prologue|The prologue]]", title="Book / The_Tower")
    assert "[Chapter 1](./the_tower/chapter_1)" in out
    assert "[The prologue](./the_tower/prologue)" in out
