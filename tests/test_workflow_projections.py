"""Projections (CODE_TO_DATA_S4_S8, I3, S5): pure functions of a registry entry. No cache, no live data."""

from __future__ import annotations

import copy

import pytest

from gateway import registry_snapshot as rs
from gateway import workflow_projections as wp
from gateway.result_bundle import ResultLimits
from gateway.workflow_kernel import generated_names
from gateway.workflow_runtime_types import BindingDef
from tests.test_registry_snapshot import CONTRACT, bundled_of, contract, stored

LIMITS = ResultLimits(max_assets=7)


class Available:
    """Test double for the run core's port: a binding is a problem only when its adapter id is in ``missing``."""

    def __init__(self, *missing: str) -> None:
        self.missing = set(missing)

    def binding_problem(self, binding: BindingDef):
        return "adapter_unavailable" if binding.adapter_id in self.missing else None


def entry_of(c=None, **record):
    c = c or CONTRACT
    document = {"workflows": {c["id"]: {**stored(c["id"]), "manifest": c, **record}}}
    return rs.build(document, bundled_of(), generation=1).get(c["id"])


def with_io(inputs=None, outputs=None, bindings=None, **extra):
    c = contract("mine.io", **extra)
    if inputs is not None:
        c["inputs"] = inputs
    if outputs is not None:
        c["outputs"] = outputs
    if bindings is not None:
        c["bindings"] = bindings
    return c


def test_the_profile_names_the_adapter_the_class_and_the_binding_facet():
    c = with_io()
    c["bindings"]["comfyui"]["facet"] = "graph"
    profile = wp.PROJECTIONS.execution_profile(entry_of(c), "comfyui", limits=LIMITS)
    assert (profile.adapter_id, profile.adapter_facet, profile.resource_class) == ("comfyui", "graph", "media")
    plain = wp.PROJECTIONS.execution_profile(entry_of(), "comfyui", limits=LIMITS)
    assert plain.adapter_facet == ""


def test_an_unknown_binding_key_raises():
    with pytest.raises(KeyError):
        wp.PROJECTIONS.execution_profile(entry_of(), "nope", limits=LIMITS)


def test_the_profile_of_an_entry_without_facts_has_an_empty_resource_class():
    bare = stored("mine.bare")
    del bare["policy"]
    entry = rs.build({"workflows": {"mine.bare": bare}}, bundled_of(), generation=1).get("mine.bare")
    assert wp.PROJECTIONS.execution_profile(entry, "comfyui", limits=LIMITS).resource_class == ""


def test_media_inputs_become_accept_rules():
    c = with_io(inputs={
        "prompt": {"type": "string", "required": True},
        "first": {"type": "asset", "required": True, "accepts": ["image/png", "image/jpeg"], "max_bytes": 100},
        "more": {"type": "array", "items": {"type": "asset", "accepts": ["audio/*"]}, "min_items": 2, "max_items": 4},
        "opt": {"type": "asset", "accepts": ["video/mp4"]},
    })
    accepts = wp.PROJECTIONS.execution_profile(entry_of(c), "comfyui", limits=LIMITS).accepts
    assert sorted(accepts) == ["first", "more", "opt"]
    assert (accepts["first"].media_types, accepts["first"].max_bytes, accepts["first"].min_items, accepts["first"].max_items) == (
        ("image/png", "image/jpeg"), 100, 1, 1)
    assert (accepts["more"].media_types, accepts["more"].min_items, accepts["more"].max_items) == (("audio/*",), 2, 4)
    assert (accepts["opt"].min_items, accepts["opt"].max_items) == (0, 1)


def test_an_array_of_media_without_a_maximum_takes_the_deployment_limit():
    c = with_io(inputs={"many": {"type": "array", "items": {"type": "asset", "accepts": ["image/*"]}}})
    assert wp.PROJECTIONS.execution_profile(entry_of(c), "comfyui", limits=LIMITS).accepts["many"].max_items == 7


def test_outputs_become_shapes_with_the_resolved_count():
    c = with_io(outputs={
        "pics": {"media_type": "image/png"},
        "two": {"media_type": "image/png", "count": {"min": 2, "max": 3}},
        "doc": {"media_type": "application/json"},
    })
    produces = wp.PROJECTIONS.execution_profile(entry_of(c), "comfyui", limits=LIMITS).produces
    assert (produces["pics"].kind, produces["pics"].min_items, produces["pics"].max_items) == ("assets", 1, 7)
    assert (produces["two"].min_items, produces["two"].max_items) == (2, 3)
    assert (produces["doc"].kind, produces["doc"].media_type, produces["doc"].min_items, produces["doc"].max_items) == (
        "json", "application/json", 1, 1)


