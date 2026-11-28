"""scripts/ci/testmap_record.py: the per-test-file dependency recorder for the diff-selected gate.

The recorder is a pytest plugin. With TEST_MAP_DIR set it records, for each test file, which repository
functions ran while that file was collected and while its tests ran (sys.monitoring PY_START), which
repository files it opened, which repository directories it listed, and whether it started a subprocess.
Work outside any test file (session fixtures, plugin start-up) is recorded under "session". Each process
writes one shard; `merge` joins the shards into one map that scripts/ci/testmap_select.py reads.

Each test here runs a real nested pytest on a throwaway project and reads the shard it wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "scripts" / "ci"
RECORDER = PLUGIN_DIR / "testmap_record.py"

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

PROJECT = {
    "pkg/__init__.py": "",
    "pkg/alpha.py": """
        LIMIT = 3

        def used():
            return helper() + LIMIT

        def helper():
            return 1

        def unused():
            return 2

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
        from pathlib import Path

        def read_rules(root):
            return (Path(root) / "rules" / "a.yml").read_text()

        def list_rules(root):
            return sorted(p.name for p in (Path(root) / "rules").iterdir())
    """,
    "pkg/gamma.py": """
        def start_up():
            return 7
    """,
    "rules/a.yml": "a: 1\n",
    "rules/b.yml": "b: 2\n",
    "tests/conftest.py": """
        import pytest
        from pkg import gamma

        @pytest.fixture(scope="session", autouse=True)
        def boot():
            gamma.start_up()
    """,
    "tests/test_one.py": """
        from pkg import alpha

        def test_used():
            assert alpha.used() == 4

        def test_box():
            assert alpha.Box().size() == 4
    """,
    "tests/test_two.py": """
        from pkg import alpha, beta

        def test_outer():
            assert alpha.outer() == 6

        def test_files(request):
            root = request.config.rootpath
            assert "start_up" in (root / "pkg" / "gamma.py").read_text()
            assert beta.read_rules(root) == "a: 1\\n"
            assert beta.list_rules(root) == ["a.yml", "b.yml"]
    """,
    "tests/test_three.py": """
        import subprocess, sys

        def test_child():
            subprocess.run([sys.executable, "-c", "pass"], check=True)
    """,
}


def write_project(root: Path) -> None:
    for name, body in PROJECT.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"))


def run_recorded(project: Path, out: Path, *pytest_args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "TEST_MAP_"))}
    env.update(PYTHONPATH=f"{PLUGIN_DIR}{os.pathsep}{project}", TEST_MAP_DIR=str(out), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "testmap_record", "-p", "no:cacheprovider",
         "-p", "no:randomly", "--rootdir", str(project), str(project / "tests"), *pytest_args],
        cwd=project, env=env, text=True, capture_output=True, timeout=120,
    )


@pytest.fixture
def recorded(tmp_path):
    project, out = tmp_path / "project", tmp_path / "shards"
    write_project(project)
    out.mkdir()
    result = run_recorded(project, out)
    assert result.returncode == 0, result.stdout + result.stderr
    shards = sorted(out.glob("shard-*.json"))
    assert len(shards) == 1, shards
    return json.loads(shards[0].read_text())


def functions(entry: dict, path: str) -> set[str]:
    return set(entry["functions"].get(path, []))


def test_records_the_functions_each_test_file_ran(recorded):
    one = recorded["tests"]["tests/test_one.py"]
    assert {"used", "helper", "Box.size"} <= functions(one, "pkg/alpha.py")
    assert not {"unused", "Box.other", "outer"} & functions(one, "pkg/alpha.py")
    two = recorded["tests"]["tests/test_two.py"]
    assert {"outer", "outer.<locals>.inner"} <= functions(two, "pkg/alpha.py")
    assert {"read_rules", "list_rules"} <= functions(two, "pkg/beta.py")
    assert "used" not in functions(two, "pkg/alpha.py")


def test_a_function_two_files_run_is_recorded_for_both(tmp_path):
    project, out = tmp_path / "project", tmp_path / "shards"
    write_project(project)
    (project / "tests/test_four.py").write_text("from pkg import alpha\n\ndef test_again():\n    assert alpha.used() == 4\n")
    out.mkdir()
    result = run_recorded(project, out)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(next(out.glob("shard-*.json")).read_text())
    for name in ("tests/test_one.py", "tests/test_four.py"):
        assert "used" in functions(data["tests"][name], "pkg/alpha.py"), name


def test_module_import_runs_count_for_the_file_that_was_being_collected(recorded):
    # pkg/alpha.py is first imported while tests/test_one.py is collected.
    assert "<module>" in functions(recorded["tests"]["tests/test_one.py"], "pkg/alpha.py")
    # pkg/beta.py is first imported while tests/test_two.py is collected.
    assert "<module>" in functions(recorded["tests"]["tests/test_two.py"], "pkg/beta.py")


def test_work_outside_a_test_file_is_session(recorded):
    session = recorded["session"]
    assert "start_up" in functions(session, "pkg/gamma.py")
    for entry in recorded["tests"].values():
        assert "start_up" not in functions(entry, "pkg/gamma.py")


def test_records_opened_files_listed_directories_and_subprocesses(recorded):
    two = recorded["tests"]["tests/test_two.py"]
    assert "rules/a.yml" in two["opened"]
    assert "rules" in two["listed"]
    # pytest's own collection lists the test directory; that listing is not a test file's or the session's.
    assert "tests" not in recorded["session"]["listed"]
    assert two["subprocess"] is False
    assert recorded["tests"]["tests/test_three.py"]["subprocess"] is True
    # Imports and pytest's own reads of test files are not dependencies: the function record covers them.
    assert recorded["tests"]["tests/test_one.py"]["opened"] == []
    # A test that reads a source file as text depends on its whole content.
    assert "pkg/gamma.py" in recorded["tests"]["tests/test_two.py"]["opened"]


def test_paths_are_repo_relative_and_outside_files_are_dropped(recorded):
    for entry in [*recorded["tests"].values(), recorded["session"]]:
        for path in [*entry["functions"], *entry["opened"], *entry["listed"]]:
            assert not path.startswith(("/", "..", "<")), path
        assert not any("site-packages" in path for path in entry["functions"])
    assert recorded["version"] == 1
    assert set(recorded["tests"]) == {"tests/test_one.py", "tests/test_two.py", "tests/test_three.py"}


def test_without_the_env_variable_the_plugin_records_nothing(tmp_path):
    project, out = tmp_path / "project", tmp_path / "shards"
    write_project(project)
    out.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "TEST_MAP_"))}
    env.update(PYTHONPATH=f"{PLUGIN_DIR}{os.pathsep}{project}", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "testmap_record", "-p", "no:cacheprovider",
         "--rootdir", str(project), str(project / "tests")],
        cwd=project, env=env, text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(out.iterdir()) == []


def test_a_failing_test_is_still_recorded(tmp_path):
    project, out = tmp_path / "project", tmp_path / "shards"
    write_project(project)
    (project / "tests/test_fail.py").write_text("from pkg import alpha\n\ndef test_bad():\n    assert alpha.unused() == 0\n")
    out.mkdir()
    result = run_recorded(project, out)
    assert result.returncode == 1, result.stdout + result.stderr
    data = json.loads(next(out.glob("shard-*.json")).read_text())
    assert "unused" in functions(data["tests"]["tests/test_fail.py"], "pkg/alpha.py")


def test_merge_joins_shards_and_stamps_the_base(tmp_path):
    shards = tmp_path / "shards"
    shards.mkdir()
    empty = {"functions": {}, "opened": [], "listed": [], "subprocess": False}
    (shards / "shard-1.json").write_text(json.dumps({"version": 1, "session": empty, "tests": {
        "tests/test_a.py": {"functions": {"m.py": ["f"]}, "opened": ["x.yml"], "listed": [], "subprocess": False}}}))
    (shards / "shard-2.json").write_text(json.dumps({"version": 1, "session": {**empty, "functions": {"m.py": ["boot"]}},
        "tests": {"tests/test_a.py": {"functions": {"m.py": ["g"]}, "opened": [], "listed": ["d"], "subprocess": True},
                  "tests/test_b.py": {"functions": {"n.py": ["h"]}, "opened": [], "listed": [], "subprocess": False}}}))
    out = tmp_path / "map.json"
    result = subprocess.run([sys.executable, str(RECORDER), "merge", "--base", "a" * 40, "--out", str(out), str(shards)],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    merged = json.loads(out.read_text())
    assert merged["version"] == 1 and merged["base"] == "a" * 40
    a = merged["tests"]["tests/test_a.py"]
    assert a == {"functions": {"m.py": ["f", "g"]}, "opened": ["x.yml"], "listed": ["d"], "subprocess": True}
    assert merged["tests"]["tests/test_b.py"]["functions"] == {"n.py": ["h"]}
    assert merged["session"]["functions"] == {"m.py": ["boot"]}


def test_merge_refuses_an_empty_shard_directory(tmp_path):
    shards = tmp_path / "shards"
    shards.mkdir()
    result = subprocess.run([sys.executable, str(RECORDER), "merge", "--base", "a" * 40, "--out",
                             str(tmp_path / "map.json"), str(shards)], text=True, capture_output=True, timeout=60)
    assert result.returncode != 0
    assert not (tmp_path / "map.json").exists()
