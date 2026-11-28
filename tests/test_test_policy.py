"""scripts/ci/policy.py fails on each injected violation and passes the clean tree.

Each case copies the files the policy reads into a temporary root, writes a
cases.jsonl in which every non-orphan test file ran once and every allowed
skip was skipped, injects one violation and runs the policy as ci-local does.
The clean inventory reuses the policy's own file discovery, so these tests
prove each rule fires, not that discovery finds every test file; the gate's
real cases.jsonl does that.

The checkout is copied once per module and each tree hard-links it; an edit
replaces the link with its own copy first.  A policy run reads the whole tree
(about 2 s), so the failing cases do not each run it: every injection goes into
one tree, the policy runs once, and each case asserts its own finding in that
run.  An injection that could hide another's finding (the same file, the same
pattern) takes its own file or is counted exactly, and each finding names the
file or case it was injected into.  The run calls ``policy.main`` in this process
(real argparse, discovery, checkers and ``SystemExit``); the clean tree and the
receipt merger still run as the processes ci-local starts.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "ci" / "policy.py"
MERGE = ROOT / "scripts" / "ci" / "merge_cases.py"
_spec = importlib.util.spec_from_file_location("ci_policy", POLICY)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)

READ_FILES = ("tests/requirements-hermetic.lock", "tests/suites.toml", "tests/budgets.toml",
              *policy.RETRY_FLAG_FILES)
# The leak scan reads the whole tree: copy its allowlist and every file it excuses,
# so that the copied tree has no finding and no stale entry.
LEAK_FILES = (policy.public_leak_scan.ALLOWLIST,
              *sorted({path for path, _rule in policy.public_leak_scan.load_allowlist(ROOT)[0]}))
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


class Template:
    """The files the policy reads and the clean inventory, built once per module."""

    def __init__(self, root: Path) -> None:
        self.root = root
        files = policy.all_files(ROOT)
        for relative in {*policy.discover(files), *policy.test_code(files), *policy.browser_code(files), *READ_FILES,
                         *LEAK_FILES}:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        suites = tomllib.loads((root / "tests/suites.toml").read_text(encoding="utf-8"))
        self.orphans = [entry["path"] for entry in suites.get("orphan", [])]
        unrun = {entry["path"] for table in ("orphan", "scheduled", "live") for entry in suites.get(table, [])}
        self.cases = [_row(path, f"{path}::case") for path in policy.discover(files) if path not in unrun]
        for table, outcome in (("skip", "skipped"), ("xfail", "xfailed")):
            for entry in suites.get(table, []):
                # A pytest case is "file::name"; a browser case is "file > describe > title".
                separator = "::" if entry["framework"] == "pytest" else " > "
                self.cases.append(_row(entry["case"].split(separator, 1)[0], entry["case"], outcome))


class Tree:
    def __init__(self, root: Path, template: Template) -> None:
        self.root = root
        self.orphans = template.orphans
        self.cases = copy.deepcopy(template.cases)
        self.allowed_skips = [row for row in self.cases if row["outcome"] != "passed"]
        for directory, _subdirectories, names in os.walk(template.root):
            target = root / Path(directory).relative_to(template.root)
            target.mkdir(parents=True, exist_ok=True)
            for name in names:
                os.link(Path(directory, name), target / name)

    @staticmethod
    def _own(path: Path) -> None:
        """Replace a hard link by a private copy before the file changes."""
        if path.exists() and path.stat().st_nlink > 1:
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)

    def edit(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        self._own(path)
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {relative}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def append(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._own(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def run(self, today: datetime.date = TODAY, base_budgets: str | None = None,
            as_process: bool = False) -> subprocess.CompletedProcess[str]:
        cases = self.root / "cases.jsonl"
        cases.write_text("".join(json.dumps(row) + "\n" for row in self.cases), encoding="utf-8")
        extra: list[str] = []
        if base_budgets is not None:
            base = self.root / "base-budgets.toml"
            base.write_text(base_budgets, encoding="utf-8")
            extra = ["--base-budgets", str(base)]
        args = ["--root", str(self.root), "--cases", str(cases), "--today", today.isoformat(), *extra]
        if as_process:
            return subprocess.run([sys.executable, str(POLICY), *args], text=True, capture_output=True)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", [str(POLICY), *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                policy.main()
            except SystemExit as stop:
                code = stop.code
        return subprocess.CompletedProcess([str(POLICY), *args], code, out.getvalue(), err.getvalue())


@pytest.fixture(scope="module", autouse=True)
def _no_private_leak_patterns(tmp_path_factory: pytest.TempPathFactory):
    """The machine's private pattern file is not test input."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv(policy.public_leak_scan.PATTERNS_ENV, raising=False)
        yield


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Template:
    return Template(tmp_path_factory.mktemp("policy-template"))


