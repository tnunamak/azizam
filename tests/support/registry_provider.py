"""A snapshot provider for tests that hold a ``WorkflowRegistry``: the records become the bundled release."""

from __future__ import annotations

from gateway import registry_snapshot as rs
from gateway.discovery import WorkflowRegistry


def provider_of(registry: WorkflowRegistry) -> rs.Provider:
    records = {record.id: record for record in registry.records()}
    return rs.Provider(rs.build({}, rs.BundledRelease(records, ()), generation=1))
