"""Receipt-owner contract tests independent of individual transport fakes."""

from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import receipt_store  # noqa: E402
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt  # noqa: E402


ROUTE_OWNER_FUNCTIONS = {
    "create_response",
    "provider_native_request",
    "_forward_current_loaded_openai_request",
    "_forward_litellm_proxy_request",
    "proxy_stt_request",
}


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _calls(node: ast.AST, dotted_name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _dotted_name(child.func) == dotted_name
    ]


@pytest.mark.parametrize("error_type,expected_status", [
    pytest.param(None, "succeeded", id="success"),
    pytest.param(RuntimeError, "failed", id="upstream-error"),
    pytest.param(TimeoutError, "failed", id="timeout"),
    pytest.param(asyncio.CancelledError, "cancelled", id="disconnect"),
    pytest.param(KeyboardInterrupt, "failed", id="base-exception"),
])
def test_terminal_owner_contract(error_type, expected_status):
    async def run():
        begun, finalized, effects = [], [], []
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
        owner = ReceiptLifecycleOwner(
            receipt, begin=begun.append,
            finalize=lambda item: finalized.append(item) or True,
            log=lambda _message: None,
        )

        async def route_body():
            async with owner:
                if error_type is not None:
                    raise error_type("terminal test")
                owner.report(status="succeeded", reason="completed")
            effects.append("after-owner")

        if error_type is None:
            await route_body()
            assert effects == ["after-owner"]
        else:
            # Catch BaseException subclasses inside this coroutine, rather
            # than allowing KeyboardInterrupt to escape the event loop.
            with pytest.raises(error_type):
                await route_body()
            assert effects == []
        owner.report(status="succeeded", reason="late")
        assert begun == [receipt]
        assert finalized == [receipt]
        assert receipt.status == expected_status

    asyncio.run(run())


@pytest.mark.parametrize("error_type,expected_status", [
    pytest.param(None, "succeeded", id="stream-success"),
    pytest.param(asyncio.CancelledError, "cancelled", id="stream-cancelled"),
    pytest.param(KeyboardInterrupt, "failed", id="stream-base-exception"),
])
def test_stream_owner_contract(error_type, expected_status):
    async def run():
        finalized = []
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate", stream=True)
        owner = ReceiptLifecycleOwner(
            receipt, begin=lambda _item: None,
            finalize=lambda item: finalized.append(item) or True,
            log=lambda _message: None,
        )

        async def source():
            if error_type is not None:
                raise error_type("stream test")
            yield b"chunk"
            owner.report(status="succeeded", reason="completed")

        async with owner:
            stream = owner.handoff_stream(source())
        assert finalized == [], "stream must remain open until iteration"
        if error_type is None:
            assert [item async for item in stream] == [b"chunk"]
        else:
            with pytest.raises(error_type):
                async for _item in stream:
                    pass
        assert finalized == [receipt]
        assert receipt.status == expected_status

    asyncio.run(run())


def test_stream_cancellation_persists_terminal_receipt():
    async def run(db):
        entered, release = asyncio.Event(), asyncio.Event()
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate", stream=True)
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

            async def source():
                entered.set()
                await release.wait()
                yield b"unreleased"
                owner.report(status="succeeded", reason="completed")

            async with owner:
                stream = owner.handoff_stream(source())
            finalized.assert_not_called()

            async def consume():
                return [item async for item in stream]

            consumer = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                finalized.assert_not_called()
                consumer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await consumer
                begun.assert_called_once_with(receipt)
                finalized.assert_called_once_with(receipt)
                assert receipt_store.count(db) == 1
                persisted = receipt_store.get(receipt.request_id, audience="internal", path=db)
                assert persisted is not None
                assert persisted["status"] == "cancelled"
                assert persisted["metadata"]["terminal_reason"] == "cancelled"
                owner.report(status="succeeded", reason="late")
                assert receipt.status == "cancelled"
                finalized.assert_called_once_with(receipt)
                assert receipt_store.get(receipt.request_id, audience="internal", path=db) == persisted
            finally:
                release.set()
                if not consumer.done():
                    consumer.cancel()
                try:
                    await consumer
                except asyncio.CancelledError:
                    pass
                await stream.aclose()

    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "receipts.db"
        receipt_store.init_db(db)
        asyncio.run(run(db))


class ReceiptRouteOwnershipSourceTests(unittest.TestCase):
    def test_migrated_routes_delegate_receipt_persistence_and_terminal_mapping_to_owner(self) -> None:
        """Routes may report normal outcomes, but never persist or terminal-map."""
        proxy_tree = ast.parse((ROOT / "proxy.py").read_text())
        functions = {
            node.name: node
            for node in proxy_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(ROUTE_OWNER_FUNCTIONS - functions.keys(), set())
        removed_route_helpers = [
            node.name
            for node in proxy_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and "receipt" in node.name
            and node.name.startswith(("_begin", "_finalize", "_finish"))
        ]
        self.assertEqual(removed_route_helpers, [])

        for name in ROUTE_OWNER_FUNCTIONS:
            function = functions[name]
            with self.subTest(route=name):
                self.assertEqual(_calls(function, "receipt_store.begin"), [])
                self.assertEqual(_calls(function, "receipt_store.finalize"), [])
                self.assertEqual(_calls(function, "_begin_route_receipt"), [])
                self.assertEqual(_calls(function, "_finalize_route_receipt"), [])
                self.assertEqual(_calls(function, "_finish_route_receipt"), [])

                for handler in (node for node in ast.walk(function) if isinstance(node, ast.ExceptHandler)):
                    exception_name = _dotted_name(handler.type) if handler.type is not None else ""
                    if exception_name in {"BaseException", "asyncio.CancelledError", "CancelledError"}:
                        self.assertEqual(
                            _calls(handler, "owner.report"), [],
                            f"{name} terminalizes {exception_name} outside ReceiptLifecycleOwner",
                        )

        owner_tree = ast.parse((ROOT / "gateway" / "receipts.py").read_text())
        owner_classes = [node for node in owner_tree.body if isinstance(node, ast.ClassDef) and node.name == "ReceiptLifecycleOwner"]
        self.assertEqual(len(owner_classes), 1)
        owner_class = owner_classes[0]
        self.assertTrue(_calls(owner_class, "self._begin"))
        self.assertTrue(_calls(owner_class, "self._finalize"))
