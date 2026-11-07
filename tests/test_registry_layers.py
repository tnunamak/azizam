"""The layered registry document (gateway/registry_layers.py; docs/design/CONFIG_HOMES.md, 3.4): pure behaviour."""

from __future__ import annotations

import copy

from gateway import registry_layers as layers

RELEASE = {
    "services": {
        "engine": {"id": "engine", "name": "Engine", "enabled": True, "note": "shipped",
                   "config": {"base_url": "http://127.0.0.1:1/v1", "timeout": 30, "health_endpoint": "/health"}},
        "provider": {"id": "provider", "name": "Provider", "enabled": True, "config": {"base_url": "https://a.test"}},
    },
    "protocol_frontends": {"api": {"id": "api", "routes": ["/api/v1"], "source": "builtin"}},
    "workflows": {},
    "capabilities": {},
}


def _stored(**parts):
    document = layers.empty_stored(2)
    document.update(parts)
    return document


def test_an_owner_document_that_changes_nothing_inherits_every_release_record():
    effective = layers.layer(layers.empty_stored(2), RELEASE)

    assert effective["services"] == RELEASE["services"]
    assert effective["protocol_frontends"] == RELEASE["protocol_frontends"]


def test_a_patch_changes_only_its_fields_and_a_release_change_to_another_field_reaches_the_install():
    stored = _stored(services={"engine": {"name": "My engine", "enabled": False}})
    newer_release = copy.deepcopy(RELEASE)
    newer_release["services"]["engine"]["config"]["timeout"] = 90
    newer_release["services"]["engine"]["note"] = "shipped, newer"

    effective = layers.layer(stored, newer_release)["services"]["engine"]

    assert effective["name"] == "My engine" and effective["enabled"] is False  # the owner's two fields
    assert effective["config"]["timeout"] == 90 and effective["note"] == "shipped, newer"  # the release's other fields


def test_an_owner_added_record_is_stored_whole_and_a_release_record_with_its_id_is_not_merged_into_it():
    mine = {"id": "mine", "name": "Mine", "config": {"base_url": "http://x"}}

    effective = layers.layer(_stored(services={"mine": mine}), RELEASE)

    assert effective["services"]["mine"] == mine
    assert set(effective["services"]) == {"engine", "provider", "mine"}


def test_a_tombstone_hides_a_release_record_and_survives_a_release_that_changes_or_re_adds_it():
    stored = _stored(tombstones={"services": ["provider"]})
    newer_release = copy.deepcopy(RELEASE)
    newer_release["services"]["provider"]["name"] = "Provider 2"

    assert "provider" not in layers.layer(stored, newer_release)["services"]
    assert "engine" in layers.layer(stored, newer_release)["services"]


def test_unlayer_stores_what_differs_and_layer_gives_the_document_back():
    effective = layers.layer(layers.empty_stored(2), RELEASE)
    effective["services"]["engine"]["name"] = "My engine"
    effective["services"]["engine"]["config"]["timeout"] = 5
    effective["services"]["mine"] = {"id": "mine", "name": "Mine"}
    del effective["services"]["provider"]

    stored = layers.unlayer(effective, RELEASE)

    # A patch also names its record (id), so the code before the layer reads it as the release record it copies.
    assert stored["services"]["engine"] == {"id": "engine", "name": "My engine", "config": {"timeout": 5}}
    assert stored["services"]["mine"] == {"id": "mine", "name": "Mine"}
    assert stored["tombstones"] == {"services": ["provider"]}
    assert "api" not in stored["protocol_frontends"], "an unchanged record is inherited, not stored"
    assert layers.layer(stored, RELEASE) == {key: value for key, value in effective.items()}


def test_a_field_the_owner_removed_is_a_null_in_the_patch_and_a_value_that_is_null_is_a_replace_wrapper():
    effective = layers.layer(layers.empty_stored(2), RELEASE)
    del effective["services"]["engine"]["note"]
    effective["services"]["provider"]["config"]["credential"] = None  # a literal null: a merge patch cannot say it

    stored = layers.unlayer(effective, RELEASE)

    assert stored["services"]["engine"] == {"id": "engine", "note": None}
    assert set(stored["services"]["provider"]) == {layers.REPLACE_KEY}
    assert layers.layer(stored, RELEASE) == effective


