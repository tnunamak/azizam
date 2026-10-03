"""Shared service availability projection.

The projection is intentionally engine-agnostic. Callers provide declared
service records, optional probe evidence, and recent request receipts; this
module reduces those facts into one UI/API state.
"""

from __future__ import annotations

import time
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gateway.capabilities import Capability

READY_STATUSES = frozenset({"ready", "up", "ok", "healthy", "available"})
STARTING_STATUSES = frozenset({"starting", "warming"})
BUSY_STATUSES = frozenset({"busy", "degraded"})
DOWN_STATUSES = frozenset({"down", "failed", "unhealthy", "unavailable", "stopped"})
SUCCESS_RECEIPT_STATUSES = frozenset({"ok", "success", "succeeded", "completed", "200"})
FAILED_RECEIPT_STATUSES = frozenset(
    {"error", "failed", "timed_out", "timeout", "cancelled", "disconnected", "500", "502", "503", "504"}
)
RECENT_EVIDENCE_WINDOW_S = 15 * 60


@dataclass(frozen=True)
class AvailabilityProjection:
    service_id: str
    enabled: bool
    configured: bool
    up: bool | None
    status: str
    reason: str
    measured: bool
    evidence: dict[str, Any]
    idle_startable: bool = False

    @property
    def available(self) -> bool:
        return self.status in {"ready", "idle"}

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "status": self.status,
            "reason": self.reason,
            "measured": self.measured,
            "enabled": self.enabled,
            "configured": self.configured,
            "up": self.up,
            "idle_startable": self.idle_startable,
            "evidence": _public_evidence(self.evidence),
        }


def project_capabilities(
    capabilities: Iterable[Capability],
    *,
    service_records: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Capability]:
    """Stamp capability lifecycle/health from the shared projection."""

    service_records = service_records or {}
    evidence = evidence or {}
    out: list[Capability] = []
    for capability in capabilities:
        projection = project_service(
            capability.backend,
            service_records=service_records,
            evidence=evidence,
            fallback_capability=capability,
        )
        lifecycle, health = _capability_states(projection)
        out.append(replace(
            capability,
            lifecycle=lifecycle,
            health=health,
            availability=projection.to_public_dict(),
        ))
    return out


def project_service(
    service_id: str,
    *,
    service_records: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
    fallback_capability: Capability | None = None,
) -> AvailabilityProjection:
    services = _services_mapping(service_records or {})
    has_service = service_id in services
    service = services.get(service_id) if has_service else {}
    enabled = bool(service.get("enabled", True)) if isinstance(service, Mapping) else True
    configured = _is_configured(service) if has_service and isinstance(service, Mapping) else bool(fallback_capability)
    idle_startable = _is_idle_startable(service) if has_service and isinstance(service, Mapping) else False
    picked = dict((evidence or {}).get(service_id) or {})

    if not enabled:
        return AvailabilityProjection(
            service_id, False, configured, False, "unavailable", "The service is disabled.", True,
            {"source": "registry", "kind": "disabled"},
        )
    if not configured:
        return AvailabilityProjection(
            service_id, enabled, False, False, "unavailable", "The service is not configured.", True,
            {"source": "registry", "kind": "unconfigured"},
        )
    if picked and not (idle_startable and _evidence_unknown(picked)):
        projected = _from_evidence(service_id, enabled, configured, idle_startable, picked)
        if projected is not None:
            return projected
    if idle_startable and _observed_stopped(picked):
        return AvailabilityProjection(
            service_id, enabled, configured, None, "idle", "Ready to start.", False,
            {"source": "registry", "kind": "idle_startable"}, idle_startable=True,
        )
    if fallback_capability is not None:
        if fallback_capability.lifecycle == "warming":
            return AvailabilityProjection(
                service_id, enabled, configured, None, "starting", "The service is starting.", True,
                {"source": "capability", "kind": "lifecycle"},
            )
        if fallback_capability.health == "unhealthy" or fallback_capability.lifecycle == "stopped":
            return AvailabilityProjection(
                service_id, enabled, configured, False, "unavailable", "The service is unavailable.", True,
                {"source": "capability", "kind": "state"},
            )
    return AvailabilityProjection(
        service_id, enabled, configured, None, "unknown", "Not measured yet", False,
        {"source": None, "kind": "unknown"},
    )


