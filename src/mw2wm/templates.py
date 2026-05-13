"""
MediaWiki template call → WikiMark template call.

Built-in mappings cover common templates (infoboxes, hatnotes, etc.).
Wiki-specific templates are handled via a plugin system:

1. **YAML mappings** — ``templates.yaml`` in the input directory for
   simple name/arg remapping (most common case).
2. **Python plugins** — ``templates/*.py`` in the input directory for
   templates that need custom conversion logic.

YAML format::

    My Template:
      target: my-module
      positional:
        "1": title
        "2": date
      rename:
        old_field: new_field
      drop:
        - unwanted_key

    Decoration Only:
      target: null  # drop entirely

Python plugin format::

    # templates/my_complex_template.py
    from mw2wm.templates import TemplateMapping

    TEMPLATES = {
        "Complex Template": TemplateMapping(
            target="complex-module",
            positional={"1": "name"},
        ),
    }

    # Or for full control, define a convert function:
    def convert_complex(args):
        name = args.get("1", "")
        return {"target": "complex-module", "name": name.upper()}
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger("mw2wm.templates")


@dataclass
class TemplateMapping:
    """How to convert one MediaWiki template to a WikiMark call.

    Set ``target`` to a module name for ``{{target args}}``, or
    ``None`` to drop the template entirely.

    Set ``inline`` to a format string (e.g. ``"{2}"``) to emit
    plain text instead of a template call. Positional args are
    available as ``{1}``, ``{2}``, etc.; named args as ``{name}``.
    """

    target: str | None
    positional: dict[str, str] = field(default_factory=dict)
    rename: dict[str, str] = field(default_factory=dict)
    drop_keys: set[str] = field(default_factory=set)
    convert_fn: Callable[[dict[str, str]], dict[str, str]] | None = None
    inline: str | None = None

    def apply(self, args: Mapping[str, str]) -> dict[str, str]:
        if self.convert_fn is not None:
            result = self.convert_fn(dict(args))
            if isinstance(result, dict):
                target = result.pop("target", self.target)
                if target is not None:
                    self.target = target
                return result

        out: dict[str, str] = {}
        for key, value in args.items():
            if key in self.drop_keys:
                continue
            new_key = self.positional.get(key, self.rename.get(key, key))
            out[new_key] = value
        return out

    def render_inline(self, args: Mapping[str, str]) -> str | None:
        """If this mapping has an inline format, render it. Returns
        None if no inline format is set.

        Uses ``$1``, ``$2`` for positional args and ``$name`` for
        named args (dollar-sign prefix avoids Python format conflicts).
        """
        if self.inline is None:
            return None
        result = self.inline
        for key, value in args.items():
            result = result.replace(f"${key}", value)
        return result


TEMPLATE_MAPPINGS: dict[str, TemplateMapping] = {
    "Italic title": TemplateMapping(target=None),
}

FRONTMATTER_TEMPLATES = {"Italic title"}

# ── Plugin registry ────────────────────────────────────────────

_custom_mappings: dict[str, TemplateMapping] = {}


def _normalize_name(name: str) -> str:
    normalized = name.strip().replace("_", " ")
    if normalized:
        normalized = normalized[0].upper() + normalized[1:]
    return normalized


def lookup(name: str) -> TemplateMapping | None:
    normalized = _normalize_name(name)
    return _custom_mappings.get(normalized) or TEMPLATE_MAPPINGS.get(normalized)


def load_plugins(input_dir: Path) -> int:
    """Load template plugins from the input directory.

    Looks for:
    - ``<input_dir>/templates.yaml`` — YAML mappings
    - ``<input_dir>/templates/*.py`` — Python plugin files

    Returns the number of custom mappings loaded.
    """
    count = 0

    yaml_file = input_dir / "templates.yaml"
    if yaml_file.is_file():
        count += _load_yaml(yaml_file)

    plugins_dir = input_dir / "templates"
    if plugins_dir.is_dir():
        for py_file in sorted(plugins_dir.glob("*.py")):
            count += _load_python_plugin(py_file)

    if count:
        logger.info("Loaded %d custom template mappings", count)
    return count


def _load_yaml(path: Path) -> int:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        logger.warning("templates.yaml: expected dict, got %s", type(data).__name__)
        return 0

    count = 0
    for name, spec in data.items():
        normalized = _normalize_name(str(name))
        if isinstance(spec, dict):
            target = spec.get("target")
            inline = spec.get("inline")
            mapping = TemplateMapping(
                target=target,
                positional={str(k): str(v) for k, v in spec.get("positional", {}).items()},
                rename={str(k): str(v) for k, v in spec.get("rename", {}).items()},
                drop_keys=set(str(k) for k in spec.get("drop", [])),
                inline=inline,
            )
        elif spec is None:
            mapping = TemplateMapping(target=None)
        else:
            logger.warning("templates.yaml: skipping %r (unexpected value type)", name)
            continue

        _custom_mappings[normalized] = mapping
        count += 1
        logger.debug("YAML: loaded %r → %s", normalized, mapping.target)

    return count


def _load_python_plugin(path: Path) -> int:
    module_name = f"mw2wm_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        logger.warning("Could not load plugin: %s", path)
        return 0

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        logger.warning("Plugin %s failed to load: %s", path.name, e)
        return 0

    count = 0

    # Look for TEMPLATES dict
    templates = getattr(module, "TEMPLATES", {})
    if isinstance(templates, dict):
        for name, mapping in templates.items():
            normalized = _normalize_name(str(name))
            if isinstance(mapping, TemplateMapping):
                _custom_mappings[normalized] = mapping
                count += 1
            elif isinstance(mapping, dict):
                _custom_mappings[normalized] = TemplateMapping(**mapping)
                count += 1
            logger.debug("Plugin %s: loaded %r", path.name, normalized)

    # Look for convert_* functions
    for attr_name in dir(module):
        if attr_name.startswith("convert_"):
            fn = getattr(module, attr_name)
            if callable(fn):
                template_name = attr_name[len("convert_"):].replace("_", " ")
                normalized = _normalize_name(template_name)
                if normalized not in _custom_mappings:
                    _custom_mappings[normalized] = TemplateMapping(
                        target=None, convert_fn=fn
                    )
                    count += 1
                    logger.debug("Plugin %s: loaded converter %r", path.name, normalized)

    return count


def reset_plugins():
    """Clear all custom mappings (useful for testing)."""
    _custom_mappings.clear()
