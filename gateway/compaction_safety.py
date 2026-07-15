"""Validated, data-declared safety contracts for bounded compaction requests.

This module knows no model, engine, alias, or application names.  It turns an
operator-authored v1 record into a small immutable admission contract and
checks the one canonical body that is subsequently forwarded unchanged.

``controlled_deployment_evidence`` is a registry control-plane assertion, not runtime
attestation: the gateway cannot observe an out-of-band backend or template
swap. Operators must install a fresh evidence-bearing generation through the
supported registry surface before this contract is eligible for use.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = 1
PURPOSE = "context_compaction"
OUTPUT_CAP_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
FINAL_BODY_BASE_FIELDS = frozenset({"model", "messages", "max_tokens", "stream"})


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
    controlled_deployment_generation: str
    expires_at: str
    reasoning_effort_default: str
    current_loaded_only: bool
    final_reasoning_control: Mapping[str, Any]


def contract_from_data(
    value: Any,
    *,
    service_id: str,
    service_controlled_deployment_evidence: Any,
    service_deployment_generation: Any,
    service_registry_generation: Any,
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
    deployment_reference = _mapping(
        value.get("controlled_deployment_reference"), "controlled_deployment_reference"
    )
    service_evidence = _mapping(
        service_controlled_deployment_evidence, "service controlled_deployment_evidence"
    )
    _required_strings(tokenizer, ("architecture", "tokenizer_model", "pretokenizer", "vocabulary_payload_sha256", "merge_payload_sha256", "byte_fallback_proof_digest"))
    _exact_int(tokenizer.get("vocabulary_entries"), "tokenizer_contract.vocabulary_entries", minimum=1)
    _exact_int(tokenizer.get("merge_entries"), "tokenizer_contract.merge_entries", minimum=0)
    _required_strings(template, ("revision", "sha256", "render_bound_proof_digest", "mode"))
    if not isinstance(template.get("kwargs"), Mapping):
        raise ContractError("template_contract.kwargs must be an object")
    _required_strings(
        validation,
        ("evidence_revision", "validated_at", "expires_at", "validator_version"),
    )
    _required_strings(
        deployment_reference,
        (
            "service_id",
            "deployment_generation",
            "evidence_revision",
            "evidence_receipt_identity", "evidence_receipt_expires_at",
            "registry_generation", "model_artifact_sha256", "base_model_id",
            "unit_wrapper_contract_digest",
            "declared_runtime_revision",
        ),
    )
    _required_strings(
        service_evidence,
        (
            "service_id",
            "deployment_generation",
            "evidence_revision",
            "evidence_receipt_identity", "evidence_receipt_expires_at",
            "registry_generation", "model_artifact_sha256", "base_model_id",
            "unit_wrapper_contract_digest",
            "declared_runtime_revision",
        ),
    )
    evidence_template = _mapping(
        service_evidence.get("template_contract"),
        "service controlled_deployment_evidence.template_contract",
    )
    reference_template = _mapping(
        deployment_reference.get("template_contract"),
        "controlled_deployment_reference.template_contract",
    )
    _required_strings(evidence_template, ("revision", "sha256", "render_bound_proof_digest", "mode"))
    _required_strings(reference_template, ("revision", "sha256", "render_bound_proof_digest", "mode"))
    if service_evidence["service_id"] != service_id:
        raise ContractError("service controlled deployment evidence does not match service")
    if (
        not isinstance(service_deployment_generation, str)
        or service_evidence["deployment_generation"] != service_deployment_generation
    ):
        raise ContractError("service controlled deployment evidence does not match deployment generation")
    if (
        not isinstance(service_registry_generation, str)
        or service_evidence["registry_generation"] != service_registry_generation
    ):
        raise ContractError("service controlled deployment evidence does not match registry generation")
    for field in (
        "deployment_generation",
        "evidence_revision",
        "evidence_receipt_identity",
        "evidence_receipt_expires_at",
        "registry_generation",
        "model_artifact_sha256",
        "base_model_id",
        "unit_wrapper_contract_digest",
        "declared_runtime_revision",
    ):
        if deployment_reference[field] != service_evidence[field]:
            raise ContractError(f"controlled deployment {field} does not match service evidence")
    if service_evidence["base_model_id"] != declared_base:
        raise ContractError("service controlled deployment evidence base_model_id does not match contract")
    for field in ("revision", "sha256", "render_bound_proof_digest", "mode"):
        if reference_template[field] != service_evidence["template_contract"][field]:
            raise ContractError(f"controlled deployment template {field} does not match service evidence")
        if template[field] != service_evidence["template_contract"][field]:
            raise ContractError(f"template_contract {field} does not match service evidence")
    if template["kwargs"] != evidence_template.get("kwargs") or template["kwargs"] != reference_template.get("kwargs"):
        raise ContractError("template_contract kwargs do not match service evidence")
    for field in ("tokenizer_contract", "request_preparation_contract", "numeric_bounds"):
        reference_value = _mapping(deployment_reference.get(field), f"controlled_deployment_reference.{field}")
        evidence_value = _mapping(service_evidence.get(field), f"service controlled_deployment_evidence.{field}")
        if reference_value != evidence_value:
            raise ContractError(f"controlled deployment {field} does not match service evidence")
    reference_reasoning = _mapping(
        deployment_reference.get("final_reasoning_control"),
        "controlled_deployment_reference.final_reasoning_control",
    )
    evidence_reasoning = _mapping(
        service_evidence.get("final_reasoning_control"),
        "service controlled_deployment_evidence.final_reasoning_control",
    )
    if not evidence_reasoning or reference_reasoning != evidence_reasoning:
        raise ContractError("final_reasoning_control does not match service evidence")
    if tokenizer != service_evidence["tokenizer_contract"]:
        raise ContractError("tokenizer_contract does not match service evidence")
    expected_bounds = {
        "context_window_tokens": context,
        "reserved_output_tokens": output,
        "reserved_headroom_tokens": headroom,
        "max_tokens_per_final_utf8_byte": ratio,
        "max_template_overhead_tokens": overhead,
        "max_final_body_utf8_bytes": ceiling,
    }
    if service_evidence["numeric_bounds"] != expected_bounds:
        raise ContractError("numeric_bounds do not match contract")
    preparation = service_evidence["request_preparation_contract"]
    _required_strings(preparation, ("wrapper_server_request_preparation_digest", "apply_template_path"))
    if preparation.get("context_window_tokens") != context:
        raise ContractError("request preparation context window does not match contract")
    if validation["evidence_revision"] != service_evidence["evidence_revision"]:
        raise ContractError("validation evidence_revision does not match service evidence")
    if validation["expires_at"] != service_evidence["evidence_receipt_expires_at"]:
        raise ContractError("validation expiry does not match service evidence")
    generation = service_evidence["deployment_generation"]
    current_time = time.time() if now is None else now
    validated_at = _timestamp(validation["validated_at"])
    expires_at = validation["expires_at"]
    expires_at_time = _timestamp(expires_at)
    if validated_at > current_time or expires_at_time <= current_time or validated_at >= expires_at_time:
        raise ContractError("contract is expired")
    return CompactionContract(contract_id, PURPOSE, declared_base, context, output, headroom,
                              ratio, overhead, ceiling, generation, expires_at,
                              reasoning_default, True, dict(evidence_reasoning))


def prepare_admitted_body(
    body: Mapping[str, Any], contract: CompactionContract
) -> tuple[dict[str, Any], bytes]:
    """Validate strict chat shape and return the exact compact UTF-8 egress bytes."""
    prepared = dict(body)
    _normalize_output_cap(prepared, contract)
    if set(prepared) - (FINAL_BODY_BASE_FIELDS | set(contract.final_reasoning_control)):
        raise AdmissionError("context compaction final body contains unsupported controls")
    if not isinstance(prepared.get("model"), str) or not prepared["model"]:
        raise AdmissionError("context compaction requires a final model")
    _validate_chat_shape(prepared)
    if "stream" in prepared and prepared["stream"] is not False:
        raise AdmissionError("context compaction permits stream only when false")
    _validate_final_reasoning_control(prepared, contract)
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


def _validate_final_reasoning_control(body: Mapping[str, Any], contract: CompactionContract) -> None:
    """Require the operator-declared final reasoning-off representation exactly."""
    actual = {field: body.get(field) for field in contract.final_reasoning_control}
    if actual != contract.final_reasoning_control:
        raise AdmissionError("context compaction final reasoning control does not match contract")


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


def _normalize_output_cap(body: dict[str, Any], contract: CompactionContract) -> None:
    """Collapse compatible OpenAI cap aliases before one final serialization."""
    supplied = [(field, body[field]) for field in OUTPUT_CAP_FIELDS if field in body]
    if not supplied:
        body["max_tokens"] = contract.reserved_output_tokens
        return
    values = [value for _, value in supplied]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise AdmissionError("output cap aliases must be non-negative integers")
    if len(set(values)) != 1:
        raise AdmissionError("output cap aliases must be identical")
    cap = values[0]
    if cap > contract.reserved_output_tokens:
        raise AdmissionError(f"output cap must be at most {contract.reserved_output_tokens}")
    for field, _ in supplied:
        body.pop(field)
    body["max_tokens"] = cap


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
