"""Endpoint tests for the /v1/models metadata contract (docs/api/MODEL_METADATA.md)."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.config_snapshot import derived_patch
from tests.support.access import access_context, authenticated, identity, plan_for  # noqa: E402
from gateway import model_metadata as mm  # noqa: E402
from gateway.models_catalog import build_models_catalog  # noqa: E402
from gateway.capabilities import Capability  # noqa: E402
from tests.support.config_snapshot import section_published

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
        "x_azizam_llm_execution": "litellm",
    }


def _fake_decision_models(*_args, **_kwargs):
    return [
        SimpleNamespace(id=model_id, view=lambda: {"capabilities": {"backend": "decisions", "available": True}})
        for model_id in ("decide", "decider-2b")
    ]


async def _no_context_probe(*_args, **_kwargs):
    return None


QWEN_PATH = "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
OTHER_PATH = "/media/windows/AI Models/LLMs/GGUF/Plain-Text-8B-Q4_K_M.gguf"
LOADED_ID = QWEN_PATH.rsplit("/", 1)[-1]


def _assert_declared_metadata(test: unittest.TestCase, entry: dict, declaration: dict) -> None:
    """Compare fallback fields directly with the effective operator declaration."""
    test.assertIsNotNone(declaration)
    test.assertEqual(entry["type"], declaration.get("type", "language"))
    test.assertEqual(entry["architecture"], {
        "input_modalities": declaration["input_modalities"],
        "output_modalities": declaration.get("output_modalities", ["text"]),
    })
    test.assertEqual(set(entry.get("tags", [])), set(declaration.get("tags", [])))
    if not declaration.get("tags"):
        test.assertNotIn("tags", entry)


def _strip_contract_fields(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    for entry in out.get("data", []):
        for key in mm.OUTPUT_FIELDS:
            entry.pop(key, None)
    return out


def _start_model_list_patches(test: unittest.TestCase, *, real_loaded_state: bool = False) -> None:
    """Serve /v1/models from the snapshot's LiteLLM ids, with nothing loaded."""
    patches = [
        authenticated(),
        patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
        patch.object(proxy, "_litellm_runtime_config", return_value=SimpleNamespace(enabled=True, service_id="litellm")),
        patch.object(proxy, "_service_availability_response", return_value=None),
        patch.object(proxy, "_litellm_models_payload", _fake_litellm_payload),
        patch.object(proxy, "_decision_models_for", _fake_decision_models),
    ]
    if not real_loaded_state:
        patches.extend([
            patch.object(proxy, "_probed_context_window", _no_context_probe),
        ])
    for patcher in patches:
        patcher.start()
        test.addCleanup(patcher.stop)


class ModelListEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _start_model_list_patches(self)

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
        # An engine-backed entry is marked from its metadata `type` (docs/api/MODEL_METADATA.md), so the mark is the
        # one field the metadata pass may change; tests/test_models_availability.py owns it.
        for payload in (with_metadata, without_metadata):
            for entry in payload["data"]:
                entry.pop("azizam_availability", None)
        self.assertEqual(_strip_contract_fields(with_metadata), without_metadata)

    def test_anthropic_list_is_unchanged(self) -> None:
        # Entries without a creation timestamp use the current time. Hold it
        # constant so crossing a second does not change this contract comparison.
        with patch.object(proxy.time, "time", return_value=1_700_000_000):
            with_metadata = self.client.get("/anthropic/v1/models").json()
            with patch.object(mm, "annotate_entries", lambda *_a, **_k: None):
                without_metadata = self.client.get("/anthropic/v1/models").json()
            sdk_shaped = self.client.get("/v1/models", headers={"anthropic-version": "2023-06-01"}).json()
        self.assertEqual(with_metadata, without_metadata)
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

    def test_dynamic_current_aliases_use_declared_fallback_when_unobserved(self) -> None:
        declarations = proxy._model_metadata_sources().declared
        by_id = {entry["id"]: entry for entry in self._list()["data"]}
        for model_id in DYNAMIC:
            with self.subTest(model=model_id):
                entry = by_id[model_id]
                _assert_declared_metadata(self, entry, declarations(model_id))
                self.assertNotIn("max_tokens", entry)

    def test_alias_target_ids_do_not_reveal_hidden_models(self) -> None:
        narrow = authenticated(identity(plan_for("fast-only", models=["fast"])))
        with narrow:
            payload = self._list()
        entries = {entry["id"]: entry for entry in payload["data"]}
        self.assertIn("fast", entries)
        self.assertNotIn("default-fast", entries)
        self.assertEqual(entries["fast"]["azizam_resolves_to"], {})
        # Metadata still resolves against the complete catalog before hiding IDs.
        self.assertEqual(entries["fast"]["architecture"]["input_modalities"], ["text", "image"])
        self.assertIn("vision", entries["fast"]["tags"])

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

    def test_member_catalog_uses_declared_current_metadata(self) -> None:
        # Without a loaded observation the dynamic aliases report the operator's
        # declaration for the slot (proxy_config.yml), and no extra lookup runs.
        registry = proxy.ADMIN_STORE.get_service_registry()
        declarations = proxy._model_metadata_sources(registry=registry).declared
        with patch.object(proxy, "_current_loaded_llm_model_ids", side_effect=AssertionError("extra loaded-state lookup")) as lookup:
            index = proxy._catalog_model_metadata(proxy._current_loaded_model_entries(), registry, [])
        lookup.assert_not_called()
        for alias in DYNAMIC:
            with self.subTest(model=alias):
                _assert_declared_metadata(self, index[alias], declarations(alias))


