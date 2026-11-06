"""Aliases under the config contract (docs/design/CONFIG_HOMES.md, slice C3; acceptance 10.1).

``model_aliases`` is a config section like any other: release defaults in ``registry/defaults``, the owner's changes as
override rows in admin.db, one boot import of the startup file's block. These tests hold every C3 acceptance item:

- fresh install, tracked file and live-like file against the C0 goldens;
- a loop or an unknown engine refused on write and skipped on load, a gone service shown dangling;
- an owner edit that is live at once, also for ``VIDEO_ALIASES`` and ``AUDIO_ALIASES``;
- the ignored startup block, logged once;
- the rollback of 8.2;
- the dry-run command the live move uses.
"""
from __future__ import annotations

import copy
import io
import json
import re
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, config_api, config_layers as layers, config_legacy_import, config_rules, config_runtime
from gateway import stable_aliases
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_runtime import create_admin_service_account_key
from gateway.stable_aliases import AliasUnavailableError, alias_problems, resolve_alias, validate_aliases
from tests import config_homes_golden as golden
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture

SECTION = "model_aliases"
STEP = "config-homes-import:model_aliases"
API = "/admin/api/config"
OWNER = {"test-role": "owner"}
ROOT = Path(__file__).resolve().parent.parent


def release_aliases() -> dict[str, Any]:
    return copy.deepcopy(config_rules.config_home().sections[SECTION].defaults)


def boot(home: config_runtime.ConfigHome, db: Path, raw: dict[str, Any], *, registry: Any = None, log: Any = None):
    """One boot's layer: the import first, then the build (what ``proxy._layer_for(..., boot=True)`` does)."""
    admin_store.init_db(db)
    return home.layer(raw, path=db, registry=registry, legacy_import=True, log=log or (lambda line: None))


def rows(db: Path) -> list[dict[str, Any]]:
    return admin_store.read_config_state(path=db)["rows"]


class AliasApp:
    """The config router over the real ``model_aliases`` section and a scratch database, with the gateway's reload."""

    def __init__(self, tmp_path: Path, *, startup: dict[str, Any] | None = None, registry: Any = None,
                 db: Path | None = None) -> None:
        self.db = db or tmp_path / "admin.db"
        admin_store.init_db(self.db)
        self.home = config_rules.config_home()
        self.registry = registry if registry is not None else {"services": {}}
        self.startup = startup if startup is not None else {}
        app = FastAPI()
        app.include_router(config_api.create_config_router(
            auth=self.auth, admin_path=self.db, home=lambda: self.home, registry=lambda: self.registry,
            reload=self.reload, startup_block=self.startup.get))
        self.client = TestClient(app)
        self.reload()

    @staticmethod
    def auth(request: Any) -> AdminContext:
        if not request.headers.get("test-role"):
            raise AdminAuthError()
        return AdminContext(subject="tester", roles=frozenset({"owner"}), auth_method="admin_key")

    def reload(self) -> None:
        layer = self.home.layer({}, path=self.db, registry=self.registry)
        self.home.publish(self.home.snapshot(self.home.attach(dict(layer.config), layer), layer=layer))

    def effective(self) -> dict[str, Any]:
        return self.home.holder.current().config[SECTION]

    def problems(self) -> list[dict[str, Any]]:
        return self.home.layer({}, path=self.db, registry=self.registry).problems.get(SECTION, [])

    def revision(self) -> str:
        return self.client.get(f"{API}/sections/{SECTION}", headers=OWNER).json()["revision"]

    def set(self, *edits: tuple[str, Any]):
        items = [{"section": SECTION, "pointer": pointer, "op": "set", "value": value} for pointer, value in edits]
        return self.client.patch(f"{API}/changes", json={"items": items, "if_match": {SECTION: self.revision()}},
                                 headers=OWNER)


# ---------------------------------------------------------------------------------------------------------------------
# The release defaults and the unconfigured answer
# ---------------------------------------------------------------------------------------------------------------------

def test_the_release_defaults_name_no_model_service_or_engine():
    defaults = release_aliases()
    assert set(defaults) <= set(stable_aliases.OPERATIONS)
    for operation, table in defaults.items():
        assert set(table) == {"stable"}, f"{operation}: engines and patterns name services and models; the owner adds them"
        assert all(target is None for target in table["stable"].values()), operation
    assert validate_aliases(defaults) == [] and alias_problems(defaults, {"registry": None}) == []


