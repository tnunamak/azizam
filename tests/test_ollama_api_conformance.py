"""Offline conformance checks for the Ollama-compatible surface.

Responses are validated against ``fixtures/ollama-api-schemas.json``, written
from ``docs/api.md`` of ollama/ollama at the commit pinned in that file (the
document itself is vendored as ``fixtures/ollama-api.md``). The upstream chat
backend is replaced by a fake, so the test needs no network.
"""

from __future__ import annotations

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
from tests.support.access import authenticated  # noqa: E402
from test_anthropic_schema_conformance import (  # noqa: E402
    _FakeClient,
    chat_completion,
    chunk,
    keyed_auth,
    litellm_catalog,
    sse,
)

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "ollama-api-schemas.json").read_text("utf-8")
)
SCHEMAS = FIXTURE["schemas"]
API_DOC = (ROOT / "tests" / "fixtures" / "ollama-api.md").read_text("utf-8")


def validate(name: str, instance: object) -> None:
    errors = sorted(
        Draft202012Validator(SCHEMAS[name]).iter_errors(instance),
        key=lambda e: list(e.path),
    )
    assert not errors, f"{name}: {errors[0].message} at {list(errors[0].path)}: {instance}"


def ndjson(response) -> list[dict]:
    lines = [line for line in response.text.split("\n") if line]
    return [json.loads(line) for line in lines]


class OllamaConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        self.auth = authenticated()
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.sent: list[dict] = []

    def test_fixture_is_pinned_to_the_vendored_doc(self) -> None:
        self.assertEqual(len(FIXTURE["commit"]), 40)
        for heading in ("## Generate a chat completion", "## List Local Models", "## Show Model Information", "## List Running Models", "## Generate Embedding"):
            self.assertIn(heading, API_DOC)
        self.assertIn('"done_reason": "load"', API_DOC)
        self.assertIn("`think`", API_DOC)

    def post(self, path: str, body: dict, upstream, *, stream: bool | None = None):
        async def fake_forward(request, auth_context, cc_body, *, stream):
            self.sent.append(cc_body)
            if stream:
                return _FakeClient(), httpx.Response(200, content=upstream)
            if isinstance(upstream, tuple):
                return httpx.Response(
                    upstream[0],
                    json=upstream[1],
                    request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
                )
            return httpx.Response(
                200,
                json=upstream,
                request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
            )

        payload = dict(body)
        if stream is not None:
            payload["stream"] = stream
        with patch.object(proxy, "_forward_chat_completions", fake_forward):
            return self.client.post(path, json=payload)

    CHAT = {"model": "default", "messages": [{"role": "user", "content": "hi"}]}
    GEN = {"model": "default", "prompt": "hi"}

    # -- chat ----------------------------------------------------------

    def test_chat_non_stream_matches_final_schema(self) -> None:
        up = chat_completion({"role": "assistant", "content": "Hello"})
        up["timings"] = {"prompt_ms": 5.0, "predicted_ms": 9.0}
        r = self.post("/ollama/api/chat", self.CHAT, up, stream=False)
        self.assertEqual(r.status_code, 200, r.text)
        validate("ChatFinal", r.json())
        body = r.json()
        self.assertEqual(body["message"], {"role": "assistant", "content": "Hello"})
        self.assertEqual((body["prompt_eval_count"], body["eval_count"]), (11, 7))
        self.assertEqual(body["eval_duration"], 9_000_000)

    def test_chat_streams_by_default_as_ndjson(self) -> None:
        up = sse(
            [
                chunk({"role": "assistant", "content": ""}),
                chunk({"content": "Hel"}),
                chunk({"content": "lo"}),
                chunk({}, "stop"),
                chunk({}, usage={"prompt_tokens": 9, "completion_tokens": 3}, choices=[]),
            ]
        )
        r = self.post("/api/chat", self.CHAT, up)  # no "stream" key
        self.assertEqual(r.headers["content-type"], "application/x-ndjson")
        objs = ndjson(r)
        for obj in objs[:-1]:
            validate("ChatChunk", obj)
        validate("ChatFinal", objs[-1])
        self.assertEqual("".join(o["message"]["content"] for o in objs), "Hello")
        self.assertEqual((objs[-1]["prompt_eval_count"], objs[-1]["eval_count"]), (9, 3))
        self.assertTrue(self.sent[0]["stream"])
        self.assertEqual(self.sent[0]["stream_options"], {"include_usage": True})

    def test_chat_stream_tool_call_is_one_complete_object(self) -> None:
        up = sse(
            [
                chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                                       "function": {"name": "get_weather", "arguments": ""}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
                chunk({}, "tool_calls"),
            ]
        )
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
        r = self.post("/api/chat", {**self.CHAT, "tools": tools}, up, stream=True)
        objs = ndjson(r)
        for obj in objs[:-1]:
            validate("ChatChunk", obj)
        validate("ChatFinal", objs[-1])
        calls = [c for o in objs for c in o["message"].get("tool_calls", [])]
        self.assertEqual(calls, [{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}])

    def test_chat_non_stream_tool_call_and_history_round_trip(self) -> None:
        up = chat_completion(
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
            "tool_calls",
        )
        history = [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]},
            {"role": "tool", "tool_name": "get_weather", "content": "Sunny"},
        ]
        r = self.post("/api/chat", {"model": "default", "messages": history}, up, stream=False)
        validate("ChatFinal", r.json())
        self.assertEqual(r.json()["message"]["tool_calls"][0]["function"]["arguments"], {"city": "Paris"})
        sent = self.sent[0]["messages"]
        call_id = sent[1]["tool_calls"][0]["id"]
        self.assertEqual(sent[1]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(sent[2], {"role": "tool", "tool_call_id": call_id, "content": "Sunny"})

    def test_think_returns_thinking_and_default_hides_it(self) -> None:
        up = chat_completion({"role": "assistant", "content": "4", "reasoning_content": "2+2"})
        shown = self.post("/api/chat", {**self.CHAT, "think": True}, up, stream=False).json()
        validate("ChatFinal", shown)
        self.assertEqual(shown["message"]["thinking"], "2+2")
        hidden = self.post("/api/chat", self.CHAT, up, stream=False).json()
        self.assertNotIn("thinking", hidden["message"])
        self.assertEqual(hidden["message"]["content"], "4")

    def test_length_stop_does_not_turn_reasoning_into_content(self) -> None:
        up = chat_completion({"role": "assistant", "content": "", "reasoning_content": "hm"}, "length")
        body = self.post("/api/chat", self.CHAT, up, stream=False).json()
        self.assertEqual(body["message"]["content"], "")
        self.assertEqual(body["done_reason"], "length")

    def test_options_are_forwarded_or_ignored_never_rejected(self) -> None:
        up = chat_completion({"role": "assistant", "content": "ok"})
        options = {"num_ctx": 4096, "num_gpu": 1, "top_k": 40, "min_p": 0.1,
                   "repeat_penalty": 1.1, "num_predict": 32, "temperature": 0.3,
                   "made_up_option": True}
        r = self.post("/api/chat", {**self.CHAT, "options": options}, up, stream=False)
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.sent[0]
        self.assertEqual(
            (sent["top_k"], sent["min_p"], sent["repeat_penalty"], sent["max_tokens"], sent["temperature"]),
            (40, 0.1, 1.1, 32, 0.3),
        )
        self.assertNotIn("num_ctx", sent)
        self.assertFalse([k for k in sent if k.startswith("__")])

    def test_unknown_model_is_404_with_ollama_wording(self) -> None:
        up = (400, {"error": {"message": "/chat/completions: Invalid model name passed in model=nope. Call `/v1/models`."}})
        for path, body in (("/api/chat", self.CHAT), ("/api/generate", self.GEN)):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    if stream:
                        async def fake(request, ctx, cc, *, stream):
                            return _FakeClient(), httpx.Response(
                                400, json=up[1]
                            )
                        with patch.object(proxy, "_forward_chat_completions", fake):
                            r = self.client.post(path, json={**body, "model": "nope", "stream": True})
                    else:
                        r = self.post(path, {**body, "model": "nope"}, up, stream=False)
                    self.assertEqual(r.status_code, 404, r.text)
                    validate("Error", r.json())
                    self.assertEqual(r.json()["error"], 'model "nope" not found, try pulling it first')

    def test_bad_requests_use_error_shape(self) -> None:
        r = self.post("/api/chat", {"model": "default", "messages": [{"role": "bot", "content": "x"}]}, {}, stream=False)
        self.assertEqual(r.status_code, 400)
        validate("Error", r.json())
        r = self.client.post("/api/chat", content=b"not json")
        self.assertEqual(r.status_code, 400)
        validate("Error", r.json())

    # -- generate ------------------------------------------------------

    def test_generate_non_stream_and_stream_match_schemas(self) -> None:
        up = chat_completion({"role": "assistant", "content": "ha"})
        r = self.post("/api/generate", {**self.GEN, "system": "terse"}, up, stream=False)
        validate("GenerateFinal", r.json())
        self.assertEqual(r.json()["response"], "ha")
        self.assertEqual(self.sent[0]["messages"][0], {"role": "system", "content": "terse"})

        s = sse([chunk({"content": "h"}), chunk({"content": "a"}), chunk({}, "stop"),
                 chunk({}, usage={"prompt_tokens": 2, "completion_tokens": 2}, choices=[])])
        objs = ndjson(self.post("/api/generate", self.GEN, s))
        for obj in objs[:-1]:
            validate("GenerateChunk", obj)
        validate("GenerateFinal", objs[-1])
        self.assertEqual("".join(o["response"] for o in objs), "ha")

    def test_generate_rejects_fill_in_the_middle_and_template(self) -> None:
        for extra in ({"suffix": "}"}, {"template": "{{ .Prompt }}"}):
            r = self.client.post("/api/generate", json={**self.GEN, **extra, "stream": False})
            self.assertEqual(r.status_code, 400, r.text)
            validate("Error", r.json())

    # -- load / unload -------------------------------------------------

    def test_empty_requests_load_and_unload_without_generating(self) -> None:
        async def boom(*a, **k):
            raise AssertionError("backend must not be called")

        with litellm_catalog(["default"]), patch.object(proxy, "_forward_chat_completions", boom):
            r = self.client.post("/api/chat", json={"model": "default", "messages": []})
            validate("LoadChat", r.json())
            self.assertEqual(r.json()["done_reason"], "load")
            r = self.client.post("/api/chat", json={"model": "default", "messages": [], "keep_alive": 0})
            self.assertEqual(r.json()["done_reason"], "unload")
            r = self.client.post("/api/generate", json={"model": "default"})
            validate("LoadGenerate", r.json())
            r = self.client.post("/api/generate", json={"model": "default", "prompt": "", "keep_alive": 0})
            self.assertEqual(r.json()["done_reason"], "unload")
            r = self.client.post("/api/generate", json={"model": "nope"})
            self.assertEqual(r.status_code, 404)
            validate("Error", r.json())

    # -- model management ---------------------------------------------

    def test_tags_show_version_match_schemas(self) -> None:
        with litellm_catalog(["default", "nomic-embed-text-v1.5"]):
            tags = self.client.get("/ollama/api/tags")
            validate("Tags", tags.json())
            self.assertIn("default", {m["name"] for m in tags.json()["models"]})
            show = self.client.post("/api/show", json={"model": "default"})
            validate("Show", show.json())
            self.assertTrue({"completion", "vision", "tools"} <= set(show.json()["capabilities"]))
            embed_show = self.client.post("/api/show", json={"model": "nomic-embed-text-v1.5"})
            self.assertEqual(embed_show.json()["capabilities"], ["embedding"])
            missing = self.client.post("/api/show", json={"model": "nope"})
            self.assertEqual(missing.status_code, 404)
            validate("Error", missing.json())
        version = self.client.get("/api/version")
        validate("Version", version.json())

    def test_ps_matches_schema(self) -> None:
        with patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value={"default"})), \
                patch.object(proxy, "get_active_backend", AsyncMock(return_value=None)), \
                patch.object(proxy, "_litellm_runtime_config") as cfg:
            cfg.return_value.enabled = False
            r = self.client.get("/ollama/api/ps")
        self.assertEqual(r.status_code, 200, r.text)
        validate("Ps", r.json())
        self.assertEqual([m["name"] for m in r.json()["models"]], ["default"])

    def test_embed_and_legacy_embeddings_match_schemas(self) -> None:
        sent = []

        async def fake_embed(request, auth_context, body):
            sent.append(body)
            return httpx.Response(
                200,
                json={"object": "list", "model": body["model"],
                      "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2]}
                               for i, _ in enumerate(body["input"] if isinstance(body["input"], list) else [0])],
                      "usage": {"prompt_tokens": 4, "total_tokens": 4}},
                request=httpx.Request("POST", "http://gateway/v1/embeddings"),
            )

        with patch.object(proxy, "_forward_embeddings", fake_embed):
            r = self.client.post(
                "/api/embed",
                json={"model": "e", "input": ["a", "b"], "truncate": True, "keep_alive": "5m",
                      "options": {"num_ctx": 512}, "dimensions": 2},
            )
            self.assertEqual(r.status_code, 200, r.text)
            validate("Embed", r.json())
            self.assertEqual(len(r.json()["embeddings"]), 2)
            self.assertEqual(sent[0], {"model": "e", "input": ["a", "b"], "dimensions": 2})
            r = self.client.post("/api/embeddings", json={"model": "e", "prompt": "a", "options": {"num_ctx": 8}})
            validate("Embeddings", r.json())

    def test_model_mutation_routes_are_501_with_error_shape(self) -> None:
        for method, path in (
            ("POST", "/api/pull"),
            ("POST", "/api/push"),
            ("POST", "/api/create"),
            ("DELETE", "/api/delete"),
            ("POST", "/api/copy"),
            ("POST", "/ollama/api/blobs/sha256:abc"),
        ):
            with self.subTest(path=path):
                r = self.client.request(method, path, json={"model": "x", "name": "x"})
                self.assertEqual(r.status_code, 501, r.text)
                validate("Error", r.json())
                self.assertIn("unsupported operation", r.json()["error"])
        self.assertEqual(self.client.head("/api/blobs/sha256:abc").status_code, 501)

    def test_missing_credentials_use_ollama_error_shape(self) -> None:
        self.auth.stop()
        self.addCleanup(self.auth.start)
        with keyed_auth("sk-conformance"):
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                r = self.client.post(
                    "/api/chat", headers=headers, json={**self.CHAT, "stream": False}
                )
                self.assertEqual(r.status_code, 401, r.text)
                validate("Error", r.json())

    def test_name_latest_is_the_same_model(self) -> None:
        up = chat_completion({"role": "assistant", "content": "ok"})
        self.post("/api/chat", {**self.CHAT, "model": "default:latest"}, up, stream=False)
        self.post("/api/generate", {**self.GEN, "model": "default:latest"}, up, stream=False)
        self.assertEqual([c["model"] for c in self.sent], ["default", "default"])


if __name__ == "__main__":
    unittest.main()
