"""Request history capture: one decision and at most one write per captured request (REQUEST_RETENTION.md 3.3-3.6).

A pure ASGI middleware, so every data-plane route passes through it once. It reads the body as it streams in and the
reply as it streams out, and it waits for the request's end. The handler authenticates the caller and leaves the
identity in the request state (``proxy.authenticate_request``), so the decision, the write and the receipt's
retention entry are made then. A request that never authenticates is not recorded.

The decision is made as soon as the caller is known. A request whose history is off, or that asks to forget, does
not have its body parsed or its reply buffered, and it writes nothing but the receipt's entry (3.3, 12). The write
runs on one dedicated worker thread. It takes the household's write lock (admin.db) first, then a short media-jobs
transaction, the order the Library and Responses writers use (3.6).

The per-request control is read here, for every data-plane route: ``extensions["com.azizam.prompts"]`` in a JSON body
(whatever its Content-Type) or in a form's fields, and ``X-Azizam-Prompts: forget`` for a body the gateway does not
parse. The header is removed before the handler runs and carried on the request's slot, which the Responses and media
decisions read. A forget keeps nothing but the receipt's structural entry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

from gateway import admin_store, media_jobs, receipt_store, request_history, request_history_slot, retention
from gateway.extensions import ExtensionError, multipart_extensions

log = logging.getLogger("gateway.request_history")

KIND = retention.REQUEST_HISTORY
GROUP = retention.GROUP_REQUEST_HISTORY
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
#: The one thread that writes request history, so a write never waits behind the shared pool's work (3.6).
WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="request-history")
#: Captures that keep a body and are not yet written, at once. Each holds its body in memory until its write is done. A
#: request takes a slot only when its decision says keep. A request beyond this limit is not captured, and its receipt
#: says storage_unavailable.
MAX_PENDING_CAPTURES = 32
_PENDING = threading.BoundedSemaphore(MAX_PENDING_CAPTURES)


def is_captured(method: str, path: str) -> bool:
    return method in CAPTURED_METHODS and path.startswith(DATA_PLANE_PREFIXES)


def _header_map(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in raw}


def _identity(state: dict) -> Any:
    return getattr(state.get(IDENTITY_STATE_KEY), "identity", None)


def receipt_entry(outcome: retention.WriteOutcome, parts: list[str]) -> dict[str, Any]:
    """The receipt's retention entry for this write (13.3). ``failed`` only when the write itself raised."""
    if outcome.kept:
        return {"kind": KIND, "decision": "kept", "reason": "kept",
                "kept_until_ms": None if outcome.kept_until is None else int(outcome.kept_until) * 1000,
                "parts": list(parts), "limited_by": outcome.limited_by or "unbounded_store"}
    if outcome.reason == "write_failed":
        return {"kind": KIND, "decision": "failed", "reason": "write_failed"}
    return {"kind": KIND, "decision": "not_kept", "reason": outcome.reason}


def _library_prompt_receipt_entry(outcome: retention.WriteOutcome) -> dict[str, Any]:
    """The durable receipt entry for a Library prompt finalized with request capture."""
    kind = retention.LIBRARY_PROMPT
    if outcome.kept:
        return {
            "kind": kind, "decision": "kept", "reason": "kept",
            "kept_until_ms": None if outcome.kept_until is None else int(outcome.kept_until) * 1000,
            "parts": list(retention.bundled().parts(kind)),
            "limited_by": outcome.limited_by or "unbounded_store",
        }
    if outcome.reason == "write_failed":
        return {"kind": kind, "decision": "failed", "reason": "write_failed"}
    return {"kind": kind, "decision": "not_kept", "reason": outcome.reason}


def _record_entry(receipt_id: str | None, outcome: retention.WriteOutcome, parts: list[str]) -> None:
    if not receipt_id:
        return
    try:
        receipt_store.update_retention(receipt_id, receipt_entry(outcome, parts))
    except Exception:
        log.warning("request history receipt update failed request_id=%s", receipt_id, exc_info=False)


