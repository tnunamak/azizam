"""Tests for ``scripts/check_workflow_drift.py`` name-based comparison.

Fixtures are copies of repo workflows (``tests/fixtures/workflow_drift``):
  * krea2-turbo: legacy UI layout (no ``control_after_generate`` slot). The
    ComfyUI 0.38 / frontend 1.53.6 positional round-trip reported
    ``cfg: 1.0 -> 'euler'`` for it.
  * qwen-image-2.1-edit: legacy layout with a linked widget and an autogrow
    dict value (``images.image_1``).
  * video-wan-2.2-ti2v-5b-q8: current layout (linked widgets keep a slot).
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_workflow_drift as drift  # noqa: E402
from scripts.check_workflow_drift import name_diff, ui_inputs_by_name  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "workflow_drift"
NAMES = ["krea2-turbo", "qwen-image-2.1-edit", "video-wan-2.2-ti2v-5b-q8"]
API_NODE_TYPE = "class_type"


def load(name: str) -> tuple[dict, dict]:
    ui = json.loads((FIXTURES / f"{name}.ui.json").read_text())
    api = json.loads((FIXTURES / f"{name}.json").read_text())
    return ui, api


@pytest.mark.parametrize("name", NAMES)
def test_fixture_twins_match_by_name(name):
    ui, api = load(name)
    assert name_diff(ui, api) == []


def test_legacy_ksampler_widgets_pair_by_name_not_position():
    ui, _ = load("krea2-turbo")
    sampler = ui_inputs_by_name(ui)["7"]
    assert sampler["steps"] == 8
    assert sampler["cfg"] == 1.0
    assert sampler["sampler_name"] == "euler"
    assert sampler["scheduler"] == "simple"


def test_control_after_generate_slot_is_skipped():
    ui, _ = load("krea2-turbo")
    node = next(n for n in ui["nodes"] if n["id"] == 7)
    node["widgets_values"] = [0, "randomize", 8, 1.0, "euler", "simple", 1.0]
    assert ui_inputs_by_name(ui)["7"] == ui_inputs_by_name(load("krea2-turbo")[0])["7"]


def test_linked_widget_and_autogrow_dict():
    ui, _ = load("qwen-image-2.1-edit")
    enc = ui_inputs_by_name(ui)["7"]
    assert enc["negative_prompt"] == ""
    assert enc["resolution"] == 1024
    assert enc["images.image_1"] == ["4", 0]
    assert enc["prompt"] == ["6", 0]


def test_current_layout_keeps_slot_for_linked_widget():
    ui, _ = load("video-wan-2.2-ti2v-5b-q8")
    latent = ui_inputs_by_name(ui)["20"]
    assert latent["length"] == 121
    assert latent["batch_size"] == 1
    assert latent["width"] == ["9008", 0]


@pytest.mark.parametrize("name", NAMES)
def test_changed_api_value_is_drift(name):
    ui, api = load(name)
    node_id = next(k for k, v in api.items() if isinstance(v, dict) and v.get("class_type") in {"KSampler", "Wan22ImageToVideoLatent"})
    key = "steps" if "steps" in api[node_id]["inputs"] else "length"
    mutated = copy.deepcopy(api)
    mutated[node_id]["inputs"][key] = 999
    diffs = name_diff(ui, mutated)
    assert any(f"{node_id}.inputs.{key}" in d for d in diffs), diffs


def test_changed_link_is_drift():
    ui, api = load("krea2-turbo")
    mutated = copy.deepcopy(api)
    mutated["7"]["inputs"]["positive"] = ["99", 0]
    assert any("7.inputs.positive" in d for d in name_diff(ui, mutated))


def test_node_missing_from_ui_twin_is_drift():
    ui, api = load("krea2-turbo")
    ui["nodes"] = [n for n in ui["nodes"] if n["id"] != 7]
    assert any("node 7 in API JSON" in d for d in name_diff(ui, api))


@pytest.mark.parametrize(
    "values",
    [[0, "randomize", 8], [0, 8], [8]],
    ids=["linked-control-slot", "linked-value-slot", "linked-slot-omitted"],
)
def test_linked_seed_layout_preserves_steps_drift(values):
    ui = {
        "nodes": [
            {
                "id": 1,
                "type": "TransformA",
                "inputs": [
                    {"name": "seed", "widget": {"name": "seed"}, "link": 1},
                    {"name": "steps", "widget": {"name": "steps"}},
                ],
                "widgets_values": values,
            },
            {"id": 9, "type": "SourceA", "inputs": [], "widgets_values": []},
        ],
        "links": [[1, 9, 0, 1, 0, "INT"]],
    }
    api = {
        "1": {API_NODE_TYPE: "TransformA", "inputs": {"seed": ["9", 0], "steps": 8}},
        "9": {API_NODE_TYPE: "SourceA", "inputs": {}},
    }

    assert ui_inputs_by_name(ui)["1"] == {"seed": ["9", 0], "steps": 8}
    assert name_diff(ui, api) == []
    api["1"]["inputs"]["steps"] = 999
    assert name_diff(ui, api) == ["  ~ 1.inputs.steps: api 999 != ui 8"]

    new_ui = copy.deepcopy(ui)
    new_ui["nodes"][0]["widgets_values"][-1] = 13
    fresh_api = {
        "1": {API_NODE_TYPE: "TransformA", "inputs": {"seed": ["9", 0], "steps": 8}},
        "9": {API_NODE_TYPE: "SourceA", "inputs": {}},
    }
    assert ui_inputs_by_name(new_ui)["1"] == {"seed": ["9", 0], "steps": 13}
    assert name_diff(new_ui, fresh_api) == ["  ~ 1.inputs.steps: api 8 != ui 13"]


def write_pair(tmp_path, name="pair", api_type="TransformA", ui_type="TransformA"):
    ui = {"nodes": [{"id": 1, "type": ui_type, "inputs": [], "widgets_values": []}], "links": []}
    api = {"1": {API_NODE_TYPE: api_type, "inputs": {}}}
    ui_path = tmp_path / f"{name}.ui.json"
    api_path = tmp_path / f"{name}.json"
    ui_path.write_text(json.dumps(ui))
    api_path.write_text(json.dumps(api))
    return ui_path, api_path


def test_node_type_mismatch_is_drift(tmp_path):
    ui_path, api_path = write_pair(tmp_path, api_type="TransformB")
    assert name_diff(json.loads(ui_path.read_text()), json.loads(api_path.read_text())) == [
        "  ~ node 1 type: api TransformB != ui TransformA"
    ]


def test_matching_node_types_are_not_drift(tmp_path):
    ui_path, api_path = write_pair(tmp_path)
    assert name_diff(json.loads(ui_path.read_text()), json.loads(api_path.read_text())) == []


@pytest.mark.parametrize("explicit", [False, True], ids=["discovery", "explicit"])
def test_orphan_ui_is_drift(tmp_path, monkeypatch, capsys, explicit):
    ui_path, api_path = write_pair(tmp_path, "orphan")
    api_path.unlink()
    monkeypatch.setattr(drift, "REPO_DIRS", [tmp_path])
    args = ["--no-frontend"]
    if explicit:
        args += ["--paths", str(ui_path)]
    assert asyncio.run(drift.main(args)) == 1
    assert "[orphan] orphan.json: no API sibling" in capsys.readouterr().out


@pytest.mark.parametrize("broken", ["api", "ui"])
def test_invalid_json_continues_and_errors_win(tmp_path, monkeypatch, capsys, broken):
    ui_path, api_path = write_pair(tmp_path, "a-bad")
    (api_path if broken == "api" else ui_path).write_text("{")
    write_pair(tmp_path, "b-valid")
    _, orphan_api = write_pair(tmp_path, "c-orphan")
    orphan_api.unlink()
    monkeypatch.setattr(drift, "REPO_DIRS", [tmp_path])
    assert asyncio.run(drift.main(["--no-frontend"])) == 2
    out = capsys.readouterr().out
    assert "[error] a-bad.json:" in out
    assert "[ok]    b-valid.json" in out
    assert "[orphan] c-orphan.json:" in out
    assert out.index("[error]") < out.index("[ok]")


def test_read_error_continues(tmp_path, monkeypatch, capsys):
    ui_path, _ = write_pair(tmp_path, "a-unreadable")
    ui_path.unlink()
    ui_path.mkdir()
    write_pair(tmp_path, "b-valid")
    monkeypatch.setattr(drift, "REPO_DIRS", [tmp_path])
    assert asyncio.run(drift.main(["--no-frontend"])) == 2
    out = capsys.readouterr().out
    assert "[error] a-unreadable.json:" in out
    assert "[ok]    b-valid.json" in out


@pytest.mark.parametrize("broken,value", [
    ("api", []), ("ui", None), ("ui", {"nodes": {}}),
    ("ui", {"nodes": [None]}), ("api", {"1": []}),
    ("ui", {"nodes": [{"id": 1, "inputs": [None]}]}),
    ("api", {"1": {"inputs": []}}),
])
def test_wrong_shape_continues(tmp_path, monkeypatch, capsys, broken, value):
    ui_path, api_path = write_pair(tmp_path, "a-bad")
    (api_path if broken == "api" else ui_path).write_text(json.dumps(value))
    write_pair(tmp_path, "b-valid")
    monkeypatch.setattr(drift, "REPO_DIRS", [tmp_path])
    assert asyncio.run(drift.main(["--no-frontend"])) == 2
    out = capsys.readouterr().out
    assert "[error] a-bad.json:" in out
    assert "[ok]    b-valid.json" in out
    assert out.index("[error]") < out.index("[ok]")


def test_nonexistent_explicit_ui_is_input_error(tmp_path, capsys):
    missing = tmp_path / "missing.ui.json"
    assert asyncio.run(drift.main(["--paths", str(missing), "--no-frontend"])) == 2
    out = capsys.readouterr().out
    assert "[error] missing.json:" in out
    assert "[orphan]" not in out


def test_unreadable_ui_without_sibling_is_input_error(tmp_path, capsys):
    unreadable = tmp_path / "unreadable.ui.json"
    unreadable.mkdir()
    assert asyncio.run(drift.main(["--paths", str(unreadable), "--no-frontend"])) == 2
    out = capsys.readouterr().out
    assert "[error] unreadable.json:" in out
    assert "[orphan]" not in out


@pytest.mark.parametrize("values,message", [
    ("not-a-list", "UI node widgets_values must be a list"),
    ([0, {}, 0], "UI widget values cannot be consumed by the layout"),
    ([0, {}], "UI widget values cannot be consumed by the layout"),
    ([0, []], "UI widget values cannot be consumed by the layout"),
    ([0, "not-a-control"], "UI widget values cannot be consumed by the layout"),
], ids=["scalar", "unconsumed-object", "control-object", "control-list", "control-string"])
def test_bad_widget_values_continues(tmp_path, monkeypatch, capsys, values, message):
    ui_path, api_path = write_pair(tmp_path, "a-bad")
    ui = json.loads(ui_path.read_text())
    ui["nodes"][0]["inputs"] = [{"name": "seed", "widget": {"name": "seed"}}]
    ui["nodes"][0]["widgets_values"] = values
    ui_path.write_text(json.dumps(ui))
    api = json.loads(api_path.read_text())
    api["1"]["inputs"] = {"seed": 0}
    api_path.write_text(json.dumps(api))
    write_pair(tmp_path, "b-valid")
    monkeypatch.setattr(drift, "REPO_DIRS", [tmp_path])
    assert asyncio.run(drift.main(["--no-frontend"])) == 2
    out = capsys.readouterr().out
    assert f"[error] a-bad.json: {message}" in out
    assert "[ok]    b-valid.json" in out
    assert out.index("[error]") < out.index("[ok]")
    assert "[ok]    a-bad.json" not in out
    assert "1 workflow(s) could not be read; 0 workflow(s) drifted" in out


def test_valid_fixed_control_reports_ok(tmp_path, monkeypatch, capsys):
    ui_path, api_path = write_pair(tmp_path, "valid-control")
    ui = json.loads(ui_path.read_text())
    ui["nodes"][0]["inputs"] = [{"name": "seed", "widget": {"name": "seed"}}]
    ui["nodes"][0]["widgets_values"] = [0, "fixed"]
    ui_path.write_text(json.dumps(ui))
    api = json.loads(api_path.read_text())
    api["1"]["inputs"] = {"seed": 0}
    api_path.write_text(json.dumps(api))
    monkeypatch.setattr(drift, "REPO_DIRS", [tmp_path])
    assert asyncio.run(drift.main(["--no-frontend"])) == 0
    out = capsys.readouterr().out
    assert "[ok]    valid-control.json" in out
    assert "[error]" not in out
    assert "[drift]" not in out
    assert "no drift" in out
