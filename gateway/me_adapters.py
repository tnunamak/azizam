"""A member's adapter upload: ``POST /me/api/adapters``.

The file is checked before it is stored (``gateway/adapter_upload.py``): safetensors only, by its own header, within the
owner's size cap, and only while the owner's setting is on and the engine runs on this computer. The refusals that need
no body (setting off, engine elsewhere) come before the body is read, and so does a Content-Length past the cap. The
global body limit (``request_body_limits``) follows the owner's current cap and stops a body that passes it chunk by
chunk, so the multipart parser never spools more than the cap plus framing. A person's uploads together stay within the
owner's per-person total. A stored upload is a Library item of kind ``adapter`` that belongs to the signed-in person;
the Library's own Delete removes it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import adapter_upload, request_forms
from gateway.admin_oidc import AdminSession

SessionProvider = Callable[[Request], AdminSession | None]
MutationValidator = Callable[[Request, AdminSession], JSONResponse | None]
Store = Callable[[BinaryIO, adapter_upload.Validated, str, str], str]
UsedBytes = Callable[..., int]       # (person_id, *, excluding=asset_id): what the person's other uploads take

NOT_OFFERED_NOTE = ("Stored in your Library, but its header names no base model this gateway knows, so it is not offered "
                    "for any workflow.")


def _error(code: str, status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_me_adapters_router(
    *,
    session_provider: SessionProvider,
    rules: Callable[[], adapter_upload.Policy],
    engine_local: Callable[[], bool],
    store: Store,
    used_bytes: UsedBytes,
    mutation_validator: MutationValidator | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/me/api", tags=["me"])
    storing = threading.Lock()       # the person's total is read and then stored under this, so two uploads cannot both fit

    @router.post("/adapters", include_in_schema=False)
    async def upload_adapter(request: Request) -> JSONResponse:
        session = await asyncio.to_thread(session_provider, request)
        if session is None:
            return _error("unauthorized", 401, "Sign in required")
        if mutation_validator is not None:
            refused = mutation_validator(request, session)
            if refused is not None:
                return refused
        policy = rules()
        try:
            # Before the body is read: a setting that is off, or an engine on another computer, needs no file to refuse.
            adapter_upload.admit(policy, size=1, engine_local=engine_local())
        except adapter_upload.Refused as exc:
            return _error(exc.code, exc.status, exc.message)
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > policy.max_bytes + adapter_upload.FRAMING_BYTES:
            return _error("adapter_too_large", 413, f"The file is larger than the {policy.max_bytes}-byte limit for adapters.")
        try:
            form = await request_forms.read_form(request)
        except Exception:  # noqa: BLE001 - a body that is not multipart is the caller's mistake
            return _error("missing_file", 400, "Send the adapter as one multipart/form-data file part named 'file'.")
        part = form.get("file")
        if part is None or not hasattr(part, "read") or not hasattr(part, "file"):
            return _error("missing_file", 400, "Send the adapter as one multipart/form-data file part named 'file'.")
        label = adapter_upload.label_of(getattr(part, "filename", None))

        def check_and_store() -> JSONResponse:
            stream: BinaryIO = part.file
            stream.seek(0, 2)
            size = stream.tell()
            try:
                adapter_upload.admit(policy, size=size, engine_local=True)
                checked = adapter_upload.validate_stream(stream, size, policy)
            except adapter_upload.Refused as exc:
                return _error(exc.code, exc.status, exc.message)
            found = adapter_upload.architectures(checked.header.metadata, adapter_upload.architecture_table())
            would_be = f"ast_{adapter_upload.asset_digest(session.subject, checked.digest)}"
            with storing:
                try:
                    adapter_upload.admit_total(policy, used=used_bytes(session.subject, excluding=would_be), size=checked.size)
                except adapter_upload.Refused as exc:
                    return _error(exc.code, exc.status, exc.message)
                asset_id = store(stream, checked, session.subject, label)
            body: dict[str, Any] = {
                "id": asset_id, "object": "gateway.asset", "kind": "adapter", "media_type": "adapter/safetensors",
                "size_bytes": checked.size, "architectures": list(found), "offered": bool(found),
            }
            if not found:
                body["note"] = NOT_OFFERED_NOTE
            return JSONResponse(body)

        try:
            return await asyncio.to_thread(check_and_store)
        finally:
            await form.close()

    return router
