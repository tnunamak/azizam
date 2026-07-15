from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import patch

from gateway import service_registry
from gateway.compaction_safety import AdmissionError, ContractError, contract_from_data, prepare_admitted_body
from gateway.policy import AppProfile, TargetPolicy
import proxy


_DEFAULT_EVIDENCE = object()


def _service_evidence(**overrides):
    evidence = {
        "service_id": "service",
        "deployment_generation": "g1",
        "evidence_revision": "receipt",
        "evidence_receipt_identity": "receipt-identity",
        "unit_wrapper_contract_digest": "f",
        "declared_runtime_revision": "g",
        "template_contract": {
            "revision": "r", "sha256": "d", "render_bound_proof_digest": "e",
        },
    }
    evidence.update(overrides)
    return evidence


def _record(**overrides):
    record = {
        "schema_version": 1,
        "id": "operator-compaction-target",
        "purpose": "context_compaction",
        "base_model_id": "base",
        "constraints": {
            "text_messages_only": True,
            "tools": "forbidden",
            "tool_calls": "forbidden",
            "vision_content": "forbidden",
            "reasoning_effort_default": "none",
            "current_loaded_only": True,
            "activation": "forbidden",
        },
        "context_window_tokens": 102400,
        "reserved_output_tokens": 8192,
        "reserved_headroom_tokens": 16384,
        "max_tokens_per_final_utf8_byte": 1,
        "max_template_overhead_tokens": 128,
        "max_final_body_utf8_bytes": 77696,
        "tokenizer_contract": {
            "architecture": "operator-declared", "tokenizer_model": "byte-bpe",
            "pretokenizer": "operator-declared", "vocabulary_entries": 256,
            "vocabulary_payload_sha256": "a", "merge_entries": 0,
            "merge_payload_sha256": "b", "byte_fallback_proof_digest": "c",
        },
        "template_contract": {"revision": "r", "sha256": "d", "kwargs": {}, "render_bound_proof_digest": "e"},
        "controlled_deployment_reference": {
            "service_id": "service", "deployment_generation": "g1",
            "evidence_revision": "receipt", "evidence_receipt_identity": "receipt-identity",
            "unit_wrapper_contract_digest": "f", "declared_runtime_revision": "g",
            "template_contract": {"revision": "r", "sha256": "d", "render_bound_proof_digest": "e"},
        },
        "validation": {"evidence_revision": "receipt", "validated_at": "2026-07-14T00:00:00Z", "expires_at": "2030-01-01T00:00:00Z", "validator_version": "v1"},
    }
    record.update(overrides)
    return record


def _contract(record=None, evidence=_DEFAULT_EVIDENCE):
    return contract_from_data(
        record or _record(),
        service_id="service",
        service_controlled_deployment_evidence=(
            _service_evidence() if evidence is _DEFAULT_EVIDENCE else evidence
        ),
        base_model_id="base",
        now=1_800_000_000,
    )


def _body(**overrides):
    body = {"model": "physical-model", "messages": [{"role": "system", "content": "summarize"}, {"role": "user", "content": "history"}]}
    body.update(overrides)
    return body


def test_record_recomputes_ceiling_and_rejects_lifecycle_mismatches():
    cases = [
        ("expired", {"validation": {**_record()["validation"], "expires_at": "1970-01-01T00:00:00Z"}}),
        ("missing deployment reference", {"controlled_deployment_reference": None}),
        ("missing evidence", {"validation": {**_record()["validation"], "evidence_revision": ""}}),
        ("derived ceiling", {"max_final_body_utf8_bytes": 77697}),
        ("boolean", {"max_tokens_per_final_utf8_byte": True}),
    ]
    for _, changes in cases:
        with pytest.raises(ContractError):
            _contract(_record(**changes))


@pytest.mark.parametrize(
    "path, changed_value",
    [
        (("deployment_generation",), "other-generation"),
        (("evidence_revision",), "other-evidence"),
        (("evidence_receipt_identity",), "other-receipt"),
        (("declared_runtime_revision",), "other-runtime"),
        (("unit_wrapper_contract_digest",), "other-wrapper"),
        (("template_contract", "revision"), "other-template-revision"),
        (("template_contract", "sha256"), "other-template-digest"),
        (("template_contract", "render_bound_proof_digest"), "other-template-proof"),
    ],
)
def test_operation_reference_must_match_separate_service_controlled_deployment_evidence(
    path, changed_value
):
    reference = _record()["controlled_deployment_reference"]
    changed = dict(reference)
    if len(path) == 1:
        changed[path[0]] = changed_value
    else:
        changed[path[0]] = dict(changed[path[0]])
        changed[path[0]][path[1]] = changed_value
    with pytest.raises(ContractError):
        _contract(_record(controlled_deployment_reference=changed))