def _ratchet_now(tree: Tree) -> int:
    return tomllib.loads((tree.root / "tests/budgets.toml").read_text(encoding="utf-8"))["ratchets"]["ad_hoc_upstream_fake_files"]


def _base(value: int | None) -> str:
    return "[ceilings]\npytest = 1\n" + ("" if value is None else f"[ratchets]\nad_hoc_upstream_fake_files = {value}\n")


def _list_injected(tree: Tree, table: str, name: str, extra: str = 'reason = "test"\n') -> str:
    path = f"tests/test_injected_{name}.py"
    tree.append(path, "def test_listed(): pass\n")
    tree.append("tests/suites.toml", f'\n[[{table}]]\npath = "{path}"\n{extra}')
    return path


def test_clean_tree_passes(tmp_path: Path, template: Template) -> None:
    """The copied tree passes as the process ci-local starts, with the target branch's ratchet equal to ours and
    with a scheduled file and a live file that no gate step runs (neither is an orphan)."""
    tree = Tree(tmp_path / "root", template)
    _list_injected(tree, "scheduled", "scheduled", 'task = "sdk-probes"\nreason = "test"\n')
    _list_injected(tree, "live", "live")
    result = tree.run(as_process=True, base_budgets=_base(_ratchet_now(tree)))
    assert result.returncode == 0, result.stdout
    assert "policy: ok:" in result.stdout


class Injections:
    """One tree that carries every violation at once; each injection returns the finding it must cause.

    A finding is a string, or a function of this object for a count that is known only after every injection.
    """

    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        self.used: set[str] = set()
        self.hygiene_lines: dict[str, int] = {}
        self.hygiene_before = policy.check_hygiene(
            tree.root, policy.all_files(tree.root), tomllib.loads((tree.root / "tests/budgets.toml").read_text()))[1]

    def row(self, prefix: str) -> dict:
        """A passed case in a file no earlier injection used, so no finding can come from another injection."""
        row = next(r for r in self.tree.cases if r["file"].startswith(prefix) and r["outcome"] == "passed"
                   and r["file"] not in self.used)
        self.used.add(row["file"])
        return row

    def file(self, prefix: str) -> str:
        return self.row(prefix)["file"]

    def hygiene(self, name: str, snippet: str, relative: str = "web/e2e/invite-signin.spec.ts") -> None:
        self.tree.append(relative, f"\n// injected\n{snippet}\n")

    def hygiene_finding(self, name: str):
        return lambda: f"hygiene: {name} occurs {self.hygiene_before[name] + self.hygiene_lines[name]} times"


def _inject_bare_test_import(x: Injections):
    x.tree.append("web/e2e/invite-signin.spec.ts", "\nimport { test as bare } from \"@playwright/test\";\n")
    return "fixture: web/e2e/invite-signin.spec.ts imports test from @playwright/test"


def _inject_rerun_plugin(x: Injections):
    x.tree.append("tests/requirements-hermetic.lock", "pytest-rerunfailures==16.0 \\\n    --hash=sha256:00\n")
    return "retries: rerun plugin pytest-rerunfailures"


def _inject_flaky_marker(x: Injections):
    file = x.file("tests/test_")
    x.tree.append(file, f"\n@pytest.{FLAKY}({RERUNS})\ndef test_injected(): pass\n")
    return f"retries: {file} uses {FLAKY}"


def _inject_flaky_decorator(x: Injections):
    file = x.file("tests/test_")
    x.tree.append(file, "\n@fla" "ky\ndef test_injected(): pass\n")
    return f"retries: {file} uses @fla" "ky"


def _inject_reruns_keyword(x: Injections):
    file = x.file("tests/test_")
    x.tree.append(file, f"\nSETTINGS = dict({RERUNS})\n")
    return f"retries: {file} uses " + RERUNS.split("=")[0] + "="


def _inject_retry_flag(x: Injections):
    x.tree.append("scripts/ci-local", "\nnpx playwright test --retries=2\n")
    return "retries: scripts/ci-local passes --retries"


def _inject_playwright_retries(x: Injections):
    x.tree.edit("web/playwright.config.ts", "retries: 0,", "retries: 2,")
    return "retries: web/playwright.config.ts sets retries: 2"