def test_a_fresh_install_serves_the_neutral_defaults_and_default_answers_alias_not_configured(tmp_path):
    home, db, lines = config_rules.config_home(), tmp_path / "admin.db", []
    layer = boot(home, db, {}, log=lines.append)
    aliases = layer.config[SECTION]
    assert aliases == release_aliases() and rows(db) == [] and lines == []
    assert admin_store.migration_step_recorded(STEP, path=db), "no block to import: the step is recorded all the same"
    for operation, table in aliases.items():
        for name in table["stable"]:
            with pytest.raises(AliasUnavailableError) as caught:
                resolve_alias(aliases, operation, name)
            assert caught.value.code == "alias_not_configured"
    assert "'default'" in str(caught.value) or "set it up" in str(caught.value)
    view = stable_aliases.describe_aliases(aliases)
    assert all(row.get("configured") is False and row["resolves_to"] is None for row in view["chat"]["stable"])


def test_a_request_for_an_unconfigured_name_is_a_503_with_the_code_not_a_crash():
    aliases = release_aliases()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(proxy, "config", {**proxy.config, SECTION: aliases})
        with pytest.raises(proxy.ModelCompatibilityError) as caught:
            proxy.resolve_model_for_operation("chat", "default")
    response = proxy.model_compatibility_error(caught.value)
    body = json.loads(response.body)["error"]
    assert response.status_code == 503 and body["code"] == "alias_not_configured" and body["param"] == "model"
    assert "default" in body["message"], "the API audience is told which model"


def test_an_unconfigured_name_is_unlisted_for_clients_and_still_shown_to_the_owner(monkeypatch):
    aliases = {"chat": {"stable": {"default": None, "fast": "some-role"}}}
    monkeypatch.setattr(proxy, "_gateway_aliases_provider", lambda operation=None: stable_aliases.describe_aliases(aliases))
    assert [entry["id"] for entry in proxy._stable_alias_model_entries()] == ["fast"]
    owner_view = proxy._gateway_aliases_provider()["chat"]["stable"]
    assert {row["alias"]: row.get("configured", True) for row in owner_view} == {"default": False, "fast": True}


def test_a_chain_or_a_pattern_that_ends_at_an_unconfigured_name_answers_the_same_way():
    aliases = {"chat": {"stable": {"a": "b", "b": None}, "openai": [{"match": "gpt-x*", "to": "b"}]}}
    for name in ("a", "b", "gpt-x1"):
        with pytest.raises(AliasUnavailableError) as caught:
            resolve_alias(aliases, "chat", name)
        assert caught.value.code == "alias_not_configured", name
    assert resolve_alias({"chat": {"stable": {"a": "role"}}}, "chat", "a").target == "role"
    assert validate_aliases(aliases) == [], "null is a state, not a fault"
    assert any("non-empty" in problem for problem in validate_aliases({"chat": {"stable": {"a": ""}}}))


# ---------------------------------------------------------------------------------------------------------------------
# The rule: invalid, or dangling
# ---------------------------------------------------------------------------------------------------------------------

def test_the_rule_names_the_entry_of_each_problem_and_its_severity():
    loop = {"chat": {"stable": {"a": "b", "b": "a"}}}
    unknown = {"speech": {"engines": {}, "stable": {"tts": ["ghost"]}}}
    odd = {"chat": {"engines": {"e": {"service": "s", "model": "m"}}}}
    found = {(p.pointer, p.severity) for p in alias_problems(loop)}
    assert found == {("/chat/stable/a", layers.INVALID), ("/chat/stable/b", layers.INVALID)}
    assert {(p.pointer, p.severity) for p in alias_problems(unknown)} == {("/speech/stable/tts", layers.INVALID)}
    assert {p.pointer for p in alias_problems(odd)} == {"/chat/engines"}
    assert [m for m in validate_aliases(loop)] == ["chat.stable.a: alias loop", "chat.stable.b: alias loop"]


def test_an_engine_whose_service_is_gone_dangles_only_when_a_registry_is_known():
    candidate = {"speech": {"engines": {"e1": {"service": "gone", "model": "m"}}, "stable": {"tts": ["e1"]}}}
    assert alias_problems(candidate, {"registry": None}) == [] and alias_problems(candidate, {}) == []
    problems = alias_problems(candidate, {"registry": {"services": {"here": {}}}})
    assert [(p.pointer, p.code, p.severity) for p in problems] == [("/speech/engines/e1", "service_gone", layers.DANGLING)]
    assert alias_problems(candidate, {"registry": {"services": {"gone": {}}}}) == []


