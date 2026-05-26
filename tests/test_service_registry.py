from __future__ import annotations

from pathlib import Path

from gateway import service_registry


def test_protocol_frontends_are_seed_data_not_python_defaults(tmp_path: Path) -> None:
    (tmp_path / "protocol_frontends.yml").write_text(
        """
records:
  - id: test-api
    name: Test API
    protocol: test
    source: builtin
    editable: false
    enabled: true
    routes:
      - /test/v1
    intents:
      - test.run
""",
        encoding="utf-8",
    )

    registry = service_registry.build_service_registry_seed(
        {"registry": {"catalog_dir": str(tmp_path)}, "backends": {}}
    )

    assert sorted(registry["protocol_frontends"]) == ["test-api"]
    assert registry["protocol_frontends"]["test-api"]["routes"] == ["/test/v1"]


def test_protocol_frontend_metadata_is_preserved_for_discovery() -> None:
    registry = service_registry.build_service_registry_seed(
        {
            "protocol_frontends": {
                "custom": {
                    "name": "Custom API",
                    "protocol": "custom",
                    "routes": ["/custom/v1"],
                    "public_discovery": {
                        "compatibility": "custom-compatible",
                        "endpoints": ["/custom/v1/run"],
                    },
                }
            }
        }
    )

    frontend = registry["protocol_frontends"]["custom"]
    assert frontend["public_discovery"]["compatibility"] == "custom-compatible"
    assert frontend["public_discovery"]["endpoints"] == ["/custom/v1/run"]


def test_seed_metadata_backfills_older_builtin_protocol_records() -> None:
    registry = service_registry.backfill_seed_catalog_metadata(
        {
            "services": {},
            "protocol_frontends": {
                "openai": {
                    "id": "openai",
                    "name": "OpenAI-compatible",
                    "protocol": "openai",
                    "routes": ["/openai/v1", "/v1"],
                }
            },
        }
    )

    frontend = registry["protocol_frontends"]["openai"]
    assert frontend["public_discovery"]["compatibility"] == "openai-compatible"
    assert frontend["registered_routes"][0]["canonical"] == "/v1/models"


def test_service_kind_is_data_not_inferred_from_capability_names() -> None:
    registry = service_registry.build_service_registry_seed(
        {
            "backends": {
                "chatty": {
                    "name": "Chatty",
                    "capabilities": ["chat"],
                    "host": "127.0.0.1",
                    "port": 9999,
                }
            }
        }
    )

    service = registry["services"]["chatty"]
    assert service["kind"] == "provider"
    assert service["capabilities"] == ["chat"]
    assert service["source"] == "provisioned"


def test_proxy_config_can_seed_custom_protocol_frontends() -> None:
    registry = service_registry.build_service_registry_seed(
        {
            "protocol_frontends": {
                "custom": {
                    "name": "Custom API",
                    "protocol": "custom",
                    "routes": [{"method": "POST", "path": "/custom/run"}],
                    "intents": ["custom.run"],
                }
            }
        }
    )

    assert registry["protocol_frontends"]["custom"]["routes"] == [
        {"method": "POST", "path": "/custom/run"}
    ]


def test_imported_source_normalizes_read_only_by_default() -> None:
    service = service_registry.normalize_service_record(
        "imported-service",
        {
            "name": "Imported Service",
            "source": "imported",
            "config": {"base_url": "http://127.0.0.1:9999"},
            "provenance": {
                "package_id": "pkg",
                "package_version": "1.0.0",
                "package_hash": "sha256:test",
            },
        },
    )
    frontend = service_registry.normalize_protocol_frontend(
        "imported-api",
        {
            "name": "Imported API",
            "source": "imported",
            "provenance": {
                "package_id": "pkg",
                "package_version": "1.0.0",
                "package_hash": "sha256:test",
            },
        },
    )

    assert service["source"] == "imported"
    assert service["editable"] is False
    assert service["provenance"]["package_hash"] == "sha256:test"
    assert frontend["source"] == "imported"
    assert frontend["editable"] is False
    assert frontend["provenance"]["package_hash"] == "sha256:test"


