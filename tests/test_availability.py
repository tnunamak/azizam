from __future__ import annotations

import time
from pathlib import Path

from gateway import receipt_store
from gateway.availability import evidence_from_receipt_store, evidence_from_receipts, merge_evidence, project_capabilities, project_service
from gateway.capabilities import Capability
from gateway.receipts import RouteReceipt


def test_no_health_endpoint_projects_unknown_not_unavailable():
    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
        fallback_capability=Capability(intent="chat.completions", backend="svc"),
    )

    assert projection.status == "unknown"
    assert projection.up is None
    assert projection.measured is False




def test_disabled_service_projects_unavailable():
    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": False, "config": {"base_url": "http://svc/v1"}}}},
    )

    assert projection.status == "unavailable"
    assert projection.enabled is False


def test_missing_service_projects_unconfigured_without_fallback():
    projection = project_service("missing", service_records={"services": {}})

    assert projection.status == "unavailable"
    assert projection.configured is False


def test_future_request_evidence_is_ignored():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "future", "backend": "svc", "status": "succeeded", "updated_at_ms": (now + 60) * 1000}
    ], now_s=now)

    assert evidence == {}


def test_same_second_terminal_failure_supersedes_success_by_milliseconds():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "success", "backend": "svc", "status": "succeeded", "updated_at_ms": now * 1000 + 100},
        {"request_id": "failure", "backend": "svc", "status": "failed", "status_code": 503, "updated_at_ms": now * 1000 + 900},
    ], now_s=now + 1)

    assert evidence["svc"]["request_id"] == "failure"
    assert evidence["svc"]["available"] is False


def test_top_level_lifecycle_declaration_projects_idle_when_stopped():
    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "lifecycle": {"driver": "systemd"}, "config": {"base_url": "http://svc/v1"}}}},
        evidence={"svc": {"available": None, "status": "stopped", "source": "status"}},
    )

    assert projection.status == "idle"
    assert projection.idle_startable is True

def test_recent_successful_request_marks_service_ready():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "req_1", "backend": "svc", "status": "succeeded", "_stored_at": now}
    ], now_s=now)

    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
        evidence=evidence,
    )

    assert projection.status == "ready"
    assert projection.up is True
    assert projection.evidence["source"] == "request"


def test_newer_backend_failure_supersedes_success():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "req_old", "backend": "svc", "status": "succeeded", "_stored_at": now - 2},
        {"request_id": "req_new", "backend": "svc", "status": "failed", "status_code": 503, "_stored_at": now - 1},
    ], now_s=now)

    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
        evidence=evidence,
    )

    assert projection.status == "unavailable"
    assert projection.up is False
    assert projection.evidence["request_id"] == "req_new"


def test_client_failure_does_not_become_backend_down_evidence():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "req_bad_input", "backend": "svc", "status": "failed", "status_code": 400, "_stored_at": now}
    ], now_s=now)

    assert evidence == {}


def test_startable_down_probe_projects_idle():
    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {
            "base_url": "http://svc/v1",
            "lifecycle": {"driver": "systemd"},
        }}}},
        evidence={"svc": {"available": None, "status": "stopped", "source": "health"}},
    )

    assert projection.status == "idle"
    assert projection.available is True
    assert projection.idle_startable is True


def test_projected_idle_capability_is_available():
    caps = project_capabilities(
        [Capability(intent="decision", backend="svc")],
        service_records={"services": {"svc": {"enabled": True, "config": {
            "lifecycle": {"driver": "systemd"},
        }}}},
        evidence={"svc": {"available": None, "status": "stopped", "source": "status"}},
    )

    assert caps[0].available is True
    assert caps[0].availability["status"] == "idle"


def test_policy_denial_overrides_projected_availability():
    caps = project_capabilities(
        [Capability(intent="decision", backend="svc", policy_available=False)],
        service_records={"services": {"svc": {"enabled": True, "config": {
            "lifecycle": {"driver": "systemd"},
        }}}},
        evidence={"svc": {"available": None, "status": "stopped", "source": "status"}},
    )

    assert caps[0].availability["available"] is True
    assert caps[0].available is False


