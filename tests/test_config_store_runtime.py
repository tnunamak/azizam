"""Store, reload and snapshot tests of the config-homes core (docs/design/CONFIG_HOMES.md 3.2, 3.6, 5; acceptance 10.1, C1).

The store tests use a real admin.db in a temporary directory. The reload tests run the real ``proxy`` reload with a
fixture section in a ``ConfigHome``; no section ships in this release, so the fixture is the only one.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import inspect
import json
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import admin_store, config_layers as layers, config_runtime
from gateway.admin_store import (
    ConfigChangedSince, ConfigChangeInvalid, ConfigItem, ConfigRevisionMismatch, ConfigRevisionRequired,
    apply_config_change, get_config_change, list_config_changes, list_config_overrides, read_config_state,
    revert_config_change,
)
from tests.test_config_layers import DEFAULTS, SCHEMA, limits_in_order
from tests.support.gateway import gateway_client
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture

SECTION = "fixture"


def item(pointer: str, value: Any = None, *, op: str = "set", base: Any = layers.ABSENT, section: str = SECTION) -> ConfigItem:
    return ConfigItem(
        section, pointer, op, None if op != "set" else json.dumps(value),
        None if base is layers.ABSENT else json.dumps(base),
    )


def revision(db: Path, section: str = SECTION) -> str:
    return read_config_state(path=db)["revisions"].get(section, "0")


def edit(db: Path, items: list[ConfigItem], **kw: Any) -> dict[str, Any]:
    sections = {i.section for i in items}
    if_match = kw.pop("if_match", {section: revision(db, section) for section in sections})
    kw.setdefault("validate", None)
    return apply_config_change(items, if_match=if_match, actor_kind="user", actor_id="owner-1", path=db, **kw)


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return path


def counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return {
            "overrides": conn.execute("SELECT COUNT(*) FROM config_overrides").fetchone()[0],
            "changes": conn.execute("SELECT COUNT(*) FROM config_changes").fetchone()[0],
            "audit": conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='config.change'").fetchone()[0],
        }
    finally:
        conn.close()


def home(**kw: Any) -> config_runtime.ConfigHome:
    section = layers.Section.create(SECTION, copy.deepcopy(DEFAULTS), SCHEMA, validator=limits_in_order)
    return config_runtime.ConfigHome({SECTION: section}, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------------------------------------------------

def test_a_change_writes_rows_the_change_and_the_audit_fact_and_advances_the_revision(db):
    result = edit(db, [item("/timeout", 60, base=30), item("/aliases/fast", op="delete", base={"target": "svc-b"})], note="why")
    rows = {row["pointer"]: row for row in list_config_overrides(SECTION, path=db)}
    assert set(rows) == {"/timeout", "/aliases/fast"}
    assert rows["/timeout"]["value_json"] == "60" and rows["/timeout"]["base_json"] == "30"
    assert rows["/aliases/fast"]["op"] == "delete" and rows["/aliases/fast"]["value_json"] is None
    assert {row["change_id"] for row in rows.values()} == {result["change_id"]}
    assert revision(db) == "1" and result["revisions"] == {SECTION: "1"}
    change = get_config_change(result["change_id"], path=db)
    assert change["kind"] == "edit" and change["note"] == "why" and change["actor_id"] == "owner-1"
    assert {entry["pointer"] for entry in change["items"]} == {"/timeout", "/aliases/fast"}
    assert counts(db) == {"overrides": 2, "changes": 1, "audit": 1}


def test_a_section_missing_from_if_match_is_refused_428_and_writes_nothing(db):
    with pytest.raises(ConfigRevisionRequired) as error:
        apply_config_change([item("/timeout", 60)], if_match={}, validate=None, path=db)
    assert error.value.sections == (SECTION,)
    assert counts(db) == {"overrides": 0, "changes": 0, "audit": 0}


def test_a_stale_revision_is_refused_412_and_writes_nothing(db):
    edit(db, [item("/timeout", 60)])
    with pytest.raises(ConfigRevisionMismatch) as error:
        apply_config_change([item("/timeout", 70)], if_match={SECTION: "0"}, validate=None, path=db)
    assert (error.value.section, error.value.expected, error.value.current) == (SECTION, "0", "1")
    assert json.loads(list_config_overrides(SECTION, path=db)[0]["value_json"]) == 60


def test_set_of_a_literal_null_is_stored_as_text_and_a_set_without_a_value_is_refused(db):
    edit(db, [item("/note", None)])
    row = list_config_overrides(SECTION, path=db)[0]
    assert row["op"] == "set" and row["value_json"] == "null"
    with pytest.raises(ValueError, match="value_json"):
        apply_config_change([ConfigItem(SECTION, "/timeout", "set", None)], if_match={SECTION: revision(db)}, validate=None, path=db)


def test_reset_removes_the_row_and_a_reset_of_nothing_changes_nothing(db):
    edit(db, [item("/timeout", 60)])
    result = edit(db, [item("/timeout", op="reset")])
    assert list_config_overrides(SECTION, path=db) == []
    assert result["entry_count"] == 1 and revision(db) == "2"
    again = edit(db, [item("/timeout", op="reset")])
    assert again["change_id"] is None and again["entry_count"] == 0
    assert revision(db) == "2" and counts(db)["changes"] == 2


def test_a_second_edit_of_one_pointer_keeps_one_row_with_the_latest_change(db):
    edit(db, [item("/timeout", 60)])
    second = edit(db, [item("/timeout", 70)])
    rows = list_config_overrides(SECTION, path=db)
    assert len(rows) == 1 and rows[0]["value_json"] == "70" and rows[0]["change_id"] == second["change_id"]


def test_the_same_pointer_twice_in_one_change_is_refused(db):
    with pytest.raises(ValueError, match="twice"):
        apply_config_change([item("/timeout", 1), item("/timeout", 2)], if_match={SECTION: "0"}, validate=None, path=db)


def test_a_validator_refusal_writes_nothing_and_names_the_problem(db):
    validate = home().write_validator({})
    with pytest.raises(ConfigChangeInvalid) as error:
        edit(db, [item("/timeout", 0), item("/mode", "slow")], validate=validate)
    assert [(p["section"], p["pointer"], p["code"]) for p in error.value.problems if p["code"] == "override_rejected"] == [
        (SECTION, "/timeout", "override_rejected")
    ]
    assert counts(db) == {"overrides": 0, "changes": 0, "audit": 0} and revision(db) == "0"


def test_the_write_validator_refuses_an_unknown_section_a_non_entry_and_a_cross_entry_fault(db):
    validate = home().write_validator({})
    for bad in (item("/timeout", 1, section="no_such_section"), item("/limits", {"min": 1, "max": 2}), item("/limits/min", 99)):
        with pytest.raises(ConfigChangeInvalid):
            edit(db, [bad], validate=validate)
    assert counts(db)["overrides"] == 0
    edit(db, [item("/limits/max", 99), item("/limits/min", 50)], validate=validate)  # a pair that is valid together
    assert {row["pointer"] for row in list_config_overrides(SECTION, path=db)} == {"/limits/max", "/limits/min"}


def test_a_dangling_problem_does_not_refuse_the_write(db):
    def dangling(candidate: Any, _context: Any) -> list[layers.Problem]:
        gone = candidate["aliases"].get("fast", {}).get("target") == "gone"
        return [layers.Problem("/aliases/fast", "no_such_service", layers.DANGLING, "")] if gone else []

    section = layers.Section.create(SECTION, copy.deepcopy(DEFAULTS), SCHEMA, validator=dangling)
    edit(db, [item("/aliases/fast", {"target": "gone"})], validate=config_runtime.ConfigHome({SECTION: section}).write_validator({}))
    assert len(list_config_overrides(SECTION, path=db)) == 1


# ---------------------------------------------------------------------------------------------------------------------
# One candidate for load and write: no result depends on the order of the section ids (review finding 2)
# ---------------------------------------------------------------------------------------------------------------------

PAIR_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {"flag": {"type": "string", "enum": ["on", "off"]}, "level": {"type": "integer", "minimum": 0}},
}
PAIR_DEFAULTS: dict[str, Any] = {"flag": "off", "level": 1}


def _needs_other_flag(other: str):
    """The rule of a section: a level above 1 needs the other section's flag to be on. A rule across sections."""

    def rule(candidate: Any, context: Any) -> list[layers.Problem]:
        if candidate.get("level", 0) > 1 and context["sections"][other].get("flag") != "on":
            return [layers.Problem("/level", "needs_other_flag", layers.INVALID, f"{other} must have its flag on")]
        return []

    return rule


