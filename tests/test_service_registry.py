from __future__ import annotations

import threading
from pathlib import Path

import pytest

from gateway import admin_store, service_registry
from tests.registry_test_helpers import write_service_registry_for_test


def test_shaped_remote_service_rejects_inline_config_key() -> None:
    with pytest.raises(ValueError, match="inline secret fields"):
        service_registry.normalize_service_record("openrouter", {
            "kind": "provider", "config": {
                "provider_type": "paid-remote", "api_key_env": "TEST_PROVIDER_KEY",
                "api_key": "test-inline-secret",
            },
        })


@pytest.mark.parametrize("extra", [
    {"headers": {"Authorization": "test-inline-secret"}},
    {"auth": {"api_key": "test-inline-secret"}},
    {"api_key_ref": "test-inline-secret"},
])
def test_remote_service_rejects_nested_and_non_env_secret_values(extra) -> None:
    with pytest.raises(ValueError, match="inline secret fields"):
        service_registry.normalize_service_record("openrouter", {
            "kind": "provider", "config": {
                "provider_type": "paid-remote", "api_key_env": "TEST_PROVIDER_KEY", **extra,
            },
        })


@pytest.mark.parametrize("config", [
    {"provider_type": "Paid-Remote"},
    {"provider_type": "local", "cost_class": "PAID-REMOTE"},
])
def test_remote_metadata_case_cannot_bypass_policy(config) -> None:
    assert service_registry.is_remote_provider_config(config)


