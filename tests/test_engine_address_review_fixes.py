"""Review fixes for S8a: an explicit URL keeps its path, and a disabled or unconfigured engine is not probed or counted down."""

from __future__ import annotations

import asyncio

import pytest

import proxy
from tests.support.speech_roles import declared
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
    derived = proxy._derive_backend_constants({"backends": declared({"tts": rec, "stt": rec, "embeddings": rec, "comfyui": rec})})
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
    config = {"backends": declared({
        "tts": _record(field, url), "comfyui": _record(field, url),
        "chat": _record(field, url, capabilities=["chat"], lifecycle={"driver": "systemd"})})}
    asyncio.run(status_endpoint._enriched_aux_health(config))
    asyncio.run(status_endpoint._graph_queue_and_stats(config))
    asyncio.run(status_endpoint._chat_props_and_slots(config))
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
    asyncio.run(status_endpoint._graph_queue_and_stats(config))
    asyncio.run(status_endpoint._chat_props_and_slots(config))
    assert probed == []
    assert status_endpoint._unconfigured_rows(config) == []
    assert status_endpoint._managed_chat_origin(config) in ("", None)


def test_a_disabled_chat_record_does_not_win_the_props_selection(probed):
    config = {"backends": {
        "off": {**COMPLETE, "enabled": "off", "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}},
        "on": {"host": "203.0.113.10", "port": 1234, "capabilities": ["chat"], "lifecycle": {"driver": "systemd"}}}}
    asyncio.run(status_endpoint._chat_props_and_slots(config))
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
    config = {"backends": {"comfyui": {**COMPLETE, "enabled": True, "health_endpoint": "/queue"}}}
    rows = asyncio.run(status_endpoint._backend_grid(config))
    assert probed == ["http://203.0.113.9:9999/queue"]
    assert [(row["key"], row["status"], row["live_status"]) for row in rows] == [
        ("comfyui", "down", "down")]


# --- recheck 1: a base address with a query ---------------------------------------------------------------------------

QUERY_BASES = {
    "query": ("https://203.0.113.7:8443/prefix?mode=a", "https://203.0.113.7:8443/prefix/{}?mode=a"),
    "query-value-ends-in-slash": ("http://203.0.113.7:9/p/?dir=x/", "http://203.0.113.7:9/p/{}?dir=x/"),
    "ipv6-query": ("http://[2001:db8::1]:9000/p?k=v", "http://[2001:db8::1]:9000/p/{}?k=v"),
}


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("base,pattern", QUERY_BASES.values(), ids=QUERY_BASES.keys())
def test_status_probes_join_the_endpoint_before_the_query(field, base, pattern, probed):
    config = {"backends": declared({
        "tts": _record(field, base), "comfyui": _record(field, base),
        "chat": _record(field, base, capabilities=["chat"], lifecycle={"driver": "systemd"})})}
    asyncio.run(status_endpoint._enriched_aux_health(config))
    asyncio.run(status_endpoint._graph_queue_and_stats(config))
    asyncio.run(status_endpoint._chat_props_and_slots(config))
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


# --- recheck 3: a disabled backend with an HTTP health check -----------------------------------------------------------

HEALTH_FORMS = {"relative": "/health", "absolute": "http://203.0.113.9:9999/ready"}


@pytest.mark.parametrize("managed", [False, True], ids=["http-only", "lifecycle-and-health"])
@pytest.mark.parametrize("health", HEALTH_FORMS.values(), ids=HEALTH_FORMS.keys())
@pytest.mark.parametrize("enabled", OFF_FORMS, ids=[repr(f) for f in OFF_FORMS])
def test_a_disabled_backend_with_a_health_check_is_not_probed_shown_or_sampled(enabled, health, managed, probed, monkeypatch):
    from gateway import lifecycle_drivers
    calls = []

    async def failed(*a, **k):
        calls.append(a)
        return "failed"

    for name in ("statuses", "status", "preflight"):
        monkeypatch.setattr(lifecycle_drivers.SystemdDriver, name, failed)
    written = []

    async def capture(rows):
        written.extend(rows)

    monkeypatch.setattr(status_endpoint, "_write_backend_samples", capture)
    record = {**COMPLETE, "enabled": enabled, "role": "required", "health_endpoint": health}
    if managed:
        record["lifecycle"] = {"driver": "systemd"}
    config = {"backends": {"svc": record}}
    assert status_endpoint._resolve_backend_targets(config) == []
    rows = asyncio.run(status_endpoint._backend_grid(config))
    asyncio.run(status_endpoint._sample_backend_health(config))
    assert probed == [] and calls == [] and written == []
    assert rows == [] or not [r for r in rows if r.get("key") == "svc"]
    assert not [r for r in rows if r.get("role") == "required" and r.get("live_status") == "down"]


def test_an_enabled_backend_with_a_health_check_is_still_a_target():
    config = {"backends": {"svc": {**COMPLETE, "role": "required", "health_endpoint": "/health"}}}
    assert [t["url"] for t in status_endpoint._resolve_backend_targets(config)] == ["http://203.0.113.9:9999/health"]


# --- recheck 4: the endpoint's own query ---------------------------------------------------------------------------------

@pytest.mark.parametrize("base,endpoint,expected", [
    ("https://h/p?tenant=a", "/health?ready=1", "https://h/p/health?tenant=a&ready=1"),
    ("https://h/p?tenant=a", "/health#frag", "https://h/p/health?tenant=a"),
    ("https://h/p?tenant=a", "/health?ready=1#frag", "https://h/p/health?tenant=a&ready=1"),
    ("https://h/p?tenant=a&x=1", "/health?x=2", "https://h/p/health?tenant=a&x=2"),
    ("https://h/p?dir=x/", "health?k=v/", "https://h/p/health?dir=x/&k=v/"),
    ("http://[2001:db8::1]:9/p/?a=1", "/q?b=2", "http://[2001:db8::1]:9/p/q?a=1&b=2"),
    ("https://h/p", "/health", "https://h/p/health"),
], ids=["both", "fragment", "both-and-fragment", "collision-endpoint-wins", "no-reencode", "ipv6", "plain"])
def test_join_base_path_merges_the_two_queries(base, endpoint, expected):
    from gateway import service_registry
    assert service_registry.join_base_path(base, endpoint) == expected


@pytest.mark.parametrize("base,endpoint,name", [
    ("https://h/p?tenant=old", "/health?%74enant=new", "tenant"),
    ("https://h/p?a+b=old", "/health?a%20b=new", "a b"),
], ids=["percent-encoded-name", "plus-versus-percent-space"])
def test_join_base_path_endpoint_overrides_decoded_parameter_name(base, endpoint, name):
    from urllib.parse import parse_qsl, urlsplit
    from gateway import service_registry
    joined = service_registry.join_base_path(base, endpoint)
    assert [v for k, v in parse_qsl(urlsplit(joined).query) if k == name] == ["new"]


# --- S8a address review: the remaining sites read the record's one address ---------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from gateway import engine_protocol, model_presentation, service_registry  # noqa: E402
from gateway.engine_protocol.types import CanonicalRequest, Connection  # noqa: E402

QUERY_BASE = "https://example.com/p?tenant=a"


def _query_conn():
    """A connection whose address holds a query. ``Connection`` refuses one today; the sites must still join correctly if one arrives."""
    return SimpleNamespace(address=QUERY_BASE, credentials={}, headers={}, timeouts=Connection(address="https://example.com").timeouts)


def test_capability_host_keeps_scheme(monkeypatch):
    rec = {"host": "example.com", "port": 8443, "scheme": "https"}
    monkeypatch.setattr(proxy, "service_for_capability", lambda _cap: ("tts", rec))
    assert proxy.host_for_capability("audio.speech") == "https://example.com:8443"
    monkeypatch.setattr(proxy, "service_for_capability", lambda _cap: ("tts", {"base_url": "https://example.com/p"}))
    assert proxy.host_for_capability("audio.speech") == "https://example.com/p"
    monkeypatch.setattr(proxy, "service_for_capability", lambda _cap: ("tts", {}))
    assert proxy.host_for_capability("audio.speech") is None


def test_alias_engine_without_host_never_builds_none_url(monkeypatch):
    backends = {"alias-eng": {"base_url": "https://example.com/p"}, "bare": {}}
    monkeypatch.setattr(proxy, "config", {**proxy.config, "backends": backends})
    monkeypatch.setattr(proxy, "_speech_engines", lambda: {"e": {"service": "alias-eng"}, "f": {"service": "bare"}})
    monkeypatch.setattr(proxy, "_alias_engine_available_for", lambda _kind: (lambda _id, _spec: True))
    assert proxy._speech_engine_endpoint("e") == ("alias-eng", "https://example.com/p")
    assert proxy._speech_engine_endpoint("f") is None
    assert proxy._speech_service_base("alias-eng") == "https://example.com/p"
    assert proxy._speech_service_base("bare") == ""
    assert proxy._speech_service_base("missing") == ""
    assert "None" not in (proxy._speech_service_base("alias-eng") or "")


def test_backend_address_reads_the_record_address():
    assert proxy._backend_address({"host": "example.com", "port": 8443, "scheme": "https"}) == "https://example.com:8443"
    assert proxy._backend_address({"base_url": "https://example.com/p"}) == "https://example.com/p"
    assert proxy._backend_address({}) is None


def test_probe_join_keeps_base_query():
    assert proxy._probe_join(QUERY_BASE, "/health") == "https://example.com/p/health?tenant=a"
    assert proxy._probe_join(QUERY_BASE, "https://example.com/h") == "https://example.com/h"


def test_inline_tts_url_keeps_base_query():
    from gateway.engine_protocol import inline_tts
    seen = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, url, **_kw):
            seen.append(url)
            raise inline_tts.httpx.ConnectError("stop")

    conn = _query_conn()
    with patch.object(inline_tts.engine_egress, "async_client", lambda **_k: _Client()):
        for op in ("speech.synthesize", "speech.voice_check"):
            with pytest.raises(Exception):
                asyncio.run(inline_tts.InlineTtsAdapter().invoke(conn, op, CanonicalRequest(op=op, body={})))
    assert seen == ["https://example.com/p/v1/audio/speech?tenant=a", "https://example.com/p/v1/audio/voice-check?tenant=a"]


