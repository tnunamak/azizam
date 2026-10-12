from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from gateway.backends.cancellation import CancellationRegistry, CancellationRegistryFull


class CancellationRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_then_signal_wakes_waiter(self) -> None:
        registry = CancellationRegistry()
        handle = registry.register("req-1")
        self.assertFalse(handle.token.cancelled)

        waiter_entered = asyncio.Event()
        waited = asyncio.Event()

        async def waiter() -> None:
            waiter_entered.set()
            await handle.token.wait()
            waited.set()

        task = asyncio.create_task(waiter())
        try:
            await asyncio.wait_for(waiter_entered.wait(), timeout=1)
            self.assertFalse(waited.is_set())

            registry.signal("req-1")
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(handle.token.cancelled)
            self.assertTrue(waited.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            handle.release()

    async def test_signal_before_register_is_observed(self) -> None:
        registry = CancellationRegistry()
        registry.signal("req-2")
        handle = registry.register("req-2")
        self.assertTrue(handle.token.cancelled)

    async def test_duplicate_register_returns_tokens_woken_by_same_signal(self) -> None:
        registry = CancellationRegistry()
        handle_a = registry.register("req-3")
        handle_b = registry.register("req-3")
        registry.signal("req-3")
        self.assertTrue(handle_a.token.cancelled)
        self.assertTrue(handle_b.token.cancelled)

    async def test_normal_completion_without_signal_leaves_token_uncancelled(self) -> None:
        registry = CancellationRegistry()
        handle = registry.register("req-5")
        handle.release()
        self.assertFalse(handle.token.cancelled)

    async def test_signal_is_idempotent(self) -> None:
        registry = CancellationRegistry()
        handle = registry.register("req-6")
        registry.signal("req-6")
        registry.signal("req-6")
        self.assertTrue(handle.token.cancelled)

    async def test_signal_empty_id_is_noop(self) -> None:
        registry = CancellationRegistry()
        registry.signal("")
        self.assertFalse(registry.is_cancelled(""))

    async def test_expired_entry_is_swept_and_register_starts_fresh(self) -> None:
        registry = CancellationRegistry(ttl_seconds=-1.0)
        registry.signal("req-7")
        self.assertTrue(registry.is_cancelled("req-7"))
        # Any subsequent register/signal call sweeps the (already-expired) entry.
        handle = registry.register("req-8")
        self.assertFalse(handle.token.cancelled)
        self.assertFalse(registry.is_cancelled("req-7"))

    async def test_distinct_ids_do_not_cross_signal(self) -> None:
        registry = CancellationRegistry()
        handle_a = registry.register("req-a")
        handle_b = registry.register("req-b")
        registry.signal("req-a")
        self.assertTrue(handle_a.token.cancelled)
        self.assertFalse(handle_b.token.cancelled)


class CancellationRegistryOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Duplicate outer/internal registration of the same id must be release-safe."""

    async def test_stale_handle_release_after_full_release_and_reregister_is_noop(self) -> None:
        registry = CancellationRegistry()
        first = registry.register("req-stale")
        first.release()
        second = registry.register("req-stale")

        # A duplicate/late release of the first (already fully-released and
        # replaced) handle must not disturb the second handle's fresh entry.
        first.release()
        self.assertFalse(second.token.cancelled)
        registry.signal("req-stale")
        self.assertTrue(second.token.cancelled)

    async def test_release_is_idempotent_per_handle(self) -> None:
        registry = CancellationRegistry()
        handle = registry.register("req-idem")
        handle.release()
        handle.release()  # must not raise or double-decrement another entry
        fresh = registry.register("req-idem")
        self.assertFalse(fresh.token.cancelled)

    async def test_outer_release_keeps_shared_entry_signalable_for_inner_waiter(self) -> None:
        # Outer finishes first (e.g. the non-stream path completes before the
        # internal LiteLLM hop's reader has released). Its release must not
        # detach or delete the inner handle's still-live waiter.
        registry = CancellationRegistry()
        outer = registry.register("req-refcount")
        inner = registry.register("req-refcount")
        outer.release()
        self.assertTrue(registry.is_cancelled("req-refcount") is False)
        # Entry still live (inner holds it) — a signal must still reach inner.
        registry.signal("req-refcount")
        self.assertTrue(inner.token.cancelled)
        inner.release()

    async def test_cancel_before_register_tombstone_survives_a_sibling_release(self) -> None:
        registry = CancellationRegistry()
        outer = registry.register("req-tombstone")
        # Internal registration races behind a signal that already fired
        # against the (not yet arrived) internal id — simulate by signalling
        # a second, not-yet-registered id and then releasing the sibling.
        registry.signal("req-tombstone-late")
        outer.release()
        late = registry.register("req-tombstone-late")
        self.assertTrue(late.token.cancelled)

    async def test_signal_then_release_by_the_only_handle_leaves_a_tombstone(self) -> None:
        """Regression: outer route signals then immediately releases its own
        span (pre-header/nonstream disconnect handling calls signal() then
        aclose() in the same breath). A slower internal-hop register() for
        the same id — racing right behind it — must still observe the
        cancellation rather than finding a freshly wiped, uncancelled entry."""
        registry = CancellationRegistry()
        outer = registry.register("req-signal-then-release")
        registry.signal("req-signal-then-release")
        outer.release()

        self.assertTrue(registry.is_cancelled("req-signal-then-release"))
        late = registry.register("req-signal-then-release")
        self.assertTrue(late.token.cancelled)

    async def test_unsignalled_release_still_drops_the_entry(self) -> None:
        """A normal (non-cancelled) completion must not leak a tombstone."""
        registry = CancellationRegistry()
        handle = registry.register("req-clean")
        handle.release()
        self.assertFalse(registry.is_cancelled("req-clean"))
        fresh = registry.register("req-clean")
        self.assertFalse(fresh.token.cancelled)


class CancellationRegistryBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_is_enforced_without_evicting_live_entries(self) -> None:
        registry = CancellationRegistry(max_entries=2)
        first = registry.register("req-1")
        registry.register("req-2")
        with self.assertRaises(CancellationRegistryFull):
            registry.register("req-3")
        # Existing live entries must be untouched by the failed attempt.
        self.assertFalse(first.token.cancelled)
        registry.signal("req-1")
        self.assertTrue(first.token.cancelled)

    async def test_capacity_check_does_not_alias_a_reused_id(self) -> None:
        registry = CancellationRegistry(max_entries=1)
        handle = registry.register("req-a")
        handle.release()
        # Table is empty again; registering a different id must succeed and
        # must not carry over any state from "req-a".
        other = registry.register("req-b")
        self.assertFalse(other.token.cancelled)

    async def test_duplicate_register_of_existing_id_does_not_count_against_capacity(self) -> None:
        registry = CancellationRegistry(max_entries=1)
        first = registry.register("req-shared")
        second = registry.register("req-shared")  # same id, must not raise
        self.assertFalse(second.token.cancelled)
        first.release()
        second.release()

    async def test_signal_for_unknown_id_honors_capacity(self) -> None:
        """signal() on ids nobody ever registers must not grow the table
        unboundedly — only register() call sites are trusted to register
        their own id before it could be signalled."""
        registry = CancellationRegistry(max_entries=1)
        registry.signal("ghost-a")
        registry.signal("ghost-b")
        self.assertEqual(len(registry._entries), 1)

    async def test_signal_for_unknown_id_at_capacity_does_not_disturb_live_entry(self) -> None:
        registry = CancellationRegistry(max_entries=1)
        live = registry.register("req-live")
        registry.signal("ghost")  # dropped: table full, "ghost" has no entry
        self.assertFalse(live.token.cancelled)
        self.assertNotIn("ghost", registry._entries)


class CancellationRegistryLiveDurationTests(unittest.IsolatedAsyncioTestCase):
    """A live entry must never TTL-expire out from under its holder — an
    upstream read can legitimately run far past the configured TTL (see
    UPSTREAM_READ_TIMEOUT=1800s in proxy_config.yml); only a fully-released
    (zero-handle) entry is actually expired."""

    async def test_live_handle_survives_ttl_deadline_past_1800_seconds(self) -> None:
        registry = CancellationRegistry(ttl_seconds=-1800.0 - 1.0)
        handle = registry.register("req-long-running")
        # Deadline is already (far) in the past the instant this entry is
        # created — simulates a read that has been in flight past any
        # realistic TTL. A sweep triggered by unrelated traffic must not
        # touch it while the handle is still live.
        registry.register("req-other-traffic")
        self.assertIn("req-long-running", registry._entries)
        self.assertFalse(handle.token.cancelled)
        registry.signal("req-long-running")
        self.assertTrue(handle.token.cancelled)
        handle.release()

    async def test_entry_expires_only_after_last_handle_releases(self) -> None:
        registry = CancellationRegistry(ttl_seconds=-1.0)
        handle = registry.register("req-two-owners")
        other = registry.register("req-two-owners")
        registry.register("req-sweep-trigger-1")
        self.assertIn("req-two-owners", registry._entries)
        handle.release()
        registry.register("req-sweep-trigger-2")
        # Still one live handle outstanding.
        self.assertIn("req-two-owners", registry._entries)
        other.release()
        registry.register("req-sweep-trigger-3")
        self.assertNotIn("req-two-owners", registry._entries)


class CancellationRegistryPositiveTTLTests(unittest.TestCase):
    def test_unowned_signal_expires_at_positive_ttl_boundary(self) -> None:
        for now, cancelled in ((109.0, True), (110.0, False), (111.0, False)):
            with self.subTest(now=now, cancelled=cancelled):
                with patch("gateway.backends.cancellation.time") as clock:
                    clock.monotonic.return_value = 100.0
                    registry = CancellationRegistry(ttl_seconds=10.0)
                    registry.signal("req-boundary")
                    self.assertTrue(registry.is_cancelled("req-boundary"))

                    clock.monotonic.return_value = now
                    handle = registry.register("req-boundary")
                    try:
                        self.assertEqual(handle.token.cancelled, cancelled)
                        self.assertEqual(registry.is_cancelled("req-boundary"), cancelled)
                    finally:
                        handle.release()

    def test_positive_ttl_never_evicts_live_owners(self) -> None:
        with patch("gateway.backends.cancellation.time") as clock:
            clock.monotonic.return_value = 100.0
            registry = CancellationRegistry(ttl_seconds=10.0)
            first = registry.register("req-live")
            second = registry.register("req-live")
            try:
                clock.monotonic.return_value = 2001.0
                traffic = registry.register("req-traffic")
                traffic.release()
                self.assertIn("req-live", registry._entries)
                self.assertFalse(first.token.cancelled)
                self.assertFalse(second.token.cancelled)

                first.release()
                registry.signal("req-live")
                self.assertTrue(second.token.cancelled)
                second.release()
                self.assertTrue(registry.is_cancelled("req-live"))

                fresh = registry.register("req-live")
                try:
                    self.assertFalse(fresh.token.cancelled)
                    self.assertNotEqual(fresh._generation, second._generation)
                finally:
                    fresh.release()
            finally:
                first.release()
                second.release()


class CancellationRegistryGenerationIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Generation identity must be globally unique per entry, not reset to 0
    for every id's first entry — otherwise a stale handle from an id's Nth
    entry can collide with that same id's (N+2)th entry once both happen to
    be freshly created (see docstring on CancellationRegistry._new_generation)."""

    async def test_generation_is_not_reused_across_full_delete_recreate_cycles(self) -> None:
        registry = CancellationRegistry()
        first = registry.register("req-cycle")
        first.release()
        second = registry.register("req-cycle")
        second.release()
        third = registry.register("req-cycle")
        self.assertNotEqual(first._generation, third._generation)
        self.assertNotEqual(second._generation, third._generation)

    async def test_stale_handle_cannot_release_a_same_id_replacement_entry(self) -> None:
        registry = CancellationRegistry()
        first = registry.register("req-cycle-2")
        first.release()
        third = registry.register("req-cycle-2")
        # A hypothetical duplicate/delayed release carrying the first
        # handle's (stale) generation must not be able to touch the entry
        # current holders (third) depend on.
        registry._release("req-cycle-2", first._generation)
        self.assertIn("req-cycle-2", registry._entries)
        registry.signal("req-cycle-2")
        self.assertTrue(third.token.cancelled)

    async def test_duplicate_handles_for_same_registration_share_generation(self) -> None:
        registry = CancellationRegistry()
        outer = registry.register("req-dup-gen")
        inner = registry.register("req-dup-gen")
        self.assertEqual(outer._generation, inner._generation)
        outer.release()
        self.assertIn("req-dup-gen", registry._entries)
        inner.release()
        self.assertNotIn("req-dup-gen", registry._entries)


if __name__ == "__main__":
    unittest.main()
