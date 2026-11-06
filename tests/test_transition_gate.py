"""The transition gate's pure parts: domain policy, admission, bounded wait, gate table, lease keying,
and the registry refusal of a domain step T1.1 cannot gate (gateway/transition_gate.py)."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from gateway import service_registry
from gateway.litellm_substrate import reaches_engine_directly
from gateway.swap_drain import SwapDrain, SwapDrainSettings, SwapDeferred
from gateway.transition_gate import (
    GateStore,
    TransitionPolicy,
    TransitionUnavailable,
    acquire_bounded,
    admit,
    domain_of,
    policy_for,
    require_supported,
    transition_enabled,
)


def _enabled(domain: str | None = None, **transition) -> dict:
    residency: dict = {"transition": {"enabled": True, **transition}}
    if domain:
        residency["domain"] = domain
    return {"residency": residency}


# -- domain policy -------------------------------------------------------------


def test_a_service_without_a_domain_is_its_own_domain_and_not_enabled() -> None:
    policy = policy_for("solo", {"solo": {"host": "x"}})
    assert policy == TransitionPolicy(domain="solo", services=("solo",))
    assert not policy.enabled


def test_services_that_declare_one_domain_share_it_and_any_member_enables_it() -> None:
    services = {
        "alias-a": _enabled("gpu1", admission_wait_s=9),
        "alias-b": {"residency": {"domain": "gpu1"}},
        "alias-c": _enabled("gpu1", admission_wait_s=4),
        "elsewhere": _enabled(),
    }
    for name in ("alias-a", "alias-b", "gpu1"):
        policy = policy_for(name, services)
        assert policy.domain == "gpu1"
        assert policy.enabled
        assert policy.services == ("alias-a", "alias-b", "alias-c")
        assert policy.admission_wait_s == 4, "the shortest declared wait bounds the domain"
    assert policy_for("elsewhere", services).services == ("elsewhere",)


def test_domain_is_read_from_the_config_block_too_and_a_bad_value_falls_back() -> None:
    assert domain_of("svc", {"config": {"residency": {"domain": "d"}}}) == "d"
    assert domain_of("svc", {"config": {"residency": "on_demand"}}) == "svc"
    assert domain_of("svc", {"residency": {"domain": "  "}}) == "svc"


def test_only_a_literal_true_enables_and_pass_through_is_per_service() -> None:
    services = {
        "a": {"residency": {"domain": "d", "transition": {"enabled": "yes"}}},
        "b": _enabled("d", activation="pass_through"),
    }
    assert policy_for("a", services).enabled, "b enables the shared domain"
    assert not policy_for("a", services).pass_through, "a itself declares no pass-through"
    assert policy_for("b", services).pass_through
    assert not policy_for("x", {"x": {"residency": {"transition": {"enabled": "yes"}}}}).enabled


# -- admission -----------------------------------------------------------------

ENABLED = TransitionPolicy(domain="d", enabled=True, services=("a", "b"))


@pytest.mark.parametrize("state", ["closed", "failed_closed", "reconciling"])
def test_admit_refuses_every_gate_state_but_open(state: str) -> None:
    with pytest.raises(TransitionUnavailable) as refused:
        admit(ENABLED, state, {})
    assert refused.value.status_code == 503
    assert refused.value.reason == f"gate_{state}"
    assert refused.value.headers == {"Retry-After": "30"}


def test_admit_refuses_a_manual_restriction_on_any_service_of_the_domain() -> None:
    admit(ENABLED, "open", {"a": {"state": "available"}})
    with pytest.raises(TransitionUnavailable) as refused:
        admit(ENABLED, "open", {"b": {"state": "Maintenance", "retry_after_seconds": 45}})
    assert (refused.value.service, refused.value.reason, refused.value.retry_after_s) == ("b", "service_maintenance", 45)


def test_admit_never_refuses_a_domain_that_is_not_enabled() -> None:
    admit(TransitionPolicy(domain="d"), "closed", {"d": {"state": "disabled"}})


# -- bounded wait --------------------------------------------------------------


def test_enabled_wait_is_bounded_and_leaves_the_lock_free() -> None:
    async def scenario() -> float:
        lock = asyncio.Lock()
        await lock.acquire()
        started = time.monotonic()
        with pytest.raises(TransitionUnavailable) as refused:
            await acquire_bounded(lock, TransitionPolicy(domain="d", enabled=True, admission_wait_s=0.1))
        assert refused.value.reason == "admission_timeout"
        assert refused.value.retry_after_s == 1
        lock.release()
        assert not lock.locked(), "the timed-out waiter does not hold the lock"
        return time.monotonic() - started

    elapsed = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert 0.09 <= elapsed < 1.5


def test_a_domain_that_is_not_enabled_waits_as_long_as_it_takes() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()
        await lock.acquire()
        waiter = asyncio.create_task(acquire_bounded(lock, TransitionPolicy(domain="d", admission_wait_s=0.01)))
        await asyncio.sleep(0.1)
        assert not waiter.done()
        lock.release()
        await waiter
        assert lock.locked()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_a_cancelled_waiter_never_holds_the_lock() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()
        await lock.acquire()
        waiter = asyncio.create_task(acquire_bounded(lock, TransitionPolicy(domain="d", enabled=True, admission_wait_s=5)))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        lock.release()
        assert not lock.locked()

    asyncio.run(asyncio.wait_for(scenario(), 5))


# -- gate table ----------------------------------------------------------------


def _write_gate(path, domain: str, state: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT OR REPLACE INTO transition_gate VALUES (?, ?, NULL, 0)", (domain, state))


def test_gate_store_reads_rows_and_a_missing_row_is_open(tmp_path) -> None:
    store = GateStore(tmp_path / "transition.db")
    store.load()
    assert store.state("d") == "open"
    _write_gate(store.path, "d", "closed")
    assert store.state("d") == "open", "the cache serves until the next load"
    store.load()
    assert store.state("d") == "closed"
    assert store.snapshot() == {"readable": True, "domains": {"d": "closed"}}


def test_gate_store_refuses_an_unknown_state_and_fails_closed_when_unreadable(tmp_path) -> None:
    store = GateStore(tmp_path / "transition.db")
    store.load()
    with pytest.raises(sqlite3.IntegrityError):
        _write_gate(store.path, "d", "half-open")
    unreadable = GateStore(tmp_path / "missing-dir-is-a-file" / "transition.db")
    (tmp_path / "missing-dir-is-a-file").write_text("not a directory")
    assert unreadable.state("any") == "reconciling", "never loaded: closed"
    unreadable.load()
    assert unreadable.state("any") == "reconciling"


# -- lease keying --------------------------------------------------------------


def test_leases_count_per_domain_so_a_swap_on_one_alias_waits_for_the_other() -> None:
    domains = {"alias-a": "gpu1", "alias-b": "gpu1"}
    drain = SwapDrain(key_for=lambda name: domains.get(name, name))

    async def scenario() -> None:
        lease = drain.lease("alias-a")
        assert drain.in_flight("alias-b") == 1
        assert drain.in_flight("gpu1") == 1
        assert drain.snapshot()["gpu1"]["in_flight"] == 1
        waiting = asyncio.create_task(drain.clear_for_swap("alias-b", SwapDrainSettings(enabled=True, timeout_s=5)))
        await asyncio.sleep(0.02)
        assert not waiting.done()
        lease.release()
        await waiting
        held = drain.lease("alias-b")
        with pytest.raises(SwapDeferred):
            await drain.clear_for_swap("alias-a", SwapDrainSettings(enabled=True, timeout_s=0.01))
        held.release()
        assert drain.in_flight("gpu1") == 0

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_an_admit_refusal_mints_no_lease() -> None:
    def refuse(name: str) -> None:
        raise TransitionUnavailable(name, reason="gate_closed")

    drain = SwapDrain(admit=refuse)
    with pytest.raises(TransitionUnavailable):
        drain.lease("svc")
    assert drain.in_flight("svc") == 0


# -- registry refusal ----------------------------------------------------------


PASS = {"transition": {"enabled": True, "activation": "pass_through"}}


def _llm(**config) -> dict:
    return {"kind": "local", "enabled": True,
            "config": {"protocol": "openai-compatible", "host": "127.0.0.1", "port": 5999,
                       "models": [{"id": "m1"}], **config}}


def _registry(*records: tuple[str, dict]) -> dict:
    return {"schema_version": 1, "services": dict(records)}


def _refused(registry: dict, match: str) -> list[str]:
    """The strict read raises ``match``; the lenient read leaves out and names the refused services."""
    with pytest.raises(ValueError, match=match):
        service_registry._read_registry(registry, strict=True)
    kept, excluded = service_registry.read_registry(registry)
    assert not {item.name for item in excluded} & set(kept["services"])
    return sorted(item.name for item in excluded)


def test_a_direct_routed_transition_service_is_refused_with_a_precise_error() -> None:
    record = _llm(residency=PASS)
    assert reaches_engine_directly("svc", record), "the fixture must be a direct route"
    assert _refused(_registry(("svc", record)), "gateway_lifecycle") == ["svc"]


def test_one_direct_model_among_routed_ones_still_counts_as_direct() -> None:
    mixed = _llm(residency=PASS, models=[{"id": "m1", "gateway_lifecycle": True}, {"id": "m2"}])
    assert reaches_engine_directly("svc", mixed)
    assert _refused(_registry(("svc", mixed)), "gateway_lifecycle") == ["svc"]
    routed = _llm(residency=PASS, models=[{"id": "m1", "gateway_lifecycle": True}])
    assert not reaches_engine_directly("svc", routed)


def test_an_empty_top_level_residency_does_not_hide_the_config_block() -> None:
    record = {**_llm(residency=PASS), "residency": {}}
    assert transition_enabled(record), "the gate reads what the runtime projection keeps"
    assert _refused(_registry(("svc", record)), "gateway_lifecycle") == ["svc"]


def test_every_member_of_an_enabled_domain_must_declare_pass_through() -> None:
    enabler = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    silent = _llm(gateway_lifecycle=True, residency={"domain": "gpu1"})
    gateway_run = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", "transition": {"enabled": True}})
    elsewhere = _llm(residency={"domain": "gpu2"})
    registry = _registry(("a", enabler), ("b", silent), ("c", gateway_run), ("d", elsewhere))
    assert _refused(registry, "activation: pass_through") == ["b", "c"]
    kept, _excluded = service_registry.read_registry(registry)
    assert sorted(kept["services"]) == ["a", "d"], "a legacy service in another domain is untouched"


def test_a_lifecycle_routed_pass_through_domain_and_a_legacy_direct_service_are_accepted() -> None:
    routed = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    alias = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    legacy = _llm()
    services = service_registry._read_registry(
        _registry(("svc", routed), ("svc-b", alias), ("legacy", legacy)), strict=True)[0]["services"]
    assert sorted(services) == ["legacy", "svc", "svc-b"]


def test_the_runtime_guard_refuses_gateway_run_activation_in_an_enabled_domain() -> None:
    services = {"a": _enabled("d", activation="pass_through"), "b": {"residency": {"domain": "d"}}}
    require_supported(policy_for("a", services))
    require_supported(policy_for("x", {"x": {}}))
    with pytest.raises(TransitionUnavailable) as refused:
        require_supported(policy_for("b", services), "b")
    assert (refused.value.reason, refused.value.service) == ("activation_not_supported", "b")


def test_the_projection_keeps_the_legacy_on_demand_string_beside_a_residency_mapping() -> None:
    """A record with both the config string and the top-level mapping still reads as on-demand, and the gate still sees the block."""
    from gateway.model_presentation import starts_on_demand
    from gateway.service_registry import LEGACY_RESIDENCY_KEY

    declared = {"domain": "gpu1", **PASS}
    record = {"id": "svc", "config": {"port": 1, "residency": "on_demand"}, "residency": declared}
    projected = service_registry.runtime_backend_from_service(record)
    assert projected["residency"] == declared, "the mapping is carried"
    assert projected[LEGACY_RESIDENCY_KEY] == "on_demand"
    assert transition_enabled(projected) and domain_of("svc", projected) == "gpu1"
    assert starts_on_demand(projected), "the flattened backend"
    assert starts_on_demand(record), "the stored record"
    # Neither shape alone changes.
    assert starts_on_demand(service_registry.runtime_backend_from_service({"id": "a", "config": {"residency": "on_demand"}}))
    assert not starts_on_demand(service_registry.runtime_backend_from_service({"id": "b", "config": {"port": 1}, "residency": declared}))
    assert not starts_on_demand({"id": "c", "config": {"residency": "always_on"}})


def test_the_runtime_projection_carries_a_declared_top_level_residency() -> None:
    declared = {"domain": "gpu1", "transition": {"enabled": True}}
    projected = service_registry.runtime_backend_from_service({"id": "svc", "config": {"port": 1}, "residency": declared})
    assert projected["residency"] == declared
    empty = service_registry.runtime_backend_from_service({"id": "svc", "config": {"residency": "on_demand"}, "residency": {}})
    assert empty["residency"] == "on_demand", "an empty top-level block never replaces the config's own value"


# -- the positive rule: a member of an enabled domain is reached only by leased hops -----------------


def _protocol_record(protocol: str, **config) -> dict:
    return {"kind": "local", "enabled": True,
            "config": {"connection": {"transport": "http", "dispatch": "protocol", "protocol": protocol,
                                      "address": "http://127.0.0.1:5998", "credentials": {}},
                       "offerings": [{"id": "o1", "kind": "model", "upstream": "u1",
                                      "tasks": [{"task": "chat", "evidence": "declared"}],
                                      "selection": {"mode": "request_field"}}],
                       "residency": {"domain": "gpu1", **PASS}, **config}}


def test_each_leased_hop_is_accepted_and_names_nothing_about_the_engine() -> None:
    routed = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    declared_protocol = _protocol_record("ollama-native")
    native = _llm(gateway_lifecycle=True, native_passthrough=True, residency={"domain": "gpu1", **PASS})
    services = service_registry._read_registry(
        _registry(("routed", routed), ("proto", declared_protocol), ("native", native)), strict=True)[0]["services"]
    assert sorted(services) == ["native", "proto", "routed"]


@pytest.mark.parametrize("record,why", [
    (_protocol_record("node-graph"), "reviewed-adapter protocol"),
    (_protocol_record("inline-tts"), "reviewed-adapter protocol"),
    (_protocol_record("no-such-protocol"), "unknown protocol"),
])
def test_a_protocol_whose_hops_hold_no_lease_is_refused_with_the_hop_named(record, why) -> None:
    assert _refused(_registry(("svc", record)), why) == ["svc"]


def test_a_lifecycle_member_that_declares_a_capability_with_no_inner_endpoint_is_refused_by_name() -> None:
    """The lifecycle hop serves chat and embeddings; a pass-through member declaring any other capability
    would be reached only by an endpoint that answers 404."""
    served = _llm(gateway_lifecycle=True, capabilities=["chat", "embed"], residency={"domain": "gpu1", **PASS})
    unserved = _llm(gateway_lifecycle=True, capabilities=["chat", "rerank"], residency={"domain": "gpu1", **PASS})
    assert sorted(service_registry._read_registry(
        _registry(("ok", served)), strict=True)[0]["services"]) == ["ok"]
    assert _refused(_registry(("bad", unserved)), "capability 'rerank'") == ["bad"]


def test_the_house_openai_word_list_is_not_refused_as_an_unserved_capability() -> None:
    """Kobold in proxy_config.yml declares ``[openai, chat, completions]``; ``openai`` is the compatibility marker
    (litellm_substrate reads it), not a capability, so it asks for no inner endpoint. Failed before the fix: the
    registry refused the word 'openai'."""
    kobold = _llm(gateway_lifecycle=True, capabilities=["openai", "chat", "completions"],
                  residency={"domain": "gpu1", **PASS})
    embedder = _llm(gateway_lifecycle=True, capabilities=["openai", "embeddings"],
                    residency={"domain": "gpu1", **PASS})
    kept = service_registry._read_registry(_registry(("kobold", kobold), ("embed", embedder)), strict=True)[0]
    assert sorted(kept["services"]) == ["embed", "kobold"]
    # a real capability with no inner endpoint is still refused, marker word or not
    unserved = _llm(gateway_lifecycle=True, capabilities=["openai", "chat", "rerank"],
                    residency={"domain": "gpu1", **PASS})
    assert _refused(_registry(("bad", unserved)), "capability 'rerank'") == ["bad"]


def test_a_domain_that_is_not_enabled_may_declare_any_capability() -> None:
    legacy = _llm(gateway_lifecycle=True, capabilities=["chat", "rerank"])
    assert service_registry._read_registry(_registry(("legacy", legacy)), strict=True)[0]["services"]


def test_a_service_with_no_leased_path_at_all_is_refused() -> None:
    bare = {"kind": "local", "enabled": True,
            "config": {"host": "127.0.0.1", "port": 5999, "residency": {"domain": "gpu1", **PASS}}}
    assert _refused(_registry(("svc", bare)), "no leased path") == ["svc"]


def test_a_member_that_also_reaches_the_engine_unleased_is_refused() -> None:
    """One leased path does not excuse an unleased one: every path must be leased."""
    both = _llm(native_passthrough=True, residency={"domain": "gpu1", **PASS},
                models=[{"id": "m1", "gateway_lifecycle": True}, {"id": "m2"}])
    assert reaches_engine_directly("svc", both)
    assert _refused(_registry(("svc", both)), "gateway_lifecycle") == ["svc"]


# -- a pass-through member owns no lifecycle and no selection effect (F9, F3) ------------------------------


@pytest.mark.parametrize("config,match", [
    ({"process": {"driver": "process", "command": ["serve"]}}, "lifecycle driver 'process'"),
    ({"residency": {"domain": "gpu1", **PASS, "idle_owner": "gateway"}}, "idle_owner: gateway"),
    ({"lifecycle": {"driver": "systemd", "unit": "engine.service"}}, "lifecycle driver 'systemd'"),
])
def test_a_pass_through_member_cannot_declare_a_gateway_owned_lifecycle(config, match) -> None:
    record = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    for key, value in config.items():
        if key == "residency":
            record["config"]["residency"] = value
        elif key == "lifecycle":
            record["lifecycle"] = value
        else:
            record["config"][key] = value
    assert _refused(_registry(("svc", record)), match) == ["svc"]


@pytest.mark.parametrize("none_or_external", [None, "none", "external"])
def test_a_member_whose_engine_is_run_by_someone_else_is_accepted(none_or_external) -> None:
    record = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    if none_or_external:
        record["lifecycle"] = {"driver": none_or_external}
    assert service_registry._read_registry(_registry(("svc", record)), strict=True)[0]["services"]


@pytest.mark.parametrize("selection,match", [
    ({"mode": "switch"}, "selection.mode: switch"),
    ({"mode": "request_field", "implicit_load": False}, "selection.implicit_load: false"),
])
def test_a_pass_through_member_cannot_declare_an_offering_that_needs_a_gateway_run_effect(selection, match) -> None:
    record = _protocol_record("ollama-native")
    record["config"]["offerings"][0]["selection"] = selection
    assert _refused(_registry(("svc", record)), match) == ["svc"]


def test_a_member_of_a_domain_that_is_not_enabled_may_declare_any_of_these() -> None:
    record = _protocol_record("node-graph")
    record["config"]["residency"] = {"domain": "gpu2"}
    record["config"]["offerings"][0]["selection"] = {"mode": "request_field", "implicit_load": False}
    record["lifecycle"] = {"driver": "systemd", "unit": "engine.service"}
    assert service_registry._read_registry(_registry(("svc", record)), strict=True)[0]["services"]


# -- the check runs again after backfill and on the projected map (F2) --------------------------------------


def _backfilled(registry: dict, seeds: dict) -> dict:
    return service_registry.backfill_seed_catalog_metadata(registry, proxy_config={"backends": seeds})


def test_a_backfill_never_replaces_a_declared_residency_block_key_by_key() -> None:
    declared = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    seed = _llm(gateway_lifecycle=True, residency={"domain": "elsewhere", "transition": {"enabled": True},
                                                  "load": "on_demand", "idle_owner": "gateway"})
    out = _backfilled(_registry(("svc", declared)), {"svc": {**seed["config"], "enabled": True}})
    block = out["services"]["svc"].get("residency") or out["services"]["svc"]["config"]["residency"]
    assert block["domain"] == "gpu1", "a declared key is never replaced"
    assert block["transition"] == PASS["transition"], "a declared transition block is never replaced or extended"


def test_a_backfill_that_leaves_a_member_unfit_is_refused() -> None:
    """The domain check ran on the stored record; the merge changes it afterwards, so it runs again on the result."""
    stored = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    registry = service_registry.normalize_service_registry(_registry(("svc", stored)))
    registry["services"]["svc"]["config"]["process"] = {"driver": "process", "command": ["serve"]}
    with pytest.raises(ValueError, match="gateway-owned lifecycle"):
        service_registry.backfill_normalized_registry(registry)


def test_a_partial_boot_leaves_out_a_service_the_backfill_made_unfit(monkeypatch) -> None:
    stored = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    healthy = _llm()
    real = service_registry._backfill_in_place

    def merge_a_process_driver(out, *, proxy_config=None):
        real(out, proxy_config=proxy_config)
        out["services"]["svc"]["config"]["process"] = {"driver": "process", "command": ["serve"]}
        return out

    monkeypatch.setattr(service_registry, "_backfill_in_place", merge_a_process_driver)
    served, left_out = service_registry.servable_registry_with_backfill(_registry(("svc", stored), ("ok", healthy)))
    assert "svc" not in served["services"] and "ok" in served["services"]
    assert "gateway-owned lifecycle" in left_out["svc"]


def test_the_projected_backend_map_is_checked_at_every_publish() -> None:
    """A record that reaches the flat map unchecked (a store written by an older build) never runs: it is left out."""
    fit = _llm(gateway_lifecycle=True, residency={"domain": "gpu1", **PASS})
    unfit = _llm(residency={"domain": "gpu1", **PASS})  # direct route: no leased path
    registry = {"schema_version": 2, "services": {
        "fit": service_registry.normalize_service_record("fit", fit),
        "unfit": service_registry.normalize_service_record("unfit", _llm(residency={"domain": "gpu2"})),
    }, "protocol_frontends": {}, "workflows": {}, "capabilities": {}}
    registry["services"]["unfit"]["config"]["residency"] = unfit["config"]["residency"]
    projected = service_registry.apply_service_registry_to_config({}, registry)["backends"]
    assert sorted(projected) == ["fit"], "the unfit member is excluded"
    errors = service_registry.runtime_transition_errors(projected)
    assert errors == {}


def test_the_legacy_residency_key_is_spelled_once() -> None:
    """Failed before the fix: model_presentation.py wrote the literal ``"legacy_residency"``."""
    import ast
    from pathlib import Path

    root = Path(service_registry.__file__).resolve().parent
    literals = [path.name for path in sorted(root.rglob("*.py")) if path.name != "service_registry.py"
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Constant) and node.value == service_registry.LEGACY_RESIDENCY_KEY]
    assert literals == [], f"import LEGACY_RESIDENCY_KEY instead of spelling it: {literals}"
