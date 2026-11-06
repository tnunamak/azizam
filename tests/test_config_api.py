"""The owner API over the config sections (docs/design/CONFIG_HOMES.md 6, 7 and 8.1; acceptance 10.1, C2).

No release section ships yet, so every test builds a ``ConfigHome`` around the fixture section of
``tests/test_config_layers.py`` and a real admin.db in a temporary directory. The router is mounted on a small app with
the same dependencies the gateway gives it; one test class at the end mounts it in the real ``proxy`` app.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, config_api, config_cli, config_layers as layers, config_runtime
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_runtime import create_admin_service_account_key
from tests.support.fake_upstream.loopback import LoopbackServer
from tests.support.gateway import use_admin_db
from tests.test_config_layers import DEFAULTS, SCHEMA, limits_in_order
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture

SECTION = "fixture"
OWNER = {"test-role": "owner"}
ADMIN = {"test-role": "admin"}
API = "/admin/api/config"
REF = "sref_" + "ab" * 12


def make_home() -> config_runtime.ConfigHome:
    section = layers.Section.create(SECTION, copy.deepcopy(DEFAULTS), SCHEMA, validator=limits_in_order)
    return config_runtime.ConfigHome({SECTION: section})


class Gateway:
    """One router, one database, one fake reload that publishes the way the gateway's reload does."""

    def __init__(self, tmp_path: Path, name: str = "admin.db", startup: dict[str, Any] | None = None) -> None:
        self.db = tmp_path / name
        admin_store.init_db(self.db)
        self.home = make_home()
        self.startup = startup if startup is not None else {}
        self.reloads = 0
        self.reload_error: Exception | None = None
        self.after_failure_note: Callable[[], None] | None = None
        self.during_reload: Callable[[], None] | None = None  # runs inside the reload, before it fails: another writer's turn
        app = FastAPI()
        app.include_router(config_api.create_config_router(
            auth=self.auth, admin_path=self.db, home=lambda: self.home, registry=lambda: {}, reload=self.reload,
            startup_block=self.startup.get,
        ))

        @app.get(f"{API}/export")  # the gateway answers this path from proxy.py (format=v1); the test app does it directly
        def export(view: str = "") -> Any:
            return config_api.build_export(self.home, self.db, version="test", effective=view == "effective")

        self.client = TestClient(app)
        self.reload_ok()

    @staticmethod
    def auth(request: Any) -> AdminContext:
        role = request.headers.get("test-role")
        if not role:
            raise AdminAuthError()
        return AdminContext(subject="tester", roles=frozenset({role}), auth_method="admin_key")

    def reload_ok(self) -> None:
        layer = self.home.layer({}, path=self.db, registry={})
        self.home.publish(self.home.snapshot(self.home.attach(dict(layer.config), layer), layer=layer))

    def reload(self) -> None:
        if self.during_reload is not None:
            hook, self.during_reload = self.during_reload, None
            hook()
        if self.reload_error is not None:
            # As proxy's reload does: note the cleaned failure text, then re-raise.
            self.home.note_reload_failed(f"{type(self.reload_error).__name__}: {self.reload_error}", path=self.db)
            if self.after_failure_note is not None:
                hook, self.after_failure_note = self.after_failure_note, None
                hook()
            raise self.reload_error
        self.reloads += 1
        self.reload_ok()

    def revision(self, section: str = SECTION) -> str:
        return self.client.get(f"{API}/sections/{section}", headers=OWNER).json()["revision"]

    def patch(self, *items: dict[str, Any], query: str = "", if_match: dict[str, str] | None = None, **extra: Any):
        body = {"items": list(items), "if_match": if_match if if_match is not None else {SECTION: self.revision()}, **extra}
        return self.client.patch(f"{API}/changes{query}", json=body, headers=OWNER)

    def entries(self) -> dict[str, dict[str, Any]]:
        view = self.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()
        return {entry["pointer"]: entry for entry in view["entries"]}

    def digest(self) -> str:
        return layers.digest(self.home.layer({}, path=self.db, registry={}).config)

    def counts(self) -> tuple[int, int]:
        conn = sqlite3.connect(self.db)
        try:
            return (conn.execute("SELECT COUNT(*) FROM config_overrides").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM config_changes").fetchone()[0])
        finally:
            conn.close()


def setting(pointer: str, value: Any) -> dict[str, Any]:
    return {"section": SECTION, "pointer": pointer, "op": "set", "value": value}


def tombstone(pointer: str) -> dict[str, Any]:
    return {"section": SECTION, "pointer": pointer, "op": "delete"}


def reset(pointer: str) -> dict[str, Any]:
    return {"section": SECTION, "pointer": pointer, "op": "reset"}


@pytest.fixture
def gw(tmp_path) -> Gateway:
    return Gateway(tmp_path)


# ---------------------------------------------------------------------------------------------------------------------
# Auth: every route is owner-only
# ---------------------------------------------------------------------------------------------------------------------

ROUTES = [
    ("GET", API, None),
    ("GET", f"{API}/sections/{SECTION}", None),
    ("PATCH", f"{API}/changes", {"items": [setting("/timeout", 5)], "if_match": {SECTION: "0"}}),
    ("DELETE", f"{API}/sections/{SECTION}/entries?pointer=/timeout", None),
    ("POST", f"{API}/changes/cfg_x/revert", {}),
    ("GET", f"{API}/changes", None),
    ("GET", f"{API}/drift", None),
    ("GET", f"{API}/problems", None),
    ("POST", f"{API}/import", {"document": {"format": layers.EXPORT_FORMAT}}),
    ("POST", f"{API}/sections/{SECTION}/legacy-import", {}),
]


@pytest.mark.parametrize("method,path,body", ROUTES, ids=[f"{m} {p.split('?')[0]}" for m, p, _ in ROUTES])
def test_every_route_needs_the_owner_not_an_admin_and_not_nobody(gw, method, path, body):
    send = lambda headers: gw.client.request(method, path, json=body, headers=headers)  # noqa: E731
    assert send({}).status_code == 401
    refused = send(ADMIN)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "owner_required"
    assert gw.counts() == (0, 0), "the refused calls wrote nothing"
    assert send(OWNER).status_code not in (401, 403)


def test_the_routes_that_need_a_precondition_answer_428_without_one(gw):
    assert gw.client.patch(f"{API}/changes", json={"items": [setting("/timeout", 5)]}, headers=OWNER).status_code == 428
    missing = gw.client.delete(f"{API}/sections/{SECTION}/entries?pointer=/timeout", headers=OWNER)
    assert missing.status_code == 428 and missing.json()["error"]["code"] == "revision_required"
    gw.startup[SECTION] = {"timeout": 9}
    legacy = gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers=OWNER)
    assert legacy.status_code == 428 and legacy.json()["sections"] == [SECTION]
    assert gw.counts() == (0, 0)


