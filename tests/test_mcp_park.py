"""The private MCP transport, exercised through real loopback HTTP connections."""
import asyncio
import json

import httpx
import pytest

from gateway.protocols.mcp_park import MAX_BODY_BYTES, ParkServer

pytest_plugins = ["anyio.pytest_plugin"]
pytestmark = pytest.mark.usefixtures("anyio_backend")


def tool(name, **fields):
    return {"type": "function", "function": {"name": name, **fields}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def rig():
    server = ParkServer()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    run = await server.register([tool("first", description="one", parameters=schema), tool("second")])
    config = run.mcp_config(660000)["mcpServers"]["caller"]
    async with httpx.AsyncClient(timeout=2) as client:
        yield server, run, client, config
    await server.close()


async def rpc(rig, method, params=None, **kwargs):
    _, _, client, config = rig
    return await client.post(config["url"], headers=config["headers"],
                             json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kwargs)


def call_params(call_id, name="first", arguments=None):
    return {"name": name, "arguments": arguments or {}, "_meta": {"claudecode/toolUseId": call_id}}


async def test_protocol_and_order(rig):
    _, run, _, config = rig
    assert config["type"] == "http" and config["timeout"] == 660000
    response = await rpc(rig, "initialize", {"protocolVersion": "2025-03-26"})
    assert response.headers["content-type"] == "application/json"
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"]
    tools = (await rpc(rig, "tools/list")).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["first", "second"]
    assert [t["description"] for t in tools] == ["one", ""]
    assert tools[0]["inputSchema"] == {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    assert tools[1]["inputSchema"] == {"type": "object"}
    for item in tools:
        assert item["annotations"] == {"readOnlyHint": True}
        assert item["_meta"] == {"anthropic/maxResultSizeChars": 2000000, "anthropic/alwaysLoad": True}
    assert (await rpc(rig, "ping")).json()["result"] == {}
    assert (await rpc(rig, "unknown")).json()["error"]["code"] == -32601
    notification = await rpc(rig, "notifications/initialized")
    assert notification.status_code == 202 and notification.content == b""
    assert run.to_caller_name("mcp__caller__first") == "first"


async def test_names_are_short_stable_and_reversible(rig):
    server, _, _, _ = rig
    names = ["a" * 51, "a" * 52, "a" * 51 + "b", "odd.name", "odd/name", "odd_name"]
    runs = [await server.register([tool(n) for n in names]) for _ in range(2)]
    mapped = [t["name"] for t in runs[0].tools]
    assert mapped == [t["name"] for t in runs[1].tools]
    assert mapped[0] == names[0] and len(set(mapped)) == len(names)
    for original, name in zip(names, mapped):
        assert len(name) <= 51
        assert all(c.isascii() and (c.isalnum() or c in "_-") for c in name)
        assert runs[0].to_caller_name(name) == original
        assert runs[0].to_caller_name("mcp__caller__" + name) == original


async def test_http_boundaries(rig):
    server, _, client, config = rig
    other = await server.register([])
    for headers in ({}, {"Authorization": "Bearer wrong"}, other.mcp_config(1)["mcpServers"]["caller"]["headers"]):
        assert (await client.post(config["url"], headers=headers, json={})).status_code == 401
    assert (await client.post(config["url"] + "missing", json={})).status_code == 404
    assert (await client.get(config["url"])).status_code == 404
    assert (await client.post(config["url"] + "/extra", json={})).status_code == 404
    assert (await client.post(config["url"], headers=config["headers"], content=b"x" * (MAX_BODY_BYTES + 1))).status_code == 413
    assert (await client.post(config["url"], headers=config["headers"], content=b"{")).status_code == 400


async def test_parallel_calls_and_other_requests_progress(rig):
    _, run, _, _ = rig
    tasks = [asyncio.create_task(rpc(rig, "tools/call", call_params(i))) for i in ("a", "b")]
    arrived = [await asyncio.wait_for(run.calls.get(), 1) for _ in tasks]
    assert {c[0] for c in arrived} == {"a", "b"}
    assert all(c[1:] == ("first", {}) for c in arrived)
    assert not any(t.done() for t in tasks)
    assert (await rpc(rig, "ping")).status_code == 200
    run.resolve("b", "second result")
    assert (await tasks[1]).json()["result"]["content"] == [{"type": "text", "text": "second result"}]
    assert not tasks[0].done()
    run.resolve("a", "first result")
    assert (await tasks[0]).json()["result"]["content"][0]["text"] == "first result"


async def test_early_and_duplicate_results(rig):
    _, run, _, _ = rig
    assert run.resolve("early", "first")
    assert not run.resolve("early", "duplicate")
    result = (await rpc(rig, "tools/call", call_params("early"))).json()["result"]
    assert result == {"content": [{"type": "text", "text": "first"}], "isError": False}
    assert not run.resolve("early", "late duplicate")
    run.resolve("never arrives", "orphan")
    run.close()
    assert not run.resolve("after close", "ignored")


async def test_fallback_uses_oldest_matching_expectation(rig):
    _, run, _, _ = rig
    run.expect("different", "first", {"x": 2})
    run.expect("oldest", "first", {"a": 1, "b": 2})
    run.expect("newest", "first", {"b": 2, "a": 1})
    for expected in ("oldest", "newest"):
        task = asyncio.create_task(rpc(rig, "tools/call", {"name": "first", "arguments": {"b": 2, "a": 1}}))
        assert (await asyncio.wait_for(run.calls.get(), 1))[0] == expected
        run.resolve(expected, "ok")
        assert not (await task).json()["result"]["isError"]
    unmatched = (await rpc(rig, "tools/call", {"name": "first", "arguments": {"x": 3}})).json()["result"]
    assert unmatched["isError"]


async def test_content_mapping(rig):
    _, run, _, _ = rig
    run.resolve("image", [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {
        "url": "data:image/png;base64,YQ=="}}, {"type": "other", "value": 3}], is_error=True)
    result = (await rpc(rig, "tools/call", call_params("image"))).json()["result"]
    assert result["isError"]
    assert result["content"][:2] == [{"type": "text", "text": "look"}, {"type": "image", "data": "YQ==", "mimeType": "image/png"}]
    assert result["content"][2]["type"] == "text"
    run.resolve("object", {"key": 3})
    result = (await rpc(rig, "tools/call", call_params("object"))).json()["result"]
    assert json.loads(result["content"][0]["text"]) == {"key": 3}


@pytest.mark.parametrize("action", ["fail_all", "close", "server_close"])
async def test_shutdown_releases_all_waiters(rig, action):
    server, run, _, config = rig
    tasks = [asyncio.create_task(rpc(rig, "tools/call", call_params(i))) for i in ("a", "b")]
    for _ in tasks:
        await asyncio.wait_for(run.calls.get(), 1)
    if action == "fail_all":
        run.fail_all("stopped")
    elif action == "close":
        run.close()
        assert (await rig[2].post(config["url"], json={})).status_code == 404
    else:
        await server.close()
    for task in tasks:
        assert (await task).json()["result"]["isError"]


async def test_disconnect_drops_parked_task(rig):
    server, run, _, config = rig
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    try:
        # Cancellation closes httpx's in-flight TCP connection.
        task = asyncio.create_task(rpc(rig, "tools/call", call_params("gone")))
        await asyncio.wait_for(run.calls.get(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(server.wait_idle(), 1)
        assert run.pending_count == 0
        assert not any(t.get_coro().__qualname__ == "ParkServer._dispatch" for t in asyncio.all_tasks() if not t.done())
        assert not run.resolve("gone", "too late")
        assert errors == []
    finally:
        loop.set_exception_handler(previous)


async def test_keep_alive_reuses_connection(rig):
    _, _, _, config = rig
    ports = []
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1), timeout=2) as client:
        for method in ("notifications/initialized", "ping", "tools/list"):
            response = await client.post(config["url"], headers=config["headers"], json={"id": 1, "method": method})
            assert response.status_code in (200, 202)
            ports.append(response.extensions["network_stream"].get_extra_info("client_addr"))
    assert ports[0] is not None and ports[0] == ports[1] == ports[2]


async def test_alias_reserves_caller_names_and_calls_round_trip(rig):
    server, _, client, _ = rig
    first = await server.register([tool("odd.name")])
    reserved = first.tools[0]["name"]
    names = ["odd.name", reserved, "mcp__caller__first", "first"]
    run = await server.register([tool(n) for n in names])
    config = run.mcp_config(1000)["mcpServers"]["caller"]
    assert len({t["name"] for t in run.tools}) == len(names)
    for name, item in zip(names, run.tools):
        assert run.to_caller_name("mcp__caller__" + item["name"]) == name
        run.resolve(name, "ok")
        response = await client.post(config["url"], headers=config["headers"],
                                     json={"id": 2, "method": "tools/call", "params": call_params(name, item["name"])})
        assert not response.json()["result"]["isError"]
        assert (await asyncio.wait_for(run.calls.get(), 1)) == (name, name, {})


async def test_chunked_oversize_does_not_break_keep_alive(rig):
    _, _, client, config = rig

    async def chunks():
        for _ in range(MAX_BODY_BYTES // 65536 + 1):
            yield b"x" * 65536

    response = await client.post(config["url"], headers=config["headers"], content=chunks())
    assert response.status_code == 413
    assert (await rpc(rig, "ping")).status_code == 200


async def test_duplicate_call_and_consumed_expectation(rig):
    _, run, _, _ = rig
    run.expect("one", "first", {})
    task = asyncio.create_task(rpc(rig, "tools/call", call_params("one")))
    await asyncio.wait_for(run.calls.get(), 1)
    duplicate = (await rpc(rig, "tools/call", call_params("one"))).json()["result"]
    assert duplicate["isError"] and not task.done()
    unmatched = (await rpc(rig, "tools/call", {"name": "first"})).json()["result"]
    assert unmatched["isError"]
    run.resolve("one", "original")
    assert (await task).json()["result"]["content"][0]["text"] == "original"
    run.fail_all("ended")
    assert not run.resolve("late", "ignored")
    assert (await rpc(rig, "tools/call", call_params("late"))).json()["result"]["isError"]


async def test_malformed_image_content_becomes_text(rig):
    _, run, _, _ = rig
    content = [{"type": "image_url", "image_url": {"url": 123}}]
    run.resolve("malformed", content)
    result = (await rpc(rig, "tools/call", call_params("malformed"))).json()["result"]
    assert result["content"] == [{"type": "text", "text": json.dumps(content[0], sort_keys=True, separators=(",", ":"))}]


async def test_process_server_registers_concurrently_and_restarts():
    from gateway.protocols import mcp_park

    try:
        runs = await asyncio.gather(mcp_park.register([]), mcp_park.register([]))
        configs = [run.mcp_config(1)["mcpServers"]["caller"] for run in runs]
        assert configs[0]["url"].split("/mcp/")[0] == configs[1]["url"].split("/mcp/")[0]
        assert configs[0]["url"] != configs[1]["url"]
        assert configs[0]["headers"] != configs[1]["headers"]
        await mcp_park.close()
        run = await mcp_park.register([])
        async with httpx.AsyncClient(timeout=2) as client:
            config = run.mcp_config(1)["mcpServers"]["caller"]
            response = await client.post(config["url"], headers=config["headers"], json={"id": 1, "method": "ping"})
            assert response.status_code == 200
    finally:
        await mcp_park.close()
