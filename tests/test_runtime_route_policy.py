"""Runtime route profile-policy enforcement tests."""

from __future__ import annotations

import asyncio
import copy
import json
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
from gateway.policy import (  # noqa: E402
    AppProfile,
    ModelAccessPolicy,
    NativePassthroughPolicy,
    TargetPolicy,
)


RAW_KEY = "opk_runtime_policy_test"
AUTH_HEADERS = {"Authorization": f"Bearer {RAW_KEY}"}


@contextmanager
def authenticated_profile(profile: AppProfile, downstream: dict | None = None):
    key_hash = proxy.hash_api_key(RAW_KEY)
    identity = proxy.AuthIdentity(
        name="runtime-policy",
        key_hash=key_hash,
        downstream=downstream or {},
        profile=profile,
    )
    def lookup(candidate_hash: str, **_kwargs):
        return identity if candidate_hash == key_hash else None

    with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
        proxy, "runtime_identity_for_app_credential_hash", side_effect=lookup
    ):
        yield identity


def ok_response() -> Response:
    return Response(
        content='{"ok": true}',
        status_code=200,
        media_type="application/json",
    )


def _migrated_model_policy_config() -> dict:
    config = copy.deepcopy(proxy.config)
    for service in (config.get("backends") or {}).values():
        if isinstance(service, dict) and isinstance(service.get("models"), list):
            service["model_policy_schema_version"] = proxy.service_registry.MODEL_POLICY_SCHEMA_VERSION
    return config


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


class _ForwardResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self):
        return (
            b'{"id":"chatcmpl_runtime_route","object":"chat.completion",'
            b'"model":"local","choices":[{"index":0,"message":{"role":"assistant",'
            b'"content":"ok"},"finish_reason":"stop"}]}'
        )

    async def aclose(self):
        return None


class _ForwardModelsResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, model_ids: list[str]):
        self._model_ids = model_ids

    async def aread(self):
        return json.dumps({
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "owned_by": "litellm"}
                for model_id in self._model_ids
            ],
        }).encode()

    async def aclose(self):
        return None


class _CaptureForwardClient:
    urls: list[str] = []
    bodies: list[bytes] = []
    headers: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, *, method, url, headers, content):
        self.__class__.urls.append(str(url))
        self.__class__.bodies.append(content)
        self.__class__.headers.append(dict(headers))
        return object()

    async def send(self, *args, **kwargs):
        return _ForwardResponse()

    async def aclose(self):
        return None


class _CaptureLoopbackClient:
    headers: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, method, url, headers, content):
        self.__class__.headers.append(dict(headers))
        return object()

    async def send(self, *args, **kwargs):
        return _ForwardResponse()

    async def aclose(self):
        return None


class _HeaderOnlyRequest:
    headers = AUTH_HEADERS


def litellm_registry():
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


class _NativePassthroughResponse:
    status_code = 200
    headers = {
        "content-type": "application/json",
        "content-length": "17",
        "x-tabby-native": "1",
    }
    content = b'{"native": true}'

    async def aiter_bytes(self):
        yield self.content


class _CaptureNativePassthroughClient:
    calls: list[dict] = []
    response = _NativePassthroughResponse()
    closed = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aclose(self):
        self.__class__.closed = True

    def stream(self, method, url, headers, content):
        self.__class__.calls.append({
            "method": method,
            "url": str(url),
            "headers": dict(headers),
            "content": content,
        })
        return _FakeAsyncStreamContext(self.__class__.response)


class _NativeStreamingResponse:
    status_code = 200
    headers = {
        "content-type": "application/x-ndjson",
        "transfer-encoding": "chunked",
    }

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            assert not _CaptureNativePassthroughClient.closed
            yield chunk


class _FakeAsyncStreamContext:
    def __init__(self, response):
        self.response = response
        self.exited = False

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc_info):
        self.exited = True
        return False


