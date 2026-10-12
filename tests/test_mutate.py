"""Self-tests for scripts/mutate (docs/design/TEST_STRATEGY.md 4.5).

Each test runs the mutation engine on a small throwaway project, so the real
pytest subprocesses, the line tracer and the kill confirmation all run.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "mutation"))
import driver  # noqa: E402
import mutgen  # noqa: E402

CALC = textwrap.dedent('''\
    def clamp(value, low, high):
        if value < low:
            return low
        if value > high:
            return high
        return value


    def label(n):
        return "big" if n >= 10 else "small"
''')
OWN_TESTS = textwrap.dedent('''\
    from calc import clamp, label


    def test_clamp_inside():
        assert clamp(5, 0, 10) == 5


    def test_clamp_below():
        assert clamp(-3, 0, 10) == 0


    def test_label_far_from_the_boundary():
        assert label(20) == "big"
        assert label(1) == "small"
''')
BOUNDARY_TEST = textwrap.dedent('''\
    from calc import label


    def test_label_at_the_boundary():
        assert label(10) == "big"
''')

BOUNDARY = "calc.py:10:cmp:>=->>"
RETURN_LOW = "calc.py:3:return->None:low->None"
CLAMP_LOW = "calc.py:2:cmp:<-><="


def make_project(root: Path, own_tests: str = OWN_TESTS, other_tests: dict[str, str] | None = None) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(CALC)
    (root / "tests" / "test_calc.py").write_text(own_tests)
    for name, text in (other_tests or {}).items():
        (root / "tests" / name).write_text(text)
    return root


def engine(tmp_path: Path, project: Path) -> driver.Engine:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return driver.Engine(project, tmp_path / "work", workers=2, base_env=env,
                         pytest_cmd=[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "xdist"],
                         min_limit=60.0, max_limit=120.0)


SPEC = {"path": "calc.py", "own_tests": [r"^tests/test_calc\b"]}


def score(tmp_path: Path, project: Path, only: set[str], survivors: dict | None = None) -> dict:
    run = engine(tmp_path, project)
    base = run.baseline(["calc.py"], [])
    return run.module("calc", SPEC, base, survivors or {}, only)


def by_id(report: dict) -> dict[str, dict]:
    return {m["id"]: m for m in report["mutants"]}


def test_a_failing_selected_test_refuses_the_module(tmp_path):
    project = make_project(tmp_path / "p", OWN_TESTS.replace("clamp(5, 0, 10) == 5", "clamp(5, 0, 10) == 6"))

    report = score(tmp_path, project, {BOUNDARY})

    assert report["refused"] == "1 selected tests fail on clean code"
    assert report["baseline"]["failed"] == ["tests/test_calc.py::test_clamp_inside"]
    assert "mutants" not in report and "score" not in report


def test_a_collection_error_refuses_the_whole_run(tmp_path):
    project = make_project(tmp_path / "p", other_tests={"test_broken.py": "import does_not_exist\n"})

    with pytest.raises(driver.Refused, match="collection errors"):
        engine(tmp_path, project).baseline(["calc.py"], [])


def test_mutant_ids_name_the_change_and_survive_edits_elsewhere(tmp_path):
    path = tmp_path / "calc.py"
    path.write_text(CALC)
    first = [m["id"] for m in mutgen.generate(str(path), "calc.py")[1]]
    again = [m["id"] for m in mutgen.generate(str(path), "calc.py")[1]]
    # Rewriting line 6 changes its mutants and nothing else's.
    path.write_text(CALC.replace("    return value\n", "    return int(value) + 0\n"))
    edited = [m["id"] for m in mutgen.generate(str(path), "calc.py")[1]]

    assert first == again
    assert len(set(first)) == len(first)
    assert {BOUNDARY, RETURN_LOW, CLAMP_LOW} <= set(first)
    assert [i for i in first if ":6:" not in i] == [i for i in edited if ":6:" not in i]


def test_a_planted_survivor_is_reported_and_a_kill_names_its_test(tmp_path):
    project = make_project(tmp_path / "p")

    report = score(tmp_path, project, {BOUNDARY, RETURN_LOW})
    mutants = by_id(report)

    assert mutants[BOUNDARY]["status"] == "survived"
    assert report["unclassified"] == [BOUNDARY]
    assert mutants[RETURN_LOW]["status"] == "killed"
    assert mutants[RETURN_LOW]["killers"] == ["tests/test_calc.py::test_clamp_below"]
    assert report["counts"]["survived"] == 1 and report["counts"]["killed"] == 1
    assert report["score"] == 0.5


def test_a_survivor_of_its_own_tests_is_killed_by_the_wide_pass(tmp_path):
    project = make_project(tmp_path / "p", other_tests={"test_boundary.py": BOUNDARY_TEST})

    mutant = by_id(score(tmp_path, project, {BOUNDARY}))[BOUNDARY]

    assert (mutant["status"], mutant["phase"]) == ("killed", "wide")
    assert mutant["killers"] == ["tests/test_boundary.py::test_label_at_the_boundary"]


def test_an_equivalent_classification_leaves_the_score_denominator(tmp_path):
    project = make_project(tmp_path / "p")
    survivors = {CLAMP_LOW: {"id": CLAMP_LOW, "class": "equivalent", "reason": "clamp(low) is low"}}

    report = score(tmp_path, project, {CLAMP_LOW, RETURN_LOW}, survivors)

    assert by_id(report)[CLAMP_LOW]["status"] == "equivalent"
    assert report["counts"]["equivalent"] == 1
    assert report["score"] == 1.0
