"""Portable evidence packages for completed (resolved) merge orders.

An evidence package is a self-contained, normalised export of one resolved
merge order:

  * ``merge_id`` / ``status`` / ``generated_at``
  * the original ``base`` document
  * the immutable ``result`` document
  * the deterministic RFC 6902 ``patch`` taking base to result
  * the complete adjudication ``audit`` chain with its SHA-256 links
  * an HMAC-SHA256 ``signature`` over the canonical JSON of every other field

Two interfaces use it:

  * ``GET /merges/{merge_id}/evidence`` builds the package from the database
    (a pending merge yields 409; an unknown one 404);
  * ``POST /evidence/verify`` verifies a presented package **without touching
    the database**, in a fixed stage order — signature first (constant-time
    comparison), then audit-chain continuity, then an actual replay of the
    patch — and reports the first failing stage and its reason.

The HMAC key comes from the ``EVIDENCE_SECRET_KEY`` environment variable. The
application refuses to start when the variable is unset/blank or shorter than
``MIN_KEY_LENGTH`` UTF-8 bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any

from .audit import GENESIS_PREV_HASH, event_payload
from .patch import (
    PatchApplicationError,
    _json_equal,
    apply_rfc6902_patch,
    canonical_json,
    generate_patch,
    sha256_hex,
)

ENV_KEY = "EVIDENCE_SECRET_KEY"
MIN_KEY_LENGTH = 32

SIGNATURE_ALGORITHM = "HMAC-SHA256"

# Verification stages, always evaluated in exactly this order; only the first
# failing stage is reported.
STAGE_SIGNATURE = "signature"
STAGE_AUDIT_CHAIN = "audit_chain"
STAGE_PATCH_REPLAY = "patch_replay"
STAGES = (STAGE_SIGNATURE, STAGE_AUDIT_CHAIN, STAGE_PATCH_REPLAY)

_signing_key: bytes | None = None


class EvidenceKeyError(RuntimeError):
    """The HMAC signing key is missing or too short for the app to start."""


def load_signing_key(env: dict | None = None) -> bytes:
    """Resolve and validate the evidence signing key from the environment.

    Reads ``EVIDENCE_SECRET_KEY`` (overrideable with an explicit ``env`` map for
    tests); raises EvidenceKeyError when it is unset/blank or shorter than
    ``MIN_KEY_LENGTH`` UTF-8 bytes.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_KEY)
    if raw is None or not raw.strip():
        raise EvidenceKeyError(
            f"{ENV_KEY} is not set: a non-empty secret key of at least "
            f"{MIN_KEY_LENGTH} characters is required to sign evidence packages"
        )
    key = raw.encode("utf-8")
    if len(key) < MIN_KEY_LENGTH:
        raise EvidenceKeyError(
            f"{ENV_KEY} is too short: it must be at least {MIN_KEY_LENGTH} "
            f"UTF-8 bytes (got {len(key)})"
        )
    return key


def init_signing_key(env: dict | None = None) -> bytes:
    """Validate and cache the signing key; called once from the lifespan.

    A failure raises EvidenceKeyError and aborts application startup.
    """
    global _signing_key
    _signing_key = load_signing_key(env)
    return _signing_key


def _key() -> bytes:
    if _signing_key is None:
        # Defensive: the lifespan initialises the key at startup, so this only
        # runs for callers importing the module without the lifespan (tests).
        return init_signing_key()
    return _signing_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_evidence_package(
    merge_id: str,
    base: Any,
    result: Any,
    patch: list[dict],
    audit_events: list[dict],
    generated_at: str | None = None,
) -> dict:
    """Assemble the normalised, signed evidence payload for one merge order.

    ``audit_events`` are the stored trail events, including their
    verification-only ``valid`` field; that field is not part of the signed
    package and is stripped here (verification recomputes the verdict).
    """
    exported_events = [
        {k: v for k, v in event.items() if k != "valid"} for event in audit_events
    ]
    return {
        "merge_id": merge_id,
        "status": "resolved",
        "generated_at": generated_at or _now(),
        "algorithm": SIGNATURE_ALGORITHM,
        "base": base,
        "result": result,
        "patch": patch,
        "audit": {
            "merge_id": merge_id,
            "event_count": len(exported_events),
            "events": exported_events,
            "genesis_prev_hash": GENESIS_PREV_HASH,
        },
        "signature": "",
    }


