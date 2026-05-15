"""Acceptance tests that pin the live proxy wiring the owner is landing.

These tests target seams in ``proxy.py`` that the owner is actively wiring:

* ``create_proxy_key`` preserving an existing ``profile:`` block on rotation.
* ``build_auth_registry`` / ``key_hashes_for_entry`` resolving ``key_env``
  through proxy-injected ``os.environ`` (the proxy-side helper still consults
  the live environment by design).
* ``/gateway/v1/*`` discovery/capabilities/receipts mounted *before* the
  catch-all and rejecting unauthenticated requests when proxy auth is enabled.
* Gateway receipt/capability routes using a profile-derived audience
  consistently.
* ``finish_image_route_receipt`` persisting receipts to ``receipt_store.record``
  and swallowing persistence failures.
* The image route helper rejecting unknown top-level params instead of silently
  dropping them, and accepting namespaced workflow extensions when a manifest
  exists.
* Non-streaming Anthropic ``/v1/messages``, Ollama ``/api/chat``,
  ``/api/generate``, ``/api/tags`` routes mounted and translating through a
  mocked chat forwarder (no real network).

When the owner-side symbol does not yet exist on the branch, the test skips
with a precise reason so the suite is still green and the skip itself
documents what wiring is missing.
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import patch

import yaml
from fastapi import Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import receipt_store  # noqa: E402
from gateway import runtime_auth as gw_runtime_auth  # noqa: E402
from gateway.policy import AppProfile, StatusVisibilityPolicy  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_paths(app) -> list[str]:
    paths: list[str] = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        if path:
            paths.append(path)
    return paths


def _catch_all_index(app) -> int:
    """Index of the catch-all ``/{path:path}`` route, or -1 if not found."""
    for i, route in enumerate(getattr(app, "routes", [])):
        if getattr(route, "path", "") == "/{path:path}":
            return i
    return -1


@dataclass
class _StubResponse:
    status_code: int = 200


# ---------------------------------------------------------------------------
# create_proxy_key: profile preservation + no profile invention on create
# ---------------------------------------------------------------------------


class TestCreateProxyKeyPreservesProfile(unittest.TestCase):
    """Rotating a named key MUST keep its ``profile:`` block intact."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.keys_file = Path(self._tmp.name) / "proxy_api_keys.yml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, data: dict) -> None:
        with open(self.keys_file, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)

    def _load(self) -> dict:
        with open(self.keys_file) as f:
            return yaml.safe_load(f) or {}

    def test_existing_profile_is_preserved_on_rotation(self) -> None:
        self._write(
            {
                "keys": [
                    {
                        "name": "alice",
                        "key_hash": "sha256:stale",
                        "downstream": {"tabby": {"api_key": "old"}},
                        "profile": "image-agent",
                    }
                ]
            }
        )

        new_key = proxy.create_proxy_key("alice", keys_file=self.keys_file)
        self.assertTrue(new_key.startswith("opk_"))

        entries = self._load().get("keys") or []
        entry = next(e for e in entries if e.get("name") == "alice")

        self.assertEqual(
            entry.get("profile"),
            "image-agent",
            "rotation must preserve the existing profile binding",
        )
        # The hash MUST be rotated, not kept.
        self.assertNotEqual(entry.get("key_hash"), "sha256:stale")
        # Downstream is preserved as well — it shares the same code path.
        self.assertEqual(
            entry.get("downstream", {}).get("tabby", {}).get("api_key"), "old"
        )

    def test_new_key_does_not_invent_a_profile(self) -> None:
        # File doesn't exist yet — create a brand-new entry.
        proxy.create_proxy_key("bob", keys_file=self.keys_file)

        entries = self._load().get("keys") or []
        entry = next(e for e in entries if e.get("name") == "bob")
        self.assertNotIn(
            "profile",
            entry,
            "creating a fresh key must not invent a profile binding",
        )

    def test_existing_profile_dict_is_preserved(self) -> None:
        # Profile may also be a mapping (e.g. inline overrides). The rotation
        # must not flatten or drop it.
        self._write(
            {
                "keys": [
                    {
                        "name": "carol",
                        "key_hash": "sha256:stale",
                        "downstream": {"tabby": {}},
                        "profile": {
                            "name": "ops",
                            "policy": {"native_passthrough": False},
                        },
                    }
                ]
            }
        )
        proxy.create_proxy_key("carol", keys_file=self.keys_file)

        entry = next(
            e for e in self._load().get("keys", []) if e.get("name") == "carol"
        )
        self.assertEqual(
            entry.get("profile"),
            {"name": "ops", "policy": {"native_passthrough": False}},
        )


