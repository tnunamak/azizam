"""The Claude plan through the gateway: the cli-sign-in mechanism and the claude-code-runner protocol.

Every run starts a fake binary (tests/support/fake_claude.py) that speaks the real binary's stream-json, so no test
needs a sign-in or the network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import admin_catalog, admin_store, model_routes, receipts, registry_snapshot, service_registry
from gateway import subscription_kinds, subscription_oauth, subscription_store, transport_budgets
from gateway.alias_policy import resolve_request_target
from gateway.connections import cli_sign_in
from gateway.protocols import claude_code_runner
from gateway.provider_connections import SubscriptionError
from tests.support import access as acc
from tests.support.fake_upstream.route_dispatch import request_for

KIND = subscription_kinds.load_kinds()["claude"]
FAKE = Path(__file__).resolve().parent / "support" / "fake_claude.py"
SECRET = "sk-ant-oat01-" + "Q" * 40  # token-shaped: must never leave the config home
EMAIL = "owner@private.test"
MODEL = "claude-test-opus"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _receipts_table():
    from gateway import receipt_store
    receipt_store.init_db()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bin_dir, homes = tmp_path / "bin", tmp_path / "homes"
    bin_dir.mkdir()
    wrapper = bin_dir / "claude"
    wrapper.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
    wrapper.chmod(0o755)
    home = homes / "claude-code" / "owner"
    home.mkdir(parents=True)
    (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": SECRET}}))
    monkeypatch.setenv(cli_sign_in.HOMES_ENV, str(homes))
    monkeypatch.setenv(cli_sign_in.BIN_DIR_ENV, str(bin_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "Z" * 30)  # the gateway's own environment
    claude_code_runner.RUNNER.forget_sessions()
    claude_code_runner.RUNNER._running.clear()
    store = subscription_store.SubscriptionStore(lambda: tmp_path / "subscriptions.db")
    service = subscription_oauth.SubscriptionService(store, {"claude": KIND})
    return SimpleNamespace(home=home, service=service, store=store, tmp=tmp_path,
                           scenario=lambda **plan: (home / "scenario.json").write_text(json.dumps(plan)))


def calls(env) -> list[dict]:
    path = env.home / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def runs(env) -> list[dict]:
    return [call for call in calls(env) if "-p" in call["argv"] and any(
        line.get("type") == "user" for line in call.get("stdin", []))]


def connect(env, owner="owner_1"):
    return run(env.service.start(owner, "claude", session=None))


def granted(**identity):
    plan = acc.plan_for("plan_granted", connections=["claude-owner"])
    return acc.identity(plan, **identity), acc.access(plan)


async def dispatch(env, body, *, surface="chat", identity=None, access=None, kind=KIND, disconnected=None):
    sid = kind.model_prefix
    connection_id = env.store.connection_id(kind.id)
    env.service.kinds = {"claude": kind}
    services = {sid: {"id": sid, "enabled": True, "config": {
        "credential_source": {"kind": "cli-session", "connection_id": connection_id},
        "request_protocol": kind.protocol, "billing_mode": kind.billing_mode, "models": []}}}
    discovered = env.store.connection(kind.id)
    snapshot = registry_snapshot.build({"services": services}, registry_snapshot.BundledRelease({}), generation=1,
                                       connection_models={connection_id: {"models": dict(discovered["efforts"])}})
    target = resolve_request_target(body["model"], runtime_config={"backends": {sid: snapshot.services[sid]["config"]}},
                                    route_index=snapshot.model_routes)
    if identity is None:
        identity, access = granted()
    current = replace(identity, access=access)

    async def fresh():
        return current

    async def admit(edge, receipt):
        return None

    async def prepare(edge, original):
        return original

    return await model_routes.dispatch_routes(
        snapshot=snapshot, target=target, request=request_for(body, surface=surface, disconnected=disconnected), body=body,
        surface=surface, identity=current, fresh_identity=fresh, admission=admit, prepare_body=prepare,
        subscriptions=env.service, runtime=SimpleNamespace(enabled=False), ledger_path=None)


def body_of(response) -> dict:
    return json.loads(response.body)


async def read_stream(response) -> str:
    return b"".join([chunk async for chunk in response.body_iterator]).decode()


def spy_receipts(monkeypatch) -> list:
    seen = []
    original = receipts.RouteReceipt

    class Spy(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self)

    monkeypatch.setattr(receipts, "RouteReceipt", Spy)
    return seen


def with_runner(**settings):
    row = {**KIND.row, "runner": {**KIND.row["runner"], **settings}}
    return subscription_kinds.normalize_kind(row, source="test")


def alive(pid: int) -> bool:
    """Read from /proc (the hermetic guard forbids signalling from a test). A zombie is not running."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return False


# ------------------------------------------------------------------------------------------------ the row