def sign_package(package: dict, key: bytes | None = None) -> str:
    """HMAC-SHA256 hex digest over canonical JSON of the package sans signature."""
    signed = {k: v for k, v in package.items() if k != "signature"}
    return hmac.new(
        key if key is not None else _key(), canonical_json(signed), hashlib.sha256
    ).hexdigest()


def export_package(
    merge_id: str,
    base: Any,
    result: Any,
    patch: list[dict],
    audit_events: list[dict],
    generated_at: str | None = None,
) -> dict:
    """Build a normalised package and attach its HMAC signature."""
    package = build_evidence_package(
        merge_id, base, result, patch, audit_events, generated_at
    )
    package["signature"] = sign_package(package)
    return package


# ---------------------------------------------------------------------------
# Database-free verification
# ---------------------------------------------------------------------------


def _fail(stage: str, reason: str, **extra: Any) -> dict:
    verdict = {"valid": False, "stage": stage, "reason": reason}
    verdict.update(extra)
    return verdict


def malformed_body_verdict() -> dict:
    """Failure verdict for a request whose body is not even parseable JSON."""
    return _fail(
        STAGE_SIGNATURE,
        "request body is not a valid JSON evidence package",
    )


def verify_signature(package: Any, key: bytes | None = None) -> dict | None:
    """Stage 1: shape checks and a constant-time HMAC comparison.

    Returns None on success or a failure verdict (never raises).
    """
    if not isinstance(package, dict):
        return _fail(
            STAGE_SIGNATURE, "evidence package must be a JSON object"
        )
    signature = package.get("signature")
    if not isinstance(signature, str) or not signature:
        return _fail(
            STAGE_SIGNATURE, "package is missing its hex 'signature' field"
        )
    if package.get("algorithm") != SIGNATURE_ALGORITHM:
        return _fail(
            STAGE_SIGNATURE,
            f"unsupported signature algorithm: expected {SIGNATURE_ALGORITHM!r}",
            expected_algorithm=SIGNATURE_ALGORITHM,
            actual_algorithm=package.get("algorithm"),
        )
    try:
        int(signature, 16)
    except ValueError:
        return _fail(
            STAGE_SIGNATURE, "signature is not a hexadecimal HMAC digest"
        )
    expected = sign_package(package, key if key is not None else _key())
    # Constant-time: no early-exit string comparison and no timing oracle on
    # how many leading bytes matched.
    if not hmac.compare_digest(signature, expected):
        return _fail(
            STAGE_SIGNATURE,
            "HMAC-SHA256 signature does not match the package contents",
        )
    return None


