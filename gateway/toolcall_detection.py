"""Tool-call detect-and-advise + structural normalization.

Implements the SLVP design from docs/design/TOOLCALL_SLVP_DESIGN.md §5–6:

    1. DETECTION  — marker-presence test on content when tools were requested
                    and tool_calls is empty/absent.  NOT a parser: it never
                    extracts structure, never mutates the body.
    2. ADVISORY   — maps (backend-id, detected-family) → exact remediation
                    string, surfaced via response header + WARN log.
    3. NORMALIZATION — two structural repairs on already-lifted tool_calls:
                    a. toolcall-args-stringify  (TabbyAPI dict→JSON string)
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

def advisory_for_backend(backend_hint: str, family: str) -> str:
    """Return the exact remediation string for (backend, family).

    ``backend_hint`` should be the preset or service-id string.  ``family`` is
    the name from TOOLCALL_MARKERS (e.g. ``"hermes_or_qwen"``, ``"mistral"``).
    """
    hint = (backend_hint or "").lower()
    fam = family or "unknown"

    # Family label used in parser flags (vLLM / SGLang accept these names).
    parser_fam = _parser_family(fam)

    if "vllm" in hint:
        return (
            f"Detected {fam}-format tool calls in content but tool_calls is empty. "
            f"This vLLM backend is missing tool-call parsing. "
            f"Restart vLLM with: --enable-auto-tool-choice --tool-call-parser {parser_fam}. "
            f"Docs: https://docs.vllm.ai/en/latest/features/tool_calling.html"
        )
    if "sglang" in hint:
        return (
            f"Detected {fam}-format tool calls in content but tool_calls is empty. "
            f"Restart SGLang with: --tool-call-parser {parser_fam}. "
            f"Docs: https://docs.sglang.ai/backend/openai_api_completions.html"
        )
    if "llama-cpp-python" in hint or "llama_cpp_python" in hint:
        return (
            f"Detected {fam}-format tool calls in content but tool_calls is empty. "
            f"Restart llama-cpp-python server with: --chat_format chatml-function-calling "
            f"(or a functionary format). "
            f"Docs: https://github.com/abetlen/llama-cpp-python"
        )
    if "llamacpp" in hint or "llama.cpp" in hint or "llama-server" in hint:
        return (
            f"Detected {fam}-format tool calls in content but tool_calls is empty. "
            f"Restart llama-server with: --jinja. "
            f"Docs: https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md"
        )
    # Backends that should already lift natively.
    for known_lifter in ("ollama", "lmstudio", "lm-studio", "tgi", "tabbyapi", "tabby", "koboldcpp", "kobold"):
        if known_lifter in hint:
            return (
                f"Detected {fam}-format tool calls in content but tool_calls is empty. "
                f"This backend ({backend_hint}) normally lifts tool calls automatically. "
                f"Check your backend version and that the model's chat template supports tools."
            )
    # Generic / unknown.
    return (
        f"Detected {fam}-format tool calls in content but tool_calls is empty. "
        f"Your backend is not lifting tool calls to the tool_calls field. "
        f"If it is vLLM or SGLang, add: --tool-call-parser {parser_fam} --enable-auto-tool-choice. "
        f"If it is llama.cpp / llama-server, add: --jinja."
    )


def _parser_family(family: str) -> str:
    """Map our family names to the flag values vLLM/SGLang accept."""
    _MAP = {
        "hermes_or_qwen": "hermes",   # advise hermes; operator checks model
        "mistral": "mistral",
        "llama3": "llama3",
        "gemma": "gemma",
        "command_r": "command-r",
    }
    return _MAP.get(family, family)


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
        self._detected_family: str | None = None

    async def wrap(self, upstream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Yield every upstream chunk unchanged; update detection state."""
        async for chunk in upstream:
            self._process_chunk(chunk)
            yield chunk

    def _process_chunk(self, chunk: bytes) -> None:
        if not self._had_tools:
            return
        if self._detected_family and self._saw_tool_calls:
            return  # already decided
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            choices = payload.get("choices")
            if not isinstance(choices, list):
                continue
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
                    self._tail += content_piece
                    # Keep tail bounded to max marker length.
                    if len(self._tail) > _MAX_MARKER_LEN * 2:
                        self._tail = self._tail[-(_MAX_MARKER_LEN * 2):]
                    if self._detected_family is None:
                        self._detected_family = _find_marker(self._tail)

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
    """toolcall-args-stringify: convert dict arguments to JSON string (TabbyAPI).

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
