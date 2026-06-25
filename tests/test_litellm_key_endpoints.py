"""Tests for the operator LiteLLM virtual-key endpoints.

Covers /admin/api/operator/litellm/keys (list, create) and
/keys/delete: admin guard, the injected-client contract, input validation,
secret-leak defenses, and the unavailable-substrate path. The sidecar client is
faked, so no LiteLLM process is required.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin_api import create_admin_router  # noqa: E402
from gateway.backends.litellm_admin import LiteLLMAdminError  # noqa: E402
from tests.test_admin_api import FakeAdminStore, _admin_auth  # noqa: E402


class _FakeKeyClient:
    """In-memory stand-in for the LiteLLM sidecar key client."""

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled
        self.keys: list[dict] = []
        self.generated: list[dict] = []
        self.deleted: list[dict] = []
        self.next_error: LiteLLMAdminError | None = None

    async def list_keys(self, **kwargs):
        if self.next_error:
            raise self.next_error
        return list(self.keys)

    async def generate_key(self, spec):
        if self.next_error:
            raise self.next_error
        self.generated.append(dict(spec))
        return {"key": "sk-minted-secret", **dict(spec)}

    async def delete_keys(self, *, keys=None, key_aliases=None):
        if self.next_error:
            raise self.next_error
        self.deleted.append({"keys": keys, "key_aliases": key_aliases})
        return {"deleted": True}


def _make(
    client: _FakeKeyClient | None,
    *,
    known_models: frozenset[str] | None = None,
) -> TestClient:
    app = FastAPI()
    getter = (lambda: known_models) if known_models is not None else None
    app.include_router(
        create_admin_router(
            FakeAdminStore(),
            auth=_admin_auth,
            litellm_key_client=client,
            known_model_names_getter=getter,
        )
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer admin-token"}


class LiteLLMKeyEndpointTests(unittest.TestCase):
    def test_list_requires_admin(self) -> None:
        client = _make(_FakeKeyClient())
        resp = client.get("/admin/api/operator/litellm/keys")
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_non_admin_forbidden(self) -> None:
        client = _make(_FakeKeyClient())
        resp = client.get(
            "/admin/api/operator/litellm/keys",
            headers={"Authorization": "Bearer viewer-token"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_list_returns_keys(self) -> None:
        fake = _FakeKeyClient()
        fake.keys = [{"key_alias": "batch", "key_name": "sk-...abcd", "spend": 2.0}]
        client = _make(fake)
        resp = client.get("/admin/api/operator/litellm/keys", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        keys = resp.json()["keys"]
        self.assertEqual(keys[0]["key_alias"], "batch")

    def test_list_redacts_raw_secret_in_token_field(self) -> None:
        fake = _FakeKeyClient()
        # A defensive case: a raw sk- secret leaked into the list payload.
        fake.keys = [{"token": "sk-supersecretvalue1234", "key_alias": "leak"}]
        client = _make(fake)
        resp = client.get("/admin/api/operator/litellm/keys", headers=_auth())
        token = resp.json()["keys"][0]["token"]
        self.assertNotEqual(token, "sk-supersecretvalue1234")
        self.assertIn("…", token)

    def test_list_drops_api_key_field(self) -> None:
        fake = _FakeKeyClient()
        fake.keys = [{"key_alias": "x", "api_key": "sk-raw", "key": "sk-raw2"}]
        client = _make(fake)
        resp = client.get("/admin/api/operator/litellm/keys", headers=_auth())
        row = resp.json()["keys"][0]
        self.assertNotIn("api_key", row)
        self.assertNotIn("key", row)

    def test_create_mints_key_and_returns_secret_once(self) -> None:
        fake = _FakeKeyClient()
        client = _make(fake)
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"key_alias": "batch", "max_budget": 10, "models": ["gpt-x"]},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["key"]["key"], "sk-minted-secret")
        # The forwarded spec was sanitized to the documented fields.
        spec = fake.generated[0]
        self.assertEqual(spec["key_alias"], "batch")
        self.assertEqual(spec["max_budget"], 10.0)
        self.assertEqual(spec["models"], ["gpt-x"])

    def test_create_rejects_supplied_secret(self) -> None:
        client = _make(_FakeKeyClient())
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"key": "sk-attacker-chosen"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_key")

    def test_create_rejects_non_numeric_budget(self) -> None:
        client = _make(_FakeKeyClient())
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"max_budget": "lots"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_delete_by_key(self) -> None:
        fake = _FakeKeyClient()
        client = _make(fake)
        resp = client.post(
            "/admin/api/operator/litellm/keys/delete",
            headers=_auth(),
            json={"keys": ["sk-1"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(fake.deleted[0]["keys"], ["sk-1"])

    def test_delete_by_alias(self) -> None:
        fake = _FakeKeyClient()
        client = _make(fake)
        resp = client.post(
            "/admin/api/operator/litellm/keys/delete",
            headers=_auth(),
            json={"key_aliases": ["batch"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(fake.deleted[0]["key_aliases"], ["batch"])

    def test_delete_requires_a_target(self) -> None:
        client = _make(_FakeKeyClient())
        resp = client.post(
            "/admin/api/operator/litellm/keys/delete",
            headers=_auth(),
            json={},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_unconfigured_substrate_returns_503(self) -> None:
        # No client injected -> capability unavailable, not a 500.
        client = _make(None)
        resp = client.get("/admin/api/operator/litellm/keys", headers=_auth())
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "litellm_unconfigured")

    def test_disabled_client_returns_503(self) -> None:
        client = _make(_FakeKeyClient(enabled=False))
        resp = client.get("/admin/api/operator/litellm/keys", headers=_auth())
        self.assertEqual(resp.status_code, 503, resp.text)

    def test_upstream_error_is_mapped(self) -> None:
        fake = _FakeKeyClient()
        fake.next_error = LiteLLMAdminError(
            502, "litellm_key_api_error", "LiteLLM: boom", upstream_status=400
        )
        client = _make(fake)
        resp = client.get("/admin/api/operator/litellm/keys", headers=_auth())
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "litellm_key_api_error")


class LiteLLMKeyModelValidationTests(unittest.TestCase):
    """Model allowlist validation against the gateway catalog."""

    _CATALOG = frozenset({"gpt-4o", "claude-3-opus"})

    def test_known_model_passes(self) -> None:
        fake = _FakeKeyClient()
        client = _make(fake, known_models=self._CATALOG)
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"key_alias": "ok", "models": ["gpt-4o"]},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(fake.generated[0]["models"], ["gpt-4o"])

    def test_unknown_model_rejected(self) -> None:
        client = _make(_FakeKeyClient(), known_models=self._CATALOG)
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"models": ["gpt-x"]},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "unknown_model")
        self.assertIn("gpt-x", body["error"]["message"])

    def test_multiple_unknown_models_all_reported(self) -> None:
        client = _make(_FakeKeyClient(), known_models=self._CATALOG)
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"models": ["gpt-x", "llama-99", "gpt-4o"]},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        msg = resp.json()["error"]["message"]
        self.assertIn("gpt-x", msg)
        self.assertIn("llama-99", msg)
        # The unknown-list portion comes before the "known models:" label.
        unknown_section = msg.split("known models:")[0]
        self.assertNotIn("gpt-4o", unknown_section)

    def test_no_catalog_skips_validation(self) -> None:
        # known_model_names_getter=None → no catalog → any model name accepted.
        fake = _FakeKeyClient()
        client = _make(fake)  # no known_models
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"models": ["totally-made-up-model"]},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(fake.generated[0]["models"], ["totally-made-up-model"])

    def test_empty_catalog_skips_validation(self) -> None:
        # Empty frozenset → catalog not yet populated → skip validation.
        fake = _FakeKeyClient()
        client = _make(fake, known_models=frozenset())
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"models": ["unknown-model"]},
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_multiple_known_models_all_pass(self) -> None:
        fake = _FakeKeyClient()
        client = _make(fake, known_models=self._CATALOG)
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"models": ["gpt-4o", "claude-3-opus"]},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(sorted(fake.generated[0]["models"]), ["claude-3-opus", "gpt-4o"])

    def test_no_models_field_skips_validation(self) -> None:
        # No ``models`` key at all → unrestricted key, no validation needed.
        fake = _FakeKeyClient()
        client = _make(fake, known_models=self._CATALOG)
        resp = client.post(
            "/admin/api/operator/litellm/keys",
            headers=_auth(),
            json={"key_alias": "unrestricted"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertNotIn("models", fake.generated[0])


if __name__ == "__main__":
    unittest.main()
