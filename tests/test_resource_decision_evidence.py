"""Regression evidence for missing observations and complete admission reasons."""
import json

from gateway.resource_admission import (
    AdmissionConstraints, CurrentLoad, ResourceEstimate, ServiceAvailability,
    advise_resource_admission,
)
from gateway.resource_claims import (
    AdmissionRequest, CapacitySnapshot, QueueClass, ResourceClaim, evaluate_admission,
    summarize_decision,
)


def test_unknown_availability_is_not_available():
    decision = advise_resource_admission(ServiceAvailability(), ResourceEstimate("chat.generate"))
    assert decision.denied


def test_missing_queue_load_is_not_measured_empty():
    args = (ServiceAvailability(available=True), ResourceEstimate("image.generate", queue_key="media"),
            AdmissionConstraints(max_queue_depth=1))
    assert advise_resource_admission(*args).denied
    assert advise_resource_admission(*args, CurrentLoad(queue_depths={"media": 0})).allowed


def test_missing_used_capacity_is_not_measured_empty():
    request = AdmissionRequest("req", ResourceClaim(vram_mb=1))
    assert not evaluate_admission(request, CapacitySnapshot(total_vram_mb=10)).accepted
    assert evaluate_admission(request, CapacitySnapshot(total_vram_mb=10, used_vram_mb=0)).accepted


def test_missing_profile_load_is_not_measured_empty():
    request = AdmissionRequest("req", ResourceClaim(profile_id="private"))
    assert not evaluate_admission(request, CapacitySnapshot(profile_limits={"private": 1})).accepted
    assert evaluate_admission(request, CapacitySnapshot(profile_limits={"private": 1},
                                                        active_by_profile={"private": 0})).accepted


def test_all_claim_blockers_survive_rejection():
    decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(vram_mb=20)),
                                 CapacitySnapshot(total_vram_mb=10, used_vram_mb=0,
                                                  queue_limits={QueueClass.INTERACTIVE: 1},
                                                  active_by_queue={QueueClass.INTERACTIVE: 1}))
    assert {reason.effect for reason in decision.details.reasons} == {"rejection", "delay"}
    assert {reason.resource_class for reason in decision.details.reasons} == {"queue", "vram"}


def test_route_degradation_survives_denial():
    decision = advise_resource_admission(ServiceAvailability(available=True, degraded=True),
                                         ResourceEstimate("image.generate"),
                                         AdmissionConstraints(disabled_intents=("image.generate",)))
    assert {reason.effect for reason in decision.details.reasons} == {"rejection", "delay"}


def test_known_legacy_route_serialization():
    decision = advise_resource_admission(ServiceAvailability(available=False, reasons=("backend down",)),
                                         ResourceEstimate("chat.generate"))
    assert json.dumps(decision.as_dict()) == ('{"outcome": "denied", "allowed": false, '
        '"retry_after": null, "reasons": ["backend down"], "reason_codes": ["service_unavailable"]}')


def test_known_legacy_claim_serialization():
    decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(vram_mb=20)),
                                 CapacitySnapshot(total_vram_mb=10, used_vram_mb=0))
    assert json.dumps(summarize_decision(decision)) == ('{"request_ref": "c3f7bdf537c4", '
        '"status": "delayed", "accepted": false, '
        '"reasons": ["not enough vram_mb: requested 20, available 10"], "caveats": []}')


import pytest
from gateway.resource_decisions import project_decision
from gateway.resource_admission_http import admission_http_response
from gateway.receipts import RouteReceipt, serialize_receipt_for_audience
from gateway import receipt_store


@pytest.mark.parametrize("name", ["vram_mb", "cpu_cores", "ram_mb", "disk_mb"])
def test_each_capacity_preserves_unknown_and_measured_zero(name):
    request = AdmissionRequest("req", ResourceClaim(**{name: 1}))
    missing = evaluate_admission(request, CapacitySnapshot(**{f"total_{name}": 10}, observed_at=123,
                                                            quality="measured"))
    assert not missing.accepted
    facts = {f.name: f for f in missing.details.facts}
    assert facts[f"used_{name}"].value is None
    assert facts[f"used_{name}"].quality == "unavailable"
    assert facts[f"available_{name}"].value is None
    empty = evaluate_admission(request, CapacitySnapshot(**{f"total_{name}": 10, f"used_{name}": 0},
                                                          observed_at=123, quality="measured"))
    assert empty.accepted
    facts = {f.name: f for f in empty.details.facts}
    assert facts[f"available_{name}"].value == 10
    assert facts[f"available_{name}"].quality == "measured"
    assert facts[f"available_{name}"].observed_at == 123


