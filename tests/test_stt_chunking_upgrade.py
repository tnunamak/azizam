"""Schema 14: only a record holding the old seeded long-audio values is upgraded, from data."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from gateway import admin_store, stt_chunking, stt_chunking_upgrade

OLD_SEEDED = {"enabled": True, "threshold_seconds": 40, "target_seconds": 30, "silence_search_seconds": 10,
              "silence_noise_db": "-30dB", "max_segments": 128, "backend_timeout_seconds": 180}


def _record(chunking):
    return {"id": "x", "name": "x", "kind": "service", "source": "user", "enabled": True,
            "capabilities": ["transcriptions"], "config": {"host": "127.0.0.1", "port": 1, "chunking": chunking}}


def test_a_record_with_the_old_seeded_values_gets_the_new_keys_and_loses_the_silence_keys() -> None:
    upgraded = stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", OLD_SEEDED, stt_chunking_upgrade.load_upgrades())
    assert upgraded["search_seconds"] == 2 and upgraded["max_segment_seconds"] == 40
    assert not {"silence_search_seconds", "silence_noise_db", "silence_min_seconds"} & set(upgraded)
    assert upgraded["max_segments"] == 128 and upgraded["target_seconds"] == 30


def test_a_record_with_other_values_or_its_own_new_keys_is_left_alone() -> None:
    rules = stt_chunking_upgrade.load_upgrades()
    assert stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", {**OLD_SEEDED, "target_seconds": 60}, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", {"threshold_seconds": 180, "target_seconds": 150, "silence_search_seconds": 120}, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("another-stt", OLD_SEEDED, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", {**OLD_SEEDED, "max_segment_seconds": 50}, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", {**OLD_SEEDED, "search_seconds": 5}, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", {**OLD_SEEDED, "silence_noise_db": "-20dB"}, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("stt-parakeet-gpu", {**OLD_SEEDED, "enabled": False}, rules) is None


def test_the_admin_migration_upgrades_only_the_matching_record_and_runs_once(tmp_path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    other = {**OLD_SEEDED, "target_seconds": 60}
    registry = {"schema_version": 1, "services": {"stt-parakeet-gpu": _record(dict(OLD_SEEDED)), "b": _record(dict(other)),
                                               "c": _record(dict(OLD_SEEDED))}}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=13")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(registry),))
    admin_store.init_db(db)
    stored = admin_store.get_service_registry_with_generation(path=db)["value"]["services"]
    assert stored["stt-parakeet-gpu"]["config"]["chunking"]["max_segment_seconds"] == 40
    assert "silence_search_seconds" not in stored["stt-parakeet-gpu"]["config"]["chunking"]
    assert stored["b"]["config"]["chunking"] == other
    assert stored["c"]["config"]["chunking"] == OLD_SEEDED
    before = json.dumps(stored, sort_keys=True)
    admin_store.init_db(db)
    assert json.dumps(admin_store.get_service_registry_with_generation(path=db)["value"]["services"], sort_keys=True) == before


def test_the_bundled_defaults_are_data_and_a_record_overrides_each_value(tmp_path) -> None:
    defaults = stt_chunking.bundled_chunking_defaults()
    assert set(defaults) == {"threshold_seconds", "target_seconds", "search_seconds", "max_segment_seconds"}
    bare = stt_chunking.SttExecutionConfig.from_service(
        service_id="s", service_config={"chunking": {"spool_dir": str(tmp_path / "spool")}}, config_directory=tmp_path)
    assert (bare.threshold_seconds, bare.target_seconds, bare.search_seconds) == (
        float(defaults["threshold_seconds"]), float(defaults["target_seconds"]), float(defaults["search_seconds"]))
    assert bare.max_segment_seconds == bare.target_seconds + 2 * bare.search_seconds
    own = stt_chunking.SttExecutionConfig.from_service(
        service_id="s", config_directory=tmp_path,
        service_config={"chunking": {"spool_dir": str(tmp_path / "spool"), "threshold_seconds": 41, "target_seconds": 31,
                                     "search_seconds": 3, "max_segment_seconds": 45}})
    assert (own.threshold_seconds, own.target_seconds, own.search_seconds, own.max_segment_seconds) == (41, 31, 3, 45)
    assert bare.chunking_view()["from_bundled_defaults"] == ["threshold_seconds", "target_seconds", "search_seconds", "max_segment_seconds"]


def test_editable_bundled_defaults_allow_zero_search_but_refuse_an_invalid_maximum(tmp_path) -> None:
    fallback = {"threshold_seconds": 60, "target_seconds": 30, "search_seconds": 0, "max_segment_seconds": 40}
    with patch.object(stt_chunking, "bundled_chunking_defaults", return_value=fallback):
        config = stt_chunking.SttExecutionConfig.from_service(
            service_id="s", service_config={"chunking": {"spool_dir": str(tmp_path / "spool")}}, config_directory=tmp_path)
        assert (config.search_seconds, config.max_segment_seconds) == (0, 40)
    for bad in ("unreadable", float("nan"), float("inf"), -1):
        with patch.object(stt_chunking, "bundled_chunking_defaults", return_value={**fallback, "max_segment_seconds": bad}):
            with pytest.raises(stt_chunking.SttConfigurationError):
                stt_chunking.SttExecutionConfig.from_service(
                    service_id="s", service_config={"chunking": {"spool_dir": str(tmp_path / "spool")}}, config_directory=tmp_path)
