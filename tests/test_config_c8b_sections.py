"""C8b of the config-homes program: the videos and audio sections (CONFIG_HOMES.md, 10.1).

The stable names of both families are in ``model_aliases`` (C3). The audio workflow directory is a storage root and stays
in the startup file until C10.
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
from tests.support.config_snapshot import section_key_published, section_published

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "registry" / "defaults"
C8B_SECTIONS = ("videos", "audio")
ABSOLUTE_HOST_PATH = re.compile(r"(?<![\w.:])/(?:media|mnt|home|Users|opt|srv|var|root|etc)(?:/|\b)")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _release(name: str) -> dict[str, Any]:
    return yaml.safe_load((DEFAULTS / f"{name}.yml").read_text(encoding="utf-8"))


def _owner_set(db: Path, section: str, pointer: str, value, base) -> None:
    state = admin_store.read_config_state(path=db)
    item = admin_store.ConfigItem(section, pointer, "set", json.dumps(value), json.dumps(base))
    admin_store.apply_config_change(
        [item], if_match={section: str(state["revisions"].get(section, "0"))}, kind="edit", origin="api",
        validate=proxy.CONFIG_HOME.write_validator(None), path=db,
    )


def _import(db: Path, startup: dict) -> dict[str, str]:
    admin_store.init_db(db)
    found = config_legacy_import.import_at_boot(proxy.CONFIG_HOME, copy.deepcopy(startup), path=db, registry=None,
                                                log=lambda line: None)
    return {name: outcome.status for name, outcome in found.outcomes.items()}


# -- the release files -----------------------------------------------------------------------------------------------

def test_each_c8b_section_ships_its_two_release_files_and_is_registered():
    for name in C8B_SECTIONS:
        assert (DEFAULTS / f"{name}.yml").is_file(), name
        assert (DEFAULTS / f"{name}.schema.json").is_file(), name
        assert name in proxy.CONFIG_HOME.sections, name


def test_the_release_files_hold_no_host_path_and_no_address():
    for name in C8B_SECTIONS:
        text = (DEFAULTS / f"{name}.yml").read_text(encoding="utf-8") + (DEFAULTS / f"{name}.schema.json").read_text(encoding="utf-8")
        assert not ABSOLUTE_HOST_PATH.search(text), name
        assert not IPV4.search(text), name


@pytest.mark.parametrize("name,graph_dir", [("videos", "workflows/videos"), ("audio", "workflows/audio")])
def test_the_release_names_only_workflows_and_graph_files_it_ships(name, graph_dir):
    release = _release(name)
    workflows = release["workflows"]
    assert release["default_workflow"] in workflows
    for workflow_id, entry in workflows.items():
        assert (ROOT / entry["file"]).is_file(), (workflow_id, entry["file"])
        assert entry["file"].startswith(graph_dir + "/"), workflow_id
        for link in ("i2v_workflow", "chain_workflow"):
            assert entry.get(link, workflow_id) in workflows, (workflow_id, link)
    assert set(release["aliases"].values()) <= set(workflows)
    if name == "videos":
        assert release["extend_workflow"] in workflows
        assert "remix_workflow" not in release, "the release ships no remix workflow; 501 until an owner names one"


def test_only_the_audio_workflow_directory_stays_in_the_startup_file():
    assert layers.STARTUP_ONLY_KEY not in proxy.CONFIG_HOME.sections["videos"].schema
    assert proxy.CONFIG_HOME.sections["audio"].schema[layers.STARTUP_ONLY_KEY] == ["workflows_dir"]


# -- the legacy import gives the startup block -----------------------------------------------------------------------

@pytest.mark.parametrize("name,not_owned", [("videos", set()), ("audio", {"workflows_dir"})])
def test_the_legacy_import_gives_each_section_its_startup_block(tmp_path, name, not_owned):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    startup = raw_config("tracked")[name]
    assert not_owned <= set(startup)
    owned = {key: value for key, value in effective[name].items() if key != "aliases"}
    assert owned == {key: value for key, value in startup.items() if key not in not_owned | {"aliases"}}
    # The older exact names stay served beside the ones folded from model_aliases (stable_aliases.fold_workflow_aliases).
    assert {**effective[name]["aliases"], **startup["aliases"]} == effective[name]["aliases"]


def test_an_install_whose_workflows_differ_keeps_them_through_the_import(tmp_path):
    startup = copy.deepcopy(raw_config("tracked"))
    startup["videos"]["max_total_seconds"] = 12
    startup["videos"]["workflows"]["owner-clip"] = {"file": "workflows/videos/video-ltx-2.5-t2v.json", "max_seconds": 3}
    startup["audio"]["workflows"]["owner-sfx"] = {"file": "workflows/audio/tangoflux.json", "max_seconds": 5}
    db = tmp_path / "admin.db"
    report = _import(db, startup)
    assert {report[name] for name in C8B_SECTIONS} == {"imported"}
    layer = proxy.CONFIG_HOME.layer(copy.deepcopy(startup), path=db, registry=None, legacy_import=True, log=lambda line: None)
    assert layer.config["videos"]["max_total_seconds"] == 12
    assert layer.config["videos"]["workflows"]["owner-clip"] == startup["videos"]["workflows"]["owner-clip"]
    assert layer.config["audio"]["workflows"]["owner-sfx"] == startup["audio"]["workflows"]["owner-sfx"]


def test_a_first_run_has_the_release_values(tmp_path):
    effective = effective_config(proxy, "first_run", tmp_path / "admin.db")
    for name in C8B_SECTIONS:
        assert {key: value for key, value in effective[name].items() if key != "aliases"} == {
            key: value for key, value in _release(name).items() if key != "aliases"}
        assert {**effective[name]["aliases"], **_release(name)["aliases"]} == effective[name]["aliases"]


def test_a_block_the_section_does_not_take_is_not_imported_and_leaves_no_step(tmp_path):
    db = tmp_path / "admin.db"
    report = _import(db, {"videos": {"max_total_seconds": 10, "not_a_key": 1}})
    assert report["videos"] == "blocked"
    assert not admin_store.migration_step_recorded("config-homes-import:videos", path=db)


# -- the readers read the snapshot, and an owner edit is live --------------------------------------------------------

def test_the_moved_constants_are_gone_from_the_module():
    for name in ("VIDEO_DEFAULT_WORKFLOW", "VIDEO_WORKFLOWS", "VIDEO_REMIX_WORKFLOW", "VIDEO_EXTEND_WORKFLOW",
                 "VIDEO_MAX_TOTAL_SECONDS", "VIDEO_ALLOW_UNTRACKED_IDS", "VIDEO_CHAIN_SEGMENT_SECONDS",
                 "AUDIO_DEFAULT_WORKFLOW", "AUDIO_WORKFLOWS_CFG", "videos_cfg", "_audio_cfg"):
        assert not hasattr(proxy, name), name


def test_the_video_readers_follow_the_snapshot():
    value = {
        "default_workflow": "w", "remix_workflow": "r", "extend_workflow": "e", "max_total_seconds": 9,
        "chain_segment_seconds": 3, "allow_untracked_ids": True, "workflows": {"w": {"file": "w.json"}},
    }
    with section_published("videos", value):
        assert proxy._video_default_workflow() == "w"
        assert proxy._video_remix_workflow() == "r"
        assert proxy._video_extend_workflow() == "e"
        assert proxy._video_max_total_seconds() == 9.0
        assert proxy._video_chain_segment_seconds() == 3.0
        assert proxy._video_allow_untracked_ids() is True
        assert dict(proxy._video_workflow_table()) == {"w": {"file": "w.json"}}
        assert proxy._resolve_video_workflow("")[0] == "w"
    with section_published("videos", {}):
        assert proxy._video_remix_workflow() is None and proxy._video_extend_workflow() is None
        assert dict(proxy._video_workflow_table()) == {}


def test_the_audio_readers_follow_the_snapshot():
    value = {"default_workflow": "a", "workflows": {"a": {"file": "a.json"}}}
    with section_published("audio", value):
        assert proxy._audio_default_workflow() == "a"
        assert dict(proxy._audio_workflow_table()) == {"a": {"file": "a.json"}}
        assert proxy._resolve_audio_workflow("")[0] == "a"
    with section_key_published("audio", "workflows", {}):
        assert proxy._resolve_audio_workflow("a") == (None, None)


def test_an_owner_edit_reaches_the_readers_through_the_layer_and_a_publish(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    _import(db, startup)
    _owner_set(db, "videos", "/max_total_seconds", 99, base=40)
    _owner_set(db, "audio", "/default_workflow", "tangoflux", base="ace-step-1.5-xl-turbo")
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    effective = proxy._load_effective_runtime_config(copy.deepcopy(startup))
    monkeypatch.setattr(proxy, "config", effective)
    proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(effective))
    assert proxy._video_max_total_seconds() == 99.0
    assert proxy._audio_default_workflow() == "tangoflux"
    assert proxy._video_default_workflow() == startup["videos"]["default_workflow"]


def test_an_owner_added_workflow_is_one_entry_and_is_refused_when_it_is_not_valid(tmp_path):
    db = tmp_path / "admin.db"
    _import(db, raw_config("tracked"))
    entry = {"file": "workflows/audio/tangoflux.json", "max_seconds": 5}
    state = admin_store.read_config_state(path=db)
    admin_store.apply_config_change(
        [admin_store.ConfigItem("audio", "/workflows/owner-sfx", "set", json.dumps(entry), None)],
        if_match={"audio": str(state["revisions"].get("audio", "0"))}, kind="edit", origin="api",
        validate=proxy.CONFIG_HOME.write_validator(None), path=db,
    )
    state = admin_store.read_config_state(path=db)
    with pytest.raises(Exception):
        admin_store.apply_config_change(
            [admin_store.ConfigItem("audio", "/workflows/bad", "set", json.dumps({"max_seconds": 5, "stray": 1}), None)],
            if_match={"audio": str(state["revisions"].get("audio", "0"))}, kind="edit", origin="api",
            validate=proxy.CONFIG_HOME.write_validator(None), path=db,
        )