def evidence_from_receipts(
    receipts: Iterable[Mapping[str, Any]],
    *,
    now_s: int | None = None,
    max_age_s: int = RECENT_EVIDENCE_WINDOW_S,
) -> dict[str, dict[str, Any]]:
    """Return latest safe request evidence per backend/service."""

    now_s = int(time.time() if now_s is None else now_s)
    out: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), Mapping) else {}
        backend = str(metadata.get("service_id") or metadata.get("service-id") or receipt.get("backend") or "").strip()
        if not backend:
            continue
        timestamp = _receipt_timestamp_s(receipt)
        if timestamp is None or timestamp > now_s or now_s - timestamp > max_age_s:
            continue
        status = str(receipt.get("status") or "").strip().lower()
        if status not in SUCCESS_RECEIPT_STATUSES and status not in FAILED_RECEIPT_STATUSES:
            continue
        observed_at_ms = _receipt_timestamp_ms(receipt)
        success = status in SUCCESS_RECEIPT_STATUSES
        if not success and not _receipt_proves_backend_failure(receipt):
            continue
        current = out.get(backend)
        if current:
            current_ms = int(current.get("observed_at_ms") or 0)
            incoming_ms = int(observed_at_ms or 0)
            if current_ms > incoming_ms:
                continue
            if current_ms == incoming_ms and current.get("available") is False:
                continue
            if current_ms == incoming_ms and success:
                continue
        out[backend] = {
            "source": "request",
            "available": success,
            "status": "ready" if success else "unavailable",
            "reason": "Recent request succeeded" if success else "Most recent request failed",
            "observed_at_s": timestamp,
            "observed_at_ms": observed_at_ms,
            "request_id": receipt.get("request_id"),
            "receipt_status": status,
        }
    return out


def evidence_from_receipt_store(
    path: Path,
    *,
    now_s: int | None = None,
    max_age_s: int = RECENT_EVIDENCE_WINDOW_S,
    scan_limit: int = 5000,
) -> dict[str, dict[str, Any]]:
    """Read recent backend evidence without depending on page-sized feeds."""

    now_s = int(time.time() if now_s is None else now_s)
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for receipt_json, created_at in conn.execute(
            "SELECT receipt_json, created_at FROM receipts "
            "WHERE parent_request_id IS NULL "
            "ORDER BY created_at DESC, request_id DESC LIMIT ?",
            (int(scan_limit),),
        ):
            try:
                payload = json.loads(receipt_json)
            except Exception:
                continue
            if isinstance(payload, dict):
                payload.setdefault("_stored_at", int(created_at))
                rows.append(payload)
    return evidence_from_receipts(rows, now_s=now_s, max_age_s=max_age_s)


def evidence_from_status_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    observed_at_s: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Convert live status rows into shared service evidence."""

    observed_at_s = int(time.time() if observed_at_s is None else observed_at_s)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        service_id = str(row.get("service_id") or row.get("key") or row.get("name") or "").strip()
        raw = str(row.get("live_status") or row.get("status") or "").strip().lower()
        if not service_id or not raw or raw == "unknown":
            continue
        if raw in {"up", "healthy", "ok", "ready"}:
            available: bool | None = True
        elif raw in {"starting", "warming", "busy", "degraded", "stopped", "idle"}:
            available = None
        else:
            available = False
        evidence = {
            "source": str(row.get("source") or "status"),
            "status": raw,
            "available": available,
            "observed_at_s": observed_at_s,
        }
        kind = row.get("kind")
        if isinstance(kind, str) and kind.strip():
            evidence["kind"] = kind.strip()
        out[service_id] = evidence
    return out


def merge_evidence(
    *sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge evidence, keeping the newest per service when timestamps exist."""

    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        for service_id, evidence in source.items():
            incoming = dict(evidence)
            current = merged.get(str(service_id))
            if current is None or _evidence_newer(incoming, current):
                merged[str(service_id)] = incoming
    return merged


