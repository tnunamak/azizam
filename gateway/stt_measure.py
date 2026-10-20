"""Scoring for the long-audio measurement: word errors by place, deletions by chunk length, and the noise floor.

Pure functions on words and times, no I/O and no engine knowledge. A run is one transcription of one recording
through the gateway: the transcript text and the segments (start, end seconds in the run's own audio) the gateway cut.
The reference is a human transcript with a time for each word.

Method, in short. Two runs of the same recording cut at different places (the second starts ``phase`` seconds in, so
its cuts fall in the middle of the first run's pieces). A word near a cut in one run is the same word as in the other
run, away from any cut, so the difference in their errors is the cost of the cut itself. Reference mistakes and hard
passages cancel in the difference. The same estimate taken at places that are away from every cut in both runs
(``placebo``) is the noise floor: what the estimate shows when there is nothing to find.
"""

from __future__ import annotations

import math
import random
import re
import unicodedata
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

_TAG = re.compile(r"[<\[{].*?[>\]}]")
# Bump when tokenization, reference membership or recommendation scoring changes.
SCORER_VERSION = 2
# Which statuses count as an error for a reference word.
OK, SUBSTITUTED, DELETED = 0, 1, 2


def normalize(text: str, drop: frozenset[str] = frozenset()) -> list[str]:
    """Canonical reference and hypothesis tokens: Unicode words, internal underscores and joined apostrophes.

    Punctuation splits words; annotation tags, standalone underscores and ``drop`` words are removed.
    Timed reference readers attach the source interval to each resulting token.
    """
    text = _TAG.sub(" ", unicodedata.normalize("NFC", text.casefold()))
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "`": "'"}))
    # A comma groups digits only in complete groups of three, not lists like 1,2.
    text = re.sub(r"(?<![\w,])\d{1,3}(?:,\d{3})+(?![\w,])", lambda m: m[0].replace(",", ""), text)
    words, token = [], []
    for i, char in enumerate(text):
        if char.isalnum() or char == "_" or (token and unicodedata.category(char).startswith("M")):
            token.append(char)
        elif char == "'" and token and i + 1 < len(text) and (text[i + 1].isalnum() or text[i + 1] == "_"):
            continue  # Internal apostrophes join the two word parts.
        elif token:
            words.append("".join(token))
            token = []
    if token:
        words.append("".join(token))
    return [word for word in words if word != "_" and word not in drop]


@dataclass(frozen=True)
class Alignment:
    """How each reference word fared: ``status[i]`` and ``inserted[i]`` (hypothesis words placed after word i)."""

    status: list[int]
    inserted: list[int]

    def errors(self, index: int) -> int:
        return (self.status[index] != OK) + self.inserted[index]


def align(reference: Sequence[str], hypothesis: Sequence[str], *, band: int | None = None) -> Alignment:
    """Edit-distance alignment inside a diagonal band (words drift little), with a trace-back.

    Inserted words are attached to the reference word before them (index 0 holds those before the first word).
    """
    n, m = len(reference), len(hypothesis)
    status = [OK] * n
    inserted = [0] * n
    if n == 0:
        return Alignment(status, inserted)
    if m == 0:
        return Alignment([DELETED] * n, inserted)
    half = band if band is not None else max(120, int(0.15 * max(n, m)))
    slope = m / n
    infinity = 1 << 30
    rows: list[tuple[int, list[int], bytearray]] = []  # (first column, costs, moves) per reference position 0..n
    previous: tuple[int, list[int]] | None = None
    for i in range(n + 1):
        centre = int(i * slope)
        lo, hi = max(0, centre - half), min(m, centre + half)
        costs = [infinity] * (hi - lo + 1)
        moves = bytearray(hi - lo + 1)  # 1 diagonal, 2 up (reference word deleted), 3 left (hypothesis word inserted)
        for j in range(lo, hi + 1):
            k = j - lo
            if i == 0 and j == 0:
                costs[k] = 0
                continue
            best, move = infinity, 0
            if previous is not None:
                plo, pcosts = previous
                if i > 0 and j > 0 and plo <= j - 1 < plo + len(pcosts):
                    diagonal = pcosts[j - 1 - plo] + (reference[i - 1] != hypothesis[j - 1])
                    if diagonal < best:
                        best, move = diagonal, 1
                if plo <= j < plo + len(pcosts):
                    up = pcosts[j - plo] + 1
                    if up < best:
                        best, move = up, 2
            if k > 0:
                left = costs[k - 1] + 1
                if left < best:
                    best, move = left, 3
            costs[k], moves[k] = best, move
        rows.append((lo, costs, moves))
        previous = (lo, costs)
    i, j = n, m
    while i > 0 or j > 0:
        lo, _costs, moves = rows[i]
        move = moves[j - lo] if lo <= j < lo + len(moves) else 0
        if move == 1:
            if reference[i - 1] != hypothesis[j - 1]:
                status[i - 1] = SUBSTITUTED
            i, j = i - 1, j - 1
        elif move == 2 or (move == 0 and i > 0):
            status[i - 1] = DELETED
            i -= 1
        else:  # a hypothesis word with no reference word: attach to the reference word before it
            inserted[max(i - 1, 0)] += 1
            j -= 1
    return Alignment(status, inserted)