def test_the_row_loads_with_the_two_new_modules():
    assert KIND.mechanism == "cli-sign-in" and KIND.protocol == "claude-code-runner" and KIND.billing_mode == "plan"
    assert "terms" in KIND.grant_note
    broken = {key: value for key, value in KIND.row.items() if key != "runner"}
    with pytest.raises(ValueError, match="runner"):
        subscription_kinds.normalize_kind(broken, source="test")


# ------------------------------------------------------------------------------------------------ connect

def test_connect_checks_the_binarys_own_sign_in_and_keeps_no_email(env):
    view = connect(env)
    assert view["status"] == "connected" and view["sign_in"] == "browser" and view["plan_type"] == "max"
    assert view["models"] == ["claude-test-opus", "claude-test-haiku"]  # the disabled row is not offered
    assert view["model_details"][1] == {"slug": "claude-test-haiku", "efforts": ["low", "medium", "high"]}
    dumped = json.dumps(view) + Path(env.tmp / "subscriptions.db").read_bytes().decode("latin-1")
    assert EMAIL not in dumped and "org-private" not in dumped and SECRET not in dumped
    status = next(call for call in calls(env) if call["argv"][:2] == ["auth", "status"])
    assert status["env"]["CLAUDE_CONFIG_DIR"] == str(env.home)


@pytest.mark.parametrize("auth", [{"loggedIn": False}, {"authMethod": "api_key"}])
def test_connect_opens_browser_when_the_binary_has_no_plan_sign_in(env, auth):
    env.scenario(auth=auth)
    async def check():
        started = await env.service.start("owner", "claude", session=None)
        try:
            assert started["authorize_url"].startswith("https://claude.ai/oauth/authorize?")
            assert env.store.connection("claude") is None
        finally:
            await env.service.close()
    run(check())


def test_connect_without_the_binary_is_a_named_refusal(env, monkeypatch):
    monkeypatch.setenv(cli_sign_in.BIN_DIR_ENV, str(env.tmp / "empty"))
    monkeypatch.setenv("PATH", str(env.tmp / "empty"))
    monkeypatch.setattr(cli_sign_in, "_resolve_executable", lambda *args: None)
    with pytest.raises(SubscriptionError) as refused:
        connect(env)
    assert refused.value.code == "plan_runner_unavailable"


def test_the_owner_status_offers_browser_sign_in_without_a_home(env):
    view = run(env.service.status_one("claude"))
    assert view["status"] == "not_connected" and view["sign_in"] == "browser"
    assert "sign_in_command" not in view
    assert str(env.home) not in json.dumps(view)


def test_owner_connect_grants_only_the_owner_and_seeds_a_cli_session_service(env):
    path = env.tmp / "admin.db"
    admin_store.init_db(path)
    owner = admin_store.upsert_oidc_user("https://issuer.test", "owner", path=path)["id"]
    other = admin_store.upsert_oidc_user("https://issuer.test", "other", path=path)["id"]
    shared = admin_store.create_access_plan(acc.plan_document("Shared"), path=path)
    with sqlite3.connect(path) as db:
        for user in (owner, other):
            db.execute("INSERT OR REPLACE INTO account_access(user_id,plan_id) VALUES(?,?)", (user, shared["id"]))
    env.store._secrets_path = lambda: path
    connect(env, owner=owner)
    assigned = admin_store.person_plan_id(owner, path=path)
    assert assigned != shared["id"]
    assert admin_store.get_access_plan(assigned, path=path)["plan"]["connections"] == ["claude-owner"]
    assert admin_store.person_plan_id(other, path=path) == shared["id"]
    assert admin_store.get_access_plan(shared["id"], path=path)["plan"]["connections"] == []
    record = service_registry.load_service_registry(path=path)["services"][KIND.model_prefix]
    assert record["config"]["credential_source"] == {"kind": "cli-session", "connection_id": "claude-owner"}
    assert record["config"]["request_protocol"] == "claude-code-runner"


def test_the_grant_carries_the_terms_note_for_the_operator(env):
    connect(env)
    choices = env.service.provider_choices()
    assert choices == [{"id": "claude-owner", "label": "Claude plan", "available": True, "grant_note": KIND.grant_note}]
    contract = admin_catalog.access_plan_editor_contract({"services": {}, "workflows": {}}, choices)
    assert contract["connections"][0]["grant_note"] == KIND.grant_note


def test_disconnect_leaves_the_binarys_own_sign_in_alone(env):
    connect(env)
    view = run(env.service.disconnect("claude", session=None))
    assert view["status"] == "disconnected" and "revoked_upstream" not in view
    assert (env.home / ".credentials.json").exists()


# ------------------------------------------------------------------------------------------------ access

def test_a_plan_without_the_connection_is_refused_before_anything_runs(env):
    connect(env)
    plan = acc.plan_for("plan_default")
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                            identity=acc.identity(plan), access=acc.access(plan)))
    assert response.status_code == 403 and body_of(response)["error"]["code"] == "not_in_access_plan"
    assert runs(env) == []


# ------------------------------------------------------------------------------------------------ one turn

