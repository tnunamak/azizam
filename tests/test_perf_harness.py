"""The performance harness judges results against budgets.toml and reports honestly.

The timed run itself needs a copy of the live databases, so it is a nightly task
(scripts/test-scheduled, task "perf"). These tests cover what can be checked without one:
the budget file, the judging, and the red-issue flow with a fake `gh`.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PERF = ROOT / "scripts" / "perf"


def _load(name: str):
    sys.path.insert(0, str(PERF))
    spec = importlib.util.spec_from_file_location(f"perf_{name}", PERF / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check")
run = _load("run")
BUDGETS = tomllib.loads((PERF / "budgets.toml").read_text(encoding="utf-8"))


def _api(route="/me/api/home", who="owner", p95=40.0, sql=30, connects=3, proc=0):
    return {"route": route, "who": who, "status": 200, "p50_ms": 20.0, "p95_ms": p95, "bytes": 10,
            "sql": sql, "connects": connects, "subproc": proc}


def _ui(route="/library", who="owner", ready=300, requests=40, kb=400, long=0, final=None):
    return {"route": route, "who": who, "final": final or route + "/", "ready_ms": ready, "requests": requests,
            "bytes": kb * 1024, "long_task_ms": long}


def test_the_budget_file_sets_every_limit_the_checker_reads() -> None:
    assert {"p95_ms", "statements", "connections", "processes"} <= set(BUDGETS["api"])
    assert {"ready_ms", "long_task_ms", "requests", "kilobytes"} <= set(BUDGETS["ui"])
    assert BUDGETS["api"]["processes"] == 0 and BUDGETS["api"]["p95_ms"] <= 100 and BUDGETS["ui"]["ready_ms"] <= 500


def test_a_cold_read_that_stalls_the_event_loop_past_its_limit_is_reported() -> None:
    assert BUDGETS["loop"]["max_stall_ms"] <= 100
    stalled = {"route": "/admin/api/status (after idle)", "who": "owner", "stall_ms": 150.0}
    assert [v["metric"] for v in check.loop_violations([stalled], BUDGETS["loop"])] == ["stall_ms"]
    assert check.loop_violations([{**stalled, "stall_ms": 40.0}], BUDGETS["loop"]) == []
    assert check.loop_violations([_api()], BUDGETS["loop"]) == [], "a row without a stall is not a loop finding"


def test_a_route_within_every_limit_is_not_reported() -> None:
    assert check.api_violations([_api()], BUDGETS["api"], counts_only=False) == []


@pytest.mark.parametrize(("row", "metric"), [
    (_api(p95=180.0), "p95_ms"), (_api(sql=101), "statements"), (_api(connects=21), "connections"),
    (_api(proc=1), "processes"),
])
def test_each_api_limit_is_enforced(row, metric) -> None:
    found = check.api_violations([row], BUDGETS["api"], counts_only=False)
    assert [v["metric"] for v in found] == [metric]


def test_counts_only_ignores_time_but_not_counts() -> None:
    assert check.api_violations([_api(p95=900.0)], BUDGETS["api"], counts_only=True) == []
    assert check.api_violations([_api(proc=3)], BUDGETS["api"], counts_only=True)


def test_a_route_with_its_own_limit_uses_it() -> None:
    budget = {**BUDGETS["api"], "routes": {"/slow": {"p95_ms": 400, "reason": "test"}}}
    assert check.api_violations([_api("/slow", p95=300.0)], budget, counts_only=False) == []
    assert check.api_violations([_api("/slow", p95=500.0)], budget, counts_only=False)


@pytest.mark.parametrize(("row", "metric"), [
    (_ui(ready=900), "ready_ms"), (_ui(requests=150), "requests"), (_ui(kb=4000), "kilobytes"), (_ui(long=500), "long_task_ms"),
])
def test_each_page_limit_is_enforced(row, metric) -> None:
    assert [v["metric"] for v in check.ui_violations([row], BUDGETS["ui"], counts_only=False)] == [metric]


def test_a_page_that_never_showed_content_is_the_worst_finding() -> None:
    row = _ui(ready=None)
    row["ready_ms"] = None
    found = check.ui_violations([row, _ui(ready=900)], BUDGETS["ui"], counts_only=False)
    assert found[0]["value"] == "never" and found[0]["over"] > found[1]["over"]


def test_a_page_the_viewer_was_redirected_from_is_not_judged() -> None:
    assert check.ui_violations([_ui(ready=9000, final="/")], BUDGETS["ui"], counts_only=False) == []


def test_the_table_ranks_the_worst_first() -> None:
    found = check.api_violations([_api(p95=150.0), _api("/b", p95=900.0)], BUDGETS["api"], counts_only=False)
    assert check.table(found).splitlines()[2].startswith("| api | /b")


class FakeGh:
    def __init__(self, open_issues: list[str]) -> None:
        self.open_issues, self.calls = open_issues, []

    def __call__(self, *args: str):
        self.calls.append(args)
        out = "\n".join(self.open_issues) if args[:2] == ("issue", "list") else ""
        return subprocess.CompletedProcess(args, 0, out, "")


def test_a_red_run_opens_one_issue_and_a_second_comments_on_it(tmp_path: Path) -> None:
    found = check.api_violations([_api(p95=300.0)], BUDGETS["api"], counts_only=False)
    gh = FakeGh([])
    assert run.open_issue(found, "abcdef0123456789", 3.0, tmp_path, gh=gh) == "opened a new issue"
    create = next(call for call in gh.calls if call[:2] == ("issue", "create"))
    assert "perf-red" in create and "--label" in create
    body = (tmp_path / "issue-body.md").read_text(encoding="utf-8")
    assert "/me/api/home" in body and "abcdef0123" in body
    again = FakeGh(["41"])
    assert run.open_issue(found, "abcdef0123456789", 3.0, tmp_path, gh=again) == "commented on #41"
    assert not any(call[:2] == ("issue", "create") for call in again.calls)


def test_the_nightly_runs_the_perf_task_and_the_policy_knows_it() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    text = (ROOT / "scripts" / "test-scheduled").read_text(encoding="utf-8")
    assert '"perf"' in text and "def task_perf" in text and "--open-issue" in text


def _home(desktop_cold=400, mobile_cold=2200, mobile_warm=900, js_kb=240, js_req=29, plain=0):
    def run(content, **extra):
        return {"content_ms": content, "js_wire_kb": js_kb, "js_requests": js_req, "js_uncompressed": plain, **extra}
    return {"profiles": {"desktop": {"cold": run(desktop_cold), "warm": run(300)},
                         "mobile": {"cold": run(mobile_cold), "warm": run(mobile_warm)}}}


def test_home_within_its_budget_is_not_reported() -> None:
    assert check.home_violations(_home(), BUDGETS["home"]) == []


def test_each_home_limit_is_enforced() -> None:
    cases = {"desktop_cold_ms": _home(desktop_cold=900), "mobile_cold_ms": _home(mobile_cold=4000),
             "mobile_warm_ms": _home(mobile_warm=2000), "js_wire_kb": _home(js_kb=500),
             "js_requests": _home(js_req=60), "uncompressed_js": _home(plain=2)}
    for metric, report in cases.items():
        assert [v["metric"] for v in check.home_violations(report, BUDGETS["home"])] == [metric]


def test_a_home_that_never_showed_content_is_a_violation() -> None:
    report = _home(mobile_cold=None)
    assert check.home_violations(report, BUDGETS["home"])[0]["value"] == "never"


# -- the fixture's copy of the owner's data never outlives the run ----------------------

def test_the_fixture_copy_is_a_scratch_directory_removed_on_exit_and_swept_when_orphaned(tmp_path, monkeypatch) -> None:
    import os
    import subprocess

    serve = _load("serve")
    monkeypatch.setenv("AZIZAM_CI_SCRATCH", str(tmp_path))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    started = serve.scratch_owner.start_time(dead.pid)
    dead.wait()
    orphan = tmp_path / f"{serve.SCRATCH_PREFIX}orphan"
    orphan.mkdir()
    (orphan / ".scratch-owner").write_text(f"{dead.pid}-{started}\n")
    (orphan / "admin.db").write_text("a copy of someone's data")
    live_neighbour = tmp_path / "azizam-other-tool"
    live_neighbour.mkdir()
    seen: list[Path] = []

    def fake_serve(state, port, source, clone=None):
        seen.append(state)
        assert state.parent == tmp_path and state.name.startswith(serve.SCRATCH_PREFIX)
        assert (state / ".scratch-owner").read_text().startswith(f"{os.getpid()}-")
        assert not orphan.exists(), "a dead owner's copy was not swept at start"
        (state / "admin.db").write_text("copy")
        raise RuntimeError("the run failed")

    monkeypatch.setattr(serve, "serve", fake_serve)
    monkeypatch.setattr(sys, "argv", ["serve.py", "--port", "18700"])
    monkeypatch.setattr(serve.scratch_owner, "exit_on_signals", lambda: None)
    with pytest.raises(RuntimeError):
        serve.main()
    assert seen and not seen[0].exists(), "the copy outlived a failed run"
    assert live_neighbour.is_dir()


def test_there_is_no_way_to_keep_a_copy() -> None:
    text = (PERF / "serve.py").read_text(encoding="utf-8")
    assert "--state" not in text and "--reuse" not in text and "--prepare-only" not in text
    assert "scratch_owner.sweep" in text and "rmtree(state" in text


def test_a_member_home_over_its_request_budget_is_a_violation() -> None:
    report = _home()
    report["who"] = "member"
    report["profiles"]["desktop"]["cold"]["member_api_requests"] = BUDGETS["home"]["member_api_requests"] + 1
    assert [v["metric"] for v in check.home_violations(report, BUDGETS["home"])] == ["member_api_requests"]


def test_an_owner_home_is_not_judged_on_member_requests() -> None:
    report = _home()
    report["who"] = "owner"
    assert check.home_violations(report, BUDGETS["home"]) == []


def test_the_budget_table_has_no_route_exceptions() -> None:
    """Every route meets the defaults in [api]. A route with its own limits hides a slow route, so the table stays empty."""
    assert BUDGETS["api"].get("routes", {}) == {}


def test_judge_includes_stalls_from_both_entry_points() -> None:
    rows = [{"route": "/cold", "who": "owner", "stall_ms": BUDGETS["loop"]["max_stall_ms"] + 1}]
    expected = check.loop_violations(rows, BUDGETS["loop"])
    assert expected
    assert check.judge(rows, [], None, BUDGETS, False) == expected
    assert run.check.judge(rows, [], None, BUDGETS, False) == expected
    assert check.judge(rows, [], None, BUDGETS, True) == []


@pytest.mark.parametrize("value", [float("nan"), None, True, "12", float("inf"), -float("inf")])
def test_judge_reports_invalid_measurements(value) -> None:
    cases = [
        (check.api_violations, [_api(p95=value)], BUDGETS["api"], False, "p95_ms"),
        (check.ui_violations, [{**_ui(), "bytes": value}], BUDGETS["ui"], False, "kilobytes"),
    ]
    for fn, rows, budget, counts_only, metric in cases:
        found = fn(rows, budget, counts_only)
        assert [(v["metric"], v["value"], v["over"]) for v in found] == [(metric, "invalid", 99)]
    stalled = {"route": "/cold", "who": "owner", "stall_ms": value}
    assert check.loop_violations([stalled], BUDGETS["loop"])[0]["value"] == "invalid"
    assert check.home_violations(_home(js_req=value), BUDGETS["home"])[0]["value"] == "invalid"


@pytest.mark.parametrize("counts_only", [False, True])
def test_judge_error_and_skipped_rows(counts_only) -> None:
    row = {"route": "/hung", "who": "owner"}
    found = check.judge([{**row, "error": "reset"}], [], None, BUDGETS, counts_only)
    assert [(v["metric"], v["value"], v["over"]) for v in found] == [("error", "error", 99)]
    assert check.judge([{**row, "skipped": "unavailable"}], [], None, BUDGETS, counts_only) == []


def test_judge_counts_only_keeps_home_counts_not_timings() -> None:
    home = _home(desktop_cold=None, mobile_cold=float("nan"), mobile_warm=999999,
                 js_req=BUDGETS["home"]["js_requests"] + 1)
    assert [v["metric"] for v in check.judge([], [], home, BUDGETS, True)] == ["js_requests"]
    home["who"] = "member"
    home["profiles"]["desktop"]["cold"]["member_api_requests"] = BUDGETS["home"]["member_api_requests"] + 1
    assert [v["metric"] for v in check.judge([], [], home, BUDGETS, True)] == ["js_requests", "member_api_requests"]


@pytest.mark.parametrize("counts_only", [False, True])
def test_judge_within_budget(counts_only) -> None:
    assert check.judge([_api()], [_ui()], _home(), BUDGETS, counts_only) == []


def test_run_main_uses_shared_judge_for_stalls(tmp_path, monkeypatch) -> None:
    rows = [{"route": "/cold", "who": "owner", "stall_ms": BUDGETS["loop"]["max_stall_ms"] + 1}]
    monkeypatch.setattr(run, "HERE", tmp_path)
    (tmp_path / "budgets.toml").write_text("")
    monkeypatch.setattr(run.tomllib, "loads", lambda _: BUDGETS)
    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "abc", ""))
    monkeypatch.setattr(run, "start_fixture", lambda *a: (object(), {}))
    monkeypatch.setattr(run, "stop", lambda process: None)
    monkeypatch.setattr(run.scratch_owner, "sweep", lambda *a: None)
    monkeypatch.setattr(run.api_bench, "run", lambda manifest: {"rows": rows})
    monkeypatch.setattr(run.os, "getloadavg", lambda: (0, 0, 0))
    monkeypatch.setenv("AZIZAM_CI_SCRATCH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["run.py", "--no-ui", "--out", str(tmp_path / "out")])
    assert run.main() == 1
    import json
    result = json.loads((tmp_path / "out" / "result.json").read_text())
    assert result["violations"] == check.judge(rows, [], None, BUDGETS, False)
