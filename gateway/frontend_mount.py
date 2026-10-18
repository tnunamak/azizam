"""Mount inbound protocol frontends from their route rows.

A frontend record declares routes under ``registered_routes``. A row that names a
``translator`` and an ``intent`` is mounted here: its canonical path and its alias
each get one route for the row's methods, served by the operation the translator
registers for that intent. A row without a translator is a declaration; the module
that owns those paths registers them itself. A disabled frontend mounts nothing.

A frontend declares ``strictness`` (``exact`` or ``lenient``; ``exact`` when it says nothing). A row
may declare ``carries``, the top-level request fields its translator can carry. Such a row mounts
behind a check: ``exact`` refuses any other field, ``lenient`` drops it, and either way the
``x-azizam-loss`` header reports it (gateway/frontend_loss.py). An ``identity`` row carries every
field, so it may not declare ``carries``; its loss report is empty.

Two registries hold what can be mounted. ``Translators`` maps translator id ->
intent -> Operation and is the only place a handler becomes reachable from data.
Owned translators are modules that register their own paths, in the order their
mount functions were given.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from gateway import frontend_loss
from gateway.frontend_loss import LossReport

IDENTITY = "identity"

WEBSOCKET = "WEBSOCKET"

# The translators a package, UI or imported row may name. Every other translator reaches code that
# only builtin rows may mount: provider-native and native forward to engines, gateway and realtime
# serve the gateway's own operations. This list is reviewed code, read when a package is reviewed
# and again when the table mounts.
PACKAGE_MOUNTABLE_TRANSLATORS = frozenset({IDENTITY, "anthropic", "ollama"})


class FrontendMountError(ValueError):
    """A row cannot be mounted. The mount refuses the whole table before it adds any route."""


@dataclass(frozen=True)
class Operation:
    """What a translator serves for one intent: the handler and the route options it was mounted with."""

    endpoint: Callable[..., Any]
    methods: frozenset[str]
    include_in_schema: bool = True
    openapi_extra: Mapping[str, Any] | None = None
    response_class: type | None = None
    # True when the handler checks the caller itself (with its own error shape, or by a signature).
    # Every other operation is authenticated by the mount before the handler runs.
    self_authenticates: bool = False


@dataclass(frozen=True)
class RouteRow:
    frontend: str
    path: str
    methods: frozenset[str]
    intent: str
    translator: str
    strictness: str = frontend_loss.EXACT
    carries: frozenset[str] | None = None
    # The row comes from the builtin seed, which ships with the code; every other row is a package row.
    builtin: bool = False


# Answers a request an exact frontend refuses. It authenticates the caller, writes the receipt and
# returns the error response; the mount supplies the row and what could not be carried.
Refuse = Callable[[Request, "RouteRow", LossReport], Awaitable[Response]]

# Authenticates the caller of an operation that does not do so itself. It returns the refusal to send
# (401 without a key), or None once the request scope carries the caller's identity.
Authenticate = Callable[[Request], Awaitable[Response | None]]


class Translators:
    """The translator registry: translator id -> intent -> Operation, plus owned mounts."""

    def __init__(self, translators: Mapping[str, Mapping[str, Operation]] | None = None) -> None:
        self._operations: dict[str, dict[str, Operation]] = {}
        self._owned: dict[str, Callable[[FastAPI], None]] = {}
        for translator_id, operations in (translators or {}).items():
            self.register(translator_id, operations)

    def register(self, translator_id: str, operations: Mapping[str, Operation]) -> None:
        if translator_id in self._operations or translator_id in self._owned:
            raise FrontendMountError(f"translator {translator_id!r} is registered twice")
        self._operations[translator_id] = dict(operations)

    def register_owned(self, translator_id: str, mount: Callable[[FastAPI], None]) -> None:
        """A module that registers its own routes. Its mount runs in ``mount_owned``, before the rows."""
        if translator_id in self._operations or translator_id in self._owned:
            raise FrontendMountError(f"translator {translator_id!r} is registered twice")
        self._owned[translator_id] = mount

    def ids(self) -> list[str]:
        return sorted({*self._operations, *self._owned})

    def operations(self, translator_id: str) -> Mapping[str, Operation]:
        return dict(self._operations.get(translator_id) or {})

    def package_mountable(self, translator_id: str) -> bool:
        return translator_id in PACKAGE_MOUNTABLE_TRANSLATORS and translator_id in self._operations

    def operation(self, translator_id: str, intent: str) -> Operation:
        operations = self._operations.get(translator_id)
        if operations is None:
            raise FrontendMountError(f"unknown translator {translator_id!r}")
        operation = operations.get(intent)
        if operation is None:
            raise FrontendMountError(f"translator {translator_id!r} serves no intent {intent!r}")
        return operation

    def mount_owned(self, app: FastAPI) -> None:
        for mount in self._owned.values():
            mount(app)


@dataclass(frozen=True)
class RowProblem:
    """Why a row (or a whole frontend) cannot mount."""

    frontend: str
    path: str
    methods: tuple[str, ...]
    message: str


def rows_from_frontends(
    frontends: Iterable[Mapping[str, Any]],
    problems: list[RowProblem] | None = None,
) -> list[RouteRow]:
    """Every mountable row of the enabled frontends, canonical path before alias, in record order.

    A frontend that cannot be read raises ``FrontendMountError``; with ``problems``, it is skipped and
    the reason is added to that list.
    """
    rows: list[RouteRow] = []
    for frontend in frontends:
        if frontend.get("enabled") is False:
            continue
        try:
            rows.extend(_rows_of(frontend))
        except FrontendMountError as error:
            if problems is None:
                raise
            problems.append(RowProblem(str(frontend.get("id") or ""), "", (), str(error)))
    return rows


def _rows_of(frontend: Mapping[str, Any]) -> list[RouteRow]:
    rows: list[RouteRow] = []
    frontend_id = str(frontend.get("id") or "")
    builtin = frontend.get("source") == "builtin"
    strictness = frontend.get("strictness", frontend_loss.EXACT)
    if strictness not in frontend_loss.STRICTNESS:
        raise FrontendMountError(
            f"{frontend_id}: strictness must be one of {list(frontend_loss.STRICTNESS)}, not {strictness!r}"
        )
    for item in frontend.get("registered_routes") or ():
        if not isinstance(item, Mapping) or not item.get("translator"):
            continue
        methods = frozenset(_methods(item.get("methods")))
        carries = _carries(frontend_id, item)
        for path in (item.get("canonical"), item.get("alias")):
            if path:
                rows.append(RouteRow(
                    frontend=frontend_id,
                    path=str(path).strip(),
                    methods=methods,
                    intent=str(item.get("intent") or ""),
                    translator=str(item["translator"]),
                    strictness=strictness,
                    carries=carries,
                    builtin=builtin,
                ))
    return rows


def package_row_problems(frontend: Mapping[str, Any]) -> list[str]:
    """What stops a package, UI or imported frontend record from mounting its rows, read without a registry.

    A package row may name only a package-mountable translator, and its path opens with a literal
    segment, so it cannot shadow the web app or a path that has a handler of its own.
    """
    frontend_id = str(frontend.get("id") or "")
    problems: list[str] = []
    for item in frontend.get("registered_routes") or ():
        if not isinstance(item, Mapping) or not item.get("translator"):
            continue
        translator = str(item["translator"])
        if translator not in PACKAGE_MOUNTABLE_TRANSLATORS:
            problems.append(
                f"{frontend_id}: a package row may not name translator {translator!r}; "
                f"package rows may name only {sorted(PACKAGE_MOUNTABLE_TRANSLATORS)}"
            )
        for path in (item.get("canonical"), item.get("alias")):
            if path and not _opens_with_literal(str(path)):
                problems.append(f"{frontend_id}: {path} must open with a literal path segment, not a parameter")
    return problems


def mount(
    app: FastAPI,
    rows: Iterable[RouteRow],
    translators: Translators,
    *,
    index: int | None = None,
    refuse: Refuse | None = None,
    authenticate: Authenticate | None = None,
) -> list[BaseRoute]:
    """Add one route per row and method group, in row order.

    ``index`` places the new routes at that position in the app's route list (before the
    web app route, for instance); by default they are appended. Every row is checked
    against the registry and against the routes already mounted before any route is added.
    ``refuse`` answers a request an exact frontend refuses; without it the mount answers with a bare error.
    ``authenticate`` runs before every operation that does not authenticate itself.
    """
    planned, problems = plan(_claimed(app.routes), rows, translators, authenticate=authenticate)
    if problems:
        raise FrontendMountError(problems[0].message)
    start = len(app.router.routes)
    for row, operation in planned:
        _add_route(app, row, operation, refuse=refuse, authenticate=authenticate)
    created = app.router.routes[start:]
    if index is not None:
        del app.router.routes[start:]
        app.router.routes[index:index] = created
    return created


def plan(
    claimed: Mapping[tuple[str, str], str],
    rows: Iterable[RouteRow],
    translators: Translators,
    *,
    authenticate: Authenticate | None,
) -> tuple[list[tuple[RouteRow, Operation]], list[RowProblem]]:
    """The rows that can mount, with the operation each reaches, and the rows that cannot.

    Builtin rows are placed first, so a package row can never take a path a builtin row holds. A
    row that cannot mount is a problem; the rows after it still mount.
    """
    claims = dict(claimed)
    planned: list[tuple[RouteRow, Operation]] = []
    problems: list[RowProblem] = []
    for row in sorted(rows, key=lambda candidate: not candidate.builtin):
        try:
            operation = _check_row(row, translators, claims, authenticate)
        except FrontendMountError as error:
            problems.append(RowProblem(row.frontend, row.path, tuple(sorted(row.methods)), str(error)))
            continue
        for method in row.methods:
            claims[(method, _shape(row.path))] = row.frontend
        planned.append((row, operation))
    return planned, problems


def _claimed(routes: Iterable[BaseRoute]) -> dict[tuple[str, str], str]:
    claimed: dict[tuple[str, str], str] = {}
    for route in routes:
        for method in getattr(route, "methods", None) or ():
            claimed[(method, _shape(getattr(route, "path", "")))] = "an existing route"
    return claimed


def _check_row(
    row: RouteRow,
    translators: Translators,
    claims: Mapping[tuple[str, str], str],
    authenticate: Authenticate | None,
) -> Operation:
    operation = translators.operation(row.translator, row.intent)
    if not row.builtin:
        if not translators.package_mountable(row.translator):
            raise FrontendMountError(
                f"{row.frontend}: {row.path} is a package row, which may not name translator {row.translator!r}; "
                f"only builtin rows may mount it"
            )
        if not _opens_with_literal(row.path):
            raise FrontendMountError(
                f"{row.frontend}: {row.path} must open with a literal path segment, not a parameter"
            )
    if not row.methods:
        raise FrontendMountError(f"{row.frontend}: {row.path} names no methods")
    if row.carries is not None and row.translator == IDENTITY:
        raise FrontendMountError(
            f"{row.frontend}: {row.path} is an identity row, which carries every field and declares no carries"
        )
    unserved = row.methods - operation.methods
    if unserved:
        raise FrontendMountError(
            f"{row.frontend}: {row.path} asks {sorted(unserved)} of {row.translator}/{row.intent}, "
            f"which serves {sorted(operation.methods)}"
        )
    if not operation.self_authenticates and (WEBSOCKET in row.methods or authenticate is None):
        raise FrontendMountError(
            f"{row.frontend}: {row.translator}/{row.intent} does not authenticate itself, "
            f"and this mount has no authenticator to run before it"
        )
    for method in sorted(row.methods):
        holder = claims.get((method, _shape(row.path)))
        if holder is not None:
            raise FrontendMountError(f"{method} {row.path} is registered twice ({holder}, {row.frontend})")
    return operation


def _add_route(
    app: FastAPI,
    row: RouteRow,
    operation: Operation,
    *,
    refuse: Refuse | None,
    authenticate: Authenticate | None,
) -> None:
    if WEBSOCKET in row.methods:
        app.add_api_websocket_route(row.path, operation.endpoint)
        return
    options: dict[str, Any] = {}
    if not operation.include_in_schema:
        options["include_in_schema"] = False
    if operation.openapi_extra is not None:
        options["openapi_extra"] = operation.openapi_extra
    if operation.response_class is not None:
        options["response_class"] = operation.response_class
    route_class = _mounted_route_class(row, operation, authenticate=authenticate, refuse=refuse)
    if route_class is not None:
        options["route_class_override"] = route_class
    app.router.add_api_route(row.path, operation.endpoint, methods=sorted(row.methods), **options)


_PARAMETER = re.compile(r"\{[^}:]*(:[^}]*)?\}")


def _shape(path: str) -> str:
    """The path with its parameter names removed: two paths of one shape match the same requests."""
    return _PARAMETER.sub(lambda match: "{" + (match.group(1) or "") + "}", path)


def _opens_with_literal(path: str) -> bool:
    return path.startswith("/") and len(path) > 1 and not path[1:].startswith("{")


def _methods(value: Any) -> list[str]:
    if value is None:
        return []
    raw = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set, frozenset)) else []
    methods: list[str] = []
    for item in raw:
        method = str(item).strip().upper()
        if method and method not in methods:
            methods.append(method)
    return methods


def _carries(frontend_id: str, item: Mapping[str, Any]) -> frozenset[str] | None:
    if "carries" not in item:
        return None
    value = item["carries"]
    if not isinstance(value, (list, tuple)) or not all(isinstance(name, str) and name for name in value):
        raise FrontendMountError(f"{frontend_id}: carries must be a list of request field names")
    return frozenset(value)


def _mounted_route_class(
    row: RouteRow,
    operation: Operation,
    *,
    authenticate: Authenticate | None,
    refuse: Refuse | None,
) -> type[APIRoute] | None:
    """A route class that authenticates the caller, and holds the row's request to what its translator carries.

    None when the operation authenticates itself and the row declares no ``carries``: the route needs no wrapper.
    """
    authenticating = not operation.self_authenticates
    if not authenticating and row.carries is None:
        return None

    class MountedRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
            serve = super().get_route_handler()

            async def handler(request: Request) -> Response:
                if authenticating:
                    assert authenticate is not None  # a row without an authenticator never plans
                    refusal = await authenticate(request)
                    if refusal is not None:
                        return refusal
                if row.carries is None:
                    return await serve(request)
                return await _serve_checked(request, serve, row, refuse)

            return handler

    return MountedRoute


async def _serve_checked(
    request: Request,
    serve: Callable[[Request], Awaitable[Response]],
    row: RouteRow,
    refuse: Refuse | None,
) -> Response:
    payload = _json_object(await request.body())
    lost = [name for name in payload if name not in row.carries] if payload is not None else []
    if not lost:
        with frontend_loss.tracking(LossReport()) as report:
            return _with_loss_header(await serve(request), report)
    report = LossReport(dropped=lost)
    if row.strictness == frontend_loss.EXACT:
        refusal = await refuse(request, row, report) if refuse else _bare_refusal(row, report)
        return _with_loss_header(refusal, report)
    kept = {name: value for name, value in payload.items() if name in row.carries}
    with frontend_loss.tracking(report):
        return _with_loss_header(await serve(_with_body(request, kept)), report)


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _with_body(request: Request, payload: Mapping[str, Any]) -> Request:
    """The same request carrying ``payload`` as its body."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    scope = dict(request.scope)
    scope["headers"] = [
        (name, value) for name, value in request.scope["headers"] if name != b"content-length"
    ] + [(b"content-length", str(len(body)).encode())]
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _with_loss_header(response: Response, report: LossReport) -> Response:
    response.headers[frontend_loss.LOSS_HEADER] = report.header_value()
    return response


def refusal_message(row: RouteRow, report: LossReport) -> str:
    fields = ", ".join(str(name)[:48] for name in report.dropped[:8])
    return f"{row.path} cannot carry these request fields: {fields}. Remove them, or use a lenient frontend."


def _bare_refusal(row: RouteRow, report: LossReport) -> Response:
    return JSONResponse(
        {"error": {"message": refusal_message(row, report), "type": "invalid_request_error",
                   "code": "frontend_field_unsupported"}},
        status_code=400,
    )
