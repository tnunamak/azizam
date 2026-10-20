"""Schema 15: only a record holding the old seeded long-audio values is upgraded, from data."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway import admin_store, engine_admin, stt_chunking, stt_chunking_upgrade

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


def test_schema_14_records_get_mode_defaults_only_when_their_seed_values_match(tmp_path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    diarize_seed = {"enabled": False, "backend_timeout_seconds": 1800, "deadline_seconds": 2400,
                    "max_duration_seconds": 14400, "max_field_bytes": 8388608}
    nemotron_seed = {"enabled": True, "target_seconds": 60, "threshold_seconds": 110,
                     "silence_search_seconds": 30, "max_segments": 64, "max_duration_seconds": 3600,
                     "backend_timeout_seconds": 180, "deadline_seconds": 360}
    customized = {**nemotron_seed, "target_seconds": 61}
    registry = {"schema_version": 1, "services": {
        "stt-diarize": _record(dict(diarize_seed)),
        "stt-nemotron": _record(dict(nemotron_seed)),
        "changed": _record(dict(customized)),
    }}
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_version SET version=14")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)",
                     (json.dumps(registry),))
    admin_store.init_db(db)
    stored = admin_store.get_service_registry_with_generation(path=db)["value"]["services"]
    assert stored["stt-diarize"]["config"]["chunking"]["native_long_audio_offline"] is True
    assert stored["stt-diarize"]["config"]["chunking"]["native_long_audio_streaming"] is False
    assert stored["stt-nemotron"]["config"]["chunking"]["native_long_audio_offline"] is False
    assert stored["stt-nemotron"]["config"]["chunking"]["native_long_audio_streaming"] is True
    assert stored["changed"]["config"]["chunking"] == customized


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


def test_native_long_audio_upgrade_is_mode_specific_and_only_applies_to_unchanged_seed_values() -> None:
    rules = stt_chunking_upgrade.load_upgrades()
    diarize_seed = {"enabled": False, "backend_timeout_seconds": 1800, "deadline_seconds": 2400,
                    "max_duration_seconds": 14400, "max_field_bytes": 8388608}
    nemotron_seed = {"enabled": True, "target_seconds": 60, "threshold_seconds": 110,
                     "silence_search_seconds": 30, "max_segments": 64, "max_duration_seconds": 3600,
                     "backend_timeout_seconds": 180, "deadline_seconds": 360}
    diarize = stt_chunking_upgrade.upgraded_chunking("stt-diarize", diarize_seed, rules)
    nemotron = stt_chunking_upgrade.upgraded_chunking("stt-nemotron", nemotron_seed, rules)
    assert diarize["native_long_audio_offline"] is True
    assert diarize["native_long_audio_streaming"] is False
    assert nemotron["native_long_audio_offline"] is False
    assert nemotron["native_long_audio_streaming"] is True
    changed_diarize = {**diarize_seed, "max_duration_seconds": 14000}
    changed_nemotron = {**nemotron_seed, "target_seconds": 61}
    assert stt_chunking_upgrade.upgraded_chunking("stt-diarize", changed_diarize, rules) is None
    assert stt_chunking_upgrade.upgraded_chunking("stt-nemotron", changed_nemotron, rules) == {
        key: value for key, value in changed_nemotron.items() if key != "silence_search_seconds"
    }


def test_record_chunking_limits_allow_exact_max_and_clamp_search_to_half_target(tmp_path) -> None:
    config = stt_chunking.SttExecutionConfig.from_service(
        service_id="s", config_directory=tmp_path,
        service_config={"chunking": {"spool_dir": str(tmp_path / "spool"), "target_seconds": 0.2,
                                     "threshold_seconds": 0.3, "max_segment_seconds": 0.2,
                                     "search_seconds": 0.2}},
    )
    assert config.max_segment_seconds == 0.2
    assert config.search_seconds <= config.target_seconds / 2


def test_planner_accepts_small_target_equal_to_maximum() -> None:
    pieces = stt_chunking.plan_quietest_frame_segments(
        duration_seconds=0.6, quietest_frame=lambda _low, _high, near: near,
        target_seconds=0.2, search_seconds=0.1, max_segment_seconds=0.2, max_segments=8,
    )
    assert max(end - start for start, end in pieces) <= 0.2


def test_configure_stt_allows_small_target_equal_to_hard_maximum(tmp_path) -> None:
    class Store:
        path = tmp_path / "admin.db"

        def get_service_registry_snapshot(self):
            return {"services": {"s": {"capabilities": ["transcriptions"], "config": {"chunking": {}}}}}, 0

        def update_service(self, **_kwargs):
            return {"operator_settings_generation": 1}

    result = engine_admin.EngineService(Store()).configure_stt(
        SimpleNamespace(roles=("owner",)), "s",
        {"target_seconds": 0.5, "max_segment_seconds": 0.5}, expected_generation=0,
        apply_recommendation=True,
    )
    assert result["settings"]["max_segment_seconds"] == 0.5
