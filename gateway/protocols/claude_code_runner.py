"""The ``claude-code-runner`` request protocol: answer a chat turn by running the unmodified Claude Code binary.

Each HTTP request drives one model segment under the connection's config home. The binary alone reads its sign-in.
Built-in tools, project settings and slash commands remain off. With caller tools, a private loopback MCP server parks
the binary until the caller supplies results on a subsequent request. Without caller tools, the original lockdown is
unchanged. The init event must confirm exactly the configured tools and server and no API key (fail closed).

The output is turned into Responses events, so the shared Responses reply decoder serves /v1/responses as is and
translates it for /v1/chat/completions (and, through that, /v1/messages). Plan errors the binary reports become the
kind's named refusals (its ``errors`` table).

Conversations and the plan's prompt cache: the binary keeps each conversation as a session in its config home. After a
turn, the gateway remembers which session holds the whole conversation so far (keyed by connection generation, caller,
model, system prompt and the exact turns). A later request that repeats that conversation and adds user turns resumes
a copy of that session (``--resume <old> --fork-session --session-id <new>``) and sends only the new turns, so the
binary sends the same prefix as before and the plan's prompt cache is read. The old session is never changed, so a
retry or two requests at once are safe. Any other request starts a new session; earlier turns, when there are any, go
into its first message as a transcript. The memory is in-process and bounded; after a restart a conversation's next
turn starts a new session.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TYPE_CHECKING

from gateway.protocols import responses_plan, mcp_park
from gateway.provider_protocols import REQUEST_PROTOCOLS, DiscoveryError, RequestError
from gateway import transport_budgets
from gateway.reasoning_efforts import capped_effort
from gateway.subscription_kinds import Refusal, SubscriptionKind, usage_percent, usage_reset_at

if TYPE_CHECKING:
    from gateway.provider_connections import Credential

# Print mode, stream-json both ways, partial text events; then everything that could reach the host turned off.
# Checked against Claude Code 2.1.329 (`claude --help`).
_PRINT_FLAGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
_LOCKDOWN_FLAGS = ("--tools", "", "--disallowedTools", "*", "--strict-mcp-config", "--setting-sources", "",
                   "--disable-slash-commands", "--permission-mode", "dontAsk", "--permission-prompts", "none",
                   "--no-chrome")
_CHAT_FIELDS = frozenset({"model", "messages", "stream", "stream_options", "reasoning_effort", "n", "tools",
                          "tool_choice", "response_format", "parallel_tool_calls", "prompt_cache_key"})
_RESPONSES_FIELDS = frozenset({"model", "input", "instructions", "stream", "reasoning", "tools", "tool_choice", "text", "parallel_tool_calls", "store", "include", "prompt_cache_key"})
_LEAST_EFFORTS = ("minimal", "none")
# Line limit, kill grace, exit wait, discovery timeout and park TTL: the kind's runner section, else registry/transport_budgets.yml.
# Each window the runner reports: its length, and the models it counts when it counts only some of them.
_USAGE_WINDOWS = {"five_hour": (300, None), "seven_day": (10080, None), "seven_day_overage_included": (10080, None),
                  "seven_day_opus": (10080, "Opus"), "seven_day_sonnet": (10080, "Sonnet")}


def _usage_snapshot(raw: Any, *, now: int) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    windows = []
    for name, (minutes, models) in _USAGE_WINDOWS.items():
        window = raw.get(name)
        if not isinstance(window, Mapping):
            continue
        utilization, reset = window.get("utilization"), window.get("resetsAt")
        if type(utilization) not in (int, float) or type(reset) is not int:
            continue
        used = usage_percent(utilization * 100)
        resets_at = usage_reset_at(reset, now=now)
        if used is not None and resets_at is not None:
            windows.append({"used_percent": used, "window_minutes": minutes, "resets_at": resets_at,
                            **({"models": models} if models else {})})
    return {"captured_at": now, "windows": windows} if windows else None


# ------------------------------------------------------------------------------------------- request checks

def _utf8(text: str, *, param: str) -> str:
    """The binary reads UTF-8 JSON on stdin: a lone surrogate is the caller's bad input, not a gateway error."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise RequestError("plan_unsupported_content", "The text holds an invalid character (a lone surrogate).",
                           param=param) from None
    return text


def _text(content: Any, *, param: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return _utf8(content, param=param)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in ("text", "input_text", "output_text") \
                    and isinstance(part.get("text"), str):
                parts.append(_utf8(part["text"], param=param))
            else:
                kind = part.get("type") if isinstance(part, Mapping) else None
                raise RequestError("plan_unsupported_content", f"Only text is supported for this plan model, not {kind or 'this part'}.",
                                   param=param)
        return "".join(parts)
    raise RequestError("plan_unsupported_content", "Message content must be text.", param=param)


def _tools(body: Mapping[str, Any], *, responses: bool = False) -> list[dict]:
    choice = body.get("tool_choice")
    if choice not in (None, "none", "auto"):
        raise RequestError("plan_tool_choice_unsupported", "Only auto and none tool choice are supported.", param="tool_choice")
    if body.get("parallel_tool_calls") is False:
        raise RequestError("plan_parallel_tools_required", "Parallel tool calls must be enabled.", param="parallel_tool_calls")
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise RequestError("plan_tool_type_unsupported", "Tools must be function tools.", param="tools")
    out = []
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            raise RequestError("plan_tool_type_unsupported", "Only function tools are supported.", param="tools")
        function = {k: v for k, v in tool.items() if k != "type"} if responses else tool.get("function")
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str) or not function["name"]:
            raise RequestError("plan_tool_type_unsupported", "A function needs a name.", param="tools")
        if function.get("strict") is True:
            raise RequestError("plan_strict_tools_unsupported", "Strict tool schemas are not supported.", param="tools")
        out.append({"type": "function", "function": dict(function)})
    return [] if choice == "none" else out


