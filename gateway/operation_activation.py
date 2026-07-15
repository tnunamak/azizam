"""Safe activation of one data-declared operation overlay.

Lifecycle rows are deliberately not a configuration source.  Until ``activate``
commits its final transaction, the candidate exists only in this module's five
tables and the service registry remains unchanged for every runtime reader.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway import admin_store, service_registry
from gateway.compaction_safety import ContractError, contract_from_data
from gateway.litellm_substrate import compile_litellm_config_from_registry

SERVICE_REGISTRY_KEY = "service_registry"


@dataclass(frozen=True)
class ActivationSeams:
    """Optional deterministic failure seams for transaction-boundary tests.

    Production passes no seams.  The callbacks run immediately before the
    named commit boundary, so tests can prove rollback without pretending that
    a Python exception is a machine crash.
    """

    before_prepare_metadata_commit: Callable[[], None] | None = None
    before_cutover_commit: Callable[[], None] | None = None


class OperationError(ValueError):
    """A stable, public operation lifecycle error."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def canonical_evidence(document: Any) -> tuple[str, str, int]:
    """Return a deterministic digest for an operator assertion, not attestation."""
    if not isinstance(document, Mapping):
        raise OperationError("invalid_evidence", "evidence must be an object", 422)
    _reject_floats(document)
    expires_at = document.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = int(datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp())
        except ValueError as exc:
            raise OperationError("invalid_evidence", "evidence expires_at must be RFC3339", 422) from exc
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise OperationError("invalid_evidence", "evidence expires_at is required", 422)
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical, expires_at


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise OperationError("invalid_evidence", "evidence cannot contain floats", 422)
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_floats(child)
    elif isinstance(value, list):
        for child in value:
            _reject_floats(child)


def _effective_registry(path: Path) -> tuple[dict[str, Any], int]:
    record = admin_store.get_service_registry_with_generation(path=path)
    if record is None:
        raise OperationError("registry_unavailable", "service registry is unavailable")
    return service_registry.normalize_service_registry(record["value"]), int(record["operator_settings_generation"])