def test_the_schema_takes_any_operation_id_and_the_operation_list_is_one_tuple(monkeypatch):
    section = config_rules.config_home().sections[SECTION]
    assert section.index.rule_for("/newop/stable/x") is not None, "no schema edit for a new operation"
    assert section.index.rule_for("/chat/openai").kind == "list"
    assert section.index.rule_for("/speech/engines/e1").kind == "member"
    candidate = {"newop": {"stable": {"x": "target"}}}
    assert [p.message for p in layers.validate_section(section, candidate, {})] == ["unknown operation section: newop"]
    monkeypatch.setattr(stable_aliases, "OPERATIONS", (*stable_aliases.OPERATIONS, "newop"))
    assert layers.validate_section(section, candidate, {}) == []
    assert layers.validate_section(section, {"Bad-Op": {}}, {}), "an id that is not a lower-case word is a structure fault"


def test_a_loop_or_an_unknown_engine_is_refused_on_write_with_422_and_writes_nothing(tmp_path):
    app = AliasApp(tmp_path)
    loop = app.set(("/chat/stable/default", "fast"), ("/chat/stable/fast", "default"))
    assert loop.status_code == 422 and loop.json()["error"]["code"] == "config_invalid"
    assert {p["pointer"] for p in loop.json()["problems"]} <= {"/chat/stable/default", "/chat/stable/fast"}
    assert loop.json()["problems"] and "alias_invalid" in loop.json()["problems"][0]["message"]
    unknown = app.set(("/speech/stable/tts", ["ghost"]))
    assert unknown.status_code == 422
    assert [p["pointer"] for p in unknown.json()["problems"]] == ["/speech/stable/tts"]
    assert rows(app.db) == [] and app.effective() == release_aliases()
    fine = app.set(("/speech/engines/e1", {"service": "svc", "model": "m"}), ("/speech/stable/tts", ["e1"]))
    assert fine.status_code == 200, fine.text


def test_a_loop_or_an_unknown_engine_that_reached_the_store_is_skipped_on_load(tmp_path):
    home, db = config_rules.config_home(), tmp_path / "admin.db"
    admin_store.init_db(db)
    items = [admin_store.ConfigItem(SECTION, "/chat/stable/default", "set", json.dumps("fast")),
             admin_store.ConfigItem(SECTION, "/chat/stable/fast", "set", json.dumps("default")),
             admin_store.ConfigItem(SECTION, "/speech/stable/tts", "set", json.dumps(["ghost"]))]
    admin_store.apply_config_change(items, if_match={SECTION: "0"}, validate=None, path=db)
    layer = home.layer({}, path=db, registry={"services": {}})
    effective = layer.config[SECTION]
    assert validate_aliases(effective) == [], "what serves is valid"
    assert effective.get("speech", {}).get("stable", {}).get("tts") is None
    assert "override_rejected" in {p["code"] for p in layer.problems[SECTION]}
    with pytest.raises(AliasUnavailableError) as caught:
        resolve_alias(effective, "chat", "default")
    assert caught.value.code in {"alias_not_configured", "alias_misconfigured"} and "loop" not in str(caught.value)


def test_an_alias_to_a_service_that_is_gone_is_applied_and_shown_dangling_never_reverted(tmp_path):
    app = AliasApp(tmp_path, registry={"services": {}})
    done = app.set(("/speech/engines/e1", {"service": "gone", "model": "m"}), ("/speech/stable/tts", ["e1"]))
    assert done.status_code == 200, done.text
    assert app.effective()["speech"]["stable"]["tts"] == ["e1"] and "e1" in app.effective()["speech"]["engines"]
    assert [(p["pointer"], p["severity"]) for p in app.problems()] == [("/speech/engines/e1", layers.DANGLING)]
    assert app.home.holder.current().problems["config"][SECTION] == app.problems(), "the snapshot carries it"
    with pytest.raises(AliasUnavailableError) as caught:
        resolve_alias(app.effective(), "speech", "tts", engine_available=lambda *_: False)
    assert caught.value.code == "model_not_deployed", "the request still answers; the owner sees the warning"
    # The service comes back: the same rows, no warning.
    app.registry = {"services": {"gone": {}}}
    app.reload()
    assert app.problems() == [] and SECTION not in (app.home.holder.current().problems.get("config") or {})