def test_a_chat_turn_runs_the_binary_locked_down(env):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "messages": [
        {"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]}))
    assert response.status_code == 200, response.body
    out = body_of(response)
    assert out["object"] == "chat.completion" and out["choices"][0]["message"]["content"] == "echo: hi"
    assert out["choices"][0]["finish_reason"] == "stop" and out["model"] == MODEL
    assert response.headers["x-azizam-billing"] == "plan" and response.headers["x-azizam-provider"] == "claude"
    [call] = runs(env)
    argv = call["argv"]
    for flags in (["--tools", ""], ["--disallowedTools", "*"], ["--setting-sources", ""], ["--permission-mode", "dontAsk"],
                  ["--permission-prompts", "none"], ["--model", MODEL], ["--input-format", "stream-json"],
                  ["--output-format", "stream-json"]):
        assert any(argv[i:i + 2] == flags for i in range(len(argv))), flags
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv and "--mcp-config" not in argv
    assert "--resume" not in argv and "--effort" not in argv
    assert call["system"] == "be brief"
    assert call["stdin"][0]["message"]["content"] == [{"type": "text", "text": "hi"}]
    # A minimal environment built from nothing: no key of the gateway's reaches the binary.
    assert "ANTHROPIC_API_KEY" not in call["env"] and call["env"]["CLAUDE_CONFIG_DIR"] == str(env.home)
    assert call["env"]["HOME"] != str(Path.home()) and call["cwd_entries"] == []
    # The prompt file is gone and the slot is free once the run ends.
    assert not Path(argv[argv.index("--system-prompt-file") + 1]).exists()
    assert claude_code_runner.RUNNER.running("claude") == 0


def test_no_system_prompt_uses_the_rows_neutral_default(env):
    connect(env)
    run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert runs(env)[0]["system"] == KIND.row["runner"]["default_system_prompt"]


def test_streamed_chat_carries_the_text_and_ends_with_done_and_usage(env):
    connect(env)
    async def streamed():  # the run's pipes belong to one event loop: dispatch and read in the same one
        response = await dispatch(env, {"model": MODEL, "stream": True, "stream_options": {"include_usage": True},
                                        "messages": [{"role": "user", "content": "stream me"}]})
        return await read_stream(response)

    text = run(streamed())
    chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    content = "".join(choice["delta"].get("content") or "" for chunk in chunks for choice in chunk["choices"])
    assert content == "echo: stream me"
    assert text.rstrip().endswith("data: [DONE]")
    assert chunks[-1]["usage"]["prompt_tokens"] > 0


def test_chat_usage_names_the_prompt_cache_writes(env):
    connect(env)
    out = body_of(run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})))
    # fresh 5 + written 2 ("hi") + read 0 (the fake's counts): a Messages caller can then tell writes from fresh input
    assert out["usage"]["prompt_tokens"] == 7
    assert out["usage"]["prompt_tokens_details"] == {"cached_tokens": 0, "cache_creation_tokens": 2}


def test_responses_surface_returns_a_response_object(env):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "instructions": "be brief", "input": "hello"}, surface="responses"))
    out = body_of(response)
    assert out["object"] == "response" and out["status"] == "completed"
    assert out["output"][0]["content"][0]["text"] == "echo: hello"
    assert runs(env)[0]["system"] == "be brief"


def test_usage_goes_into_the_receipt_without_dollars(env, monkeypatch):
    connect(env)
    seen = spy_receipts(monkeypatch)
    run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    receipt = seen[-1]
    assert receipt.cost == {"usd": None, "source": "plan", "provider": "claude", "cost_class": "plan", "mode": "plan"}
    # fresh 5 + written 2 ("hi") + read 0; output 7; thinking 1 (the fake's counts)
    assert receipt.usage == {"input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 7, "reasoning_tokens": 1,
                             "cache_write_tokens": 2}
    assert receipt.metadata["request_protocol"] == "claude-code-runner" and receipt.metadata["connection_id"] == "claude-owner"


@pytest.mark.parametrize("asked,sent,dropped", [("high", "high", False), ("minimal", "low", True)])
def test_reasoning_effort_becomes_the_binarys_effort(env, asked, sent, dropped):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "reasoning_effort": asked, "messages": [{"role": "user", "content": "hi"}]}))
    argv = runs(env)[0]["argv"]
    assert argv[argv.index("--effort") + 1] == sent
    assert ("reasoning_effort" in response.headers.get("x-azizam-dropped-params", "")) is dropped


