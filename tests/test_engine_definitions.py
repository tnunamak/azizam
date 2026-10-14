"""Add engine: engine definitions are data, validated at load (spec section 1)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from gateway import engine_definitions, service_registry
from gateway.engine_definitions import (
    DEFINITIONS_PATH,
    SCHEMA_PATH,
    EngineDefinitionError,
    load_engine_catalog,
    parse_engine_catalog,
)

SEED = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _parse(data):
    return parse_engine_catalog(data, schema=SCHEMA, source="test.json")


def test_the_seed_holds_the_five_definitions_with_any_openai_compatible_server_first():
    catalog = load_engine_catalog()
    assert catalog.ids() == [
        "openai-compatible", "own-model-list", "node-graph",
        "speech-to-text-custom-path", "text-to-speech-custom-path",
    ]
    first = catalog.public()["definitions"][0]
    assert first["title"] == "Any OpenAI-compatible server"
    assert first["fields"][0]["key"] == "address" and first["fields"][1]["key"] == "api_key"


def test_every_secret_field_is_write_only():
    for definition in SEED["definitions"]:
        for field in definition["fields"]:
            if field["type"] == "secret" or field.get("secret"):
                assert field.get("writeOnly") is True, (definition["definition"], field["key"])


def test_the_old_preset_list_is_gone():
    assert not hasattr(service_registry, "PROVIDER_PRESETS")
    assert not hasattr(service_registry, "provider_preset")


def test_the_custom_path_kinds_ask_for_a_path_and_the_node_graph_kind_for_a_workflow_name():
    catalog = load_engine_catalog()
    for definition_id in ("speech-to-text-custom-path", "text-to-speech-custom-path"):
        keys = [f["key"] for f in catalog.definition(definition_id)["fields"]]
        assert "path" in keys and "public_name" in keys
    graph_keys = [f["key"] for f in catalog.definition("node-graph")["fields"]]
    # N1: the workflow gets the name apps call it by.
    assert "workflow_public_name" in graph_keys


def test_detection_rules_name_api_shapes_not_engines():
    """Titles and examples may name servers (they are words for people); rules may not."""
    names = re.compile(r"ollama|vllm|llama\.?cpp|comfy|whisper|kokoro|piper|lm.?studio|kobold|litellm", re.IGNORECASE)
    rules = {key: value for key, value in SEED.items() if key not in {"notes", "definitions", "advice"}}
    assert not names.search(json.dumps(rules)), "a detection rule names an engine"
    for definition in SEED["definitions"]:
        assert not names.search(json.dumps(definition["probes"])), definition["definition"]


@pytest.mark.parametrize(("break_it", "says"), [
    (lambda d: d["definitions"][0].pop("fields"), "'fields' is a required property"),
    (lambda d: d["definitions"][0]["fields"][0].update(type="password"), "'password' is not one of"),
    (lambda d: d["definitions"][0]["fields"][1].pop("writeOnly"), "writeOnly"),
    (lambda d: d["definitions"][1].update(definition=d["definitions"][0]["definition"]), "the id is used twice"),
    (lambda d: d["definitions"][0]["fields"].append({"key": "x", "label": "X", "type": "text", "show_if": {"field": "nope", "equals": 1}}), "show_if: no field named 'nope'"),
    (lambda d: d["definitions"][0]["capabilities"].append("teleport"), "no capability named 'teleport'"),
    (lambda d: d["model_rules"].append({"field": "task", "matches": "([", "capability": "chat"}), "is not a 'regex'"),
    (lambda d: d["definitions"][0]["probes"]["metadata"].append("no-such-row"), "no metadata row named 'no-such-row'"),
    (lambda d: d.update(surprise=True), "Additional properties are not allowed"),
])
def test_a_malformed_definition_fails_at_load_with_a_clear_message(break_it, says):
    data = copy.deepcopy(SEED)
    break_it(data)
    with pytest.raises(EngineDefinitionError) as caught:
        _parse(data)
    message = str(caught.value)
    assert message.startswith("test.json: "), message
    assert says in message, message


def test_a_definitions_file_that_is_not_json_names_the_line(tmp_path):
    broken = tmp_path / "defs.json"
    broken.write_text('{\n  "format_version": 1,\n  oops\n}', encoding="utf-8")
    with pytest.raises(EngineDefinitionError, match=r"defs\.json: not valid JSON at line 3"):
        load_engine_catalog(broken, SCHEMA_PATH)


def test_a_changed_file_is_read_again(tmp_path):
    path = tmp_path / "defs.json"
    data = copy.deepcopy(SEED)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_engine_catalog(path, SCHEMA_PATH).definition("openai-compatible")["title"] == "Any OpenAI-compatible server"
    data["definitions"][0]["title"] = "Renamed"
    path.write_text(json.dumps(data), encoding="utf-8")
    import os
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert load_engine_catalog(path, SCHEMA_PATH).definition("openai-compatible")["title"] == "Renamed"


def test_the_published_schema_is_a_valid_json_schema():
    import jsonschema
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SEED["$schema"] == "./engine_definitions.schema.json"
    assert (Path(engine_definitions.__file__).parent.parent / "registry" / "engine_definitions.schema.json").is_file()
