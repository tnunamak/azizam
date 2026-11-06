"""Tool-call detect-and-advise + structural normalization.

Implements the SLVP design from docs/design/TOOLCALL_SLVP_DESIGN.md §5–6:

    1. DETECTION  — marker-presence test on content when tools were requested
                    and tool_calls is empty/absent.  NOT a parser: it never
                    extracts structure, never mutates the body.
    2. ADVISORY   — maps (backend-id, detected-family) → exact remediation
                    string, surfaced via response header + WARN log.
    3. NORMALIZATION — two structural repairs on already-lifted tool_calls:
                    a. toolcall-args-stringify  (dict→JSON string)
                    b. toolcall-finish-reason-repair (Ollama stop→tool_calls)

Detection gates strictly on ``request_had_tools`` (tools array present and
non-empty, tool_choice != "none") — so it is a no-op on ordinary responses and
cannot false-fire on creative-writing prose.  A false-positive in detection (some
prose that happens to contain ``<tool_call>``) produces only an advisory header
and log line — never a body mutation.  That cost is acceptable; the design §3.3
argues it honestly.

Public surface
--------------
- TOOLCALL_MARKERS        list[(marker_substr, family_name)]
- detect_unlifted_toolcalls(payload, request_had_tools) -> family | None
- advisory_for_backend(backend_hint, family) -> str
- ToolcallDetectorStreamWrapper(had_tools, backend_hint) — async generator wrapper
- normalize_toolcall_args(payload) -> bool          (args-stringify)
- normalize_toolcall_finish_reason(payload) -> bool (finish-reason repair)
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger("gateway.toolcall")

# ---------------------------------------------------------------------------
# §5.2 — Detection: marker table
# ---------------------------------------------------------------------------
# Each entry is (marker_substring, family_name).
# Family names match vLLM's --tool-call-parser argument values so they can be
# embedded verbatim in the remediation string.
#
# NOTE: this is a MEMBERSHIP TEST, not a parser.  It answers "does this look
# like an unlifted tool call?" — it does NOT extract structure.  §3.3.
#
# The table is intentionally small (~5 entries).  A new family = one new string.
# No JSON-shape knowledge, no key-name maps, no separator conventions.

TOOLCALL_MARKERS: list[tuple[str, str]] = [
    # marker substring            family name (for advisory)
    ("<tool_call>",               "hermes_or_qwen"),
    ("[TOOL_CALLS]",              "mistral"),
    ("<|python_tag|>",            "llama3"),
    ("```tool_code",              "gemma"),
    # Command-R: "Action:" is too generic alone; require the json fence too.
    # We match on "Action:" here — callers confirm "```json" is also present.
    ("Action:\n```json",          "command_r"),
    ("Action: ```json",           "command_r"),
]

# Maximum marker length — used to bound the streaming rolling tail.
_MAX_MARKER_LEN: int = max(len(m) for m, _ in TOOLCALL_MARKERS)


def _find_marker(content: str) -> str | None:
    """Return the family name of the first marker found in *content*, or None."""
    for marker, family in TOOLCALL_MARKERS:
        if marker in content:
            return family
    return None


# ---------------------------------------------------------------------------
# §5.3 — Advisory map: backend-hint → remediation
# ---------------------------------------------------------------------------
# ``backend_hint`` is the value callers derive from config["backends"][sid]:
#   prefer config["backends"][sid].get("preset") — the stored preset id
#   fall back to the service_id itself (many operators name services "vllm" etc.)
#
# Matching is case-insensitive substring so "my-vllm-gpu" still hits "vllm".

def advisory_for_backend(
    backend_hint: str, family: str, operator_advisory: str | None = None
) -> str:
    """Return a generic remediation string for a backend that emitted tool-call
    text without lifting it into ``tool_calls``.

    Engine-agnostic by design (ENGINE_KNOWLEDGE_BOUNDARY.md). The gateway does
    NOT carry per-engine remediation flags — those churn as engines change and
    are the maintenance treadmill the boundary forbids. The durable, data-driven
    value here is the *detected family* (from the marker table) plus the honest
    observation that the backend isn't structuring tool calls. If an operator
    wants an exact, backend-specific instruction, they author it as DATA: a
    ``tool_call_advisory`` string field on the service record, passed in as
    ``operator_advisory`` — never as code we maintain.

    ``family`` is the detected name from TOOLCALL_MARKERS (data). ``backend_hint``
    is included only as a label so the operator knows which backend it was.
    """
    fam = family or "unknown"
    base = (
        f"Detected {fam}-format tool calls in the response content, but tool_calls "
        f"is empty — this backend"
        + (f" ({backend_hint})" if backend_hint else "")
        + " is not lifting tool calls into the OpenAI tool_calls field. Enable "
        "tool-call parsing in the backend's own configuration (most OpenAI-"
        "compatible servers have a flag or template setting for this), and confirm "
        "the loaded model's chat template supports tools."
    )
    if operator_advisory:
        return f"{base} {operator_advisory.strip()}"
    return base


# ---------------------------------------------------------------------------
# §5.2 — Non-streaming detection
# ---------------------------------------------------------------------------

def detect_unlifted_toolcalls(
    payload: dict[str, Any],
    request_had_tools: bool,
) -> str | None:
    """Return the detected family name if unlifted tool calls are found.

    Conditions (all must hold):
    1. ``request_had_tools`` is True (tools array present, non-empty,
       tool_choice != "none").
    2. The response choice's message has empty/absent ``tool_calls``.
    3. ``message.content`` contains a known marker substring.

    Returns the family name (e.g. ``"hermes_or_qwen"``) or None.
    Never mutates ``payload``.
    """
    if not request_had_tools:
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        # Skip if tool_calls already present and non-empty.
        if message.get("tool_calls"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        family = _find_marker(content)
        if family is not None:
            return family
    return None


def request_had_tools(request_body: dict[str, Any]) -> bool:
    """Return True if this chat-completions request carried a non-empty tools array.

    Gates detection so ordinary responses never trigger the advisory.
    """
    tools = request_body.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    tool_choice = request_body.get("tool_choice")
    # tool_choice="none" means the model must not call tools; skip detection.
    if tool_choice == "none":
        return False
    return True


# ---------------------------------------------------------------------------
# §5.5 — Streaming detection wrapper
# ---------------------------------------------------------------------------

# A line longer than this is dropped from detection (best-effort advisory).
_MAX_LINE_CARRY_BYTES = 1 << 20


class ToolcallStreamDetector:
    """Stateful per-request streaming tool-call detector.

    Wraps an SSE async iterator.  Forwards every chunk byte-for-byte (never
    rewrites), maintains a bounded rolling tail of concatenated delta.content,
    and detects the unlifted-tool-call condition at stream close.

    Usage::

        detector = ToolcallStreamDetector(had_tools=True, backend_hint="vllm")
        async for chunk in detector.wrap(upstream_iterator):
            yield chunk
        advisory = detector.advisory()   # None or the remediation string
    """

    def __init__(self, had_tools: bool, backend_hint: str) -> None:
        self._had_tools = had_tools
        self._backend_hint = backend_hint
        self._saw_tool_calls = False
        self._tail = ""
        self._line_carry = b""
        self._detected_family: str | None = None

    async def wrap(self, upstream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Yield every upstream chunk unchanged; update detection state."""
        async for chunk in upstream:
            self._process_chunk(chunk)
            yield chunk
        # A final unterminated line is still a frame.
        if self._line_carry:
            rest, self._line_carry = self._line_carry, b""
            self._process_line(rest)

    def _process_chunk(self, chunk: bytes) -> None:
        if not self._had_tools:
            return
        if self._detected_family and self._saw_tool_calls:
            return  # already decided
        # A network read can end mid-line: only complete lines are parsed and
        # the unfinished tail waits for the next read.
        data = self._line_carry + chunk
        cut = data.rfind(b"\n") + 1
        self._line_carry = data[cut:] if len(data) - cut <= _MAX_LINE_CARRY_BYTES else b""
        for raw_line in data[:cut].splitlines():
            self._process_line(raw_line)
            if self._saw_tool_calls:
                return

    def _process_line(self, raw_line: bytes) -> None:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("tool_calls"):
                self._saw_tool_calls = True
                return
            content_piece = delta.get("content")
            if isinstance(content_piece, str) and content_piece:
                # Search the whole new text plus the carried tail BEFORE trimming,
                # so a marker at the start of a long delta is not cut away.
                window = self._tail + content_piece
                if self._detected_family is None:
                    self._detected_family = _find_marker(window)
                # Keep only enough tail for a marker split across deltas.
                self._tail = window[-(_MAX_MARKER_LEN - 1):]

    def advisory(self) -> str | None:
        """Return advisory string if unlifted tool calls were detected, else None."""
        if not self._had_tools:
            return None
        if self._saw_tool_calls:
            return None
        if self._detected_family is None:
            return None
        return advisory_for_backend(self._backend_hint, self._detected_family)

    @property
    def detected_family(self) -> str | None:
        return self._detected_family