# ---------------------------------------------------------------------------------------------------------------------
# The boot import
# ---------------------------------------------------------------------------------------------------------------------

def test_the_import_of_the_tracked_file_gives_the_c0_golden_and_the_block_itself(tmp_path):
    raw = golden.raw_config("tracked")
    effective = golden.effective_config(proxy, "tracked", tmp_path / "admin.db")
    want = golden.load_golden()["inputs"]["tracked"]
    assert golden.digest(effective[SECTION]) == want["section_digests"][SECTION]
    assert golden.digest(effective[SECTION]) == golden.digest(raw[SECTION]), "the effective section is the legacy block"
    assert golden.digest(effective) == want["effective_digest"]
    assert golden.alias_table(proxy, effective) == want["aliases"]
    assert effective["model_aliases"]["chat"]["openai"] == raw[SECTION]["chat"]["openai"], "the pattern list keeps its order"


def test_the_live_like_import_report_holds_the_three_live_pointers_and_gives_the_golden(tmp_path):
    raw = golden.raw_config("live_like")
    home, db = config_rules.config_home(), tmp_path / "scratch.db"
    admin_store.init_db(db)
    outcome = config_legacy_import.import_section(home, SECTION, raw[SECTION], path=db, registry=None, apply=False)
    report = outcome.summary()
    assert outcome.status == "dry_run" and rows(db) == [] and not admin_store.migration_step_recorded(STEP, path=db)
    by_pointer = {row["pointer"]: row for row in report["rows"]}
    for pointer in ("/chat/stable/default", "/chat/stable/fast", "/chat/stable/default-fast"):
        assert pointer in by_pointer, pointer
    assert by_pointer["/chat/stable/default"]["file_value"] == raw[SECTION]["chat"]["stable"]["default"]
    assert "release_value" not in by_pointer["/chat/stable/default-fast"], "an entry only the file has"
    assert report["row_count"] == len(report["rows"]) and report["removes"] == []
    applied = config_legacy_import.import_section(home, SECTION, raw[SECTION], path=db, registry=None)
    assert applied.status == "imported" and admin_store.migration_step_recorded(STEP, path=db)
    stored = {row["pointer"] for row in rows(db)}
    assert stored == set(by_pointer) and {row["origin"] for row in rows(db)} == {"legacy-import"}
    effective = golden.effective_config(proxy, "live_like", tmp_path / "admin.db")
    want = golden.load_golden()["inputs"]["live_like"]
    assert golden.digest(effective[SECTION]) == want["section_digests"][SECTION]
    assert golden.alias_table(proxy, effective) == want["aliases"]


def test_the_import_is_one_change_of_kind_legacy_import_that_records_its_step(tmp_path):
    home, db = config_rules.config_home(), tmp_path / "admin.db"
    boot(home, db, golden.raw_config("tracked"))
    conn = sqlite3.connect(db)
    try:
        changes = conn.execute("SELECT kind, note FROM config_changes").fetchall()
        steps = [r[0] for r in conn.execute("SELECT name FROM schema_migration_steps")]
    finally:
        conn.close()
    assert [kind for kind, _note in changes] == ["legacy-import"] and STEP in steps


def test_a_block_that_breaks_the_rule_is_not_imported_keeps_serving_and_is_retried_next_boot(tmp_path):
    home, db, lines = config_rules.config_home(), tmp_path / "admin.db", []
    broken = {"chat": {"stable": {"a": "b", "b": "a"}}}
    layer = boot(home, db, {SECTION: broken}, log=lines.append)
    assert layer.config[SECTION] == broken, "the file block stays in use as before"
    assert rows(db) == [] and not admin_store.migration_step_recorded(STEP, path=db)
    assert {p["code"] for p in layer.problems[SECTION]} == {"legacy_import_blocked"}
    assert len(lines) == 1 and "was not imported" in lines[0]
    reloaded = home.layer({SECTION: broken}, path=db, registry=None)
    assert reloaded.config[SECTION] == broken, "a reload keeps serving the block, as the boot did"
    fixed = {"chat": {"stable": {"a": "role", "b": "a"}}}
    after = boot(home, db, {SECTION: fixed})
    assert admin_store.migration_step_recorded(STEP, path=db)
    assert after.config[SECTION]["chat"]["stable"]["a"] == "role" and SECTION not in after.problems


