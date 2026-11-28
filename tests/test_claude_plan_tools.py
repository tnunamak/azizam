"""Caller tools cross real subprocess and loopback MCP boundaries."""
import asyncio
import json

import pytest

from gateway.protocols import claude_code_runner as runner, mcp_park
from tests.test_claude_plan_runner import (env, connect, dispatch, body_of, runs, run, MODEL, alive)

TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


def body(**extra):
    return {"model": MODEL, "messages": [{"role": "user", "content": "look it up"}], "tools": TOOLS, **extra}


def test_parallel_loop(env):
    connect(env)
    env.scenario(tool_count=2)
    async def check():
        try:
            first = body_of(await dispatch(env, body()))
            message = first["choices"][0]["message"]
            assert first["choices"][0]["finish_reason"] == "tool_calls"
            assert [c["id"] for c in message["tool_calls"]] == ["toolu_0", "toolu_1"]
            assert all(c["function"]["name"] == "lookup" for c in message["tool_calls"])
            assert runner.RUNNER.running("claude") == 0
            follow = body()
            follow["messages"] += [message, *[{"role": "tool", "tool_call_id": f"toolu_{i}", "content": str(i)} for i in range(2)]]
            final = body_of(await dispatch(env, follow))
            assert "0|1" in final["choices"][0]["message"]["content"]
            assert len(runs(env)) == 1
            assert not alive(runs(env)[0]["pid"])
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


@pytest.mark.parametrize("extra,code", [
    ({"tool_choice": "required"}, "plan_tool_choice_unsupported"),
    ({"parallel_tool_calls": False}, "plan_parallel_tools_required"),
    ({"tools": [{"type": "web_search"}]}, "plan_tool_type_unsupported"),
    ({"tools": [{"type": "function", "function": {"name": "x", "strict": True}}]}, "plan_strict_tools_unsupported"),
])
def test_named_refusals(env, extra, code):
    connect(env)
    response = run(dispatch(env, body(**extra)))
    assert response.status_code == 400
    assert body_of(response)["error"]["code"] == code


def follow_up(message, count=1):
    follow = body()
    follow["messages"] += [message, *[{"role": "tool", "tool_call_id": f"toolu_{i}", "content": str(i)} for i in range(count)]]
    return follow


@pytest.mark.parametrize("change", ["history", "tools", "model", "effort"])
def test_changed_request_rebuilds(env, change):
    connect(env)
    async def check():
        try:
            first = body_of(await dispatch(env, body()))["choices"][0]["message"]
            pid = runs(env)[0]["pid"]
            follow = follow_up(first)
            if change == "history":
                follow["messages"][0]["content"] = "edited"
            elif change == "tools":
                follow["tools"] = [{"type": "function", "function": {"name": "other"}}]
            elif change == "model":
                follow["model"] = "claude-test-haiku"
            else:
                follow["reasoning_effort"] = "high"
            out = await dispatch(env, follow)
            assert out.status_code == 200
            assert len(runs(env)) == 2
            assert not alive(pid)
            assert "toolu_0" in json.dumps(runs(env)[1]["stdin"])
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_missing_results(env):
    connect(env)
    env.scenario(tool_count=2)
    async def check():
        try:
            first = body_of(await dispatch(env, body()))["choices"][0]["message"]
            result = await dispatch(env, follow_up(first))
            assert result.status_code == 400, result.body
            assert "toolu_1" in result.body.decode()
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


@pytest.mark.parametrize("scenario", [{"tools": ["mcp__caller__lookup", "Bash"]},
    {"mcp_servers": [{"name": "caller", "status": "connected"}, {"name": "other", "status": "connected"}]}])
def test_tool_init_fails_closed(env, scenario):
    connect(env)
    env.scenario(**scenario)
    async def check():
        try:
            result = await dispatch(env, body())
            assert result.status_code == 502
            assert not alive(runs(env)[0]["pid"])
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_none_keeps_original_lockdown(env):
    connect(env)
    result = run(dispatch(env, body(tool_choice="none")))
    assert result.status_code == 200
    argv = runs(env)[0]["argv"]
    assert "--disallowedTools" in argv and "--mcp-config" not in argv


def test_responses_loop(env):
    connect(env)
    request = {"model": MODEL, "input": "look it up", "tools": [{"type": "function", **TOOLS[0]["function"]}],
               "store": False, "include": ["reasoning.encrypted_content"], "prompt_cache_key": "test-session",
               "reasoning": {"effort": "high", "summary": "auto"}}
    async def check():
        try:
            first = body_of(await dispatch(env, request, surface="responses"))
            call = next(c for c in first["output"] if c["type"] == "function_call")
            assert call["call_id"] == "toolu_0" and call["name"] == "lookup"
            follow = {**request, "input": [{"role": "user", "content": "look it up"}, call,
                      {"type": "function_call_output", "call_id": "toolu_0", "output": "found"}]}
            final = body_of(await dispatch(env, follow, surface="responses"))
            assert "found" in json.dumps(final["output"])
            assert len(runs(env)) == 1
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_messages_loop_through_shared_decoders(env):
    from gateway.protocols import anthropic
    connect(env)
    request = {"model": MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "look it up"}],
               "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}
    async def check():
        try:
            first = anthropic.canonical_response_to_messages(body_of(await dispatch(env, anthropic.messages_request_to_canonical(request))))
            assert first["stop_reason"] == "tool_use"
            call = next(c for c in first["content"] if c["type"] == "tool_use")
            assert call["id"] == "toolu_0"
            follow = {**request, "messages": [*request["messages"], {"role": "assistant", "content": first["content"]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "found"}]}]}
            final = anthropic.canonical_response_to_messages(body_of(await dispatch(env, anthropic.messages_request_to_canonical(follow))))
            assert final["stop_reason"] == "end_turn"
            assert "found" in json.dumps(final["content"])
            assert len(runs(env)) == 1
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