def test_an_effort_the_model_does_not_take_is_a_named_400(env):
    connect(env)
    response = run(dispatch(env, {"model": "claude-test-haiku", "reasoning_effort": "max",
                                  "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 400 and body_of(response)["error"]["code"] == "plan_unsupported_reasoning_effort"
    assert runs(env) == []


def test_an_effort_that_is_not_a_plain_word_never_reaches_the_command_line(env):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "reasoning_effort": "--help",
                                  "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 400 and body_of(response)["error"]["code"] == "plan_unsupported_reasoning_effort"
    assert runs(env) == []


def test_a_lone_surrogate_is_a_named_400_not_a_gateway_error(env):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi \ud800"}]}))
    assert response.status_code == 400 and body_of(response)["error"]["code"] == "plan_unsupported_content"
    assert runs(env) == []


def test_sampling_fields_are_dropped_and_disclosed(env):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "max_tokens": 50, "temperature": 0.2,
                                  "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200
    assert set(response.headers["x-azizam-dropped-params"].split(",")) == {"max_tokens", "temperature"}


# ------------------------------------------------------------------------------------------------ no tools

@pytest.mark.parametrize("extra", [
    {"tool_choice": "required"},
])
def test_required_tool_choice_is_refused_before_anything_runs(env, extra):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **extra}))
    assert response.status_code == 400 and body_of(response)["error"]["code"] == "plan_tool_choice_unsupported"
    assert runs(env) == []


def test_images_are_refused_by_name(env):
    connect(env)
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]}))
    assert response.status_code == 400 and body_of(response)["error"]["code"] == "plan_unsupported_content"


@pytest.mark.parametrize("scenario", [{"tools": ["Bash"]}, {"mcp_servers": [{"name": "x", "status": "connected"}]},
                                      {"api_key_source": "ANTHROPIC_API_KEY"}])
def test_a_run_that_starts_with_tools_or_a_key_is_stopped(env, scenario):
    connect(env)
    env.scenario(**scenario)
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 502 and body_of(response)["error"]["code"] == "plan_runner_unsafe"
    assert "echo" not in response.body.decode()


def test_text_before_the_init_event_is_refused_and_never_sent(env):
    connect(env)
    env.scenario(init_late="after_text")

    async def streamed():
        response = await dispatch(env, {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        return response.status_code, await read_stream(response)

    status, text = run(streamed())
    assert "echo" not in text and "plan_runner_unsafe" in text
    assert claude_code_runner.RUNNER.running("claude") == 0


def test_a_run_that_never_sends_the_init_event_is_refused(env):
    connect(env)
    env.scenario(init_late="always", partial=False)  # init never comes; the result does
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 502 and body_of(response)["error"]["code"] == "plan_runner_unsafe"
    assert "echo" not in response.body.decode()


# ------------------------------------------------------------------------------------------------ refusals

def test_a_usage_limit_is_a_named_429(env):
    connect(env)
    env.scenario(mode="rate_limit")
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 429 and body_of(response)["error"]["code"] == "plan_usage_limit_exceeded"
    assert "usage limit reached" not in response.body.decode()  # the gateway's words, never the binary's


def test_a_signed_out_binary_is_a_named_refusal_and_health_notes_it(env):
    connect(env)
    env.scenario(mode="logged_out")
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 503 and body_of(response)["error"]["code"] == "plan_connection_invalid"
    assert "Not logged in" not in response.body.decode()
    assert env.store.connection("claude")["last_error_code"] == "plan_connection_invalid"


def test_a_binary_that_dies_without_a_result_is_a_named_502(env):
    connect(env)
    env.scenario(mode="exit_early")
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 502 and body_of(response)["error"]["code"] == "plan_upstream_error"


# ------------------------------------------------------------------------------------------------ sessions

def test_a_later_turn_resumes_a_copy_of_the_session_and_reads_the_cache(env, monkeypatch):
    connect(env)
    seen = spy_receipts(monkeypatch)
    first = body_of(run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})))
    reply = first["choices"][0]["message"]["content"]
    second = run(dispatch(env, {"model": MODEL, "messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": reply}, {"role": "user", "content": "again"}]}))
    assert body_of(second)["choices"][0]["message"]["content"] == "echo: again"
    one, two = runs(env)
    old = one["argv"][one["argv"].index("--session-id") + 1]
    argv = two["argv"]
    assert argv[argv.index("--resume") + 1] == old and "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] != old
    assert two["stdin"][0]["message"]["content"] == [{"type": "text", "text": "again"}]  # only the new turn
    assert seen[-1].usage["cached_input_tokens"] > 0
    # A third turn continues from the second turn's session, not the first one.
    third = run(dispatch(env, {"model": MODEL, "messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": reply}, {"role": "user", "content": "again"},
        {"role": "assistant", "content": "echo: again"}, {"role": "user", "content": "more"}]}))
    assert third.status_code == 200
    argv3 = runs(env)[2]["argv"]
    assert argv3[argv3.index("--resume") + 1] == argv[argv.index("--session-id") + 1]