def test_an_import_that_hits_a_store_fault_keeps_the_file_block_and_a_missing_block_keeps_the_defaults(tmp_path, monkeypatch):
    home, db, lines = config_rules.config_home(), tmp_path / "admin.db", []
    block = {"chat": {"stable": {"a": "role"}}}

    def fault(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(admin_store, "apply_config_change", fault)
    with_block = boot(home, db, {SECTION: block}, log=lines.append)
    assert with_block.config[SECTION] == block, "a transient fault must not drop the names the file had"
    assert not admin_store.migration_step_recorded(STEP, path=db), "the step stays open for the next start"
    assert len(lines) == 1 and "failed" in lines[0] and "locked" in lines[0]
    without = boot(config_rules.config_home(), tmp_path / "other.db", {}, log=lines.append)
    assert without.config[SECTION] == release_aliases(), "no block, nothing to keep serving: the release defaults"


def test_a_block_with_a_key_the_section_does_not_take_is_blocked_not_lost(tmp_path):
    home, db = config_rules.config_home(), tmp_path / "admin.db"
    block = {"chat": {"stable": {"a": "role"}, "surprise": 1}}
    layer = boot(home, db, {SECTION: block})
    assert layer.config[SECTION] == block and rows(db) == []
    assert {p["code"] for p in layer.problems[SECTION]} == {"legacy_import_blocked"}


def test_after_the_step_is_recorded_an_edit_to_the_file_block_is_ignored_and_logged_once(tmp_path):
    db, lines = tmp_path / "admin.db", []
    raw = golden.raw_config("tracked")
    first = boot(config_rules.config_home(), db, raw, log=lines.append)
    assert lines and "imported" in lines[0] and "ignored from now on" in lines[0]
    before = golden.digest(first.config[SECTION])
    edited = copy.deepcopy(raw)
    edited[SECTION]["chat"]["stable"]["default"] = "edited-in-the-file"
    edited[SECTION]["chat"]["stable"]["fast"] = "also-edited"
    lines.clear()
    home = config_rules.config_home()  # a restart
    second = boot(home, db, edited, log=lines.append)
    third = boot(home, db, edited, log=lines.append)  # a second build in the same boot (a fallback path)
    assert golden.digest(second.config[SECTION]) == before == golden.digest(third.config[SECTION])
    assert len(lines) == 1, lines
    assert "model_aliases block is ignored" in lines[0] and re.search(r"\b2 of its entries differ\b", lines[0])
    lines.clear()
    boot(config_rules.config_home(), db, raw, log=lines.append)
    assert lines == [], "a file equal to what runs says nothing"
    assert len(rows(db)) > 0 and all(row["origin"] == "legacy-import" for row in rows(db))


def test_a_file_with_no_block_records_the_step_so_a_later_block_is_ignored(tmp_path):
    db, lines = tmp_path / "admin.db", []
    boot(config_rules.config_home(), db, {}, log=lines.append)
    later = boot(config_rules.config_home(), db, golden.raw_config("tracked"), log=lines.append)
    assert later.config[SECTION] == release_aliases() and rows(db) == []
    assert len(lines) == 1 and "ignored" in lines[0]


def test_the_boot_runs_the_import_and_a_reload_never_does(tmp_path):
    home, db = config_rules.config_home(), tmp_path / "admin.db"
    admin_store.init_db(db)
    raw = golden.raw_config("tracked")
    reloaded = home.layer(raw, path=db, registry=None)
    assert reloaded.config[SECTION] == release_aliases() and not admin_store.migration_step_recorded(STEP, path=db)
    boot(home, db, raw)
    assert admin_store.migration_step_recorded(STEP, path=db)


def test_two_boots_cannot_both_import(tmp_path):
    home, db = config_rules.config_home(), tmp_path / "admin.db"
    admin_store.init_db(db)
    block = golden.raw_config("tracked")[SECTION]
    plan = config_legacy_import.plan_import(home, SECTION, block, config_legacy_import.rows_by_section(
        home, admin_store.read_config_state(path=db)), None)
    items = plan.items()
    kwargs = dict(kind="legacy-import", origin="legacy-import", validate=None, migration_step=STEP, path=db)
    first = admin_store.apply_config_change(items, if_match={SECTION: "0"}, **kwargs)
    second = admin_store.apply_config_change(items, if_match={SECTION: "0"}, **kwargs)
    assert first["entry_count"] == len(items) and second == {"change_id": None, "skipped": True, "entry_count": 0, "revisions": {}}


# ---------------------------------------------------------------------------------------------------------------------
# Live: an owner edit reaches the next request
# ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def mounted(live, monkeypatch):  # noqa: F811
    monkeypatch.setattr(proxy, "CONFIG_HOME", config_rules.config_home())
    monkeypatch.setattr(proxy, "CONFIG_PROBLEMS", {})
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None, raising=False)
    proxy.reload_runtime_config()
    key, _, _ = create_admin_service_account_key("alias-owner", role="owner", path=live)
    client = TestClient(proxy.app)
    headers = {"Authorization": f"Bearer {key}"}

    def edit(*pairs: tuple[str, Any]):
        revision = client.get(f"{API}/sections/{SECTION}", headers=headers).json()["revision"]
        items = [{"section": SECTION, "pointer": pointer, "op": "set", "value": value} for pointer, value in pairs]
        return client.patch(f"{API}/changes", json={"items": items, "if_match": {SECTION: revision}}, headers=headers)

    return edit


