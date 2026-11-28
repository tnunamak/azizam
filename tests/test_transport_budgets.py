"""The size and time budgets of the plan runners, plan translators and transport guards live in
registry/transport_budgets.yml (and the 3D sidecar's own limits file, the remote egress limit catalog).

Two kinds of proof: the shipped numbers equal the constants the code held before, and each reader follows a changed
file (a copy of the data with one number different), so a number cannot hide in code again.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

from gateway import subscription_kinds, transport_budgets
from gateway.protocols import claude_code_runner

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
ENGINES = ROOT / "engines"

SHIPPED = {
    "plan_runner": {"line_limit_bytes": 64 * 1024 * 1024, "kill_grace_seconds": 2.0, "exit_wait_seconds": 2.0,
                    "discovery_timeout_seconds": 30.0, "park_ttl_seconds": 1800, "session_limit": 4096},
    "plan_translator": {"stream_buffer_bytes": 4 * 1024 * 1024, "max_items": 1024, "least_effort": "minimal"},
    "plan_usage_bounds": {"max_percent": 1000.0, "max_window_minutes": 366 * 24 * 60, "max_reset_seconds": 366 * 24 * 3600},
    "mcp_park": {"max_body_bytes": 8 * 1024 * 1024},
    "turn_state": {"max_value_length": 4096},
    "browser_people": {"signed_in_max": 5, "remembered_max": 10},
}


def _changed_file(tmp_path: Path, section: str, key: str, value) -> Path:
    document = yaml.safe_load((REGISTRY / "transport_budgets.yml").read_text(encoding="utf-8"))
    document[section][key] = value
    path = tmp_path / "transport_budgets.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _read_from(monkeypatch, path: Path) -> None:
    original = transport_budgets.section
    monkeypatch.setattr(transport_budgets, "section", lambda name, path=path: original(name, path))


def _fresh_copy(module, monkeypatch, path: Path):
    """The module's source run again under a private name, reading the budgets file at *path*."""
    _read_from(monkeypatch, path)
    name = f"_fresh_{module.__name__.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(name, module.__file__)
    fresh = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, fresh)
    spec.loader.exec_module(fresh)
    return fresh


def test_the_shipped_budgets_are_the_numbers_the_code_held():
    for name, values in SHIPPED.items():
        assert dict(transport_budgets.section(name)) == values


