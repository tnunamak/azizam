"""What can a handler reach? A call graph over the repo's own functions, built from live objects.

``inspect.getsource(handler)`` sees only the handler's own lines, so a risky call one helper down is
missed. ``reach`` follows the calls it can resolve, a bounded number of steps, and never enters a function
twice (a cycle ends).

A call is resolved the way Python would: a bare name in the function's module or closure, a module's
attribute (``accounts.create_link``), a method of a module-level instance or of a closure variable
(``store.create_credential``), a method through ``self``. A function handed to another call
(``asyncio.to_thread(helper, ...)``) counts as called. Only functions defined under the repo root are
entered. What it cannot resolve is not followed: a receiver that is a local variable, a call through a
dict of callbacks. So it is a lower bound, never a guess by name; a test that needs more must name the
call itself.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
MAX_DEPTH = 4


def production_files() -> list[Path]:
    """The gateway's own source: ``proxy.py`` and everything under ``gateway/``."""
    return [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]


def _plain(obj: Any) -> Callable | None:
    """The plain function behind a method, static/class method or decorated function, or None."""
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    obj = getattr(obj, "__func__", obj)
    try:
        obj = inspect.unwrap(obj)
    except ValueError:
        return None
    return obj if inspect.isfunction(obj) else None


def _own(function: Callable) -> bool:
    try:
        file = Path(inspect.getsourcefile(function) or "").resolve()
    except (TypeError, OSError):
        return False
    return ROOT in file.parents


def _static(base: Any, name: str) -> Any:
    try:
        return inspect.getattr_static(base, name)
    except AttributeError:
        return None


def _env(function: Callable) -> dict[str, Any]:
    env = dict(function.__globals__)
    for name, cell in zip(function.__code__.co_freevars, function.__closure__ or ()):
        try:
            env[name] = cell.cell_contents
        except ValueError:  # a closure cell not filled yet
            pass
    return env


def _own_class(function: Callable, env: dict[str, Any]) -> Any:
    parts = function.__qualname__.split(".")
    if len(parts) >= 2 and parts[-2] != "<locals>":
        found = env.get(parts[-2])
        return found if inspect.isclass(found) else None
    return None


@lru_cache(maxsize=None)
def tree(function: Callable) -> ast.AST:
    """The function's parsed source; empty for what cannot be parsed from its own lines (a lambda inside a call)."""
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, SyntaxError):
        return ast.Module(body=[], type_ignores=[])


@lru_cache(maxsize=None)
def call_names(function: Callable) -> frozenset[str]:
    """The names the function's own code calls (``f(...)`` gives ``f``, ``a.b.f(...)`` gives ``f``). A name in
    a docstring or a string does not count."""
    names: set[str] = set()
    for call in ast.walk(tree(function)):
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return frozenset(names)


def callees(function: Callable) -> list[Callable]:
    """The repo functions ``function`` calls or hands on, as far as they can be resolved."""
    body = tree(function)
    env = _env(function)
    cls = _own_class(function, env)

    def resolve(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            if node.id in ("self", "cls"):
                return cls
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            base = resolve(node.value)
            return None if base is None else _static(base, node.attr)
        return None

    found: list[Callable] = []
    for call in ast.walk(body):
        if not isinstance(call, ast.Call):
            continue
        for expr in [call.func, *call.args, *(kw.value for kw in call.keywords)]:
            target = _plain(resolve(expr))
            if target is not None and target is not function and _own(target) and target not in found:
                found.append(target)
    return found


def reach(start: Callable, *, depth: int = MAX_DEPTH) -> dict[Callable, tuple[Callable, ...]]:
    """Every function ``start`` can reach within ``depth`` calls, with the shortest chain that gets there.

    ``start`` is in the result with an empty chain, first. Empty for a function outside the repo.
    """
    first = _plain(start)
    if first is None or not _own(first):
        return {}
    seen: dict[Callable, tuple[Callable, ...]] = {first: ()}
    frontier = [first]
    for _ in range(depth):
        following: list[Callable] = []
        for function in frontier:
            for target in callees(function):
                if target not in seen:
                    seen[target] = (*seen[function], target)
                    following.append(target)
        frontier = following
    return seen