def _inject_missing_vitest_retry(x: Injections):
    x.tree.edit("web/vitest.config.ts", "retry: 0,", "")
    return "web/vitest.config.ts must set retry: 0"


def _inject_spec_retries(x: Injections):
    file = x.file("web/e2e/")
    x.tree.append(file, "\ntest.describe.configure({ retries: 1 });\n")
    return f"retries: {file} sets retries: 1"


def _inject_computed_vitest_retry(x: Injections):
    file = x.file("web/tests/")
    x.tree.append(file, '\nit("x", { retry: Number(2) }, () => {});\n')
    return f"retries: {file} sets retry: Number(2); only a literal 0 is allowed"


def _inject_computed_project_retries(x: Injections):
    x.tree.edit("web/playwright.config.ts", 'name: "chromium",', 'name: "chromium", retries: process.env.CI ? 2 : 0,')
    return "retries: web/playwright.config.ts sets retries: process.env.CI ? 2 : 0; only a literal 0"


def _inject_shorthand_retries(x: Injections):
    file = x.file("web/e2e/")
    x.tree.append(file, "\nconst retries = 2;\ntest.describe.configure({ retries });\n")
    return f"retries: {file} passes retries by shorthand"


def _inject_conftest_flaky_marker(x: Injections):
    x.tree.append("tests/conftest.py", f"\n# item.add_marker(pytest.{FLAKY}({RERUNS}))\n")
    return f"retries: tests/conftest.py uses {FLAKY}"


def _inject_conftest_quarantine_marker(x: Injections):
    x.tree.append("tests/conftest.py", f"\n# item.add_marker(pytest.{QUARANTINE})\n")
    return f"quarantine: tests/conftest.py uses {QUARANTINE}"


def _inject_second_attempt(x: Injections):
    row = x.row("web/e2e/")
    x.tree.cases.append({**row, "attempt": 1})
    return f"retries: playwright {row['case']} ran attempt 1"


def _inject_second_run_into_the_same_receipts(x: Injections):
    """A shell rerun (`vitest run || vitest run`) appends to the same receipts file: the merged cases show attempt 1."""
    file = x.file("web/tests/")
    receipts = x.tree.root / "vitest.cases.jsonl"
    receipts.write_text("".join(json.dumps({
        "framework": "vitest", "config": "vitest.config.ts", "project": "", "id": "x",
        "file": str(x.tree.root / file), "titles": ["t"], "duration_ms": 1, "status": status}) + "\n"
        for status in ("failed", "passed")))
    merged = x.tree.root / "merged.jsonl"
    subprocess.run([sys.executable, str(MERGE), "--sha", "0" * 40, "--seed", "1", "--out", str(merged),
                    "--receipts", str(receipts), str(x.tree.root)], check=True, capture_output=True)
    x.tree.cases = [row for row in x.tree.cases if row["file"] != file]
    x.tree.cases += [{k: v for k, v in json.loads(row).items() if k not in ("seed", "sha")}
                     for row in merged.read_text().splitlines()]
    return f"retries: vitest {file} > t ran attempt 1"


def _inject_unrun_test_file(x: Injections):
    x.tree.append("tests/test_injected_unrun.py", "def test_never_run(): pass\n")
    return "discovery: tests/test_injected_unrun.py ran in no gate step"


def _inject_stale_orphan(x: Injections):
    x.tree.append("tests/suites.toml", '\n[[orphan]]\npath = "tests/test_injected_stale.py"\n'
                  'expires_on = 2026-11-12\nreason = "test"\n')
    x.tree.append("tests/test_injected_stale.py", "def test_runs(): pass\n")
    x.tree.cases.append(_row("tests/test_injected_stale.py", "tests/test_injected_stale.py::test_runs"))
    return "discovery: orphan tests/test_injected_stale.py now runs in the gate; remove its tests/suites.toml entry"


def _inject_expired_orphan(x: Injections):
    expiry = TODAY - datetime.timedelta(days=1)
    x.tree.append("tests/suites.toml", f'\n[[orphan]]\npath = "tests/test_injected_expired.py"\n'
                  f'expires_on = {expiry}\nreason = "test"\n')
    x.tree.append("tests/test_injected_expired.py", "def test_never_run(): pass\n")
    return f"discovery: orphan tests/test_injected_expired.py expired on {expiry}"


def _inject_scheduled_file_that_ran_in_the_gate(x: Injections):
    path = _list_injected(x.tree, "scheduled", "ran", 'task = "sdk-probes"\nreason = "test"\n')
    x.tree.cases.append(_row(path, f"{path}::test_listed"))
    return f"discovery: scheduled {path} now runs in the gate"