def _candidate_projection(registry: Mapping[str, Any], service_id: str, alias_id: str, base_model_id: str, overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Apply one alias overlay to an in-memory registry copy only."""
    projection = copy.deepcopy(dict(registry))
    service = (projection.get("services") or {}).get(service_id)
    if not isinstance(service, Mapping):
        raise OperationError("target_not_found", "service not found", 404)
    models = ((service.get("config") or {}).get("models") or [])
    alias = next((row for row in models if isinstance(row, dict) and row.get("id") == alias_id), None)
    if alias is None:
        raise OperationError("target_not_found", "alias not found", 404)
    if alias.get("base_model_id") != base_model_id:
        raise OperationError("incompatible", "base model does not match target")
    alias.update(copy.deepcopy(dict(overlay)))
    try:
        return service_registry.normalize_service_registry(projection)
    except ValueError as exc:
        raise OperationError("incompatible", str(exc)) from exc


def _validate_projection(registry: Mapping[str, Any], service_id: str, alias_id: str) -> None:
    """Use the existing registry validator; no operation-specific parser exists."""
    service = (registry.get("services") or {}).get(service_id)
    if not isinstance(service, Mapping):
        raise OperationError("target_not_found", "service not found", 404)
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    alias = next(
        (item for item in config.get("models", [])
         if isinstance(item, Mapping) and item.get("id") == alias_id),
        None,
    )
    if not isinstance(alias, Mapping) or not isinstance(alias.get("compaction_safety"), Mapping):
        raise OperationError(
            "invalid_operation",
            "target alias requires a context_compaction contract",
            422,
        )
    try:
        service_registry._validate_operation_contracts({service_id: service})
        contract_from_data(
            alias["compaction_safety"], service_id=service_id,
            service_controlled_deployment_evidence=config.get("controlled_deployment_evidence"),
            service_deployment_generation=config.get("deployment_generation"),
            service_registry_generation=config.get("registry_generation"),
            base_model_id=str(alias.get("base_model_id") or alias.get("id") or ""),
        )
    except ValueError as exc:
        raise OperationError("invalid_operation", str(exc), 422) from exc


def _target_row(conn, service_id: str, alias_id: str):
    return conn.execute("SELECT * FROM operation_target_current WHERE service_id=? AND alias_id=?", (service_id, alias_id)).fetchone()


def _revision_row(conn, service_id: str, alias_id: str, revision: int):
    return conn.execute("SELECT * FROM operation_target_revisions WHERE service_id=? AND alias_id=? AND revision=?", (service_id, alias_id, revision)).fetchone()


def _status_from_row(conn, row, global_generation: int) -> dict[str, Any]:
    result = dict(row)
    candidate_revision = row["candidate_revision"]
    if candidate_revision is not None:
        revision = _revision_row(conn, row["service_id"], row["alias_id"], candidate_revision)
        result["candidate"] = {
            "revision": revision["revision"], "base_model_id": revision["base_model_id"],
            "evidence_digest": revision["evidence_digest"],
        }
    result["operator_settings_generation"] = global_generation
    return result


def _audit(conn, *, action: str, actor: str, service_id: str, alias_id: str,
           now: int, metadata: Mapping[str, Any] | None = None) -> None:
    """Append lifecycle audit inside the transaction that changes its fact."""
    conn.execute(
        "INSERT INTO audit_events(id,created_at,actor_kind,actor_id,action,"
        "target_kind,target_id,metadata_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            f"aud_{uuid.uuid4().hex}", now, "admin", actor, action,
            "operation_target", f"{service_id}/{alias_id}",
            json.dumps(dict(metadata or {}), sort_keys=True),
        ),
    )


def status(*, path: Path, service_id: str, alias_id: str) -> dict[str, Any]:
    _, generation = _effective_registry(path)
    with admin_store._conn(path) as conn:
        row = _target_row(conn, service_id, alias_id)
        if row is None:
            raise OperationError("target_not_found", "operation target not found", 404)
        return _status_from_row(conn, row, generation)


def list_targets(*, path: Path) -> list[dict[str, Any]]:
    """List lifecycle observations; this never projects candidates into config."""
    _, generation = _effective_registry(path)
    with admin_store._conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM operation_target_current ORDER BY service_id, alias_id"
        ).fetchall()
        return [_status_from_row(conn, row, generation) for row in rows]


def revision(*, path: Path, service_id: str, alias_id: str, number: int) -> dict[str, Any]:
    """Return immutable revision metadata without exposing evidence contents."""
    with admin_store._conn(path) as conn:
        row = _revision_row(conn, service_id, alias_id, number)
        if row is None:
            raise OperationError("revision_not_found", "operation revision not found", 404)
        receipt = conn.execute(
            "SELECT expires_at FROM operation_evidence_receipts WHERE digest=?",
            (row["evidence_digest"],),
        ).fetchone()
    return {
        "revision": row["revision"],
        "requested_state": row["requested_state"],
        "base_model_id": row["base_model_id"],
        "evidence_digest": row["evidence_digest"],
        "evidence_expires_at": receipt["expires_at"] if receipt else None,
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


def revisions(*, path: Path, service_id: str, alias_id: str) -> list[dict[str, Any]]:
    """List immutable revision summaries without returning overlays or evidence bytes."""
    with admin_store._conn(path) as conn:
        rows = conn.execute(
            "SELECT revision, requested_state, base_model_id, evidence_digest, created_at, created_by "
            "FROM operation_target_revisions WHERE service_id=? AND alias_id=? "
            "ORDER BY revision DESC",
            (service_id, alias_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _remove_overlay(registry: Mapping[str, Any], service_id: str, alias_id: str, overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only fields this operation placed on its identified alias."""
    updated = copy.deepcopy(dict(registry))
    service = (updated.get("services") or {}).get(service_id)
    if not isinstance(service, Mapping):
        raise OperationError("incompatible", "base service is absent", 409)
    models = ((service.get("config") or {}).get("models") or [])
    alias = next((item for item in models if isinstance(item, dict) and item.get("id") == alias_id), None)
    if alias is None:
        raise OperationError("incompatible", "base alias is absent", 409)
    for field in overlay:
        alias.pop(field, None)
    try:
        return service_registry.normalize_service_registry(updated)
    except ValueError as exc:
        raise OperationError("incompatible", str(exc), 409) from exc


def deactivate(*, path: Path, service_id: str, alias_id: str, expected_target: int, expected_registry: int, actor: str) -> dict[str, Any]:
    """Cancel a candidate or remove only the effective target's overlay."""
    registry, generation = _effective_registry(path)
    if generation != expected_registry:
        raise OperationError("generation_mismatch", "registry generation changed", 412)
    now = int(time.time())
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        generation_row = conn.execute(
            "SELECT operator_settings_generation FROM operator_settings_generations "
            "WHERE setting_key=?", (SERVICE_REGISTRY_KEY,)
        ).fetchone()
        if generation_row is None or int(generation_row[0]) != generation:
            conn.rollback()
            raise OperationError("generation_mismatch", "registry generation changed", 412)
        current = _target_row(conn, service_id, alias_id)
        if current is None or int(current["target_generation"]) != expected_target:
            conn.rollback()
            raise OperationError("generation_mismatch", "target generation changed", 412)
        effective_number = current["effective_revision"]
        if effective_number is None:
            conn.execute(
                "UPDATE operation_target_current SET candidate_revision=NULL, candidate_state='none', "
                "target_generation=target_generation+1, updated_at=? "
                "WHERE service_id=? AND alias_id=?",
                (now, service_id, alias_id),
            )
            _audit(conn, action="operator.operation.deactivate", actor=actor,
                   service_id=service_id, alias_id=alias_id, now=now,
                   metadata={"cancelled_candidate": True})
            conn.commit()
            return status(path=path, service_id=service_id, alias_id=alias_id)
        effective = _revision_row(conn, service_id, alias_id, int(effective_number))
        overlay = json.loads(effective["overlay_json"])
        projected = _remove_overlay(registry, service_id, alias_id, overlay)
        next_generation = admin_store.write_service_registry_in_transaction(
            conn, projected, updated_by=actor, expected_generation=generation, now=now,
        )
        number = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM operation_target_revisions "
            "WHERE service_id=? AND alias_id=?",
            (service_id, alias_id),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO operation_target_revisions("
            "service_id,alias_id,revision,requested_state,base_model_id,overlay_json,"
            "evidence_digest,supersedes_revision,created_at,created_by"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (service_id, alias_id, number, "inactive", effective["base_model_id"], "{}", effective["evidence_digest"], effective_number, now, actor),
        )
        conn.execute(
            "UPDATE operation_target_current SET effective_revision=NULL, candidate_revision=NULL, "
            "candidate_state='none', effective_operator_settings_generation=?, "
            "target_generation=target_generation+1, updated_at=? WHERE service_id=? AND alias_id=?",
            (next_generation, now, service_id, alias_id),
        )
        _audit(conn, action="operator.operation.deactivate", actor=actor,
               service_id=service_id, alias_id=alias_id, now=now,
               metadata={"operator_settings_generation": next_generation})
        conn.commit()
    return status(path=path, service_id=service_id, alias_id=alias_id)


def rollback(*, path: Path, service_id: str, alias_id: str, revision_number: int, actor: str, expected_target: int, expected_registry: int) -> dict[str, Any]:
    """Stage a prior same-target overlay against today's registry, never a snapshot."""
    with admin_store._conn(path) as conn:
        source = _revision_row(conn, service_id, alias_id, revision_number)
        if source is None:
            raise OperationError("revision_not_found", "operation revision not found", 404)
        receipt = conn.execute(
            "SELECT canonical_json FROM operation_evidence_receipts WHERE digest=?",
            (source["evidence_digest"],),
        ).fetchone()
    if receipt is None:
        raise OperationError("incompatible", "rollback evidence is unavailable", 409)
    registry, _ = _effective_registry(path)
    service = (registry.get("services") or {}).get(service_id)
    models = ((service or {}).get("config") or {}).get("models") or []
    if not any(isinstance(item, Mapping) and item.get("id") == source["base_model_id"] for item in models):
        raise OperationError("incompatible", "rollback base model is absent", 409)
    return stage(
        path=path, service_id=service_id, alias_id=alias_id,
        base_model_id=source["base_model_id"], overlay=json.loads(source["overlay_json"]),
        evidence=json.loads(receipt["canonical_json"]), actor=actor,
        expected_target=expected_target, expected_registry=expected_registry,
        audit_action="operator.operation.rollback",
    )


def acknowledge_local_reload(*, path: Path, service_id: str, alias_id: str, generation: int) -> bool:
    """Record only an originating-process observation after a real reload return."""
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _target_row(conn, service_id, alias_id)
        if row is None or row["effective_operator_settings_generation"] != generation:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE operation_target_current SET last_local_reload_ack_generation=?, updated_at=? "
            "WHERE service_id=? AND alias_id=?",
            (generation, int(time.time()), service_id, alias_id),
        )
        conn.commit()
    return True


