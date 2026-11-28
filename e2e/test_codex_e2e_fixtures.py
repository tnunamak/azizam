"""Offline handler checks: no HTTP server, Codex process, or model invocation."""
import asyncio
import importlib.util
import io
import json
import subprocess
import time
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


def test_text_result_preserves_json_scalar_looking_text():
    assert backend._result_text(" 123 ") == "123"
    assert backend._result_text(" null ") == "null"


def test_empty_text_is_distinct_from_an_absent_result():
    assert backend._result_text({"output": ""}) == ""
    assert backend._result_text({"content": [{"type": "text", "text": ""}]}) == ""
    assert backend._result_text(None) is None


def test_explicit_empty_output_is_not_replaced_by_secondary_content():
    assert backend._result_text({"output": "", "content": "secondary body"}) == ""


def test_unrecognized_envelope_fields_are_not_displayed_as_text():
    assert backend._result_text({"description": "fixture note", "value": "fixture value"}) is None


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


@pytest.mark.parametrize(("scenario", "finish", "model"), [
    ("initial", "tool_calls", None),
    ("completed", "stop", "fixture-model"),
    ("failed", "stop", "fixture-model"),
])
def test_backend_emits_terminal_stream_frame(monkeypatch, scenario, finish, model):
    messages = {"initial": lambda: [], "completed": lambda: pair(monkeypatch),
                "failed": lambda: patch_history(monkeypatch, "invented failure")}[scenario]()
    monkeypatch.setattr(backend, "_record", lambda body: None)

    class Request:
        async def json(self):
            return {"messages": messages, "tools": TOOLS, **({"model": model} if model is not None else {})}

    async def invoke():
        response = await backend.chat(Request())
        assert response.media_type == "text/event-stream"
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(invoke())
    assert len(chunks) == 3
    frames = [json.loads(chunk[6:]) for chunk in chunks[:2]]
    assert all(frame["object"] == "chat.completion.chunk" for frame in frames)
    assert frames[0]["id"] == frames[1]["id"]
    assert all(frame["model"] == (model or "azizam-e2e") for frame in frames)
    first, terminal = (frame["choices"][0] for frame in frames)
    assert first["finish_reason"] is None
    if scenario == "failed":
        assert first["delta"] == {
            "content": "AZIZAM_E2E_FAILURE: apply_patch did not report the expected successful file update",
        }
    assert terminal["delta"] == {}
    assert terminal["finish_reason"] == finish
    assert chunks[2] == b"data: [DONE]\n\n"


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


def test_mcp_reports_bad_frames_and_keeps_reading(monkeypatch):
    stdin = io.StringIO('{\n[]\n{"id":1,"method":"tools/list"}\n')
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", stdin)
    monkeypatch.setattr(mcp.sys, "stdout", stdout)
    monkeypatch.setattr(mcp.sys, "stderr", stderr)

    mcp.main()

    assert stderr.getvalue().splitlines() == [
        "azizam-e2e: malformed JSON frame",
        "azizam-e2e: expected an object frame",
    ]
    frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert frames[0] == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert len(frames) == 2
    assert frames[1]["id"] == 1
    assert frames[1]["result"]["tools"][0]["name"] == "namespace_probe"


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


def test_mcp_initialize_preserves_requested_protocol_version(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(json.dumps({
        "id": "init-1", "method": "initialize", "params": {"protocolVersion": "2024-11-05"},
    }) + "\n"))
    monkeypatch.setattr(mcp.sys, "stdout", output)
    mcp.main()

    response = json.loads(output.getvalue())
    assert response["id"] == "init-1"
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["capabilities"] == {"tools": {}}


def test_mcp_initialized_notification_does_not_add_a_response(monkeypatch):
    frames = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 17, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
    ]
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(
        "".join(json.dumps(frame) + "\n" for frame in frames),
    ))
    monkeypatch.setattr(mcp.sys, "stdout", output)

    mcp.main()

    response, = map(json.loads, output.getvalue().splitlines())
    assert response["id"] == 17


def test_mcp_initialize_without_version_advertises_fixture_defaults(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(
        '{"jsonrpc":"2.0","id":18,"method":"initialize"}\n',
    ))
    monkeypatch.setattr(mcp.sys, "stdout", output)

    mcp.main()

    assert json.loads(output.getvalue()) == {
        "jsonrpc": "2.0",
        "id": 18,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "azizam-e2e", "version": "1"},
        },
    }


