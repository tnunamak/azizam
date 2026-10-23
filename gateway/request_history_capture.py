"""Request history capture: one decision and at most one write per captured request (REQUEST_RETENTION.md 3.3-3.6).

A pure ASGI middleware, so every data-plane route passes through it once. It reads the body as it streams in and the
reply as it streams out, and it waits for the request's end. The handler authenticates the caller and leaves the
identity in the request state (``proxy.authenticate_request``), so the decision, the write and the receipt's
retention entry are made then. A request that never authenticates is not recorded.

The per-request control is read here, for every data-plane route: ``extensions["com.azizam.prompts"]`` in a JSON or
multipart body, and ``X-Azizam-Prompts: forget`` for a body the gateway does not parse. The header is removed before
the handler runs. A forget keeps nothing but the receipt's structural entry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

from gateway import receipt_store, request_history, request_history_slot, retention

log = logging.getLogger("gateway.request_history")

KIND = retention.REQUEST_HISTORY
#: Where a request reaches the capture: the data plane. Maker routes and the gateway's own security operations
#: (sign-in, keys, grants) are outside these prefixes, so they never hold content (section 3.5, rule 1).
DATA_PLANE_PREFIXES = (
    "/v1/", "/openai/v1/", "/anthropic/v1/", "/ollama/api/", "/api/", "/rerank", "/v2/rerank", "/typesafe/v1/",
    "/gateway/v1/media/jobs", "/gateway/v1/workflows/", "/gateway/v1/images/", "/gateway/v1/faces/",
)
CAPTURED_METHODS = frozenset({"POST", "PUT", "PATCH"})
PROMPTS_HEADER = "x-azizam-prompts"
#: proxy.py's handoff header for an in-process protocol loopback. The outer request is the one the caller made.
INTERNAL_HEADER = "x-azizam-internal-identity"
#: The request state key the handler's authentication writes its AuthContext to.
IDENTITY_STATE_KEY = "request_history_identity"
#: How long the capture waits for the rest of a request body that the route did not read. A client that has sent its
#: body finishes at once; a client that stalls leaves the record not kept.
BODY_DRAIN_SECONDS = 30.0


def is_captured(method: str, path: str) -> bool:
    return method in CAPTURED_METHODS and path.startswith(DATA_PLANE_PREFIXES)


def _header_map(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in raw}


def receipt_entry(outcome: retention.WriteOutcome, parts: list[str]) -> dict[str, Any]:
    """The receipt's retention entry for this write (13.3). ``failed`` only when the write itself raised."""
    if outcome.kept:
        return {"kind": KIND, "decision": "kept", "reason": "kept",
                "kept_until_ms": None if outcome.kept_until is None else int(outcome.kept_until) * 1000,
                "parts": list(parts), "limited_by": outcome.limited_by or "unbounded_store"}
    if outcome.reason == "write_failed":
        return {"kind": KIND, "decision": "failed", "reason": "write_failed"}
    return {"kind": KIND, "decision": "not_kept", "reason": outcome.reason}


