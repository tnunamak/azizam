"""The one-time import of a startup-file block into the override store (docs/design/CONFIG_HOMES.md, 8.1).

Generic over sections: nothing here knows what a section holds. For a section the release ships and the install has not
imported yet, the gateway takes the startup file's block, computes the fewest overrides that make the effective section
equal that block (``config_layers.overrides_for_target``) and writes them as one change of kind ``legacy-import``,
recording the step ``config-homes-import:<section>`` in the same transaction. From then on the file's block is ignored,
and each boot says once how many entries of it differ from what runs.

Three callers use it:

- the boot (``ConfigHome.layer(..., legacy_import=True)``) through ``import_at_boot``;
- the owner route ``POST /admin/api/config/sections/{id}/legacy-import`` through ``plan_import``;
- the dry-run command (``python -m gateway.config_legacy_import``), which reads a copy of a config file against a scratch
  database, so that the live move can read the whole row list before it touches anything.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gateway import admin_store, config_runtime
from gateway import config_layers as layers

STEP_PREFIX = "config-homes-import:"
CODE_BLOCKED = "legacy_import_blocked"
CODE_FAILED = "legacy_import_failed"

STATUS_IMPORTED = "imported"
STATUS_NOTHING = "nothing_to_import"  # the file has no block, or it equals the release defaults: the step is recorded
STATUS_DONE = "already_imported"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_DRY_RUN = "dry_run"


def step_name(section_id: str) -> str:
    return STEP_PREFIX + section_id


def stored_json(value: Any) -> str:
    """JSON of an owner's value as given: object keys keep their order, so the first write serves what was sent."""
    return json.dumps(value, ensure_ascii=False)


