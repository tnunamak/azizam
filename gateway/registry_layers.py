"""The service registry as a layered document (docs/design/CONFIG_HOMES.md, 3.4).

The release ships records (``registry/services.yml``, ``registry/protocol_frontends.yml``). The stored document holds
only owner data, and every read layers it over the release:

* a record the release does not ship is an **owner-added** record, stored whole;
* a record the release ships and the owner changed is stored as a **patch**: an RFC 7396 merge patch of only the fields
  the owner changed, so a release change to any other field reaches the install. A record equal to the release is not
  stored at all (it **inherits**). A record that a merge patch cannot express (a literal ``null``, a field that is an
  object replaced by a whole object) is stored as ``{"$replace": record}``;
* a release record the owner deleted is a **tombstone**: ``tombstones: {collection: [ids]}``. It stays hidden when a
  later release changes or re-adds the record.

``layer`` builds the effective document every reader sees. ``unlayer`` is its inverse: the writer hands it the whole
effective document it edited, and what is stored is the part that differs from the release. So a reader and a writer
both work on the whole document, ``unlayer(layer(x)) == x`` and ``layer(unlayer(y)) == y``, and the storage is the
only place that knows about layers.

This module is pure. The release itself (the bundled catalogs, normalized) is built by
``service_registry.release_registry``.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

COLLECTIONS: tuple[str, ...] = ("services", "protocol_frontends", "workflows", "capabilities")
LAYERING_KEY = "layering"
LAYERING_VERSION = 1
TOMBSTONES_KEY = "tombstones"
BASES_KEY = "bases"
REPLACE_KEY = "$replace"
# A stored workflow record that holds only the run control of a bundled workflow (state, publication, revision) and names
# the bundled id here. It holds no contract: the release's contract and facts serve under it.
RUN_CONTROL_PATCH_KEY = "patch_of"
# A stored patch of a release record also names the record: the keys the code before the layer needs to read a stored row
# as the release record it copies (a rollback then rebuilds the row from the release). A merge changes nothing, since each
# equals the release value; ``field_names`` leaves them out of a report.
IDENTITY_KEYS = ("id", "source", "kind")
# The one-time step that turns a registry stored before this layer into one that holds owner data only.
LAYERING_STEP = "config-homes-registry-layering"

# What a view says of a record.
SOURCE_RELEASE = "release"
SOURCE_OWNER = "owner"
SOURCE_OWNER_ADDED = "owner-added"
SOURCE_OWNER_DELETED = "owner-deleted"
DRIFT_RELEASE_CHANGED = "release_changed"
DRIFT_RELEASE_REMOVED = "release_removed"

_META_KEYS = frozenset({LAYERING_KEY, TOMBSTONES_KEY, BASES_KEY})


# -- RFC 7396 -------------------------------------------------------------------------------------------------------


def merge_patch(target: Any, patch: Any) -> Any:
    """``target`` with the RFC 7396 merge ``patch`` applied. A ``null`` in the patch deletes a key; neither argument changes."""
    if not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    result: dict[str, Any] = dict(target) if isinstance(target, Mapping) else {}
    result = copy.deepcopy(result)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def diff_patch(base: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """The merge patch that turns ``base`` into ``new`` (``{}`` when they are equal).

    A literal ``None`` in ``new`` cannot be written as a patch, so callers check the round trip (``record_entry`` does).
    """
    patch: dict[str, Any] = {}
    for key, value in new.items():
        if key not in base:
            patch[key] = copy.deepcopy(value)
        elif base[key] != value:
            if isinstance(base[key], Mapping) and isinstance(value, Mapping):
                patch[key] = diff_patch(base[key], value)
            else:
                patch[key] = copy.deepcopy(value)
    for key in base:
        if key not in new:
            patch[key] = None
    return patch


def record_entry(release_record: Any, record: Any) -> dict[str, Any] | None:
    """What to store for ``record`` over the release record with its id: None (inherit), a patch, or a ``$replace`` wrapper."""
    if record == release_record:
        return None
    if isinstance(release_record, Mapping) and isinstance(record, Mapping):
        patch = diff_patch(release_record, record)
        if REPLACE_KEY not in patch and merge_patch(release_record, patch) == record:
            return patch
    return {REPLACE_KEY: copy.deepcopy(record)}


def apply_entry(release_record: Any, entry: Any) -> Any:
    """The record a stored entry over ``release_record`` means."""
    if isinstance(entry, Mapping) and set(entry) == {REPLACE_KEY}:
        return copy.deepcopy(entry[REPLACE_KEY])
    return merge_patch(release_record, entry)


# -- layer and unlayer ----------------------------------------------------------------------------------------------


def digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def is_layered(stored: Mapping[str, Any]) -> bool:
    """Whether ``stored`` is in the owner-data form (the writers always mark it)."""
    return int(stored.get(LAYERING_KEY) or 0) >= LAYERING_VERSION


def empty_stored(schema_version: int) -> dict[str, Any]:
    """The stored document of an install whose owner has changed nothing: it inherits every release record."""
    return {"schema_version": schema_version, **{name: {} for name in COLLECTIONS}, TOMBSTONES_KEY: {},
            LAYERING_KEY: LAYERING_VERSION}


def tombstones_of(stored: Mapping[str, Any], collection: str) -> frozenset[str]:
    found = stored.get(TOMBSTONES_KEY)
    ids = found.get(collection) if isinstance(found, Mapping) else None
    return frozenset(str(item) for item in ids) if isinstance(ids, list) else frozenset()


def _collection(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    found = document.get(name)
    return found if isinstance(found, Mapping) else {}


def layer(stored: Mapping[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
    """The effective document: the release records, under the owner's patches, replacements, additions and tombstones.

    A stored document that is not in the owner-data form holds whole records, so each replaces the release record of
    its id outright (a merge patch cannot delete a key), and it still inherits the release records it lacks. The
    startup step rewrites such a document into the owner-data form before anything reads it.
    """
    layered = is_layered(stored)
    effective: dict[str, Any] = {key: copy.deepcopy(value) for key, value in stored.items() if key not in _META_KEYS}
    for name in COLLECTIONS:
        base, owned, hidden = _collection(release, name), _collection(stored, name), tombstones_of(stored, name)
        records: dict[str, Any] = {}
        for record_id, record in base.items():
            if record_id not in hidden and record_id not in owned:
                records[record_id] = copy.deepcopy(record)
        for record_id, entry in owned.items():
            records[record_id] = apply_entry(base[record_id], entry) if layered and record_id in base else _whole(entry)
        effective[name] = {record_id: records[record_id] for record_id in sorted(records)}
    return effective


def _whole(entry: Any) -> Any:
    """An owner-added record as stored. A ``$replace`` wrapper is read whatever the release holds (a release that dropped the id)."""
    if isinstance(entry, Mapping) and set(entry) == {REPLACE_KEY}:
        return copy.deepcopy(entry[REPLACE_KEY])
    return copy.deepcopy(entry)


def unlayer(
    effective: Mapping[str, Any], release: Mapping[str, Any], previous: Mapping[str, Any] | None = None,
    *, undeletable: Callable[[str, str, Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """The stored (owner-data) document for ``effective``: what differs from the release, and what the owner deleted.

    ``previous`` is the stored document being replaced. A patch that did not change keeps the release digest it was
    written against (``bases``), so the drift report still shows a release change the owner has not looked at.
    ``undeletable(collection, id, release_record)`` names a release record that no write may hide: a write that omits
    it leaves it inherited, as the managed LiteLLM row always was (the registry route refuses to remove it).
    """
    stored: dict[str, Any] = {key: copy.deepcopy(value) for key, value in effective.items() if key not in _META_KEYS}
    tombstones: dict[str, list[str]] = {}
    bases: dict[str, dict[str, str]] = {}
    before = previous or {}
    for name in COLLECTIONS:
        base, kept = _collection(release, name), _collection(effective, name)
        out: dict[str, Any] = {}
        named_bases: dict[str, str] = {}
        for record_id, record in kept.items():
            if record_id not in base:
                out[record_id] = copy.deepcopy(record)
                continue
            entry = record_entry(base[record_id], record)
            if entry is None:
                continue
            if REPLACE_KEY not in entry:
                for key in IDENTITY_KEYS:
                    if key in base[record_id] and key not in entry:
                        entry[key] = copy.deepcopy(base[record_id][key])
            out[record_id] = entry
            old_entry = _collection(before, name).get(record_id)
            old_base = _collection(_collection(before, BASES_KEY), name).get(record_id)
            named_bases[record_id] = old_base if old_base and old_entry == entry else digest(base[record_id])
        gone = sorted(
            record_id for record_id in base
            if record_id not in kept and not (undeletable and undeletable(name, record_id, base[record_id])))
        for record_id in gone:
            old_base = _collection(_collection(before, BASES_KEY), name).get(record_id)
            was_hidden = record_id in tombstones_of(before, name)
            named_bases[record_id] = old_base if old_base and was_hidden else digest(base[record_id])
        if gone:
            tombstones[name] = gone
        if named_bases:
            bases[name] = named_bases
        stored[name] = out
    stored[TOMBSTONES_KEY] = tombstones
    if bases:
        stored[BASES_KEY] = bases
    stored[LAYERING_KEY] = LAYERING_VERSION
    return stored


def sources_tombstones(stored: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{collection: [ids]}`` the stored document hides."""
    return {name: sorted(tombstones_of(stored, name)) for name in COLLECTIONS if tombstones_of(stored, name)}


