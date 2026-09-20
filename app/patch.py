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

import copy
import hashlib
import json
from typing import Any

from .merge import escape, parse_pointer


class PatchApplicationError(Exception):
    """An RFC 6902 patch op cannot be applied to the target document."""


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


# ---------------------------------------------------------------------------
# RFC 6902 patch application (add / remove / replace), strict enough to
# replay the patches this package generates.
# ---------------------------------------------------------------------------


def _navigate_parent(doc: Any, tokens: list[str], op: str):
    """Walk ``tokens[:-1]`` and return ``(parent, last)`` for an op.

    Raises PatchApplicationError when the parent path does not exist or a
    segment is not a valid object/array index.
    """
    parent = doc
    for token in tokens[:-1]:
        if isinstance(parent, dict):
            if token not in parent:
                raise PatchApplicationError(
                    f"{op}: parent key {token!r} does not exist"
                )
            parent = parent[token]
        elif isinstance(parent, list):
            index = _array_index(token, len(parent), op, allow_end=False)
            parent = parent[index]
        else:
            raise PatchApplicationError(
                f"{op}: path segment {token!r} traverses a non-container"
            )
    last = tokens[-1]
    if isinstance(parent, list):
        last = _array_index(last, len(parent), op, allow_end=(op == "add"))
    elif not isinstance(parent, dict):
        raise PatchApplicationError(
            f"{op}: target parent is neither an object nor an array"
        )
    return parent, last


def _array_index(token: str, length: int, op: str, allow_end: bool) -> int:
    if token == "-":
        # "-" addresses the (nonexistent) element after the last one; only
        # an add can use it, and replace/remove cannot target it.
        if allow_end:
            return length
        raise PatchApplicationError(f"{op}: '-' index is only valid for add")
    try:
        index = int(token)
    except (TypeError, ValueError):
        raise PatchApplicationError(
            f"{op}: array index {token!r} is not an integer"
        ) from None
    if index < 0 or token != str(index):
        # RFC 6902 forbids negative indexes and leading zeros ("01").
        raise PatchApplicationError(f"{op}: invalid array index {token!r}")
    if allow_end:
        if index > length:
            raise PatchApplicationError(
                f"{op}: array index {index} past end (length {length})"
            )
    elif index >= length:
        raise PatchApplicationError(
            f"{op}: array index {index} out of range (length {length})"
        )
    return index


def apply_rfc6902_patch(doc: Any, patch: list[dict]) -> Any:
    """Apply an RFC 6902 JSON Patch (add/remove/replace) to a deep copy.

    Arrays follow the standard index semantics (add inserts at the index or
    after the end; remove/replace target an existing element). The generated
    patches never use root removes, but the applier implements them for a
    faithful replay. Raises PatchApplicationError on any malformed op or
    unresolvable pointer.
    """
    if not isinstance(patch, list):
        raise PatchApplicationError("patch must be a list of operations")
    result = copy.deepcopy(doc)
    for op in patch:
        if not isinstance(op, dict) or "op" not in op or "path" not in op:
            raise PatchApplicationError(
                "every patch op needs 'op' and 'path' fields"
            )
        name = op["op"]
        if name not in ("add", "remove", "replace"):
            raise PatchApplicationError(f"unsupported patch op: {name!r}")
        try:
            tokens = parse_pointer(op["path"])
        except ValueError as exc:
            raise PatchApplicationError(str(exc)) from None
        if not tokens:
            if name == "remove":
                result = None
            else:
                result = copy.deepcopy(op["value"])
            continue
        parent, last = _navigate_parent(result, tokens, name)
        if name == "add":
            if isinstance(parent, list):
                parent.insert(last, copy.deepcopy(op["value"]))
            else:
                parent[last] = copy.deepcopy(op["value"])
        elif name == "remove":
            if isinstance(parent, list):
                del parent[last]
            elif last not in parent:
                raise PatchApplicationError(
                    f"remove: key {last!r} does not exist"
                )
            else:
                del parent[last]
        else:  # replace
            if isinstance(parent, list):
                parent[last] = copy.deepcopy(op["value"])
            elif last not in parent:
                raise PatchApplicationError(
                    f"replace: key {last!r} does not exist"
                )
            else:
                parent[last] = copy.deepcopy(op["value"])
    return result
