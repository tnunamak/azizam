"""Explicit setup helpers for tests that need an initial service registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store


def write_service_registry_for_test(
    value: Mapping[str, Any], *, path: Path, updated_by: str = "test"
) -> dict[str, Any]:
    """Seed a test registry through its dedicated writer, never generic settings."""
    current = admin_store.get_service_registry_with_generation(path=path)
    return admin_store.write_service_registry(
        value,
        updated_by=updated_by,
        expected_generation=(
            int(current["operator_settings_generation"]) if current else 0
        ),
        path=path,
    )


def seed_registry_document(proxy_config: Mapping[str, Any]) -> dict[str, Any]:
    """A whole registry document: the bundled release records plus one owner service for each ``backends`` entry.

    Production no longer builds a registry from ``proxy_config.yml`` ``backends`` (docs/design/CONFIG_HOMES.md, 8.5).
    A test that needs a registry with engines in it builds one explicitly, from a ``backends``-shaped mapping, and
    writes it as the owner's own records.
    """
    from gateway import service_registry as sr

    catalogs = sr.load_seed_catalogs(proxy_config)
    services = dict(catalogs.get("services") or {})
    for raw_id, raw in (proxy_config.get("backends") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        service_id = sr.normalize_id(raw_id)
        if not service_id:
            continue
        services[service_id] = sr.service_from_backend_config(
            service_id, raw, source=str(raw.get("source") or "provisioned"),
            provenance={"seed": f"fixture:backends.{raw_id}", "seed_hash": sr.stable_hash(raw)},
        )
    seeded = sr.normalize_service_registry({
        "schema_version": 1,
        "services": services,
        "protocol_frontends": catalogs.get("protocol_frontends") or {},
        "workflows": catalogs.get("workflows") or {},
        "capabilities": catalogs.get("capabilities") or {},
        "updated_at": 1,
    })
    seeded = sr.bind_credential_origins(seeded, seeded["services"])
    sr.migrate_engine_records_v2(seeded)
    return seeded


def seed_registry_from_backends(path: Path, proxy_config: Mapping[str, Any]) -> dict[str, Any]:
    """Create ``path`` with a registry holding the engines in ``proxy_config["backends"]``, as an owner's records."""
    admin_store.init_db(path)
    return write_service_registry_for_test(seed_registry_document(proxy_config), path=path)


def write_legacy_registry_for_test(value: Mapping[str, Any], *, path: Path, updated_by: str = "test") -> dict[str, Any]:
    """Store ``value`` as a registry written before the release layer: a whole document, held as given."""
    current = admin_store.get_service_registry_json_with_generation(path=path)
    return admin_store.write_service_registry(
        value,
        updated_by=updated_by,
        expected_generation=int(current["operator_settings_generation"]) if current else 0,
        path=path,
        stored_form=True,
    )