def _control_refusal(raw: bytes) -> JSONResponse | None:
    """Refuse an invalid per-request control before the route runs, as the media and Responses routes do (3.4)."""
    # A control key has no escape-free spelling without these bytes; a key written with an escape has a backslash.
    if b"extensions" not in raw and b"\\" not in raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
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
    """A forget in the body. An invalid or unreadable control counts as a forget: nothing is kept on a doubtful request.

    Every media type is read for a JSON control, because the routes parse one whatever the Content-Type says. A form
    is also read for its fields, as the routes read a form."""
    if body.media_type == "multipart/form-data":
        return _form_forget(body.control_source or {}, body.control_files)
    if body.form_source is not None and _form_forget(body.form_source, set()):
        return True
    source = body.control_source
    if not isinstance(source, dict):
        return False
    try:
        return retention.requested_mode(source) == "forget"
    except retention.PromptModeError:
        return True


def _form_forget(fields: Mapping[str, Any], files: set[str]) -> bool:
    """A form's control, read the way the routes read it: the JSON ``extensions`` field through
    ``multipart_extensions``, then the bracket field, which the routes take over the JSON block. A control sent as a
    file is not readable."""
    bracket = f"extensions[{retention.NAMESPACE}][mode]"
    if {"extensions", bracket} & files:
        return True
    try:
        controls = multipart_extensions(fields.get("extensions"))
        if bracket in fields:
            controls[retention.NAMESPACE] = {"mode": fields[bracket]}
        return retention.requested_mode({"extensions": controls}) == "forget"
    except (ExtensionError, retention.PromptModeError):
        return True


def _household_policy(admin_path: Path | None) -> dict[str, Any]:
    """The household's values for this group: the kill switch, and the group's default and cap."""
    row = admin_store.get_operator_setting(retention.SETTING_KEY, path=admin_path or admin_store.DB_PATH)
    return retention.household_view(row.get("value") if row else None, GROUP)


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


class RequestHistoryMiddleware:
    """Captures one request's history. ``admin_path`` and ``media_path`` are read at each request."""

    def __init__(self, app: Any, *, admin_path: Callable[[], Path], media_path: Callable[[], Path | None]) -> None:
        self.app = app
        self._admin_path = admin_path
        self._media_path = media_path

    def decide(self, identity: Any, *, forget: bool, now: int) -> retention.Decision:
        """The decision for a request from this caller. It reads the member's switch and the household's, and writes
        nothing: the one write, if there is one, runs under the household's lock and checks the setting again."""
        admin_path = self._admin_path()
        person_id = getattr(identity, "person_id", None)
        member = retention.member_choice(admin_path, person_id, GROUP) if person_id else None
        return retention.decide(KIND, person_id=person_id, member=member, household_policy=_household_policy(admin_path),
                                request_body=None, request_forget=forget, now=now)

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

        if _is_small_json(headers, limit):
            # Read a known-size JSON body first, so an invalid control is refused before any route runs.
            chunks, complete = await _read_all(receive)
            refusal = _control_refusal(b"".join(chunks))
            if refusal is not None:
                await refusal(scope, receive, send)
                return
            receive = _replay(chunks, receive, complete)  # the wrapper below feeds each replayed chunk, once

        media_path = Path(self._media_path() or media_jobs.DB_PATH)
        capture = _Capture(self, state=state, headers=headers, header_forget=header_forget, limit=limit,
                           route=scope["path"], media_path=media_path)
        receive_capture = capture.receive_wrapped(receive)
        slot_token = request_history_slot.open_slot(scope["path"], forget=header_forget, media_path=media_path)
        try:
            await self.app(scope, receive_capture, capture.send_wrapped(send))
        finally:
            try:
                # Only an authenticated request can produce a record, so an unauthenticated one is never read on.
                if _identity(state) is not None:
                    await capture.decide_once()
                if capture.wants_body and not (capture.complete or capture.disconnected):
                    await _drain(capture, receive_capture)
            finally:
                # However the request ends, even cancelled, the capture goes to the writer, which gives its slot back.
                slot = request_history_slot.current()
                receipt_id = slot.receipt_id if slot is not None else None
                request_history_slot.close_slot(slot_token)
                await _settle(capture, receipt_id, slot)


