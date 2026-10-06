"""Endpoint tests for the /v1/models metadata contract (docs/api/MODEL_METADATA.md)."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import model_metadata as mm  # noqa: E402
from gateway.models_catalog import build_models_catalog  # noqa: E402
from gateway.capabilities import Capability  # noqa: E402

SNAPSHOT = json.loads((ROOT / "tests" / "fixtures" / "models-list-snapshot-2026-10.json").read_text(encoding="utf-8"))

# The LLM ids LiteLLM listed in the live snapshot (owned_by "openai").
LITELLM_IDS = [e["id"] for e in SNAPSHOT["data"] if e.get("owned_by") == "openai"]
THINKING = {"default", "coder", "coder-q5", "coder-q8", "coder-unchained", "rp", "current"}
NON_THINKING = {"default-fast", "current-fast"}


async def _fake_litellm_payload(*_args, **_kwargs):
    return {
        "object": "list",
        "data": [{"id": model_id, "object": "model", "created": 1_700_000_000, "owned_by": "openai"} for model_id in LITELLM_IDS],
        "x_ai_gateway_llm_execution": "litellm",
    }


def _fake_decision_models(*_args, **_kwargs):
    return [
        SimpleNamespace(id=model_id, view=lambda: {"capabilities": {"backend": "decisions", "available": True}})
        for model_id in ("decide", "decider-2b")
    ]


async def _no_context_probe(*_args, **_kwargs):
    return None


def _strip_contract_fields(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    for entry in out.get("data", []):
        for key in mm.OUTPUT_FIELDS:
            entry.pop(key, None)
    return out


class ModelListEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_litellm_runtime_config", return_value=SimpleNamespace(enabled=True, service_id="litellm")),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "_litellm_models_payload", _fake_litellm_payload),
            patch.object(proxy, "_decision_models_for", _fake_decision_models),
            patch.object(proxy, "_kobold_context_window", _no_context_probe),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _list(self, path: str = "/v1/models") -> dict:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_every_entry_gets_a_type_and_architecture(self) -> None:
        data = self._list()["data"]
        self.assertGreater(len(data), 20)
        for entry in data:
            with self.subTest(model=entry["id"]):
                self.assertIn(entry.get("type"), mm.MODEL_TYPES)
                self.assertTrue(entry["architecture"]["input_modalities"])
                self.assertTrue(entry["architecture"]["output_modalities"])
                self.assertEqual("vision" in entry.get("tags", []), "image" in entry["architecture"]["input_modalities"])

    def test_list_routes_and_retrieve_agree(self) -> None:
        primary = self._list("/v1/models")
        self.assertEqual(self._list("/openai/v1/models")["data"], primary["data"])
        for entry in primary["data"]:
            with self.subTest(model=entry["id"]):
                response = self.client.get(f"/v1/models/{entry['id']}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), entry)

    def test_existing_fields_are_unchanged(self) -> None:
        with_metadata = self._list()
        with patch.object(mm, "annotate_entries", lambda *_a, **_k: None):
            without_metadata = self._list()
        self.assertEqual(_strip_contract_fields(with_metadata), without_metadata)

    def test_anthropic_list_is_unchanged(self) -> None:
        with_metadata = self.client.get("/anthropic/v1/models").json()
        with patch.object(mm, "annotate_entries", lambda *_a, **_k: None):
            without_metadata = self.client.get("/anthropic/v1/models").json()
        self.assertEqual(with_metadata, without_metadata)
        sdk_shaped = self.client.get("/v1/models", headers={"anthropic-version": "2023-06-01"}).json()
        self.assertEqual(sdk_shaped, with_metadata)
        for entry in with_metadata["data"]:
            self.assertEqual(set(entry), {"id", "type", "display_name", "created_at"})
            self.assertEqual(entry["type"], "model")

    def test_live_config_declares_local_chat_models_truthfully(self) -> None:
        by_id = {entry["id"]: entry for entry in self._list()["data"]}
        for model_id in THINKING | NON_THINKING:
            with self.subTest(model=model_id):
                entry = by_id[model_id]
                self.assertEqual(entry["type"], "language")
                self.assertEqual(entry["architecture"]["input_modalities"], ["text", "image"])
                self.assertIn("tool-use", entry["tags"])
                self.assertIn("vision", entry["tags"])
                self.assertEqual("reasoning" in entry["tags"], model_id in THINKING)
                self.assertNotIn("max_tokens", entry)
        self.assertEqual(by_id["vision"]["architecture"]["input_modalities"], ["text", "image"])
        self.assertIn("vision", by_id["vision"]["tags"])
        self.assertNotIn("reasoning", by_id["fast"]["tags"])
        self.assertEqual(by_id["nomic-embed-text-v1.5"]["type"], "embedding")
        self.assertEqual(by_id["embed"]["type"], "embedding")
        self.assertEqual(by_id["decide"]["type"], "decision")

    def test_openai_sdk_parses_the_payload(self) -> None:
        try:
            from openai.types import Model
        except ImportError:  # pragma: no cover - the hermetic lock pins openai
            self.skipTest("openai SDK not installed")
        for entry in self._list()["data"]:
            with self.subTest(model=entry["id"]):
                model = Model.model_validate(entry)
                self.assertEqual(model.id, entry["id"])
                self.assertEqual(model.object, "model")
                self.assertEqual(model.to_dict()["type"], entry["type"])

    def test_member_catalog_reports_the_same_values(self) -> None:
        listed = {entry["id"]: entry for entry in self._list()["data"]}
        model_entries = [
            *proxy._stable_alias_model_entries(),
            *proxy._build_workflow_model_entries(),
            *proxy._current_loaded_model_entries(),
        ]
        index = proxy._catalog_model_metadata(model_entries, proxy.ADMIN_STORE.get_service_registry(), [])
        shared = set(index) & set(listed)
        self.assertGreater(len(shared), 20)
        for model_id in shared:
            with self.subTest(model=model_id):
                self.assertEqual(index[model_id], {k: listed[model_id][k] for k in mm.OUTPUT_FIELDS if k in listed[model_id]})


class MemberCatalogRowTests(unittest.TestCase):
    def test_rows_carry_supplied_metadata(self) -> None:
        capability = Capability(intent="chat.completions", backend="engine", model="demo", input_modalities=("text",), output_modalities=("text",))
        fields = {"type": "language", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}}
        catalog = build_models_catalog(capabilities=[capability], model_metadata={"demo": fields})
        row = catalog["models"][0]
        self.assertEqual(row["type"], "language")
        self.assertEqual(row["architecture"], fields["architecture"])


class SnapshotFixtureTests(unittest.TestCase):
    """The 71-entry live snapshot: every entry is classifiable and keeps its fields."""

    def test_every_snapshot_entry_gets_a_type_and_keeps_existing_fields(self) -> None:
        entries = copy.deepcopy(SNAPSHOT["data"])
        origins = {
            **{e["id"]: mm.ORIGIN_LLM_CATALOG for e in entries if e.get("owned_by") == "openai"},
            **{e["id"]: mm.ORIGIN_DECISION for e in entries if isinstance(e.get("capabilities"), dict)},
        }
        mm.annotate_entries(entries, proxy._model_metadata_sources(origins))
        self.assertEqual(len(entries), 71)
        for before, after in zip(SNAPSHOT["data"], entries):
            with self.subTest(model=before["id"]):
                self.assertIn(after.get("type"), mm.MODEL_TYPES)
                self.assertEqual({k: after[k] for k in before}, before)
                self.assertEqual(set(after) - set(before), {k for k in mm.OUTPUT_FIELDS if k in after})


if __name__ == "__main__":
    unittest.main()
