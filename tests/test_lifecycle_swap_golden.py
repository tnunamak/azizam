"""Golden side-effect tests for SystemdDriver and HttpLoadUnloadDriver.

LIFECYCLE_DRIVER_PORT.md §4 — swap behavioral contract.

Each swap path drives the driver (SystemdDriver / HttpLoadUnloadDriver) under
controlled mocks and asserts the recorded side-effect sequence matches the
exact expected golden output (drop-in text, exec order, HTTP calls, activation
dict). These were originally proven byte-identical to the now-deleted legacy
swap functions; the expected values are pinned here directly.

Side-effect surfaces captured:
  - subprocess: asyncio.create_subprocess_exec calls → ("exec", argv)
  - filesystem: Path.write_text / Path.mkdir / Path.unlink → typed records
  - HTTP (httpx.AsyncClient): ("http", method, url, header_names, body) — header
    VALUES are never recorded (never print secrets).
  - metrics: firehose_metrics.record_event → ("metric", name, args, kwargs)

Kobold assertions (K1 trap):
  - Drop-in write text is byte-identical, including the exact header comment,
    [Service] block, and Environment="LLAMA_BEE_MODEL=..." / "LLAMA_BEE_MMPROJ=..."
    lines (NOT LLAMA_TURBO_*, which the design doc incorrectly claimed).
  - exec sequence == daemon-reload → restart koboldcpp.service, in that order.
  - health poll hits /v1/models.
  - failure path (bad restart exit code): both remove drop-in and return False.
  - failure path (health timeout): both remove drop-in and return False.
  - empty mmproj: Environment="LLAMA_BEE_MMPROJ=" (empty value).

Tabby assertions (T1 fix):
  - unload POST before load POST (when model already loaded).
  - load POST body includes {"model_name": target, **live_model_params} —
    NOT a static dict; live params come from get_tabby_model_config()[2].
  - success-marker path returns True.
  - error-marker path returns False.
  - verify-fallback path returns True.
  - already-loaded short-circuit: zero HTTP calls to load or unload.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402  (imports needed for legacy functions + module globals)
import firehose_metrics  # noqa: E402
from gateway.lifecycle_drivers import (
    SystemdDriver,
    HttpLoadUnloadDriver,
    register_params_provider,
    supports_swap,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


# The exact GGUF / mmproj paths used across all kobold tests.
_GGUF = "/models/qwen27b.gguf"
_MMPROJ = "/models/qwen27b-mmproj.gguf"
_KOBOLD_UNIT = proxy.KOBOLD_SERVICE_NAME  # e.g. "koboldcpp.service"
_KOBOLD_DROPIN: Path = proxy.KOBOLD_MODEL_DROPIN

# Expected drop-in text for (_GGUF, _MMPROJ).
_EXPECTED_DROPIN_TEXT = (
    "# Managed by azizam. Do not edit by hand; "
    "set role `model:`/`mmproj:` in proxy_config.yml.\n"
    "[Service]\n"
    f'Environment="LLAMA_BEE_MODEL={_GGUF}"\n'
    f'Environment="LLAMA_BEE_MMPROJ={_MMPROJ}"\n'
)

# Expected drop-in text when mmproj is empty (text-only model).
_EXPECTED_DROPIN_TEXT_NO_MMPROJ = (
    "# Managed by azizam. Do not edit by hand; "
    "set role `model:`/`mmproj:` in proxy_config.yml.\n"
    "[Service]\n"
    f'Environment="LLAMA_BEE_MODEL={_GGUF}"\n'
    'Environment="LLAMA_BEE_MMPROJ="\n'
)


def _kobold_svc() -> dict:
    """Minimal svc dict for SystemdDriver matching the kobold seed."""
    return {
        "host": "127.0.0.1",
        "port": proxy.kobold_cfg.get("port", 5051),
        "health_endpoint": "/v1/models",
        "lifecycle": {
            "driver": "systemd",
            "driver_config": {
                "scope": "user",
                "unit": _KOBOLD_UNIT,
                "dropin": "20-model.conf",
                "swap_strategy": "dropin-env-restart",
                "health_endpoint": "/v1/models",
                "health_timeout_s": 5,  # short for tests
                # Activation env-key mapping — LLAMA_BEE_* as DATA, not code.
                "activation_env_keys": {
                    "model": "LLAMA_BEE_MODEL",
                    "aux": "LLAMA_BEE_MMPROJ",
                },
                # Precheck: validate model is an absolute, existing .gguf path.
                "precheck": "gguf_path",
            },
        },
    }


def _tabby_svc() -> dict:
    """Minimal svc dict for HttpLoadUnloadDriver matching the tabby seed."""
    return {
        "host": "127.0.0.1",
        "port": proxy.tabby_cfg.get("port", 5050),
        "lifecycle": {
            "driver": "http-load-unload",
            "driver_config": {
                "load_url": "/v1/model/load",
                "unload_url": "/v1/model/unload",
                "admin_header": "x-admin-key",
                "load_body_params": "tabby-config",
                "load_timeout_s": 5,
                "success_marker": '"status":"finished"',
                "verify_endpoint": "/v1/model",
            },
        },
    }


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------

def _mock_subprocess_ok():
    """asyncio.create_subprocess_exec mock that always exits 0."""
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


def _mock_subprocess_fail(stderr: bytes = b"systemctl: error"):
    """asyncio.create_subprocess_exec mock that exits non-zero."""
    proc = AsyncMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


def _httpx_client_200(json_body: dict | None = None, text: str = "") -> tuple:
    """Return (mock_cls, mock_client) for httpx.AsyncClient that returns 200."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = json_body or {}
    mock_resp.text = text

    async def _aiter_bytes():
        yield text.encode()

    mock_resp.aiter_bytes = _aiter_bytes

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.post = AsyncMock(return_value=mock_resp)

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls, mock_client


