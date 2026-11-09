"""Pure tests of the config-homes layer (docs/design/CONFIG_HOMES.md sections 3 and 4; acceptance list 10.1, C1).

The sections here are fixtures. No release section ships yet, so nothing in these tests touches a database, the
network or ``proxy``.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from gateway import config_layers as layers
from gateway.config_layers import ABSENT, Override, Problem

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "timeout": {"type": "integer", "minimum": 1},
        "note": {"type": ["string", "null"]},
        "mode": {"type": "string", "enum": ["fast", "slow"], "x-azizam-apply": "restart"},
        "policy": {"type": "string", "enum": ["on", "off"], "x-azizam-fail": "closed"},
        "token": {"type": "string", "x-azizam-secret": True},
        "tags": {"type": "array", "items": {"type": "string"}},
        "limits": {
            "type": "object", "additionalProperties": False, "required": ["min", "max"],
            "properties": {"min": {"type": "integer", "minimum": 0}, "max": {"type": "integer", "minimum": 0}},
        },
        "pair": {
            "type": "object", "x-azizam-entry": "atomic", "additionalProperties": False,
            "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
        },
        "aliases": {
            "type": "object", "x-azizam-entry": "collection",
            "additionalProperties": {
                "type": "object", "additionalProperties": False,
                "properties": {"target": {"type": "string"}, "engine": {"type": "string"}},
                # exactly one of the two: a deep merge of two members can break this
                "oneOf": [{"required": ["target"]}, {"required": ["engine"]}],
            },
        },
    },
}

DEFAULTS: dict[str, Any] = {
    "timeout": 30,
    "mode": "fast",
    "policy": "on",
    "tags": ["a", "b"],
    "limits": {"min": 5, "max": 10},
    "pair": {"left": "l", "right": "r"},
    "aliases": {"default": {"target": "svc-a"}, "fast": {"target": "svc-b"}},
}


def limits_in_order(candidate: Any, _context: Any) -> list[Problem]:
    """A rule across entries. It returns the problem with NO pointer, as the design allows."""
    limits = candidate.get("limits") or {}
    if limits.get("min", 0) > limits.get("max", 0):
        return [Problem(None, "limits_order", layers.INVALID, "min is above max")]
    return []


def make_section(defaults: dict[str, Any] | None = None, validator: Any = limits_in_order) -> layers.Section:
    return layers.Section.create("fixture", copy.deepcopy(defaults if defaults is not None else DEFAULTS), SCHEMA, validator=validator)


def put(pointer: str, value: Any, **kw: Any) -> Override:
    return Override(pointer, layers.OP_SET, value, **kw)


def drop(pointer: str, **kw: Any) -> Override:
    return Override(pointer, layers.OP_DELETE, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# Section files and the entry index
# ---------------------------------------------------------------------------------------------------------------------

def test_a_section_with_release_defaults_that_break_the_schema_is_a_packaging_fault():
    with pytest.raises(layers.SectionDataError, match="timeout"):
        make_section({**DEFAULTS, "timeout": 0})


def test_a_required_fail_closed_entry_is_refused_at_load_of_the_release_files():
    schema = copy.deepcopy(SCHEMA)
    schema["required"] = ["policy"]
    with pytest.raises(layers.SectionDataError, match="fail-closed entry must be optional"):
        layers.Section.create("fixture", DEFAULTS, schema)


def test_an_unknown_annotation_or_a_bad_annotation_value_is_refused():
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["timeout"]["x-azizam-apply"] = "sometimes"
    schema["properties"]["mode"]["x-azizam-wat"] = True
    with pytest.raises(layers.SectionDataError) as error:
        layers.Section.create("fixture", DEFAULTS, schema)
    assert "x-azizam-apply must be one of" in str(error.value)
    assert "unknown annotation x-azizam-wat" in str(error.value)


def test_the_entry_index_follows_the_four_shapes():
    index = make_section().index
    assert index.entries_in(DEFAULTS) == sorted([
        "/timeout", "/mode", "/policy", "/tags", "/limits/min", "/limits/max", "/pair", "/aliases/default", "/aliases/fast",
    ])
    assert index.rule_for("/timeout").kind == "scalar"
    assert index.rule_for("/tags").kind == "list"
    assert index.rule_for("/pair").kind == "atomic"
    assert index.rule_for("/aliases/new-id").kind == "member"
    assert index.rule_for("/limits") is None, "an object is not an entry, its properties are"
    assert index.rule_for("/aliases") is None, "the members are the entries, not the collection"
    assert index.rule_for("/pair/left") is None, "inside an atomic entry"
    assert index.rule_for("/aliases/default/target") is None, "a member is replaced whole"
    assert index.rule_for("/nope") is None
    assert index.rule_for("/tags/0") is None


def test_apply_fail_and_secret_come_from_the_nearest_annotated_ancestor():
    index = make_section().index
    assert index.rule_for("/mode").apply == "restart"
    assert index.rule_for("/timeout").apply == "live"
    assert index.rule_for("/policy").fail == "closed"
    assert index.rule_for("/timeout").fail == "inherit"
    assert index.rule_for("/token").secret is True
    assert index.rule_for("/timeout").secret is False


def test_a_pointer_with_escaped_characters_names_the_right_member():
    assert layers.split_pointer("/aliases/nomic-embed-text-v1.5") == ["aliases", "nomic-embed-text-v1.5"]
    assert layers.split_pointer("/defaults/k2-fsa~1OmniVoice") == ["defaults", "k2-fsa/OmniVoice"]
    assert layers.join_pointer(["a/b", "c~d"]) == "/a~1b/c~0d"
    with pytest.raises(layers.PointerError):
        layers.split_pointer("")
    with pytest.raises(layers.PointerError):
        layers.split_pointer("timeout")


# ---------------------------------------------------------------------------------------------------------------------
# The merge (4.2)
# ---------------------------------------------------------------------------------------------------------------------

def test_a_scalar_set_replaces_and_everything_else_is_untouched():
    result = layers.build_section(make_section(), [put("/timeout", 60)])
    assert result.effective["timeout"] == 60
    assert {**result.effective, "timeout": 30} == DEFAULTS
    assert not result.problems and not result.rejected


def test_a_list_is_replaced_never_appended_to():
    result = layers.build_section(make_section(), [put("/tags", ["z"])])
    assert result.effective["tags"] == ["z"]


def test_an_atomic_object_is_replaced_whole_with_no_field_merge():
    result = layers.build_section(make_section(), [put("/pair", {"left": "only"})])
    assert result.effective["pair"] == {"left": "only"}


def test_a_collection_member_is_replaced_whole():
    result = layers.build_section(make_section(), [put("/aliases/fast", {"engine": "eng-1"})])
    assert result.effective["aliases"]["fast"] == {"engine": "eng-1"}
    assert result.effective["aliases"]["default"] == {"target": "svc-a"}
    assert not result.problems


def test_an_owner_added_member_is_an_entry_and_is_labelled_so():
    section = make_section()
    overrides = [put("/aliases/mine", {"target": "svc-z"})]
    result = layers.build_section(section, overrides)
    assert result.effective["aliases"]["mine"] == {"target": "svc-z"}
    views = {view.pointer: view for view in layers.entry_views(section, result, overrides)}
    assert views["/aliases/mine"].source == layers.SOURCE_OWNER_ADDED
    assert views["/aliases/default"].source == layers.SOURCE_RELEASE


def test_set_of_a_literal_null_is_a_value_and_delete_is_a_removal():
    section = make_section()
    nulled = layers.build_section(section, [put("/note", None)])
    assert "note" in nulled.effective and nulled.effective["note"] is None
    removed = layers.build_section(section, [drop("/aliases/fast")])
    assert "fast" not in removed.effective["aliases"]
    assert layers.get_at(nulled.effective, "/note") is None
    assert layers.get_at(nulled.effective, "/missing") is ABSENT


def test_a_tombstone_survives_a_release_that_adds_the_entry():
    tombstone = [drop("/aliases/legacy", base=ABSENT)]
    older = layers.build_section(make_section(), tombstone)
    assert "legacy" not in older.effective["aliases"]
    newer_defaults = {**DEFAULTS, "aliases": {**DEFAULTS["aliases"], "legacy": {"target": "svc-new"}}}
    newer = layers.build_section(make_section(newer_defaults), tombstone)
    assert "legacy" not in newer.effective["aliases"], "the owner removed it; a later release does not bring it back"
    views = {view.pointer: view for view in layers.entry_views(make_section(newer_defaults), newer, tombstone)}
    assert views["/aliases/legacy"].source == layers.SOURCE_OWNER_DELETED
    assert views["/aliases/legacy"].drift.kind == layers.DRIFT_RELEASE_CHANGED, "the release now ships what the owner deleted"


def test_the_merge_does_not_change_its_inputs():
    section = make_section()
    before = copy.deepcopy(section.defaults)
    layers.build_section(section, [put("/tags", ["z"]), drop("/aliases/fast"), put("/pair", {"left": "x"})])
    assert section.defaults == before


# ---------------------------------------------------------------------------------------------------------------------
# Shape checks of one override
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("pointer", "code"), [
    ("timeout", "bad_pointer"), ("", "bad_pointer"), ("/nope", "not_an_entry"), ("/limits", "not_an_entry"),
    ("/pair/left", "not_an_entry"), ("/aliases/default/target", "not_an_entry"),
])
def test_a_pointer_that_is_not_an_entry_is_refused_with_its_code(pointer, code):
    faults = layers.check_override(make_section(), put(pointer, 1))
    assert [fault.code for fault in faults] == [code]


def test_a_secret_entry_takes_a_reference_and_never_a_plain_value():
    section = make_section()
    assert [f.code for f in layers.check_override(section, put("/token", "hunter2"))] == ["secret_value"]
    assert layers.check_override(section, put("/token", "sref_0123456789abcdef01234567")) == []


# ---------------------------------------------------------------------------------------------------------------------
# The load path (3.5)
# ---------------------------------------------------------------------------------------------------------------------

def test_an_invalid_override_is_rejected_and_reported_and_the_rest_apply():
    result = layers.build_section(make_section(), [put("/timeout", 0), put("/mode", "slow")])
    assert result.effective["timeout"] == 30, "inherit: the release value stands"
    assert result.effective["mode"] == "slow"
    assert [item.override.pointer for item in result.rejected] == ["/timeout"]
    assert any(p.code == "override_rejected" and p.pointer == "/timeout" for p in result.problems)


def test_a_stored_delete_of_a_retired_entry_is_inert_and_no_boot_problem():
    # Live 2026-11-08 (train 35): the owner's delete of compile_from_pin stayed stored after the release retired the
    # setting; every boot then reported override_rejected. A delete of what the release no longer has changes nothing.
    result = layers.build_section(make_section(), [drop("/retired_setting"), put("/mode", "slow")])
    assert result.effective["mode"] == "slow" and "retired_setting" not in result.effective
    assert result.rejected == () and not [p for p in result.problems if p.code == "override_rejected"]


def test_a_stored_set_of_a_retired_entry_is_still_rejected():
    result = layers.build_section(make_section(), [put("/retired_setting", 1)])
    assert [item.override.pointer for item in result.rejected] == ["/retired_setting"]
    assert any(p.code == "override_rejected" for p in result.problems)


def test_a_rejected_fail_closed_entry_becomes_absent_and_never_takes_the_release_value():
    section = make_section()
    overrides = [put("/policy", "bogus")]
    result = layers.build_section(section, overrides)
    assert "policy" not in result.effective, "closed: unconfigured, not the release 'on'"
    views = {view.pointer: view for view in layers.entry_views(section, result, overrides)}
    assert views["/policy"].source == layers.SOURCE_UNCONFIGURED
    # and a valid override of the same entry still applies
    assert layers.build_section(section, [put("/policy", "off")]).effective["policy"] == "off"


def test_a_fail_closed_entry_with_a_valid_override_is_not_dropped():
    result = layers.build_section(make_section(), [put("/policy", "off"), put("/timeout", 0)])
    assert result.effective["policy"] == "off"
    assert [item.override.pointer for item in result.rejected] == ["/timeout"]


def test_an_override_that_needs_a_later_one_survives_the_greedy_rebuild():
    # release min=5 max=10. max=3 alone is invalid (below min 5); min=2 makes it valid. Pointer order tries max first.
    # The third override is bad on its own, so the normal path fails and the greedy rebuild runs.
    overrides = [put("/limits/max", 3), put("/limits/min", 2), put("/timeout", 0)]
    result = layers.build_section(make_section(), overrides)
    assert result.effective["limits"] == {"min": 2, "max": 3}
    assert [item.override.pointer for item in result.rejected] == ["/timeout"]


def test_a_problem_with_no_pointer_still_rejects_only_the_bad_override():
    # limits_in_order reports its problem with pointer None. Only the override that breaks the order is rejected.
    result = layers.build_section(make_section(), [put("/limits/min", 50), put("/timeout", 7)])
    assert result.effective["limits"] == {"min": 5, "max": 10}
    assert result.effective["timeout"] == 7
    assert [item.override.pointer for item in result.rejected] == ["/limits/min"]
    assert any(p.code == "limits_order" for item in result.rejected for p in item.reasons)


def test_the_greedy_loop_ends_when_nothing_more_can_be_kept():
    # every set is bad; the loop must stop after one pass that keeps none
    overrides = [put("/timeout", 0), put("/mode", "never"), put("/limits/min", 99)]
    result = layers.build_section(make_section(), overrides)
    assert result.effective == DEFAULTS
    assert len(result.rejected) == 3


def test_a_delete_of_a_required_entry_is_refused_and_the_entry_keeps_the_release_value():
    # deleting a required property would publish a section that fails its schema (readers then get a KeyError)
    section = make_section()
    assert [p.code for p in layers.check_override(section, drop("/limits/max"))] == ["delete_required"]
    result = layers.build_section(section, [drop("/limits/max"), put("/timeout", 12)])
    assert result.effective["limits"] == DEFAULTS["limits"] and result.effective["timeout"] == 12
    assert [(item.override.pointer, [p.code for p in item.reasons]) for item in result.rejected] == [
        ("/limits/max", ["delete_required"])
    ]
    assert not any(p.code.startswith("schema.") for p in result.problems), "the published section is valid"
    view = {v.pointer: v for v in layers.entry_views(section, result, [drop("/limits/max")])}["/limits/max"]
    assert view.source == layers.SOURCE_RELEASE


def test_a_delete_of_a_collection_member_or_an_optional_property_is_accepted():
    section = make_section()
    for pointer in ("/aliases/fast", "/timeout", "/tags", "/pair"):
        assert layers.check_override(section, drop(pointer)) == [], pointer
    result = layers.build_section(section, [drop("/aliases/fast"), drop("/timeout")])
    assert "fast" not in result.effective["aliases"] and "timeout" not in result.effective
    assert [o.pointer for o in result.applied] == ["/aliases/fast", "/timeout"] and not result.rejected


def test_a_delete_that_breaks_a_rule_on_load_is_rejected_and_the_entry_inherits():
    def timeout_present(candidate: Any, _context: Any) -> list[Problem]:
        return [] if "timeout" in candidate else [Problem("/timeout", "timeout_missing", layers.INVALID, "a timeout is needed")]

    section = make_section(validator=timeout_present)
    result = layers.build_section(section, [drop("/timeout"), put("/mode", "slow")])
    assert result.effective["timeout"] == 30 and result.effective["mode"] == "slow"
    assert [(item.override.pointer, item.override.op) for item in result.rejected] == [("/timeout", "delete")]
    views = {v.pointer: v for v in layers.entry_views(section, result, [drop("/timeout")])}
    assert views["/timeout"].source == layers.SOURCE_RELEASE


@pytest.mark.parametrize("pointer", ["", "no-slash", "/", "/nope", "/limits", "/aliases", "/tags/0", "//"])
def test_boot_never_raises_for_an_override_fault(pointer):
    for override in (put(pointer, 1), drop(pointer)):
        result = layers.build_section(make_section(), [override, put("/timeout", 12)])
        assert result.effective["timeout"] == 12
        assert [item.override.pointer for item in result.rejected] == [pointer]


def test_a_validator_that_raises_is_a_problem_and_not_a_crash():
    def broken(_candidate: Any, _context: Any) -> list[Problem]:
        raise RuntimeError("oops")

    result = layers.build_section(make_section(validator=broken), [put("/timeout", 12)])
    assert any(p.code == "validator_error" for p in result.problems)


def test_the_same_input_gives_the_same_result_twice_in_any_order():
    overrides = [put("/limits/max", 3), put("/limits/min", 2), put("/timeout", 0), put("/policy", "bogus"), drop("/aliases/fast")]
    first = layers.build_section(make_section(), overrides)
    second = layers.build_section(make_section(), list(reversed(overrides)))
    assert first == second
    assert layers.digest(first.effective) == layers.digest(second.effective)


def test_a_dangling_problem_does_not_reject_the_override():
    def dangling(candidate: Any, _context: Any) -> list[Problem]:
        target = (candidate.get("aliases") or {}).get("fast", {}).get("target")
        return [Problem("/aliases/fast", "no_such_service", layers.DANGLING, "no deployed service")] if target == "gone" else []

    section = make_section(validator=dangling)
    overrides = [put("/aliases/fast", {"target": "gone"})]
    result = layers.build_section(section, overrides)
    assert result.effective["aliases"]["fast"] == {"target": "gone"}
    assert not result.rejected
    assert [p.severity for p in result.problems] == [layers.DANGLING]
    view = {v.pointer: v for v in layers.entry_views(section, result, overrides)}["/aliases/fast"]
    assert [p.code for p in view.problems] == ["no_such_service"]


# ---------------------------------------------------------------------------------------------------------------------
# Positive control: a Helm-style deep merge breaks the invalid-combination guarantee (4.2, trap 2)
# ---------------------------------------------------------------------------------------------------------------------

def helm_deep_merge(base: Any, overrides: Any) -> dict:
    """The merge this design refuses: maps merge key by key and a null deletes. Used only as a test double."""
    document = copy.deepcopy(dict(base))

    def merge(into: dict, patch: dict) -> None:
        for key, value in patch.items():
            if value is None:
                into.pop(key, None)
            elif isinstance(value, dict) and isinstance(into.get(key), dict):
                merge(into[key], value)
            else:
                into[key] = copy.deepcopy(value)

    for override in sorted((o for o in overrides if o.op == layers.OP_SET), key=lambda o: o.pointer):
        patch: Any = override.value
        for segment in reversed(layers.split_pointer(override.pointer)):
            patch = {segment: patch}
        merge(document, patch)
    return document


def member_replacement_is_clean(merge: layers.MergeFn) -> None:
    """The guarantee: the owner swaps a member's ``target`` for an ``engine`` and gets exactly that, with no problem."""
    result = layers.build_section(make_section(), [put("/aliases/fast", {"engine": "eng-1"})], merge=merge)
    assert result.effective["aliases"]["fast"] == {"engine": "eng-1"}
    assert not result.rejected and not result.problems


