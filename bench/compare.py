"""Compare benchmark JSON files with request and run-block bootstrap intervals."""

import json
import math
import random
import sys
from pathlib import Path

from .stats import _statistic, percentile


METADATA_KEYS = {
    "protocol_version", "mix_hashes", "concurrency", "reps", "warmup",
    "backend_delay_ms", "chunk_interval_ms", "cpu_affinity", "python",
    "dependencies", "profile",
}
PROVENANCE_REQUIRED = {
    "harness_sha256", "lock_sha256", "kernel", "machine", "cpu_model",
    "network_namespace", "started_at", "state_filesystem", "source_sha256",
}


def _load(path):
    try:
        with Path(path).open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def _check_run(run, label):
    if not isinstance(run, dict) or run.get("schema_version") != 1:
        raise ValueError(f"{label}: expected schema_version 1")
    correctness = run.get("correctness")
    if (not isinstance(correctness, dict) or correctness.get("passed") is not True
            or correctness.get("score") != 1):
        raise ValueError(f"{label}: correctness failed or score is not 1")
    metadata = run.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != METADATA_KEYS:
        raise ValueError(f"{label}: metadata keys do not match the required shape")
    if metadata["profile"] is not False:
        raise ValueError(f"{label}: profiled runs cannot be compared")
    provenance = run.get("provenance")
    if not isinstance(provenance, dict) or any(
            key not in provenance or not isinstance(provenance[key], str)
            or not provenance[key].strip()
            for key in PROVENANCE_REQUIRED - {"network_namespace"}):
        raise ValueError(f"{label}: provenance is missing required fields")
    if not isinstance(provenance.get("network_namespace"), bool):
        raise ValueError(f"{label}: provenance network_namespace must be boolean")
    if not isinstance(run.get("metrics"), list):
        raise ValueError(f"{label}: metrics must be a list")


def _blocks(document, label):
    """Return constituent runs; aggregate envelopes may contain runs:[run,...]."""
    if not isinstance(document, dict):
        raise ValueError(f"{label}: JSON root must be an object")
    if "runs" not in document:
        _check_run(document, label)
        return [document]
    runs = document["runs"]
    if not isinstance(runs, list) or not runs:
        raise ValueError(f"{label}: runs must be a nonempty list")
    for index, run in enumerate(runs):
        _check_run(run, f"{label} run {index + 1}")
    return runs


def _metadata_compatible(base, new):
    keys = METADATA_KEYS - {"reps"}
    for label, blocks in (("baseline", base), ("new", new)):
        reference = blocks[0]["metadata"]
        for index, run in enumerate(blocks[1:], 2):
            for key in keys:
                if run["metadata"][key] != reference[key]:
                    raise ValueError(f"{label} run {index}: incompatible {key}")
        if any(run["provenance"]["source_sha256"] != blocks[0]["provenance"]["source_sha256"] for run in blocks):
            raise ValueError(f"{label}: pooled runs have different production sources")
    for key in keys:
        if base[0]["metadata"][key] != new[0]["metadata"][key]:
            raise ValueError(f"incompatible metadata: {key} differs")
    for key in PROVENANCE_REQUIRED - {"started_at", "source_sha256"}:
        reference = base[0].get("provenance", {}).get(key)
        if any(run.get("provenance", {}).get(key) != reference for run in base + new):
            raise ValueError(f"incompatible provenance: {key} differs")


def _metric_key(metric):
    fields = ("mix", "case", "concurrency", "metric", "unit", "statistic")
    if not isinstance(metric, dict) or any(key not in metric for key in fields):
        raise ValueError("metric entry is missing identity fields")
    key = tuple(metric[field] for field in fields)
    samples = metric.get("samples")
    if (not isinstance(samples, list) or not samples
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in samples)):
        raise ValueError(f"metric {key!r}: samples must be nonempty finite numbers")
    if metric["statistic"] not in {"p50", "p95", "p99", "mean"}:
        raise ValueError(f"metric {key!r}: unsupported statistic")
    return key