def test_an_owner_edit_of_the_default_chat_alias_takes_effect_on_the_next_request_with_no_restart(mounted):
    with pytest.raises(proxy.ModelCompatibilityError) as before:
        proxy.resolve_model_for_operation("chat", "default")
    assert before.value.code == "alias_not_configured"
    snapshot = proxy.runtime()
    response = mounted(("/chat/stable/default", "role-after-edit"))
    assert response.status_code == 200 and response.json()["reload"] == {"status": "ok"}, response.text
    assert proxy.runtime() is not snapshot
    assert proxy.resolve_model_for_operation("chat", "default").effective_model == "role-after-edit"
    row = next(row for row in proxy._gateway_aliases_provider()["chat"]["stable"] if row["alias"] == "default")
    assert row["resolves_to"] == "role-after-edit"
    assert "default" in [entry["id"] for entry in proxy._stable_alias_model_entries()]


def test_video_and_audio_alias_tables_follow_an_alias_edit_without_a_restart(mounted):
    assert proxy.VIDEO_ALIASES.get("video") is None and proxy.AUDIO_ALIASES.get("music") is None
    response = mounted(("/videos/stable/video", "workflow-video"), ("/audio_generate/stable/music", "workflow-music"))
    assert response.status_code == 200, response.text
    assert proxy.VIDEO_ALIASES["video"] == "workflow-video" and proxy.AUDIO_ALIASES["music"] == "workflow-music"
    mounted(("/videos/stable/video", "workflow-video-2"))
    assert proxy.VIDEO_ALIASES["video"] == "workflow-video-2"


def test_the_workflow_alias_tables_are_derived_from_the_effective_config_and_tolerate_missing_sections():
    derived = proxy._derive_backend_constants({"backends": {}})
    assert derived["VIDEO_ALIASES"] == {} and derived["AUDIO_ALIASES"] == {}
    tables = proxy._workflow_alias_tables({"videos": {"aliases": {" Video ": " wf "}}, "audio": {"aliases": {"music": "m"}}})
    assert tables == {"VIDEO_ALIASES": {"video": "wf"}, "AUDIO_ALIASES": {"music": "m"}}
    assert isinstance(proxy.VIDEO_ALIASES, dict) and isinstance(proxy.AUDIO_ALIASES, dict)


# ---------------------------------------------------------------------------------------------------------------------
# Rollback of 8.2
# ---------------------------------------------------------------------------------------------------------------------

