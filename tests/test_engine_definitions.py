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


def test_the_seed_holds_the_two_definitions_with_any_openai_compatible_server_first():
    catalog = load_engine_catalog()
    assert catalog.ids() == ["openai-compatible", "own-model-list"]
    first = catalog.public()["definitions"][0]
    assert first["title"] == "Any OpenAI-compatible server"
    assert first["fields"][0]["key"] == "address" and first["fields"][1]["key"] == "api_key"
    # Shared advice and definition-specific overrides must all be actionable.
    advice_groups = [("shared", SEED["advice"])] + [
        (definition["definition"], definition["advice"])
        for definition in SEED["definitions"]
    ]
    for owner, rows in advice_groups:
        for row in rows:
            for part in ("what", "why", "fix"):
                text = row.get(part)
                assert isinstance(text, str) and text.strip(), (owner, row.get("code"), part)


def test_every_secret_field_is_write_only():
    for definition in SEED["definitions"]:
        for field in definition["fields"]:
            if field["type"] == "secret" or field.get("secret"):
                assert field.get("writeOnly") is True, (definition["definition"], field["key"])


def test_the_old_preset_list_is_gone():
    assert not hasattr(service_registry, "PROVIDER_PRESETS")
    assert not hasattr(service_registry, "provider_preset")


# Every field a definition may offer, and where the running gateway reads what it saves.
# No control may save a setting the gateway ignores (add-engine-spec.md section 8): a new
# field fails here until it is wired into the request path and listed with its proof.
FIELDS_THE_GATEWAY_READS = {
    "address": "service_base_url -> the router's api_base (test_engine_dispatch_wiring)",
    "path_prefix": "joined into base_path -> api_base (test_engine_dispatch_wiring)",
    "api_key": "provider secret store -> the router's api_key reference (test_engine_admin_routes)",
    "api_key_env": "the router's api_key reference (test_engine_admin_routes)",
    "headers": "router extra_headers and native passthrough (test_engine_dispatch_wiring, test_runtime_route_policy)",
    "reply_timeout_s": "router timeout (test_engine_dispatch_wiring)",
    "transforms": "per-backend request adjustments on the chat path",
    "lifecycle_mode": "the command lifecycle driver",
    "lifecycle_start": "the command lifecycle driver",
    "lifecycle_stop": "the command lifecycle driver",
    "lifecycle_ready_s": "driver_config.health_timeout_s",
    "models": "the router's model list",
    "context_window": "model listing (_annotate_model_context_windows)",
}


def test_every_field_is_one_the_running_gateway_reads():
    for definition in SEED["definitions"]:
        unread = [f["key"] for f in definition["fields"] if f["key"] not in FIELDS_THE_GATEWAY_READS]
        assert not unread, f"{definition['definition']} offers {unread}, which the gateway does not use"
        local_fields = {field["key"] for field in definition["fields"]}
        for field in definition["fields"]:
            if "show_if" in field:
                condition = field["show_if"]
                assert isinstance(condition, dict), (definition["definition"], field["key"])
                assert condition.get("field") in local_fields, (
                    definition["definition"], field["key"], condition.get("field")
                )


def test_only_work_the_gateway_routes_to_an_added_engine_is_offered():
    """Chat and embeddings go through the model router to the engine. Images, speech,
    transcription, reranking and live voice go to fixed built-in services, so ticking
    them would save nothing the gateway uses (add-engine-spec.md section 8, v2)."""
    assert set(SEED["capabilities"]) == {"chat", "embeddings"}
    for definition in SEED["definitions"]:
        assert set(definition["capabilities"]) <= {"chat", "embeddings"}, definition["definition"]
        assert definition["flow"] == "detect"
        # Probe implications must refer to catalog capabilities, not arbitrary names.
        known = set(SEED["capabilities"])
        for rule in SEED["model_rules"]:
            assert rule.get("capability") in known, (
                definition["definition"], "model_rule", rule.get("field"), rule.get("capability")
            )
        metadata = {row["id"]: row for row in SEED["metadata_rows"]}
        for probe_id in definition["probes"]["metadata"]:
            assert probe_id in metadata, (definition["definition"], probe_id)
            probe = metadata[probe_id]
            for mapping in ("paths", "values"):
                for source, capability in probe.get(mapping, {}).items():
                    assert capability in known, (
                        definition["definition"], probe_id, mapping, source, capability
                    )
        if definition["probes"].get("route_probes"):
            for capability in definition["capabilities"]:
                assert capability in known, (definition["definition"], "route_probe", capability)


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
