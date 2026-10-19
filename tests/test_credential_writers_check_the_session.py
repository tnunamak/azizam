"""Nothing a browser sign-in creates is made after that sign-in ended (decision D41).

The review of round 3 signed out everywhere just before the insert and still got an active permanent
App key, an admin App key and (by the same hole) a lasting MCP grant. The fix is one helper
(gateway/session_guard.py) that every writer of such a credential calls inside its own transaction.

Three kinds of test keep it that way:

* discovery: every function in production code that writes a credential table is either a guarded writer
  or listed here with the reason no browser sign-in starts it. A new writer fails until someone decides;
* callers: every production call of a guarded writer names its ``session=``, and a literal ``None`` is
  allowed only where no browser can be asking;
* behaviour: each guarded writer refuses a revoked sign-in and writes nothing, and each route that makes
  a credential refuses one that was signed out between the cookie check and the insert.
"""
from __future__ import annotations

import ast
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from gateway import accounts, admin_store, mcp_oauth_store, secret_store, session_guard, signin_admin
from gateway.session_guard import BrowserSession, SessionEnded
from tests.support import call_graph
from tests.support.access import store_plan
from tests.test_session_lifecycle import (  # noqa: F401  (sg is a fixture, and needs gateway)
    COOKIE, HELPER, OWNER, PASSWORD, SIGNING_KEY, asks_again, cookie_session, csrf, forge, gateway, owner_client,
    person_id, put_cookie, registry, set_password, sg, signed_in_state, stale_owner, through_provider,
)

ACT = {"x-azizam-user-action": "1"}

# -- discovery --------------------------------------------------------------------------------------

# A string that creates a credential (or replaces a password), in any table that holds one.
CREATES = re.compile(
    r"(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO)\s+"
    r"(credentials|mcp_oauth_grants|mcp_oauth_codes|mcp_oauth_tokens|account_links|local_password_credentials"
    r"|provider_secrets|app_trusted_addresses|oidc_identity_links)\b"
    r"|UPDATE\s+local_password_credentials\b|DELETE\s+FROM\s+provider_secrets\b",
    re.IGNORECASE,
)

# Writers a browser sign-in can start. Each takes ``session`` and runs it through gateway/session_guard.py
# inside its own transaction.
GUARDED = {
    "gateway/admin_store.py::create_credential_hash": "App keys, Try keys, admin keys",
    "gateway/admin_store.py::set_local_password_verifier": "the owner's password",
    "gateway/accounts.py::create_link": "invitation and password-reset links",
    "gateway/mcp_oauth_store.py::Store.create_grant": "an MCP connection (consent)",
    # A registry save can move where a stored key is sent (the credential binding), so the writers of the
    # registry are credential writers too. They are found by their call of write_service_registry_in_transaction.
    "gateway/secret_store.py::put_provider_secret": "a provider key, an engine key, the secret entries of a record, the OIDC client secret",
    "gateway/secret_store.py::delete_provider_secret": "removing a stored secret",
    "gateway/admin_store.py::add_trusted_address": "a trusted address lets a device in with no key",
    "gateway/signin_admin.py::link_identity": "an identity-provider login bound to the owner's account",
    "gateway/admin_store.py::write_service_registry": "a registry save",
    "gateway/admin_store.py::write_service_registry_with_audit": "a registry save with its audit fact",
    "gateway/admin_store.py::write_service_registry_and_setting_with_audits": "a registry save with a setting and audit facts",
}

# Writers no browser sign-in starts, with why.
EXEMPT = {
    "gateway/accounts.py::accept_invite": "the holder of an invitation link; no sign-in exists yet, the new person's first key is made with the account",
    "gateway/accounts.py::reset_password": "the holder of a reset link; every sign-in of the person ends with it",
    "gateway/admin_store.py::claim_first_local_owner": "first-run setup before any sign-in exists",
    "gateway/mcp_oauth_store.py::Store.save_code": "follows create_grant in the same consent (grant checked there); a code only redeems a grant",
    "gateway/mcp_oauth_store.py::Store.save_token": "the client redeeming a code or refresh token of a grant; no browser is involved",
}

# Where production code passes ``session=None`` on purpose.
NO_BROWSER = {
    "gateway/admin_runtime.py::create_admin_service_account_key": "the operator's CLI",
    "gateway/service_registry.py::ensure_service_registry": "the gateway seeding or backfilling its own registry at start",
}

# The guarded writers by the name callers use, and the keyword that carries the sign-in.
REGISTRY_WRITERS = {"write_service_registry", "write_service_registry_with_audit",
                    "write_service_registry_and_setting_with_audits"}
