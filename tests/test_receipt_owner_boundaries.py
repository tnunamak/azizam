"""Receipt-owner contract tests independent of individual transport fakes."""

from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt  # noqa: E402


ROUTE_OWNER_FUNCTIONS = {
    "create_response",
    "provider_native_request",
    "_forward_current_loaded_openai_request",
    "_forward_litellm_proxy_request",
    "proxy_request",
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


class ReceiptLifecycleOwnerMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_matrix_is_single_writer_and_preserves_exception_semantics(self) -> None:
        """Every owner exit has one terminal CAS attempt and no later effect."""
        cases = (
            ("success", None, "succeeded", None),
            ("upstream-error", RuntimeError("upstream"), "failed", RuntimeError),
            ("timeout", TimeoutError("slow"), "failed", TimeoutError),
            ("disconnect", asyncio.CancelledError(), "cancelled", asyncio.CancelledError),
            ("base-exception", KeyboardInterrupt("stop"), "failed", KeyboardInterrupt),
        )

        for name, error, expected_status, expected_exception in cases:
            with self.subTest(name=name):
                begun: list[RouteReceipt] = []
                finalized: list[RouteReceipt] = []
                effects: list[str] = []
                receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
                owner = ReceiptLifecycleOwner(
                    receipt,
                    begin=begun.append,
                    finalize=lambda item: finalized.append(item) or True,
                    log=lambda _message: None,
                )

                async def route_body() -> None:
                    async with owner:
                        if error is not None:
                            raise error
                        owner.report(status="succeeded", reason="completed")
                    effects.append("after-owner")

                if expected_exception is None:
                    await route_body()
                    self.assertEqual(effects, ["after-owner"])
                else:
                    with self.assertRaises(expected_exception):
                        await route_body()
                    self.assertEqual(effects, [])

                # A later report is descriptive only: the terminal CAS attempt
                # remains exactly once and always targets the admitted receipt.
                owner.report(status="succeeded", reason="late")
                self.assertEqual(begun, [receipt])
                self.assertEqual(finalized, [receipt])
                self.assertEqual(receipt.status, expected_status)

    async def test_stream_matrix_owns_iteration_and_cleanup_failures(self) -> None:
        cases = (
            ("stream-success", None, None, "succeeded"),
            ("stream-cancelled", asyncio.CancelledError(), asyncio.CancelledError, "cancelled"),
            ("stream-base-exception", KeyboardInterrupt("stop"), KeyboardInterrupt, "failed"),
        )

        for name, error, expected_exception, expected_status in cases:
            with self.subTest(name=name):
                finalized: list[RouteReceipt] = []
                receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate", stream=True)
                owner = ReceiptLifecycleOwner(
                    receipt,
                    begin=lambda _item: None,
                    finalize=lambda item: finalized.append(item) or True,
                    log=lambda _message: None,
                )

                async def source():
                    if error is not None:
                        raise error
                    yield b"chunk"
                    owner.report(status="succeeded", reason="completed")

                async with owner:
                    stream = owner.handoff_stream(source())

                if expected_exception is None:
                    self.assertEqual([item async for item in stream], [b"chunk"])
                else:
                    with self.assertRaises(expected_exception):
                        async for _item in stream:
                            pass
                self.assertEqual(finalized, [receipt])
                self.assertEqual(receipt.status, expected_status)


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
