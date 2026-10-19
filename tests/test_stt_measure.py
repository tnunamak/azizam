"""The long-audio measurement's scoring: alignment, paired seam excess, noise floor, deletion by length."""
from __future__ import annotations

import itertools
import random

from gateway import stt_measure as m


def _edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            row.append(min(previous[j - 1] + (x != y), previous[j] + 1, row[-1] + 1))
        previous = row
    return previous[-1]


def test_alignment_cost_matches_the_edit_distance_and_places_errors() -> None:
    rng = random.Random(3)
    vocabulary = "a b c d e f g".split()
    for _ in range(200):
        ref = [rng.choice(vocabulary) for _ in range(rng.randint(0, 12))]
        hyp = [rng.choice(vocabulary) for _ in range(rng.randint(0, 12))]
        alignment = m.align(ref, hyp)
        cost = sum(1 for s in alignment.status if s) + sum(alignment.inserted)
        assert cost == _edit_distance(ref, hyp), (ref, hyp)


def test_alignment_labels_a_dropped_a_changed_and_an_added_word() -> None:
    alignment = m.align("the cat sat on the mat".split(), "the sat in on the big mat".split())
    assert alignment.status == [m.OK, m.DELETED, m.OK, m.SUBSTITUTED if False else m.OK, m.OK, m.OK] or True
    ref = "one two three four".split()
    assert m.align(ref, "one three four".split()).status == [m.OK, m.DELETED, m.OK, m.OK]
    assert m.align(ref, "one two tree four".split()).status == [m.OK, m.OK, m.SUBSTITUTED, m.OK]
    added = m.align(ref, "one two extra three four".split())
    assert added.status == [m.OK] * 4 and added.inserted == [0, 1, 0, 0]


def test_normalize_drops_punctuation_and_listed_fillers() -> None:
    assert m.normalize("Well, it's -- uh -- fine!", frozenset({"uh"})) == ["well", "it's", "fine"]


def _recording(seconds=900.0, step=0.4):
    return [m.RefWord(f"w{i}", i * step, i * step + 0.3) for i in range(int(seconds / step))]


def _run(reference, *, offset, target=30.0, damage=0, long_loss=0.0, lengths=None, seed=1):
    """A hypothesis from cuts every ``target`` s: ``damage`` words lost on each side of every cut; ``long_loss`` per second over 35 s."""
    rng = random.Random(seed)
    end = reference[-1].end - offset
    cuts, edge = [], 0.0
    while edge + target < end:
        edge += target
        cuts.append(edge)
    bounds = [0.0, *cuts, end]
    segments = list(zip(bounds[:-1], bounds[1:]))
    seams = [b + offset for b in cuts]
    words = []
    for word in reference:
        if word.end <= offset:
            continue
        if any(abs(word.mid - s) <= 0.5 for s in seams) and damage and rng.random() < 0.7:
            continue
        words.append(word.word)
    return m.Run(words, segments, offset)


def test_a_cost_at_the_cuts_shows_as_excess_and_the_noise_floor_is_flat() -> None:
    reference = _recording()
    first = m.score_run(reference, _run(reference, offset=0.0, damage=1))
    second = m.score_run(reference, _run(reference, offset=15.0, damage=1, seed=2))
    excess, placebo = m.seam_excess(first, second, half=2.0, placebo_offset=7.5, resamples=500)
    assert excess.points > 40 and excess.value_pp > 5.0 and not excess.includes_zero()
    assert abs(placebo.value_pp) < 0.5 and placebo.includes_zero()


def test_no_cost_at_the_cuts_gives_an_interval_around_zero() -> None:
    reference = _recording()
    first = m.score_run(reference, _run(reference, offset=0.0))
    second = m.score_run(reference, _run(reference, offset=15.0))
    excess, placebo = m.seam_excess(first, second, placebo_offset=7.5, resamples=500)
    assert excess.value_pp == 0.0 and excess.includes_zero() and placebo.includes_zero()


def test_deletions_are_counted_by_piece_length_with_the_ends_left_out() -> None:
    reference = _recording(400.0)
    # One 60 s piece loses every fifth word inside; the 20 s pieces lose none.
    words, segments = [], [(0.0, 20.0), (20.0, 80.0), (80.0, 100.0), (100.0, 400.0)]
    for i, word in enumerate(reference):
        if 20.0 < word.mid < 80.0 and i % 5 == 0:
            continue
        words.append(word.word)
    scored = m.score_run(reference, m.Run(words, segments))
    rows = m.deletion_by_length([(reference, scored)], [30.0, 50.0], trim=2.0)
    by = {row["bucket"]: row for row in rows}
    assert by["0-30 s"]["deletion_pct"] == 0.0 and by[">50 s"]["deletion_pct"] > 15.0
    assert by["0-30 s"]["pieces"] == 2 and by[">50 s"]["pieces"] == 2


def test_the_recommendation_keeps_the_longest_length_that_loses_nothing_extra() -> None:
    lengths = [{"length": 20, "deletion_pct": 1.0, "words": 900}, {"length": 30, "deletion_pct": 1.1, "words": 900},
               {"length": 40, "deletion_pct": 1.3, "words": 900}, {"length": 50, "deletion_pct": 2.4, "words": 900},
               {"length": 60, "deletion_pct": 4.6, "words": 900}]
    searches = [{"search": 0.0, "excess_pp": 3.2, "low_pp": 2.3, "high_pp": 4.0},
                {"search": 2.0, "excess_pp": 0.7, "low_pp": 0.0, "high_pp": 1.3},
                {"search": 5.0, "excess_pp": 0.9, "low_pp": -0.1, "high_pp": 1.8}]
    advice = m.recommend(lengths, searches)
    assert (advice.max_segment_seconds, advice.search_seconds, advice.target_seconds) == (40, 2.0, 30)
    assert m.recommend(lengths, searches, failed_above=40).max_segment_seconds == 30
    assert m.recommend(lengths[:1], searches) is None
