"""Offline conformance checks for the OpenAI Responses API surface.

Response objects and every streamed event are validated against the
``Response`` and ``ResponseStreamEvent`` schemas of openai/openai-openapi at
the commit pinned in ``fixtures/openai-openapi-responses-api.json`` (see
``fixtures/generate_openai_responses_api_schemas.py``). The upstream chat
backend is replaced by a fake, so the test needs no network.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from test_anthropic_schema_conformance import (  # noqa: E402
    _FakeClient,
    chat_completion,
    chunk,
    keyed_auth,
    sse,
)

SPEC = json.loads(
    (ROOT / "tests" / "fixtures" / "openai-openapi-responses-api.json").read_text("utf-8")
)
SPEC_URI = f"urn:openai-openapi:{SPEC['commit']}"
REGISTRY = Registry().with_resource(
    SPEC_URI,
    Resource.from_contents(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": SPEC_URI,
            "components": SPEC["components"],
        }
    ),
)


def validate(name: str, payload: object) -> None:
    schema = {"$ref": f"{SPEC_URI}#/components/schemas/{name}"}
    errors = sorted(
        Draft202012Validator(schema, registry=REGISTRY).iter_errors(payload),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, "\n".join(
        f"{name} at {list(e.absolute_path)}: {e.message}" for e in errors[:5]
    )


def parse_events(text: str) -> list[dict]:
    events = []
    for frame in text.strip().split("\n\n"):
        lines = frame.split("\n")
        assert lines[0].startswith("event: "), frame
        payload = json.loads(lines[1][len("data: "):])
        assert payload["type"] == lines[0][len("event: "):], frame
        events.append(payload)
    return events


class ResponsesConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        proxy.responses_store.init_db()
        self.client = TestClient(proxy.app)
        self.auth = patch.object(proxy, "AUTH_ENABLED", False)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.sent: list[dict] = []

    def create(self, upstream, body: dict, *, status: int = 200):
        async def fake_forward(request, auth_context, cc_body, *, stream):
            self.sent.append(cc_body)
            if stream:
                return _FakeClient(), httpx.Response(status, content=upstream)
            return httpx.Response(
                status,
                json=upstream,
                request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
            )

        with patch.object(proxy, "_forward_chat_completions", fake_forward):
            return self.client.post(
                "/v1/responses", json={"model": "default", "store": False, **body}
            )

    # -- non-streaming -------------------------------------------------

    def test_pinned_schemas_are_valid(self) -> None:
        self.assertEqual(SPEC["commit"], "145fd3b2e30e3ac87a4ed915913f6fa1217974fa")
        for name in SPEC["roots"]:
            Draft202012Validator.check_schema(SPEC["components"]["schemas"][name])

    def test_text_response_matches_schema(self) -> None:
        r = self.create(
            chat_completion({"role": "assistant", "content": "Hello"}),
            {"input": "hi", "instructions": "be brief", "metadata": {"a": "b"}},
        )
        self.assertEqual(r.status_code, 200, r.text)
        validate("Response", r.json())
        body = r.json()
        self.assertEqual(body["output_text"], "Hello")
        self.assertEqual(body["usage"]["input_tokens"], 11)
        self.assertEqual(body["usage"]["output_tokens"], 7)
        self.assertEqual(self.sent[0]["messages"][0], {"role": "system", "content": "be brief"})

    def test_function_call_response_matches_schema(self) -> None:
        up = chat_completion(
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
            "tool_calls",
        )
        tools = [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]
        r = self.create(up, {"input": "weather?", "tools": tools})
        validate("Response", r.json())
        call = r.json()["output"][0]
        self.assertEqual((call["type"], call["call_id"], call["name"]), ("function_call", "call_1", "get_weather"))

    def test_reasoning_item_and_usage_details_match_schema(self) -> None:
        up = chat_completion(
            {"role": "assistant", "content": "4", "reasoning_content": "2+2"},
            usage={"prompt_tokens": 5, "completion_tokens": 9,
                   "completion_tokens_details": {"reasoning_tokens": 6}},
        )
        r = self.create(up, {"input": "2+2", "reasoning": {"effort": "low"}})
        validate("Response", r.json())
        types = [o["type"] for o in r.json()["output"]]
        self.assertEqual(types, ["reasoning", "message"])
        self.assertEqual(r.json()["usage"]["output_tokens_details"]["reasoning_tokens"], 6)
        self.assertEqual(self.sent[0]["reasoning_effort"], "low")

    def test_length_stop_is_incomplete(self) -> None:
        up = chat_completion({"role": "assistant", "content": "par"}, "length")
        r = self.create(up, {"input": "long", "max_output_tokens": 3})
        validate("Response", r.json())
        self.assertEqual(r.json()["status"], "incomplete")
        self.assertEqual(r.json()["incomplete_details"], {"reason": "max_output_tokens"})

    # -- streaming -----------------------------------------------------

    def check_stream(self, events: list[dict]) -> None:
        for event in events:
            validate("ResponseStreamEvent", event)
        numbers = [e["sequence_number"] for e in events]
        self.assertEqual(numbers, sorted(set(numbers)), "sequence_number must increase")
        self.assertEqual(events[0]["type"], "response.created")
        self.assertIn(events[-1]["type"], ("response.completed", "response.incomplete"))
        self.assertEqual(
            [e["type"] for e in events].count("response.created"), 1
        )
        added = {e["output_index"] for e in events if e["type"] == "response.output_item.added"}
        done = {e["output_index"] for e in events if e["type"] == "response.output_item.done"}
        self.assertEqual(added, done, "every added item must be closed")
        final = events[-1]["response"]
        self.assertEqual(
            [i["id"] for i in final["output"]],
            [e["item"]["id"] for e in sorted(
                (e for e in events if e["type"] == "response.output_item.done"),
                key=lambda e: e["output_index"])],
        )

    def test_text_stream_matches_schema_and_reports_usage(self) -> None:
        up = sse([
            chunk({"role": "assistant", "content": ""}),
            chunk({"content": "Hel"}),
            chunk({"content": "lo"}),
            chunk({}, "stop"),
            chunk({}, usage={"prompt_tokens": 9, "completion_tokens": 3}, choices=[]),
        ])
        r = self.create(up, {"input": "hi", "stream": True})
        events = parse_events(r.text)
        self.check_stream(events)
        deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
        self.assertEqual(deltas, "Hello")
        final = events[-1]["response"]
        self.assertEqual((final["usage"]["input_tokens"], final["usage"]["output_tokens"]), (9, 3))
        self.assertEqual(self.sent[0]["stream_options"], {"include_usage": True})

    def test_function_call_stream_matches_schema(self) -> None:
        up = sse([
            chunk({"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                   "function": {"name": "get_weather", "arguments": ""}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
            chunk({}, "tool_calls"),
        ])
        tools = [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]
        events = parse_events(self.create(up, {"input": "w?", "tools": tools, "stream": True}).text)
        self.check_stream(events)
        call = events[-1]["response"]["output"][0]
        self.assertEqual((call["name"], call["arguments"], call["call_id"]),
                         ("get_weather", '{"city":"Paris"}', "call_9"))

    def test_reasoning_stream_has_reasoning_item_before_message(self) -> None:
        up = sse([
            chunk({"reasoning_content": "step 1"}),
            chunk({"reasoning_content": " step 2"}),
            chunk({"content": "42"}),
            chunk({}, "stop"),
        ])
        events = parse_events(self.create(up, {"input": "q", "stream": True}).text)
        self.check_stream(events)
        output = events[-1]["response"]["output"]
        self.assertEqual([o["type"] for o in output], ["reasoning", "message"])
        self.assertEqual(output[0]["content"][0]["text"], "step 1 step 2")
        self.assertEqual(events[-1]["response"]["output_text"], "42")

    def test_length_stop_stream_ends_with_response_incomplete(self) -> None:
        up = sse([chunk({"content": "pa"}), chunk({}, "length")])
        events = parse_events(self.create(up, {"input": "q", "stream": True}).text)
        self.check_stream(events)
        self.assertEqual(events[-1]["type"], "response.incomplete")
        self.assertEqual(events[-1]["response"]["status"], "incomplete")

    # -- state ---------------------------------------------------------

    def test_previous_response_id_retrieve_and_delete(self) -> None:
        up = chat_completion({"role": "assistant", "content": "Mango"})
        with patch.object(proxy, "_forward_chat_completions", self._fwd(up)):
            r1 = self.client.post("/v1/responses", json={"model": "default", "input": "Remember mango"})
            self.assertEqual(r1.status_code, 200, r1.text)
            validate("Response", r1.json())
            r2 = self.client.post(
                "/v1/responses",
                json={"model": "default", "input": "what word?", "previous_response_id": r1.json()["id"]},
            )
            self.assertEqual(r2.status_code, 200, r2.text)
            roles = [m["role"] for m in self.sent[-1]["messages"]]
            self.assertEqual(roles, ["user", "assistant", "user"])
            got = self.client.get(f"/v1/responses/{r1.json()['id']}")
            self.assertEqual(got.json()["id"], r1.json()["id"])
            validate("Response", got.json())
            gone = self.client.delete(f"/v1/responses/{r1.json()['id']}")
            self.assertEqual(gone.json()["deleted"], True)
            self.assertEqual(self.client.get(f"/v1/responses/{r1.json()['id']}").status_code, 404)

    def _fwd(self, upstream):
        async def fake_forward(request, auth_context, cc_body, *, stream):
            self.sent.append(cc_body)
            return httpx.Response(
                200, json=upstream, request=httpx.Request("POST", "http://gateway/x")
            )

        return fake_forward

    def test_stored_responses_are_private_to_the_creating_key(self) -> None:
        up = chat_completion({"role": "assistant", "content": "x"})
        self.auth.stop()
        self.addCleanup(self.auth.start)
        with keyed_auth("sk-owner"), patch.object(proxy, "_forward_chat_completions", self._fwd(up)):
            made = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer sk-owner"},
                json={"model": "default", "input": "hi"},
            )
            self.assertEqual(made.status_code, 200, made.text)
            rid = made.json()["id"]
            self.assertEqual(
                self.client.get(f"/v1/responses/{rid}", headers={"Authorization": "Bearer sk-owner"}).status_code, 200
            )
        with keyed_auth("sk-other"):
            for method in ("get", "delete"):
                r = getattr(self.client, method)(
                    f"/v1/responses/{rid}", headers={"Authorization": "Bearer sk-other"}
                )
                self.assertEqual(r.status_code, 404, (method, r.text))
            r = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer sk-other"},
                json={"model": "default", "input": "x", "previous_response_id": rid},
            )
            self.assertEqual(r.status_code, 404, r.text)

    def test_input_items_lists_the_stored_input(self) -> None:
        up = chat_completion({"role": "assistant", "content": "x"})
        with patch.object(proxy, "_forward_chat_completions", self._fwd(up)):
            rid = self.client.post(
                "/v1/responses", json={"model": "default", "input": "hello there"}
            ).json()["id"]
            r = self.client.get(f"/v1/responses/{rid}/input_items")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["role"], "user")
        self.assertEqual(body["data"][0]["content"], [{"type": "input_text", "text": "hello there"}])
        self.assertIs(body["has_more"], False)

    # -- unsupported and errors ---------------------------------------

    def test_unsupported_operations_are_explicit(self) -> None:
        r = self.client.post("/v1/responses/resp_x/cancel")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "unsupported_operation")
        self.assertEqual(r.json()["error"]["type"], "invalid_request_error")
        r = self.client.post("/v1/responses/input_tokens", json={"model": "default", "input": "hi"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "unsupported_operation")
        r = self.create({}, {"input": "hi", "tools": [{"type": "web_search"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "hosted_tool_not_supported")
        r = self.create({}, {"input": "hi", "background": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "unsupported_operation")

    def test_unknown_model_is_a_404_model_not_found(self) -> None:
        up = {"error": {"message": "/chat/completions: Invalid model name passed in model=nope-x. Call `/v1/models`."}}
        for stream in (False, True):
            with self.subTest(stream=stream):
                content = json.dumps(up).encode()
                r = self.create(up if not stream else content, {"input": "hi", "stream": stream}, status=400)
                self.assertEqual(r.status_code, 404, r.text)
                error = r.json()["error"]
                self.assertEqual((error["type"], error["code"], error["param"]),
                                 ("invalid_request_error", "model_not_found", "model"))

    def test_bad_requests_use_openai_error_shape(self) -> None:
        r = self.client.post("/v1/responses", json={"model": "default"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "input")
        r = self.client.post("/v1/responses", content=b"nope")
        self.assertEqual(r.status_code, 400)
        self.assertIn("message", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
