"""What a person or an app is told when a request fails.

One table maps each failure to the layer that stopped it and one next action
(principles P3 and P15, ``docs/design/VOICE.md`` rule 4). API clients keep the
stable ``code`` and HTTP status. Raw ids, parameter names, exception classes
and policy terms go in ``details``, never in ``message``.
"""

from __future__ import annotations

import math
import re
from string import Formatter
from typing import Any, Mapping

from gateway import gateway_messages

# The layers a person can tell apart. "unknown" says the evidence does not
# show where the request stopped.
LAYERS = frozenset({"request", "account", "library", "gateway", "engine", "model", "workflow", "unknown"})

UNKNOWN = "unknown"

# Every row is (layer, member copy[, API copy]); the sentences are display copy in registry/gateway_messages.yml.
_COPY: dict[str, tuple[str, str] | tuple[str, str, str]] = dict(gateway_messages.GATEWAY_MESSAGE_TABLES["public_failures"])
if not {row[0] for row in _COPY.values()} <= LAYERS:
    raise ValueError("gateway messages: public_failures names a layer that is not in LAYERS")


# API copy may name what the client itself sent; member copy never does.
_IDENTIFIER_FIELDS = frozenset({"input", "model"})


def _public_value(name: str, value: Any) -> bool:
    if name in _IDENTIFIER_FIELDS:
        return isinstance(value, str) and 0 < len(value) <= 200 and value.isprintable()
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and abs(value) <= 10 ** 12 and math.isfinite(value))


def describe(key: str, *, audience: str = "ui", **values: Any) -> tuple[str, str]:
    """Render one shared entry.

    Numeric placeholders must be bounded numbers. Only API copy names an
    input or model, the client's own request terms.
    """
    entry = _COPY.get(key, _COPY[UNKNOWN])
    template = entry[2] if audience == "api" and len(entry) == 3 else entry[1]
    fields = {name for _, name, _, _ in Formatter().parse(template) if name}
    if not all(_public_value(name, values.get(name)) for name in fields):
        return _COPY[UNKNOWN][0], _COPY[UNKNOWN][1]
    return entry[0], template.format(**values)


def _reason(message: Any, code: Any, values: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Recognize evidence, never echo arbitrary upstream text or schema values."""
    preferred = [key for key in (code, values.get("copy_key")) if isinstance(key, str) and key in _COPY]
    values = {name: value for name, value in values.items()
              if name in {"maximum", "minimum", "max_chars", "max_images"}}
    if isinstance(message, str):
        if message in {"Canceled by requester", "Cancelled by requester"}:
            return "canceled", {}
        for key in dict.fromkeys([*preferred, *_COPY]):
            for audience in ("api", "ui"):
                layer, rendered = describe(key, audience=audience, **dict(values))
                if key != UNKNOWN and rendered != _COPY[UNKNOWN][1] and message == rendered:
                    return key, dict(values)
        # Legacy errors contain the useful reason alongside ids or exception
        # classes. Extract only the bounded fact and render it from the table.
        if re.search(r"\bout of memory\b", message, re.IGNORECASE):
            return "out_of_memory", {}
        if re.search(r"\bprofile\b.{0,160}\b(?:may not|not allowed to|cannot)\s+run\b",
                     message, re.IGNORECASE):
            return "profile_denied", {}
        limit = re.search(r"\bat (most|least) (-?\d{1,12}(?:\.\d{1,6})?)(?!\w|\.\d)", message)
        if limit:
            value = float(limit[2]) if "." in limit[2] else int(limit[2])
            return ("value_too_large", {"maximum": value}) if limit[1] == "most" else (
                "value_too_small", {"minimum": value})
        for pattern, key in (
            (r"\bmust be a string\b", "input_string"),
            (r"\bmust be an integer\b", "input_integer"),
            (r"\bmust be a number\b", "input_number"),
            (r"\bmust be a boolean\b", "input_boolean"),
            (r"\bmust be one of\b", "input_choice"),
            (r"\bmissing required input\b", "input_required"),
            (r"\bunknown input\b", "input_unknown"),
        ):
            if re.search(pattern, message):
                return key, {}
    if isinstance(code, str) and code in _COPY:
        return code, dict(values)
    return UNKNOWN, {}


def project_failure(error: Any, *, audience: str = "api") -> dict[str, Any]:
    """Project a gateway error or terminal-job error without losing diagnostics.

    API envelopes retain specific safe reasons. UI callers explicitly request
    layer-and-action copy. Code, type, decision and raw evidence stay separate
    from the sentence; malformed upstream details cannot replace our layer.
    """
    source = dict(error) if isinstance(error, Mapping) else {"message": error}
    upstream = source.get("details")
    details = dict(upstream) if isinstance(upstream, Mapping) else {}
    if upstream is not None and not isinstance(upstream, Mapping):
        details["upstream_details"] = upstream
    key, values = _reason(source.get("message"), source.get("code"), details)
    layer, message = describe(key, audience=audience, **values)
    if source.get("message") != message and source.get("message") is not None:
        details.setdefault("original_message", source["message"])
    details.update(values)
    details["copy_key"] = key
    details["layer"] = layer
    details["ui_message"] = describe(key, **values)[1]
    for name, value in source.items():
        if name not in {"message", "details", "code", "type", "decision"}:
            details[name] = value
    # Reserved projection fields are always ours, even with hostile diagnostics.
    details["copy_key"] = key
    details["layer"] = layer
    details["ui_message"] = describe(key, **values)[1]
    return {**{name: source[name] for name in ("code", "type", "decision") if name in source},
            "message": message, "details": details}


def project_job(view: Mapping[str, Any], *, audience: str = "ui") -> dict[str, Any]:
    """Copy a client job and normalize terminal failure text at its boundary."""
    result = dict(view)
    status = result.get("status")
    if status not in {"failed", "canceled", "cancelled"}:
        return result
    error = result.get("error")
    if not error and status in {"canceled", "cancelled"}:
        error = {"code": "canceled"}
    if not isinstance(error, Mapping) and isinstance(result.get("error_details"), Mapping):
        error = {"message": error, "details": result["error_details"]}
    projected = project_failure(error, audience=audience)
    result["error"] = projected["message"]
    result["error_details"] = projected["details"]
    for name in ("code", "type", "decision"):
        if name in projected:
            result["error_details"][name] = projected[name]
    return result


class PublicFailure(Exception):
    """A failure whose message is safe to show; ``details`` are for developers.

    ``copy`` picks the sentence when one code covers several causes; it
    defaults to ``code``. ``details`` also fill the sentence's placeholders.
    """

    def __init__(self, code: str, status_code: int, *, copy: str | None = None,
                 retry_after: int | None = None, **details: Any) -> None:
        self.layer, self.message = describe(copy or code, **details)
        super().__init__(self.message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = {"layer": self.layer, **details}

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
