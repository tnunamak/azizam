"""The inventory of every setting Azizam reads from a file, and where it lives (docs/design/CONFIG_HOMES.md, C10).

Nothing here is written by hand. Each row comes from one of two schemas:

- a config section's schema (``registry/defaults/<section>.schema.json``): the setting lives in admin.db as an owner
  override of the release value, and the owner edits it through the config API;
- the startup schema (``registry/startup.schema.json``): the setting lives in the startup file, with an environment
  variable for some keys and for every secret.

A new key in either schema is a new row, so the inventory cannot fall behind the code. The module reads schema files
only. It imports no proxy code and reads no environment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from gateway import config_layers as layers, config_runtime, startup_config

SECTION_HOME = "admin.db"
STARTUP_HOME = "startup file"


@dataclass(frozen=True)
class SourceRow:
    """One setting. ``path`` is a JSON Pointer template for a section (``/chat/stable/*`` is one member of a collection)
    and a dotted path for the startup file (``proxy.port``)."""

    id: str
    home: str
    section: str  # the config section, or "startup"
    path: str
    kind: str  # scalar | list | atomic | member
    apply: str  # live | restart
    secret: bool
    env: str | None = None
    description: str = ""

    @property
    def change_path(self) -> str:
        """How an operator changes the setting."""
        if self.home == SECTION_HOME:
            return f"{startup_config.CONFIG_API} (the {self.section} section, entry {self.path})"
        if self.env:
            return f"Edit the startup file or set {self.env}; restart."
        return "Edit the startup file; restart."


def section_rows(sections: Mapping[str, layers.Section]) -> list[SourceRow]:
    rows = []
    for section_id in sorted(sections):
        for pointer, rule in sections[section_id].index.declared():
            rows.append(SourceRow(
                id=f"section:{section_id}:{pointer}", home=SECTION_HOME, section=section_id, path=pointer,
                kind=rule.kind, apply=rule.apply, secret=rule.secret))
    return rows


def startup_rows() -> list[SourceRow]:
    return [
        SourceRow(id=f"startup:{key.path}", home=STARTUP_HOME, section="startup", path=key.path,
                  kind="list" if "array" in key.types else "scalar", apply="restart",
                  secret=key.secret, env=key.env, description=key.description)
        for key in startup_config.iter_keys()
    ]


def inventory(sections: Mapping[str, layers.Section] | None = None) -> list[SourceRow]:
    """Every setting of the release's section schemas and of the startup schema, sorted by id."""
    loaded = config_runtime.load_sections() if sections is None else sections
    return sorted([*section_rows(loaded), *startup_rows()], key=lambda row: row.id)


def as_json(rows: list[SourceRow] | None = None) -> list[dict[str, Any]]:
    """The rows as plain JSON-able data, with the way to change each."""
    return [{**row.__dict__, "change_path": row.change_path} for row in (inventory() if rows is None else rows)]