def test_service_controlled_deployment_evidence_is_required_and_matching_succeeds():
    with pytest.raises(ContractError):
        _contract(evidence=None)
    with pytest.raises(ContractError):
        _contract(_record(validation={**_record()["validation"], "evidence_revision": "other"}))
    assert _contract().controlled_deployment_generation == "g1"


def _registry_with_operation(evidence=_DEFAULT_EVIDENCE, record=None):
    config = {
        "model_policy_schema_version": 1,
        "models": [
            {"id": "base"},
            {"id": "operator-target", "base_model_id": "base", "compaction_safety": record or _record()},
        ],
    }
    if evidence is not _DEFAULT_EVIDENCE:
        config["controlled_deployment_evidence"] = evidence
    return {"services": {"service": {"id": "service", "config": config}}}


def test_registry_normalization_preserves_separate_controlled_deployment_evidence():
    normalized = service_registry.normalize_service_registry(
        _registry_with_operation(_service_evidence())
    )
    assert normalized["services"]["service"]["config"]["controlled_deployment_evidence"] == _service_evidence()


@pytest.mark.parametrize(
    "evidence, record",
    [
        (_DEFAULT_EVIDENCE, None),
        (_service_evidence(evidence_receipt_identity="other-receipt"), None),
        (None, None),
    ],
)
def test_registry_normalization_rejects_missing_or_mismatched_controlled_deployment_evidence(
    evidence, record
):
    with pytest.raises(ValueError, match="invalid operation contract"):
        service_registry.normalize_service_registry(_registry_with_operation(evidence, record))


@pytest.mark.parametrize("cap", [0, 19, 8192])
def test_explicit_output_caps_are_preserved(cap):
    prepared, encoded = prepare_admitted_body(_body(max_tokens=cap), _contract())
    assert prepared["max_tokens"] == cap
    assert json.loads(encoded)["max_tokens"] == cap


@pytest.mark.parametrize("change", [
    {"reasoning_effort": "high"},
    {"max_tokens": 8193},
    {"tools": []},
    {"messages": [{"role": "system", "content": "x"}, {"role": "assistant", "content": "y"}]},
    {"messages": [{"role": "system", "content": [{"type": "text", "text": "x"}]}, {"role": "user", "content": "y"}]},
])
def test_incompatible_shapes_and_controls_fail_closed(change):
    with pytest.raises(AdmissionError):
        prepare_admitted_body(_body(**change), _contract())


def test_boundary_and_same_immutable_bytes_are_admitted_once():
    contract = _contract()
    body = _body()
    body["messages"][1]["content"] = ""
    fixed = len(json.dumps(body | {"reasoning_effort": "none", "max_tokens": 8192}, ensure_ascii=False, separators=(",", ":")).encode())
    body["messages"][1]["content"] = "x" * (contract.max_final_body_utf8_bytes - fixed)
    _, admitted = prepare_admitted_body(body, contract)
    assert len(admitted) == contract.max_final_body_utf8_bytes
    with pytest.raises(AdmissionError):
        prepare_admitted_body(_body(messages=[body["messages"][0], {"role": "user", "content": body["messages"][1]["content"] + "x"}]), contract)


def test_unicode_and_escaping_are_measured_as_final_utf8_not_prompt_text():
    body = _body(messages=[{"role": "system", "content": "\u0000\\\""}, {"role": "user", "content": "👩🏽‍💻 العربية e\u0301"}])
    _, encoded = prepare_admitted_body(body, _contract())
    assert json.loads(encoded) == body | {"reasoning_effort": "none", "max_tokens": 8192}


def test_resolved_target_selects_data_contract_without_alias_name_policy():
    config = {
        "backends": {
            "service": {
                "deployment_generation": "g1",
                "controlled_deployment_evidence": _service_evidence(),
                "models": [
                    {"id": "base"},
                    {"id": "any-operator-chosen-name", "base_model_id": "base", "compaction_safety": _record()},
                ],
            }
        }
    }
    with patch.object(proxy, "config", config):
        target = proxy._resolve_request_target("any-operator-chosen-name", required=True, aliases_only=True)
        assert target is not None and target.operation_contract is not None
        assert proxy._resolved_operation_requires_current_loaded("any-operator-chosen-name")
        error, prepared, exact_bytes = proxy._prepare_resolved_openai_request(
            _body(model="actual-rewritten-model"),
            request_path="/v1/chat/completions",
            target=target,
            execution_service_id="service",
        )
    assert error is None
    assert exact_bytes == json.dumps(prepared, ensure_ascii=False, separators=(",", ":")).encode()


