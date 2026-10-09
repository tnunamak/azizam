from __future__ import annotations

import time
from pathlib import Path
import pytest

from gateway import receipt_store
from gateway.availability import evidence_from_receipt_store, evidence_from_receipts, evidence_from_status_rows, merge_evidence, project_capabilities, project_service
from gateway.capabilities import Capability
from gateway.receipts import RouteReceipt


@pytest.mark.parametrize("observed,expected", [
    ("active", "running"),
    ("activating", "starting"),
    ("deactivating", "draining"),
    ("failed", "failed"),
    ("inactive", "stopped"),
    ("unknown", "unmanaged"),
    ("unexpected", "unmanaged"),
])
def test_systemd_status_observes_process_state_without_health_probe(monkeypatch, observed, expected):
    import asyncio
    from gateway import lifecycle_drivers

    class Process:
        async def communicate(self):
            return observed.encode(), b""

    async def create_process(*command, **kwargs):
        assert command == ("systemctl", "--user", "is-active", "fake.service")
        return Process()

    monkeypatch.setattr(lifecycle_drivers.asyncio, "create_subprocess_exec", create_process)
    service = {"lifecycle": {"driver": "systemd", "driver_config": {"unit": "fake.service", "scope": "user"}}}
    assert asyncio.run(lifecycle_drivers.SystemdDriver().status(service)) == expected


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


def test_startable_stopped_probe_projects_idle():
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
    old.finish("ok", now_ms=(now - 2) * 1000)
    receipt_store.record(old, created_at=now - 2, path=db)
    failed = RouteReceipt(protocol="openai", intent="chat.completions", backend="svc")
    failed.metadata["status_code"] = 503
    failed.finish("error", now_ms=(now - 1) * 1000)
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


def test_backend_grid_reports_lifecycle_stopped_from_runtime_record(monkeypatch):
    import asyncio
    import status_endpoint

    class FakeDriver:
        async def preflight(self, svc):
            assert svc["lifecycle"]["driver_config"]["unit"] == "fake.service"
            return None
        async def status(self, svc):
            return "stopped"

    monkeypatch.setattr(status_endpoint.lifecycle_drivers, "driver_for_service", lambda service_id, config_backends=None: FakeDriver())
    rows = asyncio.run(status_endpoint._backend_grid({"backends": {"svc": {
        "name": "Service",
        "role": "required",
        "lifecycle": {"driver": "systemd", "driver_config": {"unit": "fake.service"}},
    }}}))

    assert rows == [{
        "key": "svc",
        "service_id": "svc",
        "name": "Service",
        "url": None,
        "role": "required",
        "note": None,
        "status": "stopped",
        "live_status": "stopped",
        "latency_ms": None,
        "last_error": None,
        "source": "lifecycle",
        "kind": "process",
    }]


def test_backend_grid_lifecycle_starting_overrides_http_down(monkeypatch):
    import asyncio
    import status_endpoint

    class FakeDriver:
        async def preflight(self, svc):
            return None
        async def status(self, svc):
            return "starting"

    async def fake_probe(client, key, name, url, role, note, **kwargs):
        return {"key": key, "service_id": key, "name": name, "url": url, "role": role,
                "note": note, "status": "down", "live_status": "down", "latency_ms": None,
                "last_error": "connect refused"}

    monkeypatch.setattr(status_endpoint.lifecycle_drivers, "driver_for_service", lambda service_id, config_backends=None: FakeDriver())
    monkeypatch.setattr(status_endpoint, "_probe", fake_probe)
    rows = asyncio.run(status_endpoint._backend_grid({"backends": {"svc": {
        "host": "127.0.0.1",
        "port": 9,
        "health_endpoint": "/health",
        "lifecycle": {"driver": "command", "driver_config": {"status": ["true"]}},
    }}}))

    assert rows[0]["live_status"] == "starting"
    assert rows[0]["source"] == "lifecycle"