def test_the_entry_merge_gives_the_member_the_owner_wrote():
    member_replacement_is_clean(layers.apply_overrides)


def test_positive_control_a_helm_style_deep_merge_fails_the_same_test():
    # Deep merge keeps the release's `target` next to the owner's `engine`, which the schema forbids (oneOf).
    with pytest.raises(AssertionError):
        member_replacement_is_clean(helm_deep_merge)


# ---------------------------------------------------------------------------------------------------------------------
# Drift (4.4)
# ---------------------------------------------------------------------------------------------------------------------

def test_drift_release_changed_when_the_release_value_under_an_override_moved():
    section = make_section({**DEFAULTS, "timeout": 45})
    drift = layers.drift_report(section, [put("/timeout", 60, base=30)])
    assert [(d.pointer, d.kind, d.base, d.release, d.owner) for d in drift] == [
        ("/timeout", layers.DRIFT_RELEASE_CHANGED, 30, 45, 60)
    ]


def test_drift_release_removed_when_the_release_no_longer_ships_the_entry():
    defaults = {k: v for k, v in DEFAULTS.items() if k != "tags"}
    section = make_section(defaults)
    overrides = [put("/tags", ["x"], base=["a", "b"])]
    drift = layers.drift_report(section, overrides)
    assert [(d.pointer, d.kind) for d in drift] == [("/tags", layers.DRIFT_RELEASE_REMOVED)]
    result = layers.build_section(section, overrides)
    view = {v.pointer: v for v in layers.entry_views(section, result, overrides)}["/tags"]
    assert view.source == layers.SOURCE_OWNER_ADDED, "an override of a removed entry now acts as owner-added"


