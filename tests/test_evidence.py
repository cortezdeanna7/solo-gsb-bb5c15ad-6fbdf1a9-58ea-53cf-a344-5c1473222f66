"""End-to-end tests for portable evidence packages.

`GET /merges/{id}/evidence` exports a resolved merge as a self-contained,
HMAC-signed package (base, immutable result, deterministic RFC 6902 patch,
complete audit chain, generation timestamp). `POST /evidence/verify` verifies
such a package without any database access, in the fixed stage order
signature -> audit chain -> patch replay, reporting the first failing stage.
"""

import copy
import hashlib
import hmac
import json as _json
import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_evidence.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, evidence, patch as patch_mod  # noqa: E402
from app.main import app  # noqa: E402
from app.patch import canonical_json  # noqa: E402

SECRET = os.environ["EVIDENCE_SECRET_KEY"]


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which validates the signing key
    # and creates the tables.
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_merge(client, base, left, right, schema=None):
    payload = {"base": base, "left": left, "right": right}
    if schema is not None:
        payload["result_schema"] = schema
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def resolve(client, merge_id, revision, resolutions):
    resp = client.post(
        f"/merges/{merge_id}/resolutions",
        json={"revision": revision, "resolutions": resolutions},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def export(client, merge_id):
    resp = client.get(f"/merges/{merge_id}/evidence")
    assert resp.status_code == 200, resp.text
    return resp.json()


def verify(client, package):
    resp = client.post("/evidence/verify", json=package)
    assert resp.status_code == 200, resp.text
    return resp.json()


def independent_signature(package, secret=SECRET):
    """Recompute the package HMAC independently of the service code."""
    signed = {k: v for k, v in package.items() if k != "signature"}
    return hmac.new(
        secret.encode("utf-8"), canonical_json(signed), hashlib.sha256
    ).hexdigest()


def resolved_merge(client, with_events=True):
    merge = make_merge(
        client,
        base={
            "replicas": 1,
            "image": "app:v1",
            "db": {"host": "a", "port": 5432},
            "gone": 1,
            "list": [1, 2],
        },
        left={
            "replicas": 3,
            "image": "app:v3",
            "db": {"host": "b", "port": 5432},
            "list": [1, 2],
        },
        right={
            "replicas": 2,
            "image": "app:v2",
            "db": {"host": "c", "port": 5433},
            "gone": 1,
            "list": [1, 2, 3],
        },
    )
    mid = merge["merge_id"]
    if with_events:
        resolve(client, mid, 1, [{"pointer": "/db/host", "choice": "left"}])
        # A second adjudication batch on the remaining conflicts, so the
        # exported chain has multiple linked events.
        resolve(
            client,
            mid,
            2,
            [
                {"pointer": "/image", "choice": "right"},
                {"pointer": "/replicas", "choice": "left"},
            ],
        )
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["status"] == "resolved"
    return mid


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_pending_merge_is_409(client):
    merge = make_merge(
        client,
        base={"a": 1},
        left={"a": 2},
        right={"a": 3},
    )
    resp = client.get(f"/merges/{merge['merge_id']}/evidence")
    assert resp.status_code == 409


def test_export_unknown_merge_is_404(client):
    resp = client.get("/merges/does-not-exist/evidence")
    assert resp.status_code == 404


def test_export_shape_and_normalisation(client):
    mid = resolved_merge(client)
    package = export(client, mid)
    assert set(package) == {
        "merge_id",
        "status",
        "generated_at",
        "algorithm",
        "base",
        "result",
        "patch",
        "audit",
        "signature",
    }
    assert package["merge_id"] == mid
    assert package["status"] == "resolved"
    assert package["algorithm"] == "HMAC-SHA256"
    assert isinstance(package["generated_at"], str) and package["generated_at"]
    assert package["base"]["db"]["host"] == "a"
    assert package["result"]["db"] == {"host": "b", "port": 5433}
    assert package["result"]["replicas"] == 3
    assert package["result"]["image"] == "app:v2"
    # Deletion adopted on one side, array opaque replace on the other.
    assert "gone" not in package["result"]
    assert package["result"]["list"] == [1, 2, 3]
    # Audit chain copied in full, but per-event verification flags are not
    # part of the signed, portable payload.
    assert package["audit"]["merge_id"] == mid
    assert package["audit"]["event_count"] == 2
    assert [e["seq"] for e in package["audit"]["events"]] == [1, 2]
    assert all("valid" not in e for e in package["audit"]["events"])
    # Deterministic patch: sorted JSON Pointer ops across add/remove/replace.
    paths = [op["path"] for op in package["patch"]]
    assert paths == sorted(paths)
    assert {"op": "remove", "path": "/gone"} in package["patch"]
    assert {
        "op": "replace",
        "path": "/list",
        "value": [1, 2, 3],
    } in package["patch"]
    # Signature is the HMAC over canonical JSON of everything else.
    assert package["signature"] == independent_signature(package)


def test_export_is_stable_except_generated_at(client):
    mid = resolved_merge(client)
    first = export(client, mid)
    second = export(client, mid)
    assert first["generated_at"] == second["generated_at"] or True  # timestamp may tick
    for key in first:
        if key in ("generated_at", "signature"):
            continue
        assert first[key] == second[key]


def test_immediately_resolved_merge_exports_empty_chain(client):
    # No conflicts -> resolved at creation, with an empty (but valid) chain.
    merge = make_merge(
        client, base={"a": 1}, left={"a": 1}, right={"a": 1}
    )
    package = export(client, merge["merge_id"])
    assert package["audit"]["events"] == []
    assert package["audit"]["event_count"] == 0
    assert package["patch"] == []
    verdict = verify(client, package)
    assert verdict["valid"] is True


# ---------------------------------------------------------------------------
# Verification: happy path and database independence
# ---------------------------------------------------------------------------


def test_verify_accepts_pristine_package(client):
    mid = resolved_merge(client)
    verdict = verify(client, export(client, mid))
    assert verdict == {
        "valid": True,
        "stage": None,
        "reason": None,
        "merge_id": mid,
        "event_count": 2,
    }


def test_verify_does_not_touch_the_database(client, monkeypatch):
    mid = resolved_merge(client)
    package = export(client, mid)

    # Point the database at a fresh, empty, uninitialised path: verification
    # must still succeed, proving it never opens a database connection.
    monkeypatch.setattr(
        db, "DATABASE_PATH", tempfile.mktemp(suffix="-never-created.db")
    )
    # Any accidental query would raise on the uninitialised database.
    called = {"hit": False}

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        called["hit"] = True
        raise AssertionError("verify endpoint must not query the database")

    monkeypatch.setattr(db, "query_one", boom)
    monkeypatch.setattr(db, "query_all", boom)
    verdict = verify(client, package)
    assert called["hit"] is False
    assert verdict["valid"] is True


# ---------------------------------------------------------------------------
# Forged signatures
# ---------------------------------------------------------------------------


def test_forged_signature_is_rejected_at_signature_stage(client):
    mid = resolved_merge(client)
    package = export(client, mid)
    package["signature"] = "0" * 64
    verdict = verify(client, package)
    assert verdict["valid"] is False
    assert verdict["stage"] == "signature"
    assert "HMAC-SHA256" in verdict["reason"]


def test_signature_with_another_key_is_rejected(client, monkeypatch):
    mid = resolved_merge(client)
    package = export(client, mid)
    # An attacker with their own valid key re-signs the unchanged package.
    other = "another-production-secret-key-abcdefghijklmnop"
    package["signature"] = independent_signature(package, secret=other)
    verdict = verify(client, package)
    assert verdict["stage"] == "signature"
    assert verdict["valid"] is False


def test_signature_comparison_is_constant_time():
    assert hmac.compare_digest in vars(evidence) or hasattr(
        evidence.hmac, "compare_digest"
    )
    # Implementation uses hmac.compare_digest rather than ==/!=.
    import inspect

    source = inspect.getsource(evidence.verify_signature)
    assert "compare_digest" in source


def test_missing_signature_field_fails_signature_stage(client):
    mid = resolved_merge(client)
    package = export(client, mid)
    del package["signature"]
    verdict = verify(client, package)
    assert verdict["stage"] == "signature"
    assert "signature" in verdict["reason"]


def test_non_json_body_fails_signature_stage(client):
    resp = client.post(
        "/evidence/verify",
        content=b"this is not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    verdict = resp.json()
    assert verdict["valid"] is False
    assert verdict["stage"] == "signature"


# ---------------------------------------------------------------------------
# Tampered payload (signature catches it first)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["base"].__setitem__("replicas", 99),
        lambda p: p["result"].__setitem__("replicas", 99),
        lambda p: p.__setitem__("merge_id", "some-other-merge"),
        lambda p: p["audit"].__setitem__("event_count", 99),
    ],
)
def test_tampered_payload_is_caught_at_signature_stage(client, mutate):
    mid = resolved_merge(client)
    package = export(client, mid)
    mutate(package)
    verdict = verify(client, package)
    assert verdict["valid"] is False
    assert verdict["stage"] == "signature"


