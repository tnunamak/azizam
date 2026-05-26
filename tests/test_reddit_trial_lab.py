from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.research import reddit_trial_lab  # noqa: E402


class TestRedditTrialLab(unittest.TestCase):
    def test_group_results_by_scenario(self) -> None:
        results = [
            {"scenario_id": "a", "step_id": "one"},
            {"scenario_id": "b", "step_id": "two"},
            {"scenario_id": "a", "step_id": "three"},
        ]

        grouped = reddit_trial_lab.group_results(results)

        self.assertEqual(
            [item["step_id"] for item in grouped["a"]],
            ["one", "three"],
        )
        self.assertEqual(grouped["b"][0]["step_id"], "two")

    def test_scenario_summaries_distinguish_optional_failures(self) -> None:
        scenarios = [{"id": "s", "label": "Scenario"}]
        results = [
            {
                "scenario_id": "s",
                "step_id": "required_ok",
                "required": True,
                "ok": True,
                "skipped": False,
            },
            {
                "scenario_id": "s",
                "step_id": "optional_gap",
                "required": False,
                "ok": False,
                "skipped": False,
            },
        ]

        summary = reddit_trial_lab.scenario_summaries(scenarios, results)[0]

        self.assertEqual(summary["status"], "passed_with_gaps")
        self.assertEqual(summary["optional_failures"], ["optional_gap"])

    def test_scenario_summaries_mark_required_skips_untested(self) -> None:
        scenarios = [{"id": "s", "label": "Scenario"}]
        results = [
            {
                "scenario_id": "s",
                "step_id": "required_skip",
                "required": True,
                "ok": False,
                "skipped": True,
            }
        ]

        summary = reddit_trial_lab.scenario_summaries(scenarios, results)[0]

        self.assertEqual(summary["status"], "untested")
        self.assertEqual(summary["required_skipped"], ["required_skip"])

    def test_execute_local_exists_reports_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "exists.txt"
            existing.write_text("ok")
            old_root = reddit_trial_lab.ROOT
            reddit_trial_lab.ROOT = root
            try:
                result = reddit_trial_lab.execute_step(
                    {
                        "id": "files",
                        "type": "local_exists",
                        "required": True,
                        "paths": ["exists.txt", "missing.txt"],
                    },
                    scenario_id="s",
                    base_url="https://ai.vivid.fish",
                    runtime_key=None,
                    admin_key=None,
                    timeout=1,
                )
            finally:
                reddit_trial_lab.ROOT = old_root

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "missing files")
        self.assertEqual(result.summary["present"], ["exists.txt"])
        self.assertEqual(result.summary["missing"], ["missing.txt"])

    def test_result_thought_includes_skipped_reason(self) -> None:
        thought = reddit_trial_lab.result_thought(
            {
                "skipped": True,
                "reason": "missing admin key",
                "summary": {},
            }
        )

        self.assertIn("missing admin key", thought)


if __name__ == "__main__":
    unittest.main()
