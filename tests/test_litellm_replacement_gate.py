from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.compat import litellm_replacement_gate as gate  # noqa: E402


class LiteLLMGateMatrixTests(unittest.TestCase):
    def test_committed_matrix_loads_with_required_gate_ids(self) -> None:
        gates = gate.load_gates()
        self.assertEqual(
            [g.id for g in gates],
            [
                "openai.chat",
                "openai.chat.streaming",
                "openai.embeddings",
                "anthropic.messages",
                "openai.responses",
                "ollama.client_caveats",
            ],
        )

    def test_duplicate_gate_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "matrix.json"
            path.write_text(
                json.dumps(
                    {
                        "gates": [
                            {
                                "id": "x",
                                "label": "X",
                                "protocol": "p",
                                "method": "GET",
                                "path": "/x",
                            },
                            {
                                "id": "x",
                                "label": "Y",
                                "protocol": "p",
                                "method": "GET",
                                "path": "/y",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                gate.load_gates(path)


class LiteLLMGateConfigTests(unittest.TestCase):
    def test_missing_env_resolves_to_skip_preconditions(self) -> None:
        cfg = gate.config_from_env({}, gates=gate.load_gates())
        self.assertIsNone(cfg.base_url)
        self.assertIsNone(cfg.api_key)
        self.assertEqual(cfg.timeout_s, gate.DEFAULT_TIMEOUT_S)
        self.assertEqual(cfg.models["LITELLM_OPENAI_CHAT_MODEL"], "gpt-4o-mini")

    def test_env_overrides_models_and_strips_base_url(self) -> None:
        cfg = gate.config_from_env(
            {
                "LITELLM_BASE_URL": "http://127.0.0.1:4000/",
                "LITELLM_API_KEY": "sk-test",
                "LITELLM_TIMEOUT_S": "5",
                "LITELLM_OPENAI_CHAT_MODEL": "local-chat",
            },
            gates=gate.load_gates(),
        )
        self.assertEqual(cfg.base_url, "http://127.0.0.1:4000")
        self.assertEqual(cfg.api_key, "sk-test")
        self.assertEqual(cfg.timeout_s, 5)
        self.assertEqual(cfg.models["LITELLM_OPENAI_CHAT_MODEL"], "local-chat")

    def test_run_gate_skips_before_network_when_unconfigured(self) -> None:
        first = gate.load_gates()[0]
        result = gate.run_gate(first, gate.Config(None, "sk-test", 1, {}))
        self.assertEqual(result.status, "skip")
        self.assertIn("LITELLM_BASE_URL", result.detail)


class LiteLLMGateEvaluationTests(unittest.TestCase):
    def test_json_path_resolution_handles_lists_and_missing_paths(self) -> None:
        data = {"choices": [{"message": {"content": "pong"}}]}
        self.assertEqual(gate.resolve_path(data, "choices.0.message.content"), "pong")
        self.assertIsNone(gate.resolve_path(data, "choices.1.message.content"))
        self.assertIsNone(gate.resolve_path(data, "choices.x.message.content"))

    def test_evaluate_json_gate_reports_missing_required_path(self) -> None:
        g = gate.load_gates()[0]
        result = gate.evaluate_http_result(
            g,
            gate.HttpResult(status=200, headers={}, body=b'{"choices":[{"message":{}}]}'),
        )
        self.assertEqual(result.status, "fail")
        self.assertIn("missing_json_path=choices.0.message.content", result.detail)

    def test_evaluate_any_path_gate_accepts_first_available_path(self) -> None:
        responses_gate = [
            g for g in gate.load_gates() if g.id == "openai.responses"
        ][0]
        result = gate.evaluate_http_result(
            responses_gate,
            gate.HttpResult(status=200, headers={}, body=b'{"output_text":"pong"}'),
        )
        self.assertEqual(result.status, "pass")

    def test_evaluate_stream_gate_requires_done_frame(self) -> None:
        stream_gate = [
            g for g in gate.load_gates() if g.id == "openai.chat.streaming"
        ][0]
        result = gate.evaluate_http_result(
            stream_gate,
            gate.HttpResult(status=200, headers={}, body=b"data: {\"x\":1}\n\n"),
        )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.detail, "stream_missing_done_frame")

    def test_failure_row_has_stable_status_expected_detail_shape(self) -> None:
        g = gate.load_gates()[0]
        result = gate.evaluate_http_result(
            g,
            gate.HttpResult(status=500, headers={}, body=b"", error="http_error: boom"),
        )
        self.assertEqual(
            result.format_row(),
            "[FAIL] openai.chat                  status=500 expected=200 detail=http_error: boom",
        )


if __name__ == "__main__":
    unittest.main()
