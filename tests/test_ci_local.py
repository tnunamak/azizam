"""scripts/ci-local's signoff, host lock and run-directory lifecycle.

Each test runs the real ci-local, merge_cases.py, policy.py and plan_shards.py
in a throwaway repository whose test runners, installers, `node`, `gh` and
fresh-instance journey are stubs that finish in milliseconds. The origin is a
local bare repository, so `git ls-remote` is real; `gh` only records what it
was asked to post. FAKE_DURING_RUN is shell that the native fresh-instance
stub runs while the browser lanes run, to change the world mid-run; it
also delivers the signals, since the hermetic guard forbids os.kill here.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIED = ("scripts/ci-local", "scripts/low-priority", "scripts/ci/merge_cases.py", "scripts/ci/policy.py", "scripts/e2e/plan_shards.py")

STUBS = {
    # Tools on PATH.
    "npm": """case $1 in ci) mkdir -p node_modules/.bin && cp "$FAKE_BIN/playwright" node_modules/.bin/ ;; esac""",
    "npx": """
if [[ $1 == vitest ]]; then
  printf '{"framework":"vitest","config":"vitest.config.ts","project":"","id":"v1","file":"%s","titles":["a"],"status":"%s","duration_ms":5}\\n' \\
    "$PWD/tests/a.spec.ts" "${FAKE_VITEST_STATUS:-passed}" >> "$CASE_RECEIPTS_FILE"
  [[ ${FAKE_VITEST_STATUS:-passed} == passed ]]
fi""",
    "uv": """if [[ $1 == venv ]]; then for arg; do dir=$arg; done; mkdir -p "$dir/bin"; ln -sf /usr/bin/python3 "$dir/bin/python"; fi""",
    "node": """if [[ $1 == --version ]]; then echo v0-fake
elif [[ $1 == --test ]]; then
  printf '{"framework":"node","config":"","project":"","id":"g","file":"%s","titles":["g"],"status":"passed","duration_ms":5}\\n' \\
    "$PWD/${!#}" >> "$CASE_RECEIPTS_FILE"
else echo "$FAKE_BIN/chrome"; fi""",
    "chrome": "echo Fake Chromium 1",
    "ffmpeg": "echo ffmpeg version fake",
    "gh": """
