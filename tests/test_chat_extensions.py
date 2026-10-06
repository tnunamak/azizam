"""Tests for chat/responses gateway extension validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.chat_extensions import (  # noqa: E402
    CHAT_EXTENSION_NAMESPACE,
    ROUTING_EXTENSION_NAMESPACE,
    validate_chat_completions_extensions,
    validate_responses_extensions,
)


class TestChatExtensions(unittest.TestCase):
    @staticmethod
    def _request_body(validate, extensions):
        body = {"model": "default", "extensions": extensions}
        if validate is validate_chat_completions_extensions:
            body["messages"] = [{"role": "user", "content": "hello"}]
        else:
            body["input"] = "hello"
        return body

    def test_legacy_namespaces_keep_chat_and_routing_validation_errors(self) -> None:
        validators = (
            validate_chat_completions_extensions,
            validate_responses_extensions,
        )
        for validate in validators:
            with self.subTest(endpoint=validate.__name__):
                invalid = validate(
                    self._request_body(
                        validate,
                        {
                            "com.vividfish.chat": {"receipt_verbosity": "verbose"},
                            "com.vividfish.routing": {"backend": "kobold"},
                        },
                    )
                )

                self.assertFalse(invalid.ok)
                self.assertEqual(
                    invalid.chat.rejected,
                    {
                        "receipt_verbosity": (
                            "receipt_verbosity must be one of: none, summary, detailed"
                        )
                    },
                )
                self.assertEqual(
                    invalid.routing.rejected,
                    {
                        "backend": (
                            "backend is owned by the standard request, route, or policy layer"
                        )
                    },
                )

    def test_valid_legacy_namespaces_are_accepted(self) -> None:
        validators = (
            validate_chat_completions_extensions,
            validate_responses_extensions,
        )
        for validate in validators:
            with self.subTest(endpoint=validate.__name__):
                result = validate(
                    self._request_body(
                        validate,
                        {
                            "com.vividfish.chat": {"receipt_verbosity": "summary"},
                            "com.vividfish.routing": {"local_preference": "local"},
                        },
                    )
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.chat.accepted, {"receipt_verbosity": "summary"})
                self.assertEqual(result.routing.accepted, {"local_preference": "local"})

    def test_canonical_namespaces_win_over_legacy_collisions(self) -> None:
        validators = (
            validate_chat_completions_extensions,
            validate_responses_extensions,
        )
        for validate in validators:
            with self.subTest(endpoint=validate.__name__):
                result = validate(
                    self._request_body(
                        validate,
                        {
                            "com.vividfish.chat": {"receipt_verbosity": "invalid"},
                            "com.azizam.chat": {"receipt_verbosity": "detailed"},
                            "com.vividfish.routing": {"local_preference": "invalid"},
                            "com.azizam.routing": {"local_preference": "local"},
                        },
                    )
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.chat.accepted, {"receipt_verbosity": "detailed"})
                self.assertEqual(result.routing.accepted, {"local_preference": "local"})

                reverse = validate(
                    self._request_body(
                        validate,
                        {
                            "com.vividfish.chat": {"receipt_verbosity": "summary"},
                            "com.azizam.chat": {"receipt_verbosity": "invalid"},
                        },
                    )
                )
                self.assertFalse(reverse.ok)
                self.assertEqual(
                    reverse.chat.rejected,
                    {
                        "receipt_verbosity": (
                            "receipt_verbosity must be one of: none, summary, detailed"
                        )
                    },
                )

    def test_accepts_namespaced_chat_and_routing_controls(self) -> None:
        body = {
            "model": "default",
            "messages": [{"role": "user", "content": "do not log this"}],
            "extensions": {
                CHAT_EXTENSION_NAMESPACE: {"receipt_verbosity": "summary"},
                ROUTING_EXTENSION_NAMESPACE: {"local_preference": "local"},
            },
        }

        result = validate_chat_completions_extensions(body)

        self.assertTrue(result.ok)
        self.assertEqual(result.chat.accepted, {"receipt_verbosity": "summary"})
        self.assertEqual(result.routing.accepted, {"local_preference": "local"})
        self.assertNotIn("extensions", result.request)
        self.assertEqual(result.request["messages"], body["messages"])

    def test_unknown_top_level_gateway_control_is_rejected(self) -> None:
        result = validate_chat_completions_extensions(
            {
                "model": "default",
                "messages": [{"role": "user", "content": "hello"}],
                "receipt_verbosity": "summary",
            }
        )

        self.assertFalse(result.ok)
        self.assertIn("receipt_verbosity", result.unknown_top_level)

    def test_unknown_namespaced_control_is_rejected(self) -> None:
        result = validate_chat_completions_extensions(
            {
                "model": "default",
                "messages": [{"role": "user", "content": "hello"}],
                "extensions": {
                    CHAT_EXTENSION_NAMESPACE: {"trace_prompts": True},
                },
            }
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.chat.rejected, {"trace_prompts": "unknown input"})

    def test_route_and_policy_owned_fields_are_rejected(self) -> None:
        result = validate_chat_completions_extensions(
            {
                "model": "default",
                "messages": [{"role": "user", "content": "hello"}],
                "extensions": {
                    CHAT_EXTENSION_NAMESPACE: {"model": "other"},
                    ROUTING_EXTENSION_NAMESPACE: {
                        "backend": "kobold",
                        "policy_profile": "admin",
                    },
                },
            }
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.chat.rejected,
            {"model": "model is owned by the standard request, route, or policy layer"},
        )
        self.assertEqual(
            result.routing.rejected,
            {
                "backend": "backend is owned by the standard request, route, or policy layer",
                "policy_profile": "policy_profile is owned by the standard request, route, or policy layer",
            },
        )

    def test_responses_uses_same_namespaced_contract(self) -> None:
        result = validate_responses_extensions(
            {
                "model": "default",
                "input": "do not log this",
                "extensions": {
                    CHAT_EXTENSION_NAMESPACE: {"receipt_verbosity": "detailed"},
                },
            }
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.chat.accepted, {"receipt_verbosity": "detailed"})
        self.assertNotIn("extensions", result.request)


if __name__ == "__main__":
    unittest.main()
