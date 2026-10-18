"""scripts/ci-local's signoff, host lock and run-directory lifecycle.

Each test runs the real ci-local, merge_cases.py and policy.py
in a throwaway repository whose test runners, installers, `node`, `gh` and
fresh-instance journey are stubs that finish in milliseconds. The origin is a
local bare repository, so `git ls-remote` is real; `gh` only records what it
was asked to post. FAKE_DURING_RUN is shell that the native fresh-instance
stub runs while the browser lanes run, to change the world mid-run; it
also delivers the signals, since the hermetic guard forbids os.kill here.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIED = ("scripts/ci/install_cached.py", "scripts/ci-local", "scripts/low-priority", "scripts/ci/gate_cost.py", "scripts/ci/merge_cases.py", "scripts/ci/policy.py", "scripts/ci/prune_receipts.py", "scripts/ci/replay_scan.py", "scripts/ci/pi-unit", "scripts/ci/journey_clock.py")

STUBS = {
    # Tools on PATH.
    "npm": """
case "$1 ${2:-}" in
  "ci "*) mkdir -p node_modules/.bin && cp "$FAKE_BIN/playwright" node_modules/.bin/ ;;
  "run build") echo build-log; eval "${FAKE_WEB_BUILD:-:}" ;;
  "run lint") [[ "$*" == "run lint -- --no-cache" ]] || exit 91; echo lint-log; exit "${FAKE_LINT_RC:-0}" ;;
  "run doctor") echo doctor-log; exit "${FAKE_DOCTOR_RC:-0}" ;;
esac""",
    "npx": """
if [[ $1 == tsc && $PWD == */integrations/pi ]]; then
  [[ "$*" == "tsc --noEmit" ]] || exit 92
  exit "${FAKE_PI_TYPE_RC:-0}"
fi
if [[ $1 == tsc ]]; then
  [[ "$*" == "tsc --noEmit --incremental false" ]] || exit 92
  echo type-log
  eval "${FAKE_STATIC_CHECK:-:}"
  exit "${FAKE_TYPE_RC:-0}"
fi
if [[ $1 == vitest ]]; then
  printf '{"framework":"vitest","config":"vitest.config.ts","project":"","id":"v1","file":"%s","titles":["a"],"status":"%s","duration_ms":5}\\n' \\
    "$PWD/tests/a.spec.ts" "${FAKE_VITEST_STATUS:-passed}" >> "$CASE_RECEIPTS_FILE"
  [[ ${FAKE_VITEST_STATUS:-passed} == passed ]]
fi""",
    "uv": """if [[ $1 == python ]]; then echo /usr/bin/python3; elif [[ $1 == venv ]]; then for arg; do dir=$arg; done; mkdir -p "$dir/bin"; ln -sf /usr/bin/python3 "$dir/bin/python"; fi""",
    "node": """if [[ $1 == --version ]]; then echo v0-fake
elif [[ $1 == --test ]]; then
  printf '{"framework":"node","config":"","project":"","id":"%s","file":"%s","titles":["g"],"status":"passed","duration_ms":5}\\n' \\
    "${!#}" "$PWD/${!#}" >> "$CASE_RECEIPTS_FILE"
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
eval "${FAKE_BROWSER_GATE:-:}" || exit $?
if [[ -n ${FAKE_TREE_WRITE:-} ]]; then touch "$PWD/$FAKE_TREE_WRITE"; fi
case $3 in
  playwright.config.ts) file=e2e/a.spec.ts ;;
  playwright.live-data.config.ts) file=e2e/live-data-scoping.spec.ts ;;
  *) file=e2e/b.household.ts ;;
esac
printf '{"framework":"playwright","config":"%s","project":"chromium","id":"%s","file":"%s","titles":["a"],"status":"passed","duration_ms":5}\\n' \\
  "$3" "$3" "$PWD/$file" >> "$CASE_RECEIPTS_FILE"
# web/e2e/first-run.spec.ts writes its time where the gate asks (main config only).
if [[ $3 == playwright.config.ts && -n ${FIRST_RUN_TIMING_OUT:-} && -z ${FAKE_NO_FIRST_RUN_TIME:-} ]]; then
  printf '{"elapsed_seconds": %s}\\n' "${FAKE_FIRST_RUN_S:-8}" > "$FIRST_RUN_TIMING_OUT"