def pair_home(registry_probe: list | None = None) -> config_runtime.ConfigHome:
    sections = {
        "alpha": layers.Section.create("alpha", dict(PAIR_DEFAULTS), PAIR_SCHEMA, validator=_needs_other_flag("beta")),
        "beta": layers.Section.create("beta", dict(PAIR_DEFAULTS), PAIR_SCHEMA, validator=_needs_other_flag("alpha")),
    }
    return config_runtime.ConfigHome(sections)


def test_a_valid_pair_of_changes_to_two_sections_is_accepted_in_both_directions(db):
    validate = pair_home().write_validator({})
    # alpha's rule needs beta's new flag, beta's rule needs alpha's new flag: neither sorts "first" for the other
    edit(db, [item("/level", 2, section="alpha"), item("/flag", "on", section="beta")], validate=validate)
    edit(db, [item("/level", 3, section="beta"), item("/flag", "on", section="alpha")], validate=validate)
    layer = pair_home().layer({}, path=db)
    assert layer.config["alpha"] == {"flag": "on", "level": 2} and layer.config["beta"] == {"flag": "on", "level": 3}
    assert layer.problems == {}, "the load path reads the same pair the write path accepted"


def test_a_change_to_one_section_that_breaks_the_rule_of_another_is_refused_422(db):
    validate = pair_home().write_validator({})
    edit(db, [item("/flag", "on", section="alpha"), item("/level", 2, section="beta")], validate=validate)
    before = counts(db)
    with pytest.raises(ConfigChangeInvalid) as error:
        edit(db, [item("/flag", op="reset", section="alpha")], validate=validate)  # beta's level 2 now lacks alpha's flag
    assert [(p["section"], p["pointer"], p["code"]) for p in error.value.problems] == [("beta", "/level", "override_rejected")]
    assert counts(db) == before and revision(db, "alpha") == "1"
    # what was committed is what the load path reads: the override of beta is not silently rejected
    assert pair_home().layer({}, path=db).problems == {}


def test_the_candidate_is_the_same_whatever_order_the_sections_and_rows_arrive_in(db):
    edit(db, [item("/level", 2, section="alpha"), item("/flag", "on", section="beta")])
    edit(db, [item("/level", 2, section="beta"), item("/flag", "on", section="alpha")])
    rows = {
        name: [layers.Override(r["pointer"], r["op"], json.loads(r["value_json"])) for r in list_config_overrides(name, path=db)]
        for name in ("alpha", "beta")
    }
    forward = pair_home().build_candidate(rows)
    reverse_home = pair_home()
    reverse_home.sections = dict(reversed(list(reverse_home.sections.items())))
    backward = reverse_home.build_candidate(dict(reversed(list(rows.items()))))
    assert {n: r.effective for n, r in forward.items()} == {n: r.effective for n, r in backward.items()}
    assert forward["alpha"].effective == {"flag": "on", "level": 2} and forward["beta"].effective == {"flag": "on", "level": 2}
    assert all(not result.invalid for result in forward.values())


