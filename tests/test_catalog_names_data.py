"""The catalog owner default and the loaded-model alias names live in registry/catalog_names.yml, once.

Each builder reads the data at call time, so a changed value reaches every listing; a record that declares its own owner
keeps it. The static pins below hold the former code copies from coming back.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import proxy
from gateway import catalog_names, decisions_api, litellm_substrate
from tests.support.config_snapshot import derived_patch, section_published

ROOT = Path(__file__).resolve().parent.parent
OTHER_OWNER = "acme-house"
OTHER_ALIASES = (("loaded-now", False), ("loaded-now-quick", True))


def _names(tmp_path: Path, **changes) -> catalog_names.CatalogNames:
    document = yaml.safe_load(catalog_names.BUNDLED_PATH.read_text(encoding="utf-8"))
    document.update(changes)
    path = tmp_path / "catalog_names.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return catalog_names.load(path)


@pytest.fixture
def other_names(tmp_path):
    names = _names(
        tmp_path,
        catalog_owner=OTHER_OWNER,
        loaded_model_aliases=[{"name": n, "fast": f} for n, f in OTHER_ALIASES],
    )
    with patch.object(catalog_names, "NAMES", names):
        yield names


def test_shipped_values_are_what_listings_showed_before() -> None:
    assert catalog_names.NAMES.owner == "azizam"
    assert catalog_names.NAMES.loaded_aliases == (("current", False), ("current-fast", True))
    assert (proxy.USE_LOADED_MODEL_ALIAS, proxy.USE_LOADED_FAST_MODEL_ALIAS) == ("current", "current-fast")


def test_decision_model_default_owner_is_the_catalog_owner() -> None:
    assert decisions_api.DecisionModel(id="x").owned_by == "azizam"


def _roles(roles: dict, models: dict) -> list[dict]:
    with (
        patch.dict(proxy.config, {"backends": {"kobold": {"enabled": True, "host": "127.0.0.1", "port": 1}}}),
        section_published("roles", roles),
        patch.object(proxy, "_models_catalog", lambda: models),
        patch.object(proxy, "embeddings_model", lambda: "embed-x"),
        derived_patch("EMBEDDINGS_ENABLED", True),
    ):
        return proxy._build_role_catalog_entries()


ROLE = {"backend": "kobold", "model": "/m/a.gguf"}
CATALOG_ROW = {"id": "m", "backend": "kobold", "path": "/m/b.gguf", "description": ""}


def test_role_model_and_embedding_entries_follow_the_data(other_names) -> None:
    entries = _roles({"r": ROLE}, {"m": CATALOG_ROW})
    assert {e["id"] for e in entries} == {"r", "m", "embed-x"}
    assert {e["owned_by"] for e in entries} == {OTHER_OWNER}


def test_a_role_that_declares_its_owner_keeps_it(other_names) -> None:
    entries = _roles({"r": {**ROLE, "owned_by": "the-owner"}, "s": ROLE}, {})
    assert {e["id"]: e["owned_by"] for e in entries if e["id"] in {"r", "s"}} == {"r": "the-owner", "s": OTHER_OWNER}


def test_workflow_entries_follow_the_data(other_names) -> None:
    entries = proxy._build_workflow_model_entries()
    assert entries
    assert {e["owned_by"] for e in entries} == {OTHER_OWNER}


def test_stable_alias_entries_follow_the_data(other_names) -> None:
    view = {"chat": {"stable": [{"alias": "fast", "resolves_to": "x", "configured": True}]}}
    with patch.object(proxy, "_gateway_aliases_provider", lambda: view):
        entries = proxy._stable_alias_model_entries()
    assert [(e["id"], e["owned_by"]) for e in entries] == [("fast", OTHER_OWNER)]


def test_decision_models_follow_the_data_and_keep_a_declared_owner(other_names) -> None:
    assert decisions_api.DecisionModel(id="d").owned_by == OTHER_OWNER
    assert catalog_names.catalog_owner(decisions_api.DecisionModel(id="d", owned_by="declared").owned_by) == "declared"


def test_router_alias_entries_follow_the_data(other_names) -> None:
    record = {
        "id": "local", "name": "Local", "capabilities": ["openai"],
        "config": {"protocol": "openai-compatible", "gateway_lifecycle": True,
                   "models": [{"id": "default", "upstream_model": "loaded.gguf"}]},
    }
    compiled = litellm_substrate.compile_litellm_substrate_config({"local": record})
    by_name = {e["model_name"]: e for e in compiled["model_list"] if e["model_info"].get("gateway_current_loaded")}
    assert {name: e["model_info"]["gateway_current_loaded_fast"] for name, e in by_name.items()} == dict(OTHER_ALIASES)


@pytest.mark.parametrize("change", [
    {"loaded_model_aliases": [{"name": "a", "fast": False}]},
    {"loaded_model_aliases": [{"name": "a", "fast": False}, {"name": "b", "fast": False}]},
    {"loaded_model_aliases": [{"name": "a", "fast": False}, {"name": "a", "fast": True}]},
    {"catalog_owner": ""},
])
def test_a_bad_file_is_refused_with_its_path(tmp_path, change) -> None:
    with pytest.raises(ValueError, match="catalog_names.yml"):
        _names(tmp_path, **change)


@pytest.mark.parametrize("source", ["proxy.py", "gateway/litellm_substrate.py", "gateway/decisions_api.py"])
def test_no_code_copy_of_the_names(source) -> None:
    text = (ROOT / source).read_text(encoding="utf-8")
    for literal in ('"current-fast"', 'ALIAS = "current"', '("current", False)', '"owned_by": "azizam"', 'owned_by: str = "azizam"'):
        assert literal not in text, f"{source} names {literal}; it belongs in registry/catalog_names.yml"