# ---------------------------------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------------------------------

def test_the_sections_list_counts_overrides_problems_and_drift(gw):
    assert gw.client.get(API, headers=OWNER).json()["sections"] == [
        {"id": SECTION, "title": SECTION, "revision": "0", "overrides": 0, "problems": 0, "drift": 0}]
    assert gw.patch(setting("/timeout", 60), tombstone("/aliases/fast")).status_code == 200
    row = gw.client.get(API, headers=OWNER).json()["sections"][0]
    assert (row["revision"], row["overrides"]) == ("1", 2)


def test_the_section_view_gives_each_entry_its_value_source_and_override_and_the_etag(gw):
    gw.patch(setting("/timeout", 60), setting("/aliases/extra", {"target": "svc-x"}), tombstone("/aliases/fast"),
             note="why")
    response = gw.client.get(f"{API}/sections/{SECTION}", headers=OWNER)
    assert response.headers["etag"] == '"1"' and response.json()["revision"] == "1"
    entries = gw.entries()
    assert entries["/timeout"]["source"] == "owner" and entries["/timeout"]["value"] == 60
    assert entries["/timeout"]["release_value"] == 30 and entries["/timeout"]["override"]["note"] == "why"
    assert entries["/aliases/extra"]["source"] == "owner-added" and "release_value" not in entries["/aliases/extra"]
    assert entries["/aliases/fast"]["source"] == "owner-deleted" and "value" not in entries["/aliases/fast"]
    assert entries["/aliases/fast"]["release_value"] == {"target": "svc-b"}
    assert entries["/mode"]["source"] == "release" and entries["/mode"]["apply"] == "restart"
    assert entries["/timeout"]["pending"] is False
    assert gw.client.get(f"{API}/sections/nope", headers=OWNER).json()["error"]["code"] == "unknown_section"


# ---------------------------------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------------------------------

def test_a_change_commits_advances_the_revision_and_reloads_so_the_next_reader_sees_it(gw):
    before = gw.home.holder.current()
    response = gw.patch(setting("/timeout", 60))
    assert response.status_code == 200
    body = response.json()
    assert body["revisions"] == {SECTION: "1"} and body["reload"] == {"status": "ok"} and gw.reloads == 1
    now = gw.home.holder.current()
    assert now is not before and now.config[SECTION]["timeout"] == 60
    assert gw.revision() == "1"
    history = gw.client.get(f"{API}/changes", headers=OWNER).json()["changes"]
    assert [c["change_id"] for c in history] == [body["change_id"]] and history[0]["kind"] == "edit"


def test_a_stale_revision_is_412_and_a_section_missing_from_if_match_is_428(gw):
    gw.patch(setting("/timeout", 60))
    stale = gw.patch(setting("/timeout", 70), if_match={SECTION: "0"})
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "revision_mismatch"
    assert stale.json()["current"] == "1"
    missing = gw.patch(setting("/timeout", 70), if_match={})
    assert missing.status_code == 428 and missing.json()["sections"] == [SECTION]
    assert gw.entries()["/timeout"]["value"] == 60


def test_a_quoted_revision_is_accepted_as_an_etag_would_send_it(gw):
    assert gw.patch(setting("/timeout", 60), if_match={SECTION: '"0"'}).status_code == 200


def test_a_dry_run_writes_nothing_reloads_nothing_and_shows_the_diff_and_the_revisions(gw):
    response = gw.patch(setting("/timeout", 60), tombstone("/aliases/fast"), query="?dry_run=1", if_match={})
    body = response.json()
    assert response.status_code == 200 and body["dry_run"] and body["valid"] and body["problems"] == []
    assert body["revisions"] == {SECTION: "0"}
    assert {(d["pointer"], d["before"]["source"], d["after"]["source"]) for d in body["diff"]} == {
        ("/timeout", "release", "owner"), ("/aliases/fast", "release", "owner-deleted")}
    assert next(d for d in body["diff"] if d["pointer"] == "/timeout")["after"]["value"] == 60
    assert gw.counts() == (0, 0) and gw.reloads == 0 and gw.revision() == "0"


def test_a_dry_run_of_a_change_that_breaks_a_rule_reports_the_problem_with_status_200(gw):
    body = gw.patch(setting("/limits/min", 20), query="?dry_run=1").json()
    assert body["valid"] is False and body["problems"][0]["code"] == "override_rejected"
    assert "limits_order" in body["problems"][0]["message"] and body["problems"][0]["pointer"] == "/limits/min"
    assert gw.counts() == (0, 0)


