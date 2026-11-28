"""Browser sign-in mediated by a CLI; only the CLI reads its own credentials."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from gateway import subscription_store
from gateway.provider_connections import (CONNECT_MECHANISMS, ConnectionContext, ConnectionFacts, ConnectStart, Credential,
                                          SubscriptionError)
from gateway.subscription_kinds import SubscriptionKind
from gateway.subscription_store import Attempt

# Startup basics (host paths), like AZIZAM_ADMIN_KEY_FILE: where runner config homes live, and a directory searched
# for the runner executable before PATH.
HOMES_ENV = "AZIZAM_PLAN_RUNNER_HOMES"
BIN_DIR_ENV = "AZIZAM_PLAN_RUNNER_BIN_DIR"
LOGIN_TTL_SECONDS = 600
LOGIN_FINISH_SECONDS = 60
STATUS_TIMEOUT_SECONDS = 20
_STATUS_LIMIT = 64 * 1024
_REQUIRED = ("executable", "home_env", "home_dir", "home_slot", "status_argv", "status_fields", "expected_method",
             "login_argv", "login_url_prefix", "install_page_url", "missing_binary_message")


def homes_root() -> Path:
    configured = os.environ.get(HOMES_ENV)
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "azizam"


def bin_dir() -> Path:
    configured = os.environ.get(BIN_DIR_ENV)
    return Path(configured).expanduser() if configured else homes_root() / "bin"


@dataclass(frozen=True)
class RunnerHandle:
    """Where and how to start the CLI for one connection. Holds paths and fixed settings, never a secret."""

    kind_id: str
    executable: str | None
    home: Path
    workdir: Path
    env: Mapping[str, str] = field(repr=False)

    def command(self, *args: str) -> list[str]:
        if self.executable is None:
            raise SubscriptionError(503, "plan_runner_unavailable",
                                    "The gateway could not find the program it runs for this plan. The owner can check that it is installed.")
        return [self.executable, *args]


def _resolve_executable(name: str, search_dirs: list[str]) -> str | None:
    """The executable: AZIZAM_PLAN_RUNNER_BIN_DIR first, then the row's own install places, then PATH (a service
    manager's PATH often lacks a per-user install directory)."""
    if "/" in name:
        return name if os.access(name, os.X_OK) else None
    directories = [str(bin_dir()), *search_dirs]
    for directory in directories:
        if not directory:
            continue
        candidate = Path(directory).expanduser() / (name + ".exe" if os.name == "nt" and not name.endswith(".exe") else name)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def runner_handle(kind: SubscriptionKind, state_dir: Path) -> RunnerHandle:
    """The handle of a kind's runner. The run's HOME, temporary and working directories are the gateway's own, so the
    CLI cannot reach the files of the account the gateway runs as; the config home is the only place it signs in from."""
    runner = kind.row["runner"]
    home = runner_home(kind)
    base = Path(state_dir) / "plan-runner" / kind.id
    workdir, run_home, scratch = base / "work", base / "home", base / "tmp"
    for directory in (workdir, run_home, scratch):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(run_home), "TMPDIR": str(scratch),
           "LANG": "C.UTF-8", "XDG_CONFIG_HOME": str(run_home / ".config"), "XDG_CACHE_HOME": str(run_home / ".cache"),
           "XDG_DATA_HOME": str(run_home / ".local" / "share")}
    env.update({str(key): str(value) for key, value in (runner.get("env") or {}).items()})
    env[str(runner["home_env"])] = str(home)
    executable = _resolve_executable(str(runner["executable"]), [str(item) for item in runner.get("search_dirs") or ()])
    return RunnerHandle(kind.id, executable, home, workdir, env)