# -- views ----------------------------------------------------------------------------------------------------------


def sources(stored: Mapping[str, Any], release: Mapping[str, Any]) -> dict[str, Any]:
    """Per record: where its effective value comes from, and whether the release moved under the owner's change.

    ``{collection: {id: {"source": ..., "fields": [...]?, "drift": kind?}}}`` for every effective record and every
    tombstone. Field names only, never values.
    """
    view: dict[str, Any] = {}
    for name in COLLECTIONS:
        base, owned, hidden = _collection(release, name), _collection(stored, name), tombstones_of(stored, name)
        recorded = _collection(_collection(stored, BASES_KEY), name)
        rows: dict[str, Any] = {}
        for record_id in sorted({*base, *owned}):
            if record_id in hidden and record_id not in owned:
                continue
            row: dict[str, Any] = {}
            if record_id not in base:
                row["source"] = SOURCE_OWNER_ADDED
                if record_id in recorded:  # a patch whose release record is gone: it now stands alone
                    row["drift"] = DRIFT_RELEASE_REMOVED
            elif record_id in owned:
                row["source"] = SOURCE_OWNER
                row["fields"] = field_names(owned[record_id], base[record_id])
                if record_id in recorded and recorded[record_id] != digest(base[record_id]):
                    row["drift"] = DRIFT_RELEASE_CHANGED
            else:
                row["source"] = SOURCE_RELEASE
            rows[record_id] = row
        for record_id in sorted(hidden):
            row = {"source": SOURCE_OWNER_DELETED}
            if record_id not in base:
                row["drift"] = DRIFT_RELEASE_REMOVED
            elif recorded.get(record_id) and recorded[record_id] != digest(base[record_id]):
                row["drift"] = DRIFT_RELEASE_CHANGED
            rows[record_id] = row
        view[name] = rows
    return view


