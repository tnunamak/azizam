"""Contract files refuse ambiguous or undeclared policy."""
import copy
from pathlib import Path

import pytest
import yaml

from gateway.compatibility import REGISTRY, load_compatibility
from gateway.storage_kinds import load_storage_kinds


@pytest.mark.parametrize("name,loader", [("compatibility", load_compatibility), ("storage_kinds", load_storage_kinds)])
def test_contract_refuses_unknown_duplicate_and_invalid_fields(tmp_path, name, loader):
    original = (REGISTRY / f"{name}.yml").read_text()
    path = tmp_path / "contract.yml"
    for document in (original + "\nunknown: true\n", original + "\nschema_version: 1\n",
                     original.replace("schema_version: 1", "schema_version: 2")):
        path.write_text(document)
        with pytest.raises((ValueError, yaml.YAMLError)):
            loader(path)
    value = yaml.safe_load(original)
    if name == "storage_kinds":
        value["kinds"].append(value["kinds"][0])
    else:
        value["migration"]["unknown"] = "refused"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):
        loader(path)