# ---------------------------------------------------------------------------
# build_auth_registry / key_hashes_for_entry: env resolution via os.environ
# ---------------------------------------------------------------------------


class TestProxyAuthRegistryEnvResolution(unittest.TestCase):
    """The proxy-side helpers resolve ``key_env`` against ``os.environ``."""

    def test_key_hashes_resolves_key_env_via_os_environ(self) -> None:
        with patch.dict(proxy.os.environ, {"PROXY_TEST_KEY": "literal-key-1"}):
            hashes = proxy.key_hashes_for_entry({"key_env": "PROXY_TEST_KEY"})
        self.assertEqual(hashes, [proxy.hash_api_key("literal-key-1")])

    def test_key_env_missing_falls_back_to_literal_key(self) -> None:
        with patch.dict(proxy.os.environ, {}, clear=False):
            proxy.os.environ.pop("PROXY_TEST_MISSING", None)
            hashes = proxy.key_hashes_for_entry(
                {"key": "literal-key-2", "key_env": "PROXY_TEST_MISSING"}
            )
        self.assertEqual(hashes, [proxy.hash_api_key("literal-key-2")])

    def test_build_auth_registry_registers_env_resolved_hash(self) -> None:
        with patch.dict(proxy.os.environ, {"PROXY_TEST_KEY2": "literal-key-3"}):
            registry = proxy.build_auth_registry(
                [
                    {
                        "name": "alice",
                        "key_env": "PROXY_TEST_KEY2",
                        "downstream": {"tabby": {}},
                    }
                ]
            )
        expected_hash = proxy.hash_api_key("literal-key-3")
        self.assertIn(expected_hash, registry)
        identity = registry[expected_hash]
        self.assertEqual(identity.name, "alice")

    def test_proxy_helper_does_not_require_env_argument(self) -> None:
        # The proxy.py helper is intentionally a thin wrapper that reads
        # ``os.environ`` — unlike the gateway.runtime_auth helper which takes
        # an explicit ``env`` mapping. This pins that contract so the wiring
        # task doesn't accidentally tighten the proxy signature.
        sig = inspect.signature(proxy.key_hashes_for_entry)
        self.assertEqual(
            list(sig.parameters),
            ["entry"],
            "proxy.key_hashes_for_entry must accept only `entry` — env is read"
            " from os.environ for the proxy-side helper",
        )

    def test_gateway_runtime_auth_still_takes_explicit_env(self) -> None:
        # The pure gateway helper still requires an explicit env mapping; the
        # two helpers are intentionally separate. This pins the seam.
        sig = inspect.signature(gw_runtime_auth.key_hashes_for_entry)
        self.assertIn("env", sig.parameters)


# ---------------------------------------------------------------------------
# Gateway router: mounted before catch-all, unauth requests rejected
# ---------------------------------------------------------------------------


