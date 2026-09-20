"""Deterministic RFC 6902 JSON Patch generation between two JSON documents.

Diff rules:
  * objects recurse key-by-key, emitting add/remove/replace at leaf paths
  * arrays are opaque: a changed array yields a single "replace" op carrying
    the full final array, never per-index ops
  * any other type change at one path is a single "replace"

Paths are RFC 6901 JSON Pointers ("~" -> "~0", "/" -> "~1") and the op list
is sorted by path lexicographically. Every touched path is disjoint from the
others, so applying the ops in that order to the base document yields the
result exactly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .merge import escape


def generate_patch(base: Any, result: Any) -> list[dict]:
    """Build the deterministic JSON Patch (RFC 6902) from base to result."""
    ops: list[dict] = []
    _diff(base, result, "", ops)
    ops.sort(key=lambda op: op["path"])
    return ops


def _diff(base: Any, result: Any, path: str, ops: list[dict]) -> None:
    if _json_equal(base, result):
        return
    if isinstance(base, dict) and isinstance(result, dict):
        for key in sorted(base.keys() | result.keys()):
            child = path + "/" + escape(key)
            if key not in result:
                ops.append({"op": "remove", "path": child})
            elif key not in base:
                ops.append({"op": "add", "path": child, "value": result[key]})
            else:
                _diff(base[key], result[key], child, ops)
        return
    # Scalars, arrays (opaque -> whole-array replace) and type changes.
    # At the root (path == "") RFC 6901 "" names the whole document, so a
    # root-level "replace" is likewise valid.
    ops.append({"op": "replace", "path": path, "value": result})


def _json_equal(a: Any, b: Any) -> bool:
    """Deep equality with JSON-exact types.

    Unlike Python's ``==`` this distinguishes ``True`` from ``1`` and ``1``
    from ``1.0`` (and the same distinction inside arrays), so such changes
    are never silently dropped from the patch.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(
            _json_equal(x, y) for x, y in zip(a, b)
        )
    return a == b


def canonical_json(value: Any) -> bytes:
    """Canonical serialization used for content hashing.

    Keys are sorted and whitespace is stripped, so semantically identical
    documents hash identically regardless of key order.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """SHA-256 hex digest of a JSON document's canonical serialization."""
    return hashlib.sha256(canonical_json(value)).hexdigest()
