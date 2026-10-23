"""Ownership cut-over for App keys and trusted addresses (owner visibility step 2, design §8.1).

Stub: the accept and apply commands are written after their red tests (tests/test_app_binding_cutover.py).
"""

from __future__ import annotations


def record_acceptance(conn, digest: str, accepted_by: str) -> None:
    raise NotImplementedError("accept is not built yet")


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("the cut-over command is not built yet")
