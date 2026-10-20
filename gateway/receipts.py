"""Route receipt helpers.

Receipts record decisions, routing, usage and spend facts without raw request
bodies or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from time import perf_counter, time
import asyncio
import logging
import json
from datetime import date
from functools import lru_cache
import threading
import sqlite3
from time import sleep
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any, Callable, Mapping
from uuid import uuid4

from gateway.model_ids import display_model_id
from gateway.resource_decisions import project_decision
from gateway.backends.request_shape import (
    TERMINAL_REASONS,
    sanitize_request_shape_fields_for_admin,
    sanitize_request_shape_stages_for_admin,
)

PHASE_FIELDS = ("queued_at_ms", "started_at_ms", "first_output_at_ms", "finished_at_ms")

CLIENT_VISIBLE_FIELDS = {
    "request_id",
    "protocol",
    "path",
    "intent",
    "requested_model",
    "resolved_model",
    "requested_workflow",
    "resolved_workflow",
    "accepted_inputs",
    "rejected_inputs",
    "ignored_inputs",
    "fallback_reasons",
    "warnings",
    "status",
    "latency_ms",
    "job_id",
    "stream",
    "remote",
    "cost",
    "usage",
    "source",
    "source_kind",
    "source_label",
    "client_hint",
    "effective_output_limit",
    "accepted_at_ms",
    "updated_at_ms",
    "refusal",
    "retention",
    *PHASE_FIELDS,
    "attempt_number",
}

# Admin adds routing and policy facts. Ownership (``owner_key_hash``) and the raw
# ``metadata`` bag stay out; the sanitised metadata projection is merged separately.
# Allowlist: a new receipt column stays out of responses until it is added here.
ADMIN_VISIBLE_FIELDS = frozenset(
    CLIENT_VISIBLE_FIELDS
    | {
        "app_profile", "app_id", "api_key_name", "connection_id", "client_address", "backend",
        "manifest_id", "manifest_version", "manifest_hash",
        "parent_request_id", "visibility",
        "policy_decision", "policy_sources", "policy_reasons",
        "resource_decision",
        "retention_facts",
    }
)

# The operator (``internal``) tier adds the raw metadata bag and nothing else.
OPERATOR_VISIBLE_FIELDS = ADMIN_VISIBLE_FIELDS | {"metadata"}

# Machine-readable refusal facts a client may see on its own receipt. Codes
# only: no amounts, provider names or other people's usage.
REFUSAL_CODES = frozenset({"budget_exceeded", "provider_needs_reconciliation"})
REFUSAL_PERIODS = frozenset({"daily", "monthly", "request"})
REFUSAL_SCOPES = frozenset({"person", "app", "provider", "global"})

# Content retention is a structural receipt fact. Keep this vocabulary closed:
# the ordinary receipt key-name guard does not inspect scalar values.
RETENTION_KINDS = frozenset({"library_prompt", "conversation"})
RETENTION_DECISIONS = frozenset({"kept", "not_kept", "failed"})
RETENTION_REASONS = frozenset({
    "kept", "server_disabled", "request_forget", "no_person",
    "member_disabled", "no_scope", "no_content", "storage_unavailable",
    "erasure_cutoff", "expired",
    "write_failed", "incomplete",
})
RETENTION_LIMITS = frozenset({"member_choice", "owner_cap", "prior_policy", "unbounded_store"})
RETENTION_PARTS = {
    "library_prompt": frozenset({"accepted"}),
    "conversation": frozenset({"accepted", "received"}),
}
_INVALID_STORED_RETENTION_LOCK = threading.Lock()
_invalid_stored_retention_logged = False


def validate_retention(value: Any) -> list[dict[str, Any]]:
    """Reject any content, unknown field, or value outside the receipt vocabulary."""
    if not isinstance(value, list) or len(value) > len(RETENTION_KINDS):
        raise ValueError("receipt retention must be a list of existing kinds")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("receipt retention entry must be an object")
        kind, decision, reason = item.get("kind"), item.get("decision"), item.get("reason")
        if type(kind) is not str or kind not in RETENTION_KINDS or kind in seen:
            raise ValueError("receipt retention kind is unknown or repeated")
        if (type(decision) is not str or decision not in RETENTION_DECISIONS
                or type(reason) is not str or reason not in RETENTION_REASONS):
            raise ValueError("receipt retention decision or reason is unknown")
        seen.add(kind)
        if decision == "kept":
            if set(item) != {"kind", "decision", "reason", "kept_until_ms", "parts", "limited_by"}:
                raise ValueError("kept retention entry has unexpected fields")
            if (reason != "kept" or type(item["limited_by"]) is not str
                    or item["limited_by"] not in RETENTION_LIMITS):
                raise ValueError("kept retention entry has an invalid reason or limit")
            deadline = item["kept_until_ms"]
            if deadline is not None and (type(deadline) is not int or deadline < 0):
                raise ValueError("receipt retention deadline must be milliseconds or null")
            parts = item["parts"]
            if (not isinstance(parts, list) or not parts
                    or any(type(part) is not str for part in parts)
                    or len(parts) != len(set(parts))
                    or any(part not in RETENTION_PARTS[kind] for part in parts)):
                raise ValueError("receipt retention parts are invalid")
        else:
            if set(item) != {"kind", "decision", "reason"}:
                raise ValueError("unkept retention entry has unexpected fields")
            if decision == "failed" and reason != "write_failed":
                raise ValueError("failed retention entry needs write_failed")
            if decision == "not_kept" and reason in {"kept", "write_failed"}:
                raise ValueError("not_kept retention entry has an invalid reason")
        out.append(dict(item))
    return out


def _retention_from_store(value: Any) -> list[dict[str, Any]]:
    """Keep valid historical entries without letting one retired value break receipt reads."""
    if not isinstance(value, list):
        _log_invalid_stored_retention_once()
        return []
    valid: list[dict[str, Any]] = []
    for item in value:
        try:
            valid = validate_retention([*valid, item])
        except ValueError:
            _log_invalid_stored_retention_once()
    return valid


def _log_invalid_stored_retention_once() -> None:
    global _invalid_stored_retention_logged
    with _INVALID_STORED_RETENTION_LOCK:
        if not _invalid_stored_retention_logged:
            logging.getLogger(__name__).warning("Stored receipt retention entry is invalid; omitting it")
            _invalid_stored_retention_logged = True


def sanitize_refusal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("code") not in REFUSAL_CODES:
        return None
    limit = value.get("limit")
    if (isinstance(limit, Mapping) and limit.get("period") in REFUSAL_PERIODS
            and limit.get("scope") in REFUSAL_SCOPES):
        return {"code": value["code"], "limit": {"period": limit["period"], "scope": limit["scope"]}}
    return {"code": value["code"], "limit": None}

# Metadata keys promoted to top-level fields for the "admin" audience even
# though the rest of ``metadata`` is hidden there (a free-text ops note is
# not assumed operator-safe just because a caller happened to write one
# today). These specific keys are written only by the request-shape/staged
# observability path (gateway/backends/request_shape.py) and its terminal
# outcome — always structural, never a raw note — so they're safe to
# special-case rather than trust the whole metadata dict.
ADMIN_VISIBLE_METADATA_FIELDS = {
    "request_shape",
    "request_shape_stages",
    "terminal_reason",
    "litellm",
    "status_code",
    "stt",
    "decisions",
}

# Canonical allowlist for metadata["litellm"] — mirrors the keys
# proxy.py's _attach_litellm_response_metadata already restricts itself to
# (small bounded scalars: a model name/group, a cost, a duration, a
# retry/fallback count). Deliberately excludes any call-id/URL-shaped key —
# see independent-gate-3.md finding 4 (admin_model_api_base,
# admin_call_id_length). Re-declared here, not imported from proxy.py, so
# this module has no dependency on the application entrypoint; the two
# lists are kept in sync manually since call sites for each rarely change.
_LITELLM_ADMIN_SAFE_FIELDS = frozenset(
    {
        "model_id",
        "model_group",
        "response_cost",
        "response_duration_ms",
        "attempted_retries",
        "attempted_fallbacks",
    }
)
_LITELLM_ADMIN_MAX_VALUE_LENGTH = 128


def _sanitize_litellm_metadata_for_admin(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, str] = {}
    for key in _LITELLM_ADMIN_SAFE_FIELDS:
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            out[key] = raw[:_LITELLM_ADMIN_MAX_VALUE_LENGTH]
    return out or None


def _sanitize_decision_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    count = value.get("question_count")
    if type(count) is not int or count < 0:
        return None
    remote = value.get("remote")
    if type(remote) is not bool:
        return None
    raw_cost = value.get("cost_usd")
    if remote:
        if raw_cost is None:
            cost = None
        elif isinstance(raw_cost, (str, int, float)) and not isinstance(raw_cost, bool):
            try:
                amount = Decimal(str(raw_cost))
            except InvalidOperation:
                return None
            if not amount.is_finite() or amount < 0:
                return None
            cost = str(amount)
        else:
            return None
    else:
        if raw_cost != 0:
            return None
        cost = 0.0
    engine = value.get("engine")
    if engine not in (None, "cpu", "gpu") or (remote and engine is not None):
        return None
    out = {"question_count": count, "engine": engine, "remote": remote, "cost_usd": cost}
    usage = value.get("usage")
    if isinstance(usage, Mapping):
        safe_usage = {key: usage[key] for key in ("input_tokens", "output_tokens")
                      if type(usage.get(key)) is int and usage[key] >= 0}
        if safe_usage:
            out["usage"] = safe_usage
    return out


def sanitize_admin_metadata(metadata: Any) -> dict[str, Any]:
    """Re-parse ``RouteReceipt.metadata`` for the admin audience.

    This is the single trust boundary for what an admin-audience receipt
    view may expose from ``metadata`` — every field is re-derived from a
    closed per-field schema, independent of whatever internal callers
    happened to write into ``metadata``. See
    ``gateway/backends/request_shape.py`` for the stage/shape schemas this
    delegates to, and independent-gate-3.md finding 4 for the exploit this
    replaces (verbatim promotion of unvalidated nested metadata).
    """
    if not isinstance(metadata, Mapping):
        return {}
    out: dict[str, Any] = {}
    shape = sanitize_request_shape_fields_for_admin(metadata.get("request_shape"))
    if shape:
        out["request_shape"] = shape
    stages = sanitize_request_shape_stages_for_admin(metadata.get("request_shape_stages"))
    if stages:
        out["request_shape_stages"] = stages
    terminal_reason = metadata.get("terminal_reason")
    if isinstance(terminal_reason, str) and terminal_reason in TERMINAL_REASONS:
        out["terminal_reason"] = terminal_reason
    status_code = metadata.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        out["status_code"] = status_code
    stt = metadata.get("stt")
    if isinstance(stt, Mapping):
        field_count = stt.get("field_count")
        file_count = stt.get("file_count")
        attempts_started = stt.get("attempts_started")
        chunked = stt.get("chunked")
        if (
            all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (field_count, file_count, attempts_started))
            and isinstance(chunked, bool)
        ):
            out["stt"] = {
                "field_count": field_count,
                "file_count": file_count,
                "attempts_started": attempts_started,
                "chunked": chunked,
            }
    decisions = _sanitize_decision_metadata(metadata.get("decisions"))
    if decisions:
        out["decisions"] = decisions
    litellm_meta = _sanitize_litellm_metadata_for_admin(metadata.get("litellm"))
    if litellm_meta:
        out["litellm"] = litellm_meta
    return out


def new_request_id() -> str:
    return f"req_{uuid4().hex}"


# Whose request each recent receipt served, by request id. An internal hop (a
# protocol loopback, the LiteLLM round trip) knows only its parent's request id,
# so its receipt takes the caller from here. Bounded: a child of an evicted
# parent records no caller, as before.
_CALLER_FIELDS = ("app_id", "api_key_name", "owner_key_hash", "app_profile")
_CALLERS: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
_CALLERS_MAX = 4096
_CALLERS_LOCK = threading.Lock()


def _remember_caller(receipt: "RouteReceipt") -> None:
    with _CALLERS_LOCK:
        _CALLERS[receipt.request_id] = tuple(getattr(receipt, name) for name in _CALLER_FIELDS)
        _CALLERS.move_to_end(receipt.request_id)
        while len(_CALLERS) > _CALLERS_MAX:
            _CALLERS.popitem(last=False)


def _caller_of(request_id: str) -> tuple[Any, ...] | None:
    with _CALLERS_LOCK:
        return _CALLERS.get(request_id)


# The gateway injects its configured admin store. Standalone receipt users can
# still supply registry/overrides explicitly without importing the proxy.
_RETENTION_FACT_LOADER: Callable[[], tuple[Mapping[str, Any], Mapping[Any, Any]]] | None = None
_RETENTION_FACT_FAILURES: dict[str, int] = {}
_RETENTION_FACT_FAILURE_LOCK = threading.Lock()


def configure_retention_fact_capture(
    loader: Callable[[], tuple[Mapping[str, Any], Mapping[Any, Any]]],
) -> None:
    global _RETENTION_FACT_LOADER
    _RETENTION_FACT_LOADER = loader


def record_retention_fact_failure(stage: str, request_id: str, error: Exception) -> None:
    """Count optional-evidence failures without logging database contents."""
    with _RETENTION_FACT_FAILURE_LOCK:
        count = _RETENTION_FACT_FAILURES.get(stage, 0) + 1
        _RETENTION_FACT_FAILURES[stage] = count
    logging.getLogger(__name__).warning(
        "retention fact %s unavailable request_id=%s count=%d error=%s",
        stage, request_id, count, type(error).__name__,
    )


@lru_cache(maxsize=64)
def _captured_retention_generation(
    service_id: str, registry_generation: str, overrides_generation: str, today: str,
) -> tuple[dict[str, Any], Any]:
    from gateway import media_jobs, retention, retention_facts

    overrides = {tuple(key) if isinstance(key, list) else key: value
                 for key, value in json.loads(overrides_generation)}
    ledger = retention_facts.ledger(
        json.loads(registry_generation), kinds=retention.bundled().data["kinds"],
        overrides=overrides, service_id=service_id, now=date.fromisoformat(today),
    )
    return ledger, media_jobs.prepare_fact_snapshot(ledger["destinations"])


@dataclass
class RouteReceipt:
    protocol: str
    intent: str
    path: str = ""
    request_id: str = field(default_factory=new_request_id)
    app_profile: str | None = None
    # ``app_id`` is the durable id of the owning App (when the request was
    # authenticated by an App credential). It is the safe owner key for the
    # user-facing activity feed: ``app_profile`` is a shared permission-profile
    # id and ``api_key_name`` is a non-unique App name, so neither can scope a
    # feed to one user. None when there is no owning App (unauthenticated,
    # legacy config keys, or provider-native passthrough without identity).
    app_id: str | None = None
    # The person who made a content-bearing request. Internal only; an App's
    # owner is not necessarily the maker of every request through that App.
    maker_person_id: str | None = None
    api_key_name: str | None = None
    # The MCP sign-in connection (``mcp_oauth_grants.id``) that made the request.
    # Such a request runs as the person's own session App, so ``app_id`` alone
    # cannot tell ChatGPT from the person's browser. None for every other request.
    connection_id: str | None = None
    # The trusted client address a keyless request ran under; None when a key did.
    client_address: str | None = None
    source_kind: str | None = None
    source_label: str | None = None
    client_hint: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    requested_workflow: str | None = None
    resolved_workflow: str | None = None
    backend: str | None = None
    manifest_id: str | None = None
    manifest_version: str | None = None
    manifest_hash: str | None = None
    job_id: str | None = None
    parent_request_id: str | None = None
    visibility: str = "internal"
    policy_decision: str | None = None
    policy_sources: list[str] = field(default_factory=list)
    policy_reasons: list[str] = field(default_factory=list)
    accepted_inputs: list[str] = field(default_factory=list)
    rejected_inputs: dict[str, str] = field(default_factory=dict)
    ignored_inputs: dict[str, str] = field(default_factory=dict)
    fallback_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str | None = None
    latency_ms: float | None = None
    stream: bool = False
    remote: bool = False
    cost: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    effective_output_limit: int | None = None
    refusal: dict[str, Any] | None = None
    # None means a submit receipt defers the content outcome to its work receipt.
    retention: list[dict[str, Any]] | None = field(default_factory=list)
    retention_facts: list[str] = field(default_factory=list)
    resource_decision: dict[str, Any] | None = None
    accepted_at_ms: int | None = None
    updated_at_ms: int | None = None
    # Unix milliseconds; None explicitly means unavailable, never estimated.
    queued_at_ms: int | None = None
    started_at_ms: int | None = None
    first_output_at_ms: int | None = None
    finished_at_ms: int | None = None
    attempt_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _started_at: float = field(default_factory=perf_counter, repr=False)
    owner_key_hash: str | None = None

    def __post_init__(self) -> None:
        from gateway.request_source import (
            connection_for, current_client_hint, current_gateway_ui, trusted_address_for,
        )

        if self.parent_request_id and self.app_id is None:
            caller = _caller_of(self.parent_request_id)
            if caller is not None:
                for name, value in zip(_CALLER_FIELDS, caller):
                    setattr(self, name, value)
        if self.app_id:
            _remember_caller(self)
        self.client_address = self.client_address or trusted_address_for(self.app_id)
        self.connection_id = self.connection_id or connection_for(self.app_id)
        self._note_frontend_loss()
        if self.source_kind is not None:
            return
        if self.parent_request_id:
            self.source_kind = "internal"
            self.source_label = "Gateway internal request"
            return
        if current_gateway_ui():
            self.source_kind = "gateway-ui"
            self.source_label = "Gateway UI"
            return
        self.client_hint = self.client_hint or current_client_hint()
        if self.app_id:
            self.source_kind = "app"
            self.source_label = self.api_key_name or "App"
        else:
            self.source_kind = "local-network"
            self.source_label = "Local network · no key"
        if self.client_hint:
            self.source_label += f" · {self.client_hint}"

    def _note_frontend_loss(self) -> None:
        """Record what the inbound frontend dropped before this receipt opened (gateway/frontend_loss.py)."""
        from gateway import frontend_loss

        report = frontend_loss.current()
        if report is None or report.empty:
            return
        self.metadata["loss"] = report.as_dict()
        for name in report.dropped:
            self.ignored_inputs.setdefault(str(name)[:48], "dropped by a lenient frontend")

    def accept_input(self, name: str) -> None:
        if name not in self.accepted_inputs:
            self.accepted_inputs.append(name)
        self.rejected_inputs.pop(name, None)
        self.ignored_inputs.pop(name, None)

    def reject_input(self, name: str, reason: str) -> None:
        if name in self.accepted_inputs:
            self.accepted_inputs.remove(name)
        self.rejected_inputs[name] = reason
        self.ignored_inputs.pop(name, None)

    def ignore_input(self, name: str, reason: str) -> None:
        if name in self.accepted_inputs:
            self.accepted_inputs.remove(name)
        self.ignored_inputs[name] = reason
        self.rejected_inputs.pop(name, None)

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)

    def add_fallback_reason(self, reason: str) -> None:
        self.fallback_reasons.append(reason)

    def set_policy_decision(
        self,
        decision: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        if decision not in {"allowed", "denied", "not_evaluated"}:
            raise ValueError("policy decision must be allowed, denied, or not_evaluated")
        self.policy_decision = decision
        if reason:
            self.policy_reasons.append(reason)
        if source:
            self.policy_sources.append(source)

    def set_refusal(self, refusal: Mapping[str, Any]) -> None:
        """Record why the gateway refused this request (see sanitize_refusal)."""
        self.refusal = sanitize_refusal(refusal)

    def set_retention(self, **entry: Any) -> None:
        """Replace the outcome for one content kind after its write has resolved."""
        if self.retention is None:
            raise ValueError("content retention belongs to the work receipt")
        kind = entry.get("kind")
        updated = [item for item in self.retention if item["kind"] != kind]
        self.retention = validate_retention([*updated, entry])

    def capture_retention_facts(self, *, registry: Mapping[str, Any] | None = None,
                                overrides: Mapping[Any, Any] | None = None,
                                service_id: str | None = None,
                                outer: "RouteReceipt | None" = None) -> None:
        """Freeze dated evidence before the lifecycle dispatches to a destination."""
        captured = getattr(self, "_retention_fact_snapshot", None)
        if not self.backend:
            return
        if captured is not None and captured[0] == self.backend:
            if outer is not None and getattr(outer, "_retention_fact_snapshot", None) is None:
                outer.backend = self.backend
                outer._retention_fact_snapshot = captured
            return
        if registry is None or overrides is None:
            if _RETENTION_FACT_LOADER is None:
                return
            loaded_registry, loaded_overrides = _RETENTION_FACT_LOADER()
            registry = loaded_registry if registry is None else registry
            overrides = loaded_overrides if overrides is None else overrides
        destination_id = service_id or self.backend
        if destination_id not in (registry.get("services") or {}):
            return
        # Content generations also cover out-of-process edits and DB restores;
        # they never depend on a writer notifying this process about a change.
        registry_generation = json.dumps(registry, sort_keys=True, separators=(",", ":"))
        overrides_generation = json.dumps(sorted(
            ((key, value) for key, value in overrides.items()), key=lambda item: repr(item[0]),
        ), sort_keys=True, separators=(",", ":"))
        ledger, prepared = _captured_retention_generation(
            destination_id, registry_generation, overrides_generation, date.today().isoformat(),
        )
        self._retention_fact_snapshot = (self.backend, ledger, prepared)
        if outer is not None and getattr(outer, "_retention_fact_snapshot", None) is None:
            outer.backend = self.backend
            outer._retention_fact_snapshot = self._retention_fact_snapshot

    def persist_retention_facts(self) -> None:
        """Persist the evidence captured before dispatch, once per receipt."""
        captured = getattr(self, "_retention_fact_snapshot", None)
        if self.retention_facts or getattr(self, "_retention_facts_persisted", False) or captured is None or captured[0] != self.backend:
            return
        from gateway import media_jobs

        for attempt in range(3):
            try:
                self.retention_facts = media_jobs.save_fact_snapshot(
                    captured[2], source_kind="receipt", source_id=self.request_id,
                    path=media_jobs.DB_PATH, timeout=0.2,
                )
                break
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or attempt == 2:
                    raise
                # Runs in the ordered receipt worker: at most 620 ms of lock
                # waits/backoff, without delaying the event loop or finalizing early.
                sleep(0.01)
        self._retention_facts_persisted = True

    def attach_manifest(
        self,
        *,
        manifest_id: str,
        manifest_version: str | None = None,
        manifest_hash: str | None = None,
    ) -> None:
        self.manifest_id = manifest_id
        self.manifest_version = manifest_version
        self.manifest_hash = manifest_hash

    def begin(self, *, now_ms: int | None = None) -> None:
        """Mark this receipt accepted before any upstream operation begins."""
        timestamp = int(now_ms if now_ms is not None else time() * 1000)
        self.status = "in_progress"
        if self.accepted_at_ms is None:
            self.accepted_at_ms = timestamp
        if self.attempt_number is None:
            self.attempt_number = 1
        self.updated_at_ms = timestamp

    def mark_phase(self, phase: str, *, now_ms: int | None = None) -> None:
        """Record a trusted observation once for this receipt's sole attempt."""
        if phase not in PHASE_FIELDS:
            raise ValueError("unknown receipt phase")
        if self.attempt_number is None:
            self.attempt_number = 1
        if getattr(self, phase) is None:
            setattr(self, phase, int(now_ms if now_ms is not None else time() * 1000))

    def finish(
        self,
        status: str,
        latency_ms: float | None = None,
        *,
        now_ms: int | None = None,
    ) -> None:
        self.status = status
        if latency_ms is None:
            latency_ms = (perf_counter() - self._started_at) * 1000
        self.latency_ms = round(latency_ms, 3)
        self.updated_at_ms = int(now_ms if now_ms is not None else time() * 1000)
        self.mark_phase("finished_at_ms", now_ms=self.updated_at_ms)

    def to_dict(self, audience: str = "internal") -> dict[str, Any]:
        from gateway.request_source import connection_for, trusted_address_for

        # Facades that attach their App after construction get the address here.
        self.client_address = self.client_address or trusted_address_for(self.app_id)
        self.connection_id = self.connection_id or connection_for(self.app_id)
        # Some facades attach their resolved App after constructing the receipt.
        # Derive its source from that final identity before persisting it.
        if self.app_id and self.source_kind == "local-network":
            self.source_kind = "app"
            self.source_label = self.api_key_name or "App"
            if self.client_hint:
                self.source_label += f" · {self.client_hint}"
        data = {
            "request_id": self.request_id,
            "protocol": self.protocol,
            "path": self.path,
            "intent": self.intent,
            "app_profile": self.app_profile,
            "app_id": self.app_id,
            "client_address": self.client_address,
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "client_hint": self.client_hint,
            "owner_key_hash": self.owner_key_hash,
            "api_key_name": self.api_key_name,
            "connection_id": self.connection_id,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "requested_workflow": self.requested_workflow,
            "resolved_workflow": self.resolved_workflow,
            "backend": self.backend,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "job_id": self.job_id,
            "parent_request_id": self.parent_request_id,
            "visibility": self.visibility,
            "policy_decision": self.policy_decision,
            "policy_sources": list(self.policy_sources),
            "policy_reasons": list(self.policy_reasons),
            "accepted_inputs": list(self.accepted_inputs),
            "rejected_inputs": dict(self.rejected_inputs),
            "ignored_inputs": dict(self.ignored_inputs),
            "fallback_reasons": list(self.fallback_reasons),
            "warnings": list(self.warnings),
            "status": self.status,
            "latency_ms": self.latency_ms,
            "stream": self.stream,
            "remote": self.remote,
            "cost": dict(self.cost) if isinstance(self.cost, Mapping) else self.cost,
            "usage": dict(self.usage) if isinstance(self.usage, Mapping) else self.usage,
            "effective_output_limit": self.effective_output_limit,
            "accepted_at_ms": self.accepted_at_ms,
            "updated_at_ms": self.updated_at_ms,
            **{name: getattr(self, name) for name in PHASE_FIELDS},
            "attempt_number": self.attempt_number,
            "metadata": dict(self.metadata),
        }
        if self.maker_person_id is not None:
            data["maker_person_id"] = self.maker_person_id
        if self.retention is not None:
            data["retention"] = validate_retention(self.retention)
        if self.retention_facts:
            data["retention_facts"] = list(self.retention_facts)
        if self.resource_decision is not None:
            data["resource_decision"] = self.resource_decision
        if isinstance(self.refusal, Mapping):
            # Present only on refused requests; absent means none recorded.
            data["refusal"] = dict(self.refusal)
        return serialize_receipt_for_audience(data, audience)


