"""Closed frontend mappings and neutral manifest roles."""
import copy
from pathlib import Path

import pytest
import yaml

from gateway.extension_catalog import load_input_contract
from gateway.workflow_contract import check_contract
from tests.test_frontend_shapes import contract


@pytest.mark.parametrize("change", [
    {"module": "package.handler"}, {"extension_hints": []},
    {"extension_hints": {"picture": "package.handler"}},
    {"source_role_paths": []}, {"source_role_paths": [["package.handler"]]},
    {"source_role_paths": [["roles", "source"], ["roles", "source"]]},
])
def test_input_contract_refuses_invalid_fields(tmp_path, change):
    doc = {"extension_hints": {"picture": "input_reference"}, "source_role_paths": [["roles", "source"]]}
    doc.update(change)
    path = tmp_path / "frontends.yml"
    path.write_text(yaml.safe_dump({"input_contract": doc}))
    with pytest.raises(ValueError, match="input_contract"):
        load_input_contract(path)


@pytest.mark.parametrize("change", [
    {"roles": {"unknown": "source_image"}}, {"roles": {"source": "missing"}},
    {"roles": {"source": "prompt"}}, {"input_fields": {"missing": "input_reference"}},
    {"input_fields": {"source_image": "package.handler"}},
])
def test_neutral_manifest_mappings_refuse_invalid_fields(change):
    doc = contract("qwen-image-2.1-edit")
    doc.update(change)
    assert not check_contract(doc).ok


def test_neutral_roles_accept_a_declared_media_input():
    doc = contract("qwen-image-2.1-edit")
    doc.update(roles={"source": "source_image"}, input_fields={"source_image": "input_reference"})
    assert check_contract(doc).ok