def test_discovery_exposes_only_valid_authorized_operation_targets():
    expired = _record(validation={**_record()["validation"], "expires_at": "1970-01-01T00:00:00Z"})
    config = {
        "backends": {
            "service": {
                "deployment_generation": "g1",
                "controlled_deployment_evidence": _service_evidence(),
                "models": [
                    {"id": "base"},
                    {"id": "authorized", "base_model_id": "base", "compaction_safety": _record()},
                    {"id": "expired", "base_model_id": "base", "compaction_safety": expired},
                ],
            }
        }
    }
    with patch.object(proxy, "config", config):
        entries = proxy._operation_contract_model_entries()
    assert [entry["id"] for entry in entries] == ["authorized"]
    assert entries[0]["vivid_fish_operation"] == {
        "id": "operator-compaction-target",
        "purpose": "context_compaction",
        "guarantee": "conservative rendered-token bound; not exact token counting",
        "deployment_binding": "controlled registry generation; not runtime attestation",
        "max_final_body_utf8_bytes": 77696,
        "reserved_output_tokens": 8192,
    }


@pytest.mark.parametrize(
    "evidence, record",
    [
        (None, _record()),
        (
            _service_evidence(evidence_receipt_identity="other-receipt"),
            _record(),
        ),
    ],
)
def test_discovery_omits_operation_when_service_controlled_deployment_evidence_is_invalid(
    evidence, record
):
    config = {
        "backends": {
            "service": {
                "controlled_deployment_evidence": evidence,
                "models": [
                    {"id": "base"},
                    {"id": "operator-target", "base_model_id": "base", "compaction_safety": record},
                ],
            }
        }
    }
    with patch.object(proxy, "config", config):
        assert proxy._operation_contract_model_entries() == []


def test_operation_discovery_uses_the_shared_profile_model_allowlist():
    entry = {
        "id": "operator-target",
        "vivid_fish_operation": {"purpose": "context_compaction"},
    }
    allowed = AppProfile(id="allowed", models=frozenset({"operator-target"}))
    denied = AppProfile(id="denied", models=frozenset({"another-target"}))
    with patch.object(proxy, "_profile_for_auth_context", lambda _auth: allowed):
        visible, _ = asyncio.run(proxy._filter_model_list_for_profile([entry], proxy.NO_AUTH_CONTEXT))
    with patch.object(proxy, "_profile_for_auth_context", lambda _auth: denied):
        hidden, _ = asyncio.run(proxy._filter_model_list_for_profile([entry], proxy.NO_AUTH_CONTEXT))
    assert visible == [entry]
    assert hidden == []


@pytest.mark.parametrize("loaded_ids, expected", [(set(["physical"]), True), (set(), False)])
def test_current_loaded_operation_discovery_respects_available_profile_mode(loaded_ids, expected):
    entry = {
        "id": "operator-target",
        "vivid_fish_current_loaded": True,
        "vivid_fish_operation": {"purpose": "context_compaction"},
    }
    profile = AppProfile(
        id="available",
        models=frozenset({"operator-target"}),
        target_policy=TargetPolicy(visibility="available", activation="existing_only"),
    )

    async def loaded():
        return loaded_ids

    with patch.object(proxy, "_profile_for_auth_context", lambda _auth: profile), patch.object(
        proxy, "_current_loaded_llm_model_ids", loaded
    ):
        visible, _ = asyncio.run(proxy._filter_model_list_for_profile([entry], proxy.NO_AUTH_CONTEXT))
    assert bool(visible) is expected


def _pre_slice_prepare_oracle():
    source = subprocess.check_output(
        ["git", "show", "7b1b24f:proxy.py"], text=True, cwd=Path(proxy.__file__).parent
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_prepare_resolved_openai_request"
    )
    function.name = "_pre_slice_prepare_resolved_openai_request"
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = dict(vars(proxy))
    exec(compile(module, "pre_slice_proxy.py", "exec"), namespace)
    return namespace[function.name]


def test_ordinary_preparation_is_byte_identical_to_7b1b24f_with_transforms():
    oracle = _pre_slice_prepare_oracle()
    target = proxy.ResolvedRequestTarget(
        "ordinary", "service", "base",
        ({"kind": "default-params", "set": {"temperature": 0.3}},),
        ({"kind": "default-params", "set": {"top_p": 0.9}},),
    )
    for model in ("default", "current", "coder", "rp"):
        body = _body(model=model, max_tokens=19)
        old_error, old_body = oracle(
            body, request_path="/v1/chat/completions", target=target, execution_service_id=""
        )
        new_error, new_body, exact_bytes = proxy._prepare_resolved_openai_request(
            body, request_path="/v1/chat/completions", target=target, execution_service_id=""
        )
        assert (old_error.status_code if old_error else None) == (new_error.status_code if new_error else None)
        assert json.dumps(new_body).encode() == json.dumps(old_body).encode()
        assert exact_bytes is None


