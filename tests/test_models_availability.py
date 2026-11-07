"""`azizam_availability` and `?available=true` on GET /v1/models (docs/api/MODEL_METADATA.md)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
import status_endpoint  # noqa: E402
from tests.test_model_metadata_endpoints import _start_model_list_patches  # noqa: E402

NAMES = {
    "down-model": ["svc-down"],
    "up-model": ["svc-up"],
    "split-model": ["svc-down", "svc-up"],
    "both-down-model": ["svc-down", "svc-down-too"],
    "unseen-model": ["svc-unseen"],
}
UNMAPPED = "unmapped-model"
REGISTRY = {"services": {sid: {"enabled": True, "config": {}} for sid in
                         ("svc-down", "svc-down-too", "svc-up", "svc-unseen")}}


async def _payload(*_args, **_kwargs):
    ids = [*NAMES, UNMAPPED]
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

    def test_request_starts_no_probe_and_no_status_build(self) -> None:
        self._entries()
        self._entries("?available=true")
        self.probes["_engine_probe_json"].assert_not_called()
        proxy._live_probe_evidence.assert_not_called()
        proxy._status_payload_with_service_rows.assert_not_called()
        status_endpoint.get_cached_status.assert_not_called()

    def test_unmapped_entry_has_no_field_and_is_kept(self) -> None:
        self.assertNotIn("azizam_availability", self._entries()[UNMAPPED])
        self.assertIn(UNMAPPED, self._entries("?available=true"))

    def test_profile_filtering_runs_before_the_availability_filter(self) -> None:
        async def hide_up(data, *_args, **_kwargs):
            return [e for e in data if e["id"] != "up-model"], {}

        with patch.object(proxy, "_filter_model_list_for_profile", hide_up):
            listed = self._entries("?available=true")
        self.assertNotIn("up-model", listed)
        self.assertNotIn("down-model", listed)
        self.assertIn("split-model", listed)


if __name__ == "__main__":
    unittest.main()
