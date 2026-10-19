"""Persisted verification evidence is written only by its verification route."""
from __future__ import annotations

import pytest

from gateway import service_registry
from tests.test_engine_admin_routes import _put_registry, _registry
from tests.test_signin_who_can_enter import gateway, owner_and_admin  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for


def _saved_engine(gateway):
    record = service_registry.service_from_backend_config("hop", {
        "name": "Hop", "host": "hop.fk.test", "port": 80, "base_path": "/v1",
        "protocol": "openai-compatible", "gateway_lifecycle": True,
        "capabilities": ["chat", "openai"],
        "models": [{"id": "offering", "model_group": "hop-chat", "upstream_model": "up-model"}],
        "definition": "openai-compatible", "definition_version": 1,
        "engine_tests": {"chat": {
            "ok": True, "via": "saved", "at": 1, "model": "up-model",
            "evidence_token": "verified-receipt", "config_fingerprint": "verified-fingerprint",
        }},
        "engine_check": {"status": "ok", "checked_at": 1, "origin": "http://hop.fk.test:80"},
    }, source="ui")
    registry = _registry(gateway)
    registry["services"]["hop"] = record
    _put_registry(gateway, registry)
    return registry


@pytest.mark.parametrize("field, forged", [
    ("engine_tests", {"chat": {
        "ok": True, "via": "saved", "at": 2, "model": "invented-model",
        "evidence_token": "admin-chosen", "config_fingerprint": "verified-fingerprint",
    }}),
    ("engine_check", {"status": "ok", "checked_at": 2, "origin": "http://attacker.test:80"}),
])
def test_admin_whole_registry_write_cannot_replace_verification_evidence(gateway, field, forged):
    _, _, _, admin = owner_and_admin(gateway)
    _saved_engine(gateway)
    response = admin.get("/admin/api/operator/service-registry")
    assert response.status_code == 200, response.text
    registry = response.json()["service_registry"]
    registry["services"]["hop"]["config"][field] = forged

    refused = admin.put(
        "/admin/api/operator/service-registry",
        headers={**csrf_for(admin), "If-Match": response.headers["etag"]},
        json=registry,
    )

    assert refused.status_code == 403, refused.text
    assert _registry(gateway)["services"]["hop"]["config"][field] != forged


def test_admin_whole_registry_write_still_roundtrips_unchanged_evidence_with_metadata(gateway):
    _, _, _, admin = owner_and_admin(gateway)
    original = _saved_engine(gateway)
    response = admin.get("/admin/api/operator/service-registry")
    assert response.status_code == 200, response.text
    registry = response.json()["service_registry"]
    registry["services"]["hop"]["name"] = "Renamed hop"

    saved = admin.put(
        "/admin/api/operator/service-registry",
        headers={**csrf_for(admin), "If-Match": response.headers["etag"]},
        json=registry,
    )

    assert saved.status_code == 200, saved.text
    after = _registry(gateway)["services"]["hop"]
    assert after["name"] == "Renamed hop"
    assert after["config"]["engine_tests"] == original["services"]["hop"]["config"]["engine_tests"]
    assert after["config"]["engine_check"] == original["services"]["hop"]["config"]["engine_check"]
