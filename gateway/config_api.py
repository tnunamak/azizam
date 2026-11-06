"""Owner-only HTTP API over the config sections (docs/design/CONFIG_HOMES.md sections 6 and 7).

``gateway/config_layers.py`` and ``gateway/config_runtime.py`` own what an override means. This module only turns
HTTP into calls on them: read the effective sections with their sources, write a change of overrides, reset an entry to
inherit, revert, export and import, and the one-time legacy import of a startup-file block. Every write goes through
``admin_store.apply_config_change`` (one transaction: session, revisions, validation, rows, audit) and is followed by
the gateway's own reload, so a request that starts after the response sees the new snapshot. There is no cache here.

The service registry is not part of this API yet (C4). ``GET /admin/api/config/export`` keeps answering the registry's
YAML; ``?format=v1`` answers ``build_export`` below, and C4 makes it the one export.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import admin_store, config_layers as layers, config_runtime, secret_refs
from gateway.admin_api import AdminAuth, AdminContext, _require_admin
from gateway.session_guard import SessionEnded

PREFIX = "/admin/api/config"
MAX_ITEMS = 500
MODES = ("replace", "merge")
_NO_CACHE = {"Cache-Control": "no-store"}


class _DryRunDone(Exception):
    """Raised inside the write transaction after the candidate was judged, so that nothing is committed."""


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}, **extra}, status_code=status, headers=_NO_CACHE)


def _ok(body: Mapping[str, Any], status: int = 200, **headers: str) -> JSONResponse:
    return JSONResponse(dict(body), status_code=status, headers={**_NO_CACHE, **headers})


def _with(key: str, value: Any) -> dict[str, Any]:
    return {} if value is layers.ABSENT else {key: value}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _revision_text(value: Any) -> str:
    return str(value).strip().strip('"')


class _State:
    """The stored overrides of the sections this release ships, and every section's revision, from one read."""

    def __init__(self, home: config_runtime.ConfigHome, path: Path) -> None:
        stored = admin_store.read_config_state(path=path)
        self.raw: list[dict[str, Any]] = stored["rows"]
        self.revisions: dict[str, str] = {name: stored["revisions"].get(name, "0") for name in home.sections}
        self.rows: dict[str, list[layers.Override]] = {name: [] for name in home.sections}
        for row in self.raw:
            if row["section"] in self.rows:
                self.rows[row["section"]].append(config_runtime.override_from_row(row))


def _title(section: layers.Section) -> str:
    return str(section.schema.get("title") or section.id)


def _override_dict(override: layers.Override | None) -> dict[str, Any] | None:
    if override is None:
        return None
    return {
        "op": override.op, **({"value": override.value} if override.op == layers.OP_SET else {}),
        **_with("base", override.base), "origin": override.origin, "note": override.note,
        "change_id": override.change_id, "updated_at": override.updated_at, "updated_by": override.updated_by,
    }


def _drift_dict(drift: layers.Drift) -> dict[str, Any]:
    return {"pointer": drift.pointer, "kind": drift.kind, **_with("base", drift.base), **_with("release", drift.release),
            **_with("owner", drift.owner)}


def _entry_dict(view: layers.EntryView, running: Any) -> dict[str, Any]:
    """One row of the effective-source view (design 4.3). ``running`` is the published snapshot's section, or None."""
    entry: dict[str, Any] = {
        "pointer": view.pointer, "source": view.source, **_with("value", view.value),
        **_with("release_value", view.release_value), "override": _override_dict(view.override),
        "apply": view.apply, "fail": view.fail, "secret": view.secret,
        "drift": _drift_dict(view.drift) if view.drift else None,
        "problems": [problem.as_dict() for problem in view.problems], "pending": False,
    }
    if isinstance(running, Mapping):
        now = layers.get_at(running, view.pointer)
        if not layers.same_value(now, view.value):
            entry["pending"] = True
            entry.update(_with("running_value", now))
    return entry


def _entry_state(view: layers.EntryView | None) -> dict[str, Any]:
    return {"source": "absent"} if view is None else {"source": view.source, **_with("value", view.value)}