def record_reload_failure(*, path: Path, service_id: str, alias_id: str,
                          generation: int) -> bool:
    """Record a local reload observation without changing effective registry state."""
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE operation_target_current SET last_error_code='local_reload_failed', "
            "updated_at=? WHERE service_id=? AND alias_id=? "
            "AND effective_operator_settings_generation=?",
            (int(time.time()), service_id, alias_id, generation),
        ).rowcount
        conn.commit()
    return bool(changed)


def recover(*, path: Path, state_dir: Path) -> list[dict[str, Any]]:
    """Report recoverable candidates; never load their overlays into runtime."""
    recovered: list[dict[str, Any]] = []
    for target in list_targets(path=path):
        state = target["candidate_state"]
        if state not in {"staged", "preparing", "prepare_failed"}:
            continue
        artifact = target.get("prepared_artifact_relpath")
        if artifact:
            file_path = state_dir / artifact
            digest = target.get("prepared_artifact_digest")
            if not file_path.is_file() or hashlib.sha256(file_path.read_bytes()).hexdigest() != digest:
                recovered.append({"service_id": target["service_id"], "alias_id": target["alias_id"], "action": "reprepare"})
                continue
        recovered.append({"service_id": target["service_id"], "alias_id": target["alias_id"], "action": "retry"})
    return recovered