def test_current_loaded_transport_receives_the_admitted_bytes_without_reserializing():
    captured: list[bytes] = []
    admitted_inputs: list[dict] = []
    sentinel_body = _body(model="actual-rewritten-model") | {"reasoning_effort": "none", "max_tokens": 8192}
    admitted_sentinel = json.dumps(sentinel_body, ensure_ascii=False, separators=(",", ":")).encode()

    class Owner:
        def __init__(self, *_args, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        def report(self, **_kwargs): pass

    class Span:
        def __init__(self, *_args, **_kwargs): self.token = SimpleNamespace(cancelled=False)
        async def dispatch(self, execution_request):
            captured.append(execution_request.body)
            return SimpleNamespace(), SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
        async def read_body(self, *_args): return b'{"choices":[]}'
        async def aclose(self): pass

    async def current_target(*_args, **_kwargs):
        return proxy.CurrentLoadedLlmTarget("service", "http://unit.test", "actual-rewritten-model"), None

    async def close(*_args, **_kwargs): pass

    def admit(prepared, _contract):
        admitted_inputs.append(dict(prepared))
        return sentinel_body, admitted_sentinel

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    config = {"backends": {"service": {"model_policy_schema_version": 1, "deployment_generation": "g1", "controlled_deployment_evidence": _service_evidence(), "models": [{"id": "base"}, {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()}]}}}
    request = proxy.Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [], "query_string": b"", "scheme": "http", "server": ("test", 80)}, receive)
    with patch.object(proxy, "config", config), patch.object(proxy, "_current_loaded_llm_target", current_target), patch.object(proxy, "_OuterCancellationSpan", Span), patch.object(proxy, "ReceiptLifecycleOwner", Owner), patch.object(proxy, "_profile_for_auth_context", lambda _auth: proxy.DEFAULT_PROFILE), patch.object(proxy, "prepare_downstream_headers", lambda *_args: {}), patch.object(proxy, "close_llm_response", close), patch.object(proxy, "prepare_admitted_body", admit):
        response = asyncio.run(proxy._forward_current_loaded_openai_request(request, request_path="/v1/chat/completions", auth_context=SimpleNamespace(identity=None), body_json=_body(model="operator-target")))
    assert response.status_code == 200
    assert admitted_inputs[0]["model"] == "actual-rewritten-model"
    assert json.loads(admitted_sentinel)["model"] == "actual-rewritten-model"
    assert captured[0] is admitted_sentinel


@pytest.mark.parametrize("requested_model, expected_route", [
    ("default", "litellm"), ("current", "current-loaded"),
    ("coder", "litellm"), ("rp", "litellm"),
])
def test_ordinary_aliases_keep_their_top_level_dispatch_and_skip_operation_admission(
    requested_model, expected_route
):
    calls: list[tuple[str, dict]] = []
    body = _body(model=requested_model, max_tokens=19)
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def current_forward(_request, **kwargs):
        calls.append(("current-loaded", kwargs["body_json"]))
        return proxy.Response(status_code=200)

    async def litellm_forward(_request, **kwargs):
        calls.append(("litellm", kwargs["body_json"]))
        return proxy.Response(status_code=200)

    request = proxy.Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [], "query_string": b"", "scheme": "http", "server": ("test", 80)}, receive)
    auth = proxy.RuntimeRouteAuth(proxy.NO_AUTH_CONTEXT)
    models = [{"id": "base"}]
    models.extend({"id": model, "base_model_id": "base"} for model in ("default", "current", "coder", "rp"))
    config = {"backends": {"service": {"model_policy_schema_version": 1, "models": models}}}
    with patch.object(proxy, "config", config), patch.object(proxy, "authenticate_runtime_or_litellm_request", return_value=auth), patch.object(proxy, "_openai_proxy_policy_decision", return_value=None), patch.object(proxy, "_litellm_runtime_config", return_value=SimpleNamespace(enabled=False)), patch.object(proxy, "_forward_current_loaded_openai_request", current_forward), patch.object(proxy, "_forward_litellm_proxy_request", litellm_forward), patch.object(proxy, "prepare_admitted_body", side_effect=AssertionError("ordinary request invoked operation admission")):
        response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
    assert response.status_code == 200
    assert calls == [(expected_route, body)]