def test_no_drift_when_the_base_still_equals_the_release_and_a_tombstone_keeps_its_base():
    section = make_section()
    assert layers.drift_report(section, [put("/timeout", 60, base=30), drop("/aliases/fast", base={"target": "svc-b"})]) == []
    moved = make_section({**DEFAULTS, "aliases": {**DEFAULTS["aliases"], "fast": {"target": "svc-new"}}})
    drift = layers.drift_report(moved, [drop("/aliases/fast", base={"target": "svc-b"})])
    assert [(d.pointer, d.kind) for d in drift] == [("/aliases/fast", layers.DRIFT_RELEASE_CHANGED)]


def test_an_override_written_where_the_release_had_nothing_has_no_drift_until_the_release_adds_it():
    section = make_section()
    assert layers.drift_report(section, [put("/aliases/mine", {"target": "x"}, base=ABSENT)]) == []
    added = make_section({**DEFAULTS, "aliases": {**DEFAULTS["aliases"], "mine": {"target": "rel"}}})
    drift = layers.drift_report(added, [put("/aliases/mine", {"target": "x"}, base=ABSENT)])
    assert drift[0].kind == layers.DRIFT_RELEASE_CHANGED


# ---------------------------------------------------------------------------------------------------------------------
# Export format (6.1)
# ---------------------------------------------------------------------------------------------------------------------

