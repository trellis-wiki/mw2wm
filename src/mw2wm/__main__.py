"""
mw2wm CLI.

Usage::

    python -m mw2wm build <input-dir> <output-dir>

Walks ``<input-dir>/pages/`` for ``.wikitext`` files, converts each
through :func:`mw2wm.convert_page`, and writes ``.wm`` output using
WikiMark directory conventions:

- ``pages/``       — content pages (MediaWiki Main namespace → root)
- ``templates/``   — template pages
- ``categories/``  — category pages
- ``assets/``      — images and files

Other MediaWiki namespaces (File, Module, MediaWiki, etc.) are
infrastructure and not converted.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import convert_page
from .convert import _build_template_library
from .report import Report
from .templates import load_plugins, reset_plugins

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


def build(input_dir: Path, output_dir: Path) -> int:
    pages_src = input_dir / "pages"
    if not pages_src.is_dir():
        print(f"error: {pages_src} not found", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

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
            raw_name = rel.with_suffix("").as_posix()
            if mw_ns in _NS_MAP:
                raw_name = Path(*rel.parts[1:]).with_suffix("").as_posix() if len(rel.parts) > 1 else rel.stem
            display = raw_name.replace("_", " ")
            page.frontmatter["title"] = display

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page.to_wikimark(), encoding="utf-8")
        if page.redirect:
            redirects += 1
        converted += 1

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
    args = parser.parse_args(argv)
    if args.cmd == "build":
        return build(args.input_dir, args.output_dir)
    return 2


if __name__ == "__main__":
    sys.exit(main())
