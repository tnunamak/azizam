"""Runtime glue for layered config sections: load, snapshot, publish (docs/design/CONFIG_HOMES.md sections 3.5 and 5).

``gateway/config_layers.py`` is pure. This module reads the section files, reads the stored overrides, builds the
effective sections through the pure layer, and carries the result as one ``RuntimeSnapshot`` that is published by a
single reference swap. A request binds the snapshot that is current when it starts (``SnapshotMiddleware``), so a reload
that lands mid-request cannot change what that request reads.

With no section registered (the state of this release) ``ConfigHome.layer`` returns the startup config object itself and
reads no table: the effective config is exactly what it was before the layer existed.

Load and write share one function, ``ConfigHome.build_candidate``: it builds every section from its rows and judges
them together, so the order of the section ids never changes a result.
"""
from __future__ import annotations

import contextvars
import copy
import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from gateway import config_layers as layers

DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "registry" / "defaults"
SCHEMA_SUFFIX = ".schema.json"
CODE_STORE_UNREADABLE = "config_store_unreadable"
CODE_UNSETTLED = "config_unsettled"
SOURCE_STARTUP_FILE = "startup_file"  # a section serving its startup block because the import was blocked


def _print_log(line: str) -> None:
    print(line, flush=True)


def load_sections(
    defaults_dir: Path = DEFAULTS_DIR, validators: Mapping[str, layers.SemanticValidator] | None = None,
) -> dict[str, layers.Section]:
    """Every section the release ships: ``<id>.yml`` with ``<id>.schema.json``. A missing directory means none.

    A broken pair is a packaging fault, not an owner fault: ``SectionDataError`` names every rule broken, and the
    caller stops (a release that ships broken defaults must not boot with a guess).
    """
    if not defaults_dir.is_dir():
        return {}
    ids = {path.name[: -len(SCHEMA_SUFFIX)] for path in defaults_dir.glob("*" + SCHEMA_SUFFIX)}
    for defaults_path in sorted(defaults_dir.glob("*.yml")):
        if defaults_path.stem not in ids:
            raise layers.SectionDataError(f"section {defaults_path.stem}: {defaults_path.stem}{SCHEMA_SUFFIX} is missing")
    sections: dict[str, layers.Section] = {}
    for section_id in sorted(ids):
        defaults_path = defaults_dir / f"{section_id}.yml"
        if not defaults_path.is_file():
            raise layers.SectionDataError(f"section {section_id}: {defaults_path.name} is missing")
        defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
        schema = json.loads((defaults_dir / f"{section_id}{SCHEMA_SUFFIX}").read_text(encoding="utf-8"))
        sections[section_id] = layers.Section.create(
            section_id, defaults, schema, validator=(validators or {}).get(section_id),
        )
    return sections


def override_from_row(row: Mapping[str, Any]) -> layers.Override:
    """A stored row as the pure layer's ``Override``. ``base_json`` NULL means the release had no entry."""
    return layers.Override(
        pointer=row["pointer"], op=row["op"],
        value=json.loads(row["value_json"]) if row["value_json"] is not None else None,
        base=json.loads(row["base_json"]) if row["base_json"] is not None else layers.ABSENT,
        origin=row.get("origin") or "api", note=row.get("note"), change_id=row.get("change_id"),
        updated_at=row.get("updated_at"), updated_by=row.get("updated_by"),
    )


@dataclass(frozen=True)
class Layer:
    """What the override layer made of one startup config."""

    config: Mapping[str, Any]
    revisions: tuple[tuple[str, str], ...] = ()
    rows_digest: str = ""
    results: Mapping[str, layers.SectionResult] = field(default_factory=dict)
    sources: Mapping[str, Any] = field(default_factory=dict)
    problems: Mapping[str, Any] = field(default_factory=dict)

    @property
    def sections(self) -> tuple[str, ...]:
        return tuple(self.results)


NO_LAYER = Layer(config={})