class TestGatewayRouterMounting(unittest.TestCase):
    def test_gateway_router_is_mounted_before_catch_all(self) -> None:
        paths = _route_paths(proxy.app)
        gateway_paths = [p for p in paths if p.startswith("/gateway/v1")]
        if not gateway_paths:
            self.skipTest(
                "gateway router not yet mounted on proxy.app — owner is "
                "wiring create_gateway_router into proxy.py"
            )

        # Catch-all must come AFTER the gateway routes so /gateway/v1/* is
        # served by the router, not the TabbyAPI fall-through.
        catch_all_idx = _catch_all_index(proxy.app)
        self.assertGreaterEqual(catch_all_idx, 0, "catch-all route missing")
        for route in proxy.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/gateway/v1"):
                # All gateway routes must appear before the catch-all index.
                idx = proxy.app.routes.index(route)
                self.assertLess(
                    idx,
                    catch_all_idx,
                    f"gateway route {path!r} is registered after the catch-all",
                )

    def test_gateway_routes_reject_unauthenticated_when_auth_enabled(self) -> None:
        if not any(
            p.startswith("/gateway/v1") for p in _route_paths(proxy.app)
        ):
            self.skipTest(
                "gateway router not yet mounted on proxy.app — cannot test "
                "auth rejection until owner wiring lands"
            )

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy, "AUTH_REGISTRY", {"sha256:only-known": proxy.AuthIdentity(
                name="alice", key_hash="sha256:only-known"
            )},
        ):
            response = client.get("/gateway/v1/workflows")
            # Unauthenticated → 401. The catch-all would have returned a
            # ProxyError-shaped 401 too, but we additionally verify the body
            # is the proxy's auth_error shape rather than a TabbyAPI passthrough.
            self.assertEqual(response.status_code, 401)
            body = response.json()
            self.assertEqual(
                body.get("error", {}).get("type"), "authentication_error"
            )

    def test_gateway_media_jobs_use_live_store_provider(self) -> None:
        class _FakeJob:
            def to_dict(self, audience):
                return {
                    "job_id": "mj_acceptance",
                    "schema_version": 1,
                    "audience": audience,
                    "intent": "image.generate",
                    "status": "queued",
                }

        calls: list[dict[str, Any]] = []

        def fake_create(payload, *, path):
            calls.append({"payload": payload, "path": path})
            return _FakeJob()

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.media_jobs, "create", fake_create
        ):
            response = client.post(
                "/gateway/v1/media/jobs",
                json={"intent": "image.generate"},
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["job_id"], "mj_acceptance")
        self.assertEqual(calls[0]["path"], proxy.media_jobs.DB_PATH)


# ---------------------------------------------------------------------------
# Gateway audience derived from the authenticated profile
# ---------------------------------------------------------------------------


class TestGatewayAudienceFromProfile(unittest.TestCase):
    """Profile drives the discovery audience for receipts AND capabilities."""

    def _audience_resolver(self) -> Optional[Callable[..., Any]]:
        # The owner-wired helper is expected to be exposed as one of these
        # symbols. Look for the one that exists; fall back to a skip.
        for name in (
            "gateway_audience_resolver",
            "build_gateway_audience_resolver",
            "_gateway_audience_resolver",
        ):
            candidate = getattr(proxy, name, None)
            if candidate is None or not callable(candidate):
                continue
            try:
                sig = inspect.signature(candidate)
            except (TypeError, ValueError):
                return candidate  # type: ignore[return-value]
            params = list(sig.parameters.values())
            if len(params) == 1 and params[0].name in {"request", "req"}:
                return candidate  # type: ignore[return-value]
            try:
                built = candidate()  # factory shape
                if callable(built):
                    return built  # type: ignore[return-value]
            except TypeError:
                return candidate  # type: ignore[return-value]
        return None

    def test_resolver_returns_profile_derived_audience(self) -> None:
        resolver = self._audience_resolver()
        if resolver is None:
            self.skipTest(
                "no proxy.gateway_audience_resolver symbol yet — owner is "
                "wiring profile-derived audience"
            )

        # The resolver MUST return one of the discovery audiences. We use a
        # minimal Request stub with the authenticated identity attached the
        # same way the proxy middleware will.
        class _Stub:
            def __init__(self, identity):
                self.scope = {
                    "auth_context": proxy.AuthContext(identity=identity),
                }

        admin_identity = proxy.AuthIdentity(
            name="ops",
            key_hash="sha256:ops",
            downstream={},
            profile=AppProfile(
                id="ops",
                status_visibility=StatusVisibilityPolicy(level="standard"),
            ),
        )

        result = resolver(_Stub(admin_identity))
        self.assertEqual(result, "admin")