def _call_turn(call_id, name, arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    return ("call", json.dumps([call_id, name, arguments], sort_keys=True, separators=(",", ":")))


def _unknown_fields(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    for key in body:
        if key not in allowed:
            raise RequestError("plan_unsupported_parameter", f"{key} is not supported for this plan model.", param=key)


def _add_turn(turns: list[tuple[str, str]], system: list[str], role: Any, content: Any, *, param: str) -> None:
    if role in ("system", "developer"):
        text = _text(content, param=param)
        if text:
            system.append(text)
    elif role in ("user", "assistant"):
        turns.append((role, _text(content, param=param)))
    else:
        raise RequestError("plan_unsupported_content", f"The role {role!r} is not supported for this plan model.", param=param)


def _chat_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    _unknown_fields(body, _CHAT_FIELDS)
    tools = _tools(body)
    if body.get("n") not in (None, 1):
        raise RequestError("plan_unsupported_parameter", "n must be 1 for this plan model.", param="n")
    fmt = body.get("response_format")
    if fmt is not None and not (isinstance(fmt, Mapping) and fmt.get("type") == "text"):
        raise RequestError("plan_unsupported_parameter", "Only text answers are supported for this plan model.",
                           param="response_format")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestError("plan_input_required", "messages must be a non-empty list.", param="messages")
    turns: list[tuple[str, str]] = []
    system: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise RequestError("plan_input_required", "Each message must be an object.", param="messages")
        if message.get("role") == "tool":
            turns.append(("result", json.dumps([message.get("tool_call_id"), message.get("content")], sort_keys=True)))
            continue
        if message.get("content") or not message.get("tool_calls"):
            _add_turn(turns, system, message.get("role"), message.get("content"), param="messages")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            turns.append(_call_turn(call.get("id"), function.get("name"), function.get("arguments")))
    effort = body.get("reasoning_effort")
    return {"system": "\n\n".join(system), "turns": turns, "effort": effort if isinstance(effort, str) and effort else None, "tools": tools, "affinity": body.get("prompt_cache_key")}


def _responses_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    _unknown_fields(body, _RESPONSES_FIELDS)
    tools = _tools(body, responses=True)
    if body.get("store") not in (None, False):
        raise RequestError("plan_store_unsupported", "Only store:false is supported.", param="store")
    if body.get("include") and body["include"] != ["reasoning.encrypted_content"]:
        raise RequestError("plan_include_unsupported", "Only reasoning.encrypted_content may be included.", param="include")
    text = body.get("text")
    if text is not None:
        fmt = text.get("format") if isinstance(text, Mapping) else None
        if not isinstance(text, Mapping) or set(text) - {"format"} or (fmt is not None and not (
                isinstance(fmt, Mapping) and fmt.get("type") == "text")):
            raise RequestError("plan_unsupported_parameter", "Only text answers are supported for this plan model.", param="text")
    system: list[str] = []
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise RequestError("plan_input_required", "instructions must be text.", param="instructions")
        if instructions:
            system.append(_utf8(instructions, param="instructions"))
    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        raise RequestError("plan_input_required", "input must be text or a non-empty list of messages.", param="input")
    turns: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, Mapping) and item.get("type") == "function_call":
            turns.append(_call_turn(item.get("call_id"), item.get("name"), item.get("arguments")))
            continue
        if isinstance(item, Mapping) and item.get("type") == "function_call_output":
            turns.append(("result", json.dumps([item.get("call_id"), item.get("output")], sort_keys=True)))
            continue
        if not isinstance(item, Mapping) or item.get("type", "message") != "message" or "role" not in item:
            kind = item.get("type") if isinstance(item, Mapping) else None
            raise RequestError("plan_unsupported_content", f"Only messages are supported for this plan model, not {kind or 'this item'}.",
                               param="input")
        _add_turn(turns, system, item.get("role"), item.get("content"), param="input")
    reasoning = body.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, Mapping):
        raise RequestError("plan_unsupported_reasoning_effort", "reasoning must be an object.", param="reasoning")
    effort = (reasoning or {}).get("effort")
    return {"system": "\n\n".join(system), "turns": turns, "effort": effort if isinstance(effort, str) and effort else None, "tools": tools, "affinity": body.get("prompt_cache_key")}


def _check_turns(turns: list[tuple[str, str]], param: str) -> None:
    if not turns or turns[-1][0] not in ("user", "result"):
        raise RequestError("plan_input_required", "The conversation must end with a user message.", param=param)


# ------------------------------------------------------------------------------------- conversation memory

def _delete_session_file(home: Path, session_id: str) -> None:
    """Delete the file the binary wrote for a session: ``<config home>/projects/<project>/<session id>.jsonl``.

    The id is the gateway's own UUID, so it cannot hold a path part. A file is deleted only when its folder, with
    links followed, lies inside the config home's own ``projects`` directory; a link there is removed itself, never
    its target. Anything else is left alone."""
    try:
        name = f"{uuid.UUID(session_id)}.jsonl"
        root = (home / "projects").resolve()
        for folder in root.iterdir():
            path = folder / name
            if folder.resolve().parent == root and folder.is_dir() and (path.is_symlink() or path.is_file()):
                path.unlink()
    except (ValueError, OSError):
        pass


