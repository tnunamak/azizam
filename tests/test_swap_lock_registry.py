"""Tests for the generic swap-lock registry introduced in Step 0 of the
engine-registry migration (docs/design/ENGINE_REGISTRY_DESIGN.md §8).

Covers:
- swap_lock_for(id) returns the same lock object on repeated calls (identity).
- swap_lock_for("a") is not swap_lock_for("b") (per-service isolation).
- Concurrent first-access for the same id does not create two distinct locks
  (only one lock object is ever stored for a given key).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class TestSwapLockRegistry(unittest.TestCase):
    def setUp(self) -> None:
        """Clear the registry before each test so tests are independent."""
        proxy._service_swap_locks.clear()

    # ------------------------------------------------------------------
    # Identity: same id → same object
    # ------------------------------------------------------------------

    def test_same_id_returns_same_lock_object(self) -> None:
        lock1 = proxy.swap_lock_for("kobold")
        lock2 = proxy.swap_lock_for("kobold")
        self.assertIs(lock1, lock2, "swap_lock_for must return the identical lock object on repeat calls")

    def test_same_id_returns_same_object_after_many_calls(self) -> None:
        locks = [proxy.swap_lock_for("tabby") for _ in range(50)]
        self.assertEqual(len(set(id(l) for l in locks)), 1, "all calls for 'tabby' must return the same lock")

    # ------------------------------------------------------------------
    # Isolation: different ids → different objects
    # ------------------------------------------------------------------

    def test_different_ids_return_different_locks(self) -> None:
        lock_a = proxy.swap_lock_for("kobold")
        lock_b = proxy.swap_lock_for("tabby")
        self.assertIsNot(lock_a, lock_b, "different service ids must have distinct locks")

    def test_arbitrary_ids_are_isolated(self) -> None:
        ids = ["svc-alpha", "svc-beta", "svc-gamma"]
        locks = [proxy.swap_lock_for(i) for i in ids]
        # All distinct
        for i, la in enumerate(locks):
            for j, lb in enumerate(locks):
                if i != j:
                    self.assertIsNot(la, lb, f"locks[{i}] and locks[{j}] must be different objects")

    # ------------------------------------------------------------------
    # Concurrent first-access: only one lock is created per id
    # ------------------------------------------------------------------

    def test_concurrent_first_access_produces_single_lock(self) -> None:
        """Two coroutines racing to call swap_lock_for for the same id must
        end up with the same lock object.  Uses asyncio.gather to maximise
        interleaving within a single event-loop tick."""

        async def _race(service_id: str, results: list) -> None:
            results.append(proxy.swap_lock_for(service_id))

        async def _run() -> None:
            results: list = []
            # Schedule both coroutines concurrently; they execute in the same
            # event loop so the GIL + setdefault() gives us the safety guarantee.
            await asyncio.gather(
                _race("race-target", results),
                _race("race-target", results),
                _race("race-target", results),
                _race("race-target", results),
            )
            self.assertEqual(len(results), 4)
            first = results[0]
            for lock in results[1:]:
                self.assertIs(lock, first, "concurrent first-access must return the same lock object")

        asyncio.run(_run())

    # ------------------------------------------------------------------
    # Lock is a real asyncio.Lock (acquirable / releasable)
    # ------------------------------------------------------------------

    def test_returned_lock_is_asyncio_lock(self) -> None:
        lock = proxy.swap_lock_for("new-service")
        self.assertIsInstance(lock, asyncio.Lock)

    def test_lock_can_be_acquired_and_released(self) -> None:
        async def _acquire_release() -> None:
            lock = proxy.swap_lock_for("acquire-test")
            async with lock:
                self.assertTrue(lock.locked())
            self.assertFalse(lock.locked())

        asyncio.run(_acquire_release())


if __name__ == "__main__":
    unittest.main()