echo "$*" >> "$FAKE_GH_LOG"
if [[ "$1 $2" == "issue list" ]]; then echo "${FAKE_NIGHTLY_RED:-}"; fi
if [[ $1 == api && -n ${FAKE_GH_NO_COMMIT:-} && $2 == repos/*/commits/* ]]; then exit 1; fi
if [[ $1 == api ]]; then for arg; do [[ $arg == repos/* ]] && echo "https://api.github.test/${arg/\\{owner\\}\\/\\{repo\\}/o/r}"; done; fi
exit 0""",
    # Copied into web/node_modules/.bin by the npm stub.
    "playwright": """
[[ $1 == test ]] || exit 0
if [[ $2 == --list ]]; then echo "  [chromium] › e2e/a.spec.ts:3:1 › a"; exit 0; fi
case $3 in
  playwright.config.ts) file=e2e/a.spec.ts ;;
  playwright.live-data.config.ts) file=e2e/live-data-scoping.spec.ts ;;
  *) file=e2e/b.household.ts ;;
esac
printf '{"framework":"playwright","config":"%s","project":"chromium","id":"%s","file":"%s","titles":["a"],"status":"passed","duration_ms":5}\\n' \\
  "$3" "$3" "$PWD/$file" >> "$CASE_RECEIPTS_FILE"
""",
}
REPO_FILES = {
    "scripts/run-hermetic-pytest.sh": """#!/usr/bin/env bash
printf '<testsuites><testsuite name="pytest"><testcase classname="tests.test_ok" name="test_ok" time="0.1"/></testsuite></testsuites>\\n' > "$2"
""",
    "scripts/e2e/fresh_instance.sh": """#!/usr/bin/env bash
if [[ ${1:-} != --compose && -n ${FAKE_DURING_RUN:-} ]]; then eval "$FAKE_DURING_RUN"; fi
""",
    "tests/test_ok.py": "def test_ok():\n    pass\n",
    "tests/suites.toml": "",
    "tests/budgets.toml": "[ceilings]\npytest = 10\nvitest = 10\nplaywright = 10\nnode = 10\n",
    "tests/requirements-hermetic.lock": "",
    "tests/compat/node/probe-gates.test.mjs": "",
    "web/package.json": "{}\n",
    "web/vitest.config.ts": "export default { test: { retry: 0 } };\n",
    "web/playwright.config.ts": "export default { retries: 0 };\n",
    "web/playwright.live-data.config.ts": "export default { retries: 0 };\n",
    "web/playwright.household.config.ts": "export default { retries: 0 };\n",
    "web/tests/a.spec.ts": "",
    "web/e2e/a.spec.ts": "",
    "web/e2e/live-data-scoping.spec.ts": "",
    "web/e2e/b.household.ts": "",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.origin = tmp / "origin.git"
        self.repo = tmp / "repo"
        self.gh_log = tmp / "gh.log"
        self.bin.mkdir()
        for name, body in STUBS.items():
            (self.bin / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
            (self.bin / name).chmod(0o755)
        # The sandbox binds /usr/bin but not /etc/alternatives, where awk points.
        (self.bin / "awk").symlink_to(next(filter(None, map(shutil.which, ("mawk", "gawk", "original-awk")))))
        for relative, body in REPO_FILES.items():
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / relative).write_text(body)
        for relative in COPIED:
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, self.repo / relative)
        for script in ("scripts/run-hermetic-pytest.sh", "scripts/e2e/fresh_instance.sh"):
            (self.repo / script).chmod(0o755)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        _git(self.repo, "config", "user.name", "Gate Tester")
        _git(self.repo, "config", "user.email", "gate@example.test")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "origin", "main")
        self.candidate("change")

    def candidate(self, message: str) -> str:
        """Make HEAD a merge candidate: one commit on top of origin/main."""
        _git(self.repo, "reset", "-q", "--hard", "origin/main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return _git(self.repo, "rev-parse", "HEAD")

    def env(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "C.UTF-8",
                "FAKE_BIN": str(self.bin), "FAKE_GH_LOG": str(self.gh_log), **extra}

    def args(self, *extra: str) -> list[str]:
        return [str(self.repo / "scripts/ci-local"), "--force", "--shards", "1", "--order-seed", "5", *extra]

    def run(self, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
        # Files, not pipes: jobs a killed controller leaves behind keep its
        # output open, and a pipe would wait for them.
        out, err = self.tmp / "stdout", self.tmp / "stderr"
        with out.open("w") as stdout, err.open("w") as stderr:
            process = subprocess.run(self.args(*extra), cwd=self.repo, env=self.env(**env), stdout=stdout,
                                     stderr=stderr, timeout=300)
        return subprocess.CompletedProcess(process.args, process.returncode, out.read_text(), err.read_text())

    def start(self, *extra: str, **env: str) -> subprocess.Popen[str]:
        return subprocess.Popen(self.args(*extra), cwd=self.repo, env=self.env(**env), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    @property
    def receipts(self) -> Path:
        return self.home / ".tmp" / "azizam-ci-receipts" / _git(self.repo, "rev-parse", "HEAD^{tree}")

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in (self.home / ".tmp").glob("azizam-ci-local.*") if path.is_dir())

    def lock_is_free(self) -> bool:
        with (self.home / ".tmp" / "azizam-ci.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    def kill_lock_holders(self) -> None:
        lock = self.home / ".tmp" / "azizam-ci.lock"
        if lock.exists():
            subprocess.run(["fuser", "-s", "-k", "-KILL", str(lock)], capture_output=True)

    def posts(self) -> list[str]:
        return [line for line in self.gh_log.read_text().splitlines() if line.startswith("api --method POST")] \
            if self.gh_log.exists() else []


@pytest.fixture
def world(tmp_path: Path):
    world = World(tmp_path)
    yield world
    world.kill_lock_holders()  # anything a killed controller left behind


def test_signoff_posts_to_the_tested_sha_then_writes_the_receipt(world: World) -> None:
    sha = _git(world.repo, "rev-parse", "HEAD")
    result = world.run("--signoff", "--target", "main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert world.posts() == [f"api --method POST repos/{{owner}}/{{repo}}/statuses/{sha} -f state=success "
                             "-f context=signoff -f description=Gate Tester signed off --jq .url"]
    receipt = dict(line.split("\t", 1) for line in (world.receipts / f"signoff-{sha}.tsv").read_text().splitlines())
    assert receipt["sha"] == sha
    assert receipt["target_tip"] == _git(world.repo, "rev-parse", "origin/main")
    assert receipt["seed"] == "5"
    assert receipt["status_url"] == f"https://api.github.test/repos/o/r/statuses/{sha}"
    assert receipt["steps"].split(",") == sorted([
        "cases", "e2e-browser", "e2e-copy-18650", "e2e-copy-18660", "e2e-copy-18670", "e2e-household",
        "e2e-live-data", "e2e-plan", "e2e-shard-1-of-1", "e2e-venv", "fresh-compose", "fresh-native",
        "fresh-venv", "policy", "python-hermetic", "web-check", "web-npm-ci", "web-unit"])
    assert receipt["policy"].startswith("policy: ok:")
    assert world.run_dirs() == []  # a passed run removes its directory


def test_signoff_refuses_a_candidate_github_has_not_received_before_running(world: World) -> None:
    result = world.run("--signoff", "--target", "main", FAKE_GH_NO_COMMIT="1")
    assert result.returncode == 3
    assert "GitHub has no commit" in result.stderr and "push the merge candidate" in result.stderr
    assert world.run_dirs() == []  # refused before any work
    assert world.posts() == [] and not list(world.receipts.glob("signoff-*"))


@pytest.mark.parametrize(("during_run", "message"), [
    ('git -C "$FAKE_REPO" push -q "$FAKE_ORIGIN" "$FAKE_MOVED:refs/heads/main"', "origin/main moved from"),
    ('git -C "$FAKE_REPO" checkout -q --detach HEAD^', "the checkout moved from"),
])
def test_signoff_refuses_what_moved_during_the_run(world: World, during_run: str, message: str) -> None:
    moved = world.candidate("main moves on")  # a commit that will become origin/main mid-run
    world.candidate("change")
    receipts = world.receipts
    result = world.run("--signoff", "--target", "main", FAKE_DURING_RUN=during_run, FAKE_REPO=str(world.repo),
                       FAKE_ORIGIN=str(world.origin), FAKE_MOVED=moved)
    assert result.returncode == 3, result.stdout + result.stderr
    assert f"signoff refused: {message}" in result.stderr
    assert "ci-local PASSED" in result.stdout  # the tests passed; only the signoff is refused
    assert world.posts() == []
    assert not list(receipts.glob("signoff-*"))


def test_nightly_red_bypass_needs_a_commit_that_closes_the_issue(world: World) -> None:
    result = world.run("--signoff", "--target", "main", "--closes-nightly-red", "7", FAKE_NIGHTLY_RED="7")
    assert result.returncode == 3
    assert "no commit in" in result.stderr and "closes nightly-red issue #7" in result.stderr
    assert world.run_dirs() == []  # refused before any work
    sha = world.candidate("Fix the red night\n\nFixes #7")
    result = world.run("--signoff", "--target", "main", "--closes-nightly-red", "7", FAKE_NIGHTLY_RED="7")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nightly_red_closed\t7" in (world.receipts / f"signoff-{sha}.tsv").read_text()


def test_only_the_latest_failed_run_directory_is_kept(world: World) -> None:
    first = world.run(FAKE_VITEST_STATUS="failed")
    assert first.returncode == 1
    [kept] = world.run_dirs()
    assert f"kept the failed run's directory for debugging: {kept}" in first.stdout
    second = world.run(FAKE_VITEST_STATUS="failed")
    assert second.returncode == 1
    [newer] = world.run_dirs()
    assert newer != kept and f"debugging: {newer}" in second.stdout
    assert world.run().returncode == 0
    assert world.run_dirs() == [newer]
    assert (world.receipts / "cases.jsonl").is_file()  # receipts live outside the run directory


# ci-local writes "pid <pid> sha ..." into the lock file it holds.
SIGNAL_CONTROLLER = 'kill -{} "$(cut -d" " -f2 "$HOME/.tmp/azizam-ci.lock")"'


def test_interrupted_signoff_run_writes_no_receipt_and_frees_the_host(world: World) -> None:
    result = world.run("--signoff", "--target", "main", FAKE_DURING_RUN=SIGNAL_CONTROLLER.format("TERM") + "; sleep 60")
    assert result.returncode == 143, result.stdout + result.stderr
    assert not list(world.receipts.glob("signoff-*")) and world.posts() == []
    assert world.run_dirs() == [] and world.lock_is_free()


def test_a_second_gate_waits_for_the_host_lock(world: World) -> None:
    lock = world.home / ".tmp" / "azizam-ci.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        process = world.start()
        time.sleep(2)
        assert process.poll() is None and world.run_dirs() == []
        fcntl.flock(handle, fcntl.LOCK_UN)
        output = process.communicate(timeout=300)[0]
    assert process.returncode == 0, output
    assert "waiting for the host CI lock" in output


def test_a_killed_controller_keeps_the_lock_until_its_jobs_end(world: World) -> None:
    result = world.run(FAKE_DURING_RUN=SIGNAL_CONTROLLER.format("KILL") + "; sleep 60")
    assert result.returncode == -9  # SIGKILL: no cleanup ran
    [orphaned] = world.run_dirs()
    assert not world.lock_is_free()  # the surviving journey still holds it
    world.kill_lock_holders()  # what the waiting message suggests: fuser on the lock
    deadline = time.monotonic() + 30
    while not world.lock_is_free():
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert world.run().returncode == 0
    assert world.run_dirs() == []  # the next run removed the killed run's directory
    assert not os.path.exists(orphaned)