def test_an_expired_session_loses_its_file_and_nothing_outside_the_session_directory(env):
    connect(env)
    run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    (kept,) = session_files(env)
    session = kept.stem
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / f"{session}.jsonl").write_text("do not touch")  # same name, reached through a linked directory
    (env.home / "projects" / "linked").symlink_to(outside, target_is_directory=True)
    (env.home / f"{session}.jsonl").write_text("do not touch")  # same name, in the config home itself
    target = env.tmp / "target.txt"
    target.write_text("do not touch")
    (env.home / "projects" / "fake-project" / f"{uuid.uuid4()}.jsonl").symlink_to(target)
    book = claude_code_runner.RUNNER._sessions
    (key,) = list(book._entries)
    ttl = float(KIND.row["runner"]["session_ttl_seconds"])
    assert book.find(key, time.monotonic()) == session and kept.exists()  # still live: kept
    assert book.find(key, time.monotonic() + ttl + 5) is None
    assert not kept.exists()
    assert (outside / f"{session}.jsonl").read_text() == "do not touch"
    assert (env.home / f"{session}.jsonl").read_text() == "do not touch"
    assert target.read_text() == "do not touch"


def test_a_run_that_fails_leaves_no_session_file(env):
    connect(env)
    env.scenario(fail_after_write=True)
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 429
    assert session_files(env) == []  # the binary wrote it, but nothing can resume it


def test_the_session_book_deletes_a_file_when_a_session_is_dropped_replaced_or_pushed_out(tmp_path):
    home = tmp_path / "home"
    folder = home / "projects" / "p"
    folder.mkdir(parents=True)
    ids = [str(uuid.uuid4()) for _ in range(5)]
    for sid in ids:
        (folder / f"{sid}.jsonl").write_text("x")
    book = claude_code_runner._SessionBook(limit=2)
    book.put("a", ids[0], 0.0, 100.0, home)
    book.put("b", ids[1], 0.0, 100.0, home)
    book.put("c", ids[2], 0.0, 100.0, home)  # over the limit: "a" is pushed out
    assert not (folder / f"{ids[0]}.jsonl").exists() and (folder / f"{ids[1]}.jsonl").exists()
    book.drop("b")
    assert not (folder / f"{ids[1]}.jsonl").exists()
    book.put("c", ids[3], 0.0, 100.0, home)  # the key now names a newer session: the older file goes
    assert not (folder / f"{ids[2]}.jsonl").exists() and (folder / f"{ids[3]}.jsonl").exists()
    book.put("d", ids[4], 1.0, 5.0, home)
    book.sweep(50.0)  # "d" expired at 6; "c" lasts to 100
    assert not (folder / f"{ids[4]}.jsonl").exists() and (folder / f"{ids[3]}.jsonl").exists()
    book.clear()
    assert not (folder / f"{ids[3]}.jsonl").exists()


def test_a_session_id_that_is_not_a_uuid_deletes_nothing(tmp_path):
    home = tmp_path / "home"
    (home / "projects" / "p").mkdir(parents=True)
    (home / "projects" / "p" / "keep.jsonl").write_text("x")
    book = claude_code_runner._SessionBook()
    book.put("a", "*", 0.0, 100.0, home)
    book.put("b", "../projects/p/keep", 0.0, 100.0, home)
    book.clear()
    assert (home / "projects" / "p" / "keep.jsonl").exists()


def test_sessions_never_cross_callers(env):
    connect(env)
    first = body_of(run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})))
    reply = first["choices"][0]["message"]["content"]
    identity, access = granted(app_id="app_other", key_hash="sha256:other")
    run(dispatch(env, {"model": MODEL, "messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": reply}, {"role": "user", "content": "again"}]},
        identity=identity, access=access))
    second = runs(env)[1]
    assert "--resume" not in second["argv"]
    texts = [block["text"] for block in second["stdin"][0]["message"]["content"]]
    assert texts[1:] == ["[user]\nhi", f"[assistant]\n{reply}", "[user]\nagain"]  # the turns as a transcript


def test_an_edited_history_starts_a_new_session(env):
    connect(env)
    run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    run(dispatch(env, {"model": MODEL, "messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "something else"},
        {"role": "user", "content": "again"}]}))
    assert "--resume" not in runs(env)[1]["argv"]


def test_a_lost_session_falls_back_to_a_new_one(env):
    connect(env)
    first = body_of(run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})))
    for path in (env.home / "projects" / "fake-project").iterdir():
        path.unlink()
    second = run(dispatch(env, {"model": MODEL, "messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
        {"role": "user", "content": "again"}]}))
    assert second.status_code == 200 and body_of(second)["choices"][0]["message"]["content"] == "echo: again"
    tried, fresh = runs(env)[1:]
    assert "--resume" in tried["argv"] and "--resume" not in fresh["argv"]
    assert claude_code_runner.RUNNER.running("claude") == 0


def session_files(env) -> list[Path]:
    return sorted((env.home / "projects").glob("*/*.jsonl"))


def test_the_binary_may_finish_its_session_file_after_the_result(env):
    connect(env)
    env.scenario(write_after_result=0.5)  # prints the result, then writes the file and exits by itself
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200
    assert len(session_files(env)) == 1  # not cut short by a SIGTERM sent at the result
    assert claude_code_runner.RUNNER.running("claude") == 0


