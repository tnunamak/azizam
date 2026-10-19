"""The gateway never logs an upstream response body or an upstream error text (decision D43).

A credential reaches a log by being sent upstream: a server echoes what it was sent, in a body or an error, and an
``httpx`` error text names the URL (which can carry a credential). Scrubbing the line afterwards is the backup
(``gateway/log_scrub.py``); the rule is that such text is not put in a log call at all. A log line about an upstream says a
status, a content type, a byte count, the class of an error and the origin it was sent to, and a reason of our own.

This test finds every ``print``/``_say``/logger/``sys.stderr.write``/``traceback`` call in ``gateway/`` and ``proxy.py``
whose arguments are derived from

* the body of a response: ``.text``, ``.content``, ``.json()``, ``.read()``, ``.aread()``, ``.aiter_*()``, and any name
  assigned from them (so ``response_text[-300:]`` is found);
* the output of a subprocess: ``.stdout``, ``.stderr``, ``.communicate()``;
* the text of an exception caught around a network call (``str(exc)``, ``f"{exc}"``, ``exc.args``, ``repr(exc)``), or any
  exception caught as an ``httpx`` error;
* a traceback (``traceback.print_exc()``, ``format_exc()``): it holds the same text.

An argument passed through a formatter of ``gateway/log_scrub.py`` (``describe_response``, ``describe_bytes``,
``describe_error``) is allowed: they keep a status, a size and a class name. So is ``type(exc).__name__``. A call that is
found and is listed in ``ALLOWED`` below must say why the text cannot carry a credential.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANITIZERS = {"describe_response", "describe_bytes", "describe_error"}
BODY_ATTRS = {"text", "content", "stdout", "stderr"}
BODY_CALLS = {"json", "read", "aread", "readline", "readlines", "communicate", "iter_bytes", "iter_text", "iter_lines",
              "aiter_bytes", "aiter_text", "aiter_lines"}
NETWORK_NAMES = ("httpx", "client", "session", "urlopen", "requests", "aiohttp")
NETWORK_METHODS = {"aiter_bytes", "aiter_lines", "aread", "raise_for_status", "urlopen", "stream"}
HTTP_ERROR_MARKERS = ("httpx", "urllib", "aiohttp", "requests", "TransportFailure", "ProtocolUpstreamError", "ClientError")
SAFE_ERROR_ATTRS = {"__name__", "status", "status_code", "errno"}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
LOGGER_NAMES = {"logger", "logging", "log", "_log", "LOG", "LOGGER"}

# The receivers that are not an upstream: the incoming request, an uploaded or opened file.
NOT_UPSTREAM = {"request", "req", "form", "upload", "file", "f", "fp", "fh", "handle", "websocket"}
# A field extracted from an upstream's JSON is not the body (an id, a file name, a width), except the fields that carry text
# an upstream wrote about its own failure.
ERROR_FIELDS = {"error", "errors", "message", "msg", "detail", "details", "reason", "text", "description", "body", "output", "stderr"}

# A name that holds an identifier or a number is a value derived from a reply, not the text of one.
VALUE_SUFFIXES = ("_id", "_name", "_filename", "_width", "_height", "_count", "_size", "_status", "_code", "_index")

# "file::function": why the logged text cannot carry a credential an upstream was sent.
ALLOWED: dict[str, str] = {}


def _chain(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _is_log_call(call: ast.Call) -> bool:
    name, chain = _call_name(call), _chain(call.func)
    if name in ("print", "_say"):
        return True
    if name in LOG_METHODS and chain.split(".")[0] in LOGGER_NAMES:
        return True
    if name == "write" and chain.endswith(("stderr.write", "stdout.write")):
        return True
    return chain in ("traceback.print_exc", "traceback.print_exception", "traceback.print_stack")


def _is_network_call(call: ast.Call) -> bool:
    parts = _chain(call.func).lower().split(".")
    named = any(part == marker or part.endswith("_" + marker) for part in parts for marker in NETWORK_NAMES)
    return named or _call_name(call) in NETWORK_METHODS


class _Scan(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.stack: list[str] = []
        self.found: list[tuple[str, int, str]] = []

    # -- functions ---------------------------------------------------------------------------------------------------
    def _function(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        qualified = f"{self.relative}::{'.'.join(self.stack)}"
        for line, what in _violations(node):
            self.found.append((qualified, line, what))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def _own_nodes(function: ast.AST):
    """The nodes of a function body, not those of the functions and classes nested in it (each is scanned on its own)."""
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


def _tainted_handlers(function: ast.AST) -> list[ast.ExceptHandler]:
    """The ``except ... as name`` handlers of a function that makes a network call, or that catch an HTTP error.

    A function that talks to an upstream anywhere is treated as one whose errors may hold what the upstream was sent or said:
    the call that raised can be an adapter or a helper whose name does not say so.
    """
    nodes = list(_own_nodes(function))
    talks = any(isinstance(inner, ast.Call) and _is_network_call(inner) for inner in nodes)
    found: list[ast.ExceptHandler] = []
    for node in nodes:
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                kind = ast.unparse(handler.type) if handler.type is not None else ""
                if handler.name and (talks or any(marker in kind for marker in HTTP_ERROR_MARKERS)):
                    found.append(handler)
    return found


def _root(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else ""


def _field(node: ast.AST) -> str | None:
    """The literal key of ``x["key"]`` or ``x.get("key")``; ``""`` for a key that is not a literal; None if not an extraction."""
    if isinstance(node, ast.Subscript):
        return node.slice.value if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str) else ""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        return node.args[0].value if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) else ""
    return None


def _body_sources(node: ast.AST, tainted: set[str]) -> bool:
    """Whether the expression holds a response body, subprocess output or text derived from them."""
    if isinstance(node, ast.Call) and _call_name(node) in SANITIZERS:
        return False
    field = _field(node)
    if field and field.lower() not in ERROR_FIELDS:
        return False  # a named field of an upstream's JSON (an id, a name, a size) is a value, not the body
    if isinstance(node, ast.Attribute) and node.attr in BODY_ATTRS and _root(node) not in NOT_UPSTREAM | {"sys"}:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in BODY_CALLS \
            and _root(node.func.value) not in NOT_UPSTREAM:
        return True
    if isinstance(node, ast.Name) and node.id in tainted:
        return True
    return any(_body_sources(child, tainted) for child in ast.iter_child_nodes(node))


def _violations(function: ast.AST) -> list[tuple[int, str]]:
    handlers = _tainted_handlers(function)
    # What a subprocess wrote is named for what it is, often after it crossed a function boundary (``run()`` returns it).
    tainted: set[str] = {"stderr", "stdout", "response_text"}
    changed = True
    while changed:  # a name assigned from a body, or from a name assigned from one
        changed = False
        for node in _own_nodes(function):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets, value = [node.target], node.value
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets, value = [node.target], node.iter
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            if value is not None and _body_sources(value, tainted):
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and name.id not in tainted and not name.id.endswith(VALUE_SUFFIXES) \
                                and name.id not in ("id", "name", "width", "height"):
                            tainted.add(name.id)
                            changed = True
    found: list[tuple[int, str]] = []
    for node in _own_nodes(function):
        if not isinstance(node, ast.Call):
            continue
        if _chain(node.func) in ("traceback.print_exc", "traceback.print_exception", "traceback.print_stack"):
            found.append((node.lineno, "a traceback is printed"))
            continue
        if _is_log_call(node):
            exceptions = {handler.name for handler in handlers if handler.lineno <= node.lineno <= (handler.end_lineno or handler.lineno)}
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                found.extend((node.lineno, what) for what in _argument_violations(argument, tainted, exceptions))
    return found


def _argument_violations(node: ast.AST, tainted: set[str], exceptions: set[str]) -> list[str]:
    if isinstance(node, ast.Call) and _call_name(node) in SANITIZERS:
        return []
    if isinstance(node, ast.Call) and _chain(node.func) == "traceback.format_exc":
        return ["a traceback is logged"]
    if isinstance(node, ast.Attribute) and node.attr in SAFE_ERROR_ATTRS:
        return []
    if isinstance(node, ast.Attribute) and node.attr in BODY_ATTRS and _chain(node).split(".")[0] != "sys":
        return [f"a response or process {node.attr}"]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in BODY_CALLS:
        return [f"a response or process {node.func.attr}()"]
    if isinstance(node, ast.Name) and node.id in tainted:
        return [f"{node.id}, derived from a response or process output"]
    if isinstance(node, ast.Name) and node.id in exceptions:
        return [f"the text of {node.id}, an error from a network call"]
    if isinstance(node, ast.Call) and _call_name(node) == "type" and node.args and isinstance(node.args[0], ast.Name):
        return []
    out: list[str] = []
    for child in ast.iter_child_nodes(node):
        out.extend(_argument_violations(child, tainted, exceptions))
    return out


def discover() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in [*sorted((ROOT / "gateway").rglob("*.py")), ROOT / "proxy.py"]:
        scan = _Scan(path.relative_to(ROOT).as_posix())
        scan.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.extend(scan.found)
    return found


def test_no_log_call_takes_an_upstream_body_or_an_upstream_error_text():
    bare = sorted({(key, line, what) for key, line, what in discover() if key not in ALLOWED})
    assert not bare, "log calls that take text an upstream or a subprocess produced (use log_scrub.describe_*):\n" + "\n".join(
        f"  {key} line {line}: {what}" for key, line, what in bare)


def test_the_allowed_list_holds_no_stale_entry():
    keys = {key for key, _, _ in discover()}
    assert not [key for key in ALLOWED if key not in keys]


def test_the_checker_finds_what_it_is_meant_to_find():
    source = '''
import httpx
async def a(client, url):
    resp = await client.get(url)
    body = resp.text
    print(f"failed {body[:200]}")
async def b(client, url):
    try:
        await client.get(url)
    except Exception as e:
        print(f"error {e}")
async def c(client, url):
    try:
        await client.get(url)
    except Exception as e:
        print(type(e).__name__)
        print(log_scrub.describe_error(e))
async def d(client, url):
    resp = await client.get(url)
    print(log_scrub.describe_response(resp, "refused"))
def e(proc):
    print(proc.stderr)
async def f(resp):
    text = ""
    async for chunk in resp.aiter_bytes():
        text += chunk.decode()
    print(text)
def g():
    import traceback
    traceback.print_exc()
'''
    tree = ast.parse(source)
    scan = _Scan("x.py")
    scan.visit(tree)
    by_function = {key.split("::")[1] for key, _, _ in scan.found}
    assert by_function == {"a", "b", "e", "f", "g"}, scan.found
