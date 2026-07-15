"""Exact input-token preflight for the current-loaded text chat execution path.

Reuses the target backend's own template renderer and tokenizer (via the two
generic, no-inference endpoints most OpenAI-compatible local servers expose:
``POST /apply-template`` and ``POST /tokenize``) against the exact final
prepared execution body, so the count matches what the backend will actually
receive byte-for-byte. No chat-template/tokenizer logic is duplicated here,
and no engine name appears in this module — a service only participates if
its ``host`` answers those two generic paths.

This is a preflight, not a second control plane: it never selects a target,
never activates/loads a model, and never mutates the request. It only
produces a count (or an explicit "unavailable" outcome) that the caller uses
to decide whether to reject a request before dispatching it.

Transport is injected (``post_json``) rather than owned here so the caller
can route these two calls through its existing cancellation-aware dispatch
path (see ``proxy._forward_current_loaded_openai_request``) instead of this
module opening a second, unmonitored HTTP client per request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping

PostJson = Callable[[str, Mapping[str, Any]], Awaitable[tuple[int, Any]]]
"""``(path, json_body) -> (status_code, parsed_json_or_None)`` against one host."""

# Bee's oaicompat_chat_params_parse (server-common.cpp) accepts EXACTLY these
# content-block shapes and rejects anything else with an error before
# execution: a scalar string, null, or an array whose blocks are each
# "text", "image_url", or "input_audio" (image_url/input_audio decode into a
# real files vector during execution; /apply-template renders those same
# blocks against a dummy files vector, so it is not proven exact for them).
_TEXT_ONLY_CONTENT_BLOCK_TYPE = "text"


def is_text_only_chat_body(body: Mapping[str, Any]) -> bool:
    """Positively identify a chat body eligible for the exact text preflight.

    This is a POSITIVE predicate — eligible only if every message's content
    is a plain string/null, or an array whose every block is a recognized
    ``"text"`` block. Any image, audio, unknown, or malformed content-block
    shape (including a content array that fails to parse as a list of dicts)
    returns False, so the caller can route it to the existing execution path
    unguarded rather than risk silently under-counting a shape this preflight
    was never proven exact for.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping):
            return False
        content = message.get("content")
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, Mapping):
                return False
            if block.get("type") != _TEXT_ONLY_CONTENT_BLOCK_TYPE:
                return False
    return True


class PreflightCancelled(Exception):
    """Raise from a ``PostJson`` implementation to signal caller cancellation.

    Propagated unchanged rather than folded into ``PreflightUnavailable`` —
    a client disconnect/cross-hop cancel is a distinct outcome the caller
    (which owns the cancellation-aware transport) must still observe and
    react to, not a count failure that should fail open.
    """


@dataclass(frozen=True)
class PreflightCount:
    """A successful exact input-token count for one prepared request body."""

    input_tokens: int


@dataclass(frozen=True)
class PreflightUnavailable:
    """The backend could not produce an exact count for this request.

    Distinct from an overflow finding: this means the preflight itself did
    not complete (network error, non-2xx, malformed response, timeout), not
    that the request was measured and found too large.
    """

    reason: str


async def exact_input_token_count(
    *,
    body: Mapping[str, Any],
    apply_template_path: str,
    tokenize_path: str,
    post_json: PostJson,
) -> PreflightCount | PreflightUnavailable:
    """Return the exact input-token count for ``body`` as the backend would see it.

    Calls the backend's own ``apply_template_path`` (no inference — renders
    the exact prompt the chat-completions path would build from this body,
    at the operator-declared path for this capability) and then
    ``tokenize_path`` on that rendered prompt with ``add_special=True,
    parse_special=True`` — the same flags the backend's own native
    input-count path uses. The rendered prompt is held only in a local
    variable and never returned, logged, or persisted by this function.
    """
    try:
        apply_status, apply_payload = await post_json(apply_template_path, body)
    except PreflightCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - any other transport failure is "unavailable"
        return PreflightUnavailable(f"apply-template request failed: {exc.__class__.__name__}")
    if apply_status != 200:
        return PreflightUnavailable(f"apply-template returned HTTP {apply_status}")
    prompt = apply_payload.get("prompt") if isinstance(apply_payload, Mapping) else None
    if not isinstance(prompt, str):
        return PreflightUnavailable("apply-template response missing string prompt")

    try:
        tokenize_status, tokenize_payload = await post_json(
            tokenize_path,
            {"content": prompt, "add_special": True, "parse_special": True},
        )
    except PreflightCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - any other transport failure is "unavailable"
        return PreflightUnavailable(f"tokenize request failed: {exc.__class__.__name__}")
    if tokenize_status != 200:
        return PreflightUnavailable(f"tokenize returned HTTP {tokenize_status}")
    tokens = tokenize_payload.get("tokens") if isinstance(tokenize_payload, Mapping) else None
    if not isinstance(tokens, list):
        return PreflightUnavailable("tokenize response missing tokens list")
    return PreflightCount(input_tokens=len(tokens))


