from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

import proxy
from tests.support.access import access_context  # noqa: E402


def _request(body, path="/v1/responses"):
    raw = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Future()

    return Request(
        {"type": "http", "method": "POST", "path": path,
         "headers": [], "query_string": b""},
        receive,
    )


@pytest.fixture
def isolated_handlers(monkeypatch):
    route_auth = SimpleNamespace(auth_context=access_context())
    monkeypatch.setattr(
        proxy, "authenticate_runtime_request",
        lambda *args, **kwargs: route_auth,
    )
    monkeypatch.setattr(
        proxy, "_model_dispatch_admission_response",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        proxy.prompt_memory, "decide_for_person",
        lambda *args, **kwargs: SimpleNamespace(
            person_id=None, keep=False, reason="no_person", created_at=0,
        ),
    )
    monkeypatch.setattr(
        proxy.receipt_store, "begin", lambda receipt, *a, **k: receipt.request_id,
    )
    monkeypatch.setattr(
        proxy.receipt_store, "finalize", lambda receipt, *a, **k: True,
    )
    return route_auth


# 4. Expected GREEN: 429 and 5xx may retry; other 4xx, especially 499, may not.
@pytest.mark.parametrize(
    "status,retry",
    [(200, False), (400, False), (401, False), (403, False),
     (404, False), (409, False), (422, False), (429, True),
     (499, False), (500, True), (503, True), (504, True)],
)
def test_chat_fallback_status_boundary(
    monkeypatch, isolated_handlers, status, retry,
):
    alias = SimpleNamespace(
        target="primary", engine="primary", skipped_engines=(),
        fallbacks=(("backup", "backup-service"),),
    )
    monkeypatch.setattr(proxy, "resolve_stable_alias", lambda *a: alias)
    monkeypatch.setattr(proxy, "config", {
        "model_aliases": {"chat": {"engines": {"backup": {}}}},
    })
    monkeypatch.setattr(
        proxy, "_provider_alias_model", lambda engine, service, spec: engine,
    )
    once = AsyncMock(side_effect=[
        proxy.Response(status_code=status), proxy.Response(status_code=200),
    ])
    monkeypatch.setattr(proxy, "_handle_openai_litellm_request_once", once)
    request = _request(
        {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]},
        "/v1/chat/completions",
    )
    response = asyncio.run(proxy.chat_completions(request))
    assert once.await_count == (2 if retry else 1)
    assert response.status_code == (200 if retry else status)
    assert request.state.provider_requested_model == "assistant"
    assert request.state.paid_provider_fallback is retry
    if retry:
        assert once.await_args_list[1].kwargs["model_override"] == "backup"
        assert response.headers["x-azizam-fallback"] == (
            f"primary->backup (http_{status})"
        )
    else:
        assert "x-azizam-fallback" not in response.headers


# 5. Expected GREEN: visible leaf cannot launder an invisible ancestor.
@pytest.mark.parametrize("hidden_index", [0, 1, 2])
def test_responses_rejects_any_hidden_ancestor_before_translation_or_dispatch(
    monkeypatch, isolated_handlers, hidden_index,
):
    rows = [
        {"id": "oldest", "visible": True},
        {"id": "middle", "visible": True},
        {"id": "leaf", "visible": True},
    ]
    rows[hidden_index]["visible"] = False
    monkeypatch.setattr(proxy.responses_store, "walk_chain", lambda rid: rows)
    monkeypatch.setattr(
        proxy, "_responses_row_visible", lambda row, auth: row["visible"],
    )
    translate = Mock(side_effect=AssertionError("hidden history translated"))
    forward = AsyncMock(side_effect=AssertionError("hidden history dispatched"))
    monkeypatch.setattr(
        proxy.responses_translator, "stored_chain_to_messages", translate,
    )
    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)
    response = asyncio.run(proxy.create_response(_request({
        "model": "fake", "input": "continue",
        "previous_response_id": "leaf", "store": False,
    })))
    assert response.status_code == 404
    error = json.loads(response.body)["error"]
    assert error["code"] == "previous_response_not_found"
    assert error["param"] == "previous_response_id"
    translate.assert_not_called()
    forward.assert_not_awaited()
