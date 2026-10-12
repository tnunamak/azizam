from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.capabilities import Capability  # noqa: E402
from gateway.local_capabilities import build_alias_capabilities, build_local_capabilities  # noqa: E402
from gateway.models_catalog import (  # noqa: E402
    auth_contexts_from_user_snapshot,
    build_models_catalog,
    create_models_catalog_router,
    registered_model_capabilities,
    HealthProbeNoResponse,
    probe_service_health,
)


class ModelsCatalogSeedLabelsTests(unittest.TestCase):
    def test_structured_seed_records_have_readable_display_names(self) -> None:
        import yaml

        seed = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
        records = list(seed["backends"].values())
        records.extend(seed["roles"].values())
        records.extend(seed["models"])
        for service in seed["backends"].values():
            records.extend(service.get("models", []))
        for operation in seed["model_aliases"].values():
            records.extend(operation.get("engines", {}).values())
        self.assertTrue(records)
        for index, record in enumerate(records):
            with self.subTest(record=index):
                title = record.get("display_name")
                self.assertIsInstance(title, str)
                self.assertTrue(title.strip())
                self.assertNotIn("\n", title)

    def test_service_member_descriptions_omit_runtime_details(self) -> None:
        import yaml

        seed = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
        records = list(seed["backends"].values())
        for service in seed["backends"].values():
            records.extend(service.get("models", []))
        # Existing role/engine descriptions contain operator facts. They must
        # remain unchanged until the member/operator fields are separated.
        internals = (
            r"(?i)(?:\b(?:gpu\d*|cpu|vram|cuda|systemd|localhost|"
            r"backend|gateway|activation|gguf|q[458]_\w*|kv[-_ ]?cache)\b"
            r"|\.service\b|https?://|/v1/|/media/|:[0-9]{2,5}\b)"
        )
        for index, record in enumerate(records):
            with self.subTest(record=index):
                description = record.get("description")
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                self.assertNotIn("\n", description)
                self.assertNotRegex(description, internals)

    def test_unused_legacy_service_key_is_absent(self) -> None:
        import yaml

        seed = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
        for section in seed.values():
            if isinstance(section, dict):
                self.assertNotIn("kobold_service_legacy", section)