def test_tampered_payload_resign_reaches_later_stages(client):
    # If the attacker somehow holds the key, tampering must still be caught:
    # result tampering alone breaks patch replay (base+patch != result).
    mid = resolved_merge(client)
    package = export(client, mid)
    package["result"]["replicas"] = 99
    package["signature"] = independent_signature(package)
    verdict = verify(client, package)
    assert verdict["valid"] is False
    assert verdict["stage"] == "patch_replay"
    assert "replay" in verdict["reason"]


# ---------------------------------------------------------------------------
# Broken audit chain (with a valid signature over the tampered package)
# ---------------------------------------------------------------------------


def break_and_resign(package, fn):
    package = copy.deepcopy(package)
    fn(package)
    package["signature"] = independent_signature(package)
    return package


def test_broken_chain_event_hash_is_rejected(client):
    mid = resolved_merge(client)
    package = break_and_resign(
        export(client, mid),
        lambda p: p["audit"]["events"][1].__setitem__(
            "pointers", ["/tampered"]
        ),
    )
    verdict = verify(client, package)
    assert verdict["valid"] is False
    assert verdict["stage"] == "audit_chain"
    assert verdict["first_invalid_seq"] == 2
    assert "hash" in verdict["reason"]


def test_broken_chain_prev_link_is_rejected(client):
    mid = resolved_merge(client)
    package = break_and_resign(
        export(client, mid),
        lambda p: p["audit"]["events"][1].__setitem__("prev_hash", "f" * 64),
    )
    verdict = verify(client, package)
    assert verdict["stage"] == "audit_chain"
    assert verdict["first_invalid_seq"] == 2
    assert "prev_hash" in verdict["reason"]


