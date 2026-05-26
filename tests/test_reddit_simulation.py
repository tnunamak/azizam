from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.research import reddit_simulation  # noqa: E402


class TestRedditSimulation(unittest.TestCase):
    def test_gateway_root_url_strips_protocol_prefixes(self) -> None:
        self.assertEqual(
            reddit_simulation.gateway_root_url("https://ai.vivid.fish/openai/v1"),
            "https://ai.vivid.fish",
        )
        self.assertEqual(
            reddit_simulation.gateway_root_url("https://ai.vivid.fish/ollama/api/"),
            "https://ai.vivid.fish",
        )

    def test_summarize_json_counts_known_lists(self) -> None:
        summary = reddit_simulation.summarize_body(
            b'{"data":[{"id":"a"},{"id":"b"}],"object":"list"}',
            "application/json",
        )

        self.assertEqual(summary["json_type"], "dict")
        self.assertEqual(summary["data_count"], 2)
        self.assertIn("data", summary["keys"])

    def test_summarize_json_error_message(self) -> None:
        summary = reddit_simulation.summarize_body(
            b'{"error":{"type":"backend_error","message":"backend unavailable"}}',
            "application/json",
        )

        self.assertEqual(summary["error_type"], "backend_error")
        self.assertEqual(summary["error_message"], "backend unavailable")

    def test_summarize_sse_counts_done_and_content_events(self) -> None:
        raw = (
            'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()

        summary = reddit_simulation.summarize_body(raw, "text/event-stream")

        self.assertEqual(summary["sse_event_count"], 3)
        self.assertEqual(summary["sse_json_count"], 2)
        self.assertEqual(summary["sse_content_events"], 1)
        self.assertTrue(summary["sse_done"])
        self.assertIn("SSE events: 3", reddit_simulation._summary_sentence(summary))

    def test_corpus_summary_counts_unique_thread_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "threads").mkdir()
            (root / "reports").mkdir()
            (root / "threads" / "localllama-abc-one.md").write_text("one")
            (root / "threads" / "localllama-abc-two.md").write_text("two")
            (root / "threads" / "selfhosted-def-three.md").write_text("three")
            (root / "reports" / "report.md").write_text("report")

            summary = reddit_simulation.corpus_summary(root)

        self.assertEqual(summary["thread_markdown_count"], 3)
        self.assertEqual(summary["unique_thread_count"], 2)
        self.assertEqual(summary["reports"], ["report.md"])

    def test_render_trials_marks_missing_keys_as_not_tested(self) -> None:
        personas = [
            {
                "id": "p",
                "label": "Persona",
                "archetype": "Tester",
                "borrowed_environment": "Borrowed stack",
                "goals": ["Check auth"],
                "comment_angles": ["Missing key is useful signal."],
            }
        ]
        results = [
            {
                "persona_id": "p",
                "probe_id": "models",
                "method": "GET",
                "path": "/openai/v1/models",
                "auth": "runtime",
                "status": None,
                "elapsed_ms": None,
                "ok": False,
                "skipped": True,
                "reason": "missing runtime key",
                "summary": {},
            }
        ]
        context = {
            "base_url": "https://ai.vivid.fish",
            "runtime_key_env": None,
            "admin_key_env": None,
            "corpus": {"unique_thread_count": 1},
        }

        markdown = reddit_simulation.render_trials(personas, results, context)

        self.assertIn("Action: `GET /openai/v1/models`", markdown)
        self.assertIn("Not tested: missing runtime key.", markdown)

    def test_group_results_keeps_persona_probe_order(self) -> None:
        results = [
            {"persona_id": "a", "probe_id": "one"},
            {"persona_id": "b", "probe_id": "two"},
            {"persona_id": "a", "probe_id": "three"},
        ]

        grouped = reddit_simulation._group_results(results)

        self.assertEqual(
            [item["probe_id"] for item in grouped["a"]],
            ["one", "three"],
        )
        self.assertEqual(grouped["b"][0]["probe_id"], "two")


if __name__ == "__main__":
    unittest.main()
