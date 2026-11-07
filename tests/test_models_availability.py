"""`azizam_availability` and `?available=true` on GET /v1/models (docs/api/MODEL_METADATA.md)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
import status_endpoint  # noqa: E402
from gateway import availability  # noqa: E402
from gateway.litellm_substrate import litellm_public_model_service_ids  # noqa: E402
from tests.test_model_metadata_endpoints import _start_model_list_patches  # noqa: E402

NAMES = {
    "down-model": ["svc-down"],
    "up-model": ["svc-up"],
    "split-model": ["svc-down", "svc-up"],
    "both-down-model": ["svc-down", "svc-down-too"],
    "unseen-model": ["svc-unseen"],
}
UNMAPPED = "unmapped-model"
OFF_MODEL = "off-model"
REAL_INDEX = proxy._model_service_ids_index
EVIDENCE = {"svc-down": {"status": "down"}, "svc-down-too": {"status": "down"}, "svc-up": {"status": "up"}}
REGISTRY = {"services": {sid: {"enabled": True, "config": {}} for sid in
                         ("svc-down", "svc-down-too", "svc-up", "svc-unseen")}}


async def _payload(*_args, **_kwargs):
    ids = [*NAMES, UNMAPPED, OFF_MODEL]
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 1, "owned_by": "openai"} for i in ids]}


def _snapshot(*, fresh: bool = True) -> dict:
    if not fresh:
        return {}
    return {"generated_at": int(time.time()), "backends": [
        {"service_id": "svc-down", "status": "down"},
        {"service_id": "svc-down-too", "status": "down"},
        {"service_id": "svc-up", "status": "up"},
    ]}


class ModelAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _start_model_list_patches(self)
        self.status = _snapshot()
        # Fail the test if the request starts a probe or waits for a status build.
        self.probes = {
            name: MagicMock(side_effect=AssertionError(f"{name} must not run"))
            for name in ("_engine_probe_json",)
        }
        patches = [
            patch.object(proxy, "_litellm_models_payload", _payload),
            patch.object(proxy, "_model_service_ids_index", lambda _registry: NAMES),
            patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=REGISTRY),
            patch.object(proxy, "_cached_status_payload_for_availability", lambda: self.status),
            patch.object(proxy, "_live_probe_evidence", AsyncMock(side_effect=AssertionError("live probe"))),
            patch.object(proxy, "_status_payload_with_service_rows", AsyncMock(side_effect=AssertionError("status rows"))),
            patch.object(status_endpoint, "get_cached_status", AsyncMock(side_effect=AssertionError("status build"))),
            patch.object(proxy, "_engine_probe_json", self.probes["_engine_probe_json"]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _judge(self, entry: dict, *, by_name=NAMES, workflows=None, workflow_ids=None, evidence=EVIDENCE,
               fresh: bool = True, records=REGISTRY):
        return proxy._model_entry_availability(
            entry, by_name=by_name, workflows=workflows or {}, workflow_ids_by_model=workflow_ids or {},
            service_records=records, evidence=evidence, snapshot_fresh=fresh)

    def _entries(self, query: str = "") -> dict:
        response = self.client.get("/v1/models" + query)
        self.assertEqual(response.status_code, 200, response.text)
        return {entry["id"]: entry for entry in response.json()["data"]}

    def test_only_service_down_is_unavailable_and_hidden_only_on_request(self) -> None:
        entry = self._entries()["down-model"]
        self.assertEqual(entry["azizam_availability"]["state"], "unavailable")
        self.assertTrue(entry["azizam_availability"]["reason"])
        self.assertNotIn("down-model", self._entries("?available=true"))
        self.assertIn("up-model", self._entries("?available=true"))
        self.assertEqual(self._entries("?available=true")["up-model"]["azizam_availability"], {"state": "available"})

    def test_unknown_state_stays_listed_with_the_filter(self) -> None:
        self.status = _snapshot(fresh=False)
        listed = self._entries("?available=true")
        for name in NAMES:
            self.assertEqual(listed[name]["azizam_availability"], {"state": "unknown"})
        # A service with no row in a fresh snapshot is unknown too.
        self.status = _snapshot()
        self.assertEqual(self._entries("?available=true")["unseen-model"]["azizam_availability"], {"state": "unknown"})

    def test_several_services_follow_the_any_available_rule(self) -> None:
        listed = self._entries()
        self.assertEqual(listed["split-model"]["azizam_availability"], {"state": "available"})
        self.assertEqual(listed["both-down-model"]["azizam_availability"]["state"], "unavailable")
        self.assertNotIn("both-down-model", self._entries("?available=true"))
        self.assertIn("split-model", self._entries("?available=true"))

    def test_default_list_has_the_same_ids_as_before(self) -> None:
        with patch.object(proxy, "_annotate_model_availability", lambda data, **_kw: data):
            before = set(self._entries())
        self.assertTrue({*NAMES, UNMAPPED} <= before)
        self.assertEqual(set(self._entries()), before)
        for value in ("?available=false", "?available=1", "?available=", "?available=TRUE"):
            with self.subTest(query=value):
                self.assertEqual(set(self._entries(value)), before)

    def test_availability_pass_starts_no_probe_and_no_status_build(self) -> None:
        # The pass alone: the context-window annotation elsewhere in the request may read an engine, this one may not.
        get_probe = AsyncMock(side_effect=AssertionError("_get_probe_json must not run"))
        with patch.object(proxy, "_get_probe_json", get_probe):
            for only_available in (False, True):
                data = [{"id": name} for name in (*NAMES, UNMAPPED)]
                proxy._annotate_model_availability(data, only_available=only_available)
        get_probe.assert_not_called()
        self.probes["_engine_probe_json"].assert_not_called()
        proxy._live_probe_evidence.assert_not_called()
        proxy._status_payload_with_service_rows.assert_not_called()
        status_endpoint.get_cached_status.assert_not_called()

    def test_unmapped_entry_has_no_field_and_is_kept(self) -> None:
        self.assertNotIn("azizam_availability", self._entries()[UNMAPPED])
        self.assertIn(UNMAPPED, self._entries("?available=true"))

    def test_profile_filtering_runs_before_the_availability_filter(self) -> None:
        # The profile filter adds an entry (as a caller's profile can) that only the availability pass can judge.
        # With the order swapped the pass never sees it, so it would be listed with no field.
        seen_annotated: list[str] = []

        async def profile(data, *_args, **_kwargs):
            seen_annotated.extend(e["id"] for e in data if "azizam_availability" in e)
            added = {"id": "profile-added-model", "object": "model", "created": 1, "owned_by": "openai"}
            return [e for e in data if e["id"] != "up-model"] + [added], {}

        names = {**NAMES, "profile-added-model": ["svc-down"]}
        with patch.object(proxy, "_filter_model_list_for_profile", profile), \
                patch.object(proxy, "_model_service_ids_index", lambda _registry: names):
            listed = self._entries("?available=true")
            plain = self._entries()
        self.assertEqual(seen_annotated, [])
        self.assertNotIn("up-model", listed)
        self.assertNotIn("profile-added-model", listed)
        self.assertEqual(plain["profile-added-model"]["azizam_availability"]["state"], "unavailable")
        self.assertIn("split-model", listed)

    def test_alias_with_a_down_target_and_an_unmapped_target_is_unknown(self) -> None:
        mixed = {"id": "alias", "azizam_resolves_to": {"chat": "down-model", "embeddings": "no-such-model"}}
        self.assertEqual(self._judge(mixed), {"state": "unknown"})
        down = {"id": "alias", "azizam_resolves_to": {"chat": "down-model", "embeddings": "both-down-model"}}
        self.assertEqual(self._judge(down)["state"], "unavailable")
        nothing = {"id": "alias", "azizam_resolves_to": {"chat": "no-such-model", "embeddings": "also-none"}}
        self.assertIsNone(self._judge(nothing))
        up = {"id": "alias", "azizam_resolves_to": {"chat": "no-such-model", "embeddings": "up-model"}}
        self.assertEqual(self._judge(up), {"state": "available"})

    def test_workflow_with_a_down_service_and_an_unresolved_requirement_is_unavailable(self) -> None:
        def record(services, workflows):
            contract = {"bindings": {"b": {"requires": {"services": services, "workflows": workflows}}}}
            return SimpleNamespace(id="wf", contract=contract, supported_backends=["gateway"], manifest={})

        entry = {"id": "wf-model", "azizam_workflow_id": "wf"}
        down = {"wf": record(["svc-down"], ["missing-workflow"])}
        self.assertEqual(self._judge(entry, workflows=down)["state"], "unavailable")
        up = {"wf": record(["svc-up"], ["missing-workflow"])}
        self.assertEqual(self._judge(entry, workflows=up), {"state": "unknown"})
        only_missing = {"wf": record([], ["missing-workflow"])}
        self.assertEqual(self._judge(entry, workflows=only_missing), {"state": "unknown"})
        self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-up"], [])}), {"state": "available"})

    def test_disabled_service_is_unavailable_and_still_not_in_the_routing_index(self) -> None:
        registry = {"services": {
            **REGISTRY["services"],
            "svc-off": {"enabled": False, "config": {"protocol": "openai", "model": OFF_MODEL}},
        }}
        self.assertNotIn(OFF_MODEL, litellm_public_model_service_ids(registry))
        self.assertEqual(litellm_public_model_service_ids(registry, include_disabled=True)[OFF_MODEL], ["svc-off"])
        with patch.dict(proxy.config, {"backends": {}}), patch.object(proxy, "_model_service_ids_index", REAL_INDEX), \
                patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
            entry = self._entries()[OFF_MODEL]
            self.assertEqual(entry["azizam_availability"]["state"], "unavailable")
            self.assertEqual(entry["azizam_availability"]["reason"], "The service is disabled.")
            self.assertNotIn(OFF_MODEL, self._entries("?available=true"))

    def test_a_remote_provider_counts_only_under_its_prefixed_name(self) -> None:
        remote_cfg = {"protocol": "openai", "provider_type": "paid-remote", "model": "same", "api_key_env": "TEST_REMOTE_KEY"}
        registry = {"services": {"svc-remote": {"enabled": True, "config": remote_cfg}}}
        (prefixed,) = litellm_public_model_service_ids(registry)
        self.assertNotEqual(prefixed, "same")
        backends = {"svc-local": {"model_id": "same"}, "svc-remote": dict(remote_cfg, model_id="same")}
        with patch.dict(proxy.config, {"backends": backends}):
            index = REAL_INDEX(registry)
        self.assertEqual(index[proxy.normalize_model_id(prefixed)], ["svc-remote"])
        # The local model's own name must not borrow the remote service: a down local model stays unavailable.
        self.assertEqual(index["same"], ["svc-local"])

    def test_busy_and_degraded_services_count_as_available(self) -> None:
        records = {"services": {sid: {"enabled": True, "config": {}} for sid in ("busy", "bad", "gone")}}
        evidence = {"busy": {"status": "busy"}, "bad": {"status": "degraded"}, "gone": {"status": "down"}}
        for service in ("busy", "bad"):
            with self.subTest(service=service):
                self.assertEqual(availability.model_availability([service], service_records=records, evidence=evidence),
                                 {"state": "available"})
        self.assertEqual(availability.model_availability(["busy", "bad"], service_records=records, evidence=evidence,
                                                         require_all=True), {"state": "available"})
        self.assertEqual(availability.model_availability(["gone", "bad"], service_records=records, evidence=evidence)["state"],
                         "available")

    def test_expired_status_snapshot_reads_as_unknown(self) -> None:
        payload = {"backends": [{"service_id": "svc-up", "status": "up"}]}
        for age_s, expected in ((1.0, {"state": "available"}), (status_endpoint.SNAPSHOT_FRESH_S + 1, {"state": "unknown"})):
            snapshot = {"payload": payload, "section_at": {"backends": 10_000.0 - age_s}}
            with self.subTest(age_s=age_s), \
                    patch.object(status_endpoint, "_now", return_value=10_000.0), \
                    patch.object(status_endpoint, "_SNAPSHOT_WANTED_UNTIL", 0.0), \
                    patch.dict(status_endpoint._SNAPSHOT, snapshot, clear=True), \
                    patch.object(proxy, "_cached_status_payload_for_availability", status_endpoint.latest_payload):
                self.assertEqual(self._entries()["up-model"]["azizam_availability"], expected)

    def test_a_reused_entry_never_keeps_a_stale_field(self) -> None:
        stale = {"state": "available"}
        data = [{"id": UNMAPPED, "azizam_availability": dict(stale)}, {"id": "down-model", "azizam_availability": dict(stale)}]
        out = proxy._annotate_model_availability(data, only_available=False)
        self.assertNotIn("azizam_availability", out[0])
        self.assertEqual(out[1]["azizam_availability"]["state"], "unavailable")

    def test_service_mapping_is_built_once_per_request(self) -> None:
        names = {f"model-{i}": ["svc-down", "svc-up", "svc-unseen"] for i in range(25)}
        data = [{"id": name} for name in names]
        real = availability._services_mapping
        calls = MagicMock(side_effect=real)
        with patch.object(proxy, "_model_service_ids_index", lambda _registry: names), \
                patch.object(availability, "_services_mapping", calls):
            proxy._annotate_model_availability(data, only_available=False)
        self.assertLessEqual(calls.call_count, 2)

    def test_each_service_is_projected_once_per_request(self) -> None:
        names = {f"model-{i}": ["svc-down", "svc-up", "svc-unseen"] for i in range(25)}
        calls = MagicMock(side_effect=availability.project_service)
        with patch.object(proxy, "_model_service_ids_index", lambda _registry: names), \
                patch.object(availability, "project_service", calls):
            proxy._annotate_model_availability([{"id": name} for name in names], only_available=False)
        self.assertEqual(sorted(call.args[0] for call in calls.call_args_list), ["svc-down", "svc-unseen", "svc-up"])


if __name__ == "__main__":
    unittest.main()
