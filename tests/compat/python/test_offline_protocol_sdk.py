"""Offline compatibility checks that run the real gateway app and SDK parsers.

No live provider is involved: the upstream provider call is replaced by a fixed
payload, and the policy, translator and SDK response parsing are the real code.
"""

from __future__ import annotations

from contextlib import nullcontext

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import proxy
from tests.support.access import authenticated, plan_for, access# noqa: E402
from gateway import media_jobs, receipt_store
from gateway.protocols.anthropic import (
    AnthropicTranslationError,
    messages_request_to_canonical,
)


class TestOfflineProtocolSDK(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        receipt_db = root / "receipts.db"
        jobs_db = root / "media_jobs.db"
        receipt_store.init_db(receipt_db)
        media_jobs.init_db(jobs_db)
        for patcher in (
            patch.object(receipt_store, "DB_PATH", receipt_db),
            patch.object(media_jobs, "DB_PATH", jobs_db),
            patch.object(proxy, "IMAGE_CACHE_DIR", root / "images"),
            authenticated(),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The app lifespan is not entered: provider maintenance is not under test.
        self.gateway = TestClient(proxy.app)
        self.addCleanup(self.gateway.close)
        self.wire = []

    def _sdk_http_client(self):
        def through_gateway(request):
            response = self.gateway.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                headers=dict(request.headers),
                content=request.content,
            )
            self.wire.append({"path": request.url.path, "status": response.status_code})
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
                request=request,
            )

        return httpx.Client(transport=httpx.MockTransport(through_gateway))

    def test_chat_only_profile_denies_images_before_backend_work(self):
        raw_key = "opk_compat_policy_fixture"
        key_hash = proxy.hash_api_key(raw_key)
        identity = proxy.AuthIdentity(
            name="compat-chat-only",
            key_hash=key_hash,
            downstream={},
            access=access(plan_for("compat-chat-only", caps=("chat",))),
        )
        backend = AsyncMock(
            side_effect=AssertionError("Denied request reached backend selection")
        )
        with authenticated(identity), patch.object(proxy, "get_active_backend", backend):
            response = self.gateway.post(
                "/v1/images/generations",
                headers={"Authorization": f"Bearer {raw_key}"},
                json={
                    "model": "flux2-klein-4b",
                    "prompt": "fixture blue square",
                    "n": 1,
                },
            )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "not_in_access_plan")
        backend.assert_not_awaited()

    def test_openai_responses_sdk_preserves_text_and_usage(self):
        import openai

        sdk = openai.OpenAI(
            base_url="http://compat.invalid/v1",
            api_key="compat-fixture",
            max_retries=0,
            http_client=self._sdk_http_client(),
        )
        self.addCleanup(sdk.close)
        upstream = {
            "id": "chatcmpl-compat-responses",
            "object": "chat.completion",
            "created": 1,
            "model": "default",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "compatibility probe"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        sent = []

        async def provider(request, auth_context, cc_body, *, stream):
            self.assertFalse(stream)
            sent.append(cc_body)
            return httpx.Response(
                200,
                json=upstream,
                request=httpx.Request("POST", "http://provider.invalid/v1/chat/completions"),
            )

        with patch.object(proxy, "_forward_chat_completions", provider):
            result = sdk.responses.create(
                model="default", input="fixture input", store=False
            )
        self.assertEqual(len(sent), 1)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output_text, "compatibility probe")
        self.assertEqual(len(result.output), 1)
        message = result.output[0]
        self.assertEqual(message.type, "message")
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.status, "completed")
        self.assertEqual(len(message.content), 1)
        self.assertEqual(message.content[0].type, "output_text")
        self.assertEqual(message.content[0].text, "compatibility probe")
        self.assertEqual(result.usage.input_tokens, 11)
        self.assertEqual(result.usage.output_tokens, 7)
        self.assertEqual(self.wire[0]["path"], "/v1/responses")


class TestAnthropicRequestCompatibility(unittest.TestCase):
    def test_echoed_thinking_is_dropped_but_user_text_is_preserved(self):
        translated = messages_request_to_canonical(
            {
                "model": "default",
                "max_tokens": 8,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "thinking", "thinking": "fixture private reasoning"},
                            {"type": "text", "text": "compatibility probe"},
                        ],
                    }
                ],
            }
        )
        self.assertEqual(
            translated["messages"],
            [{"role": "user", "content": "compatibility probe"}],
        )
        self.assertNotIn("fixture private reasoning", json.dumps(translated))

    def test_truly_unsupported_document_block_is_rejected(self):
        with self.assertRaises(AnthropicTranslationError) as caught:
            messages_request_to_canonical(
                {
                    "model": "default",
                    "max_tokens": 8,
                    "messages": [{
                        "role": "user",
                        "content": [{
                            "type": "document",
                            "source": {"type": "text", "data": "fixture"},
                        }],
                    }],
                }
            )
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.error_type, "invalid_request_error")
        self.assertIn("document", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