def test_a_stored_row_that_is_already_bad_does_not_refuse_every_later_write(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) "
                 "VALUES('alpha','/level','set','9','api','c-x',1)")  # needs beta's flag: rejected on load
    conn.commit()
    conn.close()
    h = pair_home()
    assert h.layer({}, path=db).problems["alpha"], "the stored row is reported on load"
    with pytest.raises(ConfigChangeInvalid):
        edit(db, [item("/level", 9, section="beta")], validate=h.write_validator({}))  # adds a problem: refused
    edit(db, [item("/flag", "on", section="alpha")], validate=h.write_validator({}))  # adds none: allowed
    assert {(r["section"], r["pointer"]) for r in list_config_overrides(path=db)} == {("alpha", "/level"), ("alpha", "/flag")}


def test_release_defaults_that_already_break_a_rule_do_not_refuse_every_write(db):
    h = pair_home()
    h.sections["alpha"] = dataclasses.replace(h.sections["alpha"], defaults={"flag": "off", "level": 5})  # bypasses create
    assert h.build_candidate({})["alpha"].invalid, "the defaults break alpha's rule"
    with pytest.raises(ConfigChangeInvalid):
        edit(db, [item("/level", 9, section="beta")], validate=h.write_validator({}))  # adds a problem: refused
    edit(db, [item("/flag", "on", section="alpha")], validate=h.write_validator({}))  # adds none: allowed


def test_the_context_a_rule_sees_has_the_registry_and_the_sections_and_not_the_startup_file(db):
    seen: list[Any] = []

    def spy(candidate: Any, context: Any) -> list[layers.Problem]:
        seen.append(context)
        return []

    section = layers.Section.create("alpha", dict(PAIR_DEFAULTS), PAIR_SCHEMA, validator=spy)
    h = config_runtime.ConfigHome({"alpha": section})
    registry = {"services": {"svc-1": {}}}
    seen.clear()
    h.layer({"startup": True}, path=db, registry=registry)
    edit(db, [item("/level", 2, section="alpha")], validate=h.write_validator(registry))
    assert seen and all(set(context) == {"registry", "sections"} for context in seen)
    assert all(context["registry"] is registry for context in seen)


def test_the_release_defaults_are_checked_against_the_rule_when_the_home_is_made():
    broken = layers.Section.create("alpha", {"flag": "off", "level": 5}, PAIR_SCHEMA, validator=_needs_other_flag("beta"))
    other = layers.Section.create("beta", dict(PAIR_DEFAULTS), PAIR_SCHEMA, validator=_needs_other_flag("alpha"))
    with pytest.raises(layers.SectionDataError, match="alpha"):
        config_runtime.ConfigHome({"alpha": broken, "beta": other})
    fixed = layers.Section.create("alpha", {"flag": "off", "level": 5}, PAIR_SCHEMA, validator=_needs_other_flag("beta"))
    ok_beta = layers.Section.create("beta", {"flag": "on", "level": 1}, PAIR_SCHEMA, validator=_needs_other_flag("alpha"))
    config_runtime.ConfigHome({"alpha": fixed, "beta": ok_beta})


def test_the_write_validator_refuses_a_delete_of_a_required_entry(db):
    with pytest.raises(ConfigChangeInvalid) as error:
        edit(db, [item("/limits/max", op="delete")], validate=home().write_validator({}))
    assert [p["code"] for p in error.value.problems] == ["override_rejected"]
    assert counts(db)["overrides"] == 0
    edit(db, [item("/aliases/fast", op="delete")], validate=home().write_validator({}))  # a member: allowed


def test_apply_and_revert_name_their_validator_and_cannot_forget_it(db):
    for function in (apply_config_change, revert_config_change):
        parameter = inspect.signature(function).parameters["validate"]
        assert parameter.default is inspect.Parameter.empty, f"{function.__name__} must be told which rule judges the change"
    with pytest.raises(TypeError, match="validate"):
        apply_config_change([item("/timeout", 60)], if_match={SECTION: "0"}, path=db)
    with pytest.raises(TypeError, match="validate"):
        revert_config_change("cfg_x", if_match={SECTION: "0"}, path=db)


def test_a_multi_section_change_is_all_or_nothing(db, monkeypatch):
    real = admin_store._write_config_row
    calls: list[tuple[str, str]] = []

    def failing_after_the_first(conn, section, pointer, new, **kw):
        calls.append((section, pointer))
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(conn, section, pointer, new, **kw)

    monkeypatch.setattr(admin_store, "_write_config_row", failing_after_the_first)
    with pytest.raises(RuntimeError, match="disk full"):
        apply_config_change(
            [item("/timeout", 60, section="alpha"), item("/timeout", 60, section="beta")],
            if_match={"alpha": "0", "beta": "0"}, validate=None, path=db,
        )
    assert len(calls) == 2, "the first row was written inside the transaction before the failure"
    assert counts(db) == {"overrides": 0, "changes": 0, "audit": 0}
    assert read_config_state(path=db)["revisions"] == {}


def test_the_audit_row_and_the_change_row_commit_with_the_overrides_or_not_at_all(db, monkeypatch):
    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(admin_store, "_record_audit_in_transaction", broken_audit)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        edit(db, [item("/timeout", 60)])
    assert counts(db) == {"overrides": 0, "changes": 0, "audit": 0} and revision(db) == "0"


def test_a_multi_section_change_advances_each_touched_section_once(db):
    apply_config_change(
        [item("/timeout", 1, section="alpha"), item("/mode", "slow", section="alpha"), item("/timeout", 2, section="beta")],
        if_match={"alpha": "0", "beta": "0"}, validate=None, path=db,
    )
    assert read_config_state(path=db)["revisions"] == {"alpha": "1", "beta": "1"}
    assert counts(db)["changes"] == 1


# ---------------------------------------------------------------------------------------------------------------------
# Revert, history and the one-time import step
# ---------------------------------------------------------------------------------------------------------------------