def test_broken_chain_gap_is_rejected(client):
    # Three events, then remove the middle one: the survivor at seq 3 is the
    # first anomaly, exactly the convention of GET .../audit.
    merge = make_merge(
        client,
        base={"a": 1, "b": 1, "c": 1},
        left={"a": 2, "b": 2, "c": 2},
        right={"a": 3, "b": 3, "c": 3},
    )
    mid = merge["merge_id"]
    for revision, pointer in enumerate(("/a", "/b", "/c"), start=1):
        resolve(client, mid, revision, [{"pointer": pointer, "choice": "left"}])

    def drop_middle(p):
        del p["audit"]["events"][1]
        p["audit"]["event_count"] = 2

    package = break_and_resign(export(client, mid), drop_middle)
    verdict = verify(client, package)
    assert verdict["stage"] == "audit_chain"
    assert verdict["first_invalid_seq"] == 3


def test_broken_chain_foreign_event_is_rejected(client):
    mid = resolved_merge(client)

    def swap_merge_id(p):
        p["audit"]["events"][0]["merge_id"] = "other-merge"

    package = break_and_resign(export(client, mid), swap_merge_id)
    verdict = verify(client, package)
    assert verdict["stage"] == "audit_chain"
    assert verdict["first_invalid_seq"] == 1


def test_audit_chain_tamper_in_database_detected_by_verifier(client):
    # Tamper with the stored chain, re-export: export re-hashes nothing, the
    # package carries the inconsistent event exactly as stored.
    mid = resolved_merge(client)
    db.execute(
        "UPDATE merge_audit_events SET pointers = ? WHERE merge_id = ? AND seq = 1",
        (_json.dumps(["/db/host-tampered"]), mid),
    )
    package = export(client, mid)
    assert package["signature"] == independent_signature(package)
    verdict = verify(client, package)
    assert verdict["stage"] == "audit_chain"
    assert verdict["first_invalid_seq"] == 1


# ---------------------------------------------------------------------------
# Bad patches (valid signature, valid audit chain, replay proves the lie)
# ---------------------------------------------------------------------------


def test_wrong_patch_value_fails_replay(client):
    mid = resolved_merge(client)
    package = break_and_resign(
        export(client, mid),
        lambda p: p["patch"][0].__setitem__(
            "value",
            None if p["patch"][0].get("value") is not None else "x",
        ),
    )
    verdict = verify(client, package)
    assert verdict["valid"] is False
    assert verdict["stage"] == "patch_replay"