@pytest.mark.parametrize("item,code", [
    (setting("/nonsense", 1), "not_an_entry"),
    (setting("timeout", 1), "bad_pointer"),
    (tombstone("/limits/min"), "delete_required"),
    (setting("/token", "a-plain-secret"), "secret_value"),
    ({"section": "elsewhere", "pointer": "/x", "op": "set", "value": 1}, "unknown_section"),
])
def test_an_item_that_is_not_an_entry_is_refused_422_with_its_code_and_writes_nothing(gw, item, code):
    response = gw.patch(item)
    assert response.status_code == 422
    assert [p["code"] for p in response.json()["problems"]] == [code]
    assert gw.counts() == (0, 0) and gw.reloads == 0


def test_a_value_that_breaks_the_schema_or_a_rule_is_refused_422_and_a_fix_is_accepted(gw):
    schema = gw.patch(setting("/timeout", 0))
    assert schema.status_code == 422 and schema.json()["problems"][0]["code"] == "override_rejected"
    rule = gw.patch(setting("/limits/min", 20))
    assert rule.status_code == 422 and rule.json()["error"]["code"] == "config_invalid"
    assert gw.counts() == (0, 0)
    assert gw.patch(setting("/limits/min", 1), setting("/limits/max", 9)).status_code == 200


def test_a_secret_entry_takes_a_reference_and_a_literal_null_is_a_value(gw):
    assert gw.patch(setting("/token", REF), setting("/note", None)).status_code == 200
    entries = gw.entries()
    assert entries["/token"]["secret"] is True and entries["/token"]["value"] == REF
    assert entries["/note"]["source"] == "owner-added" and entries["/note"]["value"] is None


@pytest.mark.parametrize("body,code", [
    (None, "invalid_json"), ([], "invalid_json"), ({"items": []}, "invalid_request"),
    ({"items": [{"section": SECTION}]}, "invalid_request"),
    ({"items": [{"section": SECTION, "pointer": "/timeout", "op": "set"}]}, "invalid_request"),
    ({"items": [{"section": SECTION, "pointer": "/timeout", "op": "zap"}]}, "invalid_request"),
    ({"items": [setting("/timeout", 5)], "if_match": [1]}, "invalid_request"),
])
def test_a_malformed_body_is_400_with_the_codes_the_admin_api_uses_and_writes_nothing(gw, body, code):
    response = gw.client.patch(f"{API}/changes", content=json.dumps(body) if body is not None else b"not json", headers=OWNER)
    assert response.status_code == 400 and response.json()["error"]["code"] == code
    assert gw.counts() == (0, 0)


def test_a_body_that_is_not_text_for_a_note_is_400_on_every_write_route_and_writes_nothing(tmp_path):
    gw = Gateway(tmp_path, startup={SECTION: {**copy.deepcopy(DEFAULTS), "timeout": 99}})
    committed = gw.patch(setting("/timeout", 60)).json()["change_id"]
    before = gw.counts()
    document = export_of(gw)
    for note in (5, ["x"], {"a": 1}, True):
        attempts = {
            "patch": gw.patch(setting("/timeout", 70), note=note),
            "revert": gw.client.post(f"{API}/changes/{committed}/revert",
                                     json={"if_match": {SECTION: gw.revision()}, "note": note}, headers=OWNER),
            "import": gw.client.post(f"{API}/import", json=import_body(document, gw, note=note), headers=OWNER),
            "legacy-import": gw.client.post(f"{API}/sections/{SECTION}/legacy-import", json={"note": note},
                                            headers={**OWNER, "If-Match": gw.revision()}),
        }
        for route, response in attempts.items():
            assert response.status_code == 400, (route, note, response.text)
            assert response.json()["error"]["code"] == "invalid_request", (route, note)
    assert gw.counts() == before, "no route wrote anything"


def test_the_same_pointer_twice_in_one_change_is_400(gw):
    assert gw.patch(setting("/timeout", 5), setting("/timeout", 6)).status_code == 400


def test_reset_inherits_again_and_a_tombstone_hides_a_release_entry(gw):
    gw.patch(setting("/timeout", 60), tombstone("/aliases/fast"))
    assert gw.entries()["/aliases/fast"]["source"] == "owner-deleted"
    first = gw.client.delete(f"{API}/sections/{SECTION}/entries?pointer=/timeout", headers={**OWNER, "If-Match": '"1"'})
    assert first.status_code == 200 and first.json()["revisions"] == {SECTION: "2"}
    assert gw.entries()["/timeout"]["source"] == "release" and gw.entries()["/timeout"]["value"] == 30
    stale = gw.client.delete(f"{API}/sections/{SECTION}/entries?pointer=/aliases/fast", headers={**OWNER, "If-Match": "1"})
    assert stale.status_code == 412
    gw.client.delete(f"{API}/sections/{SECTION}/entries?pointer=/aliases/fast", headers={**OWNER, "If-Match": "2"})
    assert gw.entries()["/aliases/fast"]["source"] == "release"
    assert gw.client.delete(f"{API}/sections/{SECTION}/entries", headers={**OWNER, "If-Match": "3"}).status_code == 400


def test_a_reset_of_an_entry_with_no_override_changes_nothing_and_reloads_nothing(gw):
    response = gw.client.delete(f"{API}/sections/{SECTION}/entries?pointer=/timeout", headers={**OWNER, "If-Match": "0"})
    assert response.status_code == 200 and response.json()["change_id"] is None and gw.reloads == 0


