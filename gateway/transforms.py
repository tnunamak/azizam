"""Transform engine: declarative request/response body transforms.

This module implements the six primitive transform types from
docs/design/ENGINE_TRANSFORMS_DESIGN.md §2, a named-transform registry,
and a pipeline runner.

STEP 0: Engine + registry, DARK — nothing in proxy.py is wired to this yet
        (except the voxtral-voices built-in registered at module level, used
        in Step 1).

Public surface
--------------
- Transform (Protocol)        — on_request / on_response
- TransformContext             — opaque bag passed through the pipeline
- TransformRegistry            — load / resolve named transforms
- REGISTRY                     — module-level singleton
- run_request_pipeline(ids, body, ctx, registry) -> body
- run_response_pipeline(ids, payload, ctx, registry) -> payload
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class TransformContext:
    """Opaque context passed through every transform call.

    Currently a thin dict wrapper; future steps may attach backend metadata.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __repr__(self) -> str:  # pragma: no cover
        return f"TransformContext({self._data!r})"


# ---------------------------------------------------------------------------
# Transform protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Transform(Protocol):
    """A stateless, phase-pure transform step.

    Implementations must be safe to call from multiple coroutines concurrently
    (no mutable state beyond construction).
    """

    def on_request(self, body: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        """Apply this transform to a request body.  Return the (mutated) body."""
        ...

    def on_response(
        self, payload: dict[str, Any], ctx: TransformContext
    ) -> dict[str, Any]:
        """Apply this transform to a response payload.  Return the (mutated) payload."""
        ...


# ---------------------------------------------------------------------------
# when-predicate evaluation
# ---------------------------------------------------------------------------

_MISSING = object()  # sentinel for absent path


def _get_path(obj: Any, path: str) -> Any:
    """Resolve a dotted path (no wildcard expansion at this level).

    Supports simple dot notation: "response_format.type", "extra_body.foo".
    Wildcards like "choices[*].delta" are NOT expanded here — predicates on
    wildcard paths are not required by the Step 0/1 surface.
    """
    parts = path.replace("[*]", "").split(".")
    current = obj
    for part in parts:
        if part == "":
            continue
        if not isinstance(current, dict):
            return _MISSING
        current = current.get(part, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


def _eval_when(when: list[dict[str, Any]] | None, body: dict[str, Any]) -> bool:
    """Return True if ALL clauses in `when` hold (or `when` is None/empty)."""
    if not when:
        return True
    for clause in when:
        field = clause["field"]
        op = clause["op"]
        value = clause.get("value")
        actual = _get_path(body, field)

        if op == "present":
            if actual is _MISSING:
                return False
        elif op == "absent":
            if actual is not _MISSING:
                return False
        elif op == "equals":
            if actual is _MISSING or actual != value:
                return False
        elif op == "in":
            if actual is _MISSING or actual not in (value or []):
                return False
        elif op == "non_empty":
            if actual is _MISSING or not actual:
                return False
        elif op == "gt":
            if actual is _MISSING or not (isinstance(actual, (int, float)) and actual > value):
                return False
        elif op == "lt":
            if actual is _MISSING or not (isinstance(actual, (int, float)) and actual < value):
                return False
        else:
            raise ValueError(f"Unknown when-predicate op: {op!r}")
    return True


# ---------------------------------------------------------------------------
# Nested-path helpers for set/get/delete on dicts
# ---------------------------------------------------------------------------


def _set_nested(obj: dict[str, Any], dotted_path: str, value: Any) -> None:
    """Set a value at a dotted path, creating intermediate dicts as needed."""
    parts = dotted_path.split(".")
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _get_nested(obj: dict[str, Any], dotted_path: str) -> Any:
    """Get a value at a dotted path; return _MISSING if any segment is absent."""
    return _get_path(obj, dotted_path)


def _del_nested(obj: dict[str, Any], dotted_path: str) -> None:
    """Delete a key at a dotted path.  No-op if the path does not exist."""
    parts = dotted_path.split(".")
    current = obj
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


# ---------------------------------------------------------------------------
# Primitive base
# ---------------------------------------------------------------------------


class _Primitive:
    """Shared base for all declarative primitives."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._phase: str = cfg.get("phase", "request")
        self._when: list[dict[str, Any]] | None = cfg.get("when")

    # Subclasses implement _apply(body) -> body (pure mutation)
    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def on_request(self, body: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        if self._phase != "request":
            return body
        if not _eval_when(self._when, body):
            return body
        return self._apply(body)

    def on_response(self, payload: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        if self._phase != "response":
            return payload
        if not _eval_when(self._when, payload):
            return payload
        return self._apply(payload)


# ---------------------------------------------------------------------------
# 2.1  value-map
# ---------------------------------------------------------------------------


class ValueMapTransform(_Primitive):
    """Discrete value lookup on one param.

    YAML::

        voxtral-voices:
          kind: value-map
          param: voice
          map: { alloy: neutral_female, echo: casual_male }
          default: neutral_female
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._param: str = cfg["param"]
        self._map: dict[str, Any] = cfg.get("map") or {}
        self._default: Any = cfg.get("default")

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        current = body.get(self._param)
        if current is None and self._default is not None:
            body[self._param] = self._default
        elif current is not None:
            body[self._param] = self._map.get(str(current), self._default if self._default is not None else current)
        return body


# ---------------------------------------------------------------------------
# 2.2  drop-params
# ---------------------------------------------------------------------------


class DropParamsTransform(_Primitive):
    """Remove listed top-level params from the body.

    YAML::

        ollama-unsupported:
          kind: drop-params
          drop: [n, logprobs, top_logprobs]
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._drop: list[str] = cfg.get("drop") or []

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        for key in self._drop:
            body.pop(key, None)
        return body


# ---------------------------------------------------------------------------
# 2.3  default-params
# ---------------------------------------------------------------------------


class DefaultParamsTransform(_Primitive):
    """Set defaults on body fields using setdefault semantics (nested-path capable).

    YAML::

        sglang-separate-reasoning:
          kind: default-params
          set: { extra_body.separate_reasoning: true }
          only_if_absent: true      # always true — setdefault never overwrites
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._set: dict[str, Any] = cfg.get("set") or {}
        # only_if_absent is the documented semantic; we always honour it (setdefault).

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        for dotted_key, default_value in self._set.items():
            existing = _get_nested(body, dotted_key)
            if existing is _MISSING:
                _set_nested(body, dotted_key, default_value)
        return body


# ---------------------------------------------------------------------------
# 2.4  param-map  (rename / move a param across a possibly nested path)
# ---------------------------------------------------------------------------


class ParamMapTransform(_Primitive):
    """Rename or move a param from one path to another.

    Handles simple top-level keys and dotted nested paths.
    Wildcard path expansion (choices[*].delta.reasoning) is NOT implemented
    in this step; it is only needed for response-phase reasoning-field renaming
    (Step 4).

    YAML::

        vllm-guided-json:
          kind: param-map
          from: response_format.json_schema.schema
          to:   guided_json
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._from: str = cfg["from"]
        self._to: str = cfg["to"]

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        value = _get_nested(body, self._from)
        if value is _MISSING:
            return body
        _del_nested(body, self._from)
        _set_nested(body, self._to, value)
        return body


# ---------------------------------------------------------------------------
# 2.5  message-inject
# ---------------------------------------------------------------------------


class MessageInjectTransform(_Primitive):
    """Prepend/append/insert a message into messages[].

    Supported modes:
    - ``prepend``          — insert {"role": role, "text": text} at index 0
    - ``append``           — append to end of messages
    - ``append-or-insert`` — if a message with ``role`` already exists, append
                             ``text`` to its content; otherwise insert at index 0.
                             This reproduces the behaviour of
                             apply_chat_completions_reasoning_effort at proxy.py:5283.

    YAML::

        qwen-no-think-soft:
          kind: message-inject
          role: system
          text: "/no_think"
          mode: append-or-insert
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._role: str = cfg.get("role", "system")
        self._text: str = cfg.get("text", "")
        self._mode: str = cfg.get("mode", "prepend")

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = body.get("messages")
        if not isinstance(messages, list):
            return body

        if self._mode == "prepend":
            messages.insert(0, {"role": self._role, "content": self._text})

        elif self._mode == "append":
            messages.append({"role": self._role, "content": self._text})

        elif self._mode == "append-or-insert":
            # Find the LAST message with matching role and append text to it.
            # If none found, insert a new message at index 0.
            for msg in reversed(messages):
                if msg.get("role") == self._role:
                    existing_content = msg.get("content", "")
                    if existing_content:
                        msg["content"] = existing_content + "\n" + self._text
                    else:
                        msg["content"] = self._text
                    break
            else:
                messages.insert(0, {"role": self._role, "content": self._text})

        else:
            raise ValueError(f"Unknown message-inject mode: {self._mode!r}")

        body["messages"] = messages
        return body


# ---------------------------------------------------------------------------
# 2.6  clamp
# ---------------------------------------------------------------------------


class ClampTransform(_Primitive):
    """Coerce a numeric param into [min, max].

    YAML::

        tabby-penalty-clamp:
          kind: clamp
          param: frequency_penalty
          min: 0.0
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._param: str = cfg["param"]
        self._min: float | None = cfg.get("min")
        self._max: float | None = cfg.get("max")

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        value = body.get(self._param)
        if not isinstance(value, (int, float)):
            return body
        if self._min is not None and value < self._min:
            body[self._param] = self._min
        if self._max is not None and value > self._max:
            body[self._param] = self._max
        return body


# ---------------------------------------------------------------------------
# Primitive factory
# ---------------------------------------------------------------------------

_PRIMITIVE_KINDS: dict[str, type[_Primitive]] = {
    "value-map": ValueMapTransform,
    "drop-params": DropParamsTransform,
    "default-params": DefaultParamsTransform,
    "param-map": ParamMapTransform,
    "message-inject": MessageInjectTransform,
    "clamp": ClampTransform,
}


def _build_primitive(cfg: dict[str, Any]) -> _Primitive:
    kind = cfg.get("kind")
    cls = _PRIMITIVE_KINDS.get(kind)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"Unknown transform kind: {kind!r}")
    return cls(cfg)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TransformRegistry:
    """Named transform registry.

    Transforms are registered either as a single primitive config dict, or as a
    list of primitive config dicts (a pipeline).  The registry resolves an id
    to the list of primitives that make up that named transform.
    """

    def __init__(self) -> None:
        self._transforms: dict[str, list[_Primitive]] = {}

    def register(self, name: str, cfg: dict[str, Any] | list[dict[str, Any]]) -> None:
        """Register a named transform from a config dict or list of dicts."""
        if isinstance(cfg, list):
            self._transforms[name] = [_build_primitive(c) for c in cfg]
        else:
            self._transforms[name] = [_build_primitive(cfg)]

    def resolve(self, name: str) -> list[_Primitive]:
        """Return the list of primitives for a named transform.

        Raises KeyError if the name is not registered.
        """
        return self._transforms[name]

    def register_builtin(self, name: str, instance: Any) -> None:
        """Register a named built-in instance (must implement on_request/on_response)."""
        self._transforms[name] = [instance]

    def names(self) -> list[str]:
        return list(self._transforms.keys())


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def run_request_pipeline(
    transform_ids: list[str],
    body: dict[str, Any],
    ctx: TransformContext | None = None,
    registry: TransformRegistry | None = None,
) -> dict[str, Any]:
    """Apply the request phase of each named transform in order.

    Returns the (possibly mutated) body dict.  The input dict is modified
    in-place; callers that need the original must deepcopy before calling.
    """
    if ctx is None:
        ctx = TransformContext()
    if registry is None:
        registry = REGISTRY
    for tid in transform_ids:
        primitives = registry.resolve(tid)
        for primitive in primitives:
            body = primitive.on_request(body, ctx)
    return body


def run_response_pipeline(
    transform_ids: list[str],
    payload: dict[str, Any],
    ctx: TransformContext | None = None,
    registry: TransformRegistry | None = None,
) -> dict[str, Any]:
    """Apply the response phase of each named transform in order."""
    if ctx is None:
        ctx = TransformContext()
    if registry is None:
        registry = REGISTRY
    for tid in transform_ids:
        primitives = registry.resolve(tid)
        for primitive in primitives:
            payload = primitive.on_response(payload, ctx)
    return payload


# ---------------------------------------------------------------------------
# Named built-ins  (reviewed composed classes, not arbitrary-code plugins)
# ---------------------------------------------------------------------------

# Shared constants for the /no_think suppression surface — single source of truth.
# Background-removal helper (proxy.py) imports QWEN3_NO_THINK_HINT so the literal
# lives here alongside the transform, not scattered across call sites.
QWEN3_NO_THINK_HINT: str = "/no_think"
QWEN3_ENABLE_THINKING_KEY: str = "enable_thinking"


def apply_thinking_suppression(body: dict[str, Any]) -> None:
    """Apply Qwen3-style thinking suppression to *body* in-place.

    Single source of truth for the mutation shared by:
      - Qwen3ThinkingTransform (reasoning_effort=none/minimal path)
      - Background-removal helper (direct suppression without reasoning_effort)

    Sets chat_template_kwargs.enable_thinking = False (via setdefault) and
    injects QWEN3_NO_THINK_HINT into the FIRST system message (appending with
    "\\n" separator if the message already has non-empty content) or inserts a
    new system message at index 0 if none exists.

    Does NOT inject if the hint is already present in the first system message.
    """
    hint = QWEN3_NO_THINK_HINT
    # Belt-and-suspenders: chat template kwarg for llama-server Qwen3 family.
    kwargs = body.setdefault("chat_template_kwargs", {})
    if isinstance(kwargs, dict):
        kwargs.setdefault(QWEN3_ENABLE_THINKING_KEY, False)

    messages = body.get("messages")
    if isinstance(messages, list):
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "system":
                existing = m.get("content") or ""
                if isinstance(existing, str) and hint not in existing:
                    messages[i] = {
                        **m,
                        "content": (existing + ("\n" if existing else "") + hint),
                    }
                return
        messages.insert(0, {"role": "system", "content": hint})


class Qwen3ThinkingTransform:
    """Named built-in: reproduce apply_chat_completions_reasoning_effort() exactly.

    Registered as "qwen3-thinking" in REGISTRY.

    Handles request phase only (validated pop of reasoning_effort + body mutation).
    Returns a string error message if reasoning_effort is invalid (the caller
    must turn this into a 400 Response — the 400 plumbing stays in proxy.py).

    Contract reproduced byte-for-byte from proxy.py:5242:
      1. reasoning_effort absent → no-op.
      2. Pop reasoning_effort. If None → no-op (treat as absent after pop).
      3. If not str or not in _REASONING_EFFORT_VALUES → return error string.
      4. If not in {none, minimal} → strip only (already popped), no suppression.
      5. Suppress path (none/minimal):
         a. body.setdefault("chat_template_kwargs", {}) then
            kwargs.setdefault("enable_thinking", False)
         b. Messages inject: find the FIRST system message; if found, append
            "/no_think" (with "\\n" prefix if content non-empty) IFF it is not
            already in that message's content, then return None.  If no system
            message found, insert {"role":"system","content":"/no_think"} at 0.
    """

    # The valid set — matches _REASONING_EFFORT_VALUES in proxy.py:5235
    _VALID: frozenset[str] = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
    # Efforts that suppress thinking
    _SUPPRESS: frozenset[str] = frozenset({"none", "minimal"})

    def apply(self, body: dict[str, Any]) -> str | None:
        """Mutate body in-place.  Return an error string or None."""
        if "reasoning_effort" not in body:
            return None
        effort = body.pop("reasoning_effort")
        if effort is None:
            return None
        if not isinstance(effort, str) or effort not in self._VALID:
            return (
                f"Invalid value for 'reasoning_effort': {effort!r}. "
                f"Expected one of: {sorted(self._VALID)}."
            )
        if effort not in self._SUPPRESS:
            return None

        # Suppress thinking — delegate to shared helper.
        apply_thinking_suppression(body)
        return None

    # Transform protocol shim — lets this live in the registry alongside
    # primitives even though it is not a _Primitive subclass.
    def on_request(self, body: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        # Error return is handled by the dispatch site; here we just mutate.
        self.apply(body)
        return body

    def on_response(self, payload: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        return payload


# ---------------------------------------------------------------------------
# Step 4a — reasoning-field-normalize built-in
# ---------------------------------------------------------------------------


def split_reasoning_visible_text(text: Any) -> tuple[Optional[str], Optional[str]]:
    """Split ``<think>...</think>`` tags out of *text*.

    Ported verbatim from proxy.py:18869 (_split_reasoning_visible_text).
    Exposed here so the SSE-chunk normalizer in proxy.py can import it and
    stay in sync with the transform's logic.

    Returns (reasoning, visible):
      - If no ``</think>`` found: (None, text)  — text is the full visible content.
      - If ``</think>`` found: (reasoning_or_None, visible_or_None).
    """
    if not isinstance(text, str) or not text:
        return None, None
    lower = text.lower()
    close = lower.rfind("</think>")
    if close >= 0:
        reasoning = text[:close].strip()
        open_tag = reasoning.lower().rfind("<think>")
        if open_tag >= 0:
            reasoning = reasoning[open_tag + len("<think>"):].strip()
        visible = text[close + len("</think>"):].strip()
        return reasoning or None, visible or None
    return None, text


def normalize_chat_completion_payload(payload: dict[str, Any]) -> bool:
    """Normalize visible content in a chat-completion response payload.

    Ported verbatim from proxy.py:18884 (_normalize_chat_completion_visible_content).
    Mutates *payload* in-place; returns True iff any field was changed.

    Two cases handled for both ``message`` and ``delta`` in each choice:
      1. ``</think>`` leaked into content → strip tags, promote reasoning.
      2. ``reasoning_content`` set but ``content`` empty/absent → backfill.

    This is a detect-and-normalize (Revision D) — it inspects the payload and
    acts when the canonical shape is absent, regardless of any capability flag.
    """
    changed = False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            if isinstance(content, str) and "</think>" in content.lower():
                # Case 1: think tags leaked into content — strip them out.
                split_reasoning, visible = split_reasoning_visible_text(content)
                if visible is not None:
                    message["content"] = visible
                    if not isinstance(reasoning, str) and split_reasoning:
                        message["reasoning_content"] = split_reasoning
                    changed = True
            elif isinstance(reasoning, str) and reasoning and not content:
                # Case 2: reasoning_content set but content empty/absent.
                # Backfill content: if reasoning_content itself has think tags,
                # extract only the visible portion; otherwise mirror as-is.
                split_r, visible = split_reasoning_visible_text(reasoning)
                if visible is not None:
                    # reasoning_content had think tags — split it
                    message["content"] = visible
                    message["reasoning_content"] = split_r or reasoning
                else:
                    # No think tags — mirror reasoning_content into content
                    message["content"] = reasoning
                changed = True
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            reasoning = delta.get("reasoning_content")
            if isinstance(content, str) and "</think>" in content.lower():
                # Case 1: think tags leaked into delta content — strip them.
                _, visible = split_reasoning_visible_text(content)
                if visible is not None:
                    delta["content"] = visible
                    changed = True
            elif isinstance(reasoning, str) and reasoning and not content:
                # Case 2: delta has reasoning_content but no content — mirror it.
                delta["content"] = reasoning
                changed = True
    return changed


class ReasoningFieldNormalizeTransform:
    """Step 4a built-in: response-phase reasoning-field normalization.

    Registered as "reasoning-field-normalize" in REGISTRY.

    Detect-and-normalize (Revision D): inspects the payload and acts when the
    canonical shape is absent, regardless of any capability flag.  Delegates to
    normalize_chat_completion_payload() which is byte-identical to the legacy
    _normalize_chat_completion_visible_content() at proxy.py:18884.
    """

    def on_request(self, body: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        return body

    def on_response(self, payload: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        normalize_chat_completion_payload(payload)
        return payload


# ---------------------------------------------------------------------------
# Step 4b — streaming-think-split built-in (opt-in, new capability)
# ---------------------------------------------------------------------------

# The maximum delimiter length we need to buffer across chunks.
_THINK_CLOSE_TAG = "</think>"
_THINK_CLOSE_TAG_LEN = len(_THINK_CLOSE_TAG)


class StreamingThinkSplitTransform:
    """Step 4b built-in: catch ``</think>`` split across SSE chunks.

    Ships OPT-IN — attached only when explicitly configured.  When NOT attached,
    the existing per-chunk behavior (_normalize_chat_completion_sse_chunk) is
    byte-identical (no regression).

    Maintains a bounded cross-chunk buffer (at most len("</think>") - 1 bytes)
    to catch the case where the close-delimiter is split across two consecutive
    SSE chunks.  The buffer is flushed immediately on finding the delimiter.

    This is a stateful built-in; it holds mutable state (the carry buffer) and
    is therefore NOT safe to share across concurrent requests — each request must
    use its own instance.  The registry stores the class; callers instantiate.

    NOTE: This transform operates on raw SSE bytes, not on the dict payload, so
    it does not fit the standard on_request/on_response protocol.  It exposes a
    separate ``process_chunk(chunk: bytes) -> bytes`` API.  The on_response shim
    is a no-op (included for registry protocol compliance).
    """

    def __init__(self) -> None:
        self._carry: bytes = b""

    def process_chunk(self, chunk: bytes) -> bytes:
        """Process one raw SSE chunk, applying cross-chunk delimiter repair."""
        data = self._carry + chunk
        self._carry = b""

        # Fast path: if no hint of think tags, return immediately.
        lower = data.lower()
        if b"</think>" not in lower and b"</" not in lower:
            return chunk  # nothing to buffer — emit original

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return chunk  # can't decode — pass through unchanged

        # Check if data ends with a prefix of </think> (partial delimiter).
        # Buffer the suffix so next chunk can complete it.
        close_tag_lower = _THINK_CLOSE_TAG  # "</think>"
        # Search for the last occurrence of a partial suffix of </think> at the end.
        end_carry = b""
        for length in range(1, _THINK_CLOSE_TAG_LEN):
            suffix = _THINK_CLOSE_TAG[:length].encode("utf-8")
            if data.endswith(suffix) and b"</think>" not in data[:-length + 1 or None].lower():
                end_carry = suffix
                data = data[: len(data) - length]
                break
        self._carry = end_carry

        # Now process data for full </think> delimiters using the existing SSE logic.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._carry = b""  # reset carry on decode error
            return chunk

        if "data:" not in text or ("reasoning_content" not in text and "</think>" not in text.lower()):
            return data if end_carry else chunk

        out_lines: list[str] = []
        changed = False
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                out_lines.append(line)
                continue
            prefix, rest = line.split("data:", 1)
            newline = ""
            if rest.endswith("\r\n"):
                rest = rest[:-2]
                newline = "\r\n"
            elif rest.endswith("\n"):
                rest = rest[:-1]
                newline = "\n"
            rest = rest.strip()
            if not rest or rest == "[DONE]":
                out_lines.append(line)
                continue
            try:
                payload = json.loads(rest)
            except json.JSONDecodeError:
                out_lines.append(line)
                continue
            if isinstance(payload, dict) and normalize_chat_completion_payload(payload):
                out_lines.append(f"{prefix}data: {json.dumps(payload, separators=(',', ':'))}{newline}")
                changed = True
            else:
                out_lines.append(line)

        if changed or end_carry:
            return "".join(out_lines).encode("utf-8")
        return chunk

    # Protocol shims for registry compliance
    def on_request(self, body: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        return body

    def on_response(self, payload: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        return payload


# ---------------------------------------------------------------------------
# Step 5 — KoboldCpp null-param-strip (named built-in, value-test not key-list)
# ---------------------------------------------------------------------------


class KoboldCppNullStripTransform(_Primitive):
    """Remove top-level keys whose value is None/null (KoboldCpp 422s on null).

    Registered as "koboldcpp-null-strip" in REGISTRY.

    A value-based filter that cannot be expressed with drop-params (which is
    key-list only).  Iterates all top-level keys and removes those with a None
    value, which is the exact behavior KoboldCpp needs to avoid 422 errors.

    The ``when:`` predicate and phase routing are inherited from _Primitive.

    YAML config (conceptual, for documentation):
        koboldcpp-null-strip:
          kind: (built-in)
    """

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        null_keys = [k for k, v in body.items() if v is None]
        for k in null_keys:
            del body[k]
        return body


# ---------------------------------------------------------------------------
# Step 6 — tool-call family EXTRACTORS (response-phase built-ins, OPT-IN)
# ---------------------------------------------------------------------------
#
# Context (ENGINE_QUIRK_FINDINGS.md): a backend started WITHOUT a
# --tool-call-parser flag emits tool calls as plain TEXT in the assistant
# message content, in a model-family-specific format.  A backend that DOES have
# the flag already emits proper OpenAI tool_calls — these extractors are ONLY
# for the un-flagged case and are attached per-backend (opt-in), never
# auto-attached, so they cannot alter any current-config response.
#
# DANGER MODEL: a FALSE extract (turning ordinary prose that merely *looks*
# like a marker into a broken tool_call) is the worst failure — it corrupts a
# correct response.  Every extractor therefore DETECTS first and only acts when
# the family's exact markers are present AND parse succeeds; on any ambiguity or
# malformed markup it PASSES THROUGH the content untouched.  Under-extracting
# (leaving a genuine tool call as text) is recoverable; over-extracting is not.
#
# Verified formats (2026-06-16, see TOOLCALL_EXTRACTORS_REPORT.md):
#   Hermes     <tool_call>{json}</tool_call>           {"name","arguments"}
#   Mistral    [TOOL_CALLS][{json},...]                 JSON array, name/arguments
#   Command-R  Action: ```json [ {tool_name,parameters} ] ```
#   Qwen-XML   <tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>
#   Gemma      ```tool_code\n func(arg=val) \n```       python call expr
#   Llama-3    <|python_tag|>{json}; {json}             {"name","parameters"|"arguments"}


def _new_tool_call(name: str, arguments: Any, index: int) -> dict[str, Any]:
    """Build one OpenAI tool_call dict.

    ``arguments`` is always serialized to a JSON *string* (OpenAI shape).  A
    deterministic, dependency-free id is used (no random/uuid) so the transform
    stays pure and testable; uniqueness within a response is provided by index.
    """
    if isinstance(arguments, str):
        args_str = arguments
    else:
        args_str = json.dumps(arguments, separators=(",", ":"), ensure_ascii=False)
    return {
        "id": f"call_{index:08d}",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


def _apply_extracted(
    payload: dict[str, Any],
    extractor: "Any",
) -> dict[str, Any]:
    """Run a per-choice extractor over a chat-completion payload.

    ``extractor(content: str) -> tuple[list[tool_call] | None, str]`` returns
    (tool_calls, remaining_content).  A None tool_calls list means "no markers /
    malformed — leave this choice untouched".  When tool_calls are returned we
    set them on the message, replace content with the stripped remainder (or
    None if empty), and set finish_reason="tool_calls" (OpenAI shape).

    Only the non-streaming ``message`` shape is handled.  ``delta`` (streaming)
    is intentionally NOT handled: reconstructing a family marker that is split
    across SSE chunks is a separate, stateful problem (cf. StreamingThinkSplit)
    and a naive per-chunk extract would false-extract on partial markers.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        # Never override tool_calls the engine already produced.
        if message.get("tool_calls"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        tool_calls, remaining = extractor(content)
        if not tool_calls:
            continue
        message["tool_calls"] = tool_calls
        message["content"] = remaining if remaining else None
        choice["finish_reason"] = "tool_calls"
    return payload


class _ToolCallExtractor:
    """Base for response-phase tool-call extractors.

    Subclasses implement ``extract(content) -> (tool_calls|None, remaining)``.
    The on_request shim is a no-op; on_response delegates to _apply_extracted.
    """

    def on_request(self, body: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        return body

    def on_response(self, payload: dict[str, Any], ctx: TransformContext) -> dict[str, Any]:
        return _apply_extracted(payload, self.extract)

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:  # pragma: no cover
        raise NotImplementedError


# ---- Hermes -----------------------------------------------------------------
# <tool_call>{"name": "...", "arguments": {...}}</tool_call>
# Verified: vLLM hermes_tool_parser.py (tool_call_start/end tokens) +
# NousResearch/Hermes-2-Pro-Llama-3-8B card.  Content before the first
# <tool_call> is preserved as visible content.

_HERMES_OPEN = "<tool_call>"
_HERMES_CLOSE = "</tool_call>"


class HermesToolCallExtractor(_ToolCallExtractor):
    """Lift Hermes ``<tool_call>{json}</tool_call>`` blocks into tool_calls."""

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:
        if _HERMES_OPEN not in content:
            return None, content
        # Qwen-XML reuses <tool_call> but with a <function=...> body, NOT json.
        # Defer to the Qwen extractor for that form: if the first body looks
        # like XML (<function), this is not Hermes — pass through.
        calls: list[dict[str, Any]] = []
        pos = 0
        idx = 0
        consumed_spans: list[tuple[int, int]] = []
        while True:
            start = content.find(_HERMES_OPEN, pos)
            if start == -1:
                break
            body_start = start + len(_HERMES_OPEN)
            end = content.find(_HERMES_CLOSE, body_start)
            if end == -1:
                # Unterminated block — malformed, do not extract anything.
                return None, content
            body = content[body_start:end].strip()
            if body.startswith("<function"):
                # This is the Qwen-XML form, not Hermes JSON. Pass through.
                return None, content
            try:
                obj = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return None, content
            if not isinstance(obj, dict) or "name" not in obj:
                return None, content
            calls.append(_new_tool_call(obj["name"], obj.get("arguments", {}), idx))
            consumed_spans.append((start, end + len(_HERMES_CLOSE)))
            idx += 1
            pos = end + len(_HERMES_CLOSE)
        if not calls:
            return None, content
        remaining = _strip_spans(content, consumed_spans)
        return calls, remaining


# ---- Mistral ----------------------------------------------------------------
# [TOOL_CALLS][{"name": "...", "arguments": {...}}, ...]
# Verified: vLLM mistral_tool_parser.py (bot_token "[TOOL_CALLS]", regex
# r"\[{.*}\]").  Content before the token is preserved.

_MISTRAL_TOKEN = "[TOOL_CALLS]"


class MistralToolCallExtractor(_ToolCallExtractor):
    """Lift Mistral ``[TOOL_CALLS][{...}]`` arrays into tool_calls."""

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:
        tok = content.find(_MISTRAL_TOKEN)
        if tok == -1:
            return None, content
        before = content[:tok]
        after = content[tok + len(_MISTRAL_TOKEN):].strip()
        # The body must be a JSON array of objects. Find the array span so any
        # trailing text after it is preserved.
        if not after.startswith("["):
            return None, content
        arr, consumed = _scan_json_value(after)
        if arr is None or not isinstance(arr, list) or not arr:
            return None, content
        calls: list[dict[str, Any]] = []
        for i, obj in enumerate(arr):
            if not isinstance(obj, dict) or "name" not in obj:
                return None, content
            calls.append(_new_tool_call(obj["name"], obj.get("arguments", {}), i))
        trailing = after[consumed:].strip()
        remaining = (before + (" " if before and trailing else "") + trailing).strip()
        return calls, remaining


# ---- Command-R (classic c4ai-command-r-v01) ---------------------------------
# Action: ```json\n[ {"tool_name": "...", "parameters": {...}} ]\n```
# Verified: Cohere "Prompting Command R and R+" docs.  Keys are tool_name /
# parameters (NOT name / arguments).  New cmd3/cmd4 melody format is
# closed-source and intentionally NOT handled.

_COMMANDR_RE = re.compile(r"Action:\s*```json\s*(\[.*?\])\s*```", re.DOTALL)


class CommandRToolCallExtractor(_ToolCallExtractor):
    """Lift Command-R ``Action: ```json [...] ``` `` blocks into tool_calls."""

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:
        if "Action:" not in content or "```json" not in content:
            return None, content
        m = _COMMANDR_RE.search(content)
        if m is None:
            return None, content
        try:
            arr = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return None, content
        if not isinstance(arr, list) or not arr:
            return None, content
        calls: list[dict[str, Any]] = []
        for i, obj in enumerate(arr):
            if not isinstance(obj, dict) or "tool_name" not in obj:
                return None, content
            calls.append(_new_tool_call(obj["tool_name"], obj.get("parameters", {}), i))
        remaining = (content[:m.start()] + content[m.end():]).strip()
        return calls, remaining


# ---- Qwen-XML (Qwen3-Coder) -------------------------------------------------
# <tool_call>\n<function=NAME>\n<parameter=KEY>\nVALUE\n</parameter>\n</function>\n</tool_call>
# Verified literally: Qwen/Qwen3-Coder-30B-A3B-Instruct chat_template.

_QWEN_TOOLCALL_RE = re.compile(r"<tool_call>\s*(<function=.*?</function>)\s*</tool_call>", re.DOTALL)
_QWEN_FUNC_RE = re.compile(r"<function=([^>]+)>(.*)</function>", re.DOTALL)
_QWEN_PARAM_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)


class QwenXmlToolCallExtractor(_ToolCallExtractor):
    """Lift Qwen3-Coder ``<tool_call><function=...>`` XML into tool_calls."""

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:
        if "<tool_call>" not in content or "<function=" not in content:
            return None, content
        matches = list(_QWEN_TOOLCALL_RE.finditer(content))
        if not matches:
            return None, content
        calls: list[dict[str, Any]] = []
        spans: list[tuple[int, int]] = []
        for i, m in enumerate(matches):
            fm = _QWEN_FUNC_RE.search(m.group(1))
            if fm is None:
                return None, content
            name = fm.group(1).strip()
            if not name:
                return None, content
            args: dict[str, Any] = {}
            for pm in _QWEN_PARAM_RE.finditer(fm.group(2)):
                key = pm.group(1).strip()
                val = pm.group(2).strip()
                # Best-effort scalar coercion: try JSON (numbers/bools/objects),
                # fall back to the raw string. Values arrive as plain text.
                try:
                    args[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    args[key] = val
            calls.append(_new_tool_call(name, args, i))
            spans.append((m.start(), m.end()))
        remaining = _strip_spans(content, spans)
        return calls, remaining


# ---- Gemma ------------------------------------------------------------------
# ```tool_code\n func(arg=val, ...) \n```  (python call expression)
# CONVENTION (not a spec): Google/philschmid Gemma function-calling example.
# Body parsed via ast as a single call; an optional print(...) wrapper is
# tolerated. Non-call bodies pass through (do not false-extract arbitrary code).

_GEMMA_RE = re.compile(r"```tool_code\s*\n(.*?)\n```", re.DOTALL)


class GemmaToolCallExtractor(_ToolCallExtractor):
    """Lift Gemma ``` ```tool_code func(...) ``` `` blocks into tool_calls."""

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:
        if "```tool_code" not in content:
            return None, content
        matches = list(_GEMMA_RE.finditer(content))
        if not matches:
            return None, content
        calls: list[dict[str, Any]] = []
        spans: list[tuple[int, int]] = []
        idx = 0
        for m in matches:
            parsed = _parse_python_calls(m.group(1).strip())
            if parsed is None:
                # Body is not a clean call expression — do not false-extract.
                return None, content
            for name, args in parsed:
                calls.append(_new_tool_call(name, args, idx))
                idx += 1
            spans.append((m.start(), m.end()))
        if not calls:
            return None, content
        remaining = _strip_spans(content, spans)
        return calls, remaining


# ---- Llama-3 ----------------------------------------------------------------
# <|python_tag|>{"name": "...", "parameters": {...}}; {...}
# Verified: vLLM llama_tool_parser.py (bot_token "<|python_tag|>", "parameters"
# OR "arguments", multiple separated by "; ").  The python_tag prefix is the
# unambiguous marker; without it a bare JSON object is too risky to claim.

_LLAMA_TAG = "<|python_tag|>"


class Llama3ToolCallExtractor(_ToolCallExtractor):
    """Lift Llama-3 ``<|python_tag|>{json}; {json}`` blocks into tool_calls."""

    def extract(self, content: str) -> tuple[Optional[list[dict[str, Any]]], str]:
        tag = content.find(_LLAMA_TAG)
        if tag == -1:
            return None, content
        before = content[:tag]
        rest = content[tag + len(_LLAMA_TAG):].lstrip()
        calls: list[dict[str, Any]] = []
        idx = 0
        pos = 0
        n = len(rest)
        while pos < n:
            # Skip separators / whitespace between objects.
            while pos < n and rest[pos] in "; \t\r\n":
                pos += 1
            if pos >= n:
                break
            if rest[pos] != "{":
                # Trailing non-JSON text after the calls — stop scanning here.
                break
            obj, consumed = _scan_json_value(rest[pos:])
            if obj is None or not isinstance(obj, dict) or "name" not in obj:
                return None, content
            args = obj.get("parameters", obj.get("arguments", {}))
            calls.append(_new_tool_call(obj["name"], args, idx))
            idx += 1
            pos += consumed
        if not calls:
            return None, content
        trailing = rest[pos:].strip()
        remaining = (before.strip() + (" " if before.strip() and trailing else "") + trailing).strip()
        return calls, remaining


# ---- shared extractor helpers ----------------------------------------------


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove [start,end) spans from text and return the trimmed remainder."""
    if not spans:
        return text.strip()
    out: list[str] = []
    prev = 0
    for start, end in spans:
        out.append(text[prev:start])
        prev = end
    out.append(text[prev:])
    return "".join(out).strip()


def _scan_json_value(text: str) -> tuple[Any, int]:
    """Decode the first JSON value at the start of *text*.

    Returns (value, chars_consumed).  On failure returns (None, 0).  Uses the
    stdlib raw_decoder so trailing text after a complete value is allowed.
    """
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
        return obj, end
    except (json.JSONDecodeError, ValueError):
        return None, 0


def _parse_python_calls(src: str) -> Optional[list[tuple[str, dict[str, Any]]]]:
    """Parse one-or-more python call expressions into (name, kwargs) pairs.

    Accepts:
      - a single call: ``func(a=1, b="x")``
      - a ``print(...)`` wrapper around a single call (Gemma variant)
      - newline-separated calls
    Only keyword arguments with constant values are accepted; positional args,
    nested calls, or non-constant values cause a None return (pass-through).
    Returns None on any parse/shape failure — never raises.
    """
    import ast

    lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
    if not lines:
        return None
    results: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        try:
            node = ast.parse(line, mode="eval").body
        except (SyntaxError, ValueError):
            return None
        # Unwrap a single print(...) wrapper.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Call)
        ):
            node = node.args[0]
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            return None
        if node.args:
            # Positional args have no OpenAI mapping — refuse rather than guess.
            return None
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                return None  # **kwargs splat — refuse
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError, TypeError):
                return None
        results.append((node.func.id, kwargs))
    return results or None


# ---------------------------------------------------------------------------
# Module-level registry singleton + built-in registrations
# ---------------------------------------------------------------------------

REGISTRY: TransformRegistry = TransformRegistry()

# ---- voxtral-voices (Step 1 built-in) ------------------------------------
# Source of truth: proxy.py:2438 VOICE_MAP + proxy.py:2460 DEFAULT_VOICE
# Registered here so Step 1 can wire the :3399 application site.
REGISTRY.register(
    "voxtral-voices",
    {
        "kind": "value-map",
        "param": "voice",
        "map": {
            # OpenAI classic voices
            "alloy": "neutral_female",
            "echo": "casual_male",
            "fable": "cheerful_female",
            "nova": "casual_female",
            "onyx": "neutral_male",
            "shimmer": "cheerful_female",
            # OpenAI newer voices → best match
            "sophia": "neutral_female",
            "isabella": "cheerful_female",
            "lily": "casual_female",
            "evan": "casual_male",
            "adam": "neutral_male",
            "ash": "casual_male",
            "coral": "casual_female",
            "sage": "neutral_female",
            "verse": "neutral_male",
            "ballad": "cheerful_female",
            "juniper": "casual_female",
            # Voxtral native voices (pass-through)
            "casual_female": "casual_female",
            "casual_male": "casual_male",
            "cheerful_female": "cheerful_female",
            "neutral_female": "neutral_female",
            "neutral_male": "neutral_male",
            "ar_male": "ar_male",
            "de_female": "de_female",
            "de_male": "de_male",
            "es_female": "es_female",
            "es_male": "es_male",
            "fr_female": "fr_female",
            "fr_male": "fr_male",
            "hi_female": "hi_female",
            "hi_male": "hi_male",
            "it_female": "it_female",
            "it_male": "it_male",
            "nl_female": "nl_female",
            "nl_male": "nl_male",
            "pt_female": "pt_female",
            "pt_male": "pt_male",
        },
        "default": "neutral_female",
    },
)

# ---- qwen3-thinking (Step 2 built-in) ------------------------------------
# Reproduces apply_chat_completions_reasoning_effort() from proxy.py:5242.
# The body-mutation path runs through this registered instance; the 400 error
# sentinel is handled at the dispatch site (proxy.py) which calls
# Qwen3ThinkingTransform().apply() directly for the error return path.
REGISTRY.register_builtin("qwen3-thinking", Qwen3ThinkingTransform())

# ---- reasoning-field-normalize (Step 4a built-in) ------------------------
# Ports _normalize_chat_completion_visible_content from proxy.py:18884.
# Default-ON, detect-and-normalize (Revision D) — registered here for
# future attach-to-backend wiring.  The legacy call site at proxy.py:18631
# is updated in Step 4a to delegate to normalize_chat_completion_payload().
REGISTRY.register_builtin("reasoning-field-normalize", ReasoningFieldNormalizeTransform())

# ---- streaming-think-split (Step 4b built-in, OPT-IN) -------------------
# Catches </think> split across SSE chunks.  Each request needs its own
# instance (stateful carry buffer) — the registry stores the CLASS, not an
# instance.  Attach only when explicitly configured for a backend.
# NOTE: not registered as a singleton — use StreamingThinkSplitTransform()
# directly at the call site when opted in.

# ---------------------------------------------------------------------------
# Step 5 — Tier-1 per-backend packs (suggested-on-add, NOT auto-attached)
# ---------------------------------------------------------------------------
# These are additive — they attach to NEW backends or are opt-in.
# They do NOT alter the current 8-service config.

# ---- ollama-unsupported-params -------------------------------------------
# Ollama /v1/chat/completions rejects these OpenAI params with 400/422.
# Source: ENGINE_QUIRK_FINDINGS.md §2.2 + r/LocalLLaMA audit.
REGISTRY.register(
    "ollama-unsupported-params",
    {
        "kind": "drop-params",
        "drop": ["n", "logprobs", "top_logprobs", "logit_bias", "tool_choice", "user"],
    },
)

# ---- koboldcpp-null-strip -------------------------------------------------
# KoboldCpp 422s on keys whose value is null rather than ignoring them.
# This is a value-test built-in (see KoboldCppNullStripTransform above).
# Source: ENGINE_TRANSFORMS_DESIGN.md §3.3 (null-param-strip).
REGISTRY.register_builtin("koboldcpp-null-strip", KoboldCppNullStripTransform({}))

# ---- llamacpp-cache-prompt ------------------------------------------------
# llama-server enables KV-cache reuse when cache_prompt=true is sent.
# setdefault semantics — does not overwrite if caller explicitly sets false.
# Source: ENGINE_TRANSFORMS_DESIGN.md §2.3, r/LocalLLaMA audit.
REGISTRY.register(
    "llamacpp-cache-prompt",
    {
        "kind": "default-params",
        "set": {"cache_prompt": True},
    },
)

# ---- mistral-penalty-clamp ------------------------------------------------
# Mistral's API accepts frequency_penalty and presence_penalty in [-2.0, 2.0].
# Clamp both so out-of-range client values don't trigger a 400.
# Source: ENGINE_QUIRK_FINDINGS.md §2.6 (Mistral penalty ranges differ).
REGISTRY.register(
    "mistral-penalty-clamp",
    [
        {
            "kind": "clamp",
            "param": "frequency_penalty",
            "min": -2.0,
            "max": 2.0,
        },
        {
            "kind": "clamp",
            "param": "presence_penalty",
            "min": -2.0,
            "max": 2.0,
        },
    ],
)

# ---- tabby-penalty-floor --------------------------------------------------
# TabbyAPI (ExLlamaV2) enforces ge=0 on frequency/presence_penalty via Pydantic.
# Clamp both to a floor of 0.0 so negative client values don't 422.
# Source: ENGINE_TRANSFORMS_DESIGN.md §2.6 (tabby-penalty-clamp example).
REGISTRY.register(
    "tabby-penalty-floor",
    [
        {
            "kind": "clamp",
            "param": "frequency_penalty",
            "min": 0.0,
        },
        {
            "kind": "clamp",
            "param": "presence_penalty",
            "min": 0.0,
        },
    ],
)

# ---------------------------------------------------------------------------
# Step 6 — tool-call family extractors (response-phase, OPT-IN, NOT auto-attached)
# ---------------------------------------------------------------------------
# These lift plain-TEXT tool calls (from backends started without a
# --tool-call-parser) into proper OpenAI tool_calls.  They are registered here
# but attached only per-backend (opt-in) — never auto-attached — so they cannot
# alter any current-config response.  See TOOLCALL_EXTRACTORS_REPORT.md.
REGISTRY.register_builtin("hermes2pro-toolcall-extract", HermesToolCallExtractor())
REGISTRY.register_builtin("mistral-toolcall-extract", MistralToolCallExtractor())
REGISTRY.register_builtin("commandr-toolcall-extract", CommandRToolCallExtractor())
REGISTRY.register_builtin("qwen-xml-toolcall-extract", QwenXmlToolCallExtractor())
REGISTRY.register_builtin("gemma-toolcall-extract", GemmaToolCallExtractor())
REGISTRY.register_builtin("llama3-toolcall-extract", Llama3ToolCallExtractor())
