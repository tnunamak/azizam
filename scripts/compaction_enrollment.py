#!/usr/bin/env python3
"""Build a redacted, offline context-compaction enrollment package.

This is deliberately an operator evidence boundary, not a deployment tool.  It
only reads a declared JSON manifest and the local files named by it.  It never
opens the registry, authenticates, sends HTTP, starts a process, or performs
inference.  The /apply-template and isolated-render observations are supplied
as redacted transcripts; a live operator collects those separately.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

# Direct ``python scripts/compaction_enrollment.py`` execution otherwise puts
# only ``scripts/`` on sys.path.  Add the repository root before importing the
# deliberately dependency-minimal gateway preparation module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.operation_candidate import prepare_operation_candidate_body


FORMAT = "gateway-compaction-enrollment/v1"
FINAL_REASONING_CONTROL = {
    "thinking_budget_tokens": 0,
    "chat_template_kwargs": {"enable_thinking": False},
}
NUMERIC_BOUNDS = {
    "context_window_tokens": 102400,
    "reserved_output_tokens": 8192,
    "reserved_headroom_tokens": 16384,
    "max_tokens_per_final_utf8_byte": 1,
    "max_template_overhead_tokens": 320,
    "max_final_body_utf8_bytes": 77504,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|credential|password|secret)", re.I)
_SECRET_VALUE = re.compile(r"(?:opk_|oak_|sk-|bearer\s+)", re.I)


class EnrollmentError(ValueError):
    """A declared artifact cannot support a fail-closed enrollment package."""


def _reject_floats(value: Any, *, field: str = "document") -> None:
    if isinstance(value, float):
        raise EnrollmentError(f"{field} cannot contain floats")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_floats(child, field=f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_floats(child, field=f"{field}[{index}]")


def _canonical(value: Any) -> bytes:
    _reject_floats(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EnrollmentError("document cannot be encoded as canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentError(f"cannot read JSON {path.name}") from exc
    _reject_floats(value, field=path.name)
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EnrollmentError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnrollmentError(f"{field} must be a non-empty string")
    return value


def _sha(value: Any, field: str) -> str:
    value = _string(value, field)
    if not _SHA256.fullmatch(value):
        raise EnrollmentError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _safe_case_id(value: Any) -> str:
    value = _string(value, "probe case id")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise EnrollmentError("probe case id is not a safe artifact name")
    return value


def _declared_path(manifest_path: Path, value: Any, field: str) -> Path:
    raw = _string(value, field)
    candidate = Path(raw)
    if not candidate.is_absolute():
        if ".." in candidate.parts:
            raise EnrollmentError(f"{field} cannot traverse outside the manifest directory")
        candidate = manifest_path.parent / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise EnrollmentError(f"{field} is not a readable file")
    return candidate


def _reject_secrets(value: Any, *, field: str = "document") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise EnrollmentError(f"{field}.{key} is not permitted in enrollment input")
            _reject_secrets(child, field=f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, field=f"{field}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise EnrollmentError(f"{field} appears to contain a credential")


def _require_redacted_template_transcript(value: Any, *, field: str) -> None:
    """Reject proof inputs that would turn this package into prompt storage."""
    forbidden = {"prompt", "body", "headers", "messages", "token_ids", "tokens"}
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in forbidden:
                raise EnrollmentError(f"{field} must not persist {key}")
            _require_redacted_template_transcript(child, field=field)
    elif isinstance(value, list):
        for child in value:
            _require_redacted_template_transcript(child, field=field)


def _timestamp(value: Any, field: str) -> str:
    value = _string(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnrollmentError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise EnrollmentError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_exact_bounds(value: Any) -> None:
    if value is not None and value != NUMERIC_BOUNDS:
        raise EnrollmentError("numeric bounds must be the fixed 102400/8192/16384/320/77504 contract")


def _flag_value(argv: list[str], names: tuple[str, ...], field: str) -> str:
    for index, item in enumerate(argv):
        if item in names and index + 1 < len(argv):
            return argv[index + 1]
        for name in names:
            if item.startswith(name + "="):
                return item.removeprefix(name + "=")
    raise EnrollmentError(f"wrapper argv does not declare {field}")


def _load_tokenizer(manifest_path: Path, paths: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    transcript = _mapping(_read_json(_declared_path(manifest_path, paths.get("tokenizer_transcript"), "paths.tokenizer_transcript")), "tokenizer transcript")
    vocabulary = transcript.get("vocabulary")
    merges = transcript.get("merges")
    if not isinstance(vocabulary, list) or not vocabulary or not all(isinstance(piece, str) for piece in vocabulary):
        raise EnrollmentError("tokenizer transcript vocabulary must be a non-empty string array")
    if not isinstance(merges, list) or not all(isinstance(merge, str) for merge in merges):
        raise EnrollmentError("tokenizer transcript merges must be a string array")
    fallback = _mapping(transcript.get("byte_fallback"), "tokenizer transcript byte_fallback")
    expected_bytes = {f"{number:02x}" for number in range(256)}
    if set(fallback) != expected_bytes:
        raise EnrollmentError("tokenizer byte fallback must prove every byte from 00 through ff")
    indices: dict[str, int] = {}
    for byte, index in fallback.items():
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(vocabulary):
            raise EnrollmentError(f"tokenizer byte fallback index for {byte} is invalid")
        indices[byte] = index
    parser_version = _string(transcript.get("parser_version"), "tokenizer parser_version")
    tokenizer = {
        "architecture": _string(transcript.get("architecture"), "tokenizer architecture"),
        "tokenizer_model": _string(transcript.get("tokenizer_model"), "tokenizer model"),
        "pretokenizer": _string(transcript.get("pretokenizer"), "tokenizer pretokenizer"),
        "vocabulary_entries": len(vocabulary),
        "vocabulary_payload_sha256": _digest({"pieces": vocabulary}),
        "merge_entries": len(merges),
        "merge_payload_sha256": _digest({"merges": merges}),
        "byte_fallback_proof_digest": _digest({"parser_version": parser_version, "byte_indices": indices}),
    }
    proof = {
        "parser_version": parser_version,
        "vocabulary_entries": len(vocabulary),
        "vocabulary_payload_sha256": tokenizer["vocabulary_payload_sha256"],
        "merge_entries": len(merges),
        "merge_payload_sha256": tokenizer["merge_payload_sha256"],
        "byte_fallback": {"count": 256, "proof_digest": tokenizer["byte_fallback_proof_digest"]},
    }
    return tokenizer, proof


def _load_wrapper(manifest_path: Path, paths: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    wrapper = _mapping(_read_json(_declared_path(manifest_path, paths.get("wrapper_transcript"), "paths.wrapper_transcript")), "wrapper transcript")
    argv = wrapper.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise EnrollmentError("wrapper argv must be a non-empty string array")
    model = _declared_path(manifest_path, paths.get("model"), "paths.model")
    template = _declared_path(manifest_path, paths.get("template"), "paths.template")
    binary = _declared_path(manifest_path, paths.get("server_binary"), "paths.server_binary")
    if Path(_flag_value(argv, ("--model", "-m"), "--model")).resolve() != model:
        raise EnrollmentError("wrapper model path does not match declared model artifact")
    if Path(_flag_value(argv, ("--chat-template",), "--chat-template")).resolve() != template:
        raise EnrollmentError("wrapper template path does not match declared template artifact")
    runtime = _mapping(wrapper.get("runtime"), "wrapper runtime")
    clean = runtime.get("clean")
    if not isinstance(clean, bool):
        raise EnrollmentError("wrapper runtime clean must be boolean")
    reviewed = runtime.get("reviewed_deployment_revision")
    if not clean and (not isinstance(reviewed, str) or not reviewed):
        raise EnrollmentError("dirty runtime requires an explicit reviewed deployment revision")
    runtime_record = {
        "checkout_commit": _string(runtime.get("checkout_commit"), "wrapper runtime checkout_commit"),
        "clean": clean,
        "reviewed_deployment_revision": reviewed if isinstance(reviewed, str) else "",
        "server_binary_sha256": _file_digest(binary),
    }
    defaults = _mapping(wrapper.get("default_template_kwargs", {}), "wrapper default_template_kwargs")
    effective_kwargs = wrapper.get("effective_template_kwargs")
    if effective_kwargs != {**defaults, "enable_thinking": False}:
        raise EnrollmentError("wrapper effective_template_kwargs must exactly merge defaults with enable_thinking=false")
    unit_files = wrapper.get("unit_files", [])
    if not isinstance(unit_files, list) or not all(isinstance(item, str) for item in unit_files):
        raise EnrollmentError("wrapper unit_files must be a string array")
    unit_digests = []
    for item in unit_files:
        path = _declared_path(manifest_path, item, "wrapper unit file")
        unit_digests.append({"name": path.name, "sha256": _file_digest(path)})
    contract = {
        "server_binary_sha256": runtime_record["server_binary_sha256"],
        "unit_files": sorted(unit_digests, key=lambda item: item["name"]),
        "argv": argv,
        "default_template_kwargs": defaults,
    }
    return {
        "model_artifact_sha256": _file_digest(model),
        "template_sha256": _file_digest(template),
        "template_defaults": dict(defaults),
        "unit_wrapper_contract_digest": _digest(contract),
        "declared_runtime_revision": _digest(runtime_record),
        "server_binary_sha256": runtime_record["server_binary_sha256"],
        "argv": argv,
    }, {"runtime": runtime_record, "wrapper_contract_digest": _digest(contract)}


def _candidate_config(manifest: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    service_id = _string(manifest.get("service_id"), "service_id")
    base_model_id = _string(manifest.get("base_model_id"), "base_model_id")
    alias_id = _string(manifest.get("alias_id"), "alias_id")
    operation = _operation_overlay(manifest, evidence)
    return {"backends": {service_id: {
        "model_policy_schema_version": 1,
        "deployment_generation": evidence["deployment_generation"],
        "evidence_registry_revision": evidence["evidence_registry_revision"],
        "controlled_deployment_evidence": evidence,
        "models": [{"id": base_model_id}, {"id": alias_id, "base_model_id": base_model_id, **operation}],
    }}}


def _gateway_preparer(body: Mapping[str, Any], service_id: str, alias_id: str, runtime_config: Mapping[str, Any]) -> bytes:
    return prepare_operation_candidate_body(body, service_id, alias_id, runtime_config)


def _load_parity(manifest_path: Path, paths: Mapping[str, Any], manifest: Mapping[str, Any], evidence: Mapping[str, Any], *, preparer: Callable[[Mapping[str, Any], str, str, Mapping[str, Any]], bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    corpus = _read_json(_declared_path(manifest_path, paths.get("probe_corpus"), "paths.probe_corpus"))
    apply = _mapping(_read_json(_declared_path(manifest_path, paths.get("apply_template_transcript"), "paths.apply_template_transcript")), "apply-template transcript")
    local = _mapping(_read_json(_declared_path(manifest_path, paths.get("local_template_transcript"), "paths.local_template_transcript")), "local template transcript")
    _require_redacted_template_transcript(apply, field="apply-template transcript")
    _require_redacted_template_transcript(local, field="local template transcript")
    if not isinstance(corpus, list) or not corpus:
        raise EnrollmentError("probe corpus must be a non-empty array")
    apply_cases = _mapping(apply.get("cases"), "apply-template transcript cases")
    local_cases = _mapping(local.get("cases"), "local template transcript cases")
    if apply.get("available") is not True:
        raise EnrollmentError("apply-template parity endpoint is unavailable")
    server_digest = _file_digest(_declared_path(manifest_path, paths.get("server_binary"), "paths.server_binary"))
    if apply.get("server_binary_sha256") != server_digest or local.get("server_binary_sha256") != server_digest:
        raise EnrollmentError("template transcripts do not bind the declared server binary")
    config = _candidate_config(manifest, evidence)
    service_id = _string(manifest.get("service_id"), "service_id")
    alias_id = _string(manifest.get("alias_id"), "alias_id")
    proof_cases: list[dict[str, Any]] = []
    for item in corpus:
        item = _mapping(item, "probe corpus case")
        case_id = _safe_case_id(item.get("id"))
        body = _mapping(item.get("body"), f"probe corpus {case_id} body")
        admitted = preparer(body, service_id, alias_id, config)
        if not isinstance(admitted, bytes):
            raise EnrollmentError("gateway preparation seam did not return bytes")
        try:
            admitted_object = json.loads(admitted)
        except json.JSONDecodeError as exc:
            raise EnrollmentError("gateway preparation seam did not return JSON") from exc
        if set(admitted_object) != {"model", "messages", "max_tokens", "stream", "thinking_budget_tokens", "chat_template_kwargs"}:
            raise EnrollmentError("gateway preparation did not produce the exact final body shape")
        if {key: admitted_object.get(key) for key in FINAL_REASONING_CONTROL} != FINAL_REASONING_CONTROL:
            raise EnrollmentError("gateway preparation did not produce the exact final reasoning control")
        remote = _mapping(apply_cases.get(case_id), f"apply-template transcript case {case_id}")
        isolated = _mapping(local_cases.get(case_id), f"local template transcript case {case_id}")
        fields = ("prompt_sha256", "prompt_utf8_bytes", "token_ids_sha256", "token_count", "no_thinking_generation_prefix")
        for field in fields:
            if remote.get(field) != isolated.get(field):
                raise EnrollmentError(f"apply-template parity differs for {case_id}.{field}")
        if remote.get("no_thinking_generation_prefix") is not True:
            raise EnrollmentError(f"apply-template transcript does not prove no-thinking generation for {case_id}")
        _sha(remote.get("prompt_sha256"), f"apply-template {case_id} prompt_sha256")
        _sha(remote.get("token_ids_sha256"), f"apply-template {case_id} token_ids_sha256")
        if any(isinstance(remote.get(field), bool) or not isinstance(remote.get(field), int) or remote[field] < 0 for field in ("prompt_utf8_bytes", "token_count")):
            raise EnrollmentError(f"apply-template transcript counts are invalid for {case_id}")
        if remote["token_count"] > len(admitted) + NUMERIC_BOUNDS["max_template_overhead_tokens"]:
            raise EnrollmentError(f"rendered token bound exceeds final JSON bytes for {case_id}")
        proof_cases.append({"id": case_id, "admitted_body_sha256": hashlib.sha256(admitted).hexdigest(), "admitted_body_utf8_bytes": len(admitted), **{field: remote[field] for field in fields}})
    if set(apply_cases) != {item["id"] for item in proof_cases} or set(local_cases) != {item["id"] for item in proof_cases}:
        raise EnrollmentError("template transcripts must contain exactly the declared probe cases")
    proof = {"apply_template_path": "/apply-template", "cases": proof_cases}
    return proof, {"wrapper_server_request_preparation_digest": _digest({"gateway_preparation": proof_cases, "apply_template_path": "/apply-template"}), "apply_template_path": "/apply-template", "context_window_tokens": 102400}


def _operation_overlay(manifest: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    reference_fields = (
        "service_id", "deployment_generation", "evidence_registry_revision", "evidence_revision", "evidence_receipt_identity", "evidence_receipt_expires_at", "model_artifact_sha256", "base_model_id", "unit_wrapper_contract_digest", "declared_runtime_revision", "template_contract", "tokenizer_contract", "request_preparation_contract", "numeric_bounds", "final_reasoning_control",
    )
    return {
        "compaction_safety": {
            "schema_version": 1, "id": _string(manifest.get("operation_id"), "operation_id"), "purpose": "context_compaction", "base_model_id": _string(manifest.get("base_model_id"), "base_model_id"),
            "constraints": {"text_messages_only": True, "tools": "forbidden", "tool_calls": "forbidden", "vision_content": "forbidden", "reasoning_effort_default": "none", "current_loaded_only": True, "activation": "forbidden"},
            **NUMERIC_BOUNDS,
            "tokenizer_contract": evidence["tokenizer_contract"],
            "template_contract": evidence["template_contract"],
            "controlled_deployment_reference": {field: copy.deepcopy(evidence[field]) for field in reference_fields},
            "validation": {"evidence_revision": evidence["evidence_revision"], "validated_at": evidence["validated_at"], "expires_at": evidence["evidence_receipt_expires_at"], "validator_version": evidence["validator_version"]},
        }
    }


def _build(manifest_path: Path, *, preparer: Callable[[Mapping[str, Any], str, str, Mapping[str, Any]], bytes] = _gateway_preparer, now: float | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = _mapping(_read_json(manifest_path), "manifest")
    _reject_secrets(manifest)
    paths = _mapping(manifest.get("paths"), "manifest paths")
    for field in ("service_id", "base_model_id", "alias_id", "operation_id", "deployment_generation", "evidence_registry_revision", "evidence_revision", "evidence_receipt_identity", "validator_version"):
        _string(manifest.get(field), field)
    validated_at = _timestamp(manifest.get("validated_at"), "validated_at")
    expires_at = _timestamp(manifest.get("expires_at"), "expires_at")
    current = time.time() if now is None else now
    if datetime.fromisoformat(validated_at.replace("Z", "+00:00")).timestamp() > current or datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp() <= current or validated_at >= expires_at:
        raise EnrollmentError("evidence validation interval is expired or invalid")
    _require_exact_bounds(manifest.get("numeric_bounds"))
    wrapper, wrapper_proof = _load_wrapper(manifest_path, paths)
    tokenizer, tokenizer_proof = _load_tokenizer(manifest_path, paths)
    template_kwargs = {**wrapper["template_defaults"], "enable_thinking": False}
    evidence = {
        "service_id": manifest["service_id"], "deployment_generation": manifest["deployment_generation"], "evidence_registry_revision": manifest["evidence_registry_revision"], "evidence_revision": manifest["evidence_revision"], "evidence_receipt_identity": manifest["evidence_receipt_identity"], "expires_at": expires_at, "evidence_receipt_expires_at": expires_at,
        "model_artifact_sha256": wrapper["model_artifact_sha256"], "base_model_id": manifest["base_model_id"], "unit_wrapper_contract_digest": wrapper["unit_wrapper_contract_digest"], "declared_runtime_revision": wrapper["declared_runtime_revision"],
        "template_contract": {"revision": _string(manifest.get("template_revision"), "template_revision"), "sha256": wrapper["template_sha256"], "kwargs": template_kwargs, "render_bound_proof_digest": "0" * 64, "mode": "generation"},
        "tokenizer_contract": tokenizer, "request_preparation_contract": {"wrapper_server_request_preparation_digest": "0" * 64, "apply_template_path": "/apply-template", "context_window_tokens": 102400}, "numeric_bounds": dict(NUMERIC_BOUNDS), "final_reasoning_control": copy.deepcopy(FINAL_REASONING_CONTROL), "validated_at": validated_at, "validator_version": manifest["validator_version"],
    }
    parity_proof, preparation = _load_parity(manifest_path, paths, manifest, evidence, preparer=preparer)
    evidence["template_contract"]["render_bound_proof_digest"] = _digest(parity_proof)
    evidence["request_preparation_contract"] = preparation
    proofs = {"format": FORMAT, "artifacts": {"model_artifact_sha256": evidence["model_artifact_sha256"], "template_sha256": wrapper["template_sha256"], "server_binary_sha256": wrapper["server_binary_sha256"]}, "runtime": wrapper_proof, "tokenizer": tokenizer_proof, "preparation": parity_proof}
    _reject_secrets(evidence)
    return evidence, _operation_overlay(manifest, evidence), proofs


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value) + b"\n")


def collect(manifest_path: Path, output_dir: Path, *, preparer: Callable[[Mapping[str, Any], str, str, Mapping[str, Any]], bytes] = _gateway_preparer, now: float | None = None) -> dict[str, Any]:
    evidence, overlay, proofs = _build(manifest_path.resolve(), preparer=preparer, now=now)
    output = output_dir.resolve()
    _write_json(output / "evidence.json", evidence)
    _write_json(output / "overlay.json", overlay)
    _write_json(output / "proofs.json", proofs)
    return {"evidence_digest": _digest(evidence), "overlay_digest": _digest(overlay), "proof_digest": _digest(proofs)}


def verify(manifest_path: Path, output_dir: Path, *, preparer: Callable[[Mapping[str, Any], str, str, Mapping[str, Any]], bytes] = _gateway_preparer, now: float | None = None) -> dict[str, Any]:
    evidence, overlay, proofs = _build(manifest_path.resolve(), preparer=preparer, now=now)
    output = output_dir.resolve()
    expected = {"evidence.json": evidence, "overlay.json": overlay, "proofs.json": proofs}
    for name, value in expected.items():
        actual_path = output / name
        if not actual_path.is_file() or actual_path.read_bytes() != _canonical(value) + b"\n":
            raise EnrollmentError(f"{name} does not match recomputed enrollment evidence")
    return {"evidence_digest": _digest(evidence), "overlay_digest": _digest(overlay), "proof_digest": _digest(proofs)}


def package(manifest_path: Path, output_dir: Path, *, preparer: Callable[[Mapping[str, Any], str, str, Mapping[str, Any]], bytes] = _gateway_preparer, now: float | None = None) -> dict[str, Any]:
    digests = verify(manifest_path, output_dir, preparer=preparer, now=now)
    evidence = _read_json(output_dir.resolve() / "evidence.json")
    overlay = _read_json(output_dir.resolve() / "overlay.json")
    package_document = {"format": "gateway-operation-activation-package/v1", "claim": "operator assertion; not runtime attestation", "evidence": evidence, "evidence_digest": digests["evidence_digest"], "base_model_id": _string(_read_json(manifest_path.resolve()).get("base_model_id"), "base_model_id"), "overlay": overlay}
    _write_json(output_dir.resolve() / "activation-package.json", package_document)
    return {**digests, "package_digest": _digest(package_document)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("collect", "verify", "package"):
        child = subparsers.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True, help="explicit non-secret deployment manifest JSON")
        child.add_argument("--output", type=Path, required=True, help="explicit directory for redacted evidence artifacts")
    args = parser.parse_args(argv)
    try:
        result = {"collect": collect, "verify": verify, "package": package}[args.command](args.manifest, args.output)
    except EnrollmentError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
