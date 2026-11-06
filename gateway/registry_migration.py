"""The one-time registry migration ``config-homes-registry-layering`` (docs/design/CONFIG_HOMES.md, 8.3).

A registry stored before the layer holds a copy of every release record it ever booted with, plus the owner's records.
The migration turns that document into the owner-data form of ``gateway/registry_layers.py``:

* a stored record that matches a release record with the same id keeps only the fields that differ (a patch), or
  nothing at all when no field differs (it inherits);
* a release record the stored document lacks is a tombstone, except the records the old boot always put back (the
  builtin protocol frontends and the LiteLLM substrate), so what the gateway serves does not change;
* a record the release does not ship stays whole.

To keep what is served the same, the migration first applies what the old boot applied (the bundled-catalog backfill
below: fill the fields a stored record lacks, add the builtin frontends and the LiteLLM row), and then stores the
difference from the release. The backfill functions here run only in this step and in its dry-run; no boot runs them.

``plan_layering`` is pure. ``report`` describes a plan with record ids and field NAMES only, never a value.
"""
from __future__ import annotations

import copy
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway import registry_layers as layers
from gateway import service_registry

# Audit actions that show the owner changed a service (``gateway/admin_runtime.py``). A patch whose record has none
# of them is probably a stale copy of an older release, not an owner edit.
OWNER_EDIT_ACTIONS = (
    "operator.service.updated", "operator.service.added", "operator.service.toggled",
    "operator.service.address_confirmed", "operator.service.secret_entries",
)
_FRONTEND_BACKFILL_KEYS = ("registered_routes", "asset_content_policies", "public_discovery")


def _backfill_missing_records(
    records: dict[str, dict[str, Any]],
    seeds: Mapping[str, Any],
) -> None:
    for record_id, seed in seeds.items():
        if record_id in records or not isinstance(seed, Mapping):
            continue
        # The DB registry is the source of truth after first seed. Do not
        # resurrect ordinary bundled/catalog services an operator removed.
        # The managed LiteLLM substrate is the sole exception because the
        # gateway owns that process and remove_service_from_registry blocks its
        # deletion in current registries; old registries may predate the row.
        if service_registry.is_litellm_service_record(record_id, seed):
            records[record_id] = copy.deepcopy(dict(seed))


def _backfill_service_metadata(
    records: dict[str, dict[str, Any]],
    seeds: Mapping[str, Any],
) -> None:
    for record_id, record in records.items():
        seed = seeds.get(record_id)
        if not isinstance(seed, Mapping):
            continue
        record_source = str(record.get("source") or "").strip()
        seed_source = str(seed.get("source") or "").strip()
        if record_source and seed_source and record_source != seed_source:
            continue
        # Attachments are operator-owned after first seed. In particular, a
        # deleted attachment must not be restored by metadata backfill.
        seed = copy.deepcopy(dict(seed))
        if isinstance(seed.get("config"), dict):
            seed["config"].pop("transforms", None)
        # The residency block names the service's domain and its transition: it is merged apart, key by key,
        # so a seed never replaces or extends a block the record declares.
        seed_residency, seed_place = _take_residency(seed)
        _merge_missing_fields(record, seed)
        _merge_residency(record, seed_residency, seed_place)


def _take_residency(record: dict[str, Any]) -> tuple[Mapping[str, Any], str]:
    """Remove and return the residency block ``record`` declares (top-level wins, as the gate reads it), and
    where it was: ``"top"`` or ``"config"``. An empty mapping when it declares none. A value that is not a
    mapping (the legacy ``residency: on_demand`` string) is left in place for the ordinary merge."""
    config = record.get("config") if isinstance(record.get("config"), dict) else {}
    top, nested = record.get("residency"), config.get("residency")
    block: Mapping[str, Any] = {}
    place = "top"
    if isinstance(nested, Mapping):
        config.pop("residency")
        block, place = nested, "config"
    if isinstance(top, Mapping):
        record.pop("residency")
        if top:
            block, place = top, "top"
    return block, place


def _merge_residency(record: dict[str, Any], seed: Mapping[str, Any], place: str) -> None:
    """Fill in the residency keys ``record`` lacks. Only top-level keys: a declared ``transition`` or
    ``domain`` is never replaced, and a declared ``transition`` block never gains keys from a seed."""
    if not seed:
        return
    config = record.get("config") if isinstance(record.get("config"), dict) else None
    top = record.get("residency")
    nested = config.get("residency") if config is not None else None
    declared = top if isinstance(top, dict) and top else (nested if isinstance(nested, dict) and nested else None)
    if declared is None:
        # Nothing declared: the seed's block goes where the seed kept it.
        if place == "config" and config is not None:
            config["residency"] = copy.deepcopy(dict(seed))
        else:
            record["residency"] = copy.deepcopy(dict(seed))
        return
    for key, value in seed.items():
        declared.setdefault(key, copy.deepcopy(value))


def _merge_missing_fields(target: dict[str, Any], seed: Mapping[str, Any]) -> None:
    for key, seed_value in seed.items():
        if key not in target:
            target[key] = copy.deepcopy(seed_value)
            continue
        target_value = target.get(key)
        if isinstance(target_value, dict) and isinstance(seed_value, Mapping):
            _merge_missing_fields(target_value, seed_value)


