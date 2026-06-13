"""Unit tests for the SDK compatibility harness scaffold.

These tests stay no-network and no-SDK. Optional live SDK probes live
under ``tests/compat/python/probe_*.py`` and are invoked separately.

Run via::

    python -m unittest discover -s tests -p 'test_*.py' -v
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
    silent_wav_bytes,
)
from tests.compat.harness.fake_backends import (
    fake_chat_completion,
    fake_chat_completion_stream,
    fake_ollama_chat,
    fake_ollama_chat_stream_chunk,
    fake_ollama_embed,
    fake_ollama_embeddings,
    fake_ollama_generate,
    fake_ollama_generate_stream_chunk,
    fake_ollama_ps,
    fake_ollama_show,
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
        self.assertEqual(cfg.anthropic_base_url(), "http://127.0.0.1:8080/anthropic")
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

    def test_ollama_chat_stream_chunk_envelope(self) -> None:
        payload = fake_ollama_chat_stream_chunk(model="llama3", content="hi")
        self.assertEqual(payload["message"]["content"], "hi")
        self.assertFalse(payload["done"])

    def test_ollama_generate_envelope(self) -> None:
        payload = fake_ollama_generate(model="llama3", response="hi")
        self.assertEqual(payload["response"], "hi")
        self.assertTrue(payload["done"])

    def test_ollama_generate_stream_chunk_envelope(self) -> None:
        payload = fake_ollama_generate_stream_chunk(model="llama3", response="hi")
        self.assertEqual(payload["response"], "hi")
        self.assertFalse(payload["done"])

    def test_ollama_tags_envelope(self) -> None:
        payload = fake_ollama_tags(models=["a", "b"])
        names = [m["name"] for m in payload["models"]]
        self.assertEqual(names, ["a", "b"])
        for m in payload["models"]:
            self.assertIn("digest", m)
            self.assertTrue(m["digest"].startswith("sha256:"))

    def test_ollama_show_envelope(self) -> None:
        payload = fake_ollama_show(model="llama3")
        self.assertEqual(payload["details"]["family"], "llama")
        self.assertIn("model_info", payload)

    def test_ollama_ps_envelope(self) -> None:
        payload = fake_ollama_ps(models=["llama3"])
        self.assertEqual(payload["models"][0]["model"], "llama3")

    def test_ollama_embed_envelope(self) -> None:
        payload = fake_ollama_embed(vector=[0.1, 0.2])
        self.assertEqual(payload["embeddings"], [[0.1, 0.2]])

    def test_ollama_embeddings_envelope(self) -> None:
        payload = fake_ollama_embeddings(vector=[0.1, 0.2])
        self.assertEqual(payload["embedding"], [0.1, 0.2])


class TestSilentWavFixture(unittest.TestCase):
    def test_returns_valid_riff_wav(self) -> None:
        import wave

        data = silent_wav_bytes()
        # WAV RIFF header sanity.
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"WAVE")
        # Cheap enough to keep test runs fast (well under 100 KB).
        self.assertLess(len(data), 100_000)
        with wave.open(io.BytesIO(data), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), 16000)
            self.assertGreater(w.getnframes(), 0)

    def test_rejects_nonpositive_args(self) -> None:
        with self.assertRaises(ValueError):
            silent_wav_bytes(duration_s=0)
        with self.assertRaises(ValueError):
            silent_wav_bytes(sample_rate=0)


class TestRunMatrixCLI(unittest.TestCase):
    def test_list_probes(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_matrix.main(["--list-probes"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("raw-http", output)
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


# ---------- Probe execution tests (fake probe modules, no SDKs) ----------


def _install_fake_module(name: str, attrs: dict) -> None:
    """Register a fake module in sys.modules for runner tests."""
    import types

    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


def _make_pass_case() -> type:
    return type(
        "Probe",
        (unittest.TestCase,),
        {
            "test_a": lambda self: self.assertTrue(True),
            "test_b": lambda self: self.assertTrue(True),
        },
    )


def _make_mixed_case() -> type:
    def _fail(self):
        self.assertEqual(1, 2)

    def _error(self):
        raise RuntimeError("boom")

    def _skipped(self):
        self.skipTest("explicit skip")

    return type(
        "Probe",
        (unittest.TestCase,),
        {
            "test_pass": lambda self: self.assertEqual(1, 1),
            "test_fail": _fail,
            "test_error": _error,
            "test_skipped": _skipped,
        },
    )


class TestExecuteProbe(unittest.TestCase):
    def setUp(self) -> None:
        # Build TestCase classes dynamically so unittest discover does
        # not collect them as real tests at module load time.
        _install_fake_module(
            "tests.compat._fake_probe_pass",
            {"Probe": _make_pass_case()},
        )
        _install_fake_module(
            "tests.compat._fake_probe_mixed",
            {"Probe": _make_mixed_case()},
        )
        # Also register a fake SDK dep so the precondition check passes.
        _install_fake_module("_fake_sdk_dep", {"__version__": "9.9.9"})

    def tearDown(self) -> None:
        for name in (
            "tests.compat._fake_probe_pass",
            "tests.compat._fake_probe_mixed",
            "_fake_sdk_dep",
        ):
            sys.modules.pop(name, None)

    def test_execute_probe_pass(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_pass",
            test_class="Probe",
            sdk_dependency="_fake_sdk_dep",
            case_map=(("a", "test_a"), ("b", "test_b")),
        )
        probes = run_matrix.execute_probe(spec)
        self.assertEqual([p.case for p in probes], ["a", "b"])
        self.assertTrue(all(p.result == Result.PASS for p in probes))
        self.assertTrue(all(p.note is None for p in probes))

    def test_execute_probe_mixed(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_mixed",
            test_class="Probe",
            sdk_dependency="_fake_sdk_dep",
            case_map=(
                ("pass", "test_pass"),
                ("fail", "test_fail"),
                ("error", "test_error"),
                ("skipped", "test_skipped"),
            ),
        )
        probes = {p.case: p for p in run_matrix.execute_probe(spec)}
        self.assertEqual(probes["pass"].result, Result.PASS)
        self.assertEqual(probes["fail"].result, Result.FAIL)
        self.assertIn("AssertionError", probes["fail"].note or "")
        self.assertEqual(probes["error"].result, Result.FAIL)
        self.assertIn("RuntimeError", probes["error"].note or "")
        self.assertEqual(probes["skipped"].result, Result.SKIPPED)
        self.assertIn("explicit skip", probes["skipped"].note or "")

    def test_execute_probe_missing_method(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_pass",
            test_class="Probe",
            sdk_dependency="_fake_sdk_dep",
            case_map=(("ghost", "test_does_not_exist"),),
        )
        probes = run_matrix.execute_probe(spec)
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0].result, Result.FAIL)
        self.assertIn("not found", probes[0].note or "")

    def test_execute_probe_missing_class(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_pass",
            test_class="NoSuchClass",
            sdk_dependency="_fake_sdk_dep",
            case_map=(("a", "test_a"),),
        )
        probes = run_matrix.execute_probe(spec)
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0].result, Result.FAIL)
        self.assertIn("NoSuchClass", probes[0].note or "")

    def test_execute_probe_rejects_non_testcase_class(self) -> None:
        _install_fake_module(
            "tests.compat._fake_probe_bad_class",
            {"Probe": object},
        )
        self.addCleanup(sys.modules.pop, "tests.compat._fake_probe_bad_class", None)
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_bad_class",
            test_class="Probe",
            sdk_dependency="_fake_sdk_dep",
            case_map=(("a", "test_a"),),
        )
        probes = run_matrix.execute_probe(spec)
        self.assertEqual(probes[0].result, Result.FAIL)
        self.assertIn("unittest.TestCase", probes[0].note or "")

    def test_collect_matrices_runs_executor_when_ready(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_mixed",
            test_class="Probe",
            sdk_dependency="_fake_sdk_dep",
            case_map=(("pass", "test_pass"), ("fail", "test_fail")),
        )

        def loader(*, allow_anonymous: bool = False):
            return load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                },
                allow_anonymous=allow_anonymous,
            )

        matrices = run_matrix.collect_matrices(
            cfg_loader=loader, probe_specs=(spec,)
        )
        self.assertEqual(len(matrices), 1)
        m = matrices[0]
        self.assertEqual(m.results["pass"], Result.PASS)
        self.assertEqual(m.results["fail"], Result.FAIL)
        self.assertEqual(m.sdk_version, "9.9.9")
        validate_matrix_dict(m.to_dict())

    def test_collect_matrices_skips_when_sdk_missing(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_pass",
            test_class="Probe",
            sdk_dependency="_definitely_not_installed_xyz",
            case_map=(("a", "test_a"),),
        )

        def loader(*, allow_anonymous: bool = False):
            return load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                },
                allow_anonymous=allow_anonymous,
            )

        # Sanity: a runner that would crash if invoked — proving it's
        # never called when the SDK is absent.
        def exploding_runner(_spec):
            raise AssertionError("runner must not run when SDK is missing")

        matrices = run_matrix.collect_matrices(
            cfg_loader=loader, probe_specs=(spec,), runner=exploding_runner
        )
        self.assertEqual(matrices[0].results["a"], Result.SKIPPED)
        self.assertIn("not installed", matrices[0].notes["a"])

    def test_collect_matrices_recovers_from_runner_crash(self) -> None:
        spec = run_matrix.ProbeSpec(
            protocol="fake",
            sdk="fake-sdk",
            module="tests.compat._fake_probe_pass",
            test_class="Probe",
            sdk_dependency="_fake_sdk_dep",
            case_map=(("a", "test_a"),),
        )

        def loader(*, allow_anonymous: bool = False):
            return load_config(
                env={
                    "OPENAI_PROXY_BASE_URL": "http://127.0.0.1:8080",
                    "OPENAI_PROXY_API_KEY": "sk-test",
                },
                allow_anonymous=allow_anonymous,
            )

        def crashing_runner(_spec):
            raise RuntimeError("kaboom")

        matrices = run_matrix.collect_matrices(
            cfg_loader=loader, probe_specs=(spec,), runner=crashing_runner
        )
        self.assertEqual(matrices[0].results["a"], Result.FAIL)
        self.assertIn("kaboom", matrices[0].notes["a"])


class TestRegisteredProbeWiring(unittest.TestCase):
    """Sanity-check the wiring of real probe specs against their modules.

    Imports the probe module (allowed even without the SDK because
    probe modules guard the SDK import) and asserts the named test
    class and method names exist. This guards against typos in the
    spec registry without requiring the SDK to be installed.
    """

    def test_each_registered_probe_resolves(self) -> None:
        import importlib as _il

        for spec in run_matrix.PROBES:
            with self.subTest(probe=f"{spec.protocol}/{spec.sdk}"):
                mod = _il.import_module(spec.module)
                self.assertTrue(
                    hasattr(mod, spec.test_class),
                    f"{spec.module} missing {spec.test_class}",
                )
                cls = getattr(mod, spec.test_class)
                for case, method in spec.case_map:
                    self.assertTrue(
                        hasattr(cls, method),
                        f"{spec.test_class}.{method} missing (case {case!r})",
                    )


if __name__ == "__main__":
    unittest.main()