class _Capture:
    """One captured request: its body and reply as they stream past, and the decision made about them at its end."""

    def __init__(self, owner: RequestHistoryMiddleware, *, state: dict, headers: dict[str, str], header_forget: bool,
                 limit: int, route: str, media_path: Path) -> None:
        self._owner = owner
        self._state = state
        self._limit = limit
        self.header_forget = header_forget
        self.route = route
        self.media_path = media_path
        self.request_headers = headers
        self.started = int(time.time())
        self._began = time.monotonic()
        #: The JSON budget of this request's record, shared by its body and its reply (R6-2).
        self.budget = request_history.WorkBudget(max_bytes=limit)
        self.body = request_history.BodyCapture(headers.get("content-type"), limit=limit, budget=self.budget)
        self.reply: request_history.BodyCapture | None = None
        self.reply_status: int | None = None
        self.reply_headers: dict[str, str] = {}
        #: ``complete``: the body's last chunk arrived. ``disconnected``: the client went away first.
        self.complete = False
        self.disconnected = False
        #: Every body chunk reached the body capture. False once the early decision said not to keep it.
        self._fed = True
        #: Whether this capture holds a slot in the writer's admission limit. A slot is taken only when the decision says
        #: keep, and it is released when the write is done, so a request that keeps nothing never holds one.
        self.admitted = False
        self._decision: retention.Decision | None = None
        #: Set when the body's controls settle the outcome as the body completes (a forget, or a control that cannot be
        #: read in full). Nothing is kept then, and the admission slot has already been given back.
        self._early_reason: str | None = None
        self._deciding = asyncio.Lock()

    @property
    def wants_body(self) -> bool:
        """Whether the body is read to its end: the decision keeps it, and this capture holds a slot for the write."""
        return self._decision is not None and self._decision.keep and self.admitted

    async def decide_once(self) -> retention.Decision | None:
        """The early decision, made once the caller is known. None until then. A decision that cannot be read keeps
        nothing: capture must never change what the caller receives. A decision that keeps takes a slot, and a request
        that finds none is not captured (its receipt says storage_unavailable)."""
        if self._decision is None:
            identity = _identity(self._state)
            if identity is None:
                return None
            # A streamed reply listens for the client's disconnect while the body is read, so two tasks can get here.
            async with self._deciding:
                if self._decision is None:
                    await self._decide(identity)
        return self._decision

    async def _decide(self, identity: Any) -> None:
        try:
            decision = await asyncio.to_thread(self._owner.decide, identity, forget=self.header_forget, now=self.started)
        except Exception:
            log.warning("request history decision could not be read", exc_info=False)
            decision = retention.Decision(getattr(identity, "person_id", None), False, "storage_unavailable", kind=KIND)
        if decision.keep and self.complete:
            # The body was whole before the caller was known, so its controls are read now: a forget takes no slot, and
            # no reply is buffered for it (R6-3). The decision is published after this, under the lock in decide_once.
            await self.settle_body_controls()
        if decision.keep and self._early_reason is None:
            self.admitted = _PENDING.acquire(blocking=False)
        self._decision = decision

    async def settle_body_controls(self) -> None:
        """Read the body's controls once the body is whole. A forget, or a control that cannot be read in full, settles
        the outcome now, so the slot is given back while the route still runs (R4-4). The caller has checked that the
        decision keeps the body. It runs when the body completes with a slot held, and at the first decision for a body
        that was already whole (R6-3)."""
        try:
            await asyncio.to_thread(self.body.result)
        except Exception:
            self._settle_early("incomplete")
            return
        if _forget_requested(self.body):
            self._settle_early("request_forget")
        elif not self.body.control_readable():
            self._settle_early("incomplete")

    def _settle_early(self, reason: str) -> None:
        """Nothing will be kept: the receipt records ``reason`` at the end, the slot is given back now, and the reply is
        no longer buffered."""
        self._early_reason = reason
        self.reply = None
        self.release()

    def receive_wrapped(self, receive: Callable) -> Callable:
        async def wrapped() -> dict:
            message = await receive()
            if message["type"] == "http.request":
                decision = await self.decide_once()
                if decision is None or (decision.keep and self.admitted):
                    _quietly(self.body.feed, message.get("body", b""))
                else:
                    self._fed = False
                self.complete = not message.get("more_body", False)
                if self.complete and self.admitted:
                    await self.settle_body_controls()
            elif message["type"] == "http.disconnect":
                self.disconnected = True
            return message

        return wrapped

    def send_wrapped(self, send: Callable) -> Callable:
        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start":
                self.reply_status = int(message["status"])
                self.reply_headers = _header_map(message.get("headers", []))
                self.reply = None
                decision = await self.decide_once()
                if decision is not None and decision.keep and self.admitted and "received" in decision.parts:
                    self.reply = request_history.BodyCapture(self.reply_headers.get("content-type"), limit=self._limit,
                                                             budget=self.budget)
            elif message["type"] == "http.response.body" and self.reply is not None:
                _quietly(self.reply.feed, message.get("body", b""))
            await send(message)

        return wrapped

    def finish(self, receipt_id: str | None, slot: request_history_slot.Slot | None) -> None:
        """Decide, write and record the outcome on the receipt. Runs on the writer thread and never raises."""
        try:
            self._finish(receipt_id, slot)
        except Exception:
            log.warning("request history finish failed request_id=%s", receipt_id or "-", exc_info=False)
        finally:
            self.release()

    def release(self) -> None:
        """Give back this capture's admission slot. A slot is taken once and given back once, so this may run again."""
        if self.admitted:
            self.admitted = False
            _PENDING.release()

    def _finish(self, receipt_id: str | None, slot: request_history_slot.Slot | None) -> None:
        prompts = slot.take_library_prompts() if slot is not None else []
        identity = _identity(self._state)
        if identity is None:
            if prompts:
                self._write_media_capture(None, None, None, prompts, None,
                                          retention.WriteOutcome(False, "no_person"))
            return
        if self._decision is None:
            # A cancellation ended the request before its decision was read, so nothing is kept (R4-3).
            self._write_media_capture(None, None, None, prompts, receipt_id,
                                      retention.WriteOutcome(False, "incomplete"))
            return
        decision = self._decision
        if not decision.keep:
            self._write_media_capture(None, None, None, prompts, receipt_id,
                                      retention.WriteOutcome(False, decision.reason))
            return
        if self._early_reason is not None:
            # The body's controls settled this as the body completed, and the slot was given back then (R4-4).
            self._write_media_capture(None, None, None, prompts, receipt_id,
                                      retention.WriteOutcome(False, self._early_reason))
            return
        if not self.admitted:
            self._write_media_capture(None, None, None, prompts, receipt_id,
                                      retention.WriteOutcome(False, "storage_unavailable"))
            return
        if not self._fed or not (self.complete and not self.disconnected) or not self.body.control_readable():
            # The control could sit in a part that never arrived, or in a body that was too large or did not parse,
            # so nothing is kept: no row, and the receipt does not say kept.
            self._write_media_capture(None, None, None, prompts, receipt_id,
                                      retention.WriteOutcome(False, "incomplete"))
            return
        if _forget_requested(self.body):
            self._write_media_capture(None, None, None, prompts, receipt_id,
                                      retention.WriteOutcome(False, "request_forget"))
            return
        parts: dict[str, Any] = {}
        omissions_by_part: dict[str, list[str]] = {}
        accepted_body: Any = None
        if "accepted" in decision.parts:
            part, found = request_history.capture_request(self.request_headers, self.body)
            parts["accepted"], accepted_body = part, part["body"]
            omissions_by_part["accepted"] = sorted(found)
        if "received" in decision.parts and self.reply_status is not None and self.reply is not None:
            part, found = request_history.capture_reply(self.reply_status, self.reply_headers, self.reply)
            parts["received"] = part
            omissions_by_part["received"] = sorted(found)
        latency_ms = int((time.monotonic() - self._began) * 1000)
        summary = request_history.summarize(self.route, accepted_body, self.reply_status, latency_ms)
        record = {
            "record_id": request_history.new_record_id(), "request_id": receipt_id, "person_id": decision.person_id,
            "app_id": getattr(identity, "app_id", None), "origin_kind": getattr(identity, "auth_kind", "app_key"),
            "origin_id": _origin_id(identity), "created_at": self.started, "parts": parts,
            "omissions_by_part": omissions_by_part, "policy_version": int(retention.bundled().data["format_version"]),
            "summary": summary,
        }
        self._write_media_capture(decision, record, self.started, prompts, receipt_id,
                                  retention.WriteOutcome(False, "write_failed"))

    def _write_media_capture(
        self,
        decision: retention.Decision | None,
        record: dict[str, Any] | None,
        created_at: int | None,
        prompts: list[tuple[dict[str, Any], Callable | None]],
        receipt_id: str | None,
        no_history_outcome: retention.WriteOutcome,
    ) -> None:
        admin_path = self._owner._admin_path()
        person_id = decision.person_id if decision is not None else (
            prompts[0][0].get("person_id") if prompts else None
        )
        outcome = no_history_outcome
        written: list[str] = []
        prompt_outcomes: list[retention.WriteOutcome] = []
        erased_by_library = False

        def commit(
            history_values: tuple[int | None, tuple[str, ...]] | None,
            prompt_records: list[dict[str, Any]],
        ) -> None:
            # The one admin lock is held by retention.write_media_capture. Keep prompt and history writes in the same
            # short media transaction, with admin.db always acquired first.
            nonlocal written, erased_by_library
            with request_history.write_transaction(self.media_path) as conn:
                origins = {
                    str(value)
                    for value in (
                        (record or {}).get("request_id"), receipt_id,
                        *media_jobs.request_ids_for_assets_in(
                            conn, [str(prompt["asset_id"]) for prompt in prompt_records if prompt.get("asset_id")],
                        ),
                    )
                    if value
                }
                if person_id and any(
                    media_jobs.library_origin_erased_in(conn, person_id, origin)
                    for origin in origins
                ):
                    erased_by_library = True
                    return
                for prompt in prompt_records:
                    from gateway import prompt_memory

                    prompt_memory.insert_prompt_in(conn, prompt)
                if history_values is not None and record is not None:
                    deadline, parts = history_values
                    kept = _kept_record(record, parts, deadline)
                    request_history.insert_record(conn, kept)
                    written = list(kept["parts"])

        try:
            if record is None and not prompts:
                _record_entry(receipt_id, outcome, [])
                return
            history_result, prompt_outcomes = retention.write_media_capture(
                admin_path=admin_path, person_id=person_id, created_at=created_at or self.started,
                decision=decision, prompts=[prompt for prompt, _ in prompts], commit=commit,
            )
            if erased_by_library:
                history_result = retention.WriteOutcome(False, "erasure_cutoff")
                prompt_outcomes = [retention.WriteOutcome(False, "erasure_cutoff") for _ in prompts]
            if record is not None:
                outcome = history_result
        except Exception:
            log.warning("request history write failed request_id=%s", receipt_id or "-", exc_info=False)
            if record is not None:
                outcome = retention.WriteOutcome(False, "write_failed")
            prompt_outcomes = [retention.WriteOutcome(False, "write_failed") for _ in prompts]
        if outcome.reason == "write_failed" or any(item.reason == "write_failed" for item in prompt_outcomes):
            log.warning("media retention transaction failed request_id=%s", receipt_id or "-", exc_info=False)
        for (_, callback), prompt_outcome in zip(prompts, prompt_outcomes):
            if callback is not None:
                try:
                    callback(prompt_outcome)
                except Exception:
                    log.warning("Library prompt outcome callback failed", exc_info=False)
            if receipt_id:
                try:
                    receipt_store.update_retention(receipt_id, _library_prompt_receipt_entry(prompt_outcome))
                except Exception:
                    log.warning("Library prompt receipt update failed request_id=%s", receipt_id, exc_info=False)
        _record_entry(receipt_id, outcome, written)


