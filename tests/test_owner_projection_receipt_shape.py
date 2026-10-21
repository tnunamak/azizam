"""Projection regression against keys copied from a synthetic stored receipt.

The checked-in fixture contains field names only, never receipt values/content.
"""
import json
from pathlib import Path

import pytest

import gateway.accounts  # initialize the same import order as the gateway
from gateway import receipt_store
from gateway.content_access import project
from gateway.owner_projection import ProjectionNames
from gateway.receipts import PHASE_FIELDS, RouteReceipt

FIXTURE = Path(__file__).with_name("fixtures") / "owner_projection_receipt_keys.json"


def _stored_fixture(db):
    receipt_store.init_db(db)
    record = RouteReceipt(protocol="openai.images", intent="image.generate", path="/v1/images/generations",
                          request_id="req_" + "a" * 32, job_id="mj_" + "b" * 32, app_id="app:a",
                          source_kind="app", source_label="Synthetic App", client_hint="synthetic",
                          maker_person_id="person:a", connection_id="connection:a", api_key_name="synthetic",
                          client_address="192.0.2.1", owner_key_hash="synthetic-owner", app_profile="plan:a",
                          requested_model="requested:a", resolved_model="model:a", requested_workflow="flow:a",
                          resolved_workflow="flow:a", backend="provider:a", remote=True, stream=True,
                          manifest_id="manifest:a", manifest_version="1", manifest_hash="synthetic",
                          parent_request_id="req_" + "c" * 32, visibility="full",
                          policy_sources=["synthetic"], policy_reasons=["allowed"],
                          accepted_inputs=["model"], rejected_inputs={"synthetic": "unsupported"},
                          ignored_inputs={"synthetic": "ignored"}, fallback_reasons=["synthetic"], warnings=["synthetic"],
                          effective_output_limit=100, attempt_number=1, latency_ms=4.0,
                          usage={"images": 1, "input_tokens": 2, "output_tokens": 3, "characters": 4,
                                 "audio_seconds": 1.0, "video_seconds": 2.0},
                          cost={"amount_usd": 0.5, "quality": "measured", "source": "provider", "reconciled": True},
                          refusal={"code": "budget_exceeded", "limit": {"period": "daily", "scope": "app"}},
                          resource_decision={"reasons": [{"code": "queue_full", "effect": "delay", "resource_class": "queue"}]},
                          retention=[{"kind": "conversation", "decision": "not_kept", "reason": "request_forget"}],
                          retention_facts=["d" * 64], metadata={"terminal_reason": "completed", "status_code": 200},
                          status="succeeded", policy_decision="allowed",
                          accepted_at_ms=1_700_000_000_000, updated_at_ms=1_700_000_000_050,
                          queued_at_ms=1_700_000_000_001, started_at_ms=1_700_000_000_002,
                          first_output_at_ms=1_700_000_000_003, finished_at_ms=1_700_000_000_004)
    receipt_store.record(record, path=db)
    return receipt_store.get(record.request_id, audience="admin", path=db)


def test_projection_matches_a_real_stored_receipts_structure(tmp_path):
    stored = _stored_fixture(tmp_path / "receipts.db")
    keys = json.loads(FIXTURE.read_text())
    assert sorted(stored) == keys
    fixture = {key: stored[key] for key in keys}
    shown = project("receipt", fixture, names=ProjectionNames(apps={"app:a": "Camera"}, models={"model:a": "Image model"})).model_dump(mode="json")
    for key in ("protocol", "source_kind", "status", "policy_decision", "accepted_at_ms", "updated_at_ms", *PHASE_FIELDS):
        assert shown[key] == fixture[key], key
    assert shown["request_id"] == fixture["request_id"]
    assert shown["job_id"] == fixture["job_id"]


def test_job_seconds_and_canceled_spelling_match_the_job_store():
    from gateway.media_jobs import MediaJob, STATUS_CANCELED
    job = MediaJob(request_id="req_" + "a" * 32, intent="image.generate", status=STATUS_CANCELED,
                   created_at=1_700_000_000, updated_at=1_700_000_001)
    raw = job.to_dict()
    shown = project("job", raw).model_dump(mode="json")
    for key in ("job_id", "status", "created_at", "updated_at"):
        assert shown[key] == raw[key]


def test_projection_derives_fields_from_real_receipt_writer(tmp_path):
    stored = _stored_fixture(tmp_path / "receipts.db")
    names = ProjectionNames(people={"person:a": "Synthetic person"}, providers=frozenset({"provider:a"}))
    shown = project("receipt", stored, names=names)
    assert shown.route == "provider"
    assert shown.provider == "provider:a"
    assert (shown.queue_ms, shown.execution_ms, shown.first_output_ms) == (1, 2, 1)
    assert (shown.terminal_reason, shown.status_code) == ("completed", 200)
    # Admin/client omit the maker; the internal audience preserves its stored
    # attribution. Step 3 must use that source, never the App's current owner.
    assert shown.person is None
    for audience in ("client", "admin", "internal"):
        row = receipt_store.get(stored["request_id"], audience=audience, path=tmp_path / "receipts.db")
        assert ("maker_person_id" in row) == (audience == "internal")
        projected = project("receipt", row, names=names)
        assert (projected.person.id if projected.person else None) == ("person:a" if audience == "internal" else None)
        assert (projected.terminal_reason, projected.status_code) == (("unknown", None) if audience == "client" else ("completed", 200))


@pytest.mark.parametrize("changes,route,provider", [
    ({"remote": False, "started_at_ms": 1}, "local", None),
    ({"remote": False, "backend": None, "started_at_ms": None, "queued_at_ms": None}, "unknown", None),
    ({"path": "/gateway/v1/providers/provider:a/native", "remote": False}, "provider", "provider:a"),
    ({"path": "", "protocol": "provider-native", "remote": False}, "provider", "provider:a"),
    ({"backend": "unknown-provider"}, "provider", None),
])
def test_receipt_route_uses_stored_path_protocol_and_remote(tmp_path, changes, route, provider):
    row = _stored_fixture(tmp_path / "receipts.db") | changes
    shown = project("receipt", row, names=ProjectionNames(providers=frozenset({"provider:a"})))
    assert (shown.route, shown.provider) == (route, provider)


@pytest.mark.parametrize("phases", [
    {"started_at_ms": None}, {"finished_at_ms": 0}, {"first_output_at_ms": True},
])
def test_phase_durations_never_invent_missing_or_negative_time(tmp_path, phases):
    shown = project("receipt", _stored_fixture(tmp_path / "receipts.db") | phases)
    if "started_at_ms" in phases:
        assert shown.queue_ms is shown.execution_ms is shown.first_output_ms is None
    elif "finished_at_ms" in phases:
        assert shown.execution_ms is None
    else:
        assert shown.first_output_ms is None
