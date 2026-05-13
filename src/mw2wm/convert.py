"""
Core wikitext → WikiMark conversion.

Uses ``mwparserfromhell`` for tokenization — it's the standard
Python MediaWiki parser, battle-tested against Wikipedia's corpus,
and handles the nasty edge cases (nested templates, unclosed
brackets, etc.) far better than anything we'd write ourselves.

Strategy: one pass over the parsed node tree. For each node type
we know, emit the equivalent WikiMark. For unknown nodes, pass
through the raw text and log to the conversion report.

Frontmatter is accumulated as a dict and emitted at the top of the
output document. Categories, display titles, default sort keys, and
redirect targets all flow into frontmatter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import mwparserfromhell as mwp
import yaml

from . import templates as tpl
from .report import Report


# Magic words we extract to frontmatter. Keys are canonical (colon-
# prefixed) names; values are the frontmatter field the content maps
# to. Behavior switches (like ``__NOTOC__``) have empty-string content.
_MAGIC_PREFIX_MAP = {
    "DISPLAYTITLE": "title",
    "DEFAULTSORT": "sort_key",
}
_MAGIC_SWITCHES = {
    "__NOTOC__": ("toc", False),
    "__NOEDITSECTION__": ("editable_sections", False),
    "__TOC__": ("toc", True),
    "__FORCETOC__": ("toc", True),
}


@dataclass
class ConvertedPage:
    """Output of converting a single wikitext page."""

    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    redirect: str | None = None

    def to_wikimark(self) -> str:
        if self.redirect:
            # Redirects are frontmatter-only; no body needed.
            return _emit_frontmatter({"redirect": self.redirect})
        out = _emit_frontmatter(self.frontmatter) if self.frontmatter else ""
        if out and self.body:
            out += "\n"
        return out + self.body


def _emit_frontmatter(fm: dict[str, Any]) -> str:
    """Serialize the frontmatter dict to a ``---``-fenced YAML block."""
    if not fm:
        return ""
    yaml_body = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_body}\n---\n"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def convert_page(
    wikitext: str,
    *,
    title: str = "",
    report: Report | None = None,
    template_library: dict[str, str] | None = None,
) -> ConvertedPage:
    """Convert a single MediaWiki page to a ConvertedPage result.

    Args:
        wikitext: Raw MediaWiki source.
        title: Page title (used only for report attribution).
        report: Accumulator for unhandled constructs. A new Report is
            created and discarded if None (useful for tests).
        template_library: Dict of template name → body text for
            expanding simple templates during conversion.
    """
    report = report or Report()
    state = _ConvertState(
        title=title, report=report,
        template_library=template_library or {},
    )

    # Check for #REDIRECT before parsing — it must be at the top of
    # the file and is simple enough to handle without mwparserfromhell.
    redirect_match = re.match(
        r"^\s*#REDIRECT\s*\[\[([^\]|]+)(?:\|[^\]]*)?\]\]\s*$",
        wikitext,
        re.IGNORECASE | re.MULTILINE,
    )
    if redirect_match:
        target = redirect_match.group(1).strip()
        page = ConvertedPage(redirect=target)
        return page

    parsed = mwp.parse(wikitext)
    body = _convert_nodes(parsed.nodes, state, _toplevel=True)

    # Clean up body: collapse excessive blank lines; strip leading/
    # trailing whitespace; ensure trailing newline.
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.strip() + "\n"

    # Template pages with inputs get type: transclusion
    is_template = (title.startswith("Template:") or
                   title.startswith("Template /") or
                   title.startswith("Template/"))
    if is_template and "inputs" in state.frontmatter:
        state.frontmatter.setdefault("type", "transclusion")

    result = ConvertedPage(frontmatter=state.frontmatter, body=body)
    # Dedupe category list (order-preserving)
    if "categories" in result.frontmatter:
        seen: set[str] = set()
        uniq: list[str] = []
        for c in result.frontmatter["categories"]:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        result.frontmatter["categories"] = uniq
    return result


def convert_text(wikitext: str, *, title: str = "") -> str:
    """Convenience: convert to a WikiMark string in one call."""
    return convert_page(wikitext, title=title).to_wikimark()


# ---------------------------------------------------------------------------
# Conversion state
# ---------------------------------------------------------------------------


_INCLUDEONLY_RE = re.compile(
    r"<includeonly>(.*?)</includeonly>", re.DOTALL | re.IGNORECASE
)
_NOINCLUDE_RE = re.compile(
    r"<noinclude>.*?</noinclude>", re.DOTALL | re.IGNORECASE
)
_TEMPLATE_ARG_RE_MW = re.compile(r"\{\{\{([^}|]*)(?:\|([^}]*))?\}\}\}")


def _build_template_library(pages_dir: Path) -> dict[str, str]:
    """Build a template expansion library from Template/*.wikitext files.

    Returns a dict of normalized template name → expandable body text.
    Only includes templates with <includeonly> content (the part that
    MediaWiki actually transcludes). Falls back to full content minus
    <noinclude> if no <includeonly> tags.
    """
    tpl_dir = pages_dir / "Template"
    if not tpl_dir.is_dir():
        return {}

    library: dict[str, str] = {}
    for f in tpl_dir.glob("*.wikitext"):
        name = f.stem.replace("_", " ")
        content = f.read_text(encoding="utf-8")

        # Extract <includeonly> content if present
        m = _INCLUDEONLY_RE.search(content)
        if m:
            body = m.group(1)
        else:
            # No <includeonly> — use everything minus <noinclude> blocks
            body = _NOINCLUDE_RE.sub("", content).strip()

        if body:
            library[name] = body

    return library


def _expand_template(body: str, args: dict[str, str]) -> str:
    """Substitute {{{1}}}, {{{name}}}, {{{name|default}}} in a template body.

    Runs multiple passes to resolve nested args like {{{2|{{{simult}}}}}}.
    """
    def _replace_arg(m: re.Match) -> str:
        key = m.group(1).strip()
        default = m.group(2) if m.group(2) is not None else ""
        return args.get(key, default)
    prev = None
    result = body
    for _ in range(5):
        if prev == result:
            break
        prev = result
        result = _TEMPLATE_ARG_RE_MW.sub(_replace_arg, result)
    return result


@dataclass
class _ConvertState:
    title: str
    report: Report
    frontmatter: dict[str, Any] = field(default_factory=dict)
    footnotes: list[str] = field(default_factory=list)
    named_footnotes: dict[str, int] = field(default_factory=dict)
    _list_depth: int = 0
    _list_type: str = ""
    template_library: dict[str, str] = field(default_factory=dict)
    _node_output: list[str] | None = None
    _pending_annotation: str | None = None
    _template_depth: int = 0
    _max_template_depth: int = 10

    def add_category(self, name: str) -> None:
        self.frontmatter.setdefault("categories", []).append(name)

    def set_fm(self, key: str, value: Any) -> None:
        self.frontmatter[key] = value

    def new_footnote(self, content: str, ref_name: str = "") -> str:
        """Add a footnote. Returns the ``[^id]`` marker text."""
        if ref_name and ref_name in self.named_footnotes:
            return f"[^{ref_name}]"
        idx = len(self.footnotes) + 1
        if ref_name:
            ident = ref_name
            self.named_footnotes[ref_name] = idx
        else:
            ident = str(idx)
        self.footnotes.append(f"[^{ident}]: {content}")
        return f"[^{ident}]"


# ---------------------------------------------------------------------------
# Node dispatch
# ---------------------------------------------------------------------------


def _convert_nodes(nodes: Any, state: _ConvertState, *, _toplevel: bool = False) -> str:
    """Walk a sequence of mwparserfromhell Nodes, emit WikiMark."""
    prev_output = state._node_output
    out: list[str] = []
    state._node_output = out
    for node in nodes:
        chunk = _convert_node(node, state)
        if state._pending_annotation and chunk.strip():
            chunk = f"[{chunk.strip()}]|{state._pending_annotation}|"
            state._pending_annotation = None
        out.append(chunk)
    state._node_output = prev_output

    result = "".join(out)

    # Footnotes only at the top-level call (not inside template params)
    if _toplevel and state.footnotes:
        result += "\n\n" + "\n".join(state.footnotes) + "\n"

    return result


def _flush_list_marker(state: _ConvertState) -> str:
    """Emit accumulated GFM list prefix and reset depth."""
    if state._list_depth == 0:
        return ""
    depth = state._list_depth
    marker_type = state._list_type
    state._list_depth = 0
    state._list_type = ""
    if marker_type == "#":
        return "   " * (depth - 1) + "1. "
    return "  " * (depth - 1) + "* "


def _convert_node(node: Any, state: _ConvertState) -> str:
    """Dispatch by node type (duck-typed against mwparserfromhell)."""
    from mwparserfromhell.nodes import (
        Text, Template, Wikilink, ExternalLink, Heading,
        Tag, Comment, HTMLEntity, Argument,
    )

    # List markers (Tag li with wiki_markup) are accumulated in state
    # by _convert_tag. Before emitting any other node, flush the
    # accumulated prefix so ``*[[link]]`` becomes ``* [[link]]``.
    is_list_marker = (
        isinstance(node, Tag)
        and str(node.tag).lower() == "li"
        and getattr(node, "wiki_markup", None) in ("*", "#")
    )

    if is_list_marker:
        return _convert_tag(node, state)

    prefix = _flush_list_marker(state)

    if isinstance(node, Text):
        text = str(node)
        if prefix and text.startswith(" "):
            text = text[1:]
        return prefix + _convert_text(text, state)
    if isinstance(node, Wikilink):
        return prefix + _convert_wikilink(node, state)
    if isinstance(node, Template):
        return prefix + _convert_template(node, state)
    if isinstance(node, ExternalLink):
        return prefix + _convert_external_link(node, state)
    if isinstance(node, Heading):
        return prefix + _convert_heading(node, state)
    if isinstance(node, Tag):
        return prefix + _convert_tag(node, state)
    if isinstance(node, Comment):
        return prefix + f"<!--{node.contents}-->"
    if isinstance(node, HTMLEntity):
        return prefix + str(node)
    if isinstance(node, Argument):
        return prefix + _convert_argument(node, state)
    # Fallback: pass through raw text
    state.report.add("unknown-node-type", state.title, type(node).__name__)
    return prefix + str(node)


# ---------------------------------------------------------------------------
# Converters per node kind
# ---------------------------------------------------------------------------


def _convert_argument(node: Any, state: _ConvertState) -> str:
    """Convert {{{arg}}} / {{{arg|default}}} to WikiMark ${arg} syntax.

    On template pages, arguments become variable references. If a default
    value is present, it's recorded in the inputs frontmatter block.
    On non-template pages, arguments are passed through and reported.
    """
    name = str(node.name).strip()
    default = node.default

    is_template_page = (state.title.startswith("Template:") or
                        state.title.startswith("Template /") or
                        state.title.startswith("Template/"))

    if not is_template_page:
        state.report.add("template-argument", state.title, str(node))
        return str(node)

    if default is not None:
        default_str = _convert_nodes(default.nodes, state).strip()
        inputs = state.frontmatter.setdefault("inputs", {})
        entry = inputs.setdefault(name, {})
        if default_str:
            if "default" not in entry:
                entry["default"] = default_str
            elif entry["default"] != default_str:
                existing = entry["default"]
                state.report.add("multiple-defaults", state.title,
                                 f"{name}: {existing!r} vs {default_str!r}")
                entry["default"] = (
                    f"WARN Multiple defaults: {existing}, {default_str}"
                )
                state.add_category("Templates with multiple defaults")

    return f"${{{name}}}"


# MediaWiki inline formatting → GFM
_BOLD_ITALIC_RE = re.compile(r"'{2,5}")


def _convert_text(text: str, state: _ConvertState) -> str:
    """Convert a plain text run — mainly rewriting MediaWiki ''' and ''
    emphasis markers to GFM **...** / *...* equivalents.
    """
    # Quote-based emphasis. MediaWiki uses:
    #   '' ... ''        → italic
    #   ''' ... '''      → bold
    #   ''''' ... '''''  → bold + italic
    # We do a stateful pass: detect the longest opening run, find the
    # matching close run of the same length, convert.
    text = _convert_quote_emphasis(text)

    # Magic-word switches (__NOTOC__, etc.). Handled here because they
    # can appear mid-text without being their own nodes.
    for magic, (key, value) in _MAGIC_SWITCHES.items():
        if magic in text:
            state.set_fm(key, value)
            text = text.replace(magic, "")

    # Escape WikiMark-meaningful sequences that aren't also GFM-
    # meaningful. Primarily `[[` is now a wiki-link opener (which is
    # fine since we already processed wikilinks via the AST). We leave
    # raw `[[` and `{{` in text as literal — the spec already handles
    # unclosed delimiters as literal.
    return text


def _convert_quote_emphasis(text: str) -> str:
    """Rewrite MediaWiki '' and ''' emphasis markers as GFM * and **."""
    # Strategy: find the longest-possible run at each position, emit a
    # matching marker. The MediaWiki parser has subtle rules around
    # mid-word quotes; we take a simpler approach that covers the
    # common paragraph-boundary case correctly.
    def _replace(match: re.Match[str]) -> str:
        run_len = len(match.group(0))
        if run_len == 2:
            return "*"
        if run_len == 3:
            return "**"
        if run_len == 5:
            return "***"
        # 4 or 6+ — MediaWiki treats these oddly; emit one apostrophe
        # plus the shorter marker.
        if run_len == 4:
            return "'*"
        # 6+: 5-char marker plus extras
        return "***" + "'" * (run_len - 5)

    return _BOLD_ITALIC_RE.sub(_replace, text)


def _md_link_target(target: str) -> str:
    """Normalize a wiki page name for use as a Markdown link target.

    Replaces spaces with underscores and wraps in angle brackets if
    the target contains parentheses.
    """
    target = target.replace(" ", "_")
    if "(" in target or ")" in target:
        return f"<{target}>"
    return target


def _convert_wikilink(node: Any, state: _ConvertState) -> str:
    """Convert ``[[Target]]`` and ``[[Target|Display]]`` forms."""
    target = str(node.title).strip()
    display = str(node.text) if node.text else ""

    # Special prefixes
    if target.startswith("Category:") or target.startswith(":Category:"):
        # [[Category:Foo]] → frontmatter; [[:Category:Foo]] → in-body link
        if target.startswith(":Category:"):
            # In-body link with colon prefix — display as normal
            display_text = display or target[len(":Category:"):]
            return f"[{display_text}]({_md_link_target(target[1:])})"
        # Plain category → frontmatter, no body output
        name = target[len("Category:"):]
        if display:
            state.set_fm("sort_key", display)
        state.add_category(name)
        return ""

    if target.startswith("File:") or target.startswith("Image:"):
        return _convert_file_link(node, state)

    if ":" in target and target.split(":", 1)[0] in ("Special",):
        state.report.add("special-link", state.title, target)
        return display or target

    # Normal wiki link
    if display:
        return f"[{display}]({_md_link_target(target)})"
    return f"[[{target}]]"


_FILE_OPT_RE = re.compile(r"^(?:thumb|thumbnail|frame|frameless|border)$",
                          re.IGNORECASE)
_FILE_SIZE_RE = re.compile(r"^(\d+)(?:x(\d+))?(px)?$")
_FILE_ALIGN_RE = re.compile(r"^(?:left|right|center|centre|none)$",
                            re.IGNORECASE)


def _caption_to_plain(text: str) -> str:
    """Flatten wiki/markdown markup to plain text for alt attributes."""
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _caption_to_html(text: str) -> str:
    """Convert wiki links in a caption to HTML <a> tags."""
    from html import escape as h
    def _link(m: re.Match) -> str:
        target = m.group(1).strip()
        display = m.group(2).strip() if m.lastindex == 2 else target
        href = target.replace(" ", "_")
        return f'<a href="{h(href)}">{h(display)}</a>'

    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", _link, text)
    text = re.sub(r"\[\[([^\]]+)\]\]", _link, text)
    return text


_WIKI_LINK_IN_TEXT = re.compile(r"\[\[")


def _convert_file_link(node: Any, state: _ConvertState) -> str:
    """Convert ``[[File:X.jpg|thumb|200px|caption]]`` to WikiMark image."""
    target = str(node.title).strip()
    filename = target.split(":", 1)[1] if ":" in target else target

    classes: list[str] = []
    width: str | None = None
    height: str | None = None
    align: str | None = None
    alt_text: str | None = None
    caption = ""

    if node.text:
        parts = _split_params(str(node.text))
        for part in parts:
            part = part.strip()
            if _FILE_OPT_RE.match(part):
                classes.append(part.lower())
            elif _FILE_ALIGN_RE.match(part):
                align = part.lower()
            elif _FILE_SIZE_RE.match(part):
                m = _FILE_SIZE_RE.match(part)
                assert m is not None
                width = m.group(1)
                if m.group(2):
                    height = m.group(2)
            elif part.startswith("alt="):
                alt_text = part[4:]
            elif part.startswith(("link=", "page=", "class=", "lang=",
                                   "upright", "upright=")):
                state.report.add("file-option-dropped", state.title, part)
            else:
                caption = part

    href = filename.replace(" ", "_")

    # cmark-gfm truncates image descriptions at any inline markup.
    # For captions with wiki links, emit raw HTML so links are preserved.
    if caption and _WIKI_LINK_IN_TEXT.search(caption):
        from html import escape as h
        alt_plain = alt_text or _caption_to_plain(caption)
        caption_html = _caption_to_html(caption)
        cls_list = ["wm-figure"] + [f"wm-{c}" for c in classes]
        if align:
            cls_list.append(f"wm-align-{align}")
        cls = " ".join(cls_list)
        style_parts = []
        if width:
            style_parts.append(f"max-width:{width}px")
        style = f' style="{";".join(style_parts)}"' if style_parts else ""
        img_attrs = f' width="{width}"' if width else ""
        if height:
            img_attrs += f' height="{height}"'
        return (
            f'\n<figure class="{h(cls)}"{style}>'
            f'<img src="{h(href)}" alt="{h(alt_plain)}"{img_attrs} />'
            f'<figcaption>{caption_html}</figcaption>'
            f'</figure>\n'
        )

    alt = alt_text or (_caption_to_plain(caption) if caption else filename)
    attrs = []
    if classes:
        attrs.extend(f".{c}" for c in classes)
    if width:
        attrs.append(f"width={width}")
    if height:
        attrs.append(f"height={height}")
    if align:
        attrs.append(f"align={align}")

    attr_block = " ".join(attrs)
    attr_str = f"{{{attr_block}}}" if attr_block else ""
    return f"![{alt}]({href}){attr_str}"


def _split_params(s: str) -> list[str]:
    """Split a wikitext parameter string on pipes, respecting
    nested ``[[...]]`` and ``{{...}}``.
    """
    parts: list[str] = []
    depth_brace = 0
    depth_bracket = 0
    start = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "{" and i + 1 < len(s) and s[i + 1] == "{":
            depth_brace += 1; i += 2; continue
        if c == "}" and i + 1 < len(s) and s[i + 1] == "}":
            depth_brace -= 1; i += 2; continue
        if c == "[" and i + 1 < len(s) and s[i + 1] == "[":
            depth_bracket += 1; i += 2; continue
        if c == "]" and i + 1 < len(s) and s[i + 1] == "]":
            depth_bracket -= 1; i += 2; continue
        if c == "|" and depth_brace == 0 and depth_bracket == 0:
            parts.append(s[start:i])
            start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


def _convert_template(node: Any, state: _ConvertState) -> str:
    """Convert ``{{Name|args}}`` — either to a built-in call, a
    frontmatter field, or an error placeholder.
    """
    name = str(node.name).strip()

    # Parser functions → WikiMark built-in template calls
    if name.startswith("#"):
        return _convert_parser_function(node, name, state)

    # Magic words with colon syntax → built-in template calls
    if ":" in name:
        prefix, _, value = name.partition(":")
        prefix = prefix.strip()
        if prefix in _MAGIC_PREFIX_MAP:
            clean = re.sub(r"'{2,5}", "", value.strip())
            state.set_fm(_MAGIC_PREFIX_MAP[prefix], clean)
            return ""
        args: dict[str, str] = {"1": value.strip()}
        for i, param in enumerate(node.params, start=2):
            key = str(param.name).strip()
            if param.showkey:
                args[key] = _convert_nodes(param.value.nodes, state).strip()
            else:
                args[str(i)] = _convert_nodes(param.value.nodes, state).strip()
        return _emit_template_call(prefix.lower(), args)

    mapping = tpl.lookup(name)

    # Inline template recursion: render child nodes of each param so
    # nested wiki links / templates in args convert cleanly.
    args: dict[str, str] = {}
    for i, param in enumerate(node.params, start=1):
        key = str(param.name).strip()
        if param.showkey:
            k = key
        else:
            k = str(i)
        value = _convert_nodes(param.value.nodes, state).strip()
        args[k] = value

    if mapping is None:
        if name.strip().lower() == "infobox":
            args = _convert_mw_infobox_args(args)
        normalized = _kebabize(name)
        return _emit_template_call(normalized, args)

    if mapping.target is None:
        if name in tpl.FRONTMATTER_TEMPLATES:
            state.set_fm("title_style", "italic")
            return ""
        normalized = _kebabize(name)
        return _emit_template_call(normalized, args)

    converted_args = mapping.apply(args)
    return _emit_template_call(mapping.target, converted_args)



def _convert_parser_function(node: Any, name: str, state: _ConvertState) -> str:
    """Convert MediaWiki parser functions to WikiMark built-in template calls.

    {{#if:cond|then|else}} → {{if "cond" "then" "else"}}
    {{#switch:val|a=x|b=y}} → {{switch "val" a="x" b="y"}}
    {{#subobject:...|...}} → WikiMark semantic annotations
    {{#invoke:...}} / {{#ask:...}} → error (needs engine-side implementation)
    """
    func, _, condition = name.partition(":")
    func = func.strip().lower()
    condition = condition.strip()

    # #subobject → WikiMark semantic annotation
    if func == "#subobject":
        props = []
        if condition.strip():
            props.append(f'_id="{condition.strip()}"')
        for param in node.params:
            if param.showkey:
                key = str(param.name).strip()
                val = _convert_nodes(param.value.nodes, state).strip()
                if key and val:
                    props.append(f'{key}="{val}"')
        if not props:
            return ""
        state._pending_annotation = " ".join(props)
        return ""

    # #invoke → error (Lua modules can't be auto-converted)
    if func == "#invoke":
        state.report.add("lua-invoke", state.title, condition)
        raw = str(node)
        return f"**#INVOKE DOES NOT SUPPORT AUTOMATIC CONVERSION** `{raw}`"

    # #ask → error (needs Trellis query engine)
    if func == "#ask":
        state.report.add("smw-query", state.title, "#ask")
        raw = str(node)
        return f"**#ASK DOES NOT SUPPORT AUTOMATIC CONVERSION** `{raw}`"

    # All other parser functions → built-in template calls
    template_name = func.lstrip("#")
    cond_value = _convert_nodes(mwp.parse(condition).nodes, state).strip() if condition else ""

    args: dict[str, str] = {}
    if cond_value:
        args["1"] = cond_value

    offset = 2 if cond_value else 1
    for i, param in enumerate(node.params, start=offset):
        key = str(param.name).strip()
        if param.showkey:
            args[key] = _convert_nodes(param.value.nodes, state).strip()
        else:
            args[str(i)] = _convert_nodes(param.value.nodes, state).strip()

    return _emit_template_call(template_name, args)




_MW_INFOBOX_SKIP = {
    "bodystyle", "bodyclass", "titlestyle", "abovestyle", "belowstyle",
    "headerstyle", "labelstyle", "datastyle", "imagestyle",
    "aboveclass", "belowclass", "headerclass", "imageclass",
    "above", "below", "subheader", "subheaderstyle", "subheaderclass",
    "child", "embed", "decat", "templatestyles",
}

_MW_INFOBOX_SKIP_RE = re.compile(
    r"(style|class|colspan|rowspan)$|"
    r"^(row|child|embed|decat|templatestyles|subbox|autoheaders|bodyclass|bodystyle)"
)

_LABEL_NUM_RE = re.compile(r"^(?:label|header)(\d+)$")
_DATA_NUM_RE = re.compile(r"^data(\d+)$")
_HEADER_NUM_RE = re.compile(r"^header(\d+)$")


def _convert_mw_infobox_args(args: dict[str, str]) -> dict[str, str]:
    """Convert MW Infobox numbered label/data to named key=value.

    label29="Country" data29="Empire" → country="Empire"
    label29="${subdivision_type}" data29="${subdivision_name}" → subdivision-type="${subdivision_name}"
    """
    out: dict[str, str] = {}

    # Extract special fields
    name = args.get("above", args.get("title", args.get("name", "")))
    if name and name.strip():
        out["name"] = name
    for k in ("image", "caption", "image_caption", "subtitle", "subheader"):
        v = args.get(k, "")
        if v and v.strip() and not v.strip().startswith("<!--"):
            out_key = "caption" if k == "image_caption" else k
            out[out_key] = v

    # Collect numbered pairs
    labels: dict[int, str] = {}
    data: dict[int, str] = {}
    headers: dict[int, str] = {}

    for key, value in args.items():
        lk = key.lower().strip()
        m = _HEADER_NUM_RE.match(lk)
        if m:
            if value.strip() and not value.strip().startswith("<!--"):
                headers[int(m.group(1))] = value.strip()
            continue
        m = _LABEL_NUM_RE.match(lk)
        if m:
            labels[int(m.group(1))] = value.strip()
            continue
        m = _DATA_NUM_RE.match(lk)
        if m:
            data[int(m.group(1))] = value.strip()
            continue

    # Convert numbered pairs to named fields
    for num in sorted(set(data.keys()) | set(headers.keys())):
        if num in headers:
            continue
        val = data.get(num, "")
        if not val or val.startswith("<!--"):
            continue
        label = labels.get(num, "")
        if not label:
            continue

        # Clean the label text
        clean = re.sub(r"&nbsp;|&#160;", " ", label)
        clean = re.sub(r"<[^>]+>", "", clean)
        clean = re.sub(r"[•·]", "", clean).strip()

        # Dynamic key: ${var} stays as-is — resolves at render time
        if re.match(r"^\$\{[\w]+\}$", clean):
            out[clean] = val
        elif clean and re.match(r"^[a-zA-Z]", clean):
            out[_kebabize(clean)] = val

    return out


def _is_mw_cruft(value: str) -> bool:
    """Check if a value is empty or an MW placeholder, not meaningful data."""
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.startswith("<!--"):
        return True
    return False


_MW_INFOBOX_LABEL_RE = re.compile(r"^(?:label|header)(\d+)$")
_MW_INFOBOX_DATA_RE = re.compile(r"^data(\d+)$")


def _convert_mw_infobox(args: dict[str, str]) -> str:
    """Convert MW's base Infobox call to a clean Trellis infobox.

    Extracts meaningful data fields, skipping all MW layout/style
    params and complex computed values.
    """
    out: dict[str, str] = {}

    name = args.get("title", args.get("above", args.get("name", "")))
    if name and not _is_mw_cruft(name):
        out["name"] = name

    for k in ("image", "caption", "image_caption", "subtitle"):
        v = args.get(k, "")
        if v and not _is_mw_cruft(v):
            out[k] = v

    labels: dict[str, str] = {}
    data: dict[str, str] = {}

    for key, value in args.items():
        lk = key.lower().strip()
        if not re.match(r"^[a-z][a-z0-9_ ]*$", lk):
            continue
        if lk in _MW_INFOBOX_SKIP:
            continue
        if any(lk.startswith(p) for p in ("row", "child", "class", "colspan",
                                           "header", "below", "above", "sub")):
            continue
        if any(lk.endswith(s) for s in ("style", "class", "footnotes")):
            continue

        m_label = _MW_INFOBOX_LABEL_RE.match(lk)
        if m_label:
            labels[m_label.group(1)] = value.strip()
            continue

        m_data = _MW_INFOBOX_DATA_RE.match(lk)
        if m_data:
            data[m_data.group(1)] = value.strip()
            continue

    for num in sorted(data.keys(), key=lambda x: int(x)):
        val = data[num]
        if not val or _is_mw_cruft(val):
            continue
        label = labels.get(num, "")
        if not label or _is_mw_cruft(label):
            continue
        # If label is a variable reference ${foo}, use the variable name
        var_match = re.match(r"^\s*\$\{(\w+)\}\s*$", label)
        if var_match:
            clean = var_match.group(1)
        else:
            clean = re.sub(r"&nbsp;|&amp;nbsp;|&#160;", " ", label)
            clean = re.sub(r"<[^>]+>", "", clean)
            clean = re.sub(r"\$\{[^}]*\}", "", clean)
            clean = re.sub(r"\{\{[^}]*\}\}", "", clean)
            clean = re.sub(r"[^a-zA-Z0-9 _-]", "", clean).strip()
        if clean and re.match(r"^[a-zA-Z]", clean):
            out[_kebabize(clean)] = val

    return _emit_template_call("infobox", out)


def _kebabize(name: str) -> str:
    """Convert a MediaWiki-style name to a kebab-case identifier."""
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _emit_template_call(name: str, args: dict[str, str]) -> str:
    """Emit a WikiMark ``{{name ...}}`` call with the given args."""
    if not args:
        return f"{{{{{name}}}}}"
    parts = []
    for key, value in args.items():
        if not value:
            continue
        # Keys that parse as positional in WikiMark (numeric) stay
        # positional. Otherwise we always quote values.
        if key.isdigit():
            parts.append(f'"{_escape_arg(value)}"')
        else:
            parts.append(f'{key}="{_escape_arg(value)}"')
    return f"{{{{{name} {' '.join(parts)}}}}}"


def _escape_arg(value: str) -> str:
    """Escape a value for use inside a WikiMark template arg."""
    # Collapse newlines (WikiMark templates don't currently support
    # multi-line) and escape embedded double quotes.
    value = value.replace("\n", " ").strip()
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _convert_external_link(node: Any, state: _ConvertState) -> str:
    """``[http://example.com label]`` and bare ``http://example.com``."""
    url = str(node.url)
    if node.title:
        return f"[{_convert_nodes(node.title.nodes, state)}]({url})"
    # Bare or numbered link — GFM handles autolinks for `<url>` form
    return f"<{url}>"


def _convert_heading(node: Any, state: _ConvertState) -> str:
    """``== Heading ==`` → ``## Heading``."""
    level = node.level
    text = _convert_nodes(node.title.nodes, state).strip()
    return "\n" + "#" * level + " " + text + "\n"


# Extension tags we know how to handle
def _convert_tag(node: Any, state: _ConvertState) -> str:
    tag = str(node.tag).lower()

    # MediaWiki quote-based emphasis. mwparserfromhell surfaces
    #   '''bold''' → Tag(b, wiki_markup="'''")
    #   ''italic'' → Tag(i, wiki_markup="''")
    # Convert to GFM **...** / *...*. Anything else with tag=b/i is
    # presumed to be raw HTML and passes through.
    wiki_markup = getattr(node, "wiki_markup", None)
    if wiki_markup in ("'''", "''", "'''''"):
        inner = (_convert_nodes(node.contents.nodes, state)
                 if node.contents else "")
        marker = {"''": "*", "'''": "**", "'''''": "***"}[wiki_markup]
        return f"{marker}{inner}{marker}"

    # MediaWiki list markers: mwparserfromhell parses ``*item`` as
    # self-closing Tag(li, wiki_markup='*'). Consecutive markers
    # (``**item``) produce multiple Tag nodes. We accumulate depth
    # and emit the GFM list prefix when the next non-marker node
    # consumes it (via _flush_list_marker).
    if tag == "li" and wiki_markup in ("*", "#"):
        state._list_depth += 1
        state._list_type = wiki_markup
        return ""

    if tag == "ref":
        return _convert_ref(node, state)
    if tag == "references":
        # Footnotes auto-emit at page end; this is a no-op.
        return ""
    if tag == "nowiki":
        # Treat as literal text, wrapped in backticks if content
        # likely to contain markdown-meaningful characters.
        content = str(node.contents) if node.contents else ""
        if any(c in content for c in "*_`[]{}"):
            return f"`{content}`"
        return content

    # HTML-ish inline tags from MediaWiki syntax — pass through for GFM
    # The GFM spec allows raw HTML; libwikimark's OPT_UNSAFE preserves it.
    if tag in ("b", "i", "u", "s", "strike", "em", "strong",
               "sub", "sup", "small", "big", "code", "tt", "pre",
               "blockquote", "cite", "kbd", "samp", "var", "div", "span",
               "br", "hr", "p", "center", "dl", "dt", "dd", "ol", "ul", "li"):
        return _passthrough_html_tag(node, state)

    if tag == "syntaxhighlight" or tag == "source":
        return _convert_syntaxhighlight(node, state)

    if tag == "poem":
        inner = _convert_nodes(node.contents.nodes, state) if node.contents else ""
        lines = inner.strip().split("\n")
        return "\n" + "\\\n".join(lines) + "\n"

    if tag == "math":
        # Preserve as raw HTML; Trellis can render with MathJax later.
        return str(node)

    if tag == "dpl":
        state.report.add("dpl-block", state.title, "<dpl>...</dpl>")
        raw = str(node)
        return f"\n\n**#DPL DOES NOT SUPPORT AUTOMATIC CONVERSION**\n\n```\n{raw}\n```\n\n"

    if tag == "includeonly":
        return _convert_nodes(node.contents.nodes, state) if node.contents else ""

    if tag == "noinclude":
        _extract_noinclude_metadata(node, state)
        return ""

    if tag == "onlyinclude":
        return _convert_nodes(node.contents.nodes, state) if node.contents else ""

    if tag == "templatedata":
        _extract_templatedata(node, state)
        return ""

    # Unknown tag — pass through raw and report
    state.report.add("unknown-tag", state.title, tag)
    return str(node)


_CATEGORY_RE = re.compile(
    r"\[\[Category:([^\]|]+)(?:\|[^\]]*)?\]\]", re.IGNORECASE
)


def _passthrough_html_tag(node: Any, state: _ConvertState) -> str:
    """Pass through an HTML tag, but recurse into contents to convert
    any child nodes (e.g. template arguments on template pages)."""
    if node.self_closing or not node.contents:
        return str(node)
    from mwparserfromhell.nodes import Argument
    if not any(isinstance(c, Argument) for c in node.contents.nodes):
        return str(node)
    attrs = str(node)[: str(node).index(">") + 1]
    inner = _convert_nodes(node.contents.nodes, state)
    tag = str(node.tag).lower()
    return f"{attrs}{inner}</{tag}>"


def _extract_noinclude_metadata(node: Any, state: _ConvertState) -> None:
    """Extract meaningful metadata from a <noinclude> block into frontmatter.

    Looks for <templatedata> JSON and [[Category:...]] links. Everything
    else ({{documentation}}, HTML comments, maintenance templates) is
    discarded — it's MediaWiki infrastructure with no WikiMark equivalent.
    """
    if not node.contents:
        return

    from mwparserfromhell.nodes import Tag, Wikilink

    for child in node.contents.nodes:
        if isinstance(child, Tag) and str(child.tag).lower() == "templatedata":
            _extract_templatedata(child, state)
        elif isinstance(child, Wikilink):
            target = str(child.title).strip()
            if target.lower().startswith("category:"):
                cat_name = target[len("category:"):].strip()
                if cat_name:
                    state.add_category(cat_name)
        else:
            raw = str(child)
            for m in _CATEGORY_RE.finditer(raw):
                state.add_category(m.group(1).strip())


def _extract_templatedata(node: Any, state: _ConvertState) -> None:
    """Parse <templatedata> JSON and add parameter schema to frontmatter."""
    raw = str(node.contents) if node.contents else ""
    raw = raw.strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    params = data.get("params")
    if not isinstance(params, dict):
        return

    schema: dict[str, Any] = {}
    for key, spec in params.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {}
        desc = spec.get("description") or spec.get("label", "")
        example = spec.get("example")
        if desc and example:
            desc = f"{desc}. Example: \"{example}\""
        elif not desc and example:
            desc = f"Example: \"{example}\""
        if desc:
            entry["description"] = desc
        if "default" in spec:
            entry["default"] = spec["default"]
        schema[key] = entry

    if schema:
        state.frontmatter["inputs"] = schema

    desc = data.get("description")
    if desc and isinstance(desc, str):
        state.frontmatter.setdefault("description", desc)


def _convert_ref(node: Any, state: _ConvertState) -> str:
    """``<ref>...</ref>`` and ``<ref name="foo"/>`` → GFM footnote."""
    ref_name = ""
    if node.attributes:
        for attr in node.attributes:
            key = str(attr.name).strip()
            if key == "name":
                ref_name = str(attr.value).strip().strip('"').strip("'")

    if node.self_closing or not node.contents:
        # Reuse — just emit the marker
        return f"[^{ref_name}]" if ref_name else ""

    content = _convert_nodes(node.contents.nodes, state).strip()
    return state.new_footnote(content, ref_name=ref_name)


def _convert_syntaxhighlight(node: Any, state: _ConvertState) -> str:
    """``<syntaxhighlight lang="py">...</syntaxhighlight>`` → fenced code."""
    lang = ""
    if node.attributes:
        for attr in node.attributes:
            key = str(attr.name).strip()
            if key == "lang":
                lang = str(attr.value).strip().strip('"').strip("'")
    content = str(node.contents) if node.contents else ""
    return f"\n```{lang}\n{content}\n```\n"
