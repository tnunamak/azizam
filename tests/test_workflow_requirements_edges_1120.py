"""Pure availability and engine-check requirement contracts."""

import pytest

from gateway.workflow_requirements import judge_by_names, validate_workflow_requirements


NODE_REASON = "custom node class is not present in backend inventory"
FILE_REASON = "model artifact is not present in backend inventory"
HASH_REASON = "matching model artifact with expected hash is not present in backend inventory"
FILE = {"role": "checkpoint", "path": "Model.safetensors"}


def issue(kind, name, reason):
    return {"kind": kind, "name": name, "reason": reason}


def test_empty_requirements_ignore_unneeded_inventory():
    result = judge_by_names({}, ["OtherNode"], ["Other.safetensors"])
    assert result.as_dict() == {"ok": True, "missing": []}


def test_repeated_requirement_retains_one_issue_per_missing_entry():
    requires = {"custom_nodes": ["FixtureNode", "FixtureNode"]}
    result = judge_by_names(requires, [], [])
    missing = issue("custom_node", "FixtureNode", NODE_REASON)
    assert result.as_dict() == {"ok": False, "missing": [missing, missing]}
    assert judge_by_names(requires, ["FixtureNode"], []).as_dict() == {"ok": True, "missing": []}


def test_repeated_model_requirement_retains_one_issue_per_missing_entry():
    requires = {"models": [FILE, FILE]}
    result = judge_by_names(requires, [], [])
    missing = issue("model", "checkpoint:Model.safetensors", FILE_REASON)
    assert result.as_dict() == {"ok": False, "missing": [missing, missing]}
    assert judge_by_names(requires, [], ["Model.safetensors"]).as_dict() == {"ok": True, "missing": []}


def test_custom_node_name_is_case_sensitive():
    requires = {"custom_nodes": ["FixtureNode"]}
    result = judge_by_names(requires, ["fixturenode"], [])
    assert result.as_dict() == {
        "ok": False,
        "missing": [issue("custom_node", "FixtureNode", NODE_REASON)],
    }
    assert judge_by_names(requires, ["FixtureNode"], []).as_dict() == {"ok": True, "missing": []}


def test_model_path_matches_listed_bare_file_name():
    requires = {"models": [{"role": "checkpoint", "path": "sub/Model.safetensors"}]}
    assert judge_by_names(requires, [], ["Model.safetensors"]).as_dict() == {"ok": True, "missing": []}
    result = judge_by_names(requires, [], ["Other.safetensors"])
    assert result.as_dict() == {
        "ok": False,
        "missing": [issue("model", "checkpoint:Model.safetensors", FILE_REASON)],
    }


def test_model_path_name_matches_exact_case():
    requires = {"models": [FILE]}
    result = judge_by_names(requires, [], ["model.safetensors"])
    assert result.as_dict() == {
        "ok": False,
        "missing": [issue("model", "checkpoint:Model.safetensors", FILE_REASON)],
    }
    assert judge_by_names(requires, [], ["Model.safetensors"]).as_dict() == {"ok": True, "missing": []}


def test_hash_required_model_rejects_wrong_hash_and_names_alone():
    requires = {"models": [{**FILE, "sha256": "a" * 64}]}
    wrong = validate_workflow_requirements(
        {"requires": requires}, {"models": [{**FILE, "sha256": "b" * 64}]}
    )
    by_names = judge_by_names(requires, [], ["Model.safetensors"])
    for result in (wrong, by_names):
        assert result.missing[0].needs_hash is True
        assert result.as_dict() == {
            "ok": False,
            "missing": [issue("model", "checkpoint:Model.safetensors", HASH_REASON)],
        }
    match = validate_workflow_requirements(
        {"requires": requires}, {"models": [{**FILE, "sha256": "a" * 64}]}
    )
    assert match.as_dict() == {"ok": True, "missing": []}


def test_missing_node_and_model_are_both_reported_in_stable_order():
    result = judge_by_names({"custom_nodes": ["FixtureNode"], "models": [FILE]}, [], [])
    assert result.as_dict() == {
        "ok": False,
        "missing": [
            issue("custom_node", "FixtureNode", NODE_REASON),
            issue("model", "checkpoint:Model.safetensors", FILE_REASON),
        ],
    }


def test_listed_name_with_backslash_separators_counts_as_present():
    # An engine on Windows lists a checkpoint as folder, backslash, file name; only the file name is compared.
    requires = {"models": [FILE]}
    for listed in ("checkpoints\\Model.safetensors", "a\\b/Model.safetensors"):
        assert judge_by_names(requires, [], [listed]).as_dict() == {"ok": True, "missing": []}
    wrong = judge_by_names(requires, [], ["checkpoints\\model.safetensors"])
    assert wrong.as_dict() == {"ok": False, "missing": [issue("model", "checkpoint:Model.safetensors", FILE_REASON)]}


@pytest.mark.parametrize("declared,listed,ok", [
    ("sub/Model.safetensors", ["Model.safetensors"], True),
    ("Model.safetensors", ["sub/Model.safetensors"], True),
    ("a/Model.safetensors", ["b/Model.safetensors"], True),
    ("Model.safetensors", ["  Model.safetensors  "], True),
    ("Model.safetensors", ["Model.safetensors", "Model.safetensors"], True),
    ("Model.safetensors", ["Model.ckpt"], False),
    ("Model.safetensors", ["Model"], False),
], ids=["declared-folder-listed-bare", "declared-bare-listed-folder", "different-folders-same-file",
        "listed-name-padded", "same-name-listed-twice", "same-stem-other-extension", "listed-name-is-prefix"])
def test_model_name_judged_by_file_name_only(declared, listed, ok):
    # Only the file name is compared, never the folder; a listed name is stripped and de-duplicated.
    result = judge_by_names({"models": [{"role": "checkpoint", "path": declared}]}, [], listed).as_dict()
    if ok:
        assert result == {"ok": True, "missing": []}
    else:
        name = "checkpoint:" + declared.rsplit("/", 1)[-1]
        assert result == {"ok": False, "missing": [issue("model", name, FILE_REASON)]}
