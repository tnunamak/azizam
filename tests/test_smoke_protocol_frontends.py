"""Unit tests for ``scripts/compat/smoke_protocol_frontends.py``.

No network: the HTTP layer is swapped for a fake. The goal is to lock
in the skip/pass/fail decision tree so future protocol additions don't
silently regress (e.g. an Anthropic-only LLM probe accidentally firing
without ``OPENAI_PROXY_SMOKE_LIVE_LLM=1``).
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.compat import smoke_protocol_frontends as smoke


def _fake_request(responses: Dict[Tuple[str, str], Tuple[int, str]]):
    """Return a stand-in for ``smoke._request`` driven by a path/method map.

    ``responses`` keys are ``(method, path)``; values are
    ``(status_code, err_text)`` returned verbatim.
    """

    calls: List[dict] = []

    def fake(
        *,
        base_url: str,
        path: str,
        method: str = "GET",
        headers=None,
        body=None,
        timeout: int = smoke.DEFAULT_TIMEOUT_S,
    ) -> Tuple[int, str]:
        calls.append(
            {
                "base_url": base_url,
                "path": path,
                "method": method,
                "headers": dict(headers or {}),
                "body": body,
                "timeout": timeout,
            }
        )
        return responses.get((method, path), (599, "no fake configured"))

    return fake, calls


class TestSmokeConfigFromEnv(unittest.TestCase):
    def test_defaults_when_env_empty(self) -> None:
        cfg = smoke.SmokeConfig.from_env(env={})
        self.assertIsNone(cfg.base_url)
        self.assertIsNone(cfg.api_key)
        self.assertFalse(cfg.live_llm)
        self.assertFalse(cfg.live_embeddings)
        self.assertEqual(cfg.timeout_s, smoke.DEFAULT_TIMEOUT_S)
        self.assertEqual(cfg.anthropic_model, smoke.DEFAULT_ANTHROPIC_MODEL)
        self.assertEqual(cfg.ollama_model, smoke.DEFAULT_OLLAMA_MODEL)
        self.assertEqual(cfg.embeddings_model, smoke.DEFAULT_EMBEDDINGS_MODEL)

    def test_strips_trailing_slash_from_base_url(self) -> None:
        cfg = smoke.SmokeConfig.from_env(
            env={"OPENAI_PROXY_BASE_URL": "http://127.0.0.1:5000/"}
        )
        self.assertEqual(cfg.base_url, "http://127.0.0.1:5000")

    def test_live_llm_requires_literal_one(self) -> None:
        # Only "1" enables live calls — common truthy strings ("true",
        # "yes") should NOT enable them, so an operator can't accidentally
        # spend on a real backend by setting the wrong value.
        for value in ("", "0", "true", "yes", "on"):
            cfg = smoke.SmokeConfig.from_env(
                env={"OPENAI_PROXY_SMOKE_LIVE_LLM": value}
            )
            self.assertFalse(cfg.live_llm, msg=f"unexpected truthy for {value!r}")
        cfg = smoke.SmokeConfig.from_env(env={"OPENAI_PROXY_SMOKE_LIVE_LLM": "1"})
        self.assertTrue(cfg.live_llm)

    def test_live_embeddings_requires_literal_one(self) -> None:
        for value in ("", "0", "true", "yes", "on"):
            cfg = smoke.SmokeConfig.from_env(
                env={"OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS": value}
            )
            self.assertFalse(
                cfg.live_embeddings,
                msg=f"unexpected truthy for {value!r}",
            )
        cfg = smoke.SmokeConfig.from_env(
            env={"OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS": "1"}
        )
        self.assertTrue(cfg.live_embeddings)

    def test_timeout_falls_back_on_bad_value(self) -> None:
        cfg = smoke.SmokeConfig.from_env(
            env={"OPENAI_PROXY_SMOKE_TIMEOUT": "not-a-number"}
        )
        self.assertEqual(cfg.timeout_s, smoke.DEFAULT_TIMEOUT_S)

    def test_timeout_clamped_to_min_one(self) -> None:
        cfg = smoke.SmokeConfig.from_env(env={"OPENAI_PROXY_SMOKE_TIMEOUT": "0"})
        self.assertEqual(cfg.timeout_s, 1)
        cfg = smoke.SmokeConfig.from_env(env={"OPENAI_PROXY_SMOKE_TIMEOUT": "-5"})
        self.assertEqual(cfg.timeout_s, 1)

    def test_model_overrides(self) -> None:
        cfg = smoke.SmokeConfig.from_env(
            env={
                "OPENAI_PROXY_SMOKE_ANTHROPIC_MODEL": "claude-x",
                "OPENAI_PROXY_SMOKE_OLLAMA_MODEL": "tinyllama",
                "OPENAI_PROXY_SMOKE_EMBEDDINGS_MODEL": "nomic-x",
            }
        )
        self.assertEqual(cfg.anthropic_model, "claude-x")
        self.assertEqual(cfg.ollama_model, "tinyllama")
        self.assertEqual(cfg.embeddings_model, "nomic-x")


class TestSkipSemantics(unittest.TestCase):
    """Each check must skip cleanly when its preconditions are not met."""

    def test_all_checks_skip_when_no_base_url(self) -> None:
        cfg = smoke.SmokeConfig(
            base_url=None,
            api_key="opk_test",
            live_llm=True,
            live_embeddings=True,
            timeout_s=5,
            anthropic_model="m",
            ollama_model="m",
            embeddings_model="e",
        )
        with mock.patch.object(smoke, "_request") as patched:
            results = smoke.run(cfg)
            patched.assert_not_called()
        for r in results:
            self.assertEqual(r.status, "skip", msg=r.format_row())
            self.assertIn("OPENAI_PROXY_BASE_URL", r.detail)

    def test_capabilities_and_chat_skip_without_api_key(self) -> None:
        cfg = smoke.SmokeConfig(
            base_url="http://gw",
            api_key=None,
            live_llm=True,
            live_embeddings=True,
            timeout_s=5,
            anthropic_model="m",
            ollama_model="m",
            embeddings_model="e",
        )
        fake, calls = _fake_request({("GET", "/health"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            results = {r.name: r for r in smoke.run(cfg)}
        # /health does not require an api key
        self.assertEqual(results["gateway.health"].status, "pass")
        # Every other check should skip with an api-key reason
        for name in (
            "gateway.capabilities",
            "anthropic.messages.nonstream",
            "openai.embeddings",
            "ollama.tags",
            "ollama.chat.nonstream",
            "ollama.embed",
            "ollama.embeddings",
        ):
            self.assertEqual(results[name].status, "skip", msg=name)
            self.assertIn("OPENAI_PROXY_API_KEY", results[name].detail)
        # Only /health should have been requested.
        self.assertEqual([c["path"] for c in calls], ["/health"])

    def test_llm_probes_skip_without_live_flag(self) -> None:
        cfg = smoke.SmokeConfig(
            base_url="http://gw",
            api_key="opk_test",
            live_llm=False,
            live_embeddings=False,
            timeout_s=5,
            anthropic_model="m",
            ollama_model="m",
            embeddings_model="e",
        )
        fake, calls = _fake_request(
            {
                ("GET", "/health"): (200, ""),
                ("GET", "/gateway/v1/capabilities"): (200, ""),
                ("GET", "/api/tags"): (200, ""),
            }
        )
        with mock.patch.object(smoke, "_request", fake):
            results = {r.name: r for r in smoke.run(cfg)}
        self.assertEqual(results["anthropic.messages.nonstream"].status, "skip")
        self.assertEqual(results["ollama.chat.nonstream"].status, "skip")
        self.assertEqual(results["openai.embeddings"].status, "skip")
        self.assertEqual(results["ollama.embed"].status, "skip")
        self.assertEqual(results["ollama.embeddings"].status, "skip")
        self.assertIn(
            "OPENAI_PROXY_SMOKE_LIVE_LLM",
            results["anthropic.messages.nonstream"].detail,
        )
        self.assertIn(
            "OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS",
            results["openai.embeddings"].detail,
        )
        # Non-LLM probes still ran:
        self.assertEqual(results["gateway.health"].status, "pass")
        self.assertEqual(results["gateway.capabilities"].status, "pass")
        self.assertEqual(results["ollama.tags"].status, "pass")
        # No POST should have escaped to the fake (which means: no LLM
        # backend call was attempted).
        self.assertFalse(any(c["method"] == "POST" for c in calls))


class TestRouteWiring(unittest.TestCase):
    """When preconditions hold, each probe must hit the right route + headers."""

    def _cfg(
        self,
        *,
        live_llm: bool = True,
        live_embeddings: bool = True,
    ) -> smoke.SmokeConfig:
        return smoke.SmokeConfig(
            base_url="http://gw",
            api_key="opk_test",
            live_llm=live_llm,
            live_embeddings=live_embeddings,
            timeout_s=5,
            anthropic_model="claude-x",
            ollama_model="ollama-x",
            embeddings_model="nomic-x",
        )

    def test_capabilities_uses_bearer(self) -> None:
        fake, calls = _fake_request(
            {("GET", "/gateway/v1/capabilities"): (200, "")}
        )
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_capabilities(self._cfg())
        self.assertEqual(result.status, "pass")
        self.assertEqual(calls[0]["path"], "/gateway/v1/capabilities")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer opk_test")

    def test_anthropic_messages_sends_required_headers(self) -> None:
        fake, calls = _fake_request({("POST", "/v1/messages"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_anthropic_messages(self._cfg())
        self.assertEqual(result.status, "pass")
        call = calls[0]
        self.assertEqual(call["path"], "/v1/messages")
        self.assertEqual(call["method"], "POST")
        # Anthropic SDKs read either header; the smoke sends both so the
        # gateway can authenticate either way it's wired.
        self.assertEqual(call["headers"]["x-api-key"], "opk_test")
        self.assertEqual(call["headers"]["Authorization"], "Bearer opk_test")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertIn(b'"model": "claude-x"', call["body"])
        # The smoke prompt is a fixed, non-secret string.
        self.assertIn(smoke.TINY_PROMPT.encode(), call["body"])

    def test_ollama_tags_route(self) -> None:
        fake, calls = _fake_request({("GET", "/api/tags"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_ollama_tags(self._cfg())
        self.assertEqual(result.status, "pass")
        self.assertEqual(calls[0]["path"], "/api/tags")
        self.assertEqual(calls[0]["method"], "GET")

    def test_ollama_chat_route_and_body(self) -> None:
        fake, calls = _fake_request({("POST", "/api/chat"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_ollama_chat(self._cfg())
        self.assertEqual(result.status, "pass")
        call = calls[0]
        self.assertEqual(call["path"], "/api/chat")
        self.assertEqual(call["method"], "POST")
        self.assertIn(b'"stream": false', call["body"])
        self.assertIn(b'"model": "ollama-x"', call["body"])

    def test_openai_embeddings_route_and_body(self) -> None:
        fake, calls = _fake_request({("POST", "/v1/embeddings"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_openai_embeddings(self._cfg())
        self.assertEqual(result.status, "pass")
        call = calls[0]
        self.assertEqual(call["path"], "/v1/embeddings")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Authorization"], "Bearer opk_test")
        self.assertIn(b'"model": "nomic-x"', call["body"])
        self.assertIn(b'"input": "ping"', call["body"])

    def test_ollama_embed_route_and_body(self) -> None:
        fake, calls = _fake_request({("POST", "/api/embed"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_ollama_embed(self._cfg())
        self.assertEqual(result.status, "pass")
        call = calls[0]
        self.assertEqual(call["path"], "/api/embed")
        self.assertEqual(call["method"], "POST")
        self.assertIn(b'"model": "nomic-x"', call["body"])
        self.assertIn(b'"input": "ping"', call["body"])

    def test_ollama_embeddings_route_and_body(self) -> None:
        fake, calls = _fake_request({("POST", "/api/embeddings"): (200, "")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_ollama_embeddings(self._cfg())
        self.assertEqual(result.status, "pass")
        call = calls[0]
        self.assertEqual(call["path"], "/api/embeddings")
        self.assertEqual(call["method"], "POST")
        self.assertIn(b'"model": "nomic-x"', call["body"])
        self.assertIn(b'"prompt": "ping"', call["body"])


class TestFailureReporting(unittest.TestCase):
    def test_non_200_reports_fail_with_status(self) -> None:
        cfg = smoke.SmokeConfig(
            base_url="http://gw",
            api_key=None,
            live_llm=False,
            live_embeddings=False,
            timeout_s=5,
            anthropic_model="m",
            ollama_model="m",
            embeddings_model="e",
        )
        fake, _ = _fake_request({("GET", "/health"): (503, "Service Unavailable")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_health(cfg)
        self.assertEqual(result.status, "fail")
        self.assertIn("status=503", result.detail)

    def test_connection_error_reports_fail(self) -> None:
        cfg = smoke.SmokeConfig(
            base_url="http://gw",
            api_key=None,
            live_llm=False,
            live_embeddings=False,
            timeout_s=5,
            anthropic_model="m",
            ollama_model="m",
            embeddings_model="e",
        )
        fake, _ = _fake_request({("GET", "/health"): (0, "url_error: 'refused'")})
        with mock.patch.object(smoke, "_request", fake):
            result = smoke.check_health(cfg)
        self.assertEqual(result.status, "fail")
        self.assertIn("status=0", result.detail)


class TestSummaryAndExitCode(unittest.TestCase):
    def test_summarize_counts(self) -> None:
        results = [
            smoke.CheckResult("a", "pass"),
            smoke.CheckResult("b", "pass"),
            smoke.CheckResult("c", "skip", "reason=x"),
            smoke.CheckResult("d", "fail", "status=500"),
        ]
        self.assertEqual(smoke._summarize(results), (2, 1, 1))

    def test_main_exits_nonzero_on_failure(self) -> None:
        # Drive main() with a fake _request that fails health; everything
        # else skips. Exit code must be 1.
        env_patch = {
            "OPENAI_PROXY_BASE_URL": "http://gw",
        }
        fake, _ = _fake_request({("GET", "/health"): (500, "")})
        buf = io.StringIO()
        with mock.patch.dict("os.environ", env_patch, clear=True), \
                mock.patch.object(smoke, "_request", fake), \
                redirect_stdout(buf):
            exit_code = smoke.main([])
        self.assertEqual(exit_code, 1)
        out = buf.getvalue()
        self.assertIn("[FAIL] gateway.health", out)

    def test_main_exits_zero_when_only_skips(self) -> None:
        # No env set => every check skips => exit 0 (skips are not failures).
        buf = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(smoke, "_request") as patched, \
                redirect_stdout(buf):
            exit_code = smoke.main([])
            patched.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("[SKIP] gateway.health", buf.getvalue())

    def test_print_config_redacts_api_key(self) -> None:
        buf = io.StringIO()
        env_patch = {
            "OPENAI_PROXY_BASE_URL": "http://gw",
            "OPENAI_PROXY_API_KEY": "opk_super_secret",
        }
        with mock.patch.dict("os.environ", env_patch, clear=True), \
                redirect_stdout(buf):
            exit_code = smoke.main(["--print-config"])
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertIn("api_key         = set", output)
        # The key itself must never appear in --print-config output.
        self.assertNotIn("opk_super_secret", output)


class TestCheckCoverage(unittest.TestCase):
    """Lock the registered probe set so future protocols don't silently drop."""

    def test_registered_check_names(self) -> None:
        cfg = smoke.SmokeConfig(
            base_url=None,
            api_key=None,
            live_llm=False,
            live_embeddings=False,
            timeout_s=5,
            anthropic_model="m",
            ollama_model="m",
            embeddings_model="e",
        )
        names = {check(cfg).name for check in smoke.CHECKS}
        self.assertEqual(
            names,
            {
                "gateway.health",
                "gateway.capabilities",
                "anthropic.messages.nonstream",
                "openai.embeddings",
                "ollama.tags",
                "ollama.chat.nonstream",
                "ollama.embed",
                "ollama.embeddings",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
