"""The three owner settings that lived in ``operator_settings`` before the config layer: instance, media admission,
service availability (docs/design/CONFIG_HOMES.md, 3.3 and slice C6).

Each is a section of the generic layer now: release defaults in ``registry/defaults/<section>.yml``, the owner's changes
as sparse override rows in ``config_overrides``, one revision per section (``operator_settings_generations`` key
``config:<section>``). This module is the seam the three existing routes and the request-path readers use, so their
responses stay what they were:

- ``read`` / ``instance`` / ``media_job_admission`` / ``service_availability`` build the effective value from the
  stored rows on every call, as the old readers read the stored document on every call. They do not wait for a reload.
- ``put_instance`` / ``put_media_job_admission`` / ``put_service_availability`` write one change through
  ``admin_store.apply_config_change``, keep each route's own audit action (``operator.instance.update`` and so on) and
  count a save of the same value as a new revision, as the old store did.
- ``migrate`` imports the old ``operator_settings`` documents (and, for instance and media admission, the startup
  file and environment values that used to sit under them) as override rows, once per section. The old keys stay
  where they are, unread, until a later slice deletes them, so a rollback to the commit before this one still finds them.

Rows are sparse. An instance field equal to the release default has no row. A media admission document is written
whole (a key the owner saved, even an empty list, is a row, because the saved document used to override the file for
every key it held). A service availability member is one row; a member that is back to "available" has none.
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway import admin_store, config_layers as layers, config_runtime
from gateway.compat import get_env

INSTANCE = "instance"
ADMISSION = "resource_admission"
AVAILABILITY = "service_availability"
SECTIONS = (INSTANCE, ADMISSION, AVAILABILITY)

# The keys the three settings had in operator_settings. They name the old audit targets and the one-time import;
# nothing reads them after the import.
LEGACY_KEYS = {
    INSTANCE: "instance_presentation",
    ADMISSION: "media_job_admission_overrides",
    AVAILABILITY: "service_availability_overrides",
}
AUDIT_ACTIONS = {
    INSTANCE: "operator.instance.update",
    ADMISSION: "operator.media_job_admission.update",
    AVAILABILITY: "operator.service_availability.update",
}
STEP_PREFIX = "config-homes-import:"

INSTANCE_FIELDS = ("name", "base_url", "theme_default")
MEDIA_FIELDS = (
    "disabled_intents", "disabled_resource_classes", "reserved_tokens", "max_queue_depth", "max_app_concurrency",
    "retry_after_seconds",
)
MEDIA_PREFIX = "/media_jobs"
_RETRIES = 3

_home_lock = threading.Lock()
_home: config_runtime.ConfigHome | None = None
_built: dict[tuple, dict[str, Any]] = {}
_BUILT_MAX = 256


def home() -> config_runtime.ConfigHome:
    """The three sections of this slice, loaded from the release files. Private to this module: the gateway's own
    ``ConfigHome`` (``proxy.CONFIG_HOME``) holds the same files and publishes the snapshot."""
    global _home
    with _home_lock:
        if _home is None:
            shipped = config_runtime.load_sections()
            _home = config_runtime.ConfigHome({name: shipped[name] for name in SECTIONS})
        return _home


# ---------------------------------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SectionState:
    effective: dict[str, Any]
    revision: str
    rows: tuple[dict[str, Any], ...]


def read(path: Path, section: str) -> SectionState:
    """The effective section and its revision for the rows stored now. Raises when the store cannot be read."""
    stored = admin_store.read_config_section(section, path=path)
    rows = tuple(stored["rows"])
    key = (section, tuple((row["pointer"], row["op"], row["value_json"]) for row in rows))
    built = _built.get(key)
    if built is None:
        overrides = [config_runtime.override_from_row(row) for row in rows]
        h = home()
        built = layers.build_section(h.sections[section], overrides, None, merge=h.merge).effective
        if len(_built) >= _BUILT_MAX:
            _built.clear()
        _built[key] = built
    return SectionState(copy.deepcopy(built), stored["revision"], rows)


def instance(path: Path) -> dict[str, str]:
    return read(path, INSTANCE).effective


def instance_with_revision(path: Path) -> tuple[dict[str, str], str]:
    state = read(path, INSTANCE)
    return state.effective, state.revision


def media_job_admission(path: Path) -> tuple[dict[str, Any], str]:
    """The owner's media admission document (the keys saved, as saved) and its revision."""
    state = read(path, ADMISSION)
    media = state.effective.get("media_jobs")
    return (dict(media) if isinstance(media, Mapping) else {}), state.revision


def service_availability(path: Path) -> dict[str, Any]:
    return read(path, AVAILABILITY).effective


# ---------------------------------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------------------------------