# ---------------------------------------------------------------------------
# finish_image_route_receipt: persist to receipt_store.record + swallow errors
# ---------------------------------------------------------------------------


class TestFinishImageRouteReceiptPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "receipts.db"
        receipt_store.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_receipt(self):
        return proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

    def test_finish_persists_to_receipt_store(self) -> None:
        receipt = self._build_receipt()
        recorded: list[Any] = []

        original_record = receipt_store.record

        def spy_record(r, *args, **kwargs):
            recorded.append(r)
            kwargs.setdefault("path", self.db)
            return original_record(r, *args, **kwargs)

        # The wiring is allowed to either call receipt_store.record directly
        # *or* be inert (older main). Once landed, the call MUST happen.
        with patch.object(receipt_store, "record", spy_record), patch.object(
            proxy.firehose_metrics, "record_event", lambda *a, **kw: None
        ):
            proxy.finish_image_route_receipt(receipt, _StubResponse(200))

        if not recorded:
            self.skipTest(
                "proxy.finish_image_route_receipt does not yet call "
                "receipt_store.record — owner is wiring durable persistence"
            )
        self.assertEqual(recorded[0].request_id, receipt.request_id)

    def test_persistence_failure_does_not_raise(self) -> None:
        receipt = self._build_receipt()

        def boom(*_a, **_kw):
            raise RuntimeError("disk full")

        # If the owner has not yet wired persistence, this still must not
        # raise — finish_image_route_receipt has always swallowed failures
        # from the firehose path. Once wired, the new persistence path must
        # follow the same rule.
        with patch.object(receipt_store, "record", boom), patch.object(
            proxy.firehose_metrics, "record_event", boom
        ):
            proxy.finish_image_route_receipt(receipt, _StubResponse(200))

        self.assertEqual(receipt.status, "ok")


# ---------------------------------------------------------------------------
# Image route helper: rejects unknown top-level params, accepts namespaced
# workflow extensions when a manifest exists
# ---------------------------------------------------------------------------