def _control_refusal(raw: bytes) -> JSONResponse | None:
    """Refuse an invalid per-request control before the route runs, as the media and Responses routes do (3.4)."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or "extensions" not in parsed:
        return None
    try:
        retention.requested_mode(parsed)
    except retention.PromptModeError as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error",
                                       "code": "invalid_prompt_mode",
                                       "param": f"extensions.{retention.NAMESPACE}.mode"}}, status_code=400)
    return None


def _forget_requested(body: request_history.BodyCapture) -> bool:
    """A forget in the body. An invalid control block counts as a forget: nothing is kept on a doubtful request."""
    source = body.control_source
    if not isinstance(source, dict):
        return False
    if body.media_type == "multipart/form-data":
        return source.get(f"extensions[{retention.NAMESPACE}][mode]") == "forget"
    try:
        return retention.requested_mode(source) == "forget"
    except retention.PromptModeError:
        return True


class RequestHistoryMiddleware:
    """Captures one request's history. ``admin_path`` and ``media_path`` are read at each request."""

    def __init__(self, app: Any, *, admin_path: Callable[[], Path], media_path: Callable[[], Path | None]) -> None:
        self.app = app
        self._admin_path = admin_path
        self._media_path = media_path

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not is_captured(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = _header_map(scope.get("headers", []))
        if INTERNAL_HEADER in headers:
            await self.app(scope, receive, send)
            return
        prompts_mode = headers.get(PROMPTS_HEADER)
        if prompts_mode is not None and prompts_mode != "forget":
            response = JSONResponse({"error": {
                "message": "X-Azizam-Prompts must be forget", "type": "invalid_request_error",
                "code": "invalid_prompt_mode", "param": PROMPTS_HEADER}}, status_code=400)
            await response(scope, receive, send)
            return
        header_forget = prompts_mode == "forget"
        scope = {**scope, "headers": [(key, value) for key, value in scope.get("headers", [])
                                      if key.decode("latin-1").lower() != PROMPTS_HEADER]}
        state = scope.setdefault("state", {})
        state.pop(IDENTITY_STATE_KEY, None)
        limit = int(retention.bundled().data["kinds"][KIND]["max_record_bytes"])
        body = request_history.BodyCapture(headers.get("content-type"), limit=limit)
        reply_state: dict[str, Any] = {"status": None, "headers": {}, "body": request_history.BodyCapture(None, limit=limit)}
        started = int(time.time())
        began = time.monotonic()
        slot_token = request_history_slot.open_slot(scope["path"])
        read = {"complete": False}

        if _is_small_json(headers, limit):
            # Read a known-size JSON body first, so an invalid control is refused before any route runs.
            chunks = await _read_all(receive)
            raw = b"".join(chunks)
            refusal = _control_refusal(raw)
            if refusal is not None:
                request_history_slot.close_slot(slot_token)
                await refusal(scope, receive, send)
                return
            receive = _replay(chunks, receive)  # the wrapper below feeds each replayed chunk, once

        async def receive_wrapper() -> dict:
            message = await receive()
            if message["type"] == "http.request":
                if not header_forget:
                    _quietly(body.feed, message.get("body", b""))
                read["complete"] = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                read["complete"] = True
            return message

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                reply_state["status"] = int(message["status"])
                reply_state["headers"] = _header_map(message.get("headers", []))
                reply_state["body"] = request_history.BodyCapture(
                    reply_state["headers"].get("content-type"), limit=limit)
            elif message["type"] == "http.response.body" and not header_forget:
                _quietly(reply_state["body"].feed, message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            authenticated = getattr(state.get(IDENTITY_STATE_KEY), "identity", None) is not None
            if not read["complete"] and not header_forget and authenticated:
                # Only an authenticated request can produce a record, so an unauthenticated one is never read on.
                await _drain(receive_wrapper, read)
            slot = request_history_slot.current()
            receipt_id = slot.receipt_id if slot is not None else None
            request_history_slot.close_slot(slot_token)
            await _settle(self._finish, state=state, headers=headers, header_forget=header_forget, body=body,
                          body_complete=read["complete"], reply=reply_state["body"], reply_state=reply_state,
                          started=started, latency_ms=int((time.monotonic() - began) * 1000), route=scope["path"],
                          receipt_id=receipt_id)

    def _finish(self, *, state: dict, headers: dict[str, str], header_forget: bool,
                body: request_history.BodyCapture, body_complete: bool, reply: request_history.BodyCapture,
                reply_state: dict[str, Any], started: int, latency_ms: int, route: str,
                receipt_id: str | None) -> None:
        """Decide, write and record the outcome on the receipt. Runs in a worker thread; never raises."""
        auth = state.get(IDENTITY_STATE_KEY)
        identity = getattr(auth, "identity", None)
        if identity is None:
            return
        person_id = getattr(identity, "person_id", None)
        admin_path = self._admin_path()
        body.result()  # reads the body's control values; the parts are built from the same memo below
        forget = header_forget or _forget_requested(body)
        if not body_complete and not header_forget:
            # The control could sit in the part that never arrived, so an unread body is not kept.
            if receipt_id:
                _record_entry(receipt_id, retention.WriteOutcome(False, "incomplete"), [])
            return
        member = retention.member_choice(admin_path, person_id, retention.GROUP_REQUEST_HISTORY) if person_id else None
        control = {"extensions": {retention.NAMESPACE: {"mode": "forget"}}} if forget else None
        decision = retention.decide(KIND, person_id=person_id, member=member, household_policy=None,
                                    request_body=control, now=started)
        parts: dict[str, Any] = {}
        omissions: set[str] = set()
        accepted_body: Any = None
        if decision.keep:
            if "accepted" in decision.parts:
                part, found = request_history.capture_request(headers, body)
                parts["accepted"], accepted_body = part, part["body"]
                omissions.update(found)
            if "received" in decision.parts and reply_state["status"] is not None:
                part, found = request_history.capture_reply(reply_state["status"], reply_state["headers"], reply)
                parts["received"] = part
                omissions.update(found)
        summary = request_history.summarize(route, accepted_body, reply_state["status"], latency_ms)
        record = {
            "record_id": request_history.new_record_id(), "request_id": receipt_id, "person_id": person_id,
            "app_id": getattr(identity, "app_id", None), "origin_kind": getattr(identity, "auth_kind", "app_key"),
            "origin_id": _origin_id(identity), "created_at": started, "parts": parts,
            "omissions": sorted(omissions), "policy_version": int(retention.bundled().data["format_version"]),
            "summary": summary,
        }
        outcome = retention.WriteOutcome(False, "write_failed")
        media_path = self._media_path()

        def commit(deadline: int | None) -> None:
            request_history.insert({**record, "kept_until": deadline}, path=media_path)

        def remember(result: retention.WriteOutcome) -> None:
            nonlocal outcome
            outcome = result

        try:
            retention.write(KIND, admin_path=admin_path, person_id=person_id, created_at=started, commit=commit,
                            decision=decision, on_result=remember)
        except Exception:
            log.warning("request history write failed request_id=%s", receipt_id or "-", exc_info=False)
            outcome = retention.WriteOutcome(False, "write_failed")
        if receipt_id:
            _record_entry(receipt_id, outcome, list(decision.parts))


async def _drain(receive_wrapper: Callable, read: dict) -> None:
    """Read the rest of a body the route did not read, so the capture sees all of it. Bounded by BODY_DRAIN_SECONDS."""
    async def rest() -> None:
        while not read["complete"]:
            await receive_wrapper()
    try:
        await asyncio.wait_for(rest(), BODY_DRAIN_SECONDS)
    except (asyncio.TimeoutError, Exception):
        log.warning("request history body was not read to its end", exc_info=False)


def _record_entry(receipt_id: str, outcome: retention.WriteOutcome, parts: list[str]) -> None:
    try:
        receipt_store.update_retention(receipt_id, receipt_entry(outcome, parts))
    except Exception:
        log.warning("request history receipt update failed request_id=%s", receipt_id, exc_info=False)


def _is_small_json(headers: dict[str, str], limit: int) -> bool:
    """A JSON body whose length is declared and within the record limit, so reading it first is bounded."""
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return False
    try:
        return 0 <= int(headers.get("content-length", "")) <= limit
    except ValueError:
        return False


async def _read_all(receive: Callable) -> list[bytes]:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return chunks
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return chunks


def _replay(chunks: list[bytes], receive: Callable) -> Callable:
    """A receive that yields the chunks already read, then passes everything else through."""
    pending = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
               for index, chunk in enumerate(chunks)] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def replay() -> dict:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


def _origin_id(identity: Any) -> str | None:
    kind = getattr(identity, "auth_kind", None)
    if kind == "app_key":
        return getattr(identity, "credential_id", None)
    if kind == "mcp_grant":
        return getattr(identity, "connection_id", None)
    return None


def _quietly(function: Callable[[bytes], None], chunk: bytes) -> None:
    """Capture must never change what the caller receives: a failure only stops the capture."""
    try:
        function(chunk)
    except Exception:
        log.warning("request history capture stopped on a body chunk", exc_info=False)


async def _settle(function: Callable[..., None], **kwargs: Any) -> None:
    try:
        await asyncio.to_thread(function, **kwargs)
    except Exception:
        log.warning("request history finish failed", exc_info=False)
