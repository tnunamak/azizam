"""The long-audio measurement's scoring: alignment, paired seam excess, noise floor, deletion by length."""
from __future__ import annotations

import itertools
import random
from pathlib import Path
import yaml

POLICY = yaml.safe_load((Path(__file__).resolve().parents[1] / "registry/stt_measurement.yml").read_text())["recommendation"]

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
        ref = [rng.choice(vocabulary) for _ in range(rng.randint(1, 12))]
        hyp = [rng.choice(vocabulary) for _ in range(rng.randint(0, 12))]
        alignment = m.align(ref, hyp)
        cost = sum(1 for s in alignment.status if s) + sum(alignment.inserted)
        assert cost == _edit_distance(ref, hyp), (ref, hyp)


def test_alignment_labels_a_dropped_a_changed_and_an_added_word() -> None:
    ref = "one two three four".split()
    assert m.align(ref, "one three four".split()).status == [m.OK, m.DELETED, m.OK, m.OK]
    assert m.align(ref, "one two tree four".split()).status == [m.OK, m.OK, m.SUBSTITUTED, m.OK]
    added = m.align(ref, "one two extra three four".split())
    assert added.status == [m.OK] * 4 and added.inserted == [0, 1, 0, 0]


def test_normalize_drops_punctuation_and_listed_fillers() -> None:
    assert m.normalize("Well, it's -- uh -- fine!", frozenset({"uh"})) == ["well", "its", "fine"]
    assert m.normalize("That’s what you’re") == m.normalize("thats what youre")


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
    reference = _recording(100.0)
    # One 60 s piece loses every fifth word inside; the 20 s pieces lose none.
    words, segments = [], [(0.0, 20.0), (20.0, 80.0), (80.0, 100.0)]
    for i, word in enumerate(reference):
        if 20.0 < word.mid < 80.0 and i % 5 == 0:
            continue
        words.append(word.word)
    scored = m.score_run(reference, m.Run(words, segments))
    rows = m.deletion_by_length([(reference, scored)], [30.0, 50.0], trim=2.0)
    by = {row["bucket"]: row for row in rows}
    assert by["0-30 s"]["deletion_pct"] == 0.0 and by[">50 s"]["deletion_pct"] > 15.0
    assert by["0-30 s"]["pieces"] == 2 and by[">50 s"]["pieces"] == 1


def _estimate(value=0, low=-1, high=1, points=30, words=1200):
    return {"excess_pp": value, "low_pp": low, "high_pp": high, "points": points, "words": words}


def _length(length, **estimate):
    return {"length": length, "baseline_length": 20, "paired_deletion": _estimate(**estimate)}


def _search(width=2, **estimate):
    return {"search": width, **_estimate(), "noise": _estimate(), "difference": _estimate(points=60, words=2400, **estimate)}


def test_confidently_worse_length_bounds_the_planners_reachable_piece_sizes():
    from gateway import stt_chunking
    rows = [_length(20), _length(30, low=-2, high=5), _length(40), _length(50, value=6, low=4, high=8), _length(60)]
    assert m.select_length(rows, policy=POLICY)[0] == 40
    assert m.length_evidence(rows[1], POLICY)["state"] == "inconclusive"
    assert m.length_evidence(rows[3], POLICY)["state"] == "confidently_worse"
    assert m.select_length(rows, policy=POLICY, failed_above=40)[0] == 20
    unsafe = stt_chunking.plan_quietest_frame_segments(duration_seconds=185, quietest_frame=lambda lo, hi, near: near,
        target_seconds=45, search_seconds=5, max_segment_seconds=60, max_segments=20)
    assert any(end - start >= 50 for start, end in unsafe)
    safe = stt_chunking.plan_quietest_frame_segments(duration_seconds=185, quietest_frame=lambda lo, hi, near: near,
        target_seconds=m.target_for_maximum(40, POLICY), search_seconds=5, max_segment_seconds=40, max_segments=20)
    assert max(end - start for start, end in safe) <= 40


def test_sparse_paired_evidence_abstains():
    rows = [_length(20, points=1, words=1), _length(40, points=1, words=1)]
    assert m.recommend(rows, [_search()], policy=POLICY) is None
    rows = [_length(20), _length(40)]
    for key in (None, "noise"):
        search = _search()
        (search if key is None else search[key]).update(points=1, words=1)
        assert m.recommend(rows, [search], policy=POLICY) is None
    assert m.recommend(rows, [_search(high=5)], policy=POLICY) is None
    assert m.seam_evidence(_search(value=6, low=4, high=8), POLICY)["state"] == "confidently_worse"


def test_current_supported_width_is_retained_unless_evidence_distinguishes_an_alternative():
    rows = [_length(20), _length(40)]
    searches = [_search(0), _search(2), _search(5)]
    assert m.recommend(rows, searches, policy=POLICY, current_search=2).search_seconds == 2
    searches[0]["difference"] = _estimate(value=-2, low=-2.5, high=-1.5, points=60, words=2400)
    assert m.recommend(rows, searches, policy=POLICY, current_search=2).search_seconds == 0
    assert m.recommend(rows, searches, policy=POLICY, current_search=7).search_seconds == 0


