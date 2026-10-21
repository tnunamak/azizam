"""The unserved owner view is recursive and built from trusted names only."""
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from gateway.content_access import project
from gateway.owner_projection import (
    JobView, ProjectionNames, ReceiptListView, ReceiptView, StorageView,
    project_receipt_list,
)

MARKER = "PRIVATE_CONTENT_CANARY"
NAMES = ProjectionNames(people={"person:a": "Alice", "person:b": "Bob"},
                        apps={"app:a": "Camera"}, models={"model:a": "Image model"},
                        workflows={"flow:a": "Picture"}, providers=frozenset({"provider:a"}))


def seeded_record():
    return {
        "request_id": "req_" + "a" * 32, "job_id": "mj_" + "b" * 32, "person_id": "person:a", "app_id": "app:a",
        "resolved_model": "model:a", "resolved_workflow": "flow:a", "intent": "image.generate",
        "source_kind": "app", "protocol": "openai.images", "stream": True, "route": "provider",
        "provider": "provider:a", "status": "succeeded", "status_code": 200,
        "terminal_reason": "completed", "accepted_at_ms": 1700000000000, "updated_at_ms": 1700000001000, "policy_decision": "allowed", "attempt_number": 1,
        "usage": {"images": 2, "input_tokens": 9, "unexpected": MARKER},
        "cost": {"amount_usd": 0.5, "quality": "measured", "source": "provider", "reconciled": True, "secret": MARKER},
        "resource_decision": {"reasons": [{"code": "queue_full", "effect": "delay", "resource_class": "queue", "rule": MARKER, "observed_at": 3, "secret": MARKER}]},
        "retention": [{"kind": "library_prompt", "decision": "kept", "reason": MARKER, "secret": MARKER},
                      {"kind": "conversation", "decision": "failed", "reason": MARKER}],
        **{key: MARKER for key in ("path", "requested_model", "accepted_inputs", "rejected_inputs", "ignored_inputs",
                                   "warnings", "fallback_reasons", "client_hint", "source_label", "request_shape", "litellm",
                                   "error", "assets", "seed", "input_hash", "metadata", "client_address", "api_key_name", "future_field")},
    }


def test_recursive_projection_drops_content_and_uses_submission_person():
    record = seeded_record()
    original = deepcopy(record)
    shown = project("receipt", record, names=NAMES).model_dump(mode="json")
    assert MARKER not in json.dumps(shown)
    assert record == original
    assert shown["person"] == {"id": "person:a", "name": "Alice"}
    assert shown["app"] == {"id": "app:a", "name": "Camera"}
    assert shown["model"] == {"catalog_id": "model:a", "name": "Image model"}
    assert shown["cost"] == {"amount_usd": 0.5, "quality": "measured", "source": "provider", "reconciled": True}
    assert shown["resource_decision"] == [{"code": "queue_full", "effect": "delay", "resource_class": "queue"}]
    assert shown["retention"] == [{"kind": "library_prompt", "decision": "kept"}, {"kind": "conversation", "decision": "failed"}]
    assert set(shown["usage"]) == {"input_tokens", "output_tokens", "images", "audio_seconds", "video_seconds", "characters"}


def test_unknown_catalog_and_free_text_values_never_fall_back():
    record = seeded_record()
    for key in ("request_id", "job_id", "resolved_model", "resolved_workflow", "provider", "person_id", "app_id", "source_kind", "protocol", "status", "terminal_reason", "intent", "route"):
        record[key] = MARKER
    record["resource_decision"] = {"reasons": [{"code": MARKER, "effect": MARKER, "resource_class": MARKER}]}
    record["retention"] = [{"kind": MARKER, "decision": MARKER}]
    shown = project("receipt", record, names=NAMES).model_dump(mode="json")
    assert MARKER not in json.dumps(shown)
    assert shown["model"] == shown["workflow"] == {"catalog_id": None, "name": "Unknown model"}
    assert shown["person"] is shown["app"] is None


def test_envelope_jobs_and_storage_are_new_typed_values():
    envelope = {"coverage_start": 1.0, "aggregate_complete": True, "unknown_paid_count": 2,
                "history_note": "partial", "sources": [MARKER], "filters": MARKER, "aggregates": {MARKER: 1}}
    result = project_receipt_list([seeded_record()], envelope, names=NAMES).model_dump(mode="json")
    assert set(result) == {"items", "coverage_start", "aggregate_complete", "unknown_paid_count", "history_note"}
    assert MARKER not in json.dumps(result)
    job = project("job", seeded_record(), names=NAMES).model_dump(mode="json")
    assert MARKER not in json.dumps(job)
    storage = project("storage", {"category": "person", "person_id": "person:a", "size_bytes": 100, "path": MARKER}, names=NAMES).model_dump(mode="json")
    assert storage["size_bytes"] == 100
    assert MARKER not in json.dumps(storage)


def test_schema_recursively_forbids_additional_properties_and_untyped_strings():
    for schema_class in (ReceiptView, JobView, StorageView, ReceiptListView):
        schema = schema_class.model_json_schema()
        def walk(node, path=()):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, path
                if node.get("type") == "string":
                    assert ("enum" in node or "const" in node or "pattern" in node or path[-1:] == ("name",)), path
                for key, value in node.items():
                    walk(value, (*path, key))
            elif isinstance(node, list):
                for value in node:
                    walk(value, path)
        walk(schema)


