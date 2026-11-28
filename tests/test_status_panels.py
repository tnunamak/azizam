"""A status panel comes from a service record, not from an engine name (slice 1: the ``health_json`` kind).

The panels for the speech and transcription services are the role defaults in ``registry/engine_record_defaults.yml``.
A record of any name that declares ``status_panels`` gets its own panel with no code change. The HTML pins in
``tests/fixtures/status_panels_train54_html.json`` were captured from the renderer before the panel came from data.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import firehose_render
import status_endpoint
from tests.support.speech_roles import declared

ADDRESS = {"host": "203.0.113.7", "port": 9999}
VOX = {"status": "ok", "model": "voxtral-4b", "vram_gb": 9.5, "engine": "vllm"}
VOX_COLD = {"status": "ok", "model": "voxtral-4b", "vram_gb": 0.4}
PARA = {"status": "ok", "default_model": "parakeet-tdt-0.6b-v3", "speedup": "30x", "models": ["a", "b"], "device": "cuda"}
PINS = json.loads((Path(__file__).parent / "fixtures" / "status_panels_train54_html.json").read_text())
TODAY_PANELS = [
    {"kind": "health_json", "output_key": "voxtral", "title": "Voxtral (TTS)", "short_title": "Voxtral (TTS)"},
    {"kind": "health_json", "output_key": "parakeet", "title": "Parakeet (STT)", "short_title": "Parakeet (STT)"},
]


class _Client:
    def __init__(self, answers, calls):
        self.answers, self.calls = answers, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def get(self, url, **_kw):
        self.calls.append(url)
        body = self.answers.get(url)

        class _R:
            status_code = 200 if body is not None else 503

            @staticmethod
            def json():
                return body

        return _R()


@pytest.fixture
def answers(monkeypatch):
    table: dict[str, object] = {}
    calls: list[str] = []
    monkeypatch.setattr(status_endpoint.engine_egress, "async_client", lambda **_k: _Client(table, calls))
    table["calls"] = calls  # type: ignore[assignment]
    return table


def _probe(config, table):
    calls = table.pop("calls")
    result = asyncio.run(status_endpoint._enriched_aux_health(config))
    table["calls"] = calls
    return result, calls


def test_todays_records_publish_the_same_aux_health(answers):
    answers["http://203.0.113.7:9999/health"] = VOX
    config = {"backends": declared({"tts": dict(ADDRESS), "stt": {"host": "203.0.113.8", "port": 9998}})}
    answers["http://203.0.113.8:9998/health"] = PARA
    result, calls = _probe(config, answers)
    assert result == {"voxtral": VOX, "parakeet": PARA}
    assert sorted(calls) == ["http://203.0.113.7:9999/health", "http://203.0.113.8:9998/health"]


def test_no_record_leaves_both_keys_empty(answers):
    result, calls = _probe({"backends": {}}, answers)
    assert result == {"voxtral": None, "parakeet": None} and calls == []


def test_a_record_with_a_new_name_renders_its_own_panel(answers):
    panel = {"kind": "health_json", "output_key": "zeta_speech", "title": "Zeta speech", "short_title": "Zeta"}
    record = {**ADDRESS, "capabilities": ["zeta"], "health_endpoint": "/ready", "status_panels": [panel]}
    answers["http://203.0.113.7:9999/ready"] = {"status": "ok", "model": "zeta-1"}
    result, calls = _probe({"backends": {"zq-engine": record}}, answers)
    assert result["zeta_speech"] == {"status": "ok", "model": "zeta-1"}
    assert calls == ["http://203.0.113.7:9999/ready"]
    assert status_endpoint._aux_panels({"backends": {"zq-engine": record}})[-1] == panel


def test_a_record_panel_replaces_the_role_default(answers):
    own = {"kind": "health_json", "output_key": "own", "title": "Own", "short_title": "Own"}
    config = {"backends": declared({"tts": {**ADDRESS, "status_panels": [own]}})}
    answers["http://203.0.113.7:9999/health"] = VOX
    result, _ = _probe(config, answers)
    assert result["own"] == VOX and result["voxtral"] is None


def test_the_detail_cards_render_from_the_declared_panels():
    html = firehose_render._render_aux_health({"voxtral": VOX, "parakeet": PARA}, TODAY_PANELS)
    assert html == PINS["detail"]
    zeta = [{"kind": "health_json", "output_key": "zeta_speech", "title": "Zeta speech", "short_title": "Zeta"}]
    assert "Zeta speech" in firehose_render._render_aux_health({"zeta_speech": {"status": "ok"}}, zeta)


@pytest.mark.parametrize("name,aux", [
    ("cards_warm", {"voxtral": VOX, "parakeet": PARA}),
    ("cards_cold", {"voxtral": VOX_COLD, "parakeet": {"status": "down"}}),
    ("cards_none", {"voxtral": None, "parakeet": None}),
])
def test_the_summary_cards_render_from_the_declared_panels(name, aux):
    payload = {"aux_health": aux, "aux_panels": TODAY_PANELS}
    assert firehose_render._render_backend_readiness(payload) == PINS[name]


# --- process buckets: an engine service is a process that listens on a configured record's local port --------------

LIVE = json.loads((Path(__file__).parent / "fixtures" / "live_shaped_registry.json").read_text())["services"]


def _live_backends():
    """The live-shaped records as the status page reads them: the record's config and its other fields, flat."""
    return {sid: {**rec.get("config", {}), **{k: v for k, v in rec.items() if k != "config"}} for sid, rec in LIVE.items()}


def _top(*rows):
    head = "PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND\n"
    return head + "".join(f"{pid} u 20 0 1 1 1 S 5.0 1.0 0:01 {cmd}\n" for pid, cmd in rows)


def _ss(*listeners):
    return "".join(f'LISTEN 0 128 0.0.0.0:{port} 0.0.0.0:* users:(("{name}",pid={pid},fd=3))\n' for port, name, pid in listeners)


def _fake_run(monkeypatch, top, ss):
    def run(cmd, timeout=None):
        if cmd[0] == "top":
            return 0, top, ""
        if cmd[0] == "ss":
            return ss if isinstance(ss, tuple) else (0, ss, "")
        return 1, "", ""
    monkeypatch.setattr(status_endpoint, "_run", run)


def test_processes_listening_on_a_record_port_are_services(monkeypatch):
    ports = {sid: LIVE[sid]["config"]["port"] for sid in ("comfyui", "stt", "kobold")}
    _fake_run(
        monkeypatch,
        _top((100, "python run_a"), (101, "python run_b"), (102, "llama-server --port 5051"), (200, "brave --type=gpu")),
        _ss((ports["comfyui"], "python", 100), (ports["stt"], "python", 101), (ports["kobold"], "llama-server", 102),
            (3333, "brave", 200)))
    rows = status_endpoint._cpu_usage_quick({"backends": _live_backends()})["top_procs"]
    assert [r["pid"] for r in rows] == [100, 101, 102, 0]
    assert "1 procs" in rows[-1]["command"]


@pytest.mark.parametrize("ss", [(127, "", "ss: not found"), (1, "", "denied"), (0, "garbage\nLISTEN\n", ""), (0, "", "")])
def test_a_missing_or_broken_ss_leaves_no_engine_pids(monkeypatch, ss):
    _fake_run(monkeypatch, _top((100, "python run_a"), (200, "proxy.py")), ss)
    rows = status_endpoint._cpu_usage_quick({"backends": _live_backends()})["top_procs"]
    assert [r["pid"] for r in rows] == [200, 0]  # the named non-engine literals still count; nothing else does


def test_a_remote_record_address_adds_no_port(monkeypatch):
    backends = {"far": {"base_url": "https://203.0.113.7:7890", "capabilities": ["chat"]}}
    _fake_run(monkeypatch, _top((100, "python run_a")), _ss((7890, "python", 100)))
    assert status_endpoint._engine_listener_pids({"backends": backends}) == set()