def test_chunked_stt_url_keeps_base_query():
    from gateway.engine_protocol import chunked_stt
    seen = []

    async def post_once(url, timeout, request):
        seen.append(url)
        return None

    route = next(iter(chunked_stt.ROUTES))
    with patch.object(chunked_stt.ChunkedSttAdapter, "_post_once", staticmethod(post_once)):
        asyncio.run(chunked_stt.ChunkedSttAdapter().invoke(
            _query_conn(), route, chunked_stt.SttUpload.__new__(chunked_stt.SttUpload)))
    assert seen == [service_registry.join_base_path(QUERY_BASE, chunked_stt.ROUTES[route])]
    assert seen[0].endswith("?tenant=a") and "?tenant=a/" not in seen[0]


def test_node_graph_url_keeps_base_query():
    from gateway.engine_protocol import node_graph
    session = node_graph.GraphSession(_query_conn(), None)
    assert session._url("/view") == "https://example.com/p/view?tenant=a"


def test_executor_url_keeps_base_query():
    from gateway.engine_protocol import description, executor
    from gateway.engine_protocol.assets import MemoryAssetStore
    doc = {"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": {
        "ping": {"class": "inventory", "request": {"method": "GET", "path": "/ping", "query": [{"name": "q", "from": "/params/q"}]}, "response": {"framing": "json"}}}}
    desc = description.parse_description(doc, source="t.yml")
    ex = executor.HttpV1Executor(desc, assets=MemoryAssetStore())
    spec = desc.operations["ping"].request
    source = {"body": {}, "params": {"q": "x y"}, "files": {}}
    wire = asyncio.run(ex._build(_query_conn(), spec, desc.operations["ping"], source))
    assert wire.url == "https://example.com/p/ping?tenant=a&q=x%20y"


