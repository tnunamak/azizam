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
ORPHANED = "orphaned"                # the engine reported the prompt in neither its history nor its queue, twice

# How long a prompt that the history does not show may stay out of the queue too before the engine is taken to have lost it.
# The first look at the queue comes after this much waiting, the second this much later: a prompt a fast engine finishes
# is never looked up, and one answer that does not name the prompt is not enough.
ORPHAN_GRACE_S = 15.0
# What the progress of a run shows for each thing the engine reports (the values the old status route showed): the prompt
# is running in the queue, and the history has an entry that is not finished.
PROGRESS_IN_QUEUE = 25
PROGRESS_IN_HISTORY = 50

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
    names: tuple[str, ...] = ()          # the engine's file name of each file kept, in order


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


async def prompt_in_queue(adapter: Any, session: Any, prompt_id: str, headers: Mapping[str, str], *, timeout: Any) -> Optional[str]:
    """``"running"``, ``"pending"`` or ``"absent"`` for a prompt in the engine's queue; None when the answer says nothing (no
    answer, an error status, a body that is not the queue's shape). Only a usable answer may count towards an orphan."""
    try:
        reply = await session.queue(headers=dict(headers), timeout=timeout)
        if reply.status != 200:
            return None
        queue = reply.json()
    except Exception:  # noqa: BLE001 - any failure to read the queue is "no answer"
        return None
    if not isinstance(queue, Mapping):
        return None
    running, pending = queue.get("queue_running"), queue.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        return None
    if prompt_id in node_graph.queue_prompt_ids(running):
        return "running"
    return "pending" if prompt_id in node_graph.queue_prompt_ids(pending) else "absent"


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
    on_progress: Callable[[int], None] = lambda _percent: None,
    resume_prompt_id: str | None = None,
) -> RoundResult:
    """Submit ``payload`` and wait for its files. Raises ``RoundFailure``; a cancelled task cancels the engine's prompt first.

    ``on_progress`` hears how far the engine has got (a value below 100). A prompt that the history does not show and that
    two looks at the queue, ``ORPHAN_GRACE_S`` of waiting apart, also do not show, ends the round as ``ORPHANED``. A look
    that fails or answers in a shape that is not the queue's counts for nothing and ends the streak.

    With ``resume_prompt_id`` nothing is submitted: the prompt a restarted run saved is waited for and harvested."""
    prompt_id: Optional[str] = None
    try:
        try:
            ref = JobRef("graph.submit", resume_prompt_id) if resume_prompt_id else await adapter.submit(session.conn, "graph.submit",
                                       CanonicalRequest(op="graph.submit", body=dict(payload), headers=dict(headers), params={"session": session}))
        except TransportFailure as error:
            kind = {"timeout": SUBMIT_TIMEOUT, "unreachable": UNREACHABLE}.get(error.kind, DROPPED)
            raise RoundFailure(kind) from error
        except UpstreamError as error:
            raise RoundFailure(REFUSED, excerpt=str(error.excerpt or ""), status=error.status) from error
        except MalformedResponse as error:
            raise RoundFailure(NO_PROMPT_ID) from error
        prompt_id = ref.id
        say(f"{adapter.label} {label} {'resumed' if resume_prompt_id else 'submitted'}: {prompt_id}")

        remaining = float(max_wait_s)
        finished = None
        polls = 0
        next_look_s = ORPHAN_GRACE_S
        unseen = 0
        cancel_refused = False
        while remaining > 0 and (cadence.max_polls is None or polls < cadence.max_polls):
            if not cancel_refused and await is_disconnected():
                # The round ends as canceled only once the engine has dropped the prompt. A cancel the engine did not accept
                # leaves the prompt running there: the round asks once, then follows the prompt as it would without the cancel.
                if await cancel(adapter, cancel_connection, session, prompt_id, headers, reason="client disconnect", timeout=control_timeout, say=say):
                    raise RoundFailure(DISCONNECTED, prompt_id=prompt_id)
                cancel_refused = True
            delay = min(cadence.ramp_s[polls] if polls < len(cadence.ramp_s) else cadence.interval_s, remaining)
            polls += 1
            if delay:
                await asyncio.sleep(delay)
                remaining -= delay
            try:
                status = await adapter.poll(session.conn, ref, headers=dict(headers), session=session, graph=dict(graph))
            except (TransportFailure, UpstreamError, ValueError, TypeError):    # no answer, or an answer that is not a history
                unseen = 0
                continue
            if status.state is JobState.failed:
                raise RoundFailure(EXECUTION_FAILED, detail=str(status.detail or ""), prompt_id=prompt_id)
            if status.state is JobState.succeeded:
                finished = status
                break
            if status.state is JobState.running:
                unseen = 0
                on_progress(PROGRESS_IN_HISTORY)
            elif float(max_wait_s) - remaining >= next_look_s:
                next_look_s = float(max_wait_s) - remaining + ORPHAN_GRACE_S
                where = await prompt_in_queue(adapter, session, prompt_id, headers, timeout=control_timeout)
                unseen = unseen + 1 if where == "absent" else 0
                if where == "running":
                    on_progress(PROGRESS_IN_QUEUE)
                if unseen >= 2:
                    raise RoundFailure(ORPHANED, prompt_id=prompt_id)
        if finished is None:
            position = await queue_position(adapter, session, prompt_id, headers, timeout=control_timeout)
            await cancel(adapter, cancel_connection, session, prompt_id, headers, reason="timeout", timeout=control_timeout, say=say)
            raise RoundFailure(WAIT_EXCEEDED, prompt_id=prompt_id, queue_position=position)
        names: list[str] = []
        try:
            picked = await adapter.fetch_selected(session.conn, finished, binding, contract_outputs, limits=limits,
                                                  headers=dict(headers), session=session, names=names)
        except SelectionRefused as refusal:
            if refusal.code != "result_missing":
                raise RoundFailure(RESULT_REFUSED, code=refusal.code, prompt_id=prompt_id) from refusal
            picked = {}
        return RoundResult(files={name: tuple(files) for name, files in picked.items()}, prompt_id=prompt_id, names=tuple(names))
    except asyncio.CancelledError:
        if prompt_id:
            await cancel(adapter, cancel_connection, session, prompt_id, headers, reason="request cancellation", timeout=control_timeout, say=say)
        raise