def test_backend_grid_lifecycle_running_does_not_hide_http_down(monkeypatch):
    import asyncio
    import status_endpoint

    class FakeDriver:
        async def preflight(self, svc):
            return None
        async def status(self, svc):
            return "running"

    async def fake_probe(client, key, name, url, role, note, **kwargs):
        return {"key": key, "service_id": key, "name": name, "url": url, "role": role,
                "note": note, "status": "down", "live_status": "down", "latency_ms": None,
                "last_error": "connect refused"}

    monkeypatch.setattr(status_endpoint.lifecycle_drivers, "driver_for_service", lambda service_id, config_backends=None: FakeDriver())
    monkeypatch.setattr(status_endpoint, "_probe", fake_probe)
    rows = asyncio.run(status_endpoint._backend_grid({"backends": {"svc": {
        "host": "127.0.0.1",
        "port": 9,
        "health_endpoint": "/health",
        "lifecycle": {"driver": "docker", "driver_config": {"container": "fake"}},
    }}}))

    assert rows[0]["live_status"] == "down"
    assert rows[0].get("source") != "lifecycle"


def test_backend_grid_lifecycle_unmanaged_is_unknown_not_stopped(monkeypatch):
    import asyncio
    import status_endpoint

    class FakeDriver:
        async def preflight(self, svc):
            return None
        async def status(self, svc):
            return "unmanaged"

    monkeypatch.setattr(status_endpoint.lifecycle_drivers, "driver_for_service", lambda service_id, config_backends=None: FakeDriver())
    rows = asyncio.run(status_endpoint._backend_grid({"backends": {"svc": {
        "lifecycle": {"driver": "systemd", "driver_config": {"unit": "missing.service"}},
    }}}))

    assert rows[0]["live_status"] == "unknown"


def test_backend_grid_lifecycle_preflight_warning_is_unknown_not_stopped(monkeypatch):
    import asyncio
    import status_endpoint

    class FakeDriver:
        async def preflight(self, svc):
            return "systemd driver_config.unit is required but not set"
        async def status(self, svc):
            return "stopped"

    monkeypatch.setattr(status_endpoint.lifecycle_drivers, "driver_for_service", lambda service_id, config_backends=None: FakeDriver())
    rows = asyncio.run(status_endpoint._backend_grid({"backends": {"svc": {
        "lifecycle": {"driver": "systemd", "driver_config": {}},
    }}}))

    assert rows[0]["live_status"] == "unknown"
    assert rows[0]["last_error"] == "systemd driver_config.unit is required but not set"


def test_backend_grid_excludes_http_load_unload_from_process_lifecycle(monkeypatch):
    import asyncio
    import status_endpoint

    def fail_driver(*args, **kwargs):
        raise AssertionError("http-load-unload should not be treated as process lifecycle evidence")

    monkeypatch.setattr(status_endpoint.lifecycle_drivers, "driver_for_service", fail_driver)
    rows = asyncio.run(status_endpoint._backend_grid({"backends": {"svc": {
        "lifecycle": {"driver": "http-load-unload", "driver_config": {"load_url": "/load"}},
    }}}))

    assert rows == []


def test_status_rows_preserve_no_response_kind_so_request_can_win():
    now = int(time.time())
    status = evidence_from_status_rows([
        {"service_id": "svc", "live_status": "down", "source": "status", "kind": "no_response"}
    ], observed_at_s=now)
    request = evidence_from_receipts([
        {"request_id": "req_ok", "backend": "svc", "status": "succeeded", "updated_at_ms": now * 1000}
    ], now_s=now)

    merged = merge_evidence(status, request)

    assert status["svc"]["kind"] == "no_response"
    assert merged["svc"]["source"] == "request"


def test_status_probe_connect_failure_marks_no_response_kind():
    import asyncio
    import httpx
    import status_endpoint

    class Client:
        async def get(self, url):
            raise httpx.ConnectError("refused")

    row = asyncio.run(status_endpoint._probe(Client(), "svc", "Service", "http://127.0.0.1:9/health", "required", None))

    assert row["live_status"] == "down"
    assert row["kind"] == "no_response"


def test_cached_status_payload_for_availability_expires(monkeypatch):
    import proxy

    now = proxy.time.monotonic()
    monkeypatch.setattr(proxy.status_endpoint, "_CACHE_TTL_S", 10.0)
    monkeypatch.setattr(proxy.status_endpoint, "_CACHE", {"ts": now - 11.0, "data": {"backends": [{"key": "svc", "status": "up"}]}})
    assert proxy._cached_status_payload_for_availability() == {}

    proxy.status_endpoint._CACHE = {"ts": now, "data": {"backends": [{"key": "svc", "status": "up"}]}}
    assert proxy._cached_status_payload_for_availability()["backends"][0]["key"] == "svc"


