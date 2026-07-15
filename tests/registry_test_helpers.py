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
