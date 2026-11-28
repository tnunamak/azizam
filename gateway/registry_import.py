"""The registry part of the config export and import (docs/design/CONFIG_HOMES.md, 6.1 and 6.2).

The export holds the owner layer of the registry: the records the owner added whole, the patches of release records,
and the tombstones. It holds no release record and no secret value; a record keeps its secret references.

``plan`` judges an imported owner layer against the receiving release and returns the stored document it would write,
and a report of record ids. It reads the database and writes nothing. The report carries ids and kinds, never a value.
The caller writes ``Plan.stored`` with ``admin_store.write_service_registry_in_transaction(..., stored_form=True)``, in the
transaction of the config change when there is one, so the import is all or nothing.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store, record_carriers, registry_layers as layers, registry_migration, secret_refs, service_registry


@dataclass
class Plan:
    stored: dict[str, Any]
    generation: int  # the generation the write must find, as ``os-N`` says it
    changed_ids: list[str] = field(default_factory=list)  # "collection/id"
    problems: list[dict[str, str]] = field(default_factory=list)
    needs_secret: list[dict[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changed_ids)

    def report(self) -> dict[str, Any]:
        return {"changed": list(self.changed_ids), "revision": f"os-{self.generation}"}


def stored_now(path: Path) -> tuple[dict[str, Any], int]:
    """The stored document (the empty owner document when none) and its generation."""
    record = admin_store.get_service_registry_json_with_generation(path=path)
    if record is None:
        return layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), 0
    return service_registry._stored_document(record["value_json"]), int(record["operator_settings_generation"])


def export_part(path: Path) -> dict[str, Any]:
    """The ``registry`` member of the export document, and the references its records hold (``used_by`` names the record)."""
    stored, _generation = stored_now(path)
    if not layers.is_layered(stored):  # an install that has not started on this release: nothing is owner data yet
        stored = layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION)
    return layers.owner_layer(stored, service_registry.release_registry())


def secrets_in(part: Mapping[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for collection, records in sorted((part.get("records") or {}).items()):
        for record_id, record in sorted(records.items()):
            found += [{"ref": ref, "used_by": f"registry/{collection}/{record_id}"} for ref in record_carriers.refs_in(record)]
    return found


def plan(part: Any, mode: str, *, path: Path) -> Plan | str:
    """The plan for importing ``part`` (the ``registry`` member of an export file), or the text of a malformed file."""
    if not isinstance(part, Mapping):
        return "registry must be an object with records and tombstones"
    try:
        incoming = layers.from_owner_layer(part, service_registry.SERVICE_REGISTRY_SCHEMA_VERSION)
    except ValueError as error:
        return str(error)
    current, generation = stored_now(path)
    if not layers.is_layered(current):
        # The step has not run on this install; reading it as the owner data of an install that changed nothing would
        # drop its records. The step runs at start, so an install that serves has run it.
        return "the registry has not been migrated to the release layer yet; start the gateway once, then import"
    release = service_registry.release_registry()
    candidate = copy.deepcopy(current) if mode == "merge" else layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION)
    if mode == "merge":
        for name in layers.COLLECTIONS:
            records, hidden = dict(candidate.get(name) or {}), set(layers.tombstones_of(candidate, name))
            for record_id, entry in (incoming.get(name) or {}).items():
                records[record_id] = copy.deepcopy(entry)
                hidden.discard(record_id)
            for record_id in layers.tombstones_of(incoming, name):
                hidden.add(record_id)
                records.pop(record_id, None)
            candidate[name] = records
            candidate.setdefault(layers.TOMBSTONES_KEY, {})[name] = sorted(hidden)
    else:
        candidate = incoming
    before = layers.layer(current, release)
    effective = layers.layer(candidate, release)
    result = Plan(stored=candidate, generation=generation)
    imported = {(name, record_id) for name in layers.COLLECTIONS for record_id in (incoming.get(name) or {})}
    registry, excluded = service_registry.read_registry(effective)
    for item in excluded:
        if (item.section, item.id) in imported or item.section not in layers.COLLECTIONS:
            result.problems.append({"section": "registry", "pointer": f"/{item.section}/{item.id}", "code": "invalid_record",
                                    "message": service_registry.registry_error_text(item.error, effective)})
    for name, record_id in sorted(imported):
        record = effective.get(name, {}).get(record_id)
        if record_carriers.legacy_secret_texts(record):
            result.problems.append({"section": "registry", "pointer": f"/{name}/{record_id}", "code": "inline_secret",
                                    "message": "The record holds a secret value. Secrets are not imported: enter it in the secret store."})
        for ref in record_carriers.refs_in(record):
            if not secret_refs.configured(ref):
                result.needs_secret.append({"section": "registry", "pointer": f"/{name}/{record_id}", "code": "needs_secret"})
    if not result.problems:
        # Written as the writers write it: a record equal to the release inherits, and the bases are stamped.
        result.stored = layers.unlayer(
            effective, release, current,
            undeletable=lambda collection, record_id, record: collection == "services"
            and service_registry.is_litellm_service_record(record_id, record))
        result.stored.pop("seeded_from", None)
        result.changed_ids = registry_migration.differing_records(before, effective)
        if result.stored == current:
            result.changed_ids = []
    return result
