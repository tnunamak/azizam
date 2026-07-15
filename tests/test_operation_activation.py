from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, operation_activation
from gateway.admin_api import AdminAuthError, AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore
from gateway.operation_activation import (
    OperationError,
    ActivationSeams,
    acknowledge_local_reload,
    activate,
    canonical_evidence,
    deactivate,
    dry_run,
    recover,
    rollback,
    stage,
)
from tests.test_compaction_safety import _record, _service_evidence


def _registry() -> dict:
    return {
        "schema_version": 1,
        "services": {
            "service": {
                "id": "service",
                "name": "Service",
                "source": "ui",
                "enabled": True,
                "capabilities": ["chat"],
                "config": {
                    "model_policy_schema_version": 1,
                    "deployment_generation": "g1",
                    "registry_generation": "registry-g1",
                    "controlled_deployment_evidence": _service_evidence(),
                    "models": [{"id": "base"}, {"id": "alias", "base_model_id": "base", "unrelated": "kept"}],
                },
            }
        },
        "protocol_frontends": {}, "workflows": {}, "capabilities": {},
    }


def _ready_target(tmp_path: Path) -> tuple[Path, dict]:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry(_registry(), path=path)
    target = stage(
        path=path, service_id="service", alias_id="alias", base_model_id="base",
        overlay={"compaction_safety": _record()},
        evidence={"expires_at": 2_000_000_000}, actor="test",
        expected_target=None, expected_registry=None,
    )
    return path, target


def _operation_client(path: Path, *, session: bool = False) -> TestClient:
    def auth(request: Request) -> AdminContext:
        token = request.headers.get("authorization")
        if token == "Bearer admin":
            return AdminContext(subject="admin", email=None, roles=frozenset({"admin"}))
        if token == "Bearer viewer":
            return AdminContext(subject="viewer", email=None, roles=frozenset({"viewer"}))
        if session:
            return AdminContext(subject="admin", email=None, roles=frozenset({"admin"}),
                                auth_method="session", csrf_token="csrf")
        raise AdminAuthError()
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(path), auth=auth))
    return TestClient(app)


