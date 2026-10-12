import pytest

from gateway import admin_store, service_registry as sr


def _winner_document():
    return sr.normalize_service_registry(
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


def _write_winner(path):
    return admin_store.write_service_registry_with_audit(
        _winner_document(),
        expected_generation=0,
        updated_by="other-boot",
        audit_action="operator.service_registry.seed",
        audit_actor_kind="admin",
        audit_actor_id="other-boot",
        audit_actor_label="System",
        audit_target_kind="operator_setting",
        audit_target_id=sr.SERVICE_REGISTRY_SETTING_KEY,
        path=path,
    )


def _stale_first_reads(monkeypatch, reads):
    """Make the first `reads` registry reads return the pre-winner snapshot."""
    real_get = admin_store.get_service_registry_with_generation
    seen = []

    def get(*, path):
        seen.append(path)
        if len(seen) <= reads:
            return None
        return real_get(path=path)

    monkeypatch.setattr(admin_store, "get_service_registry_with_generation", get)
    return seen


def test_startup_lost_seed_race_keeps_the_winner_and_audits_one_seed(monkeypatch, tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    _write_winner(path)
    reads = _stale_first_reads(monkeypatch, 1)

    result = sr.ensure_service_registry({"backends": {}}, path=path, actor="test-boot")

    assert len(reads) == 2
    durable = admin_store.get_service_registry_with_generation(path=path)
    assert result == durable["value"]
    assert result["services"]["operator-service"]["config"]["operator_value"] == "must survive"
    print("GEN", durable["operator_settings_generation"], [(e["action"], e["actor_id"]) for e in admin_store.list_audit(path=path)])
    assert False


def test_startup_conflicts_stop_after_three_fresh_attempts(monkeypatch, tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    _write_winner(path)
    reads = _stale_first_reads(monkeypatch, 99)
    before = admin_store.get_service_registry_with_generation(path=path)

    with pytest.raises(RuntimeError, match="bounded startup retry"):
        sr.ensure_service_registry({"backends": {}}, path=path, actor="test-boot")

    assert len(reads) == 3
    assert admin_store.get_service_registry_with_generation(path=path) == before
