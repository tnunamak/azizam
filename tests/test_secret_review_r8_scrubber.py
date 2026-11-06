"""Round 8 of the secret handling, the scrubber's lifetime: what the review of phase1-r7 (6d7f21d4) found (decision D44).

* MED 5: ``delete_provider_secret`` forgot a value before the deletion committed (a rollback left a stored secret
  unscrubbed); a shorter replacement cleared the withhold flag of an older value too long to register that was still held;
  four concurrent values under one source evicted the first.
* MED 6: a literal secret in the auth configuration was registered under a source made from a hash of its value (a thousand
  rotations were a thousand sources); the inference connection registered ``ref:<id>`` and a deletion forgot ``<id>``.

The registry is bounded by the configuration (its sources are configured names), registered when a value is stored and
forgotten when its deletion has committed.
"""

from __future__ import annotations

import pytest

import proxy
from gateway import admin_store, engine_connection, log_scrub, runtime_secrets, secret_refs, secret_store
from tests.test_url_origin_binding import store  # noqa: F401 - fixture

VALUE = "R8-ROLLBACK-SECRET-VALUE"


def source_count() -> int:
    return len(log_scrub.sources()) if hasattr(log_scrub, "sources") else len(log_scrub._RESOLVED)


@pytest.fixture(autouse=True)
def clean_registry():
    log_scrub.clear()
    yield
    log_scrub.clear()


# ---------------------------------------------------------------------------
# MED 5
# ---------------------------------------------------------------------------

def test_a_rolled_back_deletion_leaves_the_stored_secret_scrubbed(store):  # noqa: F811
    ref = secret_refs.store(VALUE, db_path=store)
    runtime_secrets.resolve(ref)
    assert runtime_secrets.scrub(f"echo {VALUE}") == "echo [removed]"
    with pytest.raises(RuntimeError):
        with admin_store.transaction(store) as tx:
            secret_store.delete_provider_secret(ref, db_path=store, conn=tx.conn)
            raise RuntimeError("the transaction rolls back")
    assert secret_store.provider_secret_status(ref, db_path=store).state == "set", "the secret is still stored"
    assert runtime_secrets.scrub(f"echo {VALUE}") == "echo [removed]", "so it is still scrubbed"


def test_a_committed_deletion_forgets_the_secret(store):  # noqa: F811
    ref = secret_refs.store(VALUE, db_path=store)
    runtime_secrets.resolve(ref)
    with admin_store.transaction(store) as tx:
        secret_store.delete_provider_secret(ref, db_path=store, conn=tx.conn)
        assert runtime_secrets.scrub(f"echo {VALUE}") == "echo [removed]", "not before the commit"
    assert runtime_secrets.scrub(f"echo {VALUE}") == f"echo {VALUE}"
    assert source_count() == 0


def test_a_deletion_without_a_caller_transaction_forgets_after_its_own_commit(store):  # noqa: F811
    ref = secret_refs.store(VALUE, db_path=store)
    runtime_secrets.resolve(ref)
    secret_store.delete_provider_secret(ref, db_path=store)
    assert source_count() == 0


def test_a_stored_value_is_scrubbed_from_the_moment_it_is_written(store):  # noqa: F811
    secret_refs.store(VALUE, db_path=store)
    assert runtime_secrets.scrub(f"echo {VALUE}") == "echo [removed]"


def test_a_shorter_replacement_does_not_clear_the_withhold_of_an_older_long_value():
    assert log_scrub.remember("L" * (64 * 1024 + 1), "one-source") is False
    assert log_scrub.scrub("any line") == log_scrub.WITHHELD
    assert log_scrub.remember("a short replacement", "one-source") is True
    assert log_scrub.scrub("any line") == log_scrub.WITHHELD
    log_scrub.forget("one-source")
    assert log_scrub.scrub("any line") == "any line"


def test_the_withhold_is_recomputed_from_every_source_that_holds_a_long_value():
    log_scrub.remember("L" * (64 * 1024 + 1), "first")
    log_scrub.remember("M" * (64 * 1024 + 1), "second")
    log_scrub.forget("first")
    assert log_scrub.scrub("any line") == log_scrub.WITHHELD, "the second source still holds one"
    log_scrub.forget("second")
    assert log_scrub.scrub("any line") == "any line"


