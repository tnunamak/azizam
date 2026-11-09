"""S6 deleted ``workflow_execution.compile_from_pin`` (docs/design/CODE_TO_DATA_S4_S8.md, I7). A deployment can hold an
owner override at that key from before the train (the live one does: ``/compile_from_pin/audio.generate`` is true).
The release must still boot on it, keep the rest of the section, and never read the retired key again. The boot names
the stale override as a rejected one, so the owner sees it; the deploy removes the live one (no migration step: Azizam
is unreleased)."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import proxy
from gateway import admin_store, config_runtime
from tests.config_homes_golden import raw_config, startup_part
from tests.registry_test_helpers import import_deployment_sections, seed_registry_from_backends

RETIRED = "/compile_from_pin/audio.generate"


def _store_retired_override(db) -> None:
    """The row as an earlier release wrote it, set directly: this release's writer would refuse the key."""
    conn = sqlite3.connect(db)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(config_overrides)")]
        row = {"section": "workflow_execution", "pointer": RETIRED, "op": "set", "value_json": "true",
               "base_json": None, "origin": "owner", "note": "", "change_id": "chg-before-s6",
               "updated_at": 1794000000, "updated_by": "owner-1"}
        row = {key: value for key, value in row.items() if key in columns}
        conn.execute(f"INSERT INTO config_overrides ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                     tuple(row.values()))
        conn.commit()
    finally:
        conn.close()


def test_a_stored_compile_from_pin_override_from_before_s6_does_not_stop_the_boot(tmp_path):
    db = tmp_path / "admin.db"
    raw = raw_config("live_like")
    seed_registry_from_backends(db, raw)
    import_deployment_sections(db, raw)
    _store_retired_override(db)
    with patch.object(proxy, "ADMIN_DB_PATH", db):
        effective = proxy._load_effective_runtime_config(startup_part(raw), boot=True)
    section = effective["workflow_execution"]
    assert "compile_from_pin" not in section
    assert section["max_wait_s"] == 60 and section["history_depth"] == 20
    layer, _generation = config_runtime.carried(effective)
    assert [(p["pointer"], p["code"]) for p in layer.problems["workflow_execution"]] == [(RETIRED, "override_rejected")]
