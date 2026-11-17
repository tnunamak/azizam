"""Run the frozen fd30cbfca video handler against a test's current I/O ports."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import FunctionType
from functools import lru_cache

import proxy
from tests.fixtures.video_main_fd30cbfca.gateway import video_grid as video_grid_fd30cbfca

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE = ROOT / "video_fd30cbfca.py"


@lru_cache(maxsize=1)
def checked_helpers():
    source = Path(proxy.__file__).read_text()
    lines = source.splitlines(keepends=True)
    found = {node.name: hashlib.sha256("".join(lines[node.lineno - 1:node.end_lineno]).rstrip("\n").encode()).hexdigest()
             for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    pinned = json.loads(SOURCE.with_suffix(".json").read_text())["unchanged_proxy_helpers"]
    assert {name: found.get(name) for name in pinned} == pinned, "a main-oracle helper changed; freeze its main body"


def checked_source():
    checked_helpers()
    source = SOURCE.read_text()
    pinned = json.loads(SOURCE.with_suffix(".json").read_text())
    lines = source.splitlines(keepends=True)
    found = {node.name: hashlib.sha256("".join(lines[node.lineno - 1:node.end_lineno]).rstrip("\n").encode()).hexdigest()
             for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert found == pinned["functions"], "the main handler oracle was edited"
    assert hashlib.sha256((ROOT / "video_main_fd30cbfca/gateway/video_grid.py").read_bytes()).hexdigest() == pinned["grid_sha256"]
    for name, digest in pinned["registry"].items():
        assert hashlib.sha256((ROOT / "video_main_fd30cbfca/registry" / name).read_bytes()).hexdigest() == digest
    return source


def namespace():
    # Bind unchanged proxy functions to this namespace too: their calls to
    # _video_workflow_table must reach main's reader, never the branch reader.
    values = dict(vars(proxy))
    for name, value in tuple(values.items()):
        if isinstance(value, FunctionType) and value.__module__ == "proxy":
            frozen = FunctionType(value.__code__, values, value.__name__, value.__defaults__, value.__closure__)
            frozen.__kwdefaults__ = value.__kwdefaults__
            frozen.__dict__.update(value.__dict__)
            values[name] = frozen
    values["video_grid"] = video_grid_fd30cbfca
    values["_seconds_to_length"] = video_grid_fd30cbfca.seconds_to_length
    exec(compile(checked_source(), str(SOURCE), "exec"), values)
    return values


async def create(request, caller, *, body_override):
    return await namespace()["proxy_graph_video_create"](request, caller, body_override=body_override)


async def chain_route(request, caller, body):
    """Main's public chain hand-off, including route policy before the receipt."""
    return await namespace()["_legacy_video_create"](request, caller, body)


async def status(video_id, caller):
    return await namespace()["proxy_graph_video_status"](video_id, caller)