@pytest.mark.parametrize("count", [4, 7])
def test_concurrent_values_under_one_source_are_all_scrubbed(count):
    values = [f"R8-CONCURRENT-{index}-VALUE" for index in range(count)]
    for value in values:
        log_scrub.remember(value, "one-source")
    assert log_scrub.scrub(" ".join(values), limit=10000) == " ".join(["[removed]"] * count)


def test_a_source_that_declares_all_its_values_loses_none_of_them():
    values = [f"R8-DECLARED-{index:03d}-VALUE" for index in range(100)]
    log_scrub.hold("one-source", values)
    log_scrub.add("R8-ADDED-LATER-VALUE", "one-source")
    assert log_scrub.scrub(" ".join([*values, "R8-ADDED-LATER-VALUE"]), limit=100000).split() == ["[removed]"] * 101
    assert source_count() == 1


# ---------------------------------------------------------------------------
# MED 6
# ---------------------------------------------------------------------------

def test_a_thousand_rotations_of_a_configured_literal_are_one_source():
    for index in range(1000):
        proxy.resolve_secret_value(f"R8-LITERAL-ROTATION-{index:04d}-VALUE")
    assert source_count() == 1
    assert runtime_secrets.scrub("R8-LITERAL-ROTATION-0999-VALUE") == "[removed]", "the current value is scrubbed"


def test_a_thousand_rotations_of_a_stored_secret_are_one_source(store):  # noqa: F811
    ref = secret_refs.store("R8-ROTATION-0000-VALUE", db_path=store)
    for index in range(1, 1001):
        secret_store.put_provider_secret(ref, f"R8-ROTATION-{index:04d}-VALUE", db_path=store, allow_short=True, keep_suffix=False)
    assert source_count() == 1
    assert runtime_secrets.scrub("R8-ROTATION-1000-VALUE") == "[removed]"


def test_the_inference_connection_and_the_deletion_use_one_source_for_a_stored_key(store, monkeypatch):  # noqa: F811
    secret_id = "engine-key-r8"
    origin = "http://engine.invalid:8080"
    secret_store.put_provider_secret(secret_id, "R8-ENGINE-KEY-VALUE", db_path=store, approved_origin=origin)
    log_scrub.clear()
    record = {"config": {"base_url": f"{origin}/v1", "credential_origin": origin, "api_key_secret_id": secret_id}}
    engine_connection.inference_connection(
        record, "/chat/completions", fallback_address="", fallback_timeout=1.0, db_path=store, env=lambda name: None)
    assert runtime_secrets.scrub("echo R8-ENGINE-KEY-VALUE") == "echo [removed]"
    secret_store.delete_provider_secret(secret_id, db_path=store)
    assert runtime_secrets.scrub("echo R8-ENGINE-KEY-VALUE") == "echo R8-ENGINE-KEY-VALUE", "the deletion forgot what the connection registered"
    assert source_count() == 0


def test_ref_prefixed_and_bare_ids_are_one_source():
    log_scrub.remember("R8-REF-VALUE", "ref:abc123")
    log_scrub.forget("abc123")
    assert log_scrub.scrub("echo R8-REF-VALUE") == "echo R8-REF-VALUE"
    log_scrub.remember("R8-REF-VALUE", "abc123")
    log_scrub.forget("ref:abc123")
    assert log_scrub.scrub("echo R8-REF-VALUE") == "echo R8-REF-VALUE"


def test_the_registry_is_bounded_by_its_sources_whatever_rotates():
    for index in range(50):
        log_scrub.remember(f"R8-A-{index}-VALUE", "env:A")
        log_scrub.hold("env:B", [f"R8-B-{index}-VALUE", f"R8-B-{index}-OTHER"])
    assert source_count() == 2
    held = len(log_scrub._SOURCES["env:A"].current) + len(log_scrub._SOURCES["env:A"].retired)
    assert held <= 1 + log_scrub._RETIRED_PER_SOURCE