def test_merge_imported_records_refuses_unrelated_collisions() -> None:
    registry = service_registry.normalize_service_registry(
        {
            "services": {
                "local": {
                    "name": "Local",
                    "source": "ui",
                }
            },
            "protocol_frontends": {},
        }
    )

    try:
        service_registry.merge_imported_records(
            registry,
            services={
                "local": {
                    "name": "Imported Local",
                    "source": "imported",
                    "provenance": {
                        "package_id": "pkg",
                        "package_version": "1.0.0",
                        "package_hash": "sha256:test",
                    },
                }
            },
        )
    except service_registry.ServiceRegistryConflict as exc:
        assert "service 'local' already exists" in str(exc)
    else:
        raise AssertionError("expected service collision to fail")


def test_merge_imported_records_is_idempotent_for_same_package_object() -> None:
    record = {
        "name": "Imported API",
        "source": "imported",
        "protocol": "custom",
        "routes": ["/custom/v1"],
        "provenance": {
            "package_id": "pkg",
            "package_version": "1.0.0",
            "package_hash": "sha256:test",
        },
    }
    registry = service_registry.merge_imported_records(
        {"services": {}, "protocol_frontends": {}},
        protocol_frontends={"custom": record},
    )

    merged = service_registry.merge_imported_records(
        registry,
        protocol_frontends={"custom": record},
    )

    assert merged["protocol_frontends"]["custom"]["routes"] == ["/custom/v1"]


def test_registry_normalizes_workflows_and_capabilities() -> None:
    registry = service_registry.normalize_service_registry(
        {
            "services": {},
            "protocol_frontends": {},
            "workflows": {
                "image.generate.demo": {
                    "source": "imported",
                    "provenance": {
                        "package_id": "pkg",
                        "package_version": "1.0.0",
                        "package_hash": "sha256:test",
                    },
                    "manifest": {
                        "schema_version": 1,
                        "id": "image.generate.demo",
                        "intent": "image.generate",
                        "trusted": True,
                        "inputs": {"prompt": {"type": "string", "required": True}},
                        "outputs": {"image": {"media_type": "image/png"}},
                        "bindings": {
                            "comfyui": {
                                "workflow": "workflows/demo.json",
                                "map": {"prompt": [{"node": "6", "input": "text"}]},
                            }
                        },
                    },
                }
            },
            "capabilities": {
                "demo-capability": {
                    "source": "imported",
                    "intent": "image.generate",
                    "backend": "comfyui",
                    "workflow": "image.generate.demo",
                    "input_modalities": ["text"],
                    "output_modalities": ["image"],
                    "execution": "job",
                    "resources": {"gpu_required": True, "vram_gb_estimate": 8},
                    "provenance": {
                        "package_id": "pkg",
                        "package_version": "1.0.0",
                        "package_hash": "sha256:test",
                    },
                }
            },
        }
    )

    workflow = registry["workflows"]["image.generate.demo"]
    capability = registry["capabilities"]["demo-capability"]

    assert workflow["source"] == "imported"
    assert workflow["summary"]["supported_backends"] == ["comfyui"]
    assert workflow["identity"]["trusted"] is True
    assert capability["source"] == "imported"
    assert capability["resources"]["vram_gb_estimate"] == 8


def test_registry_projects_workflow_and_capability_records() -> None:
    registry = service_registry.normalize_service_registry(
        {
            "services": {},
            "protocol_frontends": {},
            "workflows": {
                "image.generate.demo": {
                    "manifest": {
                        "schema_version": 1,
                        "id": "image.generate.demo",
                        "intent": "image.generate",
                        "inputs": {"prompt": {"type": "string"}},
                        "outputs": {"image": {"media_type": "image/png"}},
                        "bindings": {
                            "comfyui": {
                                "workflow": "workflows/demo.json",
                                "map": {"prompt": [{"node": "6", "input": "text"}]},
                            }
                        },
                    }
                }
            },
            "capabilities": {
                "demo-capability": {
                    "intent": "text.embeddings",
                    "backend": "embeddings",
                    "model": "nomic-embed-text-v1.5",
                    "output_modalities": ["embedding"],
                    "execution": "sync",
                }
            },
        }
    )

    workflows = service_registry.workflow_records_from_registry(registry)
    capabilities = service_registry.capabilities_from_registry(registry)

    assert [record.id for record in workflows] == ["image.generate.demo"]
    assert capabilities[0].intent == "text.embeddings"
    assert capabilities[0].model == "nomic-embed-text-v1.5"