def test_a_binary_that_will_not_exit_after_the_result_is_still_ended(env):
    connect(env)
    env.scenario(write_after_result=60)
    started = time.monotonic()
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    assert response.status_code == 200
    budgets = transport_budgets.section("plan_runner")
    assert time.monotonic() - started < budgets["exit_wait_seconds"] + budgets["kill_grace_seconds"] + 5
    assert session_files(env) == []  # it never finished the file; it was ended, not waited for


# ------------------------------------------------------------------------------------------------ lifetime

def test_cancel_kills_the_process_group(env):
    connect(env)
    env.scenario(mode="hang")

    async def scenario():
        gone = asyncio.Event()
        task = asyncio.create_task(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                                            disconnected=gone))
        for _ in range(200):
            if (env.home / "hang.json").exists():
                break
            await asyncio.sleep(0.05)
        pids = json.loads((env.home / "hang.json").read_text())
        assert alive(pids["pid"]) and alive(pids["child"])
        gone.set()
        response = await task
        return response, pids

    response, pids = run(scenario())
    assert response.status_code == 499
    for _ in range(100):
        if not alive(pids["pid"]) and not alive(pids["child"]):
            break
        import time
        time.sleep(0.05)
    assert not alive(pids["pid"]) and not alive(pids["child"])
    assert claude_code_runner.RUNNER.running("claude") == 0


def test_a_run_past_its_time_limit_is_stopped(env):
    connect(env)
    env.scenario(mode="hang")
    response = run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                            kind=with_runner(timeout_seconds=1)))
    assert response.status_code == 504 and body_of(response)["error"]["code"] == "plan_upstream_timeout"
    pids = json.loads((env.home / "hang.json").read_text())
    assert not alive(pids["pid"]) and not alive(pids["child"])
    assert claude_code_runner.RUNNER.running("claude") == 0


def test_runs_over_the_cap_are_refused_by_name(env):
    connect(env)
    env.scenario(mode="hang")
    kind = with_runner(max_concurrent=1, timeout_seconds=5)

    async def scenario():
        gone = asyncio.Event()
        first = asyncio.create_task(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "1"}]},
                                             kind=kind, disconnected=gone))
        for _ in range(200):
            if (env.home / "hang.json").exists():
                break
            await asyncio.sleep(0.05)
        second = await dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "2"}]}, kind=kind)
        gone.set()
        await first
        return second

    second = run(scenario())
    assert second.status_code == 429 and body_of(second)["error"]["code"] == "plan_runner_busy"
    assert len(runs(env)) == 1
    assert claude_code_runner.RUNNER.running("claude") == 0


# ------------------------------------------------------------------------------------------------ secrets

def test_no_token_or_email_reaches_logs_receipts_or_views(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    seen = spy_receipts(monkeypatch)
    view = connect(env)
    run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    env.scenario(mode="logged_out")
    run(dispatch(env, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}))
    run(env.service.refresh_models("claude"))
    texts = [caplog.text, json.dumps(view, default=str), json.dumps(run(env.service.status()), default=str)]
    texts += [json.dumps({"usage": receipt.usage, "metadata": receipt.metadata, "cost": receipt.cost}, default=str)
              for receipt in seen]
    for text in texts:
        assert SECRET not in text and "sk-ant" not in text and EMAIL not in text


def test_model_discovery_reads_only_model_rows(env):
    connect(env)
    models = run(env.service.refresh_models("claude"))
    assert models == {"claude-test-opus": ["low", "medium", "high", "xhigh", "max"], "claude-test-haiku": ["low", "medium", "high"]}
    discovery = [call for call in calls(env) if call.get("stdin") and call["stdin"][0].get("type") == "control_request"]
    assert discovery and "--tools" in discovery[-1]["argv"]


# ------------------------------------------------------------------------------------------------ real ingresses

@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"model": MODEL, "input": "hi"}),
    ("/v1/messages", {"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}),
])
@pytest.mark.parametrize("stream", [False, True])
def test_each_public_route_reaches_the_plan_and_refuses_without_the_grant(env, monkeypatch, path, body, stream):
    import proxy
    from fastapi.testclient import TestClient
    from tests.support.fake_upstream.route_dispatch import loopback_sender

    connect(env)
    sid = KIND.model_prefix
    services = {sid: {"id": sid, "enabled": True, "config": {
        "credential_source": {"kind": "cli-session", "connection_id": "claude-owner"},
        "request_protocol": KIND.protocol, "billing_mode": "plan", "models": []}}}
    snapshot = registry_snapshot.build({"services": services}, registry_snapshot.BundledRelease({}), generation=1,
                                       connection_models={"claude-owner": {"models": dict(env.store.connection("claude")["efforts"])}})
    monkeypatch.setattr(proxy, "GATEWAY_REGISTRY_PROVIDER", registry_snapshot.Provider(snapshot))
    monkeypatch.setattr(proxy, "SUBSCRIPTIONS", env.service)
    monkeypatch.setattr(proxy, "_forward_chat_completions", loopback_sender(proxy.app))

    def post(connections):
        plan = acc.plan_for("plan_app", connections=connections)
        with acc.authenticated(acc.identity(plan, person_id="usr_app")):
            return TestClient(proxy.app).post(path, json={**body, "stream": stream},
                                              headers={"authorization": "Bearer k", "x-api-key": "k"})

    refused = post([])
    assert refused.status_code == 403, refused.text
    assert runs(env) == []
    served = post(["claude-owner"])
    assert served.status_code == 200, served.text
    # A stream carries the answer in pieces (the fake sends "echo" then ": hi").
    assert ("echo" in served.text and ": hi" in served.text) if stream else "echo: hi" in served.text
    assert len(runs(env)) == 1