def _kept_record(record: dict[str, Any], parts: tuple[str, ...], deadline: int | None) -> dict[str, Any]:
    """The record as it is written: the parts the member keeps, and only the omissions those parts made."""
    names = [name for name in parts if name in record["parts"]]
    omissions_by_part = record.get("omissions_by_part", {})
    return {**record, "kept_until": deadline, "parts": {name: record["parts"][name] for name in names},
            "omissions": sorted({item for name in names for item in omissions_by_part.get(name, [])})}


async def _drain(capture: _Capture, receive: Callable) -> None:
    """Read the rest of a body the route did not read, so the capture sees all of it. Bounded by BODY_DRAIN_SECONDS.
    A disconnect ends the drain at once: the body will not arrive, so the record is not kept."""
    async def rest() -> None:
        while not (capture.complete or capture.disconnected):
            await receive()
    try:
        await asyncio.wait_for(rest(), BODY_DRAIN_SECONDS)
    except (asyncio.TimeoutError, Exception):
        log.warning("request history body was not read to its end", exc_info=False)


async def _settle(capture: _Capture, receipt_id: str | None, slot: request_history_slot.Slot | None) -> None:
    """Hand the capture to the writer thread and wait for the write. The writer never raises into the request.

    The write is shielded, so a request that is cancelled does not cancel its write. A write that is still queued
    runs later and gives the capture's slot back (R4-3). A write that cannot be scheduled gives it back here."""
    try:
        future = asyncio.get_running_loop().run_in_executor(WRITER, capture.finish, receipt_id, slot)
    except Exception:
        log.warning("request history finish could not be scheduled", exc_info=False)
        capture.release()
        for _, callback in slot.take_library_prompts() if slot is not None else []:
            if callback is not None:
                try:
                    callback(retention.WriteOutcome(False, "write_failed"))
                except Exception:
                    log.warning("Library prompt outcome callback failed", exc_info=False)
        return
    try:
        await asyncio.shield(future)
    except Exception:
        log.warning("request history finish failed", exc_info=False)


def _is_small_json(headers: dict[str, str], limit: int) -> bool:
    """A JSON body whose length is declared and within the record limit, so reading it first is bounded."""
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return False
    try:
        return 0 <= int(headers.get("content-length", "")) <= limit
    except ValueError:
        return False


async def _read_all(receive: Callable) -> tuple[list[bytes], bool]:
    """The body's chunks, and whether they are the whole body. A disconnect before the end leaves it incomplete."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return chunks, False
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return chunks, True


def _replay(chunks: list[bytes], receive: Callable, complete: bool) -> Callable:
    """A receive that yields the chunks already read, then passes everything else through. A body that was cut short
    replays with ``more_body`` still set, so the route sees the disconnect that follows, not a complete body."""
    pending = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1 or not complete}
               for index, chunk in enumerate(chunks)]
    if complete and not chunks:
        pending = [{"type": "http.request", "body": b"", "more_body": False}]

    async def replay() -> dict:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay
