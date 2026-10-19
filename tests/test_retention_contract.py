"""Retention slice 1 oracles (docs/design/REQUEST_RETENTION.md section 16.1 items 1, 2, 3, 10 and 15).

The behaviour record over the whole prompt-memory and Responses surface is tests/test_retention_slice1_golden.py.
This file holds the rest: decision equivalence against the code that was moved, the settings key migration, and the
rules that keep defaults in data.
"""
from __future__ import annotations

import itertools
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from gateway import admin_store, retention, retention_migration

NOW = 1_700_000_000
REGISTRY = Path(retention.BUNDLED_PATH)
SCHEMA = Path(retention.SCHEMA_PATH)
REPO = Path(__file__).resolve().parent.parent


# --- The code that slice 1 moved, kept verbatim as the reference until slice 10 (design section 16.1.1). ---

_REF_RETENTION_SECONDS = {"never": None, "3_months": 90 * 86400, "1_year": 365 * 86400}
_REF_MEMBER_DEFAULTS = {"remember": True, "retention": "never"}
_REF_HOUSEHOLD_DEFAULTS = {"default_remember": True, "never_keep": False, "retention_cap": "never"}


def _ref_decide(*, person_id, member, household, request_body, responses_store=None, now=None):
    """prompt_memory.decide as it was at a372c303, returning (person_id, keep, reason, kept_until, created_at)."""
    mode = retention.requested_mode(request_body)
    policy = {**_REF_HOUSEHOLD_DEFAULTS, **(household or {})}
    if policy["never_keep"]:
        return (person_id, False, "server_disabled", None, None)
    if mode == "forget" or responses_store is False:
        return (person_id, False, "request_forget", None, None)
    if not person_id:
        return (None, False, "no_person", None, None)
    preference = {**_REF_MEMBER_DEFAULTS, "remember": policy["default_remember"], **(member or {})}
    if not preference.get("remember", policy["default_remember"]):
        return (person_id, False, "member_disabled", None, None)
    started = int(time.time()) if now is None else now
    member_period = _REF_RETENTION_SECONDS.get(preference.get("retention", "never"))
    cap_period = _REF_RETENTION_SECONDS.get(policy.get("retention_cap", "never"))
    finite = [period for period in (member_period, cap_period) if period is not None]
    expiry = started + min(finite) if finite else None
    return (person_id, True, "remembered", expiry, started)


def _ref_responses_store(prompt_decision, requested, response_scope):
    """The store rule of proxy.create_response at a372c303 (the lines before `req_body["store"] = bool(store)`)."""
    store = requested
    if store is None:
        store = prompt_decision[0] is not None
    if response_scope is None:
        store = False
    if not prompt_decision[1] and prompt_decision[2] != "no_person":
        store = False
    return bool(store)


def _ref_effective(member, household):
    """prompt_memory.effective at a372c303."""
    def effective_retention(member_retention, cap_retention):
        finite = [v for v in (member_retention, cap_retention) if _REF_RETENTION_SECONDS.get(v) is not None]
        if not finite:
            return "never"
        return min(finite, key=lambda v: _REF_RETENTION_SECONDS[v] or 0)

    prefs = {**_REF_MEMBER_DEFAULTS, **(member or {})}
    policy = {**_REF_HOUSEHOLD_DEFAULTS, **(household or {})}
    remember = bool(prefs.get("remember", True)) and not bool(policy.get("never_keep"))
    return {"remember": remember, "retention": effective_retention(
        str(prefs.get("retention") or "never"), str(policy.get("retention_cap") or "never"))}


def _ref_me_effective(member, household):
    """me_prompt_memory._effective at a372c303 (its own copy of the same rule)."""
    order = {"3_months": 0, "1_year": 1, "never": 2}

    def shorter(left, right):
        left = left if left in order else "never"
        right = right if right in order else "never"
        return left if order[left] <= order[right] else right

    remember = bool(member.get("remember", True)) and not bool(household.get("never_keep"))
    return {"remember": remember, "retention": shorter(
        str(member.get("retention") or "never"), str(household.get("retention_cap") or "never"))}


# The pre-slice oracle covers values the old gateway accepted. Deleted IDs now
# use the stricter fallback, which is covered by the focused contract tests.
PERIODS = ("never", "3_months", "1_year")
BODIES = (None, {}, {"input": "x"}, {"extensions": {"com.azizam.prompts": {"mode": "forget"}}},
          {"extensions": {"com.azizam.prompts": {"mode": "keep"}}}, {"extensions": "x"},
          {"extensions": {"com.azizam.prompts": {"mode": "forget", "x": 1}}})


def _members():
    return [None, {}] + [{"remember": r, "retention": p} for r in (True, False) for p in PERIODS] + [{"retention": "1_year"}, {"remember": False}]


def _households():
    return [None, {}] + [
        {"default_remember": d, "never_keep": k, "retention_cap": c}
        for d in (True, False) for k in (True, False) for c in PERIODS
    ] + [{"never_keep": True}, {"default_remember": False}, {"retention_cap": "3_months"}]


@pytest.mark.parametrize("kind", retention.KINDS)
def test_decide_equals_the_moved_decision_on_every_input(kind):
    """16.1.1: kill switch x forget x store true/false/absent x person x member keep/period x household cap x kind."""
    checked = 0
    for person, member, household, body, store in itertools.product(
        (None, "person"), _members(), _households(), BODIES, (None, True, False),
    ):
        try:
            want = _ref_decide(person_id=person, member=member, household=household, request_body=body,
                               responses_store=store, now=NOW)
        except retention.PromptModeError as exc:
            with pytest.raises(retention.PromptModeError, match=re.escape(str(exc))):
                retention.decide(kind, person_id=person, member=member, household_policy=household,
                                 request_body=body, responses_store=store, now=NOW)
            continue
        got = retention.decide(kind, person_id=person, member=member, household_policy=household,
                               request_body=body, responses_store=store, now=NOW)
        assert (got.person_id, got.keep, got.reason, got.kept_until, got.created_at) == want, (
            kind, person, member, household, body, store)
        assert got.kind == kind
        checked += 1
    assert checked > 4000


def test_a_kept_decision_names_the_parts_the_data_allows_by_default():
    for kind, parts in ((retention.LIBRARY_PROMPT, ("accepted",)), (retention.CONVERSATION, ("accepted", "received"))):
        kept = retention.decide(kind, person_id="p", member=None, household_policy=None, request_body=None, now=NOW)
        assert (kept.keep, kept.kind, kept.parts) == (True, kind, parts)
        refused = retention.decide(kind, person_id="p", member=None, household_policy={"never_keep": True},
                                   request_body=None, now=NOW)
        assert (refused.keep, refused.kind, refused.parts) == (False, kind, ())


def test_responses_store_rule_equals_the_moved_rule():
    decisions = [
        retention.decide(retention.CONVERSATION, person_id=person, member=member, household_policy=household,
                         request_body=body, responses_store=None, now=NOW)
        for person, member, household, body in itertools.product(
            (None, "person"), [None, {"remember": False}], [None, {"never_keep": True}, {"default_remember": False}],
            BODIES[:4])
    ]
    for decision, requested, scope in itertools.product(decisions, (None, True, False, "yes", 0, 1), (None, {"app_id": "a"})):
        want = _ref_responses_store((decision.person_id, decision.keep, decision.reason), requested, scope)
        assert retention.conversation_store(decision, requested, has_scope=scope is not None) is want


def test_effective_periods_equal_both_moved_copies():
    members = [None, {}] + [{"remember": r, "retention": p} for r in (True, False) for p in (*PERIODS, "", None)]
    households = [None, {}] + [{"never_keep": k, "retention_cap": c} for k in (True, False) for c in (*PERIODS, "", None)]
    for member, household in itertools.product(members, households):
        assert retention.effective(member, household) == _ref_effective(member, household)
    # The member settings route normalizes the household first; the same answer through its own copy of the rule.
    from gateway import me_prompt_memory
    for member, household in itertools.product(
        [{"remember": r, "retention": p} for r in (True, False) for p in PERIODS],
        [me_prompt_memory._household_policy(h) for h in households],
    ):
        assert me_prompt_memory._effective(member, household) == _ref_me_effective(member, household)


# --- 16.1.2 The settings key migration -----------------------------------------------------------------------------


def _legacy_database(path: Path, setting: dict | str | None, *, generation: int | None = 3, with_members: bool = True):
    """A database as the previous gateway left it: schema 13, the household policy under `prompt_memory`."""
    admin_store.init_db(path)
    members = []
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM operator_settings WHERE key IN ('prompt_memory','retention')")
        conn.execute("DELETE FROM operator_settings_generations WHERE setting_key IN ('prompt_memory','retention')")
        if setting is not None:
            payload = setting if isinstance(setting, str) else json.dumps(setting, sort_keys=True)
            conn.execute("INSERT INTO operator_settings(key,value_json,updated_at,updated_by) VALUES('prompt_memory',?,?,?)",
                         (payload, 1_650_000_000, "owner-1"))
            if generation is not None:
                conn.execute("INSERT INTO operator_settings_generations(setting_key,operator_settings_generation) "
                             "VALUES('prompt_memory',?)", (generation,))
        conn.execute("UPDATE schema_version SET version=13")
    if with_members:
        for index, (remember, period) in enumerate([(True, "never"), (False, "1_year"), (True, "3_months")]):
            user = admin_store.upsert_oidc_user("iss", f"member{index}", email=f"m{index}@example.test", path=path)["id"]
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE prompt_memory_preferences SET remember=?, retention=? WHERE user_id=?",
                             (1 if remember else 0, period, user))
            members.append(user)
    return members


def _period(decision) -> tuple:
    return (decision.keep, decision.reason, None if decision.kept_until is None else decision.kept_until - decision.created_at)


def _meaning(path: Path) -> dict:
    """What the settings say, as the running gateway reads them, whichever key holds them."""
    from gateway import prompt_memory, settings_api
    record = admin_store.get_operator_setting(retention.SETTING_KEY, path=path)
    legacy = admin_store.get_operator_setting(retention.PROMPTS_VIEW_KEY, path=path)
    with sqlite3.connect(path) as conn:
        members = [list(row) for row in conn.execute(
            "SELECT user_id, remember, retention, updated_at, updated_by FROM prompt_memory_preferences ORDER BY user_id")]
        cutoffs = [list(row) for row in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'prompt_memory_erasure%' ORDER BY name")]
        others = [list(row) for row in conn.execute(
            "SELECT key, value_json, updated_at, updated_by FROM operator_settings WHERE key NOT IN ('prompt_memory','retention') ORDER BY key")]
    holder = record or legacy
    return {
        "household": settings_api.prompt_memory_settings(path),
        "household_settings": prompt_memory.household_settings(admin_path=path),
        "revision": admin_store.operator_setting_revision(holder),
        "updated": (holder["updated_at"], holder["updated_by"]) if holder else None,
        "members": members, "erasure_tables": cutoffs, "other_settings": others,
        "decisions": {row[0]: _period(prompt_memory.decide_for_person(row[0], None, admin_path=path)) for row in members},
    }


LEGACY_SETTINGS = [
    None,
    {},
    {"never_keep": True},
    {"default_remember": False},
    {"retention_cap": "3_months"},
    {"default_remember": False, "never_keep": False, "retention_cap": "1_year"},
    {"default_remember": True, "never_keep": True, "retention_cap": "never"},
]


@pytest.mark.parametrize("legacy", LEGACY_SETTINGS)
def test_migration_keeps_every_setting_and_member_decision(tmp_path, legacy):
    path = tmp_path / "admin.db"
    _legacy_database(path, legacy)
    with sqlite3.connect(path) as conn:
        before_rows = {key: row for key, *row in conn.execute("SELECT key, value_json, updated_at, updated_by FROM operator_settings")}
    # The meaning before the move is read from the legacy row by the old reader's rules.
    expect_household = {**{"default_remember": True, "never_keep": False, "retention_cap": "never"}, **(legacy or {})}
    admin_store.init_db(path)  # the gateway starts: schema 13 -> 14 moves the setting
    assert admin_store.schema_version(path) == admin_store.SCHEMA_VERSION >= 14
    meaning = _meaning(path)
    assert meaning["household"] == expect_household
    with sqlite3.connect(path) as conn:
        keys = {row[0] for row in conn.execute("SELECT key FROM operator_settings")}
        stored = conn.execute("SELECT value_json, updated_at, updated_by FROM operator_settings WHERE key='retention'").fetchone()
    assert "prompt_memory" not in keys
    if legacy is None:
        assert stored is None
        return
    assert stored[1:] == (before_rows["prompt_memory"][1], before_rows["prompt_memory"][2])
    assert meaning["revision"] == "3"  # the counter carried over
    # Idempotent: a second start changes nothing.
    again = _meaning(path)
    admin_store.init_db(path)
    assert _meaning(path) == again == meaning
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value_json FROM operator_settings WHERE key='retention'").fetchone()[0] == stored[0]


def test_migration_maps_each_old_value_to_the_prompts_group(tmp_path):
    path = tmp_path / "admin.db"
    _legacy_database(path, {"default_remember": False, "never_keep": True, "retention_cap": "1_year"})
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        document = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='retention'").fetchone()[0])
    assert document == {"never_keep": True, "groups": {"prompts": {"default_keep": False, "retention_cap": "1_year"}}}


def test_migration_leaves_prompts_conversations_and_deadlines_alone(tmp_path):
    from gateway import media_jobs, prompt_memory
    path = tmp_path / "admin.db"
    media = tmp_path / "media_jobs.db"
    members = _legacy_database(path, {"default_remember": True, "never_keep": False, "retention_cap": "1_year"})
    media_jobs.init_db(media)
    prompt_memory.init_db(path=media)
    now = int(time.time())
    for index, user in enumerate(members):
        prompt_memory.put(f"asset_{index}", user, f"words {index}", kept_until=now + 86400 * (index + 1),
                          created_at=now - 100, path=media)
    with sqlite3.connect(media) as conn:
        before = conn.execute("SELECT * FROM asset_prompts ORDER BY asset_id").fetchall()
    admin_store.init_db(path)
    with sqlite3.connect(media) as conn:
        assert conn.execute("SELECT * FROM asset_prompts ORDER BY asset_id").fetchall() == before
    # Every member's decision is what the old rules gave.
    for user, (remember, period) in zip(members, [(True, "never"), (False, "1_year"), (True, "3_months")]):
        got = prompt_memory.decide_for_person(user, None, admin_path=path)
        want = _ref_decide(person_id=user, member={"remember": remember, "retention": period},
                           household={"default_remember": True, "never_keep": False, "retention_cap": "1_year"},
                           request_body=None)
        assert (got.person_id, got.keep, got.reason) == want[:3]
        if got.keep:
            assert got.kept_until - got.created_at == want[3] - want[4]


def test_a_partial_failure_rolls_the_whole_migration_back(tmp_path, monkeypatch):
    path = tmp_path / "admin.db"
    _legacy_database(path, {"never_keep": True})

    def boom(conn):
        raise RuntimeError("stop after the move")

    monkeypatch.setattr(admin_store, "_prompt_memory_default", boom)  # runs in the same transaction, after the move
    with pytest.raises(RuntimeError, match="stop after the move"):
        admin_store.init_db(path)
    monkeypatch.undo()
    with sqlite3.connect(path) as conn:
        keys = {row[0]: row[1] for row in conn.execute("SELECT key, value_json FROM operator_settings")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        generations = {row[0]: row[1] for row in conn.execute("SELECT setting_key, operator_settings_generation FROM operator_settings_generations")}
    assert version == 13 and "retention" not in keys and json.loads(keys["prompt_memory"]) == {"never_keep": True}
    assert generations["prompt_memory"] == 3 and "retention" not in generations
    admin_store.init_db(path)  # and the next start moves it
    assert admin_store.schema_version(path) == admin_store.SCHEMA_VERSION


def test_migration_refuses_to_choose_between_two_policies(tmp_path):
    path = tmp_path / "admin.db"
    _legacy_database(path, {"never_keep": True})
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at,updated_by) VALUES('retention','{}',1,NULL)")
    with pytest.raises(retention_migration.RetentionMigrationError, match="both"):
        admin_store.init_db(path)


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", "3"])
def test_migration_refuses_a_setting_it_cannot_read(tmp_path, payload):
    path = tmp_path / "admin.db"
    _legacy_database(path, payload)
    with pytest.raises(retention_migration.RetentionMigrationError):
        admin_store.init_db(path)


def test_migration_preserves_unknown_fields_without_exposing_them(tmp_path):
    path = tmp_path / "admin.db"
    _legacy_database(path, {"never_keep": False, "x": {"future": 1}, "retention_cap": "3_months"})
    admin_store.init_db(path)
    stored = admin_store.get_operator_setting("retention", path=path)["value"]
    assert stored["legacy_unclassified"] == {"x": {"future": 1}}
    from gateway import settings_api
    assert settings_api.prompt_memory_settings(path) == {
        "default_remember": True, "never_keep": False, "retention_cap": "3_months"}
    admin_store.put_operator_setting("prompt_memory", {
        "default_remember": False, "never_keep": False, "retention_cap": "3_months"}, path=path)
    assert admin_store.get_operator_setting("retention", path=path)["value"]["legacy_unclassified"] == {
        "x": {"future": 1}}


def test_a_database_that_never_had_the_setting_stays_without_one(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    assert admin_store.get_operator_setting("retention", path=path) is None
    assert admin_store.get_operator_setting("prompt_memory", path=path) is None


def test_after_the_move_the_member_switch_still_governs_prompts_and_conversations(tmp_path):
    """16.1.2: the switch is changed after the migration, not only once before it."""
    from gateway import media_jobs, prompt_memory
    path = tmp_path / "admin.db"
    media = tmp_path / "media_jobs.db"
    (user, *_rest) = _legacy_database(path, {"default_remember": True, "never_keep": False, "retention_cap": "never"})
    media_jobs.init_db(media)
    prompt_memory.init_db(path=media)
    admin_store.init_db(path)
    assert prompt_memory.put("asset", user, "words", kept_until=None, path=media, admin_path=path)
    admin_store.put_prompt_memory_preference(user, remember=False, retention="never", path=path)
    assert prompt_memory.decide_for_person(user, None, admin_path=path).reason == "member_disabled"
    assert prompt_memory.decide_for_person(user, {"store": True}, admin_path=path,
                                           responses_store=True, kind=retention.CONVERSATION).keep is False
    assert prompt_memory.put("asset_2", user, "more", kept_until=None, path=media, admin_path=path) is False


# --- 16.1.3 Data, not code -----------------------------------------------------------------------------------------


def _bundled_data() -> dict:
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))


def _mutated(mutate) -> dict:
    data = _bundled_data()
    mutate(data)
    return data


def test_the_bundled_file_passes_the_validator():
    contract = retention.parse_contract(_bundled_data(), schema=json.loads(SCHEMA.read_text()))
    assert contract.household_defaults(retention.LIBRARY_PROMPT) == {
        "default_remember": True, "never_keep": False, "retention_cap": "never"}
    assert contract.member_defaults(retention.CONVERSATION) == {"remember": True, "retention": "never"}
    assert contract.seconds_map() == {"never": None, "3_months": 90 * 86400, "1_year": 365 * 86400}


ILLEGAL = {
    "readers outside the core list": lambda d: d["kinds"]["library_prompt"].update(readers="everyone"),
    "scope readers on a person-scoped kind": lambda d: d["kinds"]["library_prompt"].update(readers="scope"),
    "maker readers on the conversation": lambda d: d["kinds"]["conversation"].update(readers="maker"),
    "a parts default outside allowed": lambda d: d["kinds"]["library_prompt"]["parts"].update(default=["received"]),
    "parts the kind never captures": lambda d: d["kinds"]["library_prompt"]["parts"].update(allowed=["accepted", "sent"]),
    "a kind in two groups": lambda d: d["groups"].update(extra={**d["groups"]["prompts"], "kinds": ["conversation"]}),
    "a grouped kind with its own choices": lambda d: d["kinds"]["conversation"].update(choices=["never"]),
    "a default retention outside the choices": lambda d: d["groups"]["prompts"].update(default_retention="1_hour"),
    "a default cap outside the choices": lambda d: d["groups"]["prompts"].update(default_cap="7_days"),
    "a choice missing from the vocabulary": lambda d: d["groups"]["prompts"].update(choices=["never", "2_weeks"]),
    "widening other than none": lambda d: d.update(widening="members"),
    "a kind core cannot capture": lambda d: d["kinds"].update(request_history=d["kinds"]["library_prompt"]),
    "a core kind with no values": lambda d: d["kinds"].pop("conversation"),
    "a kind in no group": lambda d: d["groups"]["prompts"].update(kinds=["library_prompt"]),
    "an unknown field": lambda d: d.update(surprise=True),
    "a wrong format version": lambda d: d.update(format_version=2),
    "a missing group field": lambda d: d["groups"]["prompts"].pop("default_keep"),
}


@pytest.mark.parametrize("name", sorted(ILLEGAL))
def test_the_validator_refuses_each_illegal_value(name):
    data = _mutated(ILLEGAL[name])
    with pytest.raises(retention.RetentionDataError) as error:
        retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()), source="retention.yml")
    assert "retention.yml" in str(error.value)


@pytest.mark.parametrize("kind,readers", [("library_prompt", "maker_signed_in"),
                                            ("conversation", "maker_signed_in")])
def test_unenforced_reader_controls_are_refused_until_slice_5(kind, readers):
    data = _bundled_data()
    data["kinds"][kind]["readers"] = readers
    with pytest.raises(retention.RetentionDataError, match="slice 5.*reader"):
        retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))


def test_unenforced_conversation_parts_are_refused_until_slice_5():
    data = _bundled_data()
    data["kinds"]["conversation"]["parts"]["default"] = ["accepted"]
    with pytest.raises(retention.RetentionDataError, match="slice 5.*parts"):
        retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))


def test_a_renamed_prompts_group_is_refused_until_slice_5():
    data = _bundled_data()
    data["groups"]["saved"] = data["groups"].pop("prompts")
    with pytest.raises(retention.RetentionDataError, match="slice 5.*group"):
        retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))


def test_removed_stored_period_has_one_display_decision_and_writer_deadline(tmp_path, monkeypatch):
    from gateway import prompt_memory
    data = _bundled_data()
    data["groups"]["prompts"].update(choices=["3_months"], default_retention="3_months", default_cap="3_months")
    del data["retention_choices"]["never"]
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    admin_path = tmp_path / "admin.db"
    media_path = tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "removed-period", path=admin_path)["id"]
    with sqlite3.connect(admin_path) as conn:
        conn.execute("UPDATE prompt_memory_preferences SET retention='never' WHERE user_id=?", (person,))
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at,updated_by) "
                     "VALUES('retention', ?, 1, NULL)",
                     (json.dumps({"groups": {"prompts": {"retention_cap": "never"}}}),))
    member = {"remember": True, "retention": "never"}
    household = {"never_keep": False, "default_remember": True, "retention_cap": "never"}
    started = int(time.time())
    expected_deadline = started + contract.seconds_map()["3_months"]
    assert retention.effective(member, household)["retention"] == "3_months"
    decision = retention.decide(retention.LIBRARY_PROMPT, person_id=person, member=member,
                                household_policy=household, request_body={}, now=started)
    assert decision.kept_until == expected_deadline
    assert prompt_memory.put("removed-period-asset", person, "prompt sentinel", kept_until=decision.kept_until,
                             created_at=started, path=media_path, admin_path=admin_path)
    assert prompt_memory.get("removed-period-asset", person, path=media_path)["kept_until"] == expected_deadline
    from gateway import settings_api
    assert settings_api.prompt_memory_settings(admin_path)["retention_cap"] == "3_months"
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.admin_oidc import AdminSession
    from gateway.me_prompt_memory import create_me_prompt_memory_router
    app = FastAPI()
    app.include_router(create_me_prompt_memory_router(
        session_provider=lambda request: AdminSession(subject=person, role="none", email=None,
                                                      issued_at=time.time(), expires_at=time.time() + 60),
        admin_db_path=admin_path, prompt_memory_path=media_path,
        household_provider=lambda: prompt_memory.household_settings(admin_path=admin_path)))
    response = TestClient(app).get("/me/api/prompt-memory")
    assert response.status_code == 200
    assert response.json()["prompt_memory"]["retention"] == "3_months"
    assert response.json()["prompt_memory"]["effective_retention"] == "3_months"


def test_removed_period_shortens_existing_prompt_deadlines(tmp_path, monkeypatch):
    from gateway import prompt_memory
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "earlier-prompt", path=admin_path)["id"]
    started = int(time.time())
    assert prompt_memory.put("earlier-asset", person, "old prompt", kept_until=None,
                             created_at=started, path=media_path, admin_path=admin_path)
    data = _bundled_data()
    data["groups"]["prompts"].update(choices=["3_months"], default_retention="3_months", default_cap="3_months")
    del data["retention_choices"]["never"]
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 1
    assert prompt_memory.get("earlier-asset", person, path=media_path)["kept_until"] == (
        started + contract.seconds_map()["3_months"])
    assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 0


@pytest.mark.parametrize("stored,owner_cap", [("3_months", False), ("3_months", True)])
def test_known_choice_keeps_its_period_when_no_longer_offered(monkeypatch, stored, owner_cap):
    data = _bundled_data()
    data["retention_choices"]["1_month"] = 30 * 86400
    data["groups"]["prompts"].update(choices=["never", "1_year", "1_month"],
                                      default_retention="never", default_cap="never")
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert retention.normalize_period(stored, kind=retention.LIBRARY_PROMPT, owner_cap=owner_cap) == "3_months"


def test_known_choice_keeps_its_period_when_only_longer_choice_is_offered(monkeypatch):
    data = _bundled_data()
    data["groups"]["prompts"].update(choices=["never", "1_year"], default_retention="never", default_cap="never")
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert retention.normalize_period("3_months", kind=retention.LIBRARY_PROMPT, owner_cap=False) == "3_months"


def test_bundled_kill_switch_is_refused_until_startup_erasure_exists():
    data = _bundled_data()
    data["household"]["never_keep"] = True
    with pytest.raises(retention.RetentionDataError, match="slice 5.*never_keep|never_keep.*slice 5"):
        retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))


@pytest.mark.parametrize("edit,member,old_days,want_days", [
    ("default_cap", "never", None, 365),
    ("seconds", "3_months", 90, 30),
])
def test_startup_repair_applies_every_stricter_contract_edit(tmp_path, monkeypatch, edit, member, old_days, want_days):
    from gateway import prompt_memory
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", edit, path=admin_path)["id"]
    with sqlite3.connect(admin_path) as conn:
        conn.execute("UPDATE prompt_memory_preferences SET retention=? WHERE user_id=?", (member, person))
    created = int(time.time()) - 86400
    prompt_memory._connect(media_path).close()
    with sqlite3.connect(media_path) as conn:
        conn.execute("INSERT INTO asset_prompts VALUES (?,?,?,?,?,?)",
                     ("asset", person, "prompt", None, created,
                      None if old_days is None else created + old_days * 86400))
    data = _bundled_data()
    if edit == "default_cap":
        data["groups"]["prompts"]["default_cap"] = "1_year"
    else:
        data["retention_choices"]["3_months"] = 30 * 86400
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 1
    with sqlite3.connect(media_path) as conn:
        assert conn.execute("SELECT kept_until FROM asset_prompts WHERE asset_id='asset'").fetchone()[0] == (
            created + want_days * 86400)
    assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 0


@pytest.mark.parametrize("stored", ["{", "[]", '{"groups": []}'])
def test_startup_repair_tolerates_corrupt_stored_setting(tmp_path, caplog, stored):
    from gateway import prompt_memory
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    with sqlite3.connect(admin_path) as conn:
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES(?,?,1)",
                     (retention.SETTING_KEY, stored))
    assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 0
    assert caplog.text.count("invalid stored retention setting") == 1


@pytest.mark.parametrize("remove_from_vocabulary", [False, True])
def test_maintenance_uses_normalized_member_period(tmp_path, monkeypatch, remove_from_vocabulary):
    import proxy
    from gateway import media_jobs, prompt_memory
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "maintenance", path=admin_path)["id"]
    with sqlite3.connect(admin_path) as conn:
        conn.execute("UPDATE prompt_memory_preferences SET retention='1_year' WHERE user_id=?", (person,))
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_path)
    monkeypatch.setattr(media_jobs, "DB_PATH", media_path)
    data = _bundled_data()
    data["groups"]["prompts"].update(choices=["never", "3_months"], default_retention="never", default_cap="never")
    if remove_from_vocabulary:
        del data["retention_choices"]["1_year"]
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    decision = prompt_memory.decide_for_person(person, None, admin_path=admin_path)
    expected_days = 90 if remove_from_vocabulary else 365
    assert decision.kept_until == decision.created_at + expected_days * 86400
    assert prompt_memory.put("asset", person, "prompt", kept_until=decision.kept_until, created_at=decision.created_at,
                             path=media_path, admin_path=admin_path)
    proxy._maintain_person_prompt_memory(person)
    assert prompt_memory.get("asset", person, path=media_path)["kept_until"] == decision.kept_until


def test_startup_repair_reads_policy_while_another_writer_holds_admin_lock(tmp_path, monkeypatch):
    from gateway import prompt_memory
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "writer", path=admin_path)["id"]
    assert prompt_memory.put("asset", person, "prompt", kept_until=None, created_at=int(time.time()),
                             path=media_path, admin_path=admin_path)
    data = _bundled_data()
    data["groups"]["prompts"]["default_cap"] = "1_year"
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    connect = prompt_memory.connect

    def short_wait(path, **kwargs):
        if Path(path) == admin_path:
            kwargs["timeout"] = 0.05
        return connect(path, **kwargs)

    monkeypatch.setattr(prompt_memory, "connect", short_wait)
    with sqlite3.connect(admin_path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 1


@pytest.mark.parametrize("owner_cap", [False, True])
def test_deleted_stored_period_uses_shortest_finite_offered_choice(monkeypatch, owner_cap):
    data = _bundled_data()
    data["groups"]["prompts"].update(choices=["never", "3_months"],
                                      default_retention="never", default_cap="never")
    del data["retention_choices"]["1_year"]
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert retention.normalize_period("1_year", kind=retention.LIBRARY_PROMPT, owner_cap=owner_cap) == "3_months"
    member = {"remember": True, "retention": "never" if owner_cap else "1_year"}
    household = {"retention_cap": "1_year" if owner_cap else "never"}
    decision = retention.decide(retention.LIBRARY_PROMPT, person_id="person", member=member,
                                household_policy=household, request_body={}, now=NOW)
    assert decision.kept_until == NOW + 90 * 86400


def test_known_stored_period_stays_exact_when_not_offered(tmp_path, monkeypatch):
    from gateway import settings_api
    from gateway.me_prompt_memory import _member_settings
    data = _bundled_data()
    data["groups"]["prompts"].update(choices=["never", "1_year"],
                                      default_retention="never", default_cap="never")
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert retention.normalize_period("3_months", kind=retention.LIBRARY_PROMPT, owner_cap=False) == "3_months"
    admin_path = tmp_path / "admin.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "legacy-choice", path=admin_path)["id"]
    with sqlite3.connect(admin_path) as conn:
        conn.execute("UPDATE prompt_memory_preferences SET retention='3_months' WHERE user_id=?", (person,))
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES('retention',?,1)",
                     (json.dumps({"groups": {"prompts": {"retention_cap": "3_months"}}}),))
    assert _member_settings(person, path=admin_path)["retention"] == "3_months"
    assert settings_api.prompt_memory_settings(admin_path)["retention_cap"] == "3_months"
    assert contract.public()["known_unoffered"] == [{"id": "3_months", "seconds": 90 * 86400}]


