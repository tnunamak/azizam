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
from gateway.policy import (  # noqa: E402
    AppProfile,
    ModelAccessPolicy,
    NativePassthroughPolicy,
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


class _CaptureForwardClient:
    urls: list[str] = []
    bodies: list[bytes] = []

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, *, method, url, headers, content):
        self.__class__.urls.append(str(url))
        self.__class__.bodies.append(content)
        return object()

    async def send(self, *args, **kwargs):
        return _ForwardResponse()

    async def aclose(self):
        return None


class _NativePassthroughResponse:
    status_code = 200
    headers = {
        "content-type": "application/json",
        "content-length": "17",
        "x-tabby-native": "1",
    }
    content = b'{"native": true}'


class _CaptureNativePassthroughClient:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method, url, headers, content):
        self.__class__.calls.append({
            "method": method,
            "url": str(url),
            "headers": dict(headers),
            "content": content,
        })
        return _NativePassthroughResponse()


class TestRuntimeRoutePolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _CaptureForwardClient.urls = []
        _CaptureForwardClient.bodies = []
        _CaptureNativePassthroughClient.calls = []

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

    def test_chat_alias_routes_to_configured_kobold_without_tabby(self) -> None:
        chat_profile = AppProfile(
            id="chat-kobold",
            protocols=frozenset({"openai.chat"}),
            backends=frozenset({"kobold"}),
        )

        with authenticated_profile(chat_profile), patch.object(
            proxy, "get_active_backend", AsyncMock(return_value="kobold")
        ), patch.object(
            proxy, "effective_llm_model_for_host", AsyncMock(return_value="kobold-local")
        ), patch.object(proxy.httpx, "AsyncClient", _CaptureForwardClient):
            response = self.client.post(
                "/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(_CaptureForwardClient.urls, [f"{proxy.KOBOLD_HOST}/v1/chat/completions"])

    def test_vision_chat_can_use_loaded_kobold_mmproj_without_static_vision_capability(self) -> None:
        chat_profile = AppProfile(
            id="vision-kobold",
            protocols=frozenset({"openai.chat"}),
            backends=frozenset({"kobold"}),
        )
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
            proxy, "effective_llm_model_for_host", AsyncMock(return_value="kobold-vision")
        ), patch.object(proxy.httpx, "AsyncClient", _CaptureForwardClient):
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
        self.assertEqual(_CaptureForwardClient.urls, [f"{proxy.KOBOLD_HOST}/v1/chat/completions"])
        ensure_target.assert_not_called()

    def test_backend_native_v1_model_passthrough_is_not_public(self) -> None:
        unrestricted = AppProfile(id="runtime", protocols=frozenset({"openai"}))

        with authenticated_profile(unrestricted), patch.object(
            proxy.httpx, "AsyncClient", _CaptureForwardClient
        ):
            response = self.client.get("/v1/model", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(_CaptureForwardClient.urls, [])

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
        self.assertIn("comfyui", proxy._PROVIDER_NATIVE_TARGETS)
        self.assertNotIn("openai-proxy", proxy._PROVIDER_NATIVE_TARGETS)

    def test_provider_native_list_projects_profile_policy(self) -> None:
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

        with authenticated_profile(profile), patch.object(
            proxy.httpx,
            "AsyncClient",
            side_effect=blocked,
        ):
            response = self.client.get(
                "/gateway/v1/providers/tabby",
                headers=AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], "tabby")

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
