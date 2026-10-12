#!/usr/bin/env python3
"""Merge one gate run's test reports into cases.jsonl, one line per attempt.

Usage:
  merge_cases.py --sha SHA --seed N --out cases.jsonl
                 [--pytest JUNIT_XML ROOT]... [--receipts CASES_JSONL ROOT]...

ROOT is the checkout the report ran in; case files are recorded relative to
it. Each output line holds: framework, config, project, case (the full case
ID), file, outcome (passed, failed, skipped or xfailed), attempt (0 is the
first run of a case), duration_s, seed and sha. scripts/ci/policy.py reads it.

--receipts reads the lines that web/tests/case-receipts.ts (vitest) and
web/e2e/case-receipts.ts (Playwright) append, one per attempt. A test ID's
first line is attempt 0, its next line attempt 1, and so on: framework
retries and a second run into the same file both raise the attempt index.

pytest's JUnit XML names a case by dotted class name; the module is the
longest dotted prefix that is a .py file under ROOT. Repeated entries for one
node ID (a teardown error, failed subtests) collapse to its worst outcome;
run-hermetic-pytest.sh refuses to overwrite a report, so one file is one run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

SEVERITY = {"passed": 0, "xfailed": 1, "skipped": 2, "failed": 3}
OUTCOME = {"passed": "passed", "failed": "failed", "timedOut": "failed", "interrupted": "failed",
           "skipped": "skipped", "pending": "skipped"}


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


def receipt_cases(report: Path, root: Path) -> list[dict]:
    seen: Counter[tuple[str, str, str]] = Counter()
    cases = []
    for line in report.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        key = (row["framework"], row["config"], row["id"])
        file = Path(os.path.relpath(row["file"], root)).as_posix()
        cases.append({"framework": row["framework"], "config": row["config"], "project": row["project"],
                      "case": " > ".join([file, *row["titles"]]), "file": file,
                      "outcome": OUTCOME[row["status"]], "attempt": seen[key],
                      "duration_s": row["duration_ms"] / 1000,
                      # The isolated fixture's data namespace (TEST_STRATEGY.md 3.4.7).
                      **({"namespace": row["namespace"]} if row.get("namespace") else {})})
        seen[key] += 1
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pytest", nargs=2, action="append", default=[], metavar=("REPORT", "ROOT"))
    parser.add_argument("--receipts", nargs=2, action="append", default=[], metavar=("REPORT", "ROOT"))
    args = parser.parse_args()
    rows = []
    for report, root in args.pytest:
        rows += pytest_cases(Path(report), Path(root))
    for report, root in args.receipts:
        rows += receipt_cases(Path(report), Path(root))
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
