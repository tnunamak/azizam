"""Runner installation uses fake release bytes, never the network or owner state."""
import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from gateway.connections import cli_sign_in, runner_install
from gateway.provider_connections import SubscriptionError
from tests.support.fake_upstream import HandlerTransport
from tests.test_claude_plan_runner import env, run  # noqa: F401

BINARY = b"#!/bin/sh\nexit 0\n"


@pytest.mark.parametrize("system,machine,libc,musl,translated,expected", [
    ("linux", "x86_64", "glibc", False, False, "linux-x64"),
    ("linux", "aarch64", "glibc", False, False, "linux-arm64"),
    ("linux", "amd64", "", True, False, "linux-x64-musl"),
    ("linux", "arm64", "musl", False, False, "linux-arm64-musl"),
    ("darwin", "arm64", "", False, False, "darwin-arm64"),
    ("darwin", "x86_64", "", False, True, "darwin-arm64"),
    ("win32", "AMD64", "", False, False, "win32-x64"),
    ("win32", "ARM64", "", False, False, "win32-arm64"),
    ("freebsd14", "amd64", "", False, False, None),
    ("linux", "riscv64", "glibc", False, False, None),
])
def test_platform(system, machine, libc, musl, translated, expected):
    assert runner_install.platform_key(system, machine, libc, musl, translated) == expected


@pytest.mark.parametrize("fault,code", [
    (None, None), ("checksum", "runner_install_checksum"), ("size", "runner_install_size"),
    ("version", "runner_install_version"), ("platform", "runner_install_platform"),
    ("network", "runner_install_download"),
])
def test_install_and_named_failures(env, monkeypatch, fault, code):
    monkeypatch.setattr(runner_install, "detect_platform", lambda: "linux-x64")
    target = Path(cli_sign_in.bin_dir()) / "claude"
    target.unlink()
    monkeypatch.setattr(cli_sign_in.shutil, "which", lambda *args: None)
    requests = []
    def serve(request):
        requests.append(request.url.path)
        if fault == "network":
            raise httpx.ConnectError("private detail", request=request)
        if request.url.path.endswith("/stable"):
            return httpx.Response(200, text="<html>no</html>" if fault == "version" else "1.2.3\n")
        if request.url.path.endswith("manifest.json"):
            entry = {"binary": "claude", "size": len(BINARY) + (1 if fault == "size" else 0),
                     "checksum": "0" * 64 if fault == "checksum" else hashlib.sha256(BINARY).hexdigest()}
            return httpx.Response(200, json={"platforms": {} if fault == "platform" else {"linux-x64": entry}})
        return httpx.Response(200, content=BINARY)
    env.service._http = lambda: httpx.AsyncClient(transport=HandlerTransport(serve))
    async def check():
        try:
            assert (await env.service.status_one("claude"))["runner"]["state"] == "missing"
            if fault is None:
                with pytest.raises(SubscriptionError) as missing:
                    await env.service.start("owner", "claude", session=None)
                assert missing.value.code == "plan_runner_unavailable"
            first, second = await asyncio.gather(env.service.install_runner("claude"), env.service.install_runner("claude"))
            # A tiny fake download may finish before the status read returns.
            assert first["runner"]["state"] in {"installing", "ready", "failed"}
            assert second["runner"]["state"] in {"installing", "ready", "failed"}
            await env.service._runner_installs["claude"]
            state = (await env.service.status_one("claude"))["runner"]
            if code:
                assert state["state"] == "failed"
                assert state["error"]["code"] == code
                assert not target.exists()
                assert list(target.parent.iterdir()) == []
            else:
                assert state["state"] == "ready" and state["version"] == "1.2.3"
                assert target.read_bytes() == BINARY
                assert cli_sign_in.runner_handle(env.service.kind("claude"), env.tmp).executable == str(target)
                assert requests.count("/claude-code-releases/1.2.3/linux-x64/claude") == 1
        finally:
            await env.service.close()
    run(check())


