"""Coordinate connection attempts, persistence, sessions and cached model facts."""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import replace
from collections.abc import Callable, Mapping, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NotRequired, TypedDict
from urllib.parse import parse_qsl, urlsplit

import httpx

from gateway import engine_egress, model_routes, subscription_store
from gateway.alias_policy import AliasPolicyResolutionError, enters_route_dispatch, resolve_public_target
from gateway.provider_connections import CONNECT_MECHANISMS, ConnectionContext, Credential, SubscriptionError
from gateway.provider_protocols import REQUEST_PROTOCOLS
from gateway.session_guard import BrowserSession
from gateway.subscription_kinds import SubscriptionKind
from gateway.subscription_store import Attempt, SubscriptionStore
from gateway.turn_state import TurnStates

_LOG = logging.getLogger("azizam.subscription")
MODELS_TTL_SECONDS = 3600
REFRESH_BACKOFF_SECONDS = 300  # a failed model refresh is not retried by list reads sooner than this
CLIENT_FAULT_STATUSES = frozenset({400, 404, 413, 422})  # refusals caused by the request, not the subscription
HEALTH_WRITE_WAIT_SECONDS = 0.25  # the longest a request waits for its health note; the write then finishes alone
TOKEN_TOTALS_REUSE_SECONDS = 60  # an owner page shows token totals up to this old instead of scanning receipts again
USAGE_REWRITE_SECONDS = 60  # an unchanged usage reading is written again only after this long
CONNECTION_TIMEOUT_SECONDS = 30


