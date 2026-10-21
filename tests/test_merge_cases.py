"""scripts/ci/merge_cases.py turns each framework's report into case receipts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MERGE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "merge_cases.py"

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="pkg.sub.test_a.TestThing" name="test_x[1.5]" time="0.5"/>
  <testcase classname="pkg.sub.test_a" name="test_skip" time="0"><skipped type="pytest.skip" message="no"/></testcase>
  <testcase classname="pkg.sub.test_a" name="test_xfail" time="0"><skipped type="pytest.xfail" message=""/></testcase>
  <testcase classname="pkg.sub.test_a" name="test_teardown" time="0.25"/>
  <testcase classname="pkg.sub.test_a" name="test_teardown" time="0.25"><error message="teardown"/></testcase>
</testsuite></testsuites>
"""


def _merge(tmp_path: Path, *inputs: str) -> list[dict]:
    out = tmp_path / "cases.jsonl"
    subprocess.run([sys.executable, str(MERGE), "--sha", "abc", "--seed", "7", "--out", str(out), *inputs],
                   check=True, capture_output=True, text=True)
    return [json.loads(line) for line in out.read_text().splitlines()]


def test_pytest_cases_get_node_ids_and_worst_outcome(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "sub" / "test_a.py").write_text("")
    (tmp_path / "junit.xml").write_text(JUNIT)
    rows = {row["case"]: row for row in _merge(tmp_path, "--pytest", str(tmp_path / "junit.xml"), str(root))}
    assert {case: row["outcome"] for case, row in rows.items()} == {
        "pkg/sub/test_a.py::TestThing::test_x[1.5]": "passed",
        "pkg/sub/test_a.py::test_skip": "skipped",
        "pkg/sub/test_a.py::test_xfail": "xfailed",
        "pkg/sub/test_a.py::test_teardown": "failed",
    }
    teardown = rows["pkg/sub/test_a.py::test_teardown"]
    assert (teardown["file"], teardown["attempt"], teardown["duration_s"]) == ("pkg/sub/test_a.py", 0, 0.5)
    assert (teardown["seed"], teardown["sha"]) == (7, "abc")


def _receipt(root: Path, test_id: str, titles: list[str], status: str, framework: str = "playwright") -> str:
    config, project = ("playwright.config.ts", "chromium") if framework == "playwright" else ("vitest.config.ts", "")
    return json.dumps({"framework": framework, "config": config, "project": project, "id": test_id,
                       "file": str(root / "web" / "e2e" / "home.spec.ts"), "titles": titles,
                       "status": status, "duration_ms": 500}) + "\n"


def test_each_repeated_test_id_is_a_later_attempt(tmp_path: Path) -> None:
    root = tmp_path / "lane"
    first_run = _receipt(root, "a", ["Home", "loads"], "failed") + _receipt(root, "a", ["Home", "loads"], "passed")
    first_run += _receipt(root, "b", ["Home", "same title"], "passed") + _receipt(root, "c", ["Home", "same title"], "timedOut")
    second_run = _receipt(root, "c", ["Home", "same title"], "passed")  # a shell rerun into the same file
    (tmp_path / "cases.jsonl").write_text(first_run + second_run)
    rows = _merge(tmp_path, "--receipts", str(tmp_path / "cases.jsonl"), str(root))
    assert [(row["config"], row["project"], row["case"], row["outcome"], row["attempt"]) for row in rows] == [
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > loads", "failed", 0),
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > loads", "passed", 1),
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > same title", "passed", 0),
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > same title", "failed", 0),
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > same title", "passed", 1),
    ]
    assert {row["file"] for row in rows} == {"web/e2e/home.spec.ts"}


def test_vitest_states_map_to_outcomes(tmp_path: Path) -> None:
    root = tmp_path / "work"
    lines = "".join(_receipt(root, str(index), ["t"], status, "vitest")
                    for index, status in enumerate(["passed", "failed", "skipped", "pending"]))
    (tmp_path / "cases.jsonl").write_text(lines)
    rows = _merge(tmp_path, "--receipts", str(tmp_path / "cases.jsonl"), str(root))
    assert [(row["framework"], row["outcome"], row["attempt"]) for row in rows] == [
        ("vitest", "passed", 0), ("vitest", "failed", 0), ("vitest", "skipped", 0), ("vitest", "skipped", 0)]


def test_a_browser_case_keeps_its_data_namespace(tmp_path: Path) -> None:
    """The isolated fixture's namespace reaches cases.jsonl, so a collision
    between two invocations can be traced to its data (TEST_STRATEGY.md 3.4.7)."""
    root = tmp_path / "lane"
    tagged = json.loads(_receipt(root, "a", ["People", "invites"], "passed"))
    (tmp_path / "cases.jsonl").write_text(json.dumps({**tagged, "namespace": "run-lane-chromium-w0-i3"}) + "\n"
                                          + _receipt(root, "b", ["People", "lists"], "passed"))
    rows = _merge(tmp_path, "--receipts", str(tmp_path / "cases.jsonl"), str(root))
    assert [row.get("namespace") for row in rows] == ["run-lane-chromium-w0-i3", None]