def test_status_probe_200_ready_false_is_not_up():
    import asyncio
    import json
    import status_endpoint

    class Response:
        status_code = 200
        def json(self):
            return {"ready": False}
    class Client:
        async def get(self, url):
            return Response()

    row = asyncio.run(status_endpoint._probe(Client(), "svc", "Service", "http://svc/health", "required", None))

    assert row["live_status"] == "down"
    assert row["last_error"] == "readiness false"


def test_status_probe_200_starting_is_starting_not_up():
    import asyncio
    import status_endpoint

    class Response:
        status_code = 200
        def json(self):
            return {"status": "starting"}
    class Client:
        async def get(self, url):
            return Response()

    row = asyncio.run(status_endpoint._probe(Client(), "svc", "Service", "http://svc/health", "required", None))

    assert row["live_status"] == "starting"


def test_status_probe_200_unrecognized_json_is_unknown_not_up():
    import asyncio
    import status_endpoint

    class Response:
        status_code = 200
        def json(self):
            return {"object": "list", "data": []}
    class Client:
        async def get(self, url):
            return Response()

    row = asyncio.run(status_endpoint._probe(Client(), "svc", "Service", "http://svc/v1/models", "required", None))

    assert row["live_status"] == "unknown"


def test_status_probe_404_missing_health_route_is_weak_no_response():
    import asyncio
    import status_endpoint

    class Response:
        status_code = 404
        def json(self):
            return {"error": "not found"}
    class Client:
        async def get(self, url):
            return Response()

    row = asyncio.run(status_endpoint._probe(Client(), "svc", "Service", "http://svc/health-not-implemented", "required", None))

    assert row["live_status"] == "unknown"
    assert row["kind"] == "no_response"


def test_status_404_missing_health_route_does_not_override_recent_success():
    now = int(time.time())
    status = evidence_from_status_rows([
        {"service_id": "svc", "live_status": "unknown", "source": "status", "kind": "no_response"}
    ], observed_at_s=now)
    request = evidence_from_receipts([
        {"request_id": "req_ok", "backend": "svc", "status": "succeeded", "updated_at_ms": now * 1000}
    ], now_s=now)

    merged = merge_evidence(status, request)

    assert merged["svc"]["source"] == "request"
    projection = project_service(
        "svc",
        service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1", "health_endpoint": "/health-not-implemented"}}}},
        evidence=merged,
    )
    assert projection.status == "ready"


def test_alias_projection_inherits_unique_concrete_target():
    alias = Capability(intent="chat.completions", backend="gateway", model="fast", tags=("alias", "resolves_to:actual"))
    concrete = Capability(intent="chat.completions", backend="svc", model="actual")
    caps = project_capabilities([alias, concrete], service_records={"services": {"svc": {"enabled": True}}},
                                evidence={"svc": {"status": "ready", "available": True, "observed_at_s": 123}})
    assert caps[0].backend == "svc"
    assert caps[0].availability["status"] == "ready"
    assert caps[0].availability["evidence"]["observed_at_s"] == 123


def test_caller_catalog_deduplicates_same_visible_model():
    from gateway.capabilities import summarize_capabilities
    caps = project_capabilities([
        Capability(intent="chat.completions", backend="gateway", model="actual"),
        Capability(intent="chat.completions", backend="svc", model="actual"),
    ], evidence={"svc": {"status": "ready", "available": True}})
    rows = summarize_capabilities(caps, "client")["capabilities"]
    assert len(rows) == 1
    assert rows[0]["availability"]["status"] == "ready"


def test_status_targets_use_declared_base_url():
    import status_endpoint
    targets = status_endpoint._resolve_backend_targets({"backends": {"svc": {
        "base_url": "https://example.test/v1", "health_endpoint": "/health",
    }}})
    assert targets[0]["url"] == "https://example.test/health"


