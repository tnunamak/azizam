"""Small deterministic bootstrap statistics for benchmark measurements."""

import math
import random
import statistics


_STATISTICS = {"p50", "p95", "p99", "mean"}


def percentile(values, p):
    """Return a linearly interpolated percentile (p is in the range 0..100)."""
    data = sorted(float(value) for value in values)
    if not data:
        raise ValueError("percentile requires at least one value")
    if not math.isfinite(float(p)) or not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    position = (len(data) - 1) * float(p) / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return data[lower] + (data[upper] - data[lower]) * (position - lower)


def _statistic(values, statistic):
    if statistic == "mean":
        return statistics.fmean(values)
    return percentile(values, {"p50": 50, "p95": 95, "p99": 99}[statistic])


def summarize(values, statistic="p50", seed=17, bootstrap=500):
    """Summarize observations and return a deterministic percentile bootstrap CI."""
    data = [float(value) for value in values]
    if not data or not all(math.isfinite(value) for value in data):
        raise ValueError("values must contain finite numbers")
    if statistic not in _STATISTICS:
        raise ValueError("statistic must be p50, p95, p99, or mean")
    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 1:
        raise ValueError("bootstrap must be a positive integer")

    value = _statistic(data, statistic)
    rng = random.Random(seed)
    estimates = [
        _statistic([data[rng.randrange(len(data))] for _ in data], statistic)
        for _ in range(bootstrap)
    ]
    lo, hi = percentile(estimates, 2.5), percentile(estimates, 97.5)
    std = statistics.stdev(data) if len(data) > 1 else 0.0
    return {
        "value": value,
        "ci95": [lo, hi],
        "std": std,
        "n": len(data),
        "noise_half_width": max(value - lo, hi - value),
    }


def summarize_blocks(blocks, statistic="p50", seed=17, bootstrap=500,
                     reference_blocks=None):
    """Summarize batches, optionally estimating paired gateway-reference deltas."""
    if statistic not in _STATISTICS:
        raise ValueError("statistic must be p50, p95, p99, or mean")
    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 1:
        raise ValueError("bootstrap must be a positive integer")
    if not isinstance(blocks, (list, tuple)) or not blocks:
        raise ValueError("blocks must contain at least one nonempty batch")
    batches = []
    for block in blocks:
        if not isinstance(block, (list, tuple)) or not block:
            raise ValueError("each block must contain at least one finite number")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in block):
            raise ValueError("blocks must contain finite numbers")
        batch = [float(value) for value in block]
        if not all(math.isfinite(value) for value in batch):
            raise ValueError("blocks must contain finite numbers")
        batches.append(batch)

    reference_batches = None
    if reference_blocks is not None:
        if not isinstance(reference_blocks, (list, tuple)) or not reference_blocks:
            raise ValueError("reference_blocks must contain at least one nonempty batch")
        if len(reference_blocks) != len(batches):
            raise ValueError("reference_blocks must match gateway batch count")
        reference_batches = []
        for index, block in enumerate(reference_blocks):
            if not isinstance(block, (list, tuple)) or not block:
                raise ValueError("each reference block must contain finite numbers")
            if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in block)):
                raise ValueError("reference_blocks must contain finite numbers")
            batch = [float(value) for value in block]
            if not all(math.isfinite(value) for value in batch):
                raise ValueError("reference_blocks must contain finite numbers")
            if len(batch) != len(batches[index]):
                raise ValueError("reference blocks must pair with gateway batch lengths")
            reference_batches.append(batch)

    data = [value for batch in batches for value in batch]
    reference_data = ([value for batch in reference_batches for value in batch]
                      if reference_batches is not None else None)
    value = _statistic(data, statistic)
    if reference_data is not None:
        value -= _statistic(reference_data, statistic)
    rng = random.Random(seed)
    estimates = []
    for _ in range(bootstrap):
        indices = [rng.randrange(len(batches)) for _ in batches]
        sampled = [batches[index] for index in indices]
        estimate = _statistic([v for batch in sampled for v in batch], statistic)
        if reference_batches is not None:
            reference_sampled = [reference_batches[index] for index in indices]
            estimate -= _statistic(
                [v for batch in reference_sampled for v in batch], statistic)
        estimates.append(estimate)
    lo, hi = percentile(estimates, 2.5), percentile(estimates, 97.5)
    return {
        "value": value,
        "ci95": [lo, hi],
        "std": statistics.stdev(
            [gateway - reference for gateway, reference in zip(data, reference_data)]
            if reference_data is not None else data
        ) if len(data) > 1 else 0.0,
        "n": len(data),
        "noise_half_width": max(value - lo, hi - value),
        "batch_count": len(batches),
        "noise_estimable": len(batches) >= 2,
    }