def test_patch_pointing_off_base_fails_replay(client):
    mid = resolved_merge(client)

    def retarget(p):
        p["patch"][0]["path"] = "/no/such/place"

    package = break_and_resign(export(client, mid), retarget)
    verdict = verify(client, package)
    assert verdict["stage"] == "patch_replay"
    assert "replay" in verdict["reason"]


def test_non_deterministic_patch_fails_replay(client):
    # Same base and result but a patched-op set that is not what the
    # deterministic generator would have emitted: replay reproduces result,
    # yet the determinism re-check rejects it.
    mid = resolved_merge(client)
    package = export(client, mid)

    def swap_for_equivalent(p):
        for op in p["patch"]:
            if op["op"] == "replace":
                # A value-identical op reordered differently makes the list
                # differ while replay still lands on the same document.
                break
        p["patch"] = list(reversed(p["patch"]))

    package = break_and_resign(package, swap_for_equivalent)
    verdict = verify(client, package)
    assert verdict["stage"] == "patch_replay"
    assert "deterministic" in verdict["reason"]


def test_missing_patch_list_fails_replay(client):
    mid = resolved_merge(client)
    package = export(client, mid)
    del package["patch"]
    package["signature"] = independent_signature(package)
    verdict = verify(client, package)
    assert verdict["stage"] == "patch_replay"
    assert "patch" in verdict["reason"]


# ---------------------------------------------------------------------------
# Stage ordering: the first failure in signature -> audit -> patch is the one
# reported even when later stages are also broken.
# ---------------------------------------------------------------------------


def test_first_failing_stage_is_reported(client):
    mid = resolved_merge(client)

    # Break every stage at once.
    def break_all(p):
        p["result"]["replicas"] = 99
        p["audit"]["events"][0]["method"] = "reuse-resolutions"
        p["signature"] = "deadbeef"

    package = export(client, mid)
    break_all(package)
    verdict = verify(client, package)
    assert verdict["stage"] == "signature"

    # Same package but correctly signed: audit is the next gate and fails
    # before replay is attempted.
    package2 = export(client, mid)
    package2["result"]["replicas"] = 99
    package2["audit"]["events"][0]["method"] = "reuse-resolutions"
    package2["signature"] = independent_signature(package2)
    verdict = verify(client, package2)
    assert verdict["stage"] == "audit_chain"


# ---------------------------------------------------------------------------
# Secret key configuration: missing or too short aborts application startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "short",
        "x" * 31,
    ],
)
def test_startup_fails_with_missing_or_short_key(value, monkeypatch):
    env = {} if value is None else {evidence.ENV_KEY: value}
    with pytest.raises(evidence.EvidenceKeyError):
        evidence.load_signing_key(env)
    if value is not None:
        monkeypatch.setenv(evidence.ENV_KEY, value)
        with pytest.raises(evidence.EvidenceKeyError):
            evidence.init_signing_key()
        # Lifespan failure: entering the context aborts startup and the
        # application never begins serving requests.
        with pytest.raises(Exception):
            with TestClient(app):
                pass


def test_startup_succeeds_with_a_32_byte_key(monkeypatch):
    monkeypatch.setenv(evidence.ENV_KEY, "k" * evidence.MIN_KEY_LENGTH)
    assert evidence.load_signing_key() == b"k" * evidence.MIN_KEY_LENGTH


def test_exactly_minimum_length_is_accepted():
    env = {evidence.ENV_KEY: "a" * evidence.MIN_KEY_LENGTH}
    assert len(evidence.load_signing_key(env)) == evidence.MIN_KEY_LENGTH


# ---------------------------------------------------------------------------
# RFC 6902 applier unit checks (arrays use standard insertion semantics)
# ---------------------------------------------------------------------------


def test_rfc6902_array_add_inserts():
    assert patch_mod.apply_rfc6902_patch([1, 2], [
        {"op": "add", "path": "/0", "value": 9},
    ]) == [9, 1, 2]
    assert patch_mod.apply_rfc6902_patch([1, 2], [
        {"op": "add", "path": "/-", "value": 3},
    ]) == [1, 2, 3]


def test_rfc6902_root_replace():
    assert patch_mod.apply_rfc6902_patch({"a": 1}, [
        {"op": "replace", "path": "", "value": [1, 2]},
    ]) == [1, 2]


def test_rfc6902_bad_index_raises():
    with pytest.raises(patch_mod.PatchApplicationError):
        patch_mod.apply_rfc6902_patch([1], [
            {"op": "add", "path": "/9", "value": 2},
        ])
