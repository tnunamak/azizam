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


def test_definition_timeouts_are_optional_positive_bounded_numbers():
    for timeouts in ({}, {"probe_s": 0.25}, {"check_s": 600}, {"real_test_s": 240},
                     {"probe_s": 7, "check_s": 30, "real_test_s": 180}):
        data = copy.deepcopy(SEED)
        data["definitions"][0]["timeouts"] = timeouts
        assert _parse(data).definition("openai-compatible")["timeouts"] == timeouts
    for key in ("probe_s", "check_s", "real_test_s"):
        for bad in (0, -1, 601, True, "5", None, [], {}):
            data = copy.deepcopy(SEED)
            data["definitions"][0]["timeouts"] = {key: bad}
            with pytest.raises(EngineDefinitionError, match="timeouts"):
                _parse(data)
    for bad in (None, [], {"unexpected_s": 5}):
        data = copy.deepcopy(SEED)
        data["definitions"][0]["timeouts"] = bad
        with pytest.raises(EngineDefinitionError, match="timeouts"):
            _parse(data)
    data = copy.deepcopy(SEED)
    for definition in data["definitions"]:
        definition.pop("timeouts", None)
    assert _parse(data).ids() == ["openai-compatible", "own-model-list"]


def test_definition_timeouts_reach_every_probe_and_cap_the_real_test():
    import asyncio
    from gateway import engine_check

    async def exercise():
        for configured, probe_s, test_s in ((None, 5, 120), ({}, 5, 120),
                                           ({"probe_s": 2.5, "real_test_s": 180}, 2.5, 180)):
            data = copy.deepcopy(SEED)
            for row in data["definitions"]:
                row.pop("timeouts", None)
                if configured is not None:
                    row["timeouts"] = configured
            catalog = _parse(data)
            for ident in catalog.ids():
                definition = catalog.definition(ident)
                calls = []

                async def fetch(method, url, **kwargs):
                    calls.append((method, url, kwargs["timeout_s"]))
                    body = b'{"data": [{"id": "fixture"}], "models": [{"name": "fixture"}]}'
                    return engine_check.Answer(method, url, status=200, body=body)

                values = {"address": "http://engine.invalid/v1", "transforms": []}
                await engine_check.run_check(catalog, definition, values,
                                             key=engine_check.KeyInput(), fetch=fetch)
                assert len(calls) > 3
                assert all(call[2] == probe_s for call in calls)
                assert any(call[0] == "POST" and call[1].endswith("/embeddings") for call in calls)
                for reply_s, expected in ((None, test_s), (3, 3), (600, test_s), ("bad", test_s)):
                    calls.clear()
                    draft = dict(values)
                    if reply_s is not None:
                        draft["reply_timeout_s"] = reply_s
                    await engine_check.run_real_test(catalog, definition, draft, "chat",
                                                     key=engine_check.KeyInput(), fetch=fetch, model="fixture")
                    assert len(calls) == 1
                    assert calls[0][2] == expected

    asyncio.run(exercise())


def test_whole_check_timeout_uses_definition_and_cancels_pending_probes(monkeypatch):
    import asyncio
    from gateway import engine_check

    original_timeout = asyncio.timeout
    contexts = []
    configured = []

    def timeout(seconds):
        configured.append(seconds)
        context = original_timeout(seconds)
        contexts.append(context)
        return context

    monkeypatch.setattr(engine_check.asyncio, "timeout", timeout)

    async def exercise():
        data = copy.deepcopy(SEED)
        data["definitions"][0]["timeouts"] = {"check_s": 37}
        catalog = _parse(data)
        definition = catalog.definition("openai-compatible")
        started, cancelled = [], []

        async def fetch(method, url, **kwargs):
            started.append((method, url))
            # Expire the real deadline deterministically, without sleeping.
            contexts[0].reschedule(asyncio.get_running_loop().time())
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append((method, url))

        with pytest.raises(TimeoutError):
            await engine_check.run_check(catalog, definition, {"address": "http://engine.invalid"},
                                         key=engine_check.KeyInput(), fetch=fetch)
        assert configured == [37]
        assert len(started) == 3
        assert sorted(cancelled) == sorted(started)

        definition = dict(definition)
        definition.pop("timeouts")
        entered = asyncio.Event()

        async def pending(method, url, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(engine_check.run_check(
            catalog, definition, {"address": "http://engine.invalid"},
            key=engine_check.KeyInput(), fetch=pending))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert configured == [37, None]

    asyncio.run(exercise())