def test_the_export_holds_overrides_only_and_parses_back_to_the_same_rows():
    section = make_section()
    rows = {"fixture": [put("/timeout", 60, base=30), drop("/aliases/fast", base={"target": "svc-b"}), put("/note", None)]}
    document = layers.export_document(rows, {"fixture": section}, exported_at="2026-11-05T00:00:00Z", gateway_version="test")
    assert document["format"] == layers.EXPORT_FORMAT
    assert document["release_digests"] == {"fixture": section.release_digest}
    assert "aliases" not in document["sections"]["fixture"][0], "no release content, only the owner's rows"
    parsed = layers.parse_export(document)["fixture"]
    assert [(o.pointer, o.op, o.value, o.base) for o in parsed] == [
        ("/aliases/fast", "delete", None, {"target": "svc-b"}),
        ("/note", "set", None, ABSENT),
        ("/timeout", "set", 60, 30),
    ]


@pytest.mark.parametrize("document", [
    None, {"format": "other/v9"}, {"format": layers.EXPORT_FORMAT, "sections": []},
    {"format": layers.EXPORT_FORMAT, "sections": {"a": [{"pointer": "/x", "op": "set"}]}},
    {"format": layers.EXPORT_FORMAT, "sections": {"a": [{"pointer": "/x", "op": "merge", "value": 1}]}},
])
def test_an_export_this_release_cannot_read_is_refused(document):
    with pytest.raises(layers.ExportFormatError):
        layers.parse_export(document)


