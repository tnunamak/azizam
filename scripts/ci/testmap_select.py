#!/usr/bin/env python3
"""Select tests reached by a git diff and a recorded test map."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

HARNESS = {
    "pytest.ini", "pyproject.toml", "requirements.txt", "tests/_hermetic.py",
    "tests/requirements-hermetic.in", "tests/requirements-hermetic.lock",
    "scripts/run-hermetic-pytest.sh", "scripts/hermetic_source_preflight.sh",
    "scripts/ci/testmap_record.py", "scripts/ci/testmap_select.py",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True).stdout


def parent_dir(path: str) -> str:
    """The directory a listing of which shows `path` (\".\" for the root)."""
    return Path(path).parent.as_posix()


def lists_it(entry: dict, status: str, path: str) -> bool:
    """A listing sees only additions and deletions in the directory it read."""
    return status in ("A", "D") and parent_dir(path) in entry.get("listed", [])


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    rows = git(repo, "diff", "--name-status", "--no-renames", base, "HEAD").splitlines()
    return [(row[0], row[2:]) for row in rows]


def changed_lines(repo: Path, base: str, path: str) -> tuple[set[int], set[int]]:
    patch = git(repo, "diff", "--no-ext-diff", "--unified=0", base, "HEAD", "--", path)
    old: set[int] = set()
    new: set[int] = set()
    for line in patch.splitlines():
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            a, ac, b, bc = match.groups()
            old.update(range(int(a), int(a) + int(ac or 1)))
            new.update(range(int(b), int(b) + int(bc or 1)))
    return old, new


def names_at(source: str, lines: set[int]) -> set[str]:
    tree = ast.parse(source)
    result: set[str] = set()

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + child.name
                first_body = child.body[0].lineno if child.body else child.lineno
                if any(first_body <= n <= child.end_lineno for n in lines):
                    result.add(qual)
                visit(child, qual + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
    visit(tree)
    # Any changed line not inside a function (including def/decorator lines) is module scope.
    covered = set()
    def collect(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = child.body[0].lineno if child.body else child.lineno
                covered.update(n for n in lines if first <= n <= child.end_lineno)
            collect(child)
    collect(tree)
    if lines - covered:
        result.add("<module>")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base")
    args = parser.parse_args()
    try:
        data = json.loads(args.map.read_text())
        if data.get("version") != 1:
            raise ValueError("unsupported map version")
        base = args.base or data["base"]
        changes = changed_files(args.repo, base)
    except Exception as exc:
        print(f"test map version/input error: {exc}", file=sys.stderr)
        return 2

    tests = data.get("tests", {})
    session = data.get("session", {})
    changed_paths = {p for _, p in changes}
    if any(p.endswith("conftest.py") or p in HARNESS for p in changed_paths):
        print("ALL", file=sys.stdout); print(f"harness changed: {sorted(p for p in changed_paths if p.endswith('conftest.py') or p in HARNESS)}", file=sys.stderr); return 0
    selected: set[str] = set()
    for status, path in changes:
        if path in session.get("opened", []) or lists_it(session, status, path):
            print("ALL"); print(f"session work read {path}", file=sys.stderr); return 0
        if path.endswith(".py"):
            if status in ("A", "D"):
                old_names = new_names = {"<module>"}
            else:
                old_lines, new_lines = changed_lines(args.repo, base, path)
                try:
                    old_names = names_at(git(args.repo, "show", f"{base}:{path}"), old_lines)
                    new_names = names_at((args.repo / path).read_text(), new_lines)
                except (SyntaxError, subprocess.CalledProcessError, OSError):
                    old_names = new_names = {"<module>"}
            names = old_names | new_names
            session_ran = set(session.get("functions", {}).get(path, []))
            if names & session_ran or ("<module>" in names and session_ran):
                print("ALL"); print(f"session work ran changed code in {path}", file=sys.stderr); return 0
            for test, entry in tests.items():
                ran = set(entry.get("functions", {}).get(path, []))
                if (names & ran) or ("<module>" in names and ran) or path in entry.get("opened", []) or entry.get("subprocess"):
                    selected.add(test)
                if lists_it(entry, status, path):
                    selected.add(test)
        else:
            for test, entry in tests.items():
                if path in entry.get("opened", []) or entry.get("subprocess") or lists_it(entry, status, path):
                    selected.add(test)
    for status, path in changes:
        if path.startswith("tests/") and Path(path).name.startswith("test_") and path.endswith(".py") and status != "D":
            selected.add(path)
    for p in (args.repo / "tests").rglob("test_*.py"):
        rel = p.relative_to(args.repo).as_posix()
        if rel not in tests:
            selected.add(rel)
    always = args.repo / "tests/always_run.txt"
    if always.exists():
        for line in always.read_text().splitlines():
            item = line.split("#", 1)[0].strip()
            if item and (args.repo / item).is_file():
                selected.add(item)
    # A subprocess started outside any test file is background work of an earlier test; the test files that
    # start subprocesses always run, so it adds no selection of its own.
    selected.update(test for test, entry in tests.items() if entry.get("subprocess"))
    if selected:
        print("\n".join(sorted(selected)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
