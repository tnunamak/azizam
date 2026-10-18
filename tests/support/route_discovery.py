"""Find every route of the exported ASGI app (decision D32 b): mounts, host routes, WebSockets and all declared methods.

The walk reads the app's own route table, recursively, so a route added later is found without anyone remembering to
list it. It reports the endpoint's module, so a test can tell which router a route belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from starlette.routing import BaseRoute, Host, Mount, WebSocketRoute


@dataclass(frozen=True)
class Found:
    path: str
    methods: tuple[str, ...]  # empty for a WebSocket
    websocket: bool
    module: str
    route: Any = None

    @property
    def management(self) -> bool:
        from gateway import egress

        return isinstance(self.route, egress.ManagementRoute)


def discover(app: Any) -> list[Found]:
    found: list[Found] = []

    def walk(routes: list[BaseRoute], prefix: str) -> Iterator[Found]:
        for route in routes:
            path = prefix + getattr(route, "path", "")
            if isinstance(route, (Mount, Host)):
                inner = getattr(route, "routes", None) or getattr(getattr(route, "app", None), "routes", None)
                if inner:
                    yield from walk(list(inner), path if isinstance(route, Mount) else prefix)
                else:
                    yield Found(path or "/", ("*",), False, getattr(getattr(route, "app", None), "__module__", "?"), route)
                continue
            endpoint = getattr(route, "endpoint", None)
            module = getattr(endpoint, "__module__", None) or getattr(getattr(route, "app", None), "__module__", "?")
            if isinstance(route, WebSocketRoute):
                yield Found(path, (), True, module, route)
            else:
                methods = tuple(sorted(getattr(route, "methods", None) or ()))
                yield Found(path, methods, False, module, route)

    found.extend(walk(list(getattr(app, "routes", ())), ""))
    return found


def projecting_names(module: Any) -> set[str]:
    """The names in ``module`` that reach ``egress.projected_response``: the constructor and every function that calls one.

    A function that builds a registry answer is found by what it calls, not by the key names of its payload.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    reaching = {"projected_response"}
    changed = True
    while changed:
        changed = False
        for function in functions:
            if function.name not in reaching and _names_in(function) & reaching:
                reaching.add(function.name)
                changed = True
    return reaching


def _names_in(node: Any) -> set[str]:
    import ast

    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def can_answer_with_a_record(found: Found) -> bool:
    """True when the route's handler calls the projected-response constructor, directly or through a helper."""
    import ast
    import inspect
    import textwrap

    endpoint = getattr(found.route, "endpoint", None)
    module = inspect.getmodule(endpoint) if endpoint is not None else None
    if module is None:
        return False
    source = textwrap.dedent(inspect.getsource(endpoint))
    return bool(_names_in(ast.parse(source)) & projecting_names(module))