def _metric_map(blocks, label):
    per_run = []
    for block_index, run in enumerate(blocks, 1):
        result = {}
        for metric in run["metrics"]:
            key = _metric_key(metric)
            if key in result:
                raise ValueError(f"{label} run {block_index}: duplicate metric {key!r}")
            samples = list(map(float, metric["samples"]))
            raw_batches = metric.get("sample_blocks")
            if "sample_blocks" not in metric:
                batches = [[sample] for sample in samples]
            else:
                if not isinstance(raw_batches, list) or not raw_batches:
                    raise ValueError(f"metric {key!r}: sample_blocks must be a nonempty list")
                batches = []
                for batch in raw_batches:
                    if (not isinstance(batch, list) or not batch
                            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                   or not math.isfinite(v) for v in batch)):
                        raise ValueError(f"metric {key!r}: sample_blocks must contain nonempty finite batches")
                    batches.append(list(map(float, batch)))
                if [v for batch in batches for v in batch] != samples:
                    raise ValueError(f"metric {key!r}: sample_blocks do not match samples")
            has_references = "reference_samples" in metric
            if "reference_blocks" in metric and not has_references:
                raise ValueError(f"metric {key!r}: reference_blocks require reference_samples")
            reference_samples = None
            reference_batches = None
            if has_references:
                raw_references = metric["reference_samples"]
                if (not isinstance(raw_references, list) or len(raw_references) != len(samples)
                        or any(isinstance(v, bool) or not isinstance(v, (int, float))
                               or not math.isfinite(v) for v in raw_references)):
                    raise ValueError(f"metric {key!r}: reference_samples must pair with samples")
                reference_samples = list(map(float, raw_references))
                raw_reference_batches = metric.get("reference_blocks")
                if raw_reference_batches is None:
                    reference_batches = []
                    offset = 0
                    for batch in batches:
                        reference_batches.append(reference_samples[offset:offset + len(batch)])
                        offset += len(batch)
                else:
                    if not isinstance(raw_reference_batches, list) or not raw_reference_batches:
                        raise ValueError(f"metric {key!r}: reference_blocks must be a nonempty list")
                    reference_batches = []
                    for batch in raw_reference_batches:
                        if (not isinstance(batch, list) or not batch
                                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                       or not math.isfinite(v) for v in batch)):
                            raise ValueError(f"metric {key!r}: reference_blocks must contain finite batches")
                        reference_batches.append(list(map(float, batch)))
                    if [v for batch in reference_batches for v in batch] != reference_samples:
                        raise ValueError(f"metric {key!r}: reference_blocks do not match reference_samples")
                if (len(reference_batches) != len(batches)
                        or any(len(gateway) != len(reference)
                               for gateway, reference in zip(batches, reference_batches))):
                    raise ValueError(f"metric {key!r}: reference blocks must pair with gateway batches")
            result[key] = {
                "samples": samples, "batches": batches,
                "reference_samples": reference_samples,
                "reference_batches": reference_batches,
            }
        per_run.append(result)
    expected = set(per_run[0])
    for index, metrics in enumerate(per_run[1:], 2):
        missing, added = expected - set(metrics), set(metrics) - expected
        if missing or added:
            details = []
            if missing:
                details.append("missing metrics: " + ", ".join(map(repr, sorted(missing))))
            if added:
                details.append("new metrics: " + ", ".join(map(repr, sorted(added))))
            raise ValueError(f"{label} run {index}: " + "; ".join(details))
    mapped = {key: [metrics[key] for metrics in per_run] for key in expected}
    for key, runs in mapped.items():
        if any(run["reference_samples"] is not None for run in runs) and any(
                run["reference_samples"] is None for run in runs):
            raise ValueError(f"{label}: metric {key!r} has references in only some runs")
    return mapped


def _estimate(blocks, statistic):
    samples = [value for block in blocks for value in block["samples"]]
    value = _statistic(samples, statistic)
    if blocks and blocks[0]["reference_samples"] is not None:
        references = [value for block in blocks for value in block["reference_samples"]]
        value -= _statistic(references, statistic)
    return value


def _resample_side(blocks, rng):
    chosen = [blocks[rng.randrange(len(blocks))] for _ in blocks]
    sampled = []
    for run in chosen:
        batches = run["batches"]
        indices = [rng.randrange(len(batches)) for _ in batches]
        batches = [batches[index] for index in indices]
        result = {"samples": [value for batch in batches for value in batch],
                  "reference_samples": None}
        if run["reference_batches"] is not None:
            references = [run["reference_batches"][index] for index in indices]
            result["reference_samples"] = [value for batch in references for value in batch]
        sampled.append(result)
    return sampled