@dataclass(frozen=True)
class RefWord:
    word: str
    start: float
    end: float

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass(frozen=True)
class Run:
    """One transcription: the words it returned, where the gateway cut, and how far into the recording it began."""

    words: list[str]
    segments: list[tuple[float, float]]
    offset: float = 0.0  # seconds of the recording before this run's audio began

    def seams(self) -> list[float]:
        """Cut times on the recording's own clock."""
        start_cut = [self.offset] if self.offset > 0 else []
        return start_cut + [end + self.offset for _start, end in self.segments[:-1]]


@dataclass
class Scored:
    """A run aligned to the reference, with the reference-word lookup by time."""

    run: Run
    alignment: Alignment
    mids: list[float]
    reference_indexes: list[int]
    _time_indexes: list[int] = field(init=False, repr=False)
    _sorted_mids: list[float] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Overlapping words can reverse midpoint order. Alignment and identity stay in transcript order.
        self._time_indexes = sorted(range(len(self.mids)), key=self.mids.__getitem__)
        self._sorted_mids = [self.mids[i] for i in self._time_indexes]

    def between(self, lo: float, hi: float) -> list[int]:
        """Local alignment indexes whose reference midpoints fall in [lo, hi)."""
        return self._time_indexes[bisect_left(self._sorted_mids, lo):bisect_left(self._sorted_mids, hi)]

    def window(self, centre: float, half: float) -> list[int]:
        return self.between(centre - half, centre + half)


def score_run(reference: Sequence[RefWord], run: Run) -> Scored:
    end = run.offset + max((segment_end for _start, segment_end in run.segments), default=0.0)
    selected = [(index, word) for index, word in enumerate(reference) if run.offset <= word.mid < end]
    alignment = align([word.word for _index, word in selected], run.words)
    return Scored(run, alignment, [word.mid for _index, word in selected], [index for index, _word in selected])


@dataclass(frozen=True)
class Estimate:
    """A paired difference (percentage points) with its bootstrap interval."""

    points: int
    words: int
    value_pp: float
    low_pp: float
    high_pp: float

    def includes_zero(self) -> bool:
        return self.low_pp <= 0.0 <= self.high_pp

    def as_dict(self) -> dict[str, float | int]:
        return {"points": self.points, "words": self.words, "excess_pp": round(self.value_pp, 2),
                "low_pp": round(self.low_pp, 2), "high_pp": round(self.high_pp, 2)}


def _pairs(first: Scored, second: Scored, points: Sequence[float], half: float, clear: float) -> list[tuple[int, int, int]]:
    """For each place: (errors in ``first``, errors in ``second``, words) over the same reference words."""
    out: list[tuple[int, int, int]] = []
    clear = max(clear, 2.0 * half)
    other_seams = second.run.seams()
    second_indexes = {reference_index: index for index, reference_index in enumerate(second.reference_indexes)}
    for point in points:
        # The second run must have no cut near the place, or the comparison is not "cut against no cut".
        if any(abs(point - seam) < clear for seam in other_seams):
            continue
        indexes = [index for index in first.window(point, half)
                   if first.reference_indexes[index] in second_indexes]
        if len(indexes) == 0:
            continue
        out.append((sum(first.alignment.errors(i) for i in indexes),
                    sum(second.alignment.errors(second_indexes[first.reference_indexes[i]]) for i in indexes), len(indexes)))
    return out


