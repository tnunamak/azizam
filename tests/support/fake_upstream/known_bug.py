"""Scope an expected failure to exactly the assertion that shows a known bug.

``@pytest.mark.xfail(strict=True, raises=KnownBug)`` plus ``expect_fixed(...)``
for the one assertion that shows the bug. Every other failure in the test (a
wrong forwarded request, a wrong status, a hang, a setup error) is not a
``KnownBug`` and stays red. The fail-closed teardown runs after the test body,
so a violation still fails the test when the bug is present.
"""

from __future__ import annotations

from typing import Any


class KnownBug(Exception):
    """The bug oracle failed: the gateway still has the known bug."""


def expect_fixed(bug_id: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise KnownBug(f"{bug_id} still present:\n  expected {expected!r}\n  actual   {actual!r}")
