#!/usr/bin/env python3
"""Check one gate run against the test policy (docs/design/TEST_STRATEGY.md 3, 6).

Usage: policy.py --root CHECKOUT --cases cases.jsonl [--base-budgets FILE] [--today YYYY-MM-DD]

CHECKOUT is the clean tree the gate ran; cases.jsonl comes from
merge_cases.py. Rules, each failing the step with one line per violation:

- retries: no rerun plugin in tests/requirements-hermetic.lock; no flaky or
  rerun marker in Python test code; no --retries/--retry/--reruns flag in
  the gate scripts; in web test code and test configs every retry, retries
  or repeats value is the literal 0 (or false), so a computed value fails;
  `retries: 0` in every Playwright config the run used and `retry: 0` in the
  vitest config; no attempt index above 0 in cases.jsonl, which also
  catches a second run into the same receipts.
- discovery: every test file under CHECKOUT ran in this gate, or is listed in
  tests/suites.toml: an [[orphan]] whose expires_on has not passed, a
  [[scheduled]] file that `scripts/test-scheduled` runs (its `task` must be a
  task that script knows), or a [[live]] file that needs real upstreams
  (TEST_STRATEGY.md 1.3, 1.4). An entry for a file that ran in the gate, no
  longer exists, or is listed twice is stale and fails too.
- skips: every skipped (xfailed) case is a [[skip]] ([[xfail]]) entry in
  tests/suites.toml for its framework, config and project, and every entry
  was skipped (xfailed) in this run.
- quarantine: no quarantine manifest file and no quarantine marker in test
  code (test files, conftest.py, tests/**/*.py, test configs).
- counts: first-attempt cases per framework stay within tests/budgets.toml
  [ceilings], in the units of TEST_STRATEGY.md 6.
- ratchets: with --base-budgets FILE (the target branch's tests/budgets.toml),
  no [ratchets] value is above its value there and none was removed, so a
  ratchet can only fall. The ratchet's own test checks the real count against
  the value.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

# Dependencies and tool output only: a pruned name hides any test file below it.
PRUNED_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", ".ci-fresh-venv",
               ".next", "test-results"}
PYTHON_TEST = re.compile(r"^(test_.*|.*_test)\.py$")
WEB_TEST = re.compile(r"^.*\.(spec|test|e2e)\.[cm]?[jt]sx?$|^.*\.household\.ts$")
TEST_CONFIG = re.compile(r"^(playwright|vitest)\b.*\.config\.[cm]?[jt]s$")
# The tasks scripts/test-scheduled runs files for (tests/test_test_scheduled.py
# keeps this set equal to the script's own).
SCHEDULED_TASKS = {"sdk-probes", "sweep"}
RERUN_PACKAGES = {"pytest-rerunfailures", "pytest-retry", "flaky", "pytest-flaky"}
PYTHON_RETRY_MARKER = re.compile(r"\bmark\.flaky\b|@flaky\b|\breruns\s*=")
RETRY_FLAG = re.compile(r"--(retries|retry|reruns)\b")
RETRY_FLAG_FILES = ("scripts/ci-local", "scripts/test-affected", "web/package.json")
RETRY_SETTING = re.compile(r"\b(retry|retries|repeats)\s*:\s*([^,}\n]*)")
# An object-literal shorthand ({ retries }); `={retry}` is a JSX prop, not one.
RETRY_SHORTHAND = re.compile(r"(?:(?<!=)\{|,)\s*(retry|retries|repeats)\s*[,}]")
QUARANTINE_MANIFEST = re.compile(r"^quarantine.*\.(json|toml|ya?ml|txt)$", re.IGNORECASE)
QUARANTINE_MARKER = re.compile(r"\bmark\.quarantine\b|@quarantine\b|\btest(\.describe)?\.quarantine\b")


def all_files(root: Path) -> list[str]:
    """Every file under root outside dependency and output directories, root-relative."""
    found = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in PRUNED_DIRS]
        found += [Path(directory, name).relative_to(root).as_posix() for name in files]
    return sorted(found)


def discover(files: list[str]) -> list[str]:
    """The test files among files."""
    return [path for path in files if PYTHON_TEST.match(Path(path).name) or WEB_TEST.match(Path(path).name)]


def test_code(files: list[str]) -> list[str]:
    """Files whose text can configure retries or markers: test files, test
    configs, conftest.py files and Python support code under tests/."""
    tests = set(discover(files))
    return [path for path in files if path in tests or TEST_CONFIG.match(Path(path).name)
            or Path(path).name == "conftest.py" or (path.startswith("tests/") and path.endswith(".py"))]


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def read(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def check_retries(root: Path, code: list[str], cases: list[dict]) -> list[str]:
    problems = []
    for line in read(root, "tests/requirements-hermetic.lock").splitlines():
        package = re.split(r"[=<>~!\[ ;]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")
        if package in RERUN_PACKAGES:
            problems.append(f"retries: rerun plugin {package} is in tests/requirements-hermetic.lock")
    for relative in RETRY_FLAG_FILES:
        if match := RETRY_FLAG.search(read(root, relative)):
            problems.append(f"retries: {relative} passes {match.group(0)}")
    for relative in code:
        text = read(root, relative)
        if relative.endswith(".py"):
            if match := PYTHON_RETRY_MARKER.search(text):
                problems.append(f"retries: {relative} uses {match.group(0)}")
            continue
        for match in RETRY_SETTING.finditer(text):
            if match.group(2).strip() not in ("0", "false"):
                problems.append(f"retries: {relative} sets {match.group(0).strip()}; only a literal 0 is allowed")
        if match := RETRY_SHORTHAND.search(text):
            problems.append(f"retries: {relative} passes {match.group(1)} by shorthand; only a literal 0 is allowed")
    used = {f"web/{row['config']}" for row in cases if row["framework"] == "playwright"}
    for relative in sorted(used):
        if not re.search(r"\bretries\s*:\s*0\b", read(root, relative)):
            problems.append(f"retries: {relative} must set retries: 0")
    if not re.search(r"\bretry\s*:\s*0\b", read(root, "web/vitest.config.ts")):
        problems.append("retries: web/vitest.config.ts must set retry: 0")
    for row in cases:
        if row["attempt"] > 0:
            problems.append(f"retries: {row['framework']} {row['case']} ran attempt {row['attempt']}")
    return problems


def check_discovery(tests: list[str], cases: list[dict], suites: dict, today: datetime.date) -> list[str]:
    problems = []
    ran = {row["file"] for row in cases}
    listed: dict[str, str] = {}

    def list_file(table: str, entry: dict) -> str:
        path = entry.get("path", "")
        if path in listed:
            problems.append(f"discovery: {path} is listed in [[{listed[path]}]] and [[{table}]]; list it once")
        listed.setdefault(path, table)
        if path in ran:
            problems.append(f"discovery: {table} {path} now runs in the gate; remove its tests/suites.toml entry")
        elif path not in tests:
            problems.append(f"discovery: {table} {path} does not exist; remove its tests/suites.toml entry")
        if not entry.get("reason"):
            problems.append(f"discovery: {table} {path} needs a reason")
        return path

    for entry in suites.get("orphan", []):
        path = list_file("orphan", entry)
        expires = entry.get("expires_on")
        if not isinstance(expires, datetime.date):
            problems.append(f"discovery: orphan {path} needs an expires_on date")
        elif expires < today:
            problems.append(f"discovery: orphan {path} expired on {expires}; run it in a gate step or delete it")
    for entry in suites.get("scheduled", []):
        path = list_file("scheduled", entry)
        if entry.get("task") not in SCHEDULED_TASKS:
            problems.append(f"discovery: scheduled {path} has task {entry.get('task')!r}; "
                            f"scripts/test-scheduled runs {sorted(SCHEDULED_TASKS)}")
    for entry in suites.get("live", []):
        list_file("live", entry)
    for path in tests:
        if path not in ran and path not in listed:
            problems.append(f"discovery: {path} ran in no gate step; run it in the gate, list it as a "
                            "[[scheduled]] file that scripts/test-scheduled runs or a [[live]] file, "
                            "or as an [[orphan]] with expires_on")
    return problems


def check_skips(cases: list[dict], suites: dict) -> list[str]:
    def name(key: tuple[str, str, str, str]) -> str:
        return " ".join(part for part in key if part)

    problems = []
    for outcome, table in (("skipped", "skip"), ("xfailed", "xfail")):
        allowed = {(entry.get("framework", ""), entry.get("config", ""), entry.get("project", ""),
                    entry.get("case", "")) for entry in suites.get(table, [])}
        observed = {(row["framework"], row["config"], row["project"], row["case"])
                    for row in cases if row["outcome"] == outcome}
        for key in sorted(observed - allowed):
            problems.append(f"skips: {name(key)} was {outcome} but is not a [[{table}]] entry in tests/suites.toml")
        for key in sorted(allowed - observed):
            problems.append(f"skips: [[{table}]] {name(key)} was not {outcome} in this run; remove the entry")
    return problems


def check_quarantine(root: Path, files: list[str], code: list[str]) -> list[str]:
    problems = []
    for relative in files:
        if QUARANTINE_MANIFEST.match(Path(relative).name):
            problems.append(f"quarantine: manifest {relative} exists; fix or revert the flake (TEST_STRATEGY.md 3.2)")
    for relative in code:
        if match := QUARANTINE_MARKER.search(read(root, relative)):
            problems.append(f"quarantine: {relative} uses {match.group(0)}")
    return problems


def check_counts(cases: list[dict], budgets: dict) -> tuple[list[str], dict[str, int]]:
    # First attempts count: a case ID can repeat (a vitest it.each title), and
    # retries, which are banned anyway, must not inflate the count.
    counts: dict[str, int] = defaultdict(int)
    for row in cases:
        counts[row["framework"]] += row["attempt"] == 0
    counts = dict(sorted(counts.items()))
    ceilings = budgets.get("ceilings", {})
    problems = []
    for framework, count in counts.items():
        ceiling = ceilings.get(framework)
        if ceiling is None:
            problems.append(f"counts: tests/budgets.toml has no ceiling for {framework}")
        elif count > ceiling:
            problems.append(f"counts: {framework} has {count} cases, over its ceiling {ceiling}")
    return problems, counts


def check_ratchets(budgets: dict, base: dict) -> list[str]:
    ratchets = budgets.get("ratchets", {})
    problems = []
    for name, base_value in base.get("ratchets", {}).items():
        value = ratchets.get(name)
        if value is None:
            problems.append(f"ratchets: {name} was removed from tests/budgets.toml (the target branch has {base_value})")
        elif value > base_value:
            problems.append(f"ratchets: {name} is {value}, above {base_value} on the target branch; a ratchet only falls")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--base-budgets", type=Path, help="the target branch's tests/budgets.toml")
    parser.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    args = parser.parse_args()
    root = args.root.resolve()
    suites = tomllib.loads(read(root, "tests/suites.toml"))
    budgets = tomllib.loads(read(root, "tests/budgets.toml"))
    cases = load_cases(args.cases)
    files = all_files(root)
    tests = discover(files)
    code = test_code(files)
    count_problems, counts = check_counts(cases, budgets)
    if args.base_budgets:
        count_problems += check_ratchets(budgets, tomllib.loads(args.base_budgets.read_text(encoding="utf-8")))
    problems = (check_retries(root, code, cases) + check_discovery(tests, cases, suites, args.today)
                + check_skips(cases, suites) + check_quarantine(root, files, code) + count_problems)
    for problem in problems:
        print(f"policy: {problem}")
    summary = ", ".join(f"{framework} {count}" for framework, count in counts.items())
    print(f"policy: {'FAILED' if problems else 'ok'}: {len(tests)} test files; cases {summary}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
