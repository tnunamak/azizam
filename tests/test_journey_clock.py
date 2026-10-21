"""scripts/ci/journey_clock.py: one clock for the ten-minute first-run bar.

The Compose bring-up (tests/e2e/fresh_instance.py --compose) and the browser
journey (web/e2e/first-run.spec.ts) each write their seconds to a file. The
gate adds them into the "journey" section of cost.json and fails at the limit.
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/journey_clock.py"


def run(tmp_path, compose, browser, *extra):
    files = {}
    for name, body in (("compose.json", compose), ("browser.json", browser)):
        files[name] = tmp_path / name
        if body is not None:
            files[name].write_text(body if isinstance(body, str) else json.dumps(body))
    out = tmp_path / "cost.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--compose", str(files["compose.json"]),
         "--first-run", str(files["browser.json"]), "--out", str(out), *extra],
        capture_output=True, text=True)
    return result, out


def test_sums_both_halves_into_cost_json(tmp_path):
    result, out = run(tmp_path, {"pull_s": 20.0, "build_s": 100.5, "up_s": 30.0, "bringup_s": 150.5},
                      {"elapsed_seconds": 7.5})
    assert result.returncode == 0, result.stderr
    cost = json.loads(out.read_text())
    assert cost == {"journey": {"compose_bringup_s": 150.5, "first_run_ui_s": 7.5, "total_s": 158.0,
                                "limit_s": 600, "passed": True}}
    assert "158.0 s of 600 s" in result.stdout


def test_fails_above_the_budget_and_still_writes_cost_json(tmp_path):
    result, out = run(tmp_path, {"pull_s": 0, "build_s": 0, "up_s": 0, "bringup_s": 590.0},
                      {"elapsed_seconds": 10.5})
    assert result.returncode == 1
    assert "600.5 s" in result.stderr and "600 s" in result.stderr
    journey = json.loads(out.read_text())["journey"]
    assert journey["total_s"] == 600.5 and journey["passed"] is False


def test_exactly_the_budget_fails(tmp_path):
    # "Under 10 minutes": the bar is strict.
    result, _ = run(tmp_path, {"bringup_s": 300.0}, {"elapsed_seconds": 300.0})
    assert result.returncode == 1


def test_updates_only_the_journey_section_of_an_existing_cost_json(tmp_path):
    (tmp_path / "cost.json").write_text(json.dumps({"schema": 1, "totals": {"wall_s": 9.0},
                                                    "journey": {"total_s": 1, "stale": True}}))
    result, out = run(tmp_path, {"bringup_s": 10.0}, {"elapsed_seconds": 5.0})
    assert result.returncode == 0, result.stderr
    cost = json.loads(out.read_text())
    assert cost["schema"] == 1 and cost["totals"] == {"wall_s": 9.0}
    assert cost["journey"] == {"compose_bringup_s": 10.0, "first_run_ui_s": 5.0, "total_s": 15.0,
                               "limit_s": 600, "passed": True}


def test_refuses_to_overwrite_a_cost_json_that_is_not_an_object(tmp_path):
    (tmp_path / "cost.json").write_text("[1]")
    result, out = run(tmp_path, {"bringup_s": 10.0}, {"elapsed_seconds": 5.0})
    assert result.returncode == 1
    assert out.read_text() == "[1]"


def test_a_missing_half_fails_without_a_cost_json(tmp_path):
    result, out = run(tmp_path, {"bringup_s": 5.0}, None)
    assert result.returncode == 1
    assert "first-run" in result.stderr
    assert not out.exists()


def test_a_non_number_fails(tmp_path):
    result, out = run(tmp_path, {"bringup_s": "fast"}, {"elapsed_seconds": 1.0})
    assert result.returncode == 1
    assert not out.exists()


def test_the_budget_can_be_lowered(tmp_path):
    result, _ = run(tmp_path, {"bringup_s": 50.0}, {"elapsed_seconds": 1.0}, "--budget", "30")
    assert result.returncode == 1
