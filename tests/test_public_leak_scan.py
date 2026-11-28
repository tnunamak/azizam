"""scripts/ci/public_leak_scan.py fails on each planted leak and passes a clean tree.

Each case builds a small git checkout in a temporary directory, plants one
leak in a tracked file (its text or its name) and runs the scan as policy.py
does. The values are built from parts so that this file does not hold them.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scripts" / "ci" / "public_leak_scan.py"
_spec = importlib.util.spec_from_file_location("ci_public_leak_scan", SCAN)
scan = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scan  # dataclasses look the module up while the class is built
_spec.loader.exec_module(scan)

LAN = "192.168." "7.23"
CGNAT = "100.64." "3.9"
HOME = "/" "home/" "someone/notes"
MAC_HOME = "/" "Users/" "someone/notes"
EMAIL = "pat" "@" "mail.invalid-domain.org"
CLEAN = (
    "# A clean project\n"
    "Point it at http://192.0.2.10:8080 or a LAN range such as 10.0.0.0/8.\n"
    "Contact: maintainers@example.com, ops@family.example, me@box.test.\n"
    "Logos: icon@2x.png. Units: user@1000.service. Paths: ~/projects, ./data.\n"
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text(CLEAN, encoding="utf-8")
    _git(root, "add", "-A")
    return root


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")


def _check(root: Path, patterns: Path | None = None) -> tuple[list[str], str]:
    return scan.check_root(root, patterns_path=patterns or root.parent / "absent.txt")


def test_a_clean_tree_passes(repo: Path) -> None:
    problems, summary = _check(repo)
    assert problems == []
    assert summary == "private patterns: not configured"


@pytest.mark.parametrize("text, rule", [
    (f"backend = http://{LAN}:8080\n", "lan-address"),
    (f"peer {CGNAT}\n", "lan-address"),
    (f"see {HOME}/plan.md\n", "home-path"),
    (f"see {MAC_HOME}/plan.md\n", "home-path"),
    (f"mail {EMAIL}\n", "email"),
], ids=["rfc1918", "cgnat", "linux-home", "mac-home", "email"])
def test_a_planted_leak_fails_with_file_line_and_rule(repo: Path, text: str, rule: str) -> None:
    _write(repo, "docs/guide.md", "Intro.\n" + text)
    problems, _summary = _check(repo)
    assert problems == [f"leaks: docs/guide.md:2: {rule}"]


def test_a_finding_never_prints_the_value(repo: Path) -> None:
    _write(repo, "notes.txt", f"{LAN} {HOME}/x {EMAIL}\n")
    problems, _summary = _check(repo)
    assert len(problems) == 3
    assert not any(value in problem for problem in problems for value in (LAN, HOME, EMAIL))


def test_a_leak_in_a_file_name_fails(repo: Path) -> None:
    _write(repo, f"captures/{LAN}.txt", "nothing here\n")
    problems, _summary = _check(repo)
    assert problems == [f"leaks: captures/{LAN}.txt (file name): lan-address"]


def test_an_untracked_file_is_not_scanned(repo: Path) -> None:
    (repo / "scratch.txt").write_text(f"{LAN}\n", encoding="utf-8")
    problems, _summary = _check(repo)
    assert problems == []


def test_a_binary_file_is_scanned_by_name_only(repo: Path) -> None:
    (repo / "image.png").write_bytes(b"\x89PNG\0\0" + LAN.encode())
    _git(repo, "add", "-A")
    problems, _summary = _check(repo)
    assert problems == []


def test_the_allowlist_excuses_one_file_and_rule(repo: Path) -> None:
    _write(repo, "tests/test_lan.py", f"ADDRESS = '{LAN}'\n")
    _write(repo, "tests/test_other.py", f"ADDRESS = '{LAN}'\nMAIL = '{EMAIL}'\n")
    _write(repo, scan.ALLOWLIST, "# comment\ntests/test_lan.py\tlan-address\tthe test needs a LAN peer\n"
           "tests/test_other.py\tlan-address\tthe test needs a LAN peer\n")
    problems, _summary = _check(repo)
    assert problems == ["leaks: tests/test_other.py:2: email"]


def test_a_stale_allowlist_entry_fails(repo: Path) -> None:
    _write(repo, scan.ALLOWLIST, "README.md\thome-path\tno longer needed\n")
    problems, _summary = _check(repo)
    assert problems == [f"leaks: {scan.ALLOWLIST} excuses README.md home-path, which has no such finding; "
                        "remove the entry"]


@pytest.mark.parametrize("line, message", [
    ("README.md\tlan-address\n", "needs path<TAB>rule<TAB>reason"),
    ("README.md\tsecrets\tbecause\n", "names unknown rule 'secrets'"),
])
def test_a_malformed_allowlist_line_fails(repo: Path, line: str, message: str) -> None:
    _write(repo, scan.ALLOWLIST, line)
    problems, _summary = _check(repo)
    assert any(message in problem for problem in problems), problems


def test_a_private_pattern_fails_in_text_and_name_without_an_allowlist(repo: Path, tmp_path: Path) -> None:
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("# private values\n\n(?i)\\bzanzibar-host\\b\n", encoding="utf-8")
    _write(repo, "docs/ops.md", "one\ntwo\nrestart Zanzibar-Host now\n")
    _write(repo, "zanzibar-host.cfg", "x\n")
    _write(repo, scan.ALLOWLIST, "docs/ops.md\tlan-address\ttries to excuse it\n")
    problems, summary = _check(repo, patterns)
    assert "leaks: docs/ops.md:3: private #3" in problems
    assert "leaks: zanzibar-host.cfg (file name): private #3" in problems
    assert summary == "private patterns: 1 checked"


def test_a_private_pattern_that_does_not_compile_fails(repo: Path, tmp_path: Path) -> None:
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("(unclosed\n", encoding="utf-8")
    problems, summary = _check(repo, patterns)
    assert problems == [problems[0]] and problems[0].startswith("leaks: private pattern on line 1 does not compile")
    assert summary == "private patterns: 0 checked"


def test_the_pattern_file_comes_from_the_environment(repo: Path, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("clean project\n", encoding="utf-8")
    monkeypatch.setenv(scan.PATTERNS_ENV, str(patterns))
    problems, summary = scan.check_root(repo)
    assert problems == ["leaks: README.md:1: private #1"]
    assert summary == "private patterns: 1 checked"


def test_without_the_environment_variable_no_pattern_file_is_read(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(scan.PATTERNS_ENV, raising=False)
    problems, summary = scan.check_root(repo)
    assert problems == []
    assert summary == "private patterns: not configured"


def test_a_root_that_is_not_a_git_checkout_is_walked(tmp_path: Path) -> None:
    root = tmp_path / "copy"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "x.js").write_text(f"'{LAN}'\n", encoding="utf-8")
    (root / "a.md").write_text(f"{HOME}/x\n", encoding="utf-8")
    problems, _summary = _check(root)
    assert problems == ["leaks: a.md:1: home-path"]


def test_the_command_line_reports_and_exits(repo: Path) -> None:
    clean = subprocess.run([sys.executable, str(SCAN), "--root", str(repo), "--patterns", str(repo / "absent")],
                           text=True, capture_output=True)
    assert clean.returncode == 0, clean.stdout
    assert clean.stdout.strip() == "leaks: ok: 0 findings; private patterns: not configured"
    _write(repo, "x.md", f"{EMAIL}\n")
    leaky = subprocess.run([sys.executable, str(SCAN), "--root", str(repo), "--patterns", str(repo / "absent")],
                           text=True, capture_output=True)
    assert leaky.returncode == 1
    assert leaky.stdout.splitlines() == ["leaks: x.md:1: email",
                                         "leaks: FAILED: 1 findings; private patterns: not configured"]
