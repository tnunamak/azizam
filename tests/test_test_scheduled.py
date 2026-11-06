"""scripts/test-scheduled: which files it runs, when it skips, and what counts as a pass.

The slow parts (a disposable gateway, the sweep) run in the nightly itself;
these tests cover its decisions: the file list is the suites.toml data,
a skipped or silent probe is never a pass, a busy host or media queue gives
SKIPPED with a nonzero exit, and a waiting gate keeps a task from starting.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
import socket
from pathlib import Path

import pytest

from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, scenario_from_data  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test-scheduled"
_loader = importlib.machinery.SourceFileLoader("test_scheduled", str(SCRIPT))
scheduled = importlib.util.module_from_spec(importlib.util.spec_from_loader("test_scheduled", _loader))
sys.modules["test_scheduled"] = scheduled  # its dataclasses look their module up here
_loader.exec_module(scheduled)
_policy_spec = importlib.util.spec_from_file_location("ci_policy", ROOT / "scripts" / "ci" / "policy.py")
policy = importlib.util.module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(policy)

SUITES = tomllib.loads((ROOT / "tests" / "suites.toml").read_text(encoding="utf-8"))


def test_policy_and_script_agree_on_the_task_names() -> None:
    assert set(scheduled.TASKS) == policy.SCHEDULED_TASKS


def test_the_files_it_runs_are_the_scheduled_entries() -> None:
    listed = scheduled.scheduled_files(SUITES)
    flat = sorted(path for paths in listed.values() for path in paths)
    assert flat == sorted(entry["path"] for entry in SUITES["scheduled"])
    assert listed["sdk-probes"] and listed["sweep"] == ["web/e2e/sweep/sweep.spec.ts"]
    assert all((ROOT / path).is_file() for path in flat)


def test_an_unknown_task_in_suites_toml_stops_the_script() -> None:
    with pytest.raises(SystemExit, match="nobody"):
        scheduled.scheduled_files({"scheduled": [{"path": "tests/x.py", "task": "nobody"}]})


def _row(file: str, status: str = "passed", root: Path = ROOT) -> dict:
    return {"file": str(root / file), "status": status}


FILES = ["tests/compat/node/a.test.mjs", "tests/compat/node/b.test.mjs"]


@pytest.mark.parametrize(("rows", "status", "reason"), [
    ([_row(FILES[0]), _row(FILES[1])], "PASS", ""),
    ([_row(FILES[0]), _row(FILES[1], "failed")], "FAIL", FILES[1]),
    ([_row(FILES[0]), _row(FILES[1], "timedOut")], "FAIL", FILES[1]),
    ([_row(FILES[0])], "INCOMPLETE", "no case ran in " + FILES[1]),
    ([_row(FILES[0]), _row(FILES[1], "skipped")], "INCOMPLETE", "skipped cases in " + FILES[1]),
    ([_row(FILES[0]), _row(FILES[1]), _row(FILES[1], "skipped")], "INCOMPLETE", "skipped cases in " + FILES[1]),
    ([_row(FILES[0]), _row(FILES[1], root=Path("/elsewhere"))], "INCOMPLETE", "no case ran in " + FILES[1]),
    ([], "INCOMPLETE", "no case ran in"),
], ids=["all-passed", "failed", "timed-out", "silent-file", "all-skipped", "some-skipped", "wrong-tree", "no-cases"])
def test_a_pass_needs_every_listed_file_to_pass_with_nothing_skipped(rows, status, reason) -> None:
    got, why, counts = scheduled.judge_files(rows, FILES, ROOT)
    assert got == status
    assert reason in why
    assert set(counts) == set(FILES)


def _queue(fakes: FakeUpstreams, *bodies: dict) -> FakeUpstream:
    """The media engine's queue endpoint on loopback, answering ``bodies`` in order."""
    scenario = scenario_from_data({
        "protocol": "comfyui", "name": "media-queue",
        "provenance": {"source": "hand-written", "reference": "ComfyUI GET /queue: queue_running and queue_pending lists"},
        "exchanges": [{"id": f"queue-{n}", "match": {"method": "GET", "path": "/queue"}, "response": {"json": body}}
                      for n, body in enumerate(bodies, 1)],
    })
    return fakes.loopback(scenario)