def field_names(entry: Any, release_record: Any = None) -> list[str]:
    """The names of the fields a stored entry changes: top-level names, and for ``config`` its key names. No values.

    With the release record given, an identity key (``IDENTITY_KEYS``) that equals the release value is not a change."""
    if not isinstance(entry, Mapping):
        return []
    if set(entry) == {REPLACE_KEY}:
        return [REPLACE_KEY]
    names: list[str] = []
    for key in sorted(entry):
        if key in IDENTITY_KEYS and isinstance(release_record, Mapping) and release_record.get(key) == entry[key]:
            continue
        names.append(str(key))
        if key == "config" and isinstance(entry[key], Mapping):
            names.extend(f"config.{inner}" for inner in sorted(entry[key]))
    return names


# -- export ---------------------------------------------------------------------------------------------------------


def owner_layer(stored: Mapping[str, Any], release: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The owner data of a stored document: what the export file holds (docs/design/CONFIG_HOMES.md, 6.1).

    With the release given, a patch of a release record leaves out the identity keys it only repeats (``IDENTITY_KEYS``);
    an import stamps them again."""
    def exported(name: str) -> dict[str, Any]:
        base, out = _collection(release or {}, name), {}
        for record_id, entry in _collection(stored, name).items():
            entry = copy.deepcopy(entry)
            if record_id in base and isinstance(entry, dict) and REPLACE_KEY not in entry:
                for key in IDENTITY_KEYS:
                    if entry.get(key) == base[record_id].get(key):
                        entry.pop(key, None)
            out[record_id] = entry
        return out

    return {
        "records": {name: exported(name) for name in COLLECTIONS if _collection(stored, name)},
        "tombstones": {name: sorted(tombstones_of(stored, name)) for name in COLLECTIONS if tombstones_of(stored, name)},
    }


def from_owner_layer(layer_document: Mapping[str, Any], schema_version: int) -> dict[str, Any]:
    """A stored document for an exported owner layer. Raises ValueError for a shape that is not one."""
    records = layer_document.get("records", {})
    hidden = layer_document.get("tombstones", {})
    if not isinstance(records, Mapping) or not isinstance(hidden, Mapping):
        raise ValueError("registry.records and registry.tombstones must be objects")
    stored = empty_stored(schema_version)
    for name, found in records.items():
        if name not in COLLECTIONS or not isinstance(found, Mapping):
            raise ValueError(f"registry.records.{name} is not a registry collection of records")
        stored[name] = copy.deepcopy(dict(found))
    for name, ids in hidden.items():
        if name not in COLLECTIONS or not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError(f"registry.tombstones.{name} must be a list of record ids")
        if ids:
            stored[TOMBSTONES_KEY][name] = sorted(set(ids))
    return stored
