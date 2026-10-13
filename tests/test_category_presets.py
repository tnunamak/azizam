"""Bundled starter categories: valid data, the same rows as a custom category, offered by the catalog."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import category_presets
from gateway.admin_api import AdminContext
from gateway.category_api import create_category_router, snapshot_from_json, snapshot_to_json
from gateway.category_presets import PresetError, load_presets

GOOD = """
    schema_version: 1
    id: {id}
    label: {label}
    description: Meaning in one line.
    visibility: {visibility}
"""


def write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(textwrap.dedent(body), encoding="utf-8")


def test_every_bundled_preset_is_valid_and_ids_are_unique():
    presets = load_presets()
    assert presets, "no bundled presets"
    assert len({preset.id for preset in presets}) == len(presets)
    assert {"adult", "experimental"} <= {preset.id for preset in presets}
    assert all(preset.description.rstrip().endswith(".") for preset in presets)


def test_loader_reads_a_directory_in_id_order(tmp_path):
    write(tmp_path, "b-two.yml", GOOD.format(id="b-two", label="Two", visibility="everyone"))
    write(tmp_path, "a-one.yml", GOOD.format(id="a-one", label="One", visibility="owner"))
    assert [(p.id, p.label, p.visibility) for p in load_presets(tmp_path)] == [
        ("a-one", "One", "owner"), ("b-two", "Two", "everyone")]


@pytest.mark.parametrize("name,body,message", [
    ("x.yml", GOOD.format(id="y", label="Y", visibility="owner"), "match the file name"),
    ("Bad_Id.yml", GOOD.format(id="Bad_Id", label="Y", visibility="owner"), "lowercase words"),
    ("x.yml", GOOD.format(id="x", label="X", visibility="members"), "visibility must be one of"),
    ("x.yml", GOOD.format(id="x", label='" X"', visibility="owner"), "label must be non-empty"),
    ("x.yml", GOOD.format(id="x", label="X", visibility="owner") + "    colour: red\n", "only"),
    ("x.yml", GOOD.format(id="x", label="X", visibility="owner").replace("schema_version: 1", "schema_version: 2"), "schema_version 1"),
    ("x.yml", "- not\n- a mapping\n", "schema_version 1"),
    ("x.yml", "id: [unclosed", "cannot read"),
])
def test_loader_refuses_a_malformed_file_and_names_it(tmp_path, name, body, message):
    write(tmp_path, name, body)
    with pytest.raises(PresetError, match=message) as error:
        load_presets(tmp_path)
    assert name in str(error.value)


@pytest.mark.parametrize("visibility,visible_to", [("owner", []), ("everyone", None)])
def test_a_preset_row_is_the_row_a_custom_category_gets(tmp_path, visibility, visible_to):
    write(tmp_path, "kept.yml", GOOD.format(id="kept", label="Kept", visibility=visibility))
    (preset,) = load_presets(tmp_path)
    row = preset.category_row()
    assert row == {"id": "kept", "label": "Kept", "kind": "set", "values": [category_presets.MEMBER_VALUE],
                   "applies_to": ["release", "workflow"], "description": "Meaning in one line.",
                   "visible_to": visible_to}
    snapshot = {"id": "owner-rules", "categories": {"kept": row}, "people": {}, "apps": {},
                "services": {}, "completeness_gate": True, "retained": {}}
    assert snapshot_to_json(snapshot_from_json(snapshot))["categories"]["kept"]["visible_to"] == visible_to


def _client(tmp_path, presets):
    def auth(request):
        return AdminContext(subject="tester", roles=frozenset({"owner"}))
    app = FastAPI()
    app.include_router(create_category_router(auth=auth, store_path=tmp_path / "categories.db",
                                              service_catalog=lambda: [], presets=presets))
    return TestClient(app)


def test_the_catalog_offers_the_bundled_presets_to_the_owner(tmp_path):
    client = _client(tmp_path, lambda: [preset.as_json() for preset in load_presets()])
    body = client.get("/admin/api/categories/services").json()
    by_id = {row["id"]: row for row in body["presets"]}
    assert by_id["adult"] == {"id": "adult", "label": "Adult", "description": "Content for adults only.", "visibility": "owner"}
    assert set(by_id["experimental"]) == {"id", "label", "description", "visibility"}


def test_a_malformed_bundled_file_costs_the_picker_not_the_catalog(tmp_path):
    def broken():
        raise PresetError("x.yml: bad")
    response = _client(tmp_path, broken).get("/admin/api/categories/services")
    assert response.status_code == 200
    assert response.json() == {"services": [], "presets": []}