def test_ambiguous_alias_target_preserves_unknown():
    caps = project_capabilities([
        Capability(intent="chat.completions", backend="gateway", model="fast", tags=("alias", "resolves_to:actual")),
        Capability(intent="chat.completions", backend="a", model="actual"),
        Capability(intent="chat.completions", backend="b", model="actual"),
    ], evidence={"a": {"status": "ready", "available": True}})
    assert caps[0].backend == "gateway"
    assert caps[0].availability["status"] == "unknown"


def test_alias_target_resolves_from_registry_model_group():
    caps = project_capabilities([
        Capability(intent="chat.completions", backend="gateway", model="fast", tags=("alias", "resolves_to:logical")),
    ], service_records={"services": {"svc": {"config": {"models": [{"id": "engine-id", "model_group": "logical"}]}}}},
       evidence={"svc": {"status": "ready", "available": True}})
    assert caps[0].backend == "svc"
    assert caps[0].availability["status"] == "ready"


def test_alias_target_resolves_from_missing_model_file_as_unavailable():
    missing = str(Path(__file__).resolve().parent / "fixtures" / "missing-model.gguf")
    caps = project_capabilities([
        Capability(intent="chat.completions", backend="gateway", model="legacy", tags=("alias", "resolves_to:legacy")),
    ], service_records={"services": {"svc": {"enabled": True, "config": {"models": [{
        "id": "legacy",
        "model_group": "legacy",
        "upstream_model": "legacy.gguf",
        "model_path": missing,
    }]}}}},
       evidence={"svc": {"status": "ready", "available": True}})

    assert caps[0].backend == "svc"
    assert caps[0].availability["status"] == "unavailable"
    assert caps[0].availability["reason"] == "Model file is not installed."
    assert not caps[0].available


def test_model_level_unavailability_wins_over_ready_service():
    missing_projection = {
        "available": False,
        "status": "unavailable",
        "reason": "Model file is not installed.",
        "measured": True,
    }
    caps = project_capabilities([
        Capability(intent="chat.completions", backend="svc", model="legacy", availability=missing_projection),
    ], service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
       evidence={"svc": {"status": "ready", "available": True}})

    assert not caps[0].available
    assert caps[0].availability == missing_projection
    assert caps[0].lifecycle == "stopped"
    assert caps[0].health == "unhealthy"


def test_alias_inherits_missing_file_reason_from_concrete_target():
    missing = {"available": False, "status": "unavailable", "reason": "Model file is not installed.", "measured": True}
    caps = project_capabilities([
        Capability(intent="chat.completions", backend="gateway", model="default", tags=("alias", "resolves_to:weights")),
        Capability(intent="chat.completions", backend="svc", model="weights", availability=missing),
    ], service_records={"services": {"svc": {"enabled": True, "config": {"base_url": "http://svc/v1"}}}},
       evidence={"svc": {"status": "ready", "available": True}})
    alias = next(cap for cap in caps if cap.model == "default")
    assert alias.backend == "svc"
    assert alias.availability == missing
    assert not alias.available


@pytest.mark.parametrize("payload,expected", [({"system": {}}, "up"), ({"devices": []}, "unknown"), ({"system": {}, "ready": False}, "down")])
def test_declared_health_success_path_does_not_mask_readiness(payload, expected):
    import asyncio
    import status_endpoint
    class Response:
        status_code = 200
        def json(self):
            return payload
    class Client:
        async def get(self, url):
            return Response()
    row = asyncio.run(status_endpoint._probe(Client(), "svc", "Service", "http://fixture/stats", "required", None,
                                             service={"health_success_path": "system"}))
    assert row["live_status"] == expected


def test_catalog_configured_health_success_has_observation_time():
    import asyncio
    from gateway.models_catalog import probe_service_health
    async def get_json(url):
        assert url == "http://fixture/stats"
        return {"system": {"version": "fixture"}}
    evidence = asyncio.run(probe_service_health({"services": {"svc": {"config": {
        "base_url": "http://fixture/v1", "health_endpoint": "/stats", "health_success_path": "system",
    }}}}, get_json=get_json))
    assert evidence["svc"]["status"] == "ready"
    assert isinstance(evidence["svc"]["observed_at_s"], int)