def _default_http(timeout: httpx.Timeout | float = CONNECTION_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    return engine_egress.async_client(timeout=timeout)


def parse_callback(value: str) -> dict[str, str]:
    """Read query values from a whole callback address or a pasted query."""
    text = str(value or "").strip()
    if not text or len(text) > 8192:
        raise SubscriptionError(400, "callback_invalid", "Paste the whole address your browser ended on.")
    query = urlsplit(text).query if "?" in text or "://" in text else text.lstrip("?")
    pairs = dict(parse_qsl(query, keep_blank_values=True))
    if not pairs:
        raise SubscriptionError(400, "callback_invalid", "That address has no sign-in result in it. Paste the whole address.")
    return pairs


class UsageWindow(TypedDict):
    used_percent: float
    window_minutes: int | None  # the window's length; None when the plan did not say
    resets_at: int | None  # Unix seconds; None when the plan did not say
    id: NotRequired[str]
    scope: NotRequired[str]
    source: NotRequired[str]
    captured_at: NotRequired[int]
    models: NotRequired[str]  # the models the window counts, only when it counts some of them ("Opus")


class UsageLimits(TypedDict):
    captured_at: int  # Unix seconds: when the gateway read these numbers from a plan response
    windows: list[UsageWindow]


def usage_limits_view(stored: Mapping[str, Any]) -> UsageLimits:
    """The owner's view of a stored usage reading: the typed fields only, whatever else the row holds."""
    def whole(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def percent(value: Any) -> float | None:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        return float(value) if ok else None

    def models(value: Any) -> dict[str, str]:
        ok = isinstance(value, str) and 0 < len(value.strip()) <= 40
        return {"models": value.strip()} if ok else {}

    def identity(item: Mapping[str, Any]) -> dict:
        fields = {key: item[key] for key in ("id", "scope", "source")
                  if isinstance(item.get(key), str) and 0 < len(item[key]) <= 80}
        if whole(item.get("captured_at")) is not None:
            fields["captured_at"] = item["captured_at"]
        return fields

    windows: list[UsageWindow] = [
        {"used_percent": percent(item.get("used_percent")), "window_minutes": whole(item.get("window_minutes")),
         "resets_at": whole(item.get("resets_at")), **models(item.get("models")), **identity(item)}
        for item in stored.get("windows") or [] if isinstance(item, Mapping) and percent(item.get("used_percent")) is not None]
    return {"captured_at": whole(stored.get("captured_at")) or 0, "windows": windows}


def plain_name_chain(snapshot: Any, name: str) -> dict[str, Any]:
    """The ordered steps a request for the plain name ``name`` walks, read from the router's own chain.

    Every step is listed as an owner who may use all of them sees it. A
    person's access plan can only skip steps: dispatch reads the same chain,
    filtered by that person's grants (``model_routes.route_chain``). ``direct``
    marks a name that its own executor serves outside the route dispatcher;
    ``steps`` is then that one target. An ambiguous or unknown name has no steps.
    """
    try:
        target = resolve_public_target(snapshot, name)
    except AliasPolicyResolutionError:
        target = None
    except Exception:  # noqa: BLE001 - one bad name must not fail the owner's whole subscription list
        _LOG.warning("route chain not read for one plan model name")
        target = None
    if target is None:
        return {"slug": name, "direct": False, "steps": []}
    pinned = target.requested_model in snapshot.model_routes.pins
    steps = []
    for step in model_routes.route_chain(snapshot.services, target.routes, pinned=pinned):
        service = snapshot.services[step.edge.service_id]
        shown = {"route_id": step.edge.id, "kind": step.kind, "service_id": step.edge.service_id,
                 "name": str(service.get("display_name") or service.get("name") or step.edge.service_id),
                 "paid_fallback": step.paid_fallback}
        if step.connection_id:
            shown["connection_id"] = step.connection_id
        if step.provider_id:
            shown["provider_id"] = step.provider_id
        steps.append(shown)
    return {"slug": name, "direct": not enters_route_dispatch(snapshot, target), "steps": steps}


class SubscriptionService:
    def __init__(self, store: SubscriptionStore, kinds: Mapping[str, SubscriptionKind], *,
                 http: Callable = _default_http, mechanisms=None,
                 clock: Callable[[], float] = time.time, snapshot: Callable | None = None, changed: Callable | None = None,
                 token_totals: Callable[[str], Mapping[str, Any]] | None = None) -> None:
        self.store, self.kinds, self._http, self._clock = store, kinds, http, clock
        self.snapshot, self.changed = snapshot, changed
        # Tokens the gateway's own receipts counted per connection (receipt_store.plan_token_totals); None: not shown.
        self.token_totals = token_totals
        self.mechanisms = CONNECT_MECHANISMS if mechanisms is None else mechanisms
        self._runner_installs: dict[str, asyncio.Task] = {}
        self._runner_states: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._refreshing: dict[str, asyncio.Task] = {}  # one background model refresh at a time per kind
        self._health_writes: set[asyncio.Future] = set()  # health notes still writing after their request moved on
        # Per connection: (read at, totals). The totals scan a week of receipts, so an owner page reuses a recent read.
        self._totals_read: dict[str, tuple[float, dict[str, Any]]] = {}
        # Per kind: (written at, windows) of the last usage reading written; an unchanged reading is not written again soon.
        self._usage_written: dict[str, tuple[float, Any]] = {}
        self.turn_states = TurnStates()  # in memory only: each conversation's current turn state (gateway/turn_state.py)

    def kind(self, kind_id: str) -> SubscriptionKind:
        row = self.store.connection(kind_id)
        kind = self.kinds.get(row["kind"] if row else kind_id)
        if kind is None:
            raise SubscriptionError(404, "unknown_subscription_kind", "That kind of connection is not available.")
        return kind

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        client = self._http()
        try:
            yield client
        finally:
            await client.aclose()

    def context(self, kind_id: str, *, session: BrowserSession | None = None,
                rejected: Credential | None = None) -> ConnectionContext:
        identity = self.store.connection_id(kind_id)
        return ConnectionContext(self.kind(kind_id), self.store, self._client, self._clock,
                                 self._locks.setdefault(identity, asyncio.Lock()),
                                 self._runtime.setdefault(identity, {}), session, rejected)

    def new_client(self, kind: SubscriptionKind) -> httpx.AsyncClient:
        limit = float(CONNECTION_TIMEOUT_SECONDS)
        return self._http(timeout=httpx.Timeout(limit, read=max(kind.read_timeout, limit)))

    def configured(self, connection_id: str) -> bool:
        row = self.store.connection(connection_id)
        if row is None or row["status"] != subscription_store.CONNECTED:
            return False
        ctx = self.context(connection_id)
        return self.mechanisms[ctx.kind.mechanism].configured(ctx, row)

    async def validate_lease(self, credential: Credential) -> None:
        if credential.connection_id is not None:
            await asyncio.to_thread(self.store.require_generation, credential.connection_id, credential.generation)

    async def start(self, owner_id: str, kind_id: str, *, session: BrowserSession | None) -> dict[str, Any]:
        ctx = await asyncio.to_thread(self.context, kind_id, session=session)
        async with ctx.lock:
            mechanism = self.mechanisms[ctx.kind.mechanism]
            draft = Attempt(uuid.uuid4().hex, owner_id, kind_id, "", {}, 0, session)
            if getattr(mechanism, "interactive", True) is False:
                return await self._complete(ctx, replace(draft, attempt_id=""), {}, session=session)
            started = await mechanism.start(ctx, draft)
            if started is None:
                return await self._complete(ctx, replace(draft, attempt_id=""), {}, session=session)
            try:
                attempt_id = await asyncio.to_thread(self.store.begin_attempt, owner_id=owner_id, kind=kind_id,
                                                    state=started.state, mechanism_state=started.mechanism_state,
                                                    ttl=started.ttl, session=session, attempt_id=draft.attempt_id)
            except BaseException:
                close = getattr(mechanism, "close", None)
                if close is not None:
                    await close(ctx)
                raise
            return {"attempt_id": attempt_id, **started.owner_action}

    async def finish(self, owner_id: str, attempt_id: str, callback: str, *,
                     session: BrowserSession | None) -> dict[str, Any]:
        attempt = await asyncio.to_thread(self.store.take_attempt, attempt_id=attempt_id, owner_id=owner_id, consume=False)
        if attempt is None:
            raise SubscriptionError(404, "attempt_not_found", "That sign-in attempt is unknown or expired. Start again.")
        ctx = await asyncio.to_thread(self.context, attempt.kind, session=session)
        mechanism = self.mechanisms[ctx.kind.mechanism]
        values = getattr(mechanism, "parse_callback", parse_callback)(callback)
        async with ctx.lock:
            pending = getattr(mechanism, "pending", None)
            if pending is not None and pending(ctx, attempt, values):
                return {"status": "pending"}
            attempt = await asyncio.to_thread(self.store.take_attempt, attempt_id=attempt_id, owner_id=owner_id)
            if attempt is None:
                raise SubscriptionError(404, "attempt_not_found", "That sign-in attempt is unknown or expired. Start again.")
            return await self._finish(attempt, values, session=session)

    async def finish_by_state(self, callback_values: Mapping[str, str]) -> dict[str, Any]:
        state = callback_values.get("state", "")
        attempt = await asyncio.to_thread(self.store.take_attempt, state=state) if state else None
        if attempt is None:
            raise SubscriptionError(400, "state_mismatch", "That sign-in result does not belong to an open attempt.")
        return await self._finish(attempt, callback_values, session=attempt.session)

    async def _finish(self, attempt: Attempt, values: Any, *, session: BrowserSession | None) -> dict[str, Any]:
        ctx = await asyncio.to_thread(self.context, attempt.kind, session=session)
        return await self._complete(ctx, attempt, values, session=session)

    async def _complete(self, ctx: ConnectionContext, attempt: Attempt, values: Any, *,
                        session: BrowserSession | None) -> dict[str, Any]:
        facts = await self.mechanisms[ctx.kind.mechanism].finish(ctx, attempt, values)
        models = None
        try:
            models = await self._fetch_models(ctx.kind, facts.credential)
        except SubscriptionError:
            pass
        await asyncio.to_thread(self.store.save_facts, ctx.kind.id, attempt.owner_id, facts, session=session, models=models)
        self._usage_written.pop(ctx.kind.id, None)  # a new or ended sign-in starts with no reading
        await asyncio.to_thread(self.store.register_service, ctx.kind, attempt.owner_id, facts.credential, session=session)
        if self.changed is not None:
            await asyncio.to_thread(self.changed)
        return await self.status_one(ctx.kind.id)

    def backfill_services(self) -> int:
        """Once per start: seed the credential service of each stored connection whose registry has none.

        A connection made before the registry kept its service has nothing to derive plan models from. Seeding runs as
        the gateway, with no browser sign-in, and changes no access plan. A second run writes nothing. A failure is
        logged by connection id and service id only and never stops the start. When a row was written the registry
        snapshot is rebuilt through ``changed``. Returns the number of services seeded.
        """
        seeded = 0
        for row in self.store.connections():
            kind = self.kinds.get(row["kind"])
            if kind is None:
                continue
            try:
                seeded += bool(self.store.backfill_service(kind, row["connected_by"]))
            except Exception:  # noqa: BLE001 - a start must survive a registry it cannot repair
                _LOG.warning("connection service not seeded: connection %s, service %s", row["connection_id"], kind.model_prefix)
        if seeded and self.changed is not None:
            try:
                self.changed()
            except Exception:  # noqa: BLE001
                _LOG.warning("registry snapshot not rebuilt after seeding %d connection service(s)", seeded)
        return seeded

    async def acquire(self, kind_id: str, *, rejected: Credential | None = None) -> Credential:
        ctx = await asyncio.to_thread(self.context, kind_id, rejected=rejected)
        row = await asyncio.to_thread(self.store.connection, kind_id)
        if row is None or row["status"] == subscription_store.DISCONNECTED:
            raise SubscriptionError(503, "plan_not_connected", f"No {ctx.kind.display_name} plan is connected to this gateway. The owner can connect one.")
        if row["status"] == subscription_store.NEEDS_RECONNECT:
            raise SubscriptionError(503, "plan_connection_invalid", f"The gateway's {ctx.kind.display_name} connection ended. The owner needs to connect again.")
        credential = await self.mechanisms[ctx.kind.mechanism].acquire(ctx, row)
        await asyncio.to_thread(self.store.require_generation, row["connection_id"], row["generation"])
        return replace(credential, connection_id=row["connection_id"], generation=row["generation"])

    async def _fetch_models(self, kind: SubscriptionKind, credential: Credential) -> dict[str, list[str]]:
        from gateway.provider_protocols import DiscoveryError
        try:
            return await REQUEST_PROTOCOLS[kind.protocol].discover_models(kind, credential, self._client)
        except DiscoveryError as error:
            raise SubscriptionError(error.status, error.code, error.message) from None

    async def refresh_models(self, kind_id: str) -> dict[str, list[str]]:
        kind = await asyncio.to_thread(self.kind, kind_id)
        try:
            credential = await self.acquire(kind_id)
            models = await self._fetch_models(kind, credential)
        except SubscriptionError as error:
            await self.note_error(kind_id, error.code)
            raise
        await asyncio.to_thread(self.store.set_models, kind_id, models)
        if self.changed is not None:
            await asyncio.to_thread(self.changed)
        return models

    async def note_error(self, kind_id: str, code: str, *, status: int | None = None) -> None:
        """Remember a failure for the owner's health view. Never fails the caller; waits at most HEALTH_WRITE_WAIT_SECONDS.

        A refusal the client caused (a bad request: status in CLIENT_FAULT_STATUSES) says nothing about the
        subscription's health and is not recorded."""
        if status in CLIENT_FAULT_STATUSES:
            return
        await self._note(self.store.record_error, kind_id, code)

    async def note_ok(self, kind_id: str) -> None:
        """Remember a good use (and clear the last error). Same promise as note_error."""
        await self._note(self.store.record_ok, kind_id)

    async def note_usage(self, kind_id: str, kind: SubscriptionKind, headers: Mapping[str, str], *,
                         generation: int | None = None) -> None:
        """Keep the usage windows a plan response reports. Nothing is written when it reports none, so the last reading
        stays. Same promise as note_error: never fails the caller, waits at most HEALTH_WRITE_WAIT_SECONDS."""
        now = self._clock()
        try:
            snapshot = kind.usage_snapshot(headers, now=int(now))
        except Exception:  # noqa: BLE001 - a reading is advice; the request it came with must not fail
            _LOG.warning("usage reading not parsed for %s", kind_id)
            return
        await self.note_usage_snapshot(kind_id, snapshot, generation=generation)

    async def note_usage_snapshot(self, kind_id: str, snapshot: Mapping[str, Any] | None, *,
                                  generation: int | None = None) -> None:
        """Keep already parsed windows with the same dedupe and bounded, never-failing write as header readings.
        ``generation`` is the sign-in the reading came from; the store keeps it only while that sign-in is current."""
        if snapshot is None:
            return
        if generation is not None:
            snapshot = {**snapshot, "generation": generation}
        now = self._clock()
        seen = (snapshot.get("generation"), snapshot["windows"])
        last = self._usage_written.get(kind_id)
        if last is not None and last[1] == seen and now - last[0] < USAGE_REWRITE_SECONDS:
            return  # the same reading was written moments ago: one database write per plan call is not needed

        def write(kind_id: str, snapshot: Mapping[str, Any]) -> None:
            self.store.record_usage(kind_id, snapshot)
            self._usage_written[kind_id] = (now, seen)  # only a write that finished suppresses the next identical one

        await self._note(write, kind_id, snapshot)

    async def _note(self, write: Callable[..., None], kind_id: str, *args: Any) -> None:
        # Health is advice: the request it describes must not depend on it. A write slowed by a busy database
        # finishes on its own after the wait; its failure is logged when it ends.
        task = asyncio.ensure_future(asyncio.to_thread(write, kind_id, *args))
        self._health_writes.add(task)

        def done(finished: asyncio.Future) -> None:
            self._health_writes.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                _LOG.warning("health note not recorded for %s", kind_id)

        task.add_done_callback(done)
        try:
            await asyncio.wait_for(asyncio.shield(task), HEALTH_WRITE_WAIT_SECONDS)
        except Exception:  # noqa: BLE001 - a timeout or a failed write; done() logs the failure
            pass

    async def _token_totals(self, row: Mapping[str, Any]) -> dict[str, Any]:
        # Keyed by sign-in: a reconnect starts a new measured segment, so its totals are read again at once.
        key = (row["connection_id"], row["generation"])
        cached = self._totals_read.get(key)
        if cached is not None and self._clock() - cached[0] < TOKEN_TOTALS_REUSE_SECONDS:
            return cached[1]
        totals = dict(await asyncio.to_thread(self.token_totals, row["connection_id"],
                                              generation=row["generation"], signed_in_at=row["connected_at"]))
        self._totals_read[key] = (self._clock(), totals)
        return totals

    def top_effort_for(self, kind_id: str) -> str | None:
        return (self.store.connection(kind_id) or {}).get("top_effort")

    def efforts_for(self, kind_id: str, slug: str) -> list[str]:
        row = self.store.connection(kind_id)
        return list((row or {}).get("efforts", {}).get(slug) or [])

    def cache_namespace(self, kind_id: str) -> str:
        row = self.store.connection(kind_id)
        return str((row or {}).get("host_id") or kind_id)

    def provider_choices(self) -> list[dict[str, Any]]:
        return [{"id": row["connection_id"], "label": f"{self.kinds[row['kind']].display_name} plan",
                 "available": row["status"] == subscription_store.CONNECTED,
                 **({"grant_note": self.kinds[row["kind"]].grant_note} if self.kinds[row["kind"]].grant_note else {})}
                for row in self.store.connections() if row["kind"] in self.kinds]

    async def import_record(self, owner_id: str, kind_id: str, record: Mapping[str, Any], *,
                            session: BrowserSession | None) -> dict[str, Any]:
        ctx = await asyncio.to_thread(self.context, kind_id, session=session)
        draft = Attempt("", owner_id, kind_id, "", {}, 0, session, operation="import")
        facts = await self.mechanisms[ctx.kind.mechanism].finish(ctx, draft, record)
        await asyncio.to_thread(self.store.save_facts, kind_id, owner_id, facts, session=session)
        self._usage_written.pop(kind_id, None)  # a new or ended sign-in starts with no reading
        await asyncio.to_thread(self.store.register_service, ctx.kind, owner_id, facts.credential, session=session)
        try:
            await self.refresh_models(kind_id)
        except SubscriptionError:
            pass
        return await self.status_one(kind_id)

    async def install_runner(self, kind_id: str) -> dict[str, Any]:
        from gateway.connections import runner_install

        kind = self.kind(kind_id)
        if not (kind.row.get("runner") or {}).get("install"):
            raise SubscriptionError(400, "runner_install_unsupported", "This plan does not offer an automatic install.")
        kind_id = kind.id
        task = self._runner_installs.get(kind_id)
        if task is None or task.done():
            self._runner_states[kind_id] = {"state": "installing"}

            async def install() -> None:
                try:
                    def progress(received: int, size: int) -> None:
                        self._runner_states[kind_id] = {"state": "installing", "percent": received * 100 // size}

                    version = await runner_install.download(kind, self._client, progress)
                    self._runner_states[kind_id] = {"state": "ready", "version": version}
                except SubscriptionError as error:
                    self._runner_states[kind_id] = {"state": "failed", "error": {"code": error.code, "message": error.message}}

            self._runner_installs[kind_id] = asyncio.create_task(install())
        return await self.status_one(kind_id)

    async def status_one(self, kind_id: str) -> dict[str, Any]:
        ctx = await asyncio.to_thread(self.context, kind_id)
        row = await asyncio.to_thread(self.store.connection, kind_id)
        base = {"kind": kind_id, "display_name": ctx.kind.display_name, "protocol": ctx.kind.protocol,
                "billing_mode": ctx.kind.billing_mode, "sign_in": "browser",
                "revokes_sign_in": getattr(self.mechanisms[ctx.kind.mechanism], "revokes_sign_in", True)}
        paste_kind = getattr(self.mechanisms[ctx.kind.mechanism], "paste_kind", None)
        if paste_kind:
            base["paste_kind"] = paste_kind
        runner = ctx.kind.row.get("runner") or {}
        if runner.get("install"):
            from gateway.connections.cli_sign_in import _resolve_executable
            present = await asyncio.to_thread(_resolve_executable, str(runner["executable"]), runner.get("search_dirs") or [])
            state = self._runner_states.get(ctx.kind.id)
            if state and state["state"] in {"installing", "failed"}:
                base["runner"] = dict(state)
            else:
                base["runner"] = {"state": "ready" if present else "missing"}
                if present and state and state.get("version"):
                    base["runner"]["version"] = state["version"]
            base["runner"].update({key: runner["install"][key] for key in ("name", "source") if key in runner["install"]})
        install_page = runner.get("install_page_url")
        if install_page:
            base["install_page_url"] = install_page
        if ctx.kind.usage_page_url:
            base["usage_page_url"] = ctx.kind.usage_page_url
        if row is None:
            return {**base, "status": "not_connected"}
        details = self.mechanisms[ctx.kind.mechanism].status(ctx, row)
        view = {**base, "connection_id": row["connection_id"], "generation": row["generation"], "status": row["status"], "connected_by": row["connected_by"], **details}
        view.setdefault("connected_at", row["connected_at"])
        view["top_effort"] = row["top_effort"]
        view["models"] = list(row["models"])
        view["model_details"] = [{"slug": slug, "efforts": list(row["efforts"].get(slug) or [])} for slug in row["models"]]
        view["models_checked_at"] = row["models_at"]
        view["status_changed_at"] = row["updated_at"]
        view["last_ok_at"] = row["last_ok_at"]
        if row["last_error_code"]:
            view["last_error"] = {"code": row["last_error_code"], "at": row["last_error_at"]}
        if row["usage"]:
            view["usage_limits"] = usage_limits_view(row["usage"])
        if self.token_totals is not None:
            try:
                view["token_totals"] = await self._token_totals(row)
            except Exception:  # noqa: BLE001 - totals are advice; a busy or missing receipt file must not hide the row
                _LOG.warning("token totals not read for %s", row["connection_id"])
        if row["status"] == subscription_store.NEEDS_RECONNECT:
            # A row ended before the reason was kept was ended by a refused refresh: the only way a plan ended then.
            view["reconnect_reason"] = row["reconnect_reason"] or subscription_store.REFRESH_REFUSED
        view["routes"] = []
        view["model_chains"] = []
        if self.snapshot is not None:
            current = self.snapshot()
            for key, edges in current.model_routes.by_model.items():
                for edge in edges:
                    config = current.services[edge.service_id].get("config") or {}
                    source = config.get("credential_source") or {}
                    if source.get("connection_id") == row["connection_id"]:
                        view["routes"].append({"id": edge.id, "logical_model": key[1],
                                              "service_id": edge.service_id, "model_id": edge.model_id})
            names = sorted({*row["models"], *(route["logical_model"] for route in view["routes"])})
            view["model_chains"] = [plain_name_chain(current, name) for name in names]
        view.update(await asyncio.to_thread(self.store.owner_plan, row["connected_by"]))
        return view

    async def status(self) -> list[dict[str, Any]]:
        views = []
        for kind in self.kinds.values():
            row = await asyncio.to_thread(self.store.connection, kind.id)
            if row is not None and row["status"] == subscription_store.CONNECTED and (
                    row["models_at"] is None or self._clock() - row["models_at"] > MODELS_TTL_SECONDS) and (
                    row["last_error_at"] is None or self._clock() - row["last_error_at"] >= REFRESH_BACKOFF_SECONDS):
                self._refresh_in_background(kind.id)
            views.append(await self.status_one(kind.id))
        return views

    def _refresh_in_background(self, kind_id: str) -> None:
        """A list read answers from what is stored; a stale model list is renewed beside it, one task per kind."""
        running = self._refreshing.get(kind_id)
        if running is not None and not running.done():
            return

        async def renew() -> None:
            try:
                await self.refresh_models(kind_id)
            except SubscriptionError:
                pass  # already recorded as the connection's last error
            except Exception:  # noqa: BLE001 - nobody awaits this task
                _LOG.warning("model refresh failed for %s", kind_id)

        self._refreshing[kind_id] = asyncio.get_running_loop().create_task(renew())

    async def disconnect(self, kind_id: str, *, session: BrowserSession | None) -> dict[str, Any]:
        ctx = await asyncio.to_thread(self.context, kind_id, session=session)
        async with ctx.lock:
            row = await asyncio.to_thread(self.store.connection, kind_id)
            if row is None:
                close = getattr(self.mechanisms[ctx.kind.mechanism], "close", None)
                if close is not None:
                    await close(ctx)
                return await self.status_one(kind_id)
            row = await asyncio.to_thread(self.store.invalidate, kind_id, session=session)
            details = await self.mechanisms[ctx.kind.mechanism].disconnect(ctx, row)
            self._usage_written.pop(kind_id, None)
            await asyncio.to_thread(self.store.drop_tokens, row["kind"], session=session, expected_generation=row["generation"])
            return {**await self.status_one(kind_id), **details}

    async def close(self) -> None:
        """End downloads and pending sign-in processes before the event loop closes."""
        for task in self._runner_installs.values():
            task.cancel()
        await asyncio.gather(*self._runner_installs.values(), return_exceptions=True)
        for kind_id in self.kinds:
            ctx = await asyncio.to_thread(self.context, kind_id)
            close = getattr(self.mechanisms[ctx.kind.mechanism], "close", None)
            if close is not None:
                await close(ctx)

    async def listen_for_callback(self, *, kind_id: str | None = None, **options: Any):
        ctx = await asyncio.to_thread(self.context, kind_id or next(iter(self.kinds)))
        listener = getattr(self.mechanisms[ctx.kind.mechanism], "listen_for_callback", None)
        return await listener(ctx, self.finish_by_state, **options) if listener is not None else None
