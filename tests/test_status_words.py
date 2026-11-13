"""The status page names no engine in code: keys, labels and series come from registry/status_words.yml, and the graph
engine's service is the record selected by capability (slice 2 of the engine-names work).

The shipped words equal the strings the code held before, so the /v1/status JSON and the page keep their bytes.
"""

from __future__ import annotations

import asyncio

import jsonschema
import pytest
import yaml

import firehose_render
import metrics_sampler
import status_endpoint
from gateway import status_words

SHIPPED_IMAGES = {
    "label": "ComfyUI", "short_label": "Comfy", "backend_names": ["comfyui", "comfy"], "queue_key": "comfy_queue",
    "custom_nodes_key": "comfy_custom_nodes", "view_key": "comfy_view", "ws_state_key": "comfy_ws_state",
    "version_key": "comfyui_version", "stats_error_key": "comfy_stats", "gpu_budget_key": "comfyui_gb",
    "gpu_budget_var": "GPU0_COMFYUI", "output_root_key": "comfy_output_root", "output_video_key": "comfy_output_video",
    "queue_running_series": "comfy_queue_running", "queue_pending_series": "comfy_queue_pending",
    "dir_env": "AZIZAM_COMFY_DIR", "output_dir_env": "AZIZAM_COMFY_OUTPUT_DIR",
    "custom_nodes_env": "AZIZAM_COMFY_CUSTOM_NODES_DIR", "baseline_field": "comfy_baseline_bytes",
    "audio_decode_class": "LTXVAudioVAEDecode", "two_stage_class": "LTXVCropGuides", "client_id_attr": "COMFY_CLIENT_ID",
}


def test_the_shipped_words_are_the_strings_the_code_held():
    assert status_words.words("images") == SHIPPED_IMAGES
    chat = status_words.words("chat")
    assert (chat["card_name"], chat["props_section"], chat["crash_event"], chat["crash_title"]) == (
        "llama-turbo", "llama_props_slots", "llama_crash", "llama crash")
    assert (chat["slots_active_series"], chat["slots_total_series"]) == ("llama_slots_active", "llama_slots_total")
    assert (chat["aux_path_key"], chat["aux_path_label"]) == ("mmproj_path", "mmproj path")
    speech = status_words.words("audio.speech")
    assert speech["unit_candidates"] == ["voxtral-int4-tts.service", "voxtral-tts.service", "tts.service"]
    assert [(row["contains"], row["label"]) for row in speech["exec_labels"]] == [
        ("vllm", "vLLM"), ("voxtral-int4", "voxtral-int4 (custom serve.py, flow-matching)"),
        ("voxtral", "Voxtral (vLLM-Omni)"), ("tts", "tts (custom)")]


