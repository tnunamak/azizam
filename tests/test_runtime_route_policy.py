"""Runtime route profile-policy enforcement tests."""

from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.policy import AppProfile, ModelAccessPolicy  # noqa: E402


RAW_KEY = "opk_runtime_policy_test"
AUTH_HEADERS = {"Authorization": f"Bearer {RAW_KEY}"}


@contextmanager
def authenticated_profile(profile: AppProfile):
    key_hash = proxy.hash_api_key(RAW_KEY)
    identity = proxy.AuthIdentity(
        name="runtime-policy",
        key_hash=key_hash,
        downstream={},
        profile=profile,
    )
    with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
        proxy, "AUTH_REGISTRY", {key_hash: identity}
    ), patch.object(proxy, "refresh_auth_registry", lambda force=False: None):
        yield


def ok_response() -> Response:
    return Response(
        content='{"ok": true}',
        status_code=200,
        media_type="application/json",
    )


class _FakeHTTPResponse:
    status_code = 200

    def json(self):
        return {"object": "list", "data": []}


class _FakeChatResponse:
    status_code = 200

    def json(self):
        return {
            "id": "chatcmpl_runtime_policy",
            "object": "chat.completion",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, *args, **kwargs):
        return _FakeHTTPResponse()


class TestRuntimeRoutePolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)

    def test_chat_only_opk_denies_media_and_models_routes(self) -> None:
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )
        blocked = AssertionError("runtime backend should not be called")

        with authenticated_profile(chat_only), patch.object(
            proxy, "proxy_tts_request", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "proxy_stt_request", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "proxy_comfy_video_create", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "proxy_audio_generate", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(side_effect=blocked)
        ):
            cases = [
                ("post", "/v1/audio/speech", {"input": "hello", "voice": "alloy"}),
                ("post", "/v1/audio/transcriptions", None),
                ("post", "/v1/videos", {"prompt": "waves"}),
                ("post", "/v1/audio/generate", {"prompt": "piano"}),
                ("get", "/v1/models", None),
            ]
            for method, path, payload in cases:
                with self.subTest(path=path):
                    request = getattr(self.client, method)
                    if payload is None:
                        response = request(path, headers=AUTH_HEADERS)
                    else:
                        response = request(path, headers=AUTH_HEADERS, json=payload)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "policy_denied",
                    )

    def test_chat_only_opk_denies_media_job_content_before_lookup(self) -> None:
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )
        blocked = AssertionError("media job content lookup should not be called")

        with authenticated_profile(chat_only), patch.object(
            proxy.media_jobs,
            "get",
            side_effect=blocked,
        ):
            response = self.client.get(
                "/gateway/v1/media/jobs/job_runtime_policy/content",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")

    def test_image_profile_allows_media_job_content(self) -> None:
        image_profile = AppProfile(
            id="image",
            protocols=frozenset({"openai.images"}),
        )
        job = {
            "job_id": "job_runtime_policy",
            "intent": "image.generate",
            "status": proxy.media_jobs.STATUS_SUCCEEDED,
        }
        payload = {"created": 123, "data": [{"url": "https://example.test/i.png"}]}

        with authenticated_profile(image_profile), patch.object(
            proxy.media_jobs,
            "get",
            return_value=job,
        ), patch.object(
            proxy,
            "_openai_image_payload_from_media_job",
            return_value=payload,
        ):
            response = self.client.get(
                "/gateway/v1/media/jobs/job_runtime_policy/content",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), payload)

    def test_media_profile_allows_audio_and_video_routes(self) -> None:
        media_profile = AppProfile(
            id="media",
            protocols=frozenset({"openai.audio", "openai.video"}),
        )

        with authenticated_profile(media_profile), patch.object(
            proxy, "proxy_tts_request", AsyncMock(return_value=ok_response())
        ), patch.object(
            proxy, "proxy_stt_request", AsyncMock(return_value=ok_response())
        ), patch.object(
            proxy, "proxy_comfy_video_create", AsyncMock(return_value=ok_response())
        ), patch.object(
            proxy, "proxy_audio_generate", AsyncMock(return_value=ok_response())
        ):
            cases = [
                ("post", "/v1/audio/speech", {"input": "hello", "voice": "alloy"}),
                ("post", "/v1/audio/transcriptions", None),
                ("post", "/v1/videos", {"prompt": "waves"}),
                ("post", "/v1/audio/generate", {"prompt": "piano"}),
            ]
            for method, path, payload in cases:
                with self.subTest(path=path):
                    request = getattr(self.client, method)
                    if payload is None:
                        response = request(path, headers=AUTH_HEADERS)
                    else:
                        response = request(path, headers=AUTH_HEADERS, json=payload)
                    self.assertEqual(response.status_code, 200, response.text)

    def test_full_openai_profile_allows_models_route(self) -> None:
        full_profile = AppProfile(
            id="full-openai",
            protocols=frozenset({"openai"}),
        )

        with authenticated_profile(full_profile), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(proxy.httpx, "AsyncClient", _FakeAsyncClient), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: None
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["object"], "list")

    def test_models_route_filters_profile_model_allowlist(self) -> None:
        model_locked = AppProfile(
            id="model-locked",
            protocols=frozenset({"openai"}),
            models=frozenset({"default"}),
        )

        with authenticated_profile(model_locked), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(proxy.httpx, "AsyncClient", _FakeAsyncClient), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: None
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        ids = [item["id"] for item in response.json()["data"]]
        self.assertEqual(ids, ["default"])
        self.assertEqual(
            response.json()["vivid_fish_profile_model_allowlist"],
            ["default"],
        )

    def test_current_loaded_profile_filters_models_and_denies_other_models(self) -> None:
        default_role = proxy.ROLES.get("default") if isinstance(proxy.ROLES, dict) else {}
        loaded_path = str(default_role.get("model") or "/models/default.gguf")
        current_loaded = AppProfile(
            id="current-loaded",
            protocols=frozenset({"openai"}),
            model_access=ModelAccessPolicy(mode="current_loaded"),
        )

        with authenticated_profile(current_loaded), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(proxy.httpx, "AsyncClient", _FakeAsyncClient), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: loaded_path
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("default", ids)
        self.assertNotIn("rp", ids)
        self.assertEqual(response.json()["vivid_fish_model_access"], "current_loaded")

        with authenticated_profile(current_loaded), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: loaded_path
        ):
            denied = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": "rp",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "policy_denied")
        self.assertIn("currently loaded", denied.json()["error"]["message"])

    def test_current_loaded_profile_denies_when_no_loaded_model_reported(self) -> None:
        current_loaded = AppProfile(
            id="current-loaded",
            protocols=frozenset({"openai"}),
            model_access=ModelAccessPolicy(mode="current_loaded"),
        )

        with authenticated_profile(current_loaded), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(proxy, "currently_loaded_kobold_model", lambda: None):
            response = self.client.post(
                "/v1/embeddings",
                headers=AUTH_HEADERS,
                json={"model": "default", "input": "hello"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")
        self.assertIn("no loaded LLM", response.json()["error"]["message"])

    def test_chat_only_profile_allows_post_responses(self) -> None:
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )

        with authenticated_profile(chat_only), patch.object(
            proxy,
            "_forward_chat_completions",
            AsyncMock(return_value=_FakeChatResponse()),
        ), patch.object(proxy.responses_store, "save", return_value=None):
            response = self.client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json={"model": "test-model", "input": "hello", "store": False},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["object"], "response")

    def test_media_only_profile_denies_post_responses(self) -> None:
        media_only = AppProfile(
            id="media-only",
            protocols=frozenset({"openai.audio", "openai.video"}),
        )
        blocked = AssertionError("responses backend should not be called")

        with authenticated_profile(media_only), patch.object(
            proxy,
            "_forward_chat_completions",
            AsyncMock(side_effect=blocked),
        ):
            response = self.client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json={"model": "test-model", "input": "hello"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")

    def test_non_chat_profile_denies_stored_response_routes(self) -> None:
        media_only = AppProfile(
            id="media-only",
            protocols=frozenset({"openai.audio", "openai.video"}),
        )
        blocked = AssertionError("responses store should not be called")

        with authenticated_profile(media_only), patch.object(
            proxy.responses_store,
            "get",
            side_effect=blocked,
        ), patch.object(
            proxy.responses_store,
            "delete",
            side_effect=blocked,
        ):
            cases = [
                ("get", "/v1/responses/resp_runtime_policy"),
                ("delete", "/v1/responses/resp_runtime_policy"),
            ]
            for method, path in cases:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(
                        path,
                        headers=AUTH_HEADERS,
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "policy_denied",
                    )


if __name__ == "__main__":
    unittest.main()
