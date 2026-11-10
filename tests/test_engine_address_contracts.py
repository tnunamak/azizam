"""Engine address contracts: missing, explicit and invalid records fail or route visibly."""
from __future__ import annotations

import asyncio

import pytest

import proxy
from tests.support.speech_roles import declared
import status_endpoint
from gateway import service_registry, stt_chunking


class _Client:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **_kwargs):
        self.calls.append(url)
        raise OSError("fixture engine is unreachable")


@pytest.fixture
def observed(monkeypatch):
    calls, samples, lifecycle = [], [], []

    async def tls():
        return False

    async def write(rows):
        samples.extend(rows)

    async def forbidden_lifecycle(*args, **kwargs):
        lifecycle.append((args, kwargs))
        raise AssertionError("an invalid address must not enter lifecycle probes")

    monkeypatch.setattr(status_endpoint, "_shared_probe_ssl_context", tls)
    monkeypatch.setattr(status_endpoint, "_write_backend_samples", write)
    monkeypatch.setattr(status_endpoint.engine_egress, "async_client", lambda **_kw: _Client(calls))
    for name in ("statuses", "status", "preflight"):
        monkeypatch.setattr(status_endpoint.lifecycle_drivers.SystemdDriver, name, forbidden_lifecycle)
    return calls, samples, lifecycle


def test_host_without_port_has_no_grid_or_history_request(observed):
    calls, samples, lifecycle = observed
    config = {"backends": {"stt": {"host": "203.0.113.9", "health_endpoint": "/health"}}}
    rows = asyncio.run(status_endpoint._backend_grid(config))
    asyncio.run(status_endpoint._sample_backend_health(config))
    assert calls == []
    assert samples == []
    assert lifecycle == []
    assert [(r["key"], r["status"], r["live_status"]) for r in rows] == [
        ("stt", "not_configured", "not_configured")]
    assert service_registry.NOT_CONFIGURED_REASON in rows[0]["last_error"]


@pytest.mark.parametrize("address,expected", [
    ("http://203.0.113.9:9999/prefix", "http://203.0.113.9:9999/prefix"),
    ("https://203.0.113.9:8443/prefix/", "https://203.0.113.9:8443/prefix"),
    ("http://203.0.113.9:9999/prefix/?tenant=a", "http://203.0.113.9:9999/prefix?tenant=a"),
])
def test_address_only_stored_row_reaches_stt_execution(address, expected):
    stored = service_registry.normalize_service_record("stt", {
        "kind": "engine", "source": "ui", "config": {"address": address},
    }, schema_version=1)
    runtime = service_registry.runtime_backends({"services": {"stt": stored}})["stt"]
    assert service_registry.engine_base_address(runtime) == expected
    execution = stt_chunking.SttExecutionConfig.from_service(
        service_id="stt", service_config=runtime, config_directory=proxy.CONFIG_PATH.parent)
    assert execution.host == expected


LOWER_ADDRESSES = [
    {"api_base": "http://203.0.113.10:9999/fallback"},
    {"default_base_url": "http://203.0.113.10:9999/fallback"},
    {"connection": {"address": "http://203.0.113.10:9999/fallback"}},
]


