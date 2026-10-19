"""A syntactic taint check for text an upstream wrote (decision D44), shared by the log test and the client-error test.

An upstream is an engine, a provider, an identity provider, a sidecar or a subprocess. What it wrote (a response body, an
error text, what a subprocess printed, the text of an exception raised around a call to it) is *tainted*: it may hold a
credential the gateway sent it, because a server echoes what it was sent. A tainted value must not reach a *sink*: a log
call, or a response/error built for an API caller.

Sources
* the body of a response: ``.text``, ``.content``, ``.json()``, ``.read()``, ``.aread()``, ``.aiter_*()``, and any name
  assigned from them;
* the output of a subprocess: ``.stdout``, ``.stderr``, ``.communicate()``;
* the text of an exception caught around a network call (``str(exc)``, ``f"{exc}"``, ``exc.args``, ``repr(exc)``), or any
  exception caught as an ``httpx`` error, and any name assigned from such text.

A value passed through a formatter of ``gateway/log_scrub.py`` (``describe_*``) is allowed; so is ``type(exc).__name__``
and the status-like attributes of an exception.

The check is syntactic. It follows names assigned in the same function; it does not follow text through a return value or
into a helper's parameter. The runtime egress (``gateway/log_egress.py``) and the fixed client builder
(``gateway/client_errors.py``) are the boundary; this check finds the call sites that would put text past them.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parents[2]
SANITIZERS = {"describe_response", "describe_bytes", "describe_error", "describe_origin", "describe_media_type",
              # a size or a flag derived from a body is a value, not its text
              "len", "int", "float", "bool", "isinstance", "id", "hash",
              # a summary the gateway builds from a history entry: node labels only, never the engine's error text
              "_graph_error_message", "execution_error_message"}
BODY_ATTRS = {"text", "content", "stdout", "stderr"}
BODY_CALLS = {"json", "read", "aread", "readline", "readlines", "communicate", "iter_bytes", "iter_text", "iter_lines",
              "aiter_bytes", "aiter_text", "aiter_lines"}
NETWORK_NAMES = ("httpx", "client", "session", "urlopen", "requests", "aiohttp", "adapter", "graph")
NETWORK_METHODS = {"aiter_bytes", "aiter_lines", "aread", "raise_for_status", "urlopen", "stream", "submit", "upload", "invoke",
                   "dispatch", "send"}
HTTP_ERROR_MARKERS = ("httpx", "urllib", "aiohttp", "requests", "TransportFailure", "ProtocolUpstreamError", "UpstreamError",
                      "ClientError", "ProtocolError", "LiteLLMAdminError", "ProviderUpstreamError")
SAFE_ERROR_ATTRS = {"__name__", "status", "status_code", "errno", "kind", "code", "param", "returncode", "retry_after"}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log", "warn", "fatal"}
LOGGER_NAMES = {"logger", "logging", "log", "_log", "LOG", "LOGGER", "_LOG", "_logger"}
TRACEBACK_TEXT = {"print_exc", "print_exception", "print_stack", "format_exc", "format_exception", "format_exception_only",
                  "TracebackException"}

# The receivers that are not an upstream: the incoming request, an uploaded or opened file.
NOT_UPSTREAM = {"request", "req", "form", "upload", "file", "f", "fp", "fh", "handle", "websocket"}
# A field extracted from an upstream's JSON is not the body (an id, a file name, a width), except the fields that carry text
# an upstream wrote about its own failure.
ERROR_FIELDS = {"error", "errors", "message", "msg", "detail", "details", "reason", "text", "description", "body", "output", "stderr"}
# A name that holds an identifier or a number is a value derived from a reply, not the text of one.
VALUE_SUFFIXES = ("_id", "_name", "_filename", "_width", "_height", "_count", "_size", "_status", "_code", "_index")


@dataclass
class ModuleInfo:
    """What a module calls its loggers, its ``logging`` module and its traceback helpers."""

    loggers: set[str] = field(default_factory=set)
    logging_modules: set[str] = field(default_factory=lambda: {"logging"})
    traceback_modules: set[str] = field(default_factory=lambda: {"traceback"})
    traceback_names: set[str] = field(default_factory=set)


def chain(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _is_get_logger(node: ast.AST, info: ModuleInfo) -> bool:
    return isinstance(node, ast.Call) and call_name(node) == "getLogger"


def module_info(tree: ast.Module) -> ModuleInfo:
    """Aliases: ``import logging as L``, ``import traceback as tb``, ``from traceback import format_exc as fe``, and every
    name or attribute assigned from ``getLogger(...)`` anywhere in the module."""
    info = ModuleInfo()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging":
                    info.logging_modules.add(alias.asname or "logging")
                if alias.name == "traceback":
                    info.traceback_modules.add(alias.asname or "traceback")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "traceback":
                for alias in node.names:
                    if alias.name in TRACEBACK_TEXT:
                        info.traceback_names.add(alias.asname or alias.name)
            if node.module == "logging":
                for alias in node.names:
                    if alias.name in LOG_METHODS:
                        info.loggers.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None and _is_get_logger(value, info):
                for target in targets:
                    name = chain(target)
                    if name:
                        info.loggers.add(name)
    return info


def is_log_call(call: ast.Call, info: ModuleInfo) -> bool:
    name, where = call_name(call), chain(call.func)
    if name in ("print", "_say"):
        return True
    if name == "write" and where.endswith(("stderr.write", "stdout.write")):
        return True
    if name in LOG_METHODS:
        receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
        if receiver is None:  # a bare ``warning(...)`` imported from logging
            return name in info.loggers or where in info.loggers
        root = chain(receiver)
        if root in LOGGER_NAMES or root in info.loggers or root.split(".")[0] in info.logging_modules:
            return True
        if _is_get_logger(receiver, info):
            return True
    return False


def is_traceback_text(call: ast.Call, info: ModuleInfo) -> bool:
    """A call that renders an exception's text and traceback (``print_exc``, ``format_exc`` and their aliases)."""
    name, where = call_name(call), chain(call.func)
    if where.split(".")[0] in info.traceback_modules and name in TRACEBACK_TEXT:
        return True
    return isinstance(call.func, ast.Name) and call.func.id in info.traceback_names