def test_unavailable_quality_cannot_admit_even_with_a_numeric_value():
    request = AdmissionRequest("req", ResourceClaim(vram_mb=1))
    assert not evaluate_admission(request, CapacitySnapshot(total_vram_mb=10, used_vram_mb=0,
                                                            quality="unavailable")).accepted
    assert advise_resource_admission(ServiceAvailability(available=True, quality="unavailable"),
                                     ResourceEstimate("chat.generate")).denied


def test_unknown_locks_differ_from_empty_and_conflicts_are_retained():
    request = AdmissionRequest("req", ResourceClaim(exclusive_locks=("another-person/model",)))
    assert not evaluate_admission(request, CapacitySnapshot()).accepted
    assert evaluate_admission(request, CapacitySnapshot(active_exclusive_locks=frozenset())).accepted
    decision = evaluate_admission(request, CapacitySnapshot(active_exclusive_locks=frozenset({"another-person/model"})))
    facts = {f.name: f.value for f in decision.details.facts}
    assert facts["conflicting_locks"] == ["another-person/model"]


@pytest.mark.parametrize("audience", ["client", "member", "app_key", "mcp", "unexpected"])
def test_caller_projection_has_no_private_prose_identities_or_quantities(audience):
    decision = advise_resource_admission(
        ServiceAvailability(available=False, reasons=("another-person has 99999 MB",), observed_at=123),
        ResourceEstimate("image.generate", queue_key="another-person/queue"),
        AdmissionConstraints(max_queue_depth=99999),
        CurrentLoad(queue_depths={"another-person/queue": 99999}, observed_at=123, quality="measured"))
    response = admission_http_response(decision, audience=audience, wait_deadline=456)
    text = json.dumps(response.body)
    assert "another-person" not in text and "99999" not in text and "123" not in text
    public = response.body["error"]["decision"]
    assert public["next_actions"][0]["deadline"] == 456
    assert "facts" not in public
    evidence = decision.details.as_dict(decision.outcome.value)
    owner = project_decision(evidence, "admin")
    assert "another-person" in json.dumps(owner)
    assert any(f["value"] == 99999 for f in owner["facts"])


def test_only_authorized_actions_are_offered_and_paid_requires_consent():
    decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(disk_mb=10)), CapacitySnapshot())
    value = decision.details.as_dict(decision.status.value)
    assert project_decision(value)["next_actions"] == []
    value.update(wait_deadline=456, can_cancel_own_work=True, permitted_options=["permitted-small"],
                 authorized_paid_options=["authorized-paid"])
    actions = project_decision(value)["next_actions"]
    assert [a["action"] for a in actions] == ["wait", "cancel_own_work", "use_option", "opt_in_paid_route"]
    assert actions[-1]["requires_explicit_consent"] is True
    assert "unload" not in json.dumps(actions)


def test_receipt_roundtrip_preserves_owner_facts_and_redacts_client(tmp_path):
    decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(vram_mb=20)),
                                 CapacitySnapshot(total_vram_mb=10, used_vram_mb=0, observed_at=123))
    receipt = RouteReceipt(protocol="test", intent="image.generate", app_id="mine")
    receipt.resource_decision = decision.details.as_dict(decision.status.value)
    receipt.resource_decision["wait_deadline"] = 456
    receipt_store.init_db(tmp_path / "receipts.db")
    receipt_store.record(receipt, path=tmp_path / "receipts.db")
    admin = receipt_store.get(receipt.request_id, path=tmp_path / "receipts.db", audience="admin")
    client = receipt_store.get(receipt.request_id, path=tmp_path / "receipts.db", audience="client")
    assert admin["resource_decision"]["facts"][0]["value"] == 20
    assert "facts" not in client["resource_decision"]
    assert "20" not in json.dumps(client["resource_decision"])
    assert client["resource_decision"]["next_actions"][0]["deadline"] == 456
    assert "resource_decision" not in RouteReceipt(protocol="test", intent="test").to_dict()
    assert "resource_decision" not in serialize_receipt_for_audience({"request_id": "legacy"}, "client")


def test_queue_read_failure_is_unknown_and_refused_without_dispatch():
    import proxy
    from unittest.mock import patch
    with patch.object(proxy, "COMFY_ENABLED", True), \
         patch.object(proxy, "_operator_service_availability_overrides", return_value={}), \
         patch.object(proxy, "_effective_media_job_admission_cfg", return_value={"max_queue_depth": 1}), \
         patch.object(proxy.media_jobs, "recent", side_effect=RuntimeError("private database detail")):
        receipt = RouteReceipt(protocol="test", intent="image.generate")
        response = proxy._media_job_admission_response("image.generate", receipt=receipt)
    assert response.status_code == 503
    assert "queue_load_unknown" in json.dumps(json.loads(response.body))
    facts = {f["name"]: f for f in receipt.resource_decision["facts"]}
    assert facts["queue_active"]["value"] is None
    assert facts["queue_active"]["quality"] == "unavailable"


