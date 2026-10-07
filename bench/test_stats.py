import contextlib
import io
import unittest

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
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([0, 10], 25), 2.5)

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

    def test_block_summary_resamples_batches_and_reports_batch_count(self):
        summary = summarize_blocks([[1, 1], [9, 9]], seed=2, bootstrap=100)
        self.assertEqual(summary["value"], 5)
        self.assertEqual(summary["batch_count"], 2)
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["ci95"], [1, 9])

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

    def test_block_summary_rejects_unmatched_reference_batches(self):
        with self.assertRaisesRegex(ValueError, "batch lengths"):
            summarize_blocks([[1, 2]], reference_blocks=[[1]])

    def test_comparison_uses_hierarchical_run_blocks(self):
        base = {"runs": [run([10, 11, 12]), run([11, 12, 13]), run([12, 13, 14])]}
        new = {"runs": [run([12, 13, 14]), run([13, 14, 15]), run([14, 15, 16])]}
        result = compare_documents(base, new, bootstrap=100)[0]
        self.assertEqual(result["absolute_delta"], 2)
        self.assertEqual(result["run_blocks"], {"baseline": 3, "new": 3})
        self.assertFalse(result["flags"]["fewer_than_3_run_blocks"])
        self.assertTrue(result["flags"]["fewer_than_5_run_blocks"])

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

    def test_report_empirical_noise_includes_maximum_run_delta(self):
        runs = [run([1, 1], references=[0, 0]),
                run([5, 5], references=[0, 0], started_at="2026-01-02T00:00:00Z")]
        row = aggregate_documents([{"runs": runs}])["baseline"][0]
        self.assertEqual(row["empirical_noise_max_delta"], 4)
        self.assertGreaterEqual(row["noise_floor_delta"], 4)
        self.assertLessEqual(row["aa_bootstrap_noise_floor_delta"], row["noise_floor_delta"])

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
        document = {"runs": [train, heldout, train, heldout]}
        rows = compare_documents(document, document, bootstrap=20)
        self.assertEqual({r["mix"] for r in rows}, {"train", "test"})
        self.assertTrue(all(r["inside_noise"] for r in rows))

    def test_source_changes_between_conditions_but_not_within_one(self):
        baseline, changed = run([1]), run([1])
        changed["provenance"]["source_sha256"] = "candidate"
        self.assertEqual(compare_documents(baseline, changed, bootstrap=20)[0]["absolute_delta"], 0)
        with self.assertRaisesRegex(ValueError, "different production sources"):
            compare_documents({"runs": [baseline, changed]}, baseline)

    def test_combined_envelope_still_rejects_changed_mix(self):
        train, heldout = run([1]), run([2])
        heldout["metadata"]["mix_hashes"] = {"test": "heldout"}
        heldout["metrics"][0]["mix"] = "test"
        changed = {**heldout, "metadata": {**heldout["metadata"], "mix_hashes": {"test": "changed"}}}
        with self.assertRaisesRegex(ValueError, "mix_hashes"):
            compare_documents({"runs": [train, heldout]}, {"runs": [train, changed]})


if __name__ == "__main__":
    unittest.main()