@pytest.mark.parametrize("lower", LOWER_ADDRESSES, ids=["api", "default", "connection"])
@pytest.mark.parametrize("invalid", [
    "not a url", "ftp://203.0.113.9/path", "http://203.0.113.9:bad",
], ids=["relative", "scheme", "port"])
def test_invalid_primary_is_a_visible_refusal_without_any_probe(lower, invalid, observed):
    calls, samples, lifecycle = observed
    record = {**lower, "base_url": invalid, "health_endpoint": "/health"}
    with pytest.raises(ValueError, match="base_url") as failure:
        service_registry.checked_engine_base_address(record)
    # The reader startup and dispatch use never raises and never falls through to the lower address.
    assert service_registry.engine_base_address(record) == ""
    assert invalid not in str(failure.value)
    with pytest.raises(stt_chunking.SttNotConfigured, match="base_url"):
        stt_chunking.SttExecutionConfig.from_service(
            service_id="stt", service_config=record, config_directory=proxy.CONFIG_PATH.parent)

    config = {"backends": {
        "tts": dict(record), "stt": dict(record), "comfyui": dict(record),
        "chat": {**record, "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}},
    }}
    rows = asyncio.run(status_endpoint._backend_grid(config))
    asyncio.run(status_endpoint._sample_backend_health(config))
    assert asyncio.run(status_endpoint._enriched_aux_health(config)) == {"voxtral": None, "parakeet": None}
    assert asyncio.run(status_endpoint._comfy_queue_and_stats(config)) == (None, None)
    assert asyncio.run(status_endpoint._llama_props_and_slots(config)) == (None, None)
    assert calls == [] and samples == [] and lifecycle == []
    assert {row["key"] for row in rows} == set(config["backends"])
    assert all(row["status"] == row["live_status"] == "not_configured" for row in rows)
    assert all("base_url" in row["last_error"] and invalid not in row["last_error"] for row in rows)


@pytest.mark.parametrize("higher,lower", [
    ("api_base", {"default_base_url": "http://203.0.113.10:9999/fallback"}),
    ("default_base_url", {"connection": {"address": "http://203.0.113.10:9999/fallback"}}),
])
def test_other_invalid_higher_priority_urls_do_not_fall_through(higher, lower):
    with pytest.raises(ValueError, match=higher):
        service_registry.checked_engine_base_address({higher: "not a url", **lower})
    assert service_registry.engine_base_address({higher: "not a url", **lower}) == ""


@pytest.mark.parametrize("record,expected", [
    ({"host": "203.0.113.9", "port": 9999, "base_url": "http://203.0.113.10:9998/base"},
     "http://203.0.113.9:9999"),
    ({"base_url": "http://203.0.113.9:9999/base", "api_base": "http://203.0.113.10:9998/api"},
     "http://203.0.113.9:9999/base"),
    ({"api_base": "http://203.0.113.9:9999/api", "default_base_url": "http://203.0.113.10:9998/default"},
     "http://203.0.113.9:9999/api"),
    ({"default_base_url": "http://203.0.113.9:9999/default", "connection": {"address": "http://203.0.113.10:9998/connection"}},
     "http://203.0.113.9:9999/default"),
])
def test_existing_valid_address_priority_is_preserved(record, expected):
    assert service_registry.engine_base_address(record) == expected


def test_disabled_invalid_record_remains_unprobed_and_unlisted(observed):
    calls, samples, lifecycle = observed
    config = {"backends": {"stt": {
        "enabled": False, "base_url": "not a url", "health_endpoint": "/health",
        "default_base_url": "http://203.0.113.10:9999/fallback",
    }}}
    assert asyncio.run(status_endpoint._backend_grid(config)) == []
    asyncio.run(status_endpoint._sample_backend_health(config))
    assert asyncio.run(status_endpoint._enriched_aux_health(config)) == {"voxtral": None, "parakeet": None}
    assert calls == [] and samples == [] and lifecycle == []


def test_a_malformed_engine_address_leaves_that_engine_off_and_the_gateway_up():
    # Startup derives every engine's address; one malformed record must not stop the rest. It reads as not configured,
    # never as the lower-priority address the record also names.
    effective = {"backends": declared({
        "stt": {"base_url": "not a url", "default_base_url": "http://203.0.113.10:9999/fallback"},
        "tts": {"base_url": "http://203.0.113.9:9999/v1"},
    })}
    derived = proxy._derive_backend_constants(effective)
    assert derived["STT_HOST"] == "" and derived["STT_ENABLED"] is False
    assert derived["TTS_HOST"] == "http://203.0.113.9:9999/v1" and derived["TTS_ENABLED"] is True
