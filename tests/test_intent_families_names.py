"""ASCII intent names and exact-prefix boundaries."""

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
