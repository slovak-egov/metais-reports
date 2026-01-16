#!/usr/bin/env python3
"""
Smart JSON writer:
- Tries to keep small objects/arrays on a single line.
- If they don't fit within max_width, they are pretty-printed with indentation.
- For very large containers (many items), we skip the inline attempt for speed.
"""

from __future__ import annotations
import json
from typing import Any, TextIO


def _dumps_inline(obj: Any, ensure_ascii: bool) -> str:
    """Fully inline JSON for a subtree, no newlines."""
    return json.dumps(obj, ensure_ascii=ensure_ascii, separators=(",", ":"))


def _dumps_inline_container(obj: Any, ensure_ascii: bool) -> str:
    """Inline representation for container children when building inline parents."""
    return _dumps_inline(obj, ensure_ascii=ensure_ascii)


def _dumps_smart(
    obj: Any,
    level: int,
    *,
    indent: int,
    max_width: int,
    ensure_ascii: bool,
    max_items_inline: int,
) -> str:
    """
    Core recursive function that decides inline vs multi-line
    for dicts and lists. Scalars are always inline.
    """
    # ---------- scalars ----------
    if not isinstance(obj, (dict, list, tuple)):
        return json.dumps(obj, ensure_ascii=ensure_ascii)

    indent_str = " " * (level * indent)

    # ---------- dict ----------
    if isinstance(obj, dict):
        if not obj:
            return "{}"

        items = list(obj.items())
        # If too many items, don't even try an inline representation
        if len(items) <= max_items_inline:
            # -- attempt inline --
            items_inline = []
            for k, v in items:
                key_s = json.dumps(k, ensure_ascii=ensure_ascii)
                val_s = _dumps_inline_container(v, ensure_ascii=ensure_ascii)
                items_inline.append(f"{key_s}:{val_s}")
            inline_str = "{" + ",".join(items_inline) + "}"

            if len(indent_str) + len(inline_str) <= max_width:
                return inline_str

        # -- multi-line fallback --
        lines = ["{"]
        n = len(items)
        for i, (k, v) in enumerate(items):
            key_s = json.dumps(k, ensure_ascii=ensure_ascii)
            val_s = _dumps_smart(
                v,
                level + 1,
                indent=indent,
                max_width=max_width,
                ensure_ascii=ensure_ascii,
                max_items_inline=max_items_inline,
            )
            child_indent = " " * ((level + 1) * indent)
            comma = "," if i < n - 1 else ""
            lines.append(f"{child_indent}{key_s}: {val_s}{comma}")

        lines.append(indent_str + "}")
        return "\n".join(lines)

    # ---------- list / tuple ----------
    seq = list(obj)

    if not seq:
        return "[]"

    # For very large lists (e.g. entities, big edge lists), skip inline attempt
    if len(seq) <= max_items_inline:
        elems_inline = [
            _dumps_inline_container(v, ensure_ascii=ensure_ascii)
            for v in seq
        ]
        inline_str = "[" + ",".join(elems_inline) + "]"
        if len(indent_str) + len(inline_str) <= max_width:
            return inline_str

    # -- multi-line fallback --
    lines = ["["]
    n = len(seq)
    for i, v in enumerate(seq):
        val_s = _dumps_smart(
            v,
            level + 1,
            indent=indent,
            max_width=max_width,
            ensure_ascii=ensure_ascii,
            max_items_inline=max_items_inline,
        )
        child_indent = " " * ((level + 1) * indent)
        comma = "," if i < n - 1 else ""
        lines.append(f"{child_indent}{val_s}{comma}")
    lines.append(indent_str + "]")
    return "\n".join(lines)


def dumps_json_smart(
    obj: Any,
    *,
    max_width: int = 80,
    indent: int = 2,
    ensure_ascii: bool = False,
    max_items_inline: int = 8,
) -> str:
    """
    Return a JSON string formatted with the smart line-breaking rules.

    max_items_inline:
      - For dicts/lists with more than this many items, skip inline attempt
        and go directly to multi-line formatting (for speed).
    """
    return _dumps_smart(
        obj,
        level=0,
        indent=indent,
        max_width=max_width,
        ensure_ascii=ensure_ascii,
        max_items_inline=max_items_inline,
    ) + "\n"


def dump_json_smart(
    obj: Any,
    fp: TextIO,
    *,
    max_width: int = 80,
    indent: int = 2,
    ensure_ascii: bool = False,
    max_items_inline: int = 12,
) -> None:
    """
    Write JSON to an open file-like object using smart formatting.
    """
    fp.write(
        dumps_json_smart(
            obj,
            max_width=max_width,
            indent=indent,
            ensure_ascii=ensure_ascii,
            max_items_inline=max_items_inline,
        )
    )