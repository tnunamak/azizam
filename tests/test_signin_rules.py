"""Sign-in rules: the evaluator and the schema 9 migration.

The evaluator is one pure function used by provider sign-in, the What changes
preview and the test sign-in. These tests pin its answers. The migration tests
start from a database at schema 8 shaped like the live instance.
"""
import json
import sqlite3

import pytest

from gateway import admin_store, signin_admin, signin_rules
from gateway.signin_rules import Claims, Rules, Standing, evaluate, parse_rules


def rules_of(*rules, fallback="closed"):
    return parse_rules({"fallback": fallback, "rules": list(rules)})


def rule(field, values=(), *, admit=False, admin=False, plan=None, id=None):
    return {"id": id or f"{field}-{'-'.join(values) or 'all'}", "field": field, "values": list(values),
            "admit": admit, "admin": admin, "plan": plan}


PERSON = Claims(email="Simon@Example.test", groups=("/Trusted", "/Family"), roles=("power",))


# ---------------------------------------------------------------------------
# Deny by default
# ---------------------------------------------------------------------------

def test_with_no_rules_nobody_gets_in():
    outcome = evaluate(Rules(), PERSON)
    assert outcome.admitted is False and outcome.admin is False and outcome.plan is None


def test_the_owner_and_an_invited_person_get_in_without_any_rule():
    assert evaluate(Rules(), PERSON, Standing(is_owner=True)).admitted is True
    invited = evaluate(Rules(), PERSON, Standing(invited=True))
    assert invited.admitted is True and invited.invited_only is True


def test_a_rule_that_matches_someone_else_lets_nobody_else_in():
    rules = rules_of(rule("group", ["/Staff"], admit=True))
    assert evaluate(rules, PERSON).admitted is False


def test_everyone_else_open_lets_in_with_no_plan_and_no_admin():
    outcome = evaluate(Rules(fallback="open"), PERSON)
    assert outcome.admitted is True and outcome.admin is False and outcome.plan is None
    assert outcome.invited_only is False


def test_a_rule_without_an_effect_or_without_values_does_nothing():
    rules = rules_of(rule("group", ["/Trusted"]), rule("group", [], admit=True))
    assert evaluate(rules, PERSON).admitted is False
    assert [r.complete for r in rules.rules] == [False, False]
    assert len(rules.incomplete()) == 2


# ---------------------------------------------------------------------------
# What a rule can do
# ---------------------------------------------------------------------------

def test_make_administrator_implies_let_in():
    document = parse_rules({"rules": [{"id": "a", "field": "group", "values": ["/Trusted"], "admin": True}]})
    assert document.rules[0].admit is True
    outcome = evaluate(document, PERSON)
    assert outcome.admitted is True and outcome.admin is True


def test_the_owner_is_never_made_an_administrator_by_a_rule():
    rules = rules_of(rule("group", ["/Trusted"], admin=True))
    outcome = evaluate(rules, PERSON, Standing(is_owner=True))
    assert outcome.admitted is True and outcome.admin is False


def test_a_let_in_rule_alone_does_not_make_an_administrator():
    outcome = evaluate(rules_of(rule("domain", ["example.test"], admit=True)), PERSON)
    assert outcome.admitted is True and outcome.admin is False


def test_a_plan_rule_alone_does_not_let_anyone_in():
    outcome = evaluate(rules_of(rule("group", ["/Trusted"], plan="plan_a")), PERSON)
    assert outcome.admitted is False and outcome.plan is None


# ---------------------------------------------------------------------------
# Plans: first matching plan rule wins; hand-set and invitation plans always win
# ---------------------------------------------------------------------------

def test_the_first_matching_plan_rule_wins_and_order_is_the_priority():
    first = rule("group", ["/Family"], admit=True, plan="plan_family", id="a")
    second = rule("group", ["/Trusted"], plan="plan_all", id="b")
    plans = {"plan_family", "plan_all"}
    assert evaluate(rules_of(first, second), PERSON, valid_plans=plans).plan == "plan_family"
    assert evaluate(rules_of(second, first), PERSON, valid_plans=plans).plan == "plan_all"


def test_a_rule_that_does_not_match_never_gives_its_plan():
    rules = rules_of(rule("group", ["/Staff"], admit=True, plan="plan_a"),
                     rule("group", ["/Family"], admit=True, plan="plan_b"))
    assert evaluate(rules, PERSON, valid_plans={"plan_a", "plan_b"}).plan == "plan_b"


def test_a_plan_set_by_hand_or_by_an_invitation_beats_every_rule():
    rules = rules_of(rule("group", ["/Trusted"], admit=True, plan="plan_rule"))
    outcome = evaluate(rules, PERSON, Standing(manual_plan="plan_hand"), valid_plans={"plan_rule"})
    assert outcome.plan == "plan_hand" and outcome.plan_from == "manual"


def test_a_rule_that_names_a_deleted_plan_gives_nothing_but_still_lets_in():
    rules = rules_of(rule("group", ["/Trusted"], admit=True, plan="plan_gone"))
    outcome = evaluate(rules, PERSON, valid_plans={"plan_other"})
    assert outcome.admitted is True and outcome.plan is None


def test_the_plan_of_a_person_the_rules_keep_out_is_none():
    rules = rules_of(rule("group", ["/Staff"], admit=True, plan="plan_a"))
    assert evaluate(rules, PERSON, valid_plans={"plan_a"}).plan is None


# ---------------------------------------------------------------------------
# What a rule can match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,values,expected", [
    ("group", ["/Trusted"], True),
    ("group", ["Trusted"], True),            # a short name means the root group, as the provider writes it
    ("group", ["/trusted"], False),          # exact: no case folding
    ("group", ["/Trusted/sub"], False),
    ("group", ["/Nope", "/Family"], True),   # several values mean "is one of"
    ("role", ["power"], True),
    ("role", ["Power"], False),
    ("email", ["simon@example.test"], True),
    ("email", ["other@example.test"], False),
    ("domain", ["example.test"], True),
    ("domain", ["EXAMPLE.TEST"], True),
    ("domain", ["test"], False),
    ("everyone", [], True),
])
def test_what_each_field_matches(field, values, expected):
    outcome = evaluate(rules_of(rule(field, values, admit=True)), PERSON)
    assert outcome.admitted is expected


def test_a_person_with_no_email_matches_no_email_or_domain_rule():
    rules = rules_of(rule("email", ["a@b.test"], admit=True), rule("domain", ["b.test"], admit=True))
    assert evaluate(rules, Claims()).admitted is False


def test_matching_rule_ids_come_back_in_list_order():
    rules = rules_of(rule("group", ["/Family"], admit=True, id="b"), rule("domain", ["example.test"], admit=True, id="a"))
    assert evaluate(rules, PERSON).rule_ids == ("b", "a")


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def test_documents_round_trip_and_keep_order():
    document = {"fallback": "open", "rules": [rule("domain", ["a.test"], admit=True, id="x"),
                                              rule("group", ["/G"], admin=True, plan="p", id="y")]}
    parsed = parse_rules(document)
    again = parse_rules(parsed.as_dict())
    assert [r.id for r in again.rules] == ["x", "y"] and again.fallback == "open"
    assert again.as_dict()["rules"][1] == {"id": "y", "field": "group", "values": ["/G"], "admit": True,
                                           "admin": True, "plan": "p"}


@pytest.mark.parametrize("bad", [
    {"fallback": "maybe"}, {"rules": "x"}, {"rules": [{"field": "nope"}]},
    {"rules": [{"field": "group", "values": "x"}]}, {"rules": [{"field": "group", "values": [1]}]},
    "not an object",
])
def test_a_malformed_document_is_refused(bad):
    with pytest.raises(signin_rules.RulesError):
        parse_rules(bad)


def test_values_are_trimmed_deduplicated_and_emails_and_domains_lowercased():
    parsed = parse_rules({"rules": [rule("email", [" A@B.test", "a@b.test"], admit=True)]})
    assert parsed.rules[0].values == ("a@b.test",)


