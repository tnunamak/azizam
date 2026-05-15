"""Tests for the shared redaction guard.

Covers:

- The pure ``gateway.redaction`` module: forbidden-key vocabulary, single- and
  nested-level scanning, error message hygiene (no values leak into messages).
- That ``gateway.receipt_store.record`` and ``gateway.media_jobs`` both reject
  the same forbidden keys, proving the two stores share one contract.
- That safe diagnostic shapes are accepted.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import media_jobs, receipt_store, redaction  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402


# Keys that every persistence surface must reject. Covers the four families:
# raw payload, media bytes (multiple modalities), secrets, credentials.
_FORBIDDEN_SAMPLE = (
    "prompt",
    "prompts",
    "messages",
    "input",
    "inputs",
    "image",
    "image_b64",
    "image_bytes",
    "video_bytes",
    "audio_bytes",
    "media_bytes",
    "bytes",
    "data_b64",
    "body",
    "raw_body",
    "raw_request",
    "raw_response",
    "request_body",
    "response_body",
    "authorization",
    "api_key",
    "api_token",
    "secret",
    "token",
    "password",
    "credentials",
    "cookie",
    "cookies",
)

# Diagnostic-shaped keys that must round-trip without complaint.
_SAFE_DIAGNOSTIC_KEYS = (
    "prompt_id",
    "prompt_hash",
    "queue_pos",
    "queue_position",
    "image_id",
    "image_url",
    "image_name",
    "image_count",
    "video_id",
    "audio_id",
    "media_type",
    "trace_id",
    "step",
    "phase",
    "total",
    "latency_ms",
    "backend_status",
    "auth_scope",
    "auth_kind",
    "token_count",
    "tokens_in",
    "tokens_out",
    "secret_hash",
)


def _make_receipt() -> RouteReceipt:
    r = RouteReceipt(
        protocol="openai",
        intent="chat.completions",
        path="/v1/chat/completions",
        app_profile="default",
        api_key_name="alice",
    )
    r.accept_input("messages")
    r.finish("ok", latency_ms=10.0)
    return r


class TestRedactionModule(unittest.TestCase):
    def test_forbidden_keys_cover_all_families(self) -> None:
        for k in _FORBIDDEN_SAMPLE:
            self.assertIn(k, redaction.FORBIDDEN_KEYS, f"{k!r} missing from FORBIDDEN_KEYS")

    def test_is_forbidden_is_case_insensitive(self) -> None:
        self.assertTrue(redaction.is_forbidden_key("PROMPT"))
        self.assertTrue(redaction.is_forbidden_key("Api_Key"))
        self.assertTrue(redaction.is_forbidden_key("AUTHORIZATION"))
        self.assertFalse(redaction.is_forbidden_key("prompt_id"))
        self.assertFalse(redaction.is_forbidden_key("auth_scope"))

    def test_find_forbidden_keys_returns_sorted_unique(self) -> None:
        found = redaction.find_forbidden_keys({"prompt": "x", "PROMPT": "y", "token": 1, "safe": True})
        self.assertEqual(found, ["prompt", "token"])

    def test_find_forbidden_keys_recurses_one_level(self) -> None:
        nested = {"trace": {"prompt": "leaked"}, "outer": "ok"}
        self.assertEqual(redaction.find_forbidden_keys(nested), ["prompt"])
        self.assertEqual(redaction.find_forbidden_keys(nested, recurse=False), [])

    def test_find_forbidden_keys_handles_non_mapping(self) -> None:
        self.assertEqual(redaction.find_forbidden_keys(None), [])
        self.assertEqual(redaction.find_forbidden_keys([1, 2, 3]), [])  # type: ignore[arg-type]

    def test_assert_safe_mapping_rejects_non_mapping(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            redaction.assert_safe_mapping("thing", "nope")
        self.assertIn("thing", str(ctx.exception))

    def test_assert_safe_mapping_accepts_diagnostic_keys(self) -> None:
        payload = {k: f"v{i}" for i, k in enumerate(_SAFE_DIAGNOSTIC_KEYS)}
        redaction.assert_safe_mapping("safe", payload)

    def test_assert_safe_mapping_rejects_each_forbidden_key(self) -> None:
        for key in _FORBIDDEN_SAMPLE:
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    redaction.assert_safe_mapping("meta", {key: "VALUE-MUST-NOT-LEAK"})
                msg = str(ctx.exception)
                self.assertIn(key.lower(), msg)
                # Crucial property: values never appear in the error message.
                self.assertNotIn("VALUE-MUST-NOT-LEAK", msg)

    def test_assert_safe_mapping_rejects_nested_forbidden_key(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            redaction.assert_safe_mapping("meta", {"trace": {"prompt": "leaked"}})
        msg = str(ctx.exception)
        self.assertIn("nested", msg)
        self.assertIn("prompt", msg)
        self.assertNotIn("leaked", msg)

    def test_assert_safe_mapping_recurse_false_skips_nested(self) -> None:
        redaction.assert_safe_mapping("meta", {"trace": {"prompt": "x"}}, recurse=False)

    def test_assert_safe_top_level_rejects_forbidden_top_keys(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            redaction.assert_safe_top_level("record", {"prompt": "x", "ok": 1})
        self.assertIn("prompt", str(ctx.exception))

    def test_assert_safe_top_level_accepts_safe_record(self) -> None:
        redaction.assert_safe_top_level("record", {"request_id": "r1", "status": "ok"})

    def test_assert_safe_top_level_ignore_exempts_named_key(self) -> None:
        # Documented escape hatch — exercise it so the parameter stays wired.
        redaction.assert_safe_top_level("record", {"input": "ok"}, ignore={"input"})


class TestSharedGuardAcrossStores(unittest.TestCase):
    """Both durable stores must reject the same vocabulary."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.receipt_db = Path(self.tmp.name) / "receipts.db"
        self.jobs_db = Path(self.tmp.name) / "media_jobs.db"
        receipt_store.init_db(self.receipt_db)
        media_jobs.init_db(self.jobs_db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_receipt_store_rejects_every_forbidden_metadata_key(self) -> None:
        for key in _FORBIDDEN_SAMPLE:
            with self.subTest(key=key):
                r = _make_receipt()
                r.metadata[key] = "DO-NOT-LEAK"
                with self.assertRaises(ValueError) as ctx:
                    receipt_store.record(r, path=self.receipt_db)
                msg = str(ctx.exception)
                self.assertIn(key, msg)
                self.assertNotIn("DO-NOT-LEAK", msg)
        self.assertEqual(receipt_store.count(self.receipt_db), 0)

    def test_receipt_store_rejects_every_forbidden_top_level_key(self) -> None:
        for key in _FORBIDDEN_SAMPLE:
            with self.subTest(key=key):
                data = _make_receipt().to_dict("internal")
                data[key] = "DO-NOT-LEAK"
                with self.assertRaises(ValueError) as ctx:
                    receipt_store.record(data, path=self.receipt_db)
                self.assertIn("top-level", str(ctx.exception))
                self.assertNotIn("DO-NOT-LEAK", str(ctx.exception))
        self.assertEqual(receipt_store.count(self.receipt_db), 0)

    def test_media_job_rejects_every_forbidden_metadata_key(self) -> None:
        for key in _FORBIDDEN_SAMPLE:
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    media_jobs.MediaJob(intent="x", metadata={key: "DO-NOT-LEAK"})
                self.assertIn(key, str(ctx.exception))
                self.assertNotIn("DO-NOT-LEAK", str(ctx.exception))

    def test_media_job_event_payload_rejects_forbidden_key(self) -> None:
        job = media_jobs.create(
            media_jobs.MediaJob(intent="image.generate", request_id="req_x"),
            path=self.jobs_db,
        )
        for key in _FORBIDDEN_SAMPLE:
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    media_jobs.append_event(
                        job.job_id,
                        "progress",
                        {key: "DO-NOT-LEAK"},
                        path=self.jobs_db,
                    )
                self.assertIn(key, str(ctx.exception))
                self.assertNotIn("DO-NOT-LEAK", str(ctx.exception))

    def test_both_stores_accept_safe_diagnostic_metadata(self) -> None:
        safe_meta = {k: f"v{i}" for i, k in enumerate(_SAFE_DIAGNOSTIC_KEYS)}

        r = _make_receipt()
        r.metadata.update(safe_meta)
        rid = receipt_store.record(r, path=self.receipt_db)
        stored = receipt_store.get(rid, path=self.receipt_db)
        self.assertIsNotNone(stored)
        for k in _SAFE_DIAGNOSTIC_KEYS:
            self.assertIn(k, stored["metadata"])  # type: ignore[index]

        job = media_jobs.create(
            media_jobs.MediaJob(
                intent="video.generate",
                request_id="req_safe",
                metadata=dict(safe_meta),
            ),
            path=self.jobs_db,
        )
        fetched = media_jobs.get(job.job_id, path=self.jobs_db)
        self.assertIsNotNone(fetched)
        for k in _SAFE_DIAGNOSTIC_KEYS:
            self.assertIn(k, fetched["metadata"])  # type: ignore[index]

    def test_shared_vocabulary_is_single_source(self) -> None:
        # media_jobs re-exports the shared frozenset under its historical
        # name. Verify they are the same object so future drift is impossible.
        self.assertIs(media_jobs._FORBIDDEN_KEYS, redaction.FORBIDDEN_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
