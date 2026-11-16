"""ASCII intent names and exact-prefix boundaries."""

from collections.abc import Mapping

import pytest

from gateway.intent_families import resolve_intent


@pytest.mark.parametrize(
    "intent, expected",
    [
        pytest.param("image.génerate", None, id="non_ascii"),
        pytest.param("image.gen-erate!", None, id="punctuation"),
        pytest.param("image.generate.x", None, id="exact_prefix"),
        pytest.param("image.upscale", "family", id="family"),
        pytest.param("image.generate", "exact", id="exact"),
        pytest.param("image_x.a1", "other_family", id="ascii_identifier"),
        pytest.param("image.generate.x.y", None, id="distant_exact_prefix"),
        pytest.param("image.upscale.x", "family", id="unclaimed_deep_name"),
        pytest.param("image..upscale", None, id="empty_segment"),
        pytest.param("imáge.upscale", None, id="non_ascii_family"),
        pytest.param("image.upscale.!", None, id="punctuation_deep"),
    ],
)
def test_resolve_names(intent, expected):
    assert resolve_intent(
        intent,
        exact={"image.generate": "exact"},
        families={"image": "family", "image_x": "other_family"},
    ) == expected


def test_full_exact_match_wins_over_shorter_exact_prefix():
    assert resolve_intent(
        "image.generate.x",
        exact={"image.generate": "parent", "image.generate.x": "child"},
        families={"image": "family"},
    ) == "child"


def test_three_segment_exact_prefix_blocks_family():
    assert resolve_intent(
        "image.upscale.fast.x",
        exact={"image.upscale.fast": "exact"},
        families={"image": "family"},
    ) is None


def test_single_segment_exact_row_does_not_block_family():
    assert resolve_intent(
        "image.upscale",
        exact={"image": "exact"},
        families={"image": "family"},
    ) == "family"


def test_malformed_exact_name_is_still_refused():
    assert resolve_intent(
        "image.gen-erate!",
        exact={"image.gen-erate!": "exact"},
        families={"image": "family"},
    ) is None


class CountingExact(Mapping):
    def __init__(self, rows):
        self.rows = rows
        self.contains_steps = 0
        self.iter_steps = 0

    def __contains__(self, key):
        self.contains_steps += 1
        return key in self.rows

    def __iter__(self):
        for key in self.rows:
            self.iter_steps += 1
            yield key

    def __getitem__(self, key):
        return self.rows[key]

    def __len__(self):
        return len(self.rows)


@pytest.mark.parametrize(
    "rows",
    [{}, {"image.other": "exact", "other.x": "other", "image": "single"}],
    ids=["empty", "small_registry"],
)
def test_deep_intent_lookup_work_is_bounded_by_registry(rows):
    exact = CountingExact(rows)
    intent = "image." + ".".join(["x"] * 2000)
    assert resolve_intent(intent, exact=exact, families={"image": "family"}) == "family"
    assert exact.contains_steps + exact.iter_steps <= 2 * len(exact) + 1


def test_deep_intent_below_registered_exact_is_refused():
    exact = CountingExact({"image.x": "exact"})
    intent = "image." + ".".join(["x"] * 2000)
    assert resolve_intent(intent, exact=exact, families={"image": "family"}) is None
    assert exact.contains_steps + exact.iter_steps <= 2 * len(exact) + 1


def test_single_segment_exact_does_not_block_deep_intent():
    exact = CountingExact({"image": "exact"})
    intent = "image." + ".".join(["x"] * 2000)
    assert resolve_intent(intent, exact=exact, families={"image": "family"}) == "family"
    assert exact.contains_steps + exact.iter_steps <= 2 * len(exact) + 1


@pytest.mark.parametrize("row", [None, False, 0, "", {}, []])
def test_falsey_exact_row_still_blocks_children(row):
    assert resolve_intent(
        "image.x.child", exact={"image.x": row}, families={"image": "family"},
    ) is None
    assert resolve_intent(
        "image.x", exact={"image.x": row}, families={"image": "family"},
    ) is row