def test_a_running_or_pending_media_job_is_in_flight(fake_upstreams: FakeUpstreams) -> None:
    fake = _queue(fake_upstreams, {"queue_running": [], "queue_pending": [["1", "p"]]},
                  {"queue_running": [], "queue_pending": []})
    assert scheduled.media_job_in_flight(f"{fake.base_url}/queue")[0] is True
    assert scheduled.media_job_in_flight(f"{fake.base_url}/queue")[0] is False


def test_a_media_queue_that_does_not_answer_is_not_a_job() -> None:
    with socket.socket() as closed:  # a port that was bound, then closed: nothing answers
        closed.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{closed.getsockname()[1]}/queue"
    busy, note = scheduled.media_job_in_flight(url)
    assert busy is False and "not answering" in note
    assert scheduled.media_job_in_flight("") == (False, "no media queue configured")


def _nightly(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    receipts = tmp_path / "receipts"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "nightly", "--receipts-dir", str(receipts)],
        env={**os.environ, "AZIZAM_PRIORITY_SET": "1", **env}, text=True, capture_output=True, timeout=60)
    [directory] = receipts.iterdir()
    return result, json.loads((directory / "receipt.json").read_text())


def test_a_loaded_host_is_skipped_not_passed(tmp_path: Path) -> None:
    result, receipt = _nightly(tmp_path, SCHEDULED_MAX_LOAD="0", SCHEDULED_MEDIA_QUEUE_URL="")
    print(result.stdout)
    assert result.returncode == 3
    assert receipt["status"] == "SKIPPED" and "load" in receipt["reason"]
    assert receipt["tasks"] == []
    assert "nightly: SKIPPED" in result.stdout


def test_a_media_job_in_flight_is_skipped_not_passed(tmp_path: Path, fake_upstreams: FakeUpstreams) -> None:
    fake = _queue(fake_upstreams, {"queue_running": [["1", "p"]], "queue_pending": []})
    result, receipt = _nightly(tmp_path, SCHEDULED_MAX_LOAD="1000000", SCHEDULED_MEDIA_QUEUE_URL=f"{fake.base_url}/queue")
    print(result.stdout)
    assert result.returncode == 3
    assert receipt["status"] == "SKIPPED" and "media job is in flight" in receipt["reason"]
    assert receipt["tasks"] == []


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scheduled, "LOCK", tmp_path / "ci.lock")
    monkeypatch.setattr(scheduled, "GATE_WAITING", tmp_path / "gate-waiting")
    monkeypatch.setattr(scheduled, "LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(scheduled, "POLL_S", 0.02)
    return tmp_path


def test_a_task_takes_the_lock_and_releases_it(paths: Path) -> None:
    with scheduled.Lock():
        with pytest.raises(scheduled.LockTimeout, match="LOCK_TIMEOUT"):
            with scheduled.Lock():
                pass
    with scheduled.Lock() as lock:  # released on exit
        assert lock.wait_s < 0.3


def test_a_waiting_gate_keeps_a_task_from_starting(paths: Path) -> None:
    (paths / "gate-waiting").write_text("pid 1\n")
    with pytest.raises(scheduled.LockTimeout, match="gate has been waiting"):
        with scheduled.Lock():
            pytest.fail("the task started while a gate waited")
    (paths / "gate-waiting").unlink()
    with scheduled.Lock():
        pass


def test_the_systemd_units_run_the_nightly_at_four() -> None:
    service = (ROOT / "docs/ops/azizam-test-nightly.service").read_text()
    timer = (ROOT / "docs/ops/azizam-test-nightly.timer").read_text()
    assert "scripts/test-scheduled nightly" in service
    assert "OnCalendar=*-*-* 04:00:00" in timer
    # Only SKIPPED (3) is not a unit failure; FAIL (1) and INCOMPLETE (4) are.
    assert [line for line in service.splitlines() if line.startswith("SuccessExitStatus")] == ["SuccessExitStatus=3"]
    assert scheduled.EXIT == {"PASS": 0, "FAIL": 1, "SKIPPED": 3, "INCOMPLETE": 4}