def compare_documents(base_doc, new_doc, bootstrap=1000, seed=17):
    base_runs, new_runs = _blocks(base_doc, "baseline"), _blocks(new_doc, "new")
    # Separate train/test runs and --mix all have the same comparison semantics.
    # Each mix retains its own independent process blocks.
    base_mixes = {mix for run in base_runs for mix in run["metadata"]["mix_hashes"]}
    new_mixes = {mix for run in new_runs for mix in run["metadata"]["mix_hashes"]}
    if base_mixes != new_mixes:
        raise ValueError("incompatible metadata: mix_hashes differs")
    if len(base_mixes) > 1:
        results = []
        for mix in sorted(base_mixes):
            sides = []
            for runs in (base_runs, new_runs):
                selected = []
                for run in runs:
                    if mix not in run["metadata"]["mix_hashes"]:
                        continue
                    selected.append({**run,
                        "metadata": {**run["metadata"], "mix_hashes": {mix: run["metadata"]["mix_hashes"][mix]}},
                        "metrics": [m for m in run["metrics"] if m.get("mix") == mix]})
                sides.append({"runs": selected})
            results.extend(compare_documents(*sides, bootstrap=bootstrap, seed=seed))
        return results
    _metadata_compatible(base_runs, new_runs)
    base_metrics = _metric_map(base_runs, "baseline")
    new_metrics = _metric_map(new_runs, "new")
    missing, added = sorted(base_metrics.keys() - new_metrics.keys()), sorted(new_metrics.keys() - base_metrics.keys())
    if missing or added:
        details = []
        if missing:
            details.append("missing metrics: " + ", ".join(map(repr, missing)))
        if added:
            details.append("new metrics: " + ", ".join(map(repr, added)))
        raise ValueError("; ".join(details))
    for key in base_metrics:
        baseline_has_references = base_metrics[key][0]["reference_samples"] is not None
        new_has_references = new_metrics[key][0]["reference_samples"] is not None
        if baseline_has_references != new_has_references:
            raise ValueError(f"metric {key!r}: references must be present in both conditions")
    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 1:
        raise ValueError("bootstrap must be a positive integer")

    results = []
    for index, key in enumerate(sorted(base_metrics)):
        left, right = base_metrics[key], new_metrics[key]
        statistic = key[-1]
        base_value, new_value = _estimate(left, statistic), _estimate(right, statistic)
        delta = new_value - base_value
        rng = random.Random(seed + index)
        deltas = []
        for _ in range(bootstrap):
            sampled_base = _resample_side(left, rng)
            sampled_new = _resample_side(right, rng)
            deltas.append(_estimate(sampled_new, statistic) - _estimate(sampled_base, statistic))
        ci = [percentile(deltas, 2.5), percentile(deltas, 97.5)]
        total_samples = sum(len(run["samples"]) for run in left + right)
        few_runs = min(len(left), len(right)) < 3
        fewer_than_five = min(len(left), len(right)) < 5
        minimum = 100 if statistic == "p95" else 1000 if statistic == "p99" else 20
        tails_undersampled = min(len(run["samples"]) for run in left + right) < minimum
        results.append({
            "mix": key[0], "case": key[1], "concurrency": key[2],
            "metric": key[3], "unit": key[4], "statistic": statistic,
            "baseline": base_value, "new": new_value,
            "absolute_delta": delta,
            "percent_delta": (delta / base_value * 100) if base_value else None,
            "delta_ci95": ci,
            "inside_noise": ci[0] <= 0 <= ci[1],
            "run_blocks": {"baseline": len(left), "new": len(right)},
            "batch_blocks": {
                "baseline": sum(len(run["batches"]) for run in left),
                "new": sum(len(run["batches"]) for run in right),
            },
            "flags": {
                "fewer_than_3_run_blocks": few_runs,
                "fewer_than_5_run_blocks": fewer_than_five,
                "ci_tails_undersampled": tails_undersampled,
            },
            "sample_count": total_samples,
        })
    return results


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m bench.compare base.json new.json", file=sys.stderr)
        return 2
    try:
        results = compare_documents(_load(args[0]), _load(args[1]))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("metric | baseline | new | delta | delta % | 95% CI | noise")
    for row in results:
        percent = "n/a" if row["percent_delta"] is None else f"{row['percent_delta']:+.2f}%"
        lo, hi = row["delta_ci95"]
        noise = "inside" if row["inside_noise"] else "outside"
        if row["flags"]["fewer_than_3_run_blocks"]:
            noise += ", <3 runs"
        elif row["flags"]["fewer_than_5_run_blocks"]:
            noise += ", <5 runs, exploratory"
        if row["flags"]["ci_tails_undersampled"]:
            noise += ", undersampled"
        name = f"{row['mix']}/{row['case']}/{row['concurrency']} {row['metric']} {row['statistic']} ({row['unit']})"
        print(f"{name} | {row['baseline']:.4g} | {row['new']:.4g} | {row['absolute_delta']:+.4g} | {percent} | [{lo:.4g}, {hi:.4g}] | {noise}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