def test_the_file_is_closed_and_a_missing_budget_is_an_error():
    schema = json.loads((REGISTRY / "transport_budgets.schema.json").read_text(encoding="utf-8"))
    document = yaml.safe_load((REGISTRY / "transport_budgets.yml").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(document)
    for mutate in (lambda d: d["mcp_park"].pop("max_body_bytes"), lambda d: d["turn_state"].update(extra=1),
                   lambda d: d["plan_runner"].update(kill_grace_seconds=0), lambda d: d.update(extra={})):
        broken = json.loads(json.dumps(document))
        mutate(broken)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(broken)


@pytest.mark.parametrize("module_name, attribute, section, key, value", [
    ("gateway.protocols.mcp_park", "MAX_BODY_BYTES", "mcp_park", "max_body_bytes", 12345),
    ("gateway.turn_state", "_MAX_VALUE_LENGTH", "turn_state", "max_value_length", 77),
    ("gateway.browser_people", "MAX_PEOPLE", "browser_people", "signed_in_max", 2),
    ("gateway.remembered_people", "MAX_PEOPLE", "browser_people", "remembered_max", 3),
    ("gateway.protocols.responses_plan", "_STREAM_BUFFER_LIMIT", "plan_translator", "stream_buffer_bytes", 999),
    ("gateway.protocols.responses_plan", "_MAX_ITEMS", "plan_translator", "max_items", 7),
    ("gateway.protocols.responses_plan", "_LEAST_EFFORT", "plan_translator", "least_effort", "none"),
    ("gateway.subscription_kinds", "_MAX_PERCENT", "plan_usage_bounds", "max_percent", 250.0),
    ("gateway.subscription_kinds", "_MAX_WINDOW_MINUTES", "plan_usage_bounds", "max_window_minutes", 60),
    ("gateway.subscription_kinds", "_MAX_RESET_SECONDS", "plan_usage_bounds", "max_reset_seconds", 3600),
])
def test_a_guard_follows_the_budgets_file(tmp_path, monkeypatch, module_name, attribute, section, key, value):
    module = importlib.import_module(module_name)
    assert getattr(module, attribute) == SHIPPED[section][key]
    fresh = _fresh_copy(module, monkeypatch, _changed_file(tmp_path, section, key, value))
    assert getattr(fresh, attribute) == value


def test_the_two_people_lists_stay_two_values():
    from gateway import browser_people, remembered_people
    assert (browser_people.MAX_PEOPLE, remembered_people.MAX_PEOPLE) == (5, 10)


def test_usage_readings_use_the_bounds_of_the_file(tmp_path, monkeypatch):
    fresh = _fresh_copy(subscription_kinds, monkeypatch, _changed_file(tmp_path, "plan_usage_bounds", "max_percent", 250.0))
    assert fresh.usage_percent(250) == 250.0 and fresh.usage_percent(251) is None
    assert subscription_kinds.usage_percent(1000) == 1000.0 and subscription_kinds.usage_percent(1001) is None


def _run_of(row: dict):
    run = object.__new__(claude_code_runner._Run)
    run._kind = SimpleNamespace(row={"runner": row})
    return run


@pytest.mark.parametrize("key", list(transport_budgets.RECORD_RUNNER_BUDGETS))
def test_a_runner_record_omitting_a_budget_gets_the_default_from_data(tmp_path, monkeypatch, key):
    assert _run_of({})._budget(key) == SHIPPED["plan_runner"][key]
    assert _run_of({key: 3})._budget(key) == 3  # the record wins
    _read_from(monkeypatch, _changed_file(tmp_path, "plan_runner", key, 41))
    assert _run_of({})._budget(key) == 41


def test_park_ttl_omitted_is_the_data_default(tmp_path, monkeypatch):
    assert _run_of({})._park_ttl_ms() == 1_800_000
    _read_from(monkeypatch, _changed_file(tmp_path, "plan_runner", "park_ttl_seconds", 60))
    assert _run_of({})._park_ttl_ms() == 60_000


def test_session_book_default_limit_is_the_data_default():
    assert claude_code_runner._SessionBook()._limit == SHIPPED["plan_runner"]["session_limit"]


@pytest.mark.parametrize("key, bad", [("line_limit_bytes", 1.5), ("kill_grace_seconds", 0), ("exit_wait_seconds", -1),
                                      ("discovery_timeout_seconds", "30"), ("park_ttl_seconds", True)])
def test_a_runner_record_with_a_bad_budget_is_refused(key, bad):
    row = {"runner": {"max_concurrent": 2, "timeout_seconds": 600, "default_system_prompt": "x", key: bad}}
    with pytest.raises(ValueError, match=f"runner.{key}"):
        claude_code_runner.RUNNER.validate(row, source="test")


def test_the_shipped_claude_kind_still_validates_and_keeps_its_park_ttl():
    kind = subscription_kinds.load_kinds()["claude"]
    claude_code_runner.RUNNER.validate(kind.row, source="claude.yml")
    assert kind.row["runner"]["park_ttl_seconds"] == SHIPPED["plan_runner"]["park_ttl_seconds"]


# -- the 3D sidecar's own limits file

def test_local3d_shipped_limits_are_the_numbers_the_code_held():
    from engines import local3d_stages_service as svc
    assert (svc.DEFAULT_MAX_MESH_BYTES, svc.DEFAULT_MAX_REFERENCE_BYTES, svc.FORM_SLACK_BYTES) == (
        256 * 1024 * 1024, 32 * 1024 * 1024, 1024 * 1024)
    assert (svc.KILL_GRACE_SECONDS, svc.COMFY_HTTP_TIMEOUT_SECONDS) == (5.0, 60.0)


@pytest.mark.parametrize("key, attribute, value", [
    ("max_mesh_bytes", "DEFAULT_MAX_MESH_BYTES", 1000), ("max_reference_bytes", "DEFAULT_MAX_REFERENCE_BYTES", 2000),
    ("form_slack_bytes", "FORM_SLACK_BYTES", 3000), ("kill_grace_seconds", "KILL_GRACE_SECONDS", 7.0),
    ("comfy_http_timeout_seconds", "COMFY_HTTP_TIMEOUT_SECONDS", 9.0)])
def test_local3d_limits_follow_the_limits_file(tmp_path, monkeypatch, key, attribute, value):
    # The service reads its limits file beside its own source: run a copy of the service beside a changed file.
    for name in ("local3d_stages_service.py", "ultratex_prompt.json"):
        shutil.copy(ENGINES / name, tmp_path / name)
    limits = json.loads((ENGINES / "local3d_stages_limits.json").read_text(encoding="utf-8"))
    limits[key] = value
    (tmp_path / "local3d_stages_limits.json").write_text(json.dumps(limits), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_fresh_local3d", tmp_path / "local3d_stages_service.py")
    fresh = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_fresh_local3d", fresh)
    spec.loader.exec_module(fresh)
    assert getattr(fresh, attribute) == value


def test_local3d_environment_override_still_beats_the_file():
    from engines import local3d_stages_service as svc
    with pytest.raises(ValueError, match="LOCAL3D_MAX_MESH_BYTES must be positive"):
        svc.create_app(env={"LOCAL3D_MAX_MESH_BYTES": "0"}, prompt={})
    assert svc.create_app(env={"LOCAL3D_MAX_MESH_BYTES": "4096", "LOCAL3D_MAX_REFERENCE_BYTES": "2048"}, prompt={}) is not None


@pytest.mark.parametrize("bad", [0, -1, "5", None, True])
def test_local3d_a_bad_limit_stops_the_service_at_start(tmp_path, bad):
    from engines import local3d_stages_service as svc
    limits = json.loads((ENGINES / "local3d_stages_limits.json").read_text(encoding="utf-8"))
    limits["kill_grace_seconds"] = bad
    path = tmp_path / "limits.json"
    path.write_text(json.dumps(limits), encoding="utf-8")
    with pytest.raises(ValueError, match="kill_grace_seconds"):
        svc._read_limits(path)
