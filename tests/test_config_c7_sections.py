"""C7 of the config-homes program: roles, llm, reasoning and model_routing move into config sections (CONFIG_HOMES.md, 10.1).

``models`` is not moved here: its startup list cannot be a section root under the layer's rules (the whole document is
never an entry). These tests cover the four sections that are registered.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import proxy
from gateway import admin_store, config_layers as layers, config_legacy_import
from tests.config_homes_golden import effective_config, raw_config
from tests.registry_test_helpers import import_blocks
from tests.support.config_snapshot import section_published

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "registry" / "defaults"
C7_SECTIONS = ("llm", "model_routing", "reasoning", "roles")
ABSOLUTE_HOST_PATH = re.compile(r"(?<![\w.:])/(?:media|mnt|home|Users|opt|srv|var|root|etc)(?:/|\b)")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _owner_set(db: Path, section: str, pointer: str, value, base) -> None:
    """One owner edit through the store, as the config API writes it (the revision is checked, the validator runs)."""
    state = admin_store.read_config_state(path=db)
    item = admin_store.ConfigItem(section, pointer, "set", json.dumps(value), json.dumps(base))
    admin_store.apply_config_change(
        [item], if_match={section: str(state["revisions"][section])}, kind="edit", origin="api",
        validate=proxy.CONFIG_HOME.write_validator(None), path=db,
    )


def _import(db: Path, startup: dict) -> dict[str, str]:
    """The one-time import of every section's block (gateway/config_legacy_import.py): {section: status}."""
    admin_store.init_db(db)
    return {name: outcome.status for name, outcome in import_blocks(db, startup).items()}


# -- the release files -----------------------------------------------------------------------------------------------

def test_each_c7_section_ships_its_two_release_files_and_is_registered():
    for name in C7_SECTIONS:
        assert (DEFAULTS / f"{name}.yml").is_file(), name
        assert (DEFAULTS / f"{name}.schema.json").is_file(), name
        assert name in proxy.CONFIG_HOME.sections, name


def test_the_release_files_hold_no_host_path_and_no_address():
    for name in C7_SECTIONS:
        text = (DEFAULTS / f"{name}.yml").read_text(encoding="utf-8") + (DEFAULTS / f"{name}.schema.json").read_text(encoding="utf-8")
        assert not ABSOLUTE_HOST_PATH.search(text), name
        assert not IPV4.search(text), name


def test_the_release_ships_no_role_and_no_owner_value():
    # A role names a backend and a model file of this install. The release ships none; the import brings them.
    assert yaml.safe_load((DEFAULTS / "roles.yml").read_text(encoding="utf-8")) == {}
    assert yaml.safe_load((DEFAULTS / "llm.yml").read_text(encoding="utf-8")) == {"pin_loaded_model": True}
    release_routing = yaml.safe_load((DEFAULTS / "model_routing.yml").read_text(encoding="utf-8"))
    assert release_routing == {"switch_timeout": 60, "upstream_read_timeout": 600}
    assert "gguf_dir" not in release_routing


# -- the legacy import gives the startup block -----------------------------------------------------------------------

def test_the_legacy_import_gives_each_section_its_startup_block(tmp_path):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    startup = raw_config("tracked")
    assert effective["roles"] == startup["roles"]
    assert effective["llm"] == startup["llm"]
    assert effective["reasoning"] == startup["reasoning"]


def test_model_routing_gives_its_startup_block(tmp_path):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    assert effective["model_routing"] == raw_config("tracked")["model_routing"]


def test_a_first_run_has_the_release_values_and_no_roles(tmp_path):
    effective = effective_config(proxy, "first_run", tmp_path / "admin.db")
    assert effective["roles"] == {}
    assert effective["llm"] == {"pin_loaded_model": True}
    assert effective["model_routing"] == {"switch_timeout": 60, "upstream_read_timeout": 600}


def test_the_import_runs_once_and_a_second_boot_keeps_the_owner_edit(tmp_path):
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    first = _import(db, startup)
    assert {first[name] for name in C7_SECTIONS} <= {"imported", "nothing_to_import"}
    _owner_set(db, "llm", "/pin_loaded_model", True, base=True)
    second = _import(db, startup)
    assert {second[name] for name in C7_SECTIONS} == {"already_imported"}
    assert admin_store.migration_step_recorded("config-homes-import:llm", path=db)
    rows = {row["pointer"]: row for row in admin_store.read_config_state(path=db)["rows"] if row["section"] == "llm"}
    assert json.loads(rows["/pin_loaded_model"]["value_json"]) is True


