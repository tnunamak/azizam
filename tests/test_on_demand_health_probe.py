"""An engine that starts on demand is idle when it is stopped, and degraded when it is busy.

Two probes judge an engine: the owner status grid and the model catalog. They used to disagree about
the same engine. The catalog probe called a refused connection "unavailable" even for an engine the
gateway starts on demand, and the status grid called it "stopped". The newer verdict won the merge, so
the owner saw "can't take requests right now" on some refreshes and nothing on others. A timeout or a
503 while a model loads is busy, not down, and the catalog had called that down too. These tests pin one
verdict per outcome, and check that a stopped on-demand engine is not recorded as down in the history.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from gateway import availability
from gateway.models_catalog import (
    HealthProbeNoResponse,
    HealthProbeRefused,
    HealthProbeTimedOut,
    probe_service_health,
)

ENDPOINT = "http://127.0.0.1:8025/health"


def _registry(residency: str | None) -> dict:
    config = {"base_url": "http://127.0.0.1:8025", "health_endpoint": "/health", "role": "standby"}
    if residency is not None:
        config["residency"] = residency
    return {"schema_version": 1, "services": {
        "decisions-quality": {"id": "decisions-quality", "enabled": True, "role": "standby", "config": config,
                              "kind": "provider", "name": "Local quality decisions"},
    }}


def _probe(registry: dict, error: Exception | None = None, payload: dict | None = None) -> dict:
    async def get_json(url: str):
        if error is not None:
            raise error
        return payload

    evidence = asyncio.run(probe_service_health(
        registry, get_json=get_json, service_ids=["decisions-quality"], endpoint_for=lambda service: ENDPOINT,
    ))
    return evidence["decisions-quality"]


def test_refused_connection_to_an_on_demand_engine_is_stopped_not_unavailable():
    evidence = _probe(_registry("on_demand"), error=HealthProbeRefused())

    assert evidence["status"] == "stopped"
    assert evidence["available"] is None


def test_refused_connection_to_a_resident_engine_is_still_unavailable():
    evidence = _probe(_registry(None), error=HealthProbeRefused())

    assert evidence["status"] == "unavailable"
    assert evidence["available"] is False


def test_timeout_is_degraded_not_unavailable():
    evidence = _probe(_registry("on_demand"), error=HealthProbeTimedOut())

    assert evidence["status"] == "degraded"
    assert evidence["available"] is None


@pytest.mark.parametrize("code", [502, 503, 504])
def test_starting_or_overloaded_engine_is_degraded(code):
    evidence = _probe(_registry("on_demand"), error=HealthProbeNoResponse(status_code=code))

    assert evidence["status"] == "degraded"


def test_server_error_is_still_unavailable():
    evidence = _probe(_registry("on_demand"), error=HealthProbeNoResponse(status_code=500))

    assert evidence["status"] == "unavailable"
    assert evidence["available"] is False


def test_owner_verdict_is_the_same_whichever_probe_ran_last():
    """The owner's grid and the catalog must judge a stopped on-demand engine as idle, in either merge order."""
    from gateway.admin_api import derive_admin_health_projection

    registry = _registry("on_demand")
    now = int(time.time())
    catalog = {"decisions-quality": {**_probe(registry, error=HealthProbeRefused()), "observed_at_s": now + 1,
                                     "last_attempt_at_s": now + 1}}
    status_row = {"service_id": "decisions-quality", "key": "decisions-quality", "name": "Local quality decisions",
                  "role": "standby", "status": "stopped", "live_status": "stopped", "kind": "process",
                  "observed_at_s": now, "starts_on_demand": True}
    payload = {"backends": [status_row], "generated_at": now}

    for health in (catalog, {}):
        evidence = availability.availability_evidence_snapshot(
            service_records=registry, status_payload=payload, health_evidence=health)
        projection = derive_admin_health_projection(registry, payload, observation_available=True,
                                                    availability_evidence=evidence)
        observation = projection["services"][0]["observation"]
        assert observation != "down", f"health evidence {'from the catalog' if health else 'alone'} said down"
        assert observation in {"standby_stopped", "idle"}


def test_stopped_on_demand_engine_is_not_recorded_as_down(monkeypatch):
    import status_endpoint

    recorded: list[tuple] = []

    class Writer:
        def record_samples(self, samples):
            recorded.extend(samples)
            return True

        def flush(self):
            return None

    import gateway.metrics_writer as metrics_writer
    monkeypatch.setattr(metrics_writer, "get_writer", lambda: Writer())
    rows = [
        {"key": "decisions-quality", "service_id": "decisions-quality", "name": "Local quality decisions",
         "role": "standby", "status": "stopped", "live_status": "stopped", "kind": "process",
         "starts_on_demand": True},
        {"key": "chat", "service_id": "chat", "name": "Chat", "role": "required", "status": "stopped",
         "live_status": "stopped"},
    ]

    status_endpoint._record_backend_health_samples(rows)

    status_codes = {tags: value for _, metric, tags, value in recorded if metric == "service_status_code"}
    decider = [value for tags, value in status_codes.items() if "decisions-quality" in tags]
    chat = [value for tags, value in status_codes.items() if '"chat"' in tags]
    assert decider == [], "a stopped on-demand engine must not be counted as down"
    assert chat == [0.0], "a stopped engine that is not started on demand is still down"
