"""A fresh gateway contacts no engine: no record means not configured, never a guessed loopback port (S8).

Acceptance test of docs/design/CODE_TO_DATA.md S8. The record-level cases (absent, partial, disabled) are pinned in
tests/test_engine_address_from_record.py; this file drives the real app with the derived values of an empty registry.
"""

from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlsplit

import pytest

import proxy
from gateway import service_registry
from tests.support.adapter_differential_rig import GatewayRig

REQUESTS = [
    ("POST", "/v1/audio/speech", {"model": "tts-1", "input": "hi", "voice": "alloy"}),
    ("POST", "/v1/audio/generate", {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}),
    ("POST", "/v1/images/generations", {"prompt": "a cat", "model": "image-fast"}),
    ("POST", "/v1/embeddings", {"model": "nomic-embed-text-v1.5", "input": "hi"}),
]


@pytest.fixture
def socket_guard(monkeypatch):
    attempts: list[object] = []

    def record(name):
        def guard(*args, **kwargs):
            attempts.append((name, args[1:] if args else args))
            raise OSError(f"network use is forbidden here: {name}")
        return guard

    monkeypatch.setattr(socket.socket, "connect", record("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", record("connect_ex"))
    monkeypatch.setattr(socket.socket, "sendto", record("sendto"))
    monkeypatch.setattr(socket, "getaddrinfo", record("getaddrinfo"))
    return attempts


def test_fresh_gateway_has_zero_engine_egress(tmp_path, socket_guard):
    empty = proxy._derive_backend_constants({"backends": {}})
    with GatewayRig("fresh-no-egress", tmp_path) as rig:
        for key in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST"):
            assert empty[key] == "", key
        for key in ("TTS_ENABLED", "STT_ENABLED", "EMBEDDINGS_ENABLED", "COMFY_ENABLED"):
            rig.monkeypatch.setitem(proxy.runtime().derived, key, empty[key])
            assert empty[key] is False, key
        for key in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST"):
            rig.monkeypatch.setitem(proxy.runtime().derived, key, empty[key])
        statuses = []
        for method, path, body in REQUESTS:
            response = asyncio.run(rig.call(method, path, label=f"fresh {path}", json_body=body))
            statuses.append((path, response.status_code))
        assert all(status >= 400 for _, status in statuses), statuses
        # The one address a fresh gateway may dial is the release record of its LiteLLM substrate (data in
        # registry/services.yml). An engine is never dialed: every other destination would be a guess.
        released = service_registry.origin_of(
            service_registry.release_registry()["services"]["litellm"]["config"]["default_base_url"])
        sent = {urlsplit(f"//{call['to']}").netloc for call in rig.recorder.engine}
        assert sent <= {urlsplit(released).netloc}, sent
    attempted = {f"{a[1][0][0]}:{a[1][0][1]}" for a in socket_guard if a[0] == "connect"}
    assert attempted <= {urlsplit(released).netloc}, attempted
    assert not [a for a in socket_guard if a[0] != "connect"]


@pytest.mark.parametrize("record", [
    {}, {"enabled": True}, {"host": "10.0.0.9"}, {"port": 9999},
    {"host": "10.0.0.9", "port": 9999, "enabled": False},
], ids=["empty", "enabled-only", "no-port", "no-host", "disabled"])
def test_absent_partial_and_disabled_records_never_dial(record):
    derived = proxy._derive_backend_constants({"backends": {
        "tts": dict(record), "stt": dict(record), "embeddings": dict(record), "comfyui": dict(record)}})
    complete_enabled = bool(record.get("host") and record.get("port")) and record.get("enabled", True) is not False
    for key in ("TTS_ENABLED", "STT_ENABLED", "COMFY_ENABLED"):
        assert derived[key] is complete_enabled, key
    if not (record.get("host") and record.get("port")):
        assert {derived[k] for k in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST")} == {""}


def test_removed_on_reload_record_is_unconfigured():
    before = proxy._derive_backend_constants({"backends": {"tts": {"host": "10.0.0.9", "port": 9999}}})
    after = proxy._derive_backend_constants({"backends": {}})
    assert before["TTS_ENABLED"] is True and before["TTS_HOST"] == "http://10.0.0.9:9999"
    assert after["TTS_ENABLED"] is False and after["TTS_HOST"] == ""


def test_explicit_loopback_address_from_the_operator_stays_allowed():
    derived = proxy._derive_backend_constants({"backends": {"tts": {"host": "127.0.0.1", "port": 8880}}})
    assert derived["TTS_HOST"] == "http://127.0.0.1:8880" and derived["TTS_ENABLED"] is True
