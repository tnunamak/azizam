"""The config inventory is generated from the section schemas and the startup schema (CONFIG_HOMES.md, C10).

No test pins a hand-kept table. These tests hold the one property that matters: every key either schema declares has a row,
and no row exists for a key that no schema declares.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from gateway import config_layers as layers, config_runtime, config_sources, startup_config

SECTIONS = config_runtime.load_sections()


def _template_matches(template: str, pointer: str) -> bool:
    want, got = layers.split_pointer(template), layers.split_pointer(pointer)
    return len(want) == len(got) and all(w in ("*", g) for w, g in zip(want, got))


def _startup_leaves(node, prefix=""):
    for name, child in (node.get("properties") or {}).items():
        if child.get("type") == "object" and "properties" in child:
            yield from _startup_leaves(child, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}"


def test_every_startup_key_has_a_row_and_every_startup_row_is_a_key():
    schema = json.loads(startup_config.SCHEMA_PATH.read_text(encoding="utf-8"))
    rows = {row.path for row in config_sources.startup_rows()}
    assert rows == set(_startup_leaves(schema))
    assert all(row.home == config_sources.STARTUP_HOME and row.apply == "restart" for row in config_sources.startup_rows())


def test_every_entry_of_every_section_has_a_row_and_every_section_row_is_declared():
    rows = config_sources.section_rows(SECTIONS)
    by_section = {name: [row for row in rows if row.section == name] for name in SECTIONS}
    assert all(by_section[name] for name in SECTIONS), "every section has at least one row"
    for name, section in SECTIONS.items():
        templates = [row.path for row in by_section[name]]
        for pointer in section.index.entries_in(section.defaults):
            assert any(_template_matches(template, pointer) for template in templates), (name, pointer)
        assert templates == [pointer for pointer, _ in section.index.declared()]
    assert {row.section for row in rows} == set(SECTIONS), "a row names a section the release ships"


def test_the_inventory_holds_exactly_the_rows_of_both_schemas_with_unique_ids():
    rows = config_sources.inventory()
    ids = [row.id for row in rows]
    assert len(ids) == len(set(ids)) and ids == sorted(ids)
    assert len(rows) == len(config_sources.section_rows(SECTIONS)) + len(config_sources.startup_rows())


def test_a_section_the_release_adds_gets_rows_without_any_edit_here(tmp_path):
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "limit": {"type": "integer", "x-azizam-apply": "restart"},
        "pass": {"type": "string", "x-azizam-secret": True},
        "named": {"type": "object", "x-azizam-entry": "collection", "additionalProperties": {"type": "string"}}}}
    section = layers.Section.create("probe", {"limit": 1, "named": {}}, schema)
    rows = {row.path: row for row in config_sources.section_rows({"probe": section})}
    assert set(rows) == {"/limit", "/pass", "/named/*"}
    assert rows["/limit"].apply == "restart" and rows["/pass"].secret and rows["/named/*"].kind == "member"


def test_startup_environment_names_and_secrets_come_from_the_startup_schema():
    rows = config_sources.startup_rows()
    assert {row.env for row in rows if row.env} == startup_config.startup_env_names()
    assert {row.path for row in rows if row.secret} == {key.path for key in startup_config.iter_keys() if key.secret}
    assert {row.path for row in rows if row.secret} >= {"admin.session_signing_key", "admin.oidc.client_secret", "images.signed_url_secret"}


def test_the_inventory_carries_no_value_and_serialises():
    for row in config_sources.as_json():
        assert set(row) == {"id", "home", "section", "path", "kind", "apply", "secret", "env", "description", "change_path"}
    text = json.dumps(config_sources.as_json())
    assert not re.search(r"/home/\w", text)


def test_the_module_reads_schemas_only():
    source = Path(config_sources.__file__).read_text(encoding="utf-8")
    assert "import proxy" not in source and "os.environ" not in source and "get_env" not in source
