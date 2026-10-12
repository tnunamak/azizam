"""scripts/ci/policy.py fails on each injected violation and passes the clean tree.

Each case copies the files the policy reads into a temporary root, writes a
cases.jsonl in which every non-orphan test file ran once and every allowed
skip was skipped, injects one violation and runs the policy as ci-local does.
The clean inventory reuses the policy's own file discovery, so these tests
prove each rule fires, not that discovery finds every test file; the gate's
real cases.jsonl does that.
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
MERGE = ROOT / "scripts" / "ci" / "merge_cases.py"
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
        for relative in {*policy.discover(files), *policy.test_code(files), *policy.browser_code(files), *READ_FILES}:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        suites = tomllib.loads((root / "tests/suites.toml").read_text(encoding="utf-8"))
        self.orphans = [entry["path"] for entry in suites.get("orphan", [])]
        unrun = {entry["path"] for table in ("orphan", "scheduled", "live") for entry in suites.get(table, [])}
        self.cases = [_row(path, f"{path}::case") for path in policy.discover(files) if path not in unrun]
        for table, outcome in (("skip", "skipped"), ("xfail", "xfailed")):
            for entry in suites.get(table, []):
                assert entry["framework"] == "pytest", "extend _row for browser entries"
                self.cases.append(_row(entry["case"].split("::", 1)[0], entry["case"], outcome))

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

    def run(self, today: datetime.date = TODAY, base_budgets: str | None = None) -> subprocess.CompletedProcess[str]:
        cases = self.root / "cases.jsonl"
        cases.write_text("".join(json.dumps(row) + "\n" for row in self.cases), encoding="utf-8")
        extra: list[str] = []
        if base_budgets is not None:
            base = self.root / "base-budgets.toml"
            base.write_text(base_budgets, encoding="utf-8")
            extra = ["--base-budgets", str(base)]
        return subprocess.run([sys.executable, str(POLICY), "--root", str(self.root), "--cases", str(cases),
                               "--today", today.isoformat(), *extra], text=True, capture_output=True)


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


def _inject_computed_vitest_retry(tree: Tree) -> None:
    tree.append(_first(tree, "web/tests/")["file"], '\nit("x", { retry: Number(2) }, () => {});\n')


def _inject_computed_project_retries(tree: Tree) -> None:
    tree.edit("web/playwright.config.ts", 'name: "chromium",', 'name: "chromium", retries: process.env.CI ? 2 : 0,')


def _inject_shorthand_retries(tree: Tree) -> None:
    tree.append(_first(tree, "web/e2e/")["file"], "\nconst retries = 2;\ntest.describe.configure({ retries });\n")


def _inject_conftest_flaky_marker(tree: Tree) -> None:
    tree.append("tests/conftest.py", f"\n# item.add_marker(pytest.{FLAKY}({RERUNS}))\n")


def _inject_conftest_quarantine_marker(tree: Tree) -> None:
    tree.append("tests/conftest.py", f"\n# item.add_marker(pytest.{QUARANTINE})\n")


def _inject_second_attempt(tree: Tree) -> None:
    tree.cases.append({**_first(tree, "web/e2e/"), "attempt": 1})


def _inject_unrun_test_file(tree: Tree) -> None:
    tree.append("tests/test_injected_orphan.py", "def test_never_run(): pass\n")


def _inject_stale_orphan(tree: Tree) -> None:
    tree.append("tests/suites.toml", '\n[[orphan]]\npath = "tests/test_injected_orphan.py"\n'
                'expires_on = 2026-11-12\nreason = "test"\n')
    tree.append("tests/test_injected_orphan.py", "def test_runs(): pass\n")
    tree.cases.append(_row("tests/test_injected_orphan.py", "tests/test_injected_orphan.py::test_runs"))


def _list_injected(tree: Tree, table: str, extra: str = 'reason = "test"\n') -> None:
    tree.append("tests/test_injected_listed.py", "def test_listed(): pass\n")
    tree.append("tests/suites.toml", f'\n[[{table}]]\npath = "tests/test_injected_listed.py"\n{extra}')


def _inject_scheduled_file_that_ran_in_the_gate(tree: Tree) -> None:
    _list_injected(tree, "scheduled", 'task = "sdk-probes"\nreason = "test"\n')
    tree.cases.append(_row("tests/test_injected_listed.py", "tests/test_injected_listed.py::test_listed"))


def _inject_scheduled_unknown_task(tree: Tree) -> None:
    _list_injected(tree, "scheduled", 'task = "nobody-runs-this"\nreason = "test"\n')


def _inject_scheduled_without_reason(tree: Tree) -> None:
    _list_injected(tree, "scheduled", 'task = "sweep"\n')


def _inject_live_file_that_no_longer_exists(tree: Tree) -> None:
    tree.append("tests/suites.toml", '\n[[live]]\npath = "tests/test_deleted_live.py"\nreason = "test"\n')


def _inject_file_in_two_tiers(tree: Tree) -> None:
    _list_injected(tree, "live")
    tree.append("tests/suites.toml", '\n[[scheduled]]\npath = "tests/test_injected_listed.py"\n'
                'task = "sweep"\nreason = "test"\n')


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


def _inject_hygiene(snippet: str):
    def inject(tree: Tree) -> None:
        tree.append("web/e2e/invite-signin.spec.ts", f"\n// injected\n{snippet}\n")
    inject.__name__ = "_inject_hygiene_" + snippet.split("(")[0].split(".")[-1].replace(" ", "_")
    return inject


def _inject_bare_test_import(tree: Tree) -> None:
    tree.append("web/e2e/invite-signin.spec.ts", "\nimport { test as bare } from \"@playwright/test\";\n")


@pytest.mark.parametrize(("inject", "message"), [
    (_inject_bare_test_import, "fixture: web/e2e/invite-signin.spec.ts imports test from @playwright/test"),
    (_inject_rerun_plugin, "retries: rerun plugin pytest-rerunfailures"),
    (_inject_flaky_marker, f"uses {FLAKY}"),
    (_inject_retry_flag, "retries: scripts/ci-local passes --retries"),
    (_inject_playwright_retries, "retries: web/playwright.config.ts sets retries: 2"),
    (_inject_missing_vitest_retry, "web/vitest.config.ts must set retry: 0"),
    (_inject_spec_retries, "sets retries: 1"),
    (_inject_computed_vitest_retry, "sets retry: Number(2); only a literal 0 is allowed"),
    (_inject_computed_project_retries, "sets retries: process.env.CI ? 2 : 0; only a literal 0"),
    (_inject_shorthand_retries, "passes retries by shorthand"),
    (_inject_conftest_flaky_marker, f"retries: tests/conftest.py uses {FLAKY}"),
    (_inject_conftest_quarantine_marker, f"quarantine: tests/conftest.py uses {QUARANTINE}"),
    (_inject_second_attempt, "ran attempt 1"),
    (_inject_unrun_test_file, "discovery: tests/test_injected_orphan.py ran in no gate step"),
    (_inject_stale_orphan, "now runs in the gate; remove its tests/suites.toml entry"),
    (_inject_scheduled_file_that_ran_in_the_gate, "scheduled tests/test_injected_listed.py now runs in the gate"),
    (_inject_scheduled_unknown_task, "scheduled tests/test_injected_listed.py has task 'nobody-runs-this'"),
    (_inject_scheduled_without_reason, "scheduled tests/test_injected_listed.py needs a reason"),
    (_inject_live_file_that_no_longer_exists, "live tests/test_deleted_live.py does not exist"),
    (_inject_file_in_two_tiers, "tests/test_injected_listed.py is listed in [[scheduled]] and [[live]]"),
    (_inject_new_skip, "was skipped but is not a [[skip]] entry"),
    (_inject_new_xfail, "was xfailed but is not a [[xfail]] entry"),
    (_inject_stale_skip, "was not skipped in this run"),
    (_inject_quarantine_manifest, "quarantine: manifest tests/quarantine.json exists"),
    (_inject_quarantine_marker, f"uses {QUARANTINE}"),
    (_inject_count_over_ceiling, "counts: vitest has"),
    (_inject_hygiene("await page.waitForTimeout(500);"), "hygiene: waitForTimeout occurs"),
    (_inject_hygiene("await page.waitForLoadState('networkidle');"), "hygiene: networkidle occurs"),
    (_inject_hygiene("await button.click({ force: true });"), "hygiene: force_true occurs"),
    (_inject_hygiene("expect(await page.getByRole('button').isVisible()).toBe(true);"), "hygiene: one_shot_expect occurs"),
    (_inject_hygiene("if (await banner.isVisible()) return;"), "hygiene: bare_isVisible occurs"),
    (_inject_hygiene("await page.screenshot({ path: '/home/someone/.tmp/shot.png' });"), "hygiene: absolute_home_path occurs"),
    (_inject_hygiene("await route.fulfill({ json: { records: [] } });"), "hygiene: raw_fulfil occurs"),
    (_inject_hygiene("await fulfilJson(route, {}, { malformed: 'because' });"), "hygiene: malformed_stub occurs"),
    (_inject_hygiene("await page.waitForTimeout (500);"), "hygiene: waitForTimeout occurs"),
    (_inject_hygiene("await page.waitForLoadState ( 'networkidle' );"), "hygiene: networkidle occurs"),
    (_inject_hygiene("expect (await page.locator('a')?.isVisible ()).toBe(true);"), "hygiene: one_shot_expect occurs"),
    (_inject_hygiene("if (await banner?.isVisible ()) return;"), "hygiene: bare_isVisible occurs"),
], ids=lambda value: value.__name__.removeprefix("_inject_") if callable(value) else None)
def test_injected_violation_fails(tree: Tree, inject, message: str) -> None:
    inject(tree)
    result = tree.run()
    print(result.stdout)  # shown with -rP: the receipt that the rule fired
    assert result.returncode == 1, result.stdout
    assert message in result.stdout, result.stdout
    assert "policy: FAILED" in result.stdout


def test_scheduled_and_live_files_need_no_gate_run(tree: Tree) -> None:
    """A file that scripts/test-scheduled runs, or a live file, is not an orphan."""
    _list_injected(tree, "scheduled", 'task = "sdk-probes"\nreason = "test"\n')
    tree.append("tests/test_injected_live.py", "def test_live(): pass\n")
    tree.append("tests/suites.toml", '\n[[live]]\npath = "tests/test_injected_live.py"\nreason = "test"\n')
    result = tree.run()
    assert result.returncode == 0, result.stdout


def test_a_test_file_in_no_tier_fails(tree: Tree) -> None:
    """The tree has no [[orphan]] left: a new test file that no tier runs fails the policy."""
    tree.append("tests/test_injected_unlisted.py", "def test_nobody_runs_me(): pass\n")
    result = tree.run()
    assert result.returncode == 1, result.stdout
    assert "discovery: tests/test_injected_unlisted.py ran in no gate step" in result.stdout


def test_expired_orphan_fails(tree: Tree) -> None:
    tree.append("tests/suites.toml", '\n[[orphan]]\npath = "tests/test_injected_orphan.py"\n'
                'expires_on = 2026-11-12\nreason = "test"\n')
    tree.append("tests/test_injected_orphan.py", "def test_never_run(): pass\n")
    suites = tomllib.loads((tree.root / "tests/suites.toml").read_text(encoding="utf-8"))
    expiry = min(entry["expires_on"] for entry in suites["orphan"])
    result = tree.run(today=expiry + datetime.timedelta(days=1))
    assert result.returncode == 1, result.stdout
    assert f"expired on {expiry}" in result.stdout


def test_skip_allowance_is_per_project(tree: Tree) -> None:
    case = "web/e2e/activity.spec.ts > plays audio"
    tree.append("tests/suites.toml", f'\n[[skip]]\nframework = "playwright"\nconfig = "playwright.config.ts"\n'
                f'project = "chromium"\ncase = "{case}"\nreason = "test"\n')
    tree.cases.append({**_row("web/e2e/activity.spec.ts", case, "skipped"), "project": "firefox"})
    result = tree.run()
    assert result.returncode == 1, result.stdout
    assert f"skips: playwright playwright.config.ts firefox {case} was skipped but is not" in result.stdout
    assert f"skips: [[skip]] playwright playwright.config.ts chromium {case} was not skipped" in result.stdout


def test_a_second_run_into_the_same_receipts_fails(tree: Tree) -> None:
    """A shell rerun (`vitest run || vitest run`) appends to the same receipts
    file: the merged cases show attempt 1 and the policy stays red."""
    file = _first(tree, "web/tests/")["file"]
    receipts = tree.root / "vitest.cases.jsonl"
    receipts.write_text("".join(json.dumps({
        "framework": "vitest", "config": "vitest.config.ts", "project": "", "id": "x",
        "file": str(tree.root / file), "titles": ["t"], "duration_ms": 1, "status": status}) + "\n"
        for status in ("failed", "passed")))
    merged = tree.root / "merged.jsonl"
    subprocess.run([sys.executable, str(MERGE), "--sha", "0" * 40, "--seed", "1", "--out", str(merged),
                    "--receipts", str(receipts), str(tree.root)], check=True, capture_output=True)
    tree.cases = [row for row in tree.cases if row["file"] != file]
    tree.cases += [{k: v for k, v in json.loads(row).items() if k not in ("seed", "sha")}
                   for row in merged.read_text().splitlines()]
    result = tree.run()
    assert result.returncode == 1, result.stdout
    assert f"retries: vitest {file} > t ran attempt 1" in result.stdout


def _ratchet_now(tree: Tree) -> int:
    return tomllib.loads((tree.root / "tests/budgets.toml").read_text(encoding="utf-8"))["ratchets"]["ad_hoc_upstream_fake_files"]


def _base(value: int | None) -> str:
    return "[ceilings]\npytest = 1\n" + ("" if value is None else f"[ratchets]\nad_hoc_upstream_fake_files = {value}\n")


def test_a_ratchet_above_the_target_branch_fails(tree: Tree) -> None:
    """The ad-hoc fake count rose: the ceiling was raised to hide new fakes."""
    result = tree.run(base_budgets=_base(_ratchet_now(tree) - 1))
    print(result.stdout)
    assert result.returncode == 1, result.stdout
    assert f"ratchets: ad_hoc_upstream_fake_files is {_ratchet_now(tree)}, above {_ratchet_now(tree) - 1} on the target branch" in result.stdout


def test_a_removed_ratchet_fails(tree: Tree) -> None:
    tree.edit("tests/budgets.toml", "ad_hoc_upstream_fake_files", "renamed_ratchet")
    result = tree.run(base_budgets=_base(5))
    assert result.returncode == 1, result.stdout
    assert "ratchets: ad_hoc_upstream_fake_files was removed from tests/budgets.toml" in result.stdout


@pytest.mark.parametrize("delta", [0, 1], ids=["equal", "fell"])
def test_a_ratchet_at_or_below_the_target_branch_passes(tree: Tree, delta: int) -> None:
    result = tree.run(base_budgets=_base(_ratchet_now(tree) + delta))
    assert result.returncode == 0, result.stdout


def test_a_target_branch_without_ratchets_imposes_none(tree: Tree) -> None:
    assert tree.run(base_budgets=_base(None)).returncode == 0
