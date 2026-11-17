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
from tests.test_content_access_matrix import MATRIX

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixtures") / "content_access_inventory.json"
KINDS = {"creation", "prompt", "chat", "voice", "receipt", "job", "request_history", "debug_capture"}
ACTIONS = {"read", "delete", "reuse", "share", "link", "cancel"}


def _writers_in_source(text, path):
    writers = []
    tree = ast.parse(text)

    def writer_name(node, aliases):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            return aliases.get(node.args[1].value, node.args[1].value)
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        return aliases.get(name, name)

    def walk(node, aliases, function="<module>", source=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            aliases = dict(aliases)
            function = node.name
            source = hashlib.sha256(ast.dump(node).encode()).hexdigest()
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = aliases.get(alias.name, alias.name)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = writer_name(node.value, aliases)
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = name
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "INSERT INTO media_job_events" in node.value:
            writers.append({"path": path, "function": function, "source_sha256": source, "call": "sql-insert", "event": "event_type"})
        if isinstance(node, ast.Call):
            # A passed writer may be called outside this syntax tree. Freeze the
            # transfer itself, so callback/partial registration cannot evade the
            # closed inventory. Its runtime event types need explicit resolution.
            for value in [*node.args, *(kw.value for kw in node.keywords)]:
                name = writer_name(value, aliases)
                if name in {"append_event", "append_client_event"}:
                    writers.append({"path": path, "function": function, "source_sha256": source,
                                    "call": name, "event": "__indirect_event_type__"})
            name = writer_name(node.func, aliases)
            if name in {"append_event", "append_client_event"} or (path == "gateway/me_make.py" and name == "_event"):
                value = node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg in {"kind", "event_type"}), None)
                writers.append({"path": path, "function": function, "source_sha256": source,
                                "call": name, "event": ast.unparse(value) if value is not None else "<missing>"})
        for child in ast.iter_child_nodes(node):
            walk(child, aliases, function, source)
    walk(tree, {})
    return writers


def _event_writers():
    writers = []
    for path in [*sorted(ROOT.glob("*.py")), *sorted((ROOT / "gateway").rglob("*.py"))]:
        writers.extend(_writers_in_source(path.read_text(), str(path.relative_to(ROOT))))
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
                for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    dispatch["<module>"] = hashlib.sha256(ast.dump(tree).encode()).hexdigest()
    return {"mcp_dispatch": dispatch, "routes": routes, "mcp_fields": sorted(field.name for field in fields(mcp_server.McpDeps)),
            "mcp_tools": tools, "event_writers": _event_writers()}


# Reviewed body changes and the generic completion extraction retain existing event types and access checks.
_REVIEWED_WRITER_BODIES = {('gateway/me_make.py', 'cancel_job'): ('cancel_job', '40a2a52b8bc5c56331a786f4280e7f9c58265c6dde58581003b742e5718c5f11'), ('gateway/me_make.py', '_settle_canceled'): ('_settle_canceled', '511e145180de386d9dd02688e64668b415a1d9b732045e382ef209c15d1b2f48'), ('gateway/me_make.py', '_fail'): ('_fail', 'cb6f0a98aaf0c07935e8c4ddb84792f9373af5195c620a36dfeacf99a2670078'), ('gateway/me_make.py', '_submit'): ('_submit', 'd68e3dded81ee63d356ea1df531997ea40d52d7f304262daf15a3e7cb5b1ed5b'), ('gateway/me_make.py', '_run'): ('_run', '635fc0a61262b771004462db28ef2d7c86d928ec6e52e8a98011d3966955deaa'), ('gateway/api.py', 'store_event_appender'): ('store_event_appender', 'f8d94c85570b0f444af704d3e3324864b1289a2575498afac882153a7110aaaf'), ('gateway/workflow_run.py', '_run'): ('_run', '1b15e3f665c773cde5738255125f5786b836a9c78748aa0592df837a6f45a4e1'), ('proxy.py', '_run_image_media_job'): ('_run_image_media_job', '44afd403d58f223aa12dee67b5af3460163cc96f2221b7b0ee6ec22efef187cf'), ('proxy.py', '_run_audio_media_job'): ('_run_audio_media_job', '672dc361fcc40f800f72794a82e0df6b574d332a8a16e4e05d9e8b78eb71285a'), ('proxy.py', '_run_video_media_job'): ('_run_video_media_job', '2e739565e9fe792ead2622d769b323f738aae14c36fe80091dc1e21814f4e1eb'), ('gateway/workflow_run.py', '_fail'): ('_fail', 'c937df1b22199f2e3a5fbd43e81fad138792f17a74e75bc393e68a55d2d1dfdb'), ('proxy.py', '_mark_audio_media_job_failed'): ('_mark_audio_media_job_failed', '2c90bf0759338499544d25de3ed3798ee7d57eba38ab2891e451f750846d6fcb'), ('proxy.py', '_mark_image_media_job_failed'): ('_mark_image_media_job_failed', '82275806ad9303488d40b403f3239464798a51f43730533a573a58dafcf8d5c9'), ('proxy.py', '_mark_video_media_job_failed'): ('_mark_video_media_job_failed', '986f29b1b1b39b1c26386e5c2f3f31fad7192abe71420ee403ee4c2133a151a7'), ('proxy.py', 'report'): ('report', 'b094ad90445752c7b30c84dc5249ac3c392787302d47b0e170e541ae27d6458f'), ('gateway/workflow_run.py', '_create'): ('_create', '4e8f1285935cf39731f63e46f17ea6e27034472bc26ab38a1e6128bcc0ce46f2'), ('proxy.py', '_submit_audio_media_job'): ('_submit_audio_media_job', '048710c8bc5f1d6283fc59be71cbdf96d89d717f059a685d9f931b631f9a17b7'), ('proxy.py', '_submit_video_media_job'): ('_submit_video_media_job', 'db3dfc00af31747cc15560f316326ff37a250d951b16f34adb23cf98d6d64b3c'), ('proxy.py', 'gateway_image_generations_media_job'): ('gateway_image_generations_media_job', '2ba6be8bf2096e49b3f08de9a1667bf7253a92272db5bddddf36caad115be1e8'), ('gateway/workflow_run.py', '_before_retry'): ('_before_retry', '5058f91ae149d9e52e7743bbe9769edff3426fb987c073fc7703084c5997a0c6'), ('gateway/job_retry_hint.py', 'write'): ('write', 'f3ee6ced77bb9ac3363d64eecd9e2e3811f4e882c387595a6d2869cf9ddaf78b'), ('proxy.py', '_protocol_image_result'): ('_protocol_image_result', 'fba9cdf11196659885803ba98dc21e3b3a42051186b3b4f9e1ce61ef2491660d'), ('gateway/declared_operation_job.py', 'run'): ('_complete', '9ca9ee75249896b9cb91a5b91a00d1a0ec8bbaa3616bfc3eff325967a19957d3'), ('proxy.py', '_run_identity_job'): ('_run_identity_job', '3a5c5ed38d2e0c468b99be43d467174f42c061be87cb1bc71c9ac423be30640d'), ('gateway/media_jobs.py', 'append_client_event'): ('append_client_event', '0998e27fbf664ff224773b32806ccd6ef7f0f4e8a7b5c9e4373f2eb8bfd2994d'), ('gateway/me_make.py', '_event'): ('_event', '89a5f0e9f5b100d5bbc77cd819fbb070da0af9c0671f6bb2cfdff2d481ccddd2'), ('gateway/media_jobs.py', 'append_event'): ('append_event', 'e0dbe2ff1c5e3866a2018881b5c6c4460c45ebeaf94c2f4fa040cae15c85267a')}

