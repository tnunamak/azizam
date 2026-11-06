"""The one-time move from profiles to access plans (design v4 §4, §8, §5.6).

The fixture is a synthetic schema-6 admin.db with the shapes of the owner's
instance (the nine old plans, people, service apps, imported keys, test apps,
two disabled mappings, an invitation, provider ``allowed_plans``). Key hashes
are fake. ``tests/fixtures/admin_db_schema_v6.sql`` is the real schema 6.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402
from gateway.access_plans import CAPABILITIES, plan_from_document, plan_summary  # noqa: E402
from gateway.admin_runtime import person_for_app_credential, runtime_identity_for_app_credential_hash  # noqa: E402
from gateway.policy import PolicyRequest, evaluate  # noqa: E402

SCHEMA_V6 = (ROOT / "tests" / "fixtures" / "admin_db_schema_v6.sql").read_text(encoding="utf-8")
T = 1_790_000_000

PROFILES = {
    "prof_librechat_full": ("LibreChat family full runtime", {"fallback": {"cloud_allowed": True}, "id": "librechat-full-runtime", "native_passthrough": {"audit": True, "enabled": True}, "status_visibility": {"level": "full"}}),
    "prof_helper": ("Legacy helper", {"fallback": {"cloud_allowed": False}, "id": "helper", "native_passthrough": {"enabled": False}, "protocols": ["openai", "anthropic", "ollama"], "status_visibility": {"level": "standard"}}),
    "prof_daisy": ("Legacy daisy", {"fallback": {"cloud_allowed": False}, "id": "daisy", "native_passthrough": {"enabled": True}, "protocols": ["openai", "anthropic", "ollama", "gateway", "provider-native"], "status_visibility": {"level": "standard"}}),
    "prof_librechat": ("Legacy librechat", {"fallback": {"cloud_allowed": False}, "id": "librechat", "native_passthrough": {"enabled": False}, "protocols": ["anthropic", "openai"], "status_visibility": {"level": "basic"}}),
    "prof_cap": ("Legacy cap", {"fallback": {"cloud_allowed": False}, "id": "cap", "native_passthrough": {"enabled": False}, "protocols": ["openai.audio"], "status_visibility": {"level": "basic"}}),
    "prof_openwebui": ("Legacy open-webui", {"fallback": {"cloud_allowed": False}, "id": "open-webui", "native_passthrough": {"enabled": False}, "protocols": ["ollama", "openai"], "status_visibility": {"level": "basic"}}),
    "prof_owner": ("Owner (local)", {"fallback": {"cloud_allowed": False}, "id": "owner-local", "remote": {"providers": ["openrouter", "typesafe"]}, "status_visibility": {"level": "full"}}),
    "prof_assistant": ("Assistant (chat + RAG)", {"allow": {"intents": ["chat", "text.completions", "text.embeddings", "models.list"]}, "fallback": {"cloud_allowed": False}, "id": "assistant-chat", "protocols": ["openai", "anthropic", "ollama"], "status_visibility": {"level": "basic"}}),
    "prof_media": ("Media creator", {"allow": {"intents": ["chat", "image.generate"]}, "id": "media-creator", "status_visibility": {"level": "basic"}}),
}
OWNER, HELPER, PARTNER = "usr_owner", "usr_helper", "usr_partner"
LEGACY_IMPORT, GATEWAY_ADMIN, LIBRECHAT_SVC = "svc_import", "svc_gwadmin", "svc_librechat"
# app id: (kind, name, owner user, owner service account, app plan, disabled)
APPS = {
    "app_owner_personal": ("personal", "owner personal access", OWNER, None, None, False),
    "app_librechat": ("service", "librechat.family.example", None, LIBRECHAT_SVC, "prof_librechat_full", False),
    "app_helper_personal": ("personal", "helper personal access", HELPER, None, None, False),
    "app_daisy": ("service", "daisy", None, LEGACY_IMPORT, "prof_daisy", False),
    "app_cap": ("service", "cap", None, LEGACY_IMPORT, "prof_cap", False),
    "app_openwebui": ("service", "open-webui", None, LEGACY_IMPORT, "prof_openwebui", False),
    "app_garden": ("personal", "Garden App", OWNER, None, "prof_owner", False),
    "app_paperless": ("service", "paperless", None, GATEWAY_ADMIN, "prof_assistant", False),
    "app_openwhispr": ("personal", "OpenWhispr", OWNER, None, None, False),
    "app_probe": ("service", "waspflow-dsh-probe", None, GATEWAY_ADMIN, None, False),
    "app_owner_try": ("personal", "Try", OWNER, None, None, False),
    "app_paid_proof": ("personal", "Paid provider proof", OWNER, None, "prof_owner", False),
    "app_empathy": ("service", "Temporary empathy Jev triage", None, GATEWAY_ADMIN, "prof_owner", True),
    "app_pi": ("service", "t3-pi-agent", None, GATEWAY_ADMIN, "prof_owner", False),
    "app_partner_first": ("personal", "My first app", PARTNER, None, "prof_assistant", False),
    "app_helper_try": ("personal", "Try", HELPER, None, None, False),
    "app_sillytavern": ("personal", "SillyTavern", OWNER, None, None, False),
}
# key id: (app, key plan, display name, prefix, revoked, last used, created_by)
KEYS = {
    "cred_owner_testing": ("app_owner_personal", None, "Testing", "opk_testing", False, None, OWNER),
    "cred_librechat_full": ("app_librechat", "prof_librechat_full", "LibreChat full-runtime key", "opk_lc", False, T, None),
    "cred_helper_legacy": ("app_helper_personal", "prof_helper", "Imported legacy helper key", "legacy-helper", False, None, None),
    "cred_daisy_legacy": ("app_daisy", "prof_daisy", "Imported legacy daisy key", "legacy-daisy", False, T, None),
    "cred_librechat_legacy": ("app_librechat", "prof_librechat", "Imported legacy librechat key", "legacy-librechat", False, None, None),
    "cred_cap_legacy": ("app_cap", "prof_cap", "Imported legacy cap key", "legacy-cap", False, None, None),
    "cred_openwebui_legacy": ("app_openwebui", "prof_openwebui", "Imported legacy open-webui key", "legacy-open-webui", False, T, None),
    "cred_garden": ("app_garden", None, "App", "opk_garden", False, None, OWNER),
    "cred_paperless": ("app_paperless", "prof_assistant", "PAPERLESS_ACCESS_KEY", "opk_pl", False, T, None),
    "cred_openwhispr": ("app_openwhispr", None, "Default key", "opk_ow", False, T, OWNER),
    "cred_probe": ("app_probe", None, "dsh-probe", "opk_probe", False, T, None),
    "cred_paid_proof_denied": ("app_paid_proof", "prof_helper", "Paid proof denied plan key", "opk_ppd", True, T, None),
    "cred_paid_proof": ("app_paid_proof", None, "Default key", "opk_pp", False, T, OWNER),
    "cred_pi": ("app_pi", None, "t3-pi-agent", "opk_pi", False, T, None),
    "cred_partner_first": ("app_partner_first", "prof_assistant", "First app key", "opk_partner", False, None, PARTNER),
    "cred_sillytavern": ("app_sillytavern", None, "SillyTavern", "opk_st", False, T, OWNER),
}


def key_hash(key_id: str) -> str:
    return "sha256:" + hashlib.sha256(key_id.encode()).hexdigest()


def build_v6(path: Path, *, extra_sql: str = "") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V6)
    rows = conn.execute
    rows("INSERT INTO schema_version(version) VALUES (6)")
    for uid, name, email in ((OWNER, "Avery Lindqvist", "owner@example.test"), (HELPER, "Helper Agent", "helper@example.test"),
                             (PARTNER, "Jo Work", "partner@example.test")):
        rows("INSERT INTO users VALUES (?, 'https://sso.example.test', ?, ?, ?, ?, ?)", (uid, uid, email, name, T, T))
    rows("INSERT INTO account_access(user_id, profile_id) VALUES (?, 'prof_assistant')", (PARTNER,))
    rows("INSERT INTO admin_role_assignments(role, subject_kind, subject_id, granted_at, provenance) VALUES ('owner','user',?,?,'manual')", (OWNER, T))
    rows("INSERT INTO admin_role_assignments(role, subject_kind, subject_id, granted_at, provenance) VALUES ('admin','user',?,?,'manual')", (HELPER, T))
    for sid, name in ((LEGACY_IMPORT, "legacy-key-import"), (GATEWAY_ADMIN, "gateway-admin"),
                      (LIBRECHAT_SVC, "librechat.family.example"), ("svc_owner_cli", "owner-cli")):
        rows("INSERT INTO service_accounts VALUES (?, ?, NULL, ?, NULL)", (sid, name, T))
    rows("INSERT INTO credentials(id, subject_kind, subject_id, hash, prefix, display_name, created_at) "
         "VALUES ('cred_admin', 'service_account', 'svc_owner_cli', 'sha256:admin', 'oak_x', 'owner-cli admin key', ?)", (T,))
    for pid, (name, doc) in PROFILES.items():
        rows("INSERT INTO profiles VALUES (?, ?, NULL, ?, ?)", (pid, name, json.dumps(doc), T))
    for aid, (kind, name, owner, svc, prof, disabled) in APPS.items():
        rows("INSERT INTO apps VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (aid, kind, name, owner, svc, prof, T, T if disabled else None))
    for kid, (app, prof, label, prefix, revoked, used, by) in KEYS.items():
        rows("INSERT INTO credentials(id, subject_kind, subject_id, app_id, profile_id, hash, prefix, display_name, "
             "created_at, revoked_at, last_used_at, created_by) VALUES (?, 'app', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (kid, app, app, prof, key_hash(kid), prefix, label, T, T if revoked else None, used, by))
    for mid, kind, value in (("map_1", "gateway_role", "admin"), ("map_2", "email", "test@example.com")):
        rows("INSERT INTO runtime_profile_mappings VALUES (?, ?, ?, 'prof_librechat_full', 50, ?, NULL, ?)", (mid, kind, value, T, T))
    rows("INSERT INTO account_links VALUES ('link_1', 'invite', 'tokhash', 'prof_assistant', ?, NULL, ?, ?, ?, ?, NULL)",
         (PARTNER, OWNER, T, T + 99, T + 1))
    provider_admin = {"global_caps": {"daily_usd": 2.0}, "providers": {
        "openrouter": {"allowed_plans": ["owner-local"], "caps": {"daily_usd": 0.2}},
        "typesafe": {"allowed_plans": ["owner-local"], "caps": {"daily_usd": 1.0}}}}
    rows("INSERT INTO operator_settings VALUES ('provider_admin', ?, ?, NULL)", (json.dumps(provider_admin), T))
    rows("INSERT INTO operator_settings VALUES ('user_access_defaults', ?, ?, NULL)",
         (json.dumps({"self_service_apps": True, "auto_create_personal_app": True}), T))
    if extra_sql:
        conn.executescript(extra_sql)
    conn.commit()
    conn.close()


@pytest.fixture
def migrated(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path)
    before = path.read_bytes()
    admin_store.init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn, before
    conn.close()


def _plans(conn):
    return {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM access_plans")}


def _app(conn, app_id):
    return conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()


def _assignment_name(conn, app_id):
    row = _app(conn, app_id)
    plans = _plans(conn)
    if row["access_plan"] == "person":
        person_plan = conn.execute("SELECT plan_id FROM account_access WHERE user_id=?", (row["owner_user_id"],)).fetchone()[0]
        return "person:" + plans[person_plan]
    return plans[row["access_plan"]]


def test_schema_7_has_no_legacy_tables_columns_or_settings(migrated):
    _, conn, _ = migrated
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == admin_store.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"profiles", "runtime_profile_mappings"}.isdisjoint(tables)
    def columns(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    assert "profile_id" not in columns("credentials")
    assert "session_timing_json" in columns("credentials")
    assert {"default_profile_id", "owner_service_account_id"}.isdisjoint(columns("apps"))
    assert "profile_id" not in columns("account_access") | columns("account_links")
    assert conn.execute("SELECT 1 FROM operator_settings WHERE key='user_access_defaults'").fetchone() is None
    provider_admin = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='provider_admin'").fetchone()[0])
    assert all("allowed_plans" not in p for p in provider_admin["providers"].values())
    assert provider_admin["global_caps"] == {"daily_usd": 2.0}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    names = set(_plans(conn).values())
    assert not any(name.startswith("Legacy") for name in names)


def test_plans_people_and_apps_keep_todays_access(migrated):
    _, conn, _ = migrated
    assert set(_plans(conn).values()) == {
        "All capabilities", "Local and paid", "Local only", "Local with direct engine access",
        "Local with direct engine access (standard receipts)", "Assistant (chat + RAG)",
    }
    people = {row[0]: _plans(conn)[row[1]] for row in conn.execute("SELECT user_id, plan_id FROM account_access")}
    assert people == {OWNER: "All capabilities", HELPER: "Local only", PARTNER: "Assistant (chat + RAG)"}
    expected = {
        "app_owner_personal": "Local only", "app_openwhispr": "Local only", "app_sillytavern": "Local only",
        "app_owner_try": "Local only", "app_librechat": "Local with direct engine access",
        "app_daisy": "Local with direct engine access (standard receipts)", "app_openwebui": "Local only",
        "app_garden": "Local only", "app_pi": "Local and paid", "app_paperless": "Assistant (chat + RAG)",
        "app_helper_personal": "person:Local only", "app_helper_try": "person:Local only",
        "app_partner_first": "person:Assistant (chat + RAG)",
    }
    assert {app: _assignment_name(conn, app) for app in expected} == expected
    assert conn.execute("SELECT COUNT(*) FROM apps WHERE access_plan IS NULL").fetchone()[0] == 0


def test_all_capabilities_plan_keeps_its_grants_under_its_truthful_name(migrated):
    _, conn, _ = migrated
    owner_plan_id = conn.execute("SELECT plan_id FROM account_access WHERE user_id=?", (OWNER,)).fetchone()[0]
    name, plan_json = conn.execute("SELECT name, plan_json FROM access_plans WHERE id=?", (owner_plan_id,)).fetchone()
    assert name == "All capabilities"
    plan = plan_from_document(json.loads(plan_json), plan_id=owner_plan_id)
    assert all(plan.can_use[cap].all_items for cap in CAPABILITIES)
    assert plan.direct_engine_access.granted and plan.direct_engine_access.all_engines
    assert plan.paid_providers == frozenset({"openrouter", "typesafe"})
    # The fixture defines no categories, so the migrated list is empty and the summary names none.
    assert plan_summary(plan, {}) == "All types of work. Paid models: openrouter, typesafe. Direct engine access. No categories."


def test_an_app_whose_keys_were_never_used_keeps_no_paid_access(migrated):
    path, conn, _ = migrated
    event = json.loads(conn.execute(
        "SELECT metadata_json FROM audit_events WHERE action='access_plans.migrate'").fetchone()[0])
    assert [a["name"] for a in event["apps_narrowed"]] == ["Garden App"]
    paid = PolicyRequest(intent="decision.systemone", model="typesafe/x", remote_provider="typesafe")
    garden = runtime_identity_for_app_credential_hash(key_hash("cred_garden"), path=path)
    assert evaluate(garden.access, paid).denied
    # A used key keeps its paid access, and so does the person's plan.
    pi = runtime_identity_for_app_credential_hash(key_hash("cred_pi"), path=path)
    assert evaluate(pi.access, paid).allowed
    assert "openrouter" in garden.access.person_plan.paid_providers


def test_service_apps_get_the_owner_as_accountable_person_and_stay_service(migrated):
    path, conn, _ = migrated
    for app_id in ("app_daisy", "app_paperless", "app_pi", "app_librechat", "app_probe"):
        row = _app(conn, app_id)
        assert row["owner_user_id"] == OWNER and row["kind"] == "service"
    daisy = dict(_app(conn, "app_daisy"))
    assert person_for_app_credential(daisy, {"created_by": None}) is None
    accounts = {r[0] for r in conn.execute("SELECT name FROM service_accounts")}
    assert accounts == {"owner-cli"}  # the ones that only owned apps are gone


def test_test_artifacts_are_disabled_and_listed_not_deleted(migrated):
    _, conn, _ = migrated
    disabled = {r[0] for r in conn.execute("SELECT id FROM apps WHERE disabled_at IS NOT NULL")}
    assert disabled == {"app_paid_proof", "app_probe", "app_cap", "app_empathy"}
    revoked_now = {r[0] for r in conn.execute("SELECT id FROM credentials WHERE revoked_at IS NOT NULL")}
    assert revoked_now == {"cred_paid_proof_denied", "cred_owner_testing", "cred_probe", "cred_helper_legacy",
                           "cred_librechat_legacy", "cred_cap_legacy"}
    event = json.loads(conn.execute(
        "SELECT metadata_json FROM audit_events WHERE action='access_plans.migrate'").fetchone()[0])
    assert {a["name"] for a in event["disabled_apps"]} == {"Paid provider proof", "waspflow-dsh-probe", "cap"}
    assert {k["id"] for k in event["revoked_keys"]} == revoked_now - {"cred_paid_proof_denied"}
    assert conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == len(APPS)


def test_used_imported_keys_keep_working_and_lose_the_legacy_name(migrated):
    path, conn, _ = migrated
    names = {r[0]: r[1] for r in conn.execute("SELECT id, display_name FROM credentials")}
    assert names["cred_daisy_legacy"] == "daisy key" and names["cred_openwebui_legacy"] == "open-webui key"
    daisy = runtime_identity_for_app_credential_hash(key_hash("cred_daisy_legacy"), path=path)
    assert daisy is not None and daisy.denial is None and daisy.accountable_person_id == OWNER
    assert daisy.access.receipt_detail == "standard"
    native = PolicyRequest(intent="provider.native", native_passthrough=True, native_backend="llama",
                           native_operation="POST /v1/completions")
    assert evaluate(daisy.access, native).allowed


def test_paid_providers_follow_the_old_allowed_plans(migrated):
    path, _, _ = migrated
    pi = runtime_identity_for_app_credential_hash(key_hash("cred_pi"), path=path)
    paid = PolicyRequest(intent="decision.systemone", model="typesafe/x", remote_provider="typesafe")
    assert evaluate(pi.access, paid).allowed
    whispr = runtime_identity_for_app_credential_hash(key_hash("cred_openwhispr"), path=path)
    assert evaluate(whispr.access, paid).denied


def test_assistant_plan_keeps_its_capabilities(migrated):
    path, conn, _ = migrated
    row = conn.execute("SELECT id, plan_json FROM access_plans WHERE name='Assistant (chat + RAG)'").fetchone()
    assistant = plan_from_document(json.loads(row["plan_json"]), plan_id=row["id"])
    assert set(assistant.can_use) == {"chat", "embed"}
    paperless = runtime_identity_for_app_credential_hash(key_hash("cred_paperless"), path=path)
    assert evaluate(paperless.access, PolicyRequest(intent="text.embeddings", model="nomic")).allowed
    assert evaluate(paperless.access, PolicyRequest(intent="image.generate", model="flux")).denied


def test_disabled_test_app_key_is_refused_and_can_be_turned_on_again(migrated):
    path, _, _ = migrated
    proof = runtime_identity_for_app_credential_hash(key_hash("cred_paid_proof"), path=path)
    assert proof is not None and proof.denial == "app_disabled"
    admin_store.enable_app("app_paid_proof", path=path)
    again = runtime_identity_for_app_credential_hash(key_hash("cred_paid_proof"), path=path)
    assert again.denial is None


def test_open_invitation_keeps_its_plan(migrated):
    _, conn, _ = migrated
    plan_id = conn.execute("SELECT plan_id FROM account_links WHERE id='link_1'").fetchone()[0]
    assert _plans(conn)[plan_id] == "Assistant (chat + RAG)"


def test_backup_is_the_database_before_the_change_and_rerun_is_a_no_op(migrated):
    path, conn, before = migrated
    backup = admin_store.access_plans_backup_path(path)
    assert backup.exists()
    old = sqlite3.connect(backup)
    assert old.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    assert old.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == len(PROFILES)
    old.close()
    plans = _plans(conn)
    admin_store.init_db(path)
    assert _plans(conn) == plans
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='access_plans.migrate'").fetchone()[0] == 1


def test_active_plan_mapping_refuses_and_changes_nothing(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path, extra_sql="UPDATE runtime_profile_mappings SET disabled_at = NULL WHERE id = 'map_1';")
    with pytest.raises(sqlite3.DatabaseError, match="active plan mappings"):
        admin_store.init_db(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == len(PROFILES)
    conn.close()


def test_keys_of_one_app_on_different_plans_refuse(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path, extra_sql=(
        "INSERT INTO credentials(id, subject_kind, subject_id, app_id, profile_id, hash, prefix, display_name, "
        "created_at, last_used_at) VALUES ('cred_pi_2', 'app', 'app_pi', 'app_pi', 'prof_assistant', 'sha256:pi2', "
        "'opk_pi2', 'second', 1, 1);"))
    with pytest.raises(sqlite3.DatabaseError, match="t3-pi-agent"):
        admin_store.init_db(path)


def test_unrepresentable_value_refuses_and_names_it(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path, extra_sql=(
        "UPDATE profiles SET profile_json = json_set(profile_json, '$.allow.exposure', json('[\"experimental\"]')) "
        "WHERE id = 'prof_owner';"))
    with pytest.raises(sqlite3.DatabaseError, match="Owner \\(local\\).*allow.exposure"):
        admin_store.init_db(path)


def test_old_plan_caps_become_plan_caps(tmp_path):
    path = tmp_path / "admin.db"
    build_v6(path, extra_sql=(
        "UPDATE profiles SET profile_json = json_set(profile_json, '$.remote.budget_usd_per_day', 5, "
        "'$.remote.max_usd_per_request', 0.25) WHERE id = 'prof_owner';"))
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        capped = [json.loads(row[0]) for row in conn.execute("SELECT plan_json FROM access_plans")]
    with_caps = [doc for doc in capped if any(doc["caps"].values())]
    assert with_caps and all(
        doc["caps"] == {"usd_per_request": "0.25", "usd_per_day": "5", "usd_per_month": None} for doc in with_caps)
    # A capped plan is its own plan, not folded into a same-named standard one.
    assert "Owner (local)" in {doc["name"] for doc in with_caps}
