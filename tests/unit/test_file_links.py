"""[[File:...]] conversion."""
from mw2wm.convert import convert_text


def test_box_size_keeps_width_only():
    out = convert_text("[[File:Young Man.jpg|thumb|256x256px|A young man.]]", title="T")
    assert "width=256" in out and "height" not in out
    assert "(Young_Man.jpg)" in out  # original file name, relative


def test_plain_width():
    out = convert_text("[[File:Pic.png|thumb|300px|Cap]]", title="T")
    assert "width=300" in out and "height" not in out