def test_install_route_owner_only_and_single_flight(env, monkeypatch):
    from fastapi import FastAPI
    from gateway.admin_api import AdminContext
    from gateway.subscription_api import create_subscription_router

    monkeypatch.setattr(runner_install, "detect_platform", lambda: "linux-x64")
    (cli_sign_in.bin_dir() / "claude").unlink()
    count = 0
    entered, release = asyncio.Event(), asyncio.Event()
    async def serve(request):
        nonlocal count
        if request.url.path.endswith("/stable"):
            return httpx.Response(200, text="1.2.3")
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json={"platforms": {"linux-x64": {
                "binary": "claude", "size": len(BINARY), "checksum": hashlib.sha256(BINARY).hexdigest()}}})
        count += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, content=BINARY)
    env.service._http = lambda: httpx.AsyncClient(transport=HandlerTransport(serve))
    def auth(request):
        roles = {"admin", "owner"} if request.headers.get("x-owner") == "yes" else {"admin"}
        return AdminContext(subject="test", roles=frozenset(roles), auth_method="key")
    app = FastAPI()
    app.include_router(create_subscription_router(service=env.service, auth=auth))
    async def check():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                url = "/admin/api/subscriptions/claude/install-runner"
                denied = await client.post(url)
                assert denied.status_code == 403
                assert denied.json()["error"]["code"] == "owner_required"
                first = await client.post(url, headers={"x-owner": "yes"})
                await asyncio.wait_for(entered.wait(), 2)
                second = await client.post(url, headers={"x-owner": "yes"})
                assert first.status_code == second.status_code == 200
                assert first.json()["subscription"]["runner"]["state"] == "installing"
                assert second.json()["subscription"]["runner"]["state"] == "installing"
                assert count == 1
                release.set()
                await env.service._runner_installs["claude"]
        finally:
            await env.service.close()
    run(check())


def test_default_bin_dir_is_first(env, monkeypatch):
    monkeypatch.delenv(cli_sign_in.BIN_DIR_ENV)
    directory = cli_sign_in.homes_root() / "bin"
    directory.mkdir()
    binary = directory / "claude"
    binary.write_bytes(BINARY)
    binary.chmod(0o755)
    assert cli_sign_in.runner_handle(env.service.kind("claude"), env.tmp).executable == str(binary)


@pytest.mark.parametrize("end", ["cancel", "timeout"])
def test_partial_download_cleanup_keeps_previous_binary(env, monkeypatch, end):
    monkeypatch.setattr(runner_install, "detect_platform", lambda: "linux-x64")
    if end == "timeout":
        monkeypatch.setattr(runner_install, "INSTALL_SECONDS", 0.1)
    target = cli_sign_in.bin_dir() / "claude"
    original = target.read_bytes()
    entered = asyncio.Event()
    class Partial(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (64 * 1024)
            entered.set()
            await asyncio.Event().wait()
    def serve(request):
        if request.url.path.endswith("/stable"):
            return httpx.Response(200, text="1.2.3")
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json={"platforms": {"linux-x64": {
                "binary": "claude", "size": 128 * 1024, "checksum": "0" * 64}}})
        return httpx.Response(200, stream=Partial())
    env.service._http = lambda: httpx.AsyncClient(transport=HandlerTransport(serve))
    async def check():
        try:
            await env.service.install_runner("claude")
            await asyncio.wait_for(entered.wait(), 2)
            assert any(p.name.startswith(".runner-") for p in target.parent.iterdir())
            if end == "cancel":
                await env.service.close()
            else:
                await env.service._runner_installs["claude"]
                assert (await env.service.status_one("claude"))["runner"]["error"]["code"] == "runner_install_download"
            assert target.read_bytes() == original
            assert list(target.parent.iterdir()) == [target]
        finally:
            await env.service.close()
    run(check())


def test_progress_reports_each_chunk_up_to_the_whole_size(env, monkeypatch):
    monkeypatch.setattr(runner_install, "detect_platform", lambda: "linux-x64")
    def serve(request):
        if request.url.path.endswith("/stable"):
            return httpx.Response(200, text="1.2.3\n")
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json={"platforms": {"linux-x64": {
                "binary": "claude", "size": len(BINARY), "checksum": hashlib.sha256(BINARY).hexdigest()}}})
        return httpx.Response(200, content=BINARY)
    env.service._http = lambda: httpx.AsyncClient(transport=HandlerTransport(serve))
    heard = []
    async def check():
        try:
            version = await runner_install.download(env.service.kind("claude"), env.service._client,
                                                    lambda received, size: heard.append((received, size)))
            assert version == "1.2.3"
        finally:
            await env.service.close()
    run(check())
    assert heard and heard[-1] == (len(BINARY), len(BINARY))
    assert [received for received, _size in heard] == sorted(received for received, _size in heard)
