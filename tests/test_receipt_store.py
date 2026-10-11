"""Tests for gateway.receipt_store — durable receipt persistence.

Uses a temporary SQLite file per test.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import receipt_store  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402


def _make_receipt(**overrides) -> RouteReceipt:
    r = RouteReceipt(
        protocol="openai",
        intent="chat.completions",
        path="/v1/chat/completions",
        app_profile="default",
        api_key_name="alice",
        requested_model="gpt-4o",
        resolved_model="gpt-4o-mini",
        backend="openai.local",
    )
    r.accept_input("messages")
    r.set_policy_decision("allowed", reason="profile-default", source="config")
    r.finish("ok", latency_ms=42.0)
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


class TestReceiptStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "receipts.db"
        receipt_store.init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- init / schema ----

    def test_init_creates_db_and_records_schema_version(self) -> None:
        self.assertTrue(self.db.exists())
        self.assertEqual(receipt_store.schema_version(self.db), receipt_store.SCHEMA_VERSION)
        self.assertEqual(receipt_store.count(self.db), 0)

    def test_init_is_idempotent(self) -> None:
        receipt_store.init_db(self.db)
        receipt_store.init_db(self.db)
        self.assertEqual(receipt_store.schema_version(self.db), receipt_store.SCHEMA_VERSION)

    def test_upgrade_keeps_unattributed_history_out_of_member_feeds(self) -> None:
        import sqlite3

        legacy = Path(self.tmp.name) / "legacy.db"
        con = sqlite3.connect(str(legacy))
        con.executescript(
            "CREATE TABLE receipts (request_id TEXT PRIMARY KEY, created_at "
            "INTEGER NOT NULL, app_profile TEXT, receipt_json TEXT NOT NULL);"
            "CREATE TABLE schema_version (version INTEGER NOT NULL);"
            "INSERT INTO schema_version(version) VALUES (1);"
            "INSERT INTO receipts VALUES ('req_old', 1, 'p', '{}');"
        )
        con.commit()
        con.close()

        receipt_store.init_db(legacy)  # must not raise

        self.assertEqual(receipt_store.schema_version(legacy), receipt_store.SCHEMA_VERSION)
        con = sqlite3.connect(str(legacy))
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(receipts)")]
            self.assertIn("app_id", cols)
            self.assertEqual(receipt_store.count(legacy), 1)
            self.assertEqual(con.execute("SELECT request_id, created_at, app_profile, receipt_json, app_id FROM receipts").fetchone(),
                             ("req_old", 1, "p", "{}", None))
            self.assertEqual(receipt_store.recent(app_ids=["some-app"], path=legacy), [])
            self.assertEqual(receipt_store.history_floor(legacy), 1)
        finally:
            con.close()

    # ---- record / get ----

    def test_record_and_get_round_trip(self) -> None:
        r = _make_receipt()
        rid = receipt_store.record(r, path=self.db)
        self.assertEqual(rid, r.request_id)

        got = receipt_store.get(rid, path=self.db)
        self.assertIsNotNone(got)
        self.assertEqual(got["request_id"], rid)
        self.assertEqual(got["protocol"], "openai")
        self.assertEqual(got["intent"], "chat.completions")
        self.assertEqual(got["resolved_model"], "gpt-4o-mini")
        self.assertEqual(got["status"], "ok")
        self.assertIn("messages", got["accepted_inputs"])
        self.assertEqual(got["policy_decision"], "allowed")
        self.assertIn("_stored_at", got)

    def test_record_accepts_plain_dict(self) -> None:
        r = _make_receipt()
        data = r.to_dict("internal")
        rid = receipt_store.record(data, path=self.db)
        self.assertEqual(rid, r.request_id)
        got = receipt_store.get(rid, path=self.db)
        self.assertEqual(got["request_id"], rid)

    def test_record_is_idempotent_on_request_id(self) -> None:
        r = _make_receipt()
        receipt_store.record(r, path=self.db)
        # Mutate and re-record same id.
        r.status = "error"
        r.add_warning("retried")
        receipt_store.record(r, path=self.db)
        self.assertEqual(receipt_store.count(self.db), 1)
        got = receipt_store.get(r.request_id, path=self.db)
        self.assertEqual(got["status"], "error")
        self.assertIn("retried", got["warnings"])

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(receipt_store.get("req_does_not_exist", path=self.db))

    # ---- audience filtering ----

    def test_get_filters_by_audience(self) -> None:
        r = _make_receipt()
        r.metadata["internal_note"] = "for ops only"
        receipt_store.record(r, path=self.db)

        internal = receipt_store.get(r.request_id, audience="internal", path=self.db)
        admin = receipt_store.get(r.request_id, audience="admin", path=self.db)
        client = receipt_store.get(r.request_id, audience="client", path=self.db)

        self.assertIn("metadata", internal)
        self.assertEqual(internal["metadata"], {"internal_note": "for ops only"})

        # Free-text metadata (e.g. an ops note) stays hidden from admin —
        # only the specific structural keys in ADMIN_VISIBLE_METADATA_FIELDS
        # (request_shape, request_shape_stages, terminal_reason, ...) are
        # promoted, since those are written exclusively by the privacy-safe
        # request-shape path and never carry free text.
        self.assertNotIn("metadata", admin)
        self.assertNotIn("internal_note", admin)
        self.assertEqual(admin["backend"], "openai.local")

        self.assertNotIn("backend", client)
        self.assertNotIn("policy_sources", client)
        self.assertEqual(client["request_id"], r.request_id)
        self.assertEqual(client["status"], "ok")
        self.assertEqual(client["resolved_model"], "gpt-4o-mini")

    def test_admin_audience_still_sees_request_shape_stage_metadata(self) -> None:
        r = _make_receipt()
        r.metadata["internal_note"] = "for ops only"
        r.metadata["terminal_reason"] = "completed"
        r.metadata["request_shape_stages"] = [{"stage": "accepted", "stream": True}]
        receipt_store.record(r, path=self.db)

        admin = receipt_store.get(r.request_id, audience="admin", path=self.db)

        self.assertNotIn("metadata", admin)
        self.assertNotIn("internal_note", admin)
        self.assertEqual(admin["terminal_reason"], "completed")
        self.assertEqual(admin["request_shape_stages"], [{"stage": "accepted", "stream": True}])

    def test_admin_audience_promotes_production_failed_terminal_reason(self) -> None:
        # "failed" is written by production nonstream routes whenever the
        # upstream responds with a non-2xx/3xx status (e.g. proxy.py's
        # `"completed" if 200 <= resp.status_code < 400 else "failed"`).
        # It must survive the admin sanitizer's closed enum, both at the
        # top-level terminal_reason and inside a terminal-stage entry —
        # dropping it silently hid genuine upstream failures from admins.
        r = _make_receipt()
        r.metadata["terminal_reason"] = "failed"
        r.metadata["request_shape_stages"] = [
            {"stage": "terminal", "status_code": 502, "terminal_reason": "failed"},
        ]
        receipt_store.record(r, path=self.db)

        admin = receipt_store.get(r.request_id, audience="admin", path=self.db)

        self.assertEqual(admin["terminal_reason"], "failed")
        self.assertEqual(
            admin["request_shape_stages"],
            [{"stage": "terminal", "status_code": 502, "terminal_reason": "failed"}],
        )

    def test_admin_audience_still_drops_hostile_terminal_reason_values(self) -> None:
        # The enum must stay strict — only real production values are
        # admitted, not arbitrary strings a caller might smuggle in.
        r = _make_receipt()
        r.metadata["terminal_reason"] = "<script>alert(1)</script>"
        r.metadata["request_shape_stages"] = [
            {"stage": "terminal", "status_code": 500, "terminal_reason": "totally-made-up"},
        ]
        receipt_store.record(r, path=self.db)

        admin = receipt_store.get(r.request_id, audience="admin", path=self.db)

        self.assertNotIn("terminal_reason", admin)
        self.assertEqual(
            admin["request_shape_stages"],
            [{"stage": "terminal", "status_code": 500}],
        )

    def test_invalid_audience_raises(self) -> None:
        r = _make_receipt()
        receipt_store.record(r, path=self.db)
        with self.assertRaises(ValueError):
            receipt_store.get(r.request_id, audience="public", path=self.db)

    # ---- safety guards ----

    def test_rejects_receipt_without_request_id(self) -> None:
        data = _make_receipt().to_dict("internal")
        data["request_id"] = ""
        with self.assertRaises(ValueError):
            receipt_store.record(data, path=self.db)

    def test_rejects_forbidden_metadata_keys(self) -> None:
        for bad_key in ("prompt", "image_b64", "authorization", "api_key", "raw_body"):
            r = _make_receipt()
            r.metadata[bad_key] = "should not be persisted"
            with self.assertRaises(ValueError) as ctx:
                receipt_store.record(r, path=self.db)
            self.assertIn(bad_key, str(ctx.exception))

        # And confirm nothing slipped through.
        self.assertEqual(receipt_store.count(self.db), 0)

    def test_rejects_forbidden_top_level_keys(self) -> None:
        data = _make_receipt().to_dict("internal")
        data["prompt"] = "should not be persisted"
        with self.assertRaises(ValueError) as ctx:
            receipt_store.record(data, path=self.db)
        self.assertIn("top-level", str(ctx.exception))

    def test_rejects_non_mapping_metadata(self) -> None:
        data = _make_receipt().to_dict("internal")
        data["metadata"] = ["not", "a", "mapping"]
        with self.assertRaises(ValueError):
            receipt_store.record(data, path=self.db)

    def test_rejects_wrong_type(self) -> None:
        with self.assertRaises(TypeError):
            receipt_store.record("nope", path=self.db)  # type: ignore[arg-type]

    # ---- recent / filters ----

    def test_recent_orders_newest_first(self) -> None:
        ids = []
        now = int(time.time())
        for i in range(5):
            r = _make_receipt()
            ids.append(r.request_id)
            receipt_store.record(r, created_at=now + i, path=self.db)

        rows = receipt_store.recent(limit=10, path=self.db)
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["request_id"] for row in rows], list(reversed(ids)))

    def test_recent_limit(self) -> None:
        now = int(time.time())
        for i in range(7):
            receipt_store.record(_make_receipt(), created_at=now + i, path=self.db)
        rows = receipt_store.recent(limit=3, path=self.db)
        self.assertEqual(len(rows), 3)

    def test_recent_filters_by_app_profile(self) -> None:
        now = int(time.time())
        a = _make_receipt(app_profile="alpha")
        b = _make_receipt(app_profile="beta")
        c = _make_receipt(app_profile="alpha")
        receipt_store.record(a, created_at=now, path=self.db)
        receipt_store.record(b, created_at=now + 1, path=self.db)
        receipt_store.record(c, created_at=now + 2, path=self.db)

        rows = receipt_store.recent(limit=10, app_profile="alpha", path=self.db)
        self.assertEqual({row["request_id"] for row in rows}, {a.request_id, c.request_id})

    def test_recent_app_ids_is_an_owner_scope_allowlist(self) -> None:
        # The user activity feed scopes receipts to the caller's owned Apps by
        # app_id. This is a *positive* membership filter — it only narrows.
        now = int(time.time())
        a1 = _make_receipt(app_id="app_alice")
        a2 = _make_receipt(app_id="app_alice")
        b = _make_receipt(app_id="app_bob")
        receipt_store.record(a1, created_at=now, path=self.db)
        receipt_store.record(a2, created_at=now + 1, path=self.db)
        receipt_store.record(b, created_at=now + 2, path=self.db)

        rows = receipt_store.recent(limit=10, app_ids=["app_alice"], path=self.db)
        self.assertEqual(
            {row["request_id"] for row in rows}, {a1.request_id, a2.request_id}
        )
        # Multiple owned apps union.
        rows_both = receipt_store.recent(
            limit=10, app_ids=["app_alice", "app_bob"], path=self.db
        )
        self.assertEqual(len(rows_both), 3)

    def test_recent_app_ids_excludes_null_app_id(self) -> None:
        # Receipts with no owning App (unauthenticated / legacy keys) must NEVER
        # appear in an owner-scoped feed — NULL is not IN any allow-list.
        unattributed = _make_receipt(app_id=None)
        owned = _make_receipt(app_id="app_alice")
        receipt_store.record(unattributed, path=self.db)
        receipt_store.record(owned, path=self.db)
        rows = receipt_store.recent(limit=10, app_ids=["app_alice"], path=self.db)
        self.assertEqual([r["request_id"] for r in rows], [owned.request_id])

    def test_recent_owner_scope_can_include_null_app_id_with_source_label(self) -> None:
        local = _make_receipt(app_id=None, api_key_name=None)
        owned = _make_receipt(app_id="app_alice", api_key_name="Alice app")
        receipt_store.record(local, created_at=1, path=self.db)
        receipt_store.record(owned, created_at=2, path=self.db)

        member_rows = receipt_store.recent(limit=10, app_ids=["app_alice"], path=self.db)
        self.assertEqual([row["request_id"] for row in member_rows], [owned.request_id])

        owner_rows = receipt_store.recent(
            limit=10, app_ids=["app_alice"], include_null_app_id=True, path=self.db
        )
        self.assertEqual([row["request_id"] for row in owner_rows], [owned.request_id, local.request_id])
        self.assertEqual(owner_rows[1]["source"]["kind"], "local-network")
        self.assertEqual(owner_rows[1]["source"]["label"], "Local network · no key")
        self.assertEqual(owner_rows[0]["source"]["kind"], "app")

    def test_recent_empty_app_ids_returns_no_rows_never_leaks(self) -> None:
        # A caller who owns no Apps gets an empty feed — it must NOT fall through
        # to an unfiltered query that would return everyone's receipts.
        receipt_store.record(_make_receipt(app_id="app_alice"), path=self.db)
        receipt_store.record(_make_receipt(app_id="app_bob"), path=self.db)
        self.assertEqual(receipt_store.recent(limit=10, app_ids=[], path=self.db), [])

    def test_record_persists_app_id_column(self) -> None:
        # app_id is stored as its own indexed column (not just inside the JSON),
        # so the owner-scope filter can use it.
        import sqlite3

        receipt_store.record(_make_receipt(app_id="app_x"), path=self.db)
        con = sqlite3.connect(str(self.db))
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(receipts)")]
            self.assertIn("app_id", cols)
            stored = con.execute(
                "SELECT app_id FROM receipts WHERE app_id = ?", ("app_x",)
            ).fetchone()
            self.assertEqual(stored[0], "app_x")
        finally:
            con.close()

    def test_recent_since_window(self) -> None:
        now = int(time.time())
        old = _make_receipt()
        fresh = _make_receipt()
        receipt_store.record(old, created_at=now - 3600, path=self.db)
        receipt_store.record(fresh, created_at=now, path=self.db)

        rows = receipt_store.recent(limit=10, since_s=60, path=self.db)
        self.assertEqual([row["request_id"] for row in rows], [fresh.request_id])

    def test_recent_applies_audience(self) -> None:
        r = _make_receipt()
        r.metadata["secret_diag"] = "internal-only"
        receipt_store.record(r, path=self.db)
        rows = receipt_store.recent(limit=10, audience="client", path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("metadata", rows[0])
        self.assertNotIn("backend", rows[0])

    def test_persistent_client_receipt_detail_omits_provider_cost_admin_keeps_it(self) -> None:
        r = _make_receipt(remote=True)
        r.cost = {
            "usd": 0.0123,
            "source": "provider-reported",
            "cost_class": "paid-remote",
            "provider": "openrouter",
        }
        receipt_store.record(r, path=self.db)

        client = receipt_store.get(r.request_id, audience="client", path=self.db)
        admin = receipt_store.get(r.request_id, audience="admin", path=self.db)

        self.assertEqual(
            client["cost"],
            {"usd": 0.0123, "source": "provider-reported", "cost_class": "paid-remote"},
        )
        self.assertEqual(admin["cost"]["provider"], "openrouter")

    def test_persistent_client_activity_omits_provider_cost_admin_keeps_it(self) -> None:
        r = _make_receipt(remote=True)
        r.cost = {
            "usd": 0.0123,
            "source": "provider-reported",
            "cost_class": "paid-remote",
            "provider": "openrouter",
        }
        receipt_store.record(r, path=self.db)

        client_rows = receipt_store.recent(limit=10, audience="client", path=self.db)
        admin_rows = receipt_store.recent(limit=10, audience="admin", path=self.db)

        self.assertEqual(
            client_rows[0]["cost"],
            {"usd": 0.0123, "source": "provider-reported", "cost_class": "paid-remote"},
        )
        self.assertEqual(admin_rows[0]["cost"]["provider"], "openrouter")

    def test_update_cost_refreshes_persisted_receipt_cost(self) -> None:
        r = _make_receipt(remote=True)
        r.cost = {
            "usd": None,
            "source": "unknown",
            "provider": "openrouter",
            "cost_class": "paid-remote",
            "reservation_id": 1,
            "reconciliation_status": "needs_reconciliation",
        }
        r.metadata["decisions"] = {
            "question_count": 2,
            "engine": None,
            "remote": True,
            "cost_usd": None,
            "usage": {"input_tokens": 302, "output_tokens": 37},
        }
        receipt_store.record(r, path=self.db)

        updated = receipt_store.update_cost(
            r.request_id,
            {
                "usd": "0.030000000",
                "source": "owner-reported",
                "provider": "openrouter",
                "cost_class": "paid-remote",
                "reservation_id": 1,
                "reconciliation_status": "settled",
            },
            path=self.db,
        )

        self.assertTrue(updated)
        stored = receipt_store.get(r.request_id, path=self.db)
        self.assertEqual(stored["cost"]["usd"], "0.030000000")
        self.assertEqual(stored["cost"]["source"], "owner-reported")
        self.assertEqual(stored["cost"]["provider"], "openrouter")
        self.assertEqual(stored["cost"]["reconciliation_status"], "settled")
        self.assertEqual(stored["metadata"]["decisions"]["cost_usd"], "0.030000000")

    def test_update_cost_noops_when_store_is_absent(self) -> None:
        missing = self.db.with_name("missing-receipts.db")

        self.assertFalse(
            receipt_store.update_cost(
                "req_missing",
                {"usd": "0.01", "source": "owner-reported"},
                path=missing,
            )
        )
        self.assertFalse(missing.exists())

    def test_update_cost_errors_when_existing_store_is_malformed(self) -> None:
        malformed = self.db.with_name("malformed-receipts.db")
        malformed.write_text("not sqlite", encoding="utf-8")

        with self.assertRaises(Exception):
            receipt_store.update_cost(
                "req_missing",
                {"usd": "0.01", "source": "owner-reported"},
                path=malformed,
            )

    def test_recent_zero_limit_returns_empty(self) -> None:
        receipt_store.record(_make_receipt(), path=self.db)
        self.assertEqual(receipt_store.recent(limit=0, path=self.db), [])

    # ---- prune ----

    def test_prune_deletes_old_rows(self) -> None:
        now = int(time.time())
        old = _make_receipt()
        fresh = _make_receipt()
        receipt_store.record(old, created_at=now - (30 * 86400 + 3600), path=self.db)
        receipt_store.record(fresh, created_at=now, path=self.db)

        deleted = receipt_store.prune(retain_s=30 * 86400, path=self.db)
        self.assertEqual(deleted, 1)
        self.assertEqual(receipt_store.count(self.db), 1)
        self.assertIsNone(receipt_store.get(old.request_id, path=self.db))
        self.assertIsNotNone(receipt_store.get(fresh.request_id, path=self.db))

    def test_prune_custom_window(self) -> None:
        now = int(time.time())
        for i in range(3):
            receipt_store.record(_make_receipt(), created_at=now - (i + 1) * 100, path=self.db)
        deleted = receipt_store.prune(retain_s=150, path=self.db)
        self.assertEqual(deleted, 2)
        self.assertEqual(receipt_store.count(self.db), 1)

    # ---- request-shape observability (structural-only fields) ----

    def test_unbounded_nonstream_request_shape_is_directly_queryable(self) -> None:
        """The abnormal shape flagged in the reliability audit — stream:false
        with an absent/-1 token limit — must be readable back from a stored
        receipt without inspecting prompt content."""
        r = _make_receipt()
        r.metadata["request_shape"] = {
            "stream": False,
            "n_predict": -1,
            "message_count": 3,
            "is_unbounded_nonstream": True,
        }
        receipt_store.record(r, path=self.db)

        stored = receipt_store.get(r.request_id, path=self.db)
        self.assertIsNotNone(stored)
        self.assertTrue(stored["metadata"]["request_shape"]["is_unbounded_nonstream"])
        self.assertEqual(stored["metadata"]["request_shape"]["stream"], False)
        self.assertEqual(stored["metadata"]["request_shape"]["n_predict"], -1)

    def test_bounded_stream_request_shape_round_trips(self) -> None:
        r = _make_receipt()
        r.metadata["request_shape"] = {
            "stream": True,
            "max_tokens": 512,
            "message_count": 2,
            "is_unbounded_nonstream": False,
        }
        receipt_store.record(r, path=self.db)

        stored = receipt_store.get(r.request_id, path=self.db)
        self.assertFalse(stored["metadata"]["request_shape"]["is_unbounded_nonstream"])

    def test_request_shape_metadata_never_admits_forbidden_keys(self) -> None:
        r = _make_receipt()
        r.metadata["request_shape"] = {
            "stream": False,
            "messages": [{"role": "user", "content": "should never be here"}],
        }
        with self.assertRaises(ValueError):
            receipt_store.record(r, path=self.db)

    def test_terminal_reason_metadata_round_trips(self) -> None:
        r = _make_receipt()
        r.metadata["terminal_reason"] = "cancelled"
        receipt_store.record(r, path=self.db)

        stored = receipt_store.get(r.request_id, path=self.db)
        self.assertEqual(stored["metadata"]["terminal_reason"], "cancelled")

    def test_client_audience_never_exposes_request_shape_metadata(self) -> None:
        """Metadata (which carries request_shape) is internal/admin-only —
        the client-facing receipt view must not leak it."""
        r = _make_receipt()
        r.metadata["request_shape"] = {"stream": False, "is_unbounded_nonstream": True}
        receipt_store.record(r, path=self.db)

        stored = receipt_store.get(r.request_id, audience="client", path=self.db)
        self.assertNotIn("metadata", stored)

    # ---- accepted receipt lifecycle ----

    def test_begin_finalize_preserves_accepted_facts_and_explicit_zero_cap(self) -> None:
        receipt = _make_receipt()
        receipt.stream = False
        receipt.effective_output_limit = 0

        receipt_store.begin(receipt, now_ms=1_000, path=self.db)
        begun = receipt_store.get(receipt.request_id, path=self.db, now_ms=1_001)
        self.assertEqual(begun["status"], "in_progress")
        self.assertEqual(begun["accepted_at_ms"], 1_000)
        self.assertEqual(begun["effective_output_limit"], 0)

        # A terminal caller may add terminal facts, but cannot rewrite what was
        # accepted (including identity/model/cap) after the upstream wait.
        receipt.app_id = "mutated-app"
        receipt.requested_model = "mutated-model"
        receipt.effective_output_limit = 99
        receipt.finish("succeeded", now_ms=2_000)
        self.assertTrue(receipt_store.finalize(receipt, path=self.db))

        stored = receipt_store.get(receipt.request_id, path=self.db, now_ms=2_001)
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["accepted_at_ms"], 1_000)
        self.assertEqual(stored["effective_output_limit"], 0)
        self.assertEqual(stored["requested_model"], "gpt-4o")

    def test_finalize_updates_resolution_facts_without_replacing_acceptance(self) -> None:
        receipt = _make_receipt()
        receipt.backend = None
        receipt.resolved_model = "current"
        receipt_store.begin(receipt, now_ms=1_000, path=self.db)

        receipt.backend = "local"
        receipt.resolved_model = "loaded.gguf"
        receipt.requested_model = "mutated-request"
        receipt.finish("failed", now_ms=2_000)
        self.assertTrue(receipt_store.finalize(receipt, path=self.db))

        stored = receipt_store.get(receipt.request_id, path=self.db)
        self.assertEqual(stored["backend"], "local")
        self.assertEqual(stored["resolved_model"], "loaded.gguf")
        self.assertEqual(stored["requested_model"], "gpt-4o")

    def test_finalize_malformed_legacy_row_leaves_row_in_progress(self) -> None:
        receipt = _make_receipt()
        receipt_store.begin(receipt, now_ms=1_000, path=self.db)
        with receipt_store._conn(self.db) as conn:
            conn.execute(
                "UPDATE receipts SET receipt_json = ? WHERE request_id = ?",
                ("{not-json", receipt.request_id),
            )
            conn.commit()
        receipt.finish("cancelled", now_ms=2_000)
        with self.assertRaises(ValueError):
            receipt_store.finalize(receipt, path=self.db)
        with receipt_store._conn(self.db) as conn:
            status = conn.execute(
                "SELECT status FROM receipts WHERE request_id = ?", (receipt.request_id,)
            ).fetchone()[0]
        self.assertEqual(status, "in_progress")

    def test_begin_is_idempotent_and_terminal_rows_never_regress(self) -> None:
        receipt = _make_receipt()
        receipt_store.begin(receipt, now_ms=1_000, path=self.db)
        receipt_store.begin(receipt, now_ms=9_000, path=self.db)
        receipt.finish("failed", now_ms=2_000)
        self.assertTrue(receipt_store.finalize(receipt, path=self.db))
        self.assertFalse(receipt_store.finalize(receipt, path=self.db))

        stored = receipt_store.get(receipt.request_id, path=self.db, now_ms=10_000)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["accepted_at_ms"], 1_000)

    def test_concurrent_finalizers_have_one_winner(self) -> None:
        receipt = _make_receipt()
        receipt_store.begin(receipt, now_ms=1_000, path=self.db)

        def finalize(status: str) -> bool:
            candidate = _make_receipt(request_id=receipt.request_id)
            candidate.finish(status, now_ms=2_000)
            return receipt_store.finalize(candidate, path=self.db)

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(finalize, ("succeeded", "failed")))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 1)
        self.assertIn(
            receipt_store.get(receipt.request_id, path=self.db)["status"],
            {"succeeded", "failed"},
        )

    def test_reopen_keeps_in_progress_and_only_projects_stale(self) -> None:
        receipt = _make_receipt()
        receipt_store.begin(receipt, now_ms=1_000, path=self.db)
        receipt_store.init_db(self.db)  # process reopen, not a recovery write

        fresh = receipt_store.get(receipt.request_id, path=self.db, now_ms=1_100, stale_after_ms=500)
        stale = receipt_store.get(receipt.request_id, path=self.db, now_ms=1_500, stale_after_ms=500)
        self.assertEqual(fresh["status"], "in_progress")
        self.assertEqual(stale["status"], "stale_incomplete")
        # The projection never persists a guessed terminal state.
        raw = receipt_store.get(receipt.request_id, path=self.db, now_ms=1_100, stale_after_ms=500)
        self.assertEqual(raw["status"], "in_progress")

    def test_lifecycle_rejects_payloads_before_any_disk_write(self) -> None:
        receipt = _make_receipt()
        receipt.metadata["headers"] = {"authorization": "not-safe"}
        with self.assertRaises(ValueError):
            receipt_store.begin(receipt, path=self.db)
        self.assertEqual(receipt_store.count(self.db), 0)

        safe = _make_receipt()
        receipt_store.begin(safe, path=self.db)
        safe.metadata["tool_calls"] = [{"args": "not-safe"}]
        safe.finish("failed")
        with self.assertRaises(ValueError):
            receipt_store.finalize(safe, path=self.db)
        self.assertEqual(receipt_store.get(safe.request_id, path=self.db)["status"], "in_progress")

    def test_primary_lists_and_counts_exclude_parent_linked_children(self) -> None:
        parent = _make_receipt()
        child = _make_receipt(parent_request_id=parent.request_id, app_id=None, api_key_name=None)
        receipt_store.record(parent, path=self.db)
        receipt_store.record(child, path=self.db)

        self.assertEqual(receipt_store.count(self.db), 1)
        self.assertEqual(receipt_store.count(self.db, outer_only=False), 2)
        self.assertEqual(
            {row["request_id"] for row in receipt_store.recent(path=self.db)}, {parent.request_id}
        )
        self.assertEqual(len(receipt_store.recent(path=self.db, outer_only=False)), 2)


    def test_cost_update_racing_finalize_keeps_both_outcome_and_cost(self) -> None:
        import threading
        from unittest.mock import patch

        receipt = _make_receipt()
        receipt_store.begin(receipt, now_ms=1_000, path=self.db)
        receipt.finish("succeeded", now_ms=2_000)
        cost = {"usd": 0.25, "source": "owner-reported", "reconciliation_status": "settled"}
        finished = []
        terminal = threading.Thread(target=lambda: finished.append(receipt_store.finalize(receipt, path=self.db)))
        real_loads = receipt_store._safe_loads
        def finalize_after_cost_read(text):
            if not terminal.is_alive() and not finished:
                terminal.start()  # finalize arrives after update_cost read the row
                terminal.join(timeout=0.3)
            return real_loads(text)
        with patch.object(receipt_store, "_safe_loads", finalize_after_cost_read):
            self.assertTrue(receipt_store.update_cost(receipt.request_id, cost, path=self.db))
        terminal.join(timeout=10)
        self.assertEqual(finished, [True])
        stored = receipt_store.get(receipt.request_id, path=self.db)
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["cost"]["usd"], 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
