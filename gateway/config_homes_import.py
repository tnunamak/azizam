"""The one-time legacy import: a startup-file block becomes the owner overrides of its section (docs/design/CONFIG_HOMES.md, 8.1).

``run_legacy_imports`` runs at boot, before the layer is built. For each registered section whose step is not recorded
yet, it computes the fewest overrides that make the section equal the startup block (``layers.overrides_for_target``),
then writes them together with the step, in one transaction (``admin_store.apply_config_change``). Afterwards the file
block is ignored for that section, and the import never runs again for it.

A block that does not fit its section (a key the section does not take, a value the schema refuses) is not imported.
The section is reported as ``blocked`` and the caller withdraws it from the layer, so the startup file keeps serving it
for this boot, the problem is shown, and the next boot retries. A store that cannot be read at all raises: the caller
withdraws every section, for the same reason.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store, config_layers as layers, config_runtime
from gateway.config_api import _canonical, _store

STEP_PREFIX = "config-homes-import:"

# Keys of a section's startup block that the section does not own. They stay in the startup file until the slice that
# moves them (C10): reading them from the section would drop them from the effective view.
STARTUP_KEYS: Mapping[str, tuple[str, ...]] = {"model_routing": ("gguf_dir",)}


def block_for(section_id: str, startup: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The startup file's block for ``section_id`` without the keys the section does not own; None when there is no block."""
    block = startup.get(section_id)
    if not isinstance(block, Mapping):
        return None
    dropped = STARTUP_KEYS.get(section_id, ())
    return {key: value for key, value in block.items() if key not in dropped}


def run_legacy_imports(
    home: config_runtime.ConfigHome, *, path: Path, startup: Mapping[str, Any], registry: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Import every registered section once. Returns {section: {"status": ..., ...}}; a ``blocked`` section is not imported.

    Raises when the store cannot be read or written. The caller decides what serves then.
    """
    report: dict[str, dict[str, Any]] = {}
    admin_store.init_db(path)
    for name in sorted(home.sections):
        step = STEP_PREFIX + name
        if admin_store.migration_step_recorded(step, path=path):
            report[name] = {"status": "already_imported"}
            continue
        block = block_for(name, startup)
        if block is None:
            report[name] = {"status": "no_startup_block"}
            continue
        state = admin_store.read_config_state(path=path)
        rows: dict[str, list[layers.Override]] = {}
        for row in state["rows"]:
            rows.setdefault(row["section"], []).append(config_runtime.override_from_row(row))
        results = home.build_candidate(rows, registry)
        context = {"registry": registry, "sections": {n: r.effective for n, r in results.items()}}
        overrides, blocked = layers.overrides_for_target(home.sections[name], block, context)
        if blocked:
            report[name] = {"status": "blocked", "problems": [problem.as_dict() for problem in blocked]}
            continue
        existing ={row["pointer"]: config_runtime.override_from_row(row) for row in state["rows"] if row["section"] == name}
        items: list[admin_store.ConfigItem] = []
        for override in overrides:
            have = existing.get(override.pointer)
            if have is not None and have.op == override.op and layers.same_value(have.value, override.value):
                continue
            items.append(admin_store.ConfigItem(
                name, override.pointer, override.op,
                _store(override.value) if override.op == layers.OP_SET else None,
                None if override.base is layers.ABSENT else _canonical(override.base),
            ))
        kept = {override.pointer for override in overrides}
        items += [admin_store.ConfigItem(name, pointer, "reset") for pointer in sorted(existing.keys() - kept)]
        if not items:
            admin_store.record_migration_step(step, path=path)
            report[name] = {"status": "imported", "changes": 0}
            continue
        admin_store.apply_config_change(
            items, if_match={name: str(state["revisions"].get(name, "0"))}, kind="legacy-import", origin="legacy-import",
            validate=home.write_validator(registry), migration_step=step, path=path,
        )
        report[name] = {"status": "imported", "changes": len(items)}
    return report
