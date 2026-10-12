import copy
from unittest.mock import Mock

import pytest

from gateway import admin_store, service_registry as sr


def test_startup_lost_seed_cas_recomputes_from_the_winner(monkeypatch, tmp_path):
    path = tmp_path / "unused-fake-admin.db"
    config = {"backends": {}}
    seed = sr.normalize_service_registry({"updated_at": 1, "services": {}})
    winner = sr.normalize_service_registry(
        {
            "updated_at": 1,
            "services": {
                "operator-service": {
                    "name": "Operator edit",
                    "config": {"operator_value": "must survive"},
                }
            },
        }
    )
    winner_before = copy.deepcopy(winner)
    merged = copy.deepcopy(winner)
    merged["services"]["operator-service"]["config"]["seeded_field"] = "new metadata"

    read = Mock(
        side_effect=[
            None,
            {"value": winner, "operator_settings_generation": 7},
        ]
    )
    build = Mock(return_value=seed)
    backfill = Mock(return_value=merged)
    write = Mock(
        side_effect=[
            admin_store.OperatorSettingsGenerationConflict("injected race"),
            {"value": merged},
        ]
    )
    monkeypatch.setattr(admin_store, "get_service_registry_with_generation", read)
    monkeypatch.setattr(sr, "build_service_registry_seed", build)
    monkeypatch.setattr(sr, "backfill_seed_catalog_metadata", backfill)
    monkeypatch.setattr(admin_store, "write_service_registry_with_audit", write)

    result = sr.ensure_service_registry(config, path=path, actor="test-boot")

    assert result == merged
    assert winner == winner_before
    assert read.call_count == 2
    assert all(c.kwargs == {"path": path} for c in read.call_args_list)
    build.assert_called_once_with(config)
    backfill.assert_called_once_with(winner, proxy_config=config)
    assert [c.args[0] for c in write.call_args_list] == [seed, merged]
    assert [c.kwargs["expected_generation"] for c in write.call_args_list] == [0, 7]
    assert [c.kwargs["audit_action"] for c in write.call_args_list] == [
        "operator.service_registry.seed",
        "operator.service_registry.backfill",
    ]
    for call in write.call_args_list:
        assert call.kwargs["path"] == path
        assert call.kwargs["updated_by"] == "test-boot"
        assert call.kwargs["audit_actor_id"] == "test-boot"
        assert call.kwargs["audit_target_id"] == sr.SERVICE_REGISTRY_SETTING_KEY
    assert write.call_args_list[1].kwargs["audit_metadata"]["service_count"] == 1
    assert not path.exists()


def test_startup_conflicts_stop_after_three_fresh_attempts(monkeypatch, tmp_path):
    path = tmp_path / "unused-fake-admin.db"
    seed = sr.normalize_service_registry({"updated_at": 1, "services": {}})
    attempts = []

    def read(*, path):
        attempts.append(path)
        if len(attempts) > 3:
            raise AssertionError("startup exceeded its three-attempt contract")
        return None

    build = Mock(return_value=seed)
    write = Mock(
        side_effect=admin_store.OperatorSettingsGenerationConflict("injected race")
    )
    monkeypatch.setattr(admin_store, "get_service_registry_with_generation", read)
    monkeypatch.setattr(sr, "build_service_registry_seed", build)
    monkeypatch.setattr(admin_store, "write_service_registry_with_audit", write)

    with pytest.raises(RuntimeError, match="bounded startup retry"):
        sr.ensure_service_registry({}, path=path, actor="test-boot")

    assert attempts == [path, path, path]
    assert build.call_count == 3
    assert write.call_count == 3
    assert [c.kwargs["expected_generation"] for c in write.call_args_list] == [0, 0, 0]
    assert not path.exists()
