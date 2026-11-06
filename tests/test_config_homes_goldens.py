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


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    return capture(proxy, tmp_path_factory.mktemp("config-homes-goldens"))


def _first_differences(expected: list, actual: list, limit: int = 3) -> list:
    differing = [{"expected": want, "actual": got} for want, got in zip(expected, actual) if want != got]
    return differing[:limit] or [{"expected_len": len(expected), "actual_len": len(actual)}]


@pytest.mark.parametrize("name", sorted(INPUT_FILES))
def test_effective_config_digest_is_unchanged(captured, name):
    want = load_golden()["inputs"][name]
    got = captured["inputs"][name]
    moved = sorted(
        key for key in {*want["section_digests"], *got["section_digests"]}
        if want["section_digests"].get(key) != got["section_digests"].get(key)
    )
    assert got["effective_digest"] == want["effective_digest"], f"{name}: sections that moved: {moved}"


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
    assert first_run["aliases"] == []
    assert first_run["models"]["roles"] == []
