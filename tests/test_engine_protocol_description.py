"""Protocol descriptions (contract section 4): data files, a JSON Schema, a loud validator."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from gateway.engine_protocol import description
from gateway.engine_protocol.errors import DescriptionError

REGISTRY = Path(__file__).resolve().parent.parent / "registry" / "protocols"


def _minimal(**overrides):
    doc = {
        "protocol_version": 1,
        "id": "tiny",
        "executor": "http-v1",
        "operations": {
            "models": {
                "class": "inventory",
                "request": {"method": "GET", "path": "/models"},
                "response": {"framing": "json", "select": {"ids": "/data/*/id"}},
            }
        },
    }
    doc.update(overrides)
    return doc


def _problems(doc) -> str:
    with pytest.raises(DescriptionError) as raised:
        description.parse_description(doc, source="tiny.yml")
    return str(raised.value)


# --- shipped files -----------------------------------------------------------------------------


def test_every_shipped_description_loads_and_matches_its_file_name() -> None:
    loaded = description.load_registry(REGISTRY)
    assert "openai-compatible" in loaded
    for path in sorted(REGISTRY.glob("*.yml")):
        assert path.stem in loaded, path


def test_the_seed_openai_compatible_description_is_data_for_the_operations_azizam_calls() -> None:
    seed = description.load_registry(REGISTRY)["openai-compatible"]
    assert seed.executor == "http-v1"
    ops = seed.operations
    assert {"models", "chat", "embeddings", "speech", "transcribe"} <= set(ops)
    assert (ops["models"].request.method, ops["models"].request.path) == ("GET", "/v1/models")
    assert (ops["chat"].request.method, ops["chat"].request.path) == ("POST", "/v1/chat/completions")
    assert ops["chat"].response.stream is not None  # the same 200 is JSON or SSE, chosen by body.stream
    assert (ops["speech"].request.method, ops["speech"].request.path) == ("POST", "/v1/audio/speech")
    assert ops["speech"].response.framing == "binary"
    assert ops["transcribe"].request.body.encoding == "multipart"
    assert seed.auth["inference"].kind == "bearer"


def _keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            if key not in ("table", "headers", "select", "states", "value"):
                yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


def test_the_shipped_files_are_data_only_and_name_no_code() -> None:
    for path in sorted(REGISTRY.glob("*.yml")):
        keys = set(_keys(yaml.safe_load(path.read_text(encoding="utf-8"))))
        assert not keys & {"class_name", "script", "expression", "python", "template", "eval", "exec", "function", "lambda", "import"}, path.name


# --- the validator is loud ---------------------------------------------------------------------


def test_a_minimal_description_is_accepted() -> None:
    parsed = description.parse_description(_minimal(), source="tiny.yml")
    assert parsed.id == "tiny"
    assert parsed.operations["models"].request.path == "/models"


def test_unknown_keys_are_refused_with_their_place() -> None:
    doc = _minimal()
    doc["operations"]["models"]["request"]["retries"] = 3
    message = _problems(doc)
    assert "tiny.yml" in message
    assert "operations.models.request" in message
    assert "retries" in message


def test_every_problem_is_listed_not_only_the_first() -> None:
    doc = _minimal(executor="python")
    doc["operations"]["models"]["request"]["method"] = "FETCH"
    doc["operations"]["models"]["class"] = "weird"
    message = _problems(doc)
    assert message.count("\n") >= 3
    for needle in ("executor", "method", "class"):
        assert needle in message


@pytest.mark.parametrize("key", ["class_name", "script", "expression", "python", "template", "eval"])
def test_a_field_that_names_code_is_refused_by_name(key: str) -> None:
    doc = _minimal()
    doc["operations"]["models"][key] = "anything"
    message = _problems(doc)
    assert "names code" in message and key in message


@pytest.mark.parametrize(
    "path",
    ["models", "http://elsewhere.example/x", "//elsewhere.example/x", "/a/../b", "/a/./b", "/x?y=1", "/x#f", "/a b", "/a\\b", "/models\n", "/a\n/b"],
)
def test_a_request_path_is_a_relative_absolute_path_template(path: str) -> None:
    doc = _minimal()
    doc["operations"]["models"]["request"]["path"] = path
    assert "path" in _problems(doc)


def test_a_path_template_placeholder_is_a_plain_name() -> None:
    doc = _minimal()
    doc["operations"]["models"]["request"]["path"] = "/jobs/{id}"
    description.parse_description(doc, source="tiny.yml")
    doc["operations"]["models"]["request"]["path"] = "/jobs/{id.__class__}"
    assert "placeholder" in _problems(doc)
    doc["operations"]["models"]["request"]["path"] = "/jobs/{id\n}"
    assert "placeholder" in _problems(doc)


def test_a_header_name_in_a_description_has_no_trailing_line_break() -> None:
    doc = _minimal()
    doc["operations"]["models"]["request"]["headers"] = {"X-Test\n": "1"}
    assert "header" in _problems(doc)


@pytest.mark.parametrize("name", ["Authorization", "authorization", "X-Api-Key", "Cookie", "Proxy-Authorization", "x-auth-token"])
def test_credential_like_header_names_cannot_be_written_in_a_description(name: str) -> None:
    doc = _minimal()
    doc["operations"]["models"]["request"]["headers"] = {name: "secret"}
    message = _problems(doc)
    assert "credential" in message and name in message


def test_a_malformed_pointer_is_refused() -> None:
    for bad in ("data/id", "/a~2b", "", "/a//b/"):
        doc = _minimal()
        doc["operations"]["models"]["response"]["select"] = {"ids": bad}
        assert "pointer" in _problems(doc), bad


def test_duplicate_yaml_keys_are_refused_instead_of_silently_overriding() -> None:
    text = "protocol_version: 1\nid: tiny\nid: other\nexecutor: http-v1\noperations: {}\n"
    with pytest.raises(DescriptionError) as raised:
        description.parse_description_text(text, source="dup.yml")
    assert "duplicate key" in str(raised.value) and "id" in str(raised.value)


def test_yaml_that_does_not_parse_is_reported_with_its_line() -> None:
    with pytest.raises(DescriptionError) as raised:
        description.parse_description_text("a: [1, 2\n", source="bad.yml")
    assert "bad.yml" in str(raised.value) and "line" in str(raised.value)


def test_the_file_name_must_equal_the_id(tmp_path: Path) -> None:
    (tmp_path / "other.yml").write_text(yaml.safe_dump(_minimal()), encoding="utf-8")
    assert description.load_registry(tmp_path) == {}
    assert any(row["file"] == "other.yml" for row in description.registry_problems())


def test_a_broken_file_leaves_the_other_descriptions_available(tmp_path: Path) -> None:
    (tmp_path / "tiny.yml").write_text(yaml.safe_dump(_minimal()), encoding="utf-8")
    (tmp_path / "broken.yml").write_text("id: broken\n", encoding="utf-8")
    assert set(description.load_registry(tmp_path)) == {"tiny"}
    assert any(row["file"] == "broken.yml" for row in description.registry_problems())


# --- mapping rules and budgets, checked when the file loads --------------------------------------


def _with_body_map(rules):
    doc = _minimal()
    doc["operations"]["chat"] = {
        "class": "generate",
        "request": {"method": "POST", "path": "/chat", "body": {"encoding": "json", "map": rules}},
        "response": {"framing": "json"},
    }
    return doc


def test_all_rule_kinds_are_accepted() -> None:
    rules = [
        {"copy": {"from": "/body/model", "to": "/model"}},
        {"rename": {"from": "/model", "to": "/engine_model"}},
        {"set": {"to": "/stream", "value": False}},
        {"default": {"to": "/n", "value": 1}},
        {"drop": {"path": "/tmp"}},
        {"enum": {"from": "/body/voice", "to": "/speaker", "table": {"a": "b"}, "unmapped": "pass"}},
        {"clamp": {"path": "/temperature", "min": 0, "max": 1}},
        {"stringify": {"from": "/body/params", "to": "/action"}},
        {"when": {"if": {"path": "/body/seed", "present": True}, "then": [{"copy": {"from": "/body/seed", "to": "/seed"}}]}},
    ]
    description.parse_description(_with_body_map(rules), source="tiny.yml")


@pytest.mark.parametrize(
    "rule, needle",
    [
        ({"copy": {"from": "/body/a", "to": "/a"}, "set": {"to": "/b", "value": 1}}, "exactly one"),
        ({"loop": {"over": "/body/messages"}}, "unknown rule"),
        ({"copy": {"from": "/body/a"}}, "to"),
        ({"copy": {"from": "/elsewhere/a", "to": "/a"}}, "source"),
        ({"enum": {"from": "/body/a", "to": "/a", "table": {}, "unmapped": "pass"}}, "table"),
        ({"enum": {"from": "/body/a", "to": "/a", "table": {"x": "y"}, "unmapped": "guess"}}, "unmapped"),
        ({"clamp": {"path": "/t", "min": 2, "max": 1}}, "min"),
        ({"when": {"if": {"path": "/body/a"}, "then": []}}, "predicate"),
        ({"when": {"if": {"path": "/body/a", "present": True, "equals": 1}, "then": []}}, "predicate"),
        ({"set": {"to": "/a", "value": {"$ref": "#/x"}}}, None),  # constants are plain JSON; a $ref is only text
    ],
)
def test_bad_mapping_rules_are_refused_with_the_rule_place(rule, needle) -> None:
    if needle is None:
        description.parse_description(_with_body_map([rule]), source="tiny.yml")
        return
    message = _problems(_with_body_map([rule]))
    assert "operations.chat.request.body.map[0]" in message
    assert needle in message


def test_the_condition_nesting_budget_is_enforced_at_load() -> None:
    inner: list = [{"set": {"to": "/x", "value": 1}}]
    for _ in range(description.DEFAULT_BUDGETS.max_when_depth + 1):
        inner = [{"when": {"if": {"path": "/body/a", "present": True}, "then": inner}}]
    message = _problems(_with_body_map(inner))
    assert "nest" in message


def test_the_rule_count_budget_is_enforced_at_load() -> None:
    rules = [{"set": {"to": f"/k{i}", "value": i}} for i in range(description.DEFAULT_BUDGETS.max_rules + 1)]
    assert "rules" in _problems(_with_body_map(rules))


def test_an_oversized_constant_is_refused() -> None:
    big = "x" * (description.DEFAULT_BUDGETS.max_constant_bytes + 1)
    assert "constant" in _problems(_with_body_map([{"set": {"to": "/x", "value": big}}]))


def test_a_get_request_cannot_carry_a_body() -> None:
    doc = _minimal()
    doc["operations"]["models"]["request"]["body"] = {"encoding": "json"}
    assert "GET" in _problems(doc)


def test_multipart_parts_are_declared_by_kind() -> None:
    doc = _minimal()
    doc["operations"]["up"] = {
        "class": "generate",
        "request": {
            "method": "POST",
            "path": "/up",
            "body": {
                "encoding": "multipart",
                "parts": [
                    {"name": "file", "kind": "file", "from": "/files/audio"},
                    {"name": "model", "kind": "field", "from": "/body/model", "required": True},
                ],
            },
        },
        "response": {"framing": "json"},
    }
    description.parse_description(doc, source="tiny.yml")
    doc["operations"]["up"]["request"]["body"]["parts"][0]["from"] = "/body/audio"
    assert "files" in _problems(doc)


def test_a_job_operation_declares_typed_id_status_and_asset_selectors() -> None:
    doc = _minimal()
    doc["operations"]["render"] = {
        "class": "generate",
        "job": {
            "submit": {"request": {"method": "POST", "path": "/jobs", "body": {"encoding": "json"}}, "id": "/job/id"},
            "poll": {
                "request": {"method": "GET", "path": "/jobs/{id}"},
                "status": {"path": "/state", "states": {"queued": "pending", "busy": "running", "ok": "succeeded", "bad": "failed"}},
                "assets": "/files/*/name",
                "interval_s": 1,
                "max_wait_s": 60,
            },
            "fetch": {"request": {"method": "GET", "path": "/jobs/{id}/files/{asset}"}},
            "cancel": {"request": {"method": "POST", "path": "/jobs/{id}/cancel"}},
        },
    }
    parsed = description.parse_description(doc, source="tiny.yml")
    assert parsed.operations["render"].job.poll.status.states["busy"] == "running"
    doc["operations"]["render"]["job"]["poll"]["status"]["states"]["busy"] = "sleeping"
    assert "states" in _problems(doc)


def test_a_job_poll_path_must_use_the_job_id() -> None:
    doc = _minimal()
    doc["operations"]["render"] = {
        "class": "generate",
        "job": {
            "submit": {"request": {"method": "POST", "path": "/jobs"}, "id": "/job/id"},
            "poll": {
                "request": {"method": "GET", "path": "/jobs"},
                "status": {"path": "/state", "states": {"ok": "succeeded"}},
                "interval_s": 1,
                "max_wait_s": 60,
            },
        },
    }
    assert "{id}" in _problems(doc)


def test_a_request_and_a_job_cannot_both_be_declared() -> None:
    doc = _minimal()
    doc["operations"]["models"]["job"] = {
        "submit": {"request": {"method": "POST", "path": "/j"}, "id": "/id"},
        "poll": {"request": {"method": "GET", "path": "/j/{id}"}, "status": {"path": "/s", "states": {"ok": "succeeded"}},
                 "interval_s": 1, "max_wait_s": 5},
    }
    assert "request" in _problems(doc) and "job" in _problems(doc)


def test_an_adapter_executor_declares_names_only() -> None:
    doc = {
        "protocol_version": 1,
        "id": "reviewed",
        "executor": "adapter:reviewed-one",
        "operations": {"speech.synthesize": {"class": "generate"}},
    }
    parsed = description.parse_description(doc, source="reviewed.yml")
    assert parsed.adapter_id == "reviewed-one"
    doc["operations"]["speech.synthesize"]["request"] = {"method": "POST", "path": "/x"}
    assert "adapter" in _problems(doc)


def test_an_http_executor_operation_needs_a_request_or_job() -> None:
    doc = _minimal()
    del doc["operations"]["models"]["request"]
    assert "request" in _problems(doc)


def test_error_advice_rows_match_by_status() -> None:
    doc = _minimal(errors=[{"status": [401, 403], "code": "auth_rejected", "advice": "Check the key."}])
    parsed = description.parse_description(doc, source="tiny.yml")
    assert parsed.advice_for(401).code == "auth_rejected"
    assert parsed.advice_for(500) is None
    doc2 = copy.deepcopy(doc)
    doc2["errors"].append({"status": 401, "code": "again", "advice": "x"})
    assert "401" in _problems(doc2)
