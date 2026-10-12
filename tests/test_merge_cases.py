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


def test_playwright_retries_become_attempts(tmp_path: Path) -> None:
    root = tmp_path / "lane"
    report = {
        "config": {"configFile": str(root / "web" / "playwright.config.ts"), "rootDir": str(root / "web" / "e2e")},
        "suites": [{"title": "home.spec.ts", "file": "home.spec.ts", "specs": [], "suites": [{
            "title": "Home", "file": "home.spec.ts", "suites": [], "specs": [{
                "title": "loads", "file": "home.spec.ts", "tests": [{
                    "projectName": "chromium",
                    "results": [{"retry": 0, "status": "failed", "duration": 1500},
                                {"retry": 1, "status": "passed", "duration": 500}]}]}]}]}],
    }
    (tmp_path / "pw.json").write_text(json.dumps(report))
    rows = _merge(tmp_path, "--playwright", str(tmp_path / "pw.json"), str(root))
    assert [(row["config"], row["project"], row["case"], row["outcome"], row["attempt"]) for row in rows] == [
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > loads", "failed", 0),
        ("playwright.config.ts", "chromium", "web/e2e/home.spec.ts > Home > loads", "passed", 1),
    ]


def test_vitest_cases_use_the_title_path(tmp_path: Path) -> None:
    root = tmp_path / "work"
    report = {"testResults": [{"name": str(root / "web" / "tests" / "a.spec.ts"), "assertionResults": [
        {"ancestorTitles": ["group"], "title": "works", "status": "passed", "duration": 12},
        {"ancestorTitles": [], "title": "later", "status": "todo", "duration": None}]}]}
    (tmp_path / "vitest.json").write_text(json.dumps(report))
    rows = _merge(tmp_path, "--vitest", str(tmp_path / "vitest.json"), str(root))
    assert [(row["case"], row["file"], row["outcome"]) for row in rows] == [
        ("web/tests/a.spec.ts > group > works", "web/tests/a.spec.ts", "passed"),
        ("web/tests/a.spec.ts > later", "web/tests/a.spec.ts", "skipped"),
    ]