def _backfill_collection_metadata(
    records: dict[str, dict[str, Any]],
    seeds: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> None:
    for record_id, record in records.items():
        seed = seeds.get(record_id)
        if not isinstance(seed, Mapping):
            continue
        for key in keys:
            if key not in record and key in seed:
                record[key] = copy.deepcopy(seed[key])


def _backfill_missing_builtin_frontends(
    records: dict[str, dict[str, Any]],
    seeds: Mapping[str, Any],
) -> None:
    """Add each builtin frontend the registry lacks. A builtin ships with the code, and the route mount already
    serves one that is missing from the store (``route_registry.mount_frontend_records``); this keeps the stored
    records, which the route checks and the Overview read, in agreement with what is served."""
    for frontend_id, seed in seeds.items():
        if frontend_id in records or not isinstance(seed, Mapping) or seed.get("source") != "builtin":
            continue
        records[frontend_id] = copy.deepcopy(dict(seed))


@dataclass(frozen=True)
class PatchRow:
    """One stored record that differs from its release record, with the names of the fields it changes."""

    collection: str
    id: str
    kind: str  # "patch" or "replace"
    fields: tuple[str, ...]
    owner_edit: bool | None  # None where the audit log has no edit actions for the collection
    probably_stale_copy: bool


@dataclass
class Plan:
    """The owner-data form of a registry, and what the step did to each record."""

    stored: dict[str, Any]
    patches: list[PatchRow] = field(default_factory=list)
    inherit: dict[str, list[str]] = field(default_factory=dict)
    owner_added: dict[str, list[str]] = field(default_factory=dict)
    tombstones: dict[str, list[str]] = field(default_factory=dict)
    carried: list[str] = field(default_factory=list)  # excluded records kept as stored

    def report(self) -> dict[str, Any]:
        """Record ids and field names. No value of any record, ever."""
        return {
            "patches": [
                {"collection": row.collection, "id": row.id, "kind": row.kind, "fields": list(row.fields),
                 "owner_edit_in_audit_log": row.owner_edit, "probably_stale_copy": row.probably_stale_copy}
                for row in self.patches
            ],
            "inherit": {name: ids for name, ids in self.inherit.items() if ids},
            "owner_added": {name: ids for name, ids in self.owner_added.items() if ids},
            "tombstones": {name: ids for name, ids in self.tombstones.items() if ids},
            "carried_excluded": list(self.carried),
        }


def effective_before(old_stored: Mapping[str, Any], catalogs: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """What the old boot served for ``old_stored``: its records, normalized, with the bundled-catalog backfill applied.

    Also the keys of the stored records that do not validate; they are carried into the document as stored.
    """
    registry, excluded = service_registry.read_registry(old_stored)
    _backfill_missing_records(registry["services"], catalogs.get("services") or {})
    _backfill_service_metadata(registry["services"], catalogs.get("services") or {})
    _backfill_collection_metadata(
        registry["protocol_frontends"], catalogs.get("protocol_frontends") or {}, keys=_FRONTEND_BACKFILL_KEYS)
    _backfill_missing_builtin_frontends(registry["protocol_frontends"], catalogs.get("protocol_frontends") or {})
    registry, merged_out = service_registry.read_registry(registry)  # normalize what the merge changed
    carried: list[str] = []
    for item in [*excluded, *merged_out]:
        records = registry.setdefault(item.section, {})
        if item.key not in records:
            records[item.key] = item.record
            carried.append(f"{item.section}/{item.id}")
    return registry, carried


def plan_layering(
    old_stored: Mapping[str, Any],
    release: Mapping[str, Any],
    catalogs: Mapping[str, Any],
    *,
    owner_edited_services: Collection[str] = (),
) -> Plan:
    """The owner-data document for a registry stored before the layer, and the report of what changed in each record."""
    effective, carried = effective_before(old_stored, catalogs)
    stored = layers.unlayer(effective, release)
    for key in ("seeded_from", "seed_hash"):  # the seed this document came from no longer exists
        stored.pop(key, None)
    plan = Plan(stored=stored, carried=carried, tombstones={k: list(v) for k, v in layers.sources_tombstones(stored).items()})
    edited = set(owner_edited_services)
    for name in layers.COLLECTIONS:
        base, kept = release.get(name) or {}, stored.get(name) or {}
        plan.inherit[name] = sorted(record_id for record_id in effective.get(name) or {}
                                    if record_id in base and record_id not in kept)
        plan.owner_added[name] = sorted(record_id for record_id in kept if record_id not in base)
        for record_id in sorted(record_id for record_id in kept if record_id in base):
            entry = kept[record_id]
            replaced = isinstance(entry, Mapping) and set(entry) == {layers.REPLACE_KEY}
            owner_edit = (record_id in edited) if name == "services" else None
            plan.patches.append(PatchRow(
                name, record_id, "replace" if replaced else "patch", tuple(layers.field_names(entry)), owner_edit,
                probably_stale_copy=owner_edit is False,
            ))
    return plan


def owner_edited_services(path: Path) -> set[str]:
    """The ids of the services the audit log shows an owner change of. Reads only; ``path`` is opened read-only."""
    marks = ",".join("?" for _ in OWNER_EDIT_ACTIONS)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            f"SELECT DISTINCT target_id FROM audit_events WHERE target_kind = 'service' AND action IN ({marks})",
            OWNER_EDIT_ACTIONS,
        ).fetchall()
    except sqlite3.OperationalError:  # no audit table: an install that never recorded an edit
        return set()
    finally:
        connection.close()
    return {str(row[0]) for row in rows if row[0]}
