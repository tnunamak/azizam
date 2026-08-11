"""Static repo-safety guard: sensitive/runtime artifacts must be gitignored.

Covers P0-1 from the 2026-07-16 red team: live admin/control-plane SQLite
backups, sidecars, root-level stray DB copies, generated LiteLLM config
backups/snapshots, and core dumps must never be stageable via `git add -A`.
Uses `git check-ignore --no-index -v` against synthetic representative
paths (no real files are created or inspected).

Global/system git config and any `core.excludesFile` are neutralized
(`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM=/dev/null`, `-c
core.excludesFile=/dev/null`) so a match can only ever come from this
worktree's own exclude sources — a machine-local `~/.gitconfig` excludesfile
or a stray `/etc/gitconfig` entry cannot make the assertion silently
false-pass. Each "must be ignored" case additionally asserts the match is
attributed to this repo's `.gitignore` (not `.git/info/exclude` or any
external file), and cross-checks that canonical tracked/generated config
paths remain unignored.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_GITIGNORE = REPO_ROOT / ".gitignore"

# Representative sensitive/runtime paths that MUST be ignored.
IGNORED_PATHS = [
    "data/admin.db",
    "data/admin.db-wal",
    "data/admin.db-shm",
    "data/admin.db.broken-20260101",
    "data/admin.db.bak.local-admin-key-cleanup.20260630-103242",
    "data/admin.db.bak.local-admin-key-cleanup.20260630-103242-wal",
    "data/admin.db.bak.local-admin-key-cleanup.20260630-103242-shm",
    "admin.db",
    "data/litellm.config.yaml",
    "data/litellm.config.yaml.old",
    "data/litellm.config.yaml.bak.20260630-103242",
    "data/litellm.config.yaml.mylllama-bak.20260605-182039",
    "data/stt-spool/request-private-audio/upload-private.part",
    "core.73804",
    "core.348027",
    "docs/design/vivid-fish-handoff/gateway/core.348027",
    "core",
]

# Representative canonical tracked/generated paths that MUST NOT be ignored.
NOT_IGNORED_PATHS = [
    "proxy.py",
    "AGENTS.md",
    ".gitignore",
    "gateway/service_registry.py",
    "tests/test_repository_hygiene.py",
    "docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md",
    "data/.gitkeep",
    "gateway/core.py",
    "web/core.ts",
    "config/core.yaml",
    "gateway/core/registry.py",
]

# Neutralize any machine-local/global/system exclude sources so a match can
# only come from this worktree's own tracked config.
_ISOLATED_ENV_OVERRIDES = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


@pytest.fixture(scope="session")
def isolated_git_worktree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Give git an isolated repository while exercising this archive's rules."""
    worktree = tmp_path_factory.mktemp("repository-hygiene")
    copied_ignore = worktree / ".gitignore"
    shutil.copyfile(REPO_GITIGNORE, copied_ignore)
    subprocess.run(
        ["git", "init", "--quiet", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_environment(),
    )
    assert copied_ignore.read_bytes() == REPO_GITIGNORE.read_bytes()
    return worktree


def _isolated_git_environment() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update(_ISOLATED_ENV_OVERRIDES)
    return env


def _check_ignore_verbose(path: str, *, worktree: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "check-ignore",
            "--no-index",
            "-v",
            path,
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=_isolated_git_environment(),
    )


@pytest.mark.parametrize("path", IGNORED_PATHS)
def test_sensitive_runtime_artifact_is_ignored_by_repo_gitignore(path, isolated_git_worktree):
    result = _check_ignore_verbose(path, worktree=isolated_git_worktree)
    assert result.returncode == 0, (
        f"expected {path!r} to be gitignored with global/system/excludesFile "
        f"neutralized, but git check-ignore reported no match "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )

    # Verbose output is "<source>:<line>:<pattern>\t<path>" per matched line;
    # take the last line, which is the pattern that actually decided the match.
    lines = [line for line in result.stdout.splitlines() if line]
    assert lines, f"expected -v output for ignored path {path!r}, got none"
    verbose_match = re.match(r"^(?P<source>.*):(?P<line>\d+):[^\t]*\t", lines[-1])
    assert verbose_match, f"unexpected -v output for ignored path {path!r}: {lines[-1]!r}"
    source = verbose_match.group("source")
    # git reports the source relative to the cwd it was invoked from.
    # when the source is inside the repo; resolve before comparing so this
    # holds regardless of git version/cwd-relative-path quirks.
    resolved_source = (isolated_git_worktree / source).resolve()
    expected_source = (isolated_git_worktree / ".gitignore").resolve()
    assert resolved_source == expected_source, (
        f"expected {path!r} to be ignored by the archived repository .gitignore "
        f"({REPO_GITIGNORE!r}), but attribution source was {source!r} "
        f"-> {resolved_source!r} (full match: {lines[-1]!r}) — a false pass "
        f"from an external exclude source"
    )


@pytest.mark.parametrize("path", NOT_IGNORED_PATHS)
def test_canonical_source_or_config_is_not_ignored(path, isolated_git_worktree):
    result = _check_ignore_verbose(path, worktree=isolated_git_worktree)
    assert result.returncode == 1, (
        f"expected {path!r} to be tracked/unignored but it is gitignored "
        f"(returncode={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r})"
    )


def test_gitless_archive_uses_isolated_git_worktree(isolated_git_worktree) -> None:
    """Regression: hermetic archives omit .git but still exercise all contracts."""
    assert (isolated_git_worktree / ".git").is_dir()
    assert (isolated_git_worktree / ".gitignore").read_bytes() == REPO_GITIGNORE.read_bytes()


def test_stt_runtime_audio_workspace_is_ignored_by_repository_policy() -> None:
    """The hermetic archive has no .git directory, so inspect the tracked rule."""
    patterns = {
        line.strip()
        for line in REPO_GITIGNORE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "data/stt-spool/" in patterns
