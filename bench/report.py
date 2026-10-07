"""Aggregate benchmark run reports with hierarchical bootstrap uncertainty."""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

from .compare import (
    METADATA_KEYS,
    PROVENANCE_REQUIRED,
    _blocks,
    _metric_map,
)
from .stats import _statistic, percentile


BOOTSTRAP = 500


def _load(path):
    try:
        with Path(path).open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def _validate_metadata(runs):
    """Check fixed settings while allowing per-file mix subsets."""
    ref = runs[0]
    if any(run["provenance"]["source_sha256"] != ref["provenance"]["source_sha256"] for run in runs):
        raise ValueError("pooled runs have different production sources")
    for index, run in enumerate(runs[1:], 2):
        for key in METADATA_KEYS - {"reps", "mix_hashes"}:
            if run["metadata"][key] != ref["metadata"][key]:
                raise ValueError(f"run {index}: incompatible {key}")
    for mix in {mix for run in runs for mix in run["metadata"]["mix_hashes"]}:
        hashes = {run["metadata"]["mix_hashes"][mix] for run in runs
                  if mix in run["metadata"]["mix_hashes"]}
        if len(hashes) != 1:
            raise ValueError(f"incompatible mix hash for {mix}")
    for key in PROVENANCE_REQUIRED - {"started_at"}:
        value = ref.get("provenance", {}).get(key)
        if any(run.get("provenance", {}).get(key) != value for run in runs):
            raise ValueError(f"incompatible provenance: {key} differs")


def _draw_run(run, rng):
    batches = run["batches"]
    indices = [rng.randrange(len(batches)) for _ in batches]
    chosen = [batches[index] for index in indices]
    drawn = {"samples": [value for batch in chosen for value in batch],
             "reference_samples": None}
    if run["reference_batches"] is not None:
        references = [run["reference_batches"][index] for index in indices]
        drawn["reference_samples"] = [value for batch in references for value in batch]
    return drawn


def _hierarchy_draw(runs, rng):
    chosen = [runs[rng.randrange(len(runs))] for _ in runs]
    drawn_runs = [_draw_run(run, rng) for run in chosen]
    return {
        "samples": [value for run in drawn_runs for value in run["samples"]],
        "reference_samples": (
            [value for run in drawn_runs for value in run["reference_samples"]]
            if drawn_runs[0]["reference_samples"] is not None else None
        ),
    }


def _estimate(metric, statistic):
    value = _statistic(metric["samples"], statistic)
    if metric["reference_samples"] is not None:
        value -= _statistic(metric["reference_samples"], statistic)
    return value


def _aggregate_metric(key, runs, seed):
    statistic = key[-1]
    samples = [value for run in runs for value in run["samples"]]
    references = ([value for run in runs for value in run["reference_samples"]]
                  if runs[0]["reference_samples"] is not None else None)
    point = _estimate({"samples": samples, "reference_samples": references}, statistic)
    rng = random.Random(seed)
    estimates = [_estimate(_hierarchy_draw(runs, rng), statistic)
                 for _ in range(BOOTSTRAP)]
    ci = [percentile(estimates, 2.5), percentile(estimates, 97.5)]
    per_run = [_estimate(run, statistic) for run in runs]
    run_std = statistics.stdev(per_run) if len(per_run) > 1 else 0.0

    # These are two bootstrap draws from the observed runs, not a real A/A test.
    delta_rng = random.Random(seed ^ 0xA11A)
    aa_deltas = []
    for _ in range(BOOTSTRAP):
        left = _estimate(_hierarchy_draw(runs, delta_rng), statistic)
        right = _estimate(_hierarchy_draw(runs, delta_rng), statistic)
        aa_deltas.append(right - left)
    delta_ci = [percentile(aa_deltas, 2.5), percentile(aa_deltas, 97.5)]
    empirical_noise = max(
        (abs(left - right) for index, left in enumerate(per_run)
         for right in per_run[index + 1:]), default=0.0)
    bootstrap_noise = max(abs(delta_ci[0]), abs(delta_ci[1]))
    return {
        "mix": key[0], "case": key[1], "concurrency": key[2],
        "metric": key[3], "unit": key[4], "statistic": statistic,
        "value": point, "ci95": ci,
        "noise_floor": max(point - ci[0], ci[1] - point),
        "run_to_run_std": run_std, "n": len(samples), "run_count": len(runs),
        "undersampled_tails": ((statistic == "p95" and any(len(r["samples"]) < 100 for r in runs))
                                or (statistic == "p99" and any(len(r["samples"]) < 1000 for r in runs))),
        "aa_delta_ci95": delta_ci,
        "empirical_noise_max_delta": empirical_noise,
        "aa_bootstrap_noise_floor_delta": bootstrap_noise,
        "noise_floor_delta": max(empirical_noise, bootstrap_noise),
    }


def aggregate_documents(documents):
    if not documents:
        raise ValueError("at least one input report is required")
    runs = []
    for index, document in enumerate(documents, 1):
        runs.extend(_blocks(document, f"input {index}"))
    _validate_metadata(runs)
    by_mix = {}
    for mix in {mix for run in runs for mix in run["metadata"]["mix_hashes"]}:
        selected = []
        for run in runs:
            if mix not in run["metadata"]["mix_hashes"]:
                continue
            selected.append({**run, "metrics": [metric for metric in run["metrics"] if metric.get("mix") == mix]})
        maps = _metric_map(selected, mix)
        for key in maps:
            if key[0] != mix:
                raise ValueError(f"{mix}: metric mix identity does not match metadata")
        by_mix[mix] = maps

    rows = []
    metric_index = 0
    for mix in sorted(by_mix):
        for key in sorted(by_mix[mix]):
            rows.append(_aggregate_metric(key, by_mix[mix][key], 1701 + metric_index))
            metric_index += 1
    return {"runs": runs, "baseline": rows}


def _table(rows):
    print("mix   case              c1 overhead p50 / p95       c32 looplag p95   noise_floor_delta")
    for mix in sorted({r["mix"] for r in rows}):
        metrics = {(r["case"], r["concurrency"], r["metric"], r["statistic"]): r for r in rows if r["mix"] == mix}
        cases = sorted({r["case"] for r in rows if r["mix"] == mix})
        for case in cases:
            p50 = metrics.get((case, 1, "overhead", "p50"))
            p95 = metrics.get((case, 1, "overhead", "p95"))
            lag = metrics.get((case, 32, "loop_lag", "p95"))
            if not (p50 or p95 or lag):
                continue
            overhead = f"{p50['value']:.3f} / {p95['value']:.3f}" if p50 and p95 else "—"
            lag_value = f"{lag['value']:.3f}" if lag else "—"
            noise = max((r["noise_floor_delta"] for r in (p50, p95, lag) if r), default=0.0)
            print(f"{mix:5} {case:17} {overhead:24} {lag_value:16} {noise:.3f}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--json", required=True, type=Path, dest="output")
    args = parser.parse_args(argv)
    try:
        result = aggregate_documents([_load(path) for path in args.files])
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _table(result["baseline"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
