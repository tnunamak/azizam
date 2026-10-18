"""Frozen slice-0 observations. Update a digest only with a named contract change.

The live-record fixture records keys and value types, never values. It was
captured from the 20 owner records with SQLite read-only before schema v2.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import engine_admin, service_registry
from gateway.api import create_gateway_router
from gateway.engine_definitions import load_engine_catalog
from gateway.litellm_substrate import compile_litellm_config_from_registry
from tests.test_engine_contract_v2 import _service
from tests.test_gateway_api import _build_registry, _sample_capabilities


FIXTURE = Path(__file__).parent / "fixtures" / "engine_contract_live_shapes.json"


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_live_registry_shapes_are_redacted_and_complete() -> None:
    shapes = json.loads(FIXTURE.read_text())
    assert len(shapes) == 20
    assert {row["kind"] for row in shapes.values()} == {"<string>"}
    assert all("config" in row and "lifecycle" in row for row in shapes.values())
    assert sum("lifecycle" in row["config"] for row in shapes.values()) == 13
    assert "http://" not in FIXTURE.read_text()
    assert "/home/" not in FIXTURE.read_text()
    assert "opk_" not in FIXTURE.read_text()
    assert "oak_" not in FIXTURE.read_text()
    assert _digest(shapes) == "FILL_SHAPES"


def test_runtime_backend_projection_is_frozen() -> None:
    projected = service_registry.runtime_backend_from_service(_service())
    assert _digest(projected) == "FILL_RUNTIME"


def test_frontend_route_graph_is_frozen() -> None:
    import proxy

    graph = sorted((path, method) for route in proxy.app.routes
                   for path in [getattr(route, "path", "")]
                   for method in (getattr(route, "methods", None) or {"WEBSOCKET"})
                   if path.startswith(("/v1/", "/openai/", "/anthropic/", "/ollama/", "/sdapi/", "/mcp")))
    assert graph
    assert _digest(graph) == "FILL_ROUTES"


def test_public_catalog_surfaces_are_frozen(tmp_path: Path) -> None:
    from tests.test_model_metadata_endpoints import ModelListEndpointTests

    case = ModelListEndpointTests("test_every_entry_gets_a_type_and_architecture")
    case.setUp()
    try:
        models = case._list()
    finally:
        case.doCleanups()
    model_surface = [(row["id"], row.get("type"), row.get("architecture")) for row in models["data"]]

    app = FastAPI()
    app.include_router(create_gateway_router(_build_registry(), capabilities_provider=lambda _r: _sample_capabilities()))
    caps = TestClient(app).get("/gateway/v1/capabilities").json()

    catalog = load_engine_catalog()
    engine = engine_admin.engine_view(catalog, _service(), db_path=tmp_path / "empty.db")
    # The old web client consumes these fields; adding v2 beside them cannot
    # silently change their content.
    old_engine = {key: engine[key] for key in ("service_id", "definition", "name", "enabled", "locked", "editable", "values", "key", "capabilities", "models")}
    assert _digest({"models": model_surface, "capabilities": caps, "engine": old_engine}) == "FILL_CATALOG"


def test_litellm_compiler_and_current_are_frozen() -> None:
    compiled = compile_litellm_config_from_registry({"services": {"engine-a": _service()}})
    names = [row["model_name"] for row in compiled["model_list"]]
    assert "current" in names and "current-fast" in names
    assert _digest(compiled) == "FILL_LITELLM"