def test_a_replace_wrapper_stands_alone_when_the_release_drops_the_record():
    stored = _stored(services={"gone": {layers.REPLACE_KEY: {"id": "gone", "name": "Gone"}}})

    assert layers.layer(stored, RELEASE)["services"]["gone"] == {"id": "gone", "name": "Gone"}


def test_a_write_that_does_not_touch_a_patch_keeps_the_release_digest_it_was_made_against():
    older = copy.deepcopy(RELEASE)
    older["services"]["engine"]["name"] = "Engine v1"
    effective = layers.layer(layers.empty_stored(2), older)
    effective["services"]["engine"]["enabled"] = False
    first = layers.unlayer(effective, older)

    # the release moves; the owner saves something else, and the patch for "engine" is unchanged
    again = layers.layer(first, RELEASE)
    again["services"]["provider"]["name"] = "Renamed"
    second = layers.unlayer(again, RELEASE, first)

    assert second["bases"]["services"]["engine"] == first["bases"]["services"]["engine"]
    view = layers.sources(second, RELEASE)["services"]
    assert view["engine"] == {"source": "owner", "fields": ["enabled"], "drift": "release_changed"}
    assert view["provider"] == {"source": "owner", "fields": ["name"]}


def test_sources_names_each_record_origin_and_never_a_value():
    stored = _stored(
        services={"engine": {"name": "Secret-looking value", "config": {"timeout": 1}}, "mine": {"id": "mine"}},
        tombstones={"services": ["provider"]},
    )

    view = layers.sources(stored, RELEASE)

    assert view["services"]["engine"] == {"source": "owner", "fields": ["config", "config.timeout", "name"]}
    assert view["services"]["mine"] == {"source": "owner-added"}
    assert view["services"]["provider"] == {"source": "owner-deleted"}
    assert view["protocol_frontends"]["api"] == {"source": "release"}
    assert "Secret-looking value" not in repr(view)


def test_an_old_whole_document_reads_as_whole_records_plus_the_release_records_it_lacks():
    whole = {"services": {"engine": {"id": "engine", "name": "Old engine"}}}

    effective = layers.layer(whole, RELEASE)

    assert effective["services"]["engine"]["name"] == "Old engine"
    assert "config" not in effective["services"]["engine"]  # replaces the release record: a patch cannot delete a key
    assert "provider" in effective["services"]


def test_the_owner_layer_round_trips_through_the_export_form():
    stored = _stored(services={"engine": {"name": "X"}, "mine": {"id": "mine"}}, tombstones={"services": ["provider"]})

    exported = layers.owner_layer(stored)
    back = layers.from_owner_layer(exported, 2)

    assert exported["records"]["services"]["engine"] == {"name": "X"}
    assert exported["tombstones"] == {"services": ["provider"]}
    assert layers.layer(back, RELEASE) == layers.layer(stored, RELEASE)


def test_an_unlayer_that_is_given_an_undeletable_record_leaves_it_inherited():
    effective = layers.layer(layers.empty_stored(2), RELEASE)
    del effective["services"]["engine"]

    stored = layers.unlayer(effective, RELEASE, undeletable=lambda collection, record_id, _record: record_id == "engine")

    assert stored["tombstones"] == {}
    assert "engine" in layers.layer(stored, RELEASE)["services"]


def test_a_patch_of_a_release_record_repeats_its_identity_keys_and_the_export_and_the_field_list_leave_them_out():
    effective = layers.layer(layers.empty_stored(2), RELEASE)
    effective["services"]["engine"]["name"] = "My engine"
    effective["services"]["engine"]["source"] = "builtin"  # equal to nothing in this release record: a real change below
    stored = layers.unlayer(effective, RELEASE)

    assert stored["services"]["engine"]["id"] == "engine"
    assert layers.owner_layer(stored, RELEASE)["records"]["services"]["engine"] == {"name": "My engine", "source": "builtin"}
    assert layers.field_names(stored["services"]["engine"], RELEASE["services"]["engine"]) == ["name", "source"]
    assert layers.layer(layers.from_owner_layer(layers.owner_layer(stored, RELEASE), 2), RELEASE)["services"]["engine"]["name"] == "My engine"
