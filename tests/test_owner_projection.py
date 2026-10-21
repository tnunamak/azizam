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
        "request_id": "req:a", "job_id": "job:a", "person_id": "person:a", "app_id": "app:a",
        "resolved_model": "model:a", "workflow_id": "flow:a", "intent": "image.generate",
        "source_kind": "app_key", "protocol": "openai", "stream": True, "route": "provider",
        "provider": "provider:a", "status": "succeeded", "status_code": 200,
        "terminal_reason": "completed", "created_at": 123.0, "attempt_number": 1,
        "usage": {"images": 2, "input_tokens": 9, "unexpected": MARKER},
        "cost": {"amount_usd": 0.5, "quality": "measured", "source": "provider", "reconciled": True, "secret": MARKER},
        "resource_decision": {"reasons": [{"code": "queue_full", "effect": "delay", "resource_class": "queue", "rule": MARKER, "observed_at": 3, "secret": MARKER}]},
        "retention": [{"kind": "library_prompt", "decision": "kept", "reason": MARKER, "secret": MARKER}],
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
    assert shown["retention"] == [{"kind": "library_prompt", "decision": "kept"}]
    assert set(shown["usage"]) == {"input_tokens", "output_tokens", "images", "audio_seconds", "video_seconds", "characters"}


def test_unknown_catalog_and_free_text_values_never_fall_back():
    record = seeded_record()
    for key in ("resolved_model", "workflow_id", "provider", "person_id", "app_id", "source_kind", "protocol", "status", "terminal_reason", "intent", "route"):
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
