"""Round 6 of W3: what the review of round 5 found in the partial boot, the writes beside it and the reload.

Each test stores a real invalid registry, boots or reloads as the gateway does, and goes through the
app or the store. D22: an alias clash leaves out only the later claimant. A partial boot still backfills
the managed LiteLLM row. A reload that fails during a partial boot says "stale" and keeps naming what is
left out. A save that cannot keep a left-out record names that record. Four contract tests pin behaviour
that mutation checks showed nothing protected.
"""

from __future__ import annotations

import json

import pytest

import proxy
from gateway import admin_store, projection_types, service_registry
from gateway.admin_api import derive_admin_attention
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture
from tests.test_registry_apply_failure import GOOD, OWNER, _boot, _owner_client, _service
from tests.test_registry_partial_operator import JUNK, _if_match, _running_partial, _stored

BAD = _service("bad-engine", models=["this is not a model record"])


def _model(model_id: str, base: str | None = None) -> dict:
    return {"id": model_id, **({"base_model_id": base} if base else {})}


def _claiming(service_id: str, *aliases: str, created_at=None) -> dict:
    record = _service(service_id, models=[_model("base"), *(_model(alias, base="base") for alias in aliases)])
    if created_at is not None:
        record["provenance"] = {"created_at": created_at}
    return record


# --- MED-1, decision D22: an alias clash leaves out only the later claimant -----------------------------


def test_two_services_that_claim_one_public_alias_leave_out_only_the_later_one():
    stored = {"services": {"two": _claiming("two", "shared"), "one": _claiming("one", "shared"), "good-engine": GOOD}}

    part, left_out = service_registry.servable_part_of_registry(stored)

    assert set(part["services"]) == {"good-engine", "one"}  # the order of storage does not decide: the id does
    assert set(left_out) == {"two"} and "shared" in left_out["two"] and "one" in left_out["two"]


def test_the_earlier_creation_time_in_provenance_keeps_the_alias_over_the_earlier_id():
    stored = {"services": {
        "aaa": _claiming("aaa", "shared", created_at="2026-03-01T00:00:00Z"),
        "zzz": _claiming("zzz", "shared", created_at="2026-01-01T00:00:00Z"),
    }}

    part, left_out = service_registry.servable_part_of_registry(stored)

    assert set(part["services"]) == {"zzz"} and set(left_out) == {"aaa"}


def test_a_service_with_no_creation_time_comes_after_one_that_has_one():
    stored = {"services": {"aaa": _claiming("aaa", "shared"), "zzz": _claiming("zzz", "shared", created_at=1_700_000_000)}}

    part, left_out = service_registry.servable_part_of_registry(stored)

    assert set(part["services"]) == {"zzz"} and set(left_out) == {"aaa"}


def test_a_claimant_that_is_left_out_holds_no_alias_against_a_third_service():
    stored = {"services": {
        "a": _claiming("a", "first"),
        "b": _claiming("b", "first", "second"),  # clashes with a on "first", so it is left out
        "c": _claiming("c", "second"),           # clashes only with b, which holds nothing: c serves
    }}

    part, left_out = service_registry.servable_part_of_registry(stored)

    assert set(part["services"]) == {"a", "c"} and set(left_out) == {"b"}


def test_a_strict_write_still_refuses_an_alias_clash_naming_both():
    registry = {"services": {"one": _claiming("one", "shared"), "two": _claiming("two", "shared")}}

    with pytest.raises(ValueError, match="'one' and 'two'"):
        service_registry.normalize_service_registry(registry)


def test_a_partial_boot_serves_the_earlier_claimant_of_a_shared_alias(live):
    write_service_registry_for_test(
        {"schema_version": 1, "services": {"one": _claiming("one", "shared"), "two": _claiming("two", "shared")}}, path=live)

    config, problem = _boot(live)

    assert "one" in config["backends"] and "two" not in config["backends"]
    assert set(problem["left_out"]) == {"two"}


# --- MED-2: a partial boot still backfills the managed LiteLLM row -------------------------------------


def test_a_partial_boot_serves_litellm_when_the_store_predates_its_row(live):
    _boot(live)  # a healthy boot seeds the store as an install does
    document = admin_store.get_service_registry_with_generation(path=live)["value"]
    document["services"].pop("litellm", None)
    document["services"]["junk-engine"] = JUNK
    write_service_registry_for_test(document, path=live)
    generation = admin_store.get_service_registry_with_generation(path=live)["operator_settings_generation"]

    config, problem = _boot(live)

    assert problem is not None and set(problem["left_out"]) == {"junk-engine"}
    assert "litellm" in config["backends"]  # chat is not answered 503 litellm_unconfigured
    after = admin_store.get_service_registry_with_generation(path=live)
    assert after["operator_settings_generation"] == generation, "a partial boot must not rewrite the store"
    assert after["value"]["services"]["junk-engine"] == JUNK and "litellm" not in after["value"]["services"]


# --- LOW-1: a failed reload during a partial boot is stale, with the new error, and still names what is left out ---