def is_network_call(call: ast.Call) -> bool:
    parts = chain(call.func).lower().split(".")
    named = any(part == marker or part.endswith("_" + marker) for part in parts for marker in NETWORK_NAMES)
    return named or call_name(call) in NETWORK_METHODS


def own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function body, not those of the functions and classes nested in it (each is scanned on its own)."""
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


BROAD_HANDLERS = {"", "Exception", "BaseException", "RuntimeError", "OSError", "IOError", "EnvironmentError"}


def tainted_handlers(function: ast.AST) -> list[ast.ExceptHandler]:
    """The ``except ... as name`` handlers whose exception may hold what an upstream was sent or said.

    A handler that names an HTTP error (``httpx.HTTPError``, a transport or upstream failure of ours) is one. So is a broad
    handler (``Exception``, ``RuntimeError``, ``OSError``, a bare ``except``) in a function that talks to an upstream
    anywhere: the call that raised can be an adapter or a helper whose name does not say so. A handler that names only
    narrower classes (``ValueError``, ``KeyError``, a module's own validation error) is not: those carry the gateway's own
    sentence about its own input.
    """
    nodes = list(own_nodes(function))
    talks = any(isinstance(inner, ast.Call) and is_network_call(inner) for inner in nodes)
    found: list[ast.ExceptHandler] = []
    for node in nodes:
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                kind = ast.unparse(handler.type) if handler.type is not None else ""
                http = any(marker in kind for marker in HTTP_ERROR_MARKERS)
                broad = bool({part.strip() for part in kind.strip("()").split(",")} & BROAD_HANDLERS)
                if handler.name and (http or (talks and broad)):
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


def body_sources(node: ast.AST, tainted: set[str]) -> bool:
    """Whether the expression holds a response body, subprocess output or text derived from them."""
    if isinstance(node, ast.Call) and call_name(node) in SANITIZERS:
        return False
    field_name = _field(node)
    if field_name and field_name.lower() not in ERROR_FIELDS:
        return False  # a named field of an upstream's JSON (an id, a name, a size) is a value, not the body
    if isinstance(node, ast.Attribute) and node.attr in BODY_ATTRS and _root(node) not in NOT_UPSTREAM | {"sys"}:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in BODY_CALLS \
            and _root(node.func.value) not in NOT_UPSTREAM:
        return True
    if isinstance(node, ast.Name) and node.id in tainted:
        return True
    return any(body_sources(child, tainted) for child in ast.iter_child_nodes(node))


TEXT_ATTRS = {"args", "detail", "message", "msg", "text", "reason", "description", "body", "response"}
TEXT_CALLS = {"str", "repr", "format", "ascii", "format_exception_only"}


def argument_violations(node: ast.AST, tainted: set[str], exceptions: set[str | None], *, bare_exception: bool = True,
                        in_text: bool = False) -> list[str]:
    """What in ``node`` is text an upstream wrote.

    A bare exception name counts when ``bare_exception`` (a logger formats whatever it is given) and, otherwise, only where it
    is turned into text: ``str(exc)``, ``repr(exc)``, an f-string, ``%``/``format``, ``exc.args`` and the text-like attributes.
    """
    if isinstance(node, ast.Call) and call_name(node) in SANITIZERS:
        return []
    if isinstance(node, ast.Call) and chain(node.func).split(".")[-1] in TRACEBACK_TEXT:
        return ["a traceback is rendered"]
    if isinstance(node, ast.Attribute) and node.attr in SAFE_ERROR_ATTRS:
        return []
    if isinstance(node, ast.Attribute) and node.attr in BODY_ATTRS and _root(node) not in NOT_UPSTREAM | {"sys"}:
        return [f"a response or process {node.attr}"]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in BODY_CALLS \
            and _root(node.func.value) not in NOT_UPSTREAM:
        return [f"a response or process {node.func.attr}()"]
    if isinstance(node, ast.Name) and node.id in tainted:
        return [f"{node.id}, derived from a response or process output"]
    if isinstance(node, ast.Name) and node.id in exceptions and (bare_exception or in_text):
        return [f"the text of {node.id}, an error from a network call"]
    if isinstance(node, ast.Attribute) and node.attr in TEXT_ATTRS and isinstance(node.value, ast.Name) \
            and node.value.id in exceptions:
        return [f"the {node.attr} of {node.value.id}, an error from a network call"]
    if isinstance(node, ast.Call) and call_name(node) == "type" and node.args and isinstance(node.args[0], ast.Name):
        return []
    text = in_text or isinstance(node, (ast.JoinedStr, ast.FormattedValue)) or (
        isinstance(node, ast.Call) and call_name(node) in TEXT_CALLS) or (
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod))
    out: list[str] = []
    for child in ast.iter_child_nodes(node):
        out.extend(argument_violations(child, tainted, exceptions, bare_exception=bare_exception, in_text=text))
    return out


def tainted_names(function: ast.AST, handlers: list[ast.ExceptHandler]) -> set[str]:
    """Names assigned (directly or through other names) from a body, a subprocess output or an exception's text."""
    # What a subprocess wrote is named for what it is, often after it crossed a function boundary (``run()`` returns it).
    tainted: set[str] = {"stderr", "stdout", "response_text"}
    nodes = list(own_nodes(function))
    changed = True
    while changed:
        changed = False
        for node in nodes:
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
            if value is None:
                continue
            inside = {handler.name for handler in handlers if handler.lineno <= getattr(node, "lineno", 0) <= (handler.end_lineno or handler.lineno)}
            if body_sources(value, tainted) or argument_violations(value, tainted, inside, bare_exception=False):
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and name.id not in tainted and not name.id.endswith(VALUE_SUFFIXES) \
                                and name.id not in ("id", "name", "width", "height"):
                            tainted.add(name.id)
                            changed = True
    return tainted


def sink_violations(function: ast.AST, info: ModuleInfo, is_sink: Callable[[ast.Call, ModuleInfo], bool],
                    *, traceback_is_sink: bool, bare_exception: bool = True) -> list[tuple[int, str]]:
    """``(line, what)`` for each call of ``function`` that passes tainted text to a sink."""
    handlers = tainted_handlers(function)
    tainted = tainted_names(function, handlers)
    found: list[tuple[int, str]] = []
    for node in own_nodes(function):
        if not isinstance(node, ast.Call):
            continue
        if traceback_is_sink and is_traceback_text(node, info):
            found.append((node.lineno, "a traceback is rendered"))
            continue
        if is_sink(node, info):
            exceptions = {handler.name for handler in handlers if handler.lineno <= node.lineno <= (handler.end_lineno or handler.lineno)}
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                found.extend((node.lineno, what) for what in argument_violations(argument, tainted, exceptions, bare_exception=bare_exception))
    return found


class Scan(ast.NodeVisitor):
    def __init__(self, relative: str, info: ModuleInfo, is_sink: Callable[[ast.Call, ModuleInfo], bool], *, traceback_is_sink: bool,
                 bare_exception: bool = True):
        self.relative, self.info, self.is_sink, self.traceback_is_sink = relative, info, is_sink, traceback_is_sink
        self.bare_exception = bare_exception
        self.stack: list[str] = []
        self.found: list[tuple[str, int, str]] = []

    def _function(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        qualified = f"{self.relative}::{'.'.join(self.stack)}"
        for line, what in sink_violations(node, self.info, self.is_sink, traceback_is_sink=self.traceback_is_sink,
                                          bare_exception=self.bare_exception):
            self.found.append((qualified, line, what))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


@functools.lru_cache(maxsize=1)
def gateway_modules() -> tuple[tuple[str, ast.Module], ...]:
    """Every production module: ``proxy.py`` and ``gateway/**/*.py``."""
    paths = [*sorted((ROOT / "gateway").rglob("*.py")), ROOT / "proxy.py"]
    return tuple((path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))) for path in paths)


def discover(is_sink: Callable[[ast.Call, ModuleInfo], bool], *, traceback_is_sink: bool,
             bare_exception: bool = True) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for relative, tree in gateway_modules():
        scan = Scan(relative, module_info(tree), is_sink, traceback_is_sink=traceback_is_sink, bare_exception=bare_exception)
        scan.visit(tree)
        found.extend(scan.found)
    return found


def scan_source(source: str, is_sink: Callable[[ast.Call, ModuleInfo], bool], *, traceback_is_sink: bool = True,
                bare_exception: bool = True) -> set[str]:
    """The function names of ``source`` that pass tainted text to a sink (the checker's own test)."""
    tree = ast.parse(source)
    scan = Scan("x.py", module_info(tree), is_sink, traceback_is_sink=traceback_is_sink, bare_exception=bare_exception)
    scan.visit(tree)
    return {key.split("::")[1] for key, _, _ in scan.found}
