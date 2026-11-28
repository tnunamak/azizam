"""Installed protocol choices for the engine editor, using the dispatch loader."""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import egress, protocol_description_store as store
from gateway.session_guard import BrowserSession, SessionEnded
from gateway.engine_admin import _is_owner
from gateway.engine_protocol import description

_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_MAX_BYTES = 256 * 1024
_REVISION = re.compile(r'"pd-(\d{1,18})"')


def installed_protocols() -> dict[str, Any]:
    """Expose validation metadata, never source contents or parser error values."""
    loaded = description.load_registry()
    user_root = description.user_directory()
    owned = description.owner_descriptions()[0]
    protocols = []
    for protocol_id, doc in sorted(loaded.items()):
        count = len(doc.operations)
        protocols.append({
            "id": protocol_id,
            "title": doc.title or protocol_id,
            "executor": doc.executor,
            "operations": sorted(doc.operations),
            "source": "user" if protocol_id in owned or (user_root / f"{protocol_id}.yml").is_file() else "seed",
            "dispatch_supported": doc.executor == "http-v1",
            "validation": {"valid": True, "message": f"Valid description. {count} operation{'s' if count != 1 else ''}."},
        })
    return {"protocols": protocols, "problems": description.registry_problems()}


def _tag(revision: int) -> str:
    return f'"pd-{revision}"'


def _shipped_text(protocol_id: str) -> str | None:
    path = description.REGISTRY_DIR / f"{protocol_id}.yml"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _item(protocol_id: str, text: str, source: str, revision: str, *, status_code: int = 200) -> JSONResponse:
    return egress.plain_json_response({"id": protocol_id, "source": source, "text": text}, response_class="engine_protocol",
                                      status_code=status_code, headers={"ETag": revision, "Cache-Control": "no-store"})


