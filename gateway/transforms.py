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
from typing import Any, Protocol, runtime_checkable

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