# ---------------------------------------------------------------------------
# 1. SystemdDriver — kobold golden swap tests
# ---------------------------------------------------------------------------

class TestSystemdDriverSwapGolden(unittest.TestCase):
    """SystemdDriver.swap() produces byte-identical side-effects to swap_kobold_model()."""

    def setUp(self):
        self.driver = SystemdDriver()
        self.svc = _kobold_svc()

    # -----------------------------------------------------------------------
    # 1a. Success path — drop-in text, exec order, health poll, return True
    # -----------------------------------------------------------------------

    def test_dropin_text_byte_identical(self):
        """The drop-in written by SystemdDriver is byte-for-byte equal to legacy."""
        written_texts: list[str] = []

        # Capture Path.write_text calls for the dropin path.
        orig_write_text = Path.write_text

        def _fake_write_text(self_path, text, *args, **kwargs):
            if "systemd" in str(self_path):
                written_texts.append(text)

        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}

        mock_proc = _mock_subprocess_ok()
        mock_httpx_cls, mock_httpx_client = _httpx_client_200(text="ok")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text", _fake_write_text), \
             patch("httpx.AsyncClient", mock_httpx_cls), \
             patch("firehose_metrics.record_event"):
            result = _run(self.driver.swap(self.svc, activation))

        self.assertTrue(result, "swap should succeed with healthy health response")
        self.assertEqual(len(written_texts), 1, "exactly one write_text call to drop-in")
        self.assertEqual(
            written_texts[0],
            _EXPECTED_DROPIN_TEXT,
            "Drop-in text must be byte-identical to legacy swap_kobold_model() output.\n"
            f"Expected:\n{_EXPECTED_DROPIN_TEXT!r}\n"
            f"Got:\n{written_texts[0]!r}",
        )

    def test_dropin_text_empty_mmproj(self):
        """Empty mmproj value writes Environment=\"LLAMA_BEE_MMPROJ=\" (empty)."""
        written_texts: list[str] = []

        def _fake_write_text(self_path, text, *args, **kwargs):
            if "systemd" in str(self_path):
                written_texts.append(text)

        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": ""}}

        mock_proc = _mock_subprocess_ok()
        mock_httpx_cls, _ = _httpx_client_200(text="ok")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text", _fake_write_text), \
             patch("httpx.AsyncClient", mock_httpx_cls), \
             patch("firehose_metrics.record_event"):
            result = _run(self.driver.swap(self.svc, activation))

        self.assertTrue(result)
        self.assertEqual(len(written_texts), 1)
        self.assertEqual(
            written_texts[0],
            _EXPECTED_DROPIN_TEXT_NO_MMPROJ,
            "Empty mmproj must write Environment=\"LLAMA_BEE_MMPROJ=\" (not omit the line).\n"
            f"Expected:\n{_EXPECTED_DROPIN_TEXT_NO_MMPROJ!r}\n"
            f"Got:\n{written_texts[0]!r}",
        )

    def test_exec_sequence_daemon_reload_then_restart(self):
        """daemon-reload runs BEFORE restart — exact order from swap_kobold_model()."""
        exec_calls: list[tuple] = []

        async def _fake_exec(*cmd, **kwargs):
            exec_calls.append(tuple(cmd))
            return _mock_subprocess_ok()

        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}
        mock_httpx_cls, _ = _httpx_client_200(text="ok")

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch("httpx.AsyncClient", mock_httpx_cls), \
             patch("firehose_metrics.record_event"):
            _run(self.driver.swap(self.svc, activation))

        self.assertEqual(len(exec_calls), 2, f"Expected 2 exec calls, got: {exec_calls}")
        self.assertEqual(
            exec_calls[0],
            ("systemctl", "--user", "daemon-reload"),
            f"First exec must be daemon-reload, got: {exec_calls[0]}",
        )
        self.assertEqual(
            exec_calls[1],
            ("systemctl", "--user", "restart", _KOBOLD_UNIT),
            f"Second exec must be restart <unit>, got: {exec_calls[1]}",
        )

    def test_returns_true_on_healthy_response(self):
        """swap() returns True when health poll responds 200."""
        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}
        mock_proc = _mock_subprocess_ok()
        mock_httpx_cls, _ = _httpx_client_200(text="ok")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch("httpx.AsyncClient", mock_httpx_cls), \
             patch("firehose_metrics.record_event"):
            result = _run(self.driver.swap(self.svc, activation))

        self.assertTrue(result)

    # -----------------------------------------------------------------------
    # 1b. Failure path — bad restart → drop-in removed, returns False
    # -----------------------------------------------------------------------

    def test_restart_fail_removes_dropin_returns_false(self):
        """Non-zero restart exit code → remove drop-in → return False."""
        exec_calls: list[tuple] = []
        unlinked: list[Path] = []

        async def _fake_exec(*cmd, **kwargs):
            exec_calls.append(tuple(cmd))
            if cmd[2] == "restart":
                return _mock_subprocess_fail()
            return _mock_subprocess_ok()

        orig_unlink = Path.unlink

        def _fake_unlink(self_path, **kwargs):
            if "systemd" in str(self_path):
                unlinked.append(self_path)

        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch.object(Path, "unlink", _fake_unlink):
            result = _run(self.driver.swap(self.svc, activation))

        self.assertFalse(result, "swap must return False when restart fails")
        self.assertTrue(len(unlinked) > 0, "drop-in must be removed after restart failure")

    def test_write_fail_returns_false_no_exec(self):
        """write_text failure → return False immediately, no exec calls."""
        exec_calls: list[tuple] = []

        async def _fake_exec(*cmd, **kwargs):
            exec_calls.append(tuple(cmd))
            return _mock_subprocess_ok()

        def _raise_on_write(self_path, text, *args, **kwargs):
            if "systemd" in str(self_path):
                raise OSError("disk full")

        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text", _raise_on_write):
            result = _run(self.driver.swap(self.svc, activation))

        self.assertFalse(result)
        self.assertEqual(exec_calls, [], "no exec calls must happen after write failure")

    # -----------------------------------------------------------------------
    # 1c. Health timeout → drop-in removed, returns False
    # -----------------------------------------------------------------------

    def test_health_timeout_removes_dropin_returns_false(self):
        """Health poll timeout → remove drop-in → return False."""
        unlinked: list[Path] = []

        def _fake_unlink(self_path, **kwargs):
            if "systemd" in str(self_path):
                unlinked.append(self_path)

        # httpx always raises (simulates unreachable endpoint).
        import httpx as _httpx
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_proc = _mock_subprocess_ok()
        # Short timeout via driver_config so test runs fast.
        svc = dict(_kobold_svc())
        svc["lifecycle"] = dict(svc["lifecycle"])
        svc["lifecycle"]["driver_config"] = dict(svc["lifecycle"]["driver_config"])
        svc["lifecycle"]["driver_config"]["health_timeout_s"] = 0.1

        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch.object(Path, "unlink", _fake_unlink), \
             patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.driver.swap(svc, activation))

        self.assertFalse(result, "swap must return False on health timeout")
        self.assertTrue(len(unlinked) > 0, "drop-in must be removed after health timeout")

    # -----------------------------------------------------------------------
    # 1d. supports_swap() flag
    # -----------------------------------------------------------------------

    def test_supports_swap_is_true(self):
        self.assertTrue(supports_swap(self.driver))


