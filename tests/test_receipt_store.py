"""Tests for gateway.receipt_store — durable receipt persistence.

Uses a temporary SQLite file per test.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
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

        self.assertNotIn("metadata", admin)
        self.assertEqual(admin["backend"], "openai.local")

        self.assertNotIn("backend", client)
        self.assertNotIn("policy_sources", client)
        self.assertEqual(client["request_id"], r.request_id)
        self.assertEqual(client["status"], "ok")
        self.assertEqual(client["resolved_model"], "gpt-4o-mini")

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

    def test_recent_zero_limit_returns_empty(self) -> None:
        receipt_store.record(_make_receipt(), path=self.db)
        self.assertEqual(receipt_store.recent(limit=0, path=self.db), [])

    # ---- prune ----

    def test_prune_deletes_old_rows(self) -> None:
        now = int(time.time())
        old = _make_receipt()
        fresh = _make_receipt()
        receipt_store.record(old, created_at=now - (receipt_store.RETAIN_S + 3600), path=self.db)
        receipt_store.record(fresh, created_at=now, path=self.db)

        deleted = receipt_store.prune(path=self.db)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