def test_a_revert_writes_the_inverse_as_a_new_change(db):
    first = edit(db, [item("/timeout", 60, base=30)])
    second = edit(db, [item("/timeout", 70, base=30), item("/aliases/fast", op="delete")])
    result = revert_config_change(second["change_id"], if_match={SECTION: revision(db)}, actor_kind="user", actor_id="owner-1", validate=None, path=db)
    rows = {row["pointer"]: row for row in list_config_overrides(SECTION, path=db)}
    assert set(rows) == {"/timeout"} and rows["/timeout"]["value_json"] == "60"
    assert get_config_change(result["change_id"], path=db)["kind"] == "revert"
    assert [c["change_id"] for c in list_config_changes(path=db)][:1] == [result["change_id"]]
    assert first["change_id"] in {c["change_id"] for c in list_config_changes(path=db)}


def test_a_revert_after_a_later_edit_of_the_same_pointer_is_refused_412_changed_since(db):
    first = edit(db, [item("/timeout", 60), item("/mode", "slow")])
    edit(db, [item("/timeout", 70)])
    with pytest.raises(ConfigChangedSince) as error:
        revert_config_change(first["change_id"], if_match={SECTION: revision(db)}, validate=None, path=db)
    assert error.value.pointers == ("/timeout",)
    assert {row["pointer"] for row in list_config_overrides(SECTION, path=db)} == {"/timeout", "/mode"}


def test_a_revert_needs_the_section_revision_and_an_unknown_change_is_not_found(db):
    first = edit(db, [item("/timeout", 60)])
    with pytest.raises(ConfigRevisionRequired):
        revert_config_change(first["change_id"], if_match={}, validate=None, path=db)
    with pytest.raises(ConfigRevisionMismatch):
        revert_config_change(first["change_id"], if_match={SECTION: "0"}, validate=None, path=db)
    with pytest.raises(LookupError):
        revert_config_change("cfg_missing", if_match={SECTION: revision(db)}, validate=None, path=db)


def test_a_one_time_step_records_in_the_same_transaction_and_the_second_call_does_nothing(db):
    step = "config.legacy-import.fixture"
    first = edit(db, [item("/timeout", 60)], migration_step=step, kind="legacy-import", origin="legacy-import")
    assert first["change_id"] and list_config_overrides(SECTION, path=db)[0]["origin"] == "legacy-import"
    second = apply_config_change([item("/timeout", 99)], if_match={SECTION: "0"}, migration_step=step, validate=None, path=db)
    assert second["skipped"] is True
    assert json.loads(list_config_overrides(SECTION, path=db)[0]["value_json"]) == 60


def test_a_refused_import_does_not_record_its_step(db):
    step = "config.legacy-import.fixture"
    with pytest.raises(ConfigChangeInvalid):
        edit(db, [item("/timeout", 0)], migration_step=step, validate=home().write_validator({}))
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT 1 FROM schema_migration_steps WHERE name=?", (step,)).fetchone() is None
    finally:
        conn.close()


def test_the_check_constraint_keeps_a_tombstone_free_of_a_value(db):
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) VALUES('s','/p','delete','1','api','c',1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO config_overrides(section,pointer,op,origin,change_id,updated_at) VALUES('s','/p','set','api','c',1)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------------------
# The load path through the runtime glue
# ---------------------------------------------------------------------------------------------------------------------

def test_with_no_section_the_layer_returns_the_startup_config_and_reads_no_table(tmp_path):
    raw = {"backends": {}, "a": 1}
    layer = config_runtime.ConfigHome({}).layer(raw, path=tmp_path / "never-created.db")
    assert layer.config is raw and layer.results == {}
    assert not (tmp_path / "never-created.db").exists()
    effective = config_runtime.ConfigHome({}).attach(raw, layer)
    assert effective is raw and type(effective) is dict


def test_no_section_ships_in_this_release():
    assert config_runtime.load_sections() == {}
    assert proxy.CONFIG_HOME.sections == {}


def test_a_stored_override_reaches_the_effective_section_with_its_source_and_a_stored_bad_row_is_reported(db):
    edit(db, [item("/timeout", 60, base=30), item("/aliases/mine", {"target": "x"})])
    # a bad row put in by hand, as an older release or a restored database could leave it
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) "
                 "VALUES('fixture','/mode','set','\"never\"','api','c-x',1)")
    conn.commit()
    conn.close()
    h = home()
    layer = h.layer({"keep": True}, path=db)
    effective = layer.config[SECTION]
    assert effective["timeout"] == 60 and effective["aliases"]["mine"] == {"target": "x"} and effective["mode"] == "fast"
    assert layer.config["keep"] is True
    assert layer.sources[SECTION]["/timeout"]["source"] == layers.SOURCE_OWNER
    assert layer.sources[SECTION]["/aliases/mine"]["source"] == layers.SOURCE_OWNER_ADDED
    assert layer.sources[SECTION]["/mode"]["apply"] == "restart"
    assert [p["pointer"] for p in layer.problems[SECTION] if p["code"] == "override_rejected"] == ["/mode"]
    assert layer.revisions == ((SECTION, "1"),)
    assert layers.digest(h.layer({"keep": True}, path=db).config) == layers.digest(layer.config), "stable across loads"


def test_the_helm_merge_double_can_be_passed_to_the_home_and_is_what_it_loads_with(db):
    from tests.test_config_layers import helm_deep_merge

    edit(db, [item("/aliases/fast", {"engine": "eng-1"})])
    sound = home().layer({}, path=db)
    helm = home(merge=helm_deep_merge).layer({}, path=db)
    assert sound.config[SECTION]["aliases"]["fast"] == {"engine": "eng-1"}
    assert helm.config[SECTION]["aliases"]["fast"] == {"target": "svc-b"}, "deep merge made it invalid, so it was dropped"
    assert helm.problems[SECTION]


# ---------------------------------------------------------------------------------------------------------------------
# Snapshot: one object per publish, one snapshot per request
# ---------------------------------------------------------------------------------------------------------------------

def test_a_publish_swaps_one_object():
    h = home()
    first = h.snapshot({"a": 1}, derived={"X": 1})
    second = h.snapshot({"a": 2})
    h.publish(first)
    assert h.holder.current() is first
    h.publish(second)
    assert h.holder.current() is second and first.config == {"a": 1}


