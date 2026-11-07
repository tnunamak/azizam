"""C10: the models and workflow_execution sections that carry the last two blocks the startup file used to hold."""
from __future__ import annotations

from pathlib import Path

import yaml

import proxy
from gateway import config_legacy_import

DEFAULTS = Path(__file__).resolve().parent.parent / "registry" / "defaults"


def test_both_sections_ship_and_are_registered():
    for name in ("models", "workflow_execution"):
        assert (DEFAULTS / f"{name}.yml").is_file() and (DEFAULTS / f"{name}.schema.json").is_file()
        assert name in proxy.CONFIG_HOME.sections


def test_the_release_ships_no_model_entry_():
    assert yaml.safe_load((DEFAULTS / "models.yml").read_text(encoding="utf-8")) in ({}, None)
    assert yaml.safe_load((DEFAULTS / "workflow_execution.yml").read_text(encoding="utf-8")) in ({}, None)


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
