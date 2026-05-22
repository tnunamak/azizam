"""Tests for local owner password/signing-key helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.local_owner import (
    hash_password,
    load_or_create_signing_key,
    verify_password,
)


class LocalOwnerTests(unittest.TestCase):
    def test_password_verifier_round_trips_without_plaintext(self) -> None:
        verifier = hash_password("correct-horse-battery", iterations=1_000)

        self.assertTrue(verify_password("correct-horse-battery", verifier))
        self.assertFalse(verify_password("wrong-password", verifier))
        self.assertNotIn("correct-horse-battery", verifier)

    def test_password_verifier_rejects_malformed_values(self) -> None:
        for value in ("", "pbkdf2_sha256$v1$bad", "other$v1$1000$aa$bb"):
            self.assertFalse(verify_password("correct-horse-battery", value))

    def test_signing_key_is_persistent_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "admin_session_key"
            first = load_or_create_signing_key(path)
            second = load_or_create_signing_key(path)

            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_signing_key_is_read_as_binary_without_stripping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_session_key"
            raw = (b"x" * 47) + b"\n"
            path.write_bytes(raw)

            self.assertEqual(load_or_create_signing_key(path), raw)


if __name__ == "__main__":
    unittest.main()
