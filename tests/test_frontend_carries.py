"""The loss report on the shipped frontends (contract section 5).

The anthropic and ollama rows declare ``carries``: the request fields their translators handle. A
path names a nested field (``messages[].role``, ``options.temperature``) where the translator flattens
a container; a name with no nested path carries everything under it. The lists come from the translator
code: the first test reads the fields the code reads, and each listed field must round-trip. ``exact``
refuses an invalid or non-object JSON body and a nested field it cannot carry; multipart rows declare
that they are not loss-checked.
"""

from __future__ import annotations

import ast
import inspect
import json
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import proxy
from gateway import frontend_loss, frontend_mount, route_registry
from gateway.protocols import anthropic, ollama

SEED = {record["id"]: record for record in route_registry.protocol_frontend_records(proxy_config=proxy.config)}


def _carries(frontend: str, canonical: str) -> set[str]:
    [route] = [item for item in SEED[frontend]["registered_routes"] if item.get("canonical") == canonical]
    assert "carries" in route, f"{frontend} {canonical} declares no carries"
    return set(route["carries"])


def _top_level(paths: set[str]) -> set[str]:
    return {re.split(r"[.\[]", path)[0] for path in paths}


def _fields_read(*functions) -> set[str]:
    """The literal keys the functions read from their ``body`` argument: body.get("x"), body["x"], "x" in body."""
    keys: set[str] = set()
    for function in functions:
        for node in ast.walk(ast.parse(inspect.getsource(function))):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "body"
                and node.args and isinstance(node.args[0], ast.Constant)
            ):
                keys.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "body"
                and isinstance(node.slice, ast.Constant)
            ):
                keys.add(node.slice.value)
    return keys


# --- the lists come from the translator code ---------------------------------------------------

def test_the_anthropic_messages_row_carries_every_field_the_translator_reads():
    read = _fields_read(anthropic.messages_request_to_canonical)
    assert read >= {"model", "messages", "max_tokens", "system", "tools", "metadata", "thinking"}
    assert _top_level(_carries("anthropic", "/v1/messages")) == read


def test_the_anthropic_count_tokens_row_carries_what_the_estimator_reads():
    carried = _top_level(_carries("anthropic", "/v1/messages/count_tokens"))
    assert carried == {"model", "messages", "system", "tools", "tool_choice", "thinking", "output_config"}


def test_the_ollama_rows_carry_every_field_their_translators_read():
    chat = _fields_read(ollama.chat_request_to_canonical, ollama._stream_flag, ollama.thinking_requested)
    generate = _fields_read(ollama.generate_request_to_canonical, ollama._stream_flag, ollama.thinking_requested)
    # The load answer in proxy.py (_ollama_load_response) reads keep_alive from either body.
    assert _top_level(_carries("ollama", "/api/chat")) == chat | {"keep_alive"}
    assert _top_level(_carries("ollama", "/api/generate")) == generate | {"keep_alive"}
    assert _top_level(_carries("ollama", "/api/embed")) == _fields_read(ollama.embed_request_to_canonical)
    assert _top_level(_carries("ollama", "/api/embeddings")) == _fields_read(ollama.embeddings_request_to_canonical)


def test_the_ollama_options_the_row_carries_are_the_ones_the_translator_maps():
    carried = {path.split(".", 1)[1] for path in _carries("ollama", "/api/chat") if path.startswith("options.")}
    assert carried == set(ollama._OLLAMA_TO_OPENAI_OPTIONS) | {"num_predict"}


def test_the_frontends_that_declare_carries_say_lenient_so_nothing_a_client_sends_is_newly_refused():
    assert SEED["anthropic"]["strictness"] == "lenient"
    assert SEED["ollama"]["strictness"] == "lenient"


# --- each listed field round-trips --------------------------------------------------------------

ANTHROPIC_BASE = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}

