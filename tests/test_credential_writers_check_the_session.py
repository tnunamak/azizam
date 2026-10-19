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

from gateway import accounts, admin_store, mcp_oauth_store
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
    r"(credentials|mcp_oauth_grants|mcp_oauth_codes|mcp_oauth_tokens|account_links|local_password_credentials)\b"
    r"|UPDATE\s+local_password_credentials\b",
    re.IGNORECASE,
)

# Writers a browser sign-in can start. Each takes ``session`` and runs it through gateway/session_guard.py
# inside its own transaction.
GUARDED = {
    "gateway/admin_store.py::create_credential_hash": "App keys, Try keys, admin keys",
    "gateway/admin_store.py::set_local_password_verifier": "the owner's password",
    "gateway/accounts.py::create_link": "invitation and password-reset links",
    "gateway/mcp_oauth_store.py::Store.create_grant": "an MCP connection (consent)",
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
}

# The guarded writers by the name callers use, and the keyword that carries the sign-in.
CALLED_AS = {"create_credential_hash", "set_local_password_verifier", "create_link", "create_grant"}


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


def writers_in(source: str, rel: str) -> set[str]:
    found = set()
    for qualname, function in _functions(ast.parse(source)):
        for node in _own_nodes(function):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CREATES.search(node.value):
                found.add(f"{rel}::{qualname}")
                break
    return found


def writers() -> set[str]:
    found = set()
    for file in call_graph.production_files():
        found |= writers_in(file.read_text(), str(file.relative_to(call_graph.ROOT)))
    return found


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
                if name not in CALLED_AS:
                    continue
                keywords = {k.arg: k.value for k in node.keywords}
                if "session" not in keywords:
                    bare.append(f"{rel}:{node.lineno} {name}")
                elif isinstance(keywords["session"], ast.Constant) and keywords["session"].value is None:
                    if f"{rel}::{qualname}" not in NO_BROWSER:
                        literal.append(f"{rel}:{node.lineno} {name} in {qualname}")

    assert bare == [], "a guarded writer is called without `session=` (pass the sign-in, or None where none asks): " + ", ".join(bare)
    assert literal == [], "session=None where a browser can be asking; add the function to NO_BROWSER with the reason: " + ", ".join(literal)


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

    writes = {
        "create_credential_hash": (make_key, "credentials"),
        "create_link": (make_link, "account_links"),
        "set_local_password_verifier": (make_password, "local_password_credentials"),
        "create_grant": (make_grant, "mcp_oauth_grants"),
    }
    return type("World", (), {"db": db, "sign_in": staticmethod(sign_in), "count": staticmethod(count),
                              "end": staticmethod(end), "writes": writes})


def _refused(error: BaseException) -> bool:
    return isinstance(error, SessionEnded) or (isinstance(error, accounts.AccountError) and error.code == "login_required")


@pytest.mark.parametrize("writer", sorted(CALLED_AS))
def test_a_guarded_writer_writes_for_a_live_sign_in_and_nothing_for_an_ended_one(world, writer):
    write, table = world.writes[writer]
    live = world.sign_in("live-session")
    ended = world.sign_in("ended-session")

    write(live)
    after_live = world.count(table)
    world.end(ended)
    with pytest.raises((SessionEnded, accounts.AccountError)) as caught:
        write(ended)

    assert _refused(caught.value), caught.value
    assert world.count(table) == after_live


@pytest.mark.parametrize("writer", sorted(CALLED_AS))
def test_a_guarded_writer_with_no_sign_in_writes(world, writer):
    write, table = world.writes[writer]
    before = world.count(table)

    write(None)

    assert world.count(table) == before + 1 or table == "local_password_credentials"


@pytest.mark.parametrize("writer", sorted(CALLED_AS))
def test_a_signed_out_everywhere_person_cannot_write_through_any_of_their_sessions(world, writer):
    write, table = world.writes[writer]
    session = world.sign_in("everywhere-session")
    before = world.count(table)
    accounts.revoke_person_sessions(session.user_id, path=world.db)

    with pytest.raises((SessionEnded, accounts.AccountError)) as caught:
        write(session)

    assert _refused(caught.value)
    assert world.count(table) == before


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
