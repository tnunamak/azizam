"""``engine_unavailable``: which failures it names, and the availability row a capability carries (train 54, L3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway import engine_unavailable
from gateway.availability import last_healthy_at, requirements_of, workflow_availability
from gateway.engine_protocol.errors import TransportFailure, UpstreamError

_COMFYUI = {"enabled": True, "name": "ComfyUI", "config": {"base_url": "http://comfyui"}}


@pytest.mark.parametrize("error,expected", [
    (TransportFailure("unreachable", "refused"), (True, "refused")),
    (TransportFailure("dns", "no such host"), (False, "dns")),
    (UpstreamError(502, "The engine answered 502.", body=b""), (True, "proxy")),
    (UpstreamError(503, "The engine answered 503.", body=b"Service Unavailable"), (True, "proxy")),
    (UpstreamError(504, "The engine answered 504.", body=b"<html>gateway timeout</html>"), (True, "proxy")),
])
def test_the_cases_the_code_can_tell_apart(error, expected):
    verdict = engine_unavailable.classify(error)
    assert (verdict.retryable, verdict.reason) == expected


@pytest.mark.parametrize("error", [
    TransportFailure("timeout", "slow"),       # a connect timeout is not told apart from a read timeout
    TransportFailure("tls", "bad cert"),
    UpstreamError(500, "The engine answered 500.", body=b'{"error": "model crashed"}'),
    UpstreamError(502, "The engine answered 502.", body=b'{"error": "out of memory in the model"}'),
    ValueError("anything else"),
])
def test_everything_else_is_not_engine_unavailable(error):
    assert engine_unavailable.classify(error) is None


def test_the_runner_unreachable_code_counts_only_as_a_502_not_a_busy_503():
    class Runner(Exception):
        code = "upstream_unavailable"

    unreachable, busy = Runner(), Runner()
    unreachable.upstream_status, busy.upstream_status = 502, 503
    assert engine_unavailable.classify(unreachable).retryable is True
    assert engine_unavailable.classify(busy) is None


def test_the_verdict_follows_the_cause_chain():
    try:
        try:
            raise TransportFailure("unreachable", "refused")
        except TransportFailure as cause:
            raise RuntimeError("wrapped") from cause
    except RuntimeError as wrapped:
        assert engine_unavailable.classify(wrapped).reason == "refused"


def test_last_healthy_at_reads_the_evidence_and_is_none_without_it():
    assert last_healthy_at("comfyui", {"comfyui": {"last_success_at_s": 1_700_000_000}}) == 1_700_000_000
    assert last_healthy_at("comfyui", {"comfyui": {}}) is None
    assert last_healthy_at("comfyui", None) is None
    assert last_healthy_at(None, {"comfyui": {"last_success_at_s": 1_700_000_000}}) is None


def _record():
    from gateway.discovery import WorkflowRegistry

    registry = WorkflowRegistry.from_directory(Path(__file__).resolve().parents[1] / "manifests/workflows")
    records = registry.records()
    return {r.id: r for r in records}, next(r for r in records if "comfyui" in r.supported_backends
                                            and not isinstance(requirements_of(r.contract), dict))


def test_an_engine_known_down_reads_unavailable_with_when_it_last_answered():
    registry, record = _record()
    row = workflow_availability(record, service_records={"services": {"comfyui": _COMFYUI}},
                                evidence={"comfyui": {"status": "down", "available": False, "observed_at_s": 900,
                                                      "last_success_at_s": 800}},
                                records=registry)
    assert row["available"] is False and row["reason"] and row["last_healthy_at"] == 800


def test_an_engine_that_answered_reads_available_and_an_unmeasured_one_is_not_called_down():
    registry, record = _record()
    services = {"services": {"comfyui": _COMFYUI}}
    up = workflow_availability(record, service_records=services, records=registry,
                               evidence={"comfyui": {"status": "ok", "available": True, "observed_at_s": 900,
                                                     "last_success_at_s": 900}})
    assert up["available"] is True and up["last_healthy_at"] == 900
    unmeasured = workflow_availability(record, service_records=services, records=registry, evidence={})
    assert unmeasured["available"] is True and unmeasured["last_healthy_at"] is None
