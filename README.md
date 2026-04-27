# mw2wm

MediaWiki → WikiMark migration tool. Takes an authenticated MediaWiki
source and produces a directory of `.wm` files that the Trellis wiki
engine can render.

See [PLAN.md](PLAN.md) for scope and sequencing.

## Fetching source content

`tools/fetch.py` authenticates against a MediaWiki API and mirrors
pages + files into `input/`:

```bash
# Credentials come from ../env (the trellis-wiki convention)
set -a && source /home/coder/projects/.env && set +a
python3 tools/fetch.py
```

Required environment variables:
- `MEDIAWIKI_URL` — script path (with trailing `/`), e.g.
  `https://wiki.example.com/w/`
- `MEDIAWIKI_USERNAME` / `MEDIAWIKI_PASSWORD` — admin credentials

Optional:
- `VERIFY_TLS=1` — enable TLS verification (default: disabled for
  homelab CAs)

The fetch is idempotent; re-running only re-downloads pages whose
revision timestamp changed. State is tracked in
`input/meta/state.json`.

## Surveying

`tools/survey.py` analyzes the fetched `input/` tree and writes
`SURVEY.md` with:
- Totals per namespace
- Template usage frequencies (which templates are actually
  referenced from articles vs. imported-but-unused)
- Parser function usage
- Extension tag usage
- Scribunto module invocations
- Unusual pages (large, unbalanced delimiters)

Run after each fetch:

```bash
python3 tools/survey.py
```

## What's in `input/`

- `input/pages/<Namespace>/<PageName>.wikitext` — raw wikitext per
  page, one file per page
- `input/files/<filename>` — binary downloads from the File:
  namespace
- `input/files/_file-metadata.jsonl` — per-file metadata (URL,
  SHA-1, MIME, size, upload comment)
- `input/meta/siteinfo.json` — full MediaWiki siteinfo dump
- `input/meta/state.json` — per-page last-fetched revision
  timestamps (used for idempotent re-runs)

**`input/` is gitignored.** It contains private wiki content and
never commits.

## Converting fetched content

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m mw2wm build input/ output/
```

Produces:
- `output/pages/<Namespace>/<Page>.wm` — WikiMark source, one file per page
- `output/assets/` — image and file binaries copied from `input/files/`
- `output/CONVERSION.md` — report of unhandled constructs for human review

Re-run mw2wm after any fetch update — it's stateless.