class _SessionBook:
    """Conversation digest -> the session that holds exactly that conversation. Bounded, expiring, in-process.

    A session that leaves the book (expired, dropped, replaced, pushed out, cleared) loses its file in its config home,
    so a conversation's transcript stays on disk no longer than the session can be resumed."""

    def __init__(self, limit: int = transport_budgets.section("plan_runner")["session_limit"]) -> None:
        self._entries: OrderedDict[str, tuple[str, float, Path | None]] = OrderedDict()
        self._limit = limit

    def _forget(self, entry: tuple[str, float, Path | None]) -> None:
        if entry[2] is not None:
            _delete_session_file(entry[2], entry[0])

    def find(self, key: str, now: float) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[1] < now:
            self.drop(key)
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: str, session_id: str, now: float, ttl: float, home: Path | None = None) -> None:
        if ttl <= 0:
            if home is not None:
                _delete_session_file(home, session_id)
            return
        self.sweep(now)
        previous = self._entries.pop(key, None)
        if previous is not None and previous[0] != session_id:
            self._forget(previous)
        self._entries[key] = (session_id, now + ttl, home)
        self._entries.move_to_end(key)
        while len(self._entries) > self._limit:
            self._forget(self._entries.popitem(last=False)[1])

    def holds(self, session_id: str) -> bool:
        return any(entry[0] == session_id for entry in self._entries.values())

    def sweep(self, now: float) -> None:
        for key in [key for key, entry in self._entries.items() if entry[1] < now]:
            self.drop(key)

    def drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._forget(entry)

    def clear(self) -> None:
        for entry in self._entries.values():
            self._forget(entry)
        self._entries.clear()