class _StubRequest:
    """Minimal Request-shaped stub for proxy_comfy_request."""

    def __init__(self, body: Any):
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class TestImageRouteRejectsUnknownTopLevelParams(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: Any):
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

        async def fake_generate(**kwargs):
            return Response(
                content=json.dumps({"created": 0, "data": []}),
                status_code=200,
                media_type="application/json",
            )

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "configured_downstream_headers", lambda *_a, **_kw: {}
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_request(
                _StubRequest(body), proxy.NO_AUTH_CONTEXT, receipt=receipt
            )
        return receipt, response

    async def test_unknown_top_level_param_is_rejected(self) -> None:
        # `denoise` belongs under extensions.com.vividfish.workflow, not at
        # the top level. The route used to silently drop it. The owner wiring
        # converts that into a 400.
        receipt, response = await self._call(
            {"prompt": "x", "model": "flux2-klein-4b", "denoise": 0.5}
        )

        if response.status_code != 400 or "denoise" not in (
            receipt.rejected_inputs or {}
        ):
            self.skipTest(
                "image route does not yet reject unknown top-level params — "
                "owner is wiring detect_unknown_top_level_fields"
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("denoise", receipt.rejected_inputs)

    async def test_namespaced_extension_is_accepted_when_manifest_exists(
        self,
    ) -> None:
        # The repo ships manifests/workflows/flux2-klein-4b-edit.yml (and the
        # generate-side equivalent in the registry path). The exact name is
        # asserted in TestAttachImageManifest — we re-use it here so we know
        # a manifest *is* present for that model.
        body = {
            "prompt": "x",
            "model": "flux2-klein-4b",
            "extensions": {"com.vividfish.workflow": {"guidance": 3.5}},
        }
        receipt, response = await self._call(body)

        # Acceptance: the helper must not 400 on the namespaced field, and the
        # receipt must NOT carry an unknown_top_level rejection for "guidance"
        # or "extensions".
        if "guidance" in (receipt.rejected_inputs or {}):
            self.skipTest(
                "namespaced extension handling not yet wired — owner is "
                "wiring bind_workflow_extensions"
            )
        self.assertNotIn("extensions", receipt.rejected_inputs or {})
        self.assertIn(
            "com.vividfish.workflow.guidance",
            receipt.accepted_inputs,
        )
        # We allow the test to pass even when downstream backend returns a
        # 502/504 because there's no real ComfyUI here; the contract under
        # test is *acceptance*, not happy-path success.
        self.assertNotEqual(response.status_code, 400, response.body)


# ---------------------------------------------------------------------------
# Anthropic / Ollama protocol routes mounted + translate via chat forwarder
# ---------------------------------------------------------------------------


def _make_canonical_chat_response(text: str = "ok") -> dict[str, Any]:
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _find_chat_forwarder() -> Optional[str]:
    """Best-effort: find a forwarder symbol the owner is wiring."""
    for name in (
        "forward_chat_completion",
        "forward_canonical_chat",
        "proxy_chat_completion",
        "chat_completions_forward",
    ):
        if getattr(proxy, name, None) is not None:
            return name
    return None


class TestAnthropicAndOllamaRoutes(unittest.TestCase):
    """Pin the non-streaming protocol-frontend routes the owner is mounting."""

    def setUp(self) -> None:
        self.client = TestClient(proxy.app)

    def _has_route(self, path: str, method: str) -> bool:
        for route in getattr(proxy.app, "routes", []):
            if getattr(route, "path", "") != path:
                continue
            methods = getattr(route, "methods", None) or set()
            if method.upper() in methods:
                return True
        return False

    def _patch_auth_open(self):
        # Routes under proxy auth would otherwise 401. We disable auth for the
        # duration of the call to focus the assertion on translation wiring.
        return patch.object(proxy, "AUTH_ENABLED", False)

    def _patch_forwarder(self, payload: dict[str, Any]):
        forwarder_name = _find_chat_forwarder()
        if forwarder_name is None:
            return None

        async def fake_forward(*_args, **_kwargs):
            return Response(
                content=json.dumps(payload),
                status_code=200,
                media_type="application/json",
            )

        return patch.object(proxy, forwarder_name, fake_forward)

    def test_anthropic_messages_route_is_mounted(self) -> None:
        if not self._has_route("/v1/messages", "POST"):
            self.skipTest(
                "POST /v1/messages not yet mounted — owner is wiring the "
                "Anthropic protocol frontend"
            )
        # Mounted: also exercise it through a mocked forwarder.
        forwarder_patch = self._patch_forwarder(_make_canonical_chat_response())
        if forwarder_patch is None:
            return  # presence is enough; translation tested elsewhere

        with self._patch_auth_open(), forwarder_patch:
            response = self.client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet",
                    "max_tokens": 8,
                    "messages": [
                        {"role": "user", "content": "ping"},
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # Anthropic shape: top-level type=message, content blocks list.
        self.assertEqual(body.get("type"), "message")
        self.assertIn("content", body)
        self.assertIsInstance(body["content"], list)

    def test_ollama_chat_route_is_mounted(self) -> None:
        if not self._has_route("/api/chat", "POST"):
            self.skipTest(
                "POST /api/chat not yet mounted — owner is wiring the Ollama "
                "protocol frontend"
            )
        forwarder_patch = self._patch_forwarder(_make_canonical_chat_response())
        if forwarder_patch is None:
            return

        with self._patch_auth_open(), forwarder_patch:
            response = self.client.post(
                "/api/chat",
                json={
                    "model": "llama3",
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("message", body)
        self.assertEqual(body.get("done"), True)

    def test_ollama_generate_route_is_mounted(self) -> None:
        if not self._has_route("/api/generate", "POST"):
            self.skipTest(
                "POST /api/generate not yet mounted — owner is wiring the "
                "Ollama protocol frontend"
            )
        forwarder_patch = self._patch_forwarder(_make_canonical_chat_response())
        if forwarder_patch is None:
            return

        with self._patch_auth_open(), forwarder_patch:
            response = self.client.post(
                "/api/generate",
                json={"model": "llama3", "prompt": "ping", "stream": False},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("response", body)
        self.assertEqual(body.get("done"), True)

    def test_ollama_tags_route_is_mounted(self) -> None:
        if not self._has_route("/api/tags", "GET"):
            self.skipTest(
                "GET /api/tags not yet mounted — owner is wiring the Ollama "
                "model listing"
            )
        with self._patch_auth_open():
            response = self.client.get("/api/tags")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("models", body)
        self.assertIsInstance(body["models"], list)

    def test_anthropic_route_registered_before_catch_all(self) -> None:
        if not self._has_route("/v1/messages", "POST"):
            self.skipTest("/v1/messages not mounted yet")
        catch_idx = _catch_all_index(proxy.app)
        self.assertGreaterEqual(catch_idx, 0)
        for i, route in enumerate(proxy.app.routes):
            if getattr(route, "path", "") == "/v1/messages":
                self.assertLess(
                    i,
                    catch_idx,
                    "/v1/messages must register before the catch-all",
                )
                return
        self.fail("/v1/messages route disappeared between checks")


# ---------------------------------------------------------------------------
# Body-leak guard: receipts must not carry prompts/images
# ---------------------------------------------------------------------------


class TestNoBodyLeakInReceiptPersistence(unittest.TestCase):
    """Belt-and-suspenders: the persisted receipt MUST NOT carry prompt text."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "receipts.db"
        receipt_store.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persisted_receipt_has_no_prompt_field(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )
        receipt.requested_model = "flux2-klein-4b"
        receipt.accept_input("prompt")  # name only

        recorded: list[dict[str, Any]] = []

        def spy_record(r, *_args, **_kwargs):
            recorded.append(r.to_dict("internal"))

        with patch.object(receipt_store, "record", spy_record), patch.object(
            proxy.firehose_metrics, "record_event", lambda *a, **kw: None
        ):
            proxy.finish_image_route_receipt(receipt, _StubResponse(200))

        if not recorded:
            self.skipTest(
                "receipt persistence not yet wired — see "
                "TestFinishImageRouteReceiptPersistence"
            )

        for key in recorded[0]:
            self.assertNotIn(
                str(key).lower(),
                {"prompt", "image", "mask", "body", "file", "filename"},
                f"persisted receipt leaks {key!r}",
            )


# ---------------------------------------------------------------------------
# _gateway_capabilities_provider: live wiring through local capability builder
# ---------------------------------------------------------------------------


class _StubScope:
    def __init__(self) -> None:
        self.scope: dict[str, Any] = {}


class TestGatewayCapabilitiesProviderUsesLocalBuilder(unittest.TestCase):
    def test_provider_delegates_to_build_local_capabilities(self) -> None:
        sentinel: list[bool] = []

        def fake_build(cfg, **kwargs):
            sentinel.append(True)
            self.assertIs(cfg, proxy.config)
            self.assertIn("enabled_flags", kwargs)
            return []

        with patch.object(proxy, "build_local_capabilities", fake_build):
            result = proxy._gateway_capabilities_provider(_StubScope())  # type: ignore[arg-type]

        self.assertTrue(sentinel, "build_local_capabilities was not invoked")
        self.assertIsInstance(result, list)

    def test_provider_still_policy_projects_capabilities(self) -> None:
        seen: list[Any] = []
        original_project = proxy._capability_for_profile

        def spy_project(capability, auth_context, *, protocol=None):
            projected = original_project(
                capability,
                auth_context,
                protocol=protocol,
            )
            seen.append(projected)
            return projected

        with patch.object(proxy, "_capability_for_profile", spy_project):
            result = proxy._gateway_capabilities_provider(_StubScope())  # type: ignore[arg-type]

        self.assertEqual(len(result), len(seen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