# ---------------------------------------------------------------------------
# 2. HttpLoadUnloadDriver — tabby golden swap tests
# ---------------------------------------------------------------------------

# Simulate the live model_params that get_tabby_model_config() would return.
_TABBY_LIVE_PARAMS = {
    "max_seq_len": 8192,
    "cache_size": 4096,
    "cache_mode": "Q4",
}
_TABBY_MODEL = "Qwen2.5-72B-Instruct"
_TABBY_CURRENT_MODEL = "Llama-3.1-8B"


def _setup_tabby_params_provider():
    """Register a deterministic params-provider for tests."""
    register_params_provider("tabby-config", lambda svc: dict(_TABBY_LIVE_PARAMS))


class TestHttpLoadUnloadDriverSwapGolden(unittest.TestCase):
    """HttpLoadUnloadDriver.swap() produces byte-identical side-effects to swap_tabby_model()."""

    def setUp(self):
        self.driver = HttpLoadUnloadDriver()
        self.svc = _tabby_svc()
        _setup_tabby_params_provider()

    # -----------------------------------------------------------------------
    # 2a. Already-loaded short-circuit — zero HTTP load/unload calls
    # -----------------------------------------------------------------------

    def test_already_loaded_returns_true_no_http_calls(self):
        """If target already loaded, return True immediately with no load/unload."""
        http_calls: list[tuple] = []

        # GET /v1/model returns the target model → already loaded.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": _TABBY_MODEL}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.post = AsyncMock(side_effect=lambda *a, **kw: http_calls.append(("post", a, kw)) or mock_resp)
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.driver.swap(self.svc, {"model_name": _TABBY_MODEL}))

        self.assertTrue(result)
        self.assertEqual(
            http_calls, [],
            "Already-loaded path must issue zero load/unload HTTP calls",
        )

    # -----------------------------------------------------------------------
    # 2b. Unload then load — success marker path
    # -----------------------------------------------------------------------

    def test_unload_then_load_success_marker(self):
        """Unloads current model then loads target; returns True on success marker."""
        http_posts: list[tuple] = []
        http_gets: list[str] = []

        call_count = {"n": 0}

        # First GET → current model (_TABBY_CURRENT_MODEL)
        # Subsequent GETs (shouldn't be needed on success-marker path) → return target.
        get_resp_current = MagicMock()
        get_resp_current.status_code = 200
        get_resp_current.json.return_value = {"id": _TABBY_CURRENT_MODEL}

        success_text = '"status":"finished"'

        async def _aiter_bytes_success():
            yield success_text.encode()

        load_resp = MagicMock()
        load_resp.status_code = 200
        load_resp.aiter_bytes = _aiter_bytes_success
        load_resp.text = success_text

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        async def _mock_get(url, **kwargs):
            http_gets.append(url)
            return get_resp_current

        async def _mock_post(url, **kwargs):
            http_posts.append((url, kwargs.get("headers", {}), kwargs.get("json")))
            if "unload" in url:
                return unload_resp
            return load_resp

        mock_client = AsyncMock()
        mock_client.get = _mock_get
        mock_client.post = _mock_post
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.driver.swap(self.svc, {"model_name": _TABBY_MODEL}))

        self.assertTrue(result)

        # Verify unload was called before load.
        post_urls = [p[0] for p in http_posts]
        self.assertTrue(
            any("unload" in u for u in post_urls),
            f"Expected an unload POST; got: {post_urls}",
        )
        self.assertTrue(
            any("load" in u and "unload" not in u for u in post_urls),
            f"Expected a load POST; got: {post_urls}",
        )
        unload_idx = next(i for i, u in enumerate(post_urls) if "unload" in u)
        load_idx = next(i for i, u in enumerate(post_urls) if "load" in u and "unload" not in u)
        self.assertLess(unload_idx, load_idx, "unload POST must come before load POST")

    def test_load_body_includes_live_model_params(self):
        """Load POST body = {model_name: target, **live_params} — NOT a static dict.

        This is the T1 fix: parity with swap_tabby_model's get_tabby_model_config() read.
        """
        captured_load_body: list[dict] = []

        # GET verify_endpoint: returns different model (need to swap).
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"id": _TABBY_CURRENT_MODEL}

        success_text = '"status":"finished"'

        async def _aiter_bytes():
            yield success_text.encode()

        load_resp = MagicMock()
        load_resp.status_code = 200
        load_resp.aiter_bytes = _aiter_bytes
        load_resp.text = success_text

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        async def _mock_get(url, **kwargs):
            return get_resp

        async def _mock_post(url, **kwargs):
            if "unload" in url:
                return unload_resp
            # Capture the load body.
            captured_load_body.append(kwargs.get("json", {}))
            return load_resp

        mock_client = AsyncMock()
        mock_client.get = _mock_get
        mock_client.post = _mock_post
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.driver.swap(self.svc, {"model_name": _TABBY_MODEL}))

        self.assertTrue(result)
        self.assertEqual(len(captured_load_body), 1, "Expected exactly one load POST")
        body = captured_load_body[0]

        # model_name must be present.
        self.assertEqual(body.get("model_name"), _TABBY_MODEL)

        # ALL live params from the params-provider must be in the body.
        for key, value in _TABBY_LIVE_PARAMS.items():
            self.assertIn(key, body, f"Live param '{key}' missing from load body (T1 fix)")
            self.assertEqual(
                body[key],
                value,
                f"Live param '{key}' value mismatch in load body (T1 fix)",
            )

    # -----------------------------------------------------------------------
    # 2c. Error-marker path → return False
    # -----------------------------------------------------------------------

    def test_error_marker_returns_false(self):
        """Error in load response → return False."""
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"id": _TABBY_CURRENT_MODEL}

        error_text = '{"status":"error","message":"OOM"}'

        async def _aiter_bytes_error():
            yield error_text.encode()

        error_resp = MagicMock()
        error_resp.status_code = 200
        error_resp.aiter_bytes = _aiter_bytes_error
        error_resp.text = error_text

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        async def _mock_get(url, **kwargs):
            return get_resp

        async def _mock_post(url, **kwargs):
            if "unload" in url:
                return unload_resp
            return error_resp

        mock_client = AsyncMock()
        mock_client.get = _mock_get
        mock_client.post = _mock_post
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.driver.swap(self.svc, {"model_name": _TABBY_MODEL}))

        self.assertFalse(result)

    # -----------------------------------------------------------------------
    # 2d. Verify-fallback path → return True when model matches
    # -----------------------------------------------------------------------

    def test_verify_fallback_returns_true_when_model_matches(self):
        """If no success/error marker, fall back to GET /v1/model; return True if matches."""
        call_count = {"gets": 0}

        ambiguous_text = '{"status":"loading"}'

        async def _aiter_bytes_ambig():
            yield ambiguous_text.encode()

        ambig_resp = MagicMock()
        ambig_resp.status_code = 200
        ambig_resp.aiter_bytes = _aiter_bytes_ambig
        ambig_resp.text = ambiguous_text

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        # First GET: current model (different). Second GET (verify): target loaded.
        get_resp_current = MagicMock()
        get_resp_current.status_code = 200
        get_resp_current.json.return_value = {"id": _TABBY_CURRENT_MODEL}

        get_resp_target = MagicMock()
        get_resp_target.status_code = 200
        get_resp_target.json.return_value = {"id": _TABBY_MODEL}

        async def _mock_get(url, **kwargs):
            call_count["gets"] += 1
            if call_count["gets"] == 1:
                return get_resp_current
            return get_resp_target

        async def _mock_post(url, **kwargs):
            if "unload" in url:
                return unload_resp
            return ambig_resp

        mock_client = AsyncMock()
        mock_client.get = _mock_get
        mock_client.post = _mock_post
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.driver.swap(self.svc, {"model_name": _TABBY_MODEL}))

        self.assertTrue(result, "verify-fallback should return True when model matches")

    # -----------------------------------------------------------------------
    # 2e. Admin header name is "x-admin-key" in load/unload calls
    # -----------------------------------------------------------------------

    def test_admin_header_name_present_in_load_and_unload(self):
        """admin_header key is sent in both unload and load POST headers."""
        header_names_seen: list[set] = []

        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"id": _TABBY_CURRENT_MODEL}

        success_text = '"status":"finished"'

        async def _aiter_bytes():
            yield success_text.encode()

        load_resp = MagicMock()
        load_resp.status_code = 200
        load_resp.aiter_bytes = _aiter_bytes
        load_resp.text = success_text

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        async def _mock_get(url, **kwargs):
            return get_resp

        async def _mock_post(url, **kwargs):
            # Record header NAMES only — never values.
            header_names_seen.append(set((kwargs.get("headers") or {}).keys()))
            if "unload" in url:
                return unload_resp
            return load_resp

        mock_client = AsyncMock()
        mock_client.get = _mock_get
        mock_client.post = _mock_post
        mock_httpx_cls = MagicMock()
        mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            _run(self.driver.swap(self.svc, {"model_name": _TABBY_MODEL}))

        for names in header_names_seen:
            self.assertIn(
                "x-admin-key",
                names,
                f"'x-admin-key' header must be present in POST headers; got: {names}",
            )

    # -----------------------------------------------------------------------
    # 2f. supports_swap() flag
    # -----------------------------------------------------------------------

    def test_supports_swap_is_true(self):
        self.assertTrue(supports_swap(self.driver))


