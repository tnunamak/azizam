"""Runtime route profile-policy enforcement tests."""

from __future__ import annotations

from contextlib import nullcontext

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_jobs, receipt_store  # noqa: E402
from gateway.access_plans import AccessPlan  # noqa: E402
from tests.support.access import access, plan_for, auth_identity  # noqa: E402


RAW_KEY = "opk_runtime_policy_test"
AUTH_HEADERS = {"Authorization": f"Bearer {RAW_KEY}"}


@contextmanager
def authenticated_profile(profile: AccessPlan, downstream: dict | None = None):
    """Requests with RAW_KEY run as an App whose person and app hold ``profile``."""
    key_hash = proxy.hash_api_key(RAW_KEY)
    identity = auth_identity(
        name="runtime-policy",
        key_hash=key_hash,
        downstream=downstream or {},
        access=access(profile),
        app_id="app_runtime_policy",
        accountable_person_id="usr_runtime_policy",
    )
    def lookup(candidate_hash: str, **_kwargs):
        return identity if candidate_hash == key_hash else None

    with patch.object(
        proxy, "runtime_identity_for_app_credential_hash", side_effect=lookup
    ):
        yield identity


def ok_response() -> Response:
    return Response(
        content='{"ok": true}',
        status_code=200,
        media_type="application/json",
    )


def routed_response(route: str) -> Response:
    """A distinct body per delegate, so the response shows which one answered."""
    return Response(
        content=json.dumps({"route": route}),
        status_code=200,
        media_type="application/json",
    )


def _migrated_model_policy_config() -> dict:
    config = copy.deepcopy(proxy.config)
    for service in (config.get("backends") or {}).values():
        if isinstance(service, dict) and isinstance(service.get("models"), list):
            service["model_policy_schema_version"] = proxy.service_registry.MODEL_POLICY_SCHEMA_VERSION
    return config


# The shipped config serves every chat role from one GGUF, so policy tests
# that need a role backed by a different model define one here.
OTHER_ROLE = "other-model"
OTHER_MODEL_PATH = "/models/other-model-not-loaded.gguf"