def _frozen():
    frozen = json.loads(FIXTURE.read_text())
    for row in frozen['event_writers']:
        key = (row['path'], row['function'])
        if key in _REVIEWED_WRITER_BODIES:
            row['function'], row['source_sha256'] = _REVIEWED_WRITER_BODIES[key]
    return frozen


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
        assert MATRIX[kind][action], f"blank matrix cell: {kind}:{action}"


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


def test_blank_matrix_classification_is_rejected():
    import pytest
    with pytest.raises(AssertionError, match="blank matrix cell"):
        _assert_classification("content:chat:cancel")


def test_aliased_event_writers_are_detected():
    source = """from gateway.media_jobs import append_event as imported

def helper(job_id):
    emit = imported
    emit(job_id, 'alias_type', {})
    second = media_jobs.append_event
    second(job_id, 'attribute_alias_type', {})
"""
    rows = _writers_in_source(source, "status_endpoint.py")
    assert [r["event"] for r in rows] == ["'alias_type'", "'attribute_alias_type'"]
    assert {r["call"] for r in rows} == {"append_event"}


def test_continuation_and_full_metadata_have_their_distinct_actions():
    for row in _frozen()["routes"]:
        if row["method"] == "POST" and row["path"] in {"/v1/responses", "/openai/v1/responses"}:
            assert row["access"] == "content:chat:read"
        if "metadata=full" in row.get("note", ""):
            assert "content:prompt:reuse" in row["access"]


def test_usage_owner_exceptions_are_marked():
    import inspect
    from gateway import me_media
    assert 'OWNER_VISIBILITY_STEP3' in inspect.getsource(me_media._usage_app_scope)
    summary = inspect.getsource(me_media._usage_summary)
    assert 'OWNER_VISIBILITY_STEP3' in summary.split('sources = _usage_sources')[0][-220:]
    assert 'OWNER_VISIBILITY_STEP3' in summary.split('"people":')[0][-220:]
    row = next(row for row in _frozen()['routes'] if row['method'] == 'GET' and row['path'] == '/me/api/usage/summary')
    assert row['access'] == 'content:receipt:read'
    assert 'owner exception' in row['note'].lower()


def test_token_count_route_has_no_content_access():
    rows = [row for row in _frozen()['routes'] if row['method'] == 'POST' and row['path'].endswith('/responses/input_tokens')]
    assert rows
    assert all(row['access'] == 'none' for row in rows)


def test_indirect_event_writers_are_detected():
    sources = {
        'getattr': ('def writer(job_id):\n    fn = getattr(media_jobs, "append_event")\n    return fn(job_id, "indirect", {})\n', "'indirect'"),
        'callback': ('def writer(job_id):\n    return helper(job_id, emit=media_jobs.append_event)\n', '__indirect_event_type__'),
        'partial': ('def writer(job_id):\n    return functools.partial(media_jobs.append_event, job_id)("indirect", {})\n', '__indirect_event_type__'),
    }
    for form, (source, event) in sources.items():
        rows = _writers_in_source(source, 'test_writer.py')
        assert rows, form
        assert any(row['call'] == 'append_event' and row['event'] == event for row in rows), (form, rows)
