#!/usr/bin/env python3
"""
Analyze fetched wikitext to understand the migration scope.

Produces a SURVEY.md summarizing:
- Template usage frequency (across Main namespace only)
- Parser-function usage frequency
- Extension-tag usage (`<ref>`, `<math>`, `<syntaxhighlight>`, etc.)
- Scribunto module invocations
- Semantic MediaWiki annotations
- Page size distribution
- Unusual page detection (very large pages, unbalanced braces)
"""

from __future__ import annotations

import re
import json
from collections import Counter
from pathlib import Path

INPUT = Path(__file__).resolve().parent.parent / "input"
PAGES = INPUT / "pages"
MAIN = PAGES / "Main"
TEMPLATE = PAGES / "Template"
MODULE = PAGES / "Module"

TEMPLATE_RE = re.compile(r"\{\{\s*([^|}{:][^|}]*?)\s*(?:\||\}\})")
PARSER_FN_RE = re.compile(r"\{\{\s*#(\w+)\s*:")
TAG_RE = re.compile(r"<(\w+)(?:\s[^>]*)?>")
INVOKE_RE = re.compile(r"\{\{\s*#invoke\s*:\s*([^|}\s]+)")
SMW_PROP_RE = re.compile(r"\[\[\s*([A-Z][^:\]]{0,40}?)\s*::")
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")


def read_pages(dir_path: Path) -> list[tuple[str, str]]:
    out = []
    if not dir_path.exists():
        return out
    for p in sorted(dir_path.rglob("*.wikitext")):
        out.append((p.name[:-len(".wikitext")], p.read_text(encoding="utf-8")))
    return out