@pytest.mark.parametrize("ending", ["ttl", "principal_cap", "connection_cap", "exit"])
def test_park_endings_release_process_and_config(env, ending):
    from pathlib import Path
    from tests.test_claude_plan_runner import with_runner
    connect(env)
    settings = {"max_concurrent": 1}
    if ending == "ttl":
        settings["park_ttl_seconds"] = 0.1
    elif ending == "principal_cap":
        settings["park_limit_per_principal"] = 1
    elif ending == "connection_cap":
        settings["park_limit_per_connection"] = 1
    else:
        env.scenario(exit_parked=True)
    env.scenario(park_child=True, exit_parked=ending == "exit")
    kind = with_runner(**settings)
    async def check():
        try:
            assert (await dispatch(env, body(prompt_cache_key="one"), kind=kind)).status_code == 200
            pid = runs(env)[0]["pid"]
            child = json.loads((env.home / "park-child.json").read_text())["pid"]
            argv = runs(env)[0]["argv"]
            config = Path(argv[argv.index("--mcp-config") + 1])
            assert runner.RUNNER.running("claude") == 0
            if "cap" in ending:
                assert config.stat().st_mode & 0o777 == 0o600
                assert (await dispatch(env, body(prompt_cache_key="two"), kind=kind)).status_code == 200
            else:
                for _ in range(100):
                    if not alive(pid) and not config.exists():
                        break
                    await asyncio.sleep(0.02)
            assert not alive(pid)
            assert not config.exists()
            for _ in range(100):
                if not alive(child):
                    break
                await asyncio.sleep(0.01)
            assert not alive(child)
            assert runner.RUNNER.running("claude") == 0
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
        assert not runner.RUNNER._parks
    run(check())


@pytest.mark.parametrize("extra,code", [
    ({"store": True}, "plan_store_unsupported"),
    ({"include": ["other"]}, "plan_include_unsupported"),
    ({"tool_choice": {"type": "function", "name": "lookup"}}, "plan_tool_choice_unsupported"),
])
def test_responses_named_refusals(env, extra, code):
    connect(env)
    request = {"model": MODEL, "input": "hi", **extra}
    out = run(dispatch(env, request, surface="responses"))
    assert out.status_code == 400
    assert body_of(out)["error"]["code"] == code