def test_a_request_bound_before_a_reload_sees_the_old_snapshot_to_its_end():
    h = home()
    old, new = h.snapshot({"v": "old"}), h.snapshot({"v": "new"})
    h.publish(old)
    seen: dict[str, Any] = {}
    entered, released = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        seen["first"] = config_runtime.runtime_for(h)
        entered.set()
        await asyncio.wait_for(released.wait(), 10)
        seen["last"] = config_runtime.runtime_for(h)

    middleware = config_runtime.SnapshotMiddleware(app, current=lambda: config_runtime.runtime_for(h))

    async def scenario():
        before = asyncio.create_task(middleware({"type": "http"}, None, None))
        # time-boxed: a crash of the app task must fail this test, never hang it
        waiter = asyncio.create_task(entered.wait())
        await asyncio.wait({before, waiter}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if before.done():
            before.result()  # re-raises the crash
        assert entered.is_set(), "the request never reached the app"
        h.publish(new)  # the reload lands while the request is in flight
        released.set()
        await asyncio.wait_for(before, 10)
        assert config_runtime.bound_snapshot() is None, "the binding ends with the request"
        assert config_runtime.runtime_for(h) is new

    asyncio.run(scenario())
    assert seen["first"] is old and seen["last"] is old, "runtime_for, not only bound_snapshot, answers the bound snapshot"


def test_a_non_http_scope_is_passed_through_unbound():
    called: list[Any] = []

    async def app(scope, receive, send):
        called.append(config_runtime.bound_snapshot())

    asyncio.run(config_runtime.SnapshotMiddleware(app, current=lambda: pytest.fail("bound a lifespan"))({"type": "lifespan"}, None, None))
    assert called == [None]


def test_outside_a_request_the_latest_published_snapshot_is_the_only_source():
    h = home()
    with pytest.raises(RuntimeError, match="no config snapshot"):
        config_runtime.runtime_for(h)
    published = h.snapshot({"a": 1})
    h.publish(published)
    assert config_runtime.runtime_for(h) is published


def test_the_gateway_installs_the_middleware_and_serves_its_boot_snapshot():
    assert any(m.cls is config_runtime.SnapshotMiddleware for m in proxy.app.user_middleware)
    assert proxy.runtime() is proxy.CONFIG_HOME.holder.current()
    assert proxy.runtime().config is proxy.config


# ---------------------------------------------------------------------------------------------------------------------
# Reload through the real gateway: success, failure after a commit, recovery
# ---------------------------------------------------------------------------------------------------------------------

def test_a_reload_publishes_the_layered_config_and_a_failed_reload_keeps_the_old_one_and_says_why(live, monkeypatch):  # noqa: F811
    db = live
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    monkeypatch.setattr(proxy, "CONFIG_PROBLEMS", {})
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None, raising=False)
    real_snapshot = h.snapshot

    proxy.reload_runtime_config()
    first = h.holder.current()
    assert first is proxy.runtime() and first.config is proxy.config
    assert proxy.config[SECTION]["timeout"] == 30
    assert first.generation[1] == ((SECTION, "0"),)

    committed = edit(db, [item("/timeout", 60, base=30)], validate=h.write_validator({}))
    h.note_commit(committed["change_id"], [(SECTION, "/timeout")])

    def broken_build(*args, **kwargs):
        raise RuntimeError("snapshot build failed")

    monkeypatch.setattr(h, "snapshot", broken_build)
    config_before = proxy.config
    with pytest.raises(RuntimeError, match="snapshot build failed"):
        proxy.reload_runtime_config()
    assert proxy.config is config_before and h.holder.current() is first and proxy.runtime() is first
    assert proxy.config[SECTION]["timeout"] == 30, "the old snapshot serves"

    failed = h.view()["reload_failed"]
    assert failed["change_id"] == committed["change_id"] and "snapshot build failed" in failed["error"]
    assert failed["entries"] == [{
        "section": SECTION, "pointer": "/timeout",
        "serving": {"present": True, "value": 30}, "stored": {"op": "set", "value": 60},
    }]

    monkeypatch.setattr(h, "snapshot", real_snapshot)
    proxy.reload_runtime_config()
    assert proxy.config[SECTION]["timeout"] == 60 and h.view()["reload_failed"] is None
    assert h.holder.current().generation[1] == ((SECTION, "1"),)


def test_a_reload_reports_a_rejected_stored_override_as_a_config_problem(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    monkeypatch.setattr(proxy, "CONFIG_PROBLEMS", {})
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None, raising=False)
    conn = sqlite3.connect(live)
    conn.execute("INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) "
                 "VALUES('fixture','/timeout','set','0','api','c-x',1)")
    conn.commit()
    conn.close()
    proxy.reload_runtime_config()
    assert proxy.config[SECTION]["timeout"] == 30
    assert [p["code"] for p in proxy.CONFIG_PROBLEMS[SECTION] if p["pointer"] == "/timeout"] == ["override_rejected"]
    assert h.view()["problems"]["config"] == proxy.CONFIG_PROBLEMS


# ---------------------------------------------------------------------------------------------------------------------
# Boot with an unreadable override store (review finding 1)
# ---------------------------------------------------------------------------------------------------------------------

