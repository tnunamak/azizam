import contextlib
import io
import random
import unittest
from unittest.mock import patch

from bench.compare import compare_documents, main
from bench.report import aggregate_documents
from bench.stats import percentile, summarize, summarize_blocks


def run(samples, reps=None, profile=False, passed=True, references=None,
        reference_blocks=None, statistic="p50", started_at="2026-01-01T00:00:00Z"):
    metric = {
        "mix": "basic", "case": "chat", "concurrency": 1,
        "metric": "latency", "unit": "ms", "statistic": statistic,
        "samples": samples,
    }
    if references is not None:
        metric["reference_samples"] = references
    if reference_blocks is not None:
        metric["reference_blocks"] = reference_blocks
    return {
        "schema_version": 1,
        "correctness": {"passed": passed, "score": int(passed)},
        "metadata": {
            "protocol_version": "v1", "mix_hashes": {"basic": "abc"},
            "concurrency": [1, 8, 32], "reps": reps or len(samples),
            "warmup": 1, "backend_delay_ms": 0, "chunk_interval_ms": 0,
            "cpu_affinity": None, "python": "3.x", "dependencies": {},
            "profile": profile,
        },
        "provenance": {
            "harness_sha256": "harness", "lock_sha256": "lock",
            "kernel": "kernel", "machine": "machine", "cpu_model": "cpu",
            "network_namespace": True, "started_at": started_at, "state_filesystem": "tmpfs", "source_sha256": "source",
        },
        "metrics": [metric],
    }