def test_a_failed_reload_after_a_partial_boot_is_stale_with_the_new_error_and_keeps_the_left_out_list(live, monkeypatch):
    partial = {"mode": "partial", "error": "the boot error", "left_out": {"bad-engine": "x"}, "served": 1}
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", partial, raising=False)
    with monkeypatch.context() as broken:
        broken.setattr(service_registry, "ensure_service_registry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down")))
        with pytest.raises(RuntimeError):
            proxy.reload_runtime_config()

    problem = proxy.REGISTRY_APPLY_PROBLEM
    assert problem["mode"] == "stale" and "store down" in problem["error"] and "the boot error" not in problem["error"]
    assert problem["left_out"] == {"bad-engine": "x"}

    with monkeypatch.context() as broken:  # a second failure replaces the error, still names the left-out record
        broken.setattr(service_registry, "ensure_service_registry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("still down")))
        with pytest.raises(RuntimeError):
            proxy.reload_runtime_config()
    assert "still down" in proxy.REGISTRY_APPLY_PROBLEM["error"] and proxy.REGISTRY_APPLY_PROBLEM["left_out"] == {"bad-engine": "x"}
    proxy.reload_runtime_config()
    assert proxy.REGISTRY_APPLY_PROBLEM is None


def test_the_overview_item_for_a_stale_reload_names_the_records_still_left_out():
    problem = {"mode": "stale", "error": "RuntimeError: store down", "left_out": {"bad-engine": "x"}, "served": 1}

    [item] = [i for i in derive_admin_attention({"registry_problem": problem, "registry_excluded": [
        {"section": "services", "id": "bad-engine", "error": "x"}]}) if i["kind"] == "registry"]

    assert "store down" in item["detail"] and "bad-engine" in item["detail"]


# --- LOW-2 and MED-3: the writes beside a left-out record ----------------------------------------------


def _keyed(key: str, record_id: str) -> dict:
    return {"id": record_id, "name": record_id, "kind": "engine", "enabled": True, "config": "oops"}


def test_adding_a_service_under_the_key_of_a_left_out_record_names_that_record_and_changes_nothing(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "x": _keyed("x", "y")})
    before = _stored(live)

    response = client.post("/admin/api/operator/services", json={"id": "x", "name": "x", "host": "10.0.0.7", "port": 8080},
                           headers=_if_match(live))

    assert response.status_code in (400, 409), response.text[:200]
    assert "'y'" in response.json()["error"]["message"], response.text[:300]  # the left-out record, not the new one
    assert _stored(live) == before


def test_a_save_never_overwrites_a_service_it_just_added_with_a_left_out_record_stored_under_its_key(live, monkeypatch):
    """The overwrite guard in ``_with_excluded_records`` itself: reached without the route's own checks."""
    _running_partial(live, monkeypatch, {"good-engine": GOOD, "x": _keyed("x", "y")})
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"]["x"] = _service("x")  # a valid service under the key the left-out record holds
    before = _stored(live)

    with pytest.raises(ValueError, match="'y'"):
        proxy.ADMIN_STORE.update_service_registry(
            data=registry, actor=OWNER, expected_generation=admin_store.get_service_registry_with_generation(path=live)["operator_settings_generation"])
    assert _stored(live) == before


def test_the_enable_toggle_on_a_left_out_service_is_refused_with_the_error_and_stores_nothing(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    before = _stored(live)

    response = client.post("/admin/api/operator/services/bad-engine/enabled", json={"enabled": False}, headers=_if_match(live))

    assert response.status_code == 400, response.text[:200]
    assert "left out" in response.json()["error"]["message"] and "bad-engine" in response.json()["error"]["message"]
    assert _stored(live) == before


def test_creating_an_engine_under_the_id_of_a_left_out_service_is_refused(live, monkeypatch):
    """``write_engine`` is the add-engine path: with no ``replace`` it must not take over a left-out id."""
    _running_partial(live, monkeypatch, {"good-engine": GOOD, "bad-engine": BAD})
    before = _stored(live)
    generation = admin_store.get_service_registry_with_generation(path=live)["operator_settings_generation"]

    with pytest.raises(ValueError, match="left out"):
        proxy.ADMIN_STORE.write_engine(
            service_id="bad-engine", raw=_service("bad-engine"), actor=OWNER, expected_generation=generation, replace=False)
    assert _stored(live) == before


def test_a_secret_argument_in_a_left_out_record_shows_as_a_configured_entry_and_never_as_a_value():
    entry = {"id": "ent_" + "c" * 16, "secret_ref": "sref_" + "d" * 24, "prefix": "--api-key="}
    record = {**_service("held", models=["x"]), "process": {"driver": "process", "start": ["engine", entry]}}

    view = projection_types.excluded_view("services", "held", "bad", record, configured=lambda ref: True)

    assert view["record"]["process"]["start"] == ["engine", {"id": "ent_" + "c" * 16, "prefix": "--api-key=", "configured": True}]
    assert "sref_" not in json.dumps(view) and view["secret_slots"] == ["process.start[1]"]