def stage(*, path: Path, service_id: str, alias_id: str, base_model_id: str, overlay: Mapping[str, Any], evidence: Mapping[str, Any], actor: str, expected_target: int | None, expected_registry: int | None, audit_action: str = "operator.operation.stage") -> dict[str, Any]:
    """Persist an immutable staged revision without changing active config."""
    digest, canonical, expires_at = canonical_evidence(evidence)
    registry, global_generation = _effective_registry(path)
    if expected_registry is not None and expected_registry != global_generation:
        raise OperationError("generation_mismatch", "registry generation changed", 412)
    projection = _candidate_projection(registry, service_id, alias_id, base_model_id, overlay)
    _validate_projection(projection, service_id, alias_id)
    now = int(time.time())
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        generation_row = conn.execute(
            "SELECT operator_settings_generation FROM operator_settings_generations "
            "WHERE setting_key=?", (SERVICE_REGISTRY_KEY,)
        ).fetchone()
        if generation_row is None or int(generation_row[0]) != global_generation:
            conn.rollback()
            raise OperationError("generation_mismatch", "registry generation changed", 412)
        current = _target_row(conn, service_id, alias_id)
        target_generation = int(current["target_generation"]) if current else 0
        if expected_target is not None and expected_target != target_generation:
            conn.rollback()
            raise OperationError("generation_mismatch", "target generation changed", 412)
        revision = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM operation_target_revisions WHERE service_id=? AND alias_id=?", (service_id, alias_id)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO operation_evidence_receipts VALUES (?, ?, ?, ?, ?)", (digest, canonical, expires_at, now, actor))
        conn.execute("INSERT INTO operation_target_revisions(service_id,alias_id,revision,requested_state,base_model_id,overlay_json,evidence_digest,supersedes_revision,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)", (service_id, alias_id, revision, "staged", base_model_id, json.dumps(dict(overlay), sort_keys=True), digest, current["candidate_revision"] if current else None, now, actor))
        if current is None:
            conn.execute("INSERT INTO operation_target_current(service_id,alias_id,target_generation,candidate_revision,candidate_state,candidate_base_operator_settings_generation,updated_at) VALUES (?,?,?,?,?,?,?)", (service_id, alias_id, 1, revision, "staged", global_generation, now))
        else:
            conn.execute("UPDATE operation_target_current SET target_generation=?,candidate_revision=?,candidate_state='staged',candidate_base_operator_settings_generation=?,prepared_artifact_digest=NULL,prepared_artifact_relpath=NULL,last_error_code=NULL,updated_at=? WHERE service_id=? AND alias_id=?", (target_generation + 1, revision, global_generation, now, service_id, alias_id))
        _audit(conn, action=audit_action, actor=actor, service_id=service_id,
               alias_id=alias_id, now=now, metadata={"revision": revision})
        conn.commit()
        return _status_from_row(conn, _target_row(conn, service_id, alias_id), global_generation)


