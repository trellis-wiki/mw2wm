"""
Conversion report: accumulates unhandled/unsupported constructs
during a mw2wm run and emits a human-readable summary.

The report is the single mechanism we use to decide when a
migration is "good enough" — every entry is something a human
should eyeball before going live. Empty report = clean conversion.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class Entry:
    """One thing that couldn't be auto-converted."""

    kind: str          # "unknown-template", "unsupported-tag", etc.
    page: str          # page title where it was found
    detail: str        # template name, tag name, snippet, ...
    location: str = ""  # optional source-line info


@dataclass
class Report:
    entries: list[Entry] = field(default_factory=list)

    def add(self, kind: str, page: str, detail: str, location: str = "") -> None:
        self.entries.append(Entry(kind=kind, page=page, detail=detail, location=location))

    def by_kind(self) -> dict[str, list[Entry]]:
        out: dict[str, list[Entry]] = {}
        for e in self.entries:
            out.setdefault(e.kind, []).append(e)
        return out

    def summary(self) -> dict[str, int]:
        return dict(Counter(e.kind for e in self.entries))

    def render_markdown(self) -> str:
        """Produce a CONVERSION.md body summarizing the run."""
        lines = ["# mw2wm conversion report\n"]
        if not self.entries:
            lines.append("_Clean run — no unhandled constructs._\n")
            return "\n".join(lines)

        lines.append(f"Total unhandled constructs: **{len(self.entries)}**\n")
        lines.append("## Summary\n")
        lines.append("| Kind | Count |")
        lines.append("|---|---:|")
        for kind, n in sorted(self.summary().items(), key=lambda x: -x[1]):
            lines.append(f"| `{kind}` | {n} |")
        lines.append("")

        grouped = self.by_kind()
        for kind in sorted(grouped.keys()):
            entries = grouped[kind]
            lines.append(f"## `{kind}` ({len(entries)})\n")
            # Collapse by detail + page for compact output
            seen: Counter[tuple[str, str]] = Counter()
            for e in entries:
                seen[(e.detail, e.page)] += 1
            lines.append("| Detail | Page | Count |")
            lines.append("|---|---|---:|")
            for (detail, page), n in sorted(seen.items(), key=lambda x: -x[1]):
                lines.append(f"| `{detail}` | `{page}` | {n} |")
            lines.append("")

        return "\n".join(lines) + "\n"