def test_the_receipt_identity_is_the_entry_revision_digest_and_the_binding():
    entry = entry_of(record_revision=4)
    ident = wp.PROJECTIONS.receipt_identity(entry, "comfyui")
    assert (ident.workflow_id, ident.record_revision, ident.contract_digest, ident.binding_key, ident.intent) == (
        entry.id, 4, entry.contract_digest, "comfyui", "poster.generate")


def test_entry_problems_orders_policy_then_binding_and_names_an_unknown_key():
    bare = stored("mine.bare")
    del bare["policy"]
    no_policy = rs.build({"workflows": {"mine.bare": bare}}, bundled_of(), generation=1).get("mine.bare")
    entry = entry_of()
    p = wp.PROJECTIONS
    assert p.entry_problems(no_policy, Available("comfyui")) == ("needs_policy", "adapter_unavailable")
    assert p.entry_problems(entry, Available()) == ()
    assert p.entry_problems(entry, Available("comfyui")) == ("adapter_unavailable",)
    assert p.entry_problems(entry, Available(), binding_key="other") == ("binding_unknown",)
    assert p.entry_problems(entry, Available("comfyui"), binding_key="comfyui") == ("adapter_unavailable",)


def test_the_default_binding_is_the_first_declared_one():
    c = with_io(bindings={"first": CONTRACT["bindings"]["comfyui"], "second": CONTRACT["bindings"]["comfyui"]})
    entry = entry_of(c)
    assert wp.PROJECTIONS.entry_problems(entry, Available("first")) == ("adapter_unavailable",)
    assert wp.PROJECTIONS.entry_problems(entry, Available("second")) == ()


def test_binding_problems_judges_only_the_pinned_binding():
    entry = entry_of()
    binding = entry.bindings["comfyui"]
    assert wp.PROJECTIONS.binding_problems(binding, Available()) == ()
    assert wp.PROJECTIONS.binding_problems(binding, Available("comfyui")) == ("adapter_unavailable",)


def test_the_input_form_is_the_settable_inputs_as_a_form_schema():
    c = with_io(inputs={"prompt": {"type": "string", "required": True}, "file": {"type": "asset", "accepts": ["image/png"]},
                        "hidden": {"type": "string", "settable": False}})
    form = wp.PROJECTIONS.input_form(entry_of(c))
    assert sorted(form["properties"]) == ["file", "prompt"]
    assert form["properties"]["file"]["format"] == "binary" and form["required"] == ["prompt"]
    assert form["x-azizam-workflow-id"] == "mine.io"


def test_the_catalog_lists_every_entry_in_id_order_with_its_static_facts():
    snap = rs.build({"workflows": {"mine.one": stored("mine.one", published=True)}}, bundled_of(CONTRACT), generation=1)
    rows = wp.PROJECTIONS.catalog_static(snap)
    assert [r["id"] for r in rows] == sorted(snap.workflows)
    one = next(r for r in rows if r["id"] == "mine.one")
    assert (one["published"], one["state"], one["needs_configuration"], one["bindings"]) == (True, "active", False, ["comfyui"])
    assert one["record_revision"] == 1 and one["contract_digest"] == snap.get("mine.one").contract_digest


def test_the_openapi_paths_use_only_the_kernel_generated_names_and_hold_each_workflow_once():
    snap = rs.build({"workflows": {"mine.one": stored("mine.one", published=True)}}, bundled_of(CONTRACT), generation=1)
    paths = wp.PROJECTIONS.openapi_paths(snap)
    ids = {op["post"]["x-azizam-workflow-id"] for op in paths.values()}
    assert ids == set(snap.workflows)
    for path, op in paths.items():
        wid = op["post"]["x-azizam-workflow-id"]
        assert op["post"]["operationId"] == generated_names(wid)["operation"]
        assert path.endswith("/run") and wid in path


def test_a_projection_is_a_pure_function_of_the_entry():
    entry = entry_of()
    assert wp.PROJECTIONS.input_form(entry) == wp.PROJECTIONS.input_form(entry)
    assert wp.PROJECTIONS.execution_profile(entry, "comfyui", limits=LIMITS) == wp.PROJECTIONS.execution_profile(
        entry, "comfyui", limits=LIMITS)
