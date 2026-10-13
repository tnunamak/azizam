"""State a test sets on a shared module must not reach the next test.

The pairs below only prove isolation when pytest runs them in file order, which
is the default without a shuffle plugin; under a shuffle the second test passes
either way, so a failure here is always a real leak.
"""

from __future__ import annotations

from gateway import media_delivery


def test_a_begin_delivery_without_a_request_scope() -> None:
    media_delivery.begin(plan_bound=None)
    assert media_delivery.current() is not None


def test_b_next_test_sees_no_delivery_context() -> None:
    assert media_delivery.current() is None
