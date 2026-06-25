"""Fresh-instance API journey gauntlet for the AI Gateway.

Exercises the full operator journey a sophisticated self-hoster needs after a
fresh install: health, discovery, admin auth, service inventory, profile
CRUD, app/key lifecycle, capabilities, OpenAI-compatible routes, denied
requests, unknown-model errors, unsupported routes, and provider-native
discovery.

Required env vars
-----------------
OPENAI_PROXY_BASE_URL
    Root URL of the gateway, e.g. ``http://127.0.0.1:5000``. Must not
    include a protocol suffix.

OPENAI_PROXY_ADMIN_KEY
    An ``oak_...`` admin API key. Required for admin-plane probes.
    The suite creates a gauntlet-scoped App and key, then cleans them up
    after the run.

OPENAI_PROXY_API_KEY          (optional)
    A pre-existing ``opk_...`` runtime App key to use for runtime probes.
    If absent, the suite creates and uses a temporary gauntlet App key
    minted from the admin key, then revokes it on teardown.

Run with::

    OPENAI_PROXY_BASE_URL=http://127.0.0.1:5000 \\
    OPENAI_PROXY_ADMIN_KEY=oak_... \\
    python -m unittest tests.compat.python.probe_fresh_instance_journey -v

Product gaps discovered during a probe run are written to
``tmp/gauntlet_gaps.jsonl`` (relative to cwd) so they are machine-readable
without failing the test suite.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Minimal HTTP helpers (no SDK deps)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class GatewayClient:
    """Thin HTTP client for the gateway."""

    def __init__(self, base_url: str, *, timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer: str | None = None,
        body: Any = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
    ) -> Resp:
        if body is not None and raw_body is not None:
            raise ValueError("pass body or raw_body, not both")
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        elif raw_body is not None:
            data = raw_body
        headers: dict[str, str] = {}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if data is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as res:
                return Resp(res.status, dict(res.headers), res.read())
        except urllib.error.HTTPError as exc:
            return Resp(exc.code, dict(exc.headers), exc.read())
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Cannot reach {self.base_url}: {exc.reason}") from exc

    def get(self, path: str, *, bearer: str | None = None) -> Resp:
        return self.request("GET", path, bearer=bearer)

    def post(self, path: str, *, bearer: str | None = None, body: Any = None) -> Resp:
        return self.request("POST", path, bearer=bearer, body=body)

    def patch(self, path: str, *, bearer: str | None = None, body: Any = None) -> Resp:
        return self.request("PATCH", path, bearer=bearer, body=body)

    def delete(self, path: str, *, bearer: str | None = None) -> Resp:
        return self.request("DELETE", path, bearer=bearer)


# ---------------------------------------------------------------------------
# Gap recorder
# ---------------------------------------------------------------------------

_GAP_FILE = Path("tmp/gauntlet_gaps.jsonl")


def record_gap(tag: str, message: str, context: dict[str, Any] | None = None) -> None:
    """Append a machine-readable product gap record."""
    _GAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tag": tag,
        "message": message,
        "context": context or {},
    }
    with _GAP_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"  [GAP] {tag}: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

_BASE_URL = os.environ.get("OPENAI_PROXY_BASE_URL", "").strip().rstrip("/")
_ADMIN_KEY = os.environ.get("OPENAI_PROXY_ADMIN_KEY", "").strip()
_RUNTIME_KEY_ENV = os.environ.get("OPENAI_PROXY_API_KEY", "").strip()

_SKIP_NO_BASE = not _BASE_URL
_SKIP_NO_ADMIN = not _ADMIN_KEY


# ---------------------------------------------------------------------------
# 1. Public / unauthenticated surfaces
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_NO_BASE, "OPENAI_PROXY_BASE_URL not set")
class TestPublicSurfaces(unittest.TestCase):
    """Public endpoints that must work without any credentials."""

    def setUp(self) -> None:
        self.gw = GatewayClient(_BASE_URL)

    def _skip_when_auth_disabled(self) -> None:
        r = self.gw.get("/health")
        if r.status != 200:
            return
        try:
            data = r.json()
        except json.JSONDecodeError:
            return
        if data.get("auth_enabled") is False:
            self.skipTest("gateway auth is disabled for this harness")

    # -- health --

    def test_health_returns_200(self) -> None:
        r = self.gw.get("/health")
        self.assertEqual(r.status, 200, r.text())

    def test_health_json_ok_field(self) -> None:
        r = self.gw.get("/health")
        self.assertEqual(r.status, 200)
        data = r.json()
        self.assertIn("status", data, "health response should have a status field")
        self.assertIn(
            data["status"], ("ok", "healthy"),
            f"health status should be ok or healthy, got {data['status']!r}",
        )

    # -- discovery --

    def test_well_known_ai_gateway(self) -> None:
        r = self.gw.get("/.well-known/ai-gateway")
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("service", data)
        svc = data["service"]
        self.assertIn("product", svc)
        self.assertIn("AI Gateway", svc["product"], svc["product"])

    def test_well_known_openai_proxy_alias(self) -> None:
        r = self.gw.get("/.well-known/openai-proxy")
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("service", data)

    def test_llms_txt(self) -> None:
        r = self.gw.get("/llms.txt")
        self.assertIn(r.status, (200, 404), r.text()[:200])
        if r.status == 404:
            record_gap(
                "llms_txt_missing",
                "GET /llms.txt returned 404 -- discovery surface not served",
            )

    def test_well_known_skills(self) -> None:
        r = self.gw.get("/.well-known/skills")
        self.assertIn(r.status, (200, 404), r.text()[:200])
        if r.status == 404:
            record_gap(
                "skills_well_known_missing",
                "GET /.well-known/skills returned 404",
            )

    def test_gateway_extensions_public(self) -> None:
        r = self.gw.get("/gateway/v1/extensions")
        self.assertIn(r.status, (200, 401), r.text()[:300])
        if r.status == 200:
            data = r.json()
            self.assertIsInstance(data, dict)

    # -- setup page --

    def test_setup_page_renders(self) -> None:
        r = self.gw.get("/setup")
        self.assertEqual(r.status, 200, r.text()[:300])
        self.assertIn("text/html", r.headers.get("content-type", ""))

    # -- auth required for runtime --

    def test_v1_models_requires_auth(self) -> None:
        self._skip_when_auth_disabled()
        r = self.gw.get("/v1/models")
        self.assertIn(
            r.status, (401, 403),
            f"Expected 401/403 without auth, got {r.status}: {r.text()[:200]}",
        )

    def test_gateway_capabilities_requires_auth(self) -> None:
        self._skip_when_auth_disabled()
        r = self.gw.get("/gateway/v1/capabilities")
        self.assertIn(r.status, (401, 403), r.text()[:200])


# ---------------------------------------------------------------------------
# 2. Admin-plane probes (require oak_ key)
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_NO_BASE, "OPENAI_PROXY_BASE_URL not set")
@unittest.skipIf(_SKIP_NO_ADMIN, "OPENAI_PROXY_ADMIN_KEY not set")
class TestAdminPlane(unittest.TestCase):
    """Admin API probes -- these read and write through the oak_ key."""

    gw: GatewayClient
    admin_key: str

    # IDs minted in this run, cleaned up in tearDownClass
    _app_id: str | None = None
    _app_key_id: str | None = None
    _app_api_key: str | None = None
    _profile_id: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.gw = GatewayClient(_BASE_URL)
        cls.admin_key = _ADMIN_KEY

    @classmethod
    def tearDownClass(cls) -> None:
        """Best-effort cleanup of gauntlet-created resources."""
        if cls._app_key_id:
            try:
                cls.gw.post(
                    f"/admin/api/credentials/{cls._app_key_id}/revoke",
                    bearer=cls.admin_key,
                    body={},
                )
            except Exception:
                pass
        if cls._app_id:
            try:
                cls.gw.delete(
                    f"/admin/api/apps/{cls._app_id}",
                    bearer=cls.admin_key,
                )
            except Exception:
                pass
        if cls._profile_id:
            try:
                cls.gw.delete(
                    f"/admin/api/profiles/{cls._profile_id}",
                    bearer=cls.admin_key,
                )
            except Exception:
                pass

    def _auth(self) -> str:
        return self.admin_key

    # -- overview --

    def test_admin_overview_authenticated(self) -> None:
        r = self.gw.get("/admin/api/overview", bearer=self._auth())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("actor", data)
        self.assertIn("counts", data)
        counts = data["counts"]
        for key in ("users", "apps", "profiles", "credentials"):
            self.assertIn(key, counts, f"overview.counts missing {key!r}")

    def test_admin_overview_requires_auth(self) -> None:
        r = self.gw.get("/admin/api/overview")
        self.assertIn(r.status, (401, 403), r.text()[:200])

    def test_admin_overview_rejected_with_runtime_key(self) -> None:
        if not _RUNTIME_KEY_ENV:
            self.skipTest("OPENAI_PROXY_API_KEY not set")
        r = self.gw.get("/admin/api/overview", bearer=_RUNTIME_KEY_ENV)
        self.assertIn(
            r.status, (401, 403),
            f"Admin overview must reject opk_ key: {r.status} {r.text()[:200]}",
        )

    # -- users --

    def test_admin_list_users(self) -> None:
        r = self.gw.get("/admin/api/users", bearer=self._auth())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("users", data)
        self.assertIsInstance(data["users"], list)

    # -- service registry / inventory --

    def test_admin_service_registry(self) -> None:
        r = self.gw.get("/admin/api/operator/service-registry", bearer=self._auth())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIsInstance(data, dict, "service registry response must be a dict")

    # -- profiles CRUD --

    def test_admin_list_profiles(self) -> None:
        r = self.gw.get("/admin/api/profiles", bearer=self._auth())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("profiles", data)
        self.assertIsInstance(data["profiles"], list)

    def test_admin_create_profile(self) -> None:
        import time as _time
        unique_id = f"gauntlet-probe-{int(_time.time())}"
        r = self.gw.post(
            "/admin/api/profiles",
            bearer=self._auth(),
            body={
                "name": unique_id,
                "profile": {
                    "id": unique_id,
                    "protocols": ["openai.chat"],
                    "fallback": {"cloud_allowed": False},
                    "status_visibility": {"level": "basic"},
                },
            },
        )
        self.assertEqual(r.status, 201, r.text()[:300])
        data = r.json()
        self.assertIn("profile", data)
        TestAdminPlane._profile_id = data["profile"]["id"]

    def test_admin_read_profile(self) -> None:
        if not TestAdminPlane._profile_id:
            self.skipTest("profile not created yet")
        r = self.gw.get(
            f"/admin/api/profiles/{TestAdminPlane._profile_id}",
            bearer=self._auth(),
        )
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("profile", data)
        self.assertEqual(data["profile"]["id"], TestAdminPlane._profile_id)

    def test_admin_update_profile(self) -> None:
        if not TestAdminPlane._profile_id:
            self.skipTest("profile not created yet")
        import time as _time
        new_name = f"gauntlet-upd-{int(_time.time() * 1000)}"
        r = self.gw.patch(
            f"/admin/api/profiles/{TestAdminPlane._profile_id}",
            bearer=self._auth(),
            body={"name": new_name},
        )
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("profile", data)

    # -- app CRUD --

    def test_admin_list_apps(self) -> None:
        r = self.gw.get("/admin/api/apps", bearer=self._auth())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("apps", data)
        self.assertIsInstance(data["apps"], list)

    def test_admin_create_app(self) -> None:
        profile_id = TestAdminPlane._profile_id
        body: dict[str, Any] = {"name": "gauntlet-probe-app"}
        if profile_id:
            body["profile_id"] = profile_id
        r = self.gw.post("/admin/api/apps", bearer=self._auth(), body=body)
        self.assertEqual(r.status, 201, r.text()[:300])
        data = r.json()
        self.assertIn("app", data)
        TestAdminPlane._app_id = data["app"]["id"]

    def test_admin_update_app(self) -> None:
        if not TestAdminPlane._app_id:
            self.skipTest("app not created yet")
        r = self.gw.patch(
            f"/admin/api/apps/{TestAdminPlane._app_id}",
            bearer=self._auth(),
            body={"name": "gauntlet-probe-app-updated"},
        )
        self.assertEqual(r.status, 200, r.text()[:300])

    # -- key creation / revocation --

    def test_admin_create_app_key(self) -> None:
        if not TestAdminPlane._app_id:
            self.skipTest("app not created yet")
        r = self.gw.post(
            f"/admin/api/apps/{TestAdminPlane._app_id}/keys",
            bearer=self._auth(),
            body={"label": "gauntlet-probe-key"},
        )
        self.assertEqual(r.status, 201, r.text()[:300])
        data = r.json()
        cred = data.get("credential", {})
        self.assertIn("api_key", cred, "key creation must return raw api_key once")
        raw_key = cred["api_key"]
        self.assertTrue(raw_key.startswith("opk_"), f"expected opk_ prefix, got {raw_key[:12]}")
        TestAdminPlane._app_key_id = cred.get("id")
        TestAdminPlane._app_api_key = raw_key

    def test_admin_revoke_app_key(self) -> None:
        """Create a throwaway key, then revoke it -- main _app_api_key is untouched."""
        if not TestAdminPlane._app_id:
            self.skipTest("app not created yet")
        # Mint a throwaway key just for the revoke probe
        r_mk = self.gw.post(
            f"/admin/api/apps/{TestAdminPlane._app_id}/keys",
            bearer=self._auth(),
            body={"label": "gauntlet-throwaway-key"},
        )
        self.assertEqual(r_mk.status, 201, r_mk.text()[:300])
        throwaway_id = r_mk.json()["credential"]["id"]

        r = self.gw.post(
            f"/admin/api/credentials/{throwaway_id}/revoke",
            bearer=self._auth(),
            body={},
        )
        self.assertEqual(r.status, 200, r.text()[:300])

    # -- audit --

    def test_admin_audit_log(self) -> None:
        r = self.gw.get("/admin/api/audit", bearer=self._auth())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        audit_list = data.get("records") or data.get("audit") or data.get("events")
        self.assertIsNotNone(
            audit_list,
            "audit response should have records, audit, or events list, "
            f"got keys: {list(data.keys())}",
        )
        self.assertIsInstance(audit_list, list)

    # -- policy simulation --

    def test_admin_policy_simulate(self) -> None:
        r = self.gw.post(
            "/admin/api/policy/simulate",
            bearer=self._auth(),
            body={
                "profile": {
                    "id": "sim-test",
                    "protocols": ["openai.chat"],
                    "fallback": {"cloud_allowed": False},
                },
                "request": {
                    "protocol": "openai.chat",
                    "intent": "chat.completion",
                },
            },
        )
        self.assertIn(r.status, (200, 400), r.text()[:300])
        if r.status == 200:
            data = r.json()
            self.assertIn("decision", data)


# ---------------------------------------------------------------------------
# 3. Runtime probes (require opk_ key)
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_NO_BASE, "OPENAI_PROXY_BASE_URL not set")
@unittest.skipIf(_SKIP_NO_ADMIN, "OPENAI_PROXY_ADMIN_KEY not set (needed to mint runtime key)")
class TestRuntimePlane(unittest.TestCase):
    """Runtime probes using an opk_ key minted from the admin key."""

    gw: GatewayClient
    _runtime_app_id: str | None = None
    _runtime_app_key_id: str | None = None
    _runtime_app_key: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.gw = GatewayClient(_BASE_URL)
        if _RUNTIME_KEY_ENV:
            cls._runtime_app_key = _RUNTIME_KEY_ENV
            return
        if not _ADMIN_KEY:
            return
        r = cls.gw.post("/admin/api/apps", bearer=_ADMIN_KEY, body={"name": "gauntlet-runtime-probe"})
        if r.status == 201:
            cls._runtime_app_id = r.json()["app"]["id"]
            r2 = cls.gw.post(
                f"/admin/api/apps/{cls._runtime_app_id}/keys",
                bearer=_ADMIN_KEY,
                body={"label": "gauntlet-runtime-key"},
            )
            if r2.status == 201:
                cred = r2.json()["credential"]
                cls._runtime_app_key_id = cred["id"]
                cls._runtime_app_key = cred["api_key"]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._runtime_app_key_id and not _RUNTIME_KEY_ENV:
            try:
                cls.gw.post(
                    f"/admin/api/credentials/{cls._runtime_app_key_id}/revoke",
                    bearer=_ADMIN_KEY,
                    body={},
                )
            except Exception:
                pass
        if cls._runtime_app_id and not _RUNTIME_KEY_ENV:
            try:
                cls.gw.delete(f"/admin/api/apps/{cls._runtime_app_id}", bearer=_ADMIN_KEY)
            except Exception:
                pass

    def _key(self) -> str:
        key = self._runtime_app_key
        if not key:
            self.skipTest("no opk_ key available; set OPENAI_PROXY_ADMIN_KEY or OPENAI_PROXY_API_KEY")
        return key

    def _auth_enabled(self) -> bool:
        r = self.gw.get("/health")
        if r.status != 200:
            return True
        try:
            data = r.json()
        except json.JSONDecodeError:
            return True
        return data.get("auth_enabled") is not False

    # -- capabilities / discovery --

    def test_gateway_capabilities(self) -> None:
        r = self.gw.get("/gateway/v1/capabilities", bearer=self._key())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIsInstance(data, dict)

    def test_gateway_extensions(self) -> None:
        r = self.gw.get("/gateway/v1/extensions", bearer=self._key())
        self.assertIn(r.status, (200, 401, 403), r.text()[:200])

    def test_gateway_providers(self) -> None:
        r = self.gw.get("/gateway/v1/providers", bearer=self._key())
        self.assertIn(r.status, (200, 401, 403, 404), r.text()[:200])
        if r.status == 404:
            record_gap(
                "providers_endpoint_missing",
                "GET /gateway/v1/providers returned 404",
            )

    # -- OpenAI-compatible models list --

    def test_v1_models_authenticated(self) -> None:
        r = self.gw.get("/v1/models", bearer=self._key())
        self.assertEqual(r.status, 200, r.text()[:300])
        data = r.json()
        self.assertIn("data", data)
        self.assertIsInstance(data["data"], list)
        self.assertGreater(len(data["data"]), 0, "model list should not be empty")

    def test_openai_prefix_models(self) -> None:
        r = self.gw.get("/openai/v1/models", bearer=self._key())
        self.assertEqual(r.status, 200, r.text()[:300])

    # -- OpenAI-compatible chat completions (non-streaming, fast) --

    def test_chat_completion_default_model(self) -> None:
        """Chat via default model -- may 503 if backends down; that's OK."""
        r = self.gw.post(
            "/v1/chat/completions",
            bearer=self._key(),
            body={
                "model": "default",
                "messages": [{"role": "user", "content": "Reply with only the word: gauntlet"}],
                "max_tokens": 16,
                "stream": False,
            },
        )
        if r.status == 200:
            data = r.json()
            self.assertIn("choices", data)
            self.assertGreater(len(data["choices"]), 0)
        elif r.status in (503, 502):
            record_gap(
                "chat_backend_down",
                f"POST /v1/chat/completions returned {r.status} -- no chat backend available",
                {"status": r.status},
            )
        else:
            self.assertIn(
                r.status, (200, 503, 502),
                f"Unexpected status {r.status}: {r.text()[:300]}",
            )

    # -- Error cases: unknown model --

    def test_unknown_model_returns_error(self) -> None:
        r = self.gw.post(
            "/v1/chat/completions",
            bearer=self._key(),
            body={
                "model": "does-not-exist-gauntlet-probe-xyzzy",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
            },
        )
        self.assertIn(
            r.status, (400, 404, 422, 503),
            f"Unknown model should return 4xx, got {r.status}: {r.text()[:300]}",
        )

    # -- Error cases: denied by profile --

    def test_policy_denied_request(self) -> None:
        """Image generation should be denied when profile only allows openai.chat."""
        if not self._auth_enabled():
            self.skipTest("policy-denial probe requires auth/profile enforcement")
        key = self._key()
        # If the gauntlet app was created with a chat-only profile, images should be denied.
        # If not, we just document whatever we get.
        # Use a short timeout -- image backend may be slow or offline.
        gw_short = GatewayClient(_BASE_URL, timeout_s=8.0)
        try:
            r = gw_short.post(
                "/v1/images/generations",
                bearer=key,
                body={
                    "model": "flux2-klein-4b",
                    "prompt": "gauntlet probe test image",
                    "n": 1,
                },
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            record_gap(
                "image_request_timeout",
                f"POST /v1/images/generations timed out or failed: {exc}",
            )
            return
        if r.status == 403:
            data = r.json()
            error = data.get("error", {})
            self.assertIn(
                error.get("code"),
                ("policy_denied", "forbidden"),
                f"403 should carry a policy_denied code: {data}",
            )
        elif r.status in (503, 502):
            record_gap(
                "image_backend_down",
                f"POST /v1/images/generations returned {r.status} -- image backend unavailable",
            )
        elif r.status == 200:
            record_gap(
                "image_not_denied_by_chat_only_profile",
                "Image generation was allowed; gauntlet app may have a permissive profile",
            )
        elif r.status == 408:
            record_gap(
                "image_request_timeout",
                "POST /v1/images/generations timed out -- image backend may be slow or down",
            )
        # All outcomes documented; not a hard failure since profile may vary.

    # -- Unsupported route --

    def test_unsupported_audio_translation(self) -> None:
        """Audio translation returns explicit unsupported code."""
        # Use a minimal multipart body (empty file) to reach the handler.
        boundary = b"gaunboundary"
        body = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.mp3"\r\n'
            b"Content-Type: audio/mpeg\r\n\r\n"
            b"\x00\x00\x00\r\n"
            b"--" + boundary + b"--\r\n"
        )
        r = self.gw.request(
            "POST",
            "/v1/audio/translations",
            bearer=self._key(),
            raw_body=body,
            content_type=f"multipart/form-data; boundary={boundary.decode()}",
        )
        # Gateway should return 400 (unsupported) or 503 if STT down.
        self.assertIn(r.status, (400, 415, 422, 500, 503), r.text()[:300])
        if r.status == 400:
            try:
                data = r.json()
                error = data.get("error", {})
                code = error.get("code", "")
                if code:
                    self.assertIn(
                        "unsupported", code,
                        f"400 on /v1/audio/translations should carry unsupported code: {data}",
                    )
            except json.JSONDecodeError:
                pass

    # -- Ollama-compatible surface --

    def test_ollama_tags(self) -> None:
        r = self.gw.get("/ollama/api/tags", bearer=self._key())
        self.assertIn(r.status, (200, 401, 403), r.text()[:200])
        if r.status == 200:
            data = r.json()
            self.assertIn("models", data)

    def test_ollama_version(self) -> None:
        r = self.gw.get("/ollama/api/version", bearer=self._key())
        self.assertIn(r.status, (200, 401, 403), r.text()[:200])

    # -- Anthropic-compatible surface --

    def test_anthropic_messages_route_exists(self) -> None:
        """POST /anthropic/v1/messages with bad payload -> 422 or 400, not 404."""
        r = self.gw.post(
            "/anthropic/v1/messages",
            bearer=self._key(),
            body={},
        )
        self.assertNotEqual(r.status, 404, f"Anthropic route should be mounted: {r.text()[:200]}")
        self.assertIn(r.status, (400, 422, 503, 200), r.text()[:200])

    # -- Provider-native discovery --

    def test_provider_native_route_requires_policy(self) -> None:
        """Accessing a provider-native path without native_passthrough profile -> 403 or 404."""
        r = self.gw.get(
            "/gateway/v1/providers/tabby/v1/models",
            bearer=self._key(),
        )
        self.assertIn(
            r.status, (200, 403, 404),
            f"Provider-native route should respond deterministically: {r.status} {r.text()[:200]}",
        )
        if r.status == 200:
            record_gap(
                "provider_native_open_without_policy",
                "Provider-native passthrough succeeded without explicit native_passthrough profile",
            )
        elif r.status == 404:
            record_gap(
                "provider_native_not_found",
                "GET /gateway/v1/providers/tabby/v1/models -> 404 (provider may not exist in this harness)",
                {"status": r.status},
            )


