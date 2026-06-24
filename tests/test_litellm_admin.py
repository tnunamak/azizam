"""Unit tests for the thin LiteLLM virtual-key admin client.

These pin the transport contract against the sidecar /key/* API: master-key
auth header, request shapes, response normalization, and the typed error
mapping the admin endpoints rely on. httpx is faked so no sidecar is required.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.backends.llm_execution import LiteLlmRuntimeConfig  # noqa: E402
from gateway.backends.litellm_admin import (  # noqa: E402
    LiteLLMAdminError,
    LiteLLMKeyAdminClient,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload, *, raw: bytes | None = None):
        self.status_code = status_code
        if raw is not None:
            self.content = raw
            self._payload = None
        else:
            body = json.dumps(payload).encode("utf-8")
            self.content = body
            self._payload = payload

    def json(self):
        if self._payload is None:
            return json.loads(self.content.decode("utf-8"))
        return self._payload


class _FakeAsyncClient:
    """Captures the request and returns a queued response."""

    captured: list[dict] = []

    def __init__(self, response, *, error: Exception | None = None):
        self._response = response
        self._error = error

    def __call__(self, *args, **kwargs):
        # httpx.AsyncClient(timeout=...) -> instance; our patch target is the
        # class, so calling it returns self.
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, *, headers=None, content=None):
        type(self).captured.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "content": content,
            }
        )
        if self._error is not None:
            raise self._error
        return self._response


def _client(*, enabled=True, base_url="http://127.0.0.1:4011", api_key="sk-master"):
    cfg = LiteLlmRuntimeConfig(
        enabled=enabled, base_url=base_url, api_key=api_key, service_id="litellm"
    )
    return LiteLLMKeyAdminClient(cfg)


def _patch_httpx(response=None, *, error=None):
    fake = _FakeAsyncClient(response, error=error)
    _FakeAsyncClient.captured = []
    return patch("gateway.backends.litellm_admin.httpx.AsyncClient", fake)


class LiteLLMKeyAdminClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_keys_sends_master_key_and_normalizes(self) -> None:
        resp = _FakeResponse(
            200,
            {"keys": [{"key_name": "sk-...abcd", "key_alias": "batch", "spend": 1.5}]},
        )
        with _patch_httpx(resp):
            keys = await _client().list_keys()
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["key_alias"], "batch")
        req = _FakeAsyncClient.captured[0]
        self.assertEqual(req["method"], "GET")
        self.assertIn("/key/list", req["url"])
        self.assertEqual(req["headers"]["X-Litellm-Key"], "Bearer sk-master")

    async def test_list_keys_accepts_bare_list(self) -> None:
        resp = _FakeResponse(200, [{"key_alias": "x"}])
        with _patch_httpx(resp):
            keys = await _client().list_keys()
        self.assertEqual(keys[0]["key_alias"], "x")

    async def test_list_keys_coerces_string_rows(self) -> None:
        resp = _FakeResponse(200, {"keys": ["sk-abc123"]})
        with _patch_httpx(resp):
            keys = await _client().list_keys()
        self.assertEqual(keys[0]["token"], "sk-abc123")

    async def test_generate_key_returns_secret_and_posts_spec(self) -> None:
        resp = _FakeResponse(200, {"key": "sk-new-secret", "key_alias": "batch"})
        with _patch_httpx(resp):
            created = await _client().generate_key({"key_alias": "batch", "max_budget": 5.0})
        self.assertEqual(created["key"], "sk-new-secret")
        req = _FakeAsyncClient.captured[0]
        self.assertEqual(req["method"], "POST")
        self.assertIn("/key/generate", req["url"])
        sent = json.loads(req["content"].decode("utf-8"))
        self.assertEqual(sent["key_alias"], "batch")
        self.assertEqual(sent["max_budget"], 5.0)

    async def test_generate_key_without_key_field_is_error(self) -> None:
        resp = _FakeResponse(200, {"key_alias": "batch"})  # no "key"
        with _patch_httpx(resp):
            with self.assertRaises(LiteLLMAdminError) as ctx:
                await _client().generate_key({"key_alias": "batch"})
        self.assertEqual(ctx.exception.code, "litellm_bad_response")

    async def test_delete_keys_posts_keys(self) -> None:
        resp = _FakeResponse(200, {"deleted_keys": ["sk-1"]})
        with _patch_httpx(resp):
            result = await _client().delete_keys(keys=["sk-1"])
        self.assertEqual(result["deleted_keys"], ["sk-1"])
        sent = json.loads(_FakeAsyncClient.captured[0]["content"].decode("utf-8"))
        self.assertEqual(sent["keys"], ["sk-1"])

    async def test_delete_without_keys_or_aliases_raises(self) -> None:
        # No transport call should happen.
        with self.assertRaises(LiteLLMAdminError) as ctx:
            await _client().delete_keys()
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_disabled_substrate_raises_unconfigured(self) -> None:
        with self.assertRaises(LiteLLMAdminError) as ctx:
            await _client(enabled=False).list_keys()
        self.assertEqual(ctx.exception.code, "litellm_unconfigured")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_missing_master_key_raises_unconfigured_without_transport(self) -> None:
        # A base_url is wired but no master key is configured. The /key/* API is
        # master-key protected, so the client must fail with an actionable
        # "unconfigured" error BEFORE building an illegal "Bearer " (empty token)
        # header that httpx would reject as a transport error -- which used to
        # surface to the operator as a confusing "unreachable" 503. No transport
        # call should be attempted, so we leave httpx unpatched.
        with self.assertRaises(LiteLLMAdminError) as ctx:
            await _client(api_key="").list_keys()
        self.assertEqual(ctx.exception.code, "litellm_unconfigured")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("master key", ctx.exception.message.lower())

    async def test_unreachable_sidecar_maps_to_503(self) -> None:
        import httpx

        with _patch_httpx(None, error=httpx.ConnectError("refused")):
            with self.assertRaises(LiteLLMAdminError) as ctx:
                await _client().list_keys()
        self.assertEqual(ctx.exception.code, "litellm_unreachable")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_master_key_rejected_maps_to_auth_failed(self) -> None:
        resp = _FakeResponse(401, {"error": {"message": "bad key"}})
        with _patch_httpx(resp):
            with self.assertRaises(LiteLLMAdminError) as ctx:
                await _client().list_keys()
        self.assertEqual(ctx.exception.code, "litellm_admin_auth_failed")
        self.assertEqual(ctx.exception.upstream_status, 401)

    async def test_upstream_error_surfaces_message(self) -> None:
        resp = _FakeResponse(400, {"error": {"message": "no such team"}})
        with _patch_httpx(resp):
            with self.assertRaises(LiteLLMAdminError) as ctx:
                await _client().generate_key({"team_id": "ghost"})
        self.assertEqual(ctx.exception.code, "litellm_key_api_error")
        self.assertIn("no such team", ctx.exception.message)

    async def test_enabled_property_requires_base_url(self) -> None:
        self.assertFalse(_client(base_url="").enabled)
        self.assertTrue(_client().enabled)


if __name__ == "__main__":
    unittest.main()