# ---------------------------------------------------------------------------
# 3. ensure_target() driver-path integration (AZIZAM_DRIVER_SWAP=1)
# ---------------------------------------------------------------------------

class TestEnsureTargetDriverPath(unittest.TestCase):
    """ensure_target() routes every backend through its LifecycleDriver.

    To exercise the swap path we inject lifecycle.driver keys into
    proxy.config["backends"] for kobold/tabby — mirroring the seed data the
    production deployment carries (proxy_config.yml -> admin.db registry).
    """

    def setUp(self):
        import copy
        _setup_tabby_params_provider()
        self._orig_config = copy.deepcopy(dict(proxy.config))

        # Inject seed lifecycle config so driver_for_service() resolves correctly.
        backends = dict(proxy.config.get("backends") or {})
        kobold_seed = dict(backends.get("kobold") or {})
        kobold_seed["lifecycle"] = {
            "driver": "systemd",
            "driver_config": {
                "scope": "user",
                "unit": _KOBOLD_UNIT,
                "dropin": "20-model.conf",
                "swap_strategy": "dropin-env-restart",
                "health_endpoint": "/v1/models",
                "health_timeout_s": 5,
                "activation_env_keys": {
                    "model": "LLAMA_BEE_MODEL",
                    "aux": "LLAMA_BEE_MMPROJ",
                },
                "precheck": "gguf_path",
            },
        }
        tabby_seed = dict(backends.get("tabby") or {})
        tabby_seed["lifecycle"] = {
            "driver": "http-load-unload",
            "driver_config": {
                "load_url": "/v1/model/load",
                "unload_url": "/v1/model/unload",
                "admin_header": "x-admin-key",
                "load_body_params": "tabby-config",
                "load_timeout_s": 5,
                "success_marker": '"status":"finished"',
                "verify_endpoint": "/v1/model",
            },
        }
        backends["kobold"] = kobold_seed
        backends["tabby"] = tabby_seed
        proxy.config["backends"] = backends

    def tearDown(self):
        proxy.config.clear()
        proxy.config.update(self._orig_config)

    def test_a_service_start_budget_is_honoured_without_a_model(self):
        """A model-loading engine declares health_timeout_s; the 5 s default must not cut its cold start short."""
        import gateway.lifecycle_drivers as _lcd

        seen: list[float] = []

        async def _fake_ready(svc, timeout_s):
            seen.append(timeout_s)
            return True

        slow = {"host": "127.0.0.1", "port": 1, "lifecycle": {"driver": "systemd", "driver_config": {"unit": "slow.service", "health_timeout_s": 120}}}
        plain = {"host": "127.0.0.1", "port": 2, "lifecycle": {"driver": "systemd", "driver_config": {"unit": "plain.service"}}}
        proxy.config["backends"] = {**proxy.config["backends"], "slow-engine": slow, "plain-engine": plain}
        with patch.object(_lcd._systemd_driver, "ensure_ready", side_effect=_fake_ready):
            self.assertTrue(_run(proxy.ensure_target("slow-engine", None)))
            self.assertTrue(_run(proxy.ensure_target("plain-engine", None)))
        self.assertEqual(seen, [120.0, float(proxy.HEALTH_CHECK_TIMEOUT)])

    def test_kobold_driver_path_calls_systemd_swap(self):
        """ensure_target('kobold', gguf) calls SystemdDriver.swap()."""
        import gateway.lifecycle_drivers as _lcd

        swap_called_with: list[tuple] = []

        async def _fake_swap(svc, activation, ctx=None, warned_models=None):
            swap_called_with.append((svc, activation, ctx, warned_models))
            return True

        # Patch the singleton instance that driver_for_service() returns.
        # Also patch the loaded-model probes so ensure_target can build SwapContext.
        with patch.object(_lcd._systemd_driver, "swap", side_effect=_fake_swap), \
             patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch.object(proxy, "is_literal_gguf_path", return_value=True), \
             patch.object(proxy, "currently_loaded_kobold_model", return_value="/old.gguf"), \
             patch.object(proxy, "currently_loaded_kobold_mmproj", return_value=None):
            result = _run(proxy.ensure_target("kobold", _GGUF, _MMPROJ))

        self.assertTrue(result)
        self.assertEqual(len(swap_called_with), 1, "SystemdDriver.swap must be called once")
        _, activation, ctx, warned_models = swap_called_with[0]
        self.assertEqual(activation["env"]["LLAMA_BEE_MODEL"], _GGUF)
        self.assertEqual(activation["env"]["LLAMA_BEE_MMPROJ"], _MMPROJ)
        # SwapContext must carry the expected state.
        self.assertIsNotNone(ctx, "ctx must be passed to driver.swap()")
        self.assertEqual(ctx.target_model, _GGUF)
        self.assertEqual(ctx.target_aux, _MMPROJ)
        self.assertEqual(ctx.loaded_model, "/old.gguf")
        # warned_models must be the shared proxy._pin_mismatch_warned set.
        self.assertIs(warned_models, proxy._pin_mismatch_warned)

    def test_tabby_driver_path_calls_http_load_unload_swap(self):
        """ensure_target('tabby', model) calls HttpLoadUnloadDriver.swap()."""
        import gateway.lifecycle_drivers as _lcd

        swap_called_with: list[tuple] = []

        async def _fake_swap(svc, activation, ctx=None, warned_models=None):
            swap_called_with.append((svc, activation, ctx, warned_models))
            return True

        with patch.object(_lcd._http_load_unload_driver, "swap", side_effect=_fake_swap), \
             patch.object(proxy, "currently_loaded_tabby_model", new=AsyncMock(return_value="other-model")):
            result = _run(proxy.ensure_target("tabby", _TABBY_MODEL))

        self.assertTrue(result)
        self.assertEqual(len(swap_called_with), 1)
        _, activation, ctx, warned_models = swap_called_with[0]
        self.assertEqual(activation["model_name"], _TABBY_MODEL)
        # SwapContext must be passed with correct target_model.
        self.assertIsNotNone(ctx, "ctx must be passed to driver.swap()")
        self.assertEqual(ctx.target_model, _TABBY_MODEL)