def test_an_unknown_plan_is_refused_when_the_caller_knows_the_plans():
    with pytest.raises(signin_rules.RulesError) as caught:
        parse_rules({"rules": [rule("group", ["/G"], admit=True, plan="gone")]}, plan_exists=lambda plan: False)
    assert caught.value.code == "plan_not_found"


def test_duplicate_or_bad_rule_ids_are_replaced():
    parsed = parse_rules({"rules": [rule("domain", ["a.test"], admit=True, id="same"),
                                    rule("domain", ["b.test"], admit=True, id="same"),
                                    rule("domain", ["c.test"], admit=True, id="bad id!")]})
    assert len({r.id for r in parsed.rules}) == 3


# ---------------------------------------------------------------------------
# Schema 9 migration
# ---------------------------------------------------------------------------

OWNER_EMAIL = "owner@example.test"
ISSUER = "https://id.example/realms/home"


def legacy_database(tmp_path, *, oidc=None, grants=True, version=8):
    """A database at schema 8, shaped like the live instance before the upgrade."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    owner = admin_store.upsert_oidc_user(ISSUER, "owner-sub", email=OWNER_EMAIL, display_name="Owner", path=path)
    simon = admin_store.upsert_oidc_user(ISSUER, "simon-sub", email="simon@example.test", display_name="Simon", path=path)
    if grants:
        admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"],
                                      granted_by="oidc", path=path)
        # The old group rule recorded its grants like this: by hand, not by claim.
        admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=simon["id"],
                                      granted_by="oidc-trusted-group", provenance="manual", path=path)
    legacy = {"enabled": True, "issuer": ISSUER, "client_id": "azizam", "admin_emails": [OWNER_EMAIL],
              "admin_groups": ["/Trusted"], "admin_roles": [], "allowed_email_domains": [],
              "groups_claim": "groups", "scopes": ["openid", "email", "profile"]} if oidc is None else oidc
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM operator_settings WHERE key IN ('signin_rules', 'signin_status')")
        conn.execute("DELETE FROM operator_settings_generations WHERE setting_key IN ('signin_rules', 'signin_status')")
        if legacy:
            conn.execute("INSERT OR REPLACE INTO operator_settings VALUES ('oidc_provider_config', ?, 100, 'fixture')",
                         (json.dumps(legacy),))
            conn.execute("INSERT OR REPLACE INTO operator_settings_generations VALUES ('oidc_provider_config', 3)")
        conn.execute("UPDATE schema_version SET version = ?", (version,))
    return path, owner, simon


def setting(path, key):
    record = admin_store.get_operator_setting(key, path=path)
    return record["value"] if record else None


def test_the_migration_converts_the_live_shaped_lists_into_rules(tmp_path):
    path, owner, simon = legacy_database(tmp_path)

    admin_store.init_db(path)

    document = setting(path, "signin_rules")
    # /Trusted becomes "Make administrator when group is /Trusted"; the owner's own email is dropped.
    assert document == {"fallback": "closed", "rules": [
        {"id": "rule_1", "field": "group", "values": ["/Trusted"], "admit": True, "admin": True, "plan": None}]}
    assert setting(path, "signin_status") == {
        "notice": "removed_owner_rule",
        "database_lists": ["admin_emails", "admin_groups", "admin_roles", "allowed_email_domains"]}
    assert admin_store.schema_version(path) == admin_store.SCHEMA_VERSION == 9


def test_the_migration_removes_the_old_lists_and_keeps_the_rest_of_the_provider_setting(tmp_path):
    path, _, _ = legacy_database(tmp_path)

    admin_store.init_db(path)

    provider = setting(path, "oidc_provider_config")
    assert {"admin_emails", "admin_groups", "admin_roles", "allowed_email_domains"}.isdisjoint(provider)
    assert provider["issuer"] == ISSUER and provider["client_id"] == "azizam" and provider["groups_claim"] == "groups"


def test_the_migration_relabels_the_legacy_group_grant_as_rule_derived_so_leaving_the_group_removes_it(tmp_path):
    path, owner, simon = legacy_database(tmp_path)

    admin_store.init_db(path)

    grants = admin_store.list_admin_roles(subject_kind="user", subject_id=simon["id"], path=path)
    assert [(g["role"], g["provenance"]) for g in grants] == [("admin", "oidc")]
    # It gets one session length to be confirmed by a sign-in, like every grant the provider gives.
    assert grants[0]["last_confirmed_at"] is not None
    assert "admin" in admin_store.effective_admin_roles("user", simon["id"], path=path)
    # The owner's own grant is untouched.
    owner_grants = [(g["role"], g["provenance"]) for g in admin_store.list_admin_roles(
        subject_kind="user", subject_id=owner["id"], path=path)]
    assert owner_grants == [("owner", "manual")]
    # A rule-derived grant is removed by the same reconcile that runs at every sign-in.
    admin_store.reconcile_oidc_admin_roles(simon["id"], set(), path=path)
    assert admin_store.list_admin_roles(subject_kind="user", subject_id=simon["id"], path=path) == []


def test_the_migration_only_relabels_grants_that_are_still_marked_as_the_old_group_rule_gave_them(tmp_path):
    path, owner, simon = legacy_database(tmp_path)
    manual = admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=owner["id"],
                                           granted_by="owner", provenance="manual", path=path)

    admin_store.init_db(path)

    kept = [g for g in admin_store.list_admin_roles(subject_kind="user", subject_id=owner["id"], path=path)
            if g["id"] == manual["id"]]
    assert kept[0]["provenance"] == "manual"


def test_the_migration_turns_domains_emails_and_roles_into_rules_and_keeps_other_peoples_emails(tmp_path):
    legacy = {"enabled": True, "issuer": ISSUER, "client_id": "azizam",
              "admin_emails": [OWNER_EMAIL, "Friend@Example.test"], "admin_groups": ["/Trusted", "/Staff"],
              "admin_roles": ["operator"], "allowed_email_domains": ["Family.test"]}
    path, _, _ = legacy_database(tmp_path, oidc=legacy)

    admin_store.init_db(path)

    rules = setting(path, "signin_rules")["rules"]
    assert [(r["field"], r["values"], r["admin"]) for r in rules] == [
        ("group", ["/Trusted", "/Staff"], True), ("role", ["operator"], True),
        ("domain", ["family.test"], False), ("email", ["friend@example.test"], False)]
    assert all(r["admit"] for r in rules)
    assert setting(path, "signin_rules")["fallback"] == "closed"


def test_an_instance_that_let_everyone_in_keeps_letting_everyone_in(tmp_path):
    legacy = {"enabled": True, "issuer": ISSUER, "client_id": "azizam", "admin_emails": [],
              "allowed_email_domains": [], "admin_groups": ["/Trusted"]}
    path, _, _ = legacy_database(tmp_path, oidc=legacy)

    admin_store.init_db(path)

    # Both lists were empty and the provider was on, so the old code admitted anyone.
    assert setting(path, "signin_rules")["fallback"] == "open"
    # The owner is told once that this is still so, because it is the opposite of the new default.
    assert setting(path, "signin_status") == {
        "notice": "open_door", "database_lists": ["admin_emails", "admin_groups", "allowed_email_domains"]}


def test_an_instance_that_closed_the_door_gets_no_open_door_notice(tmp_path):
    legacy = {"enabled": True, "issuer": ISSUER, "client_id": "azizam", "admin_emails": [],
              "allowed_email_domains": ["family.test"], "admin_groups": []}
    path, _, _ = legacy_database(tmp_path, oidc=legacy)

    admin_store.init_db(path)

    assert setting(path, "signin_rules")["fallback"] == "closed"
    assert setting(path, "signin_status")["notice"] is None


def test_an_instance_with_the_provider_off_starts_closed(tmp_path):
    legacy = {"enabled": False, "issuer": ISSUER, "admin_emails": [], "allowed_email_domains": []}
    path, _, _ = legacy_database(tmp_path, oidc=legacy)

    admin_store.init_db(path)

    assert setting(path, "signin_rules")["fallback"] == "closed"


def test_a_database_with_no_provider_setting_gets_an_empty_closed_rule_set(tmp_path):
    path, _, _ = legacy_database(tmp_path, oidc={})

    admin_store.init_db(path)

    assert setting(path, "signin_rules") == {"fallback": "closed", "rules": []}


def test_the_migration_is_idempotent_and_never_overwrites_rules_the_owner_saved(tmp_path):
    path, _, simon = legacy_database(tmp_path)
    admin_store.init_db(path)
    admin_store.put_operator_setting("signin_rules", {"fallback": "open", "rules": []}, path=path)
    grants_before = admin_store.list_admin_roles(include_revoked=True, path=path)
    audit_before = len(admin_store.list_audit(limit=500, path=path))

    admin_store.init_db(path)   # a second start is not a second migration
    # Forcing the version back runs the migration again: it must change nothing.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 8")
    admin_store.init_db(path)

    assert setting(path, "signin_rules") == {"fallback": "open", "rules": []}
    assert admin_store.list_admin_roles(include_revoked=True, path=path) == grants_before
    # The second run found nothing to do, so it recorded nothing but its own summary.
    assert len(admin_store.list_audit(limit=500, path=path)) <= audit_before + 1
    assert admin_store.schema_version(path) == 9


def test_a_fresh_database_starts_at_schema_9_with_no_rules_and_nothing_migrated(tmp_path):
    path = tmp_path / "fresh.db"

    admin_store.init_db(path)

    assert admin_store.schema_version(path) == 9
    assert setting(path, "signin_rules") is None      # an absent document means: deny by default
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signin_rule_plans", "oidc_identity_links", "signin_seen_values"} <= tables
    from gateway import signin_admin
    assert signin_admin.load_rules(path).rules == () and signin_admin.load_rules(path).fallback == "closed"


def test_a_schema_7_database_reaches_9_and_both_migrations_run_once(tmp_path):
    path, _, simon = legacy_database(tmp_path, version=7)

    admin_store.init_db(path)

    assert admin_store.schema_version(path) == 9
    assert setting(path, "signin_rules")["rules"][0]["values"] == ["/Trusted"]
    assert [g["provenance"] for g in admin_store.list_admin_roles(
        subject_kind="user", subject_id=simon["id"], path=path)] == ["oidc"]


def test_a_database_newer_than_the_code_is_refused(tmp_path):
    path = tmp_path / "newer.db"
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 10")
    with pytest.raises(sqlite3.DatabaseError):
        admin_store.init_db(path)


def test_the_migration_writes_an_audit_entry_that_names_no_email(tmp_path):
    path, _, _ = legacy_database(tmp_path)

    admin_store.init_db(path)

    records = admin_store.list_audit(limit=200, path=path)
    migrated = [e for e in records if e["action"] == "operator.signin_rules.migrate"]
    assert len(migrated) == 1
    text = json.dumps(migrated[0])
    assert OWNER_EMAIL not in text and "simon@example.test" not in text
    assert any(e["action"] == "admin_role.relabel" for e in records)


def test_the_first_owner_email_stays_in_the_provider_setting_while_no_owner_has_ever_existed(tmp_path):
    legacy = {"enabled": True, "issuer": ISSUER, "client_id": "azizam", "admin_emails": ["first@example.test"],
              "admin_groups": ["/Trusted"], "allowed_email_domains": ["family.test"]}
    path, _, _ = legacy_database(tmp_path, oidc=legacy, grants=False)

    admin_store.init_db(path)

    provider = setting(path, "oidc_provider_config")
    assert provider["admin_emails"] == ["first@example.test"]
    assert {"admin_groups", "allowed_email_domains", "admin_roles"}.isdisjoint(provider)


# ---------------------------------------------------------------------------
# Lists a deployment kept in its config file
# ---------------------------------------------------------------------------

def adopt(path, bootstrap, env=None):
    from gateway import signin_rules_migration
    return signin_rules_migration.adopt_bootstrap_lists(path, bootstrap, env or {})


def fresh_with_migration(tmp_path, oidc=None):
    path, _, _ = legacy_database(tmp_path, oidc={} if oidc is None else oidc, grants=False)
    admin_store.init_db(path)
    return path


def test_lists_in_the_config_file_become_rules_once_on_the_first_start(tmp_path):
    path = fresh_with_migration(tmp_path)
    bootstrap = {"enabled": True, "allowed_email_domains": ["family.test"], "admin_groups": ["/Trusted"],
                 "admin_roles_env": "ROLES"}

    assert adopt(path, bootstrap, {"ROLES": "operator, ops"}) is True

    rules = setting(path, "signin_rules")
    assert [(r["field"], r["values"], r["admin"]) for r in rules["rules"]] == [
        ("group", ["/Trusted"], True), ("role", ["operator", "ops"], True), ("domain", ["family.test"], False)]
    assert rules["fallback"] == "closed"
    assert setting(path, "signin_status")["bootstrap_lists_adopted"] is True
    # The second start leaves the owner's edits alone.
    admin_store.put_operator_setting("signin_rules", {"fallback": "closed", "rules": []}, path=path)
    assert adopt(path, bootstrap) is False
    assert setting(path, "signin_rules")["rules"] == []


def test_a_config_file_that_listed_nothing_and_enabled_the_provider_keeps_the_door_open_for_an_upgrade(tmp_path):
    path = fresh_with_migration(tmp_path)

    assert adopt(path, {"enabled": True}) is True

    assert setting(path, "signin_rules") == {"fallback": "open", "rules": []}
    assert setting(path, "signin_status")["notice"] == "open_door"


def test_a_new_database_stays_deny_by_default_whatever_the_config_file_enables(tmp_path):
    """No sign-in data came before, so there is no old open door to keep."""
    path = tmp_path / "new.db"
    admin_store.init_db(path)

    changed = adopt(path, {"enabled": True})

    assert changed is False
    assert signin_admin.load_rules(path).fallback == "closed"
    assert (setting(path, "signin_rules") or {"fallback": "closed"})["fallback"] == "closed"
    assert setting(path, "signin_status").get("notice") is None


def test_a_new_database_still_adopts_the_lists_in_the_config_file_as_rules_and_stays_closed(tmp_path):
    path = tmp_path / "new.db"
    admin_store.init_db(path)

    assert adopt(path, {"enabled": True, "allowed_email_domains": ["family.test"]}) is True

    rules = setting(path, "signin_rules")
    assert rules["fallback"] == "closed"
    assert [(r["field"], r["values"]) for r in rules["rules"]] == [("domain", ["family.test"])]


def test_what_the_database_already_answered_is_not_overridden_by_the_config_file(tmp_path):
    legacy = {"enabled": True, "issuer": ISSUER, "admin_emails": ["someone@example.test"], "admin_groups": []}
    path = fresh_with_migration(tmp_path, oidc=legacy)
    before = setting(path, "signin_rules")

    changed = adopt(path, {"enabled": True, "admin_groups": ["/FromFile"], "allowed_email_domains": ["file.test"]})

    # The database set admin_groups and admin_emails; only the list it never set can come from the file.
    after = setting(path, "signin_rules")
    assert changed is True and before["fallback"] == after["fallback"] == "closed"
    assert [(r["field"], r["values"]) for r in after["rules"] if r not in before["rules"]] == [
        ("domain", ["file.test"])]


def test_nothing_changes_for_a_config_file_with_no_lists_and_no_provider(tmp_path):
    path = fresh_with_migration(tmp_path)
    before = setting(path, "signin_rules")

    assert adopt(path, {"cookie": {"secure": False}}) is False

    assert setting(path, "signin_rules") == before
    assert setting(path, "signin_status")["bootstrap_lists_adopted"] is True