def _conversation_key(scope: list[Any], turns: list[tuple[str, str]]) -> str:
    material = json.dumps([scope, [[role, text.strip()] for role, text in turns]], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _transcript_blocks(head: list[tuple[str, str]], tail: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Earlier turns for a new session: one text block per turn, then the new user turns."""
    blocks = [{"type": "text", "text": "The conversation so far, oldest turn first. Answer the last user turn."}]
    blocks += [{"type": "text", "text": f"[{role}]\n{text}"} for role, text in head]
    blocks += [{"type": "text", "text": f"[user]\n{text}"} for _, text in tail]
    return blocks


# --------------------------------------------------------------------------------------------- one run

def _sse(event: Mapping[str, Any]) -> bytes:
    return (b"event: " + str(event["type"]).encode() + b"\ndata: "
            + json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n\n")


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _responses_usage(usage: Any) -> dict[str, Any] | None:
    """The binary's usage (fresh input, cache writes, cache reads, output, thinking) in the Responses shape:
    input counts every prompt token, cached counts the ones read from the prompt cache, cache_creation the ones
    written to it (LiteLLM's name for writes in prompt_tokens_details)."""
    if not isinstance(usage, Mapping):
        return None
    fresh, written, read = (_count(usage.get(key)) for key in
                            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), Mapping) else {}
    output, thinking = _count(usage.get("output_tokens")), _count(details.get("thinking_tokens"))
    return {"input_tokens": fresh + written + read,
            "input_tokens_details": {"cached_tokens": read, "cache_creation_tokens": written},
            "output_tokens": output, "output_tokens_details": {"reasoning_tokens": thinking},
            "total_tokens": fresh + written + read + output}


class _Headers(dict):
    pass


class _NoClient:
    async def aclose(self) -> None:
        return None


class _Segment:
    """An HTTP segment can close only the process lease it acquired, never a later continuation's lease."""

    def __init__(self, run):
        self._run = run
        self._epoch = run._epoch

    def __getattr__(self, name):
        return getattr(self._run, name)

    async def aclose(self):
        if self._epoch == self._run._epoch:
            await self._run.aclose()


class _Run:
    """One run of the binary, shaped as the streamed upstream reply the shared executor reads (status, bytes, close)."""

    status_code = 200

    def __init__(self, runner: "ClaudeCodeRunner", kind: SubscriptionKind, payload: Mapping[str, Any],
                 credential: "Credential", *, busy: bool) -> None:
        self.headers = _Headers()
        self.usage_snapshot: dict[str, Any] | None = None
        self._runner, self._kind, self._payload, self._credential = runner, kind, payload, credential
        self._busy = busy
        self._process: asyncio.subprocess.Process | None = None
        self._prompt_file: Path | None = None
        self._released = busy  # a busy run never held a slot
        self._closed = False
        self._model = str(payload["model"])
        self._response_id = "resp_" + secrets.token_hex(12)
        self._item_id = "msg_" + secrets.token_hex(12)
        self._created_at = int(time.time())
        self._opened = False
        self._text: list[str] = []
        self._attempted: list[_Plan] = []
        self._park = None
        self._parked = False
        self._handed_off = False
        self._config_file = None
        self._scratch_dir = None
        self._expiry_task = None
        self._calls = {}
        self._blocks = {}
        self._stop_reason = None
        self._step_calls: list[dict[str, Any]] = []  # usage of each API call this step made, from its stream events
        self._current_plan = None
        self._history_key = None
        self._scope = None
        self._park_time = 0.0
        self._continuation = False
        self._epoch = 0
        self.started = "rebuilt" if any(r == "result" for r, _ in payload["turns"]) else "new"
        self.headers["x-plan-turn-start"] = self.started

    # -- the executor's interface

    async def aread(self) -> bytes:
        return b""

    def aiter_bytes(self):
        return self._events()

    async def aclose(self) -> None:
        if not self._parked or not self._handed_off:
            self._closed = True
            await self._stop()

    # -- process lifetime

    async def _stop(self) -> None:
        await self._stop_process()
        if not self._released:
            self._released = True
            self._runner._release(self._kind.id)
        self._discard_unremembered_sessions()

    async def _stop_process(self, *, patience: float = 0.0) -> None:
        """End the process group (the binary and anything it started) and delete the run's prompt file.

        ``patience`` is how long the binary may take to exit by itself first (after its result it is finishing its
        session file); then SIGTERM, then SIGKILL."""
        if self._expiry_task and self._expiry_task is not asyncio.current_task():
            self._expiry_task.cancel()
        self._parked = False
        process = self._process
        if process is not None and process.returncode is None and patience > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), patience)
        if process is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), self._budget("kill_grace_seconds"))
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        self._process = None
        if self._park is not None:
            self._park.fail_all("Tool run ended")
            self._park.close()
            self._park = None
        self._runner._parks.pop(id(self), None)
        if self._config_file is not None:
            self._config_file.unlink(missing_ok=True)
            self._config_file = None
        if self._prompt_file is not None:
            with contextlib.suppress(OSError):
                self._prompt_file.unlink()
            self._prompt_file = None
        if self._scratch_dir is not None:
            with contextlib.suppress(OSError):
                self._scratch_dir.rmdir()
            self._scratch_dir = None

    # -- Responses events

    def _response(self, status: str, **extra: Any) -> dict[str, Any]:
        return {"id": self._response_id, "object": "response", "created_at": self._created_at, "status": status,
                "model": self._model, "metadata": {"plan_turn_start": self.started}, **extra}

    def _open(self) -> list[bytes]:
        if self._opened:
            return []
        self._opened = True
        return [_sse({"type": "response.created", "response": self._response("in_progress", output=[])}),
                _sse({"type": "response.output_item.added", "output_index": 0,
                      "item": {"type": "message", "id": self._item_id, "role": "assistant", "status": "in_progress", "content": []}}),
                _sse({"type": "response.content_part.added", "item_id": self._item_id, "output_index": 0, "content_index": 0,
                      "part": {"type": "output_text", "text": "", "annotations": []}})]

    def _delta(self, text: str) -> list[bytes]:
        self._text.append(text)
        return [*self._open(), _sse({"type": "response.output_text.delta", "item_id": self._item_id, "output_index": 0,
                                     "content_index": 0, "delta": text})]

    def _completed(self, usage: Any) -> list[bytes]:
        text = "".join(self._text)
        part = {"type": "output_text", "text": text, "annotations": []}
        item = {"type": "message", "id": self._item_id, "role": "assistant", "status": "completed", "content": [part]}
        out = self._open()
        out += [_sse({"type": "response.output_text.done", "item_id": self._item_id, "output_index": 0, "content_index": 0, "text": text}),
                _sse({"type": "response.content_part.done", "item_id": self._item_id, "output_index": 0, "content_index": 0, "part": part}),
                _sse({"type": "response.output_item.done", "output_index": 0, "item": item})]
        response = self._response("completed", output=[item, *self._calls.values()])
        converted = _responses_usage(usage)
        if converted:
            response["usage"] = converted
        out.append(_sse({"type": "response.completed", "response": response}))
        return out

    def _step_usage(self) -> dict[str, Any] | None:
        """This step's API calls summed (a step that ends at a tool call has no result event of its own)."""
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        if not any(key in call for call in self._step_calls for key in keys):
            return None
        summed: dict[str, Any] = {key: sum(_count(call.get(key)) for call in self._step_calls) for key in keys}
        summed["output_tokens_details"] = {"thinking_tokens": sum(
            _count(call["output_tokens_details"].get("thinking_tokens")) for call in self._step_calls
            if isinstance(call.get("output_tokens_details"), Mapping))}
        return summed

    def _failed(self, code: str, usage: Any = None) -> bytes:
        refusal: Refusal = self._kind.refusal_for_failed(code)
        response = self._response("failed", output=[], error={"code": code, "message": refusal.message})
        converted = _responses_usage(usage)
        if converted:
            response["usage"] = converted
        return _sse({"type": "response.failed", "response": response})

    # -- the run

    async def _spawn(self, argv: list[str], line: bytes) -> asyncio.subprocess.Process:
        handle = self._credential.value
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(handle.workdir), env={**handle.env, **({"MCP_TOOL_TIMEOUT": str(self._park_ttl_ms()), "MAX_MCP_OUTPUT_TOKENS": "200000",
                                    "ENABLE_TOOL_SEARCH": "false"} if self._park else {})}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=self._budget("line_limit_bytes"))
        self._process = process
        try:
            process.stdin.write(line)
            await process.stdin.drain()
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return process

    async def _events(self):
        epoch = self._epoch
        if self._busy:
            yield self._failed("runner_busy")
            return
        runner = self._kind.row["runner"]
        deadline = asyncio.get_running_loop().time() + float(runner.get("timeout_seconds") or 600)
        plan = self._current_plan if self._continuation else self._runner._plan(
            self._kind, self._payload, self._credential, fresh=self.started == "rebuilt")
        try:
            if self._continuation:
                async for item in self._read_run(plan, deadline):
                    if isinstance(item, bytes):
                        yield item
                return
            for attempt in (plan, None):
                if attempt is None:
                    if plan.resume is None or self._opened:
                        break
                    # The remembered session is gone (the binary cleaned it up): start a new one with the transcript.
                    self._runner._sessions.drop(plan.lookup_key)
                    attempt = self._runner._plan(self._kind, self._payload, self._credential, fresh=True)
                outcome = None
                self._attempted.append(attempt)
                async for item in self._one_run(attempt, deadline):
                    if isinstance(item, bytes):
                        yield item
                    else:
                        outcome = item
                if outcome != "session_lost":
                    return
            yield self._failed("runner_session_lost")
        finally:
            if epoch == self._epoch:
                if not self._parked or not self._handed_off:
                    await self._stop()
                    self._discard_unremembered_sessions()

    def _discard_unremembered_sessions(self) -> None:
        """A run that did not end in a remembered session (it failed, timed out or was cancelled) may still have left
        its session file: no later request can resume it, so it goes now."""
        for attempt in self._attempted:
            if attempt.home is not None and not self._runner._sessions.holds(attempt.session_id):
                _delete_session_file(attempt.home, attempt.session_id)
        self._attempted.clear()

    async def _one_run(self, plan: "_Plan", deadline: float):
        handle = self._credential.value
        try:
            prompt_dir = handle.workdir.parent / "tmp"
            prompt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._payload.get("tools"):
                self._scratch_dir = prompt_dir / f"run-{uuid.uuid4().hex}"
                self._scratch_dir.mkdir(mode=0o700)
                prompt_dir = self._scratch_dir
            self._prompt_file = prompt_dir / f"system-{uuid.uuid4().hex}.txt"
            fd = os.open(self._prompt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle_file:
                handle_file.write(plan.system)
            lockdown = list(_LOCKDOWN_FLAGS)
            if self._payload.get("tools"):
                self._park = await mcp_park.register(self._payload["tools"])
                self._config_file = prompt_dir / f"mcp-{uuid.uuid4().hex}.json"
                fd = os.open(self._config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as config:
                    json.dump(self._park.mcp_config(self._park_ttl_ms()), config)
                del lockdown[2:4]
                lockdown += ["--mcp-config", str(self._config_file), "--allowedTools", "mcp__caller__*"]
            argv = handle.command(*_PRINT_FLAGS, "--include-partial-messages", *lockdown,
                                  "--model", str(self._payload["model"]), *(("--effort", plan.effort) if plan.effort else ()),
                                  "--system-prompt-file", str(self._prompt_file), "--session-id", plan.session_id,
                                  *(("--resume", plan.resume, "--fork-session") if plan.resume else ()))
            message = {"type": "user", "message": {"role": "user", "content": plan.blocks},
                       "parent_tool_use_id": None, "session_id": ""}
            process = await self._spawn(argv, json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        except Exception:  # noqa: BLE001 - an executable that is missing or cannot start: one named refusal
            await self._stop_process()
            yield self._failed("runner_unavailable")
            return
        self._current_plan = plan
        async for item in self._read_run(plan, deadline, initialized=False):
            yield item

    def _park_ttl_ms(self):
        return int(float(self._budget("park_ttl_seconds")) * 1000)

    def _budget(self, key: str) -> Any:
        return transport_budgets.runner_budget(self._kind.row["runner"], key)

    async def _read_run(self, plan, deadline, initialized=True):
        process = self._process
        error_code: str | None = None
        rate_limited = False
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0 or self._closed:
                await self._stop_process()
                if not self._closed:
                    yield self._failed("runner_timeout")
                return
            try:
                line = await asyncio.wait_for(process.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                continue
            except (ValueError, asyncio.LimitOverrunError):
                await self._stop_process()
                yield self._failed("runner_exited")
                return
            if not line:
                await self._stop_process()
                yield self._failed("runner_exited")
                return
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, Mapping):
                continue
            kind = event.get("type")
            if not initialized and kind != "system" and (
                    (kind == "stream_event" and event.get("parent_tool_use_id") is None)
                    or (kind == "assistant" and event.get("parent_tool_use_id") is None)
                    or (kind == "result" and not (plan.resume and event.get("subtype") == "error_during_execution"
                                                  and not event.get("num_turns")))):
                # Text, an answer or a result with no init event first: the lockdown was never confirmed. (The one
                # exception is the binary's error for a resumed session it no longer has, which carries no text.)
                await self._stop_process()
                yield self._failed("runner_unsafe")
                return
            if kind == "system" and event.get("subtype") == "init":
                expected = ["mcp__caller__" + t["name"] for t in self._park.tools] if self._park else []
                servers = event.get("mcp_servers") or []
                safe_servers = (len(servers) == 1 and isinstance(servers[0], Mapping)
                                and servers[0].get("name") == "caller" and servers[0].get("status") == "connected") if self._park else not servers
                if sorted(event.get("tools") or []) != sorted(expected) or len(event.get("tools") or []) != len(expected) \
                        or not safe_servers or event.get("apiKeySource") not in (None, "none"):
                    await self._stop_process()
                    yield self._failed("runner_unsafe")
                    return
                initialized = True
            elif kind == "stream_event" and event.get("parent_tool_use_id") is None:
                raw = event.get("event") if isinstance(event.get("event"), Mapping) else {}
                delta = raw.get("delta") if isinstance(raw.get("delta"), Mapping) else {}
                event_type = raw.get("type")
                index = raw.get("index")
                if self._park and event_type == "content_block_start" and (raw.get("content_block") or {}).get("type") == "tool_use":
                    block = raw["content_block"]
                    name = self._park.to_caller_name(block["name"])
                    item = {"type": "function_call", "id": block["id"], "call_id": block["id"], "name": name,
                            "arguments": json.dumps(block["input"]) if block.get("input") else "", "status": "in_progress"}
                    self._blocks[index] = item
                    for piece in self._open():
                        yield piece
                    yield _sse({"type": "response.output_item.added", "output_index": len(self._blocks), "item": dict(item)})
                elif self._park and event_type == "content_block_delta" and delta.get("type") == "input_json_delta":
                    item = self._blocks[index]
                    item["arguments"] += delta.get("partial_json", "")
                    yield _sse({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                                "output_index": list(self._blocks).index(index) + 1, "delta": delta.get("partial_json", "")})
                elif self._park and event_type == "content_block_stop" and index in self._blocks:
                    item = self._blocks[index]
                    args = json.loads(item["arguments"] or "{}")
                    item["arguments"] = json.dumps(args, separators=(",", ":"))
                    item["status"] = "completed"
                    self._calls[item["call_id"]] = item
                    self._park.expect(item["call_id"], item["name"], args)
                    yield _sse({"type": "response.function_call_arguments.done", "item_id": item["id"],
                                "output_index": list(self._blocks).index(index) + 1, "arguments": item["arguments"]})
                    yield _sse({"type": "response.output_item.done", "output_index": list(self._blocks).index(index) + 1, "item": dict(item)})
                elif event_type == "message_delta":
                    self._stop_reason = delta.get("stop_reason")
                    if self._step_calls and isinstance(raw.get("usage"), Mapping):
                        self._step_calls[-1].update(raw["usage"])
                elif self._park and event_type == "message_stop" and self._stop_reason == "tool_use":
                    await self._runner._park_run(self)
                    for piece in self._completed(self._step_usage()):
                        if piece.startswith(b"event: response.completed\n"):
                            self._handed_off = True
                        yield piece
                    return
                elif raw.get("type") == "content_block_delta" and delta.get("type") == "text_delta" \
                        and isinstance(delta.get("text"), str) and delta["text"]:
                    for piece in self._delta(delta["text"]):
                        yield piece
                elif raw.get("type") == "message_start":
                    message = raw.get("message") if isinstance(raw.get("message"), Mapping) else {}
                    self._step_calls.append(dict(message["usage"]) if isinstance(message.get("usage"), Mapping) else {})
                    observed = (raw.get("message") or {}).get("model") if isinstance(raw.get("message"), Mapping) else None
                    if isinstance(observed, str) and observed and not observed.startswith("<"):
                        self._model = observed
            elif kind == "assistant" and event.get("parent_tool_use_id") is None:
                if isinstance(event.get("error"), str) and event["error"]:
                    error_code = event["error"]
            elif kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                # The last event that carries windows wins; one without them (a bare rejection) keeps the earlier reading.
                reading = _usage_snapshot(
                    info.get("unifiedWindows") if isinstance(info, Mapping) else None, now=int(time.time()))
                if reading is not None:
                    self.usage_snapshot = reading
                if isinstance(info, Mapping) and info.get("status") == "rejected":
                    rate_limited = True
            elif kind == "result":
                await self._stop_process(patience=max(0.0, min(self._budget("exit_wait_seconds"), deadline - loop.time())))
                usage = event.get("usage")
                if event.get("is_error") or event.get("subtype") != "success":
                    if plan.resume and event.get("subtype") == "error_during_execution" and not event.get("num_turns") \
                            and not self._opened:
                        yield "session_lost"
                        return
                    status = event.get("api_error_status")
                    code = error_code or ("rate_limit" if rate_limited else None) or {
                        429: "rate_limit", 401: "authentication_failed", 400: "invalid_request"}.get(status) or "runner_exited"
                    yield self._failed(code, usage)
                    return
                pieces = self._delta(event["result"]) if not self._text and isinstance(event.get("result"), str) \
                    and event["result"] else []
                # The result counts the whole turn. A continued run already reported its earlier steps' calls, so
                # it reports this step's calls; a run that never parked reports the result as it is.
                pieces += self._completed((self._step_usage() if self._continuation else None) or usage)
                # Remembered before the last bytes go out: a reader may stop reading at the terminal event.
                self._runner._remember(plan, "".join(self._text))
                for piece in pieces:
                    yield piece
                return


class _Plan:
    """How one request runs: the session to resume (or none), the new session's id and the first message's blocks."""

    def __init__(self, *, scope: list[Any], turns: list[tuple[str, str]], resume: str | None, lookup_key: str,
                 blocks: list[dict[str, str]], system: str, effort: str | None, ttl: float, home: Path | None) -> None:
        self.scope, self.turns, self.resume, self.lookup_key = scope, turns, resume, lookup_key
        self.blocks, self.system, self.effort, self.ttl, self.home = blocks, system, effort, ttl, home
        self.session_id = str(uuid.uuid4())


# ----------------------------------------------------------------------------------------- the protocol

class ClaudeCodeRunner:
    def __init__(self) -> None:
        self._sessions = _SessionBook()
        self._running: dict[str, int] = {}
        self._parks = OrderedDict()

    # -- data

    def validate(self, row: Mapping[str, Any], *, source: Any) -> None:
        runner = row.get("runner")
        if not isinstance(runner, Mapping):
            raise ValueError(f"{source}: the runner protocol needs a runner section")
        for key in ("max_concurrent", "timeout_seconds"):
            value = runner.get(key)
            if type(value) is not int or value < 1:
                raise ValueError(f"{source}: runner.{key} must be a positive whole number")
        ttl = runner.get("session_ttl_seconds", 0)
        if type(ttl) is not int or ttl < 0:
            raise ValueError(f"{source}: runner.session_ttl_seconds must be a whole number, 0 or more")
        if not isinstance(runner.get("default_system_prompt"), str) or not runner["default_system_prompt"].strip():
            raise ValueError(f"{source}: runner.default_system_prompt must be text")
        transport_budgets.check_runner_budgets(runner, source=source)

    # -- request preparation (local only)

    def check_request(self, surface: str, body: Mapping[str, Any], kind: SubscriptionKind, slug: str) -> dict[str, Any]:
        payload = _responses_payload(body) if surface == "responses" else _chat_payload(body)
        turns = list(payload["turns"])
        result_ids = []
        while turns and turns[-1][0] == "result":
            result_ids.append(json.loads(turns.pop()[1])[0])
        call_ids = []
        while turns and turns[-1][0] == "call":
            call_ids.append(json.loads(turns.pop()[1])[0])
        missing = sorted(set(call_ids) - set(result_ids))
        if missing and set(result_ids) <= set(call_ids):
            raise RequestError("plan_tool_results_missing", "Missing tool results: " + ", ".join(missing),
                               param="input" if surface == "responses" else "messages")
        _check_turns(payload["turns"], "input" if surface == "responses" else "messages")
        payload["stream"] = bool(body.get("stream"))
        return payload

    def apply_model_facts(self, surface: str, payload: dict[str, Any], advertised: list[str],
                          body: Mapping[str, Any], *, top_effort: str | None = None) -> Mapping[str, Any]:
        # The existing kind row still lists these fields as dropped (updated in the next slice).
        # Validate and retain their original values rather than silently losing caller intent.
        payload["tools"] = _tools(body, responses=surface == "responses")
        payload["affinity"] = body.get("prompt_cache_key")
        if surface == "responses":
            if body.get("store") not in (None, False):
                raise RequestError("plan_store_unsupported", "Only store:false is supported.", param="store")
            if body.get("include") and body["include"] != ["reasoning.encrypted_content"]:
                raise RequestError("plan_include_unsupported", "Only reasoning.encrypted_content may be included.", param="include")
        effort = payload.get("effort")
        facts: dict[str, Any] = {}
        if effort and not re.fullmatch(r"[a-z]+", effort):
            # The effort becomes a command-line value: only a plain word may reach the binary.
            raise RequestError("plan_unsupported_reasoning_effort", f"The reasoning effort {effort!r} is not a known effort.",
                               param="reasoning.effort" if surface == "responses" else "reasoning_effort")
        lowered = capped_effort(effort, advertised, top_effort)
        if lowered is not None:
            payload["effort"] = lowered
            facts["plan_effort_lowered_from"] = effort
            effort = lowered
        if effort in _LEAST_EFFORTS and advertised and effort not in advertised:
            # The Anthropic route sends "minimal" when thinking is off: the model's least effort is the nearest.
            payload["effort"] = advertised[0]
            facts["plan_effort_raised_from"] = effort
        elif effort and advertised and effort not in advertised:
            raise RequestError("plan_unsupported_reasoning_effort",
                               f"This model does not support the reasoning effort {effort!r}. It supports: {', '.join(advertised)}.",
                               param="reasoning.effort" if surface == "responses" else "reasoning_effort")
        elif effort in _LEAST_EFFORTS:
            payload["effort"] = None
        return facts

    def prepare(self, payload: Mapping[str, Any], kind: SubscriptionKind, namespace: str, slug: str, *,
                caller: str = "") -> dict[str, Any]:
        system = str(payload.get("system") or "") or str(kind.row["runner"]["default_system_prompt"])
        return {**payload, "system": system, "model": slug, "caller": caller}

    def turn_state_header(self, kind: SubscriptionKind) -> str | None:
        """No turn state travels in headers here: the runner keeps the conversation in its own session (see prepare)."""
        return None

    def continues_turn(self, items: list[Any], start: int) -> bool:
        return False

    def _plan(self, kind: SubscriptionKind, payload: Mapping[str, Any], credential: "Credential", *,
              fresh: bool = False) -> _Plan:
        turns = [(str(role), str(text)) for role, text in payload["turns"]]
        scope = [credential.connection_id, credential.generation, payload.get("caller") or "", payload["model"],
                 payload["system"], payload.get("affinity")]
        last_reply = max((index for index, (role, _) in enumerate(turns) if role == "assistant"), default=-1)
        head, tail = turns[:last_reply + 1], turns[last_reply + 1:]
        lookup_key = _conversation_key(scope, head)
        resume = None if fresh or not head else self._sessions.find(lookup_key, time.monotonic())
        if resume:
            blocks = [{"type": "text", "text": text} for _, text in tail]
        elif head or any(role in ("call", "result") for role, _ in turns):
            blocks = _transcript_blocks(turns, [])
        else:
            blocks = [{"type": "text", "text": text} for _, text in tail]
        return _Plan(scope=scope, turns=turns, resume=resume, lookup_key=lookup_key, blocks=blocks,
                     system=str(payload["system"]), effort=payload.get("effort"),
                     ttl=float(kind.row["runner"].get("session_ttl_seconds") or 0), home=credential.value.home)

    def _remember(self, plan: _Plan, reply: str) -> None:
        self._sessions.put(_conversation_key(plan.scope, [*plan.turns, ("assistant", reply)]), plan.session_id,
                           time.monotonic(), plan.ttl, plan.home)

    def _take(self, kind: SubscriptionKind) -> bool:
        limit = int(kind.row["runner"]["max_concurrent"])
        if self._running.get(kind.id, 0) >= limit:
            return False
        self._running[kind.id] = self._running.get(kind.id, 0) + 1
        return True

    def _release(self, kind_id: str) -> None:
        self._running[kind_id] = max(0, self._running.get(kind_id, 0) - 1)

    def running(self, kind_id: str) -> int:
        return self._running.get(kind_id, 0)

    def forget_sessions(self) -> None:
        self._sessions.clear()

    # -- execution

    def _park_scope(self, payload, credential):
        return _conversation_key([credential.connection_id, credential.generation, payload.get("caller"),
                                  payload.get("affinity")], [])

    def _history(self, payload, turns):
        return _conversation_key([payload["system"], payload["model"], payload.get("effort"), payload.get("tools")], turns)

    async def _park_run(self, run):
        run._parked = True
        if not run._released:
            run._released = True
            self._release(run._kind.id)
        run._scope = self._park_scope(run._payload, run._credential)
        turns = list(run._payload["turns"])
        if run._text:
            turns.append(("assistant", "".join(run._text)))
        turns += [_call_turn(c["call_id"], c["name"], c["arguments"]) for c in run._calls.values()]
        run._history_key = self._history(run._payload, turns)
        run._park_time = time.monotonic()
        self._parks[id(run)] = run
        row = run._kind.row["runner"]
        for field, key, cap in (("caller", "park_limit_per_principal", 4), ("connection", "park_limit_per_connection", 8)):
            def same(other):
                return (other._payload.get("caller") == run._payload.get("caller") if field == "caller" else
                        other._credential.connection_id == run._credential.connection_id)
            matches = [p for p in self._parks.values() if same(p)]
            while len(matches) > int(row.get(key, cap)):
                await matches.pop(0)._stop()
        async def expire():
            # Process.wait also waits for inherited stdout pipes. A surviving child can keep those open after
            # the binary exits, so inspect the reaped leader's returncode before terminating the whole group.
            deadline = asyncio.get_running_loop().time() + run._park_ttl_ms() / 1000
            while run._process is not None and run._process.returncode is None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.1, remaining))
            await run._stop()
            run._discard_unremembered_sessions()
        run._expiry_task = asyncio.create_task(expire())

    async def close_parks(self):
        for park in list(self._parks.values()):
            if park._expiry_task:
                park._expiry_task.cancel()
            await park._stop()
            park._discard_unremembered_sessions()

    async def send(self, kind: SubscriptionKind, payload: Mapping[str, Any], credential: "Credential", new_client,
                   extra_headers: Mapping[str, str] | None = None):
        payload = dict(payload)
        affinity = {k.lower(): v for k, v in (extra_headers or {}).items()}.get("session-id")
        if affinity:
            payload["affinity"] = affinity
        turns = list(payload["turns"])
        results = []
        while turns and turns[-1][0] == "result":
            results.insert(0, json.loads(turns.pop()[1]))
        if results:
            ids = [r[0] for r in results]
            scope = self._park_scope(payload, credential)
            candidates = [park for park in self._parks.values() if park._scope == scope]
            history = self._history(payload, turns)
            # Only this conversation's park is a target: its open calls share an id with the results, or the
            # history before the results is what it answered. Another conversation's park stays parked.
            matching = [park for park in candidates
                        if set(ids).intersection(park._calls) or park._history_key == history]
            for park in matching:
                valid = (park._process is not None and park._process.returncode is None
                         and time.monotonic() - park._park_time < park._park_ttl_ms() / 1000
                         and history == park._history_key)
                if valid and set(ids) < set(park._calls):
                    missing = sorted(set(park._calls) - set(ids))
                    raise RequestError("plan_tool_results_missing", "Missing tool results: " + ", ".join(missing), param="input")
                if not valid or set(ids) != set(park._calls) or len(ids) != len(set(ids)):
                    await park._stop()
                    park._discard_unremembered_sessions()
                    continue
                if not self._take(kind):
                    return _NoClient(), _Segment(_Run(self, kind, payload, credential, busy=True))
                self._parks.pop(id(park), None)
                park._expiry_task.cancel()
                park._epoch += 1
                park._parked = False
                park._handed_off = False
                park._released = False
                park._closed = False
                park._continuation = True
                park._payload = payload
                park._response_id = "resp_" + secrets.token_hex(12)
                park._item_id = "msg_" + secrets.token_hex(12)
                park._opened = False
                park._text = []
                park._calls = {}
                park._blocks = {}
                park._stop_reason = None
                park._step_calls = []
                park.started = "continued"
                park.headers["x-plan-turn-start"] = park.started
                park._current_plan.turns = list(payload["turns"])
                for call_id, content in results:
                    # Responses input_text/input_image parts use the same MCP content contract as Chat.
                    if isinstance(content, list):
                        content = [{"type": "text", "text": p.get("text", "")} if p.get("type") == "input_text" else
                                   {"type": "image_url", "image_url": {"url": p.get("image_url")}} if p.get("type") == "input_image" else p
                                   for p in content]
                    park._park.resolve(call_id, content)
                return _NoClient(), _Segment(park)
        return _NoClient(), _Segment(_Run(self, kind, payload, credential, busy=not self._take(kind)))

    def quota_rejection(self, kind: SubscriptionKind, status: int, raw: bytes) -> bool:
        return responses_plan.quota_rejection(kind, status, raw)

    def classify_error(self, kind: SubscriptionKind, status: int, raw: bytes) -> Refusal:
        return responses_plan.classify_error(kind, status, raw)

    def open_reply(self, surface: str, model: str, body: Mapping[str, Any]):
        return responses_plan.open_reply(surface, model, body)

    def usage_facts(self, usage: Mapping[str, Any]) -> dict[str, int]:
        return responses_plan.usage_facts(usage)

    async def discover_models(self, kind: SubscriptionKind, credential: "Credential", client_context) -> dict[str, list[str]]:
        """The binary's own model list (its initialize answer): model id -> the efforts it takes. No inference is run."""
        handle = credential.value
        unavailable = DiscoveryError(502, "models_unavailable", "The model list could not be read. Try again shortly.")
        request_id = "models-" + secrets.token_hex(6)
        line = json.dumps({"type": "control_request", "request_id": request_id, "request": {"subtype": "initialize"}}) + "\n"
        try:
            process = await asyncio.create_subprocess_exec(
                *handle.command(*_PRINT_FLAGS, *_LOCKDOWN_FLAGS), cwd=str(handle.workdir), env=dict(handle.env),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                limit=transport_budgets.runner_budget(kind.row["runner"], "line_limit_bytes"))
        except Exception:  # noqa: BLE001 - not installed or not startable
            raise unavailable from None
        models: Any = None
        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + transport_budgets.runner_budget(kind.row["runner"], "discovery_timeout_seconds")
            while models is None and loop.time() < deadline:
                raw = await asyncio.wait_for(process.stdout.readline(), max(0.01, deadline - loop.time()))
                if not raw:
                    break
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                answer = event.get("response") if isinstance(event, Mapping) and event.get("type") == "control_response" else None
                if isinstance(answer, Mapping) and answer.get("request_id") == request_id:
                    inner = answer.get("response") if answer.get("subtype") == "success" else None
                    # Only the model rows are read; the account part of the answer is never looked at.
                    models = inner.get("models") if isinstance(inner, Mapping) else []
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError, ValueError):
            models = None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        if not isinstance(models, list):
            raise unavailable
        out: dict[str, list[str]] = {}
        for row in models:
            if not isinstance(row, Mapping) or row.get("disabled"):
                continue
            slug = row.get("resolvedModel") or row.get("value")
            if not isinstance(slug, str) or not slug or slug.startswith("<") or slug in out:
                continue
            levels = row.get("supportedEffortLevels") if row.get("supportsEffort") else []
            out[slug] = [level for level in levels or [] if isinstance(level, str) and level]
        if not out:
            raise unavailable
        return out


RUNNER = ClaudeCodeRunner()
REQUEST_PROTOCOLS["claude-code-runner"] = RUNNER