def test_startup_banner_prints_no_userinfo_or_query(monkeypatch, capsys):
    secret_url = "https://u:pw@example.com:9/p?token=abc"
    cfg = {"backends": declared({k: {"base_url": secret_url} for k in ("tts", "stt", "comfyui")})}
    derived = proxy._derive_backend_constants(cfg)
    lines = proxy._backend_banner_lines(derived)
    text = "\n".join(lines)
    assert "pw" not in text and "token=abc" not in text and "u:" not in text
    assert "example.com" in text


@pytest.mark.parametrize("record", [
    {"host": "example.com", "port": 1, "base_url": "https://203.0.113.5:2/p"},
    {"host": "example.com", "connection": {"address": "https://203.0.113.9:9"}},
    {"base_url": "https://example.com/p"},
    {"host": "example.com", "port": 8443, "scheme": "https"},
    {},
])
def test_health_base_agrees_with_engine_base_address(record):
    expected = service_registry.engine_base_address(record) or None
    assert model_presentation.health_base({"config": record}) == expected
    assert model_presentation.health_base(record) == expected


@pytest.mark.parametrize("record", [
    {"host": "http://evil", "port": 80},
    {"host": "a b", "port": 80},
    {"host": "example.com", "port": False},
    {"host": "example.com", "port": 0},
    {"host": "example.com", "port": 70000},
])
def test_host_port_origin_refuses_malformed_host_or_port(record):
    with pytest.raises(service_registry.EngineAddressError) as caught:
        service_registry.checked_engine_base_address(record)
    assert "evil" not in str(caught.value) and "a b" not in str(caught.value)
    assert service_registry.engine_base_address(record) == ""