def test_revert_writes_the_inverse_as_a_new_change_and_refuses_after_a_later_edit_of_the_same_entry(gw):
    first = gw.patch(setting("/timeout", 60)).json()["change_id"]
    reverted = gw.client.post(f"{API}/changes/{first}/revert", json={"if_match": {SECTION: gw.revision()}}, headers=OWNER)
    assert reverted.status_code == 200 and gw.entries()["/timeout"]["source"] == "release"
    assert gw.client.get(f"{API}/changes", headers=OWNER).json()["changes"][0]["kind"] == "revert"
    second = gw.patch(setting("/timeout", 70)).json()["change_id"]
    gw.patch(setting("/timeout", 80))
    late = gw.client.post(f"{API}/changes/{second}/revert", json={"if_match": {SECTION: gw.revision()}}, headers=OWNER)
    assert late.status_code == 412 and late.json()["error"]["code"] == "changed_since"
    assert late.json()["pointers"] == ["/timeout"]
    assert gw.client.post(f"{API}/changes/cfg_missing/revert", json={"if_match": {SECTION: gw.revision()}}, headers=OWNER).status_code == 404
    assert gw.client.post(f"{API}/changes/{first}/revert", json={}, headers=OWNER).status_code == 428


def test_the_history_pages_newest_first(gw):
    ids = [gw.patch(setting("/timeout", n)).json()["change_id"] for n in (11, 12, 13)]
    page = gw.client.get(f"{API}/changes?limit=2", headers=OWNER).json()
    assert [c["change_id"] for c in page["changes"]] == [ids[2], ids[1]] and page["next"] == ids[1]
    rest = gw.client.get(f"{API}/changes?limit=2&before={page['next']}", headers=OWNER).json()
    assert [c["change_id"] for c in rest["changes"]] == [ids[0]] and rest["next"] is None
    assert gw.client.get(f"{API}/changes?before=nope", headers=OWNER).status_code == 404
    assert gw.client.get(f"{API}/changes?limit=x", headers=OWNER).status_code == 400


def test_drift_shows_an_override_whose_release_value_moved(gw):
    gw.patch(setting("/timeout", 60))
    section = layers.Section.create(SECTION, {**copy.deepcopy(DEFAULTS), "timeout": 45}, SCHEMA, validator=limits_in_order)
    gw.home.sections[SECTION] = section
    drift = gw.client.get(f"{API}/drift", headers=OWNER).json()["drift"]
    assert drift == {SECTION: [{"pointer": "/timeout", "kind": "release_changed", "base": 30, "release": 45, "owner": 60}]}
    assert gw.entries()["/timeout"]["drift"]["kind"] == "release_changed"


def test_problems_come_from_the_published_snapshot(gw):
    conn = sqlite3.connect(gw.db)
    conn.execute("INSERT INTO config_overrides(section,pointer,op,value_json,base_json,origin,change_id,updated_at) "
                 "VALUES('fixture','/timeout','set','0','30','api','cfg_bad',1)")
    conn.commit()
    conn.close()
    gw.reload_ok()
    problems = gw.client.get(f"{API}/problems", headers=OWNER).json()
    assert any(p["code"] == layers.CODE_OVERRIDE_REJECTED for p in problems["problems"]["config"][SECTION])
    assert problems["reload_failed"] is None
    assert gw.entries()["/timeout"]["value"] == 30, "the rejected override is not applied"


def test_a_reload_that_fails_after_the_commit_keeps_the_change_and_says_which_one_is_not_serving(gw):
    gw.reload_error = RuntimeError("snapshot build failed")
    response = gw.patch(setting("/timeout", 60))
    assert response.status_code == 202
    body = response.json()
    assert body["reload"]["status"] == "failed" and "snapshot build failed" in body["reload"]["error"]
    assert gw.revision() == "1", "the change is committed"
    view = gw.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()
    assert view["reload_failed"]["change_id"] == body["change_id"]
    assert view["reload_failed"]["entries"][0]["serving"] == {"present": True, "value": 30}
    timeout = gw.entries()["/timeout"]
    assert timeout["value"] == 60 and timeout["pending"] is True and timeout["running_value"] == 30
    gw.reload_error = None
    gw.reload()
    assert gw.entries()["/timeout"]["pending"] is False and gw.home.reload_failed is None


def test_a_failed_reload_after_a_revert_names_the_revert_and_its_entries(gw):
    first = gw.patch(setting("/timeout", 60)).json()["change_id"]
    gw.reload_error = RuntimeError("snapshot build failed")
    response = gw.client.post(f"{API}/changes/{first}/revert", json={"if_match": {SECTION: gw.revision()}}, headers=OWNER)
    assert response.status_code == 202
    body = response.json()
    assert "entries" not in body, "the store's own record is not part of the answer"
    view = gw.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()
    failed = view["reload_failed"]
    assert failed is not None and failed["change_id"] == body["change_id"] != first
    assert failed["entries"] == [{
        "section": SECTION, "pointer": "/timeout", "serving": {"present": True, "value": 60}, "stored": None}]


def test_a_failed_reload_names_its_own_change_when_a_second_writer_commits_during_it(gw):
    def writer_b() -> None:
        gw.patch(setting("/tags", ["b"]))  # commits B, whose own reload fails as well, and notes B as the last commit

    gw.reload_error = RuntimeError("snapshot build failed")
    gw.during_reload = writer_b
    response = gw.patch(setting("/timeout", 60))
    assert response.status_code == 202
    failed = gw.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()["reload_failed"]
    assert failed["change_id"] == response.json()["change_id"], "A's record names A, not the writer that committed last"
    assert [(e["section"], e["pointer"]) for e in failed["entries"]] == [(SECTION, "/timeout")]


def test_a_failed_reload_names_its_own_change_when_a_publish_clears_the_shared_note_during_it(gw):
    gw.reload_error = RuntimeError("snapshot build failed")
    gw.during_reload = gw.reload_ok  # a publish clears the last-commit note while A's reload is still running
    response = gw.patch(setting("/timeout", 60))
    assert response.status_code == 202
    failed = gw.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()["reload_failed"]
    assert failed is not None and failed["change_id"] == response.json()["change_id"]
    assert [(e["section"], e["pointer"]) for e in failed["entries"]] == [(SECTION, "/timeout")]


