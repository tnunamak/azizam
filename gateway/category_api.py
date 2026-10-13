"""Owner-only category snapshots, previews and compare-and-swap activation."""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.admin_api import AdminAuth, _require_admin
from gateway import category_policy as policy
from gateway.category_store import (
    CategoryStore, CorruptSnapshotError, NoActiveSnapshotError,
    SnapshotNotFoundError, StaleBaseError,
)


class _AmbiguousProvenanceError(ValueError):
    """Distinct stored records are equally eligible to supply provenance."""


def _object(value, required, optional=()):
    if type(value) is not dict or not all(type(k) is str for k in value):
        raise ValueError("Expected an object")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Unknown or missing fields")
    return value


def _text(value):
    if type(value) is not str:
        raise ValueError("Expected a string")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("Expected a boolean")
    return value


def _list(value, decode):
    if type(value) is not list:
        raise ValueError("Expected an array")
    return tuple(decode(item) for item in value)


def _map(value, decode):
    if type(value) is not dict or any(type(k) is not str for k in value):
        raise ValueError("Expected a string-keyed object")
    return {key: decode(item) for key, item in value.items()}


def _json(value):
    # Mapping first preserves the Grant mapping contract (absent optional IDs).
    if isinstance(value, Mapping):
        return {_text(key): _json(item) for key, item in value.items()}
    if is_dataclass(value):
        return {field.name: _json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("Expected a JSON value")


def _category(value):
    v = _object(value, ("id", "label"), ("kind", "values", "applies_to"))
    return policy.Category(_text(v["id"]), _text(v["label"]),
                           _text(v.get("kind", "set")),
                           _list(v.get("values", []), _text),
                           _list(v.get("applies_to", ["release", "route", "workflow"]), _text))


def _assertion(value):
    v = _object(value, ("category", "value", "source"),
                ("accepted", "observed_at", "accepted_at", "accepting_owner", "mapping_version"))
    return policy.Assertion(
        _text(v["category"]), _json(v["value"]), _text(v["source"]),
        _boolean(v.get("accepted", False)),
        *(_text(v.get(key, "")) for key in
          ("observed_at", "accepted_at", "accepting_owner", "mapping_version")))


def _subject(value):
    v = _object(value, ("id", "kind"), ("assertions",))
    return policy.Subject(_text(v["id"]), _text(v["kind"]),
                          _list(v.get("assertions", []), _assertion))


def _service(value):
    v = _object(value, ("id", "members"), ("dependencies", "checkable"))
    return policy.Service(_text(v["id"]), _list(v["members"], _subject),
                          _list(v.get("dependencies", []), _service),
                          _boolean(v.get("checkable", True)))


def _row(value):
    v = _object(value, ("id", "category", "op"), ("values",))
    return policy.Row(*(_text(v[key]) for key in ("id", "category", "op")),
                      _list(v.get("values", []), _text))


def _grant(value):
    v = _object(value, ("capability", "service"), ("release", "workflow"))
    return {key: _text(item) for key, item in v.items()}


def _plan(value):
    v = _object(value, ("id",), ("grants", "require", "deny", "missing", "mode", "legacy_tags"))
    return policy.Plan(
        _text(v["id"]), _list(v.get("grants", []), _grant),
        _list(v.get("require", []), _row), _list(v.get("deny", []), _row),
        _map(v.get("missing", {}), _text), _text(v.get("mode", "v3")),
        _list(v.get("legacy_tags", []), _text))


def snapshot_from_json(value: dict, *, validate: bool = True) -> policy.PolicyVersion:
    """Decode the schema; candidates validate after server provenance is applied."""
    v = _object(value, ("id", "categories", "people", "apps", "services"),
                ("completeness_gate", "retained"))
    result = policy.PolicyVersion(
        _text(v["id"]), _map(v["categories"], _category),
        _map(v["people"], lambda plans: _list(plans, _plan)),
        _map(v["apps"], lambda plans: _list(plans, _plan)),
        _map(v["services"], _service), _boolean(v.get("completeness_gate", True)),
        _map(v.get("retained", {}), _service))
    if validate:
        policy.validate_snapshot(result)
    return result


def _stamp_acceptance(before, after, owner, accepted_at):
    """Preserve base assertions; attribute edits to the authenticated owner."""
    def key(subject, assertion):
        # Acceptance of previously inert evidence is itself an owner action.
        return (subject.id, subject.kind, assertion.category,
                json.dumps(assertion.value, sort_keys=True, separators=(",", ":")),
                assertion.source, assertion.accepted)

    def add(records, fact_key, assertion):
        candidates = records.setdefault(fact_key, [])
        if assertion not in candidates:
            candidates.append(assertion)

    stored = {}
    stored_by_bundle = {}
    stored_by_service = {}
    active_by_stem = {}
    retained_by_stem = {}
    if before is not None:
        for group in ("services", "retained"):
            for identifier, service in getattr(before, group).items():
                local = {}
                by_service = stored_by_service.setdefault(service.id, {})
                by_stem = (active_by_stem if group == "services" else retained_by_stem).setdefault(
                    service.id.rsplit("@", 1)[0], {})
                for subject in policy.resolve(service):
                    for assertion in subject.assertions:
                        fact_key = key(subject, assertion)
                        for records in (local, by_service, by_stem, stored):
                            add(records, fact_key, assertion)
                stored_by_bundle[group, identifier] = (service.id, local)

    def sources(group, identifier, service):
        service_id, local = stored_by_bundle.get((group, identifier), (None, {}))
        # Retained keys name outputs, not services; a reused key is not identity.
        if service_id != service.id:
            local = {}
        stem = service.id.rsplit("@", 1)[0]
        return (local, stored_by_service.get(service.id, {}),
                active_by_stem.get(stem, {}), retained_by_stem.get(stem, {}), stored)

    def assertion_copy(subject, assertion, source_records):
        fact_key = key(subject, assertion)
        for records in source_records:
            candidates = records.get(fact_key, [])
            if not candidates:
                continue
            # Submitted provenance can identify a stored record, but only the
            # complete server-stored record is ever returned as authority.
            exact = [item for item in candidates if item == assertion]
            observation = [item for item in candidates
                           if (item.observed_at, item.mapping_version) ==
                           (assertion.observed_at, assertion.mapping_version)]
            eligible = exact or observation or candidates
            if len(eligible) != 1:
                raise _AmbiguousProvenanceError()
            return eligible[0]
        return replace(assertion, accepting_owner=owner, accepted_at=accepted_at)

    def service_copy(service, source_records):
        members = []
        for subject in service.members:
            assertions = tuple(assertion_copy(subject, assertion, source_records)
                               for assertion in subject.assertions)
            members.append(replace(subject, assertions=assertions))
        return replace(service, members=tuple(members),
                       dependencies=tuple(service_copy(item, source_records) for item in service.dependencies))

    return replace(after,
                   services={key: service_copy(value, sources("services", key, value))
                             for key, value in after.services.items()},
                   retained={key: service_copy(value, sources("retained", key, value))
                             for key, value in after.retained.items()})


def snapshot_to_json(snapshot: policy.PolicyVersion) -> dict:
    """Encode the complete snapshot, including provenance and retained bundles."""
    policy.validate_snapshot(snapshot)
    result = _json(snapshot)
    # Enforce the same boundary in both directions.
    snapshot_from_json(result)
    return result


def _revision(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError("Expected a positive revision")
    return value


def _error(status, code, message):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def create_category_router(*, auth: AdminAuth, store_path: Path,
                           service_catalog: Callable[[], list[dict]] = lambda: []) -> APIRouter:
    router = APIRouter(prefix="/admin/api/categories", tags=["Categories"])
    # Delayed initialization means unauthorized calls never access the store.

    def decoded(row):
        try:
            return snapshot_from_json(row.body)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CorruptSnapshotError("Invalid stored policy") from exc

    def record(row):
        return {"revision": row.revision, "parent_revision": row.parent_revision,
                "active_counter": row.active_counter, "snapshot": snapshot_to_json(decoded(row)),
                "body_sha256": row.body_sha256, "created_at": row.created_at,
                "author": row.author, "reason": row.reason}

    def base(store, expected):
        try:
            active = store.get_active()
        except NoActiveSnapshotError:
            if expected is not None:
                raise StaleBaseError()
            return None
        if active.revision != expected:
            raise StaleBaseError()
        return decoded(active)

    def compatible(before, after, store=None):
        policy.validate_snapshot(after)
        old = policy.service_catalog(before) if before is not None else {}
        new = policy.service_catalog(after)
        if any(old[key] != new[key] for key in old.keys() & new.keys()):
            raise ValueError("A changed closure requires a new service revision")
        if store is not None:
            # Service revision IDs are never recycled: an ID seen in any stored
            # snapshot keeps the closure it had there (design v3, A5/R2).
            for revision in store.revisions():
                seen = policy.service_catalog(decoded(store.get(revision)))
                if any(seen[key] != new[key] for key in seen.keys() & new.keys()):
                    raise ValueError("A service revision ID cannot be reused for a different closure")

    def install(path, method, operation):
        def handle(request: Request, raw: bytes):
            ctx = _require_admin(auth, request)
            if isinstance(ctx, JSONResponse):
                return ctx
            if "owner" not in ctx.roles:
                return _error(403, "owner_required", "Only the gateway owner can manage categories.")
            try:
                if operation == "services":
                    return {"services": service_catalog()}
                body = {}
                if method == "POST":
                    body = json.loads(raw, object_pairs_hook=_pairs,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                revision = request.path_params.get("revision")
                if revision is not None:
                    if not revision.isascii() or not revision.isdigit():
                        raise ValueError("Invalid revision")
                    revision = _revision(int(revision))
                store = CategoryStore(store_path)
                if operation == "active":
                    row = store.get_active()
                    result = record(row)
                    # created_at is when the snapshot was written; the audit row
                    # of its latest activation says when it went live.
                    activations = [entry for entry in store.history(limit=1000)
                                   if entry["action"] == "activate" and entry["revision"] == row.revision]
                    result["activated_at"] = activations[-1]["created_at"] if activations else None
                    return result
                if operation == "history":
                    return {"history": store.history()}
                if operation == "revision":
                    return record(store.get(revision))
                if operation in ("create", "restore"):
                    required = ("base_revision", "reason", "snapshot") if operation == "create" else ("base_revision", "reason")
                    _object(body, required)
                    expected = _revision(body["base_revision"], optional=True)
                    reason = _text(body["reason"])
                    if not reason.strip():
                        raise ValueError("Reason required")
                    before = base(store, expected)
                    after = (snapshot_from_json(body["snapshot"], validate=False) if operation == "create"
                             else decoded(store.get(revision)))
                    if operation == "create":
                        after = _stamp_acceptance(before, after, ctx.subject,
                                                  datetime.now(timezone.utc).isoformat())
                    compatible(before, after, store if operation == "create" else None)
                    # Compute against the exact immutable base BEFORE insertion.
                    # Concurrent activation makes create_candidate fail its own CAS.
                    preview = _json(policy.change_preview(before, after))
                    if operation == "restore":
                        row = store.restore(revision, expected, ctx.subject, reason)
                    else:
                        row = store.create_candidate(snapshot_to_json(after), expected, ctx.subject, reason)
                    result = record(row)
                    result["preview"] = preview
                    result["preview_base_revision"] = expected
                    if operation == "restore":
                        result["restored_from_revision"] = revision
                    return result
                if operation == "activate":
                    _object(body, ("expected_active_revision",))
                    expected = _revision(body["expected_active_revision"], optional=True)
                    before = base(store, expected)
                    candidate = store.get(revision)
                    if candidate.parent_revision != expected:
                        raise StaleBaseError()
                    compatible(before, decoded(candidate))
                    # Store commits pointer, counter and audit together, with CAS.
                    active = store.activate(revision, expected)
                    return {"revision": active.revision, "active_counter": active.active_counter}
                _object(body, ("person", "app", "capability", "service"), ("revision",))
                query = {key: _text(body[key]) for key in ("person", "app", "capability", "service")}
                selected = body.get("revision")
                try:
                    row = (store.get_active() if selected is None
                           else store.get(_revision(selected)))
                    version = decoded(row)
                except NoActiveSnapshotError:
                    version = None
                decisions = policy.preview_as(version, query["person"], query["app"], query["capability"])
                decision = decisions.get(query["service"], policy.decision_for(version, query))
                return _json(decision)
            except StaleBaseError:
                return _error(409, "stale_base", "The active revision changed; create a new preview.")
            except NoActiveSnapshotError:
                return _error(409, "no_active_snapshot", "No policy snapshot has been activated.")
            except SnapshotNotFoundError:
                return _error(404, "revision_not_found", "Policy revision not found.")
            except (CorruptSnapshotError, sqlite3.Error, OSError):
                if operation == "preview":
                    return _json(policy.deny("POLICY_UNAVAILABLE", "snapshot"))
                return _error(503, "policy_unavailable", "The policy store is unavailable.")
            except _AmbiguousProvenanceError:
                return _error(400, "invalid_request", "Ambiguous stored assertion provenance.")
            except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
                return _error(400, "invalid_request", "Invalid category policy request.")
        async def endpoint(request: Request):
            # Auth and the policy store use blocking SQLite: keep them off the event loop.
            raw = await request.body() if method == "POST" else b""
            return await asyncio.to_thread(handle, request, raw)

        # The endpoint reads path params itself; declare them for the OpenAPI schema.
        params = [{"name": "revision", "in": "path", "required": True, "schema": {"type": "integer"}}]
        router.add_api_route(path, endpoint, methods=[method], name="categories_" + operation,
                             openapi_extra={"parameters": params} if "{revision}" in path else None)

    install("/services", "GET", "services")
    install("/active", "GET", "active")
    install("/history", "GET", "history")
    install("/revisions/{revision}", "GET", "revision")
    install("/candidates", "POST", "create")
    install("/candidates/{revision}/activate", "POST", "activate")
    install("/revisions/{revision}/restore", "POST", "restore")
    install("/preview-as", "POST", "preview")
    return router