def test_merge_imported_records_includes_workflows_and_capabilities() -> None:
    provenance = {
        "package_id": "pkg",
        "package_version": "1.0.0",
        "package_hash": "sha256:test",
    }
    workflow = {
        "source": "imported",
        "provenance": provenance,
        "manifest": {
            "schema_version": 1,
            "id": "image.generate.demo",
            "intent": "image.generate",
            "inputs": {"prompt": {"type": "string"}},
            "outputs": {"image": {"media_type": "image/png"}},
            "bindings": {
                "comfyui": {
                    "workflow": "workflows/demo.json",
                    "map": {"prompt": [{"node": "6", "input": "text"}]},
                }
            },
        },
    }
    capability = {
        "source": "imported",
        "provenance": provenance,
        "intent": "image.generate",
        "backend": "comfyui",
        "workflow": "image.generate.demo",
        "input_modalities": ["text"],
        "output_modalities": ["image"],
        "execution": "job",
    }

    registry = service_registry.merge_imported_records(
        {"services": {}, "protocol_frontends": {}, "workflows": {}, "capabilities": {}},
        workflows={"image.generate.demo": workflow},
        capabilities={"demo-capability": capability},
    )

    assert "image.generate.demo" in registry["workflows"]
    assert "demo-capability" in registry["capabilities"]


# ---------------------------------------------------------------------------
# resource_summary / list_resource_summaries
# ---------------------------------------------------------------------------

_SAMPLE_SERVICE = {
    "id": "comfyui-local",
    "name": "ComfyUI Local",
    "kind": "comfyui",
    "source": "provisioned",
    "enabled": True,
    "priority": 50,
    "role": "required",
    "capabilities": ["image.generate", "image.edit"],
}


def test_resource_summary_client_hides_admin_fields() -> None:
    s = service_registry.resource_summary(_SAMPLE_SERVICE, audience="client")
    assert s["id"] == "comfyui-local"
    assert s["name"] == "ComfyUI Local"
    assert s["kind"] == "comfyui"
    assert s["enabled"] is True
    assert "source" not in s
    assert "priority" not in s
    assert "role" not in s


def test_resource_summary_admin_includes_extra_fields() -> None:
    s = service_registry.resource_summary(_SAMPLE_SERVICE, audience="admin")
    assert s["source"] == "provisioned"
    assert s["priority"] == 50
    assert s["role"] == "required"


def test_resource_summary_includes_runtime_facts() -> None:
    s = service_registry.resource_summary(
        _SAMPLE_SERVICE,
        audience="client",
        status="degraded",
        queue_depth=3,
        max_queue_depth=10,
    )
    assert s["status"] == "degraded"
    assert s["queue_depth"] == 3
    assert s["max_queue_depth"] == 10


def test_resource_summary_omits_none_runtime_facts() -> None:
    s = service_registry.resource_summary(_SAMPLE_SERVICE, audience="client")
    assert "status" not in s
    assert "queue_depth" not in s
    assert "max_queue_depth" not in s


def test_list_resource_summaries_skips_disabled() -> None:
    registry = {
        "services": {
            "active": {**_SAMPLE_SERVICE, "id": "active", "enabled": True},
            "disabled": {**_SAMPLE_SERVICE, "id": "disabled", "enabled": False},
        }
    }
    results = service_registry.list_resource_summaries(registry, audience="client")
    ids = [r["id"] for r in results]
    assert "active" in ids
    assert "disabled" not in ids


def test_list_resource_summaries_applies_runtime_facts() -> None:
    registry = {
        "services": {
            "svc-a": {**_SAMPLE_SERVICE, "id": "svc-a"},
        }
    }
    results = service_registry.list_resource_summaries(
        registry,
        audience="client",
        runtime_facts={"svc-a": {"status": "available", "queue_depth": 5, "max_queue_depth": 20}},
    )
    assert results[0]["status"] == "available"
    assert results[0]["queue_depth"] == 5


def test_list_resource_summaries_sorted_by_priority() -> None:
    registry = {
        "services": {
            "low": {**_SAMPLE_SERVICE, "id": "low", "priority": 200},
            "high": {**_SAMPLE_SERVICE, "id": "high", "priority": 10},
        }
    }
    results = service_registry.list_resource_summaries(registry, audience="admin")
    ids = [r["id"] for r in results]
    assert ids.index("high") < ids.index("low")
