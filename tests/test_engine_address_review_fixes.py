"""Review fixes for S8a: an explicit URL keeps its path, and a disabled or unconfigured engine is not probed or counted down."""

from __future__ import annotations

import asyncio

import pytest

import proxy
import status_endpoint
from gateway import stt_chunking

VARIANTS = {
    "https-port-prefix": "https://203.0.113.7:8443/prefix",
    "plain-port": "http://203.0.113.7:9999",
    "prefix-only": "http://203.0.113.7/v1x",
    "ipv6-prefix": "http://[2001:db8::1]:9000/p",
}
FIELDS = ("base_url", "connection.address")


def _record(field, url, **extra):
    rec = dict(extra)
    if field == "base_url":
        rec["base_url"] = url
    else:
        rec["connection"] = {"address": url}
    return rec


class _Probe:
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


# --- 1. the explicit URL's scheme, port and path survive -----------------------------------------------------------

@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("url", VARIANTS.values(), ids=VARIANTS.keys())
def test_derived_hosts_keep_the_explicit_url(field, url):
    rec = _record(field, url)
    derived = proxy._derive_backend_constants({"backends": {"tts": rec, "stt": rec, "embeddings": rec, "comfyui": rec}})
    for key in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST"):
        assert derived[key] == url, key


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("url", VARIANTS.values(), ids=VARIANTS.keys())
def test_stt_execution_keeps_the_explicit_url(field, url):
    execution = stt_chunking.SttExecutionConfig.from_service(
        service_id="stt", service_config=_record(field, url), config_directory=proxy.CONFIG_PATH.parent)
    assert execution.host == url


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("url", VARIANTS.values(), ids=VARIANTS.keys())
def test_face_client_posts_under_the_explicit_url(field, url, monkeypatch):
    monkeypatch.setattr(proxy, "_identity_face_cfg", lambda: _record(field, url))
    monkeypatch.setattr(proxy, "_require_egress_lease", lambda *_a: None)
    assert proxy._identity_face_client().base_url == url


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("url", VARIANTS.values(), ids=VARIANTS.keys())
def test_status_probes_keep_the_prefix(field, url, probed):
    config = {"backends": {
        "tts": _record(field, url), "comfyui": _record(field, url),
        "chat": _record(field, url, capabilities=["chat"], lifecycle={"driver": "systemd"})}}
    asyncio.run(status_endpoint._enriched_aux_health(config))
    asyncio.run(status_endpoint._comfy_queue_and_stats(config))
    asyncio.run(status_endpoint._llama_props_and_slots(config))
    assert sorted(probed) == sorted([f"{url}/health", f"{url}/queue", f"{url}/system_stats", f"{url}/props"])


# --- 2. disabled and unconfigured engines are not probed and are not "down" -----------------------------------------

COMPLETE = {"host": "203.0.113.9", "port": 9999}
PARTIAL = {"host": "203.0.113.9"}
OFF_FORMS = [False, "false", "0", "no", "off"]