class LayeredConfig(dict):
    """The effective config as a plain mapping, carried from a loader to the publisher with what built it.

    Readers see an ordinary dict. The loader's callers (``proxy._load_effective_runtime_config`` has patched stand-ins
    that return a plain dict) hand it on; ``carried`` reads the layer and the registry generation back out, and the
    publisher passes both to ``ConfigHome.snapshot`` explicitly. The snapshot holds the problems itself, so no reader
    depends on this attribute and a ``dict(cfg)`` copy loses nothing that was published.
    """

    layer: Layer = NO_LAYER
    registry_generation: Any = None

    def __init__(self, effective: Mapping[str, Any], layer: Layer, registry_generation: Any = None):
        super().__init__(effective)
        self.layer = layer
        self.registry_generation = registry_generation


def carried(config: Mapping[str, Any]) -> tuple[Layer, Any]:
    """The layer and registry generation a loader attached to ``config``; a plain dict carries none."""
    return getattr(config, "layer", NO_LAYER), getattr(config, "registry_generation", None)


@dataclass(frozen=True)
class RuntimeSnapshot:
    """One immutable view of the running config, published by a single reference swap."""

    generation: tuple
    config: Mapping[str, Any]
    sources: Mapping[str, Any]
    problems: Mapping[str, Any]
    derived: Mapping[str, Any]


class SnapshotHolder:
    """The latest published snapshot. ``publish`` is one reference assignment, so a reader gets the old or the new."""

    def __init__(self) -> None:
        self._current: RuntimeSnapshot | None = None

    def current(self) -> RuntimeSnapshot | None:
        return self._current

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self._current = snapshot


_BOUND: contextvars.ContextVar[RuntimeSnapshot | None] = contextvars.ContextVar("config_snapshot", default=None)


def bound_snapshot() -> RuntimeSnapshot | None:
    """The snapshot bound to the request this code runs in, or None outside a request."""
    return _BOUND.get()


class SnapshotMiddleware:
    """ASGI middleware: bind the snapshot ``current()`` returns at request start and keep it to the request's end."""

    def __init__(self, app: Any, current: Callable[[], RuntimeSnapshot]) -> None:
        self.app = app
        self.current = current

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = _BOUND.set(self.current())
        try:
            await self.app(scope, receive, send)
        finally:
            _BOUND.reset(token)


def _touched_pointers(
    stored: Mapping[str, Sequence[Mapping[str, Any]]], after: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, set[str]]:
    """The pointers whose row a change adds, replaces or removes, per section."""
    touched: dict[str, set[str]] = {}
    for name, rows in after.items():
        was = {row["pointer"]: (row["op"], row["value_json"]) for row in stored.get(name, [])}
        now = {row["pointer"]: (row["op"], row["value_json"]) for row in rows}
        touched[name] = {pointer for pointer in was.keys() | now.keys() if was.get(pointer) != now.get(pointer)}
    return touched


def _on_touched(pointer: str | None, touched: Any) -> bool:
    """Whether a problem at ``pointer`` is on, above or below a touched entry."""
    if not pointer:
        return False
    return any(pointer == t or pointer.startswith(t + "/") or t.startswith(pointer + "/") for t in touched)


def _present(value: Any) -> dict[str, Any]:
    return {"present": False} if value is layers.ABSENT else {"present": True, "value": value}


