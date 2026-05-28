"""
mw2wm CLI.

Usage::

    python -m mw2wm build <input-dir> <output-dir>
    python -m mw2wm build <input-dir> <output-dir> \\
        --category-to-path Arthagog:preserve \\
        --category-to-path Giovoria:replace

Walks ``<input-dir>/pages/`` for ``.wikitext`` files, converts each
through :func:`mw2wm.convert_page`, and writes ``.wm`` output using
WikiMark directory conventions:

- ``pages/``       — content pages (MediaWiki Main namespace → root)
- ``templates/``   — template pages
- ``categories/``  — category pages
- ``assets/``      — images and files

Other MediaWiki namespaces (File, Module, MediaWiki, etc.) are
infrastructure and not converted.

The ``--category-to-path`` flag moves pages in the given category
into a folder path (e.g. pages in category "Arthagog" move to
``pages/arthagog/``).  Mode is ``preserve`` (keep the category in
frontmatter) or ``replace`` (remove it).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from dataclasses import dataclass

from . import convert_page
from .convert import _build_template_library, _emit_frontmatter, _md_link_target
from .report import Report
from .templates import load_plugins, reset_plugins


@dataclass
class CategoryPathRule:
    """A rule mapping a category to a folder path."""
    category: str
    folder: str
    preserve: bool

    @classmethod
    def parse(cls, spec: str) -> "CategoryPathRule":
        if ":" not in spec:
            raise argparse.ArgumentTypeError(
                f"Expected 'Category:preserve' or 'Category:replace', got '{spec}'"
            )
        cat, _, mode = spec.rpartition(":")
        mode = mode.lower().strip()
        if mode not in ("preserve", "replace"):
            raise argparse.ArgumentTypeError(
                f"Mode must be 'preserve' or 'replace', got '{mode}'"
            )
        folder = cat.strip().lower().replace(" ", "_")
        return cls(category=cat.strip(), folder=folder, preserve=(mode == "preserve"))

# MediaWiki namespaces → WikiMark output directories.
# None = drop (infrastructure, not content).
_NS_MAP: dict[str, str | None] = {
    "Main": "pages",
    "Template": "templates",
    "Category": "categories",
    "Book": "pages/Book",
}
# Everything not listed is dropped with a report entry.


def _output_path_for(
    rel: Path, pages_src: Path, output_dir: Path,
) -> tuple[Path | None, str]:
    """Map a MediaWiki namespace path to a WikiMark output path.

    Returns (output_path, title) or (None, title) if the namespace
    should be dropped.
    """
    parts = rel.parts
    if not parts:
        return None, str(rel)

    mw_ns = parts[0]
    rest = Path(*parts[1:]) if len(parts) > 1 else Path(rel.stem)

    target_dir = _NS_MAP.get(mw_ns)
    if target_dir is None and mw_ns not in _NS_MAP:
        return None, f"{mw_ns} / {rest.with_suffix('').as_posix()}"

    if target_dir is None:
        return None, f"{mw_ns} / {rest.with_suffix('').as_posix()}"

    # Build the display title from the original filename
    if mw_ns == "Main":
        title = rest.with_suffix("").as_posix().replace("/", " / ")
    else:
        title = f"{mw_ns} / {rest.with_suffix('').as_posix()}"

    # Normalize the output filename
    raw = rest.with_suffix("").as_posix()
    if target_dir in ("templates", "categories"):
        # Template/category names use kebab-case to match call syntax
        normalized = raw.lower().replace(" ", "-").replace("_", "-") + ".wm"
    else:
        # Content pages: lowercase with underscores for spaces
        normalized = raw.lower().replace(" ", "_") + ".wm"
    out = output_dir / target_dir / normalized

    return out, title


_MW_SIDEBAR_MAGIC = {
    "SEARCH", "TOOLBOX", "LANGUAGES",
}

_MW_SIDEBAR_SYSTEM_LINKS = {
    "mainpage", "mainpage-description",
    "recentchanges-url", "recentchanges",
    "randompage-url", "randompage",
    "helppage", "help",
}

_MW_SPECIAL_MAP = {
    "Special:AllPages": ("/trellis/all-pages", "All Pages"),
    "Special:WantedPages": ("/trellis/wanted-pages", "Wanted Pages"),
    "Special:UncategorizedPages": ("/trellis/uncategorized-pages", "Uncategorized Pages"),
    "Special:SpecialPages": ("/trellis/dashboard", "Dashboard"),
    "Special:RecentChanges": ("/trellis/dashboard", "Dashboard"),
}


def _convert_mw_sidebar(source: str) -> str:
    """Convert a MediaWiki:Sidebar page to Trellis _trellis/sidebar.wm format."""
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_links: list[str] = []

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("** ") or stripped.startswith("**"):
            entry = stripped.lstrip("*").strip()
            if "|" in entry:
                target, display = entry.split("|", 1)
                target, display = target.strip(), display.strip()
            else:
                target = display = entry.strip()

            if target.lower() in _MW_SIDEBAR_SYSTEM_LINKS:
                continue
            if display.lower() in _MW_SIDEBAR_SYSTEM_LINKS:
                continue

            mapped = _MW_SPECIAL_MAP.get(target)
            if mapped:
                current_links.append(f"* [{mapped[1]}]({mapped[0]})")
                continue

            if target.startswith("Special:") or target.startswith("MediaWiki:"):
                continue

            page_path = target.lower().replace(" ", "_")
            current_links.append(f"* [{display}]({page_path})")

        elif stripped.startswith("* ") or stripped.startswith("*"):
            heading = stripped.lstrip("*").strip()
            if heading.upper() in _MW_SIDEBAR_MAGIC:
                continue
            if heading.lower() == "navigation":
                if current_links:
                    sections.append((current_heading, current_links))
                current_heading = None
                current_links = []
                current_links.append("* [Main Page](main_page)")
                continue

            if current_links or current_heading is not None:
                sections.append((current_heading, current_links))
            current_heading = heading
            current_links = []

    if current_links or current_heading is not None:
        sections.append((current_heading, current_links))

    parts: list[str] = []
    for i, (heading, links) in enumerate(sections):
        if i > 0:
            parts.append("---")
        if heading:
            parts.append(f"== {heading} ==")
        parts.extend(links)

    parts.append("---")
    parts.append("* [Edit Sidebar](/edit/_trellis/sidebar)")
    return "\n".join(parts) + "\n"


def _apply_category_path_rules(
    page, out_path: Path, output_dir: Path,
    rules: list[CategoryPathRule],
) -> Path:
    """If a converted page matches a category-to-path rule, relocate it
    into the folder and optionally strip the category from frontmatter.

    Returns the (possibly updated) output path.
    """
    cats = page.frontmatter.get("categories", [])
    if not cats:
        return out_path

    # Build lookup: lowercase category → rule
    rule_map = {r.category.lower(): r for r in rules}

    # Find the first matching rule (pages rarely belong to multiple
    # universe categories; if they do, the first match wins)
    matched_rule: CategoryPathRule | None = None
    for cat in cats:
        rule = rule_map.get(cat.lower())
        if rule:
            matched_rule = rule
            break

    if not matched_rule:
        return out_path

    # Only relocate Main-namespace pages (under pages/)
    pages_dir = output_dir / "pages"
    try:
        out_path.relative_to(pages_dir)
    except ValueError:
        return out_path

    # Already in a subfolder of the target? Skip (e.g. Book/ pages)
    rel_to_pages = out_path.relative_to(pages_dir)
    if len(rel_to_pages.parts) > 1:
        return out_path

    # Move into folder: pages/foo.wm → pages/arthagog/foo.wm
    new_path = pages_dir / matched_rule.folder / out_path.name

    # Strip or preserve the category
    if not matched_rule.preserve:
        page.frontmatter["categories"] = [
            c for c in cats if c.lower() != matched_rule.category.lower()
        ]
        if not page.frontmatter["categories"]:
            del page.frontmatter["categories"]

    return new_path


def _rewrite_links(content: str, rewrite_map: dict[str, str],
                    page_folder: str,
                    page_index: dict[str, str] | None = None) -> str:
    """Rewrite wiki links for relative link resolution.

    With relative links, [[Name]] resolves relative to the page's folder.
    For pages in a folder, links to siblings stay as [[Name]]. Links to
    pages in other folders or at root get an absolute prefix (/path).
    """
    page_index = page_index or {}

    def _resolve_target(key: str) -> str | None:
        """Return rewritten target, or None if no change needed."""
        # Check if target was explicitly relocated
        new_path = rewrite_map.get(key)
        actual_path = new_path or page_index.get(key, key)

        if not page_folder:
            # This page is at root — relative links resolve from root
            if new_path:
                return new_path
            return None

        # This page is in a folder — check if target is a sibling
        if "/" in actual_path:
            target_folder = actual_path.rsplit("/", 1)[0]
        else:
            target_folder = ""

        if target_folder == page_folder:
            # Sibling in same folder — keep relative (no prefix)
            return None

        # Different folder or root — use absolute path
        return f"/{actual_path}"

    def _rewrite_shorthand(m: re.Match) -> str:
        name = m.group(1)
        key = name.lower().replace(" ", "_")
        resolved = _resolve_target(key)
        if resolved:
            target = _md_link_target(resolved.replace("_", " "))
            return f"[{name}]({target})"
        return m.group(0)

    def _rewrite_angle(m: re.Match) -> str:
        display = m.group(1)
        target = m.group(2)
        key = target.lower().replace(" ", "_")
        resolved = _resolve_target(key)
        if resolved:
            return f"[{display}](<{resolved}>)"
        return m.group(0)

    def _rewrite_explicit(m: re.Match) -> str:
        display = m.group(1)
        target = m.group(2)
        if target.startswith(("http://", "https://", "/", "#")):
            return m.group(0)
        key = target.lower().replace(" ", "_")
        resolved = _resolve_target(key)
        if resolved:
            return f"[{display}]({resolved})"
        return m.group(0)

    def _rewrite_redirect(m: re.Match) -> str:
        target = m.group(1).strip()
        key = target.lower().replace(" ", "_")
        new_path = rewrite_map.get(key)
        if new_path:
            return f"redirect: {new_path}"
        return m.group(0)

    content = re.sub(r"\[\[([^\]]+)\]\]", _rewrite_shorthand, content)
    content = re.sub(r"\[([^\]]+)\]\(<([^>]+)>\)", _rewrite_angle, content)
    content = re.sub(r"\[([^\]]+)\]\(([^)<][^)]*)\)", _rewrite_explicit, content)
    content = re.sub(r"^redirect:\s*(.+)$", _rewrite_redirect, content, flags=re.MULTILINE)
    return content


def _build_page_index(output_dir: Path) -> dict[str, str]:
    """Build a map of page_stem → folder/page_stem for all pages.

    Root pages map to just their stem. Folder pages map to folder/stem.
    """
    pages_dir = output_dir / "pages"
    index: dict[str, str] = {}
    for wm_file in sorted(pages_dir.rglob("*.wm")):
        rel = wm_file.relative_to(pages_dir)
        stem = wm_file.stem
        if len(rel.parts) > 1:
            folder = str(rel.parent)
            index[stem] = f"{folder}/{stem}"
        else:
            index[stem] = stem
    return index


def _rewrite_all_links(output_dir: Path, rewrite_map: dict[str, str]) -> int:
    """Scan all .wm files and rewrite links for relative resolution.

    Uses the rewrite_map (old_stem → new_path) to know what moved,
    and a page index to know where everything lives.
    """
    pages_dir = output_dir / "pages"
    page_index = _build_page_index(output_dir)
    rewritten = 0
    for wm_file in sorted(output_dir.rglob("*.wm")):
        try:
            rel = wm_file.relative_to(pages_dir)
            page_folder = str(rel.parent) if len(rel.parts) > 1 else ""
        except ValueError:
            page_folder = ""

        content = wm_file.read_text(encoding="utf-8")
        new_content = _rewrite_links(content, rewrite_map, page_folder,
                                      page_index)
        if new_content != content:
            wm_file.write_text(new_content, encoding="utf-8")
            rewritten += 1
    return rewritten


def build(input_dir: Path, output_dir: Path,
          category_path_rules: list[CategoryPathRule] | None = None,
          redirect_stubs: bool = False) -> int:
    pages_src = input_dir / "pages"
    if not pages_src.is_dir():
        print(f"error: {pages_src} not found", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    rules = category_path_rules or []

    # Load wiki-specific template overrides (if any)
    reset_plugins()
    plugin_count = load_plugins(input_dir)
    if plugin_count:
        print(f"Loaded {plugin_count} custom template mappings")

    # Build template expansion library from Template/*.wikitext
    template_library = _build_template_library(pages_src)
    if template_library:
        print(f"Found {len(template_library)} wiki templates for expansion")

    report = Report()
    converted = 0
    redirects = 0
    errors = 0
    skipped_ns: dict[str, int] = {}
    relocated: dict[str, int] = {}
    # old_stem → folder/old_stem (for link rewriting)
    rewrite_map: dict[str, str] = {}

    for wikitext_file in sorted(pages_src.rglob("*.wikitext")):
        rel = wikitext_file.relative_to(pages_src)
        out_path, title = _output_path_for(rel, pages_src, output_dir)

        if out_path is None:
            mw_ns = rel.parts[0] if rel.parts else "unknown"
            skipped_ns[mw_ns] = skipped_ns.get(mw_ns, 0) + 1
            continue

        try:
            wikitext = wikitext_file.read_text(encoding="utf-8")
            page = convert_page(wikitext, title=title, report=report,
                               template_library=template_library)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  error converting {rel}: {e}", file=sys.stderr)
            continue

        # Ensure display title is in frontmatter (filenames are lowercase)
        if "title" not in page.frontmatter and not page.redirect:
            mw_ns = rel.parts[0] if rel.parts else ""
            target_dir = _NS_MAP.get(mw_ns)
            raw_name = rel.with_suffix("").as_posix()
            if mw_ns in _NS_MAP:
                raw_name = Path(*rel.parts[1:]).with_suffix("").as_posix() if len(rel.parts) > 1 else rel.stem
            if target_dir in ("templates", "categories"):
                display = raw_name.lower().replace(" ", "-").replace("_", "-")
            else:
                display = raw_name.replace("_", " ")
            page.frontmatter["title"] = display

        # Apply category-to-path relocation
        if rules and not page.redirect:
            new_path = _apply_category_path_rules(page, out_path, output_dir, rules)
            if new_path != out_path:
                folder_name = new_path.parent.name
                relocated[folder_name] = relocated.get(folder_name, 0) + 1
                old_stem = out_path.stem
                rewrite_map[old_stem] = f"{folder_name}/{old_stem}"
                if redirect_stubs:
                    new_wiki_path = f"{folder_name}/{old_stem}"
                    redirect_fm = _emit_frontmatter({"redirect": new_wiki_path})
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(redirect_fm, encoding="utf-8")
                out_path = new_path

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page.to_wikimark(), encoding="utf-8")
        if page.redirect:
            redirects += 1
        converted += 1

    # Convert MediaWiki sidebar if present
    sidebar_src = pages_src / "MediaWiki" / "Sidebar.wikitext"
    if sidebar_src.is_file():
        sidebar_wm = _convert_mw_sidebar(sidebar_src.read_text(encoding="utf-8"))
        sidebar_dst = output_dir / "pages" / "_trellis"
        sidebar_dst.mkdir(parents=True, exist_ok=True)
        (sidebar_dst / "sidebar.wm").write_text(sidebar_wm, encoding="utf-8")
        print("Converted MediaWiki sidebar → _trellis/sidebar.wm")

    # Overwrite auto-converted templates with hand-written clean versions
    clean_dir = input_dir.parent / "clean-templates"
    if clean_dir.is_dir():
        templates_dst = output_dir / "templates"
        templates_dst.mkdir(parents=True, exist_ok=True)
        clean_count = 0
        for f in clean_dir.glob("*.wm"):
            shutil.copy2(f, templates_dst / f.name)
            clean_count += 1
        if clean_count:
            print(f"Overwrote {clean_count} templates with clean versions")

    # Remove MW sub-templates (auto-converted slash-subtemplates)
    if (output_dir / "templates").is_dir():
        for sub in list((output_dir / "templates").rglob("*∕*")):
            if sub.is_file():
                sub.unlink()

    # Copy assets (MediaWiki files → WikiMark assets/)
    files_src = input_dir / "files"
    if files_src.is_dir():
        assets_dst = output_dir / "assets"
        assets_dst.mkdir(parents=True, exist_ok=True)
        copied_assets = 0
        for f in files_src.iterdir():
            if f.is_file() and not f.name.startswith("_"):
                shutil.copy2(f, assets_dst / f.name)
                copied_assets += 1
    else:
        copied_assets = 0

    # Rewrite links to relocated pages (unless redirect stubs are used)
    if rewrite_map and not redirect_stubs:
        rewritten = _rewrite_all_links(output_dir, rewrite_map)
        print(f"Rewrote links in {rewritten} files ({len(rewrite_map)} relocated paths)")

    # Write conversion report
    (output_dir / "CONVERSION.md").write_text(
        report.render_markdown(), encoding="utf-8"
    )

    print(f"Converted {converted} pages "
          f"({redirects} redirects)")
    print(f"  pages/: {sum(1 for _ in (output_dir / 'pages').rglob('*.wm'))}")
    if (output_dir / "templates").is_dir():
        print(f"  templates/: {sum(1 for _ in (output_dir / 'templates').rglob('*.wm'))}")
    if (output_dir / "categories").is_dir():
        print(f"  categories/: {sum(1 for _ in (output_dir / 'categories').rglob('*.wm'))}")
    print(f"  assets/: {copied_assets}")
    if skipped_ns:
        skipped_total = sum(skipped_ns.values())
        detail = ", ".join(f"{k}:{v}" for k, v in sorted(skipped_ns.items()))
        print(f"Skipped {skipped_total} MW infrastructure pages ({detail})")
    if relocated:
        for folder, count in sorted(relocated.items()):
            print(f"  → {folder}/: {count} pages relocated")
    print(f"Report: {output_dir}/CONVERSION.md "
          f"({len(report.entries)} unhandled entries)")

    if errors:
        print(f"  {errors} pages failed to convert; see stderr", file=sys.stderr)
    return 0 if errors == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mw2wm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build_p = sub.add_parser("build", help="Convert a fetched wiki to WikiMark.")
    build_p.add_argument("input_dir", type=Path,
                          help="Directory containing pages/, files/ (from fetch.py)")
    build_p.add_argument("output_dir", type=Path,
                          help="Directory to write pages/, assets/, CONVERSION.md")
    build_p.add_argument(
        "--category-to-path", action="append", dest="cat_path_rules",
        default=[], metavar="CATEGORY:MODE",
        help="Move pages in CATEGORY into a folder path. "
             "MODE is 'preserve' (keep category) or 'replace' (remove it). "
             "Can be repeated.",
    )
    build_p.add_argument(
        "--redirect-stubs", action="store_true", default=False,
        help="Write redirect stubs at old paths instead of rewriting links.",
    )
    args = parser.parse_args(argv)
    if args.cmd == "build":
        rules = [CategoryPathRule.parse(s) for s in args.cat_path_rules]
        return build(args.input_dir, args.output_dir,
                     category_path_rules=rules,
                     redirect_stubs=args.redirect_stubs)
    return 2


if __name__ == "__main__":
    sys.exit(main())
