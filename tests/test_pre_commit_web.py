"""Exercise the real hook in owned repositories; npm only records the invocation."""

import os
from pathlib import Path
import subprocess

import pytest


HOOK = Path(__file__).resolve().parents[1] / "scripts" / "pre-commit-web.sh"


@pytest.fixture
def hook_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "npm-calls"
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "NPM_CALL_LOG": str(calls),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    # Do not inherit a surrounding worktree/index selection into this fixture.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True,
            capture_output=True, text=True, timeout=30,
        ).stdout

    git("init", "-q")
    git("config", "user.name", "Hook fixture")
    git("config", "user.email", "hook-fixture@example.invalid")
    git("config", "diff.renames", "true")
    git("config", "commit.gpgsign", "false")
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    (repo / "web").mkdir()
    (repo / "web" / ".keep").write_text("keep the check directory\n")
    (repo / "README.md").write_text("fixture\n")
    git("add", ".")
    git("commit", "-qm", "initial fixture")
    hook = repo / "pre-commit-web.sh"
    hook.write_bytes(HOOK.read_bytes())
    fake_npm = tools / "npm"
    fake_npm.write_text(
        '#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> "$NPM_CALL_LOG"\nexit 0\n'
    )
    fake_npm.chmod(0o755)

    def run(expected):
        result = subprocess.run(
            ["bash", str(hook)], cwd=repo, env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        if expected:
            assert calls.read_text().splitlines() == [f"{repo / 'web'}|run check"]
            assert "pre-commit: web/** staged" in result.stdout
        else:
            assert not calls.exists()
            assert "running 'npm run check'" not in result.stdout

    return repo, git, run


def test_staged_web_file_runs_check(hook_repo):
    repo, git, run = hook_repo
    (repo / "web" / "a.ts").write_text("export const a = 1;\n")
    git("add", "web/a.ts")
    run(True)


def test_rename_out_of_web_runs_check(hook_repo):
    repo, git, run = hook_repo
    (repo / "web" / "a.ts").write_text("export const a = 1;\n")
    git("add", "web/a.ts")
    git("commit", "-qm", "seed rename")
    (repo / "src").mkdir()
    git("mv", "web/a.ts", "src/a.ts")
    assert git("diff", "--cached", "--name-status", "--diff-filter=ACMRD").strip() == (
        "R100\tweb/a.ts\tsrc/a.ts"
    ), "exercise a real rename, not a delete/add pair"
    run(True)


def test_staged_web_deletion_runs_check(hook_repo):
    repo, git, run = hook_repo
    (repo / "web" / "a.ts").write_text("export const a = 1;\n")
    git("add", "web/a.ts")
    git("commit", "-qm", "seed deletion")
    git("rm", "web/a.ts")
    assert git("diff", "--cached", "--name-status").strip() == "D\tweb/a.ts"
    run(True)


def test_only_non_web_files_skip_check(hook_repo):
    repo, git, run = hook_repo
    (repo / "README.md").write_text("changed\n")
    (repo / "not-web").mkdir()
    (repo / "not-web" / "web").mkdir()
    (repo / "not-web" / "web" / "a.ts").write_text("not a web root input\n")
    git("add", "README.md", "not-web")
    run(False)


def test_long_staged_list_with_web_file_runs_check(hook_repo):
    repo, git, run = hook_repo
    (repo / "web" / "a.ts").write_text("export const a = 1;\n")
    other = repo / "zz-other"
    other.mkdir()
    for index in range(3000):
        (other / f"{index:04d}-{'x' * 100}.txt").write_text("fixture\n")
    git("add", "web/a.ts", "zz-other")
    staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMRD")
    assert len(staged.splitlines()) == 3001
    assert staged.splitlines()[0] == "web/a.ts"
    assert len(staged.encode()) > 65536, "grep would stop before the long tail drains"
    run(True)
