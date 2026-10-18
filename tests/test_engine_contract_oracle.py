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
from gateway.receipts import RouteReceipt
from gateway.runtime_auth import evaluate_and_apply, make_policy_request
from tests.support.access import identity, plan_for
from tests.test_engine_contract_v2 import _service
from tests.test_gateway_api import _build_registry, _sample_capabilities


FIXTURE = Path(__file__).parent / "fixtures" / "engine_contract_live_shapes.json"
LIVE_KIND_SOURCE = {
    "azizam": ("provider", "provisioned"), "comfyui": ("provider", "provisioned"),
    "decisions": ("provider", "ui"), "decisions-quality": ("provider", "ui"),
    "embeddings": ("provider", "provisioned"), "kobold": ("provider", "provisioned"),
    "litellm": ("llm-substrate", "builtin"), "openrouter": ("provider", "ui"),
    "realtime": ("provider", "ui"), "rerank": ("provider", "ui"),
    "stt": ("provider", "provisioned"), "stt-canary": ("provider", "ui"),
    "stt-diarize": ("provider", "ui"), "stt-nemotron": ("provider", "ui"),
    "stt-parakeet-gpu": ("provider", "ui"), "tts": ("provider", "provisioned"),
    "tts-chatterbox": ("provider", "ui"), "tts-kokoro": ("provider", "ui"),
    "tts-supertonic": ("provider", "ui"), "typesafe": ("provider", "ui"),
}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_live_registry_shapes_are_redacted_and_complete() -> None:
    shapes = json.loads(FIXTURE.read_text())
    assert len(shapes) == 20
    assert set(shapes) == set(LIVE_KIND_SOURCE)
    assert [source for _, source in LIVE_KIND_SOURCE.values()].count("ui") == 13
    assert [source for _, source in LIVE_KIND_SOURCE.values()].count("provisioned") == 6
    assert {row["kind"] for row in shapes.values()} == {"<string>"}
    assert all("config" in row and "lifecycle" in row for row in shapes.values())
    assert sum("lifecycle" in row["config"] for row in shapes.values()) == 13
    assert "http://" not in FIXTURE.read_text()
    assert "home/" not in FIXTURE.read_text()
    assert "opk_" not in FIXTURE.read_text()
    assert "oak_" not in FIXTURE.read_text()
    assert _digest(shapes) == "b93eb7b8d52bc3e40e80fadd719d761e48147042ec18f1a3b27ea2db6ecd54d8"


def test_runtime_backend_projection_is_frozen() -> None:
    service = _service()
    projected = service_registry.runtime_backend_from_service(service)
    assert _digest(projected) == "b8671053c0d25c24753abf0b10d0bc9a35512398cdea0a029a3b54cb64fe4bd3"
    # Contract v2 collapses lifecycle storage; it does not change the runtime
    # backend consumed by the existing gateway and LiteLLM compiler.
    registry = {"schema_version": 1, "services": {"engine-a": service}}
    service_registry.migrate_engine_records_v2(registry)
    after = service_registry.runtime_backend_from_service(registry["services"]["engine-a"])
    assert _digest(after) == _digest(projected)


def test_frontend_route_graph_is_frozen() -> None:
    import proxy

    graph = sorted((path, method) for route in proxy.app.routes
                   for path in [getattr(route, "path", "")]
                   for method in (getattr(route, "methods", None) or {"WEBSOCKET"})
                   if path.startswith(("/v1/", "/openai/", "/anthropic/", "/ollama/", "/sdapi/", "/mcp")))
    assert graph
    assert _digest(graph) == "5d23367340519397b1cfa0c82fb4b4f6fac6f0e98059e6ab642dc879824b66ca"


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
    # W5 deliberately makes lifecycle-hop records editable and shows an
    # unsaved timeout as absent instead of inventing 600 seconds. All other
    # catalog fields remain frozen with these named contract changes.
    old_engine = {key: engine[key] for key in ("service_id", "definition", "name", "enabled", "locked", "editable", "values", "key", "capabilities", "models")}
    assert _digest({"models": model_surface, "capabilities": caps, "engine": old_engine}) == "e4be4df70e3ab15ee7db581f005d4188078d45e500356fe20b6391dbbf606a84"


def test_litellm_compiler_and_current_are_frozen() -> None:
    service = _service()
    service["config"]["gateway_lifecycle"] = True
    compiled = compile_litellm_config_from_registry({"services": {"engine-a": service}})
    names = [row["model_name"] for row in compiled["model_list"]]
    assert "current" in names and "current-fast" in names
    assert _digest(compiled) == "837dc6e043ab9d7cf2dfa0ac0916e662c48db9d73bd25aa0999aade926f77dd7"


def test_policy_denial_receipt_is_frozen() -> None:
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate", request_id="req_contract",
                           path="/v1/images/generations")
    decision = evaluate_and_apply(identity(plan_for("chat-only", caps=("chat",))),
                                  make_policy_request(intent="image.generate", model="example"), receipt)
    assert decision.decision == "denied"
    receipt.finish("denied", latency_ms=0, now_ms=1_700_000_000_000)
    assert _digest({audience: receipt.to_dict(audience) for audience in ("client", "admin")}) == "7dc4d305bd62836755f72655b601c9b55305bd64ed028d4fca7f6e3088feee1a"
