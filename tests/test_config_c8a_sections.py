"""C8a of the config-homes program: the images and voices sections (CONFIG_HOMES.md, 10.1).

Images and voices moved out of the startup file the way C7's sections did: release defaults and a schema, the one-time
import, readers through ``runtime()``. The storage roots, the signing secret and the voice store directory stay in the
startup file (``x-azizam-startup-only``) until C10. The job concurrency is a class R value: it sizes two semaphores at
start, so an edit shows as pending until a restart.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import proxy
from gateway import admin_store, config_api, config_layers as layers, config_legacy_import
from tests.config_homes_golden import effective_config, raw_config
from tests.registry_test_helpers import import_blocks
from tests.support.config_snapshot import section_published

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "registry" / "defaults"
C8A_SECTIONS = ("images", "voices")
ABSOLUTE_HOST_PATH = re.compile(r"(?<![\w.:])/(?:media|mnt|home|Users|opt|srv|var|root|etc)(?:/|\b)")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _owner_set(db: Path, section: str, pointer: str, value, base) -> None:
    """One owner edit through the store, as the config API writes it (the revision is checked, the validator runs)."""
    state = admin_store.read_config_state(path=db)
    item = admin_store.ConfigItem(section, pointer, "set", json.dumps(value), json.dumps(base))
    admin_store.apply_config_change(
        [item], if_match={section: str(state["revisions"].get(section, "0"))}, kind="edit", origin="api",
        validate=proxy.CONFIG_HOME.write_validator(None), path=db,
    )


def _import(db: Path, startup: dict) -> dict[str, str]:
    admin_store.init_db(db)
    return {name: outcome.status for name, outcome in import_blocks(db, startup).items()}


def _owned(section: str, block: dict) -> dict:
    return layers.owned_part(proxy.CONFIG_HOME.sections[section], block)


def _publish_edit(tmp_path, monkeypatch, section: str, pointer: str, value, base):
    """Import the tracked startup file, make one owner edit, build the layer and publish it as a reload does."""
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    _import(db, startup)
    _owner_set(db, section, pointer, value, base)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    effective = proxy._load_effective_runtime_config(copy.deepcopy(startup))
    snapshot = proxy.CONFIG_HOME.snapshot(effective)
    monkeypatch.setattr(proxy, "config", snapshot.config)
    proxy.CONFIG_HOME.publish(snapshot)
    return db, snapshot


# -- the release files -----------------------------------------------------------------------------------------------

def test_each_c8a_section_ships_its_two_release_files_and_is_registered():
    for name in C8A_SECTIONS:
        assert (DEFAULTS / f"{name}.yml").is_file(), name
        assert (DEFAULTS / f"{name}.schema.json").is_file(), name
        assert name in proxy.CONFIG_HOME.sections, name


def test_the_release_files_hold_no_host_path_and_no_address():
    for name in C8A_SECTIONS:
        text = (DEFAULTS / f"{name}.yml").read_text(encoding="utf-8") + (DEFAULTS / f"{name}.schema.json").read_text(encoding="utf-8")
        assert not ABSOLUTE_HOST_PATH.search(text), name
        assert not IPV4.search(text), name


def test_the_release_names_no_id_it_does_not_ship():
    images = yaml.safe_load((DEFAULTS / "images.yml").read_text(encoding="utf-8"))
    shipped = {path.stem for path in (ROOT / "manifests" / "workflows").glob("*.yml")}
    for workflow in (images["variations"]["default_workflow"], images["edits"]["default_workflow"],
                     images["edits"]["multiref_default_workflow"]):
        assert workflow in shipped, workflow
    voices = yaml.safe_load((DEFAULTS / "voices.yml").read_text(encoding="utf-8"))
    assert "design_engine" not in voices and "quality_engine" not in voices, "an engine id is the install's, not the release's"


def test_the_storage_roots_and_the_secret_are_not_in_the_release_files():
    images = yaml.safe_load((DEFAULTS / "images.yml").read_text(encoding="utf-8"))
    for key in ("cache_dir", "vram_estimates_file", "signed_url_secret"):
        assert key not in images, key
    assert "payload_dir" not in images["media_jobs"]
    assert "dir" not in yaml.safe_load((DEFAULTS / "voices.yml").read_text(encoding="utf-8"))
    for name, keys in (("images", ["cache_dir", "vram_estimates_file", "signed_url_secret"]), ("voices", ["dir"])):
        assert proxy.CONFIG_HOME.sections[name].schema[layers.STARTUP_ONLY_KEY] == keys


# -- the legacy import gives the startup block -----------------------------------------------------------------------

@pytest.mark.parametrize("name", C8A_SECTIONS)
def test_the_legacy_import_gives_each_section_the_values_of_its_startup_block(name, tmp_path):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    owned = _owned(name, raw_config("tracked")[name])
    assert owned, "the tracked startup file carries the block"
    for pointer in proxy.CONFIG_HOME.sections[name].index.entries_in(owned):
        assert layers.same_value(layers.get_at(effective[name], pointer), layers.get_at(owned, pointer)), (name, pointer)


@pytest.mark.parametrize("name", C8A_SECTIONS)
def test_the_keys_a_section_does_not_own_stay_in_the_startup_file(name, tmp_path):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    for key in proxy.CONFIG_HOME.sections[name].schema[layers.STARTUP_ONLY_KEY]:
        assert key in raw_config("tracked")[name]
        assert key not in effective[name]


def test_the_import_of_the_tracked_file_gives_the_voices_block_whole_but_the_directory(tmp_path):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    block = raw_config("tracked")["voices"]
    assert effective["voices"] == {key: value for key, value in block.items() if key != "dir"}


def test_a_first_run_has_the_release_values(tmp_path):
    effective = effective_config(proxy, "first_run", tmp_path / "admin.db")
    assert effective["images"] == yaml.safe_load((DEFAULTS / "images.yml").read_text(encoding="utf-8"))
    assert effective["voices"] == yaml.safe_load((DEFAULTS / "voices.yml").read_text(encoding="utf-8"))


def test_the_import_runs_once_and_a_second_boot_keeps_the_owner_edit(tmp_path):
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    first = _import(db, startup)
    assert {first[name] for name in C8A_SECTIONS} <= {"imported", "nothing_to_import"}
    _owner_set(db, "images", "/signed_url_ttl", 120, base=3600)
    second = _import(db, startup)
    assert {second[name] for name in C8A_SECTIONS} == {"already_imported"}
    rows = {row["pointer"]: row for row in admin_store.read_config_state(path=db)["rows"] if row["section"] == "images"}
    assert json.loads(rows["/signed_url_ttl"]["value_json"]) == 120


def test_a_block_the_section_does_not_take_is_not_imported(tmp_path):
    db = tmp_path / "admin.db"
    report = _import(db, {"voices": {"max_voices_per_key": 20, "not_a_key": 1}})
    assert report["voices"] == "blocked"
    assert not admin_store.migration_step_recorded("config-homes-import:voices", path=db)


def test_a_value_the_schema_refuses_is_not_imported(tmp_path):
    db = tmp_path / "admin.db"
    report = _import(db, {"voices": {"consent_mode": "sometimes"}, "images": {"max_batch_size": 0}})
    assert report["voices"] == "blocked" and report["images"] == "blocked"


# -- the readers read the snapshot, and an owner edit is live --------------------------------------------------------

def test_the_moved_constants_are_gone_from_the_module():
    for name in (
        "IMAGE_SIGNED_URL_TTL", "IMAGE_VRAM_RESERVE_BYTES", "IMAGE_FREE_VRAM_ON_EXHAUSTION", "IMAGE_MAX_BATCH_SIZE",
        "IMAGE_BACKGROUND_REMOVAL_CFG", "IMAGE_BACKGROUND_REMOVAL_ENABLED", "IMAGE_BACKGROUND_REMOVAL_TIMEOUT",
        "IMAGE_MEDIA_JOBS_CFG", "IMAGE_VARIATIONS_CFG", "IMAGE_EDITS_CFG", "IMAGE_VARIATIONS_DEFAULT_WORKFLOW",
        "IMAGE_VARIATIONS_DENOISE", "IMAGE_MULTIREF_EDIT_DEFAULT_WORKFLOW", "IMAGE_MEDIA_JOB_MAX_ATTEMPTS",
        "IMAGE_MEDIA_JOB_RETRY_BASE_SECONDS", "IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS", "IMAGE_MEDIA_JOB_RESUME_ON_START",
        "_SESSION_MAKE_CFG", "SESSION_MAKE_LIMITS",
    ):
        assert not hasattr(proxy, name), name


def _images(**groups: Any) -> dict[str, Any]:
    merged = copy.deepcopy(yaml.safe_load((DEFAULTS / "images.yml").read_text(encoding="utf-8")))
    for key, value in groups.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            for inner, inner_value in value.items():
                if isinstance(inner_value, dict):
                    merged[key][inner] = {**merged[key].get(inner, {}), **inner_value}
                else:
                    merged[key][inner] = inner_value
        else:
            merged[key] = value
    return merged


def test_the_image_readers_follow_the_snapshot():
    release = _images()
    with section_published("images", release):
        assert proxy.image_signed_url_ttl() == 3600
        assert proxy._image_vram_reserve_bytes() == 1 << 30
        assert proxy._image_free_vram_on_exhaustion() is True
        assert proxy._image_max_batch_size() == 4
        assert proxy._image_background_removal_enabled() is True
        assert proxy._image_background_removal_timeout() == 45.0
        assert proxy._image_variations_default_workflow() == "qwen-image-2.1-edit"
        assert proxy._image_variations_denoise() == 0.75
        assert proxy._image_multiref_edit_default_workflow() == "qwen-image-2.1-edit-multiref"
        assert proxy._image_media_job_max_attempts() == 3
        assert proxy._image_media_job_retry_base_seconds() == 5.0
        assert proxy._image_media_job_retry_max_seconds() == 30.0
        assert proxy._session_make_limits().per_person == 3
    edited = _images(
        signed_url_ttl=61, vram_reserve_bytes=7, free_vram_on_exhaustion=False, max_batch_size=2,
        background_removal={"enabled": False, "decision_timeout": 9},
        variations={"default_workflow": "other-edit", "denoise": 0.5}, edits={"multiref_default_workflow": "other-multiref"},
        media_jobs={"max_attempts": 5, "retry_base_seconds": 1, "retry_max_seconds": 2,
                    "make": {"max_prompt_chars": 100, "max_outstanding_per_person": 1, "max_outstanding": 2}},
    )
    with section_published("images", edited):
        assert proxy.image_signed_url_ttl() == 61
        assert proxy._image_vram_reserve_bytes() == 7
        assert proxy._image_free_vram_on_exhaustion() is False
        assert proxy._image_max_batch_size() == 2
        assert proxy.compute_max_batch(1_000_000_000, 100) == 2
        assert proxy._image_background_removal_enabled() is False
        assert proxy._image_background_removal_timeout() == 9.0
        assert proxy._image_variations_default_workflow() == "other-edit"
        assert proxy._image_variations_denoise() == 0.5
        assert proxy._image_multiref_edit_default_workflow() == "other-multiref"
        assert proxy._image_media_job_max_attempts() == 5
        assert proxy._image_media_job_retry_base_seconds() == 1.0
        assert proxy._image_media_job_retry_max_seconds() == 2.0
        limits = proxy._session_make_limits()
        assert (limits.max_prompt_chars, limits.per_person, limits.household) == (100, 1, 2)
        assert proxy._SESSION_MAKE_RUNTIME.limits == limits


def test_the_edit_default_is_the_images_release_value_unless_the_model_defaults_name_one(monkeypatch):
    """No model id in code: the images section names the edit default, and model_compatibility overrides it."""
    monkeypatch.setattr(proxy, "model_compat_defaults", lambda: {})
    with section_published("images", _images(edits={"default_workflow": "unfamiliar-editor"})):
        assert proxy.image_edit_default_model() == "unfamiliar-editor"
    missing = _images()
    del missing["edits"]["default_workflow"]
    with section_published("images", missing), pytest.raises(proxy.SettingMissing, match="images.edits.default_workflow"):
        proxy.image_edit_default_model()
    monkeypatch.setattr(proxy, "model_compat_defaults", lambda: {"image_edits": "owner-editor"})
    with section_published("images", _images(edits={"default_workflow": "unfamiliar-editor"})):
        assert proxy.image_edit_default_model() == "owner-editor"


def test_the_media_job_resume_follows_the_snapshot_and_the_environment_does_not_move_it(monkeypatch):
    # A restart entry is held by a publish (the snapshot keeps the running value), so the test swaps the holder itself.
    running = proxy.runtime()
    off = dataclasses.replace(running, config={**running.config, "images": _images(media_jobs={"resume_on_start": False})})
    proxy.CONFIG_HOME.holder.publish(off)
    assert proxy._image_media_job_resume_on_start() is False
    monkeypatch.setenv("IMAGE_MEDIA_JOB_RESUME_ON_START", "true")  # C10 (F4): no longer read
    assert proxy._image_media_job_resume_on_start() is False


def test_the_voice_readers_follow_the_snapshot():
    release = yaml.safe_load((DEFAULTS / "voices.yml").read_text(encoding="utf-8"))
    with section_published("voices", release):
        assert proxy._voice_sample_limits().max_seconds == 60.0
        assert proxy._voice_store().limits.max_voices == 20
        assert proxy._voice_design_settings() is None
    with section_published("voices", {**release, "max_voices_per_key": 3, "max_seconds": 30, "consent_mode": "required"}):
        assert proxy._voice_sample_limits().max_seconds == 30.0
        limits = proxy._voice_store().limits
        assert (limits.max_voices, limits.consent_mode) == (3, "required")


def test_the_storage_roots_are_still_read_from_the_startup_file():
    block = raw_config("tracked")
    assert proxy.IMAGE_CACHE_DIR == Path(block["images"]["cache_dir"])
    assert proxy.VRAM_ESTIMATES_FILE == Path(block["images"]["vram_estimates_file"])
    store = proxy._voice_store()
    assert store.root == proxy.CONFIG_PATH.parent / block["voices"]["dir"]


def test_an_owner_edit_of_an_images_entry_reaches_the_readers_through_the_layer_and_a_publish(tmp_path, monkeypatch):
    _publish_edit(tmp_path, monkeypatch, "images", "/signed_url_ttl", 99, base=3600)
    assert proxy.image_signed_url_ttl() == 99
    assert proxy._image_max_batch_size() == raw_config("tracked")["images"]["max_batch_size"]


def test_an_owner_edit_of_a_voices_entry_reaches_the_readers_through_the_layer_and_a_publish(tmp_path, monkeypatch):
    _publish_edit(tmp_path, monkeypatch, "voices", "/max_voices_per_key", 7, base=20)
    assert proxy._voice_store().limits.max_voices == 7
    assert proxy._voice_settings()["design_engine"] == raw_config("tracked")["voices"]["design_engine"]


# -- class R: the concurrency applies at restart ---------------------------------------------------------------------

def test_the_two_restart_entries_are_marked_in_the_schema():
    index = proxy.CONFIG_HOME.sections["images"].index
    for pointer in ("/media_jobs/concurrency", "/media_jobs/resume_on_start"):
        assert index.rule_for(pointer).apply == "restart", pointer
    assert index.rule_for("/media_jobs/max_attempts").apply == "live"


def test_a_restart_entry_edit_shows_as_pending_and_the_running_value_holds(tmp_path, monkeypatch):
    running = proxy.runtime().config["images"]["media_jobs"]["concurrency"]
    db, snapshot = _publish_edit(tmp_path, monkeypatch, "images", "/media_jobs/concurrency", running + 3, base=running)
    assert snapshot.config["images"]["media_jobs"]["concurrency"] == running, "the running value holds"
    state = config_api._State(proxy.CONFIG_HOME, db)
    result = proxy.CONFIG_HOME.build_candidate(state.rows, None)["images"]
    views = {view.pointer: view for view in layers.entry_views(proxy.CONFIG_HOME.sections["images"], result, state.rows["images"])}
    entry = config_api._entry_dict(views["/media_jobs/concurrency"], proxy.runtime().config["images"])
    assert entry["value"] == running + 3 and entry["pending"] is True and entry["running_value"] == running
    assert entry["apply"] == "restart"


def test_a_live_entry_edit_is_not_pending(tmp_path, monkeypatch):
    db, snapshot = _publish_edit(tmp_path, monkeypatch, "images", "/media_jobs/max_attempts", 9, base=3)
    state = config_api._State(proxy.CONFIG_HOME, db)
    result = proxy.CONFIG_HOME.build_candidate(state.rows, None)["images"]
    views = {view.pointer: view for view in layers.entry_views(proxy.CONFIG_HOME.sections["images"], result, state.rows["images"])}
    entry = config_api._entry_dict(views["/media_jobs/max_attempts"], proxy.runtime().config["images"])
    assert entry["pending"] is False
    assert proxy._image_media_job_max_attempts() == 9


def test_the_first_snapshot_takes_every_stored_value_including_a_restart_entry():
    from gateway import config_runtime

    home = config_runtime.ConfigHome(dict(proxy.CONFIG_HOME.sections))
    config = {"images": _images(media_jobs={"concurrency": 5})}
    assert home.snapshot(config).config["images"]["media_jobs"]["concurrency"] == 5
    home.publish(home.snapshot(config))
    later = {"images": _images(media_jobs={"concurrency": 8})}
    held = home.snapshot(later).config
    assert held["images"]["media_jobs"]["concurrency"] == 5
    assert later["images"]["media_jobs"]["concurrency"] == 8, "the config that was handed in is not changed"


def test_the_semaphores_are_sized_from_the_concurrency_at_start():
    assert proxy.IMAGE_MEDIA_JOB_CONCURRENCY == max(1, int(proxy.runtime().config["images"]["media_jobs"]["concurrency"]))


def test_the_image_workflow_defaults_have_no_fallback_name():
    """The release defaults name both workflows; a snapshot without one refuses, naming the setting."""
    for group, key, reader in (
        ("variations", "default_workflow", proxy._image_variations_default_workflow),
        ("edits", "multiref_default_workflow", proxy._image_multiref_edit_default_workflow),
    ):
        dotted = f"images.{group}.{key}"
        for broken in ({}, {group: {}}, {group: {key: None}}, {group: {key: ""}}, {group: {key: 7}}):
            with section_published("images", broken), pytest.raises(proxy.SettingMissing, match=re.escape(dotted)):
                reader()
        with section_published("images", {group: {key: "unfamiliar-workflow"}}):
            assert reader() == "unfamiliar-workflow"
    # Each setting is its own: changing variations leaves the multireference default alone.
    with section_published("images", _images(variations={"default_workflow": "a"},
                                             edits={"multiref_default_workflow": "b"})):
        assert (proxy._image_variations_default_workflow(), proxy._image_multiref_edit_default_workflow()) == ("a", "b")