def _inject_scheduled_unknown_task(x: Injections):
    path = _list_injected(x.tree, "scheduled", "task", 'task = "nobody-runs-this"\nreason = "test"\n')
    return f"discovery: scheduled {path} has task 'nobody-runs-this'"


def _inject_scheduled_without_reason(x: Injections):
    path = _list_injected(x.tree, "scheduled", "reasonless", 'task = "sweep"\n')
    return f"discovery: scheduled {path} needs a reason"


def _inject_live_file_that_no_longer_exists(x: Injections):
    x.tree.append("tests/suites.toml", '\n[[live]]\npath = "tests/test_deleted_live.py"\nreason = "test"\n')
    return "discovery: live tests/test_deleted_live.py does not exist"


def _inject_file_in_two_tiers(x: Injections):
    path = _list_injected(x.tree, "live", "two_tiers")
    x.tree.append("tests/suites.toml", f'\n[[scheduled]]\npath = "{path}"\ntask = "sweep"\nreason = "test"\n')
    return f"discovery: {path} is listed in [[scheduled]] and [[live]]"


def _inject_new_skip(x: Injections):
    row = x.row("tests/test_")
    row["outcome"] = "skipped"
    return f"skips: {row['framework']} {row['case']} was skipped but is not a [[skip]] entry"


def _inject_new_xfail(x: Injections):
    row = x.row("tests/test_")
    row["outcome"] = "xfailed"
    return f"skips: {row['framework']} {row['case']} was xfailed but is not a [[xfail]] entry"


def _inject_stale_skip(x: Injections):
    row = x.tree.allowed_skips[0]
    row["outcome"] = "passed"
    return f"{row['case']} was not skipped in this run; remove the entry"


def _inject_skip_allowance_is_per_project(x: Injections):
    case = "web/e2e/activity.spec.ts > plays audio"
    x.tree.append("tests/suites.toml", f'\n[[skip]]\nframework = "playwright"\nconfig = "playwright.config.ts"\n'
                  f'project = "chromium"\ncase = "{case}"\nreason = "test"\n')
    x.tree.cases.append({**_row("web/e2e/activity.spec.ts", case, "skipped"), "project": "firefox"})
    return [f"skips: playwright playwright.config.ts firefox {case} was skipped but is not",
            f"skips: [[skip]] playwright playwright.config.ts chromium {case} was not skipped"]


def _inject_quarantine_manifest(x: Injections):
    x.tree.append("tests/quarantine.json", "[]\n")
    return "quarantine: manifest tests/quarantine.json exists"


def _inject_quarantine_marker(x: Injections):
    file = x.file("tests/test_")
    x.tree.append(file, f"\n@pytest.{QUARANTINE}\ndef test_injected(): pass\n")
    return f"quarantine: {file} uses {QUARANTINE}"


def _inject_count_over_ceiling(x: Injections):
    ceilings = tomllib.loads((x.tree.root / "tests/budgets.toml").read_text(encoding="utf-8"))["ceilings"]
    count = sum(1 for row in x.tree.cases if row["framework"] == "vitest" and row["attempt"] == 0)
    x.tree.edit("tests/budgets.toml", f"vitest = {ceilings['vitest']}", f"vitest = {count - 1}")
    return f"counts: vitest has {count} cases, over its ceiling {count - 1}"


def _inject_a_ratchet_above_the_target_branch(x: Injections):
    """The ad-hoc fake count rose: the ceiling was raised to hide new fakes (the run's target branch has one less)."""
    now = _ratchet_now(x.tree)
    return f"ratchets: ad_hoc_upstream_fake_files is {now}, above {now - 1} on the target branch"


# Each pattern is injected in every spelling it must catch; the finding counts them all, so a spelling that stops
# matching changes the count instead of hiding behind the other.  The count of a pattern is written out: the two
# one-shot expect lines also read as bare isVisible calls, so that pattern sees four lines.
HYGIENE_INJECTIONS = {
    "waitForTimeout": (2, ["await page.waitForTimeout(500);", "await page.waitForTimeout (500);"]),
    "networkidle": (2, ["await page.waitForLoadState('networkidle');", "await page.waitForLoadState ( 'networkidle' );"]),
    "force_true": (1, ["await button.click({ force: true });"]),
    "one_shot_expect": (2, ["expect(await page.getByRole('button').isVisible()).toBe(true);",
                            "expect (await page.locator('a')?.isVisible ()).toBe(true);"]),
    "bare_isVisible": (4, ["if (await banner.isVisible()) return;", "if (await banner?.isVisible ()) return;"]),
    "absolute_home_path": (1, ["await page.screenshot({ path: '/home/someone/.tmp/shot.png' });"]),
    "raw_fulfil": (1, ["await route.fulfill({ json: { records: [] } });"]),
    "malformed_stub": (1, ["await fulfilJson(route, {}, { malformed: 'because' });"]),
}