def availability_evidence_snapshot(
    *,
    service_records: Mapping[str, Any] | None = None,
    status_payload: Mapping[str, Any] | None = None,
    health_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    receipt_store_path: Path | None = None,
    now_s: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the shared evidence map used by every availability surface."""

    payload = status_payload if isinstance(status_payload, Mapping) else {}
    raw_backends = payload.get("backends")
    status_rows = raw_backends if isinstance(raw_backends, list) else []
    generated_at = payload.get("generated_at")
    observed_at = generated_at if isinstance(generated_at, int) and not isinstance(generated_at, bool) else now_s
    status_evidence = evidence_from_status_rows(
        [row for row in status_rows if isinstance(row, Mapping)],
        observed_at_s=observed_at,
    )
    receipt_evidence = (
        evidence_from_receipt_store(receipt_store_path, now_s=now_s)
        if receipt_store_path is not None
        else {}
    )
    return merge_evidence(health_evidence or {}, status_evidence, receipt_evidence)


def _from_evidence(
    service_id: str,
    enabled: bool,
    configured: bool,
    idle_startable: bool,
    evidence: Mapping[str, Any],
) -> AvailabilityProjection | None:
    available = evidence.get("available")
    raw_status = str(evidence.get("status") or "").strip().lower()
    if available is True or raw_status in READY_STATUSES:
        return AvailabilityProjection(
            service_id, enabled, configured, True, "ready",
            str(evidence.get("reason") or "Service responded."), True, dict(evidence), idle_startable,
        )
    if raw_status in STARTING_STATUSES:
        return AvailabilityProjection(
            service_id, enabled, configured, None, "starting",
            str(evidence.get("reason") or "The service is starting."), True, dict(evidence), idle_startable,
        )
    if raw_status in BUSY_STATUSES:
        return AvailabilityProjection(
            service_id, enabled, configured, True, "busy",
            str(evidence.get("reason") or "The service is busy."), True, dict(evidence), idle_startable,
        )
    if idle_startable and _observed_stopped(evidence) and evidence.get("source") != "request":
        return AvailabilityProjection(
            service_id, enabled, configured, None, "idle",
            "Ready to start.", False, dict(evidence), idle_startable=True,
        )
    if available is False or raw_status in DOWN_STATUSES:
        return AvailabilityProjection(
            service_id, enabled, configured, False, "unavailable",
            str(evidence.get("reason") or "The service is unavailable."), True, dict(evidence), idle_startable,
        )
    if available is None:
        return AvailabilityProjection(
            service_id, enabled, configured, None, "unknown",
            str(evidence.get("reason") or "Not measured yet"), False, dict(evidence), idle_startable,
        )
    return None


def _capability_states(projection: AvailabilityProjection) -> tuple[str, str]:
    if projection.status == "ready":
        return "warm", "healthy"
    if projection.status == "starting":
        return "warming", "degraded"
    if projection.status == "busy":
        return "warm", "degraded"
    if projection.status == "idle":
        return "stopped", "unknown"
    if projection.status == "unavailable":
        return "stopped", "unhealthy"
    return "unknown", "unknown"


def _services_mapping(service_records: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = service_records.get("services") if isinstance(service_records.get("services"), Mapping) else service_records
    return {str(key): value for key, value in (raw or {}).items() if isinstance(value, Mapping)}


def _is_configured(service: Mapping[str, Any]) -> bool:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else service
    if not isinstance(config, Mapping):
        return True
    if service.get("configured") is False or config.get("configured") is False:
        return False
    if service.get("missing") or config.get("missing"):
        return False
    return True


def _is_idle_startable(service: Mapping[str, Any]) -> bool:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else service
    lifecycle = service.get("lifecycle")
    if not isinstance(lifecycle, Mapping) and isinstance(config, Mapping):
        lifecycle = config.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        driver = str(lifecycle.get("driver") or lifecycle.get("type") or "external").strip().lower()
        if driver and driver != "external":
            return True
        if lifecycle.get("managed") is True or lifecycle.get("service_name"):
            return True
    return any(config.get(flag) is True for flag in ("gateway_lifecycle", "local_lifecycle", "lifecycle_backed"))


def _receipt_timestamp_ms(receipt: Mapping[str, Any]) -> int | None:
    for key in ("updated_at_ms", "accepted_at_ms"):
        value = receipt.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    value = receipt.get("_stored_at")
    if isinstance(value, int) and not isinstance(value, bool):
        return value * 1000
    return None


def _receipt_timestamp_s(receipt: Mapping[str, Any]) -> int | None:
    timestamp_ms = _receipt_timestamp_ms(receipt)
    return timestamp_ms // 1000 if timestamp_ms is not None else None


def _receipt_proves_backend_failure(receipt: Mapping[str, Any]) -> bool:
    status = str(receipt.get("status") or "").strip().lower()
    if status in {"timed_out", "timeout", "disconnected"}:
        return True
    code = receipt.get("status_code")
    if code is None and isinstance(receipt.get("metadata"), Mapping):
        code = receipt["metadata"].get("status_code")  # type: ignore[index]
    if isinstance(code, int) and not isinstance(code, bool):
        return code >= 500
    return False


def _evidence_unknown(evidence: Mapping[str, Any]) -> bool:
    status = str(evidence.get("status") or "").strip().lower()
    return status in {"", "unknown"} and evidence.get("available") is None


def _observed_stopped(evidence: Mapping[str, Any]) -> bool:
    status = str(evidence.get("status") or evidence.get("state") or "").strip().lower()
    return status in {"stopped", "idle", "not_running"}


def _evidence_newer(incoming: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    if incoming.get("source") == "request" and current.get("kind") == "no_response":
        return True
    if _evidence_unknown(incoming) and current.get("source") == "request":
        return False
    if _evidence_unknown(current) and incoming.get("source") == "request":
        return True
    incoming_ts = incoming.get("observed_at_s")
    current_ts = current.get("observed_at_s")
    if isinstance(incoming_ts, int) and isinstance(current_ts, int):
        return incoming_ts >= current_ts
    if incoming.get("source") == "request" and current.get("source") in {None, ""}:
        return True
    return False


def _public_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("source", "kind", "receipt_status", "observed_at_s"):
        value = evidence.get(key)
        if value is not None:
            out[key] = value
    return out


__all__ = [
    "AvailabilityProjection",
    "project_capabilities",
    "project_service",
    "evidence_from_receipts",
    "evidence_from_receipt_store",
    "evidence_from_status_rows",
    "availability_evidence_snapshot",
    "merge_evidence",
]