def test_startable_unhealthy_probe_is_unavailable_not_idle():
    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {
            "base_url": "http://svc/v1",
            "lifecycle": {"driver": "systemd"},
        }}}},
        evidence={"svc": {"available": False, "status": "unhealthy", "source": "health"}},
    )

    assert projection.status == "unavailable"
    assert projection.idle_startable is True


def test_request_evidence_wins_over_probe_evidence():
    now = int(time.time())
    request = evidence_from_receipts([
        {"request_id": "req_1", "backend": "svc", "status": "succeeded", "_stored_at": now}
    ], now_s=now)
    merged = merge_evidence(
        {"svc": {"available": False, "status": "unavailable", "source": "health", "observed_at_s": now - 10}},
        request,
    )

    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
        evidence=merged,
    )

    assert projection.status == "ready"
    assert projection.evidence["source"] == "request"


def test_receipt_metadata_service_id_is_evidence_backend():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "req_1", "backend": "litellm", "metadata": {"service_id": "capture-engine"}, "status": "succeeded", "_stored_at": now}
    ], now_s=now)

    assert "capture-engine" in evidence
    assert "litellm" not in evidence


def test_current_probe_beats_older_request_evidence():
    now = int(time.time())
    request = evidence_from_receipts([
        {"request_id": "req_1", "backend": "svc", "status": "succeeded", "_stored_at": now - 600}
    ], now_s=now)
    merged = merge_evidence(
        request,
        {"svc": {"available": False, "status": "unhealthy", "source": "health", "observed_at_s": now}},
    )

    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
        evidence=merged,
    )

    assert projection.status == "unavailable"
    assert projection.evidence["source"] == "health"


def test_request_success_beats_health_no_response():
    now = int(time.time())
    request = evidence_from_receipts([
        {"request_id": "req_1", "backend": "svc", "status": "succeeded", "_stored_at": now}
    ], now_s=now)
    merged = merge_evidence(
        {"svc": {"available": False, "status": "unavailable", "source": "health", "kind": "no_response", "observed_at_s": now}},
        request,
    )

    assert merged["svc"]["source"] == "request"


def test_public_projection_omits_request_id():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "req_private", "backend": "svc", "status": "succeeded", "_stored_at": now}
    ], now_s=now)

    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
        evidence=evidence,
    )

    public = projection.to_public_dict()
    assert public["evidence"]["source"] == "request"
    assert "request_id" not in public["evidence"]


def test_receipt_store_evidence_uses_terminal_updated_time(tmp_path: Path):
    db = tmp_path / "receipts.db"
    receipt_store.init_db(db)
    now = int(time.time())
    receipt_store.record({
        "request_id": "long_success",
        "backend": "svc",
        "status": "succeeded",
        "accepted_at_ms": (now - 3600) * 1000,
        "updated_at_ms": now * 1000,
    }, created_at=now - 3600, path=db)

    evidence = evidence_from_receipt_store(db, now_s=int(time.time()) + 1, max_age_s=900)

    assert evidence["svc"]["request_id"] == "long_success"


def test_receipt_store_evidence_reads_internal_metadata_status_code(tmp_path: Path):
    db = tmp_path / "receipts.db"
    receipt_store.init_db(db)
    now = int(time.time())
    old = RouteReceipt(protocol="openai", intent="chat.completions", backend="svc")
    old.finish("ok")
    receipt_store.record(old, created_at=now - 2, path=db)
    failed = RouteReceipt(protocol="openai", intent="chat.completions", backend="svc")
    failed.metadata["status_code"] = 503
    failed.finish("error")
    receipt_store.record(failed, created_at=now - 1, path=db)

    evidence = evidence_from_receipt_store(db, now_s=now, max_age_s=900)

    assert evidence["svc"]["request_id"] == failed.request_id
    assert evidence["svc"]["available"] is False


def test_receipt_order_uses_terminal_updated_time():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "accepted_later", "backend": "svc", "status": "succeeded", "accepted_at_ms": (now - 10) * 1000, "updated_at_ms": (now - 10) * 1000},
        {"request_id": "finished_later", "backend": "svc", "status": "failed", "status_code": 503, "accepted_at_ms": (now - 100) * 1000, "updated_at_ms": now * 1000},
    ], now_s=now)

    assert evidence["svc"]["request_id"] == "finished_later"
    assert evidence["svc"]["available"] is False
