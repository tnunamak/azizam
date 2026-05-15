"""Unit tests for the SDK compatibility harness scaffold.

These tests stay no-network and no-SDK. Optional live SDK probes live
under ``tests/compat/python/probe_*.py`` and are invoked separately.

Run via::

    /home/tnunamak/applications/openai-proxy/venv/bin/python \
        -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.compat.harness import (
    ConfigError,
    ProbeResult,
    Result,
    build_matrix,
    load_config,
    matrix_to_json,
)
from tests.compat.harness.fake_backends import (
    fake_chat_completion,
    fake_chat_completion_stream,
    fake_ollama_chat,
    fake_ollama_tags,
)
from tests.compat.harness.matrix import validate_matrix_dict
from scripts.compat import run_matrix


class TestLoadConfig(unittest.TestCase):
    def test_happy_path(self) -> None:
        cfg = load_config(
            env={
                "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080",
                "OPENAI_PROXY_API_KEY": "sk-test",
            }
        )
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8080")
        self.assertEqual(cfg.api_key, "sk-test")
        self.assertEqual(cfg.openai_base_url(), "http://127.0.0.1:8080/v1")
        self.assertEqual(cfg.anthropic_base_url(), "http://127.0.0.1:8080")
        self.assertEqual(cfg.ollama_host(), "http://127.0.0.1:8080")
        self.assertEqual(cfg.request_timeout_s, 30.0)

    def test_strips_trailing_slash(self) -> None:
        cfg = load_config(
            env={
                "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080/",
                "OPENAI_PROXY_API_KEY": "sk-test",
            }
        )
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8080")
        self.assertEqual(cfg.openai_base_url(), "http://127.0.0.1:8080/v1")

    def test_missing_base_url(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(env={"OPENAI_PROXY_API_KEY": "sk-test"})

    def test_missing_api_key(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(env={"OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080"})

    def test_allow_anonymous(self) -> None:
        cfg = load_config(
            env={"OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080"},
            allow_anonymous=True,
        )
        self.assertEqual(cfg.api_key, "")
        self.assertTrue(cfg.allow_anonymous)

    def test_rejects_protocol_suffix(self) -> None:
        # Probes append /v1 or /api themselves; a base URL with the
        # suffix already baked in is a footgun.
        with self.assertRaises(ConfigError):
            load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080/v1",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                }
            )
        with self.assertRaises(ConfigError):
            load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080/api",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                }
            )

    def test_rejects_bad_scheme(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "ftp://127.0.0.1",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                }
            )

    def test_rejects_missing_host(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "http://",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                }
            )

    def test_custom_timeout(self) -> None:
        cfg = load_config(
            env={
                "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080",
                "OPENAI_PROXY_API_KEY": "sk-test",
                "OPENAI_PROXY_TIMEOUT_S": "5",
            }
        )
        self.assertEqual(cfg.request_timeout_s, 5.0)

    def test_rejects_bad_timeout(self) -> None:
        for bad in ("abc", "-1", "0"):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    load_config(
                        env={
                            "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080",
                            "OPENAI_PROXY_API_KEY": "sk-test",
                            "OPENAI_PROXY_TIMEOUT_S": bad,
                        }
                    )


class TestMatrixBuilder(unittest.TestCase):
    def test_build_and_serialize(self) -> None:
        matrix = build_matrix(
            protocol="openai",
            sdk="openai-python",
            sdk_version="1.99.0",
            gateway_version="local",
            probes=[
                ProbeResult("chat.non_streaming", Result.PASS),
                ProbeResult("chat.streaming", Result.PARTIAL, note="missing finish_reason"),
                ProbeResult("responses.streaming", Result.UNSUPPORTED),
            ],
        )
        as_dict = matrix.to_dict()
        validate_matrix_dict(as_dict)
        self.assertEqual(as_dict["results"]["chat.non_streaming"], "pass")
        self.assertEqual(as_dict["notes"]["chat.streaming"], "missing finish_reason")

        rendered = matrix_to_json([matrix])
        roundtrip = json.loads(rendered)
        self.assertEqual(len(roundtrip), 1)
        validate_matrix_dict(roundtrip[0])

    def test_duplicate_case_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_matrix(
                protocol="openai",
                sdk="openai-python",
                sdk_version="1.0",
                gateway_version="local",
                probes=[
                    ProbeResult("chat", Result.PASS),
                    ProbeResult("chat", Result.FAIL),
                ],
            )

    def test_validate_matrix_dict_rejects_unknown_result(self) -> None:
        with self.assertRaises(ValueError):
            validate_matrix_dict(
                {
                    "protocol": "openai",
                    "sdk": "openai-python",
                    "sdk_version": "1.0",
                    "gateway_version": "local",
                    "results": {"chat": "kindabusted"},
                }
            )

    def test_validate_matrix_dict_rejects_missing_keys(self) -> None:
        with self.assertRaises(ValueError):
            validate_matrix_dict(
                {"protocol": "openai", "sdk": "openai-python", "results": {}}
            )


class TestFakeBackends(unittest.TestCase):
    def test_openai_chat_envelope(self) -> None:
        payload = fake_chat_completion(model="gpt-test", content="hi")
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "hi")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        usage = payload["usage"]
        self.assertEqual(
            usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"]
        )

    def test_openai_stream_shape(self) -> None:
        chunks = fake_chat_completion_stream(model="gpt-test", tokens=["he", "llo"])
        # role chunk + 2 content chunks + terminal chunk
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"], {"content": "he"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        # All chunks share the same id and created timestamp.
        ids = {c["id"] for c in chunks}
        timestamps = {c["created"] for c in chunks}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(timestamps), 1)
        for chunk in chunks:
            self.assertEqual(chunk["object"], "chat.completion.chunk")

    def test_ollama_chat_envelope(self) -> None:
        payload = fake_ollama_chat(model="llama3", content="hi")
        self.assertEqual(payload["message"]["content"], "hi")
        self.assertTrue(payload["done"])

    def test_ollama_tags_envelope(self) -> None:
        payload = fake_ollama_tags(models=["a", "b"])
        names = [m["name"] for m in payload["models"]]
        self.assertEqual(names, ["a", "b"])
        for m in payload["models"]:
            self.assertIn("digest", m)
            self.assertTrue(m["digest"].startswith("sha256:"))


class TestRunMatrixCLI(unittest.TestCase):
    def test_list_probes(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_matrix.main(["--list-probes"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("openai-python", output)
        self.assertIn("anthropic-python", output)
        self.assertIn("ollama-python", output)

    def test_dry_run_without_env(self) -> None:
        # Force a missing env by passing a fake loader via the module's
        # public load_config? Easier: just call the CLI under a cleared
        # env using the env-isolation pattern.
        import os

        old = {
            k: os.environ.pop(k)
            for k in (
                "OPENAI_PROXY_BASE_URL",
                "OPENAI_PROXY_API_KEY",
                "OPENAI_PROXY_TIMEOUT_S",
            )
            if k in os.environ
        }
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_matrix.main(["--dry-run"])
            self.assertEqual(code, 0)
            output = buf.getvalue()
            self.assertIn("env        = unset", output)
            self.assertIn("registered probes:", output)
        finally:
            os.environ.update(old)

    def test_collect_matrices_skips_without_env(self) -> None:
        def loader(*, allow_anonymous: bool = False):
            raise ConfigError("env not set")

        matrices = run_matrix.collect_matrices(cfg_loader=loader)
        self.assertEqual(len(matrices), len(run_matrix.PROBES))
        for m in matrices:
            for case, result in m.results.items():
                self.assertEqual(result, Result.SKIPPED, msg=case)
            validate_matrix_dict(m.to_dict())


if __name__ == "__main__":
    unittest.main()