class ModelsCatalogBuildTests(unittest.TestCase):
    def test_catalog_titles_are_data_driven_and_override_repeated_capability_titles(self) -> None:
        model_ids = (
            ("chatterbox-turbo", "speech.synthesis"),
            ("parakeet-tdt-0.6b-v3", "speech.transcription"),
            ("nomic-embed-text-v1.5", "text.embeddings"),
        )
        catalog = build_models_catalog(
            capabilities=[
                Capability(intent=intent, model=model_id, backend="local")
                for model_id, intent in model_ids
            ],
            model_entries=[{"id": model_id} for model_id, _ in model_ids],
            service_records={"services": {"local": {"id": "local", "locality": "local"}}},
        )

        self.assertEqual(
            {row["id"]: row["title"] for row in catalog["models"]},
            {
                "chatterbox-turbo": "Chatterbox Turbo",
                "parakeet-tdt-0.6b-v3": "Parakeet v3",
                "nomic-embed-text-v1.5": "Nomic Embed",
            },
        )

    def test_rows_say_what_each_picture_model_needs_and_makes(self) -> None:
        from gateway.discovery import WorkflowRegistry

        registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
        wanted = {
            "krea2-turbo": ("image.generate", ("text",)),
            "qwen-image-2.1-edit": ("image.edit", ("text", "image")),
            "sdxl-seamless-tile": ("image.generate", ("text",)),
        }
        records = [
            {"id": record.id, "manifest": record.manifest}
            for record in registry.records()
            if record.id.removeprefix(record.intent + ".") in wanted
        ]
        workflow_for = {name: record["id"] for record in records for name in wanted if record["id"].endswith("." + name)}
        catalog = build_models_catalog(
            capabilities=[
                Capability(
                    intent=intent,
                    model=model_id,
                    workflow=workflow_for[model_id],
                    backend="local",
                    input_modalities=inputs,
                    output_modalities=("image",),
                )
                for model_id, (intent, inputs) in wanted.items()
            ],
            model_entries=[{"id": model_id} for model_id in wanted],
            service_records={"services": {"local": {"id": "local", "locality": "local"}}},
            workflow_records=records,
        )
        rows = {row["id"]: row for row in catalog["models"]}
        summary = lambda row: row["capabilities"][0]  # noqa: E731
        self.assertEqual(summary(rows["krea2-turbo"])["intent"], "image.generate")
        self.assertEqual(summary(rows["krea2-turbo"])["input_modalities"], ["text"])
        self.assertEqual(summary(rows["qwen-image-2.1-edit"])["intent"], "image.edit")
        self.assertIn("image", summary(rows["qwen-image-2.1-edit"])["input_modalities"])
        # Texture tiles make pictures from text but are a different task.
        self.assertEqual(rows["sdxl-seamless-tile"]["task"], "texture-tile")
        self.assertNotIn("task", rows["krea2-turbo"])

    def test_registered_models_bind_generic_capabilities_without_legacy_engines(self) -> None:
        services = {"services": {
            "capture-engine": {"enabled": True, "capabilities": ["chat", "completions"],
                               "config": {"models": [{"model_group": "demo-chat", "upstream_model": "private-id"}]}},
            "disabled": {"enabled": False, "capabilities": ["chat"],
                         "config": {"models": [{"model_group": "hidden"}]}},
        }}
        capabilities = registered_model_capabilities(services)
        self.assertEqual({cap.model for cap in capabilities}, {"demo-chat"})
        self.assertEqual({cap.intent for cap in capabilities}, {"chat.completions", "text.completions"})
        self.assertEqual({cap.backend for cap in capabilities}, {"capture-engine"})
        catalog = build_models_catalog(capabilities=capabilities, service_records=services)
        self.assertEqual([row["id"] for row in catalog["models"]], ["demo-chat"])

    def test_registered_local_model_with_missing_file_is_not_available(self) -> None:
        missing = str(ROOT / "tests" / "fixtures" / "missing-model.gguf")
        services = {"services": {
            "capture-engine": {
                "enabled": True,
                "capabilities": ["chat", "completions"],
                "lifecycle": {"driver": "systemd"},
                "config": {
                    "models": [
                        {
                            "model_group": "missing-chat",
                            "upstream_model": "missing-chat.gguf",
                            "activation_model": missing,
                        }
                    ]
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertFalse(capabilities[0].available)
        self.assertEqual(capabilities[0].availability["status"], "unavailable")
        catalog = build_models_catalog(capabilities=capabilities, service_records=services)
        row = catalog["models"][0]
        self.assertEqual(row["id"], "missing-chat")
        self.assertFalse(row["availability"]["available"])
        self.assertEqual(row["availability"]["reason"], "Model file is not installed.")

    def test_registered_local_policy_alias_inherits_missing_base_file(self) -> None:
        missing = str(ROOT / "tests" / "fixtures" / "missing-base.gguf")
        services = {"services": {
            "capture-engine": {
                "enabled": True,
                "capabilities": ["chat"],
                "lifecycle": {"driver": "systemd"},
                "config": {
                    "models": [
                        {
                            "id": "base",
                            "model_group": "base",
                            "upstream_model": "base.gguf",
                            "activation_model": missing,
                        },
                        {
                            "id": "fast",
                            "model_group": "fast",
                            "base_model_id": "base",
                            "upstream_model": "base.gguf",
                        },
                    ]
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        rows = {cap.model: cap for cap in capabilities}
        self.assertFalse(rows["base"].available)
        self.assertFalse(rows["fast"].available)

    def test_present_local_model_file_remains_available(self) -> None:
        present = str(Path(__file__).resolve())
        services = {"services": {
            "capture-engine": {
                "enabled": True,
                "capabilities": ["chat"],
                "lifecycle": {"driver": "systemd"},
                "config": {
                    "models": [
                        {
                            "model_group": "present-chat",
                            "upstream_model": "present-chat.gguf",
                            "activation_model": present,
                        }
                    ]
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertTrue(capabilities[0].available)
        self.assertIsNone(capabilities[0].availability)



    def test_activation_model_on_external_loader_is_not_treated_as_host_file(self) -> None:
        missing = str(ROOT / "tests" / "fixtures" / "container-only.gguf")
        services = {"services": {
            "external-loader": {
                "enabled": True,
                "capabilities": ["chat"],
                "lifecycle": {"driver": "http-load-unload"},
                "config": {"models": [{"model_group": "external-chat", "activation_model": missing}]},
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertTrue(capabilities[0].available)
        self.assertIsNone(capabilities[0].availability)


    def test_backend_model_file_scope_skips_gateway_path_fields(self) -> None:
        missing = str(ROOT / "tests" / "fixtures" / "backend-container.gguf")
        services = {"services": {
            "container-loader": {
                "enabled": True,
                "capabilities": ["chat"],
                "lifecycle": {"driver": "systemd"},
                "config": {
                    "model_file_scope": "backend",
                    "models": [{"model_group": "container-chat", "model_path": missing, "activation_model": missing, "activation_mmproj": missing}],
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertTrue(capabilities[0].available)
        self.assertIsNone(capabilities[0].availability)

    def test_model_file_scope_gateway_opts_activation_file_into_host_check(self) -> None:
        missing = str(ROOT / "tests" / "fixtures" / "scoped-missing.gguf")
        services = {"services": {
            "scoped-loader": {
                "enabled": True,
                "capabilities": ["chat"],
                "config": {
                    "model_file_scope": "gateway",
                    "models": [{"model_group": "scoped-chat", "activation_model": missing}],
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertFalse(capabilities[0].available)
        self.assertEqual(capabilities[0].availability["status"], "unavailable")

    def test_required_companion_model_file_is_checked_and_inherited(self) -> None:
        present = str(Path(__file__).resolve())
        missing = str(ROOT / "tests" / "fixtures" / "missing-mmproj.gguf")
        services = {"services": {
            "vision-loader": {
                "enabled": True,
                "capabilities": ["chat"],
                "config": {
                    "models": [
                        {
                            "id": "vision-base",
                            "model_group": "vision-base",
                            "model_path": present,
                            "required_model_files": [missing],
                        },
                        {"model_group": "vision-public", "base_model_id": "vision-base"},
                    ],
                },
            }
        }}

        rows = {cap.model: cap for cap in registered_model_capabilities(services)}
        self.assertFalse(rows["vision-base"].available)
        self.assertFalse(rows["vision-public"].available)

    def test_remote_provider_models_are_visible_and_priced_from_own_rows(self) -> None:
        services = {"services": {
            "openrouter": {
                "id": "openrouter",
                "kind": "provider",
                "enabled": True,
                "capabilities": ["openai"],
                "config": {
                    "remote_provider": True,
                    "provider_type": "paid-remote",
                    "provider_id": "openrouter",
                    "protocol": "openai-compatible",
                    "api_key_secret_id": "provider:openrouter:api_key",
                    "models": [
                        {"model_group": "openrouter/visible", "upstream_model": "visible-upstream"},
                        {"model_group": "openrouter/disabled", "upstream_model": "disabled-upstream", "enabled": False},
                        {"model_group": "openrouter/other", "upstream_model": "other-upstream"},
                        {"model_group": "openrouter/unpriced", "upstream_model": "unpriced-upstream"},
                    ],
                    "price": {
                        "models": {
                            "openrouter/visible": {
                                "input_usd_per_million_tokens": "3",
                                "output_usd_per_million_tokens": "15",
                            },
                            "openrouter/other": {
                                "input_usd_per_million_tokens": "0.1",
                                "output_usd_per_million_tokens": "0.2",
                            },
                        }
                    },
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertEqual({cap.model for cap in capabilities}, {"openrouter/visible", "openrouter/other", "openrouter/unpriced"})
        self.assertEqual({cap.intent for cap in capabilities}, {"chat.completions"})

        catalog = build_models_catalog(capabilities=capabilities, service_records=services)
        rows = {row["id"]: row for row in catalog["models"]}
        self.assertEqual(set(rows), {"openrouter/visible", "openrouter/other", "openrouter/unpriced"})
        self.assertEqual(rows["openrouter/visible"]["locality"], "remote")
        self.assertEqual(
            rows["openrouter/visible"]["cost"],
            {"kind": "paid", "price": "$3 input / $15 output per 1M tokens"},
        )
        self.assertEqual(
            rows["openrouter/other"]["cost"],
            {"kind": "paid", "price": "$0.1 input / $0.2 output per 1M tokens"},
        )
        self.assertEqual(rows["openrouter/unpriced"]["cost"], {"kind": "paid", "price": None})

    def test_typesafe_protocol_remote_provider_is_not_projected_as_chat(self) -> None:
        services = {"services": {
            "typesafe": {
                "id": "typesafe",
                "kind": "provider",
                "enabled": True,
                "capabilities": ["typesafe-systemone"],
                "config": {
                    "remote_provider": True,
                    "provider_type": "paid-remote",
                    "provider_id": "typesafe",
                    "protocol": "typesafe-systemone",
                    "api_key_secret_id": "provider:typesafe:api_key",
                    "models": [{"model_group": "typesafe/jev"}],
                    "price": {
                        "models": {
                            "typesafe/jev": {
                                "input_usd_per_million_tokens": "0.042",
                                "output_usd_per_million_tokens": "0",
                            }
                        }
                    },
                },
            }
        }}

        capabilities = registered_model_capabilities(services)
        self.assertEqual(capabilities, [])
        catalog = build_models_catalog(capabilities=capabilities, service_records=services)
        self.assertEqual(catalog["models"], [])

    def test_alias_rows_use_target_capability_and_receipt_timing(self) -> None:
        catalog = build_models_catalog(
            alias_views={
                "chat": {
                    "stable": [
                        {"alias": "fast", "resolves_to": "default-fast"},
                    ]
                }
            },
            capabilities=[
                Capability(
                    intent="chat.completions",
                    backend="llm-local",
                    model="fast",
                    input_modalities=("text",),
                    output_modalities=("text",),
                    tags=("alias", "resolves_to:default-fast"),
                ),
                Capability(
                    intent="chat.completions",
                    backend="llm-local",
                    model="default-fast",
                    input_modalities=("text",),
                    output_modalities=("text",),
                    streaming=True,
                ),
            ],
            model_entries=[
                {
                    "id": "default-fast",
                    "description": "Low latency chat model",
                    "provider": "local",
                }
            ],
            service_records={
                "llm-local": {
                    "id": "llm-local",
                    "provider": "local",
                    "locality": "local",
                }
            },
            recent_receipts=[
                {"request_id": "req_1", "requested_model": "fast", "resolved_model": "default-fast", "status": "succeeded", "latency_ms": 1500},
                {"request_id": "req_2", "requested_model": "fast", "resolved_model": "default-fast", "status": "succeeded", "latency_ms": 2500},
            ],
        )

        self.assertEqual(catalog["object"], "models.catalog")
        fast = catalog["aliases"][0]
        self.assertEqual(fast["id"], "fast")
        self.assertEqual(fast["resolves_to"], {"chat": "default-fast"})
        self.assertEqual(fast["capabilities"][0]["intent"], "chat.completions")
        self.assertEqual(fast["description"], "Low latency chat model.")
        self.assertEqual(fast["cost"], {"kind": "free", "price": None})
        self.assertEqual(fast["timing"]["typical_seconds"], 2.0)
        self.assertEqual(fast["timing"]["source"], "receipts")
        self.assertEqual(fast["recent_receipts"][0]["href"], "/usage/?receipt=req_1")
        self.assertFalse(fast["availability"]["available"])
        self.assertEqual(fast["availability"]["status"], "unknown")
        self.assertFalse(fast["availability"]["measured"])
        self.assertIn("/v1/chat/completions", fast["use"]["curl"])

    def test_denied_target_capability_hides_alias(self) -> None:
        catalog = build_models_catalog(
            alias_views={
                "chat": {
                    "stable": [
                        {"alias": "fast", "resolves_to": "default-fast"},
                    ]
                }
            },
            capabilities=[
                Capability(
                    intent="chat.completions",
                    backend="llm-local",
                    model="fast",
                    tags=("alias", "resolves_to:default-fast"),
                ),
                Capability(
                    intent="chat.completions",
                    backend="llm-local",
                    model="default-fast",
                    policy_available=False,
                    policy_reasons=("model 'default-fast' not permitted by profile",),
                ),
            ],
        )

        self.assertEqual(catalog["aliases"], [])

    def test_denied_workflow_public_target_hides_alias(self) -> None:
        views = {"images": {"stable": [{"alias": "image", "resolves_to": "poster"}]}}
        catalog = build_models_catalog(
            alias_views=views,
            capabilities=[
                *build_alias_capabilities(views),
                Capability(intent="image.generate", backend="image-service",
                           workflow="image.poster", policy_available=False),
            ],
            model_entries=[{"id": "poster", "azizam_workflow_id": "image.poster"}],
        )
        self.assertEqual(catalog["aliases"], [])

    def test_alias_operation_does_not_use_wrong_capability(self) -> None:
        catalog = build_models_catalog(
            alias_views={
                "chat": {"stable": [{"alias": "chat", "resolves_to": "dual-model"}]},
                "embeddings": {"stable": [{"alias": "embed", "resolves_to": "dual-model"}]},
            },
            capabilities=[
                Capability(
                    intent="chat.completions",
                    backend="svc",
                    model="dual-model",
                    policy_available=False,
                ),
                Capability(
                    intent="text.embeddings",
                    backend="svc",
                    model="dual-model",
                    input_modalities=("text",),
                    output_modalities=("embedding",),
                ),
            ],
        )

        self.assertEqual([row["id"] for row in catalog["aliases"]], ["embed"])
        self.assertEqual(catalog["aliases"][0]["operations"], ["embeddings"])
        self.assertEqual(catalog["aliases"][0]["capabilities"][0]["intent"], "text.embeddings")

    def test_shared_alias_merges_capabilities_across_operations(self) -> None:
        catalog = build_models_catalog(
            alias_views={
                "chat": {"stable": [{"alias": "omni", "resolves_to": "chat-model"}]},
                "embeddings": {"stable": [{"alias": "omni", "resolves_to": "embed-model"}]},
            },
            capabilities=[
                Capability(
                    intent="chat.completions",
                    backend="svc",
                    model="chat-model",
                    input_modalities=("text",),
                    output_modalities=("text",),
                ),
                Capability(
                    intent="text.embeddings",
                    backend="svc",
                    model="embed-model",
                    input_modalities=("text",),
                    output_modalities=("embedding",),
                ),
            ],
        )

        self.assertEqual(len(catalog["aliases"]), 1)
        alias = catalog["aliases"][0]
        self.assertEqual(alias["id"], "omni")
        self.assertEqual(alias["operations"], ["chat", "embeddings"])
        self.assertEqual(alias["resolves_to"], {"chat": "chat-model", "embeddings": "embed-model"})
        self.assertEqual([cap["intent"] for cap in alias["capabilities"]], ["chat.completions", "text.embeddings"])
        self.assertEqual(alias["io"], {"input": ["text"], "output": ["embedding", "text"]})

    def test_real_alias_producers_keep_speech_and_transcription_without_target_model_cap(self) -> None:
        views = {
            "speech": {
                "stable": [
                    {
                        "alias": "tts",
                        "resolves_to": "voice-engine",
                        "engine": "voice-engine",
                        "service": "voice-service",
                    }
                ]
            },
            "transcriptions": {
                "stable": [
                    {
                        "alias": "stt",
                        "resolves_to": "speech-engine",
                        "engine": "speech-engine",
                        "service": "speech-service",
                    }
                ]
            },
        }
        capabilities = [
            *build_local_capabilities(
                {},
                backends={
                    "voice-service": {"capabilities": ["speech"]},
                    "speech-service": {"capabilities": ["transcriptions"]},
                },
                enabled_flags={
                    "llm": False,
                    "embeddings": False,
                    "tts": True,
                    "stt": True,
                    "images": False,
                    "videos": False,
                    "audio": False,
                },
            ),
            *build_alias_capabilities(views),
        ]

        catalog = build_models_catalog(alias_views=views, capabilities=capabilities)

        self.assertEqual([row["id"] for row in catalog["aliases"]], ["stt", "tts"])
        by_id = {row["id"]: row for row in catalog["aliases"]}
        self.assertEqual(by_id["tts"]["resolves_to"], {"speech": "voice-engine"})
        self.assertEqual(by_id["tts"]["capabilities"][0]["intent"], "speech.synthesis")
        self.assertEqual(by_id["stt"]["resolves_to"], {"transcriptions": "speech-engine"})
        self.assertEqual(by_id["stt"]["capabilities"][0]["intent"], "speech.transcription")

    def test_capability_sets_do_not_flatten_policy_across_profiles(self) -> None:
        catalog = build_models_catalog(
            alias_views={"chat": {"stable": [{"alias": "chat", "resolves_to": "target"}]}},
            capability_sets=[
                [Capability(intent="chat.completions", backend="svc-a", model="other-chat")],
                [Capability(intent="text.embeddings", backend="svc-b", model="target")],
            ],
        )

        self.assertEqual(catalog["aliases"], [])

    def test_workflow_capability_uses_public_model_id(self) -> None:
        catalog = build_models_catalog(
            alias_views={"images": {"stable": [{"alias": "image", "resolves_to": "poster"}]}},
            capabilities=[
                Capability(
                    intent="image.generate",
                    backend="gateway",
                    workflow="image.poster",
                    input_modalities=("text",),
                    output_modalities=("image",),
                ),
            ],
            model_entries=[
                {
                    "id": "poster",
                    "description": "Public poster workflow",
                    "azizam_workflow": True,
                    "azizam_workflow_id": "image.poster",
                    "azizam_intent": "image.generate",
                }
            ],
        )

        self.assertEqual([row["id"] for row in catalog["aliases"]], ["image"])
        self.assertEqual(catalog["models"], [])
        self.assertEqual(catalog["aliases"][0]["resolves_to"], {"images": "poster"})
        self.assertEqual(catalog["aliases"][0]["description"], "Generate or edit images from your prompts.")
        self.assertEqual(catalog["aliases"][0]["technical_description"], "Public poster workflow")

    def test_default_profile_fallback_is_only_for_fresh_or_default_apps(self) -> None:
        contexts = auth_contexts_from_user_snapshot(
            {"apps": []},
            default_profile_id="default-local",
            profile_loader=lambda profile_id: {"id": profile_id},
            context_for_profile=lambda profile, profile_id: (profile, profile_id),
        )
        self.assertEqual(contexts, [({"id": "default-local"}, "default-local")])

        contexts = auth_contexts_from_user_snapshot(
            {"apps": [{"id": "app", "effective_profile_id": "missing-profile"}]},
            default_profile_id="default-local",
            profile_loader=lambda profile_id: {"id": profile_id},
            context_for_profile=lambda profile, profile_id: (profile, profile_id),
        )
        self.assertEqual(contexts, [({"id": "missing-profile"}, "missing-profile")])

        contexts = auth_contexts_from_user_snapshot(
            {"apps": [{"id": "app", "effective_profile_id": "default-local"}]},
            default_profile_id=None,
            profile_loader=lambda profile_id: {"id": profile_id},
            context_for_profile=lambda profile, profile_id: (profile, profile_id),
        )
        self.assertEqual(contexts, [({"id": "default-local"}, "default-local")])

    def test_receipt_timing_deduplicates_keys_and_uses_successful_calls(self) -> None:
        catalog = build_models_catalog(
            capabilities=[Capability(intent="chat.completions", backend="svc", model="model")],
            recent_receipts=[
                {
                    "request_id": "req_ok_same",
                    "requested_model": "model",
                    "resolved_model": "model",
                    "status": "succeeded",
                    "latency_ms": 1000,
                },
                {
                    "request_id": "req_ok_alias",
                    "requested_model": "alias",
                    "resolved_model": "model",
                    "status": "succeeded",
                    "latency_ms": 3000,
                },
                {
                    "request_id": "req_fail",
                    "requested_model": "model",
                    "resolved_model": "model",
                    "status": "failed",
                    "latency_ms": 9000,
                },
            ],
        )

        row = next(row for row in catalog["models"] if row["id"] == "model")
        self.assertEqual(row["timing"]["typical_seconds"], 2.0)
        self.assertEqual(row["timing"]["sample_count"], 2)
        self.assertEqual([receipt["request_id"] for receipt in row["recent_receipts"]], ["req_ok_same", "req_ok_alias", "req_fail"])

    def test_unknown_timing_stays_null(self) -> None:
        catalog = build_models_catalog(
            capabilities=[
                Capability(
                    intent="text.embeddings",
                    backend="emb",
                    model="embed",
                    input_modalities=("text",),
                    output_modalities=("embedding",),
                )
            ],
        )

        embed = next(row for row in catalog["models"] if row["id"] == "embed")
        self.assertEqual(embed["timing"], {"typical_seconds": None, "source": None})
        self.assertEqual(embed["availability"]["reason"], "Not measured yet")
        self.assertEqual(embed["availability"]["status"], "unknown")

    def test_manifest_metadata_supplies_name_description_and_timing(self) -> None:
        catalog = build_models_catalog(
            capabilities=[
                Capability(
                    intent="image.generate",
                    backend="comfyui",
                    workflow="image.poster",
                    input_modalities=("text",),
                    output_modalities=("image",),
                )
            ],
            workflow_records=[
                {
                    "id": "image.poster",
                    "manifest": {
                        "id": "image.poster",
                        "display_name": "Poster image",
                        "description": "A poster workflow",
                        "typical_seconds": 12,
                    },
                }
            ],
        )

        row = next(row for row in catalog["models"] if row["id"] == "image.poster")
        self.assertEqual(row["name"], "Poster image")
        self.assertEqual(row["description"], "Generate or edit images from your prompts.")
        self.assertEqual(row["technical_description"], "A poster workflow")
        self.assertEqual(row["timing"], {"typical_seconds": 12, "source": "configured"})

    def test_declared_audio_subtype_is_used_for_models_catalog(self) -> None:
        catalog = build_models_catalog(
            capabilities=[
                Capability(
                    intent="audio.generate",
                    backend="comfyui",
                    workflow="audio.generate.tangoflux",
                )
            ],
            workflow_records=[
                {
                    "id": "audio.generate.tangoflux",
                    "manifest": {
                        "id": "audio.generate.tangoflux",
                        "capability": "sfx",
                        "display_name": "TangoFlux",
                        "description": "Create a sound effect from a text prompt.",
                    },
                }
            ],
        )

        row = next(
            item for item in catalog["models"]
            if item["id"] == "audio.generate.tangoflux"
        )
        self.assertEqual(row["capabilities"][0]["capability"], "sfx")

    def test_ui_source_is_not_provider_and_cost_stays_unknown_without_locality(self) -> None:
        catalog = build_models_catalog(
            capabilities=[
                Capability(
                    intent="chat.completions",
                    backend="remote-ui-service",
                    model="remote-model",
                )
            ],
            service_records={"remote-ui-service": {"id": "remote-ui-service", "source": "ui"}},
        )

        row = next(row for row in catalog["models"] if row["id"] == "remote-model")
        self.assertEqual(row["provider"], "remote-ui-service")
        self.assertEqual(row["locality"], "unknown")
        self.assertEqual(row["cost"], {"kind": "unknown", "price": None})

    def test_explicit_locality_marks_free(self) -> None:
        catalog = build_models_catalog(
            capabilities=[Capability(intent="chat.completions", backend="svc", model="local-model")],
            service_records={"svc": {"id": "svc", "locality": "local"}},
        )

        row = next(row for row in catalog["models"] if row["id"] == "local-model")
        self.assertEqual(row["locality"], "local")
        self.assertEqual(row["cost"], {"kind": "free", "price": None})

    def test_health_evidence_marks_measured_availability(self) -> None:
        catalog = build_models_catalog(
            capabilities=[Capability(intent="chat.completions", backend="svc", model="m")],
            health_evidence={
                "svc": {
                    "available": True,
                    "reason": "Health endpoint responded",
                    "source": "health",
                }
            },
        )

        row = next(row for row in catalog["models"] if row["id"] == "m")
        self.assertTrue(row["availability"]["available"])
        self.assertEqual(row["availability"]["status"], "ready")
        self.assertTrue(row["availability"]["measured"])
        self.assertEqual(row["availability"]["evidence"]["source"], "health")

    def test_auth_contexts_from_user_snapshot_fail_closed_on_missing_profile(self) -> None:
        contexts = auth_contexts_from_user_snapshot(
            {
                "apps": [
                    {"id": "disabled", "effective_profile_id": "family", "self_service_enabled": False},
                    {"id": "missing", "effective_profile_id": "missing"},
                ]
            },
            default_profile_id=None,
            profile_loader=lambda _profile_id: None,
            context_for_profile=lambda profile, profile_id: (profile, profile_id),
        )

        self.assertEqual(contexts, [])

    def test_auth_contexts_from_user_snapshot_uses_existing_app_profile(self) -> None:
        contexts = auth_contexts_from_user_snapshot(
            {"apps": [{"id": "app", "effective_profile_id": "family"}]},
            default_profile_id=None,
            profile_loader=lambda profile_id: {"id": profile_id},
            context_for_profile=lambda profile, profile_id: (profile, profile_id),
        )

        self.assertEqual(contexts, [({"id": "family"}, "family")])

    def test_service_health_probe_uses_declared_endpoint(self) -> None:
        async def run():
            async def get_json(url: str):
                self.assertEqual(url, "http://svc/health")
                return {"ok": True}

            return await probe_service_health(
                {"services": {"svc": {"config": {"base_url": "http://svc/v1", "health_endpoint": "/health"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertTrue(evidence["svc"]["available"])
        self.assertEqual(evidence["svc"]["source"], "health")


    def test_service_health_probe_preserves_absolute_endpoint_path_against_versioned_base_url(self) -> None:
        async def run():
            async def get_json(url: str):
                self.assertEqual(url, "http://svc/v1/models")
                return {"data": [{"id": "model"}]}

            return await probe_service_health(
                {"services": {"svc": {"config": {"base_url": "http://svc/v1", "health_endpoint": "/v1/models"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertEqual(evidence["svc"]["status"], "unknown")

    def test_service_health_probe_does_not_synthesize_endpoint(self) -> None:
        async def run():
            async def get_json(_url: str):
                raise AssertionError("should not probe without explicit health_endpoint")

            return await probe_service_health(
                {"services": {"svc": {"config": {"base_url": "http://svc/v1"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertIsNone(evidence["svc"]["available"])
        self.assertIsNone(evidence["svc"]["source"])

    def test_service_health_probe_honors_down_status(self) -> None:
        async def run():
            async def get_json(_url: str):
                return {"status": "down"}

            return await probe_service_health(
                {"services": {"svc": {"config": {"health_endpoint": "http://svc/health"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertFalse(evidence["svc"]["available"])
        self.assertEqual(evidence["svc"]["status"], "unavailable")
        self.assertEqual(evidence["svc"]["reason"], "Health endpoint reported unavailable")

    def test_service_health_probe_preserves_degraded_status(self) -> None:
        async def run():
            async def get_json(_url: str):
                return {"status": "degraded"}

            return await probe_service_health(
                {"services": {"svc": {"config": {"health_endpoint": "http://svc/health"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertIsNone(evidence["svc"]["available"])
        self.assertEqual(evidence["svc"]["status"], "degraded")
        self.assertEqual(evidence["svc"]["reason"], "Health endpoint reported degraded")

    def test_service_health_probe_opaque_success_is_unknown(self) -> None:
        async def run():
            async def get_json(_url: str):
                return {"data": [{"id": "model"}]}

            return await probe_service_health(
                {"services": {"svc": {"config": {"health_endpoint": "http://svc/v1/models"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertIsNone(evidence["svc"]["available"])
        self.assertEqual(evidence["svc"]["status"], "unknown")

    def test_public_workflow_model_keeps_manifest_metadata_and_own_receipts(self) -> None:
        catalog = build_models_catalog(
            capabilities=[Capability(intent="image.generate", backend="svc", workflow="image.poster")],
            model_entries=[{"id": "poster", "azizam_workflow_id": "image.poster"}],
            workflow_records=[{"id": "image.poster", "manifest": {
                "id": "image.poster", "display_name": "Poster image",
                "description": "Create a poster", "typical_seconds": 12,
            }}],
            recent_receipts=[{"request_id": "own-image", "resolved_workflow": "image.poster", "status": "succeeded"}],
        )
        row = next(row for row in catalog["models"] if row["id"] == "poster")
        self.assertEqual(row["name"], "Poster image")
        self.assertEqual(row["description"], "Create a poster.")
        self.assertEqual(row["timing"]["typical_seconds"], 12)
        self.assertEqual(row["recent_receipts"][0]["request_id"], "own-image")

    def test_examples_are_protocol_shaped_and_non_json_uploads_have_no_curl(self) -> None:
        catalog = build_models_catalog(
            capabilities=[
                Capability(intent="text.completions", backend="svc", model="completion"),
                Capability(intent="speech.synthesis", backend="svc", model="tts"),
                Capability(intent="image.edit", backend="svc", model="edit"),
                Capability(intent="speech.transcription", backend="svc", model="transcribe"),
                Capability(intent="custom.intent", backend="svc", model="custom"),
            ]
        )
        rows = {row["id"]: row for row in catalog["models"]}

        completion = rows["completion"]
        self.assertEqual(completion["use"]["path"], "/v1/completions")
        self.assertEqual(completion["examples"][0]["body"], {"model": "completion", "prompt": "Hello"})
        self.assertNotIn("messages", completion["use"]["curl"])

        tts = rows["tts"]
        self.assertEqual(tts["use"]["path"], "/v1/audio/speech")
        self.assertEqual(tts["examples"][0]["body"]["voice"], "alloy")

        edit = rows["edit"]
        self.assertEqual(edit["use"]["path"], "/v1/images/edits")
        self.assertEqual(edit["examples"][0]["files"], ["image"])
        self.assertIsNone(edit["use"]["curl"])

        transcribe = rows["transcribe"]
        self.assertEqual(transcribe["use"]["path"], "/v1/audio/transcriptions")
        self.assertEqual(transcribe["examples"][0]["files"], ["file"])
        self.assertIsNone(transcribe["use"]["curl"])

        custom = rows["custom"]
        self.assertIsNone(custom["use"]["path"])
        self.assertIsNone(custom["use"]["curl"])
        self.assertIsNone(custom["use"]["python"])
        self.assertIsNone(custom["use"]["javascript"])
        self.assertIsNone(custom["examples"][0]["body"])


class ModelsCatalogRouterTests(unittest.TestCase):
    def test_router_returns_provider_payload_with_no_store(self) -> None:
        app = FastAPI()
        app.include_router(
            create_models_catalog_router(
                lambda _request: {
                    "schema_version": 1,
                    "object": "models.catalog",
                    "aliases": [],
                    "models": [],
                    "counts": {"aliases": 0, "models": 0, "capabilities": 0},
                }
            )
        )

        response = TestClient(app).get("/me/api/models/catalog")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["object"], "models.catalog")

    def test_router_preserves_auth_error_status(self) -> None:
        class NotAuthenticated(Exception):
            code = "not_authenticated"
            message = "Sign in"
            status_code = 401

        app = FastAPI()
        app.include_router(create_models_catalog_router(lambda _request: (_ for _ in ()).throw(NotAuthenticated())))

        response = TestClient(app).get("/me/api/models/catalog")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")


if __name__ == "__main__":
    unittest.main()


class ModelsCatalogHealthProbeTests(unittest.TestCase):
    def test_service_health_probe_404_missing_route_is_unknown_no_response(self) -> None:
        async def run():
            async def get_json(_url: str):
                raise HealthProbeNoResponse(status_code=404)

            return await probe_service_health(
                {"services": {"svc": {"config": {"health_endpoint": "http://svc/health-not-implemented"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertIsNone(evidence["svc"]["available"])
        self.assertEqual(evidence["svc"]["status"], "unknown")
        self.assertEqual(evidence["svc"]["kind"], "no_response")

    def test_service_health_probe_connection_failure_stays_unavailable(self) -> None:
        async def run():
            async def get_json(_url: str):
                raise OSError("connection refused")

            return await probe_service_health(
                {"services": {"svc": {"config": {"health_endpoint": "http://svc/health"}}}},
                get_json=get_json,
            )

        import asyncio

        evidence = asyncio.run(run())
        self.assertFalse(evidence["svc"]["available"])
        self.assertEqual(evidence["svc"]["status"], "unavailable")


class OperationIntentMappingTests(unittest.TestCase):
    """Every canonical alias operation (gateway/model_metadata.py _OPERATIONS) selects the right capabilities."""

    @staticmethod
    def _caps(*intents: str) -> list[Capability]:
        return [Capability(intent=intent, model="m", backend="local") for intent in intents]

    def _selected(self, operation: str, *intents: str) -> list[str]:
        from gateway.models_catalog import _capabilities_for_operation

        return [cap.intent for cap in _capabilities_for_operation(self._caps(*intents), operation)]

    def test_image_edits_selects_only_the_edit_capability(self) -> None:
        self.assertEqual(self._selected("image_edits", "image.generate", "image.edit"), ["image.edit"])

    def test_videos_covers_generation_and_editing(self) -> None:
        self.assertEqual(self._selected("videos", "video.edit"), ["video.edit"])
        self.assertEqual(self._selected("videos", "video.generate", "video.edit"), ["video.generate", "video.edit"])

    def test_every_canonical_operation_is_recognized(self) -> None:
        from gateway.model_metadata import _OPERATIONS
        from gateway.models_catalog import _intent_prefixes_for_operation

        unmapped = [op for op in _OPERATIONS if not _intent_prefixes_for_operation(op)]
        self.assertEqual(unmapped, [])


def test_two_workflows_with_one_intent_keep_their_exact_names():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="audio.generate", backend="studio", workflow=f"audio.generate.{name}") for name in ("first", "second")],
        model_entries=[{"id": name, "azizam_workflow_id": f"audio.generate.{name}"} for name in ("first", "second")],
        workflow_records=[{"id": f"audio.generate.{name}", "manifest": {"id": f"audio.generate.{name}", "display_name": title, "capability": "music"}} for name, title in (("first", "First music model"), ("second", "Second music model"))],
    )
    rows = {row["id"]: row for row in catalog["models"]}
    assert rows["first"]["title"] == "First music model"
    assert rows["second"]["title"] == "Second music model"
    assert rows["second"]["workflow"] == "audio.generate.second"