def serialize_receipt_for_audience(
    data: Mapping[str, Any], audience: str = "internal", *,
    from_store: bool = False, retention_detail: bool = True,
) -> dict[str, Any]:
    data = {**dict.fromkeys((*PHASE_FIELDS, "attempt_number")), **data}
    if "retention" in data:
        entries = _retention_from_store(data["retention"]) if from_store else validate_retention(data["retention"])
        data["retention"] = entries if retention_detail or audience == "internal" else [
            {"kind": item["kind"], "decision": item["decision"]} for item in entries
        ]
    if audience not in {"internal", "admin", "client"}:
        raise ValueError("audience must be internal, admin, or client")
    if audience == "internal":
        return dict(data)
    if audience == "admin":
        out = {name: data[name] for name in ADMIN_VISIBLE_FIELDS if name in data}
        out.update(sanitize_admin_metadata(data.get("metadata")))
        if isinstance(data.get("resource_decision"), Mapping):
            out["resource_decision"] = project_decision(data["resource_decision"], "admin")
        return out
    client_data = {name: data[name] for name in CLIENT_VISIBLE_FIELDS if name in data}
    if isinstance(data.get("resource_decision"), Mapping):
        client_data["resource_decision"] = project_decision(data["resource_decision"])
    for model_field in ("requested_model", "resolved_model"):
        if isinstance(client_data.get(model_field), str):
            client_data[model_field] = display_model_id(client_data[model_field])
    if "refusal" in client_data:
        refusal = sanitize_refusal(client_data.pop("refusal"))
        if refusal is not None:
            client_data["refusal"] = refusal
    cost = client_data.get("cost")
    if isinstance(cost, Mapping):
        client_data["cost"] = {
            name: value
            for name, value in cost.items()
            if name in {"usd", "source", "cost_class", "reconciliation_status"}
        }
    metadata = data.get("metadata")
    decisions = _sanitize_decision_metadata(metadata.get("decisions")) if isinstance(metadata, Mapping) else {}
    if decisions:
        client_data["decisions"] = decisions
    return client_data


