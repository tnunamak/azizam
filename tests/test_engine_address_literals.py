"""Guard: no engine address is a literal in core code (code-to-data slice S8).

An engine's address comes from its service record (``service_registry.engine_origin``). This scan fails when
proxy.py, status_endpoint.py or a module under gateway/ spells one as code:

- a loopback host joined to a port, in an f-string or a plain string (``http://127.0.0.1:{port}``, ``"localhost:8880"``);
- ``.get("port", <int>)``: a port default;
- ``.get("host", "<loopback>")`` or ``<x>.get("host") or "<loopback>"``: a host default.

``ALLOWED`` is the short, reviewed list of sites that are not an engine address. Each entry names the file, the exact
code and the reason. A new literal that is not listed fails the test: move the value into the engine's record, or, if
it is truly not an engine, add a reviewed entry here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = [ROOT / "proxy.py", ROOT / "status_endpoint.py", *sorted((ROOT / "gateway").rglob("*.py"))]

_LOOPBACK = r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[?::1\]?)"
_HOST_PORT = re.compile(_LOOPBACK + r"(?::(?:\{|\d))")
_LOOPBACK_VALUES = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}

# (relative file, text that the site's ast.unparse contains) -> reason. None of these is an engine address.
ALLOWED: dict[tuple[str, str], str] = {
    ("proxy.py", "f'http://127.0.0.1:{PROXY_PORT}'"):
        "The gateway's own listen port: the default URL of its config CLI.",
    ("proxy.py", "'http://127.0.0.1:5000'"):
        "The gateway's own URL: the last fallback for the public base URL of a setup page.",
    ("proxy.py", "request.headers.get('host', '127.0.0.1')"):
        "The Host header of an inbound request, used as a request netloc. It is not an engine.",
    ("proxy.py", "url.get('netloc') or headers.get('host') or '127.0.0.1'"):
        "The netloc of an inbound request, rebuilt for a replayed job. It is not an engine.",
    ("gateway/backends/chat.py", "f'http://127.0.0.1:{proxy_port}/v1/chat/completions'"):
        "The gateway's own listen port: the in-process hop back into its own route.",
    ("gateway/backends/embeddings.py", "f'http://127.0.0.1:{proxy_port}/v1/embeddings'"):
        "The gateway's own listen port: the in-process hop back into its own route.",
    ("gateway/litellm_substrate.py", "f'http://127.0.0.1:{own_listen_port()}/internal/litellm/local'"):
        "The gateway's own listen port: the substrate's callback into the gateway.",
    ("gateway/litellm_substrate.py", "f'http://127.0.0.1:{own_listen_port()}/internal/litellm/current/v1'"):
        "The gateway's own listen port: the substrate's callback into the gateway.",
    ("proxy.py", "config.get('proxy', {}).get('host', '127.0.0.1')"):
        "The gateway's own bind host: the default of its `proxy` config section, not an engine.",
    ("proxy.py", "config.get('proxy', {}).get('port', 5000)"):
        "The gateway's own listen port: the default of its `proxy` config section, not an engine.",
    ("gateway/litellm_substrate.py", "is not this gateway's own listener"):
        "An operator message that names the gateway's own listener.",
    ("gateway/setup_render.py", "'http://127.0.0.1:5000'"):
        "The gateway's own URL: the default public base of the setup page.",
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _is_loopback_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() in _LOOPBACK_VALUES


def _get_key(node: ast.AST) -> str | None:
    """'host' or 'port' when *node* is ``x.get("host"|"port"[, ...])`` or ``x["host"|"port"]``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        key = node.args[0]
        if isinstance(key, ast.Constant) and key.value in ("host", "port"):
            return str(key.value)
    return None


def literal_sites(source: str) -> list[tuple[int, str]]:
    """(line, normalized code) of every engine-address literal in *source*."""
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    sites: list[tuple[int, str]] = []
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                if child is not node:
                    inside_fstring.add(id(child))
            # A formatted value stands for "{": "127.0.0.1:{port}" reads as a host joined to a port.
            raw = "".join(str(p.value) if isinstance(p, ast.Constant) else "{" for p in node.values)
            if _HOST_PORT.search(raw):
                sites.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or id(node) in inside_fstring:
                continue
            if _HOST_PORT.search(node.value) and not node.value.lstrip().startswith(("#", "//")):
                if len(node.value) < 200:
                    sites.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Call) and _get_key(node) is not None:
            key = _get_key(node)
            default = node.args[1] if len(node.args) > 1 else None
            if key == "port" and isinstance(default, ast.Constant) and isinstance(default.value, int) \
                    and not isinstance(default.value, bool):
                sites.append((node.lineno, ast.unparse(node)))
            if key == "host" and default is not None and _is_loopback_constant(default):
                sites.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(_is_loopback_constant(v) for v in node.values[1:]) and any(
                _get_key(v) == "host" or (isinstance(v, ast.BoolOp) and any(_get_key(w) == "host" for w in v.values))
                for v in node.values[:-1]
            ):
                sites.append((node.lineno, ast.unparse(node)))
    return sites


def _is_allowed(relative: str, text: str) -> bool:
    return any(relative == file and fragment in text for (file, fragment) in ALLOWED)


def _unreviewed(path: Path, source: str) -> list[str]:
    relative = path.relative_to(ROOT).as_posix()
    found = []
    for line, text in literal_sites(source):
        if _is_allowed(relative, text):
            continue
        found.append(f"{relative}:{line}: {text}")
    return found


def test_no_engine_address_literal_in_core_code():
    problems: list[str] = []
    for path in SCANNED:
        problems += _unreviewed(path, path.read_text(encoding="utf-8"))
    assert not problems, (
        "An engine address is code here. Read it from the engine's service record with "
        "service_registry.engine_origin (an engine whose record names none is not configured), "
        "or, if this is not an engine, add a reviewed entry to ALLOWED:\n" + "\n".join(problems)
    )


def test_allowed_entries_are_all_still_used():
    seen = [
        (path.relative_to(ROOT).as_posix(), text)
        for path in SCANNED for _, text in literal_sites(path.read_text(encoding="utf-8"))
    ]
    stale = [key for key in ALLOWED if not any(file == key[0] and key[1] in text for file, text in seen)]
    assert not stale, f"ALLOWED lists a site that is gone; delete the entry: {stale}"


def test_guard_sees_each_kind_of_literal():
    cases = [
        'x = f"http://127.0.0.1:{port}"',
        'x = "http://localhost:8880/health"',
        'x = cfg.get("port", 8880)',
        'x = cfg.get("host", "127.0.0.1")',
        'x = str(cfg.get("host") or "127.0.0.1")',
    ]
    for source in cases:
        assert literal_sites(source), source
    for source in ['x = cfg.get("port")', 'x = cfg.get("host")', 'x = frozenset({"127.0.0.1", "::1"})',
                   'def f():\n    """Calls 127.0.0.1:5000."""']:
        assert not literal_sites(source), source
