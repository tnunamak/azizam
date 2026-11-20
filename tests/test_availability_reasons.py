"""Why a model is not usable: one closed set of reason codes, in `GET /v1/models` and in the refusal.

`azizam_availability.reason` (and `retry_after_s` when known) appears only when the state is not `available`. A
request to the same model that the gateway refuses carries the same code in `error.reason`. The codes are listed in
`gateway/availability_reasons.py` and in docs/api/MODEL_METADATA.md; this file pins both.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
import status_endpoint  # noqa: E402
from gateway import availability_reasons  # noqa: E402
from gateway.client_errors import ERROR_CODES  # noqa: E402
from tests.test_dispatch_admission import dispatch_env, fake_upstreams  # noqa: E402,F401 - fixtures
from tests.test_model_metadata_endpoints import _start_model_list_patches  # noqa: E402

CLOSED_SET = {
    "backend_unavailable",
    "current_model_unavailable",
    "engine_transitioning",
    "health_unknown",
    "model_not_deployed",
    "service_disabled",
    "service_maintenance",
}
MODEL = "reason-model"
SERVICE = "svc-reason"


async def _payload(*_args, **_kwargs):
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "created": 1, "owned_by": "openai"}]}


def _registry(*, enabled: bool = True) -> dict:
    return {"services": {SERVICE: {"enabled": enabled, "config": {}}}}


def _snapshot(status: str | None) -> dict:
    rows = [] if status is None else [{"service_id": SERVICE, "status": status}]
    return {"generated_at": 1, "backends": rows}


class ListingReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _start_model_list_patches(self)
        self.registry = _registry()
        self.status = _snapshot("up")
        self.overrides: dict = {}
        patches = [
            patch.object(proxy, "_litellm_models_payload", _payload),
            patch.object(proxy, "_model_service_ids_index", lambda _registry: {MODEL: [SERVICE]}),
            patch.object(proxy.ADMIN_STORE, "get_service_registry", side_effect=lambda: self.registry),
            patch.object(proxy, "_cached_status_payload_for_availability", lambda: self.status),
            patch.object(proxy, "_operator_service_availability_overrides", lambda: self.overrides),
            patch.object(proxy, "_live_probe_evidence", AsyncMock(side_effect=AssertionError("live probe"))),
            patch.object(status_endpoint, "get_cached_status", AsyncMock(side_effect=AssertionError("status build"))),
            patch.object(proxy, "_engine_probe_json", MagicMock(side_effect=AssertionError("probe"))),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _availability(self) -> dict:
        response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 200, response.text)
        entry = {item["id"]: item for item in response.json()["data"]}[MODEL]
        return entry["azizam_availability"]

    def test_each_cause_the_gateway_knows_yields_its_reason(self) -> None:
        cases = [
            ("engine stopped or unreachable", {"status": "down"}, True, {}, "unavailable", "backend_unavailable", None),
            ("engine starting or loading", {"status": "starting"}, True, {}, "unknown", "engine_transitioning", None),
            ("service turned off by the owner", {"status": "up"}, False, {}, "unavailable", "service_disabled", None),
            ("operator override: disabled", {"status": "up"}, True, {SERVICE: {"state": "disabled"}},
             "unavailable", "service_disabled", None),
            ("operator override: maintenance", {"status": "up"}, True,
             {SERVICE: {"state": "maintenance", "retry_after_seconds": 33}}, "unavailable", "service_maintenance", 33),
            ("operator override: draining", {"status": "up"}, True, {SERVICE: {"state": "draining"}},
             "unavailable", "service_maintenance", 10),
            ("health not measured", None, True, {}, "unknown", "health_unknown", None),
        ]
        for label, row, enabled, overrides, state, reason, retry in cases:
            with self.subTest(label):
                self.registry = _registry(enabled=enabled)
                self.status = _snapshot(None) if row is None else {"generated_at": 1, "backends": [{"service_id": SERVICE, **row}]}
                self.overrides = overrides
                expected = {"state": state, "reason": reason, **({"retry_after_s": retry} if retry else {})}
                self.assertEqual(self._availability(), expected)

    def test_a_stale_snapshot_reads_unknown_with_a_reason(self) -> None:
        self.status = {}
        self.assertEqual(self._availability(), {"state": "unknown", "reason": "health_unknown"})

    def test_an_available_model_carries_no_reason(self) -> None:
        for row in ("up", "busy", "degraded"):
            with self.subTest(row):
                self.status = _snapshot(row)
                self.assertEqual(self._availability(), {"state": "available"})
        # An override that only marks the service available changes nothing.
        self.status = _snapshot("up")
        self.overrides = {SERVICE: {"state": "available"}}
        self.assertEqual(self._availability(), {"state": "available"})

    def test_every_listed_reason_is_in_the_closed_set(self) -> None:
        for row in ("down", "starting", None, "up"):
            self.status = _snapshot(row)
            reason = self._availability().get("reason")
            self.assertTrue(reason is None or reason in availability_reasons.REASONS, reason)

    def test_a_model_the_key_may_not_use_stays_out_of_the_list(self) -> None:
        # The profile filter runs before the annotation: a hidden name is neither listed nor explained.
        async def hide_everything(data, _auth_context):
            return [], {}
        with patch.object(proxy, "_filter_model_list_for_profile", hide_everything):
            response = self.client.get("/v1/models")
        self.assertNotIn(MODEL, response.text)

    def test_model_not_deployed_and_the_empty_current_route(self) -> None:
        entry = {"id": "speech-alias", "azizam_alias": True, "azizam_resolves_to": {"speech": None},
                 "azizam_engine_candidates": {"speech": ["first"]}}
        engines = {"speech": {"first": {"engine": "first", "service": SERVICE, "model": "w", "deployed": False}}}
        state = proxy._model_entry_availability(
            entry, by_name={}, workflows={}, workflow_ids_by_model={}, service_records=self.registry,
            evidence={SERVICE: {"status": "up"}}, snapshot_fresh=True, engines_by_op=engines)
        self.assertEqual(state, {"state": "unavailable", "reason": "model_not_deployed"})
        current = proxy._model_entry_availability(
            {"id": "current"}, by_name={}, workflows={}, workflow_ids_by_model={}, service_records=self.registry,
            evidence={}, snapshot_fresh=True, internal_route_targets={"current": []})
        self.assertEqual(current, {"state": "unavailable", "reason": "current_model_unavailable"})


class ClosedSetTests(unittest.TestCase):
    def test_the_code_set_is_closed(self) -> None:
        self.assertEqual(set(availability_reasons.REASONS), CLOSED_SET)

    def test_a_reason_that_is_also_a_refusal_code_is_a_known_code(self) -> None:
        # A code the gateway already puts in `error.code` keeps its name; only the three owner-and-health words are new.
        new = CLOSED_SET - ERROR_CODES
        self.assertEqual(new, {"health_unknown", "service_disabled", "service_maintenance"})

    def test_the_client_doc_lists_exactly_the_closed_set(self) -> None:
        text = (ROOT / "docs" / "api" / "MODEL_METADATA.md").read_text()
        section = text.split("### Reason codes", 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE))
        self.assertEqual(documented, CLOSED_SET)


class RefusalAgreementTests(unittest.TestCase):
    """The helpers that build a refusal carry the same code as the list."""

    def test_stamp_copies_a_reason_code_and_leaves_other_codes_alone(self) -> None:
        for code in sorted(CLOSED_SET & ERROR_CODES):
            self.assertEqual(availability_reasons.stamp({"code": code, "message": "m"})["reason"], code)
        for code in ("invalid_request", "workflow_disabled", None):
            self.assertNotIn("reason", availability_reasons.stamp({"code": code, "message": "m"}))
        explicit = availability_reasons.stamp({"code": "backend_unavailable", "reason": "service_disabled"})
        self.assertEqual(explicit["reason"], "service_disabled")

    def test_the_gateways_error_builders_carry_the_reason(self) -> None:
        for code in ("backend_unavailable", "engine_transitioning", "current_model_unavailable"):
            body = proxy.openai_error_response("m", status_code=503, error_type="service_unavailable", code=code).body
            self.assertEqual(json.loads(body)["error"]["reason"], code)
        plain = proxy.openai_error_response("m", status_code=400, code="invalid_request")
        self.assertNotIn(b"reason", plain.body)
        refused = proxy._transition_unavailable_response(SimpleNamespace(reason="engine_busy", retry_after_s=5))
        self.assertEqual(json.loads(refused.body)["error"]["reason"], "engine_transitioning")
        unsupported = proxy._transition_unavailable_response(SimpleNamespace(reason="activation_not_supported", retry_after_s=5))
        self.assertNotIn("reason", json.loads(unsupported.body)["error"])

    def test_a_model_that_is_not_deployed_is_refused_with_the_listing_code(self) -> None:
        refusal = proxy.model_compatibility_error(proxy.stable_aliases.AliasUnavailableError("gone", code="model_not_deployed"))
        error = json.loads(refusal.body)["error"]
        self.assertEqual((refusal.status_code, error["code"], error["reason"]), (503, "model_not_deployed", "model_not_deployed"))
        unknown = proxy.model_compatibility_error(proxy.stable_aliases.AliasUnavailableError("gone", code="model_not_found"))
        self.assertNotIn("reason", json.loads(unknown.body)["error"])


@pytest.mark.parametrize("state,reason,retry", [
    ("disabled", "service_disabled", None),
    ("maintenance", "service_maintenance", 33),
    ("draining", "service_maintenance", 33),
])
def test_the_speech_refusal_and_the_list_give_the_same_reason(dispatch_env, monkeypatch, state, reason, retry):
    """A speech request to a service the operator took out of service, through the real route, and the listing of the model
    that service serves, read the same override and name the same reason."""
    client, calls = dispatch_env
    service_id = proxy._speech_service()[0]
    override = {"state": state, **({"retry_after_seconds": retry} if retry else {})}
    monkeypatch.setattr(proxy, "_operator_service_availability_overrides", lambda: {service_id: override})
    response = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "alloy", "response_format": "wav"})
    assert response.status_code == 503, response.text
    assert not calls
    error = response.json()["error"]
    assert error["reason"] == reason
    assert error["reason"] in availability_reasons.REASONS
    if retry:
        assert response.headers["retry-after"] == str(retry)
    # The same override, judged for the list.
    listed = proxy._model_entry_availability(
        {"id": "tts-1"}, by_name={"tts-1": [service_id]}, workflows={}, workflow_ids_by_model={},
        service_records={"services": {service_id: {"enabled": True, "config": {}}}},
        evidence={service_id: {"status": "up"}}, snapshot_fresh=True, overrides={service_id: override})
    assert listed["state"] == "unavailable"
    assert listed["reason"] == error["reason"]
    assert listed.get("retry_after_s") == retry


def _admission(monkeypatch, *, override, cfg=None, backends=None):
    monkeypatch.setattr(proxy, "_effective_media_job_admission_cfg", lambda: cfg or {})
    monkeypatch.setattr(proxy, "_operator_service_availability_overrides", lambda: {"selected": override} if override else {})
    monkeypatch.setitem(proxy.config, "backends", backends or {})
    return proxy._dispatch_admission_response("chat.generate", resource_class="chat", service_id="selected")


@pytest.mark.parametrize("override,backends,reason", [
    ({"state": "disabled"}, {}, "service_disabled"),
    ({"state": "maintenance", "retry_after_seconds": 7}, {}, "service_maintenance"),
    ({"state": "draining"}, {}, "service_maintenance"),
    ({}, {"selected": {"enabled": False}}, "service_disabled"),
])
def test_an_admission_refusal_about_the_service_names_the_listing_reason(monkeypatch, override, backends, reason):
    # With admission settings in force and no override, the service's own `enabled` decides.
    refusal = _admission(monkeypatch, override=override, backends=backends, cfg={"reserved_tokens": ["gpu"]})
    assert refusal is not None and refusal.status_code == 503
    error = json.loads(refusal.body)["error"]
    assert error["code"] == "resource_admission_denied"
    assert error["reason"] == reason


def test_an_admission_refusal_that_is_not_about_the_service_names_no_reason(monkeypatch):
    refusal = _admission(monkeypatch, override=None, cfg={"disabled_resource_classes": ["chat"]})
    assert refusal is not None
    assert "reason" not in json.loads(refusal.body)["error"]


def test_a_decision_engine_that_is_not_deployed_is_refused_with_the_listing_code():
    from gateway.decision_routing import DecisionRuntime
    result = DecisionRuntime._unavailable_result(SimpleNamespace(fallback_declared=False, fallbacks=[], backend="b"))  # type: ignore[arg-type]
    assert result.body["error"]["reason"] == "model_not_deployed"
