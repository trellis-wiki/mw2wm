#!/usr/bin/env python3
"""
Fetch a MediaWiki instance via its API into mw2wm/input/.

Authenticates with the credentials in /home/coder/projects/.env
(MEDIAWIKI_URL, MEDIAWIKI_USERNAME, MEDIAWIKI_PASSWORD), then
downloads:

- Per-namespace page lists + raw wikitext (into input/pages/<ns>/)
- Scribunto module source (into input/modules/)
- File binaries from the File: namespace (into input/files/)
- Site info, namespace table, extension list, siteinfo full dump
  (into input/meta/)

Designed to be idempotent: re-running skips pages whose latest
revision hasn't changed since the last fetch. Stores the
last-fetched revision timestamp in input/meta/state.json.

TLS verification is disabled by default for internal/homelab
wikis with self-signed certificates. If you point this at a wiki with a
public cert, set VERIFY_TLS=1 in the environment.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPSHandler, HTTPCookieProcessor
from http.cookiejar import CookieJar

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = REPO_ROOT / "input"
PAGES_DIR = INPUT_DIR / "pages"
MODULES_DIR = INPUT_DIR / "modules"
FILES_DIR = INPUT_DIR / "files"
META_DIR = INPUT_DIR / "meta"

USER_AGENT = "mw2wm/0.1 (trellis-wiki migration)"


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        sys.exit(f"error: {name} not set in environment; source .env first")
    return val


def make_opener() -> tuple:
    ctx = ssl.create_default_context()
    if os.environ.get("VERIFY_TLS") != "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar), HTTPSHandler(context=ctx))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener, jar


def api(opener, base: str, **params) -> dict:
    params.setdefault("format", "json")
    url = f"{base}api.php?" + urlencode(params)
    with opener.open(url) as r:
        body = r.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        sys.exit(f"error: non-JSON response from {url}:\n{body[:500]!r}")


def api_post(opener, base: str, **params) -> dict:
    params.setdefault("format", "json")
    data = urlencode(params).encode()
    req = Request(f"{base}api.php", data=data)
    with opener.open(req) as r:
        body = r.read()
    return json.loads(body)


def login(opener, base: str, user: str, pw: str) -> dict:
    tok = api(opener, base, action="query", meta="tokens", type="login")
    token = tok["query"]["tokens"]["logintoken"]
    result = api_post(opener, base,
                      action="login", lgname=user,
                      lgpassword=pw, lgtoken=token)
    login = result.get("login", {})
    if login.get("result") != "Success":
        sys.exit(f"login failed: {login}")
    return login


def safe_filename(title: str) -> str:
    """Convert a MediaWiki title into a filesystem-safe name.

    MediaWiki titles can contain characters that are legal on disk but
    unergonomic (spaces, colons). We mirror MediaWiki's URL convention:
    spaces → underscores. Preserve other characters; the filesystem is
    UTF-8 and modern enough.
    """
    return title.replace(" ", "_").replace("/", "\u2215")  # ∕ = U+2215


def fetch_pages_in_namespace(opener, base: str, nsid: int, ns_name: str,
                             dest_root: Path, state: dict) -> int:
    """Download wikitext for every page in a namespace.

    Skips pages whose latest-revision timestamp matches the cached one
    (idempotent re-runs).
    """
    ns_key = f"ns_{nsid}"
    prev_state = state.get(ns_key, {})
    new_state: dict[str, str] = {}

    dir_name = ns_name.replace(" ", "_") if ns_name else "Main"
    dest = dest_root / dir_name
    dest.mkdir(parents=True, exist_ok=True)

    total = 0
    skipped = 0
    cont: dict = {}
    while True:
        q = api(opener, base, action="query",
                generator="allpages", gapnamespace=nsid, gaplimit=50,
                prop="revisions", rvprop="content|timestamp|ids",
                rvslots="main",
                **cont)
        pages = q.get("query", {}).get("pages", {})
        for _pid, page in pages.items():
            title = page["title"]
            revs = page.get("revisions", [])
            if not revs:
                continue
            rev = revs[0]
            ts = rev.get("timestamp", "")
            content = rev.get("slots", {}).get("main", {}).get("*", "")
            if not content and "*" in rev:
                content = rev["*"]

            new_state[title] = ts
            if prev_state.get(title) == ts:
                skipped += 1
                total += 1
                continue

            fname = safe_filename(title.split(":", 1)[-1]) + ".wikitext"
            out = dest / fname
            out.write_text(content, encoding="utf-8")
            total += 1
        if "continue" in q:
            cont = q["continue"]
        else:
            break

    state[ns_key] = new_state
    print(f"  ns {nsid:>4} {ns_name!r:>24}: {total:>4} pages "
          f"({skipped} unchanged, {total - skipped} fetched)")
    return total


def fetch_files(opener, base: str, dest: Path) -> int:
    """Download the binary for every File: namespace page."""
    dest.mkdir(parents=True, exist_ok=True)
    meta_file = dest / "_file-metadata.jsonl"
    with meta_file.open("w", encoding="utf-8") as meta_fh:
        count = 0
        cont: dict = {}
        while True:
            q = api(opener, base, action="query",
                    generator="allimages", gailimit=50,
                    prop="imageinfo", iiprop="url|mime|size|sha1|timestamp|comment",
                    **cont)
            pages = q.get("query", {}).get("pages", {})
            for _pid, page in pages.items():
                title = page["title"]
                info = page.get("imageinfo", [{}])[0]
                meta_fh.write(json.dumps({
                    "title": title,
                    "sha1": info.get("sha1"),
                    "mime": info.get("mime"),
                    "size": info.get("size"),
                    "url": info.get("url"),
                    "timestamp": info.get("timestamp"),
                    "comment": info.get("comment"),
                }) + "\n")
                url = info.get("url")
                if not url:
                    continue

                # File name: title is "File:Something.png", we want "Something.png"
                fname = title.split(":", 1)[-1]
                out = dest / fname
                if out.exists():
                    count += 1
                    continue
                with opener.open(url) as r:
                    out.write_bytes(r.read())
                count += 1
            if "continue" in q:
                cont = q["continue"]
            else:
                break
    print(f"  files: {count} downloaded (metadata → {meta_file.name})")
    return count


def save_siteinfo(opener, base: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    # Pull the full siteinfo in one go
    siteinfo = api(opener, base, action="query", meta="siteinfo",
                   siprop="|".join([
                       "general", "namespaces", "namespacealiases",
                       "interwikimap", "extensions", "extensiontags",
                       "functionhooks", "statistics", "magicwords",
                       "fileextensions", "skins",
                   ]))
    (dest / "siteinfo.json").write_text(
        json.dumps(siteinfo, indent=2, ensure_ascii=False))
    print(f"  siteinfo → {dest}/siteinfo.json")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main() -> int:
    base = env("MEDIAWIKI_URL")
    user = env("MEDIAWIKI_USERNAME")
    pw = env("MEDIAWIKI_PASSWORD")

    print(f"mw2wm fetch — source: {base}")
    print(f"              output: {INPUT_DIR}")

    opener, _jar = make_opener()
    info = login(opener, base, user, pw)
    print(f"  logged in as: {info.get('lgusername')} "
          f"(id={info.get('lguserid')})")

    save_siteinfo(opener, base, META_DIR)

    # Namespace list
    ns_q = api(opener, base, action="query", meta="siteinfo",
               siprop="namespaces")
    namespaces = ns_q["query"]["namespaces"]

    # State file: last-fetched revision timestamps per page
    state_path = META_DIR / "state.json"
    state = load_state(state_path)

    # Page fetch — content namespaces only (skip talk pages, Special, Media)
    # Also skip User namespace to avoid private user-page content in the
    # migration; adjust if needed later.
    skip_namespaces = {-2, -1, 1, 3, 5, 7, 9, 11, 13, 15, 103, 107, 109,
                       113, 115, 829, 2301, 2303, 3001}
    # Additional skips: User/User talk (2, 3). User pages are personal
    # and shouldn't migrate into a public wiki by default.
    skip_namespaces.add(2)

    print("\nPages:")
    total = 0
    for nsid_s, info in sorted(namespaces.items(), key=lambda x: int(x[0])):
        nsid = int(nsid_s)
        if nsid in skip_namespaces:
            continue
        ns_name = info.get("*", "") or "Main"
        total += fetch_pages_in_namespace(
            opener, base, nsid, ns_name, PAGES_DIR, state)
    print(f"  TOTAL: {total} pages")

    save_state(state_path, state)

    # Files
    print("\nFiles:")
    fetch_files(opener, base, FILES_DIR)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