SECRET_STORE_WRITERS = {"put_provider_secret", "delete_provider_secret"}
CALLED_AS = {"create_credential_hash", "set_local_password_verifier", "create_link", "create_grant"} | REGISTRY_WRITERS
# Writers whose plain name another function also has (the runtime's own ``add_trusted_address(actor=...)``), called
# by their dotted names.
CALLED_AS_DOTTED = {"admin_store.add_trusted_address", "signin_admin.link_identity"}
# Calls into the secret store by their dotted names. A secret written for a browser carries its sign-in; the few
# calls that do not are listed in NO_SESSION_SECRET_CALLS.
SECRET_CALLS = {"secret_store.put_provider_secret", "secret_store.delete_provider_secret",
                "secret_refs.store", "secret_refs.putter", "secret_refs.delete"}


def _dotted(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return getattr(func, "id", getattr(func, "attr", ""))


def _functions(tree: ast.AST):
    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{prefix}{child.name}", child
                yield from visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _own_nodes(function):
    """The function's nodes without those of the functions defined inside it."""
    for child in ast.iter_child_nodes(function):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _own_nodes(child)


EXECUTE = {"execute", "executemany", "executescript"}


def _module_strings(tree: ast.AST) -> dict[str, str]:
    """The module-level names bound to a plain string."""
    return {target.id: node.value.value
            for node in getattr(tree, "body", []) if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for target in node.targets if isinstance(target, ast.Name)}


def _assembled(node: ast.AST, strings: dict[str, str]) -> str:
    """The text of a SQL argument as far as its parts are written out; ``{}`` stands for a part that is not."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _assembled(node.left, strings) + _assembled(node.right, strings)
    if isinstance(node, ast.Name) and node.id in strings:
        return strings[node.id]
    return "{}"


def _is_literal_sql(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)) or (
        isinstance(node, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in node.values))


def _execute_calls(function):
    for node in _own_nodes(function):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in EXECUTE
                and node.args):
            yield node


def writers_in(source: str, rel: str) -> set[str]:
    """Functions that write a credential table: by a string that says so, by SQL put together from written-out
    parts, or (the registry) by a call of ``write_service_registry_in_transaction``."""
    tree = ast.parse(source)
    strings = _module_strings(tree)
    found = set()
    for qualname, function in _functions(tree):
        hit = any(isinstance(node, ast.Constant) and isinstance(node.value, str) and CREATES.search(node.value)
                  for node in _own_nodes(function))
        hit = hit or any(CREATES.search(_assembled(call.args[0], strings)) for call in _execute_calls(function))
        hit = hit or any(isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", ""))
                         == "write_service_registry_in_transaction" for node in _own_nodes(function))
        if hit and not qualname.endswith("write_service_registry_in_transaction"):
            found.add(f"{rel}::{qualname}")
    return found


def writers() -> set[str]:
    found = set()
    for file in call_graph.production_files():
        found |= writers_in(file.read_text(), str(file.relative_to(call_graph.ROOT)))
    return found


# SQL that is not one written-out string, in the modules that write credential tables, with why it cannot hide a
# credential write. Each function is listed, not each call; the strings in it are still searched (``writers_in``).
DYNAMIC_SQL = {
    "gateway/admin_store.py::_exec_schema": "executes the module's own schema and migration text",
    "gateway/admin_store.py::_drop_identity_profile_grants": "drops and counts retired tables by a name from a fixed tuple",
    "gateway/admin_store.py::get_users": "SELECT with a placeholder per id",
    "gateway/admin_store.py::find_user_by_email": "SELECT; the statement is chosen from two written-out ones",
    "gateway/admin_store.py::person_plan_ids": "SELECT with a placeholder per id",
    "gateway/admin_store.py::session_app_ids": "SELECT with a placeholder per id",
    "gateway/admin_store.py::update_app": "UPDATE apps, columns from a fixed set",
    "gateway/admin_store.py::list_trusted_addresses": "SELECT with an optional WHERE",
    "gateway/admin_store.py::runtime_access_row": "SELECT over the fixed runtime-access view",
    "gateway/admin_store.py::runtime_access_rows": "SELECT over the fixed runtime-access view",
    "gateway/admin_store.py::list_active_credentials": "SELECT with an optional WHERE",
    "gateway/admin_store.py::list_active_credentials_for_apps": "SELECT with a placeholder per app",
    "gateway/admin_store.py::list_admin_roles": "SELECT with an optional WHERE",
    "gateway/admin_store.py::list_audit": "SELECT with an optional WHERE",
    "gateway/admin_store.py::put_operator_setting_in": "SELECT of the fixed setting columns",
    "gateway/admin_store.py::get_operator_setting": "SELECT of the fixed setting columns",
    "gateway/admin_store.py::get_operator_setting_in": "SELECT of the fixed setting columns",
    "gateway/admin_store.py::list_operator_settings": "SELECT of the fixed setting columns",
    "gateway/admin_store.py::_record_prompt_erasure": "INSERT into one of two prompt-erasure tables named in a fixed tuple",
    "gateway/secret_store.py::put_provider_secret": "the INSERT is a local literal bound to a name just above the call, run on the caller's or its own connection",
    "gateway/signin_admin.py::_connection_rows": "a SELECT helper: callers pass written-out SELECT statements",
    "gateway/accounts.py::initialize": "executes the module's own schema text",
    "gateway/accounts.py::require_live_session_in": "SELECT from one of two fixed table names",
    "gateway/accounts.py::revoke_family_in": "DELETE and UPDATE that revoke; a placeholder per session id",
    "gateway/mcp_oauth_store.py::grants_in": "SELECT with a placeholder per person",
    "gateway/mcp_oauth_store.py::Store._conn": "executes the module's own schema text",
    "gateway/mcp_oauth_store.py::Store.revoke_grant": "UPDATE that revokes, with an optional person filter",
}


def dynamic_sql_in(source: str, rel: str) -> set[str]:
    tree = ast.parse(source)
    return {f"{rel}::{qualname}" for qualname, function in _functions(tree)
            for call in _execute_calls(function) if not _is_literal_sql(call.args[0])}


def credential_modules() -> list[str]:
    return sorted({key.split("::")[0] for key in set(GUARDED) | set(EXEMPT)})


def test_sql_in_the_modules_that_write_credentials_is_written_out_or_listed_with_a_reason():
    found = set()
    for rel in credential_modules():
        found |= dynamic_sql_in((call_graph.ROOT / rel).read_text(), rel)

    assert sorted(found - set(DYNAMIC_SQL)) == [], (
        "These functions run SQL that is not one written-out string, in a module that writes credential tables, "
        "so the writer search cannot read it. Write the statement out, or list the function in DYNAMIC_SQL with why "
        "it cannot write a credential.")
    assert sorted(set(DYNAMIC_SQL) - found) == [], "a listed function no longer builds its SQL"


def test_discovery_sees_a_new_writer_however_it_is_spelled():
    source = (
        "class Store:\n"
        "    def mint(self, conn):\n"
        "        conn.execute('insert  into   credentials(id) values (?)', (1,))\n"
        "    def other(self, conn):\n"
        "        def inner():\n"
        "            conn.execute(\"INSERT OR REPLACE INTO mcp_oauth_grants VALUES (1)\")\n"
        "        conn.execute('UPDATE local_password_credentials SET verifier=?', (1,))\n"
        "    def reads(self, conn):\n"
        "        conn.execute('SELECT * FROM credentials')\n"
        "        conn.execute('UPDATE credentials SET revoked_at=1')\n")

    assert writers_in(source, "x.py") == {"x.py::Store.mint", "x.py::Store.other.inner", "x.py::Store.other"}


def test_discovery_reads_sql_put_together_from_written_out_parts():
    source = (
        "TABLE = 'credentials'\n"
        "PREFIX = 'INSERT INTO '\n"
        "def joined(conn):\n"
        "    conn.execute('INSERT INTO ' + 'credentials(id) VALUES (?)', (1,))\n"
        "def named(conn):\n"
        "    conn.execute(PREFIX + 'mcp_oauth_grants(id) VALUES (?)', (1,))\n"
        "def f_string(conn):\n"
        "    conn.execute(f'INSERT INTO credentials(id) VALUES ({1})')\n"
        "def registry(conn):\n"
        "    write_service_registry_in_transaction(conn, {})\n"
        "def innocent(conn):\n"
        "    conn.execute('SELECT ' + '1')\n")

    assert writers_in(source, "x.py") == {"x.py::joined", "x.py::named", "x.py::f_string", "x.py::registry"}


def test_discovery_flags_sql_it_cannot_read():
    source = (
        "def opaque(conn, parts):\n"
        "    conn.execute(' '.join(parts), (1,))\n"
        "def variable(conn, sql):\n"
        "    conn.execute(sql)\n"
        "def literal(conn):\n"
        "    conn.execute('SELECT 1')\n"
        "    conn.execute(f'SELECT 2')\n"
        "def interpolated(conn, name):\n"
        "    conn.execute(f'SELECT * FROM {name}')\n")

    assert dynamic_sql_in(source, "x.py") == {"x.py::opaque", "x.py::variable", "x.py::interpolated"}


def test_every_writer_of_a_credential_is_guarded_or_has_a_reason_no_browser_starts_it():
    found = writers()

    assert sorted(found - set(GUARDED) - set(EXEMPT)) == [], (
        "These functions write a credential and are neither guarded by gateway/session_guard.py nor listed as "
        "started by no browser sign-in. Take a `session` and call session_guard.begin_write / require_live in the "
        "write's transaction, or add the function to EXEMPT with the reason.")
    assert sorted((set(GUARDED) | set(EXEMPT)) - found) == [], "a listed writer no longer writes a credential"


def test_a_guarded_writer_calls_the_shared_helper_not_its_own_check():
    for key in GUARDED:
        rel, qualname = key.split("::")
        function = dict(_functions(ast.parse((call_graph.ROOT / rel).read_text())))[qualname]
        names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in ast.walk(function) if isinstance(n, ast.Call)}
        assert names & {"begin_write", "require_live"}, f"{key} does not go through session_guard"
        assert "session" in {a.arg for a in function.args.kwonlyargs + function.args.args}, key


def test_every_production_call_of_a_guarded_writer_names_its_session():
    bare, literal = [], []
    for file in call_graph.production_files():
        rel = str(file.relative_to(call_graph.ROOT))
        tree = ast.parse(file.read_text())
        for qualname, function in [("<module>", tree), *_functions(tree)]:
            for node in _own_nodes(function):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name not in CALLED_AS and _dotted(node.func) not in CALLED_AS_DOTTED:
                    continue
                keywords = {k.arg: k.value for k in node.keywords}
                if "session" not in keywords:
                    bare.append(f"{rel}:{node.lineno} {name}")
                elif isinstance(keywords["session"], ast.Constant) and keywords["session"].value is None:
                    if f"{rel}::{qualname}" not in NO_BROWSER:
                        literal.append(f"{rel}:{node.lineno} {name} in {qualname}")

    assert bare == [], "a guarded writer is called without `session=` (pass the sign-in, or None where none asks): " + ", ".join(bare)
    assert literal == [], "session=None where a browser can be asking; add the function to NO_BROWSER with the reason: " + ", ".join(literal)


# Calls into the secret store that carry no sign-in, with why: clean-up that must run when the sign-in has ended, a
# follow-on of a save that was already checked, or a start-up migration. Keyed ``file::function#call``.
NO_SESSION_SECRET_CALLS = {
    "gateway/secret_refs.py::delete#secret_store.delete_provider_secret": "the helper for clean-up; callers decide (see below)",
    "gateway/provider_admin.py::create_provider_admin_router.add_provider#secret_store.delete_provider_secret":
        "removes the key this same request stored when the save after it failed",
    "gateway/provider_admin.py::create_provider_admin_router.remove_provider#secret_store.delete_provider_secret":
        "removes a removed provider's key after the checked registry save",
    "gateway/engine_admin.py::EngineService._write#secret_store.delete_provider_secret":
        "removes what this request stored when the save failed, and the secrets the save replaced",
    "gateway/admin_runtime.py::AdminSQLiteStore.update_oidc_config_setting_in#secret_store.delete_provider_secret":
        "drops the replaced secret inside the transaction that was just checked",
    "gateway/admin_store.py::_move_oidc_client_secret_to_store#secret_store.put_provider_secret": "a start-up migration",
    "gateway/secret_externalize_migration.py::migrate#secret_refs.putter": "a start-up migration",
}


def test_every_production_call_into_the_secret_store_carries_its_sign_in_or_is_listed():
    bare, listed_seen = [], set()
    for file in call_graph.production_files():
        rel = str(file.relative_to(call_graph.ROOT))
        tree = ast.parse(file.read_text())
        for qualname, function in _functions(tree):
            for node in _own_nodes(function):
                if not isinstance(node, ast.Call) or _dotted(node.func) not in SECRET_CALLS:
                    continue
                if _dotted(node.func) == "secret_refs.delete":
                    continue   # clean-up helper: its one call into the store is listed above
                key = f"{rel}::{qualname}#{_dotted(node.func)}"
                if any(k.arg == "session" for k in node.keywords):
                    continue
                if key in NO_SESSION_SECRET_CALLS:
                    listed_seen.add(key)
                else:
                    bare.append(f"{rel}:{node.lineno} {key}")

    assert bare == [], (
        "These calls store or delete a secret and carry no `session=`. Pass the browser sign-in, or list the call in "
        "NO_SESSION_SECRET_CALLS with why it must not carry one: " + "; ".join(bare))
    assert sorted(set(NO_SESSION_SECRET_CALLS) - listed_seen) == [], "a listed call no longer exists"


# Every table the gateway creates, sorted by what a signed-out session writing to it could do.
CREDENTIAL_TABLES = {
    "credentials", "local_password_credentials", "account_links", "mcp_oauth_grants", "mcp_oauth_codes",
    "mcp_oauth_tokens", "provider_secrets", "app_trusted_addresses", "oidc_identity_links",
}
# Tables that hold or grant something but whose writers are listed, not guarded, with why.
REVIEWED_UNGUARDED_TABLES = {
    "admin_role_assignments": "grants authority, not a secret or a way in; behind the step-up window (D36). A role granted by a "
                              "request in flight at sign-out is the remaining limit named in D41",
    "mcp_oauth_clients": "dynamic client registration is a public endpoint: no browser sign-in asks for it, and a client holds no access until a person consents (create_grant)",
    "mcp_oauth_requests": "a pending authorization request; it grants nothing until the consent, which is guarded",
    "service_accounts": "a name for an admin key's owner; the key itself is in `credentials`, guarded",
    "account_access": "a person's plan and state, not a secret",
    "session_apps": "the App a sign-in acts through; it cannot hold keys or trusted addresses",
    "operator_settings": "settings; the ones that hold a secret keep it in provider_secrets, and the registry and OIDC writers are guarded",
}
NOT_CREDENTIAL_TABLES = {
    "access_plans", "account_login_limits", "account_oidc_verification", "account_session_lineage",
    "account_session_revocations", "account_sessions", "account_signin_refusals", "apps", "asset_prompts",
    "audit_events", "media_hidden_assets", "media_job_events", "media_job_prompt_erasure_cutoffs", "media_jobs",
    "media_jobs_migrations", "media_jobs_schema_version", "media_volume_identity", "operator_settings_generations",
    "policy_active", "policy_audit", "policy_snapshots", "prompt_memory_erasure_cutoffs",
    "prompt_memory_erasure_intents", "prompt_memory_preferences", "prompt_metadata_migration",
    "prompt_metadata_migration_attempts", "prompt_purge_done", "prompt_purge_queue", "provider_spend",
    "receipt_history", "receipts", "schema_migration_steps", "schema_version", "signin_rule_plans",
    "signin_seen_values", "user_identity_claims", "users", "video_job_records",
}


def test_every_table_is_classified_so_a_new_credential_table_is_not_missed():
    created = set()
    for file in call_graph.production_files():
        created |= set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", file.read_text()))
    classified = CREDENTIAL_TABLES | set(REVIEWED_UNGUARDED_TABLES) | NOT_CREDENTIAL_TABLES

    assert sorted(created - classified) == [], (
        "A table is new. Say what writing to it could do: add it to CREDENTIAL_TABLES (and to CREATES, so its "
        "writers are found and guarded), to REVIEWED_UNGUARDED_TABLES with why, or to NOT_CREDENTIAL_TABLES.")
    assert sorted(classified - created) == [], "a classified table is no longer created"
    assert CREDENTIAL_TABLES <= set(re.findall(r"\|?([a-z_]+)", CREATES.pattern)), "a credential table is missing from CREATES"


def test_routes_that_make_a_credential_are_found_through_their_helpers():
    """The routes below reach a guarded writer; the call graph that finds them is the same one the step-up
    coverage test uses. If discovery broke, this fails before the guarantee is silently empty."""
    import proxy

    reaching = {}
    for route in proxy.app.routes:
        for method in getattr(route, "methods", None) or ():
            endpoint = getattr(route, "endpoint", None)
            if method in {"HEAD", "OPTIONS"} or endpoint is None:
                continue
            reached = {fn.__name__ for fn in call_graph.reach(endpoint)}
            if reached & {"create_credential_hash", "create_link", "set_local_password_verifier", "create_grant"}:
                reaching[(method, re.sub(r"\{[^}]+\}", "*", route.path))] = sorted(
                    reached & {"create_credential_hash", "create_link", "set_local_password_verifier", "create_grant"})

    assert {("POST", "/me/api/apps/*/keys"), ("POST", "/me/api/try-key"), ("POST", "/admin/api/apps/*/keys"),
            ("POST", "/admin/api/admin-keys"), ("POST", "/setup/first-app"), ("POST", "/admin/api/invites"),
            ("POST", "/admin/api/people/*/reset"), ("POST", "/admin/api/signin/owner-password")} <= set(reaching), sorted(reaching)


# -- each guarded writer refuses a revoked sign-in ---------------------------------------------------

@pytest.fixture
def world(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    accounts.initialize(db)
    user = admin_store.upsert_oidc_user("https://idp.test", "person", email="person@example.test", path=db)
    app = admin_store.ensure_personal_app_for_user(user["id"], name="Mine", path=db)
    plan = store_plan("Plan", path=db, models=["m"])
    store = mcp_oauth_store.Store(lambda: db)
    client = store.put_client(client_id="c1", kind="registered", name="Client", redirect_uris=["https://c.example/cb"],
                              grant_types=["authorization_code"], auth_method="none", secret_hash=None, metadata={})
    state = {"n": 0}

    def sign_in(session_id):
        accounts.register_session(user["id"], session_id, int(time.time()) + 3600, path=db)
        return BrowserSession(user["id"], session_id, session_id)

    def count(table):
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def make_key(session):
        state["n"] += 1
        return admin_store.create_credential_hash(
            subject_kind="app", subject_id=app["id"], hash=f"h{state['n']}", prefix="opk_x", session=session, path=db)

    def make_link(session):
        return accounts.create_link("invite", user["id"], path=db, plan_id=plan["id"], session=session)

    def make_password(session):
        return admin_store.set_local_password_verifier(user["id"], "verifier", session=session, path=db)

    def make_grant(session):
        return store.create_grant(person_id=user["id"], client=client, redirect_uri="https://c.example/cb",
                                  resource="https://gw.example/mcp", scope="mcp", session=session)

    def end(session):
        accounts.revoke_session(session.session_id, int(time.time()) + 3600, path=db, family_id=session.family_id)

    def generation():
        record = admin_store.get_service_registry_with_generation(path=db)
        return 0 if record is None else record["operator_settings_generation"]

    def audit(session=None):
        return {"audit_action": "t", "audit_actor_kind": "user", "audit_actor_id": "u", "audit_actor_label": "u",
                "audit_target_kind": "operator_setting", "audit_target_id": "service_registry", "session": session}

    def make_registry_plain(session):
        return admin_store.write_service_registry({"services": {}}, expected_generation=generation(),
                                                  session=session, path=db)

    def make_registry_audited(session):
        return admin_store.write_service_registry_with_audit(
            {"services": {}}, expected_generation=generation(), updated_by="u", path=db, **audit(session))

    def make_registry_and_setting(session):
        return admin_store.write_service_registry_and_setting_with_audits(
            {"services": {}}, expected_generation=generation(), updated_by="u", setting_key="some_setting",
            setting_value={"a": 1}, audits=[{"action": "t"}], session=session, path=db)

    def make_secret(session):
        state["n"] += 1
        return secret_store.put_provider_secret(f"secret-{state['n']}", "a-secret-value", db_path=db, session=session)

    def make_trusted_address(session):
        state["n"] += 1
        return admin_store.add_trusted_address(app["id"], f"10.0.0.{state['n']}", max_entries=50, session=session, path=db)

    def make_identity_link(session):
        state["n"] += 1
        return signin_admin.link_identity(db, "https://idp.test", f"other-{state['n']}", user["id"], linked_by=user["id"],
                                          session=session)

    # writer name -> (make, a number that grows by one with each write)
    writes = {
        "put_provider_secret": (make_secret, lambda: count("provider_secrets")),
        "add_trusted_address": (make_trusted_address, lambda: count("app_trusted_addresses")),
        "link_identity": (make_identity_link, lambda: count("oidc_identity_links")),
        "create_credential_hash": (make_key, lambda: count("credentials")),
        "create_link": (make_link, lambda: count("account_links")),
        "set_local_password_verifier": (make_password, lambda: count("local_password_credentials")),
        "create_grant": (make_grant, lambda: count("mcp_oauth_grants")),
        "write_service_registry": (make_registry_plain, generation),
        "write_service_registry_with_audit": (make_registry_audited, generation),
        "write_service_registry_and_setting_with_audits": (make_registry_and_setting, generation),
    }
    return type("World", (), {"db": db, "sign_in": staticmethod(sign_in), "count": staticmethod(count),
                              "end": staticmethod(end), "writes": writes})


TESTED = sorted(CALLED_AS | {"put_provider_secret", "add_trusted_address", "link_identity"})


def test_every_guarded_writer_has_a_behaviour_test():
    guarded = {key.split("::")[1].rsplit(".", 1)[-1] for key in GUARDED}

    assert guarded == set(TESTED) | {"delete_provider_secret"}


def test_deleting_a_secret_after_sign_out_deletes_nothing(world):
    secret_store.put_provider_secret("doomed", "a-secret-value", db_path=world.db)
    live = world.sign_in("live-session")
    ended = world.sign_in("ended-session")
    world.end(ended)

    with pytest.raises(SessionEnded):
        secret_store.delete_provider_secret("doomed", db_path=world.db, session=ended)
    assert world.count("provider_secrets") == 1

    secret_store.delete_provider_secret("doomed", db_path=world.db, session=live)
    assert world.count("provider_secrets") == 0


def test_clean_up_of_a_secret_runs_even_when_the_sign_in_has_ended(world):
    secret_store.put_provider_secret("orphan", "a-secret-value", db_path=world.db)
    world.end(world.sign_in("ended-session"))

    secret_store.delete_provider_secret("orphan", db_path=world.db)       # no sign-in: clean-up

    assert world.count("provider_secrets") == 0


def _refused(error: BaseException) -> bool:
    return isinstance(error, SessionEnded) or (isinstance(error, accounts.AccountError) and error.code == "login_required")


@pytest.mark.parametrize("writer", TESTED)
def test_a_guarded_writer_writes_for_a_live_sign_in_and_nothing_for_an_ended_one(world, writer):
    write, counter = world.writes[writer]
    live = world.sign_in("live-session")
    ended = world.sign_in("ended-session")

    write(live)
    after_live = counter()
    world.end(ended)
    with pytest.raises((SessionEnded, accounts.AccountError)) as caught:
        write(ended)

    assert _refused(caught.value), caught.value
    assert counter() == after_live


@pytest.mark.parametrize("writer", TESTED)
def test_a_guarded_writer_with_no_sign_in_writes(world, writer):
    write, counter = world.writes[writer]
    before = counter()

    write(None)

    assert counter() == before + 1 or writer == "set_local_password_verifier"   # the password row is replaced, not added


@pytest.mark.parametrize("writer", TESTED)
def test_a_signed_out_everywhere_person_cannot_write_through_any_of_their_sessions(world, writer):
    write, counter = world.writes[writer]
    session = world.sign_in("everywhere-session")
    before = counter()
    accounts.revoke_person_sessions(session.user_id, path=world.db)

    with pytest.raises((SessionEnded, accounts.AccountError)) as caught:
        write(session)

    assert _refused(caught.value)
    assert counter() == before


# -- the two sequences the review ran, and the routes beside them ------------------------------------

def sign_out_just_before(sg, monkeypatch, module, name, subject):
    """Run sign-out-everywhere right before ``module.name`` writes: after the request passed its cookie check."""
    original = getattr(module, name)

    def revoke_then_write(*args, **kwargs):
        accounts.revoke_person_sessions(subject, path=sg.path)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, revoke_then_write)


def test_a_permanent_app_key_is_not_made_after_sign_out_everywhere(sg, monkeypatch):
    client = owner_client(sg)
    headers = {**csrf(client), **ACT}
    app_id = client.post("/me/api/apps", json={"name": "mine"}, headers=headers).json()["app"]["id"]
    sign_out_just_before(sg, monkeypatch, admin_store, "create_credential_hash", cookie_session(client).subject)

    response = client.post(f"/me/api/apps/{app_id}/keys", json={"label": "late"}, headers=headers)

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM credentials WHERE app_id=?", (app_id,)) == []


def test_an_admin_app_key_is_not_made_after_sign_out_everywhere(sg, monkeypatch):
    client = owner_client(sg)
    headers = {**csrf(client), **ACT}
    app_id = client.post("/me/api/apps", json={"name": "mine"}, headers=headers).json()["app"]["id"]
    sign_out_just_before(sg, monkeypatch, admin_store, "create_credential_hash", cookie_session(client).subject)

    response = client.post(f"/admin/api/apps/{app_id}/keys", json={"label": "late"}, headers=headers)

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM credentials WHERE app_id=?", (app_id,)) == []


def test_an_admin_automation_key_is_not_made_after_sign_out_everywhere(sg, monkeypatch):
    client = owner_client(sg)
    headers = {**csrf(client), **ACT}
    sign_out_just_before(sg, monkeypatch, admin_store, "create_credential_hash", cookie_session(client).subject)

    response = client.post("/admin/api/admin-keys", json={"name": "late-robot", "role": "admin"}, headers=headers)

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM credentials WHERE subject_kind='service_account'") == []
    # The account the request had made is left holding no role.
    assert sg.rows("SELECT * FROM admin_role_assignments WHERE subject_kind='service_account' AND revoked_at IS NULL") == []


def test_a_try_key_is_not_made_after_sign_out_everywhere(sg, monkeypatch):
    client = owner_client(sg)
    sign_out_just_before(sg, monkeypatch, admin_store, "create_credential_hash", cookie_session(client).subject)

    response = client.post("/me/api/try-key", json={}, headers={**csrf(client), **ACT})

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM credentials WHERE session_id IS NOT NULL") == []


def test_an_invitation_is_not_made_after_sign_out_everywhere(sg, monkeypatch):
    client = owner_client(sg)
    plan = store_plan("Invite plan", path=sg.path, models=["m"])
    sign_out_just_before(sg, monkeypatch, accounts, "create_link", cookie_session(client).subject)

    response = client.post("/admin/api/invites", json={"access_plan": plan["id"]}, headers=csrf(client))

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM account_links") == []


def test_the_owner_password_is_not_set_after_sign_out_everywhere(sg, monkeypatch):
    client = owner_client(sg)
    before = sg.rows("SELECT verifier FROM local_password_credentials")
    sign_out_just_before(sg, monkeypatch, admin_store, "set_local_password_verifier", cookie_session(client).subject)

    response = client.post("/admin/api/signin/owner-password", json={"password": "a brand new long password"},
                           headers=csrf(client))

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT verifier FROM local_password_credentials") == before


# -- a session that ran out is not live, whatever registration is required --------------------------

def _conn(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_writer_refuses_a_session_whose_stored_row_has_run_out(world):
    write, counter = world.writes["create_credential_hash"]
    session = world.sign_in("ran-out")
    with closing(_conn(world.db)) as conn:
        conn.execute("UPDATE account_sessions SET expires_at=? WHERE session_id=?", (int(time.time()) - 1, "ran-out"))
        conn.commit()
        assert [row[0] for row in conn.execute("SELECT require_registered_session FROM account_access")] in ([], [0])
    before = counter()

    with pytest.raises(SessionEnded):
        write(session)

    assert counter() == before


@pytest.mark.parametrize("column, make", [
    ("row", lambda s, at: s),
    ("cookie idle deadline", lambda s, at: BrowserSession(s.user_id, s.session_id, s.family_id, expires_at=at)),
    ("cookie absolute deadline", lambda s, at: BrowserSession(s.user_id, s.session_id, s.family_id, absolute_expires_at=at)),
])
def test_the_deadline_is_inclusive_at_the_boundary(world, column, make):
    deadline = int(time.time()) + 1000
    session = world.sign_in("boundary")
    with closing(_conn(world.db)) as conn:
        conn.execute("UPDATE account_sessions SET expires_at=? WHERE session_id=?", (deadline, "boundary"))
        conn.commit()
        checked = make(session, deadline)

        session_guard.require_live(conn, checked, now=deadline - 1)       # the last second it is live
        with pytest.raises(SessionEnded):
            session_guard.require_live(conn, checked, now=deadline)       # at the deadline it is not
        with pytest.raises(SessionEnded):
            session_guard.require_live(conn, checked, now=deadline + 1)


def test_a_retired_ids_stored_row_that_ran_out_is_refused_too(world):
    session = world.sign_in("old-id")
    accounts.rotate_session(session.user_id, "old-id", "old-id", "new-id", old_expires_at=int(time.time()) + 100,
                            expires_at=int(time.time()) + 1000, path=world.db)
    with closing(_conn(world.db)) as conn:
        conn.execute("UPDATE account_session_lineage SET expires_at=? WHERE session_id=?", (int(time.time()) - 5, "new-id"))
        conn.commit()
        with pytest.raises(SessionEnded):
            session_guard.require_live(conn, BrowserSession(session.user_id, "new-id", "old-id"))


def test_a_permanent_key_is_not_made_by_a_request_whose_session_ran_out_while_it_was_in_flight(sg, monkeypatch):
    client = owner_client(sg)
    headers = {**csrf(client), **ACT}
    app_id = client.post("/me/api/apps", json={"name": "mine"}, headers=headers).json()["app"]["id"]
    sid = cookie_session(client).session_id
    original = admin_store.create_credential_hash

    def expire_then_write(*args, **kwargs):
        with accounts.connection(sg.path) as conn:
            conn.execute("UPDATE account_sessions SET expires_at=? WHERE session_id=?", (int(time.time()) - 1, sid))
        return original(*args, **kwargs)

    monkeypatch.setattr(admin_store, "create_credential_hash", expire_then_write)

    response = client.post(f"/me/api/apps/{app_id}/keys", json={"label": "late"}, headers=headers)

    assert response.status_code == 401, response.text
    assert sg.rows("SELECT * FROM credentials WHERE app_id=?", (app_id,)) == []