def test_a_failed_reload_whose_cleaned_note_a_publish_cleared_records_only_the_error_type(gw):
    # The reload notes its cleaned failure text and releases the lock; another writer's reload publishes (clearing the
    # note) before A's handler reads it. The raw exception text can quote a stored value, so it is never recorded or
    # answered: only the error type.
    gw.reload_error = RuntimeError("snapshot build failed near a stored value")
    gw.after_failure_note = gw.reload_ok
    response = gw.patch(setting("/timeout", 60))
    assert response.status_code == 202
    failed = gw.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()["reload_failed"]
    assert failed["change_id"] == response.json()["change_id"]
    assert failed["error"] == response.json()["reload"]["error"] == "RuntimeError; see the gateway log"


def test_an_object_value_is_served_with_its_keys_in_the_order_the_owner_sent(gw):
    response = gw.client.patch(
        f"{API}/changes", headers=OWNER,
        content=b'{"items": [{"section": "fixture", "pointer": "/pair", "op": "set", "value": {"right": "R", "left": "L"}}],'
                b' "if_match": {"fixture": "0"}}')
    assert response.status_code == 200, response.text
    assert list(gw.entries()["/pair"]["value"]) == ["right", "left"]
    assert list(gw.home.holder.current().config[SECTION]["pair"]) == ["right", "left"]
    conn = sqlite3.connect(gw.db)
    try:
        stored = conn.execute("SELECT value_json FROM config_overrides WHERE pointer='/pair'").fetchone()[0]
    finally:
        conn.close()
    assert stored == '{"right": "R", "left": "L"}'


def test_a_legacy_import_keeps_the_key_order_of_the_startup_block(tmp_path):
    block = {**copy.deepcopy(DEFAULTS), "pair": {"right": "R", "left": "L"}}
    gw = Gateway(tmp_path, startup={SECTION: block})
    response = gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": "0"})
    assert response.status_code == 200, response.text
    assert list(gw.entries()["/pair"]["value"]) == ["right", "left"]


# ---------------------------------------------------------------------------------------------------------------------
# Export, import and the round trip
# ---------------------------------------------------------------------------------------------------------------------

def export_of(gw: Gateway, **kw: Any) -> dict[str, Any]:
    return config_api.build_export(gw.home, gw.db, version="test", **kw)


def import_body(document: Any, gw: Gateway, **extra: Any) -> dict[str, Any]:
    return {"document": document, "if_match": {SECTION: gw.revision()}, **extra}


def test_the_export_holds_overrides_and_secret_references_and_nothing_else(gw):
    gw.patch(setting("/timeout", 60), tombstone("/aliases/fast"), setting("/token", REF))
    document = export_of(gw)
    assert document["format"] == layers.EXPORT_FORMAT and document["release_digests"] == {SECTION: gw.home.sections[SECTION].release_digest}
    assert {row["pointer"] for row in document["sections"][SECTION]} == {"/timeout", "/aliases/fast", "/token"}
    assert document["secrets"] == [{"ref": REF, "used_by": f"{SECTION}/token"}]
    assert "svc-a" not in json.dumps(document), "no release content is exported"
    effective = export_of(gw, effective=True)
    assert effective["view"] == "effective" and effective["sections"][SECTION]["timeout"] == 60


def round_trip_changes(gw: Gateway) -> None:
    response = gw.patch(
        setting("/timeout", 60), setting("/tags", ["z"]), setting("/pair", {"right": "R", "left": "L"}),
        setting("/aliases/extra", {"engine": "eng-1"}), setting("/note", None), setting("/limits/min", 1), setting("/limits/max", 2),
        tombstone("/aliases/fast"), setting("/token", REF), setting("/policy", "off"), setting("/mode", "slow"),
    )
    assert response.status_code == 200, response.text


def test_round_trip_export_then_import_into_an_empty_owner_layer_gives_the_same_effective_config(tmp_path):
    source = Gateway(tmp_path, "a.db")
    round_trip_changes(source)
    target = Gateway(tmp_path, "b.db")
    assert target.digest() != source.digest()
    response = target.client.post(f"{API}/import", json=import_body(export_of(source), target), headers=OWNER)
    assert response.status_code == 200, response.text
    assert target.digest() == source.digest(), "export then import gives the same effective config"
    for gateway in (source, target):
        assert list(gateway.entries()["/pair"]["value"]) == ["right", "left"], "the owner's key order is served, not sorted"
        assert list(gateway.home.holder.current().config[SECTION]["pair"]) == ["right", "left"]
    assert export_of(target)["sections"] == export_of(source)["sections"], "and the same export"
    again = target.client.post(f"{API}/import", json=import_body(export_of(source), target), headers=OWNER)
    assert again.json()["change_id"] is None and again.json()["entry_count"] == 0, "importing the same file twice is a no-op"
    assert target.home.holder.current().config[SECTION]["timeout"] == 60, "the import reloaded"


def test_an_import_with_a_secret_reference_the_store_lacks_applies_the_entry_and_reports_needs_secret(tmp_path):
    source = Gateway(tmp_path, "a.db")
    source.patch(setting("/token", REF))
    target = Gateway(tmp_path, "b.db")
    body = target.client.post(f"{API}/import", json=import_body(export_of(source), target), headers=OWNER).json()
    assert body["needs_secret"] == [{"section": SECTION, "pointer": "/token", "code": "needs_secret"}]
    assert target.entries()["/token"]["value"] == REF


def test_import_replace_clears_the_overrides_the_file_does_not_name_and_merge_keeps_them(tmp_path):
    source = Gateway(tmp_path, "a.db")
    source.patch(setting("/timeout", 60))
    replace = Gateway(tmp_path, "r.db")
    replace.patch(setting("/tags", ["keep"]), setting("/timeout", 5))
    replace.client.post(f"{API}/import", json=import_body(export_of(source), replace), headers=OWNER)
    assert replace.digest() == source.digest()
    merge = Gateway(tmp_path, "m.db")
    merge.patch(setting("/tags", ["keep"]), setting("/timeout", 5))
    merge.client.post(f"{API}/import", json=import_body(export_of(source), merge, mode="merge"), headers=OWNER)
    assert merge.entries()["/tags"]["value"] == ["keep"] and merge.entries()["/timeout"]["value"] == 60


