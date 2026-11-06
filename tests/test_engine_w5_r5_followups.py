"""Regressions found in the independent W5 r4 review."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway import engine_admin, service_registry
from gateway.engine_connection import inference_connection
from gateway.litellm_substrate import compile_litellm_config_from_registry, router_reply_timeout
from tests.test_engine_w5_review import Store, record
from tests.test_engine_admin_routes import _edit, _put_registry, _record, _registry  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401
from tests.test_admin_cannot_act_as_owner import _etag  # noqa: F401


def test_clearing_hop_reply_timeout_leaves_router_limit_absent(tmp_path):
    saved = record(definition="openai-compatible", definition_version=1, timeout=1800)
    store = Store(saved, tmp_path / "admin.db")
    service = engine_admin.EngineService(store, hop_timeout=lambda: 1800.0)
    before = service.view("review", SimpleNamespace(roles=("owner",)))
    asyncio.run(service.update(SimpleNamespace(roles=("owner",)), "review", {
        "definition": before["definition"], "disabled": True,
        "values": {**before["values"], "reply_timeout_s": ""},
    }, expected_generation=0))
    assert "timeout" not in store.written
    saved["config"] = store.written
    registry = {"services": {"review": saved}}
    assert inference_connection(saved, "/chat/completions", fallback_address="", fallback_timeout=1800,
                                db_path=store.path, env=lambda _: None).read_timeout == 1800
    assert router_reply_timeout(registry, "current") is None
    compiled = compile_litellm_config_from_registry(registry)
    assert all("timeout" not in row["litellm_params"] for row in compiled["model_list"])


def test_administrator_cannot_put_absent_hop_timeout_as_600(gateway):
    owner, _, _, admin = owner_and_admin(gateway)
    registry = _registry(gateway)
    registry["services"]["hop-absent"] = service_registry.service_from_backend_config("hop-absent", {
        "host": "127.0.0.1", "port": 6061, "protocol": "openai-compatible",
        "capabilities": ["chat", "openai"], "gateway_lifecycle": True,
        "models": [{"id": "chat", "upstream_model": "up"}],
    }, source="ui")
    _put_registry(gateway, registry)
    assert "timeout" not in _record(gateway, "hop-absent")["config"]
    changed = admin.get("/admin/api/operator/service-registry").json()["service_registry"]
    changed["services"]["hop-absent"]["config"]["timeout"] = 600
    refused = admin.put("/admin/api/operator/service-registry",
                        headers={**csrf_for(admin), "If-Match": _etag(admin)}, json=changed)
    assert refused.status_code == 403, refused.text
    assert "timeout" not in _record(gateway, "hop-absent")["config"]


def test_administrator_recheck_is_refused_before_outbound_probe(gateway, monkeypatch):
    _, _, _, admin = owner_and_admin(gateway)
    registry = _registry(gateway)
    registry["services"]["hop-recheck"] = record(definition="openai-compatible", definition_version=1)
    _put_registry(gateway, registry)
    run_check = AsyncMock(side_effect=AssertionError("recheck made an outbound probe"))
    monkeypatch.setattr(engine_admin, "run_check", run_check)
    refused = _edit(admin, "hop-recheck", {"definition": "openai-compatible", "recheck": True})
    assert refused.status_code == 403, refused.text
    run_check.assert_not_called()
