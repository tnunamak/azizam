"""One round on a node-graph engine: submit a prompt, poll it on the gateway's cadence, harvest the selected files.

This is the engine side of every node-graph run. It is the same code for a caller that waits
(the old routes) and for a run the run core executes. It raises ``RoundFailure`` with a typed reason; the caller turns the
reason into its own answer (an HTTP response, a job failure). Nothing here names an engine or a workflow.

The cadence is data: the protocol description gives each run operation its ``poll`` (``ramp_s`` gaps, then
``interval_s``, at most ``max_polls`` status checks). A gap of 0 is an immediate check. The wait budget is charged only
with the gaps, never with the time a status call takes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

from gateway.engine_protocol import node_graph
from gateway.engine_protocol.description import Poll
from gateway.engine_protocol.errors import MalformedResponse, SelectionRefused, TransportFailure, UpstreamError
from gateway.engine_protocol.types import CanonicalRequest, JobRef, JobState

# Why a round ended without files.
SUBMIT_TIMEOUT = "submit_timeout"
UNREACHABLE = "unreachable"
DROPPED = "dropped"
REFUSED = "refused"                  # the engine answered the submit with an error status
NO_PROMPT_ID = "no_prompt_id"
EXECUTION_FAILED = "execution_failed"
WAIT_EXCEEDED = "wait_exceeded"
DISCONNECTED = "disconnected"
RESULT_REFUSED = "result_refused"

_OOM_MARKERS = ("cuda out of memory", "cuda oom", "out of memory")


def says_out_of_memory(text: Any, *, plain: bool = False) -> bool:
    """Whether an engine's error text reports that it ran out of memory (the text decides a retry; it is never passed on).

    ``plain`` leaves out the bare ``oom`` marker (it is in "boom"): the retry accepts it, the sentence to the caller does not."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in (*_OOM_MARKERS, *(() if plain else ("oom",))))


class RoundFailure(Exception):
    """A round that produced no files. ``kind`` is one of the constants above."""

    def __init__(self, kind: str, *, detail: str = "", excerpt: str = "", status: int | None = None, prompt_id: str | None = None,
                 queue_position: int | None = None, code: str | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.excerpt, self.status = kind, detail, excerpt, status
        self.prompt_id, self.queue_position, self.code = prompt_id, queue_position, code

    @property
    def out_of_memory(self) -> bool:
        return says_out_of_memory(self.excerpt)


@dataclass(frozen=True)
class RoundResult:
    """The files a round selected, by the contract output they belong to (empty when the run produced none)."""

    files: Mapping[str, tuple[bytes, ...]] = field(default_factory=dict)
    prompt_id: str = ""


async def queue_position(adapter: Any, session: Any, prompt_id: str, headers: Mapping[str, str], *, timeout: Any) -> Optional[int]:
    try:
        reply = await session.queue(headers=dict(headers), timeout=timeout)
    except Exception:  # noqa: BLE001 - a position is a courtesy
        return None
    if reply.status != 200:
        return None
    try:
        queue = reply.json()
    except Exception:  # noqa: BLE001
        return None
    return node_graph.queue_position(queue, prompt_id) if isinstance(queue, Mapping) else None


async def cancel(adapter: Any, connection: Callable[[], Any], session: Any, prompt_id: str, headers: Mapping[str, str], *,
                 reason: str, timeout: Any, say: Callable[[str], None]) -> bool:
    """Ask the engine to drop a prompt. Never raises: a cancel that fails is reported and the caller carries on."""
    try:
        await adapter.cancel(connection(), JobRef("graph.submit", prompt_id), headers=dict(headers), session=session, timeout=timeout)
    except UpstreamError as error:
        say(f"{adapter.label} cancel failed for {prompt_id} after {reason}: HTTP {error.status}")
        return False
    except Exception as error:  # noqa: BLE001
        say(f"{adapter.label} cancel failed for {prompt_id} after {reason}: {type(error).__name__}")
        return False
    say(f"{adapter.label} prompt cancelled after {reason}: {prompt_id}")
    return True


async def run_round(
    adapter: Any,
    session: Any,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    graph: Mapping[str, Any],
    label: str,
    max_wait_s: float,
    control_timeout: Any,
    cancel_connection: Callable[[], Any],
    is_disconnected: Callable[[], Awaitable[bool]],
    binding: Mapping[str, Any],
    contract_outputs: Mapping[str, Any],
    limits: Any,
    cadence: Poll,
    say: Callable[[str], None] = lambda _text: None,
) -> RoundResult:
    """Submit ``payload`` and wait for its files. Raises ``RoundFailure``; a cancelled task cancels the engine's prompt first."""
    prompt_id: Optional[str] = None
    try:
        try:
            ref = await adapter.submit(session.conn, "graph.submit",
                                       CanonicalRequest(op="graph.submit", body=dict(payload), headers=dict(headers), params={"session": session}))
        except TransportFailure as error:
            kind = {"timeout": SUBMIT_TIMEOUT, "unreachable": UNREACHABLE}.get(error.kind, DROPPED)
            raise RoundFailure(kind) from error
        except UpstreamError as error:
            raise RoundFailure(REFUSED, excerpt=str(error.excerpt or ""), status=error.status) from error
        except MalformedResponse as error:
            raise RoundFailure(NO_PROMPT_ID) from error
        prompt_id = ref.id
        say(f"{adapter.label} {label} submitted: {prompt_id}")

        remaining = float(max_wait_s)
        finished = None
        polls = 0
        while remaining > 0 and (cadence.max_polls is None or polls < cadence.max_polls):
            if await is_disconnected():
                await cancel(adapter, cancel_connection, session, prompt_id, headers, reason="client disconnect", timeout=control_timeout, say=say)
                raise RoundFailure(DISCONNECTED, prompt_id=prompt_id)
            delay = min(cadence.ramp_s[polls] if polls < len(cadence.ramp_s) else cadence.interval_s, remaining)
            polls += 1
            if delay:
                await asyncio.sleep(delay)
                remaining -= delay
            try:
                status = await adapter.poll(session.conn, ref, headers=dict(headers), session=session, graph=dict(graph))
            except (TransportFailure, UpstreamError):
                continue
            if status.state is JobState.failed:
                raise RoundFailure(EXECUTION_FAILED, detail=str(status.detail or ""), prompt_id=prompt_id)
            if status.state is JobState.succeeded:
                finished = status
                break
        if finished is None:
            position = await queue_position(adapter, session, prompt_id, headers, timeout=control_timeout)
            await cancel(adapter, cancel_connection, session, prompt_id, headers, reason="timeout", timeout=control_timeout, say=say)
            raise RoundFailure(WAIT_EXCEEDED, prompt_id=prompt_id, queue_position=position)
        try:
            picked = await adapter.fetch_selected(session.conn, finished, binding, contract_outputs, limits=limits,
                                                  headers=dict(headers), session=session)
        except SelectionRefused as refusal:
            if refusal.code != "result_missing":
                raise RoundFailure(RESULT_REFUSED, code=refusal.code, prompt_id=prompt_id) from refusal
            picked = {}
        return RoundResult(files={name: tuple(files) for name, files in picked.items()}, prompt_id=prompt_id)
    except asyncio.CancelledError:
        if prompt_id:
            await cancel(adapter, cancel_connection, session, prompt_id, headers, reason="request cancellation", timeout=control_timeout, say=say)
        raise