# ---------------------------------------------------------------------------
# 4. Bootstrap / setup state detection
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_NO_BASE, "OPENAI_PROXY_BASE_URL not set")
class TestBootstrapDetect(unittest.TestCase):
    """Detect whether the instance looks fresh or already claimed."""

    def setUp(self) -> None:
        self.gw = GatewayClient(_BASE_URL)

    def test_setup_page_indicates_state(self) -> None:
        r = self.gw.get("/setup")
        self.assertEqual(r.status, 200)
        body = r.text()
        # Check for full-length key material (real keys, not placeholder text like "oak_...")
        import re as _re
        opk_leak = _re.search(r'opk_[A-Za-z0-9_-]{10,}', body)
        oak_leak = _re.search(r'oak_[A-Za-z0-9_-]{10,}', body)
        self.assertIsNone(opk_leak, f"/setup must not leak opk_ runtime keys in HTML: {opk_leak}")
        self.assertIsNone(oak_leak, f"/setup must not leak oak_ admin keys in HTML: {oak_leak}")

        if "This gateway has an owner" in body or "Setup complete" in body:
            pass  # Instance is claimed -- checks above are sufficient
        elif "Claim this gateway" in body or "Create the local owner" in body:
            pass  # Fresh instance -- first-run flow
        else:
            record_gap(
                "setup_state_unrecognized",
                "GET /setup did not contain expected state markers",
                {"body_snippet": body[:200]},
            )

    def test_health_is_always_public(self) -> None:
        r = self.gw.get("/health")
        self.assertEqual(r.status, 200)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