def _ctx_fields(ctx: AdminContext) -> dict[str, Any]:
    return {
        "updated_by": ctx.subject, "actor_kind": ctx.actor_kind, "actor_id": ctx.audit_actor_id,
        "actor_label": ctx.actor_label, "session": ctx.browser_session,
    }


def build_export(
    home: config_runtime.ConfigHome, path: Path, *, version: str, effective: bool = False,
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The owner layer as an ``azizam-config/v1`` document (design 6.1); ``effective`` is the inspection view instead.

    Overrides only: no release content, no secret value (a secret entry holds a reference, listed under ``secrets``),
    no ephemeral state. The effective view is for reading and is not importable.
    """
    state = _State(home, path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if effective:
        results = home.build_candidate(state.rows, registry)
        return {
            "format": layers.EXPORT_FORMAT, "view": "effective", "exported_at": now, "gateway_version": version,
            "sections": {name: results[name].effective for name in sorted(results)},
        }
    secrets: list[dict[str, str]] = []
    for name in sorted(state.rows):
        for override in state.rows[name]:
            rule = home.sections[name].index.rule_for(override.pointer)
            if rule is not None and rule.secret and secret_refs.is_ref(override.value):
                secrets.append({"ref": override.value, "used_by": f"{name}{override.pointer}"})
    return layers.export_document(
        state.rows, home.sections, exported_at=now, gateway_version=version, secrets=secrets,
    )


def create_config_router(
    *,
    auth: AdminAuth,
    admin_path: Path | Callable[[], Path],
    home: Callable[[], config_runtime.ConfigHome],
    registry: Callable[[], Mapping[str, Any] | None],
    reload: Callable[[], Any],
    startup_block: Callable[[str], Mapping[str, Any] | None],
) -> APIRouter:
    """The routes under ``/admin/api/config``. Every dependency is a callable, so a test swaps any of them.

    ``admin_path`` is the admin.db path, or a callable that answers it now (a test swaps the database).
    ``home`` answers the ``ConfigHome`` (the module's own, looked up per request), ``registry`` the service registry
    that cross-section rules see, ``reload`` the gateway's reload (publishes the snapshot), and ``startup_block`` the
    startup file's block for a section id, for the legacy import.
    """
    router = APIRouter(prefix=PREFIX, tags=["admin"])

    def db() -> Path:
        return Path(admin_path() if callable(admin_path) else admin_path)

    def guard(request: Request) -> AdminContext | JSONResponse:
        ctx = _require_admin(auth, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if "owner" not in ctx.roles:
            return _error(403, "owner_required", "Owner role required")
        return ctx

    async def read_body(request: Request, *, required: bool = True) -> dict[str, Any] | JSONResponse:
        raw = await request.body()
        if not raw.strip():
            return {} if not required else _error(400, "invalid_body", "Send a JSON object.")
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "invalid_body", "The body is not valid JSON.")
        if not isinstance(body, dict):
            return _error(400, "invalid_body", "The body must be a JSON object.")
        return body

    def unknown_section(section_id: str) -> JSONResponse:
        return _error(404, "unknown_section", f"This release ships no config section {section_id!r}.")

    def store_error(error: Exception) -> JSONResponse:
        if isinstance(error, admin_store.ConfigRevisionRequired):
            return _error(428, "revision_required", str(error), sections=list(error.sections))
        if isinstance(error, admin_store.ConfigRevisionMismatch):
            return _error(412, "revision_mismatch", str(error), section=error.section, current=error.current)
        if isinstance(error, admin_store.ConfigChangedSince):
            return _error(412, "changed_since", str(error), pointers=list(error.pointers))
        if isinstance(error, admin_store.ConfigChangeInvalid):
            return _error(422, "config_invalid", str(error), problems=error.problems)
        if isinstance(error, SessionEnded):
            return _error(401, error.code, error.message)
        if isinstance(error, LookupError):
            return _error(404, "not_found", str(error))
        if isinstance(error, ValueError):
            return _error(400, "invalid_change", str(error))
        raise error

    def parse_if_match(value: Any) -> dict[str, str] | JSONResponse:
        if value is None:
            return {}
        if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
            return _error(400, "invalid_body", "if_match must map a section id to its revision.")
        return {name: _revision_text(revision) for name, revision in value.items()}

    # -- the core of every write -------------------------------------------------------------------------------------

    def release_base(h: config_runtime.ConfigHome, section_id: str, pointer: str) -> str | None:
        value = layers.get_at(h.sections[section_id].defaults, pointer)
        return None if value is layers.ABSENT else _dump(value)

    def diff_of(
        h: config_runtime.ConfigHome, state: _State, results: Mapping[str, layers.SectionResult],
        captured: Mapping[str, Sequence[Mapping[str, Any]]], reg: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        after_rows = {**state.rows, **{
            name: [config_runtime.override_from_row(row) for row in rows] for name, rows in captured.items() if name in h.sections
        }}
        after = h.build_candidate(after_rows, reg)
        changes: list[dict[str, Any]] = []
        for name in sorted(captured):
            if name not in h.sections:
                continue
            section = h.sections[name]
            before_views = {v.pointer: v for v in layers.entry_views(section, results[name], state.rows[name])}
            after_views = {v.pointer: v for v in layers.entry_views(section, after[name], after_rows[name])}
            for pointer in sorted(before_views.keys() | after_views.keys()):
                was, now = before_views.get(pointer), after_views.get(pointer)
                if was is not None and now is not None and was.source == now.source and layers.same_value(was.value, now.value):
                    continue
                changes.append({"section": name, "pointer": pointer, "before": _entry_state(was), "after": _entry_state(now)})
        return changes

    def run_change(
        ctx: AdminContext, items: list[admin_store.ConfigItem], *, if_match: Mapping[str, str], kind: str, origin: str,
        note: str | None, dry_run: bool, revert_of: str | None = None,
    ) -> JSONResponse:
        """Judge the change, then commit it and reload, or (dry run) report what it would do and write nothing."""
        h = home()
        reg = registry()
        validate = h.write_validator(reg)
        state = _State(h, db())
        captured: dict[str, list[dict[str, Any]]] = {}

        def judge(conn: Any, rows_after: Mapping[str, list[dict[str, Any]]]) -> None:
            captured.update(rows_after)
            validate(conn, rows_after)
            if dry_run:
                raise _DryRunDone

        sections = sorted({item.section for item in items})
        if dry_run:  # a dry run is how a client learns the revisions, so it does not need them
            if_match = {**{name: state.revisions.get(name, "0") for name in sections}, **if_match}
        try:
            if revert_of is not None:
                result = admin_store.revert_config_change(
                    revert_of, if_match=if_match, note=note, validate=judge, path=db(), **_ctx_fields(ctx))
            else:
                result = admin_store.apply_config_change(
                    items, if_match=if_match, kind=kind, origin=origin, note=note, validate=judge, path=db(),
                    **_ctx_fields(ctx))
        except _DryRunDone:
            results = h.build_candidate(state.rows, reg)
            return _ok({"dry_run": True, "valid": True, "problems": [], "revisions": if_match,
                        "diff": diff_of(h, state, results, captured, reg)})
        except admin_store.ConfigChangeInvalid as error:
            if not dry_run:
                return store_error(error)
            results = h.build_candidate(state.rows, reg)
            return _ok({"dry_run": True, "valid": False, "problems": error.problems, "revisions": if_match,
                        "diff": diff_of(h, state, results, captured, reg)})
        except (admin_store.ConfigRevisionRequired, admin_store.ConfigRevisionMismatch, admin_store.ConfigChangedSince,
                SessionEnded, LookupError, ValueError) as error:
            return store_error(error)
        if dry_run:  # nothing changed, so the judge never ran
            return _ok({"dry_run": True, "valid": True, "problems": [], "revisions": if_match, "diff": []})
        if result.get("change_id") is None:
            return _ok({**result, "reload": {"status": "skipped"}})
        entries = sorted({(item.section, item.pointer) for item in items}) if revert_of is None else []
        h.note_commit(result["change_id"], entries)
        try:
            reload()
        except Exception as error:  # noqa: BLE001 - the change is committed; the old snapshot keeps serving and says so
            failed = h.reload_failed
            if failed is None or failed.get("change_id") != result["change_id"]:
                failed = h.note_reload_failed(f"{type(error).__name__}: {error}", path=db())
            return _ok({**result, "reload": {"status": "failed", "error": failed["error"]}}, status=202)
        return _ok({**result, "reload": {"status": "ok"}})

    def build_items(
        h: config_runtime.ConfigHome, raw_items: Any,
    ) -> list[admin_store.ConfigItem] | JSONResponse:
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > MAX_ITEMS:
            return _error(400, "invalid_body", f"items must be a list of 1 to {MAX_ITEMS} entries.")
        items: list[admin_store.ConfigItem] = []
        problems: list[dict[str, Any]] = []
        for position, raw in enumerate(raw_items):
            where = f"items[{position}]"
            if not isinstance(raw, dict) or not isinstance(raw.get("section"), str) or not isinstance(raw.get("pointer"), str):
                return _error(400, "invalid_body", f"{where} needs a section and a pointer.")
            op = raw.get("op")
            if op not in ("set", "delete", "reset"):
                return _error(400, "invalid_body", f"{where}: op must be set, delete or reset.")
            if op == "set" and "value" not in raw:
                return _error(400, "invalid_body", f"{where}: a set needs a value (null is a value).")
            name, pointer = raw["section"], raw["pointer"]
            if name not in h.sections:
                problems.append({"section": name, "pointer": pointer, "code": "unknown_section",
                                 "message": f"this release ships no section {name!r}"})
                continue
            if op != "reset":
                faults = layers.check_override(h.sections[name], layers.Override(pointer, op, raw.get("value")))
                problems += [{"section": name, **fault.as_dict()} for fault in faults]
                if faults:
                    continue
            items.append(admin_store.ConfigItem(
                name, pointer, op, _dump(raw["value"]) if op == "set" else None,
                release_base(h, name, pointer) if op != "reset" else None,
            ))
        if problems:
            return _error(422, "config_invalid", f"{len(problems)} problem(s) in the change", problems=problems)
        return items

    # -- reads -------------------------------------------------------------------------------------------------------

    @router.get("")
    async def list_sections(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx

        def build() -> dict[str, Any]:
            h = home()
            state = _State(h, db())
            results = h.build_candidate(state.rows, registry())
            return {"sections": [{
                "id": name, "title": _title(h.sections[name]), "revision": state.revisions[name],
                "overrides": len(state.rows[name]), "problems": len(results[name].problems),
                "drift": len(layers.drift_report(h.sections[name], state.rows[name])),
            } for name in sorted(h.sections)]}

        return _ok(await asyncio.to_thread(build))

    @router.get("/sections/{section_id}")
    async def get_section(section_id: str, request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx

        def build() -> JSONResponse:
            h = home()
            if section_id not in h.sections:
                return unknown_section(section_id)
            state = _State(h, db())
            result = h.build_candidate(state.rows, registry())[section_id]
            snapshot = h.holder.current()
            running = snapshot.config.get(section_id) if snapshot is not None else None
            views = layers.entry_views(h.sections[section_id], result, state.rows[section_id])
            failed = h.reload_failed
            if failed is not None and not any(entry["section"] == section_id for entry in failed["entries"]):
                failed = None
            revision = state.revisions[section_id]
            return _ok({
                "section": section_id, "title": _title(h.sections[section_id]), "revision": revision,
                "effective": result.effective, "entries": [_entry_dict(view, running) for view in views],
                "problems": [problem.as_dict() for problem in result.problems], "reload_failed": failed,
            }, ETag=f'"{revision}"')

        return await asyncio.to_thread(build)

    @router.get("/changes")
    async def list_changes(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        raw_limit = request.query_params.get("limit", "50")
        if not raw_limit.isdecimal():
            return _error(400, "invalid_limit", "limit must be a whole number.")
        limit = max(1, min(int(raw_limit), 500))
        try:
            found = await asyncio.to_thread(
                admin_store.list_config_changes, limit, request.query_params.get("before"), path=db())
        except LookupError as error:
            return _error(404, "not_found", str(error))
        return _ok({"changes": found, "next": found[-1]["change_id"] if len(found) == limit else None})

    @router.get("/drift")
    async def get_drift(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx

        def build() -> dict[str, Any]:
            h = home()
            state = _State(h, db())
            return {"drift": {
                name: [_drift_dict(item) for item in layers.drift_report(h.sections[name], state.rows[name])]
                for name in sorted(h.sections) if layers.drift_report(h.sections[name], state.rows[name])
            }}

        return _ok(await asyncio.to_thread(build))

    @router.get("/problems")
    async def get_problems(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return _ok(home().view())

    # -- writes ------------------------------------------------------------------------------------------------------

    @router.patch("/changes")
    async def patch_changes(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        body = await read_body(request)
        if isinstance(body, JSONResponse):
            return body
        if_match = parse_if_match(body.get("if_match"))
        if isinstance(if_match, JSONResponse):
            return if_match
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            return _error(400, "invalid_body", "note must be text.")
        items = build_items(home(), body.get("items"))
        if isinstance(items, JSONResponse):
            return items
        dry_run = request.query_params.get("dry_run") in ("1", "true")
        return await asyncio.to_thread(
            run_change, ctx, items, if_match=if_match, kind="edit", origin="api", note=note, dry_run=dry_run)

    @router.delete("/sections/{section_id}/entries")
    async def reset_entry(section_id: str, request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if section_id not in home().sections:
            return unknown_section(section_id)
        pointer = request.query_params.get("pointer")
        if not pointer:
            return _error(400, "invalid_pointer", "Send ?pointer=<JSON Pointer of the entry>.")
        revision = request.headers.get("if-match")
        if not revision:
            return _error(428, "revision_required", f"Send If-Match with the revision of {section_id}.", sections=[section_id])
        return await asyncio.to_thread(
            run_change, ctx, [admin_store.ConfigItem(section_id, pointer, "reset")],
            if_match={section_id: _revision_text(revision)}, kind="reset", origin="api", note=None, dry_run=False)

    @router.post("/changes/{change_id}/revert")
    async def revert_change(change_id: str, request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        body = await read_body(request, required=False)
        if isinstance(body, JSONResponse):
            return body
        if_match = parse_if_match(body.get("if_match"))
        if isinstance(if_match, JSONResponse):
            return if_match
        return await asyncio.to_thread(
            run_change, ctx, [], if_match=if_match, kind="revert", origin="api", note=body.get("note"), dry_run=False,
            revert_of=change_id)

    # -- import ------------------------------------------------------------------------------------------------------

    def import_items(
        h: config_runtime.ConfigHome, state: _State, document: Any, mode: str,
    ) -> tuple[list[admin_store.ConfigItem], dict[str, Any]] | JSONResponse:
        try:
            wanted = layers.parse_export(document)
        except layers.ExportFormatError as error:
            return _error(422, "bad_export", str(error))
        skipped = sorted(name for name in wanted if name not in h.sections)
        items: list[admin_store.ConfigItem] = []
        problems: list[dict[str, Any]] = []
        needs_secret: list[dict[str, str]] = []
        names = sorted(h.sections) if mode == "replace" else sorted(name for name in wanted if name in h.sections)
        for name in names:
            section, existing = h.sections[name], {o.pointer: o for o in state.rows[name]}
            file_rows = {o.pointer: o for o in wanted.get(name, [])}
            if len(file_rows) != len(wanted.get(name, [])):
                return _error(422, "bad_export", f"sections.{name} names a pointer twice")
            for pointer in sorted(existing.keys() - file_rows.keys() if mode == "replace" else ()):
                items.append(admin_store.ConfigItem(name, pointer, "reset"))
            for pointer, row in sorted(file_rows.items()):
                faults = layers.check_override(section, row)
                problems += [{"section": name, **fault.as_dict()} for fault in faults]
                if faults:
                    continue
                have = existing.get(pointer)
                if have is not None and have.op == row.op and layers.same_value(have.value, row.value):
                    continue
                if row.op == layers.OP_SET:
                    rule = section.index.rule_for(pointer)
                    if rule is not None and rule.secret and secret_refs.is_ref(row.value) and not secret_refs.configured(
                            row.value, db_path=db()):
                        needs_secret.append({"section": name, "pointer": pointer, "code": "needs_secret"})
                items.append(admin_store.ConfigItem(
                    name, pointer, row.op, _dump(row.value) if row.op == layers.OP_SET else None,
                    None if row.base is layers.ABSENT else _dump(row.base),
                ))
        if problems:
            return _error(422, "config_invalid", f"{len(problems)} problem(s) in the import", problems=problems)
        return items, {"skipped_sections": skipped, "needs_secret": needs_secret}

    @router.post("/import")
    async def import_config(request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        body = await read_body(request)
        if isinstance(body, JSONResponse):
            return body
        mode = body.get("mode", "replace")
        if mode not in MODES:
            return _error(400, "invalid_body", f"mode must be one of {', '.join(MODES)}.")
        if_match = parse_if_match(body.get("if_match"))
        if isinstance(if_match, JSONResponse):
            return if_match
        dry_run = request.query_params.get("dry_run") in ("1", "true")

        def build() -> JSONResponse:
            h = home()
            planned = import_items(h, _State(h, db()), body.get("document"), mode)
            if isinstance(planned, JSONResponse):
                return planned
            items, report = planned
            if not items:
                return _ok({"dry_run": dry_run, "change_id": None, "entry_count": 0, "diff": [], "valid": True,
                            "problems": [], "revisions": {}, **report})
            response = run_change(ctx, items, if_match=if_match, kind="import", origin="import",
                                  note=body.get("note"), dry_run=dry_run)
            payload = json.loads(response.body)
            if "error" in payload:
                return response
            return _ok({**payload, **report}, status=response.status_code)

        return await asyncio.to_thread(build)

    @router.post("/sections/{section_id}/legacy-import")
    async def legacy_import(section_id: str, request: Request):
        ctx = guard(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        body = await read_body(request, required=False)
        if isinstance(body, JSONResponse):
            return body
        dry_run = request.query_params.get("dry_run") in ("1", "true")
        revision = request.headers.get("if-match")

        def build() -> JSONResponse:
            h = home()
            if section_id not in h.sections:
                return unknown_section(section_id)
            block = startup_block(section_id)
            if block is None:
                return _error(409, "no_startup_block", f"The startup file has no block for {section_id!r}.")
            reg = registry()
            state = _State(h, db())
            section = h.sections[section_id]
            context = {"registry": reg, "sections": {
                name: result.effective for name, result in h.build_candidate(state.rows, reg).items()}}
            overrides, blocked = layers.overrides_for_target(section, block, context)
            if blocked:
                found = [{"section": section_id, **{**p.as_dict(), "code": "legacy_import_blocked", "reason": p.code}}
                         for p in blocked]
                if dry_run:
                    return _ok({"dry_run": True, "applicable": False, "blocked": found})
                return _error(422, "legacy_import_blocked", "The startup block does not fit the section.", problems=found)
            existing = {o.pointer: o for o in state.rows[section_id]}
            items: list[admin_store.ConfigItem] = []
            for override in overrides:
                have = existing.get(override.pointer)
                if have is not None and have.op == override.op and layers.same_value(have.value, override.value):
                    continue
                items.append(admin_store.ConfigItem(
                    section_id, override.pointer, override.op,
                    _dump(override.value) if override.op == layers.OP_SET else None,
                    None if override.base is layers.ABSENT else _dump(override.base),
                ))
            kept = {override.pointer for override in overrides}
            resets = sorted(existing.keys() - kept)
            items += [admin_store.ConfigItem(section_id, pointer, "reset") for pointer in resets]
            report = {
                "section": section_id,
                "rows": [{"pointer": o.pointer, "op": o.op, **({"file_value": o.value} if o.op == layers.OP_SET else {}),
                          **_with("release_value", o.base)} for o in overrides if o.op == layers.OP_SET],
                "removes": [{"pointer": o.pointer, **_with("release_value", o.base)} for o in overrides if o.op == layers.OP_DELETE],
                "resets": resets,
            }
            if not items:
                return _ok({**report, "dry_run": dry_run, "applicable": True, "change_id": None, "entry_count": 0,
                            "diff": [], "valid": True, "problems": []})
            if not dry_run and not revision:
                return _error(428, "revision_required", f"Send If-Match with the revision of {section_id}.", sections=[section_id])
            response = run_change(
                ctx, items, if_match={section_id: _revision_text(revision)} if revision else {}, kind="legacy-import",
                origin="legacy-import", note=body.get("note"), dry_run=dry_run)
            payload = json.loads(response.body)
            if "error" in payload:
                return response
            return _ok({**report, "applicable": True, **payload}, status=response.status_code)

        return await asyncio.to_thread(build)

    return router