async def run_receipt_write(write: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Complete an ordered receipt write off-loop, even if its caller is cancelled."""
    worker = asyncio.create_task(asyncio.to_thread(write, *args, **kwargs))
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as error:
            # Do not detach a fact commit from its following finalize, or let
            # cancellation return while the receipt is still being mutated.
            if worker.cancelled():
                raise
            cancellation = error
    if cancellation is not None:
        raise cancellation
    return result


class ReceiptLifecycleOwner:
    """Own the terminal lifecycle of one admitted outer receipt.

    ``report`` only records the bounded normal outcome.  ``__aexit__`` and
    ``wrap_stream`` are the sole terminal writers: cancellation always wins
    over a reported normal outcome, ordinary BaseException is failed, and the
    store callback is deliberately best-effort.
    """

    def __init__(
        self,
        receipt: RouteReceipt,
        *,
        begin: Callable[[RouteReceipt], object] | None = None,
        finalize: Callable[[RouteReceipt], object] | None = None,
        log: Callable[[str], object] | None = None,
        on_terminal: Callable[[RouteReceipt, str], object] | None = None,
        checkpoint: Callable[[RouteReceipt], object] | None = None,
    ) -> None:
        self.receipt = receipt
        default_persistence = begin is None
        # Delayed import avoids the receipt_store -> receipts import cycle and
        # keeps persistence ownership here rather than in each HTTP route.
        if begin is None or finalize is None:
            from gateway import receipt_store
            begin = begin or receipt_store.begin
            finalize = finalize or receipt_store.finalize
        if checkpoint is None and default_persistence:
            from gateway import receipt_store
            checkpoint = receipt_store.update_phases
        self._checkpoint = checkpoint
        self._begin = begin
        self._finalize = finalize
        self._log = log or (lambda message: print(f"[proxy] {message}", flush=True))
        self._on_terminal = on_terminal
        self._outcome: tuple[str, str] | None = None
        self._closed = False
        self._stream_transferred = False
        if default_persistence:
            try:
                self.receipt.capture_retention_facts()
            except Exception as error:
                record_retention_fact_failure("capture", self.receipt.request_id, error)
        # Admission is deliberately synchronous: SQLite receipt writes are
        # synchronous, and no caller can accidentally put an await boundary
        # between construction and persistence.
        self._begin_once()

    def _begin_once(self) -> None:
        try:
            self._begin(self.receipt)
        except Exception as error:
            self._log(f"receipt begin failed: {error}")

    def report(self, *, status: str, reason: str) -> None:
        """Describe a normal route outcome; this never persists a terminal row."""
        self._outcome = (status, reason)

    def mark_phase(self, phase: str) -> None:
        if getattr(self.receipt, phase, None) is not None:
            return
        self.receipt.mark_phase(phase)
        if self._checkpoint is not None:
            try:
                self._checkpoint(self.receipt)
            except Exception as error:
                self._log(f"receipt phase write failed: {error}")

    def _close(self, status: str, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.receipt.metadata["terminal_reason"] = reason
        if self._on_terminal is not None:
            try:
                self._on_terminal(self.receipt, reason)
            except Exception as error:
                self._log(f"receipt terminal annotation failed: {error}")
        self.receipt.finish(status)
        try:
            self.receipt.persist_retention_facts()
        except Exception as error:
            record_retention_fact_failure("persist", self.receipt.request_id, error)
        try:
            self._finalize(self.receipt)
        except Exception as error:
            self._log(f"receipt finalize failed: {error}")

    async def __aenter__(self) -> "ReceiptLifecycleOwner":
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        if self._stream_transferred:
            return False
        if isinstance(exc, asyncio.CancelledError):
            await run_receipt_write(self._close, "cancelled", "cancelled")
        elif exc is not None:
            await run_receipt_write(self._close, "failed", "internal_error")
        else:
            await run_receipt_write(self._close, *(self._outcome or ("failed", "internal_error")))
        return False

    def handoff_stream(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Detach a handler scope and retain ownership in the response body."""
        if self._closed:
            raise RuntimeError("cannot hand off a closed receipt lifecycle")
        self._stream_transferred = True
        return self.wrap_stream(source)

    async def wrap_stream(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Transfer lifecycle ownership past a StreamingResponse return."""
        # The handler's __aexit__ observes this flag and leaves ownership to
        # the generator.  Once the generator starts, its own scope must close.
        self._stream_transferred = False
        async with self:
            async for item in source:
                # Ignore empty chunks and SSE comments/keepalives. This is
                # first output observed by the owner, not backend token time.
                if self.receipt.first_output_at_ms is None:
                    lines = [line.strip() for line in item.splitlines() if line.strip()]
                    if any(not line.startswith(b":") and line != b"data: [DONE]" for line in lines):
                        self.mark_phase("first_output_at_ms")
                yield item


def record_validation_result(receipt: RouteReceipt, result: Any) -> None:
    """Attach a manifest validation result without coupling receipt imports."""
    for name in getattr(result, "accepted", {}):
        receipt.accept_input(name)
    for name, reason in getattr(result, "rejected", {}).items():
        receipt.reject_input(name, reason)