def test_mcp_tool_listing_continues_after_non_json_line(monkeypatch):
    output = io.StringIO()
    request = json.dumps({"id": "list-1", "method": "tools/list"})
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO("not json\n" + request + "\n"))
    monkeypatch.setattr(mcp.sys, "stdout", output)
    mcp.main()

    parse_error, response = map(json.loads, output.getvalue().splitlines())
    assert parse_error["id"] is None
    assert parse_error["error"]["code"] == -32700
    assert response["id"] == "list-1"
    tool, = response["result"]["tools"]
    assert tool["name"] == "namespace_probe"
    assert tool["inputSchema"] == {"type": "object", "properties": {}, "additionalProperties": False}


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


def test_missing_patch_result_stops_without_a_namespace_call(monkeypatch):
    history = patch_history(monkeypatch)
    delta = chat(history[:1], monkeypatch)

    assert delta == {
        "content": "AZIZAM_E2E_FAILURE: apply_patch did not report the expected successful file update",
    }


def test_missing_namespace_result_stops_without_reporting_success(monkeypatch):
    history = pair(monkeypatch)
    delta = chat(history[:-1], monkeypatch)

    assert delta == {
        "content": "AZIZAM_E2E_FAILURE: namespace tool did not return its successful marker",
    }


def test_two_unrelated_results_do_not_succeed(monkeypatch):
    delta = chat([
        {"role": "tool", "tool_call_id": "other1", "content": PATCH_OK},
        {"role": "tool", "tool_call_id": "other2", "content": "azizam namespace probe"},
    ], monkeypatch)
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")


@pytest.mark.parametrize(("history_length", "expected_reason"), [
    (1, "apply_patch did not report the expected successful file update"),
    (3, "namespace tool did not return its successful marker"),
])
def test_tool_call_without_result_fails_at_its_own_stage(
    monkeypatch, history_length, expected_reason,
):
    history = pair(monkeypatch)[:history_length]
    assert chat(history, monkeypatch) == {
        "content": f"AZIZAM_E2E_FAILURE: {expected_reason}",
    }


def test_missing_namespace_does_not_issue_second_patch(monkeypatch):
    delta = chat(patch_history(monkeypatch), monkeypatch, tools=[])
    assert delta["content"].startswith("AZIZAM_E2E_FAILURE:")


def test_tool_result_before_its_call_cannot_satisfy_fixture(monkeypatch):
    assistant, result = patch_history(monkeypatch)
    delta = chat([result, assistant], monkeypatch)
    assert delta["content"] == "AZIZAM_E2E_FAILURE: unmatched or duplicate tool result"
    assert "tool_calls" not in delta


def test_namespace_marker_inside_mixed_error_content_is_not_success(monkeypatch):
    history = pair(monkeypatch, namespace=json.dumps({"content": [
        {"type": "text", "text": "azizam namespace probe"},
        {"isError": True, "type": "text", "text": "fixture reply failed"},
    ]}))
    delta = chat(history, monkeypatch)
    assert delta["content"] == "AZIZAM_E2E_FAILURE: namespace tool did not return its successful marker"
    assert SUCCESS not in delta["content"]


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


@pytest.mark.parametrize("transport_name", ["namespace_probe", "wrapped_fixture_probe"])
def test_backend_uses_advertised_transport_name_for_namespace_probe(transport_name):
    unrelated = {"type": "function", "function": {"name": "other_probe", "description": "Unrelated fixture."}}
    advertised = {"type": "function", "function": {
        "name": transport_name,
        "description": "Namespace: mcp__azizam_e2e. Tool: namespace_probe.",
    }}

    assert backend._namespace_function_name([unrelated, advertised]) == transport_name


def test_sse_encodes_one_event_and_preserves_multiline_unicode_payload():
    payload = {"answer": "line one\nline two: café", "sequence": 2}

    frame = backend._sse(payload)

    assert frame.endswith(b"\n\n")
    data, *trailing = frame.split(b"\n")
    assert trailing == [b"", b""]
    assert data.startswith(b"data: ")
    assert json.loads(data.removeprefix(b"data: ")) == payload


def test_result_text_preserves_order_of_multiple_plain_text_parts():
    assert backend._result_text([
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]) == "first\nsecond"


