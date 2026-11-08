"""The job envelope grammar and the restart table (S2.1)."""

from __future__ import annotations

import copy
import itertools

import pytest

from gateway.workflow_envelope import (
    Envelope,
    classify_recovery,
    definition_shas,
    parse_envelope,
)

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
GENERATORS = {"gen-kept": "value", "gen-hashed": "digest", "gen-dropped": "none"}


def valid() -> dict:
    return {
        "envelope_version": 1,
        "source": {"kind": "workflow", "id": "wf.example", "revision": 3},
        "definitions": {
            "contract": SHA_A,
            "binding": SHA_B,
            "template": SHA_C,
            "protocol": {"id": "proto-x", "revision": "r7"},
        },
        "executor": {"adapter_id": "adapter-x", "interface_version": 1},
        "upstream": {"service_id": "svc-1", "origin_digest": "sha256:" + "d" * 64},
        "generated": {
            "n": {"generator": "gen-kept", "value": 12345},
            "h": {"generator": "gen-hashed", "value": SHA_A},
            "z": {"generator": "gen-dropped"},
        },
        "results": {"bundle": None},
    }


def test_round_trip_is_exact():
    data = valid()
    envelope = parse_envelope(data, GENERATORS)
    assert envelope.to_json() == data
    assert parse_envelope(envelope.to_json(), GENERATORS) == envelope


def test_round_trip_without_template_and_with_bundle():
    data = valid()
    del data["definitions"]["template"]
    data["generated"] = {}
    data["results"]["bundle"] = "bundle-ref-1"
    data["source"] = {"kind": "offering", "id": "off-1", "revision": "r2"}
    envelope = parse_envelope(data)
    assert envelope.template is None
    assert envelope.to_json() == data


def test_to_json_does_not_alias_the_envelope():
    envelope = parse_envelope(valid(), GENERATORS)
    out = envelope.to_json()
    out["generated"]["n"]["value"] = 0
    assert envelope.generated["n"]["value"] == 12345


def test_none_class_value_none_is_normalized_to_absent():
    data = valid()
    data["generated"]["z"] = {"generator": "gen-dropped", "value": None}
    envelope = parse_envelope(data, GENERATORS)
    assert envelope.generated["z"] == {"generator": "gen-dropped"}
    assert parse_envelope(envelope.to_json(), GENERATORS) == envelope


def test_definition_shas_lists_every_blob():
    assert definition_shas(parse_envelope(valid(), GENERATORS)) == {SHA_A, SHA_B, SHA_C}
    data = valid()
    del data["definitions"]["template"]
    assert definition_shas(parse_envelope(data, GENERATORS)) == {SHA_A, SHA_B}


def refusal(mutate, match, generators=GENERATORS):
    data = valid()
    mutate(data)
    with pytest.raises(ValueError, match=match):
        parse_envelope(data, generators)


def test_not_an_object():
    with pytest.raises(ValueError, match="envelope: expected an object"):
        parse_envelope([], GENERATORS)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "match"),
    [
        ((), "envelope: unknown key 'extra'"),
        (("source",), "source: unknown key 'extra'"),
        (("definitions",), "definitions: unknown key 'extra'"),
        (("definitions", "protocol"), "definitions.protocol: unknown key 'extra'"),
        (("executor",), "executor: unknown key 'extra'"),
        (("upstream",), "upstream: unknown key 'extra'"),
        (("results",), "results: unknown key 'extra'"),
        (("generated", "n"), "generated.n: unknown key 'extra'"),
    ],
)
def test_unknown_key_refused_at_every_level(path, match):
    def add(data):
        node = data
        for part in path:
            node = node[part]
        node["extra"] = 1

    refusal(add, match)