def base_json(value: Any) -> str:
    """Key-sorted JSON: for a stored release base, which is only ever compared, never served."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def rows_by_section(home: Any, state: Mapping[str, Any]) -> dict[str, list[layers.Override]]:
    rows: dict[str, list[layers.Override]] = {name: [] for name in home.sections}
    for row in state["rows"]:
        if row["section"] in rows:
            rows[row["section"]].append(config_runtime.override_from_row(row))
    return rows


@dataclass(frozen=True)
class ImportPlan:
    """What importing one block would write: the overrides, and the faults that stop it."""

    section: str
    overrides: tuple[layers.Override, ...]
    blocked: tuple[layers.Problem, ...]

    def items(self, existing: Sequence[layers.Override] = ()) -> list[admin_store.ConfigItem]:
        """The store items for the overrides that are not already stored as they are."""
        have = {override.pointer: override for override in existing}
        items: list[admin_store.ConfigItem] = []
        for override in self.overrides:
            stored = have.get(override.pointer)
            if stored is not None and stored.op == override.op and layers.same_value(stored.value, override.value):
                continue
            items.append(admin_store.ConfigItem(
                self.section, override.pointer, override.op,
                stored_json(override.value) if override.op == layers.OP_SET else None,
                None if override.base is layers.ABSENT else base_json(override.base),
            ))
        return items

    def report(self, resets: Sequence[str] = ()) -> dict[str, Any]:
        """One line per row with the file value and the release value; the tombstones are a separate list."""
        def release(override: layers.Override) -> dict[str, Any]:
            return {} if override.base is layers.ABSENT else {"release_value": override.base}

        return {
            "section": self.section,
            "rows": [{"pointer": o.pointer, "op": o.op, "file_value": o.value, **release(o)}
                     for o in self.overrides if o.op == layers.OP_SET],
            "removes": [{"pointer": o.pointer, **release(o)} for o in self.overrides if o.op == layers.OP_DELETE],
            "resets": list(resets),
        }

    def blocked_view(self) -> list[dict[str, Any]]:
        return [{"section": self.section, **{**p.as_dict(), "code": CODE_BLOCKED, "reason": p.code}} for p in self.blocked]


def plan_import(
    home: Any, section_id: str, block: Any, rows: Mapping[str, Sequence[layers.Override]],
    registry: Mapping[str, Any] | None,
) -> ImportPlan:
    """The overrides that make the effective section equal ``block``, judged against every section as stored."""
    context = {"registry": registry, "sections": {
        name: result.effective for name, result in home.build_candidate(rows, registry).items()}}
    section = home.sections[section_id]
    overrides, blocked = layers.overrides_for_target(section, layers.owned_part(section, block), context)
    return ImportPlan(section_id, tuple(overrides), tuple(blocked))


@dataclass(frozen=True)
class ImportOutcome:
    section: str
    status: str
    plan: ImportPlan | None = None
    change_id: str | None = None
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"section": self.section, "status": self.status}
        if self.plan is not None:
            report = self.plan.report()
            out.update(row_count=len(report["rows"]), remove_count=len(report["removes"]), **report)
            if self.plan.blocked:
                out["blocked"] = self.plan.blocked_view()
        if self.change_id:
            out["change_id"] = self.change_id
        if self.error:
            out["error"] = self.error
        return out


def import_section(
    home: Any, section_id: str, block: Any, *, path: Path, registry: Mapping[str, Any] | None, apply: bool = True,
) -> ImportOutcome:
    """Import one section's block once. Never raises for a bad block or a store fault: the outcome says what happened.

    ``apply=False`` is the dry run: nothing is written and the step is not recorded.
    """
    step = step_name(section_id)
    try:
        if admin_store.migration_step_recorded(step, path=path):
            return ImportOutcome(section_id, STATUS_DONE)
        state = admin_store.read_config_state(path=path)
        rows = rows_by_section(home, state)
        if not isinstance(block, Mapping) or not block:
            if apply:
                admin_store.record_migration_step(step, path=path)
            return ImportOutcome(section_id, STATUS_NOTHING)
        plan = plan_import(home, section_id, block, rows, registry)
        if plan.blocked:
            return ImportOutcome(section_id, STATUS_BLOCKED, plan)
        items = plan.items(rows[section_id])
        if not apply:
            return ImportOutcome(section_id, STATUS_DRY_RUN, plan)
        if not items:
            admin_store.record_migration_step(step, path=path)
            return ImportOutcome(section_id, STATUS_NOTHING, plan)
        result = admin_store.apply_config_change(
            items, if_match={section_id: state["revisions"].get(section_id, "0")}, kind="legacy-import",
            origin="legacy-import", note=f"first-boot import of the startup file's {section_id} block",
            validate=None, migration_step=step, path=path,
        )
    except Exception as error:  # noqa: BLE001 - a store fault must not stop a boot; the step stays open for the next one
        return ImportOutcome(section_id, STATUS_FAILED, error=f"{type(error).__name__}: {str(error)[:200]}")
    if result.get("skipped"):
        return ImportOutcome(section_id, STATUS_DONE, plan)
    return ImportOutcome(section_id, STATUS_IMPORTED, plan, change_id=result.get("change_id"))


def ignored_entries(section: layers.Section, block: Any, effective: Any) -> int:
    """How many entries of the startup block differ from the effective section (an entry on one side only counts)."""
    if not isinstance(block, Mapping):
        return 0
    block = layers.owned_part(section, block)
    pointers = set(section.index.entries_in(block)) | set(section.index.entries_in(effective))
    return sum(1 for pointer in pointers
               if not layers.same_value(layers.get_at(block, pointer), layers.get_at(effective, pointer)))


@dataclass
class BootImport:
    """What a boot's import pass found. ``blocked`` sections keep serving the file's block (design 8.1 step 1)."""

    outcomes: dict[str, ImportOutcome] = field(default_factory=dict)
    blocked: dict[str, tuple[Any, tuple[layers.Problem, ...]]] = field(default_factory=dict)
    done: tuple[str, ...] = ()  # sections whose step was recorded before this pass: their file block is ignored


def import_at_boot(home: Any, raw_config: Mapping[str, Any], *, path: Path, registry: Mapping[str, Any] | None,
                   log: Callable[[str], None]) -> BootImport:
    """Run the import for every section whose step is not recorded. Blocked or failed ones are logged and kept open."""
    found = BootImport()
    done: list[str] = []
    for section_id in sorted(home.sections):
        block = raw_config.get(section_id)
        outcome = import_section(home, section_id, block, path=path, registry=registry)
        found.outcomes[section_id] = outcome
        if outcome.status == STATUS_DONE:
            done.append(section_id)
        elif outcome.status == STATUS_BLOCKED and outcome.plan is not None:
            found.blocked[section_id] = (copy.deepcopy(dict(block)), outcome.plan.blocked)
            where = ", ".join(sorted({p.pointer or "/" for p in outcome.plan.blocked})[:8])
            log(f"[proxy] WARNING: the startup file's {section_id} block was not imported ({len(outcome.plan.blocked)} "
                f"fault(s) at {where}); it keeps serving from the file. Fix the block and restart, or set the values "
                "through the config API.")
        elif outcome.status == STATUS_FAILED:
            if isinstance(block, Mapping) and block:  # no block means nothing to keep serving: the release defaults apply
                found.blocked[section_id] = (copy.deepcopy(dict(block)), ())
            log(f"[proxy] WARNING: the import of the startup file's {section_id} block failed ({outcome.error}); "
                "it keeps serving from the file and retries at the next start.")
        elif outcome.status == STATUS_IMPORTED and outcome.plan is not None:
            report = outcome.plan.report()
            log(f"[proxy] imported the startup file's {section_id} block into the override store: "
                f"{len(report['rows'])} row(s), {len(report['removes'])} release entr(ies) removed "
                f"(change {outcome.change_id}). The file's block is ignored from now on.")
    found.done = tuple(done)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Read what the import would write for a config file, against a scratch database. Never touches a live one."""
    import yaml

    from gateway import config_rules

    parser = argparse.ArgumentParser(
        prog="python -m gateway.config_legacy_import",
        description="Show the rows the one-time import of a startup file's blocks would write. Dry run by default.")
    parser.add_argument("--config", required=True, type=Path, help="a copy of the startup file (read only)")
    parser.add_argument("--db", type=Path, help="a scratch admin.db; with --apply the import is written here")
    parser.add_argument("--section", action="append", help="a section id; default: every section the release ships")
    parser.add_argument("--apply", action="store_true", help="write to --db (a scratch copy) instead of a dry run")
    args = parser.parse_args(argv)
    if args.apply and args.db is None:
        parser.error("--apply needs a scratch --db")
    raw = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    home = config_rules.config_home()
    with tempfile.TemporaryDirectory(prefix="azizam-import-dry-run-") as scratch:
        path: Path = args.db if args.db is not None else Path(scratch) / "admin.db"
        admin_store.init_db(path)
        ids = args.section or sorted(home.sections)
        unknown = [name for name in ids if name not in home.sections]
        if unknown:
            parser.error(f"this release ships no section {', '.join(unknown)}")
        report = [import_section(home, name, raw.get(name), path=path, registry=None, apply=args.apply).summary()
                  for name in ids]
    json.dump({"sections": report}, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
