"""Round 10 of the secret handling: what the review of phase1-r9 (cfb76f44) found in messages the gateway composes (D45b).

The engine bound to an owner is trusted with its success payloads. A message the gateway writes itself is different: any engine
value inside it must be checked against something the gateway knows. The review found two that were not:

* the graph upload's own error text went into the 502 sentence of a caller (``_upload_video_workflow_assets``);
* the engine's ``node_type`` and ``node_id`` went into the error sentence of a video status when they looked like plain labels.

``MARKER`` stands for a credential the engine echoes; it is shaped like a plain label so the old label filter let it through.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest

import proxy
from gateway import client_errors
from gateway.engine_protocol import node_graph
from gateway.engine_protocol.types import JobRef, JobState
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture

MARKER = "r10-canary-key-7741"
NODE_MARKER = "r10 canary node 7741"
GRAPH = {"7": {"class_type": "KSampler"}, "9": {"class_type": "SaveVideo"}}


def _failed_job(node_type: object, node_id: object) -> dict:
    payload = {"node_type": node_type, "node_id": node_id, "exception_message": MARKER}
    return {"status": {"completed": True, "status_str": "error", "messages": [["execution_error", payload]]}}


# --- the graph upload -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status, body", [(500, f"upload refused for key {MARKER}"), (200, f'{{"error": "{MARKER}"}}')],
                         ids=["engine-refuses", "reply-without-a-name"])
def test_a_refused_graph_upload_never_puts_the_engines_text_in_the_502(fake_upstreams, monkeypatch, status, body):
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui", "name": "upload-echo",
        "provenance": {"source": "hand-written", "reference": "ComfyUI server route POST /upload/image (ComfyUI server.py)"},
        "exchanges": [{"id": "upload", "match": {"method": "POST", "path": "/upload/image"},
                       "response": {"status": status, "body": [{"text": body}]}}],
    }))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    _, response = asyncio.run(proxy._upload_video_workflow_assets({}, {"source_video": (b"x", "a.mp4", "video/mp4")}, proxy.NO_AUTH_CONTEXT))
    assert response is not None and response.status_code == 502
    assert MARKER not in response.body.decode()
    assert json.loads(response.body)["error"]["message"] == json.loads(client_errors.client_error(502, "asset_upload_failed", error_type="api_error").body)["error"]["message"]


# --- the node a failed job names --------------------------------------------------------------------------------------------


def test_a_node_is_named_by_the_gateways_own_label_for_it():
    job = _failed_job(NODE_MARKER, "7")
    assert node_graph.execution_error_message(job, "L", GRAPH) == "L KSampler node 7: execution failed"


@pytest.mark.parametrize("graph", [None, {}, GRAPH], ids=["no-graph", "empty-graph", "id-not-in-graph"])
def test_a_node_the_gateway_did_not_submit_is_not_named(graph):
    for node_type, node_id in ((NODE_MARKER, NODE_MARKER), (NODE_MARKER, "4040"), (None, NODE_MARKER), (NODE_MARKER, None)):
        message = node_graph.execution_error_message(_failed_job(node_type, node_id), "L", graph)
        assert message == "L execution error: execution failed"
        assert "7741" not in message


def test_the_engines_node_type_is_never_used_even_for_a_known_id():
    message = node_graph.execution_error_message(_failed_job(NODE_MARKER, "9"), "L", GRAPH)
    assert message == "L SaveVideo node 9: execution failed" and "7741" not in message


def test_a_history_state_without_the_graph_names_no_node():
    state, error = node_graph.history_state(_failed_job(NODE_MARKER, NODE_MARKER), "L")
    assert state == "failed" and "7741" not in error


def test_a_pushed_execution_error_carries_no_engine_text():
    event = node_graph.job_event_from(JobRef("x", "p1"), {"type": "execution_error", "data": {"exception_message": MARKER}})
    assert event.state is JobState.failed and MARKER not in str(event.detail)


@pytest.fixture
def video_state(monkeypatch):
    for name in ("_video_job_records", "_video_progress", "_video_workflows", "_video_chain_units"):
        monkeypatch.setattr(proxy, name, {})
    monkeypatch.setattr(proxy, "_video_records_restored", set())
    monkeypatch.setattr(proxy, "_video_progress_lock", asyncio.Lock())
    monkeypatch.setattr(proxy, "_persist_video_record", Mock())
    monkeypatch.setattr(proxy, "COMFY_ENABLED", True)
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *a: {})
    monkeypatch.setattr(proxy.prompt_memory, "delete_asset", Mock())


def _history_fake(fake_upstreams, monkeypatch, job):
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui", "name": "history-failed",
        "provenance": {"source": "hand-written", "reference": "ComfyUI server route GET /history/{prompt_id} (ComfyUI server.py)"},
        "exchanges": [{"id": "history", "repeat": True, "match": {"method": "GET", "path": "/history/p"}, "response": {"json": {"p": job}}}],
    }))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)


def _status(graph):
    proxy._video_job_records["p"] = {"owner_key_hash": None, "deleted_at": None, "created_at": 1, "completed_at": None,
                                     "model": "sora-2", "size": "960x512", "seconds": "5"}
    if graph is not None:
        proxy._video_workflows["p"] = graph
    result = asyncio.run(proxy.proxy_graph_video_status("video_p", proxy.NO_AUTH_CONTEXT))
    assert result.status_code == 200
    return json.loads(result.body)


def test_a_video_status_names_no_node_the_engine_invented(video_state, fake_upstreams, monkeypatch):
    """The review's repro: a label-shaped ``node_type`` in the history reached the error sentence of the status."""
    _history_fake(fake_upstreams, monkeypatch, _failed_job(NODE_MARKER, "7741"))
    body = _status(GRAPH)
    assert body["status"] == "failed"
    assert "7741" not in json.dumps(body)


def test_a_video_status_names_a_submitted_node_by_the_gateways_label(video_state, fake_upstreams, monkeypatch):
    _history_fake(fake_upstreams, monkeypatch, _failed_job(NODE_MARKER, "7"))
    body = _status(GRAPH)
    assert "KSampler node 7" in body["error"]["message"] and "7741" not in json.dumps(body)


def test_a_video_status_without_the_submitted_graph_names_no_node(video_state, fake_upstreams, monkeypatch):
    _history_fake(fake_upstreams, monkeypatch, _failed_job(NODE_MARKER, "7"))
    body = _status(None)
    assert "node" not in body["error"]["message"].replace("execution error", "") and "7741" not in json.dumps(body)