@pytest.mark.parametrize(
    ("path", "match"),
    [
        (("envelope_version",), "envelope_version: required"),
        (("source",), "source: required|envelope.source: required"),
        (("source", "kind"), "source.kind: required"),
        (("source", "id"), "source.id: required"),
        (("source", "revision"), "source.revision: required"),
        (("definitions",), "envelope.definitions: required"),
        (("definitions", "contract"), "definitions.contract: required"),
        (("definitions", "binding"), "definitions.binding: required"),
        (("definitions", "protocol"), "definitions.protocol: required"),
        (("definitions", "protocol", "id"), "definitions.protocol.id: required"),
        (("definitions", "protocol", "revision"), "definitions.protocol.revision: required"),
        (("executor",), "envelope.executor: required"),
        (("executor", "adapter_id"), "executor.adapter_id: required"),
        (("executor", "interface_version"), "executor.interface_version: required"),
        (("upstream",), "envelope.upstream: required"),
        (("upstream", "service_id"), "upstream.service_id: required"),
        (("upstream", "origin_digest"), "upstream.origin_digest: required"),
        (("generated",), "envelope.generated: required"),
        (("generated", "n", "generator"), "generated.n.generator: required"),
        (("generated", "n", "value"), "generated.n.value: required"),
        (("generated", "h", "value"), "generated.h.value: required"),
        (("results",), "envelope.results: required"),
        (("results", "bundle"), "results.bundle: required"),
    ],
)
def test_missing_key_refused(path, match):
    def drop(data):
        node = data
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]

    refusal(drop, match)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("envelope_version",), 3, "envelope_version: expected one of 1, 2"),
        (("envelope_version",), "1", "envelope_version: expected an integer"),
        (("envelope_version",), True, "envelope_version: expected an integer"),
        (("source",), "x", "source: expected an object"),
        (("source", "kind"), "other", "source.kind: expected one of"),
        (("source", "id"), "", "source.id: expected a non-empty string"),
        (("source", "id"), 5, "source.id: expected a non-empty string"),
        (("source", "revision"), None, "source.revision: expected a non-empty string"),
        (("source", "revision"), True, "source.revision: expected a non-empty string"),
        (("source", "revision"), 1.5, "source.revision: expected a non-empty string"),
        (("definitions", "contract"), "sha256:" + "A" * 64, "definitions.contract: expected 'sha256:'"),
        (("definitions", "contract"), "sha256:" + "a" * 63, "definitions.contract: expected 'sha256:'"),
        (("definitions", "contract"), "md5:" + "a" * 64, "definitions.contract: expected 'sha256:'"),
        (("definitions", "contract"), None, "definitions.contract: expected 'sha256:'"),
        (("definitions", "binding"), "sha256:" + "a" * 64 + "\n", "definitions.binding: expected 'sha256:'"),
        (("definitions", "template"), None, "definitions.template: expected 'sha256:'"),
        (("definitions", "template"), "nope", "definitions.template: expected 'sha256:'"),
        (("definitions", "protocol"), [], "definitions.protocol: expected an object"),
        (("definitions", "protocol", "id"), "", "definitions.protocol.id: expected a non-empty string"),
        (("definitions", "protocol", "revision"), [], "definitions.protocol.revision: expected a non-empty string"),
        (("executor", "adapter_id"), 3, "executor.adapter_id: expected a non-empty string"),
        (("executor", "interface_version"), "1", "executor.interface_version: expected an integer"),
        (("executor", "interface_version"), 0, "executor.interface_version: expected an integer of at least 1"),
        (("upstream", "service_id"), "", "upstream.service_id: expected a non-empty string"),
        (("upstream", "origin_digest"), None, "upstream.origin_digest: expected 'sha256:'"),
        (("upstream", "origin_digest"), "origin-1", "upstream.origin_digest: expected 'sha256:'"),
        (("upstream", "origin_digest"), "sha256:ABC", "upstream.origin_digest: expected 'sha256:'"),
        (("generated",), [], "generated: expected an object"),
        (("generated", "n"), 5, "generated.n: expected an object"),
        (("results",), None, "results: expected an object"),
        (("results", "bundle"), "", "results.bundle: expected a non-empty string"),
        (("results", "bundle"), 5, "results.bundle: expected a non-empty string"),
    ],
)
def test_wrong_type_or_value_refused_with_path(path, value, match):
    def put(data):
        node = data
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value

    refusal(put, match)


def test_generator_not_in_the_reviewed_map_is_refused():
    refusal(lambda d: d["generated"]["n"].update(generator="unreviewed"), "generated.n.generator: 'unreviewed'")


def test_any_generated_entry_is_refused_without_a_map():
    with pytest.raises(ValueError, match="not a reviewed generator"):
        parse_envelope(valid())


def test_empty_generator_id_and_input_name_are_refused():
    refusal(lambda d: d["generated"]["n"].update(generator=""), "generated.n.generator: expected a non-empty")
    refusal(lambda d: d["generated"].update({"": {"generator": "gen-kept", "value": 1}}), "input names")


def test_unknown_persistence_class_is_refused():
    refusal(lambda _d: None, "unknown persistence class", generators={**GENERATORS, "gen-kept": "forever"})


@pytest.mark.parametrize("bad", ["abc", "sha256:xyz", 7, None, SHA_A.upper()])
def test_digest_class_requires_a_digest(bad):
    refusal(lambda d: d["generated"]["h"].update(value=bad), "generated.h.value: expected 'sha256:'")


@pytest.mark.parametrize("kept", [0, "text", 1.5, -3])
def test_value_class_accepts_a_scalar(kept):
    data = valid()
    data["generated"]["n"]["value"] = kept
    assert parse_envelope(data, GENERATORS).generated["n"]["value"] == kept


@pytest.mark.parametrize("bad", [None, True, [1], {"a": 1}, float("nan"), float("inf")])
def test_value_class_refuses_non_scalars(bad):
    refusal(lambda d: d["generated"]["n"].update(value=bad), "generated.n.value: expected a string")


@pytest.mark.parametrize("kept", [0, "", False, "x", SHA_A])
def test_none_class_refuses_any_value(kept):
    refusal(lambda d: d["generated"]["z"].update(value=kept), "generated.z.value: a generator with persistence 'none'")