def test_workflow_idle_projection_preserves_evidence():
    from gateway.workflows_catalog import _availability
    caps = project_capabilities([Capability(intent="image.generate", backend="svc", workflow="fixture")],
        service_records={"services": {"svc": {"lifecycle": {"driver": "command"}}}},
        evidence={"svc": {"status": "stopped", "source": "lifecycle", "observed_at_s": 123}})
    result = _availability(caps)
    assert result["status"] == "idle"
    assert result["available"] is True
    assert result["evidence"]["observed_at_s"] == 123


def test_alias_binding_inherits_target_policy_facts():
    from gateway.capabilities import resolve_capability_targets
    caps = resolve_capability_targets([
        Capability(intent="chat.completions", backend="gateway", model="fast", tags=("alias", "resolves_to:actual")),
        Capability(intent="chat.completions", backend="svc", model="actual", exposure="opt_in", trust_tier="experimental", tags=("private",)),
    ])
    assert caps[0].exposure == "opt_in"
    assert caps[0].trust_tier == "experimental"
    assert "private" in caps[0].tags


def test_cold_capability_provider_measures_declared_services(monkeypatch, tmp_path):
    import asyncio
    import proxy
    receipts = tmp_path / "cold-receipts.db"
    receipt_store.init_db(receipts)
    monkeypatch.setattr(proxy.receipt_store, "DB_PATH", receipts)
    registry = {"services": {"svc": {"enabled": True, "config": {"base_url": "http://fixture", "health_endpoint": "/health"}}}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(proxy, "_gateway_capabilities_for_auth_context", lambda auth: [Capability(intent="chat.completions", backend="svc", model="actual")])
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda request: None)
    monkeypatch.setattr(proxy, "_cached_status_payload_for_availability", lambda: {})
    observed = []
    async def grid(config):
        observed.append(config)
        return [{"service_id": "svc", "status": "up", "observed_at_s": 123}]
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", grid)
    caps = asyncio.run(proxy._gateway_capabilities_provider(None))
    assert observed[0]["backends"]["svc"]["health_endpoint"] == "/health"
    assert caps[0].availability["status"] == "ready"
    assert caps[0].availability["evidence"]["observed_at_s"] == 123


@pytest.mark.parametrize("reverse", [False, True])
def test_stopped_lifecycle_is_not_overridden_by_later_connect_failure(reverse):
    stopped = {"svc": {"status": "stopped", "source": "lifecycle", "observed_at_s": 100}}
    no_response = {"svc": {"status": "unavailable", "available": False, "source": "health", "kind": "no_response", "observed_at_s": 101}}
    sources = [stopped, no_response]
    if reverse:
        sources.reverse()
    projection = project_service("svc", service_records={"services": {"svc": {"lifecycle": {"driver": "systemd"}}}},
                                 evidence=merge_evidence(*sources))
    assert projection.status == "idle"


@pytest.mark.parametrize("allowed,expected", [(frozenset(), False), (frozenset({"workflow-id"}), True)])
def test_workflow_alias_policy_uses_resolved_workflow_without_changing_alias(monkeypatch, allowed, expected):
    import proxy
    from gateway.capabilities import resolve_capability_targets, summarize_capability
    from gateway.policy import AppProfile
    monkeypatch.setattr(proxy, "_profile_for_auth_context", lambda context: AppProfile(id="workflow-policy", workflows=allowed))
    alias, concrete = resolve_capability_targets([
        Capability(intent="image.generate", backend="gateway", model="image", tags=("alias", "resolves_to:workflow-id")),
        Capability(intent="image.generate", backend="fixture", workflow="workflow-id"),
    ])
    assert proxy._capability_for_profile(concrete, None).policy_available is expected
    projected = proxy._capability_for_profile(alias, None)
    assert projected.policy_available is expected
    assert projected.model == "image"
    assert projected.workflow is None
    assert "resolved_workflow" not in summarize_capability(projected, "client")


def test_denied_alias_shadows_allowed_same_named_model_before_filtering():
    from gateway.capabilities import caller_visible_capabilities, resolve_capability_targets
    alias = Capability(intent="chat.completions", backend="remote", model="fast", policy_available=False,
                       tags=("alias", "resolves_to:remote-model"))
    local = Capability(intent="chat.completions", backend="local", model="fast", policy_available=True)
    assert caller_visible_capabilities([local, alias]) == [alias]
    resolved = resolve_capability_targets([local, alias])
    assert resolved == [alias]
    assert [cap for cap in resolved if cap.policy_available] == []