def _write_prepared_artifact(registry: Mapping[str, Any], state_dir: Path) -> tuple[str, str]:
    """Publish a versioned non-watched artifact with file and directory fsync."""
    contents = json.dumps(compile_litellm_config_from_registry(registry), sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(contents).hexdigest()
    directory = state_dir / "operation-prepared"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{digest}.{os.getpid()}.tmp"
    destination = directory / f"{digest}.json"
    with temporary.open("wb") as artifact:
        artifact.write(contents)
        artifact.flush()
        os.fsync(artifact.fileno())
    os.replace(temporary, destination)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return digest, str(destination.relative_to(state_dir))


def _mark_prepare_failed(
    path: Path, service_id: str, alias_id: str, candidate_revision: int, code: str
) -> dict[str, Any]:
    """Record a failure only for the worker's still-current candidate.

    A cancelled/replaced candidate must make an old worker a harmless loser,
    never turn the new candidate into ``prepare_failed``.
    """
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE operation_target_current SET candidate_state='prepare_failed', "
            "last_error_code=?, target_generation=target_generation+1, updated_at=? "
            "WHERE service_id=? AND alias_id=? AND candidate_revision=? "
            "AND candidate_state='preparing'",
            (code, int(time.time()), service_id, alias_id, candidate_revision),
        ).rowcount
        conn.commit()
    if not changed:
        raise OperationError("obsolete_worker", "candidate was replaced or cancelled")
    return status(path=path, service_id=service_id, alias_id=alias_id)