_UNBOUNDED_OUTPUT_SENTINELS = (None, -1)


@dataclass(frozen=True)
class OutputAllowance:
    """The canonical output-token reservation this execution will actually use."""

    tokens: int
    unbounded: bool = False


def resolve_output_allowance(
    body: Mapping[str, Any],
    *,
    precedence: tuple[str, ...],
) -> OutputAllowance:
    """Resolve the ONE canonical output-token reservation this body will execute.

    ``precedence`` is REQUIRED, operator-authored service capability data
    (e.g. ``("n_predict", "max_completion_tokens", "max_tokens")`` for a
    llama.cpp server — see ``server-task.cpp``'s ``params.n_predict =
    json_value(data, "n_predict", json_value(data, "max_completion_tokens",
    max_tokens))``, where ``max_tokens = json_value(data, "max_tokens",
    defaults.n_predict)``). The first field in that order present in
    ``body`` wins — this mirrors the target's actual override semantics
    instead of guessing a default check order. Callers must validate
    ``precedence`` is non-empty before calling this (see
    ``proxy._exact_preflight_capability`` — a capability with no declared
    precedence is invalid and must not reach this function at all).

    A negative-non-sentinel or non-numeric value for the winning field is
    treated as absent (falls through to the next field). ``None``/``-1``
    mean "unbounded" (the documented llama.cpp sentinel for "no limit" —
    also llama.cpp's own default when NO precedence field is present in the
    body at all, so absent-all-fields resolves the same way): returned as
    ``unbounded=True`` so the caller can decide policy explicitly (this
    preflight's ``check_context_fit`` treats unbounded as reserving nothing
    extra beyond the input — a deliberate, documented choice, not an
    implicit default buried here).
    """
    for field in precedence:
        if field not in body:
            continue
        value = body[field]
        if value in _UNBOUNDED_OUTPUT_SENTINELS:
            return OutputAllowance(tokens=0, unbounded=True)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 0:
            continue
        return OutputAllowance(tokens=value)
    return OutputAllowance(tokens=0, unbounded=True)


@dataclass(frozen=True)
class ContextOverflow:
    """The exact input count plus requested output allowance exceeds the limit."""

    input_tokens: int
    output_allowance: int
    context_window: int


def check_context_fit(
    *,
    input_tokens: int,
    output_allowance: int,
    context_window: int,
) -> ContextOverflow | None:
    """Return the overflow finding, or ``None`` if the request fits.

    Two independent guarantees, both required:

    1. ``input_tokens >= context_window`` is ALWAYS overflow, regardless of
       output allowance — Bee rejects a prompt at or past ``slot.n_ctx``
       before generation (``server-context.cpp``), so an exact-boundary
       input alone must never be forwarded as "fits".
    2. ``input_tokens + output_allowance > context_window`` is overflow —
       the combined reservation must not exceed the window.
    """
    if input_tokens >= context_window:
        return ContextOverflow(
            input_tokens=input_tokens,
            output_allowance=output_allowance,
            context_window=context_window,
        )
    if input_tokens + output_allowance <= context_window:
        return None
    return ContextOverflow(
        input_tokens=input_tokens,
        output_allowance=output_allowance,
        context_window=context_window,
    )