def _inject_hygiene(name: str):
    def inject(x: Injections):
        extra, snippets = HYGIENE_INJECTIONS[name]
        for snippet in snippets:
            x.hygiene(name, snippet)
        x.hygiene_lines[name] = extra
        return x.hygiene_finding(name)
    return inject


def _inject_unprobed_endpoint(x: Injections):
    x.hygiene("unprobed_endpoint", 'unprobed("POST", "/injected", "no oracle");', "web/e2e/fixtures/intercept-registry.ts")
    x.hygiene_lines["unprobed_endpoint"] = 1
    return x.hygiene_finding("unprobed_endpoint")


INJECTIONS = {
    function.__name__.removeprefix("_inject_"): function for function in (
        _inject_bare_test_import, _inject_rerun_plugin, _inject_flaky_marker, _inject_flaky_decorator,
        _inject_reruns_keyword, _inject_retry_flag, _inject_playwright_retries, _inject_missing_vitest_retry,
        _inject_spec_retries, _inject_computed_vitest_retry, _inject_computed_project_retries,
        _inject_shorthand_retries, _inject_conftest_flaky_marker, _inject_conftest_quarantine_marker,
        _inject_second_attempt, _inject_second_run_into_the_same_receipts, _inject_unrun_test_file,
        _inject_stale_orphan, _inject_expired_orphan, _inject_scheduled_file_that_ran_in_the_gate,
        _inject_scheduled_unknown_task, _inject_scheduled_without_reason, _inject_live_file_that_no_longer_exists,
        _inject_file_in_two_tiers, _inject_new_skip, _inject_new_xfail, _inject_stale_skip,
        _inject_skip_allowance_is_per_project, _inject_quarantine_manifest, _inject_quarantine_marker,
        _inject_count_over_ceiling, _inject_a_ratchet_above_the_target_branch, _inject_unprobed_endpoint)
} | {f"hygiene_{name}": _inject_hygiene(name) for name in HYGIENE_INJECTIONS}


@pytest.fixture(scope="module")
def violations(tmp_path_factory: pytest.TempPathFactory, template: Template):
    """The policy's one run over a tree that carries every injection: (result, findings by injection)."""
    tree = Tree(tmp_path_factory.mktemp("policy-violations") / "root", template)
    injections = Injections(tree)
    findings = {}
    for name, inject in INJECTIONS.items():
        found = inject(injections)
        findings[name] = found if isinstance(found, list) else [found]
    now = _ratchet_now(tree)
    result = tree.run(base_budgets=_base(now - 1))
    print(result.stdout)  # shown with -rP: the receipt that every rule fired
    return result, {name: [m() if callable(m) else m for m in messages] for name, messages in findings.items()}


def test_injected_violations_fail_the_policy(violations) -> None:
    result, _findings = violations
    assert result.returncode == 1, result.stdout
    assert "policy: FAILED" in result.stdout


@pytest.mark.parametrize("name", list(INJECTIONS))
def test_injected_violation_fails(violations, name: str) -> None:
    result, findings = violations
    for message in findings[name]:
        assert message in result.stdout, result.stdout


def _ratchets(budgets: dict, base: str) -> list[str]:
    return policy.check_ratchets(budgets, tomllib.loads(base))


@pytest.fixture(scope="module")
def budgets(template: Template) -> dict:
    return tomllib.loads((template.root / "tests/budgets.toml").read_text(encoding="utf-8"))


def test_a_removed_ratchet_fails(budgets: dict) -> None:
    renamed = {**budgets, "ratchets": {"renamed_ratchet": 1}}
    assert "ratchets: ad_hoc_upstream_fake_files was removed from tests/budgets.toml" in "\n".join(_ratchets(renamed, _base(5)))


@pytest.mark.parametrize("delta", [0, 1], ids=["equal", "fell"])
def test_a_ratchet_at_or_below_the_target_branch_passes(budgets: dict, delta: int) -> None:
    assert _ratchets(budgets, _base(budgets["ratchets"]["ad_hoc_upstream_fake_files"] + delta)) == []


def test_a_target_branch_without_ratchets_imposes_none(budgets: dict) -> None:
    assert _ratchets(budgets, _base(None)) == []
