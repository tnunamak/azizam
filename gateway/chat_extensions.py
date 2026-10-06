"""Chat/Responses extension contract for gateway-native controls.

Standard OpenAI request fields stay at the top level. Gateway-owned knobs live
under ``extensions.com.azizam.chat`` or ``extensions.com.azizam.routing``.
This module validates that contract without logging prompt or message values.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from gateway.extensions import (
    ExtensionError,
    ExtensionValidationResult,
    extract_extension_namespace,
    validate_extension_inputs,
)

CHAT_EXTENSION_NAMESPACE = "com.azizam.chat"
ROUTING_EXTENSION_NAMESPACE = "com.azizam.routing"

STANDARD_CHAT_COMPLETIONS_FIELDS: frozenset[str] = frozenset(
    {
        "audio",
        "chat_template_kwargs",
        "frequency_penalty",
        "function_call",
        "functions",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "max_tokens",
        "metadata",
        "messages",
        "model",
        "modalities",
        "n",
        "parallel_tool_calls",
        "prediction",
        "presence_penalty",
        "reasoning_effort",
        "response_format",
        "seed",
        "service_tier",
        "stop",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "user",
        "web_search_options",
        "extensions",
    }
)

STANDARD_RESPONSES_FIELDS: frozenset[str] = frozenset(
    {
        "background",
        "conversation",
        "include",
        "input",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "model",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
        "extensions",
    }
)

GATEWAY_LOOKING_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "allow_backend_fallback",
        "backend",
        "local_routing",
        "local_routing_preference",
        "policy_profile",
        "receipt",
        "receipt_verbosity",
        "routing",
        "routing_preference",
    }
)

CHAT_EXTENSION_SPECS: dict[str, dict[str, Any]] = {
    "receipt_verbosity": {
        "type": "string",
        "enum": ["none", "summary", "detailed"],
    },
}

ROUTING_EXTENSION_SPECS: dict[str, dict[str, Any]] = {
    "local_preference": {
        "type": "string",
        "enum": ["auto", "local"],
    },
}

CHAT_ROUTE_MANAGED_FIELDS: frozenset[str] = frozenset(
    {
        "input",
        "instructions",
        "messages",
        "model",
        "policy_profile",
        "reasoning",
        "reasoning_effort",
        "tools",
    }
)
ROUTING_ROUTE_MANAGED_FIELDS: frozenset[str] = frozenset(
    {
        "allow_backend_fallback",
        "backend",
        "model",
        "policy_profile",
        "target_backend",
    }
)


@dataclass
class ChatExtensionsResult:
    request: dict[str, Any]
    chat: ExtensionValidationResult
    routing: ExtensionValidationResult
    unknown_top_level: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            not self.unknown_top_level
            and self.chat.ok
            and self.routing.ok
        )

    @property
    def rejected(self) -> dict[str, str]:
        rejected = dict(self.unknown_top_level)
        rejected.update(
            {
                f"extensions.{self.chat.namespace}.{name}": reason
                for name, reason in self.chat.rejected.items()
            }
        )
        rejected.update(
            {
                f"extensions.{self.routing.namespace}.{name}": reason
                for name, reason in self.routing.rejected.items()
            }
        )
        return rejected


def _unknown_top_level_gateway_fields(
    request_body: Mapping[str, Any],
    standard_fields: Iterable[str],
) -> dict[str, str]:
    standard = set(standard_fields)
    unknown: dict[str, str] = {}
    for name in request_body:
        if name in standard:
            continue
        if name in GATEWAY_LOOKING_TOP_LEVEL_FIELDS:
            unknown[str(name)] = (
                "gateway-native chat/routing controls belong under "
                f"extensions.{CHAT_EXTENSION_NAMESPACE} or "
                f"extensions.{ROUTING_EXTENSION_NAMESPACE}"
            )
    return unknown


def _reject_route_managed(
    supplied: Mapping[str, Any],
    route_managed: Iterable[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    managed = set(route_managed)
    filtered: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for name, value in supplied.items():
        if name in managed:
            rejected[name] = (
                f"{name} is owned by the standard request, route, or policy layer"
            )
        else:
            filtered[name] = value
    return filtered, rejected


def _validate_namespace(
    request_body: Mapping[str, Any],
    *,
    namespace: str,
    specs: Mapping[str, Mapping[str, Any]],
    route_managed: Iterable[str],
) -> ExtensionValidationResult:
    supplied, route_rejected = _reject_route_managed(
        extract_extension_namespace(request_body, namespace),
        route_managed,
    )
    result = validate_extension_inputs(
        specs,
        supplied,
        namespace=namespace,
        supported_names=specs.keys(),
    )
    result.rejected.update(route_rejected)
    return result


def validate_chat_extensions(
    request_body: Mapping[str, Any],
    *,
    standard_fields: Iterable[str],
) -> ChatExtensionsResult:
    if not isinstance(request_body, Mapping):
        raise ExtensionError("request body must be a mapping")

    request = copy.deepcopy(dict(request_body))
    unknown_top_level = _unknown_top_level_gateway_fields(
        request,
        standard_fields,
    )
    chat = _validate_namespace(
        request,
        namespace=CHAT_EXTENSION_NAMESPACE,
        specs=CHAT_EXTENSION_SPECS,
        route_managed=CHAT_ROUTE_MANAGED_FIELDS,
    )
    routing = _validate_namespace(
        request,
        namespace=ROUTING_EXTENSION_NAMESPACE,
        specs=ROUTING_EXTENSION_SPECS,
        route_managed=ROUTING_ROUTE_MANAGED_FIELDS,
    )
    request.pop("extensions", None)
    return ChatExtensionsResult(
        request=request,
        chat=chat,
        routing=routing,
        unknown_top_level=unknown_top_level,
    )


def validate_chat_completions_extensions(
    request_body: Mapping[str, Any],
) -> ChatExtensionsResult:
    return validate_chat_extensions(
        request_body,
        standard_fields=STANDARD_CHAT_COMPLETIONS_FIELDS,
    )


def validate_responses_extensions(
    request_body: Mapping[str, Any],
) -> ChatExtensionsResult:
    return validate_chat_extensions(
        request_body,
        standard_fields=STANDARD_RESPONSES_FIELDS,
    )