def _run_concurrent_ensure(
    *, proxy_config: dict, path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[dict]:
    original_get = admin_store.get_service_registry_with_generation
    first_reads = threading.Barrier(2)
    read_count = 0
    read_lock = threading.Lock()

    def synchronized_get(*, path: Path) -> dict | None:
        nonlocal read_count
        with read_lock:
            should_wait = read_count < 2
            read_count += 1
        if should_wait:
            first_reads.wait(timeout=5)
        return original_get(path=path)

    monkeypatch.setattr(admin_store, "get_service_registry_with_generation", synchronized_get)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(
                service_registry.ensure_service_registry(
                    proxy_config, path=path, actor="boot"
                )
            )
        except BaseException as exc:  # assertion below makes failures visible
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    return results


def test_concurrent_seed_retries_real_sqlite_cas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    results = _run_concurrent_ensure(
        proxy_config={"registry": {"catalog_dir": str(catalog_dir)}, "backends": {}},
        path=db,
        monkeypatch=monkeypatch,
    )

    assert len(results) == 2
    record = admin_store.get_service_registry_with_generation(path=db)
    assert record is not None
    assert record["operator_settings_generation"] == 1
    assert len(admin_store.list_audit(action_prefix="operator.service_registry.seed", path=db)) == 1


def test_concurrent_backfill_retries_real_sqlite_cas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "services.yml").write_text(
        "records:\n  - id: existing\n    name: Existing\n    source: ui\n    config:\n      seeded_field: seeded-value\n",
        encoding="utf-8",
    )
    write_service_registry_for_test(
        {
            "schema_version": 1,
            "services": {
                "existing": {
                    "id": "existing",
                    "name": "Existing",
                    "source": "ui",
                    "config": {"base_url": "http://127.0.0.1:9000/v1"},
                }
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        },
        path=db,
    )

    proxy_config = {"registry": {"catalog_dir": str(catalog_dir)}, "backends": {}}
    results = _run_concurrent_ensure(
        proxy_config=proxy_config, path=db, monkeypatch=monkeypatch
    )

    assert len(results) == 2
    record = admin_store.get_service_registry_with_generation(path=db)
    assert record is not None
    assert record["operator_settings_generation"] == 2
    assert (
        record["value"]["services"]["existing"]["config"]["seeded_field"]
        == "seeded-value"
    )
    assert len(admin_store.list_audit(action_prefix="operator.service_registry.backfill", path=db)) == 1


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


def test_litellm_substrate_service_is_seed_data() -> None:
    registry = service_registry.build_service_registry_seed({"backends": {}})

    service = registry["services"]["litellm"]
    assert service["kind"] == "llm-substrate"
    assert service["capabilities"] == ["llm.execution"]
    assert service["config"]["execution_substrate"] == "litellm"
    assert service["config"]["base_url_env"] == "AI_GATEWAY_LITELLM_BASE_URL"
    assert service["config"]["default_base_url"] == "http://127.0.0.1:4011"
    assert service["config"]["health_endpoint"] == "/health/liveliness"


def test_litellm_detection_does_not_treat_every_llm_execution_provider_as_substrate() -> None:
    assert not service_registry.is_litellm_service_record(
        "local-openai",
        {
            "id": "local-openai",
            "kind": "provider",
            "capabilities": ["llm.execution"],
            "config": {"protocol": "openai-compatible"},
        },
    )
    assert service_registry.is_litellm_service_record(
        "local-substrate",
        {
            "id": "local-substrate",
            "kind": "provider",
            "capabilities": ["llm.execution"],
            "config": {"execution_substrate": "litellm"},
        },
    )


def test_litellm_substrate_service_backfills_existing_registries() -> None:
    registry = service_registry.backfill_seed_catalog_metadata(
        {
            "services": {"comfyui": {"id": "comfyui", "name": "ComfyUI"}},
            "protocol_frontends": {},
        }
    )

    assert "comfyui" in registry["services"]
    assert registry["services"]["litellm"]["kind"] == "llm-substrate"


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


def test_imported_source_normalizes_editable_for_non_managed_rows() -> None:
    # EDITABILITY LAW: source is provenance/history, not permission.
    # Imported service rows are editable (only managed-substrate is locked).
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
    # Non-managed imported rows are editable under the editability law.
    assert service["editable"] is True
    assert service["provenance"]["package_hash"] == "sha256:test"
    assert frontend["source"] == "imported"
    # Protocol frontends retain their own editable logic (not changed by editability law).
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
    assert s["type"] == "comfyui"
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


# ---------------------------------------------------------------------------
# Single-service CRUD ops (operator add/edit/remove/enable from the UI).
# ---------------------------------------------------------------------------


def _seed_with_provisioned() -> dict:
    return service_registry.build_service_registry_seed(
        {
            "backends": {
                "tabby": {
                    "name": "TabbyAPI",
                    "host": "127.0.0.1",
                    "port": 5050,
                    "capabilities": ["vision", "chat"],
                    "source": "provisioned",
                }
            }
        }
    )


def test_service_form_to_raw_parses_base_url_and_applies_preset() -> None:
    raw = service_registry.service_form_to_raw(
        {
            "name": "My vLLM",
            "preset": "vllm",
            "base_url": "http://10.0.0.5:8000/v1",
            "role": "required",
        }
    )
    assert raw["host"] == "10.0.0.5"
    assert raw["port"] == 8000
    assert raw["base_path"] == "/v1"
    # The vLLM preset supplies default capabilities when the form omits them.
    assert raw["capabilities"] == ["chat", "completions"]
    assert raw["native_passthrough"] is True


def test_service_form_to_raw_keeps_secret_as_env_ref_only() -> None:
    raw = service_registry.service_form_to_raw(
        {"name": "x", "base_url": "http://h:1/v1", "api_key_env": "MY_KEY"}
    )
    assert raw["api_key_env"] == "MY_KEY"
    # The plaintext key is never accepted into the stored config.
    assert "api_key" not in raw


def test_upsert_creates_ui_service_and_rejects_duplicate() -> None:
    reg = _seed_with_provisioned()
    raw = service_registry.service_form_to_raw(
        {"name": "My vLLM", "preset": "vllm", "base_url": "http://h:8000/v1"}
    )
    reg2, rec = service_registry.upsert_service_in_registry(
        reg, service_id="my-vllm", raw=raw, source="ui"
    )
    assert rec["id"] == "my-vllm"
    assert rec["source"] == "ui"
    assert rec["editable"] is True
    assert reg2["services"]["my-vllm"]["config"]["port"] == 8000

    try:
        service_registry.upsert_service_in_registry(
            reg2, service_id="my-vllm", raw=raw
        )
        raise AssertionError("duplicate create should raise")
    except ValueError as exc:
        assert "already exists" in str(exc)


def test_upsert_edit_allows_provisioned_records() -> None:
    # EDITABILITY LAW: provisioned rows are editable — source is provenance,
    # not permission. Only managed-substrate (litellm) rows are locked.
    reg = _seed_with_provisioned()
    raw = service_registry.service_form_to_raw(
        {"name": "TabbyAPI Updated", "base_url": "http://h:5050/v1"}
    )
    # Provisioned records are now editable (editability law).
    assert reg["services"]["tabby"]["editable"] is True
    reg2, rec = service_registry.upsert_service_in_registry(
        reg, service_id="tabby", raw=raw, replace=True
    )
    assert rec["id"] == "tabby"
    assert rec["name"] == "TabbyAPI Updated"
    # Source is preserved from the existing record (provenance history).
    assert rec["source"] == "provisioned"


def test_upsert_edit_rejects_managed_substrate_host_changes() -> None:
    # Managed-substrate rows (litellm) block endpoint/protocol changes.
    reg = service_registry.build_service_registry_seed({"backends": {}})
    assert service_registry.is_litellm_service_record("litellm", reg["services"]["litellm"])
    raw = service_registry.service_form_to_raw(
        {"name": "LiteLLM", "base_url": "http://127.0.0.1:9999/v1"}
    )
    try:
        service_registry.upsert_service_in_registry(
            reg, service_id="litellm", raw=raw, replace=True
        )
        raise AssertionError("editing managed substrate host should raise")
    except ValueError as exc:
        assert "managed substrate" in str(exc)


def test_upsert_edit_allows_managed_substrate_rename() -> None:
    # Managed-substrate rows allow name, enabled, note changes.
    reg = service_registry.build_service_registry_seed({"backends": {}})
    raw = {"name": "LiteLLM Renamed", "enabled": True}
    reg2, rec = service_registry.upsert_service_in_registry(
        reg, service_id="litellm", raw=raw, replace=True
    )
    assert rec["name"] == "LiteLLM Renamed"
    # Config is preserved from existing record.
    assert rec["config"]["execution_substrate"] == "litellm"


def test_upsert_edit_persists_managed_note_in_canonical_config() -> None:
    """Managed metadata PATCH stores note in config without losing config data."""
    reg = service_registry.build_service_registry_seed({"backends": {}})
    original_config = dict(reg["services"]["litellm"]["config"])

    reg2, rec = service_registry.upsert_service_in_registry(
        reg,
        service_id="litellm",
        raw={"note": "Operator-owned note"},
        replace=True,
    )

    assert rec["config"]["note"] == "Operator-owned note"
    assert reg2["services"]["litellm"]["config"]["note"] == "Operator-owned note"
    assert rec["config"] == {**original_config, "note": "Operator-owned note"}
    assert "note" not in {key for key in rec if key != "config"}


def test_remove_allows_any_non_managed_source() -> None:
    # EDITABILITY LAW: all sources are removable except managed-substrate.
    # provisioned rows (like tabby) can now be removed.
    reg = _seed_with_provisioned()
    raw = service_registry.service_form_to_raw(
        {"name": "Mine", "base_url": "http://h:9/v1"}
    )
    reg, _ = service_registry.upsert_service_in_registry(
        reg, service_id="mine", raw=raw, source="ui"
    )
    # UI row is removable.
    reg = service_registry.remove_service_from_registry(reg, "mine")
    assert "mine" not in reg["services"]

    # Provisioned row is also removable (it won't resurrect on boot).
    reg = service_registry.remove_service_from_registry(reg, "tabby")
    assert "tabby" not in reg["services"]


def test_remove_blocks_managed_substrate() -> None:
    # Only managed-substrate rows (litellm) are blocked from removal.
    reg = service_registry.build_service_registry_seed({"backends": {}})
    assert "litellm" in reg["services"]
    try:
        service_registry.remove_service_from_registry(reg, "litellm")
        raise AssertionError("deleting managed substrate should raise")
    except ValueError as exc:
        assert "managed substrate" in str(exc)


def test_set_service_enabled_toggles_any_source() -> None:
    reg = _seed_with_provisioned()
    reg = service_registry.set_service_enabled(reg, "tabby", False)
    assert reg["services"]["tabby"]["enabled"] is False
    reg = service_registry.set_service_enabled(reg, "tabby", True)
    assert reg["services"]["tabby"]["enabled"] is True


def test_provider_presets_have_a_custom_escape_hatch() -> None:
    ids = [p["id"] for p in service_registry.PROVIDER_PRESETS]
    assert "openai-compatible" in ids
    assert service_registry.provider_preset("vllm")["default_port"] == 8000
    assert service_registry.provider_preset("nope") is None


# ---------------------------------------------------------------------------
# Model/deployment mapping: an OpenAI-compatible service added via the form
# must produce a routable LiteLLM model_list entry (the SLVP gap fix).
# ---------------------------------------------------------------------------


def test_openai_compatible_preset_sets_routing_protocol() -> None:
    raw = service_registry.service_form_to_raw(
        {"name": "tgw", "preset": "openai-compatible", "base_url": "http://h:7860/v1"}
    )
    assert raw["protocol"] == "openai-compatible"


def test_form_models_become_deployment_records() -> None:
    raw = service_registry.service_form_to_raw(
        {
            "name": "tgw",
            "preset": "openai-compatible",
            "base_url": "http://h:7860/v1",
            "models": [{"public": "my-llama", "upstream": "TheBloke_Llama"}],
        }
    )
    assert raw["models"] == [
        {"model_group": "my-llama", "upstream_model": "TheBloke_Llama"}
    ]


def test_form_model_row_defaults_public_to_upstream() -> None:
    raw = service_registry.service_form_to_raw(
        {
            "name": "tgw",
            "preset": "openai-compatible",
            "base_url": "http://h:7860/v1",
            "models": [{"upstream": "only-upstream"}, {"public": "only-public"}],
        }
    )
    assert raw["models"] == [
        {"model_group": "only-upstream", "upstream_model": "only-upstream"},
        {"model_group": "only-public", "upstream_model": "only-public"},
    ]


def test_added_openai_service_with_model_is_routable() -> None:
    # The end-to-end data proof: form -> service record -> a real LiteLLM
    # model_list entry. (Before the fix this list was empty -> inert service.)
    from gateway import litellm_substrate

    raw = service_registry.service_form_to_raw(
        {
            "name": "text-generation-webui",
            "preset": "openai-compatible",
            "base_url": "http://127.0.0.1:7860/v1",
            "capabilities": ["chat", "completions"],
            "models": [{"public": "my-llama", "upstream": "TheBloke_Llama-3-8B"}],
        }
    )
    svc = service_registry.service_from_backend_config(
        "text-generation-webui", raw, source="ui"
    )
    registry = {
        "schema_version": 1,
        "services": {"text-generation-webui": svc},
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }
    compiled = litellm_substrate.compile_litellm_config_from_registry(registry)
    model_list = compiled["model_list"]
    assert len(model_list) == 1
    entry = model_list[0]
    assert entry["model_name"] == "my-llama"
    assert entry["litellm_params"]["model"] == "openai/TheBloke_Llama-3-8B"
    assert entry["litellm_params"]["api_base"] == "http://127.0.0.1:7860/v1"


def test_added_service_without_models_is_inert() -> None:
    # Documents the boundary: no model mapping -> no routable model. (This is
    # the gap the form's Models section fixes; an operator must add a mapping.)
    from gateway import litellm_substrate

    raw = service_registry.service_form_to_raw(
        {"name": "bare", "preset": "openai-compatible", "base_url": "http://h:7860/v1"}
    )
    svc = service_registry.service_from_backend_config("bare", raw, source="ui")
    registry = {
        "schema_version": 1,
        "services": {"bare": svc},
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }
    compiled = litellm_substrate.compile_litellm_config_from_registry(registry)
    assert compiled["model_list"] == []


def test_remote_provider_requires_env_key_reference() -> None:
    with pytest.raises(ValueError, match="requires api_key_env"):
        service_registry.service_from_backend_config(
            "openrouter",
            {
                "kind": "provider",
                "provider_id": "openrouter",
                "provider_type": "paid-remote",
                "protocol": "openai-compatible",
                "base_url": "https://openrouter.ai/api/v1",
                "models": [{"id": "openai/gpt-4.1-mini"}],
            },
            source="ui",
        )


def test_remote_provider_rejects_inline_secret_value() -> None:
    with pytest.raises(ValueError, match="inline secret fields"):
        service_registry.service_from_backend_config(
            "openrouter",
            {
                "kind": "provider",
                "provider_id": "openrouter",
                "provider_type": "paid-remote",
                "protocol": "openai-compatible",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-should-not-be-stored",
                "api_key_env": "OPENROUTER_API_KEY",
                "models": [{"id": "openai/gpt-4.1-mini"}],
            },
            source="ui",
        )


# ---------------------------------------------------------------------------
# Fresh-install reproducibility: a nested lifecycle block in proxy_config.yml
# (driver + driver_config) must survive first-boot seeding so the swap path is
# real for a brand-new operator — not just on a hand-seeded DB.
# ---------------------------------------------------------------------------


def test_nested_lifecycle_driver_survives_first_boot_seed() -> None:
    """A backend declaring lifecycle.{driver,driver_config} in proxy_config.yml
    must seed through build_service_registry_seed -> normalize -> runtime_backends
    with the driver config intact, so driver_for_service resolves the right
    driver on a fresh install.

    Regression guard for the silent-degradation bug: service_from_backend_config
    used to assume lifecycle was a scalar string and str()'d a nested dict into
    `type`, destroying driver/driver_config — which silently degraded every swap
    to ExternalDriver (a no-op) on any install that wasn't hand-seeded.
    """
    from gateway.lifecycle_drivers import driver_for_service

    proxy_config = {
        "backends": {
            "kobold": {
                "host": "127.0.0.1",
                "port": 5051,
                "lifecycle": {
                    "type": "systemd",
                    "managed": True,
                    "service_name": "llama-bee.service",
                    "driver": "systemd",
                    "driver_config": {
                        "unit": "llama-bee.service",
                        "activation_env_keys": {"model": "LLAMA_BEE_MODEL"},
                        "precheck": "gguf_path",
                    },
                },
            },
            "tabby": {
                "host": "127.0.0.1",
                "port": 5050,
                "lifecycle": {
                    "driver": "http-load-unload",
                    "driver_config": {
                        "load_url": "/v1/model/load",
                        "unload_url": "/v1/model/unload",
                    },
                },
            },
        }
    }

    registry = service_registry.normalize_service_registry(
        service_registry.build_service_registry_seed(proxy_config)
    )
    backends = service_registry.runtime_backends(registry)

    # driver + driver_config preserved through the whole first-boot pipeline
    kobold_lc = backends["kobold"]["lifecycle"]
    assert kobold_lc["driver"] == "systemd"
    assert kobold_lc["driver_config"]["activation_env_keys"] == {"model": "LLAMA_BEE_MODEL"}
    assert kobold_lc["driver_config"]["precheck"] == "gguf_path"

    tabby_lc = backends["tabby"]["lifecycle"]
    assert tabby_lc["driver"] == "http-load-unload"
    assert tabby_lc["driver_config"]["load_url"] == "/v1/model/load"

    # ...and the resolver picks the right driver from the seeded backends
    assert type(driver_for_service("kobold", backends)).__name__ == "SystemdDriver"
    assert type(driver_for_service("tabby", backends)).__name__ == "HttpLoadUnloadDriver"


# ---------------------------------------------------------------------------
# Editability law: ensure_service_registry / backfill_seed_catalog_metadata
# ---------------------------------------------------------------------------


def test_remove_config_sourced_service_not_resurrected_on_reboot(tmp_path: Path) -> None:
    """After removal, a second ensure_service_registry call must NOT resurrect the row.

    The DB registry is the source of truth after the one-time seed. Removing a
    config-imported row marks the DB as intentional; YAML is never re-merged.
    """
    proxy_config = {
        "backends": {
            "my-vllm": {
                "name": "My vLLM",
                "host": "127.0.0.1",
                "port": 8000,
                "capabilities": ["chat"],
            }
        }
    }
    db = tmp_path / "admin.db"
    admin_store.init_db(db)

    # First boot: seeds the registry.
    registry = service_registry.ensure_service_registry(proxy_config, path=db)
    assert "my-vllm" in registry["services"]

    # Operator removes the service.
    updated = service_registry.remove_service_from_registry(registry, "my-vllm")
    write_service_registry_for_test(
        updated,
        updated_by="test",
        path=db,
    )

    # Second boot with the same proxy_config: must NOT re-add the removed row.
    registry2 = service_registry.ensure_service_registry(proxy_config, path=db)
    assert "my-vllm" not in registry2["services"], (
        "ensure_service_registry must not resurrect a removed service"
    )


def test_existing_registry_model_rows_are_not_silently_upgraded_from_yaml(tmp_path: Path) -> None:
    """Old DB shapes stay DB-owned and must fail closed until migrated."""
    old_config = {
        "backends": {
            "kobold": {
                "models": [
                    {"id": "default", "model_group": "default", "upstream_model": "base.gguf"},
                    {"id": "default-fast", "model_group": "default-fast", "upstream_model": "base.gguf"},
                ]
            }
        }
    }
    new_config = {
        "backends": {
            "kobold": {
                "models": [
                    {"id": "default", "model_group": "default", "upstream_model": "base.gguf"},
                    {"id": "current", "model_group": "current", "upstream_model": "base.gguf", "base_model_id": "default"},
                ]
            }
        }
    }
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    old_registry = service_registry.build_service_registry_seed(old_config)
    write_service_registry_for_test(
        old_registry,
        updated_by="test",
        path=db,
    )

    reloaded = service_registry.ensure_service_registry(new_config, path=db)

    config = reloaded["services"]["kobold"]["config"]
    assert [model["id"] for model in config["models"]] == ["default", "default-fast"]
    assert "model_policy_schema_version" not in config


def test_backfill_seed_catalog_metadata_never_overwrites_operator_edits() -> None:
    """backfill_seed_catalog_metadata fills missing fields only, never overwrites.

    An operator rename (e.g. the litellm service) must survive a boot that
    calls backfill_seed_catalog_metadata.
    """
    # Simulate an existing DB registry where the operator renamed litellm.
    existing_registry = {
        "schema_version": 1,
        "services": {
            "litellm": {
                "id": "litellm",
                "name": "My Custom LiteLLM Name",  # operator edit
                "kind": "llm-substrate",
                "source": "builtin",
                "editable": False,
                "enabled": True,
                "priority": 10,
                "role": "required",
                "capabilities": ["llm.execution"],
                "config": {
                    "execution_substrate": "litellm",
                    "protocol": "litellm",
                    "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL",
                    "default_base_url": "http://127.0.0.1:4011",
                },
                "secret_refs": {},
                "lifecycle": {"type": "external", "managed": False, "service_name": "litellm"},
                "provenance": {},
            }
        },
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }

    result = service_registry.backfill_seed_catalog_metadata(existing_registry, proxy_config={})

    # The operator's custom name must be preserved.
    assert result["services"]["litellm"]["name"] == "My Custom LiteLLM Name", (
        "backfill must not overwrite operator-edited name"
    )
    # Missing bundled config fields should still be added.
    assert (
        result["services"]["litellm"]["config"]["health_endpoint"]
        == "/health/liveliness"
    )


def test_backfill_seed_catalog_metadata_skips_different_source_records() -> None:
    """A user-owned row with a seed-colliding id must not inherit builtin data."""
    existing_registry = {
        "schema_version": 1,
        "services": {
            "litellm": {
                "id": "litellm",
                "name": "User-owned service named litellm",
                "kind": "provider",
                "source": "ui",
                "enabled": True,
                "config": {"base_url": "http://example.invalid"},
            }
        },
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }

    result = service_registry.backfill_seed_catalog_metadata(existing_registry, proxy_config={})

    service = result["services"]["litellm"]
    assert service["source"] == "ui"
    assert service["kind"] == "provider"
    assert service["config"] == {"base_url": "http://example.invalid"}


def test_ensure_service_registry_persists_seed_catalog_backfill(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    existing_registry = {
        "schema_version": 1,
        "services": {
            "litellm": {
                "id": "litellm",
                "name": "LiteLLM",
                "kind": "llm-substrate",
                "source": "builtin",
                "editable": False,
                "enabled": True,
                "priority": 10,
                "role": "required",
                "capabilities": ["llm.execution"],
                "config": {
                    "execution_substrate": "litellm",
                    "protocol": "litellm",
                    "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL",
                    "default_base_url": "http://127.0.0.1:4011",
                },
                "secret_refs": {},
                "lifecycle": {"type": "external", "managed": False, "service_name": "litellm"},
                "provenance": {},
            }
        },
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }
    write_service_registry_for_test(
        existing_registry,
        updated_by="test",
        path=db,
    )

    result = service_registry.ensure_service_registry({}, path=db, actor="startup")
    stored = admin_store.get_operator_setting(
        service_registry.SERVICE_REGISTRY_SETTING_KEY,
        path=db,
    )["value"]

    assert result["services"]["litellm"]["config"]["health_endpoint"] == "/health/liveliness"
    assert stored["services"]["litellm"]["config"]["health_endpoint"] == "/health/liveliness"


def test_models_list_round_trips_through_form_to_raw_to_litellm(tmp_path: Path) -> None:
    """form models list -> service_form_to_raw -> upsert -> litellm compilation.

    This is the full round-trip proof: a PATCH body with a models list must
    produce a routable LiteLLM model_list entry (the PATCH models acceptance test).
    """
    from gateway import litellm_substrate

    # Simulate what the PATCH route does: form -> raw -> upsert.
    form = {
        "name": "my-tgw",
        "preset": "openai-compatible",
        "base_url": "http://127.0.0.1:7860/v1",
        "models": [
            {"public": "llama-3-8b", "upstream": "Meta-Llama-3-8B-Instruct"},
            {"public": "llama-3-70b", "upstream": "Meta-Llama-3-70B-Instruct"},
        ],
    }
    raw = service_registry.service_form_to_raw(form)
    assert raw["models"] == [
        {"model_group": "llama-3-8b", "upstream_model": "Meta-Llama-3-8B-Instruct"},
        {"model_group": "llama-3-70b", "upstream_model": "Meta-Llama-3-70B-Instruct"},
    ]

    # Upsert into registry.
    seed = service_registry.build_service_registry_seed({"backends": {}})
    registry, record = service_registry.upsert_service_in_registry(
        seed, service_id="my-tgw", raw=raw, source="ui"
    )

    # Registry normalization assigns stable ids even when a legacy form row
    # only supplied public/upstream names.
    assert record["config"]["models"] == [
        {"id": "llama-3-8b", **raw["models"][0]},
        {"id": "llama-3-70b", **raw["models"][1]},
    ]

    # LiteLLM compilation produces one entry per model mapping.
    compiled = litellm_substrate.compile_litellm_config_from_registry(registry)
    model_names = [e["model_name"] for e in compiled["model_list"]]
    assert "llama-3-8b" in model_names
    assert "llama-3-70b" in model_names


def test_litellm_service_is_editable_false_managed_substrate() -> None:
    """litellm is the only editable=False record after the editability law."""
    registry = service_registry.build_service_registry_seed({"backends": {}})
    litellm_svc = registry["services"]["litellm"]
    assert litellm_svc["editable"] is False
    assert service_registry.is_litellm_service_record("litellm", litellm_svc)


def test_provisioned_service_is_editable_true_after_editability_law() -> None:
    """Provisioned rows are editable=True — source is provenance, not permission."""
    reg = _seed_with_provisioned()
    assert reg["services"]["tabby"]["source"] == "provisioned"
    assert reg["services"]["tabby"]["editable"] is True


def test_provider_presets_custom_is_first() -> None:
    """The custom/openai-compatible preset must be the first entry."""
    presets = service_registry.PROVIDER_PRESETS
    assert presets[0]["id"] == "openai-compatible"
    assert presets[0]["label"].lower().startswith("openai")


# ---------------------------------------------------------------------------
# service_form_to_raw — timeout, resources, note round-trip
# ---------------------------------------------------------------------------

def test_service_form_to_raw_preserves_timeout() -> None:
    """timeout field is accepted and stored as an integer."""
    raw = service_registry.service_form_to_raw(
        {"name": "x", "base_url": "http://h:1/v1", "timeout": 120}
    )
    assert raw["timeout"] == 120


def test_service_form_to_raw_preserves_timeout_string() -> None:
    """timeout can arrive as a string (form submission) and is coerced to int."""
    raw = service_registry.service_form_to_raw(
        {"name": "x", "base_url": "http://h:1/v1", "timeout": "60"}
    )
    assert raw["timeout"] == 60


def test_service_form_to_raw_preserves_resources() -> None:
    """resources dict (gpu_required, max_parallel, vram_gb_estimate, duration_class) round-trips."""
    raw = service_registry.service_form_to_raw(
        {
            "name": "embeddings",
            "base_url": "http://127.0.0.1:5061/v1",
            "resources": {
                "gpu_required": False,
                "max_parallel": 4,
                "vram_gb_estimate": 0,
                "duration_class": "interactive",
            },
        }
    )
    assert raw["resources"] == {
        "gpu_required": False,
        "max_parallel": 4,
        "vram_gb_estimate": 0,
        "duration_class": "interactive",
    }


def test_service_form_to_raw_preserves_health_endpoint() -> None:
    """health_endpoint is passed through explicitly."""
    raw = service_registry.service_form_to_raw(
        {"name": "x", "base_url": "http://h:1/v1", "health_endpoint": "/health"}
    )
    assert raw["health_endpoint"] == "/health"


def test_service_form_to_raw_preserves_note() -> None:
    """note is passed through as a string."""
    raw = service_registry.service_form_to_raw(
        {"name": "x", "base_url": "http://h:1/v1", "note": "on-demand backend"}
    )
    assert raw["note"] == "on-demand backend"


def test_full_embeddings_record_round_trips_through_form_to_raw() -> None:
    """The example 'embeddings' service from the spec round-trips losslessly.

    Verifies that all fields from the live DB record survive the
    service_form_to_raw -> upsert_service_in_registry pipeline.
    """
    form = {
        "name": "embeddings",
        "base_url": "http://127.0.0.1:5061/v1",
        "health_endpoint": "/health",
        "timeout": 120,
        "protocol": "openai-compatible",
        "native_passthrough": True,
        "capabilities": ["openai", "embeddings"],
        "role": "required",
        "priority": 100,
        "note": "Local embedding backend",
        "resources": {
            "duration_class": "interactive",
            "gpu_required": False,
            "max_parallel": 4,
            "vram_gb_estimate": 0,
        },
        "models": [{"public": "nomic-embed-text-v1.5", "upstream": "nomic-embed-text-v1.5"}],
    }
    raw = service_registry.service_form_to_raw(form)

    assert raw["host"] == "127.0.0.1"
    assert raw["port"] == 5061
    assert raw["base_path"] == "/v1"
    assert raw["health_endpoint"] == "/health"
    assert raw["timeout"] == 120
    assert raw["protocol"] == "openai-compatible"
    assert raw["native_passthrough"] is True
    assert raw["role"] == "required"
    assert raw["priority"] == 100
    assert raw["note"] == "Local embedding backend"
    assert raw["resources"]["gpu_required"] is False
    assert raw["resources"]["max_parallel"] == 4
    assert raw["resources"]["duration_class"] == "interactive"
    assert raw["models"] == [
        {"model_group": "nomic-embed-text-v1.5", "upstream_model": "nomic-embed-text-v1.5"}
    ]

    # Upsert into a fresh registry — stored config must carry all fields.
    seed = service_registry.build_service_registry_seed({"backends": {}})
    registry, record = service_registry.upsert_service_in_registry(
        seed, service_id="embeddings", raw=raw, source="ui"
    )
    cfg = record["config"]
    assert cfg.get("health_endpoint") == "/health"
    assert cfg.get("timeout") == 120
    assert cfg.get("resources", {}).get("max_parallel") == 4
    assert cfg.get("resources", {}).get("duration_class") == "interactive"
    assert cfg.get("note") == "Local embedding backend"
    assert cfg.get("native_passthrough") is True
    assert record["role"] == "required"
    assert record["priority"] == 100


# ---------------------------------------------------------------------------
# ComfyUI protocol fields — service_form_to_raw round-trip
# ---------------------------------------------------------------------------

def test_service_form_to_raw_comfyui_protocol_fields() -> None:
    """ComfyUI-specific fields round-trip through service_form_to_raw."""
    raw = service_registry.service_form_to_raw(
        {
            "name": "comfy",
            "host": "127.0.0.1",
            "port": 8188,
            "protocol": "comfyui",
            "capabilities": ["image", "generations", "videos"],
            "workflows_dir": "/data/workflows",
            "video_workflows_dir": "/data/video_workflows",
            "video_timeout": 300,
            "default_model": "my_workflow",
        }
    )
    assert raw["workflows_dir"] == "/data/workflows"
    assert raw["video_workflows_dir"] == "/data/video_workflows"
    assert raw["video_timeout"] == 300
    assert raw["default_model"] == "my_workflow"
    assert "generations" in raw["capabilities"]
    assert "videos" in raw["capabilities"]


def test_service_form_to_raw_video_timeout_coerces_string() -> None:
    """video_timeout is coerced to int from a string (form submission)."""
    raw = service_registry.service_form_to_raw(
        {
            "name": "comfy",
            "host": "127.0.0.1",
            "port": 8188,
            "video_timeout": "120",
        }
    )
    assert raw["video_timeout"] == 120


def test_comfyui_protocol_fields_stored_in_config() -> None:
    """ComfyUI fields survive the upsert_service_in_registry pipeline."""
    raw = service_registry.service_form_to_raw(
        {
            "name": "comfyui",
            "host": "127.0.0.1",
            "port": 8188,
            "protocol": "comfyui",
            "capabilities": ["image", "generations", "videos"],
            "workflows_dir": "/data/workflows",
            "video_workflows_dir": "/data/video_workflows",
            "video_timeout": 300,
            "default_model": "my_workflow",
        }
    )
    seed = service_registry.build_service_registry_seed({"backends": {}})
    _registry, record = service_registry.upsert_service_in_registry(
        seed, service_id="comfyui", raw=raw, source="ui"
    )
    cfg = record["config"]
    assert cfg.get("workflows_dir") == "/data/workflows"
    assert cfg.get("video_workflows_dir") == "/data/video_workflows"
    assert cfg.get("video_timeout") == 300
    assert cfg.get("default_model") == "my_workflow"


# ---------------------------------------------------------------------------
# Capability vocabulary — generations and videos
# ---------------------------------------------------------------------------

def test_capability_choices_includes_generations_and_videos() -> None:
    """_CAPABILITY_CHOICES must include generations and videos (honesty H2)."""
    assert "generations" in service_registry._CAPABILITY_CHOICES
    assert "videos" in service_registry._CAPABILITY_CHOICES


def test_unknown_config_key_survives_form_to_raw_passthrough() -> None:
    """An unknown config key present in the form dict passes through unchanged.

    This is the generic regression guard: any future config field that
    service_form_to_raw doesn't explicitly model must not be silently dropped.
    Simulates the pattern used by update_service: the existing config is
    flattened into form_base, so all keys end up in the merged dict.
    """
    # Simulate a future field the form doesn't model yet.
    raw = service_registry.service_form_to_raw(
        {
            "name": "x",
            "host": "localhost",
            "port": 9999,
            "some_future_field": "preserved-value",
        }
    )
    # service_form_to_raw drops blank/None values but must not strip unknown keys.
    assert raw.get("some_future_field") == "preserved-value"


def test_model_policy_records_round_trip_through_form_and_registry() -> None:
    raw = service_registry.service_form_to_raw(
        {
            "name": "models",
            "host": "127.0.0.1",
            "port": 8000,
            "model_policy_schema_version": service_registry.MODEL_POLICY_SCHEMA_VERSION,
            "models": [
                {
                    "id": "base",
                    "public": "base",
                    "upstream": "shared.gguf",
                    "transforms": [{"kind": "default-params", "set": {"max_tokens": 9}}],
                },
                {
                    "id": "fast",
                    "public": "fast",
                    "upstream": "shared.gguf",
                    "base_model_id": "base",
                    "policy_overlay": [
                        {"kind": "default-params", "set": {"reasoning_effort": "none"}}
                    ],
                },
            ],
        }
    )
    registry, record = service_registry.upsert_service_in_registry(
        service_registry.build_service_registry_seed({"backends": {}}),
        service_id="models",
        raw=raw,
    )

    assert record["config"]["models"] == raw["models"]
    assert record["config"]["model_policy_schema_version"] == service_registry.MODEL_POLICY_SCHEMA_VERSION
    projected = service_registry.runtime_backends(registry)
    assert projected["models"]["models"] == raw["models"]
    assert projected["models"]["model_policy_schema_version"] == service_registry.MODEL_POLICY_SCHEMA_VERSION


@pytest.mark.parametrize("marker", [True, "1", -1])
def test_registry_rejects_invalid_model_policy_schema_markers(marker: object) -> None:
    with pytest.raises(ValueError, match="model_policy_schema_version"):
        service_registry.normalize_service_registry(
            {
                "services": {
                    "service": {
                        "id": "service",
                        "config": {"model_policy_schema_version": marker, "models": []},
                    }
                }
            }
        )


@pytest.mark.parametrize(
    ("models", "match"),
    [
        ([
            {"id": "base", "model_group": "base", "upstream_model": "base"},
            {"id": "alias", "model_group": "alias", "upstream_model": "base", "base_model_id": "missing"},
        ], "unknown base_model_id"),
        ([
            {"id": "base", "model_group": "base", "upstream_model": "base"},
            {"id": "alias-a", "model_group": "alias-a", "upstream_model": "base", "base_model_id": "alias-b"},
            {"id": "alias-b", "model_group": "alias-b", "upstream_model": "base", "base_model_id": "alias-a"},
        ], "cycle"),
        ([
            {"id": "base", "model_group": "base", "upstream_model": "base"},
            {"id": "alias-a", "model_group": "alias-a", "upstream_model": "base", "base_model_id": "base"},
            {"id": "alias-b", "model_group": "alias-b", "upstream_model": "base", "base_model_id": "alias-a"},
        ], "base policy record"),
        ([
            {"id": "base", "model_group": "base", "upstream_model": "base"},
            {"id": "base", "model_group": "other", "upstream_model": "base"},
        ], "duplicate model id"),
        ([
            {"id": "base", "model_group": "base", "upstream_model": "base"},
            {"id": "alias", "model_group": "alias", "upstream_model": "base", "base_model_id": "base", "transforms": ["named"]},
        ], "policy_overlay"),
    ],
)
def test_registry_rejects_invalid_model_policy_graphs(models: list[dict], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        service_registry.normalize_service_registry(
            {
                "services": {
                    "service": {
                        "id": "service",
                        "config": {"models": models},
                    }
                }
            }
        )


def test_registry_write_rejects_an_invalid_model_policy_reference() -> None:
    with pytest.raises(ValueError, match="unknown base_model_id"):
        service_registry.upsert_service_in_registry(
            service_registry.build_service_registry_seed({"backends": {}}),
            service_id="service",
            raw={
                "models": [
                    {"id": "base", "model_group": "base", "upstream_model": "base"},
                    {
                        "id": "alias",
                        "model_group": "alias",
                        "upstream_model": "base",
                        "base_model_id": "missing",
                    },
                ]
            },
        )


def test_registry_rejects_a_public_alias_declared_by_two_services() -> None:
    alias = {
        "id": "current",
        "model_group": "current",
        "upstream_model": "base",
        "base_model_id": "base",
    }
    base = {"id": "base", "model_group": "base", "upstream_model": "base"}
    with pytest.raises(ValueError, match="ambiguous across services"):
        service_registry.normalize_service_registry(
            {
                "services": {
                    "a": {"id": "a", "config": {"models": [base, alias]}},
                    "b": {"id": "b", "config": {"models": [base, alias]}},
                }
            }
        )