class TestRuntimeRoutePolicy(unittest.TestCase):
    def setUp(self) -> None:
        self._model_policy_config = patch.object(proxy, "config", _migrated_model_policy_config())
        self._model_policy_config.start()
        self.addCleanup(self._model_policy_config.stop)
        self.client = TestClient(proxy.app)
        _CaptureForwardClient.urls = []
        _CaptureNativePassthroughClient.calls = []
        _CaptureNativePassthroughClient.response = _NativePassthroughResponse()
        _CaptureNativePassthroughClient.closed = False
        _CaptureForwardClient.bodies = []
        _CaptureForwardClient.headers = []
        _CaptureLoopbackClient.headers = []
        _CaptureNativePassthroughClient.calls = []

    def test_protocol_frontend_loopback_marks_internal_forward(self) -> None:
        _CaptureLoopbackClient.headers = []
        with patch.object(proxy.httpx, "AsyncClient", _CaptureLoopbackClient):
            client, response = asyncio.run(
                proxy._forward_chat_completions(
                    _HeaderOnlyRequest(),
                    proxy.NO_AUTH_CONTEXT,
                    {
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    stream=True,
                )
            )
            asyncio.run(response.aclose())
            asyncio.run(client.aclose())

        self.assertEqual(len(_CaptureLoopbackClient.headers), 1)
        self.assertEqual(
            _CaptureLoopbackClient.headers[0][proxy._INTERNAL_PROTOCOL_FORWARD_HEADER],
            proxy._INTERNAL_PROTOCOL_FORWARD_SECRET,
        )

    def test_protocol_frontend_loopback_avoids_openai_policy_requirement(self) -> None:
        anthropic_only = AppProfile(
            id="anthropic-only",
            protocols=frozenset({"anthropic.messages"}),
        )
        identity = proxy.AuthIdentity(
            name="anthropic-only",
            key_hash=proxy.hash_api_key(RAW_KEY),
            downstream={},
            profile=anthropic_only,
        )
        auth_context = proxy.AuthContext(identity=identity)
        body = json.dumps({"model": "test-model"}).encode("utf-8")

        direct_decision = proxy._openai_proxy_policy_decision(
            auth_context,
            "/v1/chat/completions",
            body,
        )

        class RequestLike:
            headers = {
                proxy._INTERNAL_PROTOCOL_FORWARD_HEADER: (
                    proxy._INTERNAL_PROTOCOL_FORWARD_SECRET
                )
            }
            client = type("Client", (), {"host": "127.0.0.1"})()

        self.assertIsNotNone(direct_decision)
        self.assertTrue(direct_decision.denied)
        self.assertTrue(proxy._is_internal_protocol_forward(RequestLike()))

    def test_anthropic_only_opk_allows_models_and_count_tokens(self) -> None:
        anthropic_only = AppProfile(
            id="anthropic-only",
            protocols=frozenset({"anthropic.messages"}),
        )
        payload = {
            "model": "claude-local",
            "messages": [{"role": "user", "content": "hello"}],
        }

        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["claude-local"])

        with authenticated_profile(anthropic_only), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            models_response = self.client.get(
                "/anthropic/v1/models",
                headers=AUTH_HEADERS,
            )
            count_response = self.client.post(
                "/anthropic/v1/messages/count_tokens",
                headers=AUTH_HEADERS,
                json=payload,
            )

        self.assertEqual(models_response.status_code, 200, models_response.text)
        self.assertEqual(count_response.status_code, 200, count_response.text)
        self.assertGreater(count_response.json()["input_tokens"], 0)

    def test_openai_only_opk_denies_anthropic_count_tokens(self) -> None:
        openai_only = AppProfile(
            id="openai-only",
            protocols=frozenset({"openai.chat"}),
        )

        with authenticated_profile(openai_only):
            response = self.client.post(
                "/anthropic/v1/messages/count_tokens",
                headers=AUTH_HEADERS,
                json={
                    "model": "claude-local",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["type"], "permission_error")

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
                ("post", "/v1/audio/translations", None),
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

    def test_chat_only_opk_denies_video_list_and_delete(self) -> None:
        """GET /v1/videos (list) and DELETE /v1/videos/{id} share a
        canonical path with POST /v1/videos and GET /v1/videos/{video_id}
        respectively, so this registry can't carry a second, method-specific
        policy entry for them (see registry/protocol_frontends.yml's comment
        next to the video routes). They must still be denied in-handler,
        the same as every other video route -- a valid chat-only credential
        must not be able to enumerate or delete videos."""
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )
        blocked = AssertionError("runtime backend should not be called")

        with authenticated_profile(chat_only), patch.object(
            proxy, "proxy_comfy_video_list", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "proxy_comfy_video_delete", AsyncMock(side_effect=blocked)
        ):
            cases = [
                ("get", "/v1/videos"),
                ("delete", "/v1/videos/video_does-not-exist"),
            ]
            for method, path in cases:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(path, headers=AUTH_HEADERS)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "policy_denied",
                    )

    def test_media_profile_allows_video_list_and_delete(self) -> None:
        media_profile = AppProfile(
            id="media",
            protocols=frozenset({"openai.video"}),
        )

        with authenticated_profile(media_profile), patch.object(
            proxy, "proxy_comfy_video_list", AsyncMock(return_value=ok_response())
        ), patch.object(
            proxy, "proxy_comfy_video_delete", AsyncMock(return_value=ok_response())
        ):
            cases = [
                ("get", "/v1/videos"),
                ("delete", "/v1/videos/video_does-not-exist"),
            ]
            for method, path in cases:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(path, headers=AUTH_HEADERS)
                    self.assertEqual(response.status_code, 200, response.text)

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

    def test_audio_profile_allows_audio_media_job_content(self) -> None:
        audio_profile = AppProfile(
            id="audio",
            protocols=frozenset({"openai.audio"}),
        )
        job = {
            "job_id": "job_audio_policy",
            "intent": "music.generate",
            "status": proxy.media_jobs.STATUS_SUCCEEDED,
            "assets": [{
                "asset_id": "aud_" + "a" * 64,
                "media_type": "audio/mpeg",
            }],
        }

        with authenticated_profile(audio_profile), patch.object(
            proxy.media_jobs,
            "get",
            return_value=job,
        ):
            response = self.client.get(
                "/gateway/v1/media/jobs/job_audio_policy/content",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["object"], "audio.generation")
        self.assertEqual(body["data"][0]["asset_id"], "aud_" + "a" * 64)

    def test_image_profile_denies_audio_media_job_content_after_lookup(self) -> None:
        image_profile = AppProfile(
            id="image",
            protocols=frozenset({"openai.images"}),
        )
        job = {
            "job_id": "job_audio_policy",
            "intent": "music.generate",
            "status": proxy.media_jobs.STATUS_SUCCEEDED,
            "assets": [{"asset_id": "aud_" + "a" * 64}],
        }

        with authenticated_profile(image_profile), patch.object(
            proxy.media_jobs,
            "get",
            return_value=job,
        ):
            response = self.client.get(
                "/gateway/v1/media/jobs/job_audio_policy/content",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")

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
                ("post", "/v1/audio/translations", None),
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

    def test_service_availability_override_blocks_tts_and_stt_routes(self) -> None:
        media_profile = AppProfile(
            id="media",
            protocols=frozenset({"openai.audio"}),
        )
        overrides = {
            "service_availability": {
                "tts": {
                    "state": "maintenance",
                    "reason": "tts reserved for operator",
                    "retry_after_seconds": 31,
                },
                "stt": {
                    "state": "disabled",
                    "reason": "stt paused",
                    "retry_after_seconds": 32,
                },
            },
            "media_job_admission": {},
        }

        with authenticated_profile(media_profile), patch.object(
            proxy,
            "TTS_ENABLED",
            True,
        ), patch.object(
            proxy,
            "STT_ENABLED",
            True,
        ), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value=overrides,
        ):
            speech = self.client.post(
                "/v1/audio/speech",
                headers=AUTH_HEADERS,
                json={"input": "hello", "voice": "alloy"},
            )
            transcription = self.client.post(
                "/v1/audio/transcriptions",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(speech.status_code, 503, speech.text)
        self.assertEqual(speech.headers["retry-after"], "31")
        self.assertEqual(speech.json()["error"]["message"], "tts reserved for operator")
        self.assertEqual(transcription.status_code, 503, transcription.text)
        self.assertEqual(transcription.headers["retry-after"], "32")
        self.assertEqual(transcription.json()["error"]["message"], "stt paused")

    def test_full_openai_profile_allows_models_route(self) -> None:
        full_profile = AppProfile(
            id="full-openai",
            protocols=frozenset({"openai"}),
        )
        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["default"])

        with authenticated_profile(full_profile), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy, "_kobold_context_window", AsyncMock(return_value=8192)
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
        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["default", "rp"])

        with authenticated_profile(model_locked), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy, "_kobold_context_window", AsyncMock(return_value=8192)
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
        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["default", "rp"])

        with authenticated_profile(current_loaded), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: loaded_path
        ), patch.object(
            proxy, "_kobold_context_window", AsyncMock(return_value=8192)
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("default", ids)
        self.assertNotIn("rp", ids)
        self.assertEqual(response.json()["vivid_fish_target_visibility"], "available")
        self.assertEqual(response.json()["vivid_fish_target_activation"], "existing_only")
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
            target_policy=TargetPolicy(
                visibility="available",
                activation="existing_only",
            ),
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

    def test_current_model_returns_503_for_existing_only_profile_when_unloaded(self) -> None:
        current_loaded = AppProfile(
            id="current-loaded",
            protocols=frozenset({"openai"}),
            target_policy=TargetPolicy(
                visibility="available",
                activation="existing_only",
            ),
        )

        async def forbidden_send(*_args, **_kwargs):
            raise AssertionError("model='current' must not forward when unloaded")

        with authenticated_profile(current_loaded), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "current_llm_model_for_backend", AsyncMock(return_value="")
        ), patch.object(
            proxy, "_service_availability_response", lambda *_args, **_kwargs: None
        ), patch.object(
            proxy, "send_llm_execution_request", forbidden_send
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": "current",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "current_model_unavailable")

    def test_private_requested_model_field_cannot_spoof_existing_only_policy(self) -> None:
        default_role = proxy.ROLES.get("default") if isinstance(proxy.ROLES, dict) else {}
        loaded_path = str(default_role.get("model") or "/models/default.gguf")
        current_loaded = AppProfile(
            id="current-loaded",
            protocols=frozenset({"openai"}),
            target_policy=TargetPolicy(
                visibility="available",
                activation="existing_only",
            ),
        )

        async def forbidden_send(*_args, **_kwargs):
            raise AssertionError("spoofed existing-only request must not forward")

        with authenticated_profile(current_loaded), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: loaded_path
        ), patch.object(
            proxy, "send_llm_execution_request", forbidden_send
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": "rp",
                    "_vf_requested_model": "default",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")
        self.assertIn("currently loaded", response.json()["error"]["message"])

    def test_existing_only_target_policy_uses_workflow_availability(self) -> None:
        existing_only = AppProfile(
            id="workflow-existing-only",
            protocols=frozenset({"openai.images"}),
            target_policy=TargetPolicy(
                visibility="available",
                activation="existing_only",
            ),
        )

        with authenticated_profile(existing_only) as identity, patch.object(
            proxy, "COMFY_ENABLED", True
        ), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value={},
        ):
            auth_context = proxy.AuthContext(identity)
            allowed = proxy._workflow_target_policy_decision(
                auth_context,
                requested="sdxl-seamless-tile",
                resolved="sdxl-seamless-tile",
            )

        self.assertIsNone(allowed)

        with authenticated_profile(existing_only) as identity, patch.object(
            proxy, "COMFY_ENABLED", False
        ):
            auth_context = proxy.AuthContext(identity)
            denied = proxy._workflow_target_policy_decision(
                auth_context,
                requested="sdxl-seamless-tile",
                resolved="sdxl-seamless-tile",
            )

        self.assertIsNotNone(denied)
        assert denied is not None
        self.assertTrue(denied.denied)
        self.assertIn("no available workflow", denied.reasons[0])

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

    def test_vision_chat_can_use_loaded_kobold_mmproj_without_static_vision_capability(self) -> None:
        chat_profile = AppProfile(
            id="vision-kobold",
            protocols=frozenset({"openai.chat"}),
            backends=frozenset({"kobold"}),
        )
        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardResponse()

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

        roles = {
            **(proxy.ROLES if isinstance(proxy.ROLES, dict) else {}),
            "vision": {
                "backend": "kobold",
                "model": "/models/vision.gguf",
                "mmproj": "/models/vision-mmproj.gguf",
            },
        }

        def supports(backend: str, capability: str) -> bool:
            return backend == "kobold" and capability == "chat"

        with authenticated_profile(chat_profile), patch.object(
            proxy, "ROLES", roles
        ), patch.object(
            proxy, "backend_supports", supports
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "kobold_has_mmproj", return_value=True
        ), patch.object(
            proxy, "ensure_target", AsyncMock(return_value=True)
        ) as ensure_target, patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,aGVsbG8="
                                    },
                                },
                            ],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")
        ensure_target.assert_not_called()

    def test_backend_native_v1_model_passthrough_is_not_public(self) -> None:
        unrestricted = AppProfile(id="runtime", protocols=frozenset({"openai"}))

        with authenticated_profile(unrestricted), patch.object(
            proxy.httpx, "AsyncClient", _CaptureForwardClient
        ):
            response = self.client.get("/v1/model", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(_CaptureForwardClient.urls, [])

    def test_ollama_model_mutation_routes_require_auth(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", True):
            response = self.client.post("/api/pull", json={"name": "llama3"})

        self.assertEqual(response.status_code, 401, response.text)

    def test_ollama_model_mutation_routes_are_ollama_profile_gated(self) -> None:
        openai_only = AppProfile(id="openai-only", protocols=frozenset({"openai"}))
        blocked = AssertionError("runtime backend should not be called")

        with authenticated_profile(openai_only), patch.object(
            proxy.httpx, "AsyncClient", side_effect=blocked
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "ensure_target", AsyncMock(side_effect=blocked)
        ):
            response = self.client.post(
                "/ollama/api/pull",
                headers=AUTH_HEADERS,
                json={"name": "llama3"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertIn("protocol", body["error"])

    def test_ollama_model_mutation_routes_return_unsupported_without_backend(self) -> None:
        ollama_profile = AppProfile(id="ollama", protocols=frozenset({"ollama"}))
        blocked = AssertionError("runtime backend should not be called")
        routes = [
            ("post", "/api/create", "/api/create", {"model": "local"}),
            (
                "post",
                "/ollama/api/copy",
                "/api/copy",
                {"source": "llama3", "destination": "llama3-copy"},
            ),
            ("post", "/api/pull", "/api/pull", {"name": "llama3"}),
            ("post", "/ollama/api/push", "/api/push", {"name": "llama3"}),
            ("delete", "/api/delete", "/api/delete", {"name": "llama3"}),
        ]

        with authenticated_profile(ollama_profile), patch.object(
            proxy.httpx, "AsyncClient", side_effect=blocked
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(side_effect=blocked)
        ), patch.object(
            proxy, "ensure_target", AsyncMock(side_effect=blocked)
        ):
            for method, path, canonical_path, payload in routes:
                response = self.client.request(
                    method.upper(),
                    path,
                    headers=AUTH_HEADERS,
                    json=payload,
                )

                self.assertEqual(response.status_code, 501, response.text)
                body = response.json()
                self.assertEqual(set(body), {"error"})
                self.assertIn("unsupported operation", body["error"])
                self.assertIn(canonical_path, body["error"])
                self.assertIn("does not mutate local model files", body["error"])

    def test_ollama_discovery_routes_require_auth_by_default(self) -> None:
        for path in ("/api/tags", "/ollama/api/tags", "/api/version", "/ollama/api/version"):
            with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
                proxy, "OLLAMA_COMPAT_DISCOVERY_ENABLED", False
            ):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 401, f"{path} should require auth by default")

    def test_ollama_discovery_routes_public_when_compat_mode_enabled(self) -> None:
        for path in ("/api/tags", "/ollama/api/tags", "/api/version", "/ollama/api/version"):
            with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
                proxy, "OLLAMA_COMPAT_DISCOVERY_ENABLED", True
            ), patch.object(
                proxy, "_ollama_model_catalog_summaries", AsyncMock(return_value=[])
            ):
                response = self.client.get(path)
            self.assertNotEqual(
                response.status_code, 401, f"{path} should be public with compat mode enabled"
            )
            self.assertEqual(response.status_code, 200, response.text)

    def test_ollama_mutation_routes_still_require_auth_when_compat_mode_enabled(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy, "OLLAMA_COMPAT_DISCOVERY_ENABLED", True
        ):
            response = self.client.post("/api/pull", json={"name": "llama3"})
        self.assertEqual(response.status_code, 401, response.text)

    def test_provider_native_tabby_requires_native_passthrough_policy(self) -> None:
        profile = AppProfile(
            id="native-disabled",
            protocols=frozenset({"provider-native"}),
        )
        blocked = AssertionError("native backend should not be called")

        with authenticated_profile(profile), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.get(
                "/gateway/v1/providers/tabby/v1/models",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")
        self.assertIn("native passthrough disabled", response.json()["error"]["message"])

    def test_provider_native_tabby_forwards_with_downstream_auth_only(self) -> None:
        profile = AppProfile(
            id="tabby-native",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"tabby"}),
                allowed_operations=frozenset({"v1/models"}),
            ),
        )
        downstream = {
            "tabby": {
                "api_key": "tabby-api-key",
                "admin_key": "tabby-admin-key",
            }
        }

        with authenticated_profile(profile, downstream=downstream), patch.object(
            proxy.httpx,
            "AsyncClient",
            _CaptureNativePassthroughClient,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.get(
                "/gateway/v1/providers/tabby/v1/models?limit=1",
                headers={
                    **AUTH_HEADERS,
                    "X-Api-Key": "caller-secret",
                    "X-Request-Id": "request-123",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"native": True})
        self.assertEqual(
            _CaptureNativePassthroughClient.calls[0]["url"],
            f"{proxy.TABBY_HOST}/v1/models?limit=1",
        )
        forwarded_headers = {
            key.lower(): value
            for key, value in _CaptureNativePassthroughClient.calls[0][
                "headers"
            ].items()
        }
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotEqual(forwarded_headers.get("x-api-key"), "caller-secret")
        self.assertEqual(forwarded_headers["x-admin-key"], "tabby-admin-key")
        self.assertEqual(forwarded_headers["x-request-id"], "request-123")
        self.assertEqual(response.headers["x-tabby-native"], "1")

    def test_provider_native_targets_include_configured_local_backends(self) -> None:
        self.assertIn("tabby", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("kobold", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("tts", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("stt", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("embeddings", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("comfyui", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertNotIn("openai-proxy", proxy._PROVIDER_NATIVE_TARGETS)

    def test_provider_native_list_projects_profile_policy(self) -> None:
        profile = AppProfile(
            id="native-list",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"comfyui"}),
                allowed_operations=frozenset({"POST /prompt"}),
            ),
        )

        with authenticated_profile(profile):
            response = self.client.get(
                "/gateway/v1/providers",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["object"], "list")
        by_id = {row["id"]: row for row in body["data"]}
        self.assertIn("comfyui", by_id)
        self.assertNotIn("tabby", by_id)
        self.assertNotIn("allowed", by_id["comfyui"])
        self.assertEqual(
            by_id["comfyui"]["policy"]["native_passthrough"],
            {
                "enabled": True,
                "denied": False,
                "allowed_backends": ["comfyui"],
                "allowed_operations": ["POST /prompt"],
                "audit": True,
            },
        )
        self.assertEqual(by_id["comfyui"]["auth"]["required"], "gateway_api_key")
        self.assertEqual(
            by_id["comfyui"]["auth"]["downstream_service"],
            "comfyui",
        )
        self.assertEqual(by_id["comfyui"]["auth"]["downstream_auth_classes"], [])
        self.assertFalse(by_id["comfyui"]["auth"]["downstream_auth_configured"])
        self.assertTrue(by_id["comfyui"]["live"]["enabled"])
        self.assertEqual(
            by_id["comfyui"]["native_passthrough_path"],
            "/gateway/v1/providers/comfyui/{path}",
        )

    def test_provider_native_detail_hides_disallowed_provider(self) -> None:
        profile = AppProfile(
            id="native-list",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"comfyui"}),
            ),
        )

        with authenticated_profile(profile):
            response = self.client.get(
                "/gateway/v1/providers/tabby",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "provider_not_found")

    def test_provider_native_detail_does_not_forward_to_backend(self) -> None:
        profile = AppProfile(
            id="native-detail",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(enabled=True),
        )
        blocked = AssertionError("metadata route should not call native backend")

        downstream = {"tabby": {"api_key_env": "TABBY_API_KEY", "admin_key": "secret"}}

        with authenticated_profile(profile, downstream=downstream), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ):
            response = self.client.get(
                "/gateway/v1/providers/tabby",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], "tabby")
        self.assertEqual(
            body["policy"]["native_passthrough"],
            {
                "enabled": True,
                "denied": False,
                "allowed_backends": ["tabby"],
                "allowed_operations": ["*"],
                "audit": True,
            },
        )
        self.assertEqual(body["auth"]["required"], "gateway_api_key")
        self.assertEqual(body["auth"]["downstream_service"], "tabby")
        self.assertEqual(
            body["auth"]["downstream_auth_classes"],
            ["admin_key", "api_key"],
        )
        self.assertTrue(body["auth"]["downstream_auth_configured"])
        self.assertTrue(body["live"]["enabled"])

    def test_openai_voice_aliases_enforce_audio_profile_policy(self) -> None:
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )

        with authenticated_profile(chat_only):
            for path in ("/openai/v1/voices", "/openai/v1/audio/voices"):
                with self.subTest(path=path):
                    response = self.client.get(path, headers=AUTH_HEADERS)

                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "policy_denied"
                    )

    def test_provider_native_comfyui_forwards_generic_backend_path(self) -> None:
        profile = AppProfile(
            id="comfyui-native",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"comfyui"}),
                allowed_operations=frozenset({"POST /prompt"}),
            ),
        )
        downstream = {
            "comfyui": {
                "headers": {
                    "X-Comfy-Key": "comfy-secret",
                }
            }
        }

        with authenticated_profile(profile, downstream=downstream), patch.object(
            proxy.httpx,
            "AsyncClient",
            _CaptureNativePassthroughClient,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.post(
                "/gateway/v1/providers/comfyui/prompt",
                headers={
                    **AUTH_HEADERS,
                    "X-Api-Key": "caller-secret",
                    "X-Request-Id": "request-456",
                },
                json={"prompt": {"1": {"inputs": {}}}},
            )

        self.assertEqual(response.status_code, 200, response.text)
        call = _CaptureNativePassthroughClient.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{proxy.COMFY_HOST}/prompt")
        self.assertIn(b'"prompt"', call["content"])
        forwarded_headers = {
            key.lower(): value
            for key, value in call["headers"].items()
        }
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotEqual(forwarded_headers.get("x-api-key"), "caller-secret")
        self.assertEqual(forwarded_headers["x-comfy-key"], "comfy-secret")
        self.assertEqual(forwarded_headers["x-request-id"], "request-456")

    def test_provider_native_preserves_streaming_response_shape(self) -> None:
        profile = AppProfile(
            id="tabby-native-stream",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"tabby"}),
                allowed_operations=frozenset({"POST /v1/chat/completions"}),
            ),
        )
        _CaptureNativePassthroughClient.response = _NativeStreamingResponse([
            b'{"message":"hel"}\n',
            b'{"message":"lo","done":true}\n',
        ])

        with authenticated_profile(profile), patch.object(
            proxy.httpx,
            "AsyncClient",
            _CaptureNativePassthroughClient,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.post(
                "/gateway/v1/providers/tabby/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={"stream": True, "messages": []},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, '{"message":"hel"}\n{"message":"lo","done":true}\n')
        self.assertEqual(
            response.headers["content-type"],
            "application/x-ndjson",
        )
        self.assertNotIn("transfer-encoding", response.headers)

    def test_provider_native_tabby_operation_allowlist_denies_before_backend(self) -> None:
        profile = AppProfile(
            id="tabby-native",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"tabby"}),
                allowed_operations=frozenset({"v1/models"}),
            ),
        )
        blocked = AssertionError("native backend should not be called")

        with authenticated_profile(profile), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.post(
                "/gateway/v1/providers/tabby/v1/model/load",
                headers=AUTH_HEADERS,
                json={"model": "unsafe"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "policy_denied")
        self.assertIn("not in allow-list", response.json()["error"]["message"])

    def test_provider_native_unknown_provider_returns_clear_404(self) -> None:
        profile = AppProfile(
            id="native",
            protocols=frozenset({"provider-native"}),
            native_passthrough=NativePassthroughPolicy(enabled=True),
        )

        with authenticated_profile(profile):
            response = self.client.get(
                "/gateway/v1/providers/not-a-provider/v1/models",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "provider_not_found")

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