def test_availability_does_not_measure_services_when_caller_has_no_allowed_capabilities(monkeypatch):
    import asyncio
    import proxy
    def unexpected(*args, **kwargs):
        raise AssertionError("No allowed capabilities require measurement")
    monkeypatch.setattr(proxy, "_cached_status_payload_for_availability", unexpected)
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", unexpected)
    denied = Capability(intent="chat.completions", backend="private", policy_available=False)
    assert asyncio.run(proxy._capabilities_availability_evidence([], {})) == {}
    assert asyncio.run(proxy._capabilities_availability_evidence([denied], {})) == {}


def test_availability_probes_only_caller_visible_services(monkeypatch, tmp_path):
    import asyncio
    import proxy
    receipts = tmp_path / "scoped-receipts.db"
    receipt_store.init_db(receipts)
    monkeypatch.setattr(proxy.receipt_store, "DB_PATH", receipts)
    registry = {"services": {name: {"config": {"base_url": f"http://{name}", "health_endpoint": "/health"}}
                             for name in ("allowed", "private")}}
    monkeypatch.setattr(proxy, "_cached_status_payload_for_availability", lambda: {})
    async def grid(config):
        assert set(config["backends"]) == {"allowed"}
        return []
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", grid)
    caps = [Capability(intent="chat.completions", backend="allowed"),
            Capability(intent="chat.completions", backend="private", policy_available=False)]
    assert asyncio.run(proxy._capabilities_availability_evidence(caps, registry)) == {}


# Evidence time is the check time (design ask 3). Nothing here may invent a
# time: absent means "not known", and the attempt and the last success are
# separate facts.

def test_status_rows_without_check_time_carry_no_time():
    evidence = evidence_from_status_rows([{"service_id": "svc", "live_status": "up"}])
    assert "observed_at_s" not in evidence["svc"]
    assert "last_success_at_s" not in evidence["svc"]


def test_status_rows_carry_snapshot_check_time_and_success_only_when_ready():
    evidence = evidence_from_status_rows([
        {"service_id": "up", "live_status": "up"},
        {"service_id": "down", "live_status": "down"},
    ], observed_at_s=100)
    assert {key: evidence["up"][key] for key in ("observed_at_s", "last_attempt_at_s", "last_success_at_s")} == {
        "observed_at_s": 100, "last_attempt_at_s": 100, "last_success_at_s": 100}
    assert evidence["down"]["observed_at_s"] == 100
    assert evidence["down"]["last_attempt_at_s"] == 100
    assert "last_success_at_s" not in evidence["down"]


def test_snapshot_without_generated_at_does_not_stamp_now():
    from gateway.availability import availability_evidence_snapshot
    evidence = availability_evidence_snapshot(
        status_payload={"backends": [{"service_id": "svc", "live_status": "degraded"}]}, now_s=999)
    assert "observed_at_s" not in evidence["svc"]
    stamped = availability_evidence_snapshot(
        status_payload={"generated_at": 120, "backends": [{"service_id": "svc", "live_status": "degraded"}]}, now_s=999)
    assert stamped["svc"]["observed_at_s"] == 120


def test_failed_health_probe_keeps_attempt_time_and_last_success_separate(monkeypatch):
    import asyncio
    from gateway import models_catalog
    monkeypatch.setattr(models_catalog, "_HEALTH_LAST_SUCCESS_S", {})
    clock = {"now": 1000}
    monkeypatch.setattr(models_catalog.time, "time", lambda: clock["now"])
    answers = iter([{"status": "ok"}, None])
    async def get_json(_url):
        return next(answers)
    registry = {"services": {"svc": {"config": {"health_endpoint": "http://svc/health"}}}}
    ok = asyncio.run(models_catalog.probe_service_health(registry, get_json=get_json))["svc"]
    assert (ok["observed_at_s"], ok["last_attempt_at_s"], ok["last_success_at_s"]) == (1000, 1000, 1000)
    clock["now"] = 2000
    failed = asyncio.run(models_catalog.probe_service_health(registry, get_json=get_json))["svc"]
    assert failed["available"] is False and failed["kind"] == "no_response"
    assert failed["observed_at_s"] == 2000
    assert failed["last_attempt_at_s"] == 2000
    assert failed["last_success_at_s"] == 1000
    public = project_service("svc", service_records=registry, evidence={"svc": failed}).to_public_dict()["evidence"]
    assert public["last_success_at_s"] == 1000 and public["last_attempt_at_s"] == 2000


