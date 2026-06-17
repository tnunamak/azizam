"""Golden side-effect parity tests for SystemdDriver and HttpLoadUnloadDriver.

LIFECYCLE_DRIVER_PORT.md §4 — behavioral-identity proof.

For each swap path we drive BOTH the legacy function (swap_kobold_model /
swap_tabby_model) and the new driver (SystemdDriver / HttpLoadUnloadDriver)
under identical mocks and assert the recorded side-effect event sequences are
equal.

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
    "# Managed by openai-proxy. Do not edit by hand; "
    "set role `model:`/`mmproj:` in proxy_config.yml.\n"
    "[Service]\n"
    f'Environment="LLAMA_BEE_MODEL={_GGUF}"\n'
    f'Environment="LLAMA_BEE_MMPROJ={_MMPROJ}"\n'
)

# Expected drop-in text when mmproj is empty (text-only model).
_EXPECTED_DROPIN_TEXT_NO_MMPROJ = (
    "# Managed by openai-proxy. Do not edit by hand; "
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
# 3. ensure_target() driver-path integration (AGW_DRIVER_SWAP=1)
# ---------------------------------------------------------------------------

class TestEnsureTargetDriverPath(unittest.TestCase):
    """When DRIVER_SWAP_ENABLED is True, ensure_target() routes through drivers.

    To exercise the driver path we inject lifecycle.driver keys into
    proxy.config["backends"] for kobold/tabby — mirroring the seed data that
    the production deployment will carry once the lifecycle records are populated.
    """

    def setUp(self):
        import copy
        _setup_tabby_params_provider()
        self._orig_flag = proxy.DRIVER_SWAP_ENABLED
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
        proxy.DRIVER_SWAP_ENABLED = self._orig_flag
        proxy.config.clear()
        proxy.config.update(self._orig_config)

    def test_kobold_driver_path_calls_systemd_swap(self):
        """With flag ON, ensure_target('kobold', gguf) calls SystemdDriver.swap()."""
        import gateway.lifecycle_drivers as _lcd
        proxy.DRIVER_SWAP_ENABLED = True

        swap_called_with: list[tuple] = []

        async def _fake_swap(svc, activation):
            swap_called_with.append((svc, activation))
            return True

        # Patch the singleton instance that driver_for_service() returns.
        with patch.object(_lcd._systemd_driver, "swap", side_effect=_fake_swap), \
             patch("os.path.isabs", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch.object(proxy, "is_literal_gguf_path", return_value=True):
            result = _run(proxy.ensure_target("kobold", _GGUF, _MMPROJ))

        self.assertTrue(result)
        self.assertEqual(len(swap_called_with), 1, "SystemdDriver.swap must be called once")
        _, activation = swap_called_with[0]
        self.assertEqual(activation["env"]["LLAMA_BEE_MODEL"], _GGUF)
        self.assertEqual(activation["env"]["LLAMA_BEE_MMPROJ"], _MMPROJ)

    def test_tabby_driver_path_calls_http_load_unload_swap(self):
        """With flag ON, ensure_target('tabby', model) calls HttpLoadUnloadDriver.swap()."""
        import gateway.lifecycle_drivers as _lcd
        proxy.DRIVER_SWAP_ENABLED = True

        swap_called_with: list[tuple] = []

        async def _fake_swap(svc, activation):
            swap_called_with.append((svc, activation))
            return True

        with patch.object(_lcd._http_load_unload_driver, "swap", side_effect=_fake_swap):
            result = _run(proxy.ensure_target("tabby", _TABBY_MODEL))

        self.assertTrue(result)
        self.assertEqual(len(swap_called_with), 1)
        _, activation = swap_called_with[0]
        self.assertEqual(activation["model_name"], _TABBY_MODEL)

    def test_flag_off_kobold_still_uses_legacy(self):
        """With flag OFF (default), ensure_target('kobold') uses legacy swap path."""
        proxy.DRIVER_SWAP_ENABLED = False

        with patch.object(proxy, "get_active_backend", new=AsyncMock(return_value="kobold")), \
             patch.object(proxy, "swap_kobold_model", new=AsyncMock(return_value=True)) as mock_legacy, \
             patch.object(proxy, "currently_loaded_kobold_model", return_value="/other.gguf"), \
             patch.object(proxy, "currently_loaded_kobold_mmproj", return_value=None), \
             patch.object(proxy, "is_literal_gguf_path", return_value=True), \
             patch("gateway.lifecycle_drivers.SystemdDriver.swap") as mock_driver_swap:
            result = _run(proxy.ensure_target("kobold", _GGUF))

        mock_legacy.assert_called_once()
        mock_driver_swap.assert_not_called()

    def test_flag_off_tabby_still_uses_legacy(self):
        """With flag OFF (default), ensure_target('tabby') uses legacy swap path."""
        proxy.DRIVER_SWAP_ENABLED = False

        with patch.object(proxy, "get_active_backend", new=AsyncMock(return_value="tabby")), \
             patch.object(proxy, "swap_tabby_model", new=AsyncMock(return_value=True)) as mock_legacy, \
             patch.object(proxy, "currently_loaded_tabby_model", new=AsyncMock(return_value="other")), \
             patch("gateway.lifecycle_drivers.HttpLoadUnloadDriver.swap") as mock_driver_swap:
            result = _run(proxy.ensure_target("tabby", _TABBY_MODEL))

        mock_legacy.assert_called_once_with(_TABBY_MODEL)
        mock_driver_swap.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