def test_a_boot_that_cannot_read_the_override_store_serves_the_release_defaults_and_says_so(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    def no_registry(*args, **kwargs):
        raise RuntimeError("registry store unreadable")

    monkeypatch.setattr(proxy, "_load_effective_runtime_config", no_registry)
    monkeypatch.setattr(proxy, "_partial_runtime_config", no_registry)
    monkeypatch.setattr(admin_store, "read_config_state", unreadable)
    effective, problem = proxy._boot_effective_runtime_config(proxy._bootstrap_config)  # must not raise
    assert problem["mode"] == "empty"
    assert effective[SECTION] == DEFAULTS, "no override applies: release defaults"
    layer, _generation = config_runtime.carried(effective)
    assert [p["code"] for p in layer.problems[SECTION]] == [config_runtime.CODE_STORE_UNREADABLE]
    assert "malformed" not in json.dumps(layer.problems), "the problem names the error class, never its text"
    snapshot = h.snapshot(effective, layer=layer)
    assert snapshot.problems["config"][SECTION][0]["code"] == "config_store_unreadable"


def test_a_reload_that_cannot_read_the_override_store_keeps_what_is_running(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    proxy.reload_runtime_config()
    running = h.holder.current()

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(admin_store, "read_config_state", unreadable)
    with pytest.raises(sqlite3.OperationalError):
        proxy.reload_runtime_config()
    assert h.holder.current() is running and proxy.runtime() is running


# ---------------------------------------------------------------------------------------------------------------------
# The snapshot holder is the only source (review finding 4)
# ---------------------------------------------------------------------------------------------------------------------

def test_runtime_answers_the_published_snapshot_and_never_a_config_nobody_published(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    proxy.reload_runtime_config()
    published = h.holder.current()
    monkeypatch.setattr(proxy, "config", {"unpublished": True})  # the window of a half-done reload
    assert proxy.runtime() is published and "unpublished" not in proxy.runtime().config


def test_a_reload_publishes_first_and_the_module_config_is_the_snapshots_own(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    seen: dict[str, Any] = {}
    real_publish = h.publish

    def watch(snapshot):
        seen["config_at_publish"] = proxy.config  # the module name has not moved yet
        real_publish(snapshot)

    monkeypatch.setattr(h, "publish", watch)
    before = proxy.config
    proxy.reload_runtime_config()
    assert seen["config_at_publish"] is before
    assert proxy.config is h.holder.current().config and proxy.runtime().config is proxy.config


def test_a_reload_carries_the_registry_problem_and_the_layer_problems_into_its_snapshot(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    conn = sqlite3.connect(live)
    conn.execute("INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) "
                 "VALUES('fixture','/timeout','set','0','api','c-x',1)")
    conn.commit()
    conn.close()
    real = proxy._runtime_config_for_reload
    problem = {"mode": "partial", "error": "one record", "left_out": {"svc": "bad"}, "served": 1}
    monkeypatch.setattr(proxy, "_runtime_config_for_reload", lambda: (real()[0], problem))
    proxy.reload_runtime_config()
    snapshot = proxy.runtime()
    assert snapshot.problems["registry"] == problem, "a reload snapshot says what the boot snapshot says"
    assert [p["code"] for p in snapshot.problems["config"][SECTION] if p["pointer"] == "/timeout"] == ["override_rejected"]


def test_a_snapshot_keeps_the_layer_problems_when_the_config_is_copied(live, monkeypatch):  # noqa: F811
    h = home()
    conn = sqlite3.connect(live)
    conn.execute("INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) "
                 "VALUES('fixture','/timeout','set','0','api','c-x',1)")
    conn.commit()
    conn.close()
    layer = h.layer({}, path=live)
    layered = h.attach({"x": 1, **layer.config}, layer)
    assert config_runtime.carried(layered)[0] is layer
    copied = dict(layered)  # a plain copy carries no attribute
    assert config_runtime.carried(copied)[0] is config_runtime.NO_LAYER
    assert h.snapshot(copied, layer=layer).problems["config"] == layer.problems, "the layer is handed over explicitly"


def test_proxy_runtime_in_a_real_request_keeps_the_snapshot_it_started_with_while_a_reload_lands(live, monkeypatch):  # noqa: F811
    """A guard, not a proof of the holder fix: the middleware already bound a snapshot before that fix, so this passes on
    the earlier head too. It pins that ``proxy.runtime()`` inside a real ASGI request, in the worker thread of a sync
    route, answers the snapshot the request started with while a reload lands, and a later request binds the new one.
    What fails without the holder rewrite is ``test_a_reload_publishes_first_and_the_module_config_is_the_snapshots_own``."""
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    proxy.reload_runtime_config()
    first = h.holder.current()
    assert first.config[SECTION]["timeout"] == 30
    seen: dict[str, Any] = {}

    def credential_count_while_a_reload_lands():  # runs in the request's worker thread, as a sync admin route's reload does
        seen["before"] = proxy.runtime()
        committed = edit(live, [item("/timeout", 60, base=30)], validate=h.write_validator({}))
        h.note_commit(committed["change_id"], [(SECTION, "/timeout")])
        proxy.reload_runtime_config()
        seen["after"] = proxy.runtime()
        return 1

    monkeypatch.setattr(proxy, "active_app_credential_count", credential_count_while_a_reload_lands)

    async def call() -> int:
        async with gateway_client() as client:
            return (await client.get("/health")).status_code

    assert asyncio.run(call()) == 200
    assert seen["before"] is first and seen["after"] is first, "the request keeps its own snapshot to its end"
    assert seen["after"].config[SECTION]["timeout"] == 30
    latest = proxy.runtime()
    assert latest is not first and latest.config[SECTION]["timeout"] == 60, "a request that starts now binds the new one"

    later: dict[str, Any] = {}
    monkeypatch.setattr(proxy, "active_app_credential_count", lambda: later.setdefault("seen", proxy.runtime()) and 1)
    assert asyncio.run(call()) == 200
    assert later["seen"] is latest


# ---------------------------------------------------------------------------------------------------------------------
# The generation in the snapshot key (review finding 9)
# ---------------------------------------------------------------------------------------------------------------------

def test_the_generation_in_the_key_is_the_one_of_the_registry_the_config_was_built_from(live):  # noqa: F811
    from gateway import service_registry

    service_registry.ensure_service_registry(proxy._bootstrap_config, path=live)
    stored = admin_store.get_service_registry_with_generation(path=live)
    registry = stored["value"]
    assert proxy._generation_of_registry(registry) == stored["operator_settings_generation"]
    moved = copy.deepcopy(registry)
    moved["services"]["a-newer-service"] = copy.deepcopy(next(iter(registry["services"].values())))
    admin_store.write_service_registry_with_audit(
        moved, expected_generation=stored["operator_settings_generation"], updated_by="test", audit_action="test.registry",
        audit_actor_kind="admin", audit_actor_id="test", audit_actor_label="Test", audit_target_kind="operator_setting",
        audit_target_id="service_registry", audit_metadata={}, session=None, path=live,
    )
    assert proxy._generation_of_registry(registry) is None, "the store now holds a newer registry than the one used"


def test_a_load_carries_the_generation_of_the_registry_it_used_not_a_later_read(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    monkeypatch.setattr(proxy, "_write_litellm_config_artifact", lambda registry: None)
    effective = proxy._load_effective_runtime_config(proxy._bootstrap_config)
    layer, generation = config_runtime.carried(effective)
    assert generation == admin_store.get_service_registry_with_generation(path=live)["operator_settings_generation"]
    assert h.snapshot(effective, layer=layer, registry_generation=generation).generation[0] == generation


# ---------------------------------------------------------------------------------------------------------------------
# Loading sections, and the history of changes (review finding 11)
# ---------------------------------------------------------------------------------------------------------------------

def test_a_defaults_file_without_a_schema_stops_the_load(tmp_path):
    (tmp_path / "orphan.yml").write_text("a: 1\n")
    with pytest.raises(layers.SectionDataError, match="orphan.*schema.json.*missing"):
        config_runtime.load_sections(tmp_path)
    (tmp_path / "orphan.schema.json").write_text(json.dumps(_schema := {
        "type": "object", "additionalProperties": False, "properties": {"a": {"type": "integer"}}}))
    assert set(config_runtime.load_sections(tmp_path)) == {"orphan"} and _schema


def test_a_schema_without_a_defaults_file_still_stops_the_load(tmp_path):
    (tmp_path / "lonely.schema.json").write_text(json.dumps({"type": "object", "additionalProperties": False}))
    with pytest.raises(layers.SectionDataError, match="lonely.yml is missing"):
        config_runtime.load_sections(tmp_path)


def test_paging_from_an_unknown_change_is_an_error_not_an_empty_page(db):
    edit(db, [item("/timeout", 60)])
    with pytest.raises(LookupError, match="cfg_nope"):
        list_config_changes(before="cfg_nope", path=db)


def test_the_history_is_ordered_by_its_own_sequence_not_by_rowid(db):
    ids = [edit(db, [item("/timeout", value)])["change_id"] for value in (10, 20, 30)]
    conn = sqlite3.connect(db)
    # what VACUUM may do to a table whose primary key is text: the rowids no longer follow the write order
    conn.execute("UPDATE config_changes SET rowid = rowid + 100 WHERE change_id = ?", (ids[0],))
    conn.commit()
    conn.close()
    assert [c["change_id"] for c in list_config_changes(path=db)] == ids[::-1]
    assert [c["change_id"] for c in list_config_changes(before=ids[2], path=db)] == [ids[1], ids[0]]
    assert [c["change_id"] for c in list_config_changes(before=ids[1], path=db)] == [ids[0]]


def test_a_history_table_made_before_the_sequence_column_is_upgraded_in_place(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        DROP TABLE config_changes;
        CREATE TABLE config_changes (change_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, kind TEXT NOT NULL,
          actor_kind TEXT, actor_id TEXT, actor_label TEXT, note TEXT, items_json TEXT NOT NULL);
        INSERT INTO config_changes VALUES('cfg_a', 1, 'edit', NULL, NULL, NULL, NULL, '[]');
        INSERT INTO config_changes VALUES('cfg_b', 1, 'edit', NULL, NULL, NULL, NULL, '[]');
    """)
    conn.commit()
    conn.close()
    admin_store.init_db(path)
    admin_store.init_db(path)  # idempotent
    assert [c["change_id"] for c in list_config_changes(path=path)] == ["cfg_b", "cfg_a"]
    newest = edit(path, [item("/timeout", 5)])["change_id"]
    assert [c["change_id"] for c in list_config_changes(path=path)] == [newest, "cfg_b", "cfg_a"]


# ---------------------------------------------------------------------------------------------------------------------
# Second review (N1 to N6)
# ---------------------------------------------------------------------------------------------------------------------

def test_n1_a_boot_with_a_readable_registry_and_an_unreadable_override_store_serves_the_registrys_backends(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    monkeypatch.setattr(proxy, "_write_litellm_config_artifact", lambda registry: None)
    healthy = proxy._load_effective_runtime_config(proxy._bootstrap_config)
    assert healthy["backends"], "the fixture registry serves backends"

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(admin_store, "read_config_state", unreadable)
    effective, registry_problem = proxy._boot_effective_runtime_config(proxy._bootstrap_config)
    assert registry_problem is None, "the registry applied whole; the store is what failed"
    assert effective["backends"] == healthy["backends"], "the registry's backends are served, not zero"
    assert effective[SECTION] == DEFAULTS
    layer, _generation = config_runtime.carried(effective)
    assert [p["code"] for p in layer.problems[SECTION]] == [config_runtime.CODE_STORE_UNREADABLE]


def test_n1_a_partial_registry_with_an_unreadable_override_store_still_serves_what_validates(live, monkeypatch):  # noqa: F811
    h = home()
    monkeypatch.setattr(proxy, "CONFIG_HOME", h)
    monkeypatch.setattr(proxy, "_write_litellm_config_artifact", lambda registry: None)
    proxy._load_effective_runtime_config(proxy._bootstrap_config)  # seeds the stored registry

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(admin_store, "read_config_state", unreadable)
    effective, problem = proxy._partial_runtime_config(
        proxy._bootstrap_config, "RuntimeError: a record does not validate", require_exclusions=False, boot=True,
    )
    assert problem["mode"] == "partial" and problem["served"] > 0 and effective["backends"]
    layer, _generation = config_runtime.carried(effective)
    assert [p["code"] for p in layer.problems[SECTION]] == [config_runtime.CODE_STORE_UNREADABLE]


CAP_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["limit"],
    "properties": {"limit": {"type": "integer", "maximum": 5}},
}
USE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["x"],
    "properties": {"x": {"type": "integer", "minimum": 0}},
}


def _within_cap(candidate: Any, context: Any) -> list[layers.Problem]:
    cap = context["sections"]["a"]["limit"]
    return [layers.Problem("/x", "over_cap", layers.INVALID, "x is over the cap")] if candidate["x"] > cap else []


def cap_home() -> config_runtime.ConfigHome:
    return config_runtime.ConfigHome({
        "a": layers.Section.create("a", {"limit": 5}, CAP_SCHEMA),
        "b": layers.Section.create("b", {"x": 1}, USE_SCHEMA, validator=_within_cap),
    })


def _store_raw(db: Path, section: str, pointer: str, value: Any) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO config_overrides(section,pointer,op,value_json,origin,change_id,updated_at) VALUES(?,?, 'set',?, 'api','c-x',1) "
        "ON CONFLICT(section,pointer) DO UPDATE SET value_json=excluded.value_json", (section, pointer, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def test_n2_a_section_that_breaks_a_rule_against_the_effective_context_inherits_whatever_the_row_order():
    rows = {"a": [layers.Override("/limit", "set", 10)], "b": [layers.Override("/x", "set", 8)]}
    for reverse in (False, True):
        h = cap_home()
        order = dict(reversed(list(rows.items()))) if reverse else rows
        if reverse:
            h.sections = dict(reversed(list(h.sections.items())))
        result = h.build_candidate(order)
        assert result["a"].effective == {"limit": 5}, "a's stored 10 fails a's own schema"
        assert result["b"].effective == {"x": 1}, "b's 8 was judged against a's intent; it must not be published over 5"
        assert [p.code for p in result["b"].invalid] == ["override_rejected"]
        assert not [p for p in result["b"].problems if p.code == "over_cap"], "what is published breaks no rule"


def test_n2_a_chain_that_does_not_settle_serves_the_release_defaults_and_says_so(monkeypatch):
    real = layers.build_section
    calls = {"n": 0}

    def drifting(section, overrides, context=None, *, merge=layers.apply_overrides):
        result = real(section, overrides, context, merge=merge)
        if section.id == "a" and overrides:
            calls["n"] += 1
            return dataclasses.replace(result, effective={"flag": "off", "level": 100 + calls["n"]})  # never the same twice
        return result

    def level_is_one(candidate: Any, context: Any) -> list[layers.Problem]:
        return [layers.Problem("/level", "not_one", layers.INVALID, "level must be 1")] if candidate["level"] != 1 else []

    monkeypatch.setattr(layers, "build_section", drifting)
    h = config_runtime.ConfigHome({"a": layers.Section.create("a", dict(PAIR_DEFAULTS), PAIR_SCHEMA, validator=level_is_one)})
    result = h.build_candidate({"a": [layers.Override("/level", "set", 3)]})
    assert calls["n"] <= 1 + len(h.sections) + 1, "the loop is bounded by the number of sections"
    assert result["a"].effective == PAIR_DEFAULTS
    assert "config_unsettled" in [p.code for p in result["a"].problems]


def test_n3_a_new_bad_value_at_a_pointer_that_is_already_bad_is_refused_and_a_fix_or_a_reset_is_accepted(db):
    h = cap_home()
    validate = h.write_validator({})
    _store_raw(db, "a", "/limit", 10)  # rejected on load (schema.maximum); the served value is 5
    assert h.layer({}, path=db).config["a"] == {"limit": 5}
    before = counts(db)
    with pytest.raises(ConfigChangeInvalid) as error:
        edit(db, [item("/limit", 50, section="a")], validate=validate)
    assert [(p["section"], p["pointer"]) for p in error.value.problems] == [("a", "/limit")]
    assert counts(db) == before
    edit(db, [item("/limit", 3, section="a")], validate=validate)  # a write that fixes the pointer
    assert h.layer({}, path=db).config["a"] == {"limit": 3}
    _store_raw(db, "a", "/limit", 10)
    edit(db, [item("/limit", op="reset", section="a")], validate=validate)  # a reset of the bad row
    assert h.layer({}, path=db).config["a"] == {"limit": 5} and not read_config_state(path=db)["rows"]


def test_n4_a_rule_that_needs_the_registry_does_not_stop_the_import_check(db):
    def needs_registry(candidate: Any, context: Any) -> list[layers.Problem]:
        services = context["registry"]["services"]  # TypeError when the check has no registry
        return [layers.Problem("/level", "unknown_service", layers.INVALID, "no such service")] if (
            candidate["level"] > 1 and "svc" not in services) else []

    section = layers.Section.create("alpha", dict(PAIR_DEFAULTS), PAIR_SCHEMA, validator=needs_registry)
    h = config_runtime.ConfigHome({"alpha": section})  # must not raise: the check has no registry to give the rule
    assert h.layer({}, path=db, registry={"services": {"svc": {}}}).problems == {}
    _store_raw(db, "alpha", "/level", 2)
    assert h.layer({}, path=db, registry={"services": {"svc": {}}}).problems == {}
    assert h.layer({}, path=db, registry={"services": {}}).problems["alpha"], "with a registry the rule runs and reports"


def test_n6_with_no_section_a_load_reads_no_registry_generation(live, monkeypatch):  # noqa: F811
    assert not proxy.CONFIG_HOME.sections, "no section ships in this release"
    monkeypatch.setattr(proxy, "_write_litellm_config_artifact", lambda registry: None)
    monkeypatch.setattr(proxy, "_generation_of_registry", lambda registry: pytest.fail("the generation was read and thrown away"))
    effective = proxy._load_effective_runtime_config(proxy._bootstrap_config)
    assert type(effective) is dict and effective["backends"]
