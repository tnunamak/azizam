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
    post_json: PostJson,
) -> PreflightCount | PreflightUnavailable:
    """Return the exact input-token count for ``body`` as the backend would see it.

    Calls the backend's own ``POST /apply-template`` (no inference — renders
    the exact prompt the chat-completions path would build from this body)
    and then ``POST /tokenize`` on that rendered prompt with
    ``add_special=True, parse_special=True`` — the same flags the backend's
    own native input-count path uses. The rendered prompt is held only in a
    local variable and never returned, logged, or persisted by this
    function.
    """
    try:
        apply_status, apply_payload = await post_json("/apply-template", body)
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
            "/tokenize",
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

    ``output_allowance`` is the canonical requested-output reservation
    (``effective_output_limit`` — ``0`` if the request declared no cap, since
    a cap-less request reserves nothing extra beyond the input itself for
    this comparison; the backend's own generation-time limit still governs
    actual output length).
    """
    if input_tokens + output_allowance <= context_window:
        return None
    return ContextOverflow(
        input_tokens=input_tokens,
        output_allowance=output_allowance,
        context_window=context_window,
    )
