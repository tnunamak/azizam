"""The engine form discovers installed descriptions through the same loader as dispatch."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import yaml

from gateway import egress, engine_protocol_admin
from gateway.engine_protocol import description


def _write(root, name="tiny", *, valid=True):
    root.mkdir(exist_ok=True)
    doc = {"protocol_version": 1, "id": name, "title": "Tiny protocol", "executor": "http-v1",
           "operations": {"models": {"class": "inventory", "request": {"method": "GET", "path": "/models"},
                                      "response": {"framing": "json", "select": {"ids": "/data/*/id"}}}}}
    if not valid:
        doc["not_a_field"] = "unreadable-secret-text"
    (root / f"{name}.yml").write_text(yaml.safe_dump(doc))


def _client():
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)
    engine_protocol_admin.register_engine_protocol_routes(router, guard=lambda request: SimpleNamespace(role="owner"))
    app.include_router(router)
    return TestClient(app)


def test_catalog_shows_validated_seed_and_persistent_user_descriptions(tmp_path, monkeypatch):
    seeds, user = tmp_path / "seed", tmp_path / "user"
    _write(seeds, "seeded")
    _write(user)
    _write(user, "broken", valid=False)
    monkeypatch.setattr(description, "REGISTRY_DIR", seeds)
    monkeypatch.setenv("AZIZAM_PROTOCOLS_DIR", str(user))
    response = _client().get("/admin/api/engine-protocols")
    assert response.status_code == 200
    payload = response.json()
    rows = {row["id"]: row for row in payload["protocols"]}
    assert set(rows) == {"seeded", "tiny"}
    assert rows["tiny"] == {"id": "tiny", "title": "Tiny protocol", "executor": "http-v1",
                            "operations": ["models"], "source": "user", "dispatch_supported": True,
                            "validation": {"valid": True, "message": "Valid description. 1 operation."}}
    assert rows["seeded"]["source"] == "seed"
    assert payload["problems"][0]["file"] == "broken.yml"
    assert "unreadable-secret-text" not in response.text
    assert str(tmp_path) not in response.text


def test_invalid_user_override_is_not_offered_as_a_valid_seed(tmp_path, monkeypatch):
    seeds, user = tmp_path / "seed", tmp_path / "user"
    _write(seeds)
    _write(user, valid=False)
    monkeypatch.setattr(description, "REGISTRY_DIR", seeds)
    monkeypatch.setenv("AZIZAM_PROTOCOLS_DIR", str(user))
    assert _client().get("/admin/api/engine-protocols").json()["protocols"] == []


def test_catalog_preserves_the_admin_guard_response():
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)
    engine_protocol_admin.register_engine_protocol_routes(router, guard=lambda request: egress.safe_error_response(403, "admin_required", "Sign in as an administrator."))
    app.include_router(router)
    assert TestClient(app).get("/admin/api/engine-protocols").status_code == 403