def test_an_import_dry_run_writes_nothing_and_gives_the_revisions_the_apply_needs(tmp_path):
    source = Gateway(tmp_path, "a.db")
    source.patch(setting("/timeout", 60))
    target = Gateway(tmp_path, "b.db")
    dry = target.client.post(f"{API}/import?dry_run=1", json={"document": export_of(source)}, headers=OWNER)
    body = dry.json()
    assert dry.status_code == 200 and body["valid"] and body["revisions"] == {SECTION: "0"}
    assert [d["pointer"] for d in body["diff"]] == ["/timeout"] and target.counts() == (0, 0) and target.reloads == 0
    applied = target.client.post(f"{API}/import", json={"document": export_of(source), "if_match": body["revisions"]}, headers=OWNER)
    assert applied.status_code == 200 and target.digest() == source.digest()
    no_precondition = Gateway(tmp_path, "c.db").client.post(f"{API}/import", json={"document": export_of(source)}, headers=OWNER)
    assert no_precondition.status_code == 428


def test_an_import_lists_unknown_sections_and_refuses_what_it_cannot_read_or_what_breaks_a_rule(tmp_path):
    target = Gateway(tmp_path)
    document = export_of(target)
    document["sections"] = {"elsewhere": [{"pointer": "/a", "op": "set", "value": 1}]}
    skipped = target.client.post(f"{API}/import", json=import_body(document, target), headers=OWNER)
    assert skipped.status_code == 200 and skipped.json()["skipped_sections"] == ["elsewhere"]
    for bad in ({"format": "other/v9"}, {**document, "sections": {SECTION: [{"pointer": "/timeout", "op": "set"}]}}):
        refused = target.client.post(f"{API}/import", json=import_body(bad, target), headers=OWNER)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "bad_export"
    rule = {**document, "sections": {SECTION: [{"pointer": "/limits/min", "op": "set", "value": 20}]}}
    broken = target.client.post(f"{API}/import", json=import_body(rule, target), headers=OWNER)
    assert broken.status_code == 422 and "limits_order" in broken.json()["problems"][0]["message"]
    assert target.client.post(f"{API}/import", json={"document": document, "mode": "x"}, headers=OWNER).status_code == 400
    assert target.counts() == (0, 0)


# ---------------------------------------------------------------------------------------------------------------------
# The legacy import
# ---------------------------------------------------------------------------------------------------------------------

LEGACY_BLOCK = {
    "timeout": 45, "mode": "slow", "policy": "on", "tags": ["a", "b"], "limits": {"min": 5, "max": 10},
    "pair": {"left": "l", "right": "r"},
    "aliases": {"default": {"target": "svc-live"}, "extra": {"engine": "eng-1"}},
}


def test_the_fewest_overrides_that_make_the_section_equal_the_file_block():
    section = layers.Section.create(SECTION, copy.deepcopy(DEFAULTS), SCHEMA, validator=limits_in_order)
    overrides, blocked = layers.overrides_for_target(section, LEGACY_BLOCK)
    assert blocked == []
    assert [(o.pointer, o.op) for o in overrides] == [
        ("/aliases/default", "set"), ("/aliases/extra", "set"), ("/mode", "set"), ("/timeout", "set"),
        ("/aliases/fast", "delete")]
    assert next(o for o in overrides if o.pointer == "/timeout").base == 30
    assert next(o for o in overrides if o.pointer == "/aliases/extra").base is layers.ABSENT
    effective = layers.apply_overrides(section.defaults, overrides)
    assert layers.digest(effective) == layers.digest(LEGACY_BLOCK), "the effective section equals the legacy block"
    assert layers.overrides_for_target(section, copy.deepcopy(DEFAULTS))[0] == [], "a block equal to the release gives no row"


def test_a_scalar_the_file_leaves_out_gives_no_row_and_a_member_it_leaves_out_gives_a_tombstone():
    section = layers.Section.create(SECTION, copy.deepcopy(DEFAULTS), SCHEMA, validator=limits_in_order)
    overrides, blocked = layers.overrides_for_target(section, {"aliases": {"default": {"target": "svc-a"}}})
    assert blocked == [] and [(o.pointer, o.op) for o in overrides] == [("/aliases/fast", "delete")]


@pytest.mark.parametrize("block,code", [
    ({"surprise": 1}, "legacy_key_unknown"),
    ({"timeout": "ten"}, "schema.type"),
    ({"limits": {"min": 20, "max": 10}}, "limits_order"),
    ({"token": "plain"}, "secret_value"),
    ([], "not_an_object"),
])
def test_a_block_the_section_cannot_take_blocks_the_import_and_names_why(block, code):
    section = layers.Section.create(SECTION, copy.deepcopy(DEFAULTS), SCHEMA, validator=limits_in_order)
    overrides, blocked = layers.overrides_for_target(section, block)
    assert code in {p.code for p in blocked}