def test_stt_refusal_keeps_decision_on_exception(tmp_path):
    from gateway import stt_chunking
    from unittest.mock import patch
    from types import SimpleNamespace
    config = SimpleNamespace(max_input_bytes=1024, max_decoded_bytes=1024, min_free_bytes=0,
                             spool_dir=tmp_path, global_concurrency=0,
                             request_claim=lambda: ResourceClaim(disk_mb=1))
    with patch.object(stt_chunking, "_disk_free_bytes", return_value=1024 * 1024):
        with pytest.raises(stt_chunking.SttResourceLimitExceeded) as caught:
            stt_chunking.SttResourceGate().acquire(request_id="req", config=config)
    assert caught.value.decision.status.value == "rejected"
    assert caught.value.decision.details.reasons[0].code == "queue_full"


def test_unknown_app_and_queue_counts_are_not_zero():
    args = (ServiceAvailability(available=True), ResourceEstimate("chat.generate", app_id="mine"),
            AdmissionConstraints(max_app_concurrency={"mine": 1}))
    assert advise_resource_admission(*args).denied
    assert advise_resource_admission(*args, CurrentLoad(active_by_app={"mine": 0})).allowed
    request = AdmissionRequest("req", ResourceClaim())
    assert not evaluate_admission(request, CapacitySnapshot(queue_limits={QueueClass.INTERACTIVE: 1})).accepted
    assert evaluate_admission(request, CapacitySnapshot(queue_limits={QueueClass.INTERACTIVE: 1},
                                                        active_by_queue={QueueClass.INTERACTIVE: 0})).accepted


def test_empty_queue_read_is_measured_and_admitted():
    import proxy
    from unittest.mock import patch
    with patch.object(proxy, "COMFY_ENABLED", True), \
         patch.object(proxy, "_operator_service_availability_overrides", return_value={}), \
         patch.object(proxy, "_effective_media_job_admission_cfg", return_value={"max_queue_depth": 1}), \
         patch.object(proxy.media_jobs, "recent", return_value=[]):
        receipt = RouteReceipt(protocol="test", intent="image.generate")
        assert proxy._media_job_admission_response("image.generate", receipt=receipt) is None
    facts = {f["name"]: f for f in receipt.resource_decision["facts"]}
    assert facts["queue_active"]["value"] == 0
    assert facts["queue_active"]["quality"] == "measured"
    assert facts["queue_active"]["observed_at"] is not None


@pytest.mark.parametrize("quality", ["declared", "estimated", "measured"])
def test_capacity_source_quality_survives(quality):
    decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(ram_mb=1)),
                                 CapacitySnapshot(total_ram_mb=10, used_ram_mb=0, quality=quality, observed_at=123))
    facts = {f.name: f for f in decision.details.facts}
    assert facts["available_ram_mb"].quality == quality
    assert all(f.observed_at == 123 for f in facts.values())


@pytest.mark.parametrize("available,degraded,expected", [
    (True, False, '{"outcome": "allowed", "allowed": true, "retry_after": null, "reasons": ["resources available"], "reason_codes": ["resources_available"]}'),
    (True, True, '{"outcome": "degraded", "allowed": true, "retry_after": null, "reasons": ["service degraded"], "reason_codes": ["service_degraded"]}'),
    (False, False, '{"outcome": "denied", "allowed": false, "retry_after": null, "reasons": ["service unavailable"], "reason_codes": ["service_unavailable"]}'),
])
def test_legacy_route_outcomes_are_byte_identical(available, degraded, expected):
    decision = advise_resource_admission(ServiceAvailability(available=available, degraded=degraded),
                                         ResourceEstimate("image.generate"))
    assert json.dumps(decision.as_dict()) == expected


@pytest.mark.parametrize("requested,total,active,expected_status,expected_reasons", [
    (1, 10, 0, "accepted", ["capacity available"]),
    (20, 10, 0, "delayed", ["not enough vram_mb: requested 20, available 10"]),
    (20, 10, 1, "rejected", ["queue concurrency limit reached for interactive"]),
])
def test_legacy_claim_outcomes_are_byte_identical(requested, total, active, expected_status, expected_reasons):
    decision = evaluate_admission(AdmissionRequest("req", ResourceClaim(vram_mb=requested)),
                                 CapacitySnapshot(total_vram_mb=total, used_vram_mb=0,
                                                  active_by_queue={QueueClass.INTERACTIVE: active},
                                                  queue_limits={QueueClass.INTERACTIVE: 1}))
    expected = {"request_ref": "c3f7bdf537c4", "status": expected_status, "accepted": expected_status == "accepted",
                "reasons": expected_reasons, "caveats": []}
    assert json.dumps(summarize_decision(decision)) == json.dumps(expected)