def test_extra_nested_fields_invalid_enums_and_wrong_scalars_are_rejected():
    value = project("receipt", seeded_record(), names=NAMES).model_dump()
    for path in ((), ("cost",), ("usage",), ("person",), ("model",), ("resource_decision", 0), ("retention", 0)):
        changed = deepcopy(value)
        target = changed
        for part in path:
            target = target[part]
        target["unexpected"] = MARKER
        with pytest.raises(ValidationError):
            ReceiptView.model_validate(changed)
    for field, invalid in (("status", MARKER), ("latency_ms", True), ("stream", "true")):
        with pytest.raises(ValidationError):
            ReceiptView.model_validate({**value, field: invalid})


def test_invalid_numeric_values_are_unavailable_not_strings_or_nonfinite():
    raw = seeded_record()
    raw["usage"] = {"images": True, "input_tokens": MARKER, "audio_seconds": float("inf")}
    raw["cost"] = {"amount_usd": float("nan")}
    shown = project("receipt", raw).model_dump(mode="json")
    assert all(value is None for value in shown["usage"].values())
    assert shown["cost"]["amount_usd"] is None


@pytest.mark.parametrize("reason,status,route,expected", [
    ("connect_error", 502, "provider", "provider"), ("read_error", 502, "local", "engine"),
    ("internal_error", 500, "local", "gateway"), ("failed", 400, "local", "request"),
    ("failed", 403, "local", "plan"), ("failed", 500, "local", "unknown"),
])
def test_support_layer_is_derived_without_raw_error(reason, status, route, expected):
    shown = project("receipt", {"terminal_reason": reason, "status_code": status, "route": route, "error": MARKER})
    assert shown.layer == expected
    assert MARKER not in shown.model_dump_json()


@pytest.mark.parametrize("field,prefix", [("request_id", "req_"), ("job_id", "mj_")])
@pytest.mark.parametrize("invalid", [MARKER, "private content", "req_1", "mj_1", "req_" + "A" * 32, "mj_" + "B" * 32, {"secret": MARKER}])
def test_invalid_record_ids_are_unavailable(field, prefix, invalid):
    raw = seeded_record()
    raw[field] = invalid
    shown = project("receipt", raw, names=NAMES).model_dump(mode="json")
    assert shown[field] is None
    assert MARKER not in json.dumps(shown)
    if field == "job_id":
        assert project("job", raw).job_id is None


def test_real_shaped_stored_receipt_preserves_operational_fields():
    from gateway.receipts import RouteReceipt
    raw = RouteReceipt(
        protocol="openai.images", intent="image.generate", path="/v1/images/generations",
        request_id="req_" + "1" * 32, job_id="mj_" + "2" * 32,
        app_id="app_1", source_kind="app", resolved_model="local-image",
        status="cancelled", accepted_at_ms=1700000000000,
        queued_at_ms=1700000000010, started_at_ms=1700000000020,
        first_output_at_ms=1700000000030, finished_at_ms=1700000000040,
        updated_at_ms=1700000001000, policy_decision="allowed",
    ).to_dict()
    shown = project("receipt", raw).model_dump(mode="json")
    for key in ("request_id", "job_id", "protocol", "source_kind", "status", "policy_decision",
                "accepted_at_ms", "queued_at_ms", "started_at_ms", "first_output_at_ms",
                "finished_at_ms", "updated_at_ms"):
        assert shown[key] == raw[key]
    assert "path" not in shown
    assert "policy" not in shown
    assert "created_at" not in shown


@pytest.mark.parametrize("source_kind", ["app", "local-network", "gateway-ui", "internal", "trusted_address", "mcp"])
def test_real_source_kind_vocabulary(source_kind):
    assert project("receipt", {"source_kind": source_kind}).source_kind == source_kind


@pytest.mark.parametrize("decision", ["allowed", "denied", "not_evaluated"])
def test_real_policy_decision_vocabulary(decision):
    shown = project("receipt", {"policy_decision": decision, "policy": {"secret": MARKER}})
    assert shown.policy_decision == decision
    assert MARKER not in shown.model_dump_json()


def test_registered_runtime_protocols_are_preserved():
    from gateway.discovery_metadata import _load_protocol_frontend_records
    protocols = set()
    for record in _load_protocol_frontend_records():
        routes = record.get("registered_routes", ())
        if isinstance(routes, dict):
            routes = routes.values()
        for route in routes:
            if isinstance(route, dict) and isinstance(route.get("runtime_policy"), dict):
                protocol = route["runtime_policy"].get("protocol")
                if isinstance(protocol, str) and protocol:
                    protocols.add(protocol)
    assert "openai.images" in protocols
    for protocol in protocols:
        assert project("receipt", {"protocol": protocol}).model_dump(mode="json")["protocol"] == protocol


def test_receipt_protocol_metadata_extends_closed_enum_vocabulary(monkeypatch):
    import gateway.owner_projection as op
    monkeypatch.setattr(op, "_load_protocol_frontend_records", lambda: [
        {"receipt_protocols": ["fixture.receipt", None, 7],
         "registered_routes": [{"runtime_policy": {"protocol": "fixture.runtime"}}]},
        {"receipt_protocols": "not-a-list", "registered_routes": []},
    ])
    values = op._receipt_protocols()
    assert {"fixture.receipt", "fixture.runtime"} <= values
    assert "not-a-list" not in values
