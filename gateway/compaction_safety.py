"""Validated, data-declared safety contracts for bounded compaction requests.

This module knows no model, engine, alias, or application names.  It turns an
operator-authored v1 record into a small immutable admission contract and
checks the one canonical body that is subsequently forwarded unchanged.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = 1
PURPOSE = "context_compaction"


class ContractError(ValueError):
    """An operator contract is absent, stale, malformed, or incompatible."""


class AdmissionError(ValueError):
    """A request does not fit an otherwise valid contract."""


@dataclass(frozen=True)
class CompactionContract:
    id: str
    purpose: str
    base_model_id: str
    context_window_tokens: int
    reserved_output_tokens: int
    reserved_headroom_tokens: int
    max_tokens_per_final_utf8_byte: int
    max_template_overhead_tokens: int
    max_final_body_utf8_bytes: int
    deployment_generation: str
    expires_at: str
    reasoning_effort_default: str
    current_loaded_only: bool


def contract_from_data(
    value: Any,
    *,
    service_id: str,
    service_generation: Any,
    base_model_id: str,
    now: float | None = None,
) -> CompactionContract:
    """Parse and validate the supported data-only v1 contract.

    Static proof identifiers and artifact identities are deliberately data
    requirements. Gateway execution validates their shape and the derived
    arithmetic; an offline operator validator owns artifact verification.
    """
    if not isinstance(value, Mapping):
        raise ContractError("context_compaction contract is absent")
    _exact_int(value.get("schema_version"), "schema_version", minimum=SCHEMA_VERSION,
               exact=SCHEMA_VERSION)
    contract_id = _nonempty_string(value.get("id"), "id")
    if value.get("purpose") != PURPOSE:
        raise ContractError("purpose must be context_compaction")
    declared_base = _nonempty_string(value.get("base_model_id"), "base_model_id")
    if declared_base != base_model_id:
        raise ContractError("base_model_id does not match resolved base model")

    constraints = _mapping(value.get("constraints"), "constraints")
    if constraints.get("text_messages_only") is not True:
        raise ContractError("constraints.text_messages_only must be true")
    for name in ("tools", "tool_calls", "vision_content"):
        if constraints.get(name) != "forbidden":
            raise ContractError(f"constraints.{name} must be forbidden")
    reasoning_default = constraints.get("reasoning_effort_default")
    if reasoning_default != "none":
        raise ContractError("constraints.reasoning_effort_default must be none")
    if constraints.get("current_loaded_only") is not True:
        raise ContractError("constraints.current_loaded_only must be true")
    if constraints.get("activation") != "forbidden":
        raise ContractError("constraints.activation must be forbidden")

    context = _exact_int(value.get("context_window_tokens"), "context_window_tokens", minimum=1)
    output = _exact_int(value.get("reserved_output_tokens"), "reserved_output_tokens", minimum=0)
    headroom = _exact_int(value.get("reserved_headroom_tokens"), "reserved_headroom_tokens", minimum=0)
    ratio = _exact_int(value.get("max_tokens_per_final_utf8_byte"), "max_tokens_per_final_utf8_byte", minimum=1)
    overhead = _exact_int(value.get("max_template_overhead_tokens"), "max_template_overhead_tokens", minimum=0)
    ceiling = _exact_int(value.get("max_final_body_utf8_bytes"), "max_final_body_utf8_bytes", minimum=1)
    expected_ceiling = (context - output - headroom - overhead) // ratio
    if expected_ceiling < 1 or ceiling != expected_ceiling:
        raise ContractError("max_final_body_utf8_bytes does not match declared inequality")

    tokenizer = _mapping(value.get("tokenizer_contract"), "tokenizer_contract")
    template = _mapping(value.get("template_contract"), "template_contract")
    validation = _mapping(value.get("validation"), "validation")
    binding = _mapping(value.get("runtime_binding"), "runtime_binding")
    _required_strings(tokenizer, ("architecture", "tokenizer_model", "pretokenizer", "vocabulary_payload_sha256", "merge_payload_sha256", "byte_fallback_proof_digest"))
    _exact_int(tokenizer.get("vocabulary_entries"), "tokenizer_contract.vocabulary_entries", minimum=1)
    _exact_int(tokenizer.get("merge_entries"), "tokenizer_contract.merge_entries", minimum=0)
    _required_strings(template, ("revision", "sha256", "render_bound_proof_digest"))
    if not isinstance(template.get("kwargs"), Mapping):
        raise ContractError("template_contract.kwargs must be an object")
    _required_strings(
        validation,
        ("evidence_revision", "validated_at", "expires_at", "validator_version"),
    )
    _required_strings(binding, ("service_id", "deployment_generation", "unit_wrapper_contract_digest", "runtime_revision"))
    if binding["service_id"] != service_id:
        raise ContractError("runtime_binding.service_id does not match service")
    generation = binding["deployment_generation"]
    if not isinstance(service_generation, str) or not service_generation or generation != service_generation:
        raise ContractError("runtime deployment generation does not match contract")
    current_time = time.time() if now is None else now
    validated_at = _timestamp(validation["validated_at"])
    expires_at = validation["expires_at"]
    expires_at_time = _timestamp(expires_at)
    if validated_at > current_time or expires_at_time <= current_time or validated_at >= expires_at_time:
        raise ContractError("contract is expired")
    return CompactionContract(contract_id, PURPOSE, declared_base, context, output, headroom,
                              ratio, overhead, ceiling, generation, expires_at,
                              reasoning_default, True)


def prepare_admitted_body(
    body: Mapping[str, Any], contract: CompactionContract
) -> tuple[dict[str, Any], bytes]:
    """Validate strict chat shape and return the exact compact UTF-8 egress bytes."""
    prepared = dict(body)
    _validate_chat_shape(prepared)
    _validate_reasoning(prepared, contract)
    _validate_output_limit(prepared, contract)
    prepared.setdefault("reasoning_effort", contract.reasoning_effort_default)
    if "max_tokens" not in prepared and "max_completion_tokens" not in prepared:
        prepared["max_tokens"] = contract.reserved_output_tokens
    try:
        encoded = json.dumps(
            prepared, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AdmissionError("body cannot be encoded as canonical UTF-8 JSON") from exc
    count = len(encoded)
    if count > contract.max_final_body_utf8_bytes:
        raise AdmissionError("canonical body exceeds conservative byte ceiling")
    conservative_tokens = (
        count * contract.max_tokens_per_final_utf8_byte
        + contract.max_template_overhead_tokens
        + contract.reserved_output_tokens
        + contract.reserved_headroom_tokens
    )
    if conservative_tokens > contract.context_window_tokens:
        raise AdmissionError("canonical body exceeds conservative context guarantee")
    return prepared, encoded


def _validate_chat_shape(body: Mapping[str, Any]) -> None:
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise AdmissionError("context compaction requires exactly system and user messages")
    for expected_role, message in zip(("system", "user"), messages):
        if (
            not isinstance(message, Mapping)
            or message.get("role") != expected_role
            or not isinstance(message.get("content"), str)
        ):
            raise AdmissionError("context compaction requires scalar system and user text")
        if set(message) != {"role", "content"}:
            raise AdmissionError("context compaction messages may contain only role and content")
    for forbidden in ("tools", "tool_choice", "parallel_tool_calls"):
        if forbidden in body:
            raise AdmissionError(f"context compaction forbids {forbidden}")


def _validate_reasoning(body: Mapping[str, Any], contract: CompactionContract) -> None:
    for field in ("reasoning_effort", "reasoning"):
        if field in body and body[field] not in (None, contract.reasoning_effort_default):
            raise AdmissionError("context compaction requires reasoning none")


def _validate_output_limit(body: Mapping[str, Any], contract: CompactionContract) -> None:
    for field in ("max_tokens", "max_completion_tokens"):
        if field not in body:
            continue
        value = body[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > contract.reserved_output_tokens
        ):
            raise AdmissionError(f"{field} must be an integer between 0 and {contract.reserved_output_tokens}")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} must be a non-empty string")
    return value


def _required_strings(value: Mapping[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        _nonempty_string(value.get(field), field)


def _exact_int(value: Any, field: str, *, minimum: int, exact: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (exact is not None and value != exact)
    ):
        raise ContractError(f"{field} must be an integer")
    return value


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("validation.expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("validation.expires_at must include a timezone")
    return parsed.astimezone(timezone.utc).timestamp()
