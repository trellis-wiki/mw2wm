"""
MediaWiki template call → WikiMark template call.

The actively-used templates from a typical MediaWiki survey (see
``mw2wm/SURVEY.md``) map one-to-one to Trellis built-in modules.
For each known template we record:

- The target module name (what appears in the WikiMark output)
- A positional-arg name map (MediaWiki templates often take
  numbered positional args; WikiMark + the built-in modules want
  named kwargs)
- An optional filter that drops unwanted keys or renames fields
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class TemplateMapping:
    """How to convert one MediaWiki template to a WikiMark call."""

    target: str | None  # None = drop entirely (handled elsewhere or no-op)
    positional: dict[str, str] = field(default_factory=dict)
    rename: dict[str, str] = field(default_factory=dict)
    drop_keys: set[str] = field(default_factory=set)

    def apply(self, args: Mapping[str, str]) -> dict[str, str]:
        """Apply positional renaming + field renaming + drops.
        Returns a new dict of args ready for WikiMark emission.
        """
        out: dict[str, str] = {}
        for key, value in args.items():
            if key in self.drop_keys:
                continue
            new_key = self.positional.get(key, self.rename.get(key, key))
            out[new_key] = value
        return out


# Mapping of MediaWiki template names (canonical first-letter-upper form)
# to their WikiMark built-in equivalents. Anything not here is treated
# as unknown and logged to the conversion report.
TEMPLATE_MAPPINGS: dict[str, TemplateMapping] = {
    # Navigation
    "Hatnote": TemplateMapping(
        target="hatnote",
        positional={"1": "text"},
    ),
    # Common in worldbuilding wikis
    "Timeline event": TemplateMapping(
        target="timeline-event",
        positional={"1": "label", "2": "date"},
    ),
    # Personal data
    "Birth date and age": TemplateMapping(
        target="birth-date-and-age",
        positional={"1": "year", "2": "month", "3": "day"},
    ),
    # Infoboxes — positional args uncommon; mostly named
    "Infobox officeholder": TemplateMapping(target="infobox-officeholder"),
    "Infobox settlement": TemplateMapping(target="infobox-settlement"),
    "Infobox country": TemplateMapping(target="infobox-country"),
    "Infobox person": TemplateMapping(target="infobox-person"),
    "Infobox": TemplateMapping(target="infobox"),
    # Formatting
    "Lang": TemplateMapping(
        target="lang",
        positional={"1": "code", "2": "text"},
    ),
    "Coord": TemplateMapping(
        target="coord",
        positional={"1": "lat", "2": "lon"},
    ),
    "URL": TemplateMapping(
        target="url",
        positional={"1": "href", "2": "text"},
    ),
    "Quote": TemplateMapping(
        target="quote",
        positional={"1": "text", "2": "source", "3": "date"},
    ),
    "Tl": TemplateMapping(
        target="tl",
        positional={"1": "name"},
    ),
    # Metadata-only — handled via frontmatter elsewhere; call site drops
    "Italic title": TemplateMapping(target=None),
}


def lookup(name: str) -> TemplateMapping | None:
    """Look up a template by its MediaWiki-canonical name.

    Handles the first-letter-uppercase + spaces (not underscores)
    convention MediaWiki canonicalizes template calls to.
    """
    normalized = name.strip().replace("_", " ")
    if normalized:
        normalized = normalized[0].upper() + normalized[1:]
    return TEMPLATE_MAPPINGS.get(normalized)


# Template names whose entire call should be silently dropped
# from the output (handled via frontmatter instead of inline).
FRONTMATTER_TEMPLATES = {"Italic title"}