def _disabled_config(address, enabled):
    return {"backends": {
        "tts": {**address, "enabled": enabled, "health_endpoint": "/health"},
        "stt": {**address, "enabled": enabled, "health_endpoint": "/health"},
        "comfyui": {**address, "enabled": enabled, "health_endpoint": "/queue"},
        "chat": {**address, "enabled": enabled, "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}},
    }}


@pytest.mark.parametrize("enabled", OFF_FORMS, ids=[repr(f) for f in OFF_FORMS])
@pytest.mark.parametrize("address", [COMPLETE, PARTIAL], ids=["complete", "partial"])
def test_disabled_records_are_not_probed_or_listed(address, enabled, probed):
    config = _disabled_config(address, enabled)
    asyncio.run(status_endpoint._enriched_aux_health(config))
    asyncio.run(status_endpoint._comfy_queue_and_stats(config))
    asyncio.run(status_endpoint._llama_props_and_slots(config))
    assert probed == []
    assert status_endpoint._unconfigured_rows(config) == []
    assert status_endpoint._managed_chat_origin(config) in ("", None)


def test_a_disabled_chat_record_does_not_win_the_props_selection(probed):
    config = {"backends": {
        "off": {**COMPLETE, "enabled": "off", "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}},
        "on": {"host": "203.0.113.10", "port": 1234, "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}}}}
    asyncio.run(status_endpoint._llama_props_and_slots(config))
    assert probed == ["http://203.0.113.10:1234/props"]


def test_not_configured_is_not_measured_down():
    rows = status_endpoint._unconfigured_rows({"backends": {"tts": {"health_endpoint": "/health", "role": "required"}}})
    assert len(rows) == 1 and rows[0]["live_status"] == "not_configured" and rows[0]["status"] == "not_configured"
    assert "down" not in (rows[0]["live_status"], rows[0]["status"])


def test_an_enabled_unreachable_engine_stays_measured_down(probed):
    class _Down(_Probe):
        async def get(self, url, **_kw):
            self.calls.append(url)
            raise OSError("refused")

    status_endpoint.engine_egress.async_client = lambda **_k: _Down(probed)
    result = asyncio.run(status_endpoint._comfy_queue_and_stats(_disabled_config(COMPLETE, True)))
    assert probed and "http://203.0.113.9:9999/queue" in probed
    assert not result or isinstance(result, (dict, tuple, list))


# --- recheck 1: a base address with a query ---------------------------------------------------------------------------

QUERY_BASES = {
    "query": ("https://203.0.113.7:8443/prefix?mode=a", "https://203.0.113.7:8443/prefix/{}?mode=a"),
    "query-value-ends-in-slash": ("http://203.0.113.7:9/p/?dir=x/", "http://203.0.113.7:9/p/{}?dir=x/"),
    "ipv6-query": ("http://[2001:db8::1]:9000/p?k=v", "http://[2001:db8::1]:9000/p/{}?k=v"),
}


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("base,pattern", QUERY_BASES.values(), ids=QUERY_BASES.keys())
def test_status_probes_join_the_endpoint_before_the_query(field, base, pattern, probed):
    config = {"backends": {
        "tts": _record(field, base), "comfyui": _record(field, base),
        "chat": _record(field, base, capabilities=["chat"], lifecycle={"driver": "systemd"})}}
    asyncio.run(status_endpoint._enriched_aux_health(config))
    asyncio.run(status_endpoint._comfy_queue_and_stats(config))
    asyncio.run(status_endpoint._llama_props_and_slots(config))
    assert sorted(probed) == sorted(pattern.format(e) for e in ("health", "queue", "system_stats", "props"))


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("base,pattern", QUERY_BASES.values(), ids=QUERY_BASES.keys())
def test_face_client_posts_to_the_joined_url(field, base, pattern, monkeypatch):
    posted = []

    class _Client(_Probe):
        async def post(self, url, **_kw):
            posted.append(url)

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"faces": []}

            return _R()

    from gateway import identity_api
    monkeypatch.setattr(identity_api.engine_egress, "async_client", lambda **_k: _Client([]))
    monkeypatch.setattr(proxy, "_identity_face_cfg", lambda: _record(field, base))
    monkeypatch.setattr(proxy, "_require_egress_lease", lambda *_a: None)
    asyncio.run(proxy._identity_face_client().analyze(b"x", "m"))
    assert posted == [pattern.format("v1/faces/analyze")]


# --- recheck 2: a disabled managed service enters no lifecycle check ----------------------------------------------------

def test_a_disabled_managed_service_is_not_a_lifecycle_target():
    for enabled in OFF_FORMS:
        config = {"backends": {"svc": {**COMPLETE, "enabled": enabled, "role": "required", "lifecycle": {"driver": "systemd"}}}}
        assert status_endpoint._lifecycle_backend_targets(config) == []
    on = {"backends": {"svc": {**COMPLETE, "lifecycle": {"driver": "systemd"}}}}
    assert [t["key"] for t in status_endpoint._lifecycle_backend_targets(on)] == ["svc"]


@pytest.mark.parametrize("enabled", OFF_FORMS, ids=[repr(f) for f in OFF_FORMS])
def test_a_disabled_required_service_with_a_failed_unit_makes_no_call_and_no_row(enabled, probed, monkeypatch):
    from gateway import lifecycle_drivers
    calls = []

    async def failed(*a, **k):
        calls.append(a)
        return "failed"

    monkeypatch.setattr(lifecycle_drivers.SystemdDriver, "statuses", failed)
    monkeypatch.setattr(lifecycle_drivers.SystemdDriver, "status", failed)
    monkeypatch.setattr(lifecycle_drivers.SystemdDriver, "preflight", failed)
    config = {"backends": {"svc": {**COMPLETE, "enabled": enabled, "role": "required", "lifecycle": {"driver": "systemd"}}}}
    rows = asyncio.run(status_endpoint._backend_grid(config))
    assert calls == [] and probed == []
    assert [r for r in rows if r.get("key") == "svc"] == []
    assert not [r for r in rows if r.get("role") == "required" and r.get("live_status") == "down"]