def test_mixed_result_parts_are_rejected_as_unreadable():
    messages = [
        {"role": "assistant", "tool_calls": [{
            "id": "call_apply_patch",
            "function": {"name": "apply_patch", "arguments": json.dumps({"input": backend.PATCH})},
        }]},
        {"role": "tool", "tool_call_id": "call_apply_patch", "content": [
            {"type": "text", "text": PATCH_OK},
            {"type": "unsupported-fixture-type", "text": "synthetic value"},
        ]},
    ]

    delta, finish = backend._decision(messages, "translated_probe")

    assert finish == "stop"
    assert delta == {"content": "AZIZAM_E2E_FAILURE: apply_patch did not report the expected successful file update"}


def test_namespace_result_after_patch_but_before_its_call_is_rejected():
    messages = [
        {"role": "assistant", "tool_calls": [{
            "id": "call_apply_patch",
            "function": {"name": "apply_patch", "arguments": json.dumps({"input": backend.PATCH})},
        }]},
        {"role": "tool", "tool_call_id": "call_apply_patch", "content": PATCH_OK},
        {"role": "tool", "tool_call_id": "call_namespace_probe", "content": "azizam namespace probe"},
    ]

    delta, finish = backend._decision(messages, "translated_probe")

    assert finish == "stop"
    assert delta == {"content": "AZIZAM_E2E_FAILURE: unmatched or duplicate tool result"}


@pytest.mark.parametrize("duplicate_index", [
    pytest.param(0, id="patch-call"),
    pytest.param(2, id="namespace-call"),
])
def test_duplicate_assistant_call_rejects_otherwise_complete_fixture_history(duplicate_index):
    history = [
        {"role": "assistant", "tool_calls": [{
            "id": "call_apply_patch",
            "function": {"name": "apply_patch", "arguments": json.dumps({"input": backend.PATCH})},
        }]},
        {"role": "tool", "tool_call_id": "call_apply_patch", "content": PATCH_OK},
        {"role": "assistant", "tool_calls": [{
            "id": "call_namespace_probe",
            "function": {"name": "translated_probe", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call_namespace_probe", "content": "azizam namespace probe"},
    ]

    assert backend._decision(history, "translated_probe") == ({"content": SUCCESS}, "stop")

    delta, finish = backend._decision([*history, history[duplicate_index]], "translated_probe")

    assert finish == "stop"
    assert delta == {"content": "AZIZAM_E2E_FAILURE: unexpected or duplicate tool call"}


WAIT_SH = Path(__file__).with_name("codex_e2e_wait.sh")


def run_wait_snippet(snippet):
    """Run a snippet with codex_e2e_wait.sh sourced, reporting the exit code."""
    return subprocess.run(
        ["bash", "-c", f"source \"{WAIT_SH}\"\n{snippet}"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_wait_ready_times_out_when_backend_never_answers():
    started = time.monotonic()
    result = run_wait_snippet("wait_ready http://127.0.0.1:9/health 1; echo \"rc=$?\"")
    assert "rc=1" in result.stdout
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("deadline", ["ten", "1.5", "-1", ""])
def test_wait_ready_rejects_non_numeric_deadline(deadline):
    started = time.monotonic()
    result = run_wait_snippet(
        f"wait_ready http://127.0.0.1:9/health '{deadline}'; echo \"rc=$?\""
    )
    assert "rc=2" in result.stdout
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("pid", ["", "0", "-1"])
def test_stop_group_refuses_empty_zero_or_negative_pid(pid):
    result = run_wait_snippet(f"stop_group '{pid}' 5; echo \"rc=$?\"")
    assert "rc=2" in result.stdout


def test_tool_result_with_non_string_id_cannot_satisfy_fixture():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_apply_patch",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({"input": backend.PATCH}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": 123, "content": PATCH_OK},
    ]
    delta, finish = backend._decision(messages, "translated_probe")
    assert finish == "stop"
    assert delta == {"content": "AZIZAM_E2E_FAILURE: unmatched or duplicate tool result"}
    assert SUCCESS not in delta["content"]


def test_mcp_unknown_method_with_id_returns_method_not_found(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(json.dumps({
        "id": 3, "method": "resources/list",
    }) + "\n"))
    monkeypatch.setattr(mcp.sys, "stdout", output)

    mcp.main()

    assert json.loads(output.getvalue()) == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32601, "message": "Method not found"},
    }


def test_mcp_unknown_method_notification_is_ignored(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(json.dumps({
        "method": "notifications/unknown",
    }) + "\n"))
    monkeypatch.setattr(mcp.sys, "stdout", output)

    mcp.main()

    assert output.getvalue() == ""
