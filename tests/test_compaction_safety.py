from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from urllib.parse import urlsplit
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient
from unittest.mock import patch

from gateway import service_registry
from gateway.compaction_safety import (
    AdmissionError,
    ContractError,
    contract_from_data,
    prepare_admitted_body,
)
from gateway.policy import AppProfile, TargetPolicy
import proxy


class _InProcessLoopbackClient:
    """Run the Responses loopback through the same ASGI app without a port."""

    def __init__(self, **_kwargs):
        pass

    async def post(self, url, *, headers, content):
        inner = TestClient(proxy.app, client=("127.0.0.1", 48123))
        response = inner.post(urlsplit(url).path, headers=headers, content=content)
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": response.headers.get("content-type", "application/json")},
                              request=httpx.Request("POST", url))

    async def aclose(self):
        return None


_DEFAULT_EVIDENCE = object()
_DIGEST = "a" * 64


def _service_evidence(**overrides):
    tokenizer = {
        "architecture": "operator-declared", "tokenizer_model": "byte-bpe",
        "pretokenizer": "operator-declared", "vocabulary_entries": 256,
        "vocabulary_payload_sha256": _DIGEST, "merge_entries": 0,
        "merge_payload_sha256": _DIGEST, "byte_fallback_proof_digest": _DIGEST,
    }
    template = {
        "revision": "r", "sha256": _DIGEST, "kwargs": {"enable_thinking": False},
        "render_bound_proof_digest": _DIGEST, "mode": "generation",
    }
    bounds = {
        "context_window_tokens": 102400, "reserved_output_tokens": 8192,
        "reserved_headroom_tokens": 16384, "max_tokens_per_final_utf8_byte": 1,
        "max_template_overhead_tokens": 320, "max_final_body_utf8_bytes": 77504,
    }
    evidence = {
        "service_id": "service",
        "deployment_generation": "g1",
        "registry_generation": "registry-g1",
        "evidence_revision": "receipt",
        "evidence_receipt_identity": "receipt-identity",
        "evidence_receipt_expires_at": "2030-01-01T00:00:00Z",
        "model_artifact_sha256": _DIGEST,
        "base_model_id": "base",
        "unit_wrapper_contract_digest": _DIGEST,
        "declared_runtime_revision": "g",
        "template_contract": template,
        "tokenizer_contract": tokenizer,
        "request_preparation_contract": {
            "wrapper_server_request_preparation_digest": _DIGEST,
            "apply_template_path": "declared-path", "context_window_tokens": 102400,
        },
        "numeric_bounds": bounds,
        "final_reasoning_control": {
            "thinking_budget_tokens": 0, "chat_template_kwargs": {"enable_thinking": False},
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
        "max_template_overhead_tokens": 320,
        "max_final_body_utf8_bytes": 77504,
        "tokenizer_contract": {
            "architecture": "operator-declared", "tokenizer_model": "byte-bpe",
            "pretokenizer": "operator-declared", "vocabulary_entries": 256,
            "vocabulary_payload_sha256": _DIGEST, "merge_entries": 0,
            "merge_payload_sha256": _DIGEST, "byte_fallback_proof_digest": _DIGEST,
        },
        "template_contract": {"revision": "r", "sha256": _DIGEST, "kwargs": {"enable_thinking": False}, "render_bound_proof_digest": _DIGEST, "mode": "generation"},
        "controlled_deployment_reference": {
            "service_id": "service", "deployment_generation": "g1",
            "evidence_revision": "receipt", "evidence_receipt_identity": "receipt-identity",
            "evidence_receipt_expires_at": "2030-01-01T00:00:00Z",
            "registry_generation": "registry-g1", "model_artifact_sha256": _DIGEST, "base_model_id": "base",
            "unit_wrapper_contract_digest": _DIGEST, "declared_runtime_revision": "g",
            "template_contract": {"revision": "r", "sha256": _DIGEST, "kwargs": {"enable_thinking": False}, "render_bound_proof_digest": _DIGEST, "mode": "generation"},
            "tokenizer_contract": _service_evidence()["tokenizer_contract"],
            "request_preparation_contract": _service_evidence()["request_preparation_contract"],
            "numeric_bounds": _service_evidence()["numeric_bounds"],
            "final_reasoning_control": _service_evidence()["final_reasoning_control"],
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
        service_deployment_generation="g1",
        service_registry_generation="registry-g1",
        base_model_id="base",
        now=1_800_000_000,
    )


def _body(**overrides):
    body = {"model": "physical-model", "messages": [{"role": "system", "content": "summarize"}, {"role": "user", "content": "history"}], "thinking_budget_tokens": 0, "chat_template_kwargs": {"enable_thinking": False}}
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
        (("registry_generation",), "other-registry-generation"),
        (("evidence_revision",), "other-evidence"),
        (("evidence_receipt_identity",), "other-receipt"),
        (("evidence_receipt_expires_at",), "2031-01-01T00:00:00Z"),
        (("model_artifact_sha256",), "other-model-artifact"),
        (("declared_runtime_revision",), "other-runtime"),
        (("unit_wrapper_contract_digest",), "other-wrapper"),
        (("template_contract", "revision"), "other-template-revision"),
        (("template_contract", "sha256"), "other-template-digest"),
        (("template_contract", "render_bound_proof_digest"), "other-template-proof"),
        (("template_contract", "mode"), "other-template-mode"),
        (("tokenizer_contract", "architecture"), "other-architecture"),
        (("tokenizer_contract", "vocabulary_payload_sha256"), "other-vocab"),
        (("tokenizer_contract", "merge_payload_sha256"), "other-merge"),
        (("tokenizer_contract", "pretokenizer"), "other-pretokenizer"),
        (("tokenizer_contract", "byte_fallback_proof_digest"), "other-byte-proof"),
        (("request_preparation_contract", "wrapper_server_request_preparation_digest"), "other-wrapper-path"),
        (("request_preparation_contract", "apply_template_path"), "other-apply-template"),
        (("request_preparation_contract", "context_window_tokens"), 999),
        (("numeric_bounds", "reserved_output_tokens"), 999),
        (("final_reasoning_control", "thinking_budget_tokens"), 1),
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


@pytest.mark.parametrize("path", [
    ("model_artifact_sha256",), ("unit_wrapper_contract_digest",),
    ("tokenizer_contract", "vocabulary_payload_sha256"),
    ("tokenizer_contract", "merge_payload_sha256"),
    ("tokenizer_contract", "byte_fallback_proof_digest"),
    ("template_contract", "sha256"), ("template_contract", "render_bound_proof_digest"),
    ("request_preparation_contract", "wrapper_server_request_preparation_digest"),
])
def test_malformed_matching_digest_is_rejected_before_comparison(path):
    evidence = _service_evidence()
    reference = _record()["controlled_deployment_reference"]
    for record in (evidence, reference):
        target = record
        for field in path[:-1]:
            target = target[field]
        target[path[-1]] = "not-a-digest"
    with pytest.raises(ContractError):
        _contract(_record(controlled_deployment_reference=reference), evidence)


def _registry_with_operation(evidence=_DEFAULT_EVIDENCE, record=None):
    config = {
        "model_policy_schema_version": 1,
        "deployment_generation": "g1",
        "registry_generation": "registry-g1",
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


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_output_cap_aliases_normalize_to_one_canonical_final_field(field):
    prepared, encoded = prepare_admitted_body(_body(**{field: 19}), _contract())
    assert prepared["max_tokens"] == 19
    assert set(prepared).issuperset({"model", "messages", "max_tokens", "thinking_budget_tokens"})
    assert all(alias not in prepared for alias in ("max_completion_tokens", "max_output_tokens"))
    assert json.loads(encoded) == prepared


def test_output_cap_is_normalized_after_generic_request_transforms():
    target = proxy.ResolvedRequestTarget(
        "operator-target", "service", "base",
        ({"kind": "default-params", "set": {"max_output_tokens": 19}},), (),
        operation_contract=_contract(),
    )
    error, prepared, encoded = proxy._prepare_resolved_openai_request(
        _body(model="physical-model"), request_path="/v1/chat/completions",
        target=target, execution_service_id="",
    )
    assert error is None
    assert prepared["max_tokens"] == 19
    assert "max_output_tokens" not in prepared
    assert prepared["thinking_budget_tokens"] == 0
    assert prepared["chat_template_kwargs"] == {"enable_thinking": False}
    assert json.loads(encoded) == prepared


@pytest.mark.parametrize("change", [
    {"max_tokens": 0, "max_output_tokens": 19},
    {"max_tokens": True}, {"max_output_tokens": -1}, {"max_completion_tokens": 8193},
    {"chat_template_kwargs": {}}, {"tools": []}, {"response_format": {"type": "json_object"}},
    {"modalities": ["text"]}, {"stream": True},
    {"messages": [{"role": "system", "content": "x", "name": "extra"}, {"role": "user", "content": "y"}]},
])
def test_final_body_schema_and_output_cap_admission_fail_closed(change):
    with pytest.raises(AdmissionError):
        prepare_admitted_body(_body(**change), _contract())


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
    fixed = len(json.dumps(body | {"max_tokens": 8192}, ensure_ascii=False, separators=(",", ":")).encode())
    body["messages"][1]["content"] = "x" * (contract.max_final_body_utf8_bytes - fixed)
    _, admitted = prepare_admitted_body(body, contract)
    assert len(admitted) == contract.max_final_body_utf8_bytes
    with pytest.raises(AdmissionError):
        prepare_admitted_body(_body(messages=[body["messages"][0], {"role": "user", "content": body["messages"][1]["content"] + "x"}]), contract)


def test_unicode_and_escaping_are_measured_as_final_utf8_not_prompt_text():
    body = _body(messages=[{"role": "system", "content": "\u0000\\\""}, {"role": "user", "content": "👩🏽‍💻 العربية e\u0301"}])
    _, encoded = prepare_admitted_body(body, _contract())
    assert json.loads(encoded) == body | {"max_tokens": 8192}


def test_resolved_target_selects_data_contract_without_alias_name_policy():
    config = {
        "backends": {
            "service": {
                "deployment_generation": "g1",
                "registry_generation": "registry-g1",
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
                "registry_generation": "registry-g1",
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
        "request_construction": {
            "version": 1,
            "model": "authorized",
            "messages": [
                {"role": "system", "content": "scalar_text"},
                {"role": "user", "content": "scalar_text"},
            ],
            "stream": False,
            "output_limit": {"field": "max_tokens", "default": 8192, "maximum": 8192},
            "max_final_body_utf8_bytes": 77504,
            "token_counting": "conservative_not_exact",
        },
    }


def test_authenticated_discovery_consumer_needs_no_internal_control_and_reaches_admitted_transport(tmp_path):
    config = {"backends": {"service": {
        "model_policy_schema_version": 1, "deployment_generation": "g1", "registry_generation": "registry-g1",
        "controlled_deployment_evidence": _service_evidence(),
        "models": [{"id": "base"}, {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()}],
    }}}
    profile = AppProfile(id="authorized", models=frozenset({"operator-target"}))
    auth = proxy.RuntimeRouteAuth(proxy.NO_AUTH_CONTEXT)
    transported: list[bytes] = []
    admitted: list[tuple[dict, bytes]] = []

    class Span:
        def __init__(self, *_args, **_kwargs):
            self.token = SimpleNamespace(cancelled=False)

        async def dispatch(self, execution_request):
            transported.append(execution_request.body)
            class Client:
                async def aclose(self):
                    return None

            class Response:
                status_code = 200
                headers = {"content-type": "application/json"}

                async def aclose(self):
                    return None

            return Client(), Response()

        async def read_body(self, *_args):
            return b'{"id":"chat","object":"chat.completion","model":"physical-model","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'

        async def aclose(self):
            return None

    async def current_target(*_args, **_kwargs):
        return proxy.CurrentLoadedLlmTarget("service", "http://unit.test", "physical-model"), None

    real_admit = proxy.prepare_admitted_body

    def counted_admit(prepared, contract):
        admitted_body, admitted_bytes = real_admit(prepared, contract)
        admitted.append((admitted_body, admitted_bytes))
        return admitted_body, admitted_bytes

    real_begin, real_finalize = proxy.receipt_store.begin, proxy.receipt_store.finalize
    proxy.receipt_store.init_db(tmp_path / "receipts.db")
    with patch.object(proxy, "config", config), patch.object(
        proxy, "authenticate_runtime_or_litellm_request", return_value=auth
    ), patch.object(proxy, "_profile_for_auth_context", lambda _auth: profile), patch.object(
        proxy, "_current_loaded_llm_target", current_target
    ), patch.object(proxy, "_OuterCancellationSpan", Span), patch.object(
        proxy, "prepare_admitted_body", counted_admit
    ), patch.object(
        proxy.receipt_store, "begin", lambda receipt: real_begin(receipt, path=tmp_path / "receipts.db")
    ), patch.object(
        proxy.receipt_store, "finalize", lambda receipt: real_finalize(receipt, path=tmp_path / "receipts.db")
    ):
        client = TestClient(proxy.app)
        discovery = client.get("/v1/models")
        assert discovery.status_code == 200, discovery.text
        entry = next(item for item in discovery.json()["data"] if item["id"] == "operator-target")
        projection = entry["vivid_fish_operation"]["request_construction"]
        assert set(projection) == {
            "version", "model", "messages", "stream", "output_limit",
            "max_final_body_utf8_bytes", "token_counting",
        }
        body = {
            "model": projection["model"],
            "messages": [
                {"role": item["role"], "content": content}
                for item, content in zip(projection["messages"], ("summarize", "history"))
            ],
            projection["output_limit"]["field"]: projection["output_limit"]["default"],
            "stream": projection["stream"],
        }
        assert "final_reasoning_control" not in projection
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200, response.text
        expected = {
            "model": "physical-model",
            "messages": body["messages"],
            "stream": False,
            "thinking_budget_tokens": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 8192,
        }
        expected_bytes = json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
        assert admitted == [(expected, expected_bytes)]
        assert len(admitted) == 1
        assert transported == [expected_bytes]
        assert transported[0] is admitted[0][1]
        assert projection["token_counting"] == "conservative_not_exact"
        assert projection["output_limit"] == {
            "field": "max_tokens", "default": 8192, "maximum": 8192,
        }


def test_duplicate_operation_targets_fail_closed_for_discovery_and_admission():
    config = {"backends": {
        "first": {"deployment_generation": "g1", "registry_generation": "registry-g1", "controlled_deployment_evidence": _service_evidence(service_id="first"), "models": []},
        "second": {"deployment_generation": "g1", "registry_generation": "registry-g1", "controlled_deployment_evidence": _service_evidence(service_id="second"), "models": []},
    }}
    for service_id in ("first", "second"):
        evidence = config["backends"][service_id]["controlled_deployment_evidence"]
        record = _record()
        reference = dict(record["controlled_deployment_reference"])
        reference["service_id"] = service_id
        record["controlled_deployment_reference"] = reference
        config["backends"][service_id]["models"] = [
            {"id": "base"}, {"id": "duplicate", "base_model_id": "base", "compaction_safety": record},
        ]
    with patch.object(proxy, "config", config):
        assert proxy._operation_contract_model_entries() == []
        with pytest.raises(proxy.AliasPolicyResolutionError, match="ambiguous"):
            proxy._resolve_request_target("duplicate", required=True, aliases_only=True)


def test_ordinary_aliases_never_project_or_activate_an_operation_contract():
    config = {"backends": {"service": {"model_policy_schema_version": 1, "models": [
        {"id": "base"}, {"id": "ordinary", "base_model_id": "base"},
    ]}}}
    with patch.object(proxy, "config", config):
        assert proxy._operation_contract_model_entries() == []
        target = proxy._resolve_request_target("ordinary", required=True, aliases_only=True)
    assert target is not None and target.operation_contract is None


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


def test_named_litellm_refuses_operation_before_receipt_or_activation():
    request = SimpleNamespace(method="POST")
    auth = proxy.NO_AUTH_CONTEXT
    runtime = SimpleNamespace(enabled=True)
    config = {
        "backends": {
            "service": {
                "deployment_generation": "g1", "registry_generation": "registry-g1",
                "controlled_deployment_evidence": _service_evidence(),
                "models": [
                    {"id": "base"},
                    {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()},
                ],
            }
        }
    }
    with patch.object(proxy, "config", config), patch.object(
        proxy, "_start_litellm_route_receipt", side_effect=AssertionError("receipt created")
    ):
        response = asyncio.run(proxy._forward_litellm_proxy_request(
            request, request_path="/v1/chat/completions", auth_context=auth,
            body_json=_body(model="operator-target"), runtime_config=runtime,
        ))
    assert response.status_code == 422


def test_responses_loopback_routes_logical_target_to_one_owned_admission(tmp_path):
    admissions: list[bytes] = []
    config = {"backends": {"service": {
        "model_policy_schema_version": 1, "deployment_generation": "g1", "registry_generation": "registry-g1",
        "controlled_deployment_evidence": _service_evidence(),
        "models": [{"id": "base"}, {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()}],
    }}}

    transported=[]
    class Span:
        def __init__(self,*a,**k): self.token=SimpleNamespace(cancelled=False)
        async def dispatch(self, req):
            transported.append(req.body)
            class Client:
                async def aclose(self): pass
            class Response:
                status_code=200
                headers={"content-type":"application/json"}
                async def aclose(self): pass
            return Client(), Response()
        async def read_body(self,*a): return b'{"id":"chat","object":"chat.completion","model":"physical-model","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'
        async def aclose(self): pass
        async def stop_watcher_for_streaming_handoff(self): pass
    async def current_target(*a,**k): return proxy.CurrentLoadedLlmTarget("service","http://unit.test","physical-model"),None
    real=proxy.prepare_admitted_body
    real_begin, real_finalize = proxy.receipt_store.begin, proxy.receipt_store.finalize
    proxy.receipt_store.init_db(tmp_path / "receipts.db")
    def counted(body,contract):
        result=real(body,contract); admissions.append(result[1]); return result

    with patch.object(proxy, "config", config), patch.object(proxy, "AUTH_ENABLED", False), patch.object(
        proxy.httpx, "AsyncClient", _InProcessLoopbackClient
    ), patch.object(proxy,"_current_loaded_llm_target",current_target),patch.object(proxy,"_OuterCancellationSpan",Span),patch.object(proxy,"prepare_admitted_body",counted),patch.object(proxy.receipt_store,"begin",lambda receipt: real_begin(receipt,path=tmp_path/"receipts.db")),patch.object(proxy.receipt_store,"finalize",lambda receipt: real_finalize(receipt,path=tmp_path/"receipts.db")):
        response = TestClient(proxy.app).post("/v1/responses", json={
            "model": "operator-target", "instructions": "summarize", "input": "history", "store": False,
        })
    assert response.status_code == 200, response.text
    assert len(admissions) == 1
    assert transported[0] is admissions[0]
    rows = proxy.receipt_store.recent(path=tmp_path/"receipts.db", limit=10, outer_only=False)
    assert len(rows) == 2
    outer = next(row for row in rows if row["protocol"] == "openai.responses")
    child = next(row for row in rows if row["protocol"] == "openai.chat")
    child_detail = proxy.receipt_store.get(child["request_id"], audience="internal", path=tmp_path/"receipts.db")
    assert child_detail["parent_request_id"] == outer["request_id"] and child_detail["status"] == "succeeded"


def test_runtime_reload_drops_stale_service_evidence_before_resolution():
    previous = dict(proxy.config)
    previous_derived = {name: getattr(proxy, name) for name in proxy._derive_backend_constants(previous)}
    stale = {"backends": {"service": {
        "deployment_generation": "g1", "registry_generation": "registry-g1",
        "controlled_deployment_evidence": _service_evidence(evidence_revision="stale"),
        "models": [{"id": "base"}, {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()}],
    }}}
    try:
        with patch.object(proxy, "config", {"backends": {"service": {
            "deployment_generation": "g1", "registry_generation": "registry-g1",
            "controlled_deployment_evidence": _service_evidence(),
            "models": [{"id": "base"}, {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()}],
        }}}):
            assert [entry["id"] for entry in proxy._operation_contract_model_entries()] == ["operator-target"]
        with patch.object(proxy, "_load_effective_runtime_config", return_value=stale):
            proxy.reload_runtime_config()
        assert proxy._operation_contract_model_entries() == []
        with pytest.raises(proxy.AliasPolicyResolutionError):
            proxy._resolve_request_target("operator-target", required=True, aliases_only=True)
    finally:
        proxy.config.clear()
        proxy.config.update(previous)
        for name, value in previous_derived.items():
            setattr(proxy, name, value)


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

    config = {"backends": {"service": {"model_policy_schema_version": 1, "deployment_generation": "g1", "registry_generation": "registry-g1", "controlled_deployment_evidence": _service_evidence(), "models": [{"id": "base"}, {"id": "operator-target", "base_model_id": "base", "compaction_safety": _record()}]}}}
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
