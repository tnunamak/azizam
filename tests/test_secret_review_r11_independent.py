"""Round 11 of the secret handling: what the independent review of phase1-r10 (0b2428a1) found in what the gateway composes (D45b).

Two engine values reached a caller without the gateway checking them against something it knows:

* the ``Retry-After`` of an engine's error, copied by name after a scrub of known credential spellings only (an encoded
  credential, or any text, passed);
* the ``node`` of a video status, taken from the engine's ``executing``, ``progress`` and ``progress_state`` events.

``MARKER`` stands for a credential the engine echoes, in a shape no scrubber of known spellings recognizes.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

import proxy
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.test_secret_review_r10_composed_messages import GRAPH, _status, video_state  # noqa: F401 - fixture

MARKER = "r11-review-synthetic-credential-8392"


def _event(kind: str, node: str) -> dict:
    if kind == "progress_state":
        return {"type": kind, "data": {"prompt_id": "p", "nodes": {node: {"state": "running", "value": 1, "max": 10}}}}
    return {"type": kind, "data": {"prompt_id": "p", "node": node, "value": 1, "max": 10}}


def _history(fake_upstreams, monkeypatch, in_history):
    job = {"status": {"completed": False, "status_str": "running", "messages": []}}
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui", "name": "node-echo",
        "provenance": {"source": "hand-written", "reference": "independent D45b review"},
        "exchanges": [{"id": "history", "match": {"method": "GET", "path": "/history/p"},
                       "response": {"json": {"p": job} if in_history else {}}}],
    }))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)


# --- the node of a video status ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["executing", "progress", "progress_state"])
@pytest.mark.parametrize("in_history", [False, True], ids=["live", "in-history"])
def test_a_node_the_submitted_graph_does_not_have_is_not_shown(video_state, fake_upstreams, monkeypatch, kind, in_history):
    _history(fake_upstreams, monkeypatch, in_history)
    proxy._video_workflows["p"] = GRAPH
    asyncio.run(proxy._record_event(_event(kind, MARKER)))
    body = _status(GRAPH)
    assert MARKER not in GRAPH
    assert body["status"] == "in_progress"
    assert body.get("node") in (None, "")
    assert MARKER not in json.dumps(body)


@pytest.mark.parametrize("kind", ["executing", "progress", "progress_state"])
@pytest.mark.parametrize("in_history", [False, True], ids=["live", "in-history"])
def test_a_node_of_the_submitted_graph_is_shown(video_state, fake_upstreams, monkeypatch, kind, in_history):
    _history(fake_upstreams, monkeypatch, in_history)
    proxy._video_workflows["p"] = GRAPH
    asyncio.run(proxy._record_event(_event(kind, "7")))
    assert _status(GRAPH)["node"] == "7"


@pytest.mark.parametrize("kind", ["executing", "progress", "progress_state"])
def test_without_the_submitted_graph_no_node_is_shown(video_state, fake_upstreams, monkeypatch, kind):
    """After a restart the graph is not known: even an id that would be in it is the engine's word alone."""
    _history(fake_upstreams, monkeypatch, False)
    asyncio.run(proxy._record_event(_event(kind, "7")))
    asyncio.run(proxy._record_event(_event(kind, MARKER)))
    body = _status(None)
    assert body.get("node") in (None, "")
    assert MARKER not in json.dumps(body)


# --- Retry-After ------------------------------------------------------------------------------------------------------------


def _protocol_refusal(retry_after: str):
    from gateway.engine_protocol.errors import UpstreamError
    from gateway.engine_protocol.types import Credential
    from tests.test_engine_protocol_executor import conn, make, req

    def engine(request):
        return httpx.Response(429, json={"error": "refused"}, headers={"Retry-After": retry_after})

    ex, _ = make({"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}}},
                 engine, auth={"inference": {"kind": "bearer"}})
    with pytest.raises(UpstreamError) as caught:
        asyncio.run(ex.invoke(conn(credentials={"inference": Credential(MARKER)}), req("x")))
    return proxy._protocol_failure(caught.value)


@pytest.mark.parametrize("wire_value", [base64.b64encode(MARKER.encode()).decode(), "unexpected-marker-8392", MARKER],
                         ids=["encoded-credential", "other-text", "plain-credential"])
def test_a_protocol_error_retry_after_that_is_not_a_number_is_dropped(wire_value):
    response = _protocol_refusal(wire_value)
    assert response.status_code == 429
    assert "retry-after" not in response.headers
    assert MARKER not in json.dumps(dict(response.headers))


def test_a_protocol_error_retry_after_is_written_by_the_gateway_as_a_number():
    assert _protocol_refusal(" 7 ").headers["retry-after"] == "7"


@pytest.mark.parametrize("wire_value", [base64.b64encode(MARKER.encode()).decode(), "soon"])
def test_a_re_shaped_frontend_error_drops_a_retry_after_that_is_not_a_number(wire_value):
    upstream = SimpleNamespace(headers={"retry-after": wire_value})
    assert proxy._upstream_retry_after(upstream) == {}
    assert proxy._upstream_retry_after(SimpleNamespace(headers={"retry-after": "12"})) == {"Retry-After": "12"}


def test_a_dispatch_refusal_passed_across_a_hop_drops_a_retry_after_that_is_not_a_number():
    body = {"error": {"code": "resource_admission_denied", "decision": {"outcome": "denied"}}}
    for wire_value, expected in ((base64.b64encode(MARKER.encode()).decode(), None), ("30", "30")):
        upstream = httpx.Response(503, json=body, headers={"retry-after": wire_value})
        response = proxy._upstream_admission_response(upstream, "openai")
        assert response is not None
        assert response.headers.get("retry-after") == expected
