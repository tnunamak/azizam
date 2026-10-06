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
    declarations: set[str] = set()
    if path:
        name = Path(path).name
        ids = {*proxy.USE_LOADED_MODEL_ALIASES, path, name}
        ids |= proxy._role_catalog_ids_for_loaded_path(backend="kobold", loaded_path=path, loaded_model_id=name)
        declarations = proxy._declarations_for_loaded_model("kobold", path)

    async def fake(*_args, **kwargs):
        metadata_ids = kwargs.get("metadata_ids")
        if metadata_ids is not None:
            metadata_ids.update(declarations)
        return set(ids)

    return fake


def _strip_contract_fields(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    for entry in out.get("data", []):
        for key in mm.OUTPUT_FIELDS:
            entry.pop(key, None)
    return out


def _start_model_list_patches(test: unittest.TestCase, *, real_loaded_state: bool = False) -> None:
    """Serve /v1/models from the snapshot's LiteLLM ids, with nothing loaded."""
    patches = [
        patch.object(proxy, "AUTH_ENABLED", False),
        patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
        patch.object(proxy, "_litellm_runtime_config", return_value=SimpleNamespace(enabled=True, service_id="litellm")),
        patch.object(proxy, "_service_availability_response", return_value=None),
        patch.object(proxy, "_litellm_models_payload", _fake_litellm_payload),
        patch.object(proxy, "_decision_models_for", _fake_decision_models),
    ]
    if not real_loaded_state:
        patches.extend([
            patch.object(proxy, "_kobold_context_window", _no_context_probe),
            patch.object(proxy, "_current_loaded_llm_model_ids", _loaded(None)),
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
        visibility = patch.object(proxy, "_profile_target_visibility", return_value="available")
        visibility.start()
        self.addCleanup(visibility.stop)

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

        with patch.object(proxy, "_current_loaded_llm_model_ids", counting), \
                patch.object(proxy, "_profile_target_visibility", return_value="available"):
            self._list()
        self.assertEqual(calls["n"], 1)

    def test_alias_target_ids_do_not_reveal_hidden_models(self) -> None:
        with patch.object(proxy, "_profile_model_allowlist", return_value={"fast"}):
            payload = self._list()
        entries = {entry["id"]: entry for entry in payload["data"]}
        self.assertIn("fast", entries)
        self.assertNotIn("default-fast", entries)
        self.assertEqual(entries["fast"]["vivid_fish_resolves_to"], {})
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

    def test_member_catalog_uses_conservative_current_metadata(self) -> None:
        with patch.object(proxy, "_current_loaded_llm_model_ids", side_effect=AssertionError("extra loaded-state lookup")) as lookup:
            index = proxy._catalog_model_metadata(proxy._current_loaded_model_entries(), proxy.ADMIN_STORE.get_service_registry(), [])
        lookup.assert_not_called()
        self.assertEqual(index["current"]["architecture"]["input_modalities"], ["text"])
        self.assertNotIn("tags", index["current"])
        self.assertEqual(index["current-fast"]["architecture"]["input_modalities"], ["text"])
        self.assertNotIn("tags", index["current-fast"])


class RequestLoadedMetadataTests(unittest.TestCase):
    """Keep real health/state readers; count requests at the HTTP transport."""

    def setUp(self) -> None:
        _start_model_list_patches(self, real_loaded_state=True)
        self.requests: list[tuple[str, str]] = []
        self.active = "tabby"
        declaration = {"type": "language", "input_modalities": ["text"],
                       "output_modalities": ["text"], "tags": ["tool-use"]}
        self.backends = {
            "tabby": {"base_url": "http://tabby.test", "health_endpoint": "/health",
                      "priority": 1, "state_type": "tabbyapi",
                      "protocol": "openai-compatible",
                      "capabilities": ["chat", "openai"], "models": [
                          {"id": "private-public", "upstream_model": "loaded-tabby",
                           "model": "deployment-tabby", "context_window": 4096,
                           "model_metadata": copy.deepcopy(declaration)},
                          *[{"id": alias, "context_window": 202752} for alias in sorted(DYNAMIC)],
                      ]},
            "kobold": {"base_url": "http://kobold.test", "health_endpoint": "/health",
                       "priority": 2, "state_type": "llama_cpp",
                       "protocol": "openai-compatible",
                       "capabilities": ["chat", "openai"], "models": []},
        }
        self.backends["tabby"]["models"][0]["model_metadata"].update(
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
            patch.object(proxy, "ROLES", {}),
            patch.object(proxy, "MODELS_CATALOG", {}),
            patch.object(proxy, "LLM_BACKEND_HOSTS", {
                name: record["base_url"] for name, record in self.backends.items()}),
            patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry),
            patch.object(proxy, "TABBY_HOST", "http://tabby.test"),
            patch.object(proxy, "KOBOLD_HOST", "http://kobold.test"),
            patch.object(proxy, "get_tabby_model_config", return_value=("", "", {})),
            patch.object(proxy, "currently_loaded_kobold_model", return_value=None),
            patch.object(proxy, "_MODEL_CONTEXT_CACHE", {}),
            patch.object(proxy, "_profile_target_visibility", return_value="available"),
            patch.object(proxy, "_profile_model_allowlist", return_value=None),
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
            return httpx.Response(200 if backend == self.active else 503)
        if request.url.path == "/v1/model":
            return httpx.Response(200, json={"id": f"loaded-{backend}"})
        self.assertEqual(request.url.path, "/v1/models")
        return httpx.Response(200, json={"data": [{"id": f"loaded-{backend}", "context_window": 8192}]})

    async def _catalog_payload(self, *args, **kwargs):
        payload = await _fake_litellm_payload(*args, **kwargs)
        payload["data"].extend({"id": model_id, "object": "model", "created": 0}
                               for model_id in ("loaded-tabby", "private-public"))
        return payload

    def _payload(self, *, baseline: bool = False, headers: dict | None = None) -> dict:
        if baseline:
            # Freeze main's filtering and context-probe code, with the same
            # test-owned config and real HTTP readers as the new endpoint.
            namespace = dict(vars(proxy))
            fixture = ROOT / "tests" / "fixtures" / "catalog-main-filtering.py"
            exec(compile(fixture.read_text(), str(fixture), "exec"), namespace)

            async def main_filter(data, auth_context, **_kwargs):
                return await namespace["_filter_model_list_for_profile"](data, auth_context)

            async def main_windows(data, **_kwargs):
                await namespace["_annotate_model_context_windows"](data)

            with patch.object(proxy, "_filter_model_list_for_profile", main_filter), \
                    patch.object(proxy, "_annotate_model_context_windows", main_windows):
                response = self.client.get("/v1/models", headers=headers)
        else:
            response = self.client.get("/v1/models", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _observe(self, *, baseline: bool = False, headers: dict | None = None):
        self.requests.clear()
        payload = self._payload(baseline=baseline, headers=headers)
        return {item["id"] for item in payload["data"]}, list(self.requests)

    def _route_current(self) -> proxy.CurrentLoadedLlmTarget:
        context = proxy._catalog_auth_context_for_profile(proxy.DEFAULT_PROFILE, "default-local")
        target, error = asyncio.run(proxy._current_loaded_llm_target(
            context, enforce_gateway_policy=False))
        self.assertIsNone(error)
        self.assertIsNotNone(target)
        return target

    def _list(self, path: str = "/v1/models") -> dict[str, dict]:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return {item["id"]: item for item in response.json()["data"]}

    def test_backend_change_drops_previous_loaded_metadata(self) -> None:
        self.assertIn("vision", self._list()["current"]["tags"])
        self.active = "kobold"
        with patch.object(proxy, "currently_loaded_kobold_model", return_value=QWEN_PATH) as legacy:
            target = self._route_current()
            listed = self._list()
        self.assertEqual((target.backend, target.model), ("kobold", "loaded-kobold"))
        self.assertEqual(listed["current"]["architecture"]["input_modalities"], ["text"])
        self.assertNotIn("tags", listed["current"])
        self.assertGreaterEqual(legacy.call_count, 1)

    def test_other_backend_preserves_main_availability_without_inheriting_cached_path(self) -> None:
        self.active = "engine"
        self.backends["engine"] = {"base_url": "http://engine.test", "health_endpoint": "/health",
                                   "protocol": "openai-compatible", "capabilities": ["chat"]}
        with patch.dict(proxy.LLM_BACKEND_HOSTS, {"engine": "http://engine.test"}, clear=True), \
                patch.object(proxy, "currently_loaded_kobold_model", return_value=QWEN_PATH):
            baseline_ids, baseline_calls = self._observe(baseline=True)
            ids, calls = self._observe()
            listed = self._list()
        self.assertEqual(ids, baseline_ids)
        self.assertTrue(DYNAMIC <= ids)
        self.assertEqual(calls, baseline_calls)
        self.assertEqual(calls, [("engine.test", "/health")])
        for alias in DYNAMIC:
            self.assertEqual(listed[alias]["architecture"]["input_modalities"], ["text"])
            self.assertNotIn("tags", listed[alias])

    def test_loaded_window_wins_over_static_alias_window(self) -> None:
        listed = self._list()
        for alias in DYNAMIC:
            self.assertEqual(listed[alias]["context_window"], 4096)

    def test_live_upstream_window_wins_over_loaded_declaration(self) -> None:
        async def observed_payload(*args, **kwargs):
            payload = await _fake_litellm_payload(*args, **kwargs)
            for entry in payload["data"]:
                if entry["id"] in DYNAMIC:
                    entry["context_window"] = 8192
            return payload

        with patch.object(proxy, "_litellm_models_payload", observed_payload):
            listed = self._list()
        for alias in DYNAMIC:
            self.assertEqual(listed[alias]["context_window"], 8192)

    def test_registry_activation_model_inherits_without_duplicate_role(self) -> None:
        target = self._route_current()
        self.assertEqual(target.model, "loaded-tabby")
        self.assertEqual(proxy.ROLES, {})
        self.assertEqual(proxy.MODELS_CATALOG, {})
        self.assertNotEqual(self.backends["tabby"]["models"][0]["id"], target.model)
        self.assertNotEqual(self.backends["tabby"]["models"][0]["model"], target.model)
        listed = self._list()
        self.assertEqual(listed["current"]["architecture"]["input_modalities"], ["text", "image"])
        self.assertIn("reasoning", listed["current"]["tags"])
        self.assertNotIn("private-public", listed)
        self.assertIn("loaded-tabby", listed)
        activation = proxy._configured_litellm_local_model_target("private-public", service_id="tabby")
        self.assertEqual(activation.model, target.model)

    def test_probe_calls_and_availability_match_main_over_time(self) -> None:
        for headers in (None, {"anthropic-version": "2023-06-01"}):
            for elapsed in (0, 6, 600):
                with self.subTest(headers=headers, elapsed=elapsed), \
                        patch.object(time, "monotonic", return_value=time.monotonic() + elapsed):
                    baseline_ids, baseline_calls = self._observe(baseline=True, headers=headers)
                    ids, calls = self._observe(headers=headers)
                    self.assertEqual(ids, baseline_ids)
                    self.assertTrue(DYNAMIC | {"loaded-tabby"} <= ids)
                    self.assertNotIn("private-public", ids)
                    self.assertEqual(calls, baseline_calls)
                    self.assertEqual(calls, [("tabby.test", "/health"), ("tabby.test", "/v1/model")])

    def test_hidden_profile_availability_and_probes_match_main_over_time(self) -> None:
        for headers in (None, {"anthropic-version": "2023-06-01"}):
            for visibility in ("available", "catalog"):
                for elapsed in (0, 6, 600):
                    with self.subTest(headers=headers, visibility=visibility, elapsed=elapsed), \
                            patch.object(proxy, "_profile_target_visibility", return_value=visibility), \
                            patch.object(proxy, "_profile_model_allowlist", return_value={"loaded-tabby"}), \
                            patch.object(time, "monotonic", return_value=time.monotonic() + elapsed):
                        baseline_ids, baseline_calls = self._observe(baseline=True, headers=headers)
                        ids, calls = self._observe(headers=headers)
                        self.assertEqual(ids, baseline_ids)
                        self.assertTrue(DYNAMIC.isdisjoint(ids))
                        self.assertIn("loaded-tabby", ids)
                        self.assertEqual(calls, baseline_calls)
                        self.assertEqual(len(calls), 2 if visibility == "available" else 0)

    def test_catalog_profile_has_no_identity_and_keeps_conservative_metadata(self) -> None:
        with patch.object(proxy, "_profile_target_visibility", return_value="catalog"):
            listed = self._list()
        for alias in DYNAMIC:
            self.assertEqual(listed[alias]["architecture"]["input_modalities"], ["text"])
            self.assertNotIn("tags", listed[alias])
        self.assertEqual(self.requests, [])

    def test_existing_context_probe_cache_matches_main(self) -> None:
        # A catalog profile performs no loaded-state lookup, but main's
        # context-window annotation still probes once on a cold/expired cache.
        aliases = self.backends["tabby"]["models"][1:]
        self.backends["tabby"]["models"] = self.backends["tabby"]["models"][:1]
        for alias in aliases:
            alias.pop("context_window", None)
        self.backends["kobold"]["models"] = aliases
        now = time.time()
        for cache in ({}, {"kobold": (now, 8192)}, {"kobold": (now - 31, 8192)}):
            with self.subTest(cache=cache), \
                    patch.object(proxy, "_profile_target_visibility", return_value="catalog"):
                with patch.object(proxy, "_MODEL_CONTEXT_CACHE", copy.deepcopy(cache)):
                    baseline_ids, baseline_calls = self._observe(baseline=True)
                with patch.object(proxy, "_MODEL_CONTEXT_CACHE", copy.deepcopy(cache)):
                    ids, calls = self._observe()
                self.assertEqual(ids, baseline_ids)
                self.assertEqual(calls, baseline_calls)
                self.assertEqual(calls, [] if cache and now - cache["kobold"][0] < 30
                                 else [("kobold.test", "/v1/models")])

    def test_current_model_ids_apply_same_visibility_as_data(self) -> None:
        for headers in (None, {"anthropic-version": "2023-06-01"}):
            with self.subTest(headers=headers), \
                    patch.object(proxy, "_profile_model_allowlist", return_value={"current", "private-public"}):
                payload = self._payload(headers=headers)
            visible_ids = {item["id"] for item in payload["data"]}
            self.assertLessEqual(set(payload["vivid_fish_current_model_ids"]), visible_ids)
            self.assertEqual(payload["vivid_fish_current_model_ids"], ["current"])
            self.assertEqual(payload["vivid_fish_profile_model_allowlist"], ["current"])
            self.assertNotIn("private-public", visible_ids)



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

    def setUp(self) -> None:
        patcher = patch.object(proxy, "_current_loaded_llm_model_ids", _loaded(QWEN_PATH))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _catalog_rows(self) -> list[dict]:
        registry = proxy.ADMIN_STORE.get_service_registry()
        context = proxy._catalog_auth_context_for_profile(proxy.DEFAULT_PROFILE, "default-local")
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
        loaded = patch.object(proxy, "_current_loaded_llm_model_ids", _loaded(QWEN_PATH))
        loaded.start()
        self.addCleanup(loaded.stop)
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
