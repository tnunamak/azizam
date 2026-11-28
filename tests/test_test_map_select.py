"""scripts/ci/testmap_select.py: choose the test files a diff can reach, from a recorded test map.

The map (written by scripts/ci/testmap_record.py merge) says, per test file, which functions of which
repository files ran, which files it opened, which directories it listed and whether it started a
subprocess. The selector reads the diff from the map's base commit to HEAD, turns each changed Python
line into the function that holds it (or "<module>" for module and class level lines, decorators and
def lines), and selects every test file that ran a changed function. A module-level change selects every
test file that ran anything in that file. It falls back to the whole suite when the change can reach
work the map cannot attribute to one test file.

Each test builds a throwaway git repository with two commits and a hand-written map.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "scripts" / "ci" / "testmap_select.py"

BASE_FILES = {
    "pkg/__init__.py": "",
    "pkg/alpha.py": """
        LIMIT = 3


        def used():
            return helper() + LIMIT


        def helper():
            return 1


        class Box:
            def size(self):
                return 4

            def other(self):
                return 5


        def outer():
            def inner():
                return 6
            return inner()
    """,
    "pkg/beta.py": """
        def read_rules():
            return 1
    """,
    "pkg/gamma.py": """
        def start_up():
            return 7
    """,
    "rules/a.yml": "a: 1\n",
    "docs/notes.md": "notes\n",
    "tests/conftest.py": "",
    "tests/test_one.py": "def test_one():\n    pass\n",
    "tests/test_two.py": "def test_two():\n    pass\n",
    "tests/test_three.py": "def test_three():\n    pass\n",
    "tests/test_four.py": "def test_four():\n    pass\n",
}


def entry(functions=None, opened=(), listed=(), subprocess_=False):
    return {"functions": functions or {}, "opened": list(opened), "listed": list(listed), "subprocess": subprocess_}


MAP_TESTS = {
    "tests/test_one.py": entry({"pkg/alpha.py": ["<module>", "used", "helper", "Box.size"]}),
    "tests/test_two.py": entry({"pkg/alpha.py": ["outer", "outer.<locals>.inner"], "pkg/beta.py": ["<module>", "read_rules"]},
                               opened=["rules/a.yml"], listed=["rules"]),
    "tests/test_three.py": entry(subprocess_=True),
    "tests/test_four.py": entry({"pkg/beta.py": ["read_rules"]}),
}
MAP_SESSION = entry({"pkg/gamma.py": ["<module>", "start_up"]})


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


def write(repo: Path, files: Mapping[str, str | None]) -> None:
    for name, body in files.items():
        path = repo / name
        if body is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"))


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    write(repo, BASE_FILES)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def select(repo: Path, change: Mapping[str, str | None], *, tests=None, session=None, extra=()) -> tuple[str, list[str]]:
    """Commit `change` on top of the base, run the selector, return (mode, lines)."""
    base = git(repo, "rev-parse", "HEAD")
    write(repo, change)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "change")
    map_path = repo.parent / "map.json"
    map_path.write_text(json.dumps({"version": 1, "base": base, "tests": MAP_TESTS if tests is None else tests,
                                    "session": MAP_SESSION if session is None else session}))
    result = subprocess.run([sys.executable, str(SELECTOR), "--map", str(map_path), "--repo", str(repo), *extra],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    if lines == ["ALL"]:
        return "all", []
    return "some", sorted(lines)


def alpha_with(old: str, new: str) -> dict[str, str]:
    body = textwrap.dedent(BASE_FILES["pkg/alpha.py"]).lstrip("\n")
    assert old in body
    return {"pkg/alpha.py": body.replace(old, new)}


def test_a_function_body_change_selects_only_the_files_that_ran_it(repo):
    assert select(repo, alpha_with("return 1\n", "return 2\n")) == ("some", ["tests/test_one.py", "tests/test_three.py"])


def test_a_method_body_change_matches_the_class_qualified_name(repo):
    assert select(repo, alpha_with("return 4\n", "return 40\n")) == ("some", ["tests/test_one.py", "tests/test_three.py"])


def test_a_change_no_test_ran_selects_only_the_subprocess_tests(repo):
    assert select(repo, alpha_with("return 5\n", "return 50\n")) == ("some", ["tests/test_three.py"])


def test_a_nested_function_change_matches_its_locals_name(repo):
    assert select(repo, alpha_with("return 6\n", "return 60\n")) == ("some", ["tests/test_three.py", "tests/test_two.py"])


def test_a_module_level_change_selects_every_file_that_ran_anything_in_the_file(repo):
    assert select(repo, alpha_with("LIMIT = 3\n", "LIMIT = 4\n")) == (
        "some", ["tests/test_one.py", "tests/test_three.py", "tests/test_two.py"])


def test_a_def_line_change_counts_as_module_level(repo):
    assert select(repo, alpha_with("def helper():", "def helper(extra=0):")) == (
        "some", ["tests/test_one.py", "tests/test_three.py", "tests/test_two.py"])


def test_an_added_function_counts_as_module_level(repo):
    assert select(repo, alpha_with("class Box:", "def added():\n    return 9\n\n\nclass Box:")) == (
        "some", ["tests/test_one.py", "tests/test_three.py", "tests/test_two.py"])


def test_a_deleted_method_counts_as_module_level(repo):
    changed = alpha_with("    def other(self):\n        return 5\n", "")
    assert select(repo, changed) == ("some", ["tests/test_one.py", "tests/test_three.py", "tests/test_two.py"])


def test_an_opened_data_file_selects_the_files_that_opened_it(repo):
    assert select(repo, {"rules/a.yml": "a: 2\n"}) == ("some", ["tests/test_three.py", "tests/test_two.py"])


def test_a_file_added_to_a_listed_directory_selects_the_files_that_listed_it(repo):
    assert select(repo, {"rules/c.yml": "c: 3\n"}) == ("some", ["tests/test_three.py", "tests/test_two.py"])


def test_a_file_nothing_read_selects_only_the_subprocess_tests(repo):
    assert select(repo, {"docs/notes.md": "more notes\n"}) == ("some", ["tests/test_three.py"])


def test_a_changed_test_file_is_selected(repo):
    assert select(repo, {"tests/test_four.py": "def test_four():\n    assert True\n"}) == (
        "some", ["tests/test_four.py", "tests/test_three.py"])


def test_a_test_file_missing_from_the_map_is_selected(repo):
    assert select(repo, {"tests/test_five.py": "def test_five():\n    pass\n"}) == (
        "some", ["tests/test_five.py", "tests/test_three.py"])


def test_a_session_function_change_selects_the_whole_suite(repo):
    assert select(repo, {"pkg/gamma.py": "def start_up():\n    return 8\n"}) == ("all", [])


def test_a_data_file_the_session_opened_selects_the_whole_suite(repo):
    session = entry({"pkg/gamma.py": ["start_up"]}, opened=["rules/a.yml"])
    assert select(repo, {"rules/a.yml": "a: 3\n"}, session=session) == ("all", [])


@pytest.mark.parametrize("harness", ["tests/conftest.py", "pytest.ini", "tests/requirements-hermetic.lock",
                                     "scripts/run-hermetic-pytest.sh", "scripts/ci/testmap_select.py"])
def test_a_harness_change_selects_the_whole_suite(repo, harness):
    assert select(repo, {harness: "# changed\n"}) == ("all", [])


def test_a_python_file_that_does_not_parse_selects_every_file_that_ran_anything_in_it(repo):
    assert select(repo, {"pkg/beta.py": "def read_rules(:\n"}) == (
        "some", ["tests/test_four.py", "tests/test_three.py", "tests/test_two.py"])


def test_a_deleted_python_file_selects_every_file_that_ran_anything_in_it(repo):
    assert select(repo, {"pkg/beta.py": None}) == ("some", ["tests/test_four.py", "tests/test_three.py", "tests/test_two.py"])


def test_a_deleted_test_file_is_not_selected(repo):
    assert select(repo, {"tests/test_four.py": None}) == ("some", ["tests/test_three.py"])


def test_no_change_selects_only_the_subprocess_tests(repo):
    assert select(repo, {}) == ("some", ["tests/test_three.py"])


def test_always_run_list_adds_its_files(repo):
    (repo / "tests" / "always_run.txt").write_text("# cross-cutting checks\ntests/test_four.py\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "list")
    assert select(repo, alpha_with("return 5\n", "return 50\n")) == ("some", ["tests/test_four.py", "tests/test_three.py"])


def test_an_explicit_base_overrides_the_maps_base(repo):
    first = git(repo, "rev-parse", "HEAD")
    write(repo, alpha_with("return 1\n", "return 2\n"))
    git(repo, "commit", "-q", "-am", "earlier change")
    head = git(repo, "rev-parse", "HEAD")
    map_path = repo.parent / "map.json"
    map_path.write_text(json.dumps({"version": 1, "base": first, "tests": MAP_TESTS, "session": MAP_SESSION}))

    def run(*extra):
        result = subprocess.run([sys.executable, str(SELECTOR), "--map", str(map_path), "--repo", str(repo), *extra],
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return sorted(line for line in result.stdout.splitlines() if line)

    # From the map's base the helper change counts; from --base HEAD nothing changed.
    assert run() == ["tests/test_one.py", "tests/test_three.py"]
    assert run("--base", head) == ["tests/test_three.py"]


def test_a_map_of_another_version_is_refused(repo, tmp_path):
    map_path = tmp_path / "old.json"
    map_path.write_text(json.dumps({"version": 0, "base": git(repo, "rev-parse", "HEAD"), "tests": {}, "session": entry()}))
    result = subprocess.run([sys.executable, str(SELECTOR), "--map", str(map_path), "--repo", str(repo)],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode != 0
    assert "version" in result.stderr


def test_the_reason_for_the_whole_suite_is_named_on_stderr(repo):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, {"tests/conftest.py": "# changed\n"})
    git(repo, "commit", "-q", "-am", "harness")
    map_path = repo.parent / "map.json"
    map_path.write_text(json.dumps({"version": 1, "base": base, "tests": MAP_TESTS, "session": MAP_SESSION}))
    result = subprocess.run([sys.executable, str(SELECTOR), "--map", str(map_path), "--repo", str(repo)],
                            text=True, capture_output=True, timeout=60)
    assert result.stdout.split() == ["ALL"]
    assert "tests/conftest.py" in result.stderr


def test_a_modified_file_in_a_listed_directory_selects_only_the_files_that_opened_it(repo):
    session = entry({"pkg/gamma.py": ["start_up"]})
    tests = {**MAP_TESTS, "tests/test_four.py": entry(listed=["rules"])}
    assert select(repo, {"rules/a.yml": "a: 9\n"}, tests=tests, session=session) == (
        "some", ["tests/test_three.py", "tests/test_two.py"])


def test_a_file_added_below_a_listed_directory_is_not_seen_by_that_listing(repo):
    assert select(repo, {"rules/deeper/c.yml": "c: 3\n"}) == ("some", ["tests/test_three.py"])


def test_a_root_listing_sees_files_added_at_the_root(repo):
    tests = {**MAP_TESTS, "tests/test_four.py": entry(listed=["."])}
    assert select(repo, {"NEWFILE.md": "x\n"}, tests=tests) == ("some", ["tests/test_four.py", "tests/test_three.py"])


def test_a_nested_test_file_missing_from_the_map_is_selected(repo):
    assert select(repo, {"tests/sub/test_nested.py": "def test_nested():\n    pass\n"}) == (
        "some", ["tests/sub/test_nested.py", "tests/test_three.py"])


def test_an_empty_selection_prints_nothing(repo):
    tests = {name: value for name, value in MAP_TESTS.items() if name != "tests/test_three.py"}
    (repo / "tests" / "test_three.py").unlink()
    git(repo, "commit", "-q", "-am", "drop the subprocess test")
    assert select(repo, {"docs/notes.md": "more\n"}, tests=tests) == ("some", [])


def test_a_session_subprocess_does_not_select_the_whole_suite(repo):
    session = entry({"pkg/gamma.py": ["start_up"]}, subprocess_=True)
    assert select(repo, alpha_with("return 5\n", "return 50\n"), session=session) == ("some", ["tests/test_three.py"])