# --------------------------------------------------------------------------------------- plan usage windows

USAGE_NOW = 1_800_000_000
USAGE_WINDOWS = {"five_hour": {"utilization": 0.05, "resetsAt": USAGE_NOW + 300},
                 "seven_day": {"utilization": 0.83, "resetsAt": USAGE_NOW + 7000}}
USAGE_SNAPSHOT = {"captured_at": USAGE_NOW, "windows": [
    {"used_percent": 5.0, "window_minutes": 300, "resets_at": USAGE_NOW + 300,
     "id": "five_hour", "scope": "Account", "source": "runner", "captured_at": USAGE_NOW},
    {"used_percent": 83.0, "window_minutes": 10080, "resets_at": USAGE_NOW + 7000,
     "id": "seven_day", "scope": "Account", "source": "runner", "captured_at": USAGE_NOW}]}


def usage_limits(env):
    return run(env.service.status_one("claude")).get("usage_limits")


def usage_request(env, *, stream=False):
    async def request():
        response = await dispatch(env, {"model": MODEL, "stream": stream,
                                       "messages": [{"role": "user", "content": "hi"}]})
        if stream and hasattr(response, "body_iterator"):
            await read_stream(response)
        return response
    return run(request())


@pytest.mark.parametrize("stream", [False, True])
def test_runner_records_last_usage_windows_for_the_connection(env, monkeypatch, stream):
    connect(env)
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(rate_limit_events=[
        {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.01, "resetsAt": USAGE_NOW}}},
        {"status": "allowed_warning", "unifiedWindows": USAGE_WINDOWS}])
    assert usage_request(env, stream=stream).status_code == 200
    assert usage_limits(env) == USAGE_SNAPSHOT
    row = env.store.connection("claude")
    assert row["usage"] == {**USAGE_SNAPSHOT, "generation": row["generation"]}


def test_runner_without_usage_keeps_the_last_reading_and_writes_nothing(env, monkeypatch):
    connect(env)
    env.store.record_usage("claude", USAGE_SNAPSHOT)
    writes = []
    monkeypatch.setattr(env.store, "record_usage", lambda *args: writes.append(args))
    env.scenario()
    assert usage_request(env).status_code == 200
    assert usage_limits(env) == USAGE_SNAPSHOT
    assert writes == []


@pytest.mark.parametrize("bad", [
    {"utilization": -0.1, "resetsAt": USAGE_NOW},
    {"utilization": "bad", "resetsAt": USAGE_NOW},
    {"utilization": float("nan"), "resetsAt": USAGE_NOW},
    {"utilization": float("inf"), "resetsAt": USAGE_NOW},
    {"utilization": 10.01, "resetsAt": USAGE_NOW},
    *[{"utilization": 0.1, "resetsAt": reset} for reset in
      ["soon", "1800000000", 1800000000.0, True, 0, -1, USAGE_NOW + 366 * 86400 + 1]],
    None, [],
])
def test_runner_drops_bad_usage_windows_without_failing(env, monkeypatch, bad):
    connect(env)
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(rate_limit_events=[{"status": "allowed", "unifiedWindows": {
        "five_hour": bad, "seven_day": USAGE_WINDOWS["seven_day"],
        "unknown_window": {"utilization": 0.1, "resetsAt": USAGE_NOW}}}])
    assert usage_request(env).status_code == 200
    assert usage_limits(env) == {"captured_at": USAGE_NOW, "windows": USAGE_SNAPSHOT["windows"][1:]}


@pytest.mark.parametrize("stream", [False, True])
def test_runner_usage_limit_still_refuses_and_records_usage(env, monkeypatch, stream):
    connect(env)
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(mode="rate_limit", rate_limit_events=[{"status": "rejected", "unifiedWindows": USAGE_WINDOWS}])
    response = usage_request(env, stream=stream)
    assert response.status_code == 429
    assert body_of(response)["error"]["code"] == "plan_usage_limit_exceeded"
    assert usage_limits(env) == USAGE_SNAPSHOT


