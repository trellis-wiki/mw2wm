"""Link rewriting when pages move into folders (--category-to-path)."""
from mw2wm.__main__ import _rewrite_links

INDEX = {"tiana_obenchain": "lore/tiana_obenchain", "main_page": "main_page",
         "great_houses": "realm/great_houses"}


def rewrite(text, folder="lore"):
    return _rewrite_links(text, {}, folder, INDEX)


def test_sibling_in_same_folder_stays_relative():
    assert rewrite("[[Tiana Obenchain]]") == "[[Tiana Obenchain]]"


def test_page_at_root_or_other_folder_gets_absolute_path():
    assert rewrite("[[Main Page]]") == "[Main Page](/main_page)"
    assert rewrite("[[Great Houses]]") == "[Great Houses](/realm/great_houses)"


def test_missing_page_stays_relative_so_it_lands_in_this_folder():
    assert rewrite("[[Victor Koval]]") == "[[Victor Koval]]"
    assert rewrite("[the emperor](Victor_Koval)") == "[the emperor](Victor_Koval)"


def test_images_are_not_rewritten_as_links():
    src = "![A young man](Young_Man_In_Town.jpg){.thumb width=256}"
    assert rewrite(src) == src