fi
""",
}
REPO_FILES = {
    "scripts/run-hermetic-pytest.sh": """#!/usr/bin/env bash
sleep "${FAKE_PYTEST_SECONDS:-0}"
printf '<testsuites><testsuite name="pytest"><testcase classname="tests.test_ok" name="test_ok" time="0.1"/></testsuite></testsuites>\\n' > "$2"
""",
    "scripts/ci/mcp-conformance": "#!/usr/bin/env bash\n",
    "scripts/e2e/fresh_instance.sh": """#!/usr/bin/env bash
if [[ ${1:-} != --compose && -n ${FAKE_DURING_RUN:-} ]]; then eval "$FAKE_DURING_RUN"; fi
if [[ ${1:-} == --compose && -n ${FRESH_INSTANCE_TIMING_OUT:-} ]]; then
  printf '{"pull_s": 1.0, "build_s": 2.0, "up_s": 3.0, "bringup_s": %s}\\n' "${FAKE_COMPOSE_S:-120}" > "$FRESH_INSTANCE_TIMING_OUT"
fi
""",
    "tests/test_ok.py": "def test_ok():\n    pass\n",
    "tests/suites.toml": "",
    "tests/budgets.toml": "[ceilings]\npytest = 10\nvitest = 10\nplaywright = 10\nnode = 10\n[hygiene]\nwaitForTimeout = 0\nnetworkidle = 0\nforce_true = 0\none_shot_expect = 0\nbare_isVisible = 0\nabsolute_home_path = 0\nraw_fulfil = 0\nmalformed_stub = 0\nunprobed_endpoint = 0\n",
    "tests/requirements-hermetic.lock": "",
    "tests/compat/node/probe-gates.test.mjs": "",
    "integrations/pi/test/a.test.ts": "",
    "web/package.json": "{}\n",
    "web/package-lock.json": '{"packages":{"node_modules/example":{"hasInstallScript":true}}}\n',
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
        for script in ("scripts/run-hermetic-pytest.sh", "scripts/e2e/fresh_instance.sh", "scripts/ci/mcp-conformance"):
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
        return [str(self.repo / "scripts/ci-local"), "--force", "--browser-workers", "1", "--order-seed", "5", *extra]

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
        "cases", "compat-node", "e2e-browser", "e2e-household", "e2e-live-data", "e2e-main", "e2e-tree-unchanged", "e2e-venv",
        "fresh-compose", "fresh-native", "fresh-venv", "journey-clock", "mcp-conformance", "pi-unit", "policy", "python-hermetic",
        "web-build", "web-check", "web-npm-ci", "web-unit"])
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


def test_failed_runs_keep_logs_without_scratch(world: World) -> None:
    for _ in range(2):
        result = world.run(FAKE_VITEST_STATUS="failed")
        assert result.returncode == 1, result.stdout + result.stderr
        assert world.run_dirs() == []
        assert (world.receipts / "logs" / "web-unit.log").is_file()
        assert (world.receipts / "cases.jsonl").is_file()
        assert not (world.receipts / "test-results").exists()
    assert world.run().returncode == 0
    assert world.run_dirs() == []


def test_abandoned_and_legacy_kept_scratch_are_swept(world: World) -> None:
    root = world.home / ".tmp"
    for name in ("SIGKILLED", "OLDFAILURE"):
        directory = root / f"azizam-ci-local.{name}"
        (directory / "python").mkdir(parents=True)
        (directory / ".ci-local-run").touch()
        (directory / "python" / "copy").write_text("x")
    # A stale legacy pointer is not a reason to keep a complete failed build.
    (root / "azizam-ci-local.kept").write_text(str(root / "azizam-ci-local.OLDFAILURE"))
    untouched = root / "azizam-ci-local.UNMARKED"
    untouched.mkdir()
    assert world.run().returncode == 0
    assert world.run_dirs() == [untouched]


def test_finished_installs_are_released_before_native_journey(world: World) -> None:
    result = world.run(FAKE_DURING_RUN=(
        'test ! -d ../web-build/tests/compat/node/node_modules && '
        'test ! -d ../web-build/integrations/pi/node_modules && '
        'test -d ../web-build/web/node_modules && '
        'test -d .ci-fresh-venv && test -d ../python'
    ))
    assert result.returncode == 0, result.stdout + result.stderr
    assert world.run_dirs() == []


def test_the_gate_compacts_old_receipts_at_start(world: World) -> None:
    root = world.home / ".tmp" / "azizam-ci-receipts"
    old = []
    for index in range(25):
        run = root / f"{index:040x}"
        (run / "logs").mkdir(parents=True)
        (run / "logs" / "a.log").write_text("log")
        (run / "run.tsv").write_text("sha\tx\n")
        (run / "PASS").write_text("x")
        stamp = time.time() - (100 + index) * 3600
        os.utime(run / "run.tsv", (stamp, stamp))
        old.append(run)
    assert world.run().returncode == 0
    assert (old[0] / "logs" / "a.log").is_file() and (old[18] / "logs" / "a.log").is_file()
    assert not (old[24] / "logs").exists() and (old[24] / "PASS").is_file() and (old[24] / "run.tsv").is_file()
    assert (world.receipts / "cases.jsonl").is_file()


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
        # A scheduled run starts no new task while a gate waits (TEST_STRATEGY.md 1.2).
        waiting = world.home / ".tmp" / "azizam-ci.gate-waiting"
        assert waiting.exists()
        fcntl.flock(handle, fcntl.LOCK_UN)
        output = process.communicate(timeout=300)[0]
    assert process.returncode == 0, output
    assert "waiting for the host CI lock" in output
    assert not waiting.exists()


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


def test_a_browser_write_into_the_shared_build_fails_the_run(world: World) -> None:
    """Browser tests share one build tree: runner output goes to the linked
    test-results directory, and any other write into web/ fails the gate."""
    result = world.run(FAKE_TREE_WRITE="test-results/report.html")
    assert result.returncode == 0, result.stdout + result.stderr
    result = world.run(FAKE_TREE_WRITE="stray-output.json")
    assert result.returncode == 1, result.stdout + result.stderr
    log = (world.receipts / "logs" / "e2e-tree-unchanged.log").read_text()
    assert "browser tests wrote into the shared build tree:" in log and "web/stray-output.json" in log


# These barriers prove actual overlap without depending on scheduler speed.
# A wrong ordering fails in five seconds instead of hanging the gate.
def _await_marker(name: str) -> str:
    return (f'for attempt in {{1..100}}; do test -f "$FAKE_SYNC/{name}" && break; sleep .05; done; '
            f'test -f "$FAKE_SYNC/{name}" || exit 93; ')


def test_static_checks_overlap_browsers_after_build_in_native_slot(world: World) -> None:
    sync = world.tmp / "sync"
    sync.mkdir()
    result = world.run(
        FAKE_SYNC=str(sync),
        FAKE_WEB_BUILD='touch "$FAKE_SYNC/build"',
        FAKE_STATIC_CHECK=(
            'test -f "$FAKE_SYNC/build" || exit 94; touch "$FAKE_SYNC/static"; '
            + _await_marker("browser") + 'touch "$FAKE_SYNC/static-done"'
        ),
        FAKE_BROWSER_GATE=(
            'test -f "$FAKE_SYNC/build" || exit 95; touch "$FAKE_SYNC/browser"; '
            + _await_marker("static")
        ),
        FAKE_DURING_RUN='test -f "$FAKE_SYNC/static-done" || exit 96',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.split("\t") for line in (world.receipts / "steps.tsv").read_text().splitlines()]
    names = [row[0] for row in rows]
    assert names == sorted(names)
    assert len(names) == len(set(names)) == 20
    assert all(row[1] == "0" for row in rows)
    assert (world.receipts / "logs/web-build.log").read_text() == "build-log\n"
    assert (world.receipts / "logs/web-check.log").read_text() == "type-log\nlint-log\ndoctor-log\n"


@pytest.mark.parametrize("stage", ["TYPE", "LINT", "DOCTOR"])
def test_static_check_failure_is_fatal_with_its_own_receipt(world: World, stage: str) -> None:
    result = world.run(**{f"FAKE_{stage}_RC": "17"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/web-check.tsv").read_text().split("\t")[:2] == ["web-check", "17"]
    assert (world.receipts / "steps/e2e-main.tsv").read_text().split("\t")[1] == "0"
    assert (world.receipts / "steps/fresh-native.tsv").read_text().split("\t")[1] == "0"
    assert not (world.receipts / "PASS").exists()
    log = (world.receipts / "logs/web-check.log").read_text()
    assert "type-log" in log
    assert ("lint-log" in log) == (stage != "TYPE")
    assert ("doctor-log" in log) == (stage == "DOCTOR")


def test_failed_build_skips_browsers_but_still_checks_static_sources(world: World) -> None:
    result = world.run(FAKE_WEB_BUILD="exit 18")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/web-build.tsv").read_text().split("\t")[1] == "18"
    assert (world.receipts / "steps/web-check.tsv").read_text().split("\t")[1] == "0"
    assert not (world.receipts / "steps/e2e-main.tsv").exists()
    assert not (world.receipts / "PASS").exists()


def test_static_checks_reuse_the_existing_native_job() -> None:
    script = (ROOT / "scripts/ci-local").read_text()
    native_job = script[script.rindex('start_job "$run/fresh"'):].split('for pid in "${pids[@]}"', 1)[0]
    assert native_job.index("step web-check ") < native_job.index("step fresh-native ")
    assert native_job.count("start_job ") == 1
    # Four preparation jobs and the same browser-loop/native launch sites.
    assert script.count("\nstart_job ") == 5
    assert script.count("\n    start_job ") == 1


def test_pi_typecheck_failure_stays_fatal_and_separate_from_web(world: World) -> None:
    result = world.run(FAKE_PI_TYPE_RC="19")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/pi-unit.tsv").read_text().split("\t")[1] == "19"
    assert (world.receipts / "steps/web-check.tsv").read_text().split("\t")[1] == "0"
    assert (world.receipts / "steps/e2e-main.tsv").read_text().split("\t")[1] == "0"
    assert not (world.receipts / "PASS").exists()
    assert world.run_dirs() == []


def test_static_check_write_into_shared_build_is_fatal(world: World) -> None:
    result = world.run(FAKE_STATIC_CHECK='touch "$PWD/static-output.json"')
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/web-check.tsv").read_text().split("\t")[1] == "0"
    assert (world.receipts / "steps/e2e-tree-unchanged.tsv").read_text().split("\t")[1] == "1"
    assert "web/static-output.json" in (world.receipts / "logs/e2e-tree-unchanged.log").read_text()
    assert not (world.receipts / "PASS").exists()
    assert world.run_dirs() == []


def load_gate_cost():
    spec = importlib.util.spec_from_file_location("gate_cost", ROOT / "scripts/ci/gate_cost.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_step_records_wall_cpu_and_peak_rss(world: World) -> None:
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.split("\t") for line in (world.receipts / "steps.tsv").read_text().splitlines()]
    assert len(rows) == 20
    for name, rc, seconds, wall, cpu, peak_kb in rows:
        assert rc == "0" and seconds.isdigit(), name
        assert re.fullmatch(r"\d+\.\d{2}", wall) and re.fullmatch(r"\d+\.\d{2}", cpu), name
        assert peak_kb.isdigit() and int(peak_kb) > 0, name


def test_a_skipped_compose_step_keeps_the_same_columns(world: World) -> None:
    result = world.run("--skip-compose")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/fresh-compose.tsv").read_text() == "fresh-compose\tskipped\t0\t-\t-\t-\n"


def test_cost_json_has_totals_and_the_critical_path(world: World) -> None:
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    gate_cost = load_gate_cost()
    cost = json.loads((world.receipts / "cost.json").read_text())
    steps = cost["steps"]
    assert cost["measure"] == "gnu-time"
    assert set(steps) == set(gate_cost.GATE_DEPS)
    assert cost["totals"]["cpu_s"] == pytest.approx(sum(step["cpu_s"] for step in steps.values()), abs=0.01)
    assert cost["totals"]["max_step_rss_kb"] == max(step["max_rss_kb"] for step in steps.values())
    path = cost["critical_path"]["steps"]
    assert path[-1] == "policy"
    # Each step on the path waits for the one before it.
    assert all(before in gate_cost.GATE_DEPS[after] for before, after in zip(path, path[1:]))
    assert cost["critical_path"]["wall_s"] == pytest.approx(sum(steps[name]["wall_s"] for name in path), abs=0.01)
    assert cost["elapsed_s"] + 1 >= cost["critical_path"]["wall_s"]  # elapsed has whole seconds


def test_the_critical_path_follows_pytest_when_it_is_slowest(world: World) -> None:
    result = world.run(FAKE_PYTEST_SECONDS="2.5", FAKE_WEB_BUILD="sleep 0.5", FAKE_BROWSER_GATE="sleep 0.3")
    assert result.returncode == 0, result.stdout + result.stderr
    path = json.loads((world.receipts / "cost.json").read_text())["critical_path"]["steps"]
    assert path == ["python-hermetic", "e2e-tree-unchanged", "journey-clock", "cases", "policy"]


def test_the_critical_path_follows_the_web_build_when_it_is_slowest(world: World) -> None:
    result = world.run(FAKE_WEB_BUILD="sleep 2.5", FAKE_BROWSER_GATE="sleep 0.3")
    assert result.returncode == 0, result.stdout + result.stderr
    path = json.loads((world.receipts / "cost.json").read_text())["critical_path"]["steps"]
    assert path[:2] == ["web-npm-ci", "web-build"]
    assert path[2] in ("e2e-main", "e2e-live-data", "e2e-household")  # the three browser lanes tie
    assert path[3:] == ["e2e-tree-unchanged", "journey-clock", "cases", "policy"]


def test_the_cost_table_is_sorted_by_wall_clock(world: World) -> None:
    result = world.run(FAKE_PYTEST_SECONDS="1.5", FAKE_WEB_BUILD="sleep 0.5")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    header = next(line for line in lines if line.startswith("step "))
    assert header.split() == ["step", "rc", "wall_s", "cpu_s", "peak_mb"]
    rows = []
    for line in lines[lines.index(header) + 1:]:
        if line.startswith("critical path"):
            break
        rows.append(line.split())
    assert len(rows) == 20
    walls = [float(row[2]) for row in rows]
    assert walls == sorted(walls, reverse=True)
    assert rows[0][0] == "python-hermetic"


def test_without_gnu_time_a_wait4_fallback_measures_each_step(world: World) -> None:
    result = world.run(AZIZAM_CI_TIME=str(world.tmp / "no-such-time"), FAKE_TYPE_RC="17")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/web-check.tsv").read_text().split("\t")[:2] == ["web-check", "17"]
    # The fallback adds nothing to the step's log: no GNU time message.
    assert (world.receipts / "logs/web-check.log").read_text() == "type-log\n"
    cost = json.loads((world.receipts / "cost.json").read_text())
    assert cost["measure"] == "wait4"
    assert cost["steps"]["web-check"]["rc"] == 17
    assert all(step["cpu_s"] is not None and step["max_rss_kb"] > 0 for step in cost["steps"].values())


def test_the_wait4_wrapper_returns_a_signalled_steps_shell_status(tmp_path: Path) -> None:
    cost = tmp_path / "step.cost"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/gate_cost.py"), "run", str(cost), "--",
                             "sh", "-c", "kill -TERM $$"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 143  # 128 + SIGTERM, as the shell reports it
    assert result.stderr == ""
    wall, user, system, peak_kb = cost.read_text().split()
    assert float(wall) >= 0 and float(user) >= 0 and float(system) >= 0 and int(peak_kb) > 0


def test_gate_cost_prints_the_median_and_worst_of_the_last_runs(world: World) -> None:
    root = world.tmp / "receipts"
    for index, (wall, started) in enumerate([(10, "2026-10-01T00:00:00Z"), (20, "2026-10-02T00:00:00Z"),
                                             (90, "2026-10-03T00:00:00Z")]):
        run = root / f"{index:040x}"
        run.mkdir(parents=True)
        (run / "run.tsv").write_text(f"started\t{started}\nfinished\t{started}\n")
        (run / "steps.tsv").write_text(f"python-hermetic\t0\t{wall}\t{wall}.00\t1.00\t2048\n")
    for runs, median in ((2, "55.00"), (3, "20.00")):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/gate-cost"), "-n", str(runs),
                                 "--receipts", str(root)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        row = next(line.split() for line in result.stdout.splitlines() if line.startswith("python-hermetic "))
        assert row[1] == str(runs) and row[2] == median and row[3] == "90.00"


def test_critical_path_takes_the_longest_chain_and_ignores_unmeasured_predecessors() -> None:
    gate_cost = load_gate_cost()
    durations = {"a": 1.0, "b": 2.0, "c": 4.0, "d": 0.5, "e": 6.0}
    deps = {"a": (), "b": ("a",), "c": ("a", "b"), "d": ("missing",), "e": ()}
    assert gate_cost.critical_path(durations, deps) == (["a", "b", "c"], 7.0)


@pytest.mark.parametrize(("existing", "version", "install_rc"), [
    (False, "3.12.13", 0),
    (True, "3.12.13", 0),
    (False, "3.12.12", 0),
    (True, "3.12.12", 0),
    (False, "3.12.13", 19),
])
def test_hermetic_runner_self_heals_pinned_python(
    tmp_path: Path, existing: bool, version: str, install_rc: int,
) -> None:
    repo, home, bin_dir = (tmp_path / name for name in ("repo", "home", "bin"))
    (repo / "scripts/lib").mkdir(parents=True)
    (repo / "tests").mkdir()
    bin_dir.mkdir()
    runner = repo / "scripts/run-hermetic-pytest.sh"
    shutil.copy2(ROOT / "scripts/run-hermetic-pytest.sh", runner)
    (repo / "scripts/lib/scratch.sh").write_text("")
    for relative in ("tests/requirements-hermetic.lock", "tests/requirements-hermetic.in",
                     "requirements.txt", "proxy_config.yml"):
        (repo / relative).write_text("")
    log = bin_dir / "calls"
    store = home / ".tmp/azizam-hermetic-python"
    python = store / "cpython-3.12.13-linux-x86_64-gnu/bin/python3.12"
    interpreter = f'#!/bin/bash\n[[ $1 == --version ]] || exit 91\necho "Python {version}"\n'

    def script(path: Path, body: str) -> None:
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    script(repo / "scripts/hermetic_source_preflight.sh", ":")
    for name in ("bwrap", "git", "ip"):
        script(bin_dir / name, "exit 92")  # No sandbox, checkout or network is needed.
    script(bin_dir / "uv", f"""
