"""scripts/ci/policy.py fails on each injected violation and passes the clean tree.

Each case copies the files the policy reads into a temporary root, writes a
cases.jsonl in which every non-orphan test file ran once and every allowed
skip was skipped, injects one violation and runs the policy as ci-local does.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "ci" / "policy.py"
_spec = importlib.util.spec_from_file_location("ci_policy", POLICY)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)

READ_FILES = ("tests/requirements-hermetic.lock", "tests/suites.toml", "tests/budgets.toml",
              *policy.RETRY_FLAG_FILES)
TODAY = datetime.date(2026, 10, 12)
# Split so that this file does not itself carry the markers it injects.
FLAKY = "mark." "flaky"
QUARANTINE = "mark." "quarantine"
RERUNS = "re" "runs=2"


def _framework(path: str) -> tuple[str, str]:
    if path.endswith(".py"):
        return "pytest", ""
    if path.endswith(".household.ts"):
        return "playwright", "playwright.household.config.ts"
    if path.startswith("web/e2e/"):
        return "playwright", "playwright.config.ts"
    return "vitest", "vitest.config.ts"


def _row(path: str, case: str, outcome: str = "passed", attempt: int = 0) -> dict:
    framework, config = _framework(path)
    return {"framework": framework, "config": config, "project": "chromium" if framework == "playwright" else "",
            "case": case, "file": path, "outcome": outcome, "attempt": attempt,
            "duration_s": 0.1, "seed": 1, "sha": "0" * 40}


class Tree:
    def __init__(self, root: Path) -> None:
        self.root = root
        files = policy.all_files(ROOT)
        configs = [path for path in files if policy.TEST_CONFIG.match(Path(path).name)]
        for relative in {*policy.discover(files), *configs, *READ_FILES}:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        suites = tomllib.loads((root / "tests/suites.toml").read_text(encoding="utf-8"))
        self.orphans = [entry["path"] for entry in suites.get("orphan", [])]
        skips = {entry["case"]: "skipped" for entry in suites.get("skip", [])}
        skips |= {entry["case"]: "xfailed" for entry in suites.get("xfail", [])}
        self.cases = [_row(path, f"{path}::case") for path in policy.discover(files) if path not in self.orphans]
        self.cases += [_row(case.split("::", 1)[0], case, outcome) for case, outcome in skips.items()]

    def edit(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {relative}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def append(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def run(self, today: datetime.date = TODAY) -> subprocess.CompletedProcess[str]:
        cases = self.root / "cases.jsonl"
        cases.write_text("".join(json.dumps(row) + "\n" for row in self.cases), encoding="utf-8")
        return subprocess.run([sys.executable, str(POLICY), "--root", str(self.root), "--cases", str(cases),
                               "--today", today.isoformat()], text=True, capture_output=True)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path / "root")


def test_clean_tree_passes(tree: Tree) -> None:
    result = tree.run()
    assert result.returncode == 0, result.stdout
    assert "policy: ok:" in result.stdout


def _first(tree: Tree, prefix: str) -> str:
    return next(row for row in tree.cases if row["file"].startswith(prefix) and row["outcome"] == "passed")


def _inject_rerun_plugin(tree: Tree) -> None:
    tree.append("tests/requirements-hermetic.lock", "pytest-rerunfailures==16.0 \\\n    --hash=sha256:00\n")


def _inject_flaky_marker(tree: Tree) -> None:
    tree.append(_first(tree, "tests/test_")["file"], f"\n@pytest.{FLAKY}({RERUNS})\ndef test_injected(): pass\n")


def _inject_retry_flag(tree: Tree) -> None:
    tree.append("scripts/ci-local", "\nnpx playwright test --retries=2\n")


def _inject_playwright_retries(tree: Tree) -> None:
    tree.edit("web/playwright.config.ts", "retries: 0,", "retries: 2,")


def _inject_missing_vitest_retry(tree: Tree) -> None:
    tree.edit("web/vitest.config.ts", "retry: 0,", "")


def _inject_spec_retries(tree: Tree) -> None:
    tree.append(_first(tree, "web/e2e/")["file"], "\ntest.describe.configure({ retries: 1 });\n")


def _inject_second_attempt(tree: Tree) -> None:
    tree.cases.append({**_first(tree, "web/e2e/"), "attempt": 1})


def _inject_unrun_test_file(tree: Tree) -> None:
    tree.append("tests/test_injected_orphan.py", "def test_never_run(): pass\n")


def _inject_stale_orphan(tree: Tree) -> None:
    tree.cases.append(_row(tree.orphans[0], f"{tree.orphans[0]} > now runs"))


def _inject_new_skip(tree: Tree) -> None:
    _first(tree, "tests/test_")["outcome"] = "skipped"


def _inject_new_xfail(tree: Tree) -> None:
    _first(tree, "tests/test_")["outcome"] = "xfailed"


def _inject_stale_skip(tree: Tree) -> None:
    next(row for row in tree.cases if row["outcome"] == "skipped")["outcome"] = "passed"


def _inject_quarantine_manifest(tree: Tree) -> None:
    tree.append("tests/quarantine.json", "[]\n")


def _inject_quarantine_marker(tree: Tree) -> None:
    tree.append(_first(tree, "tests/test_")["file"], f"\n@pytest.{QUARANTINE}\ndef test_injected(): pass\n")


def _inject_count_over_ceiling(tree: Tree) -> None:
    ceilings = tomllib.loads((tree.root / "tests/budgets.toml").read_text(encoding="utf-8"))["ceilings"]
    count = sum(1 for row in tree.cases if row["framework"] == "vitest")
    tree.edit("tests/budgets.toml", f"vitest = {ceilings['vitest']}", f"vitest = {count - 1}")


@pytest.mark.parametrize(("inject", "message"), [
    (_inject_rerun_plugin, "retries: rerun plugin pytest-rerunfailures"),
    (_inject_flaky_marker, f"uses {FLAKY}"),
    (_inject_retry_flag, "retries: scripts/ci-local passes --retries"),
    (_inject_playwright_retries, "retries: web/playwright.config.ts sets retries: 2"),
    (_inject_missing_vitest_retry, "web/vitest.config.ts must set retry: 0"),
    (_inject_spec_retries, "sets retries: 1"),
    (_inject_second_attempt, "ran attempt 1"),
    (_inject_unrun_test_file, "discovery: tests/test_injected_orphan.py ran in no gate step"),
    (_inject_stale_orphan, "now runs in the gate; remove its tests/suites.toml entry"),
    (_inject_new_skip, "was skipped but is not a [[skip]] entry"),
    (_inject_new_xfail, "was xfailed but is not a [[xfail]] entry"),
    (_inject_stale_skip, "was not skipped in this run"),
    (_inject_quarantine_manifest, "quarantine: manifest tests/quarantine.json exists"),
    (_inject_quarantine_marker, f"uses {QUARANTINE}"),
    (_inject_count_over_ceiling, "counts: vitest has"),
], ids=lambda value: value.__name__.removeprefix("_inject_") if callable(value) else None)
def test_injected_violation_fails(tree: Tree, inject, message: str) -> None:
    inject(tree)
    result = tree.run()
    print(result.stdout)  # shown with -rP: the receipt that the rule fired
    assert result.returncode == 1, result.stdout
    assert message in result.stdout, result.stdout
    assert "policy: FAILED" in result.stdout


def test_expired_orphan_fails(tree: Tree) -> None:
    suites = tomllib.loads((tree.root / "tests/suites.toml").read_text(encoding="utf-8"))
    expiry = min(entry["expires_on"] for entry in suites["orphan"])
    result = tree.run(today=expiry + datetime.timedelta(days=1))
    assert result.returncode == 1, result.stdout
    assert f"expired on {expiry}" in result.stdout
