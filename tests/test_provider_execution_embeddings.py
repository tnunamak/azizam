"""Remote ``/v1/embeddings`` on the paid path: its own field set, and the body the official SDK sends.

The SDK adds ``encoding_format="base64"`` to the embeddings requests it builds, so a plain
``client.embeddings.create(model=<remote>, input="hello")`` must be admitted. Chat keeps its own field set.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import openai

from gateway import provider_execution, receipt_store
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from tests.support.access import identity as identity_of
from tests.test_provider_execution import _Client, _Response, _profile, _provider, _request

EMBEDDING_MODEL = "openrouter/openai/text-embedding-3-small"
CHAT_MODEL = "openrouter/openai/gpt-4.1-mini"
BODY = {"model": EMBEDDING_MODEL, "input": "hello"}


def _embedding_provider():
    provider = _provider()
    models = [{"id": "openai/gpt-4.1-mini"}, {"id": "openai/text-embedding-3-small"}]
    return replace(provider, config={**provider.config, "models": models})


class _SdkWire(httpx.BaseTransport):
    """Records the request the SDK builds and answers with one embedding. Nothing leaves the process."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        vector = base64.b64encode(struct.pack("<2f", 0.5, 1.5)).decode()  # the SDK decodes base64 by default
        reply = {
            "object": "list", "model": EMBEDDING_MODEL,
            "data": [{"object": "embedding", "index": 0, "embedding": vector}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        return httpx.Response(200, json=reply, request=request)


def _sdk_embeddings_body(**options) -> dict:
    """The JSON body the official SDK sends for ``client.embeddings.create`` with these options."""
    wire = _SdkWire()
    sdk = openai.OpenAI(api_key="sk-unused", base_url="http://gateway.test/v1", max_retries=0,
                        http_client=httpx.Client(transport=wire))
    sdk.embeddings.create(model=EMBEDDING_MODEL, input="hello", **options)
    return json.loads(wire.sent[0].content)


def _forwarded(send: AsyncMock) -> dict:
    return json.loads(send.await_args.args[0].body)


class RemoteEmbeddingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Path(tmp.name) / "provider_spend.db"
        self.runtime = LiteLlmRuntimeConfig(
            enabled=True, base_url="http://litellm.test", api_key="sk-litellm", service_id="litellm",
        )
        self.provider = _embedding_provider()
        self.profile = _profile()
        for patcher in (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-provider"}),
            patch.object(receipt_store, "begin", lambda _receipt: None),
            patch.object(receipt_store, "finalize", lambda _receipt: True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _post(self, body: dict, *, path: str = "/v1/embeddings"):
        send = AsyncMock(return_value=(_Client(), _Response(
            headers={"content-type": "application/json", "x-litellm-response-cost": "0.000001"},
            body=json.dumps({
                "object": "list", "model": EMBEDDING_MODEL, "data": [],
                "usage": {"cost": "0.000001", "prompt_tokens": 1, "total_tokens": 1},
            }).encode(),
        )))
        with patch.object(provider_execution, "send_llm_execution_request", send):
            response = await provider_execution.execute_paid_litellm(
                _request(body),
                request_path=path,
                body=body,
                provider=self.provider,
                identity=identity_of(self.profile, name="paid-app", app_id="app_test", accountable_person_id="usr_test"),
                runtime=self.runtime,
                ledger_path=self.ledger,
            )
        return response, send

    async def test_the_sdk_default_body_is_admitted_and_forwarded(self) -> None:
        body = _sdk_embeddings_body()
        self.assertEqual(body["encoding_format"], "base64")  # the SDK default this path has to admit
        response, send = await self._post(body)
        self.assertEqual(response.status_code, 200, response.body)
        send.assert_awaited_once()
        self.assertEqual(_forwarded(send)["encoding_format"], "base64")
        self.assertEqual(_forwarded(send)["input"], "hello")

    async def test_float_and_base64_are_admitted(self) -> None:
        for encoding_format in ("float", "base64"):
            with self.subTest(encoding_format=encoding_format):
                response, send = await self._post({**BODY, "encoding_format": encoding_format})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(_forwarded(send)["encoding_format"], encoding_format)

    async def test_any_other_encoding_format_is_a_clear_400_before_dispatch(self) -> None:
        for encoding_format in ("hex", "", None, ["base64"]):
            with self.subTest(encoding_format=encoding_format):
                response, send = await self._post({**BODY, "encoding_format": encoding_format})
                self.assertEqual(response.status_code, 400)
                error = json.loads(response.body)["error"]
                self.assertEqual((error["code"], error["param"]), ("invalid_request", "encoding_format"))
                send.assert_not_awaited()

    async def test_dimensions_must_be_a_positive_integer_and_are_passed_through(self) -> None:
        response, send = await self._post({**BODY, "dimensions": 256})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_forwarded(send)["dimensions"], 256)
        for dimensions in (0, -1, "256", 1.5, True, None):
            with self.subTest(dimensions=dimensions):
                response, send = await self._post({**BODY, "dimensions": dimensions})
                self.assertEqual(response.status_code, 400)
                error = json.loads(response.body)["error"]
                self.assertEqual((error["code"], error["param"]), ("invalid_request", "dimensions"))
                send.assert_not_awaited()

    async def test_embeddings_admit_only_their_own_fields(self) -> None:
        for field, value in (
            ("messages", [{"role": "user", "content": "hi"}]),
            ("max_tokens", 8),
            ("max_completion_tokens", 8),
            ("stream", True),
            ("temperature", 0.5),
        ):
            with self.subTest(field=field):
                response, send = await self._post({**BODY, field: value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.body)["error"]["code"], "paid_option_unsupported")
                send.assert_not_awaited()

    async def test_chat_keeps_refusing_the_embeddings_options(self) -> None:
        chat = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        for field, value in (("encoding_format", "base64"), ("dimensions", 256)):
            with self.subTest(field=field):
                response, send = await self._post({**chat, field: value}, path="/v1/chat/completions")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.body)["error"]["code"], "paid_option_unsupported")
                send.assert_not_awaited()

    def test_the_embeddings_field_list_is_explicit(self) -> None:
        self.assertEqual(
            provider_execution.PAID_EMBEDDINGS_REQUEST_FIELDS,
            frozenset({"dimensions", "encoding_format", "input", "model", "user"}),
        )


if __name__ == "__main__":
    unittest.main()