def test_startup_tightens_saved_prompts_without_a_preference_row(tmp_path, monkeypatch):
    from gateway import prompt_memory
    admin_path, media_path = tmp_path / "admin.db", tmp_path / "media.db"
    admin_store.init_db(admin_path)
    person = admin_store.upsert_oidc_user("iss", "missing-preference", path=admin_path)["id"]
    with sqlite3.connect(admin_path) as conn:
        conn.execute("DELETE FROM prompt_memory_preferences WHERE user_id=?", (person,))
    created = int(time.time()) - 86400
    prompt_memory._connect(media_path).close()
    with sqlite3.connect(media_path) as conn:
        conn.execute("INSERT INTO asset_prompts VALUES(?,?,?,?,?,?)", ("asset", person, "prompt", None, created, None))
    data = _bundled_data()
    data["groups"]["prompts"]["default_retention"] = "3_months"
    contract = retention.parse_contract(data, schema=json.loads(SCHEMA.read_text()))
    monkeypatch.setattr(retention, "bundled", lambda: contract)
    assert prompt_memory.tighten_contract_periods(admin_path=admin_path, path=media_path) == 1
    with sqlite3.connect(media_path) as conn:
        assert conn.execute("SELECT kept_until FROM asset_prompts WHERE asset_id='asset'").fetchone()[0] == (
            created + 90 * 86400)
    assert admin_store.get_prompt_memory_preference(person, path=admin_path) is None


def test_startup_repair_failure_does_not_stop_gateway_boot(monkeypatch, capsys):
    import asyncio
    import proxy

    async def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic busy database")

    monkeypatch.setattr(proxy.asyncio, "to_thread", unavailable)
    asyncio.run(proxy._repair_prompt_deadlines_at_startup())
    assert "prompt deadline repair pending: OperationalError" in capsys.readouterr().out


def test_a_missing_file_stops_startup_with_a_plain_message(tmp_path, monkeypatch):
    import asyncio
    import proxy
    monkeypatch.setattr(retention, "BUNDLED_PATH", tmp_path / "retention.yml")

    async def start():
        async with proxy.lifespan(proxy.app):
            pass

    with pytest.raises(retention.RetentionDataError, match="cannot read the retention file"):
        asyncio.run(start())


def test_a_broken_file_stops_startup_with_the_rule_it_broke(tmp_path, monkeypatch):
    broken = tmp_path / "retention.yml"
    broken.write_text(yaml.safe_dump(_mutated(ILLEGAL["widening other than none"])), encoding="utf-8")
    monkeypatch.setattr(retention, "BUNDLED_PATH", broken)
    with pytest.raises(retention.RetentionDataError, match="widening"):
        retention.require_bundled()


def test_changing_a_default_in_the_file_changes_a_new_member_with_no_code_change(tmp_path, monkeypatch):
    from gateway import prompt_memory
    changed = tmp_path / "retention.yml"
    data = _bundled_data()
    data["groups"]["prompts"]["default_keep"] = False
    data["groups"]["prompts"]["default_retention"] = "1_year"
    data["groups"]["prompts"]["default_cap"] = "1_year"
    changed.write_text(yaml.safe_dump(data), encoding="utf-8")
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    keeper = admin_store.upsert_oidc_user("iss", "before", email="b@example.test", path=path)["id"]
    monkeypatch.setattr(retention, "BUNDLED_PATH", changed)
    newcomer = admin_store.upsert_oidc_user("iss", "after", email="a@example.test", path=path)["id"]
    assert prompt_memory.get_member(newcomer, path=path) == {"remember": False, "retention": "1_year"}
    assert prompt_memory.decide_for_person(newcomer, None, admin_path=path).reason == "member_disabled"
    # A row that already existed is never changed by a new default.
    assert prompt_memory.get_member(keeper, path=path) == {"remember": True, "retention": "never"}
    assert prompt_memory.household_settings(admin_path=path) == {
        "default_remember": False, "never_keep": False, "retention_cap": "1_year"}


SOURCE_ROOTS = ("gateway", "proxy.py")
LITERALS = (
    re.compile(r"""["'](3_months|1_year)["']"""),
    re.compile(r"\b(90|365)\s*\*\s*86400\b"),
    re.compile(r"\b(HOUSEHOLD_DEFAULTS?|MEMBER_DEFAULTS?|RETENTION_SECONDS|PROMPT_MEMORY_DEFAULT|RETENTION_ORDER)\b"),
    re.compile(r"""["']default_remember["']\s*:\s*(True|False)"""),
    re.compile(r"""["']retention_cap["']\s*:\s*["']never["']"""),
    re.compile(r"""["']retention["']\s*:\s*["']never["']"""),
)


