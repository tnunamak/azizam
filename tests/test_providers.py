"""Remote provider policy and spend ledger tests."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402
from gateway.policy import DEFAULT_PROFILE, RemoteProviderPolicy  # noqa: E402
from gateway.policy import PolicyRequest, evaluate  # noqa: E402
from gateway.providers import (  # noqa: E402
    BudgetExceeded,
    Cost,
    ProviderAuthFailed,
    ProviderError,
    ProviderKeyMissing,
    ProviderRateLimited,
    ProviderRef,
    Usage,
    attach_cost_to_receipt,
    call_typesafe_systemone,
    ensure_ledger,
    estimate_chat_cost,
    estimate_request_cost,
    estimate_typesafe_cost,
    extract_provider_cost,
    provider_lookup,
    release_provider_spend,
    reserve_provider_spend,
    settle_provider_spend,
)
from gateway.receipts import RouteReceipt  # noqa: E402


def provider(**overrides: object) -> ProviderRef:
    data = {
        "id": "typesafe",
        "base_url": "https://api.typesafe.test",
        "protocols": ("typesafe-systemone",),
        "api_key_env": "TYPESAFE_TEST_KEY",
        "cost_class": "paid-remote",
        "price": {"input_usd_per_million_tokens": "0.10"},
        "config": {},
    }
    data.update(overrides)
    return ProviderRef(**data)  # type: ignore[arg-type]


def profile(**overrides: object):
    remote = RemoteProviderPolicy(providers=frozenset({"typesafe"}), **overrides)
    return DEFAULT_PROFILE.with_overrides(id="paid-plan", remote=remote)


class TestProviderLookup(unittest.TestCase):
    def test_provider_lookup_reads_normalized_registry_record(self) -> None:
        registry = {
            "services": {
                "typesafe": {
                    "id": "typesafe",
                    "kind": "provider",
                    "capabilities": ["typesafe-systemone"],
                    "config": {
                        "base_url": "https://api.typesafe.ai",
                        "api_key_env": "TYPESAFE_API_KEY",
                        "cost_class": "paid-remote",
                        "price": {"input_usd_per_million_tokens": "0.042"},
                    },
                }
            }
        }

        ref = provider_lookup(registry, "typesafe", protocol="typesafe-systemone")

        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.api_key_env, "TYPESAFE_API_KEY")
        self.assertEqual(ref.base_url, "https://api.typesafe.ai")

    def test_provider_lookup_respects_protocol(self) -> None:
        registry = {
            "services": {
                "typesafe": {
                    "id": "typesafe",
                    "kind": "provider",
                    "capabilities": ["openai-chat"],
                    "config": {"api_key_env": "TYPESAFE_API_KEY"},
                }
            }
        }

        self.assertIsNone(
            provider_lookup(registry, "typesafe", protocol="typesafe-systemone")
        )

    def test_provider_lookup_carries_disabled_state(self) -> None:
        registry = {
            "services": {
                "typesafe": {
                    "id": "typesafe",
                    "kind": "provider",
                    "enabled": False,
                    "capabilities": ["typesafe-systemone"],
                    "config": {"api_key_env": "TYPESAFE_API_KEY"},
                }
            }
        }

        ref = provider_lookup(registry, "typesafe")

        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertFalse(ref.enabled)


class TestSpendReservations(unittest.TestCase):
    def test_concurrent_requests_cannot_reserve_the_same_remaining_budget(self) -> None:
        def reserve():
            try:
                reserve_provider_spend(profile(budget_usd_per_day=Decimal("0.01")), provider(),
                    Cost(Decimal("0.006"), "price-table", "typesafe"), path=self.ledger)
                return "allowed"
            except BudgetExceeded:
                return "denied"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sorted(results), ["allowed", "denied"])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.db"
        self.admin_db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.admin_db)
        os.environ["TYPESAFE_TEST_KEY"] = "test-key"

    def tearDown(self) -> None:
        os.environ.pop("TYPESAFE_TEST_KEY", None)
        self.tmp.cleanup()

    def test_default_profile_denies_paid_provider_before_ledger_write(self) -> None:
        with self.assertRaises(Exception) as cm:
            reserve_provider_spend(
                DEFAULT_PROFILE,
                provider(),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
            )

        self.assertIn("remote provider", str(cm.exception))
        self.assertFalse(self.ledger.exists())

    def test_missing_key_fails_closed(self) -> None:
        os.environ.pop("TYPESAFE_TEST_KEY", None)

        with self.assertRaises(ProviderKeyMissing):
            reserve_provider_spend(
                profile(),
                provider(),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
            )

    def test_disabled_provider_fails_before_ledger_write(self) -> None:
        with self.assertRaises(Exception) as cm:
            reserve_provider_spend(
                profile(),
                provider(enabled=False),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
            )

        self.assertIn("disabled", str(cm.exception))
        self.assertFalse(self.ledger.exists())

    def test_unknown_cost_rejected_when_profile_has_cap(self) -> None:
        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile(budget_usd_per_day=Decimal("1")),
                provider(),
                Cost(None, "unknown", "typesafe"),
                path=self.ledger,
            )

    def test_daily_cap_includes_reserved_and_settled_costs_atomically(self) -> None:
        p = profile(budget_usd_per_day=Decimal("0.01"))
        ref = provider()
        first = reserve_provider_spend(
            p,
            ref,
            Cost(Decimal("0.006"), "price-table", "typesafe"),
            path=self.ledger,
            now=1000,
        )
        settle_provider_spend(
            first,
            Cost(Decimal("0.006"), "provider-reported", "typesafe"),
            path=self.ledger,
            now=1001,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                p,
                ref,
                Cost(Decimal("0.005"), "price-table", "typesafe"),
                path=self.ledger,
                now=1002,
            )

    def test_provider_data_cap_uses_same_transaction(self) -> None:
        p = profile()
        ref = provider(config={"spend_caps_usd": {"daily": "0.01"}})
        reserve_provider_spend(
            p,
            ref,
            Cost(Decimal("0.006"), "price-table", "typesafe"),
            path=self.ledger,
            now=1000,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                p,
                ref,
                Cost(Decimal("0.006"), "price-table", "typesafe"),
                path=self.ledger,
                now=1001,
            )

    def test_provider_data_cap_is_enforced_across_profiles(self) -> None:
        ref = provider(config={"spend_caps_usd": {"daily": "0.01"}})
        reserve_provider_spend(
            profile(),
            ref,
            Cost(Decimal("0.006"), "price-table", "typesafe"),
            path=self.ledger,
            now=1000,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile().with_overrides(id="other-paid-plan"),
                ref,
                Cost(Decimal("0.006"), "price-table", "typesafe"),
                path=self.ledger,
                now=1001,
            )

    def test_admin_provider_cap_is_enforced_across_profiles(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {"typesafe": {"caps": {"daily_usd": 0.01}}},
                "global_caps": {},
            },
            path=self.admin_db,
        )
        ref = provider()
        reserve_provider_spend(
            profile(),
            ref,
            Cost(Decimal("0.006"), "price-table", "typesafe"),
            path=self.ledger,
            admin_db_path=self.admin_db,
            now=1000,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile().with_overrides(id="other-paid-plan"),
                ref,
                Cost(Decimal("0.006"), "price-table", "typesafe"),
                path=self.ledger,
                admin_db_path=self.admin_db,
                now=1001,
            )

    def test_admin_global_cap_aggregates_all_providers(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {"providers": {}, "global_caps": {"daily_usd": 0.01}},
            path=self.admin_db,
        )
        reserve_provider_spend(
            profile(),
            provider(id="typesafe"),
            Cost(Decimal("0.006"), "price-table", "typesafe"),
            path=self.ledger,
            admin_db_path=self.admin_db,
            now=1000,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile().with_overrides(
                    id="other-paid-plan",
                    remote=RemoteProviderPolicy(providers=frozenset({"openrouter"})),
                ),
                provider(
                    id="openrouter",
                    api_key_env="TYPESAFE_TEST_KEY",
                    protocols=("openai-compatible",),
                ),
                Cost(Decimal("0.006"), "price-table", "openrouter"),
                path=self.ledger,
                admin_db_path=self.admin_db,
                now=1001,
            )

    def test_admin_db_defaults_to_ledger_directory_when_present(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {"typesafe": {"allowed_plans": []}},
                "global_caps": {},
            },
            path=self.ledger.parent / "admin.db",
        )

        with self.assertRaises(Exception) as cm:
            reserve_provider_spend(
                profile(),
                provider(),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
            )

        self.assertIn("not allowed for access plan", str(cm.exception))

    def test_profile_daily_budget_aggregates_all_providers(self) -> None:
        reserve_provider_spend(
            profile(budget_usd_per_day=Decimal("0.01")),
            provider(id="typesafe"),
            Cost(Decimal("0.006"), "price-table", "typesafe"),
            path=self.ledger,
            now=1000,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile(
                    budget_usd_per_day=Decimal("0.01"),
                ).with_overrides(
                    remote=RemoteProviderPolicy(
                        providers=frozenset({"typesafe", "openrouter"}),
                        budget_usd_per_day=Decimal("0.01"),
                    )
                ),
                provider(
                    id="openrouter",
                    api_key_env="TYPESAFE_TEST_KEY",
                    protocols=("openai-compatible",),
                ),
                Cost(Decimal("0.006"), "price-table", "openrouter"),
                path=self.ledger,
                now=1001,
            )

    def test_settled_zero_actual_cost_counts_as_zero_not_estimate(self) -> None:
        p = profile(budget_usd_per_day=Decimal("0.01"))
        ref = provider()
        reservation = reserve_provider_spend(
            p,
            ref,
            Cost(Decimal("0.009"), "price-table", "typesafe"),
            path=self.ledger,
            now=1000,
        )
        settle_provider_spend(
            reservation,
            Cost(Decimal("0"), "provider-reported", "typesafe"),
            path=self.ledger,
            now=1001,
        )

        reserve_provider_spend(
            p,
            ref,
            Cost(Decimal("0.009"), "price-table", "typesafe"),
            path=self.ledger,
            now=1002,
        )

    def test_admin_allowed_plans_empty_list_denies_all(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {"typesafe": {"allowed_plans": []}},
                "global_caps": {},
            },
            path=self.admin_db,
        )

        with self.assertRaises(Exception) as cm:
            reserve_provider_spend(
                profile(),
                provider(),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
                admin_db_path=self.admin_db,
            )

        self.assertIn("not allowed for access plan", str(cm.exception))
        self.assertFalse(self.ledger.exists())

    def test_admin_allowed_plans_allows_named_profile(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {"typesafe": {"allowed_plans": ["paid-plan"]}},
                "global_caps": {},
            },
            path=self.admin_db,
        )

        reserve_provider_spend(
            profile(),
            provider(),
            Cost(Decimal("0.001"), "price-table", "typesafe"),
            path=self.ledger,
            admin_db_path=self.admin_db,
        )

    def test_unknown_cost_rejected_when_admin_cap_exists(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {"providers": {}, "global_caps": {"daily_usd": 0}},
            path=self.admin_db,
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile(),
                provider(),
                Cost(None, "unknown", "typesafe"),
                path=self.ledger,
                admin_db_path=self.admin_db,
            )

    def test_invalid_admin_cap_fails_closed(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {"providers": {}, "global_caps": {"daily_usd": "NaN"}},
            path=self.admin_db,
        )

        with self.assertRaises(ProviderError):
            reserve_provider_spend(
                profile(),
                provider(),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
                admin_db_path=self.admin_db,
            )

    def test_invalid_provider_config_cap_fails_closed(self) -> None:
        with self.assertRaises(ProviderError):
            reserve_provider_spend(
                profile(),
                provider(config={"spend_caps_usd": {"daily": "Infinity"}}),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
            )

    def test_completion_prompt_batch_output_cost_must_hit_per_request_cap(self) -> None:
        ref = provider(
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "10",
            }
        )
        estimate = estimate_chat_cost(ref, {"prompt": ["x"] * 20, "max_tokens": 100})

        self.assertEqual(estimate.usd, Decimal("0.020000000"))
        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile(max_usd_per_request=Decimal("0.002")),
                ref,
                estimate,
                path=self.ledger,
            )

    def test_completion_token_id_prompt_list_counts_as_one_prompt(self) -> None:
        ref = provider(
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "10",
            }
        )
        estimate = estimate_chat_cost(ref, {"prompt": [1, 2, 3], "max_tokens": 100})

        self.assertEqual(estimate.usd, Decimal("0.001000000"))

    def test_completion_token_batch_prompt_list_counts_each_prompt(self) -> None:
        ref = provider(
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "10",
            }
        )
        estimate = estimate_chat_cost(
            ref,
            {"prompt": [[1, 2, 3], [4, 5, 6]], "max_tokens": 100},
        )

        self.assertEqual(estimate.usd, Decimal("0.002000000"))

    def test_completion_best_of_output_cost_must_hit_per_request_cap(self) -> None:
        ref = provider(
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "10",
            }
        )
        estimate = estimate_chat_cost(
            ref,
            {"prompt": "x", "max_tokens": 100, "best_of": 20},
        )

        self.assertEqual(estimate.usd, Decimal("0.020000000"))
        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile(max_usd_per_request=Decimal("0.002")),
                ref,
                estimate,
                path=self.ledger,
            )

    def test_mixed_case_provider_id_uses_saved_admin_deny_all_plans(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {
                    "openrouter": {
                        "allowed_plans": [],
                        "caps": {"daily_usd": 100},
                    }
                },
                "global_caps": {},
            },
            path=self.admin_db,
        )
        mixed_case_provider = provider(
            id="OpenRouter",
            api_key_env="TYPESAFE_TEST_KEY",
            protocols=("openai-compatible",),
        )

        with self.assertRaises(Exception) as cm:
            reserve_provider_spend(
                profile().with_overrides(
                    remote=RemoteProviderPolicy(providers=frozenset({"OpenRouter"}))
                ),
                mixed_case_provider,
                Cost(Decimal("0.01"), "price-table", "OpenRouter"),
                path=self.ledger,
                admin_db_path=self.admin_db,
            )

        self.assertIn("not allowed for access plan", str(cm.exception))

    def test_mixed_case_provider_id_uses_saved_admin_zero_cap_when_plan_allowed(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {
                    "openrouter": {
                        "allowed_plans": ["paid-plan"],
                        "caps": {"daily_usd": 0},
                    }
                },
                "global_caps": {},
            },
            path=self.admin_db,
        )
        mixed_case_provider = provider(
            id="OpenRouter",
            api_key_env="TYPESAFE_TEST_KEY",
            protocols=("openai-compatible",),
        )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile().with_overrides(
                    remote=RemoteProviderPolicy(providers=frozenset({"OpenRouter"}))
                ),
                mixed_case_provider,
                Cost(Decimal("0.01"), "price-table", "OpenRouter"),
                path=self.ledger,
                admin_db_path=self.admin_db,
            )

    def test_mixed_case_provider_id_matches_normalized_profile_provider(self) -> None:
        mixed_case_provider = provider(
            id="OpenRouter",
            api_key_env="TYPESAFE_TEST_KEY",
            protocols=("openai-compatible",),
        )

        reserve_provider_spend(
            profile().with_overrides(
                remote=RemoteProviderPolicy(providers=frozenset({"openrouter"}))
            ),
            mixed_case_provider,
            Cost(Decimal("0.001"), "price-table", "OpenRouter"),
            path=self.ledger,
        )

    def test_policy_request_remote_provider_is_canonicalized(self) -> None:
        decision = evaluate(
            profile().with_overrides(
                remote=RemoteProviderPolicy(providers=frozenset({"openrouter"}))
            ),
            PolicyRequest(remote_provider="OpenRouter"),
        )

        self.assertTrue(decision.allowed)

    def test_historical_mixed_case_ledger_rows_count_toward_provider_caps(self) -> None:
        ensure_ledger(self.ledger)
        with sqlite3.connect(self.ledger) as conn:
            conn.execute(
                """
                INSERT INTO provider_spend
                (provider, profile_id, status, estimated_usd, cost_source, created_at)
                VALUES (?, ?, 'reserved', ?, ?, ?)
                """,
                ("OpenRouter", "paid-plan", "0.006", "price-table", 1000),
            )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile().with_overrides(
                    remote=RemoteProviderPolicy(providers=frozenset({"openrouter"}))
                ),
                provider(
                    id="openrouter",
                    config={"spend_caps_usd": {"daily": "0.01"}},
                ),
                Cost(Decimal("0.006"), "price-table", "openrouter"),
                path=self.ledger,
                now=1001,
            )

    def test_historical_mixed_case_ledger_rows_count_toward_admin_caps(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {
                "providers": {"openrouter": {"caps": {"daily_usd": 0.01}}},
                "global_caps": {},
            },
            path=self.admin_db,
        )
        ensure_ledger(self.ledger)
        with sqlite3.connect(self.ledger) as conn:
            conn.execute(
                """
                INSERT INTO provider_spend
                (provider, profile_id, status, estimated_usd, cost_source, created_at)
                VALUES (?, ?, 'reserved', ?, ?, ?)
                """,
                ("OpenRouter", "paid-plan", "0.006", "price-table", 1000),
            )

        with self.assertRaises(BudgetExceeded):
            reserve_provider_spend(
                profile().with_overrides(
                    remote=RemoteProviderPolicy(providers=frozenset({"openrouter"}))
                ),
                provider(
                    id="openrouter",
                ),
                Cost(Decimal("0.006"), "price-table", "openrouter"),
                path=self.ledger,
                admin_db_path=self.admin_db,
                now=1001,
            )

    def test_admin_policy_read_error_fails_closed(self) -> None:
        bad_admin_path = Path(self.tmp.name)

        with self.assertRaises(ProviderError):
            reserve_provider_spend(
                profile(),
                provider(),
                Cost(Decimal("0.001"), "price-table", "typesafe"),
                path=self.ledger,
                admin_db_path=bad_admin_path,
            )

    def test_released_reservation_no_longer_counts(self) -> None:
        p = profile(budget_usd_per_day=Decimal("0.01"))
        ref = provider()
        reservation = reserve_provider_spend(
            p,
            ref,
            Cost(Decimal("0.009"), "price-table", "typesafe"),
            path=self.ledger,
            now=1000,
        )
        release_provider_spend(reservation, path=self.ledger, now=1001)

        reserve_provider_spend(
            p,
            ref,
            Cost(Decimal("0.009"), "price-table", "typesafe"),
            path=self.ledger,
            now=1002,
        )


class TestCosts(unittest.TestCase):
    def test_missing_output_price_is_unknown_instead_of_input_price(self) -> None:
        ref = provider(price={"input_usd_per_million_tokens": "1"})
        self.assertIsNone(estimate_chat_cost(ref, {"messages": [], "max_tokens": 100}).usd)

    def test_multiple_completions_reserve_each_output(self) -> None:
        ref = provider(price={"input_usd_per_million_tokens": "0", "output_usd_per_million_tokens": "1"})
        cost = estimate_chat_cost(ref, {"messages": [], "max_tokens": 1000, "n": 3})
        self.assertEqual(cost.usd, Decimal("0.003"))

    def test_nested_image_has_unknown_cost_before_paid_dispatch(self) -> None:
        ref = provider(price={"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "1"})
        cost = estimate_chat_cost(ref, {"messages": [{"content": [{"type": "image_url", "image_url": {"url": "https://example.test/image"}}]}], "max_tokens": 10})
        self.assertIsNone(cost.usd)

    def test_typesafe_estimate_uses_provider_price_data_only(self) -> None:
        ref = provider(price={})
        cost = estimate_typesafe_cost(
            ref,
            {"state": "hello", "questions": {"q": {"noul": {"instructions": "ok"}}}},
        )

        self.assertIsNone(cost.usd)
        self.assertEqual(cost.source, "unknown")

    def test_chat_estimate_requires_output_limit(self) -> None:
        ref = provider(
            price={
                "input_usd_per_million_tokens": "1",
                "output_usd_per_million_tokens": "2",
            }
        )

        unknown = estimate_chat_cost(ref, {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        known = estimate_chat_cost(
            ref,
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )

        self.assertIsNone(unknown.usd)
        self.assertIsNotNone(known.usd)

    def test_chat_estimate_counts_tools_payload(self) -> None:
        ref = provider(
            price={
                "input_usd_per_million_tokens": "1",
                "output_usd_per_million_tokens": "0",
            }
        )

        small = estimate_chat_cost(
            ref,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
        )
        large = estimate_chat_cost(
            ref,
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "x", "description": "y" * 200}}],
                "max_tokens": 1,
            },
        )

        self.assertGreater(large.usd or Decimal("0"), small.usd or Decimal("0"))

    def test_embeddings_estimate_does_not_require_output_limit(self) -> None:
        ref = provider(price={"input_usd_per_million_tokens": "1"})

        cost = estimate_chat_cost(ref, {"model": "embed", "input": "hello"})

        self.assertIsNotNone(cost.usd)

    def test_media_payload_estimate_is_unknown(self) -> None:
        ref = provider(price={"input_usd_per_million_tokens": "1"})

        cost = estimate_chat_cost(
            ref,
            {"model": "m", "messages": [], "image": "data:image/png;base64,...", "max_tokens": 1},
        )

        self.assertIsNone(cost.usd)

    def test_mixed_protocol_provider_estimates_chat_with_chat_shape(self) -> None:
        ref = provider(
            id="openrouter",
            protocols=("typesafe-systemone", "openai-compatible"),
            price={
                "input_usd_per_million_tokens": "8",
                "output_usd_per_million_tokens": "8",
            },
        )
        body = {
            "model": "openrouter/test-chat",
            "messages": [{"role": "user", "content": "x" * 10000}],
            "max_tokens": 10000,
        }

        self.assertEqual(
            estimate_request_cost(ref, body),
            estimate_chat_cost(ref, body),
        )

    def test_provider_reported_cost_wins_then_litellm_then_price_table(self) -> None:
        ref = provider(price={"input_usd_per_million_tokens": "1"})

        reported = extract_provider_cost(ref, body={"usage": {"cost": "0.25"}})
        litellm = extract_provider_cost(ref, headers={"x-litellm-response-cost": "0.5"})
        priced = extract_provider_cost(
            ref,
            body={"model": "m", "usage": {"input_tokens": 1000}},
        )

        self.assertEqual(reported.usd, Decimal("0.250000000"))
        self.assertEqual(reported.source, "provider-reported")
        self.assertEqual(litellm.source, "litellm")
        self.assertEqual(priced.source, "price-table")


class TestTypeSafeHttpHook(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.db"
        os.environ["TYPESAFE_TEST_KEY"] = "test-key"

    def tearDown(self) -> None:
        os.environ.pop("TYPESAFE_TEST_KEY", None)
        self.tmp.cleanup()

    def test_vendor_auth_error_maps_to_provider_auth_failed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        transport = httpx.MockTransport(handler)

        async def run() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(ProviderAuthFailed):
                    await call_typesafe_systemone(
                        provider(),
                        profile(),
                        {"model": "jev-latest", "state": "x", "questions": {}},
                        ledger_path=self.ledger,
                        client=client,
                    )

        asyncio.run(run())

    def test_rate_limit_preserves_retry_after(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "2"}, json={})

        transport = httpx.MockTransport(handler)

        async def run() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(ProviderRateLimited) as cm:
                    await call_typesafe_systemone(
                        provider(),
                        profile(),
                        {"model": "jev-latest", "state": "x", "questions": {}},
                        ledger_path=self.ledger,
                        client=client,
                    )
                self.assertEqual(cm.exception.retry_after, "2")

        asyncio.run(run())

    def test_typesafe_hook_strips_gateway_provider_prefix_before_vendor_call(self) -> None:
        seen: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "model": "jev-test",
                    "answers": {},
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            )

        transport = httpx.MockTransport(handler)

        async def run() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                await call_typesafe_systemone(
                    provider(),
                    profile(),
                    {"model": "typesafe/jev-latest", "state": "x", "questions": {}},
                    ledger_path=self.ledger,
                    client=client,
                )

        asyncio.run(run())
        self.assertEqual(seen["model"], "jev-latest")

    def test_paid_fallback_permission_checked_before_dispatch(self) -> None:
        called = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)

        async def run() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(Exception) as cm:
                    await call_typesafe_systemone(
                        provider(),
                        profile(),
                        {"model": "jev-latest", "state": "x", "questions": {}},
                        ledger_path=self.ledger,
                        client=client,
                        paid_fallback=True,
                    )
                self.assertIn("paid fallback disabled", str(cm.exception))

        asyncio.run(run())
        self.assertFalse(called)

    def test_direct_hook_redacts_echoed_provider_key(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "jev-test",
                    "answers": {"x": {"noul": 1.0}},
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                    "debug": "test-key",
                },
            )

        transport = httpx.MockTransport(handler)

        async def run() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                body, _cost, _usage, _reservation = await call_typesafe_systemone(
                    provider(),
                    profile(),
                    {"model": "jev-latest", "state": "x", "questions": {}},
                    ledger_path=self.ledger,
                    client=client,
                )
                self.assertEqual(body["debug"], "<redacted>")

        asyncio.run(run())


class TestReceiptFields(unittest.TestCase):
    def test_cost_and_usage_are_top_level_and_client_sanitizes_provider(self) -> None:
        receipt = RouteReceipt(protocol="typesafe", intent="decision.systemone")

        attach_cost_to_receipt(
            receipt,
            Cost(Decimal("0.001"), "provider-reported", "typesafe"),
            Usage(input_tokens=10, output_tokens=0),
        )

        internal = receipt.to_dict()
        client = receipt.to_dict(audience="client")
        self.assertTrue(internal["remote"])
        self.assertEqual(internal["cost"]["provider"], "typesafe")
        self.assertNotIn("provider", client["cost"])
        self.assertEqual(client["usage"]["input_tokens"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
