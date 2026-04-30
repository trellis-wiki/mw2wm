"""
mw2wm CLI.

Usage::

    python -m mw2wm build <input-dir> <output-dir>

Walks ``<input-dir>/pages/`` for ``.wikitext`` files, converts each
through :func:`mw2wm.convert_page`, writes ``.wm`` output preserving
the directory layout, copies ``<input-dir>/files/`` to
``<output-dir>/assets/``, and emits a ``CONVERSION.md`` report.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import convert_page
from .report import Report
from .templates import load_plugins, reset_plugins


def build(input_dir: Path, output_dir: Path) -> int:
    pages_src = input_dir / "pages"
    if not pages_src.is_dir():
        print(f"error: {pages_src} not found", file=sys.stderr)
        return 2

    pages_dst = output_dir / "pages"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load wiki-specific template plugins
    reset_plugins()
    plugin_count = load_plugins(input_dir)
    if plugin_count:
        print(f"Loaded {plugin_count} custom template mappings")

    report = Report()
    converted = 0
    redirects = 0
    errors = 0

    for wikitext_file in sorted(pages_src.rglob("*.wikitext")):
        rel = wikitext_file.relative_to(pages_src)
        title = rel.with_suffix("").as_posix().replace("/", " / ")
        try:
            wikitext = wikitext_file.read_text(encoding="utf-8")
            page = convert_page(wikitext, title=title, report=report)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  error converting {rel}: {e}", file=sys.stderr)
            continue

        out_path = (pages_dst / rel).with_suffix(".wm")
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
          f"({redirects} redirects) → {pages_dst}")
    print(f"Copied {copied_assets} assets → {output_dir}/assets/")
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
