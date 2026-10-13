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