ANTHROPIC_CASES = {
    "model": (lambda b: b, lambda c: c["model"] == "m"),
    "max_tokens": (lambda b: b, lambda c: c["max_tokens"] == 5),
    "stream": (lambda b: {**b, "stream": True}, lambda c: c["stream"] is True),
    "temperature": (lambda b: {**b, "temperature": 0.3}, lambda c: c["temperature"] == 0.3),
    "top_p": (lambda b: {**b, "top_p": 0.5}, lambda c: c["top_p"] == 0.5),
    "top_k": (lambda b: {**b, "top_k": 7}, lambda c: c["top_k"] == 7),
    "stop_sequences": (lambda b: {**b, "stop_sequences": ["x"]}, lambda c: c["stop"] == ["x"]),
    "system": (lambda b: {**b, "system": "S"}, lambda c: c["messages"][0] == {"role": "system", "content": "S"}),
    "messages[].role": (
        lambda b: {**b, "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                     {"role": "user", "content": "c"}]},
        lambda c: [m["role"] for m in c["messages"]] == ["user", "assistant", "user"],
    ),
    "messages[].content": (
        lambda b: {**b, "messages": [{"role": "user", "content": [{"type": "text", "text": "blocks"}]}]},
        lambda c: "blocks" in json.dumps(c["messages"]),
    ),
    "tools[].type": (
        lambda b: {**b, "tools": [{"type": "custom", "name": "t", "input_schema": {"type": "object"}}]},
        lambda c: c["tools"][0]["type"] == "function",
    ),
    "tools[].name": (
        lambda b: {**b, "tools": [{"name": "t"}]}, lambda c: c["tools"][0]["function"]["name"] == "t"),
    "tools[].description": (
        lambda b: {**b, "tools": [{"name": "t", "description": "d"}]},
        lambda c: c["tools"][0]["function"]["description"] == "d",
    ),
    "tools[].input_schema": (
        lambda b: {**b, "tools": [{"name": "t", "input_schema": {"type": "object", "title": "s"}}]},
        lambda c: c["tools"][0]["function"]["parameters"]["title"] == "s",
    ),
    "tool_choice.type": (
        lambda b: {**b, "tool_choice": {"type": "any"}}, lambda c: c["tool_choice"] == "required"),
    "tool_choice.name": (
        lambda b: {**b, "tool_choice": {"type": "tool", "name": "t"}},
        lambda c: c["tool_choice"]["function"]["name"] == "t",
    ),
    "tool_choice.disable_parallel_tool_use": (
        lambda b: {**b, "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}},
        lambda c: c["parallel_tool_calls"] is False,
    ),
    "thinking.type": (
        lambda b: {**b, "thinking": {"type": "enabled", "budget_tokens": 10000}},
        lambda c: c["reasoning_effort"] == "high",
    ),
    "thinking.budget_tokens": (
        lambda b: {**b, "thinking": {"type": "enabled", "budget_tokens": 1000}},
        lambda c: c["reasoning_effort"] == "low",
    ),
    "output_config.effort": (
        lambda b: {**b, "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}},
        lambda c: c["reasoning_effort"] == "low",
    ),
    "metadata.user_id": (
        lambda b: {**b, "metadata": {"user_id": "u1"}}, lambda c: c["user"] == "u1"),
}


@pytest.mark.parametrize("path", sorted(ANTHROPIC_CASES))
def test_each_field_the_anthropic_row_carries_reaches_the_canonical_request(path):
    mutate, reached = ANTHROPIC_CASES[path]
    assert reached(anthropic.messages_request_to_canonical(mutate(dict(ANTHROPIC_BASE)))), path


def test_the_anthropic_round_trip_cases_are_exactly_the_messages_carries():
    assert set(ANTHROPIC_CASES) == _carries("anthropic", "/v1/messages")


OLLAMA_CHAT_BASE = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
OLLAMA_GENERATE_BASE = {"model": "m", "prompt": "hi"}
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _option_case(name: str, value):
    target = "max_tokens" if name == "num_predict" else ollama._OLLAMA_TO_OPENAI_OPTIONS[name]
    return (lambda b: {**b, "options": {name: value}}, lambda c: c[target] == value)


OLLAMA_COMMON = {
    "model": (lambda b: b, lambda c: c["model"] == "m"),
    "stream": (lambda b: {**b, "stream": False}, lambda c: c["stream"] is False),
    "format": (lambda b: {**b, "format": "json"}, lambda c: c["response_format"] == {"type": "json_object"}),
    "think": (lambda b: {**b, "think": "high"}, lambda c: c["reasoning_effort"] == "high"),
    **{f"options.{name}": _option_case(name, 5 if name in {"num_predict", "top_k", "seed", "repeat_last_n", "mirostat"}
                                       else 0.5)
       for name in [*ollama._OLLAMA_TO_OPENAI_OPTIONS, "num_predict"] if name != "stop"},
    "options.stop": _option_case("stop", ["x"]),
}

OLLAMA_CHAT_CASES = {
    **OLLAMA_COMMON,
    "messages[].role": (
        lambda b: {**b, "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]},
        lambda c: [m["role"] for m in c["messages"]] == ["system", "user"],
    ),
    "messages[].content": (lambda b: b, lambda c: c["messages"][0]["content"] == "hi"),
    "messages[].images": (
        lambda b: {**b, "messages": [{"role": "user", "content": "hi", "images": [PNG]}]},
        lambda c: c["messages"][0]["content"][1]["type"] == "image_url",
    ),
    "messages[].tool_calls": (
        lambda b: {**b, "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}]},
            {"role": "user", "content": "go"}]},
        lambda c: c["messages"][0]["tool_calls"][0]["function"]["name"] == "f",
    ),
    "messages[].tool_name": (
        lambda b: {**b, "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {}}}]},
            {"role": "tool", "content": "r", "tool_name": "f"}]},
        lambda c: c["messages"][1]["tool_call_id"] == c["messages"][0]["tool_calls"][0]["id"],
    ),
    "tools[].type": (
        lambda b: {**b, "tools": [{"type": "function", "function": {"name": "f"}}]},
        lambda c: c["tools"][0]["type"] == "function",
    ),
    "tools[].function": (
        lambda b: {**b, "tools": [{"type": "function", "function": {"name": "f", "description": "d"}}]},
        lambda c: c["tools"][0]["function"]["description"] == "d",
    ),
    "keep_alive": (
        lambda b: {**b, "messages": [], "keep_alive": 0},
        lambda c: ollama.load_response("m", endpoint="chat", keep_alive=0)["done_reason"] == "unload",
    ),
}

OLLAMA_GENERATE_CASES = {
    **OLLAMA_COMMON,
    "prompt": (lambda b: b, lambda c: c["messages"][-1]["content"] == "hi"),
    "system": (lambda b: {**b, "system": "S"}, lambda c: c["messages"][0] == {"role": "system", "content": "S"}),
    "images": (
        lambda b: {**b, "images": [PNG]}, lambda c: c["messages"][-1]["content"][1]["type"] == "image_url"),
    "raw": (lambda b: {**b, "raw": True}, lambda c: c["__ollama_raw__"] is True),
    "suffix": (lambda b: {**b, "suffix": "s"}, None),
    "template": (lambda b: {**b, "template": "t"}, None),
    "keep_alive": (
        lambda b: {**b, "prompt": "", "keep_alive": "0s"},
        lambda c: ollama.load_response("m", endpoint="generate", keep_alive="0s")["done_reason"] == "unload",
    ),
}


def _run(translator, body: dict, case):
    mutate, reached = case
    sent = mutate(dict(body))
    if reached is None:  # a field the translator refuses by name: it must still reach the translator
        with pytest.raises(ollama.OllamaTranslationError, match="unsupported"):
            translator(sent)
        return True
    if sent.get("messages") == [] or sent.get("prompt") == "":
        assert ollama.is_load_request(sent, endpoint="chat" if "messages" in sent else "generate")
        return reached(None)
    return reached(translator(sent))


@pytest.mark.parametrize("path", sorted(OLLAMA_CHAT_CASES))
def test_each_field_the_ollama_chat_row_carries_reaches_the_canonical_request(path):
    assert _run(ollama.chat_request_to_canonical, OLLAMA_CHAT_BASE, OLLAMA_CHAT_CASES[path]), path


@pytest.mark.parametrize("path", sorted(OLLAMA_GENERATE_CASES))
def test_each_field_the_ollama_generate_row_carries_reaches_the_canonical_request(path):
    assert _run(ollama.generate_request_to_canonical, OLLAMA_GENERATE_BASE, OLLAMA_GENERATE_CASES[path]), path


def test_the_ollama_embedding_rows_carry_what_they_translate():
    embed = ollama.embed_request_to_canonical({"model": "m", "input": ["a"], "dimensions": 8})
    assert embed == {"model": "m", "input": ["a"], "dimensions": 8}
    assert ollama.embeddings_request_to_canonical({"model": "m", "prompt": "p"}) == {"model": "m", "input": "p"}
    assert _carries("ollama", "/api/embed") == {"model", "input", "dimensions"}
    assert _carries("ollama", "/api/embeddings") == {"model", "prompt"}


def test_the_ollama_round_trip_cases_are_exactly_the_chat_and_generate_carries():
    assert set(OLLAMA_CHAT_CASES) == _carries("ollama", "/api/chat")
    assert set(OLLAMA_GENERATE_CASES) == _carries("ollama", "/api/generate")


# --- the same row carries on the alias path -----------------------------------------------------

def test_every_row_that_declares_carries_mounts_with_them_on_its_canonical_path_and_its_alias():
    rows = frontend_mount.rows_from_frontends([SEED["anthropic"], SEED["ollama"]])
    carrying = [row for row in rows if row.carries is not None]
    assert {row.path for row in carrying} >= {
        "/v1/messages", "/anthropic/v1/messages", "/v1/messages/count_tokens", "/anthropic/v1/messages/count_tokens",
        "/api/chat", "/ollama/api/chat", "/api/generate", "/ollama/api/generate",
        "/api/embed", "/ollama/api/embed", "/api/embeddings", "/ollama/api/embeddings",
    }


# --- exact and nested fields ----------------------------------------------------------------------

class Seen:
    def __init__(self) -> None:
        self.bodies: list = []

    async def handler(self, request: Request):
        try:
            self.bodies.append(await request.json())
        except ValueError:
            return JSONResponse({"error": "the handler could not read the body"}, status_code=400)
        return {"ok": True}


def _mount(seen: Seen, *, strictness: str, carries: list[str] | None, body: str = "json") -> TestClient:
    app = FastAPI()
    translators = frontend_mount.Translators({
        "vendor-v1": {"chat.completions": frontend_mount.Operation(seen.handler, frozenset({"POST"}),
                                                                   self_authenticates=True)},
    })
    route = {"methods": ["POST"], "canonical": "/vendor/v1/chat", "intent": "chat.completions",
             "translator": "vendor-v1", "body": body}
    if carries is not None:
        route["carries"] = carries
    rows = frontend_mount.rows_from_frontends([
        {"id": "vendor", "enabled": True, "source": "builtin", "strictness": strictness, "registered_routes": [route]},
    ])
    frontend_mount.mount(app, rows, translators)
    return TestClient(app)


NESTED = ["model", "messages[].role", "messages[].content", "metadata.user_id"]
NESTED_BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "metadata": {"user_id": "u"}}


def test_an_exact_frontend_refuses_a_body_that_is_not_json_or_not_an_object():
    seen = Seen()
    client = _mount(seen, strictness="exact", carries=NESTED)
    for content in (b"{not json", b"", b"[1, 2]", b'"text"', b"null", b"42"):
        response = client.post("/vendor/v1/chat", content=content, headers={"content-type": "application/json"})
        assert response.status_code == 400, (content, response.text)
        assert response.json()["error"]["code"] == "frontend_body_invalid"
        assert "x-azizam-loss" in response.headers
    multipart = client.post("/vendor/v1/chat", files={"file": ("a.txt", b"x")})
    assert multipart.status_code == 400
    assert multipart.json()["error"]["code"] == "frontend_body_invalid"
    assert seen.bodies == []


def test_a_lenient_frontend_hands_a_body_it_cannot_read_to_the_handler_and_says_it_was_not_checked():
    seen = Seen()
    client = _mount(seen, strictness="lenient", carries=NESTED)

    response = client.post("/vendor/v1/chat", content=b"{not json", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"error": "the handler could not read the body"}
    assert json.loads(response.headers["x-azizam-loss"])["unchecked"]


def test_an_exact_frontend_refuses_a_nested_field_the_translator_flattens_away():
    seen = Seen()
    client = _mount(seen, strictness="exact", carries=NESTED)
    body = {**NESTED_BODY, "messages": [{"role": "user", "content": "hi", "name": "bob"}],
            "metadata": {"user_id": "u", "session": "s"}}

    response = client.post("/vendor/v1/chat", json=body)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "frontend_field_unsupported"
    assert json.loads(response.headers["x-azizam-loss"])["dropped"] == ["messages[].name", "metadata.session"]
    assert seen.bodies == []


def test_a_lenient_frontend_strips_a_nested_field_and_reports_its_path():
    seen = Seen()
    client = _mount(seen, strictness="lenient", carries=NESTED)
    body = {**NESTED_BODY, "messages": [{"role": "user", "content": "hi", "name": "bob"}],
            "metadata": {"user_id": "u", "session": "s"}, "extra": 1}

    response = client.post("/vendor/v1/chat", json=body)

    assert response.status_code == 200, response.text
    assert seen.bodies == [NESTED_BODY]
    assert json.loads(response.headers["x-azizam-loss"])["dropped"] == ["messages[].name", "metadata.session", "extra"]


def test_a_name_with_no_nested_path_carries_everything_under_it():
    seen = Seen()
    client = _mount(seen, strictness="exact", carries=["model", "options"])
    body = {"model": "m", "options": {"anything": {"at": ["all"]}}}

    assert client.post("/vendor/v1/chat", json=body).status_code == 200
    assert seen.bodies == [body]


def test_a_carries_path_is_checked_down_a_list_of_objects():
    node = frontend_loss.Carries(["messages[].role"])
    kept, lost = node.split({"messages": [{"role": "a", "x": 1}, {"role": "b"}], "other": 1})
    assert kept == {"messages": [{"role": "a"}, {"role": "b"}]}
    assert lost == ["messages[].x", "other"]


# --- multipart rows --------------------------------------------------------------------------------

def test_a_multipart_row_declares_it_is_not_loss_checked_and_the_response_says_so():
    async def handler(request: Request):
        return {"ok": True}

    app = FastAPI()
    translators = frontend_mount.Translators({
        "vendor-v1": {"images.edit": frontend_mount.Operation(handler, frozenset({"POST"}), self_authenticates=True)},
    })
    rows = frontend_mount.rows_from_frontends([{
        "id": "vendor", "enabled": True, "source": "builtin", "strictness": "exact",
        "registered_routes": [{"methods": ["POST"], "canonical": "/vendor/v1/edit", "intent": "images.edit",
                               "translator": "vendor-v1", "body": "multipart"}],
    }])
    frontend_mount.mount(app, rows, translators)

    response = TestClient(app).post("/vendor/v1/edit", files={"image": ("a.png", b"x")})

    assert response.status_code == 200
    assert json.loads(response.headers["x-azizam-loss"]) == {
        "dropped": [], "approximated": [], "unchecked": "multipart body"}


def test_a_multipart_row_cannot_also_declare_carries():
    with pytest.raises(frontend_mount.FrontendMountError, match="multipart"):
        _mount(Seen(), strictness="exact", carries=["model"], body="multipart")


def test_a_row_names_its_body_as_json_or_multipart():
    with pytest.raises(frontend_mount.FrontendMountError, match="body"):
        _mount(Seen(), strictness="exact", carries=None, body="xml")


def test_the_seed_marks_exactly_the_routes_that_read_a_multipart_body():
    multipart = {
        (record["id"], route["canonical"])
        for record in SEED.values() for route in record.get("registered_routes") or []
        if route.get("body") == "multipart"
    }
    assert multipart == {
        ("openai", "/v1/audio/transcriptions"), ("openai", "/v1/audio/translations"),
        ("openai", "/v1/images/edits"), ("openai", "/v1/images/variations"),
        ("openai", "/openai/v1/images/variations"),
    }
    for record in SEED.values():
        for route in record.get("registered_routes") or []:
            if route.get("body") == "multipart":
                assert route["translator"] == "identity" and "carries" not in route