def test_the_legacy_import_route_shows_the_whole_report_in_a_dry_run_then_applies_it(tmp_path):
    gw = Gateway(tmp_path, startup={SECTION: copy.deepcopy(LEGACY_BLOCK)})
    dry = gw.client.post(f"{API}/sections/{SECTION}/legacy-import?dry_run=1", headers=OWNER).json()
    assert dry["applicable"] and gw.counts() == (0, 0)
    assert {r["pointer"]: (r["file_value"], r.get("release_value")) for r in dry["rows"]} == {
        "/timeout": (45, 30), "/mode": ("slow", "fast"), "/aliases/default": ({"target": "svc-live"}, {"target": "svc-a"}),
        "/aliases/extra": ({"engine": "eng-1"}, None)}
    assert dry["removes"] == [{"pointer": "/aliases/fast", "release_value": {"target": "svc-b"}}]
    response = gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": '"0"'})
    assert response.status_code == 200 and response.json()["rows"] == dry["rows"], response.text
    assert layers.digest(gw.home.holder.current().config[SECTION]) == layers.digest(LEGACY_BLOCK)
    change = gw.client.get(f"{API}/changes", headers=OWNER).json()["changes"][0]
    assert change["kind"] == "legacy-import" and len(change["items"]) == 5
    conn = sqlite3.connect(gw.db)
    assert {r[0] for r in conn.execute("SELECT origin FROM config_overrides")} == {"legacy-import"}
    conn.close()


def test_a_second_legacy_import_after_the_file_changed_is_a_new_change_that_also_resets_what_it_drops(tmp_path):
    gw = Gateway(tmp_path, startup={SECTION: copy.deepcopy(LEGACY_BLOCK)})
    gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": "0"})
    again = gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": "1"}).json()
    assert again["change_id"] is None, "the same block again changes nothing"
    gw.startup[SECTION] = {**copy.deepcopy(DEFAULTS), "timeout": 99}
    gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": "1"})
    assert gw.home.holder.current().config[SECTION] == {**DEFAULTS, "timeout": 99}
    assert {p for p in gw.entries() if gw.entries()[p]["source"] != "release"} == {"/timeout"}


def test_a_blocked_legacy_import_writes_nothing_and_names_each_bad_pointer(tmp_path):
    gw = Gateway(tmp_path, startup={SECTION: {"timeout": "ten", "surprise": 1}})
    response = gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": "0"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "legacy_import_blocked"
    assert {p["pointer"] for p in response.json()["problems"]} >= {"/timeout", "/surprise"}
    assert gw.counts() == (0, 0)
    dry = gw.client.post(f"{API}/sections/{SECTION}/legacy-import?dry_run=1", headers=OWNER).json()
    assert dry["applicable"] is False and dry["blocked"]


def test_a_legacy_import_with_no_startup_block_or_an_unknown_section_is_refused(gw):
    assert gw.client.post(f"{API}/sections/{SECTION}/legacy-import", headers=OWNER).status_code == 409
    assert gw.client.post(f"{API}/sections/nope/legacy-import", headers=OWNER).status_code == 404


# ---------------------------------------------------------------------------------------------------------------------
# The command line: HTTP only
# ---------------------------------------------------------------------------------------------------------------------

class CliRun:
    """The command line over the test app: its transport is the app's own client, and it opens no database."""

    def __init__(self, gw: Gateway, capsys: Any) -> None:
        self.gw, self.capsys, self.err = gw, capsys, ""

    def transport(self, method: str, url: str, headers: dict[str, str], body: bytes | None):
        assert headers["Authorization"] == "Bearer owner-key"
        response = self.gw.client.request(method, url.removeprefix("http://gateway.test"), content=body,
                                          headers={"test-role": "owner", **{k: v for k, v in headers.items() if k != "Authorization"}})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def __call__(self, *argv: str) -> tuple[int, str]:
        code = config_cli.main(["--url", "http://gateway.test", *argv], transport=self.transport)
        seen = self.capsys.readouterr()
        self.err = seen.err
        return code, seen.out


@pytest.fixture
def cli(gw, capsys, monkeypatch) -> CliRun:
    monkeypatch.setenv("AZIZAM_ADMIN_KEY", "owner-key")
    return CliRun(gw, capsys)


def test_the_command_line_gets_sets_resets_and_dry_runs_through_the_api(cli, gw):
    code, out = cli("set", SECTION, "/timeout", "60", "--note", "slower")
    assert code == 0 and json.loads(out)["revisions"] == {SECTION: "1"}
    code, out = cli("get", SECTION, "/timeout")
    entry = json.loads(out)["entries"][0]
    assert code == 0 and (entry["value"], entry["source"], entry["override"]["note"]) == (60, "owner", "slower")
    code, out = cli("delete", SECTION, "/aliases/fast", "--dry-run")
    assert code == 0 and json.loads(out)["diff"][0]["after"]["source"] == "owner-deleted"
    assert gw.entries()["/aliases/fast"]["source"] == "release", "a dry run wrote nothing"
    assert cli("reset", SECTION, "/timeout")[0] == 0 and gw.entries()["/timeout"]["source"] == "release"
    assert json.loads(cli("get")[1])["sections"][0]["id"] == SECTION
    assert cli("drift")[0] == 0 and cli("problems")[0] == 0


def test_the_command_line_reports_a_refusal_and_exits_non_zero(cli):
    assert cli("set", SECTION, "/timeout", "0")[0] == 1
    assert "422 config_invalid" in cli.err and "override_rejected" in cli.err
    assert cli("set", SECTION, "/timeout", "not json")[0] == 1


def test_the_command_line_exports_and_imports_with_a_dry_run_first(tmp_path, capsys, monkeypatch):
    source = Gateway(tmp_path, "a.db")
    round_trip_changes(source)
    target = Gateway(tmp_path, "b.db")
    monkeypatch.setenv("AZIZAM_ADMIN_KEY", "owner-key")
    out_file = tmp_path / "export.json"
    assert config_cli.main(["--url", "http://gateway.test", "export", "-o", str(out_file)],
                           transport=CliRun(source, capsys).transport) == 0
    assert json.loads(out_file.read_text(encoding="utf-8")) == {**export_of(source), "exported_at": json.loads(out_file.read_text(encoding="utf-8"))["exported_at"]}
    run = CliRun(target, capsys)
    assert config_cli.main(["--url", "http://gateway.test", "import", str(out_file)], transport=run.transport) == 0
    assert target.counts() == (0, 0), "without --apply it is a dry run"
    assert "dry run only" in capsys.readouterr().err
    assert config_cli.main(["--url", "http://gateway.test", "import", str(out_file), "--apply"], transport=run.transport) == 0
    assert target.digest() == source.digest()
    assert list(target.entries()["/pair"]["value"]) == ["right", "left"], "export and import keep the owner's key order"