# ---------------------------------------------------------------------------------------------------------------------
# A fail-closed entry whose override fails its shape check stays closed (review finding 6)
# ---------------------------------------------------------------------------------------------------------------------

def test_a_fail_closed_secret_entry_with_a_plain_stored_value_is_absent_and_unconfigured():
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["token"] = {"type": ["string", "null"], "x-azizam-secret": True, "x-azizam-fail": "closed"}
    defaults = {**copy.deepcopy(DEFAULTS), "token": None}
    section = layers.Section.create("fixture", defaults, schema, validator=limits_in_order)
    overrides = [put("/token", "plain-text-key")]
    result = layers.build_section(section, overrides)
    assert [p.code for item in result.rejected for p in item.reasons] == ["secret_value"]
    assert "token" not in result.effective, "closed means absent, never the release null"
    view = {v.pointer: v for v in layers.entry_views(section, result, overrides)}["/token"]
    assert view.source == layers.SOURCE_UNCONFIGURED and view.value is ABSENT


# ---------------------------------------------------------------------------------------------------------------------
# Entries never nest (review finding 7)
# ---------------------------------------------------------------------------------------------------------------------

def _schema_with(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties}


def test_an_open_object_that_is_not_a_collection_is_refused_when_the_section_is_loaded():
    open_object = _schema_with({"extra": {"type": "object", "properties": {"x": {"type": "integer"}}}})
    with pytest.raises(layers.SectionDataError, match="additionalProperties"):
        layers.Section.create("fixture", {"extra": {"x": 1}}, open_object)
    with pytest.raises(layers.SectionDataError, match="additionalProperties"):
        layers.Section.create("fixture", {}, {"type": "object", "properties": {}})  # the root too
    layers.Section.create("fixture", {"extra": {"x": 1}}, _schema_with(
        {"extra": {"type": "object", "additionalProperties": False, "properties": {"x": {"type": "integer"}}}}))
    layers.Section.create("fixture", {"extra": {"x": 1}}, _schema_with(
        {"extra": {"type": "object", "x-azizam-entry": "atomic"}}))  # an atomic entry may be open inside


def test_nothing_below_a_scalar_entry_is_an_entry():
    section = layers.Section.create("fixture", {"extra": {"x": 1}}, _schema_with({"extra": {}}))  # an untyped entry
    assert section.index.rule_for("/extra") is not None
    assert section.index.rule_for("/extra/x") is None, "/extra and /extra/x would be a path_conflict on load"
    assert [p.code for p in layers.check_override(section, put("/extra/x", 2))] == ["not_an_entry"]
