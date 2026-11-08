"""C0 goldens of the config-homes program: what the three inputs mean today (docs/design/CONFIG_HOMES.md, 10.1).

Every later slice must leave these unchanged unless it changes a setting on purpose and says so. A failure names the
input, the section whose digest moved and the first alias or model rows that differ. To accept a change on purpose,
regenerate with ``python tests/config_homes_golden.py --write`` (see that module) and review the diff.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.config_homes_golden import INPUT_FILES, capture, effective_config, load_golden, raw_config  # noqa: E402


RELEASE_WORKFLOW_EXECUTION = {"max_wait_s": 60, "history_depth": 20,
                              "bounds": {"max_template_bytes": 524288, "max_nodes": 2000, "max_depth": 32}}


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    return capture(proxy, tmp_path_factory.mktemp("config-homes-goldens"))


def _first_differences(expected: list, actual: list, limit: int = 3) -> list:
    differing = [{"expected": want, "actual": got} for want, got in zip(expected, actual) if want != got]
    return differing[:limit] or [{"expected_len": len(expected), "actual_len": len(actual)}]


# C10 acceptance 8: the effective config of an install whose sections were imported is what it was before C10, except
# these top-level entries, each changed on purpose (docs/design/CONFIG_HOMES.md, "C10 as built"). The goldens are the
# pre-C10 capture, not regenerated.
ENTRIES_CHANGED_ON_PURPOSE = {
    # models is its own section now (a mapping, was a list); a deployment's list was empty. workflow_execution is a
    # section: the deployment's block is the same, a first run gains the empty release section. routing was read by no
    # code and is gone. admin.brand.product is the instance section's name, not a startup key. workflow_execution
    # (tracked and live_like) lost compile_from_pin and shows the release defaults of the S4 to S8 train (I7).
    # videos: each workflow entry gained its node_roles (S7 parity M9, builder V2), the roles that were the graph file's
    # _meta block before.
    "tracked": {"models", "routing", "admin", "workflow_execution", "videos"},
    "live_like": {"models", "routing", "admin", "workflow_execution", "videos"},
    "first_run": {"models", "workflow_execution", "videos"},
}


@pytest.mark.parametrize("name", sorted(INPUT_FILES))
def test_effective_config_digest_is_unchanged_except_what_c10_changes_on_purpose(captured, name):
    want = load_golden()["inputs"][name]
    got = captured["inputs"][name]
    moved = {
        key for key in {*want["section_digests"], *got["section_digests"]}
        if want["section_digests"].get(key) != got["section_digests"].get(key)
    }
    assert moved == ENTRIES_CHANGED_ON_PURPOSE[name], f"{name}: entries that moved: {sorted(moved)}"


@pytest.mark.parametrize("name", ["tracked", "live_like"])
def test_the_entries_c10_changes_are_these_and_no_others(name, tmp_path):
    raw = raw_config(name)
    effective = effective_config(proxy, name, tmp_path / "admin.db")
    assert effective["models"] == {} and raw["models"] == []
    assert "routing" not in effective and "routing" in raw
    assert effective["admin"] == {key: value for key, value in raw["admin"].items() if key != "brand"}
    assert raw["admin"]["brand"] == {"product": "Azizam"}
    assert "workflow_execution" not in raw
    assert effective["workflow_execution"] == RELEASE_WORKFLOW_EXECUTION


def test_a_first_run_gains_the_two_empty_sections(tmp_path):
    effective = effective_config(proxy, "first_run", tmp_path / "admin.db")
    assert effective["models"] == {} and effective["workflow_execution"] == RELEASE_WORKFLOW_EXECUTION


@pytest.mark.parametrize("name", sorted(INPUT_FILES))
def test_alias_resolution_table_is_unchanged(captured, name):
    want = load_golden()["inputs"][name]["aliases"]
    got = captured["inputs"][name]["aliases"]
    assert got == want, _first_differences(want, got)


@pytest.mark.parametrize("name", sorted(INPUT_FILES))
def test_models_and_roles_are_unchanged(captured, name):
    want = load_golden()["inputs"][name]["models"]
    got = captured["inputs"][name]["models"]
    assert got["roles"] == want["roles"]
    assert got["ids"] == want["ids"]
    assert got["alias_targets"] == want["alias_targets"]


def test_the_live_like_input_differs_from_the_tracked_file_in_the_chat_aliases_only():
    tracked, live_like = raw_config("tracked"), raw_config("live_like")
    assert {key for key in tracked if tracked[key] != live_like[key]} == {"model_aliases"}
    assert {key for key in tracked["model_aliases"]
            if tracked["model_aliases"][key] != live_like["model_aliases"][key]} == {"chat"}
    stable_t, stable_l = tracked["model_aliases"]["chat"]["stable"], live_like["model_aliases"]["chat"]["stable"]
    assert {key for key in {*stable_t, *stable_l} if stable_t.get(key) != stable_l.get(key)} == {
        "default", "fast", "default-fast",
    }
    assert live_like["model_aliases"]["chat"]["openai"] == tracked["model_aliases"]["chat"]["openai"]


def test_the_effective_config_does_not_depend_on_the_scratch_database_path(tmp_path):
    (tmp_path / "one").mkdir()
    one = effective_config(proxy, "tracked", tmp_path / "one" / "admin.db")
    two = effective_config(proxy, "tracked", tmp_path / "two.db")
    assert one == two


def test_first_run_has_no_aliases_or_roles_and_the_goldens_say_so():
    first_run = load_golden()["inputs"]["first_run"]
    # C3: the release ships the alias names with no target, so each one answers "not configured"; no engine, no pattern.
    assert first_run["aliases"] and {(row["kind"], row.get("error")) for row in first_run["aliases"]} == {
        ("stable", "alias_not_configured")}
    assert first_run["models"]["roles"] == []