def test_streaming_argument_deltas(env):
    from tests.test_claude_plan_runner import read_stream
    connect(env)
    async def check():
        try:
            response = await dispatch(env, body(stream=True))
            stream = await read_stream(response)
            chunks = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: {")]
            calls = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
            assert calls[0]["id"] == "toolu_0"
            assert "".join(c.get("function", {}).get("arguments", "") for c in calls) == '{"x":0}'
            assert runner.RUNNER.running("claude") == 0
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


@pytest.mark.parametrize("content", ["text", [{"type": "text", "text": "parts"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]])
def test_tool_result_content(env, content):
    connect(env)
    async def check():
        try:
            first = body_of(await dispatch(env, body()))["choices"][0]["message"]
            follow = follow_up(first)
            follow["messages"][-1]["content"] = content
            final = body_of(await dispatch(env, follow))
            answer = final["choices"][0]["message"]["content"]
            assert ("parts[image]" if isinstance(content, list) else "text") in answer
            assert len(runs(env)) == 1
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


@pytest.mark.parametrize("change", ["unknown", "extra", "affinity"])
def test_bad_result_ids_or_other_session_cannot_resume(env, change):
    connect(env)
    async def check():
        try:
            first = body_of(await dispatch(env, body(prompt_cache_key="one")))["choices"][0]["message"]
            follow = follow_up(first)
            follow["prompt_cache_key"] = "one"
            if change == "unknown":
                follow["messages"][-1]["tool_call_id"] = "toolu_unknown"
            elif change == "extra":
                follow["messages"].append({"role": "tool", "tool_call_id": "toolu_unknown", "content": "extra"})
            else:
                follow["prompt_cache_key"] = "two"
            out = await dispatch(env, follow)
            assert out.status_code == 200
            assert len(runs(env)) == 2
            if change != "affinity":
                assert not alive(runs(env)[0]["pid"])
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_disconnect_mid_continuation(env):
    connect(env)
    env.scenario(delay=1)
    async def check():
        try:
            first = body_of(await dispatch(env, body()))["choices"][0]["message"]
            pid = runs(env)[0]["pid"]
            gone = asyncio.Event()
            task = asyncio.create_task(dispatch(env, follow_up(first), disconnected=gone))
            for _ in range(100):
                if runner.RUNNER.running("claude"):
                    break
                await asyncio.sleep(0.01)
            gone.set()
            assert (await task).status_code == 499
            assert not alive(pid)
            assert runner.RUNNER.running("claude") == 0
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_two_tool_rounds_keep_one_process(env):
    connect(env)
    env.scenario(tool_rounds=2)
    async def check():
        try:
            request = body()
            for i in range(2):
                answer = body_of(await dispatch(env, request))["choices"][0]["message"]
                call = answer["tool_calls"][0]
                request["messages"] += [answer, {"role": "tool", "tool_call_id": call["id"], "content": f"round {i}"}]
                assert runner.RUNNER.running("claude") == 0
            final = body_of(await dispatch(env, request))
            assert "round 1" in final["choices"][0]["message"]["content"]
            assert len(runs(env)) == 1
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_each_tool_round_reports_its_own_call_usage(env):
    # The binary's result reports the whole turn; each step must report only the API call it made.
    calls = [[100, 10, 1000, 5], [120, 0, 1110, 6], [130, 0, 1230, 7]]
    connect(env)
    env.scenario(tool_rounds=2, call_usage=calls)
    async def check():
        try:
            request, seen = body(), []
            for i in range(3):
                reply = body_of(await dispatch(env, request))
                seen.append(reply["usage"])
                if i < 2:
                    answer = reply["choices"][0]["message"]
                    request["messages"] += [answer, {"role": "tool", "tool_call_id": answer["tool_calls"][0]["id"],
                                                     "content": f"round {i}"}]
            assert [(u["prompt_tokens"], u["prompt_tokens_details"]["cached_tokens"], u["completion_tokens"],
                     u["completion_tokens_details"]["reasoning_tokens"]) for u in seen] == [(f + w + r, r, o, 1) for f, w, r, o in calls]
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_lost_park_discloses_rebuilt_in_responses(env):
    connect(env)
    async def check():
        try:
            await dispatch(env, body())
            await runner.RUNNER.close_parks()
            request = {"model": MODEL, "input": [{"role": "user", "content": "look it up"},
                {"type": "function_call", "call_id": "toolu_0", "name": "lookup", "arguments": '{"x":0}'},
                {"type": "function_call_output", "call_id": "toolu_0", "output": "already ran"}]}
            out = body_of(await dispatch(env, request, surface="responses"))
            assert out["metadata"]["plan_turn_start"] == "rebuilt"
            assert len(runs(env)) == 2
            assert "already ran" in json.dumps(runs(env)[1]["stdin"])
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_timeout_budget_restarts_after_a_park(env):
    from tests.test_claude_plan_runner import with_runner
    connect(env)
    kind = with_runner(timeout_seconds=1)
    async def check():
        try:
            first = body_of(await dispatch(env, body(), kind=kind))["choices"][0]["message"]
            await asyncio.sleep(1.1)
            out = await dispatch(env, follow_up(first), kind=kind)
            assert out.status_code == 200
            assert len(runs(env)) == 1
            assert runner.RUNNER.running("claude") == 0
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_a_lost_park_request_leaves_other_conversations_parked(env):
    # Two conversations of one caller share a park scope when neither sends a cache key; a request whose
    # results match no park (its own park was lost) must rebuild without ending the other conversation's park.
    connect(env)
    async def check():
        try:
            other = body()
            other["messages"][0]["content"] = "another task"
            await dispatch(env, other)
            parked_pid = runs(env)[0]["pid"]
            request = {"model": MODEL, "messages": [{"role": "user", "content": "look it up"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "toolu_lost", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "toolu_lost", "content": "already ran"}], "tools": TOOLS}
            out = await dispatch(env, request)
            assert out.status_code == 200
            assert alive(parked_pid)
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())


def test_a_tool_run_keeps_the_lockdown_and_loads_only_the_callers_server(env):
    connect(env)
    async def check():
        try:
            await dispatch(env, body())
            record = runs(env)[0]
            argv = record["argv"]
            def after(flag):
                return argv[argv.index(flag) + 1]
            assert "--disallowedTools" not in argv
            assert after("--tools") == "" and after("--setting-sources") == ""
            assert after("--permission-mode") == "dontAsk" and "--strict-mcp-config" in argv
            assert after("--allowedTools") == "mcp__caller__*"
            servers = json.loads(open(after("--mcp-config")).read())["mcpServers"]
            assert list(servers) == ["caller"] and servers["caller"]["url"].startswith("http://127.0.0.1:")
            assert record["env"]["ENABLE_TOOL_SEARCH"] == "false"
            assert record["env"]["MCP_TOOL_TIMEOUT"] == str(1800 * 1000)
        finally:
            await runner.RUNNER.close_parks()
            await mcp_park.close()
    run(check())
