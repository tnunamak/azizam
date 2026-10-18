"""Mount inbound protocol frontends from their route rows.

A frontend record declares routes under ``registered_routes``. A row that names a
``translator`` and an ``intent`` is mounted here: its canonical path and its alias
each get one route for the row's methods, served by the operation the translator
registers for that intent. A row without a translator is a declaration; the module
that owns those paths registers them itself. A disabled frontend mounts nothing.

Two registries hold what can be mounted. ``Translators`` maps translator id ->
intent -> Operation and is the only place a handler becomes reachable from data.
Owned translators are modules that register their own paths, in the order their
mount functions were given.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from starlette.routing import BaseRoute

WEBSOCKET = "WEBSOCKET"


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


@dataclass(frozen=True)
class RouteRow:
    frontend: str
    path: str
    methods: frozenset[str]
    intent: str
    translator: str


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


def rows_from_frontends(frontends: Iterable[Mapping[str, Any]]) -> list[RouteRow]:
    """Every mountable row of the enabled frontends, canonical path before alias, in record order."""
    rows: list[RouteRow] = []
    for frontend in frontends:
        if frontend.get("enabled") is False:
            continue
        frontend_id = str(frontend.get("id") or "")
        for item in frontend.get("registered_routes") or ():
            if not isinstance(item, Mapping) or not item.get("translator"):
                continue
            methods = frozenset(_methods(item.get("methods")))
            for path in (item.get("canonical"), item.get("alias")):
                if path:
                    rows.append(RouteRow(
                        frontend=frontend_id,
                        path=str(path).strip(),
                        methods=methods,
                        intent=str(item.get("intent") or ""),
                        translator=str(item["translator"]),
                    ))
    return rows


def mount(
    app: FastAPI,
    rows: Iterable[RouteRow],
    translators: Translators,
    *,
    index: int | None = None,
) -> list[BaseRoute]:
    """Add one route per row and method group, in row order.

    ``index`` places the new routes at that position in the app's route list (before the
    web app route, for instance); by default they are appended. Every row is checked
    against the registry and against the routes already mounted before any route is added.
    """
    planned: list[tuple[RouteRow, Operation]] = []
    claimed: dict[tuple[str, str], str] = {}
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            claimed[(method, getattr(route, "path", ""))] = "an existing route"

    for row in rows:
        operation = translators.operation(row.translator, row.intent)
        if not row.methods:
            raise FrontendMountError(f"{row.frontend}: {row.path} names no methods")
        unserved = row.methods - operation.methods
        if unserved:
            raise FrontendMountError(
                f"{row.frontend}: {row.path} asks {sorted(unserved)} of {row.translator}/{row.intent}, "
                f"which serves {sorted(operation.methods)}"
            )
        for method in sorted(row.methods):
            key = (method, row.path)
            if key in claimed:
                raise FrontendMountError(f"{method} {row.path} is registered twice ({claimed[key]}, {row.frontend})")
            claimed[key] = row.frontend
        planned.append((row, operation))

    start = len(app.router.routes)
    for row, operation in planned:
        if WEBSOCKET in row.methods:
            app.add_api_websocket_route(row.path, operation.endpoint)
            continue
        options: dict[str, Any] = {}
        if not operation.include_in_schema:
            options["include_in_schema"] = False
        if operation.openapi_extra is not None:
            options["openapi_extra"] = operation.openapi_extra
        if operation.response_class is not None:
            options["response_class"] = operation.response_class
        app.add_api_route(row.path, operation.endpoint, methods=sorted(row.methods), **options)

    created = app.router.routes[start:]
    if index is not None:
        del app.router.routes[start:]
        app.router.routes[index:index] = created
    return created


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