def test_parse_does_not_mutate_its_input():
    data = valid()
    snapshot = copy.deepcopy(data)
    parse_envelope(data, GENERATORS)
    assert data == snapshot


def test_envelope_is_frozen():
    envelope = parse_envelope(valid(), GENERATORS)
    with pytest.raises(AttributeError):
        envelope.contract = SHA_B  # type: ignore[misc]
    assert isinstance(envelope, Envelope)


# --- D3 recovery table -------------------------------------------------------------------------------------------


# The D3 table, row by row, with ``terminal`` False. Columns: submitting, protocol_job, bundle_committed,
# spool_present -> outcome. D3 names four states; the first match wins, in the order of the table below it:
#   committed bundle                          -> replay
#   protocol_job saved                        -> poll
#   protocol_submitting, nothing else         -> submission_unknown
#   no protocol_submitting, spool present     -> resume
#   no protocol_submitting, spool gone        -> interrupted_before_submit
RECOVERY_ROWS = [
    # submitting, protocol_job, bundle_committed, spool_present, outcome
    (False, False, False, False, "interrupted_before_submit"),
    (False, False, False, True, "resume"),
    (False, False, True, False, "replay"),
    (False, False, True, True, "replay"),
    (False, True, False, False, "poll"),
    (False, True, False, True, "poll"),
    (False, True, True, False, "replay"),
    (False, True, True, True, "replay"),
    (True, False, False, False, "submission_unknown"),
    (True, False, False, True, "submission_unknown"),
    (True, False, True, False, "replay"),
    (True, False, True, True, "replay"),
    (True, True, False, False, "poll"),
    (True, True, False, True, "poll"),
    (True, True, True, False, "replay"),
    (True, True, True, True, "replay"),
]


@pytest.mark.parametrize(("submitting", "protocol_job", "bundle_committed", "spool_present", "outcome"), RECOVERY_ROWS)
def test_recovery_table_rows_for_a_job_that_is_not_terminal(
    submitting, protocol_job, bundle_committed, spool_present, outcome
):
    assert (
        classify_recovery(
            terminal=False,
            submitting=submitting,
            protocol_job=protocol_job,
            bundle_committed=bundle_committed,
            spool_present=spool_present,
        )
        == outcome
    )


def test_recovery_table_is_complete():
    assert {row[:4] for row in RECOVERY_ROWS} == set(itertools.product([False, True], repeat=4))
    assert len(RECOVERY_ROWS) == 16


@pytest.mark.parametrize("state", list(itertools.product([False, True], repeat=4)))
def test_a_terminal_job_needs_no_recovery_whatever_else_is_saved(state):
    submitting, protocol_job, bundle_committed, spool_present = state
    assert (
        classify_recovery(
            terminal=True,
            submitting=submitting,
            protocol_job=protocol_job,
            bundle_committed=bundle_committed,
            spool_present=spool_present,
        )
        == "terminal"
    )


def test_recovery_takes_keywords_only():
    with pytest.raises(TypeError):
        classify_recovery(False, False, False, False, False)  # type: ignore[misc]


# ---------------------------------------------------------------------------------------- version 2 (section 7.4)


def valid_v2(**source) -> dict:
    data = valid()
    data["envelope_version"] = 2
    data["source"] = {"kind": "workflow", "id": "wf.example", "revision": 3, "incarnation": "inc-1",
                      "binding_key": "fake", "record_revision": 7, **source}
    return data


def test_version_two_round_trips_and_carries_the_registry_facts():
    data = valid_v2()
    envelope = parse_envelope(data, GENERATORS)
    assert (envelope.incarnation, envelope.binding_key, envelope.record_revision) == ("inc-1", "fake", 7)
    assert envelope.to_json() == data
    assert parse_envelope(envelope.to_json(), GENERATORS) == envelope


def test_version_one_is_still_read_and_has_no_registry_facts():
    envelope = parse_envelope(valid(), GENERATORS)
    assert (envelope.envelope_version, envelope.incarnation, envelope.binding_key, envelope.record_revision) == (1, None, None, None)
    assert "incarnation" not in envelope.to_json()["source"]


def test_version_two_requires_incarnation_and_binding_key_but_record_revision_is_audit_only():
    for missing in ("incarnation", "binding_key"):
        data = valid_v2()
        del data["source"][missing]
        with pytest.raises(ValueError, match=f"source.{missing}: required"):
            parse_envelope(data, GENERATORS)
    data = valid_v2(record_revision=None)
    assert parse_envelope(data, GENERATORS).record_revision is None
    data = valid_v2()
    del data["source"]["record_revision"]
    assert parse_envelope(data, GENERATORS).record_revision is None


def test_version_one_refuses_the_version_two_keys():
    data = valid()
    data["source"]["incarnation"] = "inc-1"
    with pytest.raises(ValueError, match="unknown key"):
        parse_envelope(data, GENERATORS)