def test_rollback_the_code_before_c3_serves_the_file_from_a_migrated_database_and_roll_forward_reimports(tmp_path):
    db = tmp_path / "admin.db"
    raw = golden.raw_config("live_like")
    boot(config_rules.config_home(), db, raw)
    assert rows(db) and admin_store.migration_step_recorded(STEP, path=db)
    # The code before C3 ships no model_aliases section: its home has none, so it reads the file and nothing else.
    admin_store.init_db(db)
    old = config_runtime.ConfigHome({}).layer(raw, path=db, registry=None)
    assert old.config[SECTION] == raw[SECTION], "the pre-import aliases, from the restored file"
    # Roll forward with the file changed meanwhile: the boot ignores it and warns; the route imports it on purpose.
    changed = copy.deepcopy(raw[SECTION])
    changed["chat"]["stable"]["default"] = "after-rollback"
    lines: list[str] = []
    forward = boot(config_rules.config_home(), db, {**raw, SECTION: changed}, log=lines.append)
    assert forward.config[SECTION]["chat"]["stable"]["default"] == raw[SECTION]["chat"]["stable"]["default"]
    assert len(lines) == 1 and "1 of its entries differ" in lines[0]
    app = AliasApp(tmp_path, startup={SECTION: changed}, db=db)
    dry = app.client.post(f"{API}/sections/{SECTION}/legacy-import?dry_run=1", headers=OWNER).json()
    assert dry["applicable"] and dry["valid"], "the report lists every row; only one differs from the store"
    assert [change["pointer"] for change in dry["diff"]] == ["/chat/stable/default"]
    done = app.client.post(f"{API}/sections/{SECTION}/legacy-import", headers={**OWNER, "If-Match": app.revision()})
    assert done.status_code == 200, done.text
    assert app.effective()["chat"]["stable"]["default"] == "after-rollback"
    assert golden.digest(app.effective()) == golden.digest(changed)


# ---------------------------------------------------------------------------------------------------------------------
# The dry-run command
# ---------------------------------------------------------------------------------------------------------------------

def run_import_command(*argv: str) -> dict[str, Any]:
    out = io.StringIO()
    with redirect_stdout(out):
        assert config_legacy_import.main(list(argv)) == 0
    return json.loads(out.getvalue())


def aliases_entry(result: dict[str, Any]) -> dict[str, Any]:
    """The command reports every section the release ships; these tests read the aliases entry."""
    (report,) = [entry for entry in result["sections"] if entry["section"] == SECTION]
    return report


def test_the_dry_run_command_lists_every_row_for_a_copy_of_a_config_file_and_writes_nothing(tmp_path):
    config = tmp_path / "proxy_config.copy.yml"
    config.write_text((ROOT / "proxy_config.yml").read_text(encoding="utf-8"), encoding="utf-8")
    scratch = tmp_path / "scratch.db"
    result = run_import_command("--config", str(config), "--db", str(scratch))
    report = aliases_entry(result)
    assert report["section"] == SECTION and report["status"] == "dry_run"
    assert report["row_count"] == len(report["rows"]) > 0
    assert {"/chat/stable/default", "/embeddings/stable/embed", "/speech/engines/omnivoice"} <= {r["pointer"] for r in report["rows"]}
    assert rows(scratch) == [] and not admin_store.migration_step_recorded(STEP, path=scratch)
    # Without a --db the command makes and removes its own scratch store.
    assert aliases_entry(run_import_command("--config", str(config)))["row_count"] == report["row_count"]


def test_the_command_applies_only_to_a_scratch_database_it_is_given(tmp_path, capsys):
    config = tmp_path / "live-copy.yml"
    config.write_text((ROOT / "proxy_config.yml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        config_legacy_import.main(["--config", str(config), "--apply"])
    assert refused.value.code == 2
    scratch = tmp_path / "scratch.db"
    report = aliases_entry(run_import_command("--config", str(config), "--db", str(scratch), "--apply"))
    assert report["status"] == "imported" and len(rows(scratch)) == report["row_count"] + len(report["removes"])
    again = aliases_entry(run_import_command("--config", str(config), "--db", str(scratch)))
    assert again["status"] == "already_imported"
    with pytest.raises(SystemExit):
        config_legacy_import.main(["--config", str(config), "--section", "nope"])


# ---------------------------------------------------------------------------------------------------------------------
# The generic layer stays generic
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["config_layers", "config_runtime", "config_legacy_import", "config_api", "config_cli"])
def test_the_generic_config_modules_know_nothing_of_aliases(module):
    source = (ROOT / "gateway" / f"{module}.py").read_text(encoding="utf-8")
    assert "alias" not in source.lower() and "stable_aliases" not in source


def test_the_only_place_that_names_a_rule_is_the_registration():
    assert set(config_rules.RULES) == {SECTION} and config_rules.RULES[SECTION] is stable_aliases.alias_problems
    assert set(config_rules.config_home().sections) >= {SECTION}