def _release_base(section: str, pointer: str) -> str | None:
    value = layers.get_at(home().sections[section].defaults, pointer)
    return None if value is layers.ABSENT else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _items(section: str, rows: Mapping[str, Mapping[str, Any]], desired: Mapping[str, Any]) -> list[admin_store.ConfigItem]:
    """The edits that make the stored rows hold ``desired`` (pointer -> value, or ABSENT for no row)."""
    items: list[admin_store.ConfigItem] = []
    for pointer, value in sorted(desired.items()):
        have = rows.get(pointer)
        if value is layers.ABSENT:
            if have is not None:
                items.append(admin_store.ConfigItem(section, pointer, "reset"))
        elif have is None or have["op"] != "set" or not layers.same_value(json.loads(have["value_json"]), value):
            items.append(admin_store.ConfigItem(
                section, pointer, "set", json.dumps(value, ensure_ascii=False), _release_base(section, pointer)))
    return items


def _apply(
    path: Path, section: str, plan: Any, actor: Any, *, expected_revision: str | None,
    metadata: Callable[[Any], Mapping[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    """Commit what ``plan(state)`` asks for. ``plan`` returns ``(desired, result)`` for the state it is given, and
    ``metadata(result)`` is the audit row's metadata.

    With ``expected_revision`` the change is made against that revision only, and a section that moved raises
    ``admin_store.ConfigRevisionMismatch``. Without it the change is made against the revision read just before, and
    is read and planned again (a few times) when another writer got in between: the old route had no precondition.
    """
    attempts = 1 if expected_revision is not None else _RETRIES
    for attempt in range(attempts):
        state = read(path, section)
        desired, result = plan(state)
        items = _items(section, {row["pointer"]: row for row in state.rows}, desired)
        try:
            outcome = admin_store.apply_config_change(
                items, if_match={section: state.revision if expected_revision is None else expected_revision},
                kind="edit", origin="api", validate=home().write_validator(None),
                updated_by=actor.subject, actor_kind=actor.actor_kind, actor_id=actor.audit_actor_id,
                actor_label=actor.actor_label, sections=[section], always=True,
                audit={"action": AUDIT_ACTIONS[section], "target_kind": "operator_setting",
                       "target_id": LEGACY_KEYS[section], "metadata": dict(metadata(result))},
                path=path,
            )
        except admin_store.ConfigRevisionMismatch:
            if attempt == attempts - 1:
                raise
            continue
        return result, outcome
    raise AssertionError("unreachable")


def put_instance(path: Path, document: Mapping[str, str], actor: Any, *, expected_revision: str) -> None:
    """Save the whole instance document (name, base_url, theme_default). A field equal to the release default has no row."""
    defaults = home().sections[INSTANCE].defaults

    def plan(_state: SectionState) -> tuple[dict[str, Any], None]:
        desired = {
            f"/{field}": layers.ABSENT if document[field] == defaults.get(field) else document[field]
            for field in INSTANCE_FIELDS
        }
        return desired, None

    _apply(path, INSTANCE, plan, actor, expected_revision=expected_revision,
           metadata=lambda _result: {"fields": sorted(document)})


def put_media_job_admission(path: Path, document: Mapping[str, Any], actor: Any, *, expected_revision: str) -> str:
    """Save the whole media admission document and answer the new revision. A key not in ``document`` has no row."""

    def plan(_state: SectionState) -> tuple[dict[str, Any], None]:
        return {f"{MEDIA_PREFIX}/{field}": document.get(field, layers.ABSENT) for field in MEDIA_FIELDS}, None

    _result, outcome = _apply(
        path, ADMISSION, plan, actor, expected_revision=expected_revision,
        metadata=lambda _result: {"fields": sorted(document)})
    return outcome["revisions"][ADMISSION]


def put_service_availability(
    path: Path, document: Mapping[str, Mapping[str, Any]], actor: Any, *, expected_revision: str | None = None,
) -> dict[str, Any]:
    """Save the whole availability document: each member is a row, a member stored before and not in it loses its row."""

    def plan(state: SectionState) -> tuple[dict[str, Any], dict[str, Any]]:
        desired: dict[str, Any] = {layers.join_pointer([key]): layers.ABSENT for key in state.effective}
        desired.update({layers.join_pointer([key]): dict(record) for key, record in document.items()})
        return desired, dict(sorted((key, dict(record)) for key, record in document.items()))

    merged, _outcome = _apply(
        path, AVAILABILITY, plan, actor, expected_revision=expected_revision,
        metadata=lambda result: {"override_count": len(result)})
    return merged


# ---------------------------------------------------------------------------------------------------------------------
# The one-time import of the old stores
# ---------------------------------------------------------------------------------------------------------------------

def startup_instance(raw_config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The instance values the startup file and environment gave before this slice (the old route's defaults)."""
    admin_block = raw_config.get("admin")
    brand = admin_block.get("brand") if isinstance(admin_block, Mapping) else None
    proxy_block = raw_config.get("proxy")
    brand = brand if isinstance(brand, Mapping) else {}
    proxy_block = proxy_block if isinstance(proxy_block, Mapping) else {}
    defaults = home().sections[INSTANCE].defaults
    return {
        "name": str(get_env("AZIZAM_BRAND", environ=environ) or brand.get("product") or defaults["name"]),
        "base_url": str(get_env("AZIZAM_PUBLIC_URL", environ=environ) or proxy_block.get("public_url") or defaults["base_url"]),
        "theme_default": defaults["theme_default"],
    }


def _startup_media_jobs(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    gateway = raw_config.get("gateway")
    admission = gateway.get("resource_admission") if isinstance(gateway, Mapping) else None
    media = admission.get("media_jobs") if isinstance(admission, Mapping) else None
    return dict(media) if isinstance(media, Mapping) else {}


def _stored_document(path: Path, section: str) -> dict[str, Any]:
    record = admin_store.get_operator_setting(LEGACY_KEYS[section], path=path)
    value = record["value"] if record else None
    return dict(value) if isinstance(value, Mapping) else {}


def _legacy_target(section: str, path: Path, raw_config: Mapping[str, Any], environ: Mapping[str, str] | None) -> dict[str, Any]:
    """What the section's effective value was before this slice, as one document."""
    stored = _stored_document(path, section)
    if section == INSTANCE:
        return {**startup_instance(raw_config, environ), **stored}
    if section == ADMISSION:
        return {"media_jobs": {**_startup_media_jobs(raw_config), **{k: v for k, v in stored.items() if k in MEDIA_FIELDS}}}
    return stored


def _without(target: Mapping[str, Any], entry: str) -> dict[str, Any]:
    document = copy.deepcopy(dict(target))
    segments = layers.split_pointer(entry)
    node: Any = document
    for segment in segments[:-1]:
        node = node.get(segment) if isinstance(node, dict) else None
    if isinstance(node, dict):
        node.pop(segments[-1], None)
    return document


def _fit(section: layers.Section, target: Mapping[str, Any]) -> tuple[list[layers.Override], list[str]]:
    """Overrides that make the section equal ``target``, leaving out each entry the section refuses.

    An entry the closed schema does not take (a value out of range, a key it does not know) is dropped and named, so
    one bad key does not cost the owner every other value. A fault the section cannot place on one entry (it has no
    pointer, or dropping the entry changes nothing) leaves the whole import empty: the step is then recorded with
    no rows, and the log line says what was left out.
    """
    dropped: list[str] = []
    for _round in range(50):
        overrides, blocked = layers.overrides_for_target(section, target, None)
        if not blocked:
            return overrides, dropped
        places = {
            section.index.entry_containing(problem.pointer) or problem.pointer
            for problem in blocked if problem.pointer not in (None, "", "/")
        }
        if len(places) != len(blocked) and any(problem.pointer in (None, "", "/") for problem in blocked):
            return [], [*dropped, "/"]
        smaller = target
        for place in sorted(places):
            smaller = _without(smaller, place)
        if smaller == target:
            return [], [*dropped, "/"]
        dropped.extend(sorted(places))
        target = smaller
    return [], [*dropped, "/"]


def migrate(path: Path, raw_config: Mapping[str, Any], *, environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Import the old stores as override rows, once per section, and say what was done (one record per section).

    A section whose step is recorded is left alone. A section that already has override rows (the owner set them
    through the config API before this ran) is not touched either: its step is recorded and nothing is written. The
    old ``operator_settings`` documents are only read.
    """
    admin_store.init_db(path)
    report: list[dict[str, Any]] = []
    h = home()
    for name in SECTIONS:
        step = STEP_PREFIX + name
        if admin_store.migration_step_done(step, path=path):
            continue
        section = h.sections[name]
        state = admin_store.read_config_section(name, path=path)
        if state["rows"]:
            admin_store.mark_migration_step(step, path=path)
            report.append({"section": name, "step": step, "rows": 0, "dropped": [], "note": "the section already has rows"})
            continue
        overrides, dropped = _fit(section, _legacy_target(name, path, raw_config, environ))
        if overrides:
            items = [
                admin_store.ConfigItem(
                    name, override.pointer, override.op,
                    json.dumps(override.value, ensure_ascii=False) if override.op == layers.OP_SET else None,
                    None if override.base is layers.ABSENT else json.dumps(override.base, ensure_ascii=False, sort_keys=True),
                )
                for override in overrides
            ]
            try:
                admin_store.apply_config_change(
                    items, if_match={name: state["revision"]}, kind="legacy-import", origin=layers.ORIGIN_LEGACY,
                    note=f"imported from {LEGACY_KEYS[name]} and the startup values", actor_kind="system",
                    actor_id="startup", actor_label="startup import", validate=None, migration_step=step, path=path)
            except admin_store.ConfigRevisionMismatch:
                continue  # another writer got in first; the next boot looks again
        else:
            admin_store.mark_migration_step(step, path=path)
        report.append({"section": name, "step": step, "rows": len(overrides), "dropped": dropped})
    return report