def test_the_words_file_is_closed_a_missing_or_extra_word_is_an_error():
    schema = yaml.safe_load(status_words.SCHEMA_PATH.read_text())
    document = yaml.safe_load(status_words.BUNDLED_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(document)
    del document["status_words"]["images"]["queue_key"]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(document)
    document = yaml.safe_load(status_words.BUNDLED_PATH.read_text())
    document["status_words"]["chat"]["extra"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(document)


# --- the graph engine is the record selected by capability, whatever its id ---------------------------------------

def _graph_record_config(**records):
    return {"backends": records}


def test_the_graph_service_is_selected_by_capability_not_by_id():
    config = _graph_record_config(
        comfyui={"capabilities": ["chat"], "host": "203.0.113.9", "port": 1},
        render={"capabilities": ["images"], "host": "203.0.113.7", "port": 8188},
    )
    assert status_endpoint._graph_record(config) is config["backends"]["render"]
    assert status_endpoint._graph_record({"backends": {}}) == {}
    assert status_endpoint._graph_record(None) == {}


def test_the_queue_probe_reads_the_selected_records_address(monkeypatch):
    seen: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def get(self, url, **_kw):
            seen.append(url)

            class R:
                status_code = 200

                def json(self_inner):
                    return {"queue_running": [[0, "a"]], "queue_pending": []} if url.endswith("/queue") else {"system": {}}
            return R()

    monkeypatch.setattr(status_endpoint.engine_egress, "async_client", lambda **_kw: Client())
    config = _graph_record_config(render={"capabilities": ["images"], "host": "203.0.113.7", "port": 8188})
    queue, stats = asyncio.run(status_endpoint._graph_queue_and_stats(config))
    assert queue == {"running": 1, "pending": 0, "running_ids": ["a"], "pending_ids": []}
    assert stats == {"system": {}}
    assert any("203.0.113.7:8188/queue" in url for url in seen)


# --- the speech engine's unit and framework come from data -----------------------------------------------------------

def _fake_run(active_unit: str, exec_start: str):
    def run(cmd, timeout=None):
        if "is-active" in cmd:
            return (0, "active\n", "") if cmd[-1] == active_unit else (3, "inactive\n", "")
        return 0, "{ path=/x ; argv[]=" + exec_start + " ; ignore_errors=no }\n", ""
    return run


def test_the_speech_unit_and_framework_are_read_from_the_words(monkeypatch):
    monkeypatch.setattr(status_endpoint, "_run", _fake_run("voxtral-tts.service", "/opt/VLLM/bin/serve --port 1"))
    assert status_endpoint._resolve_tts_engine(None, {"model": "m"}) == {
        "unit": "voxtral-tts", "engine": "vLLM", "model": "m", "exec_start": "/opt/VLLM/bin/serve --port 1"}
    monkeypatch.setattr(status_endpoint, "_run", _fake_run("tts.service", "/srv/voxtral-int4/serve.py"))
    assert status_endpoint._resolve_tts_engine(None, None)["engine"] == "voxtral-int4 (custom serve.py, flow-matching)"
    monkeypatch.setattr(status_endpoint, "_run", _fake_run("tts.service", "/srv/other-tts/run"))
    assert status_endpoint._resolve_tts_engine(None, None)["engine"] == "tts (custom)"


def test_other_speech_words_change_what_is_tried_and_how_it_is_named(monkeypatch):
    words = {**status_endpoint._SPEECH_WORDS, "unit_candidates": ["speak.service"],
             "exec_labels": [{"contains": "zzz", "label": "Zed"}]}
    monkeypatch.setattr(status_endpoint, "_SPEECH_WORDS", words)
    monkeypatch.setattr(status_endpoint, "_run", _fake_run("speak.service", "/bin/ZZZ-server"))
    assert status_endpoint._resolve_tts_engine(None, None)["unit"] == "speak"
    assert status_endpoint._resolve_tts_engine(None, None)["engine"] == "Zed"
    monkeypatch.setattr(status_endpoint, "_run", _fake_run("voxtral-tts.service", "/bin/vllm"))
    assert status_endpoint._resolve_tts_engine(None, None)["unit"] is None


# --- the page reads the payload keys and labels from the words ------------------------------------------------------

QUEUE = {"running": 1, "pending": 2, "running_ids": ["a"], "pending_ids": []}
QUEUE_HTML = (
    '<div><span class="pill ok">1 running</span> <span class="pill">2 pending</span></div>'
    '<div class="mono small muted">running_ids=[\'a\'] pending_ids=[]</div>'
)


def test_the_queue_panel_keeps_its_bytes_and_follows_the_queue_key(monkeypatch):
    assert firehose_render._render_graph_queue({"comfy_queue": QUEUE}) == QUEUE_HTML
    assert firehose_render._render_graph_queue({"other_queue": QUEUE}) == '<div class="muted">unavailable</div>'
    monkeypatch.setattr(firehose_render, "_GRAPH_WORDS", {**firehose_render._GRAPH_WORDS, "queue_key": "other_queue"})
    assert firehose_render._render_graph_queue({"other_queue": QUEUE}) == QUEUE_HTML
    assert firehose_render._render_graph_queue({"comfy_queue": QUEUE}) == '<div class="muted">unavailable</div>'


def test_the_websocket_panel_keeps_its_bytes_and_names_the_engine_from_the_words(monkeypatch):
    payload = {"swap_state": {}, "comfy_ws_state": {"connected": True, "client_id": "abcdefghijklmnop"}}
    quiet = ('<div><span class="pill ok">all swap locks idle, ComfyUI ws connected</span>'
             ' <span class="muted small" title="ComfyUI client_id">(ws client abcdefghijkl…)</span></div>')
    assert firehose_render._render_swap_state(payload) == quiet
    down = firehose_render._render_swap_state({"swap_state": {}, "comfy_ws_state": {"connected": False, "client_id": "c"}})
    assert "ComfyUI websocket" in down and "ComfyUI client_id" in down
    monkeypatch.setattr(firehose_render, "_GRAPH_WORDS", {**firehose_render._GRAPH_WORDS, "label": "Graphy"})
    assert firehose_render._render_swap_state(payload) == quiet.replace("ComfyUI", "Graphy")


def test_the_view_panel_keeps_its_labels_and_reads_the_version_key(monkeypatch):
    view = {"comfyui_version": "1.2", "device": {"name": "d", "vram_free": 1}}
    html_out = firehose_render._render_graph_view(view)
    assert "ComfyUI version" in html_out and "1.2" in html_out and "device[0] vram_free (Comfy view)" in html_out
    monkeypatch.setattr(firehose_render, "_GRAPH_WORDS", {**firehose_render._GRAPH_WORDS, "version_key": "v", "short_label": "G"})
    assert "device[0] vram_free (G view)" in firehose_render._render_graph_view({"v": "9", "device": {}})


def test_a_backend_row_named_by_the_words_makes_the_graph_card(monkeypatch):
    card_function = firehose_render._render_backend_readiness
    payload = {"backends": [{"name": "comfy", "live_status": "up"}], "models": {}}
    assert "ComfyUI" in card_function(payload) and "responding to /system_stats" in card_function(payload)
    monkeypatch.setattr(firehose_render, "_GRAPH_WORDS",
                        {**firehose_render._GRAPH_WORDS, "backend_names": ["render"], "label": "Graphy"})
    assert "responding to /system_stats" not in card_function(payload)
    payload["backends"] = [{"name": "render", "live_status": "up"}]
    assert "Graphy" in card_function(payload)


# --- the sampler stores the series the words name --------------------------------------------------------------------

def test_the_sampler_stores_the_series_the_words_name(monkeypatch):
    recorded: list[str] = []

    class Writer:
        def record_sample(self, _now, name, _value, *_a, **_kw):
            recorded.append(name)

    async def queue(_cfg):
        return {"running": 1, "pending": 0}, None

    async def slots(_cfg):
        return None, [{"is_processing": True}]

    monkeypatch.setattr(status_endpoint, "_graph_queue_and_stats", queue)
    monkeypatch.setattr(status_endpoint, "_chat_props_and_slots", slots)
    asyncio.run(metrics_sampler._emit_fast_async(Writer(), 1))
    assert recorded == ["comfy_queue_running", "comfy_queue_pending", "llama_slots_active", "llama_slots_total"]
    recorded.clear()
    monkeypatch.setattr(status_endpoint, "_GRAPH_WORDS", {**status_endpoint._GRAPH_WORDS, "queue_running_series": "q_run"})
    asyncio.run(metrics_sampler._emit_fast_async(Writer(), 1))
    assert recorded[0] == "q_run"