class StatsTests(unittest.TestCase):
    def test_single_draw_is_a_point_interval_for_both_summaries(self):
        for call, data in ((summarize, [3, 8]), (summarize_blocks, [[3], [8]])):
            with self.subTest(function=call.__name__):
                result = call(data, statistic="mean", seed=8, bootstrap=1)
                self.assertEqual(result["value"], 5.5)
                self.assertEqual(result["ci95"][0], result["ci95"][1])
                self.assertEqual(result["noise_half_width"], abs(result["value"] - result["ci95"][0]))

    def test_percentile_interpolates(self):
        self.assertEqual(percentile([0, 10], 25), 2.5)

    def test_percentile_endpoints_are_extrema_for_unsorted_observations(self):
        self.assertEqual(percentile([7, 2, 9], 0), 2)
        self.assertEqual(percentile([7, 2, 9], 100), 9)

    def test_percentile_rejects_nonfinite_and_out_of_range_percentiles(self):
        for quantile in (-1, 101, float("nan"), float("inf")):
            with self.subTest(quantile=quantile):
                with self.assertRaisesRegex(ValueError, "^p must be between 0 and 100$"):
                    percentile([2, 8], quantile)

    def test_both_summaries_require_a_positive_integer_bootstrap_count(self):
        for summarize_fn, values in ((summarize, [2, 8]), (summarize_blocks, [[2], [8]])):
            for count in (0, -1, True, 1.0, 1.5):
                with self.subTest(summary=summarize_fn.__name__, count=count):
                    with self.assertRaisesRegex(ValueError, "^bootstrap must be a positive integer$"):
                        summarize_fn(values, bootstrap=count)

    def test_single_observation_tail_has_no_bootstrap_spread(self):
        self.assertEqual(summarize([7], statistic="p99", bootstrap=3), {
            "value": 7, "ci95": [7, 7], "std": 0, "n": 1, "noise_half_width": 0,
        })

    def test_summary_is_deterministic_and_has_expected_shape(self):
        first = summarize([1, 2, 3, 4], seed=3, bootstrap=50)
        self.assertEqual(first, summarize([1, 2, 3, 4], seed=3, bootstrap=50))
        self.assertEqual(set(first), {"value", "ci95", "std", "n", "noise_half_width"})
        self.assertEqual(first["value"], 2.5)
        self.assertEqual(first["n"], 4)

    def test_rejects_empty_and_unknown_statistic(self):
        with self.assertRaises(ValueError):
            summarize([])
        with self.assertRaises(ValueError):
            summarize([1], statistic="median")

    def test_percentile_of_an_empty_sequence_reports_missing_values(self):
        with self.assertRaisesRegex(ValueError, "^percentile requires at least one value$"):
            percentile([], 50)

    def test_single_batch_summary_refuses_nonfinite_samples(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "^values must contain finite numbers$"):
                    summarize([1.0, invalid], bootstrap=3)

    def test_one_paired_batch_cannot_estimate_between_batch_noise(self):
        result = summarize_blocks([[4, 8]], statistic="mean",
                                  reference_blocks=[[2, 6]], bootstrap=10)
        self.assertEqual(result["value"], 2)
        self.assertEqual(result["ci95"], [2, 2])
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["batch_count"], 1)
        self.assertFalse(result["noise_estimable"])

    def test_gateway_and_reference_blocks_reject_non_numeric_observations(self):
        for sample in (True, float("nan"), float("inf"), float("-inf"), "4"):
            for gateway, reference, diagnostic in (
                ([[1, sample]], [[1, 2]], "blocks must contain finite numbers"),
                ([[1, 2]], [[1, sample]], "reference_blocks must contain finite numbers"),
            ):
                with self.subTest(sample=sample, diagnostic=diagnostic):
                    with self.assertRaisesRegex(ValueError, f"^{diagnostic}$"):
                        summarize_blocks(gateway, reference_blocks=reference)

    def test_empty_gateway_batch_reports_missing_measurements(self):
        with self.assertRaisesRegex(ValueError, "^each block must contain at least one finite number$"):
            summarize_blocks([[1], []])

    def test_reference_batch_count_must_match_even_with_same_sample_count(self):
        with self.assertRaisesRegex(ValueError, "^reference_blocks must match gateway batch count$"):
            summarize_blocks([[1], [2]], reference_blocks=[[1, 2]])

    def test_block_summary_resamples_batches_and_reports_batch_count(self):
        summary = summarize_blocks([[1, 1], [9, 9]], seed=2, bootstrap=100)
        self.assertEqual(summary["value"], 5)
        self.assertEqual(summary["batch_count"], 2)
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["ci95"], [1, 9])

    def test_block_summary_does_not_claim_noise_from_one_batch(self):
        summary = summarize_blocks([[2, 8]], statistic="mean", bootstrap=20)
        self.assertEqual(summary["value"], 5)
        self.assertEqual(summary["ci95"], [5, 5])
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["batch_count"], 1)
        self.assertFalse(summary["noise_estimable"])

    def test_block_summary_weights_point_estimate_by_observations(self):
        summary = summarize_blocks([[0], [8, 8, 8]], statistic="mean",
                                   bootstrap=20)
        self.assertEqual(summary["value"], 6)
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["batch_count"], 2)
        self.assertTrue(summary["noise_estimable"])

    def test_block_summary_estimates_difference_of_quantiles(self):
        summary = summarize_blocks([[5, 6, 7]], statistic="p95",
                                   reference_blocks=[[1, 3, 5]], bootstrap=20)
        self.assertAlmostEqual(summary["value"], 2.1)
        self.assertNotEqual(summary["value"], 3.0)

    def test_block_summary_resamples_gateway_and_reference_batches_together(self):
        summary = summarize_blocks(
            [[1, 1], [9, 9]], statistic="mean", reference_blocks=[[0, 0], [8, 8]],
            bootstrap=100)
        self.assertEqual(summary["value"], 1)
        self.assertEqual(summary["ci95"], [1, 1])

    def test_single_paired_batch_reports_spread_without_estimable_batch_noise(self):
        summary = summarize_blocks([[8, 11, 17]], reference_blocks=[[7, 9, 14]],
                                   bootstrap=20)
        self.assertEqual(summary["value"], 2)
        self.assertEqual(summary["std"], 1)
        self.assertEqual(summary["ci95"], [2, 2])
        self.assertEqual(summary["batch_count"], 1)
        self.assertEqual(summary["n"], 3)
        self.assertFalse(summary["noise_estimable"])

    def test_paired_mean_weights_observations_not_unequal_batches(self):
        summary = summarize_blocks([[10], [20, 20, 20]], statistic="mean",
                                   reference_blocks=[[9], [14, 14, 14]], bootstrap=20)
        self.assertEqual(summary["value"], 4.75)
        self.assertEqual(summary["batch_count"], 2)
        self.assertEqual(summary["n"], 4)
        self.assertTrue(summary["noise_estimable"])

    def test_swapping_paired_conditions_reflects_the_interval(self):
        gateway = [[2, 5], [10, 17], [21, 35]]
        reference = [[1, 4], [9, 10], [15, 25]]
        forward = summarize_blocks(
            gateway, reference_blocks=reference, statistic="mean", seed=11, bootstrap=40)
        reverse = summarize_blocks(
            reference, reference_blocks=gateway, statistic="mean", seed=11, bootstrap=40)

        self.assertAlmostEqual(reverse["value"], -forward["value"])
        self.assertAlmostEqual(reverse["ci95"][0], -forward["ci95"][1])
        self.assertAlmostEqual(reverse["ci95"][1], -forward["ci95"][0])
        self.assertAlmostEqual(reverse["std"], forward["std"])
        self.assertAlmostEqual(reverse["noise_half_width"], forward["noise_half_width"])

    def test_block_summary_rejects_unmatched_reference_batches(self):
        with self.assertRaisesRegex(ValueError, "batch lengths"):
            summarize_blocks([[1, 2]], reference_blocks=[[1]])

    def test_gateway_nonfinite_batch_reports_invalid_measurements(self):
        with self.assertRaisesRegex(ValueError, "^blocks must contain finite numbers$"):
            summarize_blocks([[1.0, float("nan")]])

    def test_empty_reference_batch_reports_missing_measurements(self):
        with self.assertRaisesRegex(ValueError, "^each reference block must contain finite numbers$"):
            summarize_blocks([[1.0]], reference_blocks=[[]])

    def test_comparison_uses_hierarchical_run_blocks(self):
        base = {"runs": [run([10, 11, 12]), run([11, 12, 13]), run([12, 13, 14])]}
        new = {"runs": [run([12, 13, 14]), run([13, 14, 15]), run([14, 15, 16])]}
        result = compare_documents(base, new, bootstrap=100)[0]
        self.assertEqual(result["absolute_delta"], 2)
        self.assertEqual(result["run_blocks"], {"baseline": 3, "new": 3})
        self.assertFalse(result["flags"]["fewer_than_3_run_blocks"])
        self.assertTrue(result["flags"]["fewer_than_5_run_blocks"])

    def test_comparison_rejects_duplicate_process_blocks_on_either_side(self):
        for label in ("baseline", "new"):
            with self.subTest(label=label):
                repeated = {"runs": [run([1]), run([1]), run([1])]}
                fresh = run([2])
                base, new = (repeated, fresh) if label == "baseline" else (fresh, repeated)
                with self.assertRaisesRegex(ValueError, f"{label} run 2: duplicate run"):
                    compare_documents(base, new, bootstrap=2)

    def test_comparison_subtracts_reference_quantiles(self):
        baseline = run([5, 6, 7], references=[1, 3, 5], statistic="p95")
        new = run([6, 7, 8], references=[1, 3, 5], statistic="p95")
        result = compare_documents(baseline, new, bootstrap=30)[0]
        self.assertAlmostEqual(result["baseline"], 2.1)
        self.assertAlmostEqual(result["new"], 3.1)
        self.assertAlmostEqual(result["absolute_delta"], 1.0)

    def test_comparison_resamples_reference_batches_with_gateway_batches(self):
        base = run([1, 1, 9, 9], references=[0, 0, 8, 8],
                   reference_blocks=[[0, 0], [8, 8]])
        new = run([2, 2, 10, 10], references=[0, 0, 8, 8],
                  reference_blocks=[[0, 0], [8, 8]])
        for document in (base, new):
            document["metrics"][0]["sample_blocks"] = [[1, 1], [9, 9]] if document is base else [[2, 2], [10, 10]]
        result = compare_documents(base, new, bootstrap=100)[0]
        self.assertEqual(result["delta_ci95"], [1, 1])

    def test_comparison_rejects_references_on_only_one_condition(self):
        with self.assertRaisesRegex(ValueError, "both conditions"):
            compare_documents(run([1], references=[0]), run([1]))

    def test_provenance_is_required_but_started_at_may_differ(self):
        baseline = run([1], started_at="2026-01-01T00:00:00Z")
        new = run([2], started_at="2026-01-02T00:00:00Z")
        self.assertEqual(len(compare_documents(baseline, new, bootstrap=20)), 1)
        del baseline["provenance"]["cpu_model"]
        with self.assertRaisesRegex(ValueError, "provenance"):
            compare_documents(baseline, new, bootstrap=20)

    def test_report_refuses_no_input_reports(self):
        with self.assertRaisesRegex(ValueError, "^at least one input report is required$"):
            aggregate_documents([])

    def test_report_rejects_same_run_across_standalone_and_enveloped_inputs(self):
        first = run([1, 2])
        reordered = dict(reversed(list(first.items())))
        with self.assertRaisesRegex(
                ValueError,
                r"^input 2 run 2: duplicate run \(already included as input 1 run 1\)$"):
            aggregate_documents([first, {"runs": [run([3, 4]), reordered]}])

    def test_report_empirical_noise_includes_maximum_run_delta(self):
        runs = [run([1, 1], references=[0, 0]),
                run([5, 5], references=[0, 0], started_at="2026-01-02T00:00:00Z")]
        row = aggregate_documents([{"runs": runs}])["baseline"][0]
        self.assertEqual(row["empirical_noise_max_delta"], 4)
        self.assertGreaterEqual(row["noise_floor_delta"], 4)
        self.assertLessEqual(row["aa_bootstrap_noise_floor_delta"], row["noise_floor_delta"])

    def test_report_table_preserves_single_available_overhead_percentile(self):
        from bench.report import _table

        for statistic, expected in (("p50", "4.250 / —"), ("p95", "— / 4.250")):
            with self.subTest(statistic=statistic):
                document = run([4.25], statistic=statistic)
                document["metrics"][0]["metric"] = "overhead"
                rows = aggregate_documents([document])["baseline"]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    _table(rows)
                self.assertIn(expected, output.getvalue())

    def test_reps_may_differ_but_protocol_may_not(self):
        old, new = run([1, 2, 3], 3), run([1, 2, 3, 4], 4)
        self.assertEqual(len(compare_documents(old, new, bootstrap=20)), 1)
        new["metadata"]["protocol_version"] = "v2"
        with self.assertRaisesRegex(ValueError, "protocol_version"):
            compare_documents(old, new)

    def test_profile_and_failed_correctness_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "profiled"):
            compare_documents(run([1], profile=True), run([1]))
        with self.assertRaisesRegex(ValueError, "correctness"):
            compare_documents(run([1], passed=False), run([1]))

    def test_missing_metric_is_error_and_cli_exits_nonzero(self):
        old, new = run([1]), run([1])
        new["metrics"] = []
        with self.assertRaisesRegex(ValueError, "missing metrics"):
            compare_documents(old, new)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main([]), 2)

    def test_comparison_rejects_empty_metrics_on_both_sides(self):
        baseline, candidate = run([1]), run([1])
        baseline["metrics"] = []
        candidate["metrics"] = []
        for aggregate in (False, True):
            with self.subTest(aggregate=aggregate):
                left = {"runs": [baseline]} if aggregate else baseline
                right = {"runs": [candidate]} if aggregate else candidate
                with self.assertRaisesRegex(ValueError, "baseline.*missing metrics"):
                    compare_documents(left, right)

    def test_comparison_rejects_undeclared_metric_mix_before_split(self):
        for side in ("baseline", "new"):
            for aggregate in (False, True):
                with self.subTest(side=side, aggregate=aggregate):
                    baseline, candidate = run([1]), run([1])
                    for document in (baseline, candidate):
                        document["metadata"]["mix_hashes"]["test"] = "heldout"
                        document["metrics"].append({
                            **document["metrics"][0], "mix": "test",
                        })
                    bad = baseline if side == "baseline" else candidate
                    bad["metrics"].append({**bad["metrics"][0], "mix": "basci"})
                    left = {"runs": [baseline]} if aggregate else baseline
                    right = {"runs": [candidate]} if aggregate else candidate
                    with self.assertRaisesRegex(ValueError, side + ".*undeclared mix.*basci"):
                        compare_documents(left, right)

    def test_comparison_rejects_declared_mix_without_metrics(self):
        for side in ("baseline", "new", "both"):
            with self.subTest(side=side):
                baseline, candidate = run([1]), run([1])
                for label, document in (("baseline", baseline), ("new", candidate)):
                    document["metadata"]["mix_hashes"]["test"] = "heldout"
                    if side not in (label, "both"):
                        document["metrics"].append({
                            **document["metrics"][0], "mix": "test",
                        })
                label = "baseline" if side == "both" else side
                with self.assertRaisesRegex(ValueError, label + ".*missing metrics.*test"):
                    compare_documents(baseline, candidate)

    def test_invalid_comparison_cli_has_no_header_and_exits_nonzero(self):
        baseline, candidate = run([1]), run([1])
        baseline["metrics"] = []
        candidate["metrics"] = []
        output, errors = io.StringIO(), io.StringIO()
        with patch("bench.compare._load", side_effect=[baseline, candidate]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(main(["baseline.json", "new.json"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("baseline: missing metrics", errors.getvalue())

    def test_malformed_statistic_cli_rejects_without_traceback_or_header(self):
        for statistic in ([], {}):
            with self.subTest(statistic=statistic):
                baseline = run([1], statistic=statistic)
                output, errors = io.StringIO(), io.StringIO()
                with patch("bench.compare._load", side_effect=[baseline, run([1])]):
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        self.assertEqual(main(["baseline.json", "new.json"]), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("unsupported statistic", errors.getvalue())

    def test_malformed_metric_identity_reports_invalid_input(self):
        for field, value in (("case", []), ("unit", {}), ("concurrency", True)):
            with self.subTest(field=field):
                broken = run([2])
                broken["metrics"][0][field] = value
                with self.assertRaisesRegex(ValueError, f"metric identity {field}"):
                    compare_documents(run([1]), broken)
                with self.assertRaisesRegex(ValueError, f"metric identity {field}"):
                    aggregate_documents([broken])

    def test_duplicate_metric_rejected_inside_aggregate_run(self):
        duplicate = run([1])
        duplicate["metrics"].append(dict(duplicate["metrics"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate metric"):
            compare_documents({"runs": [duplicate]}, {"runs": [run([1])]})

    def test_missing_metric_in_individual_aggregate_run_rejected(self):
        with self.assertRaisesRegex(ValueError, "run 2: missing metrics"):
            compare_documents(
                {"runs": [run([1]), {**run([2]), "metrics": []}]},
                {"runs": [run([1]), run([2])]})

    def test_metadata_settings_except_reps_must_match(self):
        baseline, new = run([1]), run([1])
        new["metadata"]["cpu_affinity"] = [1]
        with self.assertRaisesRegex(ValueError, "cpu_affinity"):
            compare_documents(baseline, new)

    def test_sample_blocks_must_flatten_to_samples(self):
        baseline, new = run([1, 2]), run([1, 2])
        baseline["metrics"][0]["sample_blocks"] = [[1], [3]]
        with self.assertRaisesRegex(ValueError, "do not match samples"):
            compare_documents(baseline, new)

    def test_reference_blocks_require_source_samples(self):
        baseline = run([1, 2], reference_blocks=[[0, 1]])
        candidate = run([1, 2], references=[0, 1])
        with self.assertRaisesRegex(ValueError, "reference_blocks require reference_samples"):
            compare_documents(baseline, candidate)

    def test_reference_blocks_must_flatten_to_reference_samples(self):
        baseline = run([1, 2], references=[0, 1], reference_blocks=[[0, 2]])
        candidate = run([1, 2], references=[0, 1])
        with self.assertRaisesRegex(ValueError, "reference_blocks do not match reference_samples"):
            compare_documents(baseline, candidate)

    def test_reference_blocks_must_match_gateway_batch_boundaries(self):
        baseline = run([3, 4, 5], references=[0, 1, 2], reference_blocks=[[0], [1, 2]])
        baseline["metrics"][0]["sample_blocks"] = [[3, 4], [5]]
        candidate = run([3, 4, 5], references=[0, 1, 2])
        with self.assertRaisesRegex(ValueError, "reference blocks must pair with gateway batches"):
            compare_documents(baseline, candidate)

    def test_compare_resamples_whole_correlated_batches(self):
        baseline, new = run([1, 1, 9, 9]), run([2, 2, 10, 10])
        for document in (baseline, new):
            document["metrics"][0]["sample_blocks"] = [[document["metrics"][0]["samples"][0]] * 2,
                                                         [document["metrics"][0]["samples"][2]] * 2]
        result = compare_documents(baseline, new, bootstrap=100)[0]
        self.assertEqual(result["absolute_delta"], 1)
        self.assertEqual(result["batch_blocks"], {"baseline": 2, "new": 2})

    def test_combined_train_test_envelope(self):
        train, heldout = run([1, 2]), run([3, 4])
        train["metadata"]["mix_hashes"] = {"train": "training"}
        train["metrics"][0]["mix"] = "train"
        heldout["metadata"]["mix_hashes"] = {"test": "heldout"}
        heldout["metrics"][0]["mix"] = "test"
        later = "2026-01-02T00:00:00Z"
        train_later = {**train, "provenance": {**train["provenance"], "started_at": later}}
        heldout_later = {**heldout, "provenance": {**heldout["provenance"], "started_at": later}}
        document = {"runs": [train, heldout, train_later, heldout_later]}
        rows = compare_documents(document, document, bootstrap=20)
        self.assertEqual({r["mix"] for r in rows}, {"train", "test"})
        self.assertTrue(all(r["inside_noise"] for r in rows))

    def test_source_changes_between_conditions_but_not_within_one(self):
        baseline, changed = run([1]), run([1])
        changed["provenance"]["source_sha256"] = "candidate"
        self.assertEqual(compare_documents(baseline, changed, bootstrap=20)[0]["absolute_delta"], 0)
        with self.assertRaisesRegex(ValueError, "different production sources"):
            compare_documents({"runs": [baseline, changed]}, baseline)

    def test_comparison_cli_names_missing_candidate_and_keeps_stdout_empty(self):
        import json

        output, errors = io.StringIO(), io.StringIO()
        baseline_file = io.StringIO(json.dumps(run([1])))
        with patch("bench.compare.Path.open", side_effect=[baseline_file, FileNotFoundError("missing receipt")]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(main(["baseline.json", "missing-candidate.json"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("cannot read missing-candidate.json:", errors.getvalue())

    def test_report_cli_does_not_print_success_table_when_output_write_fails(self):
        from bench.report import main as report_main

        output, errors = io.StringIO(), io.StringIO()
        with patch("bench.report._load", return_value=run([1])):
            with patch("bench.report.Path.write_text", side_effect=PermissionError("synthetic write denied")):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(report_main(["run.json", "--json", "output.json"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("synthetic write denied", errors.getvalue())

    def test_combined_envelope_still_rejects_changed_mix(self):
        train, heldout = run([1]), run([2])
        heldout["metadata"]["mix_hashes"] = {"test": "heldout"}
        heldout["metrics"][0]["mix"] = "test"
        changed = {**heldout, "metadata": {**heldout["metadata"], "mix_hashes": {"test": "changed"}}}
        with self.assertRaisesRegex(ValueError, "mix_hashes"):
            compare_documents({"runs": [train, heldout]}, {"runs": [train, changed]})

    def test_reference_adjusted_zero_baseline_does_not_claim_a_percent_delta(self):
        baseline = run([5], references=[5])
        candidate = run([7], references=[5])
        row = compare_documents(baseline, candidate, bootstrap=10)[0]
        self.assertEqual(row["baseline"], 0)
        self.assertEqual(row["absolute_delta"], 2)
        self.assertIsNone(row["percent_delta"])

    def test_negative_delta_interval_excludes_zero(self):
        row = compare_documents(run([5]), run([3]), bootstrap=10)[0]
        self.assertEqual(row["delta_ci95"], [-2, -2])
        self.assertFalse(row["inside_noise"])


    def test_report_pools_only_runs_that_declare_each_mix(self):
        train = run([2, 2])
        train["metadata"]["mix_hashes"] = {"train": "training"}
        train["metrics"][0]["mix"] = "train"
        mixed = run([6, 6], started_at="2026-01-02T00:00:00Z")
        mixed["metadata"]["mix_hashes"] = {"train": "training", "test": "heldout"}
        mixed["metrics"][0]["mix"] = "train"
        mixed["metrics"].append({**mixed["metrics"][0], "mix": "test",
                                 "samples": [40, 40]})

        report = aggregate_documents([train, {"runs": [mixed]}])
        self.assertEqual(report["runs"], [train, mixed])
        self.assertEqual(
            [(row["mix"], row["value"], row["n"], row["run_count"])
             for row in report["baseline"]],
            [("test", 40, 2, 1), ("train", 4, 4, 2)])

    def test_report_refuses_conflicting_hash_for_shared_mix(self):
        first, second = run([1]), run([2])
        second["metadata"]["mix_hashes"]["basic"] = "different"
        with self.assertRaisesRegex(ValueError, "incompatible mix hash for basic"):
            aggregate_documents([first, {"runs": [second]}])

    def test_report_bootstrap_keeps_paired_reference_batches_together(self):
        document = run([1, 1, 9, 9], references=[0, 0, 8, 8],
                       reference_blocks=[[0, 0], [8, 8]])
        document["metrics"][0]["sample_blocks"] = [[1, 1], [9, 9]]
        row = aggregate_documents([document])["baseline"][0]
        self.assertEqual(row["value"], 1)
        self.assertEqual(row["ci95"], [1, 1])
        self.assertEqual(row["aa_delta_ci95"], [0, 0])
        self.assertEqual(row["noise_floor_delta"], 0)


    def test_report_bad_input_preserves_existing_output_and_prints_no_table(self):
        import tempfile
        from pathlib import Path

        from bench.report import main as report_main

        for case in ("missing", "malformed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as workspace:
                source = Path(workspace) / "input.json"
                destination = Path(workspace) / "baseline.json"
                original = b"previous result\n"
                destination.write_bytes(original)
                if case == "malformed":
                    source.write_text("{", encoding="utf-8")
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(report_main([str(source), "--json", str(destination)]), 1)
                self.assertIn("cannot read", errors.getvalue())
                self.assertIn("input.json", errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(destination.read_bytes(), original)

    def test_report_output_failure_does_not_print_a_success_table(self):
        import json
        import tempfile
        from pathlib import Path

        from bench.report import main as report_main

        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "input.json"
            source.write_text(json.dumps(run([1, 2])), encoding="utf-8")
            destination = Path(workspace) / "missing-directory" / "baseline.json"
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(report_main([str(source), "--json", str(destination)]), 1)
            self.assertTrue(errors.getvalue().startswith("error:"))
            self.assertIn("baseline.json", errors.getvalue())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(destination.exists())


    def test_aggregate_tail_coverage_is_per_run_not_pooled(self):
        for statistic, minimum in (("p95", 100), ("p99", 1000)):
            with self.subTest(statistic=statistic):
                enough = run([4] * minimum, statistic=statistic)
                short = run([2] * (minimum - 1), statistic=statistic)
                row = aggregate_documents([short, enough])["baseline"][0]
                self.assertEqual(row["n"], 2 * minimum - 1)
                self.assertEqual(row["run_count"], 2)
                self.assertTrue(row["undersampled_tails"])
                second = run([4] * minimum, statistic=statistic, started_at="2026-01-02T00:00:00Z")
                full = aggregate_documents([enough, second])["baseline"][0]
                self.assertFalse(full["undersampled_tails"])

    def test_comparison_tail_warning_checks_both_conditions(self):
        full = run([4] * 100, statistic="p95")
        short = run([6] * 99, statistic="p95")
        for baseline, candidate in ((full, short), (short, full)):
            with self.subTest(baseline_reps=baseline["metadata"]["reps"]):
                row = compare_documents(baseline, candidate, bootstrap=20)[0]
                self.assertEqual(row["sample_count"], 199)
                self.assertEqual(row["run_blocks"], {"baseline": 1, "new": 1})
                self.assertTrue(row["flags"]["ci_tails_undersampled"])
        row = compare_documents(full, full, bootstrap=20)[0]
        self.assertFalse(row["flags"]["ci_tails_undersampled"])

class BootstrapContractTests(unittest.TestCase):
    def test_summary_uses_private_seed_without_advancing_callers_rng(self):
        state = random.getstate()
        try:
            random.seed(931)
            expected = random.random()
            random.seed(931)
            summarize([1, 2, 9], seed=12, bootstrap=8)
            summarize_blocks([[1, 2], [9]], seed=12, bootstrap=8)
            self.assertEqual(random.random(), expected)
        finally:
            random.setstate(state)


if __name__ == "__main__":
    unittest.main()