def paired_excess(
    pairs: Sequence[tuple[int, int, int]], *, resamples: int = 4000, seed: int = 20261019,
) -> Estimate:
    """Mean of (errors with a cut - errors without) over words, in percentage points, with a 95% bootstrap interval."""
    if not pairs:
        return Estimate(0, 0, 0.0, 0.0, 0.0)
    total_words = sum(n for _a, _b, n in pairs)
    value = 100.0 * sum(a - b for a, b, _n in pairs) / total_words
    rng = random.Random(seed)
    count = len(pairs)
    draws = []
    for _ in range(resamples):
        sample = [pairs[rng.randrange(count)] for _ in range(count)]
        words = sum(n for _a, _b, n in sample)
        draws.append(100.0 * sum(a - b for a, b, _n in sample) / words if words else 0.0)
    draws.sort()
    return Estimate(count, total_words, value, draws[int(0.025 * resamples)], draws[int(0.975 * resamples) - 1])


def seam_pairs(
    first: Scored, second: Scored, *, half: float = 2.0, clear: float = 2.5, placebo_offset: float | None = None,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """(pairs at the cuts, pairs at the noise-floor places) from the cuts of both runs.

    ``placebo_offset`` is the distance from a cut to the place used as the noise floor (a quarter of the target).
    """
    pairs: list[tuple[int, int, int]] = []
    placebo: list[tuple[int, int, int]] = []
    for here, there in ((first, second), (second, first)):
        seams = here.run.seams()
        pairs += _pairs(here, there, seams, half, clear)
        if placebo_offset is not None:
            places = [seam + placebo_offset for seam in seams]
            # Keep each placebo window disjoint from every cut window in both runs.
            all_seams = seams + there.run.seams()
            separation = max(clear, 2.0 * half)
            places = [p for p in places if all(abs(p - s) >= separation for s in all_seams)]
            placebo += _pairs(here, there, places, half, clear)
    return pairs, placebo


def seam_excess(
    first: Scored, second: Scored, *, half: float = 2.0, clear: float = 2.5, placebo_offset: float | None = None,
    resamples: int = 4000,
) -> tuple[Estimate, Estimate]:
    """(excess at the cuts, placebo) pooled over the cuts of both runs."""
    pairs, placebo = seam_pairs(first, second, half=half, clear=clear, placebo_offset=placebo_offset)
    return paired_excess(pairs, resamples=resamples), paired_excess(placebo, resamples=resamples)


def piece_counts(scored: Scored, *, trim: float = 2.0) -> list[tuple[int, int, float]]:
    """(words, deleted, length) for each piece of a run, with ``trim`` seconds at each end left out."""
    out = []
    for start, end in scored.run.segments:
        lo, hi = start + scored.run.offset + trim, end + scored.run.offset - trim
        if hi <= lo:
            continue
        indexes = scored.between(lo, hi)
        out.append((len(indexes), sum(1 for i in indexes if scored.alignment.status[i] == DELETED), end - start))
    return out


def interior_deletions(scored: Scored, *, trim: float = 2.0) -> dict[int, int]:
    """Local alignment index -> 1 if dropped, for words at least ``trim`` s inside a piece.

    Use ``scored.reference_indexes`` to map these indexes to the supplied reference.
    """
    out: dict[int, int] = {}
    for start, end in scored.run.segments:
        lo, hi = start + scored.run.offset + trim, end + scored.run.offset - trim
        for i in scored.between(lo, hi) if hi > lo else ():
            out[i] = 1 if scored.alignment.status[i] == DELETED else 0
    return out


def deletion_blocks(
    base: Mapping[int, int], other: Mapping[int, int], mids: Sequence[float], *, block: float = 30.0,
) -> list[tuple[int, int, int]]:
    """Per ``block`` seconds: (deletions in ``other`` minus ``base``, 0, words) over words inside a piece in both runs.

    The same words are compared, so how often an engine drops a short backchannel cancels. Feed the blocks of all
    recordings to ``paired_excess`` for the interval (blocks, not words, are resampled).
    """
    blocks: dict[int, list[int]] = {}
    for index in base.keys() & other.keys():
        entry = blocks.setdefault(int(mids[index] // block), [0, 0])
        entry[0] += other[index] - base[index]
        entry[1] += 1
    return [(d, 0, n) for d, n in blocks.values()]


def rate_interval(pieces: Sequence[tuple[int, int, float]], *, resamples: int = 4000, seed: int = 20261019) -> dict[str, float | int]:
    """Deletion rate (%) over pieces with a 95% bootstrap interval resampling whole pieces."""
    pieces = [p for p in pieces if p[0] > 0]
    if not pieces:
        return {"pieces": 0, "words": 0, "deletion_pct": math.nan, "low_pct": math.nan, "high_pct": math.nan}
    total = sum(w for w, _d, _l in pieces)
    rate = 100.0 * sum(d for _w, d, _l in pieces) / total
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        sample = [pieces[rng.randrange(len(pieces))] for _ in pieces]
        words = sum(w for w, _d, _l in sample)
        draws.append(100.0 * sum(d for _w, d, _l in sample) / words if words else 0.0)
    draws.sort()
    return {"pieces": len(pieces), "words": total, "deletion_pct": round(rate, 2),
            "low_pct": round(draws[int(0.025 * resamples)], 2), "high_pct": round(draws[int(0.975 * resamples) - 1], 2)}


def difference(first: Sequence[tuple[int, int, int]], second: Sequence[tuple[int, int, int]], *, resamples: int = 4000,
               seed: int = 20261019) -> Estimate:
    """Excess of ``first`` minus excess of ``second`` (independent sets of places), with a bootstrap interval."""
    if not first or not second:
        return Estimate(0, 0, 0.0, 0.0, 0.0)

    def value(pairs: Sequence[tuple[int, int, int]]) -> float:
        words = sum(n for _a, _b, n in pairs)
        return 100.0 * sum(a - b for a, b, _n in pairs) / words if words else 0.0

    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        a = [first[rng.randrange(len(first))] for _ in first]
        b = [second[rng.randrange(len(second))] for _ in second]
        draws.append(value(a) - value(b))
    draws.sort()
    return Estimate(len(first) + len(second), sum(n for _a, _b, n in first) + sum(n for _a, _b, n in second),
                    value(first) - value(second), draws[int(0.025 * resamples)], draws[int(0.975 * resamples) - 1])


def deletion_by_length(
    runs: Sequence[tuple[Sequence[RefWord], Scored]], edges: Sequence[float], *, trim: float = 2.0,
) -> list[dict[str, float | int | str]]:
    """Deletion rate of reference words inside pieces, by piece length bucket (``trim`` s at each end left out)."""
    buckets = [{"words": 0, "deleted": 0, "pieces": 0} for _ in range(len(edges) + 1)]
    for reference, scored in runs:
        for start, end in scored.run.segments:
            length = end - start
            lo = start + scored.run.offset + trim
            hi = end + scored.run.offset - trim
            if hi <= lo:
                continue
            indexes = scored.between(lo, hi)
            slot = bisect_left(list(edges), length)
            bucket = buckets[slot]
            bucket["pieces"] += 1
            bucket["words"] += len(indexes)
            bucket["deleted"] += sum(1 for i in indexes if scored.alignment.status[i] == DELETED)
    rows = []
    for slot, bucket in enumerate(buckets):
        low = 0.0 if slot == 0 else edges[slot - 1]
        label = f"{low:g}-{edges[slot]:g} s" if slot < len(edges) else f">{edges[-1]:g} s"
        words = bucket["words"]
        rows.append({"bucket": label, "pieces": bucket["pieces"], "words": words,
                     "deletion_pct": round(100.0 * bucket["deleted"] / words, 2) if words else math.nan})
    return rows


def word_error_rate(scored: Scored) -> float:
    alignment = scored.alignment
    n = len(alignment.status)
    return 100.0 * sum(alignment.errors(i) for i in range(n)) / n if n else 0.0


@dataclass
class Recommendation:
    target_seconds: float
    search_seconds: float
    max_segment_seconds: float
    reasons: list[str] = field(default_factory=list)


def evidence_state(estimate: Mapping, *, min_points: int, min_words: int, margin: float) -> dict[str, str]:
    """A noninferiority screen, not evidence that the difference is zero."""
    enough = estimate.get("points", 0) >= min_points and estimate.get("words", 0) >= min_words
    valid = all(isinstance(estimate.get(k), (int, float)) and math.isfinite(estimate[k])
                for k in ("excess_pp", "low_pp", "high_pp"))
    if not enough or not valid or not estimate["low_pp"] <= estimate["excess_pp"] <= estimate["high_pp"]:
        return {"state": "inconclusive", "reason": "Insufficient paired words/blocks or pairs, or invalid interval."}
    if estimate["high_pp"] <= margin:
        return {"state": "recommended", "reason": f"Paired upper CI is within the {margin:g} percentage-point gross-harm margin."}
    if estimate["low_pp"] > margin:
        return {"state": "confidently_worse", "reason": f"Paired lower CI exceeds the {margin:g} percentage-point gross-harm margin."}
    return {"state": "inconclusive", "reason": f"Paired CI crosses the {margin:g} percentage-point gross-harm margin."}


def length_evidence(row: Mapping, policy: Mapping) -> dict[str, str]:
    if row.get("failed"):
        return {"state": "confidently_worse", "reason": "Confirmed upstream size refusal."}
    return evidence_state(row.get("paired_deletion", {}), min_points=policy["min_length_blocks"],
                          min_words=policy["min_length_words"], margin=policy["length_noninferiority_pp"])


def seam_evidence(row: Mapping, policy: Mapping) -> dict[str, str]:
    for key, pairs, words in ((None, "min_seam_pairs", "min_seam_words"), ("noise", "min_noise_pairs", "min_noise_words")):
        sample = row if key is None else row.get(key, {})
        if sample.get("points", 0) < policy[pairs] or sample.get("words", 0) < policy[words]:
            return {"state": "inconclusive", "reason": "Insufficient seam or noise pairs/words."}
    return evidence_state(row.get("difference", {}), min_points=policy["min_seam_pairs"] + policy["min_noise_pairs"],
                          min_words=policy["min_seam_words"] + policy["min_noise_words"], margin=policy["seam_noninferiority_pp"])


def select_length(length_rows: Sequence[Mapping], *, policy: Mapping,
                  failed_above: float | None = None) -> tuple[float, list[str]] | None:
    """Longest measured maximum for which every measured length through it passes."""
    attempted = [row for row in length_rows if failed_above is None or row["length"] < failed_above]
    evidenced = [row for row in attempted if not row.get("failed")
                 and row.get("paired_deletion", {}).get("points", 0) >= policy["min_length_blocks"]
                 and row.get("paired_deletion", {}).get("words", 0) >= policy["min_length_words"]]
    if len({row["length"] for row in evidenced}) < policy["min_length_rows"]:
        return None
    supported = [row for row in evidenced if length_evidence(row, policy)["state"] == "recommended"
                 and all(length_evidence(other, policy)["state"] == "recommended"
                         for other in attempted if other["length"] <= row["length"])]
    if not supported:
        return None
    chosen = max(supported, key=lambda row: row["length"])
    reasons = [f"Maximum {chosen['length']:g} s against shortest successful {chosen['baseline_length']:g} s: "
               + length_evidence(chosen, policy)["reason"], "Gross-harm screen only; this does not prove no harm."]
    blockers = [row["length"] for row in attempted if row["length"] <= chosen["length"]
                and length_evidence(row, policy)["state"] != "recommended"]
    if blockers:
        reasons.append(f"A non-passing measured length at {min(blockers):g} s bounds this maximum.")
    return float(chosen["length"]), reasons


def target_for_maximum(maximum: float, policy: Mapping[str, float]) -> float:
    return max(1.0, math.floor(maximum * policy["target_margin"]))


def recommend(length_rows: Sequence[Mapping], search_rows: Sequence[Mapping], *,
              policy: Mapping | None = None, failed_above: float | None = None,
              current_search: float | None = None) -> Recommendation | None:
    if policy is None:
        return None
    length = select_length(length_rows, policy=policy, failed_above=failed_above)
    if length is None:
        return None
    supported = [row for row in search_rows if seam_evidence(row, policy)["state"] == "recommended"]
    if not supported:
        return None
    current = next((row for row in supported if row["search"] == current_search), None)
    # The upper interval bound is the conservative tie-break. Keep the current
    # setting only when its evidence is tied for best; a missing/unsupported
    # current setting never silently turns into zero width.
    best_high = min(row["difference"]["high_pp"] for row in supported)
    tied = [row for row in supported if row["difference"]["high_pp"] == best_high]
    chosen = current if current in tied else min(tied, key=lambda row: row["search"])
    maximum, reasons = length
    reasons.append(f"Search {chosen['search']:g} s: " + seam_evidence(chosen, policy)["reason"])
    return Recommendation(target_for_maximum(maximum, policy), float(chosen["search"]), maximum, reasons)
