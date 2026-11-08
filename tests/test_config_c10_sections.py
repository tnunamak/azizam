"""C10: the models and workflow_execution sections that carry the last two blocks the startup file used to hold."""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import proxy
from gateway import config_legacy_import, model_metadata
from gateway.admin_runtime import create_admin_service_account_key
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture

DEFAULTS = Path(__file__).resolve().parent.parent / "registry" / "defaults"


def test_both_sections_ship_and_are_registered():
    for name in ("models", "workflow_execution"):
        assert (DEFAULTS / f"{name}.yml").is_file() and (DEFAULTS / f"{name}.schema.json").is_file()
        assert name in proxy.CONFIG_HOME.sections


def test_the_release_ships_no_model_entry_():
    assert yaml.safe_load((DEFAULTS / "models.yml").read_text(encoding="utf-8")) in ({}, None)
    # workflow_execution ships the I7 keys (docs/design/CODE_TO_DATA_S4_S8.md) and nothing else
    assert yaml.safe_load((DEFAULTS / "workflow_execution.yml").read_text(encoding="utf-8")) == {
        "max_wait_s": 60, "history_depth": 20,
        "bounds": {"max_template_bytes": 524288, "max_nodes": 2000, "max_depth": 32}}


def test_the_old_models_list_becomes_the_mapping():
    old = [{"id": "Foo", "backend": "b", "path": "/x.gguf"}, {"backend": "b", "path": "no id"}, "junk"]
    assert config_legacy_import.legacy_block("models", old) == {"Foo": {"backend": "b", "path": "/x.gguf"}}
    assert config_legacy_import.legacy_block("models", {"a": {}}) == {"a": {}}
    assert config_legacy_import.legacy_block("roles", old) is old


def test_the_catalog_reads_the_mapping(monkeypatch):
    backend = next(iter(proxy.config["backends"]))
    raw = {"Foo": {"backend": backend, "path": " /x.gguf ", "description": "d"},
           "bad": {"backend": "no-such-backend", "path": "/y"}, "nopath": {"backend": backend}}
    catalog = proxy._normalize_models_catalog(raw)
    assert catalog == {"foo": {"id": "foo", "backend": backend, "path": "/x.gguf", "mmproj": None, "description": "d"}}
    assert proxy._normalize_models_catalog([{"id": "x", "backend": backend, "path": "/y"}]) == {}
    assert proxy._normalize_models_catalog(None) == {}


# --- model_metadata: the one field of an entry that the metadata reader keeps --------------------------------------------

METADATA = {"type": "language", "input_modalities": ["text", "image"], "output_modalities": ["text"], "max_tokens": 4096, "tags": ["tool-use", "vision"]}


def _models_section():
    return proxy.CONFIG_HOME.sections["models"]


def test_the_models_schema_accepts_the_metadata_the_reader_reads_and_refuses_what_it_cannot():
    from gateway import config_layers as layers

    section = _models_section()
    entry = {"backend": "b", "path": "/x.gguf", "model_metadata": METADATA}
    assert layers.validate_section(section, {"foo": entry}) == []
    for bad in ({"type": "gadget"}, {"max_tokens": 0}, {"max_tokens": "big"}, {"tags": ["magic"]},
                {"input_modalities": ["smell"]}, {"output_modalities": ["smell"]}, {"output_modalities": "text"}, {"extra": 1}):
        problems = layers.validate_section(section, {"foo": {**entry, "model_metadata": bad}})
        assert problems, bad


def test_an_old_list_import_keeps_the_model_metadata(live):
    from gateway import config_layers as layers

    old = [{"id": "Foo", "backend": "b", "path": "/x.gguf", "model_metadata": METADATA}]
    block = config_legacy_import.legacy_block("models", old)
    plan = config_legacy_import.plan_import(proxy.CONFIG_HOME, "models", block, {n: [] for n in proxy.CONFIG_HOME.sections}, {})
    assert plan.blocked == ()
    effective = layers.apply_overrides(_models_section().defaults, list(plan.overrides))
    assert effective["Foo"]["model_metadata"] == METADATA


def test_the_config_api_round_trips_an_entry_with_model_metadata_and_the_reader_reports_it(live):
    owner, _, _ = create_admin_service_account_key("c10-owner", role="owner", path=live)
    proxy.reload_runtime_config()
    client = TestClient(proxy.app)
    headers = {"Authorization": f"Bearer {owner}"}
    revision = client.get("/admin/api/config/sections/models", headers=headers).json()["revision"]
    entry = {"backend": "b", "path": "/x.gguf", "model_metadata": METADATA}
    written = client.patch("/admin/api/config/changes", headers=headers, json={
        "items": [{"section": "models", "pointer": "/Foo", "op": "set", "value": entry}], "if_match": {"models": revision}})
    assert written.status_code == 200, written.text
    view = client.get("/admin/api/config/sections/models", headers=headers).json()
    assert [e["value"] for e in view["entries"] if e["pointer"] == "/Foo"] == [entry]
    refused = client.patch("/admin/api/config/changes", headers=headers, json={
        "items": [{"section": "models", "pointer": "/Bar", "op": "set", "value": {**entry, "model_metadata": {"type": "gadget"}}}],
        "if_match": {"models": view["revision"]}})
    assert refused.status_code == 422
    assert proxy._declared_model_metadata_index({}) == {"foo": METADATA}
    sources = proxy._model_metadata_sources({}, {})
    assert model_metadata.apply_declared(None, sources.declared("Foo")).to_fields() == {
        "type": "language", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        "max_tokens": 4096, "tags": ["tool-use", "vision"]}


# --- the settings views and the per-section legacy import are gone (CONFIG_HOMES.md, C10) -------------------------------

ABSENT = (
    ("GET", "/admin/api/settings/effective"),
    ("GET", "/admin/api/settings/effective/export"),
    ("POST", "/admin/api/config/sections/models/legacy-import"),
)


def test_the_effective_settings_views_and_the_section_legacy_import_have_no_route():
    from starlette.routing import Match

    for method, path in ABSENT:
        scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}
        matched = [r.path for r in proxy.app.routes if r.matches(scope)[0] != Match.NONE and "{path:path}" not in r.path]  # the catch-all is the unknown-route answer
        assert matched == [], (method, path)


def test_for_the_owner_each_of_them_answers_as_an_unknown_route_does(live):
    owner, _, _ = create_admin_service_account_key("c10-absent-owner", role="owner", path=live)
    client = TestClient(proxy.app)
    headers = {"Authorization": f"Bearer {owner}"}
    assert client.get("/admin/api/config", headers=headers).status_code == 200  # the key opens the owner's routes
    unknown = {m: client.request(m, "/admin/api/settings/never-a-route", headers=headers, json={} if m == "POST" else None)
               for m in ("GET", "POST")}
    for method, path in ABSENT:
        answer = client.request(method, path, headers=headers, json={} if method == "POST" else None)
        assert answer.status_code in (401, 404, 405), (method, path, answer.status_code)
        assert answer.status_code == unknown[method].status_code and answer.json() == unknown[method].json(), (method, path)
