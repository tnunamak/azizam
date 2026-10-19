"""Offline conformance checks for the Anthropic Messages surface.

Responses and stream events from the gateway routes are validated against the
JSON schemas in ``fixtures/anthropic-sdk-schemas.json``. That file is generated
from the official ``anthropic`` Python SDK 1.11.0 response models (see
``fixtures/generate_anthropic_sdk_schemas.py``). Stream events are also checked
for the ordering rules the SDK's accumulator relies on. The upstream chat
backend is replaced by a fake, so the test needs no network.
"""

from __future__ import annotations

from contextlib import nullcontext

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.access import authenticated, plan_for, access, auth_identity# noqa: E402

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "anthropic-sdk-schemas.json").read_text("utf-8")
)
assert FIXTURE["package"] == "anthropic" and FIXTURE["version"] == "1.11.0"
SCHEMAS = FIXTURE["schemas"]
MESSAGE_KEYS = {
    "id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage",
}


def validate(name: str, instance: object) -> None:
    schema = SCHEMAS[name]
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path)
    )
    assert not errors, f"{name}: {errors[0].message} at {list(errors[0].path)}"


def chat_completion(message: dict, finish_reason: str = "stop", usage=None) -> dict:
    return {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "model": "default",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
    }


def sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


class _FakeClient:
    async def aclose(self) -> None:
        return None


def chunk(delta: dict, finish: str | None = None, **extra) -> dict:
    out = {
        "id": "chatcmpl-abc",
        "object": "chat.completion.chunk",
        "model": "default",
        "choices": [{"index": 0, "delta": delta}],
        **extra,
    }
    if finish:
        out["choices"][0]["finish_reason"] = finish
    return out


def parse_events(text: str) -> list[dict]:
    events = []
    for frame in text.strip().split("\n\n"):
        lines = frame.split("\n")
        assert lines[0].startswith("event: "), frame
        assert lines[1].startswith("data: "), frame
        payload = json.loads(lines[1][len("data: "):])
        assert payload["type"] == lines[0][len("event: "):], frame
        events.append(payload)
    return events


def check_event_sequence(events: list[dict]) -> dict:
    """Apply the ordering rules the Anthropic SDK accumulator depends on.

    Returns the accumulated content blocks by index.
    """
    assert events[0]["type"] == "message_start"
    assert events[-1]["type"] == "message_stop"
    assert [e["type"] for e in events].count("message_delta") == 1
    assert events[-2]["type"] == "message_delta"
    blocks: dict[int, dict] = {}
    open_index = None
    expected_delta = {
        "text": "text_delta",
        "thinking": ("thinking_delta", "signature_delta"),
        "tool_use": "input_json_delta",
    }
    for event in events[1:-2]:
        kind = event["type"]
        if kind == "content_block_start":
            assert open_index is None, "block opened while another is open"
            assert event["index"] == len(blocks), "block indexes must be sequential"
            open_index = event["index"]
            blocks[open_index] = dict(event["content_block"])
        elif kind == "content_block_delta":
            assert event["index"] == open_index, "delta for a block that is not open"
            block = blocks[open_index]
            allowed = expected_delta[block["type"]]
            allowed = (allowed,) if isinstance(allowed, str) else allowed
            assert event["delta"]["type"] in allowed
            d = event["delta"]
            if d["type"] == "text_delta":
                block["text"] += d["text"]
            elif d["type"] == "thinking_delta":
                block["thinking"] += d["thinking"]
            elif d["type"] == "signature_delta":
                block["signature"] += d["signature"]
            else:
                block["_json"] = block.get("_json", "") + d["partial_json"]
        elif kind == "content_block_stop":
            assert event["index"] == open_index
            open_index = None
        else:
            raise AssertionError(f"unexpected event in block section: {kind}")
    assert open_index is None, "stream ended with a block still open"
    for block in blocks.values():
        if block["type"] == "tool_use":
            block["input"] = json.loads(block.pop("_json", "") or "{}")
    return blocks


