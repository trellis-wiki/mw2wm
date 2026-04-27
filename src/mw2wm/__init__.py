"""mw2wm — MediaWiki → WikiMark converter.

Takes MediaWiki wikitext and produces WikiMark source that Trellis
(via wikimark-python) can render.

The library is designed for migrating one specific wiki at a time,
not as a general-purpose MediaWiki converter. Features that the
source wiki doesn't use aren't implemented. See ``PLAN.md`` for the
scope driver.
"""

from .convert import convert_page, convert_text

__all__ = ["convert_page", "convert_text"]
__version__ = "0.1.0"
