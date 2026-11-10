"""Problem reports (docs/design/MCP_PROBLEM_REPORTS.md): the report_problem MCP tool, dedupe, scrubbing,
rate limit, context attached by the gateway, webhook notification and the owner admin routes.

Nothing here reaches a network: the webhook poster is a fake."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, log_scrub, mcp_server, problem_reports, secret_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.mcp_server import McpDeps
from gateway.problem_reports import Caller, ProblemReportError, ProblemReports
from tests.test_mcp_server import ALIASES, CATALOG, H, Core

WEBHOOK = {"id": "ops", "type": "webhook", "url": "https://hooks.example.com/t/secret-topic", "format": "json",
           "enabled": True}


class FakePoster:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def __call__(self, url, *, body, headers, as_json):
        self.sent.append({"url": url, "body": body, "headers": headers, "as_json": as_json})
        if self.fail:
            raise RuntimeError("boom at https://hooks.example.com/t/secret-topic")


def make(tmp_path, *, destinations=(WEBHOOK,), poster=None, jobs=None, limit=20):
    poster = poster or FakePoster()
    svc = ProblemReports(tmp_path / "problem_reports.db", destinations=lambda: list(destinations), post=poster,
                         job_lookup=lambda job_id: (jobs or {}).get(job_id), hourly_limit=limit)
    return svc, poster


def run(coro):
    return asyncio.run(coro)


A1 = Caller(app_id="app_1", app_name="Open WebUI", person_id="per_1")
A2 = Caller(app_id="app_2", app_name="Claude Code", person_id="per_2")
FAILURE = {"tool": "run", "target": "video-long", "error": "metadata dropped at 90% (job 4411)",
           "code": "generation_failed", "category": "upstream", "http_status": 502}


def test_report_stores_what_the_agent_wrote_and_what_the_gateway_knows(tmp_path):
    svc, _ = make(tmp_path)
    svc.note_failure(A1, FAILURE)
    out = run(svc.submit(A1, {"category": "workaround", "target": "video-long", "summary": "The -long path drops metadata at about 90 percent.",
                              "workaround": "Switched to the standard t2v path."}))
    assert out["status"] == "recorded" and out["duplicate"] is False and out["count"] == 1
    assert out["known_workaround"] is None
    row = svc.get(out["report_id"])
    assert row["tool"] == "run" and row["target"] == "video-long" and row["error_code"] == "generation_failed"
    assert row["summary"].startswith("The -long path") and row["workaround_used"] == "Switched to the standard t2v path."
    assert row["apps"] == [{"app_id": "app_1", "app_name": "Open WebUI"}]
    assert row["last_context"]["person_id"] == "per_1" and row["last_context"]["error"].startswith("metadata dropped")
    assert row["text_format"] == "plain_text" and row["status"] == "open"


def test_a_report_without_a_failure_still_records_and_says_so(tmp_path):
    svc, _ = make(tmp_path)
    out = run(svc.submit(A1, {"category": "differs_from_description", "summary": "speech ignores the speed input"}))
    row = svc.get(out["report_id"])
    assert row["tool"] is None and row["error_code"] is None and row["category"] == "differs_from_description"


def test_same_failure_from_two_agents_is_one_report_with_a_count(tmp_path):
    t = [1000]
    svc = ProblemReports(tmp_path / "p.db", destinations=lambda: [], post=FakePoster(), now=lambda: t[0])
    svc.note_failure(A1, FAILURE)
    svc.note_failure(A2, {**FAILURE, "error": "metadata dropped at 87% (job 9120)"})  # other numbers, same failure
    first = run(svc.submit(A1, {"category": "failure", "target": "video-long", "summary": "one"}))
    t[0] = 2000
    second = run(svc.submit(A2, {"category": "failure", "target": "video-long", "summary": "two, in my own words"}))
    assert second["duplicate"] is True and second["report_id"] == first["report_id"] and second["count"] == 2
    rows = svc.list()
    assert len(rows) == 1 and rows[0]["count"] == 2
    assert rows[0]["first_seen"] == 1000 and rows[0]["last_seen"] == 2000
    assert {a["app_id"] for a in rows[0]["apps"]} == {"app_1", "app_2"}
    assert rows[0]["summary"] == "two, in my own words"


def test_a_store_whose_path_moves_makes_its_tables_in_the_new_database(tmp_path):
    # The gateway passes a callable path read at each use; the database it names can change.
    where = [tmp_path / "one" / "p.db"]
    svc = ProblemReports(lambda: where[0], destinations=lambda: [], post=FakePoster())
    run(svc.submit(A1, {"category": "failure", "summary": "lands in the first database"}))
    where[0] = tmp_path / "two" / "p.db"
    assert svc.list() == [] and svc.get("pr_none") is None
    assert not where[0].exists(), "a read must not create the database"
    run(svc.submit(A2, {"category": "failure", "summary": "lands in the second database"}))
    assert [row["summary"] for row in svc.list()] == ["lands in the second database"]


def test_a_different_failure_is_a_different_report(tmp_path):
    svc, _ = make(tmp_path, destinations=())
    svc.note_failure(A1, FAILURE)
    one = run(svc.submit(A1, {"category": "failure", "summary": "a"}))
    svc.note_failure(A1, {**FAILURE, "target": "music", "code": "out_of_memory", "error": "oom"})
    two = run(svc.submit(A1, {"category": "failure", "summary": "b"}))
    assert one["report_id"] != two["report_id"] and len(svc.list()) == 2


def test_reports_without_a_seen_failure_split_by_what_the_agent_says(tmp_path):
    # Live 2026-11-09: two different video problems with no failure the gateway saw became one report, and the
    # second summary replaced the first. Without a seen failure the agent's own words are the identity.
    svc, _ = make(tmp_path, destinations=())
    one = run(svc.submit(A1, {"category": "failure", "target": "video", "summary": "Renders fail at 90% with a 403."}))
    two = run(svc.submit(A1, {"category": "differs_from_description", "target": "video",
                              "summary": "The receipt link for a finished video is dead."}))
    assert one["report_id"] != two["report_id"] and two["duplicate"] is False
    assert sorted(row["summary"] for row in svc.list()) == ["Renders fail at 90% with a 403.",
                                                            "The receipt link for a finished video is dead."]
    again = run(svc.submit(A2, {"category": "failure", "target": "video", "summary": "Renders fail at 87% with a 403."}))
    assert again["report_id"] == one["report_id"] and again["count"] == 2  # numbers aside, the same words


def test_reports_that_differ_only_in_category_or_only_in_target_stay_apart(tmp_path):
    svc, _ = make(tmp_path, destinations=())
    base = {"category": "failure", "target": "video", "summary": "The preview never loads."}
    one = run(svc.submit(A1, base))
    other_category = run(svc.submit(A1, {**base, "category": "differs_from_description"}))
    other_target = run(svc.submit(A1, {**base, "target": "music"}))
    ids = {one["report_id"], other_category["report_id"], other_target["report_id"]}
    assert len(ids) == 3 and len(svc.list()) == 3


def test_http_statuses_in_the_words_keep_two_reports_apart_and_the_first_summary_survives(tmp_path):
    svc, _ = make(tmp_path, destinations=())
    a = run(svc.submit(A1, {"category": "failure", "target": "video", "summary": "Render fails with HTTP 403."}))
    b = run(svc.submit(A2, {"category": "failure", "target": "video", "summary": "Render fails with HTTP 500."}))
    assert a["report_id"] != b["report_id"] and len(svc.list()) == 2
    same = run(svc.submit(A2, {"category": "failure", "target": "video", "summary": "Render fails with HTTP 403."}))
    assert same["report_id"] == a["report_id"] and same["count"] == 2
    row = svc.get(a["report_id"])
    assert row["summary"] == "Render fails with HTTP 403."


def test_a_second_report_with_the_same_basis_keeps_the_first_summary_and_puts_the_latest_in_context(tmp_path):
    svc, _ = make(tmp_path, destinations=())
    first = run(svc.submit(A1, {"category": "failure", "target": "video", "summary": "Renders fail at 90% with a 403."}))
    run(svc.submit(A2, {"category": "failure", "target": "video", "summary": "Renders fail at 87% with a 403."}))
    row = svc.get(first["report_id"])
    assert row["count"] == 2 and row["summary"] == "Renders fail at 90% with a 403."
    assert row["last_context"].get("latest_summary") == "Renders fail at 87% with a 403."


def test_a_report_that_names_no_target_and_no_job_takes_nothing_from_an_unrelated_failure(tmp_path):
    svc, _ = make(tmp_path, destinations=())
    svc.note_failure(A1, FAILURE)
    out = run(svc.submit(A1, {"category": "failure", "summary": "The voice list is empty."}))
    row = svc.get(out["report_id"])
    assert row["tool"] is None and row["target"] is None and row["error_text"] is None
    assert row["error_code"] is None and "job_id" not in row["last_context"]


def test_operator_workaround_comes_back_to_the_next_agent(tmp_path):
    svc, _ = make(tmp_path)
    svc.note_failure(A1, FAILURE)
    first = run(svc.submit(A1, {"category": "failure", "target": "video-long", "summary": "x"}))
    svc.update(first["report_id"], workaround="Use target `video` with mode=async instead of `video-long`.")
    svc.note_failure(A2, FAILURE)
    again = run(svc.submit(A2, {"category": "failure", "target": "video-long", "summary": "y"}))
    assert again["duplicate"] is True
    assert again["known_workaround"] == "Use target `video` with mode=async instead of `video-long`."


def test_a_resolved_report_reopens_when_it_comes_back(tmp_path):
    svc, _ = make(tmp_path)
    svc.note_failure(A1, FAILURE)
    first = run(svc.submit(A1, {"category": "failure", "target": "video-long", "summary": "x"}))
    svc.update(first["report_id"], status="resolved")
    svc.note_failure(A1, FAILURE)
    run(svc.submit(A1, {"category": "failure", "target": "video-long", "summary": "x again"}))
    assert svc.get(first["report_id"])["status"] == "open"


def test_secrets_and_control_text_never_rest_in_the_store(tmp_path):
    log_scrub.remember("opk_supersecretvalue1234567890", "test:problem-report")
    try:
        svc, _ = make(tmp_path)
        text = "key opk_supersecretvalue1234567890 leaked\x00\x1b[31m <script>alert(1)</script> " + "z" * 3000
        out = run(svc.submit(A1, {"category": "failure", "summary": text, "workaround": text}))
        row = svc.get(out["report_id"])
        stored = json.dumps(row)
        assert "opk_supersecretvalue1234567890" not in stored
        assert "\x00" not in row["summary"] and "\x1b" not in row["summary"]
        assert "<script>alert(1)</script>" in row["summary"]  # kept as inert text, never rendered or stripped into HTML
        assert len(row["summary"]) <= 1000 and len(row["workaround_used"]) <= 500
    finally:
        log_scrub.forget("test:problem-report")


def test_bad_input_is_refused(tmp_path):
    svc, _ = make(tmp_path)
    for args in ({}, {"summary": ""}, {"summary": "ok", "category": "nonsense"}, {"summary": "ok", "extra": 1},
                 {"summary": 5}):
        with pytest.raises(ProblemReportError):
            run(svc.submit(A1, args))


def test_rate_limit_is_per_app(tmp_path):
    svc, _ = make(tmp_path, destinations=(), limit=2)
    for word in ("alpha", "beta"):
        run(svc.submit(A1, {"category": "failure", "summary": f"{word} is broken"}))
    with pytest.raises(ProblemReportError) as err:
        run(svc.submit(A1, {"category": "failure", "summary": "gamma is broken"}))
    assert err.value.code == "rate_limited"
    assert len(svc.list()) == 2
    run(svc.submit(A2, {"category": "failure", "summary": "other app is fine"}))


# ---------------------------------------------------------------- context from the gateway


def test_job_id_pulls_the_stored_job_facts_for_the_callers_own_job(tmp_path):
    job = {"job_id": "mj_7", "request_id": "req_7", "workflow": "video.generate.ltx-long", "backend": "svc-video-a",
           "creating_receipt_id": "rcpt_7", "error_code": "upstream_failed", "error": "dropped", "app_id": "app_1"}
    svc, _ = make(tmp_path, jobs={"mj_7": job})
    out = run(svc.submit(A1, {"category": "failure", "summary": "x", "job_id": "mj_7"}))
    ctx = svc.get(out["report_id"])["last_context"]
    assert (ctx["workflow"], ctx["engine"], ctx["request_id"], ctx["receipt_id"], ctx["job_id"]) == (
        "video.generate.ltx-long", "svc-video-a", "req_7", "rcpt_7", "mj_7")
    assert svc.get(out["report_id"])["error_code"] == "upstream_failed"


def test_another_apps_job_adds_nothing(tmp_path):
    job = {"job_id": "mj_8", "request_id": "req_8", "workflow": "secret.flow", "backend": "svc-x", "app_id": "app_2"}
    svc, _ = make(tmp_path, jobs={"mj_8": job})
    out = run(svc.submit(A1, {"category": "failure", "summary": "x", "job_id": "mj_8"}))
    ctx = svc.get(out["report_id"])["last_context"]
    assert "engine" not in ctx and "workflow" not in ctx and "request_id" not in ctx


def test_an_old_failure_is_not_attached(tmp_path):
    t = [1000]
    svc = ProblemReports(tmp_path / "p.db", destinations=lambda: [], post=FakePoster(), now=lambda: t[0])
    svc.note_failure(A1, FAILURE)
    t[0] += 31 * 60
    out = run(svc.submit(A1, {"category": "failure", "summary": "x"}))
    assert svc.get(out["report_id"])["tool"] is None


# ---------------------------------------------------------------- notification


def test_new_report_posts_json_to_the_configured_webhook_once(tmp_path):
    svc, poster = make(tmp_path)
    svc.note_failure(A1, FAILURE)
    run(svc.submit(A1, {"category": "failure", "target": "video-long", "summary": "first"}))
    svc.note_failure(A2, FAILURE)
    run(svc.submit(A2, {"category": "failure", "target": "video-long", "summary": "second"}))  # duplicate: no new post
    assert len(poster.sent) == 1
    sent = poster.sent[0]
    assert sent["url"] == WEBHOOK["url"] and sent["as_json"] is True
    body = sent["body"]
    assert body["event"] == "problem_report.new" and body["count"] == 1 and body["tool"] == "run"
    assert body["target"] == "video-long" and body["summary"] == "first" and body["app"] == "Open WebUI"
    assert body["text_format"] == "plain_text"


def test_text_destination_posts_a_short_plain_body_with_title_header(tmp_path):
    dest = {**WEBHOOK, "id": "ntfy", "format": "text"}
    svc, poster = make(tmp_path, destinations=(dest,))
    svc.note_failure(A1, FAILURE)
    run(svc.submit(A1, {"category": "failure", "target": "video-long", "summary": "metadata dropped", "workaround": "used t2v"}))
    sent = poster.sent[0]
    assert sent["as_json"] is False and isinstance(sent["body"], str)
    assert "metadata dropped" in sent["body"] and "video-long" in sent["body"]
    assert sent["headers"]["Title"].startswith("Azizam problem") and "\n" not in sent["headers"]["Title"]


def test_duplicate_notifies_again_only_at_ten(tmp_path):
    svc, poster = make(tmp_path, limit=100)
    for n in range(10):
        svc.note_failure(A1, FAILURE)
        run(svc.submit(A1, {"category": "failure", "summary": f"s{n}"}))
    assert [s["body"]["count"] for s in poster.sent] == [1, 10]


def test_disabled_destination_is_skipped_and_unknown_type_is_ignored(tmp_path):
    svc, poster = make(tmp_path, destinations=({**WEBHOOK, "enabled": False}, {"id": "x", "type": "carrier-pigeon"}))
    run(svc.submit(A1, {"category": "failure", "summary": "x"}))
    assert poster.sent == []


def test_failed_delivery_does_not_fail_the_report_and_leaks_no_url(tmp_path):
    svc, poster = make(tmp_path, poster=FakePoster(fail=True))
    out = run(svc.submit(A1, {"category": "failure", "summary": "x"}))
    assert out["status"] == "recorded"
    row = svc.get(out["report_id"])
    assert row["last_notify_error"] == "delivery_failed"
    assert "secret-topic" not in json.dumps(row)


# ---------------------------------------------------------------- the MCP tool


@pytest.fixture()
def mcp(tmp_path):
    core = Core()
    svc, poster = make(tmp_path)

    def authenticate(request, _path):
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": {"message": "Missing API key."}}, status_code=401)
        return SimpleNamespace(identity="k")

    def caller(auth):
        return A1

    async def report(request, auth, args):
        try:
            return await svc.submit(caller(auth), args)
        except ProblemReportError as exc:
            from gateway.workflow_call import CallError
            raise CallError(str(exc), code=exc.code) from None

    deps = McpDeps(authenticate=authenticate, call=core.call, catalog=lambda: CATALOG, aliases=lambda: ALIASES,
                   store_asset=core.store_asset, read_asset=core.read_asset, public_base_url=lambda r: "https://gw.test",
                   poll_interval=0.0, allowed_origins=lambda r: ["https://gw.test"],
                   problem_report=report, note_failure=lambda request, auth, info: svc.note_failure(caller(auth), info))
    app = FastAPI()
    mcp_server.register_mcp_routes(app, deps)
    return SimpleNamespace(core=core, svc=svc, poster=poster, client=TestClient(app))


def rpc(mcp, method, params=None):
    resp = mcp.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                           headers={**H, "mcp-protocol-version": "2025-11-25"})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def test_tool_is_listed_with_a_description_that_says_when_to_call_it(mcp):
    tools = {t["name"]: t for t in rpc(mcp, "tools/list")["tools"]}
    tool = tools["report_problem"]
    text = tool["description"].lower()
    assert "without being asked" in text and "failed" in text and "workaround" in text and "differs" in text
    assert tool["inputSchema"]["required"] == ["summary"] and tool["inputSchema"]["additionalProperties"] is False
    assert tool["annotations"]["readOnlyHint"] is False and tool["annotations"]["destructiveHint"] is False
    assert "report_problem" in mcp_server.INSTRUCTIONS


def test_tool_is_absent_on_a_host_that_does_not_wire_it():
    core = Core()
    deps = McpDeps(authenticate=lambda r, p: SimpleNamespace(identity="k"), call=core.call, catalog=lambda: CATALOG,
                   aliases=lambda: ALIASES, store_asset=core.store_asset, read_asset=core.read_asset,
                   public_base_url=lambda r: "https://gw.test")
    app = FastAPI()
    mcp_server.register_mcp_routes(app, deps)
    resp = TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers={**H, "mcp-protocol-version": "2025-11-25"})
    assert "report_problem" not in {t["name"] for t in resp.json()["result"]["tools"]}


def test_failed_run_carries_a_report_hint_and_the_report_gets_its_context(mcp):
    mcp.core.routes[("POST", "/v1/images/generations")] = httpx.Response(
        502, json={"error": {"message": "engine dropped metadata at 90%", "code": "generation_failed"}})
    result = rpc(mcp, "tools/call", {"name": "run", "arguments": {"target": "krea", "inputs": {"prompt": "x"}}})
    assert result["isError"] is True
    assert "report_problem" in result["structuredContent"]["report_hint"]
    reported = rpc(mcp, "tools/call", {"name": "report_problem", "arguments": {
        "category": "workaround", "target": "krea", "summary": "krea fails on every call", "workaround": "used another image alias"}})
    assert "isError" not in reported or reported["isError"] is False
    out = reported["structuredContent"]
    assert out["status"] == "recorded" and out["duplicate"] is False
    row = mcp.svc.get(out["report_id"])
    assert row["tool"] == "run" and row["target"] == "krea" and row["error_code"] == "generation_failed"
    assert len(mcp.poster.sent) == 1


def test_bad_arguments_are_an_error_result_not_a_protocol_error(mcp):
    bad = rpc(mcp, "tools/call", {"name": "report_problem", "arguments": {"category": "failure"}})
    assert bad["isError"] is True
    assert "summary" in bad["structuredContent"]["error"]


def test_success_result_is_not_hinted(mcp):
    result = rpc(mcp, "tools/call", {"name": "capabilities", "arguments": {}})
    assert "report_hint" not in result["structuredContent"]


# ---------------------------------------------------------------- the owner admin routes


@pytest.fixture()
def admin(tmp_path):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    svc, poster = make(tmp_path, destinations=())

    def auth(request):
        role = request.headers.get("test-role", "owner")
        if role == "anonymous":
            raise AdminAuthError("Sign in", status_code=401)
        session = {"session_id": "sess_test"} if request.headers.get("test-session") else {}
        return AdminContext(subject="tester", roles=frozenset({role}), **session)

    app = FastAPI()
    app.include_router(problem_reports.create_problem_reports_router(auth=auth, service=svc, admin_path=admin_db))
    return SimpleNamespace(client=TestClient(app), svc=svc, path=admin_db)


def test_owner_lists_reads_and_edits_reports(admin):
    admin.svc.note_failure(A1, FAILURE)
    rid = run(admin.svc.submit(A1, {"category": "failure", "summary": "<b>bold</b>"}))["report_id"]
    listing = admin.client.get("/admin/api/problem-reports").json()
    assert listing["text_format"] == "plain_text" and [r["id"] for r in listing["reports"]] == [rid]
    assert listing["reports"][0]["summary"] == "<b>bold</b>"
    assert admin.client.get(f"/admin/api/problem-reports/{rid}").json()["id"] == rid
    patched = admin.client.patch(f"/admin/api/problem-reports/{rid}", json={"workaround": "use video", "status": "resolved"})
    assert patched.status_code == 200 and patched.json()["operator_workaround"] == "use video"
    assert admin.client.get("/admin/api/problem-reports", params={"status": "open"}).json()["reports"] == []
    assert admin.client.get("/admin/api/problem-reports/pr_missing").status_code == 404
    assert admin.client.patch(f"/admin/api/problem-reports/{rid}", json={"status": "weird"}).status_code == 400


def test_only_the_owner_may_use_the_routes(admin):
    for role, status in (("member", 403), ("anonymous", 401)):
        assert admin.client.get("/admin/api/problem-reports", headers={"test-role": role}).status_code == status
        assert admin.client.put("/admin/api/problem-reports/destinations", json={"destinations": []},
                                headers={"test-role": role}).status_code == status


def test_destinations_are_owner_data_with_the_url_hidden_on_read(admin):
    put = admin.client.put("/admin/api/problem-reports/destinations", json={"destinations": [WEBHOOK]})
    assert put.status_code == 200
    listed = admin.client.get("/admin/api/problem-reports/destinations").json()["destinations"]
    assert listed == [{"id": "ops", "type": "webhook", "origin": "https://hooks.example.com", "format": "json",
                       "enabled": True, "has_secret": False}]
    assert "secret-topic" not in json.dumps(listed)
    assert problem_reports.destinations_from_settings(admin.path)[0]["url"] == WEBHOOK["url"]
    # A PUT that omits the url keeps the stored one.
    admin.client.put("/admin/api/problem-reports/destinations",
                     json={"destinations": [{"id": "ops", "type": "webhook", "format": "text", "enabled": False}]})
    kept = problem_reports.destinations_from_settings(admin.path)[0]
    assert kept["url"] == WEBHOOK["url"] and kept["format"] == "text" and kept["enabled"] is False


@pytest.mark.parametrize("bad", [
    {"id": "a", "type": "webhook", "url": "ftp://x.example.com/", "format": "json", "enabled": True},
    {"id": "a", "type": "webhook", "url": "not a url", "format": "json", "enabled": True},
    {"id": "a", "type": "webhook", "url": "https://x.example.com/", "format": "xml", "enabled": True},
    {"id": "a", "type": "pigeon", "url": "https://x.example.com/", "format": "json", "enabled": True},
    {"type": "webhook", "url": "https://x.example.com/", "format": "json", "enabled": True},
])
def test_bad_destinations_are_refused(admin, bad):
    resp = admin.client.put("/admin/api/problem-reports/destinations", json={"destinations": [bad]})
    assert resp.status_code == 400


NTFY = {"id": "ntfy", "type": "webhook", "url": "https://ntfy.example.com/alerts", "format": "text", "enabled": True}


def _settings_json(path):
    return json.dumps(problem_reports.destinations_from_settings(path))


def test_a_destination_secret_is_write_only_bound_to_its_origin_and_sent_as_a_bearer_token(admin, tmp_path):
    put = admin.client.put("/admin/api/problem-reports/destinations",
                           json={"destinations": [{**NTFY, "secret": "tk_destinationtoken123"}]})
    assert put.status_code == 200 and "tk_destinationtoken123" not in put.text
    assert put.json()["destinations"][0]["has_secret"] is True
    listed = admin.client.get("/admin/api/problem-reports/destinations")
    assert "tk_destinationtoken123" not in listed.text and listed.json()["destinations"][0]["has_secret"] is True
    stored = problem_reports.destinations_from_settings(admin.path)[0]
    assert "tk_destinationtoken123" not in _settings_json(admin.path) and stored["secret_ref"]
    assert secret_store.get_provider_secret(stored["secret_ref"], db_path=admin.path,
                                            expected_origin="https://ntfy.example.com") == "tk_destinationtoken123"
    poster = FakePoster()
    svc = ProblemReports(tmp_path / "sent.db", post=poster,
                         destinations=lambda: problem_reports.destinations_from_settings(admin.path),
                         secret_for=lambda ref, origin: secret_store.get_provider_secret(
                             ref, db_path=admin.path, expected_origin=origin))
    run(svc.submit(A1, {"category": "failure", "summary": "a problem"}))
    assert poster.sent[0]["url"] == NTFY["url"]
    assert poster.sent[0]["headers"]["Authorization"] == "Bearer tk_destinationtoken123"
    assert svc.list()[0]["last_notify_error"] is None


def test_a_destination_secret_is_kept_on_the_same_origin_and_dropped_when_the_origin_moves(admin):
    admin.client.put("/admin/api/problem-reports/destinations",
                     json={"destinations": [{**NTFY, "secret": "tk_destinationtoken123"}]})
    first = problem_reports.destinations_from_settings(admin.path)[0]["secret_ref"]
    admin.client.put("/admin/api/problem-reports/destinations",
                     json={"destinations": [{**NTFY, "url": "https://ntfy.example.com/other"}]})
    assert problem_reports.destinations_from_settings(admin.path)[0]["secret_ref"] == first
    moved = admin.client.put("/admin/api/problem-reports/destinations",
                             json={"destinations": [{**NTFY, "url": "https://elsewhere.example.net/x"}]})
    assert moved.json()["destinations"][0]["has_secret"] is False
    assert "secret_ref" not in problem_reports.destinations_from_settings(admin.path)[0]
    with pytest.raises(KeyError):  # the old token is deleted, not left behind
        secret_store.get_provider_secret(first, db_path=admin.path)


def test_a_destination_secret_is_cleared_by_null_and_bad_secrets_are_refused(admin):
    admin.client.put("/admin/api/problem-reports/destinations",
                     json={"destinations": [{**NTFY, "secret": "tk_destinationtoken123"}]})
    ref = problem_reports.destinations_from_settings(admin.path)[0]["secret_ref"]
    cleared = admin.client.put("/admin/api/problem-reports/destinations", json={"destinations": [{**NTFY, "secret": None}]})
    assert cleared.status_code == 200 and cleared.json()["destinations"][0]["has_secret"] is False
    with pytest.raises(KeyError):
        secret_store.get_provider_secret(ref, db_path=admin.path)
    for bad in ("", "has space", "x" * 5000, 7, "tk_bad\ttoken"):
        resp = admin.client.put("/admin/api/problem-reports/destinations", json={"destinations": [{**NTFY, "secret": bad}]})
        assert resp.status_code == 400
        if isinstance(bad, str) and bad:
            assert bad not in resp.text


def test_a_secret_that_cannot_be_read_is_a_failed_delivery_that_names_nothing(tmp_path):
    poster = FakePoster()

    def unreadable(ref, origin):
        raise PermissionError("provider secret is not approved for this endpoint origin")

    svc = ProblemReports(tmp_path / "p.db", post=poster, secret_for=unreadable,
                         destinations=lambda: [{**NTFY, "secret_ref": "sec_1"}])
    out = run(svc.submit(A1, {"category": "failure", "summary": "a problem"}))
    assert poster.sent == [] and svc.get(out["report_id"])["last_notify_error"] == "delivery_failed"


# ---------------------------------------------------------------- review findings: origin, atomicity, reader

_SECRET = "tk_destinationtoken123"
_PUT = "/admin/api/problem-reports/destinations"


def _secret_rows(path):
    import sqlite3
    with sqlite3.connect(path) as conn:
        try:
            return conn.execute("SELECT id FROM provider_secrets").fetchall()
        except sqlite3.OperationalError:
            return []


def test_a_url_whose_origin_cannot_be_computed_is_refused_with_its_index_and_field(admin):
    odd = {**NTFY, "id": "odd", "url": "https://good.example:+443/x"}
    for body in ({**odd, "secret": _SECRET}, odd):
        resp = admin.client.put(_PUT, json={"destinations": [NTFY, body]})
        assert resp.status_code == 400, resp.text
        assert "destinations[1]" in resp.text and "url" in resp.text and _SECRET not in resp.text
    assert _secret_rows(admin.path) == []


def test_an_edit_to_an_unparseable_origin_never_sends_the_bearer_to_the_new_host(admin, tmp_path):
    good = {**NTFY, "url": "https://good.example/x"}
    assert admin.client.put(_PUT, json={"destinations": [{**good, "secret": _SECRET}]}).status_code == 200
    evil = admin.client.put(_PUT, json={"destinations": [{**good, "url": "https://evil.example:+443/y"}]})
    assert evil.status_code == 400
    poster = FakePoster()
    svc = ProblemReports(tmp_path / "sent.db", post=poster,
                         destinations=lambda: problem_reports.destinations_from_settings(admin.path),
                         secret_for=lambda ref, origin: secret_store.get_provider_secret(
                             ref, db_path=admin.path, expected_origin=origin))
    run(svc.submit(A1, {"category": "failure", "summary": "a problem"}))
    assert all("evil.example" not in sent["url"] for sent in poster.sent)


def test_a_stored_destination_without_a_computable_origin_is_never_delivered_to(tmp_path):
    poster = FakePoster()
    asked = []
    svc = ProblemReports(tmp_path / "p.db", post=poster,
                         secret_for=lambda ref, origin: asked.append((ref, origin)) or "tk_value",
                         destinations=lambda: [{**NTFY, "url": "https://evil.example:+443/y", "secret_ref": "sref_1"}])
    out = run(svc.submit(A1, {"category": "failure", "summary": "a problem"}))
    assert poster.sent == [] and asked == []
    assert svc.get(out["report_id"])["last_notify_error"] == "delivery_failed"


def test_a_secret_is_refused_on_a_plain_http_url_but_the_url_alone_is_allowed(admin):
    plain = {**NTFY, "url": "http://ntfy.example.com/alerts"}
    resp = admin.client.put(_PUT, json={"destinations": [{**plain, "secret": _SECRET}]})
    assert resp.status_code == 400 and "destinations[0]" in resp.text and _SECRET not in resp.text
    assert _secret_rows(admin.path) == []
    assert admin.client.put(_PUT, json={"destinations": [plain]}).status_code == 200


def test_a_failed_setting_write_leaves_no_stored_secret(admin, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(admin_store, "put_operator_setting", refuse)
    with pytest.raises(RuntimeError):
        admin.client.put(_PUT, json={"destinations": [{**NTFY, "secret": _SECRET}]})
    assert _secret_rows(admin.path) == []


def test_a_session_that_ends_before_the_second_secret_leaves_no_stored_secret(admin, monkeypatch):
    from gateway import session_guard
    original = secret_store.put_provider_secret
    calls = []

    def second_store_finds_the_session_ended(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise session_guard.SessionEnded()
        return original(*args, **{**kwargs, "session": None})

    monkeypatch.setattr(secret_store, "put_provider_secret", second_store_finds_the_session_ended)
    other = {**NTFY, "id": "other", "url": "https://other.example.com/x"}
    resp = admin.client.put(_PUT, headers={"test-session": "1"},
                            json={"destinations": [{**NTFY, "secret": _SECRET}, {**other, "secret": "tk_second_token_99"}]})
    assert resp.status_code == 401, resp.text
    assert _secret_rows(admin.path) == []
    assert problem_reports.destinations_from_settings(admin.path) == []


def test_a_sign_out_after_the_save_does_not_undo_it_or_keep_the_old_token(admin, monkeypatch):
    from gateway import session_guard
    admin.client.put(_PUT, json={"destinations": [{**NTFY, "secret": _SECRET}]})
    old = problem_reports.destinations_from_settings(admin.path)[0]["secret_ref"]
    original_put, original_delete = secret_store.put_provider_secret, secret_store.delete_provider_secret

    def delete(*args, **kwargs):
        if kwargs.get("session") is not None:
            raise session_guard.SessionEnded()
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(secret_store, "delete_provider_secret", delete)
    monkeypatch.setattr(secret_store, "put_provider_secret",
                        lambda *a, **k: original_put(*a, **{**k, "session": None}))
    resp = admin.client.put(_PUT, headers={"test-session": "1"},
                            json={"destinations": [{**NTFY, "secret": "tk_replacement_token_7"}]})
    assert resp.status_code == 200, resp.text
    assert problem_reports.destinations_from_settings(admin.path)[0]["secret_ref"] != old
    with pytest.raises(KeyError):
        secret_store.get_provider_secret(old, db_path=admin.path)


def test_the_gateways_store_sends_the_bearer_to_its_origin_only_and_registers_the_value(admin, tmp_path, monkeypatch):
    import proxy
    from gateway import secret_refs
    assert admin.client.put(_PUT, json={"destinations": [{**NTFY, "secret": _SECRET}]}).status_code == 200
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", admin.path)
    monkeypatch.setattr(secret_refs, "_db_path", None)
    poster = FakePoster()
    reports = proxy.PROBLEM_REPORTS
    monkeypatch.setattr(reports, "_post", poster)
    monkeypatch.setattr(reports, "_path_source", lambda: tmp_path / "gateway_reports.db")
    log_scrub.clear()
    run(reports.submit(A1, {"category": "failure", "summary": "the first problem"}))
    assert poster.sent[0]["url"] == NTFY["url"]
    assert poster.sent[0]["headers"]["Authorization"] == "Bearer " + _SECRET
    assert _SECRET not in log_scrub.scrub("an upstream echoed " + _SECRET)
    ref = problem_reports.destinations_from_settings(admin.path)[0]["secret_ref"]
    admin_store.put_operator_setting(problem_reports.SETTING_KEY, {"destinations": [
        {**NTFY, "url": "https://elsewhere.example.net/x", "secret_ref": ref}]}, path=admin.path)
    run(reports.submit(A1, {"category": "failure", "summary": "the second problem"}))
    assert len(poster.sent) == 1  # the secret is bound to another origin: nothing was sent
    second = [row for row in reports.list() if row["summary"] == "the second problem"][0]
    assert second["last_notify_error"] == "delivery_failed"