def test_width_selection_chooses_the_separated_winner_not_an_overlapping_narrower_width():
    rows = [_length(20), _length(40)]
    searches = [_search(0), _search(2), _search(5)]
    searches[0]["difference"] = _estimate(value=-.5, low=-1.5, high=.5, points=60, words=2400)
    searches[2]["difference"] = _estimate(value=-3, low=-3.5, high=-2.5, points=60, words=2400)
    assert m.recommend(rows, searches, policy=POLICY, current_search=2).search_seconds == 5


def test_broad_raw_intervals_do_not_reject_supported_paired_evidence():
    rows = [{**_length(length), "deletion_pct": 10, "low_pct": 6, "high_pct": 14} for length in (20, 40)]
    search = _search()
    search["noise"].update(low_pp=-2, high_pp=2)
    search.update(low_pp=-2, high_pp=2)
    assert m.recommend(rows, [search], policy=POLICY) is not None


def test_default_operator_grid_supports_noisy_shared_deletions_within_caps():
    from gateway import stt_samples, stt_chunking
    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "registry/stt_measurement.yml").read_text())
    specs = {s.id: s for s in stt_samples.load_manifest()}
    selected = [specs[sid] for sid in data["sample_ids"]]
    assert {s.corpus for s in selected} == {"ami", "icsi", "earnings21"}
    assert sum(s.download_bytes() for s in selected) <= data["max_download_bytes"]
    seconds = data["audio_seconds_per_sample"]
    assert all(s.duration_seconds >= seconds for s in selected)
    requests = len(selected) * (sum(int(seconds // length) for length in data["lengths"]) + 2 * len(data["search_widths"]))
    assert requests == 153 and requests <= data["max_gateway_requests"]
    reference = _recording(seconds, step=.4)
    # 10% shared deletions, clustered in alternate 30s blocks, plus 1.5%
    # independent loss per run (about 3% disagreement between runs).
    def noisy_run(segments, offset, seed):
        rng = random.Random(seed)
        words = [w.word for i, w in enumerate(reference) if w.mid >= offset
                 and not (int(w.mid // 30) % 2 == 0 and i % 5 == 0) and rng.random() >= .015]
        return m.Run(words, segments, offset)
    rows, blocks_by_length, pieces_by_length = [], {n: [] for n in data["lengths"]}, {n: [] for n in data["lengths"]}
    for sample in range(len(selected)):
        scored = {}
        for length in data["lengths"]:
            end = int(seconds // length) * length
            segments = [(i * length, (i+1) * length) for i in range(int(seconds // length))]
            run = noisy_run(segments, 0, sample * 100 + length)
            run = m.Run([word for word in run.words if int(word[1:]) * .4 < end], segments)
            scored[length] = m.score_run(reference, run)
            pieces_by_length[length] += m.piece_counts(scored[length])
        baseline = m.interior_deletions(scored[min(scored)])
        for length, run in scored.items():
            blocks_by_length[length] += m.deletion_blocks(baseline, m.interior_deletions(run), [w.mid for w in reference], block=30)
    for length in data["lengths"]:
        rows.append({"length": length, "baseline_length": min(data["lengths"]),
                     **m.rate_interval(pieces_by_length[length], resamples=200),
                     "paired_deletion": m.paired_excess(blocks_by_length[length], resamples=200).as_dict()})
    assert any(r["high_pct"] - r["low_pct"] > 1 for r in rows)
    maximum, _ = m.select_length(rows, policy=POLICY)
    assert maximum == max(data["lengths"])
    target = m.target_for_maximum(maximum, POLICY)
    searches = []
    for width in data["search_widths"]:
        pairs, noise = [], []
        for sample in range(len(selected)):
            scored = []
            for offset in (0, target / 2):
                segments = stt_chunking.plan_quietest_frame_segments(duration_seconds=seconds-offset,
                    quietest_frame=lambda lo, hi, near: near, target_seconds=target, search_seconds=width,
                    max_segment_seconds=maximum, max_segments=512)
                scored.append(m.score_run(reference, noisy_run(segments, offset, sample*100 + int(offset) + int(width))))
            p, n = m.seam_pairs(*scored, placebo_offset=target/4)
            pairs += p
            noise += n
        searches.append({"search": width, **m.paired_excess(pairs, resamples=200).as_dict(),
                         "noise": m.paired_excess(noise, resamples=200).as_dict(),
                         "difference": m.difference(pairs, noise, resamples=200).as_dict()})
    advice = m.recommend(rows, searches, policy=POLICY, current_search=2)
    assert advice is not None
    chosen = next(row for row in searches if row["search"] == advice.search_seconds)
    assert m.seam_evidence(chosen, POLICY)["state"] == "recommended"
    assert "Gross-harm" in " ".join(advice.reasons)