def verify_audit_chain(package: dict) -> dict | None:
    """Stage 2: continuity of the complete audit chain embedded in the package.

    Events must be present in sequence order with seq 1..n, each chained to
    its predecessor by SHA-256 over the canonical event payload, and every
    event must belong to the package's merge order. An empty chain (a merge
    resolved at creation, before any adjudication) is valid.
    """
    audit = package.get("audit")
    if not isinstance(audit, dict) or not isinstance(audit.get("events"), list):
        return _fail(
            STAGE_AUDIT_CHAIN, "package is missing its audit.events list"
        )
    merge_id = package.get("merge_id")
    if audit.get("merge_id") != merge_id:
        return _fail(
            STAGE_AUDIT_CHAIN,
            "audit.merge_id does not match the package merge_id",
        )
    expected_prev = (
        audit.get("genesis_prev_hash")
        if isinstance(audit.get("genesis_prev_hash"), str)
        else GENESIS_PREV_HASH
    )
    if expected_prev != GENESIS_PREV_HASH:
        return _fail(
            STAGE_AUDIT_CHAIN,
            "audit.genesis_prev_hash does not match the canonical genesis hash",
        )
    expected_seq = 1
    # Two running links walk the chain: expected_prev is the stored hash
    # the next event must name (so a tampered prev_hash fails), while
    # verified_tip is the hash of the last event that itself verified — a
    # deleted middle event leaves the survivor's stored prev_hash pointing
    # at the missing row, which verified_tip does not extend.
    verified_tip = GENESIS_PREV_HASH
    for event in audit["events"]:
        if not isinstance(event, dict):
            return _fail(
                STAGE_AUDIT_CHAIN,
                f"audit event {expected_seq} is not a JSON object",
            )
        label = event.get("seq", expected_seq)
        # Embedded events inherit the package merge order: the stored trail
        # omits the redundant per-event merge_id, but if one is present it
        # must agree with the package.
        event_merge_id = (
            event["merge_id"] if "merge_id" in event else merge_id
        )
        if event_merge_id != merge_id:
            return _fail(
                STAGE_AUDIT_CHAIN,
                f"event seq {label} does not belong to the package merge_id",
                first_invalid_seq=label,
            )
        if event.get("seq") != expected_seq:
            return _fail(
                STAGE_AUDIT_CHAIN,
                f"event sequence is not continuous: expected seq {expected_seq}",
                first_invalid_seq=label,
            )
        if (
            event.get("prev_hash") != expected_prev
            or event.get("prev_hash") != verified_tip
        ):
            # Distinguishes a forged link (expected_prev) from a deleted
            # predecessor: the stored prev_hash still names the missing row,
            # but no verified event ends at that hash (verified_tip).
            return _fail(
                STAGE_AUDIT_CHAIN,
                f"event seq {expected_seq} prev_hash does not link to its "
                "immediate verified predecessor",
                first_invalid_seq=expected_seq,
            )
        try:
            payload = event_payload(
                event_merge_id,
                event["seq"],
                event["method"],
                event["revision_before"],
                event["revision_after"],
                event["pointers"],
                event["detail"],
                event["prev_hash"],
                event["created_at"],
            )
            stored_hash = event["hash"]
        except (KeyError, TypeError):
            return _fail(
                STAGE_AUDIT_CHAIN,
                f"event seq {expected_seq} is missing chain fields",
                first_invalid_seq=expected_seq,
            )
        if not isinstance(stored_hash, str) or sha256_hex(payload) != stored_hash:
            return _fail(
                STAGE_AUDIT_CHAIN,
                f"event seq {expected_seq} hash does not match its payload",
                first_invalid_seq=expected_seq,
            )
        expected_seq += 1
        expected_prev = stored_hash
        verified_tip = stored_hash
    if audit.get("event_count") != len(audit["events"]):
        return _fail(
            STAGE_AUDIT_CHAIN,
            "audit.event_count does not match the number of events",
        )
    return None


def verify_patch_replay(package: dict) -> dict | None:
    """Stage 3: actually replay the patch and compare against the result.

    The stored RFC 6902 patch is applied to the stored base in full; the
    replayed document must equal the stored result exactly. Determinism is
    re-checked too: regenerating the base-to-result diff must yield exactly
    the patch the package carries.
    """
    base = package.get("base")
    result = package.get("result")
    patch = package.get("patch")
    if not isinstance(patch, list):
        return _fail(
            STAGE_PATCH_REPLAY, "package is missing its RFC 6902 'patch' list"
        )
    try:
        replayed = apply_rfc6902_patch(base, patch)
    except PatchApplicationError as exc:
        return _fail(STAGE_PATCH_REPLAY, f"patch replay failed: {exc}")
    if not _json_equal(replayed, result):
        return _fail(
            STAGE_PATCH_REPLAY,
            "replaying the patch on base does not reproduce the signed result",
        )
    regenerated = generate_patch(base, result)
    if regenerated != patch:
        return _fail(
            STAGE_PATCH_REPLAY,
            "the carried patch is not the deterministic base-to-result patch",
        )
    return None


def verify_package(package: Any, key: bytes | None = None) -> dict:
    """Verify an evidence package without any database access.

    Runs the three checks in fixed order (signature, audit chain, patch
    replay) and returns the first failing stage with its reason, or a
    success verdict. Pure: only the package contents and the configured key
    are used.
    """
    for check in (
        lambda: verify_signature(package, key),
        lambda: verify_audit_chain(package),
        lambda: verify_patch_replay(package),
    ):
        failure = check()
        if failure is not None:
            return failure
    return {
        "valid": True,
        "stage": None,
        "reason": None,
        "merge_id": package["merge_id"],
        "event_count": package["audit"]["event_count"],
    }
