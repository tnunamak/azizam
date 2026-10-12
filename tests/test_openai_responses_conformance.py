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
        self.auth = patch.object(proxy, "authenticate_request", return_value=proxy.AuthContext(
            identity=proxy.AuthIdentity(name="conformance", key_hash="sha256:conformance-responses")
        ))
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

    def test_codex_style_custom_patch_request_and_continuation_offline(self):
        patch_input = '*** Begin Patch\n*** Add File: hello.txt\n+"سلام 🌍"\n*** End Patch\n'
        # Hand-built from Codex ResponsesApiRequest and its apply_patch declaration.
        tool = {"type": "custom", "name": "apply_patch", "description": "Edit files with a freeform patch.",
                "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "*** Begin Patch"'}}
        body = {"input": [{"role": "developer", "content": [{"type": "input_text", "text": "Edit the project."}]},
                          {"role": "user", "content": [{"type": "input_text", "text": "Create hello.txt"}]}],
                "tools": [tool, {"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
                "tool_choice": "auto", "parallel_tool_calls": True, "store": False,
                "reasoning": {"effort": "medium", "summary": "auto"}, "include": ["reasoning.encrypted_content"]}
        arguments = json.dumps({"input": patch_input}, ensure_ascii=False)
        calls = [{"index": 0, "id": "call_patch", "type": "function",
                  "function": {"name": "apply_patch", "arguments": arguments}},
                 {"index": 1, "id": "call_exec", "type": "function",
                  "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'}}]
        for stream in (False, True):
            with self.subTest(stream=stream):
                up = sse([chunk({"tool_calls": calls}), chunk({}, "tool_calls")]) if stream else chat_completion(
                    {"content": None, "tool_calls": calls}, "tool_calls")
                r = self.create(up, {**body, "stream": stream})
                self.assertEqual(r.status_code, 200, r.text)
                if stream:
                    events = parse_events(r.text)
                    self.check_stream(events)
                    obj = events[-1]["response"]
                    done = next(e for e in events if e["type"] == "response.custom_tool_call_input.done")
                    self.assertEqual(done["input"], patch_input)
                    self.assertEqual(next(e for e in events if e["type"] == "response.custom_tool_call_input.delta")["delta"], patch_input)
                else:
                    obj = r.json()
                validate("Response", obj)
                self.assertEqual([i["type"] for i in obj["output"]], ["custom_tool_call", "function_call"])
                self.assertEqual(obj["output"][0]["input"], patch_input)
                continuation = self.create(chat_completion({"content": "Done."}), {**body, "input": [
                    *body["input"], *obj["output"],
                    {"type": "custom_tool_call_output", "call_id": "call_patch", "output": "Success."},
                    {"type": "function_call_output", "call_id": "call_exec", "output": "hello.txt"}]})
                self.assertEqual(continuation.status_code, 200, continuation.text)
                validate("Response", continuation.json())
                self.assertEqual(self.sent[-1]["messages"][-2],
                                 {"role": "tool", "tool_call_id": "call_patch", "content": "Success."})
                self.assertEqual(json.loads(self.sent[-1]["messages"][2]["tool_calls"][0]["function"]["arguments"])["input"], patch_input)

    def test_namespace_call_and_client_search_match_schemas_and_continue(self):
        import responses_translator as rt

        tool = {"type": "namespace", "name": "azizam", "description": "Local test tools.", "tools": [
            {"type": "function", "name": "echo", "parameters": {"type": "object"}}]}
        body = {"input": "Echo", "tools": [tool]}
        alias = rt.to_chat_completions(body)["tools"][0]["function"]["name"]
        search = {"type": "tool_search", "execution": "client", "description": "Discover tools.",
                  "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}
        cases = [(body, alias, '{}', "function_call"),
                 ({"input": "Find echo", "tools": [search]}, "tool_search", '{"query":"echo"}', "tool_search_call")]
        for req, name, arguments, expected_type in cases:
            for stream in (False, True):
                with self.subTest(type=expected_type, stream=stream):
                    calls = [{"index": 0, "id": "call_ns", "type": "function", "function": {"name": name, "arguments": arguments}}]
                    upstream = sse([chunk({"tool_calls": calls}), chunk({}, "tool_calls")]) if stream else chat_completion(
                        {"content": None, "tool_calls": calls}, "tool_calls")
                    response = self.create(upstream, {**req, "stream": stream})
                    self.assertEqual(response.status_code, 200, response.text)
                    if stream:
                        events = parse_events(response.text)
                        self.check_stream(events)
                        obj = events[-1]["response"]
                    else:
                        obj = response.json()
                    validate("Response", obj)
                    item = obj["output"][0]
                    self.assertEqual(item["type"], expected_type)
                    if expected_type == "function_call":
                        self.assertEqual((item["namespace"], item["name"]), ("azizam", "echo"))
                        result = {"type": "function_call_output", "call_id": "call_ns", "output": "echoed"}
                    else:
                        self.assertEqual(item["arguments"], {"query": "echo"})
                        result = {"type": "tool_search_output", "call_id": "call_ns", "execution": "client", "status": "completed", "tools": [tool]}
                    continuation = self.create(chat_completion({"content": "Done."}), {**req, "input": [item, result]})
                    self.assertEqual(continuation.status_code, 200, continuation.text)
                    validate("Response", continuation.json())
                    if expected_type == "tool_search_call":
                        self.assertEqual(len(self.sent[-1]["tools"]), 2)

    def test_malformed_upstream_custom_call_returns_502_without_executable_output(self):
        up = chat_completion({"tool_calls": [{"id": "call_patch", "type": "function",
                              "function": {"name": "apply_patch", "arguments": '{"input":false}'}}]}, "tool_calls")
        r = self.create(up, {"input": "Edit", "tools": [{"type": "custom", "name": "apply_patch"}]})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()["error"]["code"], "upstream_error")
        self.assertNotIn("output", r.json())

    def test_custom_previous_response_chain_replays_wrapped_input(self):
        patch_input = "*** Begin Patch\n*** Delete File: hello.txt\n*** End Patch\n"
        tools = [{"type": "custom", "name": "apply_patch"}]
        up = chat_completion({"tool_calls": [{"id": "call_patch", "type": "function", "function": {
            "name": "apply_patch", "arguments": json.dumps({"input": patch_input})}}]}, "tool_calls")
        made = self.create(up, {"input": "Delete hello.txt", "tools": tools, "store": True})
        self.assertEqual(made.status_code, 200, made.text)
        next_response = self.create(chat_completion({"content": "Done."}), {
            "previous_response_id": made.json()["id"], "tools": tools,
            "input": [{"type": "custom_tool_call_output", "call_id": "call_patch", "output": "Deleted."}]})
        self.assertEqual(next_response.status_code, 200, next_response.text)
        self.assertEqual([m["role"] for m in self.sent[-1]["messages"]], ["user", "assistant", "tool"])
        self.assertEqual(json.loads(self.sent[-1]["messages"][1]["tool_calls"][0]["function"]["arguments"])["input"], patch_input)

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
            r1 = self.client.post("/v1/responses", json={"model": "default", "input": "Remember mango", "store": True})
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
                json={"model": "default", "input": "hi", "store": True},
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
                "/v1/responses", json={"model": "default", "input": "hello there", "store": True}
            ).json()["id"]
            r = self.client.get(f"/v1/responses/{rid}/input_items")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["role"], "user")
        self.assertEqual(body["data"][0]["content"], [{"type": "input_text", "text": "hello there"}])
        self.assertIs(body["has_more"], False)

    # -- unsupported and errors ---------------------------------------

    def test_input_items_pagination_defaults_order_and_stable_cursors(self):
        inputs = [{"role": "user", "content": str(i)} for i in range(23)]
        made = self.create(chat_completion({"content": "ok"}), {"input": inputs, "store": True})
        path = f"/v1/responses/{made.json()['id']}/input_items"
        default = self.client.get(path).json()
        self.assertEqual(len(default["data"]), 20)
        self.assertEqual(default["data"][0]["content"][0]["text"], "22")
        self.assertTrue(default["has_more"])
        first = self.client.get(path, params={"limit": 1, "order": "asc"}).json()
        self.assertEqual(first["data"][0]["content"][0]["text"], "0")
        self.assertEqual(first["first_id"], first["last_id"])
        again = self.client.get(path, params={"limit": 1, "order": "asc"}).json()
        self.assertEqual(first, again)
        page = self.client.get(path, params={"limit": 2, "order": "asc", "after": first["last_id"]}).json()
        self.assertEqual([i["content"][0]["text"] for i in page["data"]], ["1", "2"])
        last = self.client.get(path, params={"after": default["last_id"]}).json()
        self.assertEqual([i["content"][0]["text"] for i in last["data"]], ["2", "1", "0"])
        self.assertFalse(last["has_more"])
        empty = self.client.get(path, params={"after": last["last_id"]}).json()
        self.assertEqual(empty["data"], [])
        self.assertIsNone(empty["first_id"])
        self.assertIsNone(empty["last_id"])
        self.assertFalse(empty["has_more"])
        for param, value in (("limit", "0"), ("limit", "101"), ("limit", "no"),
                             ("order", "sideways"), ("after", "item_missing")):
            with self.subTest(param=param, value=value):
                error = self.client.get(path, params={param: value})
                self.assertEqual(error.status_code, 400)
                self.assertEqual(error.json()["error"]["param"], param)

    def test_storage_failure_returns_error_before_success(self):
        with patch.object(proxy.responses_store, "save", side_effect=OSError("disk full")):
            buffered = self.create(chat_completion({"content": "ok"}), {"input": "hi", "store": True})
            self.assertEqual(buffered.status_code, 500)
            self.assertEqual(buffered.json()["error"]["code"], "internal_error")
            streamed = self.create(sse([chunk({"content": "ok"}), chunk({}, "stop")]),
                                   {"input": "hi", "stream": True, "store": True})
            events = parse_events(streamed.text)
            self.assertEqual(events[-1]["type"], "response.failed")
            self.assertEqual(events[-1]["response"]["status"], "failed")
            self.assertNotIn("response.completed", [e["type"] for e in events])
            self.assertIn("persist", events[-1]["response"]["error"]["message"].lower())
            for event in events:
                validate("ResponseStreamEvent", event)

    def test_stream_is_saved_before_terminal_success_is_yielded(self):
        # Check the generator directly so TestClient buffering cannot hide ordering.
        import asyncio
        from starlette.requests import Request

        async def exercise():
            raw = json.dumps({"model": "default", "input": "ordering-sentinel",
                              "stream": True, "store": True}).encode()

            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}

            request = Request({"type": "http", "method": "POST",
                               "path": "/v1/responses",
                               "headers": [], "query_string": b""}, receive)
            upstream = sse([chunk({"content": "ok"}), chunk({}, "stop")])

            async def forward(*args, **kwargs):
                return _FakeClient(), httpx.Response(200, content=upstream)

            real_save = proxy.responses_store.save
            events = []
            with patch.object(proxy, "_forward_chat_completions", forward), patch.object(
                proxy.responses_store, "save", wraps=real_save
            ) as save:
                response = await proxy.create_response(request)
                self.assertEqual(response.status_code, 200)
                async for frame in response.body_iterator:
                    for event in parse_events(frame.decode()):
                        events.append(event)
                        if event["type"] == "response.completed":
                            self.assertEqual(save.call_count, 1)
                            row = proxy.responses_store.get(event["response"]["id"])
                            self.assertIsNotNone(row)
                            self.assertEqual(row["request_json"]["input"], "ordering-sentinel")
                            self.assertEqual(row["response_json"]["output_text"], "ok")
                kinds = [event["type"] for event in events]
                self.assertEqual(kinds.count("response.completed"), 1, kinds)
                self.assertEqual(kinds[-1], "response.completed")
                self.assertFalse({"error", "response.failed", "response.incomplete"} & set(kinds))
                self.assertEqual(events[-1]["response"]["output_text"], "ok")
                self.assertEqual(save.call_count, 1)

        asyncio.run(exercise())

    def test_stream_read_error_uses_flat_error_and_increasing_sequence(self):
        for count in (1, 10005):
            with self.subTest(count=count):
                async def broken_stream(client, upstream):
                    for _ in range(count):
                        yield sse([chunk({"content": "x"})]).split(b"data: [DONE]")[0]
                    raise OSError("backend read failed")
                with patch.object(proxy, "iter_llm_response_bytes", broken_stream):
                    r = self.create(b"", {"input": "hi", "stream": True})
                events = parse_events(r.text)
                error = next(e for e in events if e["type"] == "error")
                self.assertEqual(error["message"], "backend read failed")
                self.assertIsNone(error["param"])
                self.assertNotIn("error", error)
                self.assertEqual([e["sequence_number"] for e in events], list(range(1, len(events) + 1)))
                self.assertEqual(events[-1]["type"], "response.failed")
                validate("ResponseStreamEvent", error)
                validate("ResponseStreamEvent", events[-1])

    def test_stream_upstream_error_and_premature_eof_match_failed_schema(self):
        for upstream in (sse([chunk({"content": "partial"}), {"error": {"message": "failed"}}]),
                         sse([chunk({"content": "partial"})])):
            with self.subTest(upstream=upstream):
                events = parse_events(self.create(upstream, {"input": "hi", "stream": True}).text)
                self.assertEqual(events[-1]["type"], "response.failed")
                self.assertEqual(events[-1]["response"]["output_text"], "partial")
                for event in events:
                    validate("ResponseStreamEvent", event)

    def test_refusals_match_buffered_and_streamed_schemas(self):
        buffered = self.create(chat_completion({"content": None, "refusal": "Cannot help."}), {"input": "hi"})
        validate("Response", buffered.json())
        self.assertEqual(buffered.json()["output"][0]["content"], [{"type": "refusal", "refusal": "Cannot help."}])
        events = parse_events(self.create(sse([chunk({"refusal": "Cannot "}), chunk({"refusal": "help."}, "stop")]),
                                         {"input": "hi", "stream": True}).text)
        self.check_stream(events)
        self.assertEqual(next(e for e in events if e["type"] == "response.refusal.done")["refusal"], "Cannot help.")

    def test_unsupported_requests_return_parameter_errors_without_forwarding(self):
        cases = [({"tool_choice": {"type": "allowed_tools", "mode": "required", "tools": []}}, "tool_choice"),
                 ({"tools": [{"type": "tool_search", "execution": "server"}],
                   "tool_choice": {"type": "tool_search"}}, "tool_choice"),
                 ({"input": [{"type": "additional_tools", "role": "developer", "tools": []}]}, "input[0].type"),
                 ({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "file_test"}]}]},
                  "input[0].content[0]"),
                 ({"input": [{"role": "user", "content": [{"type": "input_image", "file_id": "file_test"}]}]},
                  "input[0].content[0]")]
        for fields, param in cases:
            with self.subTest(fields=fields):
                before = len(self.sent)
                r = self.create({}, {"input": "hi", **fields})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["error"]["param"], param)
                self.assertEqual(len(self.sent), before)

    def test_unsupported_operations_are_explicit(self) -> None:
        r = self.client.post("/v1/responses/resp_x/cancel")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "unsupported_operation")
        self.assertEqual(r.json()["error"]["type"], "invalid_request_error")
        r = self.client.post("/v1/responses/input_tokens", json={"model": "default", "input": "hi"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "unsupported_operation")
        r = self.create({}, {"input": "hi", "tools": [{"type": "web_search"}],
                             "tool_choice": {"type": "web_search"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "hosted_tool_not_supported")
        r = self.create({}, {"input": "hi", "background": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "unsupported_operation")

    def test_optional_hosted_tools_are_omitted_with_header_buffered_and_streamed(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                upstream = sse([chunk({"content": "Hello"}), chunk({}, "stop")]) if stream else chat_completion(
                    {"role": "assistant", "content": "Hello"})
                r = self.create(upstream, {"input": "hi", "stream": stream,
                    "tools": [{"type": "web_search"}, {"type": "file_search", "vector_store_ids": []},
                              {"type": "web_search"}, {"type": "tool_search", "execution": "server"}]})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.headers["x-azizam-omitted-tools"], "web_search, file_search, tool_search")
                self.assertNotIn("tools", self.sent[-1])
                if stream:
                    self.check_stream(parse_events(r.text))
                else:
                    validate("Response", r.json())
                    self.assertNotIn("warnings", r.json())

    def test_forced_hosted_choices_fail_without_forwarding(self):
        for choice in ({"type": "web_search"}, {"type": "function", "name": "web_search"},
                       {"type": "function", "function": {"name": "web_search"}},
                       {"type": "custom", "name": "web_search"}, "required"):
            with self.subTest(choice=choice):
                before = len(self.sent)
                r = self.create({}, {"input": "hi", "tools": [{"type": "web_search"}], "tool_choice": choice})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["error"]["code"], "hosted_tool_not_supported")
                self.assertEqual(r.json()["error"]["param"], "tool_choice")
                self.assertEqual(len(self.sent), before)

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
