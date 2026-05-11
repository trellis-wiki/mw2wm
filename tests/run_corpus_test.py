#!/usr/bin/env python3
"""Run mw2wm conversion on the test corpus and report results.

Converts all pages in the corpus through mw2wm and checks for:
- Conversion errors (exceptions)
- MediaWiki syntax leaks (<noinclude>, <includeonly>, {{{arg}}}, etc.)
- Unconverted constructs (DOES NOT SUPPORT)
- Output quality (non-empty, has content)

Usage:
    python tests/run_corpus_test.py [--corpus tests/corpus]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mw2wm import convert_page
from mw2wm.convert import _build_template_library


@dataclass
class PageResult:
    wiki: str
    title: str
    is_template: bool
    success: bool
    error: str = ""
    output_len: int = 0
    has_frontmatter: bool = False
    convert_time_ms: float = 0
    # Leak checks
    noinclude_leak: int = 0
    includeonly_leak: int = 0
    templatedata_leak: int = 0
    mw_arg_leak: int = 0  # {{{arg}}}
    documentation_leak: int = 0
    magic_word_leak: int = 0  # __NOTOC__ etc in body
    # Quality checks
    does_not_support_count: int = 0
    empty_body: bool = False


@dataclass
class CorpusResults:
    pages: list[PageResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.pages)

    @property
    def content_pages(self) -> list[PageResult]:
        return [p for p in self.pages if not p.is_template]

    @property
    def template_pages(self) -> list[PageResult]:
        return [p for p in self.pages if p.is_template]

    @property
    def errors(self) -> list[PageResult]:
        return [p for p in self.pages if not p.success]

    @property
    def content_with_leaks(self) -> list[PageResult]:
        return [p for p in self.content_pages if
                p.noinclude_leak or p.includeonly_leak or
                p.templatedata_leak or p.mw_arg_leak or
                p.documentation_leak or p.magic_word_leak]


_MW_ARG_RE = re.compile(r"\{{3}[^}]+\}{3}")
_MAGIC_RE = re.compile(r"__(?:NOTOC|TOC|FORCETOC|NOEDITSECTION)__")


def check_page(wiki_id: str, wikitext: str, title: str,
               is_template: bool, template_library: dict) -> PageResult:
    result = PageResult(wiki=wiki_id, title=title, is_template=is_template,
                        success=True)

    t0 = time.monotonic()
    try:
        page = convert_page(wikitext, title=title,
                           template_library=template_library)
        output = page.to_wikimark()
    except Exception as e:
        result.success = False
        result.error = str(e)
        result.convert_time_ms = (time.monotonic() - t0) * 1000
        return result

    result.convert_time_ms = (time.monotonic() - t0) * 1000
    result.output_len = len(output)
    result.has_frontmatter = output.startswith("---")
    is_redirect = page.redirect is not None

    # Strip frontmatter for body checks
    body = output
    if body.startswith("---"):
        end = body.find("---", 3)
        if end != -1:
            body = body[end + 3:]

    result.empty_body = len(body.strip()) == 0 and not is_redirect
    result.noinclude_leak = body.count("<noinclude")
    result.includeonly_leak = body.count("<includeonly")
    result.templatedata_leak = body.count("<templatedata")
    result.mw_arg_leak = len(_MW_ARG_RE.findall(body))
    result.documentation_leak = body.lower().count("{{documentation")
    result.magic_word_leak = len(_MAGIC_RE.findall(body))
    result.does_not_support_count = body.count("DOES NOT SUPPORT AUTOMATIC CONVERSION")

    return result


def run_wiki(wiki_id: str, wiki_dir: Path) -> list[PageResult]:
    results = []

    # Build template library
    tpl_dir = wiki_dir / "Template"
    template_library = _build_template_library(wiki_dir) if tpl_dir.is_dir() else {}

    # Convert content pages
    for f in sorted(wiki_dir.glob("*.wikitext")):
        title = f.stem.replace("_", " ")
        wikitext = f.read_text(encoding="utf-8")
        result = check_page(wiki_id, wikitext, f"Main / {f.stem}",
                           is_template=False, template_library=template_library)
        results.append(result)

    # Convert templates
    if tpl_dir.is_dir():
        for f in sorted(tpl_dir.glob("*.wikitext")):
            title = f.stem.replace("_", " ")
            wikitext = f.read_text(encoding="utf-8")
            result = check_page(wiki_id, wikitext, f"Template / {f.stem}",
                               is_template=True, template_library=template_library)
            results.append(result)

    return results


def print_report(results: CorpusResults):
    print(f"\n{'='*70}")
    print("MW2WM CORPUS TEST REPORT")
    print(f"{'='*70}\n")

    # Summary
    content = results.content_pages
    templates = results.template_pages
    errors = results.errors

    print(f"Total pages:     {results.total}")
    print(f"  Content pages: {len(content)}")
    print(f"  Templates:     {len(templates)}")
    print(f"  Errors:        {len(errors)}")
    print()

    # Conversion errors
    if errors:
        print(f"CONVERSION ERRORS ({len(errors)}):")
        for p in errors[:20]:
            print(f"  {p.wiki}/{p.title}: {p.error[:80]}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        print()

    # Content page quality
    print("CONTENT PAGE QUALITY:")
    leaky = results.content_with_leaks
    empty = [p for p in content if p.empty_body and p.success]
    dns = [p for p in content if p.does_not_support_count > 0]

    print(f"  Clean:                {len(content) - len(leaky) - len(empty)}")
    print(f"  With MW syntax leaks: {len(leaky)}")
    print(f"  Empty body:           {len(empty)}")
    print(f"  Has DOES NOT SUPPORT: {len(dns)}")
    print()

    if leaky:
        print("  LEAKS IN CONTENT PAGES:")
        for p in leaky[:15]:
            parts = []
            if p.noinclude_leak: parts.append(f"noinclude:{p.noinclude_leak}")
            if p.includeonly_leak: parts.append(f"includeonly:{p.includeonly_leak}")
            if p.templatedata_leak: parts.append(f"templatedata:{p.templatedata_leak}")
            if p.mw_arg_leak: parts.append(f"{{{{{{{'}}}'}}}:{p.mw_arg_leak}")
            if p.documentation_leak: parts.append(f"documentation:{p.documentation_leak}")
            if p.magic_word_leak: parts.append(f"magic:{p.magic_word_leak}")
            print(f"    {p.wiki}/{p.title}: {', '.join(parts)}")
        if len(leaky) > 15:
            print(f"    ... and {len(leaky) - 15} more")
        print()

    # Template quality
    print("TEMPLATE QUALITY:")
    tpl_dns = [p for p in templates if p.does_not_support_count > 0]
    tpl_with_inputs = [p for p in templates if p.has_frontmatter]
    tpl_mw_args = [p for p in templates if p.mw_arg_leak > 0]

    print(f"  With frontmatter:     {len(tpl_with_inputs)}")
    print(f"  Module-backed (DNS):  {len(tpl_dns)}")
    print(f"  With {{{'{{{'}}} leaks:  {len(tpl_mw_args)}")
    print()

    # Performance
    times = [p.convert_time_ms for p in results.pages if p.success]
    if times:
        avg = sum(times) / len(times)
        p95 = sorted(times)[int(len(times) * 0.95)]
        mx = max(times)
        slowest = max((p for p in results.pages if p.success),
                      key=lambda p: p.convert_time_ms)
        print("PERFORMANCE:")
        print(f"  Average: {avg:.0f}ms")
        print(f"  P95:     {p95:.0f}ms")
        print(f"  Max:     {mx:.0f}ms ({slowest.wiki}/{slowest.title})")
        print()

    # Per-wiki breakdown
    print("PER-WIKI BREAKDOWN:")
    wiki_ids = sorted(set(p.wiki for p in results.pages))
    for wiki_id in wiki_ids:
        wp = [p for p in results.pages if p.wiki == wiki_id]
        wc = [p for p in wp if not p.is_template]
        wt = [p for p in wp if p.is_template]
        we = [p for p in wp if not p.success]
        wl = [p for p in wc if
              p.noinclude_leak or p.includeonly_leak or p.mw_arg_leak]
        print(f"  {wiki_id}: {len(wc)} pages, {len(wt)} templates, "
              f"{len(we)} errors, {len(wl)} content leaks")
    print()

    # Final verdict
    content_clean = len(content) - len(leaky) - len(empty)
    content_total = len(content) if content else 1
    pct = content_clean / content_total * 100

    print(f"{'='*70}")
    if pct == 100 and not errors:
        print("PASS — 100% content pages clean, 0 conversion errors")
    elif pct >= 95:
        print(f"MOSTLY CLEAN — {pct:.1f}% content pages clean, {len(errors)} errors")
    else:
        print(f"NEEDS WORK — {pct:.1f}% content pages clean, {len(errors)} errors")
    print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(description="Run mw2wm corpus test")
    parser.add_argument("--corpus", type=Path, default=Path("tests/corpus"),
                        help="Corpus directory (default: tests/corpus)")
    parser.add_argument("--wiki", type=str, default=None,
                        help="Test only this wiki")
    args = parser.parse_args()

    if not args.corpus.is_dir():
        print(f"Corpus not found at {args.corpus}. Run fetch_corpus.py first.",
              file=sys.stderr)
        return 1

    results = CorpusResults()

    pages_dir = args.corpus / "pages"
    for wiki_dir in sorted(pages_dir.iterdir()):
        if not wiki_dir.is_dir():
            continue
        wiki_id = wiki_dir.name
        if args.wiki and wiki_id != args.wiki:
            continue
        print(f"Testing {wiki_id}...")
        wiki_results = run_wiki(wiki_id, wiki_dir)
        results.pages.extend(wiki_results)
        print(f"  {len(wiki_results)} pages tested")

    print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