def _python_sources():
    for root in SOURCE_ROOTS:
        base = REPO / root
        yield from ([base] if base.is_file() else sorted(base.rglob("*.py")))


def test_no_retention_default_is_written_in_python():
    """16.1.3: the defaults are in registry/retention.yml. A grep finds none in the gateway's Python."""
    offenders = []
    sources = list(_python_sources())
    assert len(sources) > 100  # the scan reads the gateway; a moved directory must not make it pass by reading nothing
    for source in sources:
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for literal in LITERALS:
                if literal.search(line):
                    offenders.append(f"{source.relative_to(REPO)}:{number}: {line.strip()}")
    assert offenders == []


WEB_LITERALS = (
    re.compile(r"""["'](3_months|1_year)["']"""),
    re.compile(r"""default_remember\s*:\s*(true|false)"""),
    re.compile(r"""retention_cap\s*:\s*["']"""),
    re.compile(r"""\bretention\s*:\s*["']never["']"""),
)


def test_no_retention_default_is_written_in_the_web_source():
    """16.1.3: the screens read the choices and defaults from the settings responses (their `contract`)."""
    offenders = []
    scanned = 0
    for source in sorted((REPO / "web" / "src").rglob("*.ts*")):
        if re.search(r"\.spec\.tsx?$", source.name):
            continue
        scanned += 1
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            for literal in WEB_LITERALS:
                if literal.search(line):
                    offenders.append(f"{source.relative_to(REPO)}:{number}: {line.strip()}")
    assert scanned > 100  # the scan reads the web source; a moved directory must not make it pass by reading nothing
    assert offenders == []


# --- 16.1.15 Conversation scope --------------------------------------------------------------------------------------


SCOPE_APP = {"person_id": "p1", "app_id": "app1", "owner_key_hash": None}
SCOPE_KEY = {"person_id": "p1", "app_id": None, "owner_key_hash": "sha256:k"}


@pytest.mark.parametrize("row, scope, visible", [
    ({"person_id": "p1", "app_id": "app1"}, SCOPE_APP, True),
    ({"person_id": "p1", "app_id": "app2"}, SCOPE_APP, False),
    ({"person_id": "p2", "app_id": "app1"}, SCOPE_APP, False),
    ({"person_id": None, "app_id": "app1"}, SCOPE_APP, False),
    ({"person_id": None, "app_id": "app1"}, {**SCOPE_APP, "person_id": None}, True),
    ({"app_id": "app1"}, SCOPE_APP, False),
    ({"person_id": "p1", "app_id": None, "owner_key_hash": "sha256:k"}, SCOPE_KEY, True),
    ({"person_id": "p1", "app_id": None, "owner_key_hash": "sha256:other"}, SCOPE_KEY, False),
    ({"person_id": "p1", "app_id": "app1", "owner_key_hash": "sha256:k"}, SCOPE_KEY, False),
    ({"person_id": "p1", "app_id": "app1"}, None, False),
    (None, SCOPE_APP, False),
    ({}, SCOPE_APP, False),
])
def test_the_conversation_scope_rule(row, scope, visible):
    assert retention.conversation_row_visible(row, scope) is visible


def test_a_null_person_never_matches_a_person_check():
    assert retention.conversation_row_visible({"person_id": None, "app_id": "a"}, {**SCOPE_APP, "app_id": "a"}) is False
    assert retention.conversation_row_visible({"person_id": "p1", "app_id": "a"}, {**SCOPE_APP, "person_id": None, "app_id": "a"}) is False


# --- The settings responses carry the contract the web reads ---------------------------------------------------------


def test_the_settings_responses_carry_the_bundled_choices_and_defaults(tmp_path):
    import time as _time
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from gateway.admin_api import AdminContext
    from gateway.admin_oidc import AdminSession
    from gateway.me_prompt_memory import create_me_prompt_memory_router
    from gateway.settings_api import create_settings_router

    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    user = admin_store.upsert_oidc_user("iss", "carol", email="c@example.test", path=path)["id"]
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at,updated_by) "
                     "VALUES('retention', ?, 1, NULL)",
                     (json.dumps({"legacy_unclassified": {"x": "hidden legacy sentinel"}}),))
    app = FastAPI()
    app.include_router(create_settings_router(
        auth=lambda request: AdminContext(subject="o", roles=frozenset({"owner"}), auth_method="session", csrf_token="x"),
        admin_path=path, config_provider=lambda: {}))
    app.include_router(create_me_prompt_memory_router(
        session_provider=lambda request: AdminSession(subject=user, email="c@example.test", role="none",
                                                      issued_at=_time.time(), expires_at=_time.time() + 60),
        admin_db_path=path))
    client = TestClient(app)
    expected = retention.public_contract()
    assert expected["choices"] == [{"id": "never", "seconds": None}, {"id": "1_year", "seconds": 31536000},
                                   {"id": "3_months", "seconds": 7776000}]
    assert expected["defaults"] == {"default_remember": True, "never_keep": False, "retention_cap": "never",
                                    "remember": True, "retention": "never"}
    owner = client.get("/admin/api/settings/prompt-memory", headers={"x-azizam-csrf": "x"}).json()
    member = client.get("/me/api/prompt-memory").json()
    assert owner["contract"] == member["contract"] == expected
    assert "hidden legacy sentinel" not in json.dumps(owner)
    assert "hidden legacy sentinel" not in json.dumps(member)
