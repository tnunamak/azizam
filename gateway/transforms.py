"""Transform engine: declarative request/response body transforms.

This module implements the six primitive transform types from
docs/design/ENGINE_TRANSFORMS_DESIGN.md §2, a named-transform registry,
and a pipeline runner.

Service and model records attach transforms as data; proxy.py runs the
attached pipelines.

Public surface
--------------
- Transform (Protocol)        — on_request / on_response
- TransformContext             — opaque bag passed through the pipeline
- TransformRegistry            — load / resolve named transforms
- REGISTRY                     — module-level singleton
- run_request_pipeline(specs, body, ctx, registry) -> body
- run_response_pipeline(specs, payload, ctx, registry) -> payload
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any, Optional, Protocol, TypeAlias, runtime_checkable

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


class TransformRequestError(ValueError):
    """An attached transform rejected a request before upstream dispatch."""

    def __init__(self, message: str, *, param: str, code: str = "invalid_value") -> None:
        super().__init__(message)
        self.param = param
        self.code = code


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


# A service/model attachment is either a reviewed named built-in or one inline
# instance of the fixed declarative primitive vocabulary. This lets ordinary
# policy remain in service data rather than requiring a Python registration for
# every configuration value.
TransformSpec: TypeAlias = str | Mapping[str, Any]


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

        kind: value-map
        param: quality
        map: { hd: high, standard: medium }
        default: medium
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

        completion-cap-canonicalize:
          kind: param-map
          from: max_completion_tokens
          to: max_tokens
          only_if_destination_absent: true
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._from: str = cfg["from"]
        self._to: str = cfg["to"]
        self._only_if_destination_absent = bool(
            cfg.get("only_if_destination_absent", False)
        )

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        value = _get_nested(body, self._from)
        if value is _MISSING:
            return body
        _del_nested(body, self._from)
        if (
            self._only_if_destination_absent
            and _get_nested(body, self._to) is not _MISSING
        ):
            return body
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
    transform_specs: list[TransformSpec],
    body: dict[str, Any],
    ctx: TransformContext | None = None,
    registry: TransformRegistry | None = None,
) -> dict[str, Any]:
    """Apply request transforms in order.

    Each spec is either a named built-in id or an inline declarative primitive
    mapping from a service/model record. The input body is modified in-place.
    Built-ins declaring request_phase="final" run after ordinary policy steps,
    in attachment order, so translation can consume model/alias defaults.
    """
    if ctx is None:
        ctx = TransformContext()
    if registry is None:
        registry = REGISTRY
    final_transforms = []
    for spec in transform_specs:
        primitives = (
            registry.resolve(spec)
            if isinstance(spec, str)
            else [_build_primitive(dict(spec))]
        )
        for primitive in primitives:
            if getattr(primitive, "request_phase", "request") == "final":
                final_transforms.append(primitive)
            else:
                body = primitive.on_request(body, ctx)
    for primitive in final_transforms:
        body = primitive.on_request(body, ctx)
    return body


def run_response_pipeline(
    transform_specs: list[TransformSpec],
    payload: dict[str, Any],
    ctx: TransformContext | None = None,
    registry: TransformRegistry | None = None,
) -> dict[str, Any]:
    """Apply response transforms in order; see :func:`run_request_pipeline`."""
    if ctx is None:
        ctx = TransformContext()
    if registry is None:
        registry = REGISTRY
    for spec in transform_specs:
        primitives = (
            registry.resolve(spec)
            if isinstance(spec, str)
            else [_build_primitive(dict(spec))]
        )
        for primitive in primitives:
            payload = primitive.on_response(payload, ctx)
    return payload


# ---------------------------------------------------------------------------
# Named built-ins  (reviewed composed classes, not arbitrary-code plugins)
# ---------------------------------------------------------------------------

# Constant for the enable_thinking kwarg key — single source of truth.
# QWEN3_NO_THINK_HINT is kept for backward-compat (tests/test_step3 imports it);
# the /no_think prompt-string injection is removed — only the kwarg is load-bearing.
QWEN3_NO_THINK_HINT: str = "/no_think"
QWEN3_ENABLE_THINKING_KEY: str = "enable_thinking"


def apply_thinking_suppression(body: dict[str, Any]) -> None:
    """Suppress reasoning/thinking on *body* in-place via the standard mechanism.

    Sets ``chat_template_kwargs.enable_thinking = False`` (via setdefault). This
    is the ecosystem-standard reasoning toggle (vLLM auto-maps reasoning_effort
    to it; the model's own chat template interprets it) — engine-agnostic DATA,
    not a prompt-string hack.

    The legacy ``/no_think`` PROMPT-STRING injection was removed: it is
    non-standard AND verified non-functional on this deployment (a live
    reasoning_effort=none test showed the enable_thinking kwarg suppresses
    thinking while the /no_think tag alone did not). See
    docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md D-1.
    """
    kwargs = body.setdefault("chat_template_kwargs", {})
    if isinstance(kwargs, dict):
        kwargs.setdefault(QWEN3_ENABLE_THINKING_KEY, False)


class ReasoningEffortTransform:
    """Translate qualitative effort into the configured backend budget.

    Registered as "reasoning-effort" in REGISTRY.

    Explicit attachments run in list order on every request path, using the
    instance policy. apply() returns an error string; the legacy on_request()
    shim only mutates the body and leaves error handling to the caller.

    Contract:
      1. Effort absent → preserve a valid concrete budget, else use the default
         effort if configured, else no-op.
      2. Pop reasoning_effort. If None → no-op (treat as absent after pop).
      3. If not str or not in _REASONING_EFFORT_VALUES → return error string.
      4. Translate the configured effort to ``thinking_budget_tokens``.
      5. For zero budget, also set ``enable_thinking=False``.
    """

    # The valid set — matches _REASONING_EFFORT_VALUES in proxy.py:5235
    _VALID: frozenset[str] = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
    # Efforts that suppress thinking
    _DEFAULT_BUDGETS: dict[str, int | str] = {
        "none": 0,
        "minimal": 2048,
        "low": 4096,
        "medium": 6144,
        "high": 8192,
        "xhigh": "unlimited",
    }

    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        policy = policy or {}
        raw_budgets = policy.get("effort_budgets")
        budgets = raw_budgets if isinstance(raw_budgets, dict) else self._DEFAULT_BUDGETS
        self._budgets: dict[str, int | str] = dict(budgets)
        self._default_effort = policy.get("default_effort")

    def apply(self, body: dict[str, Any]) -> str | None:
        """Mutate body in-place.  Return an error string or None."""
        if "reasoning_effort" not in body:
            # Internal routing can pass through the gateway more than once
            # (gateway -> LiteLLM -> gateway -> engine).  A concrete budget is
            # the output of this transform, so preserve it instead of applying
            # the default policy a second time.
            if "thinking_budget_tokens" in body:
                existing_budget = body["thinking_budget_tokens"]
                if (
                    not isinstance(existing_budget, int)
                    or isinstance(existing_budget, bool)
                    or existing_budget < -1
                ):
                    return (
                        "Invalid value for 'thinking_budget_tokens': "
                        f"{existing_budget!r}. Expected an integer >= -1."
                    )
                return None
            if isinstance(self._default_effort, str):
                body["reasoning_effort"] = self._default_effort
            else:
                return None
        effort = body.pop("reasoning_effort")
        if effort is None:
            return None
        if not isinstance(effort, str) or effort not in self._VALID:
            return (
                f"Invalid value for 'reasoning_effort': {effort!r}. "
                f"Expected one of: {sorted(self._VALID)}."
            )
        budget = self._budgets.get(effort)
        if not isinstance(budget, (int, str)) or isinstance(budget, bool):
            return f"No reasoning budget configured for effort {effort!r}."
        if isinstance(budget, str) and budget != "unlimited":
            return f"Invalid reasoning budget configured for effort {effort!r}."
        body["thinking_budget_tokens"] = -1 if budget == "unlimited" else budget
        if budget == 0:
            apply_thinking_suppression(body)
        return None

    # Transform protocol shim — lets this live in the registry alongside
    # primitives even though it is not a _Primitive subclass.
    def on_request(self, body: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        self.apply(body)
        return body

    def on_response(self, payload: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        return payload


class FinalChatReasoningEffortTransform(ReasoningEffortTransform):
    """Opt-in replacement for the former automatic final chat translation.

    Registered separately so existing explicit reasoning-effort attachments
    retain their ordering, path coverage, and instance policy. Runtime context
    supplies this step's policy; validation errors become HTTP 400 responses.
    """

    request_phase = "final"

    def on_request(self, body: dict[str, Any], ctx: "TransformContext") -> dict[str, Any]:
        if ctx.get("request_path", "/v1/chat/completions") != "/v1/chat/completions":
            return body
        policy = ctx.get("reasoning")
        transform = ReasoningEffortTransform(policy) if policy is not None else self
        error = transform.apply(body)
        if error:
            raise TransformRequestError(error, param="reasoning_effort")
        return body


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
      2. ``reasoning_content`` contains ``<think>…</think>`` plus visible text
         → split the fields while preserving the visible text.

    A response with only ``reasoning_content`` and empty/absent ``content`` is
    already a valid thinking-only response.  Do not copy private reasoning
    into visible content: doing so leaks chain-of-thought to clients and makes
    streaming reasoning appear as repeated assistant output.

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
                # Case 2: some backends put both reasoning and visible text in
                # reasoning_content. Recover only that explicitly marked
                # visible suffix; plain reasoning-only content stays private.
                split_r, visible = split_reasoning_visible_text(reasoning)
                if "</think>" in reasoning.lower() and visible is not None:
                    message["content"] = visible
                    message["reasoning_content"] = split_r or reasoning
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
            # A reasoning-only delta is valid. Never mirror it into visible
            # content; clients may render the two fields separately.
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
# Step 5 — drop-null-params (generic built-in, value-test not key-list)
# ---------------------------------------------------------------------------


class DropNullParamsTransform(_Primitive):
    """Remove top-level keys whose value is None/null.

    Registered as "drop-null-params" in REGISTRY.

    A value-based filter that cannot be expressed with drop-params (which is
    key-list only).  Iterates all top-level keys and removes those with a None
    value.  Useful for any strict backend that 422s on explicit null params
    rather than treating them as absent.

    The ``when:`` predicate and phase routing are inherited from _Primitive.

    YAML config (conceptual, for documentation):
        drop-null-params:
          kind: (built-in)
    """

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        null_keys = [k for k, v in body.items() if v is None]
        for k in null_keys:
            del body[k]
        return body



# ---------------------------------------------------------------------------
# Module-level registry singleton + built-in registrations
# ---------------------------------------------------------------------------

REGISTRY: TransformRegistry = TransformRegistry()

# ---- reasoning effort (reviewed opt-in built-ins) -------------------------
# Keep explicit list-order translation separate from the final chat policy.
REGISTRY.register_builtin("reasoning-effort", ReasoningEffortTransform())
REGISTRY.register_builtin("reasoning-effort-final-chat", FinalChatReasoningEffortTransform())

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

# ---- drop-null-params -----------------------------------------------------
# Some strict backends 422 on keys whose value is null rather than ignoring
# them.  This is a value-test built-in (see DropNullParamsTransform above).
# Source: ENGINE_TRANSFORMS_DESIGN.md §3.3 (null-param-strip).
REGISTRY.register_builtin("drop-null-params", DropNullParamsTransform({}))

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
