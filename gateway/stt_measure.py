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
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
# Which statuses count as an error for a reference word.
OK, SUBSTITUTED, DELETED = 0, 1, 2


def normalize(text: str, drop: frozenset[str] = frozenset()) -> list[str]:
    """Lowercase words with punctuation removed; apostrophes inside a word stay; ``drop`` words are removed."""
    words = _WORD.findall(text.lower().replace("’", "'"))
    return [word for word in words if word not in drop]


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
        return [end + self.offset for _start, end in self.segments[:-1]]


@dataclass
class Scored:
    """A run aligned to the reference, with the reference-word lookup by time."""

    run: Run
    alignment: Alignment
    mids: list[float]

    def window(self, centre: float, half: float) -> range:
        return range(bisect_left(self.mids, centre - half), bisect_left(self.mids, centre + half))


def score_run(reference: Sequence[RefWord], run: Run) -> Scored:
    alignment = align([w.word for w in reference], run.words)
    return Scored(run, alignment, [w.mid for w in reference])


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
    other_seams = second.run.seams()
    for point in points:
        # The second run must have no cut near the place, or the comparison is not "cut against no cut".
        if any(abs(point - seam) < clear for seam in other_seams):
            continue
        indexes = first.window(point, half)
        if len(indexes) == 0:
            continue
        out.append((sum(first.alignment.errors(i) for i in indexes), sum(second.alignment.errors(i) for i in indexes), len(indexes)))
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
            # Away from every cut in both runs, so neither run is paying for one.
            places = [p for p in places if all(abs(p - s) >= clear for s in seams)]
            placebo += _pairs(here, there, places, half, clear)
    return pairs, placebo


def seam_excess(
    first: Scored, second: Scored, *, half: float = 2.0, clear: float = 2.5, placebo_offset: float | None = None,
    resamples: int = 4000,
) -> tuple[Estimate, Estimate]:
    """(excess at the cuts, placebo) pooled over the cuts of both runs."""
    pairs, placebo = seam_pairs(first, second, half=half, clear=clear, placebo_offset=placebo_offset)
    return paired_excess(pairs, resamples=resamples), paired_excess(placebo, resamples=resamples)


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
            indexes = range(bisect_left(scored.mids, lo), bisect_left(scored.mids, hi))
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


def recommend(
    length_rows: Sequence[Mapping[str, float]], search_rows: Sequence[Mapping[str, float]], *, tolerance_pp: float = 0.5,
    margin: float = 0.75, failed_above: float | None = None,
) -> Recommendation | None:
    """Pick the settings from what was measured; detect and advise, nothing is applied.

    ``length_rows``: ``{"length": seconds, "deletion_pct": x, "words": n}`` for pieces of that length, in order.
    The hard maximum is the longest length whose deletion rate is within ``tolerance_pp`` of the best rate seen at
    shorter lengths and below every length that is worse or failed. The target is ``margin`` of it (cuts may fall a
    little late, never past the maximum). ``search_rows``: ``{"search": seconds, "excess_pp": x, "low_pp": a, "high_pp": b}``;
    the narrowest width whose excess is not worse than the best interval's upper end.
    """
    usable = [row for row in length_rows if row.get("words", 0) and not math.isnan(row["deletion_pct"])]
    if len(usable) < 2 or not search_rows:
        return None
    usable.sort(key=lambda row: row["length"])
    best = min(row["deletion_pct"] for row in usable)
    maximum = usable[0]["length"]
    reasons = [f"Deletion is {best:.1f}% at its best; the longest piece within {tolerance_pp:g} points of that is kept."]
    for row in usable:
        if row["deletion_pct"] <= best + tolerance_pp and (failed_above is None or row["length"] < failed_above):
            maximum = row["length"]
        else:
            break
    if failed_above is not None:
        reasons.append(f"The engine refused pieces of {failed_above:g} s and longer.")
    ranked = sorted(search_rows, key=lambda row: row["excess_pp"])
    limit = ranked[0]["high_pp"]
    chosen = min((row for row in search_rows if row["excess_pp"] <= limit), key=lambda row: row["search"])
    reasons.append(
        f"Searching {chosen['search']:g} s either side of each target costs {chosen['excess_pp']:+.1f} points near a cut "
        f"(the best width measured: {ranked[0]['search']:g} s at {ranked[0]['excess_pp']:+.1f})."
    )
    search = float(chosen["search"])
    target = max(1.0, math.floor(maximum * margin))
    return Recommendation(target, search, maximum, reasons)