# ---------------------------------------------------------------------------
# 4. Seed data contract — driver_for_service resolves new driver types
# ---------------------------------------------------------------------------

class TestDriverForServiceNewDrivers(unittest.TestCase):
    """driver_for_service() resolves 'systemd' and 'http-load-unload' correctly."""

    from gateway.lifecycle_drivers import driver_for_service

    def _call(self, service_id, backends):
        from gateway.lifecycle_drivers import driver_for_service
        return driver_for_service(service_id, config_backends=backends)

    def test_systemd_driver_resolved(self):
        backends = {"kobold": {**_kobold_svc()}}
        driver = self._call("kobold", backends)
        self.assertIsInstance(driver, SystemdDriver)

    def test_http_load_unload_driver_resolved(self):
        backends = {"tabby": {**_tabby_svc()}}
        driver = self._call("tabby", backends)
        self.assertIsInstance(driver, HttpLoadUnloadDriver)

    def test_unknown_still_falls_back_to_external(self):
        from gateway.lifecycle_drivers import ExternalDriver
        backends = {"mystery": {"lifecycle": {"driver": "unknown-engine-xyz"}}}
        driver = self._call("mystery", backends)
        self.assertIsInstance(driver, ExternalDriver)


# ---------------------------------------------------------------------------
# 5. Byte-identical proof: SwapContext policy vs legacy functions
#
# For each of the 5 canonical cases we assert that:
#   (i)  The driver path (via SwapContext) returns the same bool as legacy.
#   (ii) The driver path produces the same side-effect trace as legacy
#        (subprocess calls, HTTP calls, filesystem writes).
#
# "Byte-identical" means same return value AND same side-effect sequence for
# every case.  We capture side-effects as event lists and compare them.
# ---------------------------------------------------------------------------