async def read_status(kind: SubscriptionKind, handle: RunnerHandle) -> dict[str, Any]:
    """Run the CLI's own status command and keep only the named fields (signed_in, method, plan)."""
    runner = kind.row["runner"]
    unavailable = SubscriptionError(503, "plan_runner_unavailable",
                                    f"The gateway could not run {kind.display_name}'s sign-in check. The owner can check that it is installed.")
    try:
        process = await asyncio.create_subprocess_exec(
            *handle.command(*[str(item) for item in runner["status_argv"]]), cwd=str(handle.workdir), env=dict(handle.env),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        raise unavailable from None
    try:
        raw = await asyncio.wait_for(process.stdout.read(_STATUS_LIMIT), STATUS_TIMEOUT_SECONDS)
        await asyncio.wait_for(process.wait(), STATUS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise unavailable from None
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise unavailable from None
    if not isinstance(answer, Mapping):
        raise unavailable
    names = runner["status_fields"]
    method, plan = answer.get(str(names["method"])), answer.get(str(names["plan"]))
    return {"signed_in": answer.get(str(names["signed_in"])) is True,
            "method": method if isinstance(method, str) else None,
            "plan": plan if isinstance(plan, str) and plan.replace("_", "").replace("-", "").isalnum() else None}


def runner_home(kind: SubscriptionKind) -> Path:
    runner = kind.row["runner"]
    return homes_root() / str(runner["home_dir"]) / str(runner["home_slot"])


@dataclass
class LoginProcess:
    """One in-memory attempt. Output is discarded after the authorize address is read."""

    process: asyncio.subprocess.Process
    attempt_id: str
    drain: asyncio.Task | None = None
    deadline: asyncio.TimerHandle | None = None

    async def close(self) -> None:
        if self.deadline is not None:
            self.deadline.cancel()
        if self.drain is None:
            self.drain = asyncio.create_task(_discard_output(self.process.stdout))
        if self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await self.process.wait()
        if self.drain is not None:
            await self.drain


async def _discard_output(stream) -> None:
    while await stream.read(4096):
        pass


async def _authorize_url(stream, prefix: str) -> str:
    # Long addresses can wrap across lines. A prompt can arrive without its
    # final newline, so a short quiet read also marks the end of an address.
    buffer = ""
    expected = urlsplit(prefix)
    while len(buffer) < _STATUS_LIMIT:
        idle = False
        try:
            chunk = await asyncio.wait_for(stream.read(4096), 0.15 if "https://" in buffer else 20)
        except asyncio.TimeoutError:
            chunk, idle = b"", True
        buffer += chunk.decode("utf-8", errors="replace")
        for match in re.finditer(r"https://", buffer):
            lines = buffer[match.start():].replace("\r", "").split("\n")
            address = lines[0].split()[0]
            for line in lines[1:]:
                part = line.strip()
                if not part or any(c.isspace() for c in part) or part.startswith("https://"):
                    break
                address += part
            if chunk and not any(c.isspace() for c in lines[-1]):
                continue
            actual = urlsplit(address)
            if (actual.scheme == "https" and actual.netloc == expected.netloc
                    and actual.path == expected.path and not actual.fragment):
                return address
        if not chunk and not idle:
            break
    raise SubscriptionError(502, "plan_sign_in_unavailable", "The sign-in page could not be opened. Start again.")


class CliSignInMechanism:
    """The CLI owns OAuth; the gateway supplies the browser address and pasted code."""

    interactive = True
    paste_kind = "code"
    revokes_sign_in = False
    credential_kind = "runner"

    def validate(self, row: Mapping[str, Any], *, source: Any) -> None:
        runner = row.get("runner")
        if not isinstance(runner, Mapping) or not all(runner.get(key) for key in _REQUIRED):
            raise ValueError(f"{source}: runner needs {', '.join(_REQUIRED)}")
        for key in ("home_dir", "home_slot"):
            value = str(runner[key])
            if "/" in value or value in {".", ".."}:
                raise ValueError(f"{source}: runner.{key} must be one path segment")
        if not isinstance(runner["status_argv"], list) or not isinstance(runner["status_fields"], Mapping) or not all(
                runner["status_fields"].get(key) for key in ("signed_in", "method", "plan")):
            raise ValueError(f"{source}: runner.status_argv must be a list and status_fields must name signed_in, method and plan")
        if not isinstance(runner["login_argv"], list):
            raise ValueError(f"{source}: runner.login_argv must be a list")
        for key in ("login_url_prefix", "install_page_url"):
            address = urlsplit(str(runner[key]))
            if address.scheme != "https" or not address.netloc:
                raise ValueError(f"{source}: runner.{key} must be an https address")
        if not isinstance(runner.get("env") or {}, Mapping):
            raise ValueError(f"{source}: runner.env must be a mapping")
        if not isinstance(runner.get("search_dirs") or [], list):
            raise ValueError(f"{source}: runner.search_dirs must be a list of directories")

    def handle(self, ctx: ConnectionContext) -> RunnerHandle:
        return runner_handle(ctx.kind, ctx.store.state_dir())

    @staticmethod
    def parse_callback(value: str) -> dict[str, str]:
        return {"code": value.strip()}

    def pending(self, ctx: ConnectionContext, attempt: Attempt, values: Any) -> bool:
        login = ctx.runtime.get("login")
        return bool(not values.get("code") and login is not None
                    and login.attempt_id == attempt.attempt_id and login.process.returncode is None)

    async def close(self, ctx: ConnectionContext) -> None:
        login = ctx.runtime.pop("login", None)
        if login is not None:
            await login.close()

    async def start(self, ctx: ConnectionContext, attempt: Attempt) -> ConnectStart | None:
        await self.close(ctx)
        handle = await asyncio.to_thread(self.handle, ctx)
        runner = ctx.kind.row["runner"]
        if handle.executable is None:
            raise SubscriptionError(503, "plan_runner_unavailable", str(runner["missing_binary_message"]))
        status = await read_status(ctx.kind, handle)
        if status["signed_in"] and status["method"] == runner["expected_method"]:
            return None  # Existing owners connect with the same status check as before.
        try:
            process = await asyncio.create_subprocess_exec(
                *handle.command(*runner["login_argv"]), cwd=str(handle.workdir),
                env={**handle.env, "BROWSER": "/bin/true", "COLUMNS": "4000"},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise SubscriptionError(503, "plan_runner_unavailable", str(runner["missing_binary_message"])) from None
        login = LoginProcess(process, attempt.attempt_id)
        ctx.runtime["login"] = login
        try:
            address = await asyncio.wait_for(_authorize_url(process.stdout, str(runner["login_url_prefix"])), 20)
            login.drain = asyncio.create_task(_discard_output(process.stdout))
            login.deadline = asyncio.get_running_loop().call_later(
                LOGIN_TTL_SECONDS, lambda: asyncio.create_task(login.close()))
            return ConnectStart(owner_action={"authorize_url": address, "expires_in": LOGIN_TTL_SECONDS,
                                             "paste_kind": "code"},
                                state=attempt.attempt_id, mechanism_state={}, ttl=LOGIN_TTL_SECONDS)
        except asyncio.TimeoutError:
            await self.close(ctx)
            raise SubscriptionError(504, "plan_sign_in_unavailable", "The sign-in page took too long to open. Start again.") from None
        except BaseException:
            await self.close(ctx)
            raise

    async def finish(self, ctx: ConnectionContext, attempt: Attempt, values: Any) -> ConnectionFacts:
        if attempt.operation == "import":
            raise SubscriptionError(400, "import_unsupported", "This plan has no sign-in file to import. Connect it instead.")
        kind = ctx.kind
        handle = await asyncio.to_thread(self.handle, ctx)
        login = ctx.runtime.get("login")
        refusal = SubscriptionError(409, "plan_sign_in_refused",
                                    f"{kind.display_name} didn't accept that code. Start the sign-in again.")
        if attempt.attempt_id:
            if login is None or login.attempt_id != attempt.attempt_id:
                raise refusal
            try:
                code = str(values.get("code") or "").strip()
                if code and login.process.returncode is None:
                    login.process.stdin.write((code + "\n").encode())
                    await login.process.stdin.drain()
                result = await asyncio.wait_for(login.process.wait(), LOGIN_FINISH_SECONDS)
                if result != 0:
                    raise refusal
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                raise refusal from None
            finally:
                await self.close(ctx)
        status = await read_status(kind, handle)
        if not status["signed_in"] or status["method"] != kind.row["runner"]["expected_method"]:
            if attempt.attempt_id:
                raise refusal
            code = "plan_sign_in_method" if status["signed_in"] else "plan_not_signed_in"
            raise SubscriptionError(409, code,
                                    f"Start the sign-in again and sign in with your {kind.display_name} plan, not an API key or Console account.")
        return ConnectionFacts(
            metadata={"client_id": "", "host_id": "", "scopes": [status["plan"]] if status["plan"] else [],
                      "email": None, "subject": None},
            credential=Credential(self.credential_kind, handle))

    async def acquire(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Credential:
        handle = await asyncio.to_thread(self.handle, ctx)
        if not handle.home.is_dir():
            raise SubscriptionError(503, "plan_connection_invalid",
                                    f"The gateway's {ctx.kind.display_name} sign-in is gone. The owner needs to sign in again.")
        return Credential(self.credential_kind, handle)

    def configured(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> bool:
        return connection["status"] == subscription_store.CONNECTED

    def status(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Mapping[str, Any]:
        # The scopes column holds the plan type the CLI reported at connect time (no email is ever kept).
        plan = connection["scopes"].split()
        return {"connected_at": connection["connected_at"], "plan_type": plan[0] if plan else None}

    async def disconnect(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Mapping[str, Any]:
        """The gateway stops using the plan. The CLI's own sign-in stays: only the owner signs it out.

        No upstream sign-out is tried, so the answer carries no revoked_upstream claim either way."""
        await self.close(ctx)
        return {}


CONNECT_MECHANISMS["cli-sign-in"] = CliSignInMechanism()
