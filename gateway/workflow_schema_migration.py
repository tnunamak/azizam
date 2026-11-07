"""The one-time migration ``workflow-schema-2``: stored workflow records go from schema 1 to schema 2 (CODE_TO_DATA_S3, D3).

The release rewrites the bundled workflow files; this step rewrites what the owner's database holds, in the form of the
config-homes layering step (``gateway/registry_migration.py``): a pure plan, a report with ids and field names only, then an
apply. Temporary: it goes with ``gateway/workflow_convert.py`` and ``gateway/migration_data/``, once no install can hold a
schema 1 record.

What the plan does with each stored record of the ``workflows`` section, which is in the owner-data form
(``gateway/registry_layers.py``) when this step runs:

* **convert-whole**: a record the release does not ship (an owner record, a record a package imported, or an override
  of a bundled id) holds a whole schema 1 document. It is converted, and stored whole.
* **convert-patch**: an entry over a release record is a merge patch (or a ``$replace``). It is applied to the schema 1
  release record, the result is converted, and what is stored is the difference from the schema 2 release record. A patch
  does not commute with the converter (the converter renames a binding by the ``transport`` it holds, moves ``requires``
  into the one binding, and keys the seed rule on the ``map`` of every binding), so converting the patch alone is wrong.
  The schema 1 release records cannot be read from the bundled files, which are schema 2 once S3.5a lands: they are a
  frozen copy, ``gateway/migration_data/workflow_schema_1_release.json``.
* **already-v2**: a record whose document is schema 2 is not touched.
* **cannot-convert**: the converted record does not pass the checks a load makes, or there is no schema 1 release record to
  apply a patch to. It is kept as it was, stored disabled, with its error list under ``migration``. It is never dropped.
  A document that fails the checks is left out of a load and shown with the registry's left-out records; it never runs.

A converted record also states its policy facts (``exposure``, ``trust_tier``, ``resource_tier``, ``tags``), which a schema 1
document held beside its contract: what the old readers served for it, so what it serves does not change.

``plan_workflow_schema_2`` is pure. ``report`` describes a plan with record ids, field names, error pointers and codes, never
a value of any record.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from gateway import registry_layers as layers
from gateway import service_registry
from gateway.workflow_convert import POLICY_KEYS, convert_and_check
from gateway.workflow_policy import facts_from_mapping

STEP = service_registry.WORKFLOW_SCHEMA_2_STEP
FROZEN_RELEASE_PATH = Path(__file__).resolve().parent / "migration_data" / "workflow_schema_1_release.json"
FROZEN_FIXES_PATH = Path(__file__).resolve().parent / "migration_data" / "workflow_schema_1_release_fixes.json"
MAX_STORED_ERRORS = 20

CONVERT_WHOLE = "convert-whole"
CONVERT_PATCH = "convert-patch"
ALREADY_V2 = "already-v2"
CANNOT_CONVERT = "cannot-convert"

# What an inline stored record holds beside its document: the keys the record normalizer reads apart from it.
_RECORD_KEYS = frozenset({
    "editable", "enabled", "identity", "manifest_hash", "manifest_path", "migration", "overrides", "policy", "provenance",
    "registry_source", "source_path", "summary",
})
_DERIVED_KEYS = frozenset({"identity", "summary"})


# -- the frozen schema 1 release records ------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _frozen_documents() -> dict[str, Any]:
    return json.loads(FROZEN_RELEASE_PATH.read_text(encoding="utf-8"))["documents"]


def frozen_release_records() -> dict[str, dict[str, Any]]:
    """The schema 1 release records by id, in the shape of a release record (``id`` and ``manifest``). A fresh copy."""
    return {record_id: {"id": record_id, "manifest": copy.deepcopy(document)}
            for record_id, document in _frozen_documents().items()}


@lru_cache(maxsize=1)
def _frozen_fixes() -> dict[str, list[list[str]]]:
    document = json.loads(FROZEN_FIXES_PATH.read_text(encoding="utf-8"))["fixes"]
    return {record_id: [list(path) for path in fix["remove"]] for record_id, fix in document.items()}


def _at(document: Any, path: list[str]) -> Any:
    """The value of ``document`` at ``path`` (keys of nested objects), or ``_OMIT`` where there is none."""
    for key in path:
        if not isinstance(document, Mapping) or key not in document:
            return _OMIT
        document = document[key]
    return document


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality: ``True`` is not ``1`` and ``1`` is not ``1.0``, as a stored document would show."""
    if left is _OMIT or right is _OMIT:
        return left is right
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def apply_release_fixes(record_id: str, manifest: dict[str, Any]) -> None:
    """Remove from a schema 1 ``manifest`` what S3.5a's defect fixes removed from the release document of ``record_id``.

    The paths are data (``workflow_schema_1_release_fixes.json``). The paths go only if the record still holds the frozen
    release value at every one: an owner who changed one keeps all, and the record then fails the checks as it would have."""
    frozen = _frozen_documents().get(record_id)
    paths = _frozen_fixes().get(record_id, ()) if frozen is not None else ()
    # The fix is one change: if the owner edited any part of it, none is made, and the record fails the checks as it was.
    if not paths or any(_at(frozen, path) is _OMIT or not _json_equal(_at(manifest, path), _at(frozen, path)) for path in paths):
        return
    for path in paths:
        del _at(manifest, path[:-1])[path[-1]]