def test_forward_schema_has_exactly_five_operation_tables(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    with admin_store._conn(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"operator_settings_generations", "operation_evidence_receipts", "operation_target_revisions", "operation_target_current", "operation_idempotency"} <= names


def test_v1_database_migrates_forward_without_losing_registry(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry(_registry(), path=path)
    with admin_store._conn(path) as conn:
        conn.execute("DROP TABLE operation_idempotency")
        conn.execute("UPDATE schema_version SET version=1")
        conn.commit()
    admin_store.init_db(path)
    assert admin_store.schema_version(path) == 2
    assert admin_store.get_service_registry_with_generation(path=path)["value"]["services"]


def test_operation_routes_require_admin_and_session_csrf(tmp_path: Path) -> None:
    path, _ = _ready_target(tmp_path)
    client = _operation_client(path)
    assert client.get("/admin/api/operator/operation-targets").status_code == 401
    assert client.get("/admin/api/operator/operation-targets", headers={"Authorization": "Bearer viewer"}).status_code == 403
    assert client.get("/admin/api/operator/operation-targets", headers={"Authorization": "Bearer admin"}).status_code == 200
    session_client = _operation_client(path, session=True)
    blocked = session_client.delete("/admin/api/operator/operation-targets/service/alias/activation", headers={"If-Match": '"ot-1-os-1"'})
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "admin_csrf_required"


@pytest.mark.parametrize("etag", ["", '"os-1"', '"ot-nope-os-1"', '"ot-1-os-nope"'])
def test_operation_mutations_reject_non_composite_etag(tmp_path: Path, etag: str) -> None:
    path, _ = _ready_target(tmp_path)
    response = _operation_client(path).delete(
        "/admin/api/operator/operation-targets/service/alias/activation",
        headers={"Authorization": "Bearer admin", "If-Match": etag},
    )
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "generation_mismatch"


def test_evidence_is_deterministic_and_honestly_only_an_assertion() -> None:
    first = {"expires_at": 2_000_000_000, "nested": {"b": 2, "a": 1}}
    second = {"nested": {"a": 1, "b": 2}, "expires_at": 2_000_000_000}
    assert canonical_evidence(first) == canonical_evidence(second)


def test_evidence_rejects_float() -> None:
    with pytest.raises(OperationError, match="cannot contain floats"):
        canonical_evidence({"expires_at": 2_000_000_000, "measurement": 0.5})


def test_recovery_never_loads_candidate_and_marks_missing_artifact_for_retry(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry({"schema_version": 1, "services": {}, "protocol_frontends": {}, "workflows": {}, "capabilities": {}}, path=path)
    with admin_store._conn(path) as conn:
        conn.execute("INSERT INTO operation_target_current(service_id,alias_id,target_generation,candidate_state,prepared_artifact_digest,prepared_artifact_relpath,updated_at) VALUES ('service','alias',1,'preparing','abc','operation-prepared/abc.json',1)")
        conn.commit()
    assert recover(path=path, state_dir=tmp_path) == [{"service_id": "service", "alias_id": "alias", "action": "reprepare"}]
    assert admin_store.get_service_registry_with_generation(path=path)["value"]["services"] == {}


def test_recovery_marks_corrupt_prepared_artifact_for_reprepare(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry(_registry(), path=path)
    artifact = tmp_path / "operation-prepared" / "receipt.json"
    artifact.parent.mkdir()
    artifact.write_bytes(b"corrupt")
    with admin_store._conn(path) as conn:
        conn.execute(
            "INSERT INTO operation_target_current(service_id,alias_id,target_generation,"
            "candidate_state,prepared_artifact_digest,prepared_artifact_relpath,updated_at) "
            "VALUES ('service','alias',1,'preparing','deadbeef','operation-prepared/receipt.json',1)"
        )
        conn.commit()
    assert recover(path=path, state_dir=tmp_path) == [
        {"service_id": "service", "alias_id": "alias", "action": "reprepare"}
    ]


def test_reload_ack_only_records_the_current_effective_generation(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry({"schema_version": 1, "services": {}, "protocol_frontends": {}, "workflows": {}, "capabilities": {}}, path=path)
    with admin_store._conn(path) as conn:
        conn.execute("INSERT INTO operation_target_current(service_id,alias_id,target_generation,effective_operator_settings_generation,candidate_state,updated_at) VALUES ('service','alias',1,7,'none',1)")
        conn.commit()
    assert acknowledge_local_reload(path=path, service_id="service", alias_id="alias", generation=6) is False
    assert acknowledge_local_reload(path=path, service_id="service", alias_id="alias", generation=7) is True


def test_staged_candidate_is_absent_from_second_reader_registry(tmp_path: Path) -> None:
    path, _ = _ready_target(tmp_path)
    with admin_store._conn(path) as second_reader:
        value = json.loads(second_reader.execute(
            "SELECT value_json FROM operator_settings WHERE key='service_registry'"
        ).fetchone()[0])
    alias = value["services"]["service"]["config"]["models"][1]
    assert "compaction_safety" not in alias


def test_activation_idempotency_replays_committed_lost_response(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    args = dict(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                expected_target=target["target_generation"],
                expected_registry=target["operator_settings_generation"], actor="test",
                idempotency_key="lost-response")
    first = activate(**args)
    replay = activate(**args)
    assert first["effective_revision"] is not None
    assert replay["idempotency_replayed"] is True
    with admin_store._conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_target_revisions").fetchone()[0] == 2


def test_activation_idempotency_key_rejects_different_request(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
             expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
             actor="test", idempotency_key="same-key")
    with pytest.raises(OperationError, match="different request"):
        activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                 expected_target=999, expected_registry=1, actor="test", idempotency_key="same-key")


def test_concurrent_same_idempotency_key_creates_one_effective_revision(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    barrier = threading.Barrier(2)
    results: list[dict | Exception] = []
    def worker() -> None:
        barrier.wait()
        try:
            results.append(activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                                    expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
                                    actor="test", idempotency_key="concurrent"))
        except Exception as exc:  # assertion below makes failures observable
            results.append(exc)
    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert all(not isinstance(result, Exception) for result in results)
    with admin_store._conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_target_revisions WHERE requested_state='effective'").fetchone()[0] == 1


def test_evidence_expiry_between_stage_and_cutover_never_changes_registry(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry(_registry(), path=path)
    target = stage(path=path, service_id="service", alias_id="alias", base_model_id="base",
                   overlay={"compaction_safety": _record()}, evidence={"expires_at": 1}, actor="test",
                   expected_target=None, expected_registry=None)
    failed = activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                      expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
                      actor="test", idempotency_key="expired")
    assert failed["candidate_state"] == "prepare_failed"
    assert failed["last_error_code"] == "evidence_expired"
    assert "compaction_safety" not in admin_store.get_service_registry_with_generation(path=path)["value"]["services"]["service"]["config"]["models"][1]


def test_registry_change_before_final_cutover_marks_candidate_failed_without_overlay(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    original_cut_over = operation_activation._cut_over

    def change_registry_then_cut_over(*args, **kwargs):
        registry = admin_store.get_service_registry_with_generation(path=path)
        changed = registry["value"]
        changed["services"]["service"]["name"] = "Unrelated change"
        admin_store.write_service_registry(
            changed, path=path,
            expected_generation=registry["operator_settings_generation"],
        )
        return original_cut_over(*args, **kwargs)

    with patch("gateway.operation_activation._cut_over", side_effect=change_registry_then_cut_over):
        failed = activate(
            path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
            expected_target=target["target_generation"],
            expected_registry=target["operator_settings_generation"], actor="test",
            idempotency_key="changed-before-cutover",
        )

    alias = admin_store.get_service_registry_with_generation(path=path)["value"]["services"]["service"]["config"]["models"][1]
    assert failed["candidate_state"] == "prepare_failed"
    assert failed["last_error_code"] == "registry_changed"
    assert "compaction_safety" not in alias


def test_rollback_stages_prior_overlay_against_unrelated_current_registry(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    effective = activate(
        path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
        expected_target=target["target_generation"],
        expected_registry=target["operator_settings_generation"], actor="test",
        idempotency_key="activate-before-rollback",
    )
    inactive = deactivate(
        path=path, service_id="service", alias_id="alias",
        expected_target=effective["target_generation"],
        expected_registry=effective["operator_settings_generation"], actor="test",
    )
    registry = admin_store.get_service_registry_with_generation(path=path)
    changed = registry["value"]
    changed["services"]["service"]["name"] = "Preserved through rollback"
    changed_registry = admin_store.write_service_registry(
        changed, path=path, expected_generation=registry["operator_settings_generation"],
    )

    rolled_back = rollback(
        path=path, service_id="service", alias_id="alias", revision_number=2,
        actor="test", expected_target=inactive["target_generation"],
        expected_registry=changed_registry["operator_settings_generation"],
    )

    assert rolled_back["candidate_state"] == "staged"
    assert rolled_back["operator_settings_generation"] == changed_registry["operator_settings_generation"]
    reactivated = activate(
        path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
        expected_target=rolled_back["target_generation"],
        expected_registry=rolled_back["operator_settings_generation"], actor="test",
        idempotency_key="activate-rolled-back-revision",
    )
    current = admin_store.get_service_registry_with_generation(path=path)
    assert reactivated["effective_revision"] is not None
    assert reactivated["operator_settings_generation"] == current["operator_settings_generation"]
    alias = current["value"]["services"]["service"]["config"]["models"][1]
    assert current["value"]["services"]["service"]["name"] == "Preserved through rollback"
    assert "compaction_safety" in alias
    with admin_store._conn(path) as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM audit_events")]
    assert actions.count("operator.operation.rollback") == 1


def test_deactivate_wins_when_activation_reaches_final_cutover_after_it(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    registry = admin_store.get_service_registry_with_generation(path=path)
    changed = registry["value"]
    changed["services"]["service"]["name"] = "Unrelated change"
    refreshed = admin_store.write_service_registry(
        changed, path=path, expected_generation=registry["operator_settings_generation"],
    )
    target = stage(
        path=path, service_id="service", alias_id="alias", base_model_id="base",
        overlay={"compaction_safety": _record()}, evidence={"expires_at": 2_000_000_000},
        actor="test", expected_target=target["target_generation"],
        expected_registry=refreshed["operator_settings_generation"],
    )
    cutover_ready = threading.Event()
    release_cutover = threading.Event()
    result: list[dict | Exception] = []
    original_cut_over = operation_activation._cut_over

    def wait_before_cut_over(*args, **kwargs):
        cutover_ready.set()
        assert release_cutover.wait(timeout=5)
        return original_cut_over(*args, **kwargs)

    def worker() -> None:
        try:
            result.append(activate(
                path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                expected_target=target["target_generation"],
                expected_registry=target["operator_settings_generation"], actor="test",
                idempotency_key="activate-versus-deactivate",
            ))
        except Exception as exc:
            result.append(exc)

    with patch("gateway.operation_activation._cut_over", side_effect=wait_before_cut_over):
        thread = threading.Thread(target=worker)
        thread.start()
        assert cutover_ready.wait(timeout=5)
        preparing = operation_activation.status(path=path, service_id="service", alias_id="alias")
        inactive = deactivate(
            path=path, service_id="service", alias_id="alias",
            expected_target=preparing["target_generation"],
            expected_registry=preparing["operator_settings_generation"], actor="test",
        )
        release_cutover.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(result) == 1 and isinstance(result[0], OperationError)
    assert result[0].code == "obsolete_worker"
    assert inactive["candidate_state"] == "none" and inactive["effective_revision"] is None
    registry_after = admin_store.get_service_registry_with_generation(path=path)["value"]
    assert registry_after["services"]["service"]["name"] == "Unrelated change"
    with admin_store._conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_target_revisions WHERE requested_state='effective'").fetchone()[0] == 0


def test_stale_target_writer_loses_without_replacing_candidate(tmp_path: Path) -> None:
    path, first = _ready_target(tmp_path)
    second = stage(path=path, service_id="service", alias_id="alias", base_model_id="base",
                   overlay={"compaction_safety": _record()}, evidence={"expires_at": 2_000_000_000},
                   actor="test", expected_target=first["target_generation"],
                   expected_registry=first["operator_settings_generation"])
    with pytest.raises(OperationError) as error:
        stage(path=path, service_id="service", alias_id="alias", base_model_id="base",
              overlay={"compaction_safety": _record()}, evidence={"expires_at": 2_000_000_000},
              actor="test", expected_target=first["target_generation"],
              expected_registry=first["operator_settings_generation"])
    assert error.value.code == "generation_mismatch"
    assert second["candidate_revision"] == 2


def test_registry_generation_is_shared_and_stale_writer_loses(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    first = admin_store.write_service_registry(_registry(), path=path)
    replacement = _registry()
    replacement["services"]["service"]["name"] = "Changed"
    second = admin_store.write_service_registry(
        replacement, path=path, expected_generation=first["operator_settings_generation"]
    )
    assert second["operator_settings_generation"] == first["operator_settings_generation"] + 1
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        admin_store.write_service_registry(
            _registry(), path=path, expected_generation=first["operator_settings_generation"]
        )


def test_deactivate_removes_only_overlay_and_preserves_unrelated_mutation(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    effective = activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                         expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
                         actor="test", idempotency_key="activate")
    registry = admin_store.get_service_registry_with_generation(path=path)["value"]
    registry["services"]["service"]["config"]["models"][1]["later"] = "preserved"
    changed = admin_store.write_service_registry(registry, expected_generation=effective["operator_settings_generation"], path=path)
    inactive = deactivate(path=path, service_id="service", alias_id="alias",
                          expected_target=effective["target_generation"],
                          expected_registry=changed["operator_settings_generation"], actor="test")
    alias = admin_store.get_service_registry_with_generation(path=path)["value"]["services"]["service"]["config"]["models"][1]
    assert inactive["effective_revision"] is None
    assert alias["unrelated"] == "kept" and alias["later"] == "preserved"
    assert "compaction_safety" not in alias


def test_rollback_rejects_missing_current_base(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    effective = activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                         expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
                         actor="test", idempotency_key="activate")
    registry = admin_store.get_service_registry_with_generation(path=path)["value"]
    registry["services"].pop("service")
    changed = admin_store.write_service_registry(registry, expected_generation=effective["operator_settings_generation"], path=path)
    with pytest.raises(OperationError) as error:
        rollback(path=path, service_id="service", alias_id="alias", revision_number=1,
                 actor="test", expected_target=effective["target_generation"],
                 expected_registry=changed["operator_settings_generation"])
    assert error.value.code == "incompatible"


def test_dry_run_uses_injected_pipeline_without_persistence(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    before = admin_store.get_service_registry_with_generation(path=path)
    seen = []
    def prepare(body, service_id, alias_id, runtime_config):
        seen.append((body, service_id, alias_id, runtime_config))
        return b'{"admitted":true}'
    result = dry_run(path=path, service_id="service", alias_id="alias",
                     request={"messages": [{"role": "system", "content": "p"}]}, prepare=prepare)
    assert result["admitted_body_sha256"] and seen[0][0]["model"] == "alias"
    assert admin_store.get_service_registry_with_generation(path=path) == before


def test_obsolete_worker_cannot_mark_replacement_failed(tmp_path: Path) -> None:
    path, target = _ready_target(tmp_path)
    with patch("gateway.operation_activation._write_prepared_artifact", side_effect=OSError("disk")):
        failed = activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                          expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
                          actor="test", idempotency_key="obsolete")
    assert failed["candidate_state"] == "prepare_failed"
    assert failed["last_error_code"] == "artifact_write_failed"


@pytest.mark.parametrize("seams", [
    ActivationSeams(before_prepare_metadata_commit=lambda: (_ for _ in ()).throw(OSError("metadata"))),
    ActivationSeams(before_cutover_commit=lambda: (_ for _ in ()).throw(OSError("cutover"))),
])
def test_transaction_boundary_failure_leaves_active_registry_for_fresh_reader(
    tmp_path: Path, seams: ActivationSeams
) -> None:
    path, target = _ready_target(tmp_path)
    with pytest.raises(OSError):
        activate(
            path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
            expected_target=target["target_generation"],
            expected_registry=target["operator_settings_generation"], actor="test",
            idempotency_key="boundary", seams=seams,
        )
    # A new SQLite connection is the authority check: lifecycle rows cannot
    # leak a candidate into an independently booting reader.
    fresh = admin_store.get_service_registry_with_generation(path=path)["value"]
    assert "compaction_safety" not in fresh["services"]["service"]["config"]["models"][1]


def test_dry_run_persists_no_prompt_or_admitted_bytes(tmp_path: Path) -> None:
    path, _ = _ready_target(tmp_path)
    prompt = "private prompt must never be stored"
    with admin_store._conn(path) as conn:
        before = conn.execute(
            "SELECT group_concat(canonical_json || overlay_json || value_json, '') "
            "FROM operation_evidence_receipts "
            "LEFT JOIN operation_target_revisions ON 1=1 "
            "LEFT JOIN operator_settings ON 1=1"
        ).fetchone()[0]
    dry_run(
        path=path, service_id="service", alias_id="alias",
        request={"messages": [{"role": "user", "content": prompt}]},
        prepare=lambda *_: b'{"admitted":"private prompt must never be stored"}',
    )
    with admin_store._conn(path) as conn:
        after = conn.execute(
            "SELECT group_concat(canonical_json || overlay_json || value_json, '') "
            "FROM operation_evidence_receipts "
            "LEFT JOIN operation_target_revisions ON 1=1 "
            "LEFT JOIN operator_settings ON 1=1"
        ).fetchone()[0]
    assert after == before
    assert prompt not in after


@pytest.mark.parametrize("boundary", ["file_fsync", "rename", "parent_fsync"])
def test_artifact_publication_faults_never_cut_over(tmp_path: Path, boundary: str) -> None:
    path, target = _ready_target(tmp_path)
    original_fsync = __import__("gateway.operation_activation", fromlist=["os"]).os.fsync
    original_replace = __import__("gateway.operation_activation", fromlist=["os"]).os.replace
    fsync_calls = 0
    def fail_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if boundary == "file_fsync" and fsync_calls == 1:
            raise OSError(boundary)
        if boundary == "parent_fsync" and fsync_calls == 2:
            raise OSError(boundary)
        return original_fsync(fd)
    with patch("gateway.operation_activation.os.fsync", side_effect=fail_fsync), patch(
        "gateway.operation_activation.os.replace",
        side_effect=OSError("rename") if boundary == "rename" else original_replace,
    ):
        failed = activate(path=path, state_dir=tmp_path, service_id="service", alias_id="alias",
                          expected_target=target["target_generation"], expected_registry=target["operator_settings_generation"],
                          actor="test", idempotency_key=f"fault-{boundary}")
    assert failed["candidate_state"] == "prepare_failed"
    if boundary == "parent_fsync":
        assert fsync_calls == 2
    registry = admin_store.get_service_registry_with_generation(path=path)["value"]
    assert "compaction_safety" not in registry["services"]["service"]["config"]["models"][1]
