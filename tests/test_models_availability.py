"""`azizam_availability` and `?available=true` on GET /v1/models (docs/api/MODEL_METADATA.md)."""

from __future__ import annotations

import asyncio
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
from gateway import availability, decision_provider_execution  # noqa: E402
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
               fresh: bool = True, records=REGISTRY, engines_by_op=None):
        return proxy._model_entry_availability(
            entry, by_name=by_name, workflows=workflows or {}, workflow_ids_by_model=workflow_ids or {},
            service_records=records, evidence=evidence, snapshot_fresh=fresh, engines_by_op=engines_by_op)

    def _entries(self, query: str = "") -> dict:
        response = self.client.get("/v1/models" + query)
        self.assertEqual(response.status_code, 200, response.text)
        return {entry["id"]: entry for entry in response.json()["data"]}

    def test_absolute_path_ids_are_dropped_whatever_their_suffix(self) -> None:
        async def with_paths(*_args, **_kwargs):
            payload = await _payload()
            payload["data"] += [{"id": i, "object": "model", "created": 1, "owned_by": "openai"}
                                for i in ("/models/a.bin", "/models/b.safetensors", "rel/c.bin")]
            return payload
        with patch.object(proxy, "_litellm_models_payload", with_paths):
            listed = self._entries()
        self.assertNotIn("/models/a.bin", listed)
        self.assertNotIn("/models/b.safetensors", listed)
        self.assertIn("rel/c.bin", listed)
        self.assertIn("up-model", listed)

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

    def test_a_workflow_is_judged_by_its_engine_even_when_it_declares_other_requirements(self) -> None:
        # Bindings commonly declare the files and nodes they need but no services; the engine still decides.
        def record(backends, requires):
            contract = {"bindings": {"b": {"requires": requires}}} if requires is not None else {"bindings": {}}
            return SimpleNamespace(id="wf", contract=contract, supported_backends=backends, manifest={})

        entry = {"id": "wf-model", "azizam_workflow_id": "wf"}
        files_only = {"models": [{"role": "checkpoint", "path": "checkpoints/x.safetensors"}], "custom_nodes": ["N"]}
        for requires in (None, files_only):
            with self.subTest(requires=requires):
                self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-up"], requires)}), {"state": "available"})
                self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-down"], requires)})["state"], "unavailable")
                self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-down", "svc-up"], requires)}),
                                 {"state": "available"})
                self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-up"], requires)}, fresh=False),
                                 {"state": "unknown"})
        # The engine and each required service must all be there at once.
        with_service = dict(files_only, services=["svc-down"])
        self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-up"], with_service)})["state"], "unavailable")
        self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-down"], dict(files_only, services=["svc-up"]))})
                         ["state"], "unavailable")
        self.assertEqual(self._judge(entry, workflows={"wf": record(["svc-up"], dict(files_only, services=["svc-unseen"]))}),
                         {"state": "unknown"})
        # A required workflow's engine is required too.
        two = {"wf": record(["svc-up"], {"workflows": ["dep"]}),
               "dep": SimpleNamespace(id="dep", contract={"bindings": {}}, supported_backends=["svc-down"], manifest={})}
        self.assertEqual(self._judge(entry, workflows=two)["state"], "unavailable")
        # Each backend is one way to run it, judged with its own binding's requirements: a down engine does not hide a
        # composed binding whose services are up, and a backend nothing can be judged for reads as unknown.
        def mixed(engine, composed_services):
            contract = {"bindings": {engine: {"requires": files_only}, "gateway": {"requires": {"services": composed_services}}}}
            return {"wf": SimpleNamespace(id="wf", contract=contract, supported_backends=[engine, "gateway"], manifest={})}

        self.assertEqual(self._judge(entry, workflows=mixed("svc-down", ["svc-up"])), {"state": "available"})
        self.assertEqual(self._judge(entry, workflows=mixed("svc-up", ["svc-down"])), {"state": "available"})
        self.assertEqual(self._judge(entry, workflows=mixed("svc-down", ["svc-down-too"]))["state"], "unavailable")
        self.assertEqual(self._judge(entry, workflows=mixed("svc-down", [])), {"state": "unknown"})
        # A backend that is no registry service (the gateway composing it) leaves the requirements to decide.
        self.assertEqual(self._judge(entry, workflows={"wf": record(["gateway"], {"services": ["svc-up"]})}),
                         {"state": "available"})
        self.assertIsNone(self._judge(entry, workflows={"wf": record(["gateway"], files_only)}))

    def test_every_bundled_workflow_on_a_known_engine_gets_a_state(self) -> None:
        records = {record.id: record for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records()}
        engines = {str(b) for record in records.values() for b in record.supported_backends} - {"gateway"}
        self.assertTrue(engines)
        registry = {"services": {sid: {"enabled": True, "config": {}} for sid in engines}}
        evidence = {sid: {"status": "up"} for sid in engines}
        unmarked = [
            workflow_id for workflow_id, record in records.items()
            if set(map(str, record.supported_backends)) & engines
            and self._judge({"id": workflow_id, "azizam_workflow_id": workflow_id}, workflows=records,
                            evidence=evidence, records=registry) is None
        ]
        self.assertEqual(unmarked, [])

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

    def test_a_lifecycle_routed_model_is_judged_by_the_backend_that_serves_it(self) -> None:
        # The catalog service declares the model; lifecycle routing sends it to another backend.
        record = {"id": "routed-model", "upstream_model": "routed-model", "gateway_backend": "svc-engine"}
        catalog_cfg = {"protocol": "openai", "gateway_lifecycle": True, "models": [record]}
        engine_cfg = {"protocol": "openai", "model": "engine-own-model"}
        registry = {"services": {"svc-catalog": {"enabled": True, "config": catalog_cfg},
                                 "svc-engine": {"enabled": True, "config": engine_cfg}}}
        self.assertEqual(litellm_public_model_service_ids(registry)["routed-model"], ["svc-engine"])
        # The record's own flag routes it the same way when the service config has none.
        own_flag = {"services": {"svc-catalog": {"enabled": True, "config": {
            "protocol": "openai", "models": [dict(record, gateway_lifecycle=True)]}}}}
        self.assertEqual(litellm_public_model_service_ids(own_flag)["routed-model"], ["svc-engine"])
        # Without lifecycle routing the declaring service answers, whatever the record names.
        plain = {"services": {"svc-catalog": {"enabled": True, "config": {"protocol": "openai", "models": [record]}}}}
        self.assertEqual(litellm_public_model_service_ids(plain)["routed-model"], ["svc-catalog"])
        # A turned-off declaring service routes nothing, so it still judges its own models.
        off = {"services": {**registry["services"], "svc-catalog": {"enabled": False, "config": catalog_cfg}}}
        self.assertEqual(litellm_public_model_service_ids(off, include_disabled=True)["routed-model"], ["svc-catalog"])
        # The runtime-config part of the index must not add the declaring service back, not even through the
        # service's own model name when a record already names it.
        for runtime_cfg in (catalog_cfg, dict(catalog_cfg, model_id="routed-model")):
            with self.subTest(runtime_cfg=sorted(runtime_cfg)), patch.dict(proxy.config, {"backends": {"svc-catalog": runtime_cfg}}):
                self.assertEqual(REAL_INDEX(registry)["routed-model"], ["svc-engine"])
        with patch.dict(proxy.config, {"backends": {"svc-catalog": dict(catalog_cfg, model_id="own-name")}}):
            self.assertEqual(REAL_INDEX(registry)["own-name"], ["svc-catalog"])
        for catalog, engine, expected in (("down", "up", "available"), ("up", "down", "unavailable")):
            self.status = {"generated_at": int(time.time()), "backends": [
                {"service_id": "svc-catalog", "status": catalog}, {"service_id": "svc-engine", "status": engine}]}
            with self.subTest(catalog=catalog, engine=engine), \
                    patch.dict(proxy.config, {"backends": {"svc-catalog": catalog_cfg}}), \
                    patch.object(proxy, "_model_service_ids_index", REAL_INDEX), \
                    patch.object(proxy, "_litellm_models_payload", AsyncMock(return_value={
                        "object": "list", "data": [{"id": "routed-model", "object": "model", "owned_by": "openai"}]})), \
                    patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
                self.assertEqual(self._entries()["routed-model"]["azizam_availability"]["state"], expected)
                self.assertEqual("routed-model" in self._entries("?available=true"), expected == "available")

    def test_the_status_snapshot_is_read_on_the_event_loop(self) -> None:
        # A read from a worker thread cannot restart a stopped status refresher; a read on the loop can.
        seen: list[bool] = []

        def read():
            try:
                asyncio.get_running_loop()
                seen.append(True)
            except RuntimeError:
                seen.append(False)
            return self.status

        with patch.object(proxy, "_cached_status_payload_for_availability", read):
            self.assertEqual(self._entries()["down-model"]["azizam_availability"]["state"], "unavailable")
        self.assertTrue(seen)
        self.assertTrue(all(seen), seen)

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

    @staticmethod
    def _engines(operation: str, *rows: tuple[str, str, bool]) -> dict:
        """{operation: {engine id: row}} as the alias discovery view lists it: (engine, service, deployed)."""
        return {operation: {engine: {"engine": engine, "service": service, "model": f"{engine}-weights", "deployed": deployed}
                            for engine, service, deployed in rows}}

    @staticmethod
    def _plain(model_id: str, kind: str = "decision", **extra) -> dict:
        """A plain list entry (no alias fields) as the metadata pass types it."""
        return {"id": model_id, "object": "model", "type": kind, **extra}

    @staticmethod
    def _engine_alias(operation: str, candidates: list[str], picked: str | None) -> dict:
        return {"id": "engine-alias", "azizam_alias": True, "azizam_resolves_to": {operation: picked},
                "azizam_engine_candidates": {operation: candidates}}

    def test_an_engine_alias_is_judged_by_its_deployed_engine_service(self) -> None:
        engines = self._engines("speech", ("first", "svc-down", True), ("second", "svc-up", True))
        # A deployed engine's service is up: the alias is available, though another candidate is down.
        alias = self._engine_alias("speech", ["first", "second"], "first")
        self.assertEqual(self._judge(alias, engines_by_op=engines), {"state": "available"})
        only_up = self._engines("speech", ("first", "svc-up", True))
        self.assertEqual(self._judge(self._engine_alias("speech", ["first"], "first"), engines_by_op=only_up),
                         {"state": "available"})

    def test_an_engine_alias_with_every_candidate_service_down_is_unavailable(self) -> None:
        engines = self._engines("speech", ("first", "svc-down", True), ("second", "svc-down-too", True))
        alias = self._engine_alias("speech", ["first", "second"], "first")
        self.assertEqual(self._judge(alias, engines_by_op=engines)["state"], "unavailable")
        # With no candidate deployed, all of them are judged, so the alias still reads down rather than unmarked.
        none_deployed = self._engines("speech", ("first", "svc-down", False), ("second", "svc-down-too", False))
        self.assertEqual(self._judge(self._engine_alias("speech", ["first", "second"], None),
                                     engines_by_op=none_deployed)["state"], "unavailable")

    def test_an_engine_alias_reads_unknown_without_a_fresh_snapshot(self) -> None:
        engines = self._engines("speech", ("first", "svc-up", True))
        alias = self._engine_alias("speech", ["first"], "first")
        self.assertEqual(self._judge(alias, engines_by_op=engines, fresh=False), {"state": "unknown"})
        # A service with no row in a fresh snapshot is unknown too.
        unseen = self._engines("speech", ("first", "svc-unseen", True))
        self.assertEqual(self._judge(alias, engines_by_op=unseen), {"state": "unknown"})

    def test_an_undeployed_first_candidate_is_skipped(self) -> None:
        # The undeployed first candidate is up and the deployed second one is down: the request can only reach the
        # second, so the alias is unavailable. Judging every candidate would call it available.
        engines = self._engines("transcriptions", ("first", "svc-up", False), ("second", "svc-down", True))
        alias = self._engine_alias("transcriptions", ["first", "second"], "second")
        self.assertEqual(self._judge(alias, engines_by_op=engines)["state"], "unavailable")
        # Several deployed candidates follow the any-available rule.
        both = self._engines("transcriptions", ("first", "svc-up", False), ("second", "svc-down", True),
                             ("third", "svc-up", True))
        self.assertEqual(self._judge(self._engine_alias("transcriptions", ["first", "second", "third"], "second"),
                                     engines_by_op=both), {"state": "available"})

    def test_a_decision_model_entry_is_judged_by_the_service_that_serves_it(self) -> None:
        engines = self._engines("decisions", ("decider-x", "svc-up", True), ("decider-y", "svc-down", True))
        # Its engine id, or the model the engine serves: a plain decision model entry.
        self.assertEqual(self._judge(self._plain("decider-x"), engines_by_op=engines), {"state": "available"})
        self.assertEqual(self._judge(self._plain("decider-y-weights"), engines_by_op=engines)["state"], "unavailable")
        # A name that points at an engine slot (no candidate list) is judged by that slot's service.
        pointer = {"id": "jev-alias", "azizam_alias": True, "azizam_resolves_to": {"decisions": "decider-y"}}
        self.assertEqual(self._judge(pointer, engines_by_op=engines)["state"], "unavailable")
        self.assertIsNone(self._judge(self._plain("no-such-model"), engines_by_op=engines))

    def test_a_decision_provider_catalog_name_maps_to_the_provider_service(self) -> None:
        registry = {"services": {"prov": {"id": "prov", "kind": "provider", "enabled": True,
                                           "capabilities": ["typesafe-systemone"],
                                           "config": {"provider_id": "prov", "base_url": "https://prov.test",
                                                      "api_key_env": "PROV_TEST_KEY", "cost_class": "paid-remote",
                                                      "default_models": ["prov/jev-latest"]}}}}
        with patch.dict(proxy.config, {"backends": {}}):
            self.assertEqual(REAL_INDEX(registry)["prov/jev-latest"], ["prov"])

    def test_known_candidates_with_none_deployed_read_unavailable_whatever_their_health(self) -> None:
        # A request to the alias gets model_not_deployed; the services' own health cannot make it routable.
        for service, label in (("svc-up", "fresh up"), ("svc-unseen", "no row in the snapshot")):
            engines = self._engines("speech", ("first", service, False), ("second", service, False))
            with self.subTest(service=label):
                state = self._judge(self._engine_alias("speech", ["first", "second"], None), engines_by_op=engines)
                self.assertEqual(state["state"], "unavailable")
                self.assertTrue(state["reason"])
        # Idle and startable on demand is still not deployed.
        idle = {"svc-idle": {"status": "idle"}}
        records = {"services": {"svc-idle": {"enabled": True, "config": {"lifecycle": {"mode": "on_demand"}}}}}
        engines = self._engines("speech", ("first", "svc-idle", False))
        self.assertEqual(self._judge(self._engine_alias("speech", ["first"], None), engines_by_op=engines,
                                     evidence=idle, records=records)["state"], "unavailable")
        # The direct lookup (a name that points at one engine, and an engine id) reads the same.
        pointer = {"id": "pointer", "azizam_alias": True, "azizam_resolves_to": {"speech": "first"}}
        self.assertEqual(self._judge(pointer, engines_by_op=engines, evidence=idle, records=records)["state"], "unavailable")
        self.assertEqual(self._judge(self._plain("first", "audio"), engines_by_op=engines, evidence=idle,
                                     records=records)["state"], "unavailable")
        # Without a fresh snapshot the deployment still decides: it comes from the registry, not the snapshot.
        up = self._engines("speech", ("first", "svc-up", False))
        self.assertEqual(self._judge(self._engine_alias("speech", ["first"], None), engines_by_op=up,
                                     fresh=False)["state"], "unavailable")

    def test_no_known_candidate_row_keeps_the_old_behaviour(self) -> None:
        # No row for the candidate at all: nothing to judge, so no mark (the target name is tried instead).
        alias = self._engine_alias("speech", ["ghost"], None)
        self.assertIsNone(self._judge(alias, engines_by_op={"speech": {}}))
        self.assertIsNone(self._judge(alias, engines_by_op=None))
        # A candidate with no row, beside an undeployed one: the known row decides.
        engines = self._engines("speech", ("first", "svc-up", False))
        self.assertEqual(self._judge(self._engine_alias("speech", ["ghost", "first"], None),
                                     engines_by_op=engines)["state"], "unavailable")

    def test_an_engine_id_beats_another_engines_model_of_the_same_name(self) -> None:
        # alpha (service down) and beta (service up) serving a model named alpha: asking for alpha selects the alpha
        # engine alone, with no fallback to beta.
        for alpha_service, beta_service, expected in (("svc-down", "svc-up", "unavailable"),
                                                      ("svc-up", "svc-down", "available")):
            engines = {"decisions": {
                "alpha": {"engine": "alpha", "service": alpha_service, "model": "alpha-weights", "deployed": True},
                "beta": {"engine": "beta", "service": beta_service, "model": "alpha", "deployed": True}}}
            with self.subTest(alpha=alpha_service):
                self.assertEqual(self._judge(self._plain("alpha"), engines_by_op=engines)["state"], expected)
        # With no engine of that id, the served-model name still maps.
        engines = {"decisions": {"beta": {"engine": "beta", "service": "svc-down", "model": "alpha", "deployed": True}}}
        self.assertEqual(self._judge(self._plain("alpha"), engines_by_op=engines)["state"], "unavailable")

    def test_a_name_is_matched_within_one_operation(self) -> None:
        engines = {"decisions": {"shared": {"engine": "shared", "service": "svc-down", "model": "m", "deployed": True}},
                   "speech": {"shared": {"engine": "shared", "service": "svc-up", "model": "m", "deployed": True}},
                   "rerank": {"other": {"engine": "other", "service": "svc-up", "model": "shared", "deployed": True}}}
        self.assertEqual(self._judge(self._plain("shared"), engines_by_op=engines)["state"], "unavailable")
        self.assertEqual(self._judge(self._plain("shared", "audio", architecture={
            "input_modalities": ["text"], "output_modalities": ["audio"]}), engines_by_op=engines),
            {"state": "available"})
        # A reranking entry never takes the decision engine of the same id, nor the speech one.
        self.assertEqual(self._judge(self._plain("shared", "reranking"), engines_by_op=engines), {"state": "available"})
        # A transcription entry looks in transcriptions only.
        self.assertIsNone(self._judge(self._plain("shared", "audio", architecture={
            "input_modalities": ["audio"], "output_modalities": ["text"]}), engines_by_op=engines))

    def test_an_ordinary_entry_never_inherits_an_engines_state(self) -> None:
        engines = {"decisions": {"decider": {"engine": "decider", "service": "svc-down", "model": "common", "deployed": True}},
                   "speech": {"voice": {"engine": "voice", "service": "svc-down", "model": "chat-name", "deployed": True}}}
        for kind in ("language", "embedding", "image", None):
            for name in ("common", "decider", "voice", "chat-name"):
                entry = self._plain(name, kind) if kind else {"id": name}
                with self.subTest(kind=kind, name=name):
                    self.assertIsNone(self._judge(entry, engines_by_op=engines))
        # Through the model list: an unmapped language entry keeps no mark and stays under ?available=true.
        data = [{"id": "common", "type": "language"}, {"id": "decider", "type": "decision"}]
        with patch.object(proxy, "_gateway_aliases_provider", lambda *_a, **_k: {
                op: {"engines": list(rows.values()), "stable": []} for op, rows in engines.items()}):
            out = proxy._annotate_model_availability(data, only_available=True)
        self.assertEqual([item["id"] for item in out], ["common"])
        self.assertNotIn("azizam_availability", out[0])

    def test_a_mapped_language_entry_keeps_its_own_services_state(self) -> None:
        # Its own service is up; a decision engine with the same model name is down. The name mapping wins.
        engines = {"decisions": {"decider": {"engine": "decider", "service": "svc-down", "model": "up-model",
                                             "deployed": True}}}
        self.assertEqual(self._judge(self._plain("up-model", "language"), engines_by_op=engines), {"state": "available"})
        self.assertEqual(self._judge(self._plain("down-model", "decision"), engines_by_op=engines)["state"], "unavailable")
        # A decision-typed entry that is mapped by name is judged by that mapping first.
        self.assertEqual(self._judge(self._plain("up-model", "decision"), engines_by_op=engines), {"state": "available"})

    def test_a_turned_off_decision_provider_keeps_its_catalog_names_and_reads_unavailable(self) -> None:
        def registry(enabled: bool) -> dict:
            return {"services": {"prov": {"id": "prov", "kind": "provider", "enabled": enabled,
                                          "capabilities": ["typesafe-systemone"],
                                          "config": {"provider_id": "prov", "base_url": "https://prov.test",
                                                     "api_key_env": "PROV_TEST_KEY", "cost_class": "paid-remote",
                                                     "default_models": ["prov/jev-latest"]}}}}
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch.dict(proxy.config, {"backends": {}}):
                self.assertEqual(REAL_INDEX(registry(enabled))["prov/jev-latest"], ["prov"])
        records = registry(False)
        state = self._judge(self._plain("prov/jev-latest"), by_name={"prov/jev-latest": ["prov"]}, records=records,
                            evidence={"prov": {"status": "up"}})
        self.assertEqual(state["state"], "unavailable")

    def test_a_name_the_internal_route_answers_follows_its_resolved_target(self) -> None:
        # A turned-off service lists a model named like the gateway's own `current` route. The route resolves its target
        # at request time, so the turned-off record must not mark the name.
        name = "current"
        registry = {"services": {
            **REGISTRY["services"],
            "svc-off": {"enabled": False, "config": {"protocol": "openai", "model": name}},
        }}
        data = [{"id": name, "object": "model", "owned_by": "gateway"}]
        for target, expected in (("svc-up", "available"), ("svc-down", "unavailable")):
            with self.subTest(target=target), patch.dict(proxy.config, {"backends": {}}), \
                    patch.object(proxy, "_model_service_ids_index", REAL_INDEX), \
                    patch.object(proxy, "_llm_backend_candidates", lambda target=target: [target]), \
                    patch.object(proxy, "_litellm_models_payload", AsyncMock(return_value={"object": "list", "data": data})), \
                    patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
                self.assertEqual(self._entries()[name]["azizam_availability"]["state"], expected)
                self.assertEqual(name in self._entries("?available=true"), expected == "available")
        # No backend to resolve to: a request is refused, so the name reads unavailable (never the turned-off record).
        with patch.dict(proxy.config, {"backends": {}}), patch.object(proxy, "_model_service_ids_index", REAL_INDEX), \
                patch.object(proxy, "_llm_backend_candidates", lambda: []), \
                patch.object(proxy, "_litellm_models_payload", AsyncMock(return_value={"object": "list", "data": data})), \
                patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
            state = self._entries()[name]["azizam_availability"]
            self.assertEqual(state["state"], "unavailable")
            self.assertNotEqual(state["reason"], "The service is disabled.")

    def test_a_turned_off_listing_does_not_mark_a_name_an_enabled_service_serves(self) -> None:
        registry = {"services": {
            "svc-up": {"enabled": True, "config": {"protocol": "openai", "model": "both"}},
            "svc-off": {"enabled": False, "config": {"protocol": "openai", "model": "both"}},
            "svc-off-only": {"enabled": False, "config": {"protocol": "openai", "model": "alone"}},
        }}
        with patch.dict(proxy.config, {"backends": {}}):
            index = REAL_INDEX(registry)
        self.assertEqual(index["both"], ["svc-up"])
        self.assertEqual(index["alone"], ["svc-off-only"])

    def test_the_operations_of_a_name_are_resolved_each_on_its_own(self) -> None:
        def engines(speech_state: str, transcription_state: str) -> dict:
            return {"speech": {"shared": {"engine": "shared", "service": speech_state, "model": "m", "deployed": True}},
                    "transcriptions": {"shared": {"engine": "shared", "service": transcription_state, "model": "m",
                                                  "deployed": True}}}
        evidence = {"svc-up": {"status": "up"}, "svc-down": {"status": "down"}}
        for architecture in ({}, {"input_modalities": []}, {"input_modalities": ["video"]}):
            entry = self._plain("shared", "audio", architecture=architecture)
            for speech, transcription, expected in (("svc-down", "svc-up", "available"),
                                                    ("svc-up", "svc-down", "available"),
                                                    ("svc-down", "svc-down", "unavailable"),
                                                    ("svc-up", "svc-up", "available")):
                with self.subTest(architecture=architecture, speech=speech, transcription=transcription):
                    self.assertEqual(self._judge(entry, engines_by_op=engines(speech, transcription),
                                                 evidence=evidence)["state"], expected)

    def test_a_served_model_name_in_one_operation_beats_nothing_in_the_other(self) -> None:
        evidence = {"svc-up": {"status": "up"}, "svc-down": {"status": "down"}}
        # Speech engine `voice` serves model `shared` and is up; the transcription engine id `shared` is down. Each
        # operation resolves on its own, and one that can serve the name is enough.
        engines = {"speech": {"voice": {"engine": "voice", "service": "svc-up", "model": "shared", "deployed": True}},
                   "transcriptions": {"shared": {"engine": "shared", "service": "svc-down", "model": "w", "deployed": True}}}
        entry = self._plain("shared", "audio", architecture={})
        self.assertEqual(self._judge(entry, engines_by_op=engines, evidence=evidence), {"state": "available"})
        # Not merged across engines inside one operation: the exact engine id still wins there.
        within = {"speech": {"shared": {"engine": "shared", "service": "svc-down", "model": "w", "deployed": True},
                             "voice": {"engine": "voice", "service": "svc-up", "model": "shared", "deployed": True}}}
        self.assertEqual(self._judge(entry, engines_by_op=within, evidence=evidence)["state"], "unavailable")
        # A name that matches in no eligible operation stays unmarked.
        self.assertIsNone(self._judge(self._plain("other", "audio", architecture={}), engines_by_op=engines,
                                      evidence=evidence))

    def test_a_turned_off_decision_provider_is_still_excluded_without_the_flag(self) -> None:
        registry = {"services": {"prov": {"id": "prov", "kind": "provider", "enabled": False,
                                          "capabilities": ["typesafe-systemone"],
                                          "config": {"provider_id": "prov", "base_url": "https://prov.test",
                                                     "api_key_env": "PROV_TEST_KEY", "cost_class": "paid-remote",
                                                     "default_models": ["prov/jev-latest"]}}}}
        self.assertEqual(decision_provider_execution.systemone_providers(registry), [])
        self.assertEqual([p.id for p in decision_provider_execution.systemone_providers(registry, include_disabled=True)],
                         ["prov"])

    def test_stamped_engine_entries_are_marked_through_the_model_list(self) -> None:
        # The entries as the list builder gives them (type from the metadata pass): decision models and speech names.
        engines = {"decisions": {"decider": {"engine": "decider", "service": "svc-down", "model": "decider-2b",
                                             "deployed": True}},
                   "speech": {"voice": {"engine": "voice", "service": "svc-up", "model": "voice-m", "deployed": True}}}
        data = [{"id": "decider-2b", "type": "decision", "owned_by": "azizam"},
                {"id": "voice-m", "type": "audio", "architecture": {"input_modalities": ["text"],
                                                                      "output_modalities": ["audio"]}},
                {"id": "decider-2b-chat", "type": "language"}]
        with patch.object(proxy, "_gateway_aliases_provider", lambda *_a, **_k: {
                op: {"engines": list(rows.values()), "stable": []} for op, rows in engines.items()}):
            out = {item["id"]: item for item in proxy._annotate_model_availability(data, only_available=False)}
        self.assertEqual(out["decider-2b"]["azizam_availability"]["state"], "unavailable")
        self.assertEqual(out["voice-m"]["azizam_availability"], {"state": "available"})
        self.assertNotIn("azizam_availability", out["decider-2b-chat"])

    def test_engine_aliases_are_marked_in_the_model_list(self) -> None:
        engines = {"speech": {"engines": [{"engine": "first", "service": "svc-down", "model": "m", "deployed": False},
                                          {"engine": "second", "service": "svc-up", "model": "m2", "deployed": True}],
                              "stable": []}}
        alias = self._engine_alias("speech", ["first", "second"], "second")
        with patch.object(proxy, "_gateway_aliases_provider", lambda *_a, **_k: engines):
            out = proxy._annotate_model_availability([dict(alias)], only_available=False)
        self.assertEqual(out[0]["azizam_availability"], {"state": "available"})

    def test_a_reused_entry_never_keeps_a_stale_field(self) -> None:
        stale = {"state": "available"}
        data = [{"id": UNMAPPED, "azizam_availability": dict(stale)}, {"id": "down-model", "azizam_availability": dict(stale)}]
        out = proxy._annotate_model_availability(data, only_available=False)
        self.assertNotIn("azizam_availability", out[0])
        self.assertEqual(out[1]["azizam_availability"]["state"], "unavailable")
        with patch.object(proxy.ADMIN_STORE, "get_service_registry", side_effect=RuntimeError("unreadable")):
            out = proxy._annotate_model_availability(
                [{"id": "down-model", "azizam_availability": dict(stale)}], only_available=True)
        self.assertEqual(out, [{"id": "down-model"}])

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
