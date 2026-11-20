"""``report_problem`` reaches every client shape: the MCP tool, a REST route and a pointer from ``run`` and
``capabilities``. A client that only drives ``run`` with targets from ``capabilities`` must still learn how to file.

The two doors share one core (``PROBLEM_REPORTS.submit``), so these tests file through one door and read through the other."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import problem_reports
from gateway.problem_report_tool import HOW_TO_REPORT, NOT_A_TARGET_NAMES, REST_PATH
from tests.support.access import authenticated
from tests.test_mcp_server import call as mcp_call, _build_env

async def _wired(request, auth, args):
    return {"status": "recorded"}


def _env(**extra):
    return _build_env([], problem_report=_wired, **extra)


CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
ROUTE = "/gateway/v1/problem-reports"
SUMMARY = {"category": "workaround", "summary": "speech ignores the speed input", "workaround": "re-ran with tts-fast"}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    svc = problem_reports.ProblemReports(Path(tmp_path) / "problem_reports.db", destinations=lambda: [])
    monkeypatch.setattr(proxy, "PROBLEM_REPORTS", svc)
    return svc


@pytest.fixture()
def client():
    return TestClient(proxy.app)


def _tool(client: TestClient, name: str, arguments: dict) -> dict:
    resp = client.post("/mcp", json={**CALL, "params": {"name": name, "arguments": arguments}},
                       headers={"mcp-protocol-version": "2025-11-25"})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def test_a_key_files_a_report_over_rest_and_gets_what_the_tool_gets(client, store):
    with authenticated():
        resp = client.post(ROUTE, json=SUMMARY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "recorded" and body["duplicate"] is False and body["count"] == 1
    assert body["report_id"] and set(body) >= {"known_workaround", "note"}
    row = store.get(body["report_id"])
    assert row["summary"] == SUMMARY["summary"] and row["workaround_used"] == "re-ran with tts-fast"


def test_the_rest_route_and_the_tool_dedupe_into_one_report(client, store):
    with authenticated():
        over_rest = client.post(ROUTE, json=SUMMARY).json()
        over_mcp = _tool(client, "report_problem", SUMMARY)["structuredContent"]
        again = client.post(ROUTE, json=SUMMARY).json()
    assert over_mcp["report_id"] == over_rest["report_id"] and over_mcp["duplicate"] is True and over_mcp["count"] == 2
    assert again["duplicate"] is True and again["count"] == 3
    assert len(store.list()) == 1


def test_the_rest_route_names_the_same_caller_as_the_tool(client, store):
    with authenticated():
        rest_id = client.post(ROUTE, json={**SUMMARY, "summary": "one"}).json()["report_id"]
        mcp_id = _tool(client, "report_problem", {**SUMMARY, "summary": "two"})["structuredContent"]["report_id"]
    assert store.get(rest_id)["apps"] == store.get(mcp_id)["apps"]
    assert store.get(rest_id)["last_context"]["app_id"] == store.get(mcp_id)["last_context"]["app_id"]


def test_the_rest_route_needs_a_key(client, store):
    resp = client.post(ROUTE, json=SUMMARY)
    assert resp.status_code == 401
    assert store.list() == []


@pytest.mark.parametrize("body, param", [
    ({"category": "failure"}, "summary"),
    ({"summary": "x", "category": "nonsense"}, "category"),
    ({"summary": "x", "bogus": 1}, "bogus"),
    ({"summary": "x", "target": 5}, "target"),
])
def test_bad_arguments_are_a_400_that_names_the_parameter(client, store, body, param):
    with authenticated():
        resp = client.post(ROUTE, json=body)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_report" and error["param"] == param and error["message"]
    assert store.list() == []


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"'])
def test_a_body_that_is_not_an_object_is_a_400(client, store, raw):
    with authenticated():
        resp = client.post(ROUTE, content=raw, headers={"content-type": "application/json"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_report"


def test_the_hourly_limit_is_a_429_with_its_own_code(client, store, monkeypatch):
    monkeypatch.setattr(store, "_hourly_limit", 1)
    with authenticated():
        assert client.post(ROUTE, json=SUMMARY).status_code == 200
        resp = client.post(ROUTE, json={**SUMMARY, "summary": "another"})
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "rate_limited"


def test_the_gateway_error_codes_the_route_uses_are_on_the_list():
    from gateway import client_errors
    assert {"invalid_report", "rate_limited"} <= client_errors.GATEWAY_CODES


# -- `run` with a target that is a tool


@pytest.mark.parametrize("target", sorted(NOT_A_TARGET_NAMES) + [" report_problem "])
def test_run_with_report_problem_points_to_both_ways_to_file(target):
    env = _env()
    result = mcp_call(env, "run", {"target": target, "inputs": {"summary": "x"}})
    assert result["isError"] is True
    out = result["structuredContent"]
    assert "tool, not a run target" in out["error"]
    assert "report_problem" in out["error"] and REST_PATH in out["error"]
    assert "unknown target" not in out["error"] and "targets" not in out


def test_capabilities_with_that_target_gets_the_same_pointer():
    env = _env()
    out = mcp_call(env, "capabilities", {"target": "report_problem"})["structuredContent"]
    assert "tool, not a run target" in out["error"]


# -- `capabilities` names how to report


def test_the_mcp_capabilities_answer_names_how_to_report():
    env = _env()
    out = mcp_call(env, "capabilities", {})["structuredContent"]
    assert out["report_problem"] == HOW_TO_REPORT
    assert HOW_TO_REPORT == {"tool": "report_problem", "rest": f"POST {REST_PATH}"}


def test_the_rest_capabilities_answer_names_how_to_report():
    from fastapi import FastAPI
    from gateway.api import create_gateway_router

    app = FastAPI()
    app.include_router(create_gateway_router(SimpleNamespace(current=lambda: None), capabilities_provider=lambda _: [],
                                            audience_resolver=lambda _: "admin"))
    body = TestClient(app).get("/gateway/v1/capabilities").json()
    assert body["report_problem"] == HOW_TO_REPORT
    assert set(body) == {"schema_version", "audience", "capabilities", "report_problem"}


def test_a_host_that_does_not_wire_the_tool_points_nowhere():
    env = _build_env([])
    out = mcp_call(env, "run", {"target": "report_problem"})["structuredContent"]
    assert "unknown target" in out["error"]
    assert "report_problem" not in mcp_call(env, "capabilities", {})["structuredContent"]
