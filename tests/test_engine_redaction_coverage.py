"""Every response that carries a registry or an engine record hides a command secret.

Each test here fails when one production hunk is reverted: the other-settings block of the engine
view, the three provider-admin responses (create, models, remove), the extension-enable response,
and the package sources the extension routes return (a git URL may hold credentials).
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, extension_packages, service_registry
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from gateway.provider_admin import create_provider_admin_router
from tests.test_admin_owner_holes_round4 import _enable, _git_package
from tests.test_engine_redaction import MARKER, SECRET, _command_record, _seed, _view
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

OWNER = AdminContext(subject="test-owner", roles=frozenset({"owner"}))


def test_the_engine_view_hides_a_command_secret_in_other_settings() -> None:
    record = _command_record()
    record["config"]["process"] = {"driver": "process", "start": ["runner", "--token", SECRET],
                                   "env": {"API_KEY": SECRET}, "port": {"fixed": 8080}}
    view = _view(record)
    assert SECRET not in json.dumps(view)
    assert view["other_settings"]["process"]["start"] == ["runner", "--token", MARKER]
    assert view["other_settings"]["process"]["env"] == {"API_KEY": MARKER}


MODEL = {"id": "test/model", "upstream_model": "test/model", "enabled": True,
         "price": {"input_usd_per_million_tokens": "3.00", "output_usd_per_million_tokens": "15.00"}}


@pytest.fixture
def providers(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    store = AdminSQLiteStore(db)
    store.init_db()
    registry = {"schema_version": 2, "services": {"engine-a": _command_record(native=True)}}
    admin_store.write_service_registry(service_registry.normalize_service_registry(registry), expected_generation=0, path=db)
    assert SECRET in json.dumps(store.get_service_registry())
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=lambda _request: OWNER))
    return TestClient(app)


def _create(client):
    return client.post("/admin/api/providers", json={
        "preset_id": "openrouter", "provider_id": "cloud-test", "key_mode": "secret",
        "api_key": "synthetic-provider-key-4f2a", "models": [MODEL], "caps": {"daily_usd": 5}})


def test_creating_a_provider_returns_a_registry_without_a_command_secret(providers) -> None:
    created = _create(providers)
    assert created.status_code == 201, created.text
    assert SECRET not in created.text
    assert created.json()["service_registry"]["services"]["engine-a"]["process"]["start"] == ["runner", "--token", MARKER]


def test_replacing_provider_models_returns_a_registry_without_a_command_secret(providers) -> None:
    assert _create(providers).status_code == 201
    updated = providers.put("/admin/api/providers/cloud-test/models", json={"models": [MODEL]})
    assert updated.status_code == 200, updated.text
    assert SECRET not in updated.text
    assert "engine-a" in updated.json()["service_registry"]["services"]


def test_removing_a_provider_returns_a_registry_without_a_command_secret(providers) -> None:
    assert _create(providers).status_code == 201
    removed = providers.delete("/admin/api/providers/cloud-test")
    assert removed.status_code == 200, removed.text
    assert SECRET not in removed.text
    assert "engine-a" in removed.json()["service_registry"]["services"]


_PLAIN = ("id: plainsvc\nname: Plain\nconfig:\n  protocol: openai-compatible\n"
          "  host: 192.168.1.80\n  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n")


def test_enabling_an_extension_returns_a_registry_without_a_command_secret(gateway, tmp_path, monkeypatch) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    enabled = _enable(admin, _git_package(monkeypatch, tmp_path, _PLAIN, name="plainpkg"))
    assert enabled.status_code == 200, enabled.text
    assert SECRET not in enabled.text
    assert "plainsvc" in enabled.json()["service_registry"]["services"]
    assert enabled.json()["service_registry"]["services"]["engine-a"]["process"]["start"] == ["runner", "--token", MARKER]


GIT_SECRETS = ("pw-syn-1", "tok-syn-2", "key-syn-3")
GIT_URL = "https://user:pw-syn-1@example.com/package.git?token=tok-syn-2&api_key=key-syn-3"


def _credentialed_git_package(monkeypatch, tmp_path):
    source = _git_package(monkeypatch, tmp_path, _PLAIN, name="plainpkg")
    return {**source, "url": GIT_URL}


def test_package_sources_are_returned_without_url_credentials(gateway, tmp_path, monkeypatch) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    source = _credentialed_git_package(monkeypatch, tmp_path)
    enabled = _enable(admin, source)
    assert enabled.status_code == 200, enabled.text
    for secret in GIT_SECRETS:
        assert secret not in enabled.text
    listed = admin.get("/admin/api/extensions")
    for secret in GIT_SECRETS:
        assert secret not in listed.text
    package = listed.json()["extension_packages"]["packages"]["plainpkg"]
    assert package["source"]["url"] == "https://example.com/package.git"
    # The stored index keeps what the owner entered; only the responses hide it.
    stored = admin_store.get_operator_setting(extension_packages.EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=gateway.path)
    assert "pw-syn-1" in json.dumps(stored)


def test_validating_a_package_source_returns_no_url_credentials(gateway, tmp_path, monkeypatch) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    source = _credentialed_git_package(monkeypatch, tmp_path)
    reviewed = admin.post("/admin/api/extensions/validate", headers=csrf_for(admin), json={"source": source})
    assert reviewed.status_code == 200, reviewed.text
    for secret in GIT_SECRETS:
        assert secret not in reviewed.text


def test_removing_a_package_returns_no_url_credentials(gateway, tmp_path, monkeypatch) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    other = _enable(admin, _credentialed_git_package(monkeypatch, tmp_path))
    assert other.status_code == 200, other.text
    second = _git_package(monkeypatch, tmp_path, _PLAIN.replace("plainsvc", "second"), name="secondpkg")
    assert _enable(admin, second).status_code == 200
    removed = admin.delete("/admin/api/extensions/secondpkg", headers=csrf_for(admin))
    assert removed.status_code == 200, removed.text
    for secret in GIT_SECRETS:
        assert secret not in removed.text