@pytest.mark.parametrize("window_id", ["seven_day_overage_included", "seven_day_opus", "seven_day_sonnet"])
def test_runner_records_other_known_weekly_windows_and_over_limit_use(env, monkeypatch, window_id):
    connect(env)
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(rate_limit_events=[{"unifiedWindows": {
        window_id: {"utilization": 1.2, "resetsAt": USAGE_NOW}}}])
    assert usage_request(env).status_code == 200
    models = {"seven_day_opus": {"models": "Opus"}, "seven_day_sonnet": {"models": "Sonnet"}}.get(window_id, {})
    assert usage_limits(env) == {"captured_at": USAGE_NOW, "windows": [
        {"used_percent": 120.0, "window_minutes": 10080, "resets_at": USAGE_NOW, **models, "id": window_id,
         "scope": "Model" if models else "Overage", "source": "runner", "captured_at": USAGE_NOW}]}


@pytest.mark.parametrize("last_info", [{}, {"unifiedWindows": {}}, {"unifiedWindows": []}, None])
def test_runner_a_later_event_without_usable_windows_keeps_this_runs_earlier_reading(env, monkeypatch, last_info):
    connect(env)
    env.store.record_usage("claude", {"captured_at": 1, "windows": [
        {"used_percent": 1.0, "window_minutes": 300, "resets_at": 2}]})
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(rate_limit_events=[{"unifiedWindows": USAGE_WINDOWS}, last_info])
    assert usage_request(env).status_code == 200
    assert usage_limits(env)["windows"] == USAGE_SNAPSHOT["windows"]


def test_runner_usage_store_failure_never_fails_the_answer(env, monkeypatch):
    connect(env)
    def broken(*args):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(env.store, "record_usage", broken)
    env.scenario(rate_limit_events=[{"unifiedWindows": USAGE_WINDOWS}])
    assert usage_request(env).status_code == 200


def test_runner_identical_usage_readings_reuse_the_note_dedupe(env, monkeypatch):
    connect(env)
    writes = []
    keep = env.store.record_usage
    monkeypatch.setattr(env.store, "record_usage", lambda *args: writes.append(args) or keep(*args))
    env.scenario(rate_limit_events=[{"unifiedWindows": USAGE_WINDOWS}])
    assert usage_request(env).status_code == 200
    assert usage_request(env).status_code == 200
    assert len(writes) == 1
    assert writes[0][0] == "claude-owner"


def test_runner_reading_names_the_sign_in_and_a_late_earlier_one_is_refused(env, monkeypatch):
    connect(env)
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(rate_limit_events=[{"unifiedWindows": USAGE_WINDOWS}])
    assert usage_request(env).status_code == 200
    row = env.store.connection("claude")
    assert row["usage"]["generation"] == row["generation"]
    env.store.record_usage("claude", {"captured_at": USAGE_NOW - 1, "generation": row["generation"], "windows": [
        {"used_percent": 1.0, "window_minutes": 300, "resets_at": USAGE_NOW}]})
    assert usage_limits(env) == USAGE_SNAPSHOT


def test_usage_window_identity_and_fields_are_provider_data():
    mappings = [
        {"key": "base", "id": "account", "scope": "Account", "window_minutes": 90,
         "used_field": "percent", "reset_field": "reset", "scale": 1},
        {"key": "extra", "id": "overage", "scope": "Overage", "window_minutes": 90,
         "used_field": "percent", "reset_field": "reset", "scale": 1},
    ]
    result = claude_code_runner._usage_snapshot(
        {key: {"percent": 25, "reset": USAGE_NOW + 100} for key in ("base", "extra")},
        now=USAGE_NOW, mappings=mappings)
    assert [w["id"] for w in result["windows"]] == ["account", "overage"]
    assert [w["scope"] for w in result["windows"]] == ["Account", "Overage"]
    assert all(w["used_percent"] == 25 and w["window_minutes"] == 90 for w in result["windows"])


@pytest.mark.parametrize("stream", [False, True])
def test_chat_request_preserves_distinct_weekly_window_identity_in_owner_status(env, monkeypatch, stream):
    connect(env)
    monkeypatch.setattr(claude_code_runner.time, "time", lambda: USAGE_NOW)
    env.scenario(rate_limit_events=[{"status": "allowed", "unifiedWindows": {
        "seven_day": {"utilization": 0.25, "resetsAt": USAGE_NOW + 7000},
        "seven_day_overage_included": {"utilization": 0.50, "resetsAt": USAGE_NOW + 7000},
        "seven_day_opus": {"utilization": 0.75, "resetsAt": USAGE_NOW + 7000}}}])
    assert usage_request(env, stream=stream).status_code == 200
    windows = usage_limits(env)["windows"]
    assert [(window["id"], window["scope"], window["used_percent"]) for window in windows] == [
        ("seven_day", "Account", 25.0), ("seven_day_overage_included", "Overage", 50.0),
        ("seven_day_opus", "Model", 75.0)]
    assert all(window["window_minutes"] == 10080 and window["source"] == "runner"
               and window["captured_at"] == USAGE_NOW for window in windows)
    assert windows[2]["models"] == "Opus"
