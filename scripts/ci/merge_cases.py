#!/usr/bin/env python3
"""Merge one gate run's test reports into cases.jsonl, one line per attempt.

Usage:
  merge_cases.py --sha SHA --seed N --out cases.jsonl
                 [--pytest JUNIT_XML ROOT]... [--vitest JSON ROOT]...
                 [--playwright JSON ROOT]...

ROOT is the checkout the report ran in; case files are recorded relative to
it. Each line holds: framework, config, project, case (the full case ID),
file, outcome (passed, failed, skipped or xfailed), attempt (0 is the first
run of a case), duration_s, seed and sha. scripts/ci/policy.py reads it.

pytest's JUnit XML names a case by dotted class name; the module is the
longest dotted prefix that is a .py file under ROOT. Repeated entries for one
node ID (a teardown error, failed subtests) collapse to its worst outcome.
vitest's JSON report has no retry field; its attempt is always 0, and policy
requires `retry: 0` in its config instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SEVERITY = {"passed": 0, "xfailed": 1, "skipped": 2, "failed": 3}
PLAYWRIGHT_OUTCOME = {"passed": "passed", "skipped": "skipped", "failed": "failed",
                      "timedOut": "failed", "interrupted": "failed"}
VITEST_OUTCOME = {"passed": "passed", "failed": "failed", "skipped": "skipped",
                  "pending": "skipped", "todo": "skipped"}


def relative(path: str, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def pytest_cases(report: Path, root: Path) -> list[dict]:
    cases: dict[str, dict] = {}
    for testcase in ET.parse(report).iter("testcase"):
        parts = testcase.get("classname", "").split(".")
        for split in range(len(parts), 0, -1):
            module = "/".join(parts[:split]) + ".py"
            if (root / module).is_file():
                break
        else:
            sys.exit(f"merge_cases: no module under {root} for {testcase.get('classname')}")
        case = "::".join([module, *parts[split:], testcase.get("name", "")])
        outcome = "passed"
        for child in testcase:
            if child.tag in ("failure", "error"):
                outcome = "failed"
            elif child.tag == "skipped" and outcome == "passed":
                outcome = "xfailed" if child.get("type") == "pytest.xfail" else "skipped"
        seconds = float(testcase.get("time") or 0)
        if case in cases:
            previous = cases[case]
            previous["duration_s"] += seconds
            if SEVERITY[outcome] > SEVERITY[previous["outcome"]]:
                previous["outcome"] = outcome
        else:
            cases[case] = {"framework": "pytest", "config": "", "project": "", "case": case,
                           "file": module, "outcome": outcome, "attempt": 0, "duration_s": seconds}
    return list(cases.values())


def vitest_cases(report: Path, root: Path) -> list[dict]:
    data = json.loads(report.read_text(encoding="utf-8"))
    cases = []
    for result in data["testResults"]:
        file = relative(result["name"], root)
        for assertion in result["assertionResults"]:
            titles = [*assertion["ancestorTitles"], assertion["title"]]
            cases.append({"framework": "vitest", "config": "vitest.config.ts", "project": "",
                          "case": " > ".join([file, *titles]), "file": file,
                          "outcome": VITEST_OUTCOME[assertion["status"]], "attempt": 0,
                          "duration_s": (assertion.get("duration") or 0) / 1000})
    return cases


def playwright_cases(report: Path, root: Path) -> list[dict]:
    data = json.loads(report.read_text(encoding="utf-8"))
    config = Path(data["config"]["configFile"]).name
    test_dir = data["config"]["rootDir"]
    cases = []

    def walk(suite: dict, titles: list[str]) -> None:
        for spec in suite.get("specs", []):
            file = relative(os.path.join(test_dir, spec["file"]), root)
            case = " > ".join([file, *titles, spec["title"]])
            for test in spec["tests"]:
                # A test that never started (an earlier fatal error) has no result.
                results = test["results"] or [{"retry": 0, "status": "skipped", "duration": 0}]
                for result in results:
                    cases.append({"framework": "playwright", "config": config,
                                  "project": test["projectName"], "case": case, "file": file,
                                  "outcome": PLAYWRIGHT_OUTCOME[result["status"]],
                                  "attempt": result["retry"], "duration_s": result["duration"] / 1000})
        for child in suite.get("suites", []):
            walk(child, [*titles, child["title"]])

    for top in data["suites"]:  # one per file; its title is the file name
        walk(top, [])
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    for name in ("pytest", "vitest", "playwright"):
        parser.add_argument(f"--{name}", nargs=2, action="append", default=[], metavar=("REPORT", "ROOT"))
    args = parser.parse_args()
    readers = {"pytest": pytest_cases, "vitest": vitest_cases, "playwright": playwright_cases}
    rows = []
    for name, reader in readers.items():
        for report, root in getattr(args, name):
            rows += reader(Path(report), Path(root))
    if not rows:
        sys.exit("merge_cases: the reports hold no cases")
    temporary = args.out.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps({**row, "seed": args.seed, "sha": args.sha}) + "\n")
    temporary.replace(args.out)
    print(f"merge_cases: {len(rows)} attempts -> {args.out}")


if __name__ == "__main__":
    main()