class RequestLoadedMetadataTests(unittest.TestCase):
    """Keep real health/state readers; count requests at the HTTP transport."""

    def setUp(self) -> None:
        _start_model_list_patches(self, real_loaded_state=True)
        self.requests: list[tuple[str, str]] = []
        self.healthy = {"kobold", "other"}
        self.loaded_paths = {"kobold": QWEN_PATH, "other": OTHER_PATH}
        # A text-only fallback exposes stale inheritance after a backend switch
        # and accidental loaded-state inheritance for catalog-only profiles.
        fallback = {"type": "language", "input_modalities": ["text"],
                    "output_modalities": ["text"], "tags": []}
        declaration = {**fallback, "tags": ["tool-use"]}
        self.backends = {
            "kobold": {"base_url": "http://kobold.test", "health_endpoint": "/health",
                       "priority": 1, "state_type": "llama_cpp",
                       "loaded_model_identity": "path",
                       "context_window_probe": {"endpoint": "/v1/models"},
                       "protocol": "openai-compatible",
                       "capabilities": ["chat", "openai"], "models": [
                           {"id": "private-public", "upstream_model": "loaded-kobold",
                            "activation_model": QWEN_PATH,
                            "model": "deployment-kobold", "context_window": 4096,
                            "model_metadata": copy.deepcopy(declaration)},
                           *[{"id": alias, "context_window": 202752,
                              "model_metadata": copy.deepcopy(fallback)} for alias in sorted(DYNAMIC)],
                       ]},
            "other": {"base_url": "http://other.test", "health_endpoint": "/health",
                      "priority": 2, "state_type": "llama_cpp",
                      "loaded_model_identity": "path",
                      "protocol": "openai-compatible",
                      "capabilities": ["chat", "openai"], "models": [
                          {"id": "plain-text", "upstream_model": "loaded-other",
                           "activation_model": OTHER_PATH, "model": "deployment-other",
                           "model_metadata": {"type": "language", "input_modalities": ["text"],
                                              "output_modalities": ["text"], "tags": ["tool-use"]}},
                      ]},
        }
        self.backends["kobold"]["models"][0]["model_metadata"].update(
            input_modalities=["text", "image"], tags=["vision", "reasoning", "tool-use"])
        registry = {"services": {
            name: {"id": name, "kind": "llm", "capabilities": ["openai", "chat"], "config": record}
            for name, record in self.backends.items()
        }}
        cfg = {**proxy.config, "backends": self.backends, "roles": {}, "models": []}
        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(self._respond)
        patches = [
            patch.object(proxy, "config", cfg),
            section_published("roles", {}),
            patch.object(proxy, "MODELS_CATALOG", {}),
            derived_patch("LLM_BACKEND_HOSTS", {
                name: record["base_url"] for name, record in self.backends.items()}),
            patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry),
            patch.object(proxy, "currently_loaded_model", side_effect=lambda backend: self.loaded_paths.get(backend)),
            patch.object(proxy, "_MODEL_CONTEXT_CACHE", {}),
            patch.object(proxy, "_model_list_served_declarations", proxy._model_list_served_declarations.real),
            patch.object(proxy, "_litellm_models_payload", self._catalog_payload),
            patch.object(proxy.httpx, "AsyncClient",
                         side_effect=lambda *args, **kwargs: original_client(
                             *args, **kwargs, transport=transport)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(proxy.app)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.host, request.url.path))
        backend = request.url.host.removesuffix(".test")
        if request.url.path == "/health":
            return httpx.Response(200 if backend in self.healthy else 503)
        self.assertEqual(request.url.path, "/v1/models")
        return httpx.Response(200, json={"data": [{"id": f"loaded-{backend}", "context_window": 8192}]})

    async def _catalog_payload(self, *args, **kwargs):
        payload = await _fake_litellm_payload(*args, **kwargs)
        payload["data"].extend({"id": model_id, "object": "model", "created": 0}
                               for model_id in (LOADED_ID, "private-public", "plain-text"))
        return payload

    def _payload(self, *, headers: dict | None = None) -> dict:
        response = self.client.get("/v1/models", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _observe(self, *, headers: dict | None = None):
        # The gateway keeps a service's health and resource answers for a few seconds; each
        # observation here is a separate moment in which the service is asked.
        proxy._HEALTH_EVIDENCE_CACHE.clear()
        proxy._RESOURCE_FACT_CACHE.clear()
        self.requests.clear()
        payload = self._payload(headers=headers)
        return {item["id"] for item in payload["data"]}, list(self.requests)

    def _route_current(self) -> proxy.CurrentLoadedLlmTarget:
        context = access_context()
        target, error = asyncio.run(proxy._current_loaded_llm_target(
            context, enforce_gateway_policy=False))
        self.assertIsNone(error, getattr(error, "body", None))
        self.assertIsNotNone(target)
        return target

    def _list(self, path: str = "/v1/models") -> dict[str, dict]:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return {item["id"]: item for item in response.json()["data"]}

    def test_served_id_differs_from_the_declaration_the_activation_path_names(self) -> None:
        target = self._route_current()
        self.assertEqual(target.model, "loaded-kobold")
        self.assertEqual(proxy._roles(), {})
        self.assertEqual(proxy.MODELS_CATALOG, {})
        self.assertNotEqual(self.backends["kobold"]["models"][0]["id"], target.model)
        self.assertNotEqual(self.backends["kobold"]["models"][0]["model"], target.model)
        activation = proxy._configured_litellm_local_model_target("private-public", service_id="kobold")
        self.assertEqual(activation.model, QWEN_PATH)
        # The selector's observation names the declaration through the loaded path.
        self.assertEqual(proxy._declarations_for_loaded_model("kobold", QWEN_PATH), {"private-public"})
        self.assertEqual(asyncio.run(proxy._model_list_served_declarations()), {"private-public"})

    def _served(self, *, headers=None, path="/v1/models"):
        """List the models once, counting selector calls; returns (entries, calls)."""
        real = proxy._first_servable_loaded_llm_target
        calls: list[str] = []

        async def spy(*args, **kwargs):
            calls.append("selector")
            return await real(*args, **kwargs)

        with patch.object(proxy, "_first_servable_loaded_llm_target", spy):
            proxy._HEALTH_EVIDENCE_CACHE.clear()
            proxy._RESOURCE_FACT_CACHE.clear()
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            entries = payload["data"] if "data" in payload else [payload]
            return {entry["id"]: entry for entry in entries}, calls

    def _distinct_metadata(self) -> tuple[dict, dict[str, dict]]:
        """Make retaining either alias's fallback observably wrong on inheritance."""
        source = self.backends["kobold"]["models"][0]["model_metadata"]
        source["max_tokens"] = 8192
        fallbacks = {
            "current": {"type": "embedding", "input_modalities": ["text"],
                        "output_modalities": ["embedding"], "tags": [], "max_tokens": 257},
            "current-fast": {"type": "audio", "input_modalities": ["text"],
                             "output_modalities": ["audio"], "tags": [], "max_tokens": 513},
        }
        for record in self.backends["kobold"]["models"][1:]:
            record["model_metadata"] = copy.deepcopy(fallbacks[record["id"]])
        return copy.deepcopy(source), fallbacks

    def _assert_complete_metadata(self, entry: dict, declaration: dict) -> None:
        _assert_declared_metadata(self, entry, declaration)
        self.assertEqual(entry["max_tokens"], declaration["max_tokens"])

    def test_list_and_retrieve_routes_inherit_complete_metadata_once_per_request(self) -> None:
        source, _ = self._distinct_metadata()
        with patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
            for path, aliases in (
                ("/v1/models", sorted(DYNAMIC)),
                ("/openai/v1/models", sorted(DYNAMIC)),
                ("/v1/models/current", ["current"]),
                ("/v1/models/current-fast", ["current-fast"]),
            ):
                with self.subTest(path=path):
                    listed, calls = self._served(path=path)
                    self.assertEqual(calls, ["selector"])
                    for alias in aliases:
                        self._assert_complete_metadata(listed[alias], source)

    def test_selector_exception_restores_complete_fallback_without_leaking_marker(self) -> None:
        source, fallbacks = self._distinct_metadata()
        with patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
            inherited, calls = self._served()
            self.assertEqual(calls, ["selector"])
            for alias in sorted(DYNAMIC):
                self._assert_complete_metadata(inherited[alias], source)
            marker = "synthetic-private-path-token-METADATA-FAILURE"
            with patch.object(proxy, "_first_servable_loaded_llm_target",
                              side_effect=RuntimeError(marker)) as selector:
                response = self.client.get("/v1/models")
            selector.assert_awaited_once()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(marker, response.text)
        listed = {entry["id"]: entry for entry in response.json()["data"]}
        for alias in sorted(DYNAMIC):
            with self.subTest(alias=alias):
                self._assert_complete_metadata(listed[alias], fallbacks[alias])

    def test_non_path_service_inherits_by_declared_served_id_without_path_lookup(self) -> None:
        source, _ = self._distinct_metadata()
        backend = self.backends["kobold"]
        backend.pop("loaded_model_identity")
        record = backend["models"][0]
        record["upstream_model"] = record["id"]
        record.pop("activation_model")
        record.pop("model")

        async def selected():
            return SimpleNamespace(backend="kobold", model=record["id"]), None

        with patch.object(proxy, "_first_servable_loaded_llm_target", selected), \
                patch.object(proxy, "_policy_turns_reasoning_off", return_value=False), \
                patch.object(proxy, "currently_loaded_model", wraps=proxy.currently_loaded_model) as lookup:
            listed, calls = self._served()
        self.assertEqual(calls, ["selector"])
        self.assertNotIn("kobold", [call.args[0] for call in lookup.call_args_list])
        for alias in sorted(DYNAMIC):
            with self.subTest(alias=alias):
                self._assert_complete_metadata(listed[alias], source)

    def test_identical_served_ids_inherit_only_the_selected_backend_declaration(self) -> None:
        source, _ = self._distinct_metadata()
        other_source = self.backends["other"]["models"][0]["model_metadata"]
        other_source["max_tokens"] = 2048
        expected = {"kobold": source, "other": copy.deepcopy(other_source)}
        for backend in self.backends.values():
            backend.pop("loaded_model_identity")
            record = backend["models"][0]
            record["upstream_model"] = "shared-served-id"
            record.pop("activation_model")
            record.pop("model")
        for backend in ("kobold", "other"):
            with self.subTest(backend=backend):
                async def selected():
                    return SimpleNamespace(backend=backend, model="shared-served-id"), None

                with patch.object(proxy, "_first_servable_loaded_llm_target", selected), \
                        patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
                    listed, calls = self._served()
                self.assertEqual(calls, ["selector"])
                for alias in sorted(DYNAMIC):
                    self._assert_complete_metadata(listed[alias], expected[backend])

    def test_current_fast_only_plan_inherits_complete_metadata_with_one_selector_call(self) -> None:
        source, _ = self._distinct_metadata()
        narrow = authenticated(identity(plan_for("current-fast-only", models=["current-fast"])))
        with narrow, patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
            listed, calls = self._served()
        self.assertIn("current-fast", listed)
        self.assertNotIn("current", listed)
        self.assertEqual(calls, ["selector"])
        self._assert_complete_metadata(listed["current-fast"], source)

    def test_member_catalog_keeps_fallback_while_request_inherits_loaded_metadata(self) -> None:
        source, fallbacks = self._distinct_metadata()
        with patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
            listed, calls = self._served()
        self.assertEqual(calls, ["selector"])
        registry = proxy.ADMIN_STORE.get_service_registry()
        with patch.object(proxy, "_first_servable_loaded_llm_target",
                          side_effect=AssertionError("catalog must not select a loaded model")) as selector:
            index = proxy._catalog_model_metadata(proxy._current_loaded_model_entries(), registry, [])
        selector.assert_not_called()
        for alias in sorted(DYNAMIC):
            with self.subTest(alias=alias):
                self._assert_complete_metadata(listed[alias], source)
                self._assert_complete_metadata(index[alias], fallbacks[alias])

    def test_aliases_inherit_from_the_backend_current_serves(self) -> None:
        listed, calls = self._served()
        self.assertEqual(calls, ["selector"])
        for alias in DYNAMIC:
            with self.subTest(alias=alias):
                self.assertEqual(listed[alias]["architecture"]["input_modalities"], ["text", "image"])
                self.assertIn("vision", listed[alias]["tags"])

    def test_no_inheritance_from_a_backend_the_selector_skips(self) -> None:
        refuse = proxy.openai_error_response("refused", status_code=503, error_type="service_unavailable")
        with patch.object(proxy, "_service_availability_response",
                          side_effect=lambda backend, **_kw: refuse if backend == "kobold" else None):
            listed, calls = self._served()
        self.assertEqual(calls, ["selector"])
        for alias in DYNAMIC:
            with self.subTest(alias=alias):
                self.assertEqual(listed[alias]["architecture"]["input_modalities"], ["text"])
                self.assertNotIn("vision", listed[alias]["tags"])
                self.assertEqual(listed[alias]["tags"], ["tool-use"])

    def test_no_inheritance_from_a_drained_backend(self) -> None:
        self.healthy = {"other"}
        listed, _ = self._served()
        for alias in DYNAMIC:
            with self.subTest(alias=alias):
                self.assertNotIn("vision", listed[alias]["tags"])
                self.assertEqual(listed[alias]["tags"], ["tool-use"])

    def test_an_empty_backend_is_skipped_for_the_next_loaded_one(self) -> None:
        self.loaded_paths["kobold"] = None
        listed, _ = self._served()
        self.assertNotIn("vision", listed["current"]["tags"])

    def test_alias_keeps_its_own_declaration_when_nothing_is_servable(self) -> None:
        self.healthy = set()
        listed, calls = self._served()
        self.assertEqual(calls, ["selector"])
        declarations = proxy._model_metadata_sources().declared
        for alias in DYNAMIC:
            with self.subTest(alias=alias):
                _assert_declared_metadata(self, listed[alias], declarations(alias))

    def test_served_model_with_no_declaration_keeps_the_alias_declaration(self) -> None:
        self.loaded_paths["kobold"] = "/media/windows/AI Models/LLMs/GGUF/Undeclared-24B.gguf"
        listed, _ = self._served()
        declarations = proxy._model_metadata_sources().declared
        _assert_declared_metadata(self, listed["current"], declarations("current"))

    def _list_selecting(self, backend: str, observed: str) -> dict[str, dict]:
        """List the models with the selector reporting `observed` served on `backend`."""
        async def selected():
            return SimpleNamespace(backend=backend, model=observed), None

        with patch.object(proxy, "_first_servable_loaded_llm_target", selected), \
                patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
            return self._list()

    def _assert_aliases(self, listed: dict[str, dict], expected: dict[str, dict]) -> None:
        for alias in sorted(DYNAMIC):
            with self.subTest(alias=alias):
                self._assert_complete_metadata(listed[alias], expected[alias])

    def test_a_path_backend_never_matches_a_declaration_by_its_served_name(self) -> None:
        # The record's deployment serves a name equal to the loaded path, but it activates other
        # weights: only the activation path says which weights are loaded.
        _, fallbacks = self._distinct_metadata()
        record = self.backends["kobold"]["models"][0]
        record["activation_model"] = "/weights/different.gguf"
        record["upstream_model"] = QWEN_PATH
        self.assertEqual(proxy._declarations_for_loaded_model("kobold", QWEN_PATH), set())
        with patch.object(proxy, "_policy_turns_reasoning_off", return_value=False):
            listed, calls = self._served()
        self.assertEqual(calls, ["selector"])
        self._assert_aliases(listed, fallbacks)

    def test_a_path_backend_whose_loaded_path_is_gone_by_the_lookup_keeps_the_fallback(self) -> None:
        # The selector saw a served model; the loaded-state entry is gone when the listing maps it.
        # The served name alone does not prove the weights, so nothing is inherited.
        _, fallbacks = self._distinct_metadata()
        self.loaded_paths.pop("kobold")
        self._assert_aliases(self._list_selecting("kobold", "loaded-kobold"), fallbacks)

    def test_a_non_path_backend_matches_only_the_record_serving_the_observed_id(self) -> None:
        source, fallbacks = self._distinct_metadata()
        backend = self.backends["kobold"]
        backend.pop("loaded_model_identity")
        backend["models"][0].pop("activation_model")
        plain = {"type": "language", "input_modalities": ["text"], "output_modalities": ["text"],
                 "tags": ["tool-use"], "max_tokens": 1024}
        backend["models"].insert(1, {"id": "plain-kobold", "upstream_model": "served-plain",
                                     "model_metadata": copy.deepcopy(plain)})
        cases = (
            ("served-plain", {alias: plain for alias in DYNAMIC}),
            ("loaded-kobold", {alias: source for alias in DYNAMIC}),
            ("not-declared", fallbacks),
        )
        for order in ("as declared", "reversed"):
            if order == "reversed":
                backend["models"][:2] = backend["models"][1::-1]
            for observed, expected in cases:
                with self.subTest(order=order, observed=observed):
                    self._assert_aliases(self._list_selecting("kobold", observed), expected)

    def test_a_record_without_upstream_model_serves_its_deployment_name_not_its_public_id(self) -> None:
        # The deployment compiler asks the engine for `model` when upstream_model is absent.
        source, fallbacks = self._distinct_metadata()
        backend = self.backends["kobold"]
        backend.pop("loaded_model_identity")
        record = backend["models"][0]
        record.pop("activation_model")
        record.pop("upstream_model")
        self.assertEqual(record["model"], "deployment-kobold")
        for observed, expected in (
            ("deployment-kobold", {alias: source for alias in DYNAMIC}),
            ("private-public", fallbacks),
        ):
            with self.subTest(observed=observed):
                self._assert_aliases(self._list_selecting("kobold", observed), expected)

    def test_reasoning_follows_alias_policy_in_both_directions_not_alias_spelling(self) -> None:
        source, _ = self._distinct_metadata()
        for disabled in ("current", "current-fast"):
            with self.subTest(disabled=disabled):
                with patch.object(proxy, "_policy_turns_reasoning_off", lambda model_id: model_id == disabled):
                    listed, calls = self._served()
                self.assertEqual(calls, ["selector"])
                for alias in sorted(DYNAMIC):
                    expected = copy.deepcopy(source)
                    if alias == disabled:
                        expected["tags"].remove("reasoning")
                    self._assert_complete_metadata(listed[alias], expected)

    def test_no_observation_when_the_aliases_are_not_listed(self) -> None:
        hidden = authenticated(identity(plan_for("no-aliases", models=["private-public"])))
        with hidden:
            listed, calls = self._served()
        self.assertNotIn("current", listed)
        self.assertNotIn("current-fast", listed)
        self.assertEqual(calls, [])

    @staticmethod
    async def _no_target():
        return set()

    def test_context_probe_cache_is_honored(self) -> None:
        # The selector's own reads are held out; the context-window annotation
        # still probes once on a cold/expired cache.
        selector = patch.object(proxy, "_model_list_served_declarations", self._no_target)
        selector.start()
        self.addCleanup(selector.stop)
        for alias in self.backends["kobold"]["models"][1:]:
            alias.pop("context_window", None)
        now = time.time()
        for cache in ({}, {"kobold": (now, 8192)}, {"kobold": (now - 31, 8192)}):
            with self.subTest(cache=cache):
                with patch.object(proxy, "_MODEL_CONTEXT_CACHE", copy.deepcopy(cache)):
                    ids, calls = self._observe()
                self.assertEqual(calls, [] if cache and now - cache["kobold"][0] < 30
                                 else [("kobold.test", "/v1/models")])

# What each `/v1/models` type may show as on the Models page. The oracle for
# the agreement tests below: written out here, not imported from the code.
UI_CAPABILITIES_FOR_TYPE = {
    "language": {"chat"}, "image": {"image"}, "video": {"video"},
    "audio": {"music", "sfx", "speech", "transcribe", "audio"},
    "embedding": {"embed"}, "reranking": {"rerank"}, "decision": {"decide"},
}
UI_ICON_FOR_CAPABILITY = {
    "chat": "chat", "image": "image", "video": "video", "music": "music", "sfx": "sfx",
    "speech": "speech", "transcribe": "transcribe", "audio": "sfx", "decide": "decide",
    "embed": "embed", "rerank": "rerank",
}


class ModelsPageAgreesWithModelListTests(unittest.TestCase):
    """The Models page classification and `/v1/models` `type` never disagree for one id."""

    def _catalog_rows(self) -> list[dict]:
        registry = proxy.ADMIN_STORE.get_service_registry()
        context = access_context()
        capability_sets = [proxy._gateway_capabilities_for_auth_context(context)]
        model_entries = [
            *proxy._stable_alias_model_entries(),
            *proxy._build_workflow_model_entries(),
            *proxy._current_loaded_model_entries(),
        ]
        catalog = build_models_catalog(
            model_metadata=proxy._catalog_model_metadata(model_entries, registry, capability_sets),
            alias_views=proxy._gateway_aliases_provider(),
            capability_sets=capability_sets,
            model_entries=model_entries,
            service_records=registry,
            workflow_records=[{"id": r.id, "manifest": r.manifest} for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records()],
        )
        return [*catalog["aliases"], *catalog["models"]]

    def test_every_typed_row_shows_a_capability_of_its_type(self) -> None:
        rows = [row for row in self._catalog_rows() if "type" in row]
        self.assertGreater(len(rows), 10)
        for row in rows:
            with self.subTest(model=row["id"]):
                self.assertIn(row["capability"], UI_CAPABILITIES_FOR_TYPE[row["type"]])
                self.assertEqual(row["icon_key"], UI_ICON_FOR_CAPABILITY[row["capability"]])
                self.assertEqual(row["io"], {
                    "input": row["architecture"]["input_modalities"],
                    "output": row["architecture"]["output_modalities"],
                })

    def test_catalog_rows_have_the_type_v1_models_lists(self) -> None:
        _start_model_list_patches(self)
        response = TestClient(proxy.app).get("/v1/models")
        self.assertEqual(response.status_code, 200, response.text)
        listed = {entry["id"]: entry for entry in response.json()["data"]}
        shared = [row for row in self._catalog_rows() if row["id"] in listed]
        self.assertGreater(len(shared), 10)
        for row in shared:
            with self.subTest(model=row["id"]):
                self.assertEqual(row.get("type"), listed[row["id"]].get("type"))
                if "type" in row:
                    self.assertIn(row["capability"], UI_CAPABILITIES_FOR_TYPE[listed[row["id"]]["type"]])

    def test_a_declared_type_moves_the_row(self) -> None:
        # A chat-serving id declared as an embedding model is shown as one.
        capability = Capability(intent="chat.completions", backend="engine", model="m",
                                input_modalities=("text",), output_modalities=("text",))
        fields = {"type": "embedding", "architecture": {"input_modalities": ["text"], "output_modalities": ["embedding"]}}
        row, = build_models_catalog(capabilities=[capability], model_metadata={"m": fields})["models"]
        self.assertEqual((row["type"], row["capability"], row["icon_key"]), ("embedding", "embed", "embed"))
        self.assertEqual(row["io"], {"input": ["text"], "output": ["embedding"]})


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