class ConfigHome:
    """The sections this release ships, the latest snapshot and the state of the last reload."""

    def __init__(
        self, sections: Mapping[str, layers.Section] | None = None, *,
        defaults_dir: Path = DEFAULTS_DIR, merge: layers.MergeFn = layers.apply_overrides,
    ) -> None:
        self.sections: dict[str, layers.Section] = dict(sections) if sections is not None else load_sections(defaults_dir)
        self.merge = merge
        self.verify_release_defaults()
        self.holder = SnapshotHolder()
        self._lock = threading.Lock()
        self._last_commit: tuple[str | None, tuple[tuple[str, str], ...]] | None = None
        self.reload_failed: dict[str, Any] | None = None
        # Sections whose startup block could not be imported at the last boot, with that block: a reload keeps serving
        # the block, as the boot did, until a later boot imports it (design 8.1 step 1).
        self._unimported: dict[str, tuple[Any, tuple[layers.Problem, ...]]] = {}
        self._warned_ignored: set[str] = set()

    def verify_release_defaults(self) -> None:
        """Stop when the release's own defaults break a section's rule. A packaging fault, like a broken schema.

        The check runs at import, before any registry is read, so a rule sees ``context["registry"]`` as ``None`` here.
        A rule that needs the registry cannot run yet: its ``validator_error`` is not a finding of this check, and the
        rule runs for real, with the registry, at every build (``layer``), where what it finds is a reported problem.
        """
        found = [
            f"section {name}: {problem.pointer or '/'}: {problem.message or problem.code}"
            for name, result in self.build_candidate({}).items() for problem in result.invalid
            if problem.code != layers.CODE_VALIDATOR_ERROR
        ]
        if found:
            raise layers.SectionDataError("the release defaults break their rules:\n  " + "\n  ".join(found[:20]))

    # -- build ---------------------------------------------------------------------------------------------------------

    def layer(
        self, raw_config: Mapping[str, Any], *, path: Path, registry: Mapping[str, Any] | None = None,
        legacy_import: bool = False, log: Callable[[str], None] | None = None,
    ) -> Layer:
        """The startup config with each registered section replaced by its effective value. Never raises for an override.

        A store that cannot be read does raise: the caller keeps what is running, as for the registry. ``registry`` is
        the service registry the config will be applied with; cross-section rules see it in their context.

        ``legacy_import`` is a boot: a section not imported yet takes its startup block into the store first (design
        8.1), and one already imported says once per boot how many entries of its ignored block differ. ``log`` receives
        those lines (default: print).
        """
        if not self.sections:
            return Layer(config=raw_config)
        from gateway import admin_store  # lazy: pure users of this module need no database

        admin_store.init_db(path)
        if legacy_import:
            self._import_at_boot(raw_config, path=path, registry=registry, log=log or _print_log)
        state = admin_store.read_config_state(path=path)
        rows: dict[str, list[layers.Override]] = {}
        for row in state["rows"]:
            rows.setdefault(row["section"], []).append(override_from_row(row))
        revisions = tuple((name, state["revisions"].get(name, "0")) for name in sorted(self.sections))
        return self._layer_from(raw_config, rows, registry, revisions=revisions, rows_digest=layers.digest(state["rows"]))

    def _import_at_boot(
        self, raw_config: Mapping[str, Any], *, path: Path, registry: Mapping[str, Any] | None, log: Callable[[str], None],
    ) -> None:
        from gateway import config_legacy_import  # lazy: it imports this module

        found = config_legacy_import.import_at_boot(self, raw_config, path=path, registry=registry, log=log)
        self._unimported = dict(found.blocked)
        for section_id in found.done:
            block = raw_config.get(section_id)
            if section_id in self._warned_ignored or not isinstance(block, Mapping) or not block:
                continue
            effective = self.build_candidate(self._stored_rows(path), registry)[section_id].effective
            differing = config_legacy_import.ignored_entries(self.sections[section_id], block, effective)
            if differing:
                self._warned_ignored.add(section_id)
                log(f"[proxy] WARNING: the startup file's {section_id} block is ignored (it was imported once); "
                    f"{differing} of its entries differ from what runs. Review them with "
                    f"POST /admin/api/config/sections/{section_id}/legacy-import?dry_run=1.")

    def _stored_rows(self, path: Path) -> dict[str, list[layers.Override]]:
        from gateway import admin_store

        rows: dict[str, list[layers.Override]] = {}
        for row in admin_store.read_config_state(path=path)["rows"]:
            rows.setdefault(row["section"], []).append(override_from_row(row))
        return rows

    def defaults_layer(self, raw_config: Mapping[str, Any], error: BaseException, registry: Mapping[str, Any] | None = None) -> Layer:
        """The layer for a boot whose override store cannot be read: release defaults, and a problem that says so.

        Used only where there is no running config to keep (the last boot fallback); a reload raises instead.
        """
        if not self.sections:
            return Layer(config=raw_config)
        unreadable = layers.Problem(
            None, CODE_STORE_UNREADABLE, layers.INVALID,
            f"the owner's overrides could not be read ({type(error).__name__}); the release defaults are serving",
        )
        revisions = tuple((name, "unread") for name in sorted(self.sections))
        return self._layer_from(raw_config, {}, registry, revisions=revisions, rows_digest="", extra=(unreadable,))

    def _layer_from(
        self, raw_config: Mapping[str, Any], rows: Mapping[str, Sequence[layers.Override]], registry: Mapping[str, Any] | None,
        *, revisions: tuple[tuple[str, str], ...], rows_digest: str, extra: Sequence[layers.Problem] = (),
    ) -> Layer:
        results = self.build_candidate(rows, registry)
        merged = dict(raw_config)
        sources: dict[str, Any] = {}
        problems: dict[str, Any] = {}
        for name in sorted(results):
            result = results[name]
            merged[name] = result.effective
            sources[name] = {
                view.pointer: {"source": view.source, "apply": view.apply, "drift": view.drift.kind if view.drift else None}
                for view in layers.entry_views(self.sections[name], result, rows.get(name, []))
            }
            section_extra = tuple(extra)
            unimported = self._unimported.get(name)
            if unimported is not None:
                # The startup block could not be imported: it keeps serving, as before the section existed.
                block, faults = unimported
                merged[name] = copy.deepcopy(block)
                sources[name] = {pointer: {"source": SOURCE_STARTUP_FILE, "apply": "live", "drift": None}
                                 for pointer in self.sections[name].index.entries_in(block)}
                section_extra += tuple(layers.Problem(p.pointer, "legacy_import_blocked", layers.INVALID, p.message)
                                       for p in faults)
            found = [problem.as_dict() for problem in (*result.problems, *section_extra)]
            if found:
                problems[name] = found
        return Layer(
            config=merged, revisions=revisions, rows_digest=rows_digest, results=results, sources=sources, problems=problems,
        )

    def build_candidate(
        self, rows: Mapping[str, Sequence[layers.Override]], registry: Mapping[str, Any] | None = None,
    ) -> dict[str, layers.SectionResult]:
        """Every section built from its rows, judged together. The one function the load path and the write path share.

        Order of the sections never matters. Each section is built against the same context: the registry and, for
        every section, the value its rows would give if all of them applied (the owner's intent), so a valid pair of
        changes to two sections is not refused for the order they sort in. Then every section's rule is judged against
        the sections as built; a section that breaks it there is rebuilt against that context, its offending overrides
        dropped so it inherits, until nothing changes. What is left is part of the result, so nothing that breaks a
        rule across sections is published as if it were fine.
        """
        names = sorted(self.sections)
        intent: dict[str, Any] = {}
        for name in names:
            section = self.sections[name]
            usable = sorted(
                (row for row in rows.get(name, []) if not layers.check_override(section, row)), key=lambda o: o.pointer,
            )
            try:
                intent[name] = self.merge(section.defaults, usable)
            except layers.MergeError:
                intent[name] = section.defaults
        context: dict[str, Any] = {"registry": registry, "sections": intent}
        results = {
            name: layers.build_section(self.sections[name], rows.get(name, []), context, merge=self.merge) for name in names
        }
        # Settle. Judge every section against what the others really are; a section that now breaks its rule is rebuilt
        # against that context (its offending overrides drop out, so it inherits). All sections are judged on the same
        # context in a round and rebuilt together, so the order of the ids never changes the result. A rebuild can change
        # what another section's rule sees, so repeat until nothing changes: at most one round per section plus the
        # round that finds nothing to do. A chain that still moves after that is not settled: the sections that moved in
        # the last round serve their release defaults and say so (``config_unsettled``).
        unsettled: set[str] = set()
        moved: set[str] = set()
        for _round in range(len(names) + 1):
            context = {"registry": registry, "sections": {name: result.effective for name, result in results.items()}}
            broken = [name for name in names if self._breaks(name, results[name], context)]
            rebuilt = {
                name: layers.build_section(self.sections[name], rows.get(name, []), context, merge=self.merge) for name in broken
            }
            moved = {name for name in broken if rebuilt[name].effective != results[name].effective}
            results.update(rebuilt)
            if not moved:
                break
        else:
            unsettled = moved
            context = {"registry": registry, "sections": {name: result.effective for name, result in results.items()}}
            for name in sorted(unsettled):
                results[name] = layers.build_section(self.sections[name], [], context, merge=self.merge)
        context = {"registry": registry, "sections": {name: result.effective for name, result in results.items()}}
        for name in names:
            result = results[name]
            found = layers.validate_section(self.sections[name], result.effective, context)
            kept = [problem for problem in result.problems if problem.code == layers.CODE_OVERRIDE_REJECTED]
            extra = [layers.Problem(None, CODE_UNSETTLED, layers.INVALID, "the overrides of the sections do not settle; "
                                    "this section serves its release defaults")] if name in unsettled else []
            results[name] = replace(result, problems=(*found, *kept, *extra))
        return results

    def _breaks(self, name: str, result: layers.SectionResult, context: Mapping[str, Any]) -> bool:
        """Whether ``result`` breaks its rule against ``context`` in a way its own build did not already report."""
        own = layers.invalid_counter(problem for problem in result.problems if problem.code != layers.CODE_OVERRIDE_REJECTED)
        found = layers.invalid_counter(layers.validate_section(self.sections[name], result.effective, context))
        return bool(found - own)

    def write_validator(self, registry: Mapping[str, Any] | None = None) -> Callable[[Any, Mapping[str, list[dict[str, Any]]]], None]:
        """The check ``admin_store.apply_config_change`` runs inside its write transaction (design 3.5, write path).

        It builds the whole candidate config twice with ``build_candidate``, once from the stored rows and once from the
        stored rows with the change applied, and refuses (``ConfigChangeInvalid``, HTTP 422) two kinds of ``invalid``
        problem: any on an entry the change touches (a new bad value at a pointer that is already bad is refused, a
        value that fixes it or a reset is not), and, on every other entry, the problems the change adds. A problem that
        was already there on an entry the change does not touch (a stored row that went bad, release defaults that
        break a rule) does not refuse a later change. A ``dangling`` problem never refuses. Nothing is written when it
        raises.
        """
        from gateway import admin_store

        def validate(conn: Any, rows_after: Mapping[str, list[dict[str, Any]]]) -> None:
            found: list[dict[str, Any]] = []
            stored_rows: dict[str, list[dict[str, Any]]] = {}
            for row in admin_store.list_config_overrides_in(conn):
                stored_rows.setdefault(row["section"], []).append(row)
            stored = {name: [override_from_row(row) for row in rows] for name, rows in stored_rows.items()}
            candidate = {**stored, **{name: [override_from_row(row) for row in rows] for name, rows in rows_after.items()}}
            touched = _touched_pointers(stored_rows, rows_after)
            for name in sorted(rows_after):
                if name not in self.sections:
                    found.append({"section": name, "pointer": None, "code": "unknown_section",
                                  "message": f"this release ships no section {name!r}"})
            before = self.build_candidate(stored, registry)
            after = self.build_candidate(candidate, registry)
            for name in sorted(after):
                added = layers.invalid_counter(after[name].problems) - layers.invalid_counter(before[name].problems)
                found += [
                    {"section": name, **problem.as_dict()} for problem in after[name].problems
                    if problem.severity == layers.INVALID and (
                        _on_touched(problem.pointer, touched.get(name, ())) or (problem.pointer, problem.code, problem.message) in added
                    )
                ]
            if found:
                raise admin_store.ConfigChangeInvalid(found)

        return validate

    def attach(self, effective: Mapping[str, Any], layer: Layer, registry_generation: Any = None) -> dict[str, Any]:
        """``effective`` as the object to publish: a plain dict when no section is registered, else a ``LayeredConfig``.

        The registry generation is the one of the registry read the config was built from; it rides with the layer.
        """
        if not layer.results:
            return effective if isinstance(effective, dict) else dict(effective)
        return LayeredConfig(effective, layer, registry_generation)

    def snapshot(
        self, config: Mapping[str, Any], *, layer: Layer | None = None, derived: Mapping[str, Any] | None = None,
        registry_generation: Any = None, registry_problem: Mapping[str, Any] | None = None,
    ) -> RuntimeSnapshot:
        """The snapshot for ``config``. Pure of side effects: build it before publishing anything else.

        ``layer`` is what built ``config`` (``carried`` reads it from a loader's result); a config with none has no
        section and no problem. The snapshot holds the problems itself, the registry's as well.
        """
        layer = layer if layer is not None else carried(config)[0]
        problems: dict[str, Any] = {"config": dict(layer.problems)}
        if registry_problem:
            problems["registry"] = dict(registry_problem)
        return RuntimeSnapshot(
            generation=(registry_generation, layer.revisions, layer.rows_digest), config=config,
            sources=dict(layer.sources), problems=problems, derived=dict(derived or {}),
        )

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.holder.publish(snapshot)
        with self._lock:
            self.reload_failed = None
            self._last_commit = None

    # -- failure -------------------------------------------------------------------------------------------------------

    def note_commit(self, change_id: str | None, entries: Sequence[tuple[str, str]]) -> None:
        """Remember the last committed change, so a failed reload can say which change is not serving yet."""
        with self._lock:
            self._last_commit = (change_id, tuple(entries))

    def note_reload_failed(
        self, error_text: str, *, path: Path | None = None, change_id: str | None = None,
        entries: Sequence[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Record that a reload failed after a commit: the old snapshot serves, and the view says what differs.

        A caller that knows its own change passes ``change_id`` and ``entries``: a second writer's commit, or a
        publish that cleared the shared last-commit note, then cannot change which change the record names. Without
        them the record falls back to the last commit noted (the reload path, which has no change of its own).
        """
        if entries is None and change_id is None:
            change_id, entries = self._last_commit or (None, ())
        entries = entries or ()
        serving = self.holder.current()
        stored: dict[tuple[str, str], Mapping[str, Any]] = {}
        if entries and path is not None:
            try:
                from gateway import admin_store

                stored = {(row["section"], row["pointer"]): row for row in admin_store.read_config_state(path=path)["rows"]}
            except Exception:  # noqa: BLE001 - an unreadable store has no stored value to show
                stored = {}
        details = []
        for section, pointer in entries:
            now = layers.get_at(serving.config.get(section), pointer) if serving is not None and isinstance(
                serving.config.get(section), Mapping) else layers.ABSENT
            row = stored.get((section, pointer))
            details.append({
                "section": section, "pointer": pointer, "serving": _present(now),
                "stored": None if row is None else (
                    {"op": row["op"]} if row["op"] == layers.OP_DELETE else {"op": row["op"], "value": json.loads(row["value_json"])}
                ),
            })
        record = {"change_id": change_id, "error": error_text[:300], "entries": details}
        with self._lock:
            self.reload_failed = record
        return record

    def view(self, *, registry_problem: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """What the owner is told about the running config: its generation, its problems and a failed reload."""
        snapshot = self.holder.current()
        problems = dict(snapshot.problems) if snapshot is not None else {}
        if registry_problem:
            problems["registry"] = dict(registry_problem)
        return {
            "generation": list(snapshot.generation) if snapshot is not None else None,
            "problems": problems, "reload_failed": self.reload_failed,
        }


def runtime_for(home: ConfigHome) -> RuntimeSnapshot:
    """The snapshot a reader gets: the request's bound one, else the latest published.

    The holder is the only source. A module-level ``config`` name is assigned from a snapshot after it is published and
    is never consulted here, so a reload that is half done cannot hand a reader a view that was never published.
    """
    bound = bound_snapshot()
    if bound is not None:
        return bound
    latest = home.holder.current()
    if latest is None:
        raise RuntimeError("no config snapshot has been published")
    return latest