# -- one record --------------------------------------------------------------------------------------------------------


def v1_effective_facts(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What the schema 1 readers served for a document's policy facts: what it said, or the default each reader filled in."""
    tags = manifest.get("tags")
    return {
        "exposure": _text(manifest.get("exposure")) or "default",
        "trust_tier": _text(manifest.get("trust_tier")) or ("stable" if manifest.get("trusted", False) else "experimental"),
        "resource_tier": _text(manifest.get("resource_tier")) or "batch",
        "tags": list(dict.fromkeys(item for item in tags if isinstance(item, str) and item)) if isinstance(tags, list) else [],
    }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Failure:
    """A reason a record cannot be converted. The message may quote the record: it is stored with it, never reported."""

    pointer: str
    code: str
    message: str


def _split(entry: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(manifest, the record fields beside it)`` of a stored record, nested or inline (the record normalizer reads both)."""
    if isinstance(entry.get("manifest"), Mapping):
        return copy.deepcopy(dict(entry["manifest"])), {k: copy.deepcopy(v) for k, v in entry.items() if k != "manifest"}
    manifest = {k: copy.deepcopy(v) for k, v in entry.items() if k not in _RECORD_KEYS}
    beside = {k: copy.deepcopy(v) for k, v in entry.items() if k in _RECORD_KEYS}
    if isinstance(manifest.get("source"), str):  # the record normalizer reads an inline record's source from the document's
        beside.setdefault("source", manifest["source"])
    return manifest, beside


def _convert_record(record_id: str, entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[Failure], list[str]]:
    """The schema 2 form of one whole schema 1 record, as a load normalizes it; or its failures.

    The third value names the fields the conversion changed (manifest keys, and ``policy``)."""
    manifest, beside = _split(entry)
    apply_release_fixes(record_id, manifest)
    facts = v1_effective_facts(manifest)
    record_facts = facts_from_mapping(beside.get("policy"))
    if record_facts is not None:  # facts the record states itself win over those derived from the manifest, then the defaults
        facts = {"exposure": record_facts.exposure, "trust_tier": record_facts.trust_tier,
                 "resource_tier": record_facts.resource_tier, "tags": list(record_facts.tags)}
    stated = {key: manifest.pop(key) for key in POLICY_KEYS if key in manifest}  # a v2 document holds none of these
    converted, notes, report = convert_and_check(manifest)
    failures = [Failure(error.pointer or "/", error.code, error.message) for error in report.errors]
    candidate = {**beside, "id": record_id, "manifest": converted, "policy": facts}
    for key in _DERIVED_KEYS:
        candidate.pop(key, None)
    if failures:
        return None, failures, []
    # A load that refuses the record raises here; the caller turns that into a cannot-convert row whose message holds the
    # kind of error only (the error text may quote a stored value).
    normalized = service_registry.normalize_workflow_record(record_id, candidate)
    normalized.setdefault("policy", facts)  # a loader that keeps the facts has put them there already
    changed = {str(note.path[0]) for note in notes if note.path} | set(stated)
    return normalized, [], [*sorted(changed), "policy"]


# Returned in place of an entry to store when a record's stored entry is dropped (a patch that now equals the release
# record). It is distinct from None, which is a stored value: a JSON null record is kept as it is.
_OMIT: Any = object()


def _failed_entry(entry: Any, failures: list[Failure]) -> Any:
    """What is stored for a record that cannot convert: as it was, disabled, with the reasons.

    A ``$replace`` envelope stays the only key of its entry (a registry read takes an entry as a replacement only then):
    the flag and the reasons go into the replaced document."""
    if not isinstance(entry, Mapping):
        return copy.deepcopy(entry)
    if set(entry) == {layers.REPLACE_KEY}:
        if not isinstance(entry[layers.REPLACE_KEY], Mapping):
            return copy.deepcopy(dict(entry))
        return {layers.REPLACE_KEY: _failed_entry(entry[layers.REPLACE_KEY], failures)}
    kept = copy.deepcopy(dict(entry))
    kept["enabled"] = False
    kept["migration"] = {"step": STEP, "errors": [
        {"pointer": item.pointer, "code": item.code, "message": item.message} for item in failures[:MAX_STORED_ERRORS]]}
    return kept


def _schema_version(entry: Mapping[str, Any]) -> Any:
    manifest = entry.get("manifest") if isinstance(entry.get("manifest"), Mapping) else entry
    return manifest.get("schema_version")


# -- the plan ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """What the plan does with one stored workflow record. Ids, names and codes: no value of the record."""

    id: str
    action: str
    fields: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()  # (pointer, code)
    overrides_bundled: bool = False
    package: str | None = None


@dataclass
class Plan:
    """The stored document after the step, and what the step did to each record."""

    stored: dict[str, Any]
    rows: list[Row] = field(default_factory=list)
    # A converted record a package imported: id -> (the entry as stored, the record as a load now reads it).
    package_records: dict[str, tuple[Any, dict[str, Any]]] = field(default_factory=dict)

    @property
    def changes(self) -> bool:
        return any(row.action != ALREADY_V2 for row in self.rows)

    def report(self) -> dict[str, Any]:
        """Record ids, field names, error pointers and codes. No value of any record, and no error message."""
        counts = {action: sum(row.action == action for row in self.rows)
                  for action in (CONVERT_WHOLE, CONVERT_PATCH, ALREADY_V2, CANNOT_CONVERT)}
        return {
            "counts": counts,
            "records": [
                {"id": row.id, "action": row.action, "fields": list(row.fields),
                 "overrides_bundled": row.overrides_bundled, "package": row.package}
                for row in self.rows if row.action != CANNOT_CONVERT
            ],
            "needs_configuration": [
                {"id": row.id, "errors": [{"pointer": pointer, "code": code} for pointer, code in row.errors]}
                for row in self.rows if row.action == CANNOT_CONVERT
            ],
        }


def plan_workflow_schema_2(
    stored: Mapping[str, Any],
    release: Mapping[str, Any],
    *,
    v1_release: Mapping[str, Any] | None = None,
    bundled_ids: frozenset[str] | set[str] = frozenset(),
) -> Plan:
    """The owner-data document with every stored workflow record at schema 2, and the report of what changed.

    ``release`` is the effective release registry (its ``workflows`` are the schema 2 release records); ``v1_release`` is
    the schema 1 form of those records by id (default: the frozen copy); ``bundled_ids`` marks the records that override a
    bundled workflow in the report."""
    v1_release = frozen_release_records() if v1_release is None else v1_release
    out = copy.deepcopy(dict(stored))
    owned = out.get("workflows")
    plan = Plan(stored=out)
    if not isinstance(owned, Mapping) or not owned:
        return plan
    release_records = release.get("workflows") if isinstance(release.get("workflows"), Mapping) else {}
    layered = layers.is_layered(stored)
    new_owned: dict[str, Any] = {}
    for record_id in sorted(owned, key=str):
        entry = owned[record_id]
        base = release_records.get(record_id) if layered else None
        try:
            row, new_entry = _plan_one(str(record_id), entry, base, v1_release.get(record_id), plan)
        except Exception as error:  # noqa: BLE001 - one malformed record must not stop the others (or the start)
            # The code names the kind of failure; the message and the error text may quote the record, so neither is used.
            failure = [Failure("/", "conversion_failed", f"the record could not be converted ({type(error).__name__})")]
            plan.package_records.pop(str(record_id), None)
            row, new_entry = _failed_row(str(record_id), failure), _failed_entry(entry, failure)
        row = Row(row.id, row.action, row.fields, row.errors, str(record_id) in bundled_ids, _package_of(entry))
        plan.rows.append(row)
        if new_entry is not _OMIT:
            new_owned[record_id] = new_entry
    out["workflows"] = new_owned
    return plan


def _package_of(entry: Any) -> str | None:
    provenance = entry.get("provenance") if isinstance(entry, Mapping) else None
    package = provenance.get("package_id") if isinstance(provenance, Mapping) else None
    return str(package) if package else None


def _plan_one(
    record_id: str, entry: Any, base: Any, v1_base: Any, plan: Plan,
) -> tuple[Row, Any]:
    """``(row, entry to store)``; ``_OMIT`` leaves nothing stored (the patch now equals the release record)."""
    if not isinstance(entry, Mapping):
        failure = [Failure("/", "record_not_object", "the stored record is not an object")]
        return _failed_row(record_id, failure), copy.deepcopy(entry)
    if base is not None:  # an entry over a release record: a patch, or a replacement
        return _plan_patch(record_id, entry, base, v1_base)
    if set(entry) == {layers.REPLACE_KEY} and isinstance(entry[layers.REPLACE_KEY], Mapping):
        entry = entry[layers.REPLACE_KEY]  # no release record to replace: it is stored whole, as a read takes it
    if _schema_version(entry) == 2:
        return Row(record_id, ALREADY_V2), copy.deepcopy(dict(entry))
    record, failures, fields = _convert_record(record_id, entry)
    if record is None:
        return _failed_row(record_id, failures), _failed_entry(entry, failures)
    if _package_of(entry):  # the install proof is of the record as a load reads it back
        plan.package_records[record_id] = (
            copy.deepcopy(dict(entry)), service_registry.normalize_workflow_record(record_id, record))
    return Row(record_id, CONVERT_WHOLE, tuple(fields)), record


def _failed_row(record_id: str, failures: list[Failure]) -> Row:
    return Row(record_id, CANNOT_CONVERT, errors=tuple((item.pointer, item.code) for item in failures[:MAX_STORED_ERRORS]))


def _is_v2_patch(entry: Mapping[str, Any]) -> bool:
    """Whether a stored entry over a release record needs no conversion: it is a replacement that is schema 2, or it
    has no ``manifest`` key (a record field such as ``enabled`` means the same in both schemas), or its manifest patch
    says schema 2. A manifest patch that says nothing of the version is a schema 1 patch. A ``manifest`` that is not an
    object is not a patch: ``_plan_patch`` refuses it before this is asked."""
    if layers.REPLACE_KEY in entry:
        replaced = entry[layers.REPLACE_KEY]
        return isinstance(replaced, Mapping) and _schema_version(replaced) == 2
    manifest = entry.get("manifest")
    return "manifest" not in entry or (isinstance(manifest, Mapping) and manifest.get("schema_version") == 2)


def _plan_patch(record_id: str, entry: Mapping[str, Any], base: Mapping[str, Any], v1_base: Any) -> tuple[Row, Any]:
    """A stored entry over the release record ``base`` (schema 2): apply it to the schema 1 release record, convert the
    result, and store the difference from ``base``, so the effective record is the conversion of the whole patched
    schema 1 record."""
    if layers.REPLACE_KEY not in entry and "manifest" in entry and not isinstance(entry["manifest"], Mapping):
        # Layered over the release record, a null manifest would delete it and a scalar would replace it.
        failure = [Failure("/manifest", "manifest_not_object", "the manifest of a patch is not an object")]
        return _failed_row(record_id, failure), _failed_entry(entry, failure)
    if _is_v2_patch(entry):
        return Row(record_id, ALREADY_V2), copy.deepcopy(dict(entry))
    if not isinstance(v1_base, Mapping):
        failure = [Failure("/", "release_record_unknown", "no schema 1 release record is known to apply this patch to")]
        return _failed_row(record_id, failure), _failed_entry(entry, failure)
    # The release record's own fields (its source, whether it is editable) are the record's unless the patch says otherwise.
    v1_record = {**{k: v for k, v in base.items() if k not in ("manifest", "identity", "summary", "policy")},
                 "manifest": v1_base["manifest"]}
    record, failures, fields = _convert_record(record_id, layers.apply_entry(v1_record, entry))
    if record is None:
        return _failed_row(record_id, failures), _failed_entry(entry, failures)
    if record.get("policy") == v1_effective_facts(v1_base["manifest"]) and "policy" not in entry:
        # The owner did not change the facts: the release record's stay, so none is stored.
        record = {key: value for key, value in record.items() if key != "policy"}
        if "policy" in base:
            record["policy"] = copy.deepcopy(base["policy"])
        fields = [name for name in fields if name != "policy"]
    new_entry = layers.record_entry(base, record)
    if new_entry is None:
        return Row(record_id, CONVERT_PATCH, tuple(fields)), _OMIT
    if layers.REPLACE_KEY not in new_entry:
        for key in layers.IDENTITY_KEYS:
            if key in base and key not in new_entry:
                new_entry[key] = copy.deepcopy(base[key])
    return Row(record_id, CONVERT_PATCH, tuple(fields)), new_entry


# -- apply -------------------------------------------------------------------------------------------------------------

AUDIT_ACTION = "operator.service_registry.workflow_schema_2"
PACKAGE_INDEX_KEY = "extension_package_index"


def _refresh_package_proofs(plan: Plan, path: Path, actor: str):
    """The in-transaction part of the apply: a package keeps a keyed proof of each record it installed, to tell whether
    the owner changed it. A converted record would stop matching, and the package could no longer remove it, so a record
    that still matched its proof gets the proof of its converted form. One that did not match keeps a proof that matches
    nothing: the owner had changed it, and it stays detached."""
    from gateway import admin_store, extension_packages, record_integrity

    def run(conn, now: int) -> None:
        row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (PACKAGE_INDEX_KEY,)).fetchone()
        if row is None or not plan.package_records:
            return
        index = extension_packages.normalize_extension_package_index(json.loads(row[0]))
        key: bytes | None = None
        changed = False
        for record_id, (old, new) in plan.package_records.items():
            package = index["packages"].get(_package_of(old))
            entry = (((package or {}).get("installed_records") or {}).get("workflows") or {}).get(record_id)
            if not isinstance(entry, dict) or "mac" not in entry:
                continue
            key = key or record_integrity.install_key(path)
            state = ((package.get("disabled_states") or {}).get("workflows") or {}).get(record_id)
            if not extension_packages._matches_install(old, entry, package, state, key):
                continue
            entry["mac"] = extension_packages.installed_record_digest(extension_packages.comparable_record(new, state), key)
            changed = True
        if changed:
            admin_store._upsert_setting_in_transaction(
                conn, PACKAGE_INDEX_KEY, json.dumps(index, ensure_ascii=False, sort_keys=True), now, actor)

    return run


def ensure(*, path: Path, actor: str = "system", release: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Run the step once: the stored registry after it, or None when there is no stored registry to migrate.

    Run at start, after the layering step and before anything reads the workflow records. The step is recorded in the same
    transaction as the registry revision it writes (and the package proofs it refreshes), so it never runs twice. A
    registry with nothing to convert only records the step. Bounded retry on a concurrent write, as ``ensure_service_registry``."""
    from gateway import admin_store
    from gateway.discovery import bundled_workflow_manifests

    release = service_registry.release_registry() if release is None else release
    for _attempt in range(3):
        # The marker is read before the registry and again after it. A "done" before the read means the read is at least as
        # new as the step's own write, so it is returned. A "done" only after the read means another start finished in
        # between: the read may be from before or after its write, so read again. A start that finishes after the second
        # marker read moves the generation, so the write below conflicts and the next attempt sees "done".
        done = admin_store.migration_step_recorded(STEP, path=path)
        record = admin_store.get_service_registry_json_with_generation(path=path)
        if record is None or not service_registry._parses_as_object(record["value_json"]):
            return None
        stored = service_registry._stored_document(record["value_json"])
        if done:
            return stored
        if admin_store.migration_step_recorded(STEP, path=path):
            continue
        generation = int(record["operator_settings_generation"])
        plan = plan_workflow_schema_2(stored, release, bundled_ids=frozenset(bundled_workflow_manifests()))
        if not plan.changes:
            try:  # recorded only if the registry is still the one planned on: a record written since was never looked at
                admin_store.record_migration_step_at_registry_generation(STEP, expected_generation=generation, path=path)
            except admin_store.OperatorSettingsGenerationConflict:
                continue
            return stored
        counts = plan.report()["counts"]
        try:
            admin_store.write_service_registry_with_audit(
                plan.stored,
                expected_generation=generation,
                updated_by=actor,
                audit_action=AUDIT_ACTION,
                audit_actor_kind="admin",
                audit_actor_id=actor,
                audit_actor_label="System",
                audit_target_kind="operator_setting",
                audit_target_id=service_registry.SERVICE_REGISTRY_SETTING_KEY,
                audit_metadata={"migration_step": STEP, **counts},
                session=None,  # the gateway migrating its own registry at start: no browser sign-in asked
                path=path,
                migration_step=STEP,
                stored_form=True,
                also_in_transaction=_refresh_package_proofs(plan, path, actor),
            )
        except admin_store.OperatorSettingsGenerationConflict:
            continue
        return plan.stored
    raise RuntimeError("service registry changed during bounded startup retry")