def _redirecting(seen: list[tuple[str, dict[str, str]]], target: Callable[[], str]):
    """An upstream that records each request and answers the config API with a 302 to ``target()``."""
    async def app(scope, receive, send) -> None:
        await receive()
        seen.append((scope["path"], {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}))
        if scope["path"].startswith("/admin/api/config"):
            await send({"type": "http.response.start", "status": 302, "headers": [(b"location", target().encode())]})
            await send({"type": "http.response.body", "body": b""})
        else:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b"{}"})
    return app


def test_the_command_line_does_not_follow_a_redirect_and_never_sends_the_key_elsewhere(tmp_path, capsys, monkeypatch):
    seen: list[tuple[str, dict[str, str]]] = []
    other = LoopbackServer(_redirecting(seen, lambda: "")).start()
    front = LoopbackServer(_redirecting(seen, lambda: f"{other.base_url}/stolen")).start()
    try:
        monkeypatch.setenv("AZIZAM_ADMIN_KEY", "owner-key-secret")
        code = config_cli.main(["--url", front.base_url, "get"])
    finally:
        front.stop()
        other.stop()
    captured = capsys.readouterr()
    assert code == 1 and "HTTP 302" in captured.err and "redirect" in captured.err
    assert [path for path, _ in seen] == ["/admin/api/config"], "the redirect target was never asked"
    assert "owner-key-secret" not in captured.out + captured.err


def test_the_command_line_needs_an_address_and_a_key(monkeypatch, capsys):
    monkeypatch.delenv("AZIZAM_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AZIZAM_URL", raising=False)
    monkeypatch.delenv("AZIZAM_BASE_URL", raising=False)
    with pytest.raises(SystemExit) as stop:
        config_cli.main(["get"])
    assert stop.value.code == 2


def test_the_command_line_module_reads_no_database_and_loads_no_gateway_code():
    root = Path(__file__).resolve().parent.parent
    probe = (f"import sys; sys.path.insert(0, {str(root)!r}); import gateway.config_cli; "
             "bad = [m for m in ('sqlite3', 'gateway.admin_store', 'fastapi', 'proxy') if m in sys.modules]; "
             "print(bad)")
    result = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------------------------------------------------------------
# Mounted in the gateway
# ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def mounted(live, monkeypatch):  # noqa: F811
    db = live
    h = make_home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    monkeypatch.setattr(proxy, "CONFIG_PROBLEMS", {})
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None, raising=False)
    proxy.reload_runtime_config()
    owner, _, _ = create_admin_service_account_key("config-owner", role="owner", path=db)
    admin, _, _ = create_admin_service_account_key("config-admin", role="admin", path=db)
    return TestClient(proxy.app), {"Authorization": f"Bearer {owner}"}, {"Authorization": f"Bearer {admin}"}, h


def test_in_the_gateway_the_routes_are_owner_only_and_a_write_reaches_the_published_snapshot(mounted):
    client, owner, admin, h = mounted
    assert client.get(API).status_code == 401
    assert client.get(API, headers=admin).status_code == 403
    assert client.get(API, headers=owner).json()["sections"][0]["id"] == SECTION
    revision = client.get(f"{API}/sections/{SECTION}", headers=owner).json()["revision"]
    before = proxy.runtime()
    response = client.patch(f"{API}/changes", json={"items": [setting("/timeout", 60)], "if_match": {SECTION: revision}}, headers=owner)
    assert response.status_code == 200 and response.json()["reload"] == {"status": "ok"}, response.text
    assert proxy.runtime() is not before and proxy.runtime().config[SECTION]["timeout"] == 60
    assert proxy.config[SECTION]["timeout"] == 60 and h.holder.current() is proxy.runtime()
    assert client.patch(f"{API}/changes", json={"items": [setting("/timeout", 70)], "if_match": {SECTION: revision}},
                        headers=admin).status_code == 403


def test_in_the_gateway_the_export_route_answers_the_overrides_with_format_v1_and_still_the_registry_yaml_without_it(mounted):
    client, owner, admin, _ = mounted
    assert client.get(f"{API}/export?format=v1", headers=admin).status_code == 403
    assert client.get(f"{API}/export?format=v1").status_code == 401
    revision = client.get(f"{API}/sections/{SECTION}", headers=owner).json()["revision"]
    client.patch(f"{API}/changes", json={"items": [setting("/timeout", 60)], "if_match": {SECTION: revision}}, headers=owner)
    document = client.get(f"{API}/export?format=v1", headers=owner).json()
    assert document["sections"][SECTION][0]["pointer"] == "/timeout"
    assert client.get(f"{API}/export?format=v1&view=effective", headers=owner).json()["sections"][SECTION]["timeout"] == 60
    assert client.get(f"{API}/export", headers=owner).text.startswith("# Gateway service registry")


def test_the_proxy_command_line_hands_config_to_the_http_client(monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(config_cli, "main", lambda argv, **kw: seen.update(argv=list(argv), **kw) or 0)
    monkeypatch.setattr(sys, "argv", ["proxy.py", "config", "get", SECTION])
    with pytest.raises(SystemExit) as stop:
        proxy.handle_cli_command()
    assert stop.value.code == 0 and seen["argv"] == ["get", SECTION]
    assert seen["default_url"] == f"http://127.0.0.1:{proxy.PROXY_PORT}"
