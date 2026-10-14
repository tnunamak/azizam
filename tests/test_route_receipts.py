"""Standalone tests for route receipt diagnostics."""

from __future__ import annotations

import sys
import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import receipt_store  # noqa: E402
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt, record_validation_result  # noqa: E402


class FakeValidationResult:
    accepted = {"denoise": 0.35}
    rejected = {"surprise": "unknown input"}


class TestRouteReceipt(unittest.TestCase):
    def test_receipt_records_inputs_without_values(self) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            path="/v1/images/edits",
            intent="image.edit",
            app_profile="image-agent",
            requested_workflow="image.edit.controlnet.example",
            backend="comfyui.local",
        )

        receipt.set_policy_decision(
            "allowed",
            reason="profile permits image edit workflows",
            source="profile:image-agent",
        )
        receipt.attach_manifest(
            manifest_id="image.edit.controlnet.example",
            manifest_version="0.1.0",
            manifest_hash="abc123",
        )
        record_validation_result(receipt, FakeValidationResult())
        receipt.ignore_input("seed", "backend selected deterministic seed")
        receipt.add_warning("fallback disabled by profile")
        receipt.job_id = "job_123"
        receipt.finish("rejected", latency_ms=12.34567)

        data = receipt.to_dict()
        self.assertTrue(data["request_id"].startswith("req_"))
        self.assertEqual(data["accepted_inputs"], ["denoise"])
        self.assertEqual(data["rejected_inputs"], {"surprise": "unknown input"})
        self.assertEqual(data["ignored_inputs"], {"seed": "backend selected deterministic seed"})
        self.assertEqual(data["warnings"], ["fallback disabled by profile"])
        self.assertEqual(data["policy_decision"], "allowed")
        self.assertEqual(data["manifest_id"], "image.edit.controlnet.example")
        self.assertEqual(data["manifest_hash"], "abc123")
        self.assertEqual(data["job_id"], "job_123")
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["latency_ms"], 12.346)
        self.assertNotIn("0.35", str(data))

        client_data = receipt.to_dict(audience="client")
        self.assertNotIn("api_key_name", client_data)
        self.assertNotIn("backend", client_data)
        self.assertNotIn("policy_sources", client_data)
        self.assertNotIn("manifest_hash", client_data)
        self.assertEqual(client_data["job_id"], "job_123")

    def test_input_state_is_single_valued(self) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")

        receipt.reject_input("reasoning_effort", "unsupported")
        receipt.accept_input("reasoning_effort")
        receipt.ignore_input("reasoning_effort", "profile strips it")

        self.assertEqual(receipt.accepted_inputs, [])
        self.assertEqual(receipt.rejected_inputs, {})
        self.assertEqual(receipt.ignored_inputs, {"reasoning_effort": "profile strips it"})

    def test_invalid_policy_decision_fails_fast(self) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
        with self.assertRaises(ValueError):
            receipt.set_policy_decision("maybe")


class TestReceiptLifecycleOwner(unittest.IsolatedAsyncioTestCase):
    async def test_default_store_wiring_begins_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "receipts.db"
            receipt_store.init_db(db)
            receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
            real_begin, real_finalize = receipt_store.begin, receipt_store.finalize
            real_phases = receipt_store.update_phases
            # The store functions bind ``path`` at definition time, so the
            # spies route to this temporary store explicitly.
            with patch.object(
                receipt_store, "begin", side_effect=lambda r: real_begin(r, path=db)
            ) as begun, patch.object(
                receipt_store, "finalize", side_effect=lambda r: real_finalize(r, path=db)
            ) as finalized, patch.object(
                receipt_store, "update_phases", side_effect=lambda r: real_phases(r, path=db)
            ):
                owner = ReceiptLifecycleOwner(receipt)
                async with owner:
                    owner.report(status="succeeded", reason="completed")

            begun.assert_called_once_with(receipt)
            finalized.assert_called_once_with(receipt)
            self.assertEqual(receipt_store.count(db), 1)
            persisted = receipt_store.get(receipt.request_id, audience="internal", path=db)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted["request_id"], receipt.request_id)
            self.assertEqual(persisted["status"], "succeeded")
            self.assertEqual(persisted["metadata"]["terminal_reason"], "completed")

    async def test_cancellation_wins_and_store_errors_do_not_mask_it(self) -> None:
        begun: list[RouteReceipt] = []
        finalized: list[RouteReceipt] = []
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
        owner = ReceiptLifecycleOwner(
            receipt,
            begin=begun.append,
            finalize=lambda item: finalized.append(item) or (_ for _ in ()).throw(OSError("disk full")),
            log=lambda _message: None,
        )

        async def cancelled() -> None:
            async with owner:
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await cancelled()
        self.assertEqual(begun, [receipt])
        self.assertEqual(finalized, [receipt])
        self.assertEqual(receipt.status, "cancelled")
        self.assertEqual(receipt.metadata["terminal_reason"], "cancelled")

    async def test_normal_outcome_is_written_once_after_stream_handoff(self) -> None:
        finalized: list[RouteReceipt] = []
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate", stream=True)
        owner = ReceiptLifecycleOwner(
            receipt, begin=lambda _item: None, finalize=finalized.append, log=lambda _message: None
        )

        async def body():
            yield b"one"
            owner.report(status="succeeded", reason="completed")

        async with owner:
            stream = owner.handoff_stream(body())
        self.assertEqual([item async for item in stream], [b"one"])
        self.assertEqual(finalized, [receipt])
        self.assertEqual(receipt.status, "succeeded")


def test_category_failure_does_not_log_prompt_or_credentials(caplog) -> None:
    """Backend errors containing request data retain only operational facts."""
    import logging
    from unittest.mock import Mock

    from gateway.category_enforcement import CategoryGate

    prompt = "PRIVATE_PROMPT: a clockwork fox walks through a violet orchard"
    sensitive = prompt + " key=fake-test-key person@example.invalid https://example.invalid/?token=fake"
    for error_type in (ValueError, RuntimeError):
        caplog.clear()
        gate = CategoryGate(Path("unused-category-store.db"))
        gate._store = Mock()
        gate._store.active_revision_counter = 1
        gate._store.get_active.side_effect = error_type(sensitive)
        gate._watch = Mock()
        gate._watch.execute.return_value.fetchone.return_value = (1,)
        with patch.object(Path, "exists", return_value=True):
            with caplog.at_level(logging.DEBUG, logger="gateway.category_enforcement"):
                assert gate._active_now() == (None, True)
        assert "category rules unavailable" in caplog.text
        assert error_type.__name__ in caplog.text
        for value in (prompt, "fake-test-key", "person@example.invalid", "?token=fake"):
            assert value not in caplog.text
        records = [record for record in caplog.records
                   if record.name == "gateway.category_enforcement"]
        assert records
        assert all(record.exc_info is None for record in records)
        assert gate._failed


if __name__ == "__main__":
    unittest.main(verbosity=2)