def test_failed_health_probe_without_known_success_has_no_success_time(monkeypatch):
    import asyncio
    from gateway import models_catalog
    monkeypatch.setattr(models_catalog, "_HEALTH_LAST_SUCCESS_S", {})
    async def get_json(_url):
        return None
    evidence = asyncio.run(models_catalog.probe_service_health(
        {"services": {"svc": {"config": {"health_endpoint": "http://svc/health"}}}}, get_json=get_json))["svc"]
    assert isinstance(evidence["observed_at_s"], int)
    assert "last_success_at_s" not in evidence


def test_unmeasured_health_has_no_observation_time():
    import asyncio
    from gateway.models_catalog import probe_service_health
    async def get_json(_url):
        raise AssertionError("no endpoint is declared")
    evidence = asyncio.run(probe_service_health({"services": {"svc": {"config": {}}}}, get_json=get_json))
    assert evidence["svc"]["status"] == "unknown"
    assert "observed_at_s" not in evidence["svc"]


def test_unmeasured_placeholder_does_not_mask_a_real_status_observation():
    merged = merge_evidence(
        {"svc": {"available": None, "status": "unknown", "source": None}},
        {"svc": {"available": False, "status": "down", "source": "status", "observed_at_s": 100}},
    )
    assert merged["svc"]["status"] == "down"
    merged = merge_evidence(
        {"svc": {"available": False, "status": "down", "source": "status", "observed_at_s": 100}},
        {"svc": {"available": None, "status": "unknown", "source": None}},
    )
    assert merged["svc"]["status"] == "down"


def test_request_evidence_carries_last_success_separately_from_latest_failure():
    now = int(time.time())
    evidence = evidence_from_receipts([
        {"request_id": "ok", "backend": "svc", "status": "succeeded", "_stored_at": now - 100},
        {"request_id": "bad", "backend": "svc", "status": "failed", "status_code": 503, "_stored_at": now - 10},
    ], now_s=now)["svc"]
    assert evidence["available"] is False
    assert evidence["observed_at_s"] == now - 10
    assert evidence["last_attempt_at_s"] == now - 10
    assert evidence["last_success_at_s"] == now - 100


# Busy versus failing (design ask 4). A service whose recent requests fail
# with server errors is not "busy", even when its status probe says busy or
# degraded and that probe is newer than the failed request.

@pytest.mark.parametrize("raw_status", ["busy", "degraded"])
@pytest.mark.parametrize("reverse", [False, True])
def test_recent_5xx_request_failure_is_not_projected_as_busy(raw_status, reverse):
    now = int(time.time())
    request = evidence_from_receipts([
        {"request_id": "bad", "backend": "svc", "status": "failed", "status_code": 503, "_stored_at": now - 120},
    ], now_s=now)
    status = evidence_from_status_rows([{"service_id": "svc", "live_status": raw_status}], observed_at_s=now)
    sources = [status, request]
    if reverse:
        sources.reverse()
    projection = project_service("svc", service_records={"services": {"svc": {"enabled": True}}},
                                 evidence=merge_evidence(*sources))
    assert projection.status == "unavailable"
    assert projection.available is False
    assert projection.evidence["source"] == "request"


def test_later_request_success_clears_the_failure_and_busy_stays_busy():
    now = int(time.time())
    request = evidence_from_receipts([
        {"request_id": "bad", "backend": "svc", "status": "failed", "status_code": 503, "_stored_at": now - 120},
        {"request_id": "ok", "backend": "svc", "status": "succeeded", "_stored_at": now - 60},
    ], now_s=now)
    status = evidence_from_status_rows([{"service_id": "svc", "live_status": "busy"}], observed_at_s=now)
    projection = project_service("svc", service_records={"services": {"svc": {"enabled": True}}},
                                 evidence=merge_evidence(status, request))
    assert projection.status == "busy"