# ---------------------------------------------------------------------------
# §6 — Structural normalization built-ins
# ---------------------------------------------------------------------------

def normalize_toolcall_args(payload: dict[str, Any]) -> bool:
    """toolcall-args-stringify: convert dict arguments to JSON string.

    §6.1: If tool_calls[].function.arguments is a dict (or any non-string),
    JSON-encode it to the spec-required string.  If it is already a string,
    no-op.  Returns True if any change was made.

    Safety: only serializes a structured value to its canonical string form.
    Cannot corrupt already-correct output (a string stays a string).
    """
    changed = False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        # Handle both message (non-streaming) and delta (streaming) shapes.
        for msg_key in ("message", "delta"):
            message = choice.get(msg_key)
            if not isinstance(message, dict):
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function")
                if not isinstance(func, dict):
                    continue
                args = func.get("arguments")
                if isinstance(args, str):
                    continue  # Already correct — no-op.
                if args is not None:
                    func["arguments"] = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
                    changed = True
    return changed


def normalize_toolcall_finish_reason(payload: dict[str, Any]) -> bool:
    """toolcall-finish-reason-repair: fix stop→tool_calls when tool_calls present (Ollama).

    §6.2: If finish_reason is "stop" (or absent/None) but the choice has
    non-empty tool_calls, set finish_reason = "tool_calls".  Only fires when
    tool_calls are present — never invents tool_calls, never touches a plain stop.
    Returns True if any change was made.
    """
    changed = False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        # Handle both message and delta shapes.
        has_tool_calls = False
        for msg_key in ("message", "delta"):
            message = choice.get(msg_key)
            if isinstance(message, dict) and message.get("tool_calls"):
                has_tool_calls = True
                break
        if not has_tool_calls:
            continue
        finish_reason = choice.get("finish_reason")
        if finish_reason in ("stop", None):
            choice["finish_reason"] = "tool_calls"
            changed = True
    return changed