def activate(*, path: Path, state_dir: Path, service_id: str, alias_id: str, expected_target: int, expected_registry: int, actor: str, idempotency_key: str, seams: ActivationSeams | None = None) -> dict[str, Any]:
    """Prepare outside the registry, then perform one final CAS cutover."""
    request_digest = _activation_request_digest(service_id, alias_id, expected_target, expected_registry)
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT request_digest, response_json "
            "FROM operation_idempotency "
            "WHERE actor_id=? AND endpoint='activation' AND key=?",
            (actor, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != request_digest:
                conn.rollback()
                raise OperationError("idempotency_conflict", "Idempotency-Key was used for a different request")
            response = json.loads(existing["response_json"])
            conn.rollback()
            target = dict(response["target"])
            target["idempotency_replayed"] = True
            return target
        current = _target_row(conn, service_id, alias_id)
        if current is None or current["candidate_state"] not in ("staged", "prepare_failed") or int(current["target_generation"]) != expected_target:
            conn.rollback()
            raise OperationError("generation_mismatch", "target is no longer staged", 412)
        now = int(time.time())
        conn.execute("UPDATE operation_target_current SET candidate_state='preparing',target_generation=target_generation+1,updated_at=? WHERE service_id=? AND alias_id=?", (now, service_id, alias_id))
        reserved = _status_from_row(conn, _target_row(conn, service_id, alias_id), expected_registry)
        conn.execute(
            "INSERT INTO operation_idempotency("
            "actor_id, endpoint, key, request_digest, response_status, response_json, "
            "target_generation, operator_settings_generation, created_at"
            ") VALUES (?, 'activation', ?, ?, 202, ?, ?, ?, ?)",
            (actor, idempotency_key, request_digest, json.dumps({"target": reserved}, sort_keys=True), reserved["target_generation"], expected_registry, now),
        )
        conn.commit()
    claimed_generation = int(reserved["target_generation"])
    candidate_revision = int(reserved["candidate"]["revision"])
    with admin_store._conn(path) as conn:
        candidate_row = _revision_row(conn, service_id, alias_id, candidate_revision)
        if candidate_row is None:
            raise OperationError("obsolete_worker", "candidate revision disappeared")
        candidate = {
            "revision": candidate_row["revision"],
            "base_model_id": candidate_row["base_model_id"],
            "overlay": json.loads(candidate_row["overlay_json"]),
            "evidence_digest": candidate_row["evidence_digest"],
        }
    registry, generation = _effective_registry(path)
    if generation != expected_registry:
        return _mark_prepare_failed(path, service_id, alias_id, candidate_revision, "registry_changed")
    try:
        projection = _candidate_projection(registry, service_id, alias_id, candidate["base_model_id"], candidate["overlay"])
        _validate_projection(projection, service_id, alias_id)
        digest, relative_path = _write_prepared_artifact(projection, state_dir)
    except OperationError:
        raise
    except OSError as exc:
        return _mark_prepare_failed(path, service_id, alias_id, candidate_revision, "artifact_write_failed")

    # Durable bytes are recorded before cutover.  The CAS closes the stale-worker
    # hole between filesystem publication and the final registry transaction.
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _target_row(conn, service_id, alias_id)
        if (
            row is None
            or row["candidate_state"] != "preparing"
            or int(row["candidate_revision"] or -1) != candidate_revision
            or int(row["target_generation"]) != claimed_generation
        ):
            conn.rollback()
            raise OperationError("obsolete_worker", "candidate was replaced or cancelled")
        conn.execute(
            "UPDATE operation_target_current SET prepared_artifact_digest=?, "
            "prepared_artifact_relpath=?, prepared_at=?, target_generation=target_generation+1, "
            "updated_at=? WHERE service_id=? AND alias_id=?",
            (digest, relative_path, int(time.time()), int(time.time()), service_id, alias_id),
        )
        prepared_generation = int(_target_row(conn, service_id, alias_id)["target_generation"])
        if seams and seams.before_prepare_metadata_commit:
            seams.before_prepare_metadata_commit()
        conn.commit()
    return _cut_over(path, service_id, alias_id, generation, candidate, projection, digest,
                     relative_path, prepared_generation, actor, idempotency_key, request_digest,
                     seams=seams)


def _activation_request_digest(service_id: str, alias_id: str, target_generation: int, registry_generation: int) -> str:
    canonical = json.dumps({"service_id": service_id, "alias_id": alias_id, "target_generation": target_generation, "operator_settings_generation": registry_generation}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cut_over(path: Path, service_id: str, alias_id: str, validated_generation: int,
              candidate: Mapping[str, Any], projection: Mapping[str, Any], digest: str,
              relative_path: str, expected_target_generation: int, actor: str,
              idempotency_key: str, request_digest: str,
              seams: ActivationSeams | None = None) -> dict[str, Any]:
    """The sole transaction that makes a candidate visible to runtime readers."""
    now = int(time.time())
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _target_row(conn, service_id, alias_id)
        generation_row = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key=?", (SERVICE_REGISTRY_KEY,)).fetchone()
        if (current is None or current["candidate_state"] != "preparing"
                or int(current["candidate_revision"] or -1) != int(candidate["revision"])
                or int(current["target_generation"]) != expected_target_generation
                or current["prepared_artifact_digest"] != digest
                or current["prepared_artifact_relpath"] != relative_path
                or generation_row is None or int(generation_row[0]) != validated_generation):
            conn.rollback()
            raise OperationError("obsolete_worker", "candidate was replaced or registry changed")
        receipt = conn.execute("SELECT expires_at FROM operation_evidence_receipts WHERE digest=?", (candidate["evidence_digest"],)).fetchone()
        if receipt is None or int(receipt[0]) <= now:
            conn.rollback()
            return _mark_prepare_failed(path, service_id, alias_id, int(candidate["revision"]), "evidence_expired")
        # Rebuild under the final transaction, rather than trusting the bytes
        # prepared before a concurrent registry writer could have committed.
        raw_registry = conn.execute(
            "SELECT value_json FROM operator_settings WHERE key=?", (SERVICE_REGISTRY_KEY,)
        ).fetchone()
        try:
            current_projection = _candidate_projection(
                service_registry.normalize_service_registry(json.loads(raw_registry[0])),
                service_id, alias_id, str(candidate["base_model_id"]), candidate["overlay"],
            )
            _validate_projection(current_projection, service_id, alias_id)
        except (OperationError, json.JSONDecodeError) as exc:
            conn.rollback()
            return _mark_prepare_failed(path, service_id, alias_id, int(candidate["revision"]), "registry_changed")
        # Registry normalization carries observational timestamps.  Those may
        # differ between two reads of the *same* generation, so comparing the
        # whole normalized document makes a same-key replay spuriously fail at
        # a clock boundary.  The prepared artifact is the actual durable
        # cutover prerequisite; compare its deterministic bytes instead.
        current_artifact = json.dumps(
            compile_litellm_config_from_registry(current_projection),
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(current_artifact).hexdigest() != digest:
            conn.rollback()
            return _mark_prepare_failed(path, service_id, alias_id, int(candidate["revision"]), "registry_changed")
        projection = current_projection
        next_generation = admin_store.write_service_registry_in_transaction(
            conn, projection, updated_by=actor, expected_generation=validated_generation,
            now=now,
        )
        revision = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM operation_target_revisions WHERE service_id=? AND alias_id=?", (service_id, alias_id)).fetchone()[0]
        conn.execute("INSERT INTO operation_target_revisions(service_id,alias_id,revision,requested_state,base_model_id,overlay_json,evidence_digest,supersedes_revision,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)", (service_id, alias_id, revision, "effective", candidate["base_model_id"], json.dumps(candidate["overlay"], sort_keys=True), candidate["evidence_digest"], candidate["revision"], now, "operation-activation"))
        conn.execute("UPDATE operation_target_current SET target_generation=target_generation+1,effective_revision=?,candidate_revision=NULL,candidate_state='none',effective_operator_settings_generation=?,prepared_artifact_digest=?,prepared_artifact_relpath=?,prepared_at=?,updated_at=? WHERE service_id=? AND alias_id=?", (revision, next_generation, digest, relative_path, now, now, service_id, alias_id))
        _audit(conn, action="operator.operation.activate", actor=actor,
               service_id=service_id, alias_id=alias_id, now=now,
               metadata={"revision": revision,
                         "operator_settings_generation": next_generation})
        committed = _status_from_row(conn, _target_row(conn, service_id, alias_id), next_generation)
        response_json = json.dumps({"target": committed}, sort_keys=True)
        conn.execute(
            "UPDATE operation_idempotency SET response_status=202, response_json=?, "
            "target_generation=?, operator_settings_generation=? "
            "WHERE actor_id=? AND endpoint='activation' AND key=? AND request_digest=?",
            (response_json, committed["target_generation"], next_generation, actor, idempotency_key, request_digest),
        )
        if seams and seams.before_cutover_commit:
            seams.before_cutover_commit()
        conn.commit()
    return committed


def dry_run(*, path: Path, service_id: str, alias_id: str, request: Mapping[str, Any], prepare: Callable[[Mapping[str, Any], str, str, Mapping[str, Any]], bytes]) -> dict[str, Any]:
    """Run an injected, side-effect-free public preparation pipeline."""
    current = status(path=path, service_id=service_id, alias_id=alias_id)
    candidate_summary = current.get("candidate")
    if not isinstance(candidate_summary, Mapping):
        raise OperationError("target_state", "a staged candidate is required")
    with admin_store._conn(path) as conn:
        candidate_row = _revision_row(
            conn, service_id, alias_id, int(candidate_summary["revision"])
        )
    if candidate_row is None:
        raise OperationError("target_state", "candidate revision is unavailable")
    candidate = {
        "revision": candidate_row["revision"],
        "base_model_id": candidate_row["base_model_id"],
        "overlay": json.loads(candidate_row["overlay_json"]),
    }
    registry, _ = _effective_registry(path)
    projection = _candidate_projection(registry, service_id, alias_id, str(candidate["base_model_id"]), candidate["overlay"])
    runtime_config = service_registry.apply_service_registry_to_config({}, projection)
    public_body = copy.deepcopy(dict(request))
    public_body.setdefault("model", alias_id)
    public_body.setdefault("stream", False)
    try:
        admitted = prepare(public_body, service_id, alias_id, runtime_config)
    except Exception as exc:
        raise OperationError("validation_failed", "candidate request was rejected", 422) from exc
    projection_digest = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"projection_digest": projection_digest, "admitted_body_sha256": hashlib.sha256(admitted).hexdigest(), "admitted_byte_length": len(admitted), "resolver": {"service_id": service_id, "alias_id": alias_id, "base_model_id": candidate["base_model_id"]}, "diagnostics": []}