def litellm_catalog(ids: list[str]):
    """Patch the model catalog source, as test_protocol_route_aliases does."""

    class _Response:
        status_code = 200
        headers: dict = {}

        async def aread(self):
            return json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": i, "object": "model", "owned_by": "litellm", "created": 1}
                        for i in ids
                    ],
                }
            ).encode()

        async def aclose(self):
            return None

    def fake_registry():
        return {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": "http://127.0.0.1:4011"},
                }
            }
        }

    async def fake_send(_req, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
        return _FakeClient(), _Response()

    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(proxy.ADMIN_STORE, "get_service_registry", fake_registry))
    stack.enter_context(patch.object(proxy, "send_llm_execution_request", fake_send))
    return stack


def keyed_auth(raw_key: str):
    """Enable auth with one valid runtime key, as test_runtime_route_policy does."""
    from contextlib import ExitStack

    key_hash = proxy.hash_api_key(raw_key)
    identity = auth_identity(
        name=raw_key,
        key_hash=key_hash,
        downstream={},
        access=access(plan_for("conformance", caps=('chat',))),
    )

    def lookup(candidate_hash: str, **_kwargs):
        return identity if candidate_hash == key_hash else None

    stack = ExitStack()
    stack.enter_context(
        patch.object(proxy, "runtime_identity_for_app_credential_hash", side_effect=lookup)
    )
    return stack


class AnthropicConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        self.auth = authenticated()
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.sent: list[dict] = []

    def post(self, upstream, body: dict, stream: bool = False):
        async def fake_forward(request, auth_context, cc_body, *, stream):
            self.sent.append(cc_body)
            if stream:
                return _FakeClient(), httpx.Response(200, content=upstream)
            return httpx.Response(
                200 if not isinstance(upstream, tuple) else upstream[0],
                json=upstream if not isinstance(upstream, tuple) else upstream[1],
                request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
            )

        with patch.object(proxy, "_forward_chat_completions", fake_forward):
            return self.client.post(
                "/anthropic/v1/messages",
                json={"model": "default", "max_tokens": 64, **body},
            )

    USER = {"messages": [{"role": "user", "content": "hi"}]}

    # -- non-streaming -------------------------------------------------

    def test_text_message_matches_message_schema(self) -> None:
        r = self.post(chat_completion({"role": "assistant", "content": "Hello"}), self.USER)
        self.assertEqual(r.status_code, 200, r.text)
        validate("Message", r.json())
        self.assertEqual(set(r.json()), MESSAGE_KEYS)
        self.assertEqual(r.json()["stop_reason"], "end_turn")
        self.assertEqual(r.json()["usage"], {"input_tokens": 11, "output_tokens": 7})
        self.assertTrue(r.headers["request-id"].startswith("req_"))

    def test_tool_use_message_matches_schema(self) -> None:
        upstream = chat_completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }
                ],
            },
            "tool_calls",
        )
        r = self.post(
            upstream,
            {
                **self.USER,
                "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
            },
        )
        validate("Message", r.json())
        self.assertEqual(r.json()["stop_reason"], "tool_use")
        self.assertEqual(
            r.json()["content"],
            [{"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}}],
        )

    def test_max_tokens_and_reasoning_are_not_leaked_as_text(self) -> None:
        upstream = chat_completion(
            {"role": "assistant", "content": "", "reasoning_content": "The user said hi"},
            "length",
        )
        r = self.post(upstream, self.USER)
        validate("Message", r.json())
        self.assertEqual(r.json()["stop_reason"], "max_tokens")
        self.assertEqual(r.json()["content"], [])

    def test_thinking_enabled_returns_thinking_block_first(self) -> None:
        upstream = chat_completion(
            {"role": "assistant", "content": "4", "reasoning_content": "2+2"}
        )
        r = self.post(
            upstream,
            {**self.USER, "max_tokens": 4096, "thinking": {"type": "enabled", "budget_tokens": 1024}},
        )
        self.assertEqual(r.status_code, 200, r.text)
        validate("Message", r.json())
        self.assertEqual(
            r.json()["content"],
            [
                {"type": "thinking", "thinking": "2+2", "signature": ""},
                {"type": "text", "text": "4"},
            ],
        )
        self.assertEqual(self.sent[0]["reasoning_effort"], "low")

    # -- streaming -----------------------------------------------------

    def test_text_stream_events_match_schema_and_ordering(self) -> None:
        stream = sse(
            [
                chunk({"role": "assistant", "content": ""}),
                chunk({"content": "Hel"}),
                chunk({"content": "lo"}),
                chunk({}, "stop"),
                chunk({}, usage={"prompt_tokens": 9, "completion_tokens": 3}, choices=[]),
            ]
        )
        r = self.post(stream, self.USER | {"stream": True}, stream=True)
        self.assertEqual(r.headers["content-type"], "text/event-stream; charset=utf-8")
        events = parse_events(r.text)
        for event in events:
            validate("RawMessageStreamEvent", event)
        blocks = check_event_sequence(events)
        self.assertEqual(blocks[0]["text"], "Hello")
        final = events[-2]
        self.assertEqual(final["delta"]["stop_reason"], "end_turn")
        self.assertEqual(final["usage"], {"output_tokens": 3, "input_tokens": 9})
        self.assertEqual(self.sent[0]["stream_options"], {"include_usage": True})

    def test_tool_stream_emits_tool_use_block_with_json_deltas(self) -> None:
        stream = sse(
            [
                chunk({"role": "assistant", "content": "Let me check."}),
                chunk({"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                       "function": {"name": "get_weather", "arguments": ""}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
                chunk({}, "tool_calls"),
            ]
        )
        r = self.post(
            stream,
            self.USER | {
                "stream": True,
                "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        events = parse_events(r.text)
        for event in events:
            validate("RawMessageStreamEvent", event)
        blocks = check_event_sequence(events)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(
            blocks[1],
            {"type": "tool_use", "id": "call_9", "name": "get_weather", "input": {"city": "Paris"}},
        )
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")

    def test_thinking_stream_has_thinking_then_text_blocks(self) -> None:
        stream = sse(
            [
                chunk({"reasoning_content": "step 1"}),
                chunk({"reasoning_content": " step 2"}),
                chunk({"content": "42"}),
                chunk({}, "stop"),
            ]
        )
        r = self.post(
            stream,
            self.USER | {
                "stream": True,
                "max_tokens": 4096,
                "thinking": {"type": "enabled", "budget_tokens": 2048},
            },
            stream=True,
        )
        events = parse_events(r.text)
        for event in events:
            validate("RawMessageStreamEvent", event)
        blocks = check_event_sequence(events)
        self.assertEqual(blocks[0], {"type": "thinking", "thinking": "step 1 step 2", "signature": ""})
        self.assertEqual(blocks[1]["text"], "42")

    def test_stream_does_not_leak_reasoning_when_thinking_is_off(self) -> None:
        stream = sse(
            [
                chunk({"reasoning_content": "private"}),
                chunk({"content": "answer"}),
                chunk({}, "stop"),
            ]
        )
        r = self.post(stream, self.USER | {"stream": True}, stream=True)
        blocks = check_event_sequence(parse_events(r.text))
        self.assertEqual([b["type"] for b in blocks.values()], ["text"])
        self.assertEqual(blocks[0]["text"], "answer")

    # -- other routes --------------------------------------------------

    def test_count_tokens_matches_schema(self) -> None:
        r = self.client.post(
            "/anthropic/v1/messages/count_tokens",
            json={"model": "default", "messages": [{"role": "user", "content": "hello world"}]},
        )
        self.assertEqual(r.status_code, 200)
        validate("MessageTokensCount", r.json())

    def test_models_list_and_retrieve_match_model_info_schema(self) -> None:
        for prefix in ("/anthropic/v1/models", "/v1/models"):
            with self.subTest(prefix=prefix), litellm_catalog(["claude-local", "other"]):
                headers = {"anthropic-version": "2023-06-01"}
                r = self.client.get(prefix, headers=headers)
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                self.assertIs(body["has_more"], False)
                self.assertTrue(body["data"])
                for model in body["data"]:
                    validate("ModelInfo", model)
                first = body["data"][0]["id"]
                r = self.client.get(f"{prefix}/{first}", headers=headers)
                self.assertEqual(r.status_code, 200, r.text)
                validate("ModelInfo", r.json())
                r = self.client.get(f"{prefix}/no-such-model", headers=headers)
                self.assertEqual(r.status_code, 404)
                validate("ErrorResponse", r.json())
                self.assertEqual(r.json()["error"]["type"], "not_found_error")

    @patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set()))
    def test_openai_clients_still_get_openai_model_list(self) -> None:
        with litellm_catalog(["claude-local"]):
            r = self.client.get("/v1/models")
            one = self.client.get("/v1/models/claude-local")
            missing = self.client.get("/v1/models/nope")
        self.assertEqual(r.status_code, 200, r.text)
        entry = next(m for m in r.json()["data"] if m["id"] == "claude-local")
        # OpenAI shape: `object: model` plus the model-metadata `type`
        # (docs/api/MODEL_METADATA.md), never Anthropic's `type: model`.
        self.assertEqual(entry["type"], "language")
        self.assertNotIn("display_name", entry)
        self.assertEqual(entry["object"], "model")
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(one.json()["id"], "claude-local")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "model_not_found")

    # -- errors --------------------------------------------------------

    def test_errors_match_error_schema_and_types(self) -> None:
        cases = [
            ({"messages": []}, 400, "invalid_request_error"),
            ({"messages": [{"role": "system", "content": "x"}]}, 400, "invalid_request_error"),
            ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}, 400, "invalid_request_error"),
            (
                {"messages": [{"role": "user", "content": "x"}], "thinking": {"type": "enabled"}},
                400,
                "invalid_request_error",
            ),
        ]
        for body, status, kind in cases:
            with self.subTest(body=body):
                r = self.post({}, body)
                self.assertEqual(r.status_code, status, r.text)
                validate("ErrorResponse", r.json())
                self.assertEqual(r.json()["error"]["type"], kind)
                self.assertIn("request-id", r.headers)

    def test_unknown_model_is_404_not_found_error(self) -> None:
        upstream = (
            400,
            {"error": {"message": "/chat/completions: Invalid model name passed in model=nope-x. Call `/v1/models`."}},
        )
        r = self.post(upstream, self.USER)
        self.assertEqual(r.status_code, 404)
        validate("ErrorResponse", r.json())
        # the engine's words decide only that the model is unknown; the name is the caller's own (decision D45)
        self.assertEqual(
            r.json()["error"], {"type": "not_found_error", "message": "model: default"}
        )
        self.assertNotIn("nope-x", r.text)

    def test_upstream_statuses_map_to_anthropic_error_types(self) -> None:
        for status, kind in ((429, "rate_limit_error"), (503, "api_error"), (401, "authentication_error")):
            with self.subTest(status=status):
                r = self.post((status, {"error": {"message": "boom"}}), self.USER)
                self.assertEqual(r.status_code, status)
                validate("ErrorResponse", r.json())
                self.assertEqual(r.json()["error"]["type"], kind)

    def test_missing_credentials_use_anthropic_error_shape(self) -> None:
        self.auth.stop()
        self.addCleanup(self.auth.start)
        with keyed_auth("sk-conformance"):
            for headers in ({}, {"x-api-key": "wrong"}, {"Authorization": "Bearer wrong"}):
                r = self.client.post(
                    "/anthropic/v1/messages",
                    headers=headers,
                    json={"model": "default", "max_tokens": 8, **self.USER},
                )
                self.assertEqual(r.status_code, 401, r.text)
                validate("ErrorResponse", r.json())
                self.assertEqual(r.json()["error"]["type"], "authentication_error")

    def test_x_api_key_and_bearer_both_authenticate(self) -> None:
        self.auth.stop()
        self.addCleanup(self.auth.start)
        up = chat_completion({"role": "assistant", "content": "ok"})

        async def fake_forward(request, auth_context, cc_body, *, stream):
            return httpx.Response(
                200, json=up, request=httpx.Request("POST", "http://gateway/x")
            )

        with keyed_auth("sk-conformance"), patch.object(
            proxy, "_forward_chat_completions", fake_forward
        ):
            for headers in ({"x-api-key": "sk-conformance"}, {"Authorization": "Bearer sk-conformance"}):
                r = self.client.post(
                    "/anthropic/v1/messages",
                    headers=headers,
                    json={"model": "default", "max_tokens": 8, **self.USER},
                )
                self.assertEqual(r.status_code, 200, r.text)
                validate("Message", r.json())


if __name__ == "__main__":
    unittest.main()