calls="$(dirname "$0")/calls"
printf '%s\\n' "$@" >> "$calls"
[[ $# == 5 && $1 == python && $2 == install && $3 == 3.12.13 && $4 == --install-dir ]] || exit 93
exit_code={install_rc}
((exit_code == 0)) || exit "$exit_code"
root="$5/cpython-3.12.13-linux-x86_64-gnu"
mkdir -p "$root/bin"
cat > "$root/bin/python3.12" <<'PYTHON'
{interpreter}PYTHON
chmod +x "$root/bin/python3.12"
""")
    # Stop at the first operation after bootstrap; no packages are installed.
    script(bin_dir / "sha256sum", 'echo continued >> "$(dirname "$0")/calls"; exit 73')
    (bin_dir / "awk").symlink_to(next(filter(None, map(shutil.which, ("mawk", "gawk", "original-awk")))))
    if existing:
        python.parent.mkdir(parents=True)
        python.write_text(interpreter)
        python.chmod(0o755)
    result = subprocess.run(
        ["bash", str(runner)], text=True, capture_output=True, timeout=10,
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "AZIZAM_PRIORITY_SET": "1"},
    )
    calls = log.read_text().splitlines() if log.exists() else []
    expected = [] if existing else ["python", "install", "3.12.13", "--install-dir", str(store)]
    if install_rc:
        assert result.returncode == install_rc, result.stderr
        assert not python.exists()
    elif version != "3.12.13":
        assert result.returncode == 1, result.stderr
        assert "requires Python 3.12.13" in result.stderr
    else:
        assert result.returncode == 73, result.stderr
        assert python.is_file()
        expected.append("continued")
    assert calls == expected
    assert result.stderr.count("hermetic pytest: reinstalling Python 3.12.13") == int(not existing)
def test_the_first_run_journey_is_timed_end_to_end_into_cost_json(world: World) -> None:
    result = world.run(FAKE_COMPOSE_S="200", FAKE_FIRST_RUN_S="12.5")
    assert result.returncode == 0, result.stdout + result.stderr
    cost = json.loads((world.receipts / "cost.json").read_text())
    assert cost["journey"] == {"compose_bringup_s": 200.0, "first_run_ui_s": 12.5, "total_s": 212.5, "limit_s": 600, "passed": True}
    # One file: gate_cost.py's schema, with the journey section journey-clock wrote kept.
    assert cost["schema"] == 1 and set(cost["steps"]) >= {"journey-clock", "fresh-compose", "e2e-main"}
    assert cost["steps"]["journey-clock"]["rc"] == 0
    path = cost["critical_path"]["steps"]
    assert path[-3:] == ["journey-clock", "cases", "policy"]


def test_a_journey_over_ten_minutes_fails_the_gate_but_keeps_cost_json(world: World) -> None:
    result = world.run(FAKE_COMPOSE_S="595", FAKE_FIRST_RUN_S="5")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/journey-clock.tsv").read_text().split("\t")[1] == "1"
    journey = json.loads((world.receipts / "cost.json").read_text())["journey"]
    assert journey["total_s"] == 600.0 and journey["limit_s"] == 600 and journey["passed"] is False
    assert not (world.receipts / "PASS").exists()


def test_a_journey_with_no_browser_time_fails_the_gate(world: World) -> None:
    result = world.run(FAKE_NO_FIRST_RUN_TIME="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (world.receipts / "steps/journey-clock.tsv").read_text().split("\t")[1] == "1"
    assert "journey" not in json.loads((world.receipts / "cost.json").read_text())


def test_target_without_signoff_runs_budget_comparison(world: World) -> None:
    result = world.run("--target", "main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (world.receipts / "base-budgets.toml").is_file()
    assert not world.gh_log.exists()


def test_dependencies_are_private_and_warm_runs_still_test(world: World) -> None:
    first = world.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "cache miss: node" in (world.receipts / "logs/web-npm-ci.log").read_text()
    assert "cache miss: venv" in (world.receipts / "logs/e2e-venv.log").read_text()
    cases = (world.receipts / "cases.jsonl").read_text()
    second = world.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "cache hit: node" in (world.receipts / "logs/web-npm-ci.log").read_text()
    assert "cache hit: venv" in (world.receipts / "logs/e2e-venv.log").read_text()
    assert (world.receipts / "cases.jsonl").read_text() == cases
    cache = world.home / "Documents/azizam/ci-cache"
    assert not list(cache.glob("node-*/environment/.cache"))
    snapshot = next(cache.glob("node-*/environment/.bin/playwright"))
    snapshot.write_text("corrupted cache content")
    repaired = world.run()
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert "cache miss: node" in (world.receipts / "logs/web-npm-ci.log").read_text()
    assert snapshot.read_text().startswith("#!/usr/bin/env bash")

    # Retention is per kind and must not evict another active cache key.
    older = cache / ("node-" + "0" * 64)
    recent = cache / ("node-" + "1" * 64)
    for index, entry in enumerate((older, recent), 1):
        entry.mkdir()
        (entry / "complete").write_text("old fixture")
        os.utime(entry / "complete", (index, index))
    with (cache / f"{older.name}.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = world.run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert older.is_dir()  # an active publisher/reader is never removed
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not older.exists() and recent.is_dir()
    assert len(list(cache.glob("node-*/complete"))) == 2
    assert len(list(cache.glob("venv-*/complete"))) == 1


def test_cache_does_not_publish_failed_install(world: World) -> None:
    npm = world.bin / "npm"
    npm.write_text(npm.read_text().replace('mkdir -p node_modules/.bin', 'exit 41; mkdir -p node_modules/.bin'))
    result = world.run()
    assert result.returncode == 1
    assert not list((world.home / "Documents/azizam/ci-cache").glob("node-*/complete"))


def test_lockfile_change_invalidates_node_cache(world: World) -> None:
    assert world.run().returncode == 0
    (world.repo / "web/package-lock.json").write_text('{"packages":{"node_modules/example":{"hasInstallScript":true}},"changed":true}\n')
    _git(world.repo, "add", "web/package-lock.json")
    _git(world.repo, "commit", "-qm", "change dependency lock")
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cache miss: node" in (world.receipts / "logs/web-npm-ci.log").read_text()

    (world.repo / "install-input.txt").write_text("dependency hook input changed")
    _git(world.repo, "add", "install-input.txt")
    _git(world.repo, "commit", "-qm", "change project input read by dependency hooks")
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cache miss: node" in (world.receipts / "logs/web-npm-ci.log").read_text()