def test_a_block_the_section_does_not_take_is_not_imported_and_leaves_no_step(tmp_path):
    db = tmp_path / "admin.db"
    report = _import(db, {"llm": {"pin_loaded_model": False, "not_a_key": 1}})
    assert report["llm"] == "blocked"
    assert not admin_store.migration_step_recorded("config-homes-import:llm", path=db)
    assert admin_store.read_config_state(path=db)["rows"] == []


def test_the_startup_keys_a_section_does_not_own_are_left_out_of_the_import():
    audio = proxy.CONFIG_HOME.sections["audio"]
    assert layers.owned_part(audio, {"workflows_dir": "/x", "default_workflow": "a"}) == {"default_workflow": "a"}
    assert layers.owned_part(proxy.CONFIG_HOME.sections["roles"], {"workflows_dir": "/x"}) == {"workflows_dir": "/x"}


def test_the_startup_only_annotation_is_checked():
    base = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "integer"}}}
    layers.Section.create("ok", {}, {**base, layers.STARTUP_ONLY_KEY: ["b"]})
    for bad in (["a"], "b", [""], [1]):
        with pytest.raises(layers.SectionDataError, match="startup-only"):
            layers.Section.create("bad", {}, {**base, layers.STARTUP_ONLY_KEY: bad})
    nested = {**base, "properties": {"a": {"type": "object", "additionalProperties": False, layers.STARTUP_ONLY_KEY: ["x"]}}}
    with pytest.raises(layers.SectionDataError, match="only at the root"):
        layers.Section.create("nested", {}, nested)


def test_every_startup_only_name_is_a_key_of_the_tracked_startup_block():
    """A misspelt name would strip nothing and block the import at the first boot; catch it here instead."""
    tracked = raw_config("tracked")
    for section_id, section in proxy.CONFIG_HOME.sections.items():
        names = section.schema.get(layers.STARTUP_ONLY_KEY) or []
        block = tracked.get(section_id) or {}
        assert all(name in block for name in names), (section_id, names)


def test_nothing_reads_gguf_dir_at_import():
    """The constant had no reader and fell back to one household's host path; the setting is still accepted."""
    assert not hasattr(proxy, "GGUF_DIR")


# -- the readers read the snapshot, and an owner edit is live --------------------------------------------------------

def test_the_moved_constants_are_gone_from_the_module():
    for name in ("ROLES", "PIN_LOADED_MODEL", "SWITCH_TIMEOUT", "UPSTREAM_READ_TIMEOUT"):
        assert not hasattr(proxy, name), name


def test_upstream_read_timeout_follows_the_snapshot_and_the_environment_does_not_move_it(monkeypatch):
    with section_published("model_routing", {"switch_timeout": 60, "upstream_read_timeout": 42}):
        assert proxy.upstream_read_timeout() == 42.0
    monkeypatch.setenv("UPSTREAM_READ_TIMEOUT", "7")  # C10 (F4): no longer read
    with section_published("model_routing", {"switch_timeout": 60, "upstream_read_timeout": 42}):
        assert proxy.upstream_read_timeout() == 42.0


def test_pin_loaded_model_follows_the_snapshot():
    with section_published("llm", {"pin_loaded_model": False}):
        assert proxy._pin_loaded_model() is False
    with section_published("llm", {"pin_loaded_model": True}):
        assert proxy._pin_loaded_model() is True


def test_roles_follow_the_snapshot():
    with section_published("roles", {"only": {"backend": "kobold", "model": "m.gguf"}}):
        assert set(proxy._roles()) == {"only"}
    with section_published("roles", {}):
        assert proxy._roles() == {}


def test_an_owner_edit_reaches_the_readers_through_the_layer_and_a_publish(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    _import(db, startup)
    _owner_set(db, "model_routing", "/upstream_read_timeout", 99, base=600)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    effective = proxy._load_effective_runtime_config(copy.deepcopy(startup))
    monkeypatch.setattr(proxy, "config", effective)
    proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(effective))
    assert proxy.upstream_read_timeout() == 99.0
    assert proxy.runtime().config["model_routing"]["switch_timeout"] == startup["model_routing"]["switch_timeout"]
