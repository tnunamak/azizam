"""Browser sign-in through a fake CLI only: never an owner home or provider."""
import asyncio
import json
import logging

import pytest

from gateway.connections import cli_sign_in
from gateway.provider_connections import SubscriptionError
from tests.test_claude_plan_runner import env, calls, alive, run  # noqa: F401

URL = "https://claude.ai/oauth/authorize?opaque=fake"


def unsigned(env, **extra):
    env.scenario(auth={"loggedIn": False}, **extra)


def test_start_returns_browser_url_and_owner_view_has_no_home(env):
    async def check():
        unsigned(env)
        started = await env.service.start("owner", "claude", session=None)
        try:
            assert started["authorize_url"] == URL
            view = await env.service.status_one("claude")
            assert view["sign_in"] == "browser"
            assert "sign_in_command" not in view
            assert str(env.home) not in json.dumps(view)
            login = next(c for c in calls(env) if c["argv"][:2] == ["auth", "login"])
            assert "--claudeai" in login["argv"]
            assert login["env"]["CLAUDE_CONFIG_DIR"] == str(env.home)
            assert int(login["env"]["COLUMNS"]) >= 4000
        finally:
            await env.service.close()
    run(check())


@pytest.mark.parametrize("code", ["good", " good#state "])
def test_pasted_code_connects_without_logging_it(env, caplog, code):
    caplog.set_level(logging.DEBUG)
    async def check():
        unsigned(env)
        started = await env.service.start("owner", "claude", session=None)
        try:
            result = await env.service.finish("owner", started["attempt_id"], code, session=None)
            assert result["status"] == "connected"
            assert code.strip() not in caplog.text
        finally:
            await env.service.close()
    run(check())


def test_bad_code_is_plain_named_refusal(env):
    async def check():
        unsigned(env)
        started = await env.service.start("owner", "claude", session=None)
        try:
            with pytest.raises(SubscriptionError) as refused:
                await env.service.finish("owner", started["attempt_id"], "bad", session=None)
            assert refused.value.code == "plan_sign_in_refused"
            assert refused.value.message == "Claude didn't accept that code. Start the sign-in again."
            assert env.store.connection("claude") is None
        finally:
            await env.service.close()
    run(check())


def test_self_finished_login_accepts_no_code(env):
    async def check():
        unsigned(env, login="self")
        started = await env.service.start("owner", "claude", session=None)
        try:
            for _ in range(100):
                view = await env.service.finish("owner", started["attempt_id"], "", session=None)
                if view["status"] == "connected":
                    break
                await asyncio.sleep(0.02)
            assert view["status"] == "connected"
        finally:
            await env.service.close()
    run(check())


def test_poll_does_not_spend_pending_attempt(env):
    async def check():
        unsigned(env, wrapped_url=True)
        started = await env.service.start("owner", "claude", session=None)
        try:
            assert started["authorize_url"] == URL
            assert (await env.service.finish("owner", started["attempt_id"], "", session=None))["status"] == "pending"
            assert (await env.service.finish("owner", started["attempt_id"], "good", session=None))["status"] == "connected"
        finally:
            await env.service.close()
    run(check())


@pytest.mark.parametrize("end", ["replace", "timeout", "disconnect", "shutdown"])
def test_pending_process_is_ended(env, monkeypatch, end):
    async def check():
        unsigned(env)
        if end == "timeout":
            monkeypatch.setattr(cli_sign_in, "LOGIN_TTL_SECONDS", 0.15)
        await env.service.start("owner", "claude", session=None)
        pid = next(c["pid"] for c in calls(env) if c["argv"][:2] == ["auth", "login"])
        try:
            if end == "replace":
                await env.service.start("owner", "claude", session=None)
            elif end == "disconnect":
                await env.service.disconnect("claude", session=None)
            elif end == "shutdown":
                await env.service.close()
            else:
                for _ in range(100):
                    if not alive(pid):
                        break
                    await asyncio.sleep(0.02)
            assert not alive(pid)
        finally:
            await env.service.close()
    run(check())


def test_missing_binary_gives_install_words_and_url(env, monkeypatch):
    monkeypatch.setattr(cli_sign_in, "_resolve_executable", lambda *args: None)
    with pytest.raises(SubscriptionError) as refused:
        run(env.service.start("owner", "claude", session=None))
    assert refused.value.code == "plan_runner_unavailable"
    assert "Azizam can't find Claude Code on this computer. Install it, then try again." in refused.value.message
    assert "https://code.claude.com/docs/en/setup" in refused.value.message
    assert str(env.home) not in refused.value.message


def test_start_ignores_a_url_on_another_host(env):
    async def check():
        unsigned(env, other_url=True)
        try:
            started = await env.service.start("owner", "claude", session=None)
            assert started["authorize_url"] == URL
        finally:
            await env.service.close()
    run(check())