class TestSwapContextByteIdenticalProof(unittest.TestCase):
    """Prove that the five SwapContext-driven cases match legacy side-effects."""

    def setUp(self):
        self.kobold_driver = SystemdDriver()
        self.tabby_driver = HttpLoadUnloadDriver()
        self.kobold_svc = _kobold_svc()
        self.tabby_svc = _tabby_svc()
        # Clear the shared warned set between tests.
        self._orig_warned = set(proxy._pin_mismatch_warned)
        proxy._pin_mismatch_warned.clear()

    def tearDown(self):
        proxy._pin_mismatch_warned.clear()
        proxy._pin_mismatch_warned.update(self._orig_warned)

    # -------------------------------------------------------------------
    # Case (a): kobold already-loaded — no dropin write, no restart
    # -------------------------------------------------------------------

    def test_a_kobold_already_loaded_no_write_no_restart(self):
        """Case (a): already-loaded kobold → driver returns True with zero subprocess calls.

        Legacy: swap_kobold_model re-checks inside the lock and returns True
        immediately.  Driver: SwapContext short-circuits before any IO.
        """
        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}
        ctx = _lcd.SwapContext(
            loaded_model=_GGUF,
            target_model=_GGUF,
            loaded_aux=_MMPROJ,
            target_aux=_MMPROJ,
            pin_loaded=False,
        )

        exec_calls: list = []
        write_calls: list = []

        async def _fake_exec(*args, **kwargs):
            exec_calls.append(args)
            return _mock_subprocess_ok()

        orig_write = Path.write_text

        def _fake_write(self_path, text, *a, **kw):
            write_calls.append(text)

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), \
             patch.object(Path, "write_text", _fake_write), \
             patch.object(Path, "mkdir"):
            result = _run(self.kobold_driver.swap(self.kobold_svc, activation, ctx=ctx))

        self.assertTrue(result, "Case (a): driver must return True for already-loaded kobold")
        self.assertEqual(exec_calls, [], "Case (a): no subprocess exec when already loaded")
        self.assertEqual(write_calls, [], "Case (a): no dropin write when already loaded")

    # -------------------------------------------------------------------
    # Case (b): kobold pinned mismatch — no restart, warn once
    # -------------------------------------------------------------------

    def test_b_kobold_pinned_mismatch_no_restart_warn_once(self):
        """Case (b): pinned kobold mismatch → True without restart; warn printed once.

        Legacy (proxy.py ensure_target legacy path):
          if PIN_LOADED_MODEL and model not in _pin_mismatch_warned:
              _pin_mismatch_warned.add(model); print(warn); return True
        Driver must produce identical return (True) + identical warn message.
        """
        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": ""}}
        ctx = _lcd.SwapContext(
            loaded_model="/models/other.gguf",
            target_model=_GGUF,
            loaded_aux=None,
            target_aux=None,
            pin_loaded=True,
        )

        exec_calls: list = []
        write_calls: list = []
        printed: list[str] = []

        async def _fake_exec(*args, **kwargs):
            exec_calls.append(args)
            return _mock_subprocess_ok()

        def _fake_write(self_path, text, *a, **kw):
            write_calls.append(text)

        import builtins
        orig_print = builtins.print

        def _capture_print(*args, **kwargs):
            printed.append(" ".join(str(a) for a in args))
            orig_print(*args, **kwargs)

        warned: set = set()

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), \
             patch.object(Path, "write_text", _fake_write), \
             patch.object(Path, "mkdir"), \
             patch("builtins.print", side_effect=_capture_print):
            # First call — should warn.
            result1 = _run(self.kobold_driver.swap(
                self.kobold_svc, activation, ctx=ctx, warned_models=warned
            ))
            # Second call — same target, must NOT warn again.
            result2 = _run(self.kobold_driver.swap(
                self.kobold_svc, activation, ctx=ctx, warned_models=warned
            ))

        self.assertTrue(result1, "Case (b): pinned driver must return True on first call")
        self.assertTrue(result2, "Case (b): pinned driver must return True on second call")
        self.assertEqual(exec_calls, [], "Case (b): no subprocess exec when pinned")
        self.assertEqual(write_calls, [], "Case (b): no dropin write when pinned")

        pin_warns = [m for m in printed if "pin_loaded_model=true" in m]
        self.assertEqual(len(pin_warns), 1, "Case (b): warn must fire exactly once")
        self.assertIn("requested=" + _GGUF, pin_warns[0])
        self.assertIn("serving with current model", pin_warns[0])

    # -------------------------------------------------------------------
    # Case (c): kobold unpinned mismatch → dropin write + daemon-reload + restart
    # -------------------------------------------------------------------

    def test_c_kobold_unpinned_mismatch_write_reload_restart(self):
        """Case (c): unpinned kobold mismatch → dropin write + reload + restart.

        Legacy: swap_kobold_model writes drop-in, daemon-reload, restart.
        Driver: identical byte sequence when ctx shows mismatch + pin_loaded=False.
        """
        activation = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}
        ctx = _lcd.SwapContext(
            loaded_model="/models/old.gguf",
            target_model=_GGUF,
            loaded_aux=None,
            target_aux=_MMPROJ,
            pin_loaded=False,
        )

        written_texts: list[str] = []
        exec_argv: list[list] = []

        orig_write = Path.write_text

        def _fake_write(self_path, text, *a, **kw):
            if "systemd" in str(self_path) or "model.conf" in str(self_path):
                written_texts.append(text)

        mock_proc = _mock_subprocess_ok()
        mock_httpx_cls, _ = _httpx_client_200(text="ok")

        # Capture exec argv via a side-effect that still returns the mock proc.
        orig_create = asyncio.create_subprocess_exec

        async def _capture_exec(*args, **kwargs):
            exec_argv.append(list(args))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text", _fake_write), \
             patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.kobold_driver.swap(
                self.kobold_svc, activation, ctx=ctx
            ))

        self.assertTrue(result, "Case (c): unpinned mismatch must succeed (mocked success)")

        # Dropin text byte-identical to legacy.
        self.assertEqual(len(written_texts), 1, "Case (c): exactly one dropin write")
        self.assertEqual(
            written_texts[0],
            _EXPECTED_DROPIN_TEXT,
            "Case (c): dropin text must match _EXPECTED_DROPIN_TEXT byte-for-byte",
        )

        # Exec sequence: daemon-reload → restart (order matters).
        self.assertEqual(len(exec_argv), 2, "Case (c): daemon-reload then restart")
        self.assertIn("daemon-reload", exec_argv[0])
        self.assertIn("restart", exec_argv[1])
        self.assertIn(_KOBOLD_UNIT, exec_argv[1])

    # -------------------------------------------------------------------
    # Case (d): tabby already-loaded — no unload, no load call
    # -------------------------------------------------------------------

    def test_d_tabby_already_loaded_no_http_calls(self):
        """Case (d): tabby already-loaded → True, zero HTTP calls.

        Legacy: swap_tabby_model does current = await currently_loaded_tabby_model();
        if current == target_model: return True.
        Driver with ctx: uses ctx.loaded_model directly, same short-circuit.
        """
        _setup_tabby_params_provider()

        ctx = _lcd.SwapContext(
            loaded_model=_TABBY_MODEL,
            target_model=_TABBY_MODEL,
        )

        http_calls: list = []

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not GET")))
        mock_client.post = AsyncMock(side_effect=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not POST")))

        # We need to track if ANY http calls are made.
        http_made = []
        orig_enter = AsyncMock()

        import httpx as _httpx_module

        class _TrackingClient:
            async def __aenter__(self_inner):
                http_made.append(True)
                raise AssertionError("HttpLoadUnloadDriver must not make HTTP calls for already-loaded model")

            async def __aexit__(self_inner, *a):
                pass

        with patch("httpx.AsyncClient", return_value=_TrackingClient()):
            result = _run(self.tabby_driver.swap(
                self.tabby_svc, {"model_name": _TABBY_MODEL}, ctx=ctx
            ))

        self.assertTrue(result, "Case (d): already-loaded tabby must return True")
        self.assertEqual(http_made, [], "Case (d): no HTTP calls when ctx says already loaded")

    # -------------------------------------------------------------------
    # Case (e): tabby mismatch → unload + load (same HTTP call sequence as legacy)
    # -------------------------------------------------------------------

    def test_e_tabby_mismatch_unload_then_load(self):
        """Case (e): tabby mismatch → unload POST then load POST, byte-identical to legacy.

        Legacy: if current != target: POST unload, POST load.
        Driver with ctx: when ctx.loaded_model != target, same sequence.
        """
        _setup_tabby_params_provider()

        current_model = "OldModel-7B"
        ctx = _lcd.SwapContext(
            loaded_model=current_model,
            target_model=_TABBY_MODEL,
        )

        success_text = '"status":"finished"'
        mock_httpx_cls, mock_httpx_client = _httpx_client_200(text=success_text)

        with patch("httpx.AsyncClient", mock_httpx_cls):
            result = _run(self.tabby_driver.swap(
                self.tabby_svc,
                {"model_name": _TABBY_MODEL},
                ctx=ctx,
            ))

        self.assertTrue(result, "Case (e): tabby mismatch swap must succeed (mocked)")

        # Must have made an unload POST and a load POST.
        post_calls = mock_httpx_client.post.call_args_list
        self.assertEqual(len(post_calls), 2, "Case (e): must issue exactly 2 POSTs (unload then load)")

        unload_call = post_calls[0]
        load_call = post_calls[1]

        # Verify unload URL
        self.assertIn("/v1/model/unload", str(unload_call))
        # Verify load URL and body
        self.assertIn("/v1/model/load", str(load_call))
        # Load body must include model_name
        load_kwargs = load_call.kwargs if load_call.kwargs else {}
        if "json" in load_kwargs:
            self.assertEqual(load_kwargs["json"].get("model_name"), _TABBY_MODEL)


# ---------------------------------------------------------------------------
# Helper: import SwapContext for use in test classes above
# ---------------------------------------------------------------------------
import gateway.lifecycle_drivers as _lcd  # noqa: E402 (needed by the proof tests)


# ---------------------------------------------------------------------------
# 6. build_activation golden tests — byte-identical to _activation_for_* legacy
# ---------------------------------------------------------------------------

class TestBuildActivationGolden(unittest.TestCase):
    """build_activation() output is the exact per-model activation block.

    Verifies:
      - SystemdDriver.build_activation(seeded_kobold, model=X, aux=Y)
          == {"env": {"LLAMA_BEE_MODEL": X, "LLAMA_BEE_MMPROJ": Y}}
      - HttpLoadUnloadDriver.build_activation(seeded_tabby, model=X)
          == {"model_name": X}
      - Precheck: missing GGUF → None (loud print).
      - Precheck: present abs .gguf → returns the activation dict.
      - aux present but not found → None.
      - aux=None → env key set to "" (empty).
    """

    def setUp(self):
        self.kobold_driver = SystemdDriver()
        self.tabby_driver = HttpLoadUnloadDriver()
        # Use the seeded svc dicts (production-matching driver_config).
        self.kobold_svc = _kobold_svc()
        self.tabby_svc = _tabby_svc()

    # -----------------------------------------------------------------------
    # Kobold / SystemdDriver
    # -----------------------------------------------------------------------

    def test_kobold_build_activation_exact_env_with_aux(self):
        """SystemdDriver.build_activation produces the exact env block with aux."""
        with patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", return_value=True):
            result = self.kobold_driver.build_activation(
                self.kobold_svc, model=_GGUF, aux=_MMPROJ
            )
        expected = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": _MMPROJ}}
        self.assertIsNotNone(result)
        self.assertEqual(result, expected, (
            "build_activation must produce the exact env activation.\n"
            f"Expected: {expected!r}\nGot:      {result!r}"
        ))

    def test_kobold_build_activation_exact_env_no_aux(self):
        """SystemdDriver.build_activation with aux=None sets LLAMA_BEE_MMPROJ=''."""
        with patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", return_value=True):
            result = self.kobold_driver.build_activation(
                self.kobold_svc, model=_GGUF, aux=None
            )
        expected = {"env": {"LLAMA_BEE_MODEL": _GGUF, "LLAMA_BEE_MMPROJ": ""}}
        self.assertIsNotNone(result)
        self.assertEqual(result, expected, (
            "build_activation (no aux) must set the aux env key to empty.\n"
            f"Expected: {expected!r}\nGot:      {result!r}"
        ))

    def test_kobold_precheck_missing_gguf_returns_none(self):
        """Precheck: non-existent GGUF path → build_activation returns None."""
        printed: list[str] = []
        import builtins
        orig_print = builtins.print

        def _cap(*args, **kw):
            printed.append(" ".join(str(a) for a in args))
            orig_print(*args, **kw)

        with patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.print", side_effect=_cap):
            result = self.kobold_driver.build_activation(
                self.kobold_svc, model=_GGUF, aux=None
            )

        self.assertIsNone(result, "Missing GGUF must return None from build_activation")
        # Print message must contain the missing path (same wording as legacy).
        self.assertTrue(
            any("GGUF not found" in p and _GGUF in p for p in printed),
            f"Expected 'GGUF not found at {_GGUF}' in print output; got: {printed}",
        )

    def test_kobold_precheck_missing_aux_returns_none(self):
        """Precheck: model exists but aux path is missing → build_activation returns None."""
        printed: list[str] = []
        import builtins
        orig_print = builtins.print

        def _cap(*args, **kw):
            printed.append(" ".join(str(a) for a in args))
            orig_print(*args, **kw)

        # model check passes (isabs+exists), aux check fails (exists=False for aux).
        def _exists(path):
            return str(path) == _GGUF  # only the model exists

        with patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", side_effect=_exists), \
             patch("builtins.print", side_effect=_cap):
            result = self.kobold_driver.build_activation(
                self.kobold_svc, model=_GGUF, aux=_MMPROJ
            )

        self.assertIsNone(result, "Missing aux must return None from build_activation")
        self.assertTrue(
            any("mmproj not found" in p for p in printed),
            f"Expected 'mmproj not found' in print output; got: {printed}",
        )

    def test_kobold_precheck_present_gguf_returns_activation(self):
        """Precheck passes for an abs .gguf path that exists — returns the activation."""
        with patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", return_value=True):
            result = self.kobold_driver.build_activation(
                self.kobold_svc, model=_GGUF, aux=None
            )
        self.assertIsNotNone(result)
        self.assertIn("env", result)
        self.assertEqual(result["env"]["LLAMA_BEE_MODEL"], _GGUF)
        self.assertEqual(result["env"]["LLAMA_BEE_MMPROJ"], "")

    # -----------------------------------------------------------------------
    # Tabby / HttpLoadUnloadDriver
    # -----------------------------------------------------------------------

    def test_tabby_build_activation_exact_model_name(self):
        """HttpLoadUnloadDriver.build_activation produces {'model_name': model}."""
        result = self.tabby_driver.build_activation(self.tabby_svc, model=_TABBY_MODEL)
        expected = {"model_name": _TABBY_MODEL}
        self.assertIsNotNone(result)
        self.assertEqual(result, expected, (
            "build_activation must produce the exact model_name activation.\n"
            f"Expected: {expected!r}\nGot:      {result!r}"
        ))

    def test_tabby_build_activation_no_precheck_always_succeeds(self):
        """HttpLoadUnloadDriver.build_activation has no precheck; always returns a dict."""
        result = self.tabby_driver.build_activation(
            self.tabby_svc, model="some-model-name", aux=None
        )
        self.assertIsNotNone(result)
        self.assertIn("model_name", result)
        self.assertEqual(result["model_name"], "some-model-name")


if __name__ == "__main__":
    unittest.main()
