"""Closed inventory, not a claim that today's surfaces enforce the final matrix.

Each method/route (including HEAD), MCP field/tool/suboperation and event writer
has an explicit classification. New entries fail until intentionally classified.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import fields
import json
import hashlib
from pathlib import Path

import proxy
import route_graph
from gateway import mcp_server

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixtures") / "content_access_inventory.json"
KINDS = {"creation", "prompt", "chat", "voice", "receipt", "job", "request_history", "debug_capture"}
ACTIONS = {"read", "delete", "reuse", "share", "link", "cancel"}


def _event_writers():
    writers = []
    for path in [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]:
        tree = ast.parse(path.read_text())
        def walk(node, function="<module>", source=""):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = node.name
                source = hashlib.sha256(ast.dump(node).encode()).hexdigest()
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "INSERT INTO media_job_events" in node.value:
                writers.append({"path": str(path.relative_to(ROOT)), "function": function,
                                "source_sha256": source, "call": "sql-insert", "event": "event_type"})
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in {"append_event", "append_client_event"} or (path.name == "me_make.py" and name == "_event"):
                    value = node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg in {"kind", "event_type"}), None)
                    writers.append({"path": str(path.relative_to(ROOT)), "function": function,
                                    "source_sha256": source, "call": name, "event": ast.unparse(value) if value is not None else "<missing>"})
            for child in ast.iter_child_nodes(node):
                walk(child, function, source)
        walk(tree)
    return sorted(writers, key=lambda row: json.dumps(row, sort_keys=True))


def _observed():
    routes = []
    for row in route_graph.routes(proxy.app):
        for method in row["methods"] or [row["type"].upper()]:
            routes.append({"method": method, "path": row["path"], "handler": row["endpoint"]})
    tools = []
    for tool in mcp_server.TOOLS:
        properties = tool["inputSchema"].get("properties", {})
        tools.append({"name": tool["name"], "parameters": sorted(properties),
                      "actions": properties.get("action", {}).get("enum", [])})
    tree = ast.parse(Path(mcp_server.__file__).read_text())
    dispatch = {node.name: hashlib.sha256(ast.dump(node).encode()).hexdigest()
                for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {"Tools", "_Tools", "call_tool", "handle_message", "_job_paths"}}
    return {"mcp_dispatch": dispatch, "routes": routes, "mcp_fields": sorted(field.name for field in fields(mcp_server.McpDeps)),
            "mcp_tools": tools, "event_writers": _event_writers()}


def _frozen():
    return json.loads(FIXTURE.read_text())


def test_every_route_callback_tool_and_event_writer_is_classified():
    observed = _observed()
    frozen = _frozen()
    for surface in ("routes", "mcp_tools", "event_writers"):
        expected = [{key: value for key, value in row.items() if key not in {"access", "operations", "note", "resolved_types"}}
                    for row in frozen.get(surface, [])]
        assert Counter(json.dumps(row, sort_keys=True) for row in observed[surface]) == Counter(
            json.dumps(row, sort_keys=True) for row in expected), surface
    assert observed["mcp_dispatch"] == frozen["mcp_dispatch"]
    assert set(observed["mcp_fields"]) == set(frozen["mcp_fields"])
    for surface in ("routes", "mcp_tools", "event_writers"):
        for row in frozen[surface]:
            _assert_classification(row["access"])
            for access in row.get("operations", {}).values():
                _assert_classification(access)
    for access in frozen["mcp_fields"].values():
        _assert_classification(access)


def _assert_classification(access):
    # A callback may compose several checks; all must be named explicitly.
    for part in access.split("+"):
        if part in {"operational", "own-account", "none"}:
            continue
        category, kind, action = part.split(":")
        assert category == "content" and kind in KINDS and action in ACTIONS


def test_job_event_types_have_a_closed_classified_inventory():
    frozen = _frozen()
    actual = set()
    for writer in _event_writers():
        value = ast.parse(writer["event"], mode="eval").body
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            actual.add(value.value)
        else:
            match = next(row for row in frozen["event_writers"] if all(row.get(k) == v for k, v in writer.items()))
            assert match.get("resolved_types"), writer
            actual.update(match["resolved_types"])
    # Client writers already permit only these two values. Trusted lifecycle
    # writers remain open at append_event until step 3; inventory is a guard,
    # not a change to the store's behavior.
    actual.update({"progress", "note"})
    assert actual == set(frozen["event_types"])
    for classification in frozen["event_types"].values():
        assert classification == "content:job:read"


def test_sensitive_newer_callbacks_and_ledger_routes_are_not_operational_content():
    frozen = _frozen()
    callbacks = frozen["mcp_fields"]
    assert callbacks["retention_ledger"] == "content:receipt:read+none"
    assert callbacks["stt_measurement"] == "operational"
    assert callbacks["rename"] == "own-account"
    for field in ("visible", "authorization_servers", "scopes", "owner_visible", "retention_visible"):
        assert callbacks[field] == "none"
    for path in ("/admin/api/requests/{request_id}/retention/ledger", "/admin/api/me/retention/ledger"):
        rows = [row for row in frozen["routes"] if row["method"] == "GET" and row["path"] == path]
        assert len(rows) == 1
        assert rows[0]["access"] == ("content:receipt:read" if "{request_id}" in path else "none")