def test_busy_without_request_failure_stays_busy():
    now = int(time.time())
    status = evidence_from_status_rows([{"service_id": "svc", "live_status": "busy"}], observed_at_s=now)
    # A client-side failure (4xx) proves nothing about the backend.
    request = evidence_from_receipts([
        {"request_id": "bad", "backend": "svc", "status": "failed", "status_code": 400, "_stored_at": now - 10},
    ], now_s=now)
    projection = project_service("svc", service_records={"services": {"svc": {"enabled": True}}},
                                 evidence=merge_evidence(status, request))
    assert projection.status == "busy"


def test_degraded_health_check_keeps_member_projection_degraded():
    evidence = evidence_from_status_rows(
        [{"service_id": "svc", "live_status": "degraded"}], observed_at_s=500,
    )
    capabilities = project_capabilities(
        [Capability(intent="chat.completions", backend="svc")],
        service_records={"services": {"svc": {"enabled": True}}},
        evidence=evidence,
    )

    availability = capabilities[0].availability
    assert availability["status"] == "degraded"
    assert availability["available"] is False
    assert availability["up"] is True
    assert availability["measured"] is True
    assert capabilities[0].health == "degraded"


def test_availability_projection_is_presentation_only_not_a_routing_input():
    """Routing and fallback must not read gateway.availability.

    The projection changes for design ask 4 are safe only while this holds.
    """
    import ast
    root = Path(__file__).resolve().parents[1]
    importers = set()
    for path in sorted((root / "gateway").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "gateway.availability":
                importers.add(path.relative_to(root).as_posix())
            elif isinstance(node, ast.ImportFrom) and node.module == "gateway" and any(a.name == "availability" for a in node.names):
                importers.add(path.relative_to(root).as_posix())
            elif isinstance(node, ast.Import) and any(a.name == "gateway.availability" for a in node.names):
                importers.add(path.relative_to(root).as_posix())
    # Discovery, Home/Status, admin Status, and catalog presentation only.
    assert importers <= {"gateway/home_api.py", "gateway/admin_api.py", "gateway/models_catalog.py",
                         "gateway/capabilities.py"}

    tree = ast.parse((root / "proxy.py").read_text())
    names = {"availability_evidence_snapshot", "project_capabilities", "_runtime_availability_evidence",
             "_capabilities_availability_evidence", "_status_payload_with_availability", "_gateway_capabilities_provider"}
    users: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & names
            if used:
                users[node.name] = used
    assert set(users) <= {
        "_gateway_capabilities_provider", "_capabilities_availability_evidence", "_runtime_availability_evidence",
        "_status_payload_with_availability", "_user_models_catalog_payload", "_user_workflows_catalog_payload",
    }
    # The two provider callables are only handed to discovery and Home/Status routers.
    module_refs = [n for n in ast.walk(tree) if isinstance(n, ast.keyword)
                   and isinstance(n.value, ast.Name) and n.value.id in {"_gateway_capabilities_provider", "_status_payload_with_availability"}]
    assert {(k.arg, k.value.id) for k in module_refs} <= {
        ("capabilities_provider", "_gateway_capabilities_provider"),
        ("status_provider", "_status_payload_with_availability"),
    }


@pytest.mark.parametrize("raw_status", ["busy", "degraded"])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_second_ready_health_check_beats_busy_status_row(raw_status, reverse):
    health = {"svc": {"source": "health", "status": "ready", "available": True, "observed_at_s": 500}}
    status = evidence_from_status_rows([{"service_id": "svc", "live_status": raw_status}], observed_at_s=500)
    sources = [health, status]
    if reverse:
        sources.reverse()
    merged = merge_evidence(*sources)
    assert merged["svc"]["source"] == "health"
    projection = project_service("svc", service_records={"services": {"svc": {"enabled": True}}}, evidence=merged)
    assert projection.status == "ready"


def test_newer_busy_row_still_beats_older_ready_check():
    health = {"svc": {"source": "health", "status": "ready", "available": True, "observed_at_s": 499}}
    status = evidence_from_status_rows([{"service_id": "svc", "live_status": "busy"}], observed_at_s=500)
    assert merge_evidence(health, status)["svc"]["source"] == "status"
