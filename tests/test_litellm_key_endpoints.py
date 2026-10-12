"""Operator LiteLLM virtual-key endpoints over the real sidecar client.

/admin/api/operator/litellm/keys (list, create) and /keys/delete run through
the real admin router and the real LiteLLMKeyAdminClient. The sidecar's
``/key/*`` API is an FK fake (protocol ``litellm-admin``), so each case also
proves the wire contract: master key in X-Litellm-Key, field names, query.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.admin_api import create_admin_router
from gateway.backends.litellm_admin import LiteLLMKeyAdminClient
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from tests.support.fake_upstream import FakeUpstream, fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.test_admin_api import FakeAdminStore, _admin_auth

MASTER_KEY = "fk-litellm-master-key"
KEYS = "/admin/api/operator/litellm/keys"
ADMIN = {"Authorization": "Bearer admin-token"}
CATALOG = frozenset({"gpt-4o", "claude-3-opus"})


class Sidecar:
    """Serves one litellm-admin scenario and builds the gateway around it."""

    def __init__(self, fakes) -> None:
        self._fakes = fakes
        self.fake: FakeUpstream | None = None

    def serve(self, name: str, **values) -> FakeUpstream:
        self.fake = self._fakes.asgi(load_scenario(f"litellm-admin/{name}", master_key=MASTER_KEY, **values), host="litellm.fk.test")
        return self.fake

    def gateway(self, *, known_models: frozenset[str] | None = None, enabled: bool = True, api_key: str = MASTER_KEY, configured: bool = True) -> TestClient:
        client = None
        if configured:
            base_url = self.fake.base_url if self.fake else "http://unused.fk.test"
            client = LiteLLMKeyAdminClient(LiteLlmRuntimeConfig(enabled=enabled, base_url=base_url, api_key=api_key, service_id="litellm"))
        app = FastAPI()
        app.include_router(create_admin_router(
            FakeAdminStore(),
            auth=_admin_auth,
            litellm_key_client=client,
            known_model_names_getter=(lambda: known_models) if known_models is not None else None,
        ))
        return TestClient(app)


@pytest.fixture
def sidecar(fake_upstreams) -> Sidecar:
    return Sidecar(fake_upstreams)


def test_list_requires_admin(sidecar) -> None:
    sidecar.serve("key-list", status=200, body={"keys": []})
    gateway = sidecar.gateway()
    assert gateway.get(KEYS).status_code == 401
    assert gateway.get(KEYS, headers={"Authorization": "Bearer viewer-token"}).status_code == 403
    assert sidecar.fake.requests == []
    # The scenario's one list call is required; spend it as the admin.
    assert gateway.get(KEYS, headers=ADMIN).status_code == 200


def test_list_returns_redacted_keys_from_the_sidecar(sidecar) -> None:
    sidecar.serve("key-list", status=200, body={"keys": [
        {"key_alias": "batch", "key_name": "sk-...abcd", "spend": 2.0},
        {"key_alias": "leak", "token": "sk-supersecretvalue1234", "api_key": "sk-raw", "key": "sk-raw2"},
    ]})

    response = sidecar.gateway().get(KEYS, headers=ADMIN)

    assert response.status_code == 200, response.text
    assert response.json() == {"keys": [
        {"key_alias": "batch", "key_name": "sk-...abcd", "spend": 2.0},
        {"key_alias": "leak", "token": "sk-su…1234"},
    ]}
    for secret in ("sk-supersecretvalue1234", "sk-raw", MASTER_KEY):
        assert secret not in response.text


def test_list_accepts_the_string_only_key_shape(sidecar) -> None:
    sidecar.serve("key-list", status=200, body={"keys": ["sk-...wxyz"]})

    response = sidecar.gateway().get(KEYS, headers=ADMIN)

    assert response.json() == {"keys": [{"token": "sk-..…wxyz", "key_name": "sk-...wxyz"}]}


def test_create_mints_key_and_returns_secret_once(sidecar) -> None:
    sidecar.serve("key-generate", spec={"key_alias": "batch", "max_budget": 10.0, "models": ["gpt-x"]})

    response = sidecar.gateway().post(KEYS, headers=ADMIN, json={"key_alias": "batch", "max_budget": "10", "models": ["gpt-x", " "]})

    assert response.status_code == 201, response.text
    assert response.json() == {"key": {"key": "sk-minted-secret", "key_name": "sk-...cret", "expires": None}}
    [forwarded] = sidecar.fake.calls("generate")
    assert forwarded.json == {"key_alias": "batch", "max_budget": 10.0, "models": ["gpt-x"]}


@pytest.mark.parametrize(
    ("body", "code"),
    [({"key": "sk-attacker-chosen"}, "invalid_key"), ({"max_budget": "lots"}, "invalid_key")],
    ids=["supplied-secret", "non-numeric-budget"],
)
def test_create_rejects_invalid_input_before_the_sidecar(sidecar, body, code) -> None:
    response = sidecar.gateway().post(KEYS, headers=ADMIN, json=body)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("body", "target"),
    [({"keys": ["sk-1"]}, {"keys": ["sk-1"]}), ({"key_aliases": ["batch"]}, {"key_aliases": ["batch"]}), ({"key_alias": "batch", "key": "sk-2"}, {"keys": ["sk-2"], "key_aliases": ["batch"]})],
    ids=["keys", "aliases", "singular-fields"],
)
def test_delete_forwards_keys_and_aliases(sidecar, body, target) -> None:
    sidecar.serve("key-delete", target=target, deleted=target.get("keys", []))

    response = sidecar.gateway().post(f"{KEYS}/delete", headers=ADMIN, json=body)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": {"deleted_keys": target.get("keys", [])}}
    assert sidecar.fake.calls("delete")[0].json == target


def test_delete_requires_a_target(sidecar) -> None:
    response = sidecar.gateway().post(f"{KEYS}/delete", headers=ADMIN, json={})

    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"configured": False}, "litellm_unconfigured"),
        ({"enabled": False}, "litellm_unconfigured"),
        ({"api_key": ""}, "litellm_unconfigured"),
    ],
    ids=["no-client", "disabled", "no-master-key"],
)
def test_unconfigured_substrate_returns_503(sidecar, setup, code) -> None:
    response = sidecar.gateway(**setup).get(KEYS, headers=ADMIN)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "litellm_key_api_error"), (401, "litellm_admin_auth_failed"), (403, "litellm_admin_auth_failed")],
)
def test_sidecar_rejection_maps_to_502(sidecar, status, code) -> None:
    sidecar.serve("key-list", status=status, body={"error": {"message": "LiteLLM: boom"}})

    response = sidecar.gateway().get(KEYS, headers=ADMIN)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == code
    assert MASTER_KEY not in response.text


def test_sidecar_drop_maps_to_503_unreachable(sidecar) -> None:
    sidecar.serve("key-list-dropped")

    response = sidecar.gateway().get(KEYS, headers=ADMIN)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "litellm_unreachable"


@pytest.mark.parametrize("models", [["gpt-4o"], ["gpt-4o", "claude-3-opus"]], ids=["one", "two"])
def test_known_models_pass_catalog_validation(sidecar, models) -> None:
    sidecar.serve("key-generate", spec={"models": models})

    response = sidecar.gateway(known_models=CATALOG).post(KEYS, headers=ADMIN, json={"models": models})

    assert response.status_code == 201, response.text
    assert sidecar.fake.calls("generate")[0].json == {"models": models}


def test_unknown_models_are_all_reported_and_never_reach_the_sidecar(sidecar) -> None:
    response = sidecar.gateway(known_models=CATALOG).post(KEYS, headers=ADMIN, json={"models": ["gpt-x", "llama-99", "gpt-4o"]})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "unknown_model"
    assert error["message"] == (
        "model(s) not in gateway catalog: ['gpt-x', 'llama-99']; known models: ['claude-3-opus', 'gpt-4o']"
    )


@pytest.mark.parametrize("known_models", [None, frozenset()], ids=["no-catalog", "empty-catalog"])
def test_missing_catalog_skips_validation(sidecar, known_models) -> None:
    sidecar.serve("key-generate", spec={"models": ["totally-made-up-model"]})

    response = sidecar.gateway(known_models=known_models).post(KEYS, headers=ADMIN, json={"models": ["totally-made-up-model"]})

    assert response.status_code == 201, response.text


def test_no_models_field_creates_an_unrestricted_key(sidecar) -> None:
    sidecar.serve("key-generate", spec={"key_alias": "unrestricted"})

    response = sidecar.gateway(known_models=CATALOG).post(KEYS, headers=ADMIN, json={"key_alias": "unrestricted"})

    assert response.status_code == 201, response.text
    assert sidecar.fake.calls("generate")[0].json == {"key_alias": "unrestricted"}
