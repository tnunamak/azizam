"""Backfill of connection_id onto sign-in receipts recorded before receipts carried it."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from gateway import admin_store, mcp_oauth_store, receipt_store
from gateway.receipts import RouteReceipt

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/migrations/receipt_connection_ids.py"
SPEC = importlib.util.spec_from_file_location("receipt_connection_ids", SCRIPT)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)

T0 = 1_800_000_000


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.admin = tmp_path / "admin.db"
        self.receipts = tmp_path / "receipts.db"
        admin_store.init_db(self.admin)
        receipt_store.init_db(self.receipts)
        self.store = mcp_oauth_store.Store(lambda: self.admin)
        self.person = admin_store.upsert_oidc_user("issuer", "ann", email="ann@example.test", path=self.admin)
        self.other = admin_store.upsert_oidc_user("issuer", "ben", email="ben@example.test", path=self.admin)
        self.session = admin_store.ensure_session_app_for_user(self.person["id"], name="Azizam", path=self.admin)
        self.key_app = admin_store.create_app("Key app", kind="personal", owner_user_id=self.person["id"],
                                              access_plan="person", path=self.admin)

    def grant(self, name: str = "ChatGPT", *, person=None, created: int = T0, revoked: int | None = None):
        person = person or self.person
        client = self.store.put_client(
            client_id=f"c_{name}_{created}_{person['id']}", kind="registered", name=name,
            redirect_uris=["https://x.test/cb"], grant_types=["authorization_code"], auth_method="none",
            secret_hash=None, metadata={})
        grant = self.store.create_grant(person_id=person["id"], client=client, redirect_uri="https://x.test/cb",
                                        resource="https://gw.test/mcp", scope="mcp")
        with admin_store._conn(self.admin) as conn:
            conn.execute("UPDATE mcp_oauth_grants SET created_at = ?, revoked_at = ? WHERE id = ?",
                         (created, revoked, grant.id))
            conn.commit()
        return grant

    def receipt(self, request_id: str, *, at: int, app_id: str | None = None, name: str = "ChatGPT (MCP)",
                status: str = "succeeded", connection_id: str | None = None) -> None:
        receipt = RouteReceipt(protocol="mcp", intent="chat.generate", request_id=request_id,
                               app_id=app_id or self.session["id"], api_key_name=name, status=status,
                               connection_id=connection_id)
        receipt.accepted_at_ms, receipt.updated_at_ms = at * 1000, at * 1000
        receipt_store.record(receipt, created_at=at, path=self.receipts)

    def connection_ids(self) -> dict[str, str | None]:
        with sqlite3.connect(self.receipts) as conn:
            return dict(conn.execute("SELECT request_id, json_extract(receipt_json, '$.connection_id') FROM receipts"))


def test_dry_run_counts_and_changes_nothing(tmp_path, capsys):
    world = World(tmp_path)
    grant = world.grant()
    world.receipt("r1", at=T0 + 10)
    world.receipt("r2", at=T0 + 20)
    plan = migration.plan(world.admin, world.receipts)
    assert (plan.examined, len(plan.tagged), plan.ambiguous, plan.no_grant) == (2, 2, 0, 0)
    assert plan.per_grant == {grant.id: 2}
    before = (tmp_path / "receipts.db").read_bytes()
    assert migration.main(["--admin-db", str(world.admin), "--receipts-db", str(world.receipts)]) == 0
    assert "to tag with one connection: 2" in capsys.readouterr().out
    assert (tmp_path / "receipts.db").read_bytes() == before
    assert set(world.connection_ids().values()) == {None}


def test_apply_tags_once_and_a_second_run_changes_nothing(tmp_path, capsys):
    world = World(tmp_path)
    grant = world.grant()
    world.receipt("r1", at=T0 + 10)
    args = ["--admin-db", str(world.admin), "--receipts-db", str(world.receipts), "--apply"]
    assert migration.main(args) == 0
    assert "tagged: 1" in capsys.readouterr().out
    assert world.connection_ids() == {"r1": grant.id}
    assert migration.main(args) == 0
    second = capsys.readouterr().out
    assert "sign-in receipts without a connection: 0" in second
    assert "tagged: 0" in second
    assert world.connection_ids() == {"r1": grant.id}
    with sqlite3.connect(world.receipts) as conn:
        row = json.loads(conn.execute("SELECT receipt_json FROM receipts").fetchone()[0])
    assert row["api_key_name"] == "ChatGPT (MCP)" and row["status"] == "succeeded"


def test_only_receipts_one_connection_could_have_made_are_tagged(tmp_path):
    world = World(tmp_path)
    grant = world.grant(created=T0)
    world.receipt("fits", at=T0 + 5)
    world.receipt("before_connection", at=T0 - 5)
    world.receipt("a_key_named_like_one", at=T0 + 5, app_id=world.key_app["id"])
    world.receipt("other_client_name", at=T0 + 5, name="Claude (MCP)")
    world.receipt("browser", at=T0 + 5, name="Azizam")
    world.receipt("unfinished", at=T0 + 5, status="in_progress")
    world.receipt("already_tagged", at=T0 + 5, connection_id="mcg_earlier")
    assert migration.main(["--admin-db", str(world.admin), "--receipts-db", str(world.receipts), "--apply"]) == 0
    assert world.connection_ids() == {
        "fits": grant.id, "before_connection": None, "a_key_named_like_one": None, "other_client_name": None,
        "browser": None, "unfinished": None, "already_tagged": "mcg_earlier",
    }


def test_another_persons_connection_never_claims_a_receipt(tmp_path):
    world = World(tmp_path)
    world.grant(person=world.other)
    world.receipt("mine", at=T0 + 5)
    plan = migration.plan(world.admin, world.receipts)
    assert (len(plan.tagged), plan.no_grant) == (0, 1)


def test_two_overlapping_connections_of_one_client_are_left_alone(tmp_path):
    world = World(tmp_path)
    world.grant(created=T0)
    world.grant(created=T0 + 1)
    world.receipt("either", at=T0 + 5)
    plan = migration.plan(world.admin, world.receipts)
    assert (len(plan.tagged), plan.ambiguous) == (0, 1)


def test_a_reconnected_client_splits_history_at_the_old_connections_end(tmp_path):
    world = World(tmp_path)
    old = world.grant(created=T0, revoked=T0 + 100)
    new = world.grant(created=T0 + 200)
    world.receipt("during_old", at=T0 + 50)
    world.receipt("gap", at=T0 + 150)
    world.receipt("during_new", at=T0 + 250)
    assert migration.main(["--admin-db", str(world.admin), "--receipts-db", str(world.receipts), "--apply"]) == 0
    assert world.connection_ids() == {"during_old": old.id, "gap": None, "during_new": new.id}


def test_database_without_connections_is_a_no_op(tmp_path):
    world = World(tmp_path)
    with admin_store._conn(world.admin) as conn:
        conn.execute("DROP TABLE IF EXISTS mcp_oauth_grants")
        conn.commit()
    world.receipt("r1", at=T0)
    assert migration.plan(world.admin, world.receipts).tagged == {}