def main() -> int:
    main_pages = read_pages(MAIN)
    template_pages = read_pages(TEMPLATE)
    module_pages = read_pages(MODULE)

    report = []
    report.append("# Wiki migration survey\n")
    report.append(f"Source: {(INPUT / 'meta' / 'siteinfo.json').read_text(encoding='utf-8')[:200]}...\n")
    report.append("## Totals\n")
    report.append(f"- Main articles: **{len(main_pages)}**")
    report.append(f"- Templates:     **{len(template_pages)}**")
    report.append(f"- Modules (Lua): **{len(module_pages)}**")

    # --- Page size distribution ---
    sizes = sorted(len(t) for _, t in main_pages)
    if sizes:
        import statistics
        report.append("\n## Main-article size distribution (bytes)\n")
        report.append(f"- Min:    {sizes[0]:>8,}")
        report.append(f"- Median: {statistics.median(sizes):>8,.0f}")
        report.append(f"- Mean:   {statistics.mean(sizes):>8,.0f}")
        report.append(f"- P95:    {sizes[int(len(sizes) * 0.95)]:>8,}")
        report.append(f"- Max:    {sizes[-1]:>8,}")
        report.append(f"- Total:  {sum(sizes):>8,}")

    # --- Template usage in main articles ---
    tpl_counts: Counter = Counter()
    pf_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    invoke_counts: Counter = Counter()
    smw_counts: Counter = Counter()

    for _name, text in main_pages:
        for m in TEMPLATE_RE.finditer(text):
            tpl = m.group(1).strip()
            # Skip parser functions (handled separately)
            if tpl.startswith("#"):
                continue
            # Normalize first letter (MediaWiki convention)
            if tpl:
                tpl = tpl[0].upper() + tpl[1:]
            tpl_counts[tpl] += 1
        for m in PARSER_FN_RE.finditer(text):
            pf_counts[m.group(1)] += 1
        for m in TAG_RE.finditer(text):
            tag_counts[m.group(1).lower()] += 1
        for m in INVOKE_RE.finditer(text):
            invoke_counts[m.group(1)] += 1
        for m in SMW_PROP_RE.finditer(text):
            smw_counts[m.group(1)] += 1

    report.append(f"\n## Template usage (in Main namespace only)\n")
    report.append(f"Distinct templates referenced: **{len(tpl_counts)}** "
                  f"(of {len(template_pages)} defined)")
    report.append(f"Total template invocations: **{sum(tpl_counts.values())}**")
    report.append(f"\nTop 30 most-used templates:\n")
    report.append("| Template | Count |")
    report.append("|---|---:|")
    for name, n in tpl_counts.most_common(30):
        report.append(f"| `{name}` | {n} |")

    # Unused templates (defined but never invoked from Main)
    defined_names = {name for name, _ in template_pages}
    referenced_names = {name.replace(" ", "_") for name in tpl_counts.keys()}
    unused = defined_names - referenced_names
    report.append(f"\n### Templates defined but not referenced from Main: "
                  f"**{len(unused)}**\n")
    report.append("(These may be invoked by other templates, not orphans.)\n")

    # --- Parser functions ---
    report.append("\n## Parser function usage (Main namespace)\n")
    if pf_counts:
        report.append("| Function | Count |")
        report.append("|---|---:|")
        for name, n in pf_counts.most_common():
            report.append(f"| `#{name}` | {n} |")
    else:
        report.append("_None found._")

    # --- Extension tags ---
    report.append("\n## Extension tag usage (Main namespace)\n")
    known_html = {"p", "div", "span", "br", "hr", "table", "tr", "td", "th",
                  "tbody", "thead", "tfoot", "ul", "ol", "li", "dl", "dt",
                  "dd", "i", "b", "em", "strong", "u", "s", "small", "big",
                  "h1", "h2", "h3", "h4", "h5", "h6", "a", "img", "pre",
                  "code", "blockquote", "center", "font", "tt", "strike",
                  "sub", "sup", "figure", "figcaption"}
    interesting = Counter({k: v for k, v in tag_counts.items()
                            if k not in known_html})
    if interesting:
        report.append("| Tag | Count |")
        report.append("|---|---:|")
        for name, n in interesting.most_common():
            report.append(f"| `<{name}>` | {n} |")
    else:
        report.append("_No non-HTML extension tags found._")

    # --- Module invocations ---
    report.append("\n## Scribunto module invocations (Main namespace)\n")
    if invoke_counts:
        report.append("Modules invoked directly from Main articles:\n")
        report.append("| Module | Count |")
        report.append("|---|---:|")
        for name, n in invoke_counts.most_common():
            report.append(f"| `Module:{name}` | {n} |")
    else:
        report.append("_No direct `#invoke` calls from Main. Modules are "
                      "likely invoked transitively via templates._")

    # Also check template invocations
    tpl_invoke_counts: Counter = Counter()
    for _name, text in template_pages:
        for m in INVOKE_RE.finditer(text):
            tpl_invoke_counts[m.group(1)] += 1
    report.append(f"\nModule invocations from Template namespace: "
                  f"**{sum(tpl_invoke_counts.values())}** "
                  f"across {len(tpl_invoke_counts)} distinct modules.\n")
    if tpl_invoke_counts:
        report.append("| Module | Count |")
        report.append("|---|---:|")
        for name, n in tpl_invoke_counts.most_common(15):
            report.append(f"| `Module:{name}` | {n} |")

    # --- SMW properties ---
    report.append("\n## Semantic MediaWiki property annotations (Main)\n")
    if smw_counts:
        report.append("Inline `[[Property::value]]` annotations:\n")
        report.append("| Property | Count |")
        report.append("|---|---:|")
        for name, n in smw_counts.most_common(20):
            report.append(f"| `{name}` | {n} |")
    else:
        report.append("_No inline SMW property annotations found._")

    # --- Large pages (candidates for careful review) ---
    report.append("\n## Large pages (likely complex)\n")
    large = sorted(main_pages, key=lambda x: -len(x[1]))[:10]
    report.append("| Page | Size (bytes) |")
    report.append("|---|---:|")
    for name, text in large:
        report.append(f"| `{name}` | {len(text):,} |")

    # --- Unbalanced braces (possible parsing hazards) ---
    hazards = []
    for name, text in main_pages:
        open_tpl = text.count("{{")
        close_tpl = text.count("}}")
        open_link = text.count("[[")
        close_link = text.count("]]")
        if open_tpl != close_tpl or open_link != close_link:
            hazards.append((name, open_tpl, close_tpl, open_link, close_link))
    if hazards:
        report.append(f"\n## Pages with unbalanced delimiters "
                      f"({len(hazards)})\n")
        report.append("| Page | {{ | }} | [[ | ]] |")
        report.append("|---|---:|---:|---:|---:|")
        for name, ot, ct, ol, cl in hazards[:20]:
            report.append(f"| `{name}` | {ot} | {ct} | {ol} | {cl} |")

    out = "\n".join(report) + "\n"
    dest = INPUT.parent / "SURVEY.md"
    dest.write_text(out, encoding="utf-8")
    print(f"Wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