def _roles_with_other_model() -> dict:
    roles = copy.deepcopy(proxy.ROLES)
    roles[OTHER_ROLE] = {"backend": "kobold", "model": OTHER_MODEL_PATH}
    return roles


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
        "x-native-marker": "1",
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.receipts_db = root / "receipts.db"
        self.media_jobs_db = root / "media_jobs.db"
        self.cache_dir = root / "cache"
        receipt_store.init_db(self.receipts_db)
        media_jobs.init_db(self.media_jobs_db)
        self._receipt_store_path = patch.object(receipt_store, "DB_PATH", self.receipts_db)
        self._media_jobs_path = patch.object(media_jobs, "DB_PATH", self.media_jobs_db)
        self._image_cache_dir = patch.object(proxy, "IMAGE_CACHE_DIR", self.cache_dir)
        self._image_url_secret = patch.object(proxy, "_image_url_signing_secret", b"test-secret")
        for patcher in (self._receipt_store_path, self._media_jobs_path, self._image_cache_dir, self._image_url_secret):
            patcher.start()
            self.addCleanup(patcher.stop)
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
                    Request({
                        "type": "http", "method": "POST", "path": "/v1/messages",
                        "headers": [(b"authorization", f"Bearer {RAW_KEY}".encode())],
                        "query_string": b"", "client": ("203.0.113.10", 41000),
                    }),
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
        # Plans do not restrict by protocol (design v4 §2.1); a plan without chat
        # denies the direct route, and the loopback marker is still recognized.
        anthropic_only = plan_for("no-chat", caps=('embed',))
        identity = auth_identity(
            name="anthropic-only",
            key_hash=proxy.hash_api_key(RAW_KEY),
            downstream={},
            access=access(anthropic_only),
        )
        auth_context = proxy.AuthContext(identity=identity)
        body = json.dumps({"model": "test-model"}).encode("utf-8")

        direct_decision = proxy._azizam_policy_decision(
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
        anthropic_only = plan_for("anthropic-only", caps=('chat',))
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

    def test_plan_without_chat_denies_anthropic_count_tokens(self) -> None:
        openai_only = plan_for("embed-only", caps=('embed',))

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
        chat_only = plan_for("chat-only", caps=('chat',))
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
                        "not_in_access_plan",
                    )

    def test_chat_only_opk_denies_video_list_and_delete(self) -> None:
        """GET /v1/videos (list) and DELETE /v1/videos/{id} share a
        canonical path with POST /v1/videos and GET /v1/videos/{video_id}
        respectively, so this registry can't carry a second, method-specific
        policy entry for them (see registry/protocol_frontends.yml's comment
        next to the video routes). They must still be denied in-handler,
        the same as every other video route -- a valid chat-only credential
        must not be able to enumerate or delete videos."""
        chat_only = plan_for("chat-only", caps=('chat',))
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
                        "not_in_access_plan",
                    )

    def _assert_only_awaited(self, target: str, delegates: dict[str, AsyncMock]) -> AsyncMock:
        """The allowed route awaited exactly its own delegate, once, and no other."""
        for name, delegate in delegates.items():
            if name == target:
                delegate.assert_awaited_once()
            else:
                delegate.assert_not_awaited()
        return delegates[target]

    def test_media_profile_allows_video_list_and_delete(self) -> None:
        media_profile = plan_for("media", caps=('video',))
        delegates = {
            "proxy_comfy_video_list": AsyncMock(return_value=routed_response("list")),
            "proxy_comfy_video_delete": AsyncMock(return_value=routed_response("delete")),
        }

        with authenticated_profile(media_profile) as identity, patch.object(
            proxy, "proxy_comfy_video_list", delegates["proxy_comfy_video_list"]
        ), patch.object(
            proxy, "proxy_comfy_video_delete", delegates["proxy_comfy_video_delete"]
        ):
            cases = [
                ("get", "/v1/videos", "proxy_comfy_video_list", "list"),
                ("delete", "/v1/videos/video_does-not-exist", "proxy_comfy_video_delete", "delete"),
            ]
            for method, path, target, route in cases:
                with self.subTest(path=path):
                    for delegate in delegates.values():
                        delegate.reset_mock()
                    response = getattr(self.client, method)(path, headers=AUTH_HEADERS)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {"route": route})
                    args = self._assert_only_awaited(target, delegates).await_args.args
                    self.assertIs(args[1].identity, identity)
                    if route == "list":
                        self.assertEqual((args[0].method, args[0].url.path), ("GET", path))
                    else:
                        self.assertEqual(args[0], "video_does-not-exist")

    def test_chat_only_opk_denies_media_job_content_before_lookup(self) -> None:
        chat_only = plan_for("chat-only", caps=('chat',))
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
        self.assertEqual(response.json()["error"]["code"], "not_in_access_plan")

    def test_image_profile_allows_media_job_content(self) -> None:
        image_profile = plan_for("image", caps=('image',))
        job = {
            "job_id": "job_runtime_policy",
            "intent": "image.generate",
            "status": proxy.media_jobs.STATUS_SUCCEEDED,
            "owner_key_hash": proxy.hash_api_key(RAW_KEY),
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
        audio_profile = plan_for("audio", caps=('speech', 'transcribe', 'music', 'sfx', 'audio'))
        job = {
            "job_id": "job_audio_policy",
            "intent": "music.generate",
            "status": proxy.media_jobs.STATUS_SUCCEEDED,
            "owner_key_hash": proxy.hash_api_key(RAW_KEY),
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
        image_profile = plan_for("image", caps=('image',))
        job = {
            "job_id": "job_audio_policy",
            "intent": "music.generate",
            "status": proxy.media_jobs.STATUS_SUCCEEDED,
            "owner_key_hash": proxy.hash_api_key(RAW_KEY),
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
        self.assertEqual(response.json()["error"]["code"], "not_in_access_plan")

    def test_media_profile_allows_audio_and_video_routes(self) -> None:
        media_profile = plan_for("media", caps=('speech', 'transcribe', 'music', 'sfx', 'audio', 'video'))

        delegates = {
            name: AsyncMock(return_value=routed_response(name))
            for name in ("proxy_tts_request", "proxy_stt_request",
                         "proxy_comfy_video_create", "proxy_audio_generate")
        }

        with authenticated_profile(media_profile) as identity, patch.object(
            proxy, "proxy_tts_request", delegates["proxy_tts_request"]
        ), patch.object(
            proxy, "proxy_stt_request", delegates["proxy_stt_request"]
        ), patch.object(
            proxy, "proxy_comfy_video_create", delegates["proxy_comfy_video_create"]
        ), patch.object(
            proxy, "proxy_audio_generate", delegates["proxy_audio_generate"]
        ):
            # (path, payload, delegate, route-specific keyword arguments)
            cases = [
                ("/v1/audio/speech", {"input": "hello", "voice": "alloy"}, "proxy_tts_request", {"receipt"}),
                ("/v1/audio/transcriptions", None, "proxy_stt_request", {"operation": "transcriptions"}),
                ("/v1/audio/translations", None, "proxy_stt_request", {"operation": "translations"}),
                ("/v1/videos", {"prompt": "waves"}, "proxy_comfy_video_create", {}),
                ("/v1/audio/generate", {"prompt": "piano"}, "proxy_audio_generate", {"receipt"}),
            ]
            for path, payload, target, keywords in cases:
                with self.subTest(path=path):
                    for delegate in delegates.values():
                        delegate.reset_mock()
                    if payload is None:
                        response = self.client.post(path, headers=AUTH_HEADERS)
                    else:
                        response = self.client.post(path, headers=AUTH_HEADERS, json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {"route": target})
                    call = self._assert_only_awaited(target, delegates).await_args
                    request, auth_context = call.args[:2]
                    self.assertEqual((request.method, request.url.path), ("POST", path))
                    self.assertIs(auth_context.identity, identity)
                    if isinstance(keywords, dict):
                        for name, value in keywords.items():
                            self.assertEqual(call.kwargs.get(name), value, name)
                    else:  # The route's own receipt travels with the request.
                        for name in keywords:
                            self.assertIsNotNone(call.kwargs.get(name), name)

    def test_service_availability_override_blocks_tts_and_stt_routes(self) -> None:
        media_profile = plan_for("media", caps=('speech', 'transcribe', 'music', 'sfx', 'audio'))
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
        speech_error = speech.json()["error"]
        self.assertEqual(speech_error["code"], "resource_admission_denied")
        self.assertEqual(speech_error["message"], "This service is not available right now.")
        self.assertTrue(speech_error["decision"]["reasons"])
        self.assertNotIn("tts reserved for operator", speech.text)
        self.assertEqual(transcription.status_code, 503, transcription.text)
        self.assertEqual(transcription.headers["retry-after"], "32")
        transcription_error = transcription.json()["error"]
        self.assertEqual(transcription_error["code"], "resource_admission_denied")
        self.assertEqual(transcription_error["message"], "This service is not available right now.")
        self.assertTrue(transcription_error["decision"]["reasons"])
        self.assertNotIn("stt paused", transcription.text)

    @patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set()))
    def test_full_openai_profile_allows_models_route(self) -> None:
        full_profile = plan_for("full-openai")
        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["default"])

        with authenticated_profile(full_profile), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy, "_probed_context_window", AsyncMock(return_value=8192)
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["object"], "list")

    @patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set()))
    def test_models_route_filters_profile_model_allowlist(self) -> None:
        model_locked = plan_for("model-locked", models=['default'])
        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["default", "rp"])

        with authenticated_profile(model_locked), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy, "_probed_context_window", AsyncMock(return_value=8192)
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        ids = [item["id"] for item in response.json()["data"]]
        # `vision` is another name for `default`, so a plan that lists `default`
        # can use it, and the listing says so (a request for it is allowed too).
        self.assertEqual(ids, ["default", "vision"])

    def test_plan_that_cannot_load_models_lists_them_but_denies_starting_one(self) -> None:
        """Discovery lists granted items with their state; starting one needs May load (design v4 §2.13)."""
        default_role = proxy.ROLES.get("default") if isinstance(proxy.ROLES, dict) else {}
        loaded_path = str(default_role.get("model") or "/models/default.gguf")
        current_loaded = plan_for("current-loaded", load=False)
        self.assertNotEqual(
            loaded_path, OTHER_MODEL_PATH, "fixture role must use a different model"
        )

        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _CaptureForwardClient(), _ForwardModelsResponse(["default", OTHER_ROLE])

        with authenticated_profile(current_loaded), patch.object(
            proxy, "ROLES", _roles_with_other_model()
        ), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "currently_loaded_model", lambda _backend: loaded_path
        ), patch.object(
            proxy, "_probed_context_window", AsyncMock(return_value=8192)
        ):
            response = self.client.get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("default", ids)
        self.assertIn(OTHER_ROLE, ids)

        with authenticated_profile(current_loaded), patch.object(
            proxy, "ROLES", _roles_with_other_model()
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "currently_loaded_model", lambda _backend: loaded_path
        ):
            denied = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": OTHER_ROLE,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "cannot_start_model")
        self.assertIn("isn't running, and your access plan can't start it", denied.json()["error"]["message"])

    def test_current_loaded_profile_denies_when_no_loaded_model_reported(self) -> None:
        current_loaded = plan_for("current-loaded", load=False)

        with authenticated_profile(current_loaded), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(proxy, "currently_loaded_model", lambda _backend: None):
            response = self.client.post(
                "/v1/embeddings",
                headers=AUTH_HEADERS,
                json={"model": "default", "input": "hello"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "cannot_start_model")
        self.assertIn("no loaded LLM", response.json()["error"]["message"])

    def test_current_model_returns_503_for_existing_only_profile_when_unloaded(self) -> None:
        current_loaded = plan_for("current-loaded", load=False)

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
        current_loaded = plan_for("current-loaded", load=False)

        self.assertNotEqual(
            loaded_path, OTHER_MODEL_PATH, "fixture role must use a different model"
        )

        async def forbidden_send(*_args, **_kwargs):
            raise AssertionError("spoofed existing-only request must not forward")

        with authenticated_profile(current_loaded), patch.object(
            proxy, "ROLES", _roles_with_other_model()
        ), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", litellm_registry
        ), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "currently_loaded_model", lambda _backend: loaded_path
        ), patch.object(
            proxy, "send_llm_execution_request", forbidden_send
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": OTHER_ROLE,
                    "_vf_requested_model": "default",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "cannot_start_model")
        self.assertIn("currently loaded", response.json()["error"]["message"])

    def test_existing_only_target_policy_uses_workflow_availability(self) -> None:
        existing_only = plan_for("workflow-existing-only", caps=('image',), load=False)

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
        chat_only = plan_for("chat-only", caps=('chat',))

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
        media_only = plan_for("media-only", caps=('speech', 'transcribe', 'music', 'sfx', 'audio', 'video'))
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
        self.assertEqual(response.json()["error"]["code"], "not_in_access_plan")

    def test_vision_chat_can_use_loaded_kobold_mmproj_without_static_vision_capability(self) -> None:
        chat_profile = plan_for("vision-kobold", caps=('chat',))
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
            proxy, "loaded_aux_present", return_value=True
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
                    "model": "unaliased-vision-model",
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
        unrestricted = plan_for("runtime")

        with authenticated_profile(unrestricted), patch.object(
            proxy.httpx, "AsyncClient", _CaptureForwardClient
        ):
            response = self.client.get("/v1/model", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(_CaptureForwardClient.urls, [])

    def test_ollama_model_mutation_routes_require_auth(self) -> None:
        with nullcontext():
            response = self.client.post("/api/pull", json={"name": "llama3"})

        self.assertEqual(response.status_code, 401, response.text)

    def test_ollama_model_mutation_routes_return_unsupported_without_backend(self) -> None:
        ollama_profile = plan_for("ollama")
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

    def test_ollama_discovery_routes_require_auth(self) -> None:
        for path in ("/api/tags", "/ollama/api/tags", "/api/version", "/ollama/api/version"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, f"{path} should require auth")

    def test_provider_native_kobold_requires_native_passthrough_policy(self) -> None:
        profile = plan_for("native-disabled", caps=())
        blocked = AssertionError("native backend should not be called")

        with authenticated_profile(profile), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.get(
                "/gateway/v1/providers/kobold/v1/models",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "not_in_access_plan")
        self.assertIn("Direct engine access to kobold isn't in your access plan", response.json()["error"]["message"])

    def test_provider_native_kobold_forwards_with_downstream_auth_only(self) -> None:
        profile = plan_for("kobold-native", caps=(), engines=['kobold'], operations=['v1/models'])
        downstream = {
            "kobold": {
                "api_key": "kobold-api-key",
                "admin_key": "kobold-admin-key",
            }
        }

        # Downstream auth is operator data on the service record: the admin key
        # for model-list paths, the api key otherwise.
        inject = [
            {"paths": ["/v1/models"], "header": "x-admin-key",
             "secret": "admin_key", "secret_env": "FIXTURE_ADMIN_KEY"},
            {"header": "x-api-key", "secret": "api_key", "secret_env": "FIXTURE_API_KEY"},
        ]
        with authenticated_profile(profile, downstream=downstream), patch.dict(
            proxy.config["backends"]["kobold"], {"auth_inject": inject},
        ), patch.object(
            proxy.httpx,
            "AsyncClient",
            _CaptureNativePassthroughClient,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.get(
                "/gateway/v1/providers/kobold/v1/models?limit=1&api_key=caller-query&API_KEY=second-query",
                headers={
                    **AUTH_HEADERS,
                    "X-Api-Key": "caller-secret",
                    "X-Request-Id": "request-123",
                    "Cookie": "azizam_admin_session=synthetic-session",
                    "X-CSRF-Token": "synthetic-csrf",
                    "X-Azizam-Internal-Forward": "synthetic-internal",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"native": True})
        self.assertEqual(
            _CaptureNativePassthroughClient.calls[0]["url"],
            f"{proxy.KOBOLD_HOST}/v1/models?limit=1",
        )
        forwarded_headers = {
            key.lower(): value
            for key, value in _CaptureNativePassthroughClient.calls[0][
                "headers"
            ].items()
        }
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotEqual(forwarded_headers.get("x-api-key"), "caller-secret")
        self.assertEqual(forwarded_headers["x-admin-key"], "kobold-admin-key")
        for name in ("x-request-id", "cookie", "x-csrf-token", "x-azizam-internal-forward"):
            self.assertNotIn(name, forwarded_headers)
        self.assertEqual(response.headers["x-native-marker"], "1")

    def test_provider_native_targets_include_configured_local_backends(self) -> None:
        self.assertIn("kobold", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("tts", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("stt", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("embeddings", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertIn("comfyui", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertNotIn("azizam", proxy._PROVIDER_NATIVE_TARGETS)

    def test_provider_native_list_projects_profile_policy(self) -> None:
        profile = plan_for("native-list", caps=(), engines=['comfyui'], operations=['POST /prompt'])

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
        self.assertNotIn("kobold", by_id)
        self.assertNotIn("allowed", by_id["comfyui"])
        self.assertEqual(
            by_id["comfyui"]["policy"]["native_passthrough"],
            {
                "enabled": True,
                "denied": False,
                "allowed_backends": ["comfyui"],
                "allowed_operations_by_plan": {"native-list": ["POST /prompt"]},
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
        profile = plan_for("native-list", caps=(), engines=['comfyui'], operations=['*'])

        with authenticated_profile(profile):
            response = self.client.get(
                "/gateway/v1/providers/kobold",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "provider_not_found")

    def test_provider_native_detail_does_not_forward_to_backend(self) -> None:
        profile = plan_for("native-detail", caps=(), engines='all', operations=['*'])
        blocked = AssertionError("metadata route should not call native backend")

        downstream = {"kobold": {"api_key_env": "KOBOLD_API_KEY", "admin_key": "secret"}}

        with authenticated_profile(profile, downstream=downstream), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ):
            response = self.client.get(
                "/gateway/v1/providers/kobold",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], "kobold")
        self.assertEqual(
            body["policy"]["native_passthrough"],
            {
                "enabled": True,
                "denied": False,
                "allowed_backends": ["kobold"],
                "allowed_operations_by_plan": {"native-detail": ["*"]},
                "audit": True,
            },
        )
        self.assertEqual(body["auth"]["required"], "gateway_api_key")
        self.assertEqual(body["auth"]["downstream_service"], "kobold")
        self.assertEqual(
            body["auth"]["downstream_auth_classes"],
            ["admin_key", "api_key"],
        )
        self.assertTrue(body["auth"]["downstream_auth_configured"])
        self.assertTrue(body["live"]["enabled"])

    def test_openai_voice_aliases_enforce_audio_profile_policy(self) -> None:
        chat_only = plan_for("chat-only", caps=('chat',))

        with authenticated_profile(chat_only):
            for path in ("/openai/v1/voices", "/openai/v1/audio/voices"):
                with self.subTest(path=path):
                    response = self.client.get(path, headers=AUTH_HEADERS)

                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "not_in_access_plan"
                    )

    def test_provider_native_comfyui_forwards_generic_backend_path(self) -> None:
        profile = plan_for("comfyui-native", caps=(), engines=['comfyui'], operations=['POST /prompt'])
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
        self.assertNotIn("x-request-id", forwarded_headers)

    def test_provider_native_preserves_streaming_response_shape(self) -> None:
        profile = plan_for("kobold-native-stream", caps=(), engines=['kobold'], operations=['POST /v1/chat/completions'])
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
                "/gateway/v1/providers/kobold/v1/chat/completions",
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

    def test_provider_native_kobold_operation_allowlist_denies_before_backend(self) -> None:
        profile = plan_for("kobold-native", caps=(), engines=['kobold'], operations=['v1/models'])
        blocked = AssertionError("native backend should not be called")

        with authenticated_profile(profile), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ), patch.object(proxy.receipt_store, "record", return_value="req_test"):
            response = self.client.post(
                "/gateway/v1/providers/kobold/v1/model/load",
                headers=AUTH_HEADERS,
                json={"model": "unsafe"},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "not_in_access_plan")
        self.assertIn("isn't in your access plan", response.json()["error"]["message"])

    def test_provider_native_unknown_provider_returns_clear_404(self) -> None:
        profile = plan_for("native", caps=(), engines='all', operations=['*'])

        with authenticated_profile(profile):
            response = self.client.get(
                "/gateway/v1/providers/not-a-provider/v1/models",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "provider_not_found")

    def test_non_chat_profile_denies_stored_response_routes(self) -> None:
        media_only = plan_for("media-only", caps=('speech', 'transcribe', 'music', 'sfx', 'audio', 'video'))
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
                        "not_in_access_plan",
                    )


if __name__ == "__main__":
    unittest.main()
