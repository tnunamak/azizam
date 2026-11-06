"""Decision D30: the executable LiteLLM config is built only in a runtime-private module; the management route shows a preview.

``gateway/litellm_substrate.py`` compiles from public data and cannot read a secret. ``gateway/litellm_runtime.py`` gives it a
materializer that resolves what a record only references, and writes the result to a private file. The management route
answers with the substrate's own output, which carries a header's name and a placeholder for its value and an endpoint
without its credentials.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from gateway import admin_store, litellm_runtime, litellm_substrate, record_carriers, runtime_secrets, secret_refs, service_registry
from tests.support import canary
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

M = canary.MARKERS


def _record(db):
    legacy = {
        "id": "engine-a", "name": "Engine A", "kind": "provider", "source": "ui", "enabled": True, "capabilities": ["chat"],
        "config": {"base_url": f"http://{M['url-user']}:{M['url-password']}@engine.invalid:8080/v1?token={M['url-query']}",
                   "protocol": "openai-compatible", "models": [{"id": "first", "upstream_model": "up"}],
                   "headers": [{"name": "X-Region", "value": M["header-neutral"]}]},
    }
    return record_carriers.externalize_service(legacy, secret_refs.putter(db))[0]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


def test_the_substrate_alone_compiles_the_preview_with_no_value_and_no_credential(db):
    compiled = litellm_substrate.compile_litellm_substrate_config({"engine-a": _record(db)})
    params = compiled["model_list"][0]["litellm_params"]
    assert params["extra_headers"] == {"X-Region": litellm_substrate.HEADER_PLACEHOLDER}
    assert params["api_base"] == "http://engine.invalid:8080/v1"
    assert not canary.leaks_in(json.dumps(compiled))


def test_the_runtime_module_renders_the_executable_config_with_the_values_the_router_needs(db):
    compiled = litellm_runtime.compile_executable_config({"services": {"engine-a": _record(db)}})
    params = compiled["model_list"][0]["litellm_params"]
    assert params["extra_headers"] == {"X-Region": M["header-neutral"]}
    assert params["api_base"] == f"http://{M['url-user']}:{M['url-password']}@engine.invalid:8080/v1?token={M['url-query']}"


def test_the_preview_and_the_executable_config_differ_only_in_what_a_secret_supplies(db):
    record = _record(db)
    preview = litellm_substrate.compile_litellm_substrate_config({"engine-a": record})
    executable = litellm_runtime.compile_executable_config({"services": {"engine-a": record}})
    for config in (preview, executable):
        config["model_list"][0]["litellm_params"].pop("extra_headers")
        config["model_list"][0]["litellm_params"].pop("api_base")
    assert preview == executable


def test_the_artifact_is_written_privately_and_replaced_in_one_step(db, tmp_path):
    registry = {"services": {"engine-a": _record(db)}}
    path = litellm_runtime.write_litellm_config_artifact(registry, base_dir=tmp_path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert M["header-neutral"] in path.read_text()
    assert not list(tmp_path.glob(".litellm.config.yaml.*.tmp"))


def test_a_secret_that_cannot_be_read_leaves_its_header_out_of_the_executable_config(db, tmp_path):
    record = _record(db)
    runtime_secrets.configure(tmp_path / "empty.db")
    admin_store.init_db(tmp_path / "empty.db")
    compiled = litellm_runtime.compile_executable_config({"services": {"engine-a": record}})
    params = compiled["model_list"][0]["litellm_params"]
    assert "extra_headers" not in params and params["api_base"] == "http://engine.invalid:8080/v1"


def test_the_management_route_answers_with_the_preview(gateway):
    owner, *_ = owner_and_admin(gateway)
    put = secret_refs.putter(gateway.path)
    registry = service_registry.normalize_service_registry({"schema_version": 2, "services": {
        "engine-a": record_carriers.externalize_service({
            "id": "engine-a", "name": "Engine A", "kind": "provider", "source": "ui", "enabled": True, "capabilities": ["chat"],
            "config": {"base_url": f"http://{M['url-user']}:{M['url-password']}@engine.invalid:8080/v1", "protocol": "openai-compatible",
                       "models": [{"id": "first", "upstream_model": "up"}], "headers": [{"name": "X-Region", "value": M["header-neutral"]}]}}, put)[0]}})
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=0, updated_by="test", audit_action="test", audit_actor_kind=None, audit_actor_id=None,
        audit_actor_label=None, audit_target_kind=None, audit_target_id=None, path=gateway.path)

    response = owner.get("/admin/api/operator/litellm-config")

    assert response.status_code == 200, response.text
    assert not canary.leaks_in(response.text)
    params = response.json()["litellm_config"]["model_list"][0]["litellm_params"]
    assert params["extra_headers"] == {"X-Region": litellm_substrate.HEADER_PLACEHOLDER}
