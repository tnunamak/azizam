"""Endpoint tests for the /v1/models metadata contract (docs/api/MODEL_METADATA.md)."""

from __future__ import annotations

import asyncio
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
THINKING = {"default", "coder", "coder-q5", "coder-q8", "coder-unchained", "rp"}
NON_THINKING = {"default-fast"}
# Resolve to whichever local LLM is loaded, including text-only models.
DYNAMIC = {"current", "current-fast"}


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


QWEN_PATH = "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"


def _loaded(path: str | None):
    """A fake `_current_loaded_llm_model_ids` for the kobold file at `path` (None: nothing loaded)."""
    ids: set[str] = set()
    if path:
        name = Path(path).name
        ids = {*proxy.USE_LOADED_MODEL_ALIASES, path, name}
        ids |= proxy._role_catalog_ids_for_loaded_path(backend="kobold", loaded_path=path, loaded_model_id=name)

    async def fake(*_args, **_kwargs):
        return set(ids)

    return fake


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
            patch.object(proxy, "_current_loaded_llm_model_ids", _loaded(None)),
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
                if entry["type"] == "language":
                    self.assertEqual("vision" in entry.get("tags", []), "image" in entry["architecture"]["input_modalities"])
                else:
                    self.assertNotIn("tags", entry)

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

    def test_dynamic_current_aliases_claim_only_text_in_text_out(self) -> None:
        # Any local LLM can be the loaded one; cydonia-24b is text-only.
        cydonia = next(m for m in proxy.config["models"] if m["id"] == "cydonia-24b")
        self.assertIsNone(cydonia["mmproj"])
        by_id = {entry["id"]: entry for entry in self._list()["data"]}
        for model_id in DYNAMIC:
            with self.subTest(model=model_id):
                entry = by_id[model_id]
                self.assertEqual(entry["type"], "language")
                self.assertEqual(entry["architecture"], {"input_modalities": ["text"], "output_modalities": ["text"]})
                self.assertNotIn("tags", entry)
                self.assertNotIn("max_tokens", entry)

    def _with_loaded(self, path: str | None) -> None:
        patcher = patch.object(proxy, "_current_loaded_llm_model_ids", _loaded(path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dynamic_current_aliases_inherit_the_loaded_model(self) -> None:
        self._with_loaded(QWEN_PATH)
        listed = self._list()
        by_id = {entry["id"]: entry for entry in listed["data"]}
        for model_id in DYNAMIC:
            with self.subTest(model=model_id):
                entry = by_id[model_id]
                self.assertEqual(entry["type"], "language")
                self.assertEqual(entry["architecture"]["input_modalities"], ["text", "image"])
                self.assertIn("tool-use", entry["tags"])
                self.assertIn("vision", entry["tags"])
                self.assertEqual("reasoning" in entry["tags"], model_id == "current")
                self.assertNotIn("max_tokens", entry)
                # Every endpoint serving the same metadata agrees.
                self.assertEqual(self.client.get(f"/v1/models/{model_id}").json(), entry)
        self.assertEqual(self._list("/openai/v1/models")["data"], listed["data"])

    def test_dynamic_aliases_fall_back_when_the_loaded_model_is_undeclared(self) -> None:
        cydonia = next(m for m in proxy.config["models"] if m["id"] == "cydonia-24b")
        self._with_loaded(cydonia["path"])
        by_id = {entry["id"]: entry for entry in self._list()["data"]}
        for model_id in DYNAMIC:
            with self.subTest(model=model_id):
                self.assertEqual(by_id[model_id]["architecture"]["input_modalities"], ["text"])
                self.assertNotIn("tags", by_id[model_id])

    def test_fast_alias_drops_reasoning_because_of_its_policy_data(self) -> None:
        # Remove the `reasoning_effort: none` overlay from the current-fast
        # record: the alias then inherits `reasoning` like `current`.
        backends = copy.deepcopy(proxy.config["backends"])
        for service in backends.values():
            for record in service.get("models") or []:
                if isinstance(record, dict) and record.get("id") == "current-fast":
                    record.pop("policy_overlay", None)
        self._with_loaded(QWEN_PATH)
        with patch.dict(proxy.config, {"backends": backends}):
            by_id = {entry["id"]: entry for entry in self._list()["data"]}
        self.assertIn("reasoning", by_id["current-fast"]["tags"])

    def test_dynamic_aliases_inherit_a_declared_context_window_when_none_is_observed(self) -> None:
        async def no_litellm(*_args, **_kwargs):
            return None  # LiteLLM down: the gateway lists its own current entries.

        self._with_loaded(QWEN_PATH)
        with patch.object(proxy, "_litellm_models_payload", no_litellm):
            by_id = {entry["id"]: entry for entry in self._list()["data"]}
        # The loaded file serves several records; the smallest declared window is claimed.
        self.assertEqual(by_id["current"]["context_window"], 145408)
        self.assertEqual(by_id["current-fast"]["context_window"], 145408)

    def test_one_loaded_model_lookup_per_list(self) -> None:
        calls = {"n": 0}
        inner = _loaded(QWEN_PATH)

        async def counting(*args, **kwargs):
            calls["n"] += 1
            return await inner(*args, **kwargs)

        with patch.object(proxy, "_current_loaded_llm_model_ids", counting):
            self._list()
        self.assertEqual(calls["n"], 1)

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
        index = asyncio.run(proxy._catalog_model_metadata(model_entries, proxy.ADMIN_STORE.get_service_registry(), []))
        shared = set(index) & set(listed)
        self.assertGreater(len(shared), 20)
        for model_id in shared:
            with self.subTest(model=model_id):
                self.assertEqual(index[model_id], {k: listed[model_id][k] for k in mm.OUTPUT_FIELDS if k in listed[model_id]})

    def test_member_catalog_reports_inherited_values(self) -> None:
        self._with_loaded(QWEN_PATH)
        self.test_member_catalog_reports_the_same_values()
        index = asyncio.run(proxy._catalog_model_metadata(proxy._current_loaded_model_entries(), proxy.ADMIN_STORE.get_service_registry(), []))
        self.assertIn("reasoning", index["current"]["tags"])
        self.assertNotIn("reasoning", index["current-fast"]["tags"])


def _remote_registry(model_count: int = 1) -> dict:
    models = [{"id": "text-embedding-3-small", "upstream_model": "text-embedding-3-small",
               "model_metadata": {"type": "embedding", "input_modalities": ["text"], "output_modalities": ["embedding"]}}]
    models += [{"id": f"chat-{i}", "upstream_model": f"chat-{i}"} for i in range(model_count - 1)]
    return {"services": {"openai-remote": {
        "id": "openai-remote", "kind": "provider", "capabilities": ["openai", "chat"],
        "config": {
            "provider_type": "paid-remote", "provider_id": "openai", "protocol": "openai-compatible",
            "base_url": "https://provider.test/v1", "credential_origin": "https://provider.test:443",
            "api_key_env": "TEST_PROVIDER_KEY", "models": models,
        },
    }}}


class ServiceModelIndexTests(unittest.TestCase):
    def _annotate(self, registry: dict, entries: list[dict], origins: dict) -> mm.MetadataSources:
        from gateway.service_registry import runtime_backends

        with patch.dict(proxy.config, {"backends": runtime_backends(registry)}), \
                patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
            sources = proxy._model_metadata_sources(origins)
            mm.annotate_entries(entries, sources)
        return sources

    def test_remote_declarations_follow_the_public_prefixed_name(self) -> None:
        public = "openai/text-embedding-3-small"
        entries = [{"id": public, "object": "model"}]
        sources = self._annotate(_remote_registry(), entries, {public: mm.ORIGIN_LLM_CATALOG})
        self.assertEqual(entries[0]["type"], "embedding")
        self.assertEqual(entries[0]["architecture"]["output_modalities"], ["embedding"])
        # The raw registry id is not a name the gateway serves for a remote record.
        self.assertIsNone(sources.declared("text-embedding-3-small"))

    def test_annotation_work_does_not_grow_with_model_count(self) -> None:
        calls = {"records": 0}
        original = proxy._service_model_records

        def counting(service_cfg):
            calls["records"] += 1
            return original(service_cfg)

        def run(model_count: int) -> int:
            origins = {f"openai/chat-{i}": mm.ORIGIN_LLM_CATALOG for i in range(model_count - 1)}
            entries = [{"id": model_id, "object": "model"} for model_id in origins]
            calls["records"] = 0
            registry = _remote_registry(model_count)
            registry["services"]["local"] = {"id": "local", "capabilities": ["openai", "chat"], "config": {
                "protocol": "openai-compatible", "base_url": "http://127.0.0.1:9/v1", "models": ["local-chat"]}}
            with patch.object(proxy, "_service_model_records", counting):
                self._annotate(registry, entries, origins)
            self.assertTrue(all(e.get("type") == "language" for e in entries))
            return calls["records"]

        self.assertGreater(run(10), 0)
        self.assertEqual(run(10), run(400))


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