def _refuse(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return egress.safe_error_response(status_code, code, message, extra=extra)


def _precondition(request: Request) -> tuple[str, int | None] | JSONResponse:
    """``("create", None)``, ``("match", revision)`` or an error. A weak or unknown tag matches no revision (412)."""
    if_match, if_none = request.headers.get("if-match"), request.headers.get("if-none-match")
    if if_match is not None and if_none is not None:
        return _refuse(400, "ambiguous_precondition", "Send If-Match or If-None-Match, not both.")
    if if_none is not None:
        if if_none.strip() != "*":
            return _refuse(400, "invalid_precondition", "If-None-Match must be *.")
        return "create", None
    if if_match is None:
        return _refuse(428, "protocol_revision_required", "Send If-None-Match: * to create, or If-Match with the current ETag.")
    found = _REVISION.fullmatch(if_match.strip())
    return ("match", int(found.group(1))) if found else ("match", -1)


# A parser problem is free text: it can quote the posted document (a key, a pointer, a value, a tag). A refusal therefore
# carries only a stable code per problem, picked by what the problem is about, never the problem text.
_PROBLEM_CODES = (
    (re.compile(r"^YAML aliases"), "yaml_alias"),
    (re.compile(r"^YAML at "), "yaml_syntax"),
    (re.compile(r"the file must hold one mapping"), "not_a_mapping"),
    (re.compile(r"names code; descriptions are data only"), "code_field"),
    (re.compile(r"keys must be strings"), "non_string_key"),
    (re.compile(r"unknown reviewed implementation"), "unknown_implementation"),
    (re.compile(r"pointer"), "bad_pointer"),
    (re.compile(r"credential-like header|set by the transport|not a valid header name"), "bad_header"),
    (re.compile(r"path .*(relative|segments)|placeholder"), "bad_path"),
)


def _problem_codes(problems: list[str]) -> str:
    found = {next((code for pattern, code in _PROBLEM_CODES if pattern.search(problem)), "invalid_value") for problem in problems}
    return ",".join(sorted(found))


def _validate(protocol_id: str, text: str) -> JSONResponse | description.ProtocolDescription:
    try:
        parsed = description.parse_description_text(text, source=protocol_id)
    except description.DescriptionError as error:
        return _refuse(422, error.code, "The description is not valid. Check it with the validator and send it again.",
                       problem_codes=_problem_codes(error.problems), problem_count=len(error.problems))
    if parsed.id != protocol_id:
        return _refuse(422, "id_mismatch", "The id inside the description must equal the id in the address.")
    return parsed


def _unavailable() -> JSONResponse:
    return _refuse(503, "protocol_store_unavailable", "The stored protocol descriptions cannot be read now. Try again.")


def _write(protocol_id: str, text: str, kind: str, expect: int | None, owner: str | None, declares: Any,
           session: BrowserSession | None = None) -> JSONResponse:
    try:
        revision = store.put(protocol_id, text, expect=expect, updated_by=owner, declares=declares, session=session)
    except SessionEnded as ended:
        return _refuse(ended.status_code, ended.code, ended.message)
    except store.StoreConflict as conflict:
        if conflict.kind == "operations":
            return _refuse(409, "protocol_in_use", "Engines still offer tasks this description would no longer declare: "
                           f"{conflict.detail}. Change them first.")
        return _refuse(412, "revision_mismatch", "The description changed or already exists. Read it again." if conflict.kind != "missing"
                       else "There is no description with this id to replace.")
    except sqlite3.Error:
        return _unavailable()
    return _item(protocol_id, text, "user", _tag(revision), status_code=201 if kind == "create" else 200)


def _remove(protocol_id: str, expect: int, session: BrowserSession | None = None) -> JSONResponse:
    try:
        store.delete(protocol_id, expect=expect, session=session)
    except SessionEnded as ended:
        return _refuse(ended.status_code, ended.code, ended.message)
    except store.StoreConflict as conflict:
        if conflict.kind == "referenced":
            return _refuse(409, "protocol_in_use", f"Engines still use this description: {conflict.detail}. Change them first.")
        return _refuse(412, "revision_mismatch", "The description changed or is gone. Read it again.")
    except sqlite3.Error:
        return _unavailable()
    return egress.plain_json_response({"deleted": True}, response_class="acknowledgement")


def register_engine_protocol_routes(router: APIRouter, *, guard: Callable[[Request], Any]) -> None:
    """The routes, the listing and the dispatch loader all read the one store ``protocol_description_store.db_path()`` names.

    An owner row wins over a shipped file of the same id (the loader's rule). A shipped id refuses every PUT; DELETE
    removes an existing owner row even when a later release ships the id, so the owner is never stuck behind it."""
    @router.get("/engine-protocols")
    async def engine_protocols(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        return egress.plain_json_response(await asyncio.to_thread(installed_protocols), response_class="engine_protocols")


    @router.get("/engine-protocols/{protocol_id}")
    async def engine_protocol(protocol_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            row = await asyncio.to_thread(store.get, protocol_id) if _ID.fullmatch(protocol_id) else None
        except sqlite3.Error:
            return _unavailable()
        if row is not None:
            return _item(protocol_id, row.text, "user", _tag(row.revision))
        shipped = _shipped_text(protocol_id) if _ID.fullmatch(protocol_id) else None
        if shipped is None:
            return _refuse(404, "protocol_not_found", "There is no protocol description with this id.")
        return _item(protocol_id, shipped, "seed", f'"seed-{hashlib.sha256(shipped.encode()).hexdigest()[:16]}"')

    @router.put("/engine-protocols/{protocol_id}")
    async def put_engine_protocol(protocol_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if not _is_owner(ctx):
            return _refuse(403, "owner_required", "Only the owner can install a protocol description.")
        if not _ID.fullmatch(protocol_id):
            return _refuse(422, "invalid_protocol_id", "A description id is lower case letters, digits and hyphens, starting with a letter.")
        if _shipped_text(protocol_id) is not None:
            return _refuse(409, "protocol_shipped", "This description ships with the release and cannot be changed. Use another id.")
        condition = _precondition(request)
        if isinstance(condition, JSONResponse):
            return condition
        raw = await request.body()
        if len(raw) > _MAX_BYTES:
            return _refuse(422, "description_too_large", "The description is too large.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _refuse(422, "invalid_description", "The description must be UTF-8 text.")
        parsed = await asyncio.to_thread(_validate, protocol_id, text)
        if isinstance(parsed, JSONResponse):
            return parsed
        kind, expect = condition
        return await asyncio.to_thread(_write, protocol_id, text, kind, expect, getattr(ctx, "audit_actor_id", None), set(parsed.operations),
                                     getattr(ctx, "browser_session", None))

    @router.delete("/engine-protocols/{protocol_id}")
    async def delete_engine_protocol(protocol_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        if not _is_owner(ctx):
            return _refuse(403, "owner_required", "Only the owner can remove a protocol description.")
        if not _ID.fullmatch(protocol_id):
            return _refuse(404, "protocol_not_found", "There is no protocol description with this id.")
        if _shipped_text(protocol_id) is not None:
            # A shipped id has nothing to remove, unless the owner installed a row under it before the release shipped it.
            try:
                owner_row = await asyncio.to_thread(store.get, protocol_id)
            except sqlite3.Error:
                return _unavailable()
            if owner_row is None:
                return _refuse(409, "protocol_shipped", "This description ships with the release and cannot be removed.")
        condition = _precondition(request)
        if isinstance(condition, JSONResponse):
            return condition
        if condition[0] != "match":
            return _refuse(428, "protocol_revision_required", "Send If-Match with the current ETag.")
        return await asyncio.to_thread(_remove, protocol_id, condition[1], getattr(ctx, "browser_session", None))
