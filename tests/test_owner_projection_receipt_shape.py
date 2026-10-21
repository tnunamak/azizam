"""Projection regression against keys copied from a synthetic stored receipt.

The checked-in fixture contains field names only, never receipt values/content.
"""
import json
from pathlib import Path

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
                          source_kind="app", resolved_model="model:a", status="cancelled", policy_decision="allowed",
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
    job = MediaJob(request_id="req_" + "a" * 32, status=STATUS_CANCELED,
                   created_at=1_700_000_000, updated_at=1_700_000_001)
    raw = job.to_dict()
    shown = project("job", raw).model_dump(mode="json")
    for key in ("job_id", "status", "created_at", "updated_at"):
        assert shown[key] == raw[key]
