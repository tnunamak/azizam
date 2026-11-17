"""Offline handler checks: no HTTP server, Codex process, or model invocation."""
import asyncio
import importlib.util
import io
import json
from pathlib import Path

import pytest


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend = load_fixture("codex_e2e_backend")
mcp = load_fixture("codex_e2e_mcp")
TOOLS = [{"type": "function", "function": {
    "name": "translated_probe",
    "description": "Namespace: mcp__azizam_e2e. Tool: namespace_probe.",
}}]
PATCH_OK = "Success. Updated the following files:\nA hello.txt\n"
SUCCESS = "Created hello.txt with apply_patch. Namespace tool path was available."


def call_result(call, content):
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


def chat(messages, monkeypatch, tools=TOOLS):
    monkeypatch.setattr(backend, "_record", lambda body: None)

    class Request:
        async def json(self):
            return {"messages": messages, "tools": tools}

    async def invoke():
        response = await backend.chat(Request())
        chunks = [chunk async for chunk in response.body_iterator]
        event = json.loads(chunks[0].decode().split("data: ", 1)[1])
        return event["choices"][0]["delta"]
    return asyncio.run(invoke())


def patch_history(monkeypatch, content=PATCH_OK):
    delta = chat([], monkeypatch)
    call = delta["tool_calls"][0]
    assert call["function"]["name"] == "apply_patch"
    return [{"role": "assistant", **delta}, call_result(call, content)]


def pair(monkeypatch, patch=PATCH_OK, namespace="azizam namespace probe"):
    history = patch_history(monkeypatch, patch)
    if patch != PATCH_OK:
        return history
    delta = chat(history, monkeypatch)
    call = delta["tool_calls"][0]
    assert call["function"]["name"] == "translated_probe"
    return history + [{"role": "assistant", **delta}, call_result(call, namespace)]


@pytest.mark.parametrize("params", [
    {"name": "wrong", "arguments": {}},
    {"name": "namespace_probe", "arguments": {"extra": True}},
    {"name": "namespace_probe", "arguments": []},
    {"name": "namespace_probe", "arguments": None},
])
def test_mcp_rejects_wrong_name_or_arguments(monkeypatch, params):
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(json.dumps({
        "id": 1, "method": "tools/call", "params": params,
    }) + "\n"))
    monkeypatch.setattr(mcp.sys, "stdout", output)
    mcp.main()
    response = json.loads(output.getvalue())
    assert "error" in response or response["result"].get("isError") is True
    assert "azizam namespace probe" not in output.getvalue()


def test_mcp_intended_empty_call(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(json.dumps({
        "id": 2, "method": "tools/call",
        "params": {"name": "namespace_probe", "arguments": {}},
    }) + "\n"))
    monkeypatch.setattr(mcp.sys, "stdout", output)
    mcp.main()
    assert json.loads(output.getvalue())["result"]["content"][0]["text"] == "azizam namespace probe"


@pytest.mark.parametrize("bad", ["Error: patch failed", json.dumps({"output": PATCH_OK, "metadata": {"exit_code": 1}})])
def test_failed_patch_never_succeeds(monkeypatch, bad):
    delta = chat(patch_history(monkeypatch, bad), monkeypatch)
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")
    assert SUCCESS not in delta["content"]


@pytest.mark.parametrize("bad", ["wrong marker", json.dumps({
    "isError": True, "content": [{"type": "text", "text": "azizam namespace probe"}],
})])
def test_failed_namespace_never_succeeds(monkeypatch, bad):
    delta = chat(pair(monkeypatch, namespace=bad), monkeypatch)
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")


def test_intended_pair_succeeds(monkeypatch):
    assert chat(pair(monkeypatch), monkeypatch)["content"] == SUCCESS


def test_two_unrelated_results_do_not_succeed(monkeypatch):
    delta = chat([
        {"role": "tool", "tool_call_id": "other1", "content": PATCH_OK},
        {"role": "tool", "tool_call_id": "other2", "content": "azizam namespace probe"},
    ], monkeypatch)
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")


def test_missing_namespace_does_not_issue_second_patch(monkeypatch):
    delta = chat(patch_history(monkeypatch), monkeypatch, tools=[])
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")


def test_duplicate_patch_results_are_not_a_pair(monkeypatch):
    history = patch_history(monkeypatch)
    assert chat(history + [history[-1]], monkeypatch)["content"].startswith("AZIZAM_E2E_FAILURE:")


@pytest.mark.parametrize("call_index, arguments", [
    pytest.param(0, json.dumps({"input": backend.PATCH.replace(
        "+hello", "+wrong bytes", 1,
    )}), id="wrong-patch"),
    pytest.param(0, "{", id="malformed-json"),
    pytest.param(0, None, id="null-arguments"),
    pytest.param(0, {}, id="non-string-arguments"),
    pytest.param(0, "[]", id="array-json"),
    pytest.param(0, "null", id="null-json"),
    pytest.param(0, '"patch"', id="string-json"),
    pytest.param(0, "1", id="number-json"),
    pytest.param(0, "true", id="boolean-json"),
    pytest.param(0, "{}", id="missing-input"),
    pytest.param(0, json.dumps({"input": backend.PATCH, "extra": True}), id="extra-input"),
    pytest.param(2, '{"extra": true}', id="nonempty-namespace"),
    pytest.param(2, "{", id="malformed-namespace"),
    pytest.param(2, "[]", id="array-namespace"),
])
def test_assistant_arguments_must_match_expected_input(monkeypatch, call_index, arguments):
    history = pair(monkeypatch)
    history[call_index]["tool_calls"][0]["function"]["arguments"] = arguments
    # Keep both successful result strings: only the assistant arguments change.
    assert history[1]["content"] == PATCH_OK
    assert history[3]["content"] == "azizam namespace probe"
    delta = chat(history, monkeypatch)
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")
    assert SUCCESS not in delta["content"]


@pytest.mark.parametrize("call_index", [
    pytest.param(0, id="patch"),
    pytest.param(2, id="namespace"),
])
def test_missing_assistant_arguments_fail(monkeypatch, call_index):
    history = pair(monkeypatch)
    del history[call_index]["tool_calls"][0]["function"]["arguments"]
    assert history[1]["content"] == PATCH_OK
    assert history[3]["content"] == "azizam namespace probe"
    assert chat(history, monkeypatch)["content"].startswith("AZIZAM_E2E_FAILURE:")


def test_json_wrapped_success_pair(monkeypatch):
    history = pair(monkeypatch)
    history[1]["content"] = json.dumps({"output": PATCH_OK, "metadata": {"exit_code": 0}})
    history[-1]["content"] = json.dumps({"content": [{"type": "text", "text": "azizam namespace probe"}], "isError": False})
    assert chat(history, monkeypatch)["content"] == SUCCESS
