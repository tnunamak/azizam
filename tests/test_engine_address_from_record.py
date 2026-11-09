"""An engine's address is the one its service record names; a record that names none is not configured (S8).

Every reader below takes the address from ``service_registry.engine_origin`` and never guesses a loopback port.
"""

from __future__ import annotations

import asyncio

import pytest

import proxy
import status_endpoint
from gateway import lifecycle_drivers, service_registry, stt_chunking

NON_DEFAULT = {"host": "10.9.8.7", "port": 9999}
OTHER_HOST_NAMES = ("tts", "stt", "embeddings", "comfyui")


def _config(**records):
    return {"backends": {name: dict(rec) for name, rec in records.items()}}


class _Probe:
    """A stand-in HTTP client that records each URL the status page asks for."""

    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def get(self, url, **_kw):
        self.calls.append(url)

        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True}

        return _R()


@pytest.fixture
def probed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(status_endpoint.engine_egress, "async_client", lambda **_k: _Probe(calls))
    return calls


# --- 2. a record on a non-default port is used as written -------------------------------------------------------------

def test_derived_constants_use_the_record_as_written():
    derived = proxy._derive_backend_constants(_config(
        tts=NON_DEFAULT, stt=NON_DEFAULT, embeddings=NON_DEFAULT, comfyui=NON_DEFAULT))
    for key in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST"):
        assert derived[key] == "http://10.9.8.7:9999", key


def test_status_probe_dials_the_address_the_record_names(probed):
    config = _config(tts=NON_DEFAULT, stt=NON_DEFAULT, comfyui=NON_DEFAULT)
    asyncio.run(status_endpoint._enriched_aux_health(config))
    asyncio.run(status_endpoint._comfy_queue_and_stats(config))
    assert sorted(probed) == sorted([
        "http://10.9.8.7:9999/health", "http://10.9.8.7:9999/health",
        "http://10.9.8.7:9999/queue", "http://10.9.8.7:9999/system_stats"])


def test_status_probe_of_the_chat_engine_dials_its_record(probed):
    config = _config(chat={**NON_DEFAULT, "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}})
    asyncio.run(status_endpoint._llama_props_and_slots(config))
    assert probed == ["http://10.9.8.7:9999/props"]


def test_stt_chunking_uses_the_record_address():
    execution = stt_chunking.SttExecutionConfig.from_service(
        service_id="stt", service_config=dict(NON_DEFAULT), config_directory=proxy.CONFIG_PATH.parent)
    assert execution.host == "http://10.9.8.7:9999"


def test_lifecycle_driver_dials_the_record_address():
    driver = lifecycle_drivers.HttpLoadUnloadDriver
    assert driver._base_url(dict(NON_DEFAULT)) == "http://10.9.8.7:9999"
    request = lifecycle_drivers.engine_read_request(dict(NON_DEFAULT), "/health")
    assert request is not None and request[0] == "http://10.9.8.7:9999/health"


# --- 3. a record with no address is not configured ---------------------------------------------------------------------

def test_a_record_with_no_address_is_not_configured():
    derived = proxy._derive_backend_constants(_config(tts={"enabled": True}, stt={"timeout": 5}, comfyui={}))
    for host in ("TTS_HOST", "STT_HOST", "COMFY_HOST"):
        assert derived[host] == ""
    assert derived["TTS_ENABLED"] is False
    assert derived["STT_ENABLED"] is False
    assert derived["COMFY_ENABLED"] is False
    # A host without a port, or a port without a host, names no address either.
    half = proxy._derive_backend_constants(_config(tts={"host": "10.0.0.1"}, stt={"port": 5092}))
    assert half["TTS_HOST"] == half["STT_HOST"] == ""


def test_status_probe_sends_nothing_for_a_record_with_no_address(probed):
    config = _config(tts={}, stt={}, comfyui={}, chat={"capabilities": ["chat"], "lifecycle": {"driver": "systemd"}})
    assert asyncio.run(status_endpoint._enriched_aux_health(config)) == {"voxtral": None, "parakeet": None}
    assert asyncio.run(status_endpoint._comfy_queue_and_stats(config)) == (None, None)
    assert asyncio.run(status_endpoint._llama_props_and_slots(config)) == (None, None)
    assert probed == []


def test_status_shows_the_reason_for_a_record_with_no_address():
    rows = status_endpoint._unconfigured_rows(_config(tts={"health_endpoint": "/health", "name": "Speech"}))
    assert [(r["key"], r["status"], r["kind"]) for r in rows] == [("tts", "down", "not_configured")]
    assert service_registry.NOT_CONFIGURED_REASON in rows[0]["last_error"]
    assert status_endpoint._unconfigured_rows(_config(tts={"health_endpoint": "/health", **NON_DEFAULT})) == []


def test_stt_without_an_address_refuses_and_names_it():
    with pytest.raises(stt_chunking.SttConfigurationError, match="names no address"):
        stt_chunking.SttExecutionConfig.from_service(
            service_id="stt", service_config={"timeout": 5}, config_directory=proxy.CONFIG_PATH.parent)


def test_lifecycle_driver_dials_nothing_for_a_record_with_no_address():
    assert lifecycle_drivers.HttpLoadUnloadDriver._base_url({}) == ""
    assert lifecycle_drivers.engine_read_request({}, "/health") is None


def test_the_face_service_without_an_address_is_refused_with_a_reason():
    response = asyncio.run(_ensure_face({"faces": {"enabled": True}}))
    assert response is not None and response.status_code == 503
    assert service_registry.NOT_CONFIGURED_REASON.encode() in response.body


async def _ensure_face(backends):
    original = proxy._identity_face_cfg
    proxy._identity_face_cfg = lambda: backends["faces"]
    try:
        return await proxy._identity_ensure_face_service()
    finally:
        proxy._identity_face_cfg = original


# --- 4. a config that names host and port derives what the old code derived ---------------------------------------------

@pytest.mark.parametrize("host,port", [("10.1.0.3", 8880), ("127.0.0.1", 5092), ("nas.lan", 7890), ("::1", 5061)])
def test_host_and_port_derive_the_old_address(host, port):
    old = f"http://{host}:{port}" if ":" not in host else f"http://[{host}]:{port}"
    derived = proxy._derive_backend_constants(_config(
        tts={"host": host, "port": port}, stt={"host": host, "port": port},
        embeddings={"host": host, "port": port}, comfyui={"host": host, "port": port}))
    assert {derived[k] for k in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST")} == {old}
    assert proxy._backend_host_map(_config(chat={"host": host, "port": port}).get("backends"), lambda *_: True) == {"chat": old}


def test_release_records_give_the_same_origins_as_their_connection_address():
    release = service_registry.release_registry()
    records = release.get("services") or release.get("records") or {}
    checked = 0
    for record in (records.values() if isinstance(records, dict) else records):
        config = record.get("config") or {}
        base = config.get("default_base_url") or config.get("base_url")
        if base and not (config.get("host") and config.get("port")):
            checked += 1
            assert service_registry.engine_origin(config) == service_registry.origin_of(base)
    assert checked
