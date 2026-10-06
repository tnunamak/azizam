"""Tests pinning the tier-2 registry migration behavior (2026-06-12).

Covers:
- _is_chat_capable_native_backend: filtering criteria
- _build_llm_backend_hosts (via LLM_BACKEND_HOSTS derivation)
- backend_for_host: registry fallback instead of literal "kobold"
- _highest_priority_chat_backend: priority ordering
- vision_capable_llm_backends: membership + ordering
- check_service_health: reads health_endpoint from config["backends"]
- Pinning: today's proxy_config.yml produces tabby+kobold in LLM_BACKEND_HOSTS
- Pinning: comfyui gets /system_stats, kobold gets /v1/models from seed

For imports, follow the pattern in test_proxy_gateway_acceptance.py:
import proxy at module level, then patch proxy.config inside tests.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.service_registry import (  # noqa: E402
    apply_service_registry_to_config,
    build_service_registry_seed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backends_config(**overrides: dict) -> dict:
    """Build a minimal config["backends"]-shaped dict for unit tests."""
    return {"backends": overrides}


def _make_tabby(
    *,
    enabled: bool = True,
    capabilities: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 5050,
    priority: int = 100,
) -> dict:
    return {
        "enabled": enabled,
        "capabilities": capabilities if capabilities is not None else ["chat", "completions", "vision"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/v1/models",
    }


def _make_kobold(
    *,
    enabled: bool = True,
    capabilities: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 5051,
    priority: int = 100,
) -> dict:
    return {
        "enabled": enabled,
        "capabilities": capabilities if capabilities is not None else ["openai", "chat", "completions"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/v1/models",
    }


def _make_litellm() -> dict:
    return {
        "enabled": True,
        "capabilities": ["llm.execution"],
        "execution_substrate": "litellm",
        "host": None,
        "port": None,
        "priority": 10,
    }


# ---------------------------------------------------------------------------
# _is_chat_capable_native_backend
# ---------------------------------------------------------------------------

class TestIsChatCapableNativeBackend(unittest.TestCase):
    """Unit tests for the filter predicate; no config patching needed."""

    def _call(self, backend_id: str, cfg: dict) -> bool:
        return proxy._is_chat_capable_native_backend(backend_id, cfg)

    def test_tabby_chat_completions_vision_is_true(self) -> None:
        self.assertTrue(self._call("tabby", _make_tabby()))

    def test_kobold_chat_completions_is_true(self) -> None:
        self.assertTrue(self._call("kobold", _make_kobold()))

    def test_disabled_backend_is_false(self) -> None:
        self.assertFalse(self._call("tabby", _make_tabby(enabled=False)))

    def test_backend_without_chat_or_completions_is_false(self) -> None:
        cfg = {"enabled": True, "capabilities": ["audio", "speech"], "port": 8001}
        self.assertFalse(self._call("tts", cfg))

    def test_completions_only_is_sufficient(self) -> None:
        cfg = {"enabled": True, "capabilities": ["completions"], "port": 9000}
        self.assertTrue(self._call("mybackend", cfg))

    def test_chat_only_is_sufficient(self) -> None:
        cfg = {"enabled": True, "capabilities": ["chat"], "port": 9001}
        self.assertTrue(self._call("mybackend", cfg))

    def test_litellm_by_execution_substrate_is_false(self) -> None:
        self.assertFalse(self._call("litellm", _make_litellm()))

    def test_litellm_by_kind_llm_substrate_is_false(self) -> None:
        cfg = {"enabled": True, "capabilities": ["chat", "completions"], "kind": "llm-substrate", "port": 4011}
        self.assertFalse(self._call("substrate", cfg))

    def test_litellm_by_kind_litellm_is_false(self) -> None:
        cfg = {"enabled": True, "capabilities": ["chat"], "kind": "litellm", "port": 4011}
        self.assertFalse(self._call("sub2", cfg))

    def test_litellm_by_id_name_is_false(self) -> None:
        cfg = {"enabled": True, "capabilities": ["chat", "completions"], "port": 4011}
        self.assertFalse(self._call("litellm", cfg))

    def test_enabled_absent_defaults_to_true(self) -> None:
        cfg = {"capabilities": ["chat"], "port": 9002}
        self.assertTrue(self._call("mybackend", cfg))

    def test_embeddings_only_is_false(self) -> None:
        cfg = {"enabled": True, "capabilities": ["openai", "embeddings"], "port": 5061}
        self.assertFalse(self._call("embeddings", cfg))


# ---------------------------------------------------------------------------
# LLM_BACKEND_HOSTS derivation (_build_llm_backend_hosts)
# ---------------------------------------------------------------------------

class TestBuildLlmBackendHosts(unittest.TestCase):
    """Verify the {id: url} derivation from config["backends"]."""

    def _build(self, backends: dict) -> dict:
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            return proxy._build_llm_backend_hosts()

    def test_tabby_and_kobold_included(self) -> None:
        result = self._build({"tabby": _make_tabby(), "kobold": _make_kobold()})
        self.assertIn("tabby", result)
        self.assertIn("kobold", result)
        self.assertEqual(result["tabby"], "http://127.0.0.1:5050")
        self.assertEqual(result["kobold"], "http://127.0.0.1:5051")

    def test_litellm_excluded(self) -> None:
        result = self._build({"tabby": _make_tabby(), "litellm": _make_litellm()})
        self.assertNotIn("litellm", result)

    def test_tts_excluded(self) -> None:
        tts = {"enabled": True, "capabilities": ["audio", "speech"], "host": "127.0.0.1", "port": 8001}
        result = self._build({"tabby": _make_tabby(), "tts": tts})
        self.assertNotIn("tts", result)

    def test_disabled_backend_excluded(self) -> None:
        result = self._build({"tabby": _make_tabby(enabled=False), "kobold": _make_kobold()})
        self.assertNotIn("tabby", result)
        self.assertIn("kobold", result)

    def test_backend_without_port_excluded(self) -> None:
        no_port = {"enabled": True, "capabilities": ["chat", "completions"], "host": "127.0.0.1"}
        result = self._build({"noport": no_port})
        self.assertNotIn("noport", result)

    def test_url_format(self) -> None:
        cfg = {"enabled": True, "capabilities": ["chat"], "host": "192.168.1.10", "port": 9000}
        result = self._build({"custom": cfg})
        self.assertEqual(result["custom"], "http://192.168.1.10:9000")

    def test_empty_backends_returns_empty(self) -> None:
        result = self._build({})
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _highest_priority_chat_backend
# ---------------------------------------------------------------------------

class TestHighestPriorityChatBackend(unittest.TestCase):
    """Verify priority-based selection."""

    def _call(self, backends: dict) -> str | None:
        # Rebuild LLM_BACKEND_HOSTS from the test backends, then call the function.
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            # Re-derive LLM_BACKEND_HOSTS for the patched config.
            hosts = proxy._build_llm_backend_hosts()
            with patch.object(proxy, "LLM_BACKEND_HOSTS", hosts):
                return proxy._highest_priority_chat_backend()

    def test_single_backend_returned(self) -> None:
        self.assertEqual(self._call({"kobold": _make_kobold()}), "kobold")

    def test_lower_priority_number_wins(self) -> None:
        backends = {
            "tabby": _make_tabby(priority=50),
            "kobold": _make_kobold(priority=100),
        }
        self.assertEqual(self._call(backends), "tabby")

    def test_equal_priority_deterministic(self) -> None:
        # Both at 100: one of them is returned (not None), test is stable.
        backends = {
            "tabby": _make_tabby(priority=100),
            "kobold": _make_kobold(priority=100),
        }
        result = self._call(backends)
        self.assertIn(result, ("tabby", "kobold"))

    def test_empty_hosts_returns_none(self) -> None:
        result = self._call({})
        self.assertIsNone(result)

    def test_second_backend_wins_when_first_has_higher_number(self) -> None:
        backends = {
            "alpha": {**_make_kobold(priority=200), "port": 9000},
            "beta": {**_make_kobold(priority=10), "port": 9001},
        }
        self.assertEqual(self._call(backends), "beta")


# ---------------------------------------------------------------------------
# vision_capable_llm_backends
# ---------------------------------------------------------------------------

class TestVisionCapableLlmBackends(unittest.TestCase):
    """vision_capable_llm_backends returns only backends with 'vision' cap, ordered."""

    def _call(self, backends: dict) -> list[str]:
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            hosts = proxy._build_llm_backend_hosts()
            with patch.object(proxy, "LLM_BACKEND_HOSTS", hosts):
                return proxy.vision_capable_llm_backends()

    def test_tabby_with_vision_included(self) -> None:
        result = self._call({"tabby": _make_tabby(capabilities=["chat", "completions", "vision"])})
        self.assertIn("tabby", result)

    def test_kobold_without_vision_excluded(self) -> None:
        result = self._call({"kobold": _make_kobold(capabilities=["chat", "completions"])})
        self.assertNotIn("kobold", result)

    def test_priority_ordering(self) -> None:
        backends = {
            "beta": {**_make_tabby(capabilities=["chat", "completions", "vision"], priority=200), "port": 5052},
            "alpha": {**_make_tabby(capabilities=["chat", "completions", "vision"], priority=50), "port": 5053},
        }
        result = self._call(backends)
        self.assertEqual(result, ["alpha", "beta"])

    def test_empty_when_no_vision_capable(self) -> None:
        result = self._call({"kobold": _make_kobold(capabilities=["chat", "completions"])})
        self.assertEqual(result, [])

    def test_today_config_tabby_is_vision_capable(self) -> None:
        """Pin: today's config has tabby with vision, kobold without."""
        backends = {
            "tabby": _make_tabby(capabilities=["chat", "completions", "vision"]),
            "kobold": _make_kobold(capabilities=["openai", "chat", "completions"]),
        }
        result = self._call(backends)
        self.assertIn("tabby", result)
        self.assertNotIn("kobold", result)


# ---------------------------------------------------------------------------
# backend_for_host
# ---------------------------------------------------------------------------

class TestBackendForHost(unittest.TestCase):
    """backend_for_host returns the service id for a known host URL, else None."""

    def _call(self, host: str, llm_hosts: dict) -> str | None:
        with patch.object(proxy, "LLM_BACKEND_HOSTS", llm_hosts):
            return proxy.backend_for_host(host)

    def test_tabby_host_returns_tabby(self) -> None:
        hosts = {"tabby": "http://127.0.0.1:5050", "kobold": "http://127.0.0.1:5051"}
        self.assertEqual(self._call("http://127.0.0.1:5050", hosts), "tabby")

    def test_kobold_host_returns_kobold(self) -> None:
        hosts = {"tabby": "http://127.0.0.1:5050", "kobold": "http://127.0.0.1:5051"}
        self.assertEqual(self._call("http://127.0.0.1:5051", hosts), "kobold")

    def test_unknown_host_returns_none(self) -> None:
        hosts = {"tabby": "http://127.0.0.1:5050"}
        result = self._call("http://127.0.0.1:9999", hosts)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# check_service_health
# ---------------------------------------------------------------------------

class TestCheckServiceHealth(unittest.TestCase):
    """check_service_health reads host/port/health_endpoint from config["backends"]."""

    def _run(self, coro):
        # Use asyncio.run() so we always get a fresh event loop; avoids
        # "There is no current event loop" errors when the full suite runs
        # after anyio-based tests that close the default loop.
        return asyncio.run(coro)

    def test_returns_false_for_unknown_service(self) -> None:
        with patch.dict(proxy.config, {"backends": {}}, clear=False):
            result = self._run(proxy.check_service_health("nonexistent"))
        self.assertFalse(result)

    def test_returns_false_when_port_missing(self) -> None:
        backends = {"myservice": {"host": "127.0.0.1", "health_endpoint": "/health"}}
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            result = self._run(proxy.check_service_health("myservice"))
        self.assertFalse(result)

    def test_uses_health_endpoint_from_config(self) -> None:
        """Verify the URL constructed matches health_endpoint from config."""
        backends = {
            "comfyui": {
                "host": "127.0.0.1",
                "port": 7890,
                "health_endpoint": "/system_stats",
            }
        }
        captured_urls = []

        class _FakeResponse:
            status_code = 200

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                captured_urls.append(url)
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                result = self._run(proxy.check_service_health("comfyui"))

        self.assertTrue(result)
        self.assertEqual(captured_urls, ["http://127.0.0.1:7890/system_stats"])

    def test_uses_health_endpoint_kobold(self) -> None:
        """kobold's health_endpoint is /v1/models (not the old /api/v1/model default)."""
        backends = {
            "kobold": {
                "host": "127.0.0.1",
                "port": 5051,
                "health_endpoint": "/v1/models",
            }
        }
        captured_urls = []

        class _FakeResponse:
            status_code = 200

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                captured_urls.append(url)
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                result = self._run(proxy.check_service_health("kobold"))

        self.assertTrue(result)
        self.assertEqual(captured_urls, ["http://127.0.0.1:5051/v1/models"])

    def test_returns_false_on_http_error_status(self) -> None:
        backends = {
            "myservice": {"host": "127.0.0.1", "port": 9000, "health_endpoint": "/health"}
        }

        class _FakeResponse:
            status_code = 503

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                result = self._run(proxy.check_service_health("myservice"))

        self.assertFalse(result)

    def test_runtime_fact_maps_degraded_status(self) -> None:
        backends = {
            "myservice": {"host": "127.0.0.1", "port": 9000, "health_endpoint": "/health"}
        }

        class _FakeResponse:
            status_code = 503

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                result = self._run(proxy._service_resource_runtime_fact("myservice"))

        self.assertEqual(result, {"status": "degraded"})

    def test_runtime_fact_uses_registry_only_service_base_url(self) -> None:
        registry = {
            "services": {
                "substrate": {
                    "id": "substrate",
                    "name": "Substrate",
                    "enabled": True,
                    "config": {
                        "default_base_url": "http://127.0.0.1:4011",
                        "health_endpoint": "/health/liveliness",
                    },
                }
            }
        }
        captured_urls = []

        class _FakeResponse:
            status_code = 200

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                captured_urls.append(url)
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": {}}, clear=False):
            with patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
                with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                    result = self._run(proxy._service_resource_runtime_fact("substrate"))

        self.assertEqual(result, {"status": "available"})
        self.assertEqual(captured_urls, ["http://127.0.0.1:4011/health/liveliness"])

    def test_runtime_fact_env_base_url_overrides_registry_default(self) -> None:
        registry = {
            "services": {
                "substrate": {
                    "id": "substrate",
                    "name": "Substrate",
                    "enabled": True,
                    "config": {
                        "base_url_env": "AZIZAM_TEST_SUBSTRATE_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011",
                        "health_endpoint": "ready",
                    },
                }
            }
        }
        captured_urls = []

        class _FakeResponse:
            status_code = 200

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                captured_urls.append(url)
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": {}}, clear=False):
            with patch.dict("os.environ", {"AZIZAM_TEST_SUBSTRATE_BASE_URL": "http://127.0.0.1:4999"}, clear=False):
                with patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
                    with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                        result = self._run(proxy._service_resource_runtime_fact("substrate"))

        self.assertEqual(result, {"status": "available"})
        self.assertEqual(captured_urls, ["http://127.0.0.1:4999/ready"])

    def test_gateway_resource_lister_enriches_registry_with_runtime_status(self) -> None:
        registry = {
            "services": {
                "svc-a": {
                    "id": "svc-a",
                    "name": "Service A",
                    "kind": "provider",
                    "enabled": True,
                    "capabilities": ["chat"],
                }
            }
        }

        async def fake_fact(service_id: str) -> dict[str, str]:
            self.assertEqual(service_id, "svc-a")
            return {"status": "available"}

        with patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=registry):
            with patch.object(proxy, "_service_resource_runtime_fact", side_effect=fake_fact):
                result = self._run(proxy._gateway_resource_lister(audience="client"))

        self.assertEqual(result[0]["id"], "svc-a")
        self.assertEqual(result[0]["status"], "available")

    def test_returns_false_on_connection_error(self) -> None:
        backends = {
            "myservice": {"host": "127.0.0.1", "port": 9001, "health_endpoint": "/health"}
        }

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                raise ConnectionRefusedError("refused")

        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                result = self._run(proxy.check_service_health("myservice"))

        self.assertFalse(result)

    def test_defaults_to_health_endpoint_when_absent(self) -> None:
        backends = {"myservice": {"host": "127.0.0.1", "port": 9002}}
        captured_urls = []

        class _FakeResponse:
            status_code = 200

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, timeout=None):
                captured_urls.append(url)
                return _FakeResponse()

        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            with patch("proxy.httpx.AsyncClient", return_value=_FakeClient()):
                self._run(proxy.check_service_health("myservice"))

        self.assertEqual(captured_urls, ["http://127.0.0.1:9002/health"])


# ---------------------------------------------------------------------------
# Seed pinning: proxy_config.yml → projected config["backends"]
# ---------------------------------------------------------------------------

class TestSeedPinning(unittest.TestCase):
    """Verify that the live proxy_config.yml seeds the exact health_endpoints
    and capabilities that the rest of the code relies on.

    These are regression tests — they fail if proxy_config.yml drifts away
    from what the code expects.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cfg_path = ROOT / "proxy_config.yml"
        with open(cfg_path, encoding="utf-8") as f:
            pconf = yaml.safe_load(f)
        registry = build_service_registry_seed(pconf)
        projected = apply_service_registry_to_config(pconf, registry)
        cls.backends: dict = projected.get("backends", {})

    def test_kobold_health_endpoint_is_v1_models(self) -> None:
        """kobold uses /v1/models not the old /api/v1/model default."""
        self.assertEqual(self.backends["kobold"]["health_endpoint"], "/v1/models")

    def test_comfyui_health_endpoint_is_system_stats(self) -> None:
        self.assertEqual(self.backends["comfyui"]["health_endpoint"], "/system_stats")

    def test_tabby_health_endpoint_is_v1_models(self) -> None:
        self.assertEqual(self.backends["tabby"]["health_endpoint"], "/v1/models")

    def test_tts_health_endpoint_is_health(self) -> None:
        self.assertEqual(self.backends["tts"]["health_endpoint"], "/health")

    def test_stt_health_endpoint_is_health(self) -> None:
        self.assertEqual(self.backends["stt"]["health_endpoint"], "/health")

    def test_embeddings_health_endpoint_is_health(self) -> None:
        self.assertEqual(self.backends["embeddings"]["health_endpoint"], "/health")

    def test_tabby_is_chat_capable(self) -> None:
        caps = self.backends["tabby"]["capabilities"]
        cap_set = {str(c).lower() for c in caps}
        self.assertTrue(cap_set & {"chat", "completions"})

    def test_kobold_is_chat_capable(self) -> None:
        caps = self.backends["kobold"]["capabilities"]
        cap_set = {str(c).lower() for c in caps}
        self.assertTrue(cap_set & {"chat", "completions"})

    def test_tabby_has_vision_capability(self) -> None:
        caps = self.backends["tabby"]["capabilities"]
        cap_set = {str(c).lower() for c in caps}
        self.assertIn("vision", cap_set)

    def test_kobold_does_not_have_vision_capability(self) -> None:
        caps = self.backends["kobold"]["capabilities"]
        cap_set = {str(c).lower() for c in caps}
        self.assertNotIn("vision", cap_set)

    def test_litellm_excluded_from_llm_backend_hosts(self) -> None:
        """litellm execution substrate must never appear in LLM_BACKEND_HOSTS."""
        hosts = proxy._build_llm_backend_hosts.__wrapped__ if hasattr(proxy._build_llm_backend_hosts, "__wrapped__") else proxy._build_llm_backend_hosts
        with patch.dict(proxy.config, {"backends": self.backends}, clear=False):
            result = proxy._build_llm_backend_hosts()
        self.assertNotIn("litellm", result)

    def test_tabby_and_kobold_in_llm_backend_hosts(self) -> None:
        with patch.dict(proxy.config, {"backends": self.backends}, clear=False):
            result = proxy._build_llm_backend_hosts()
        self.assertIn("tabby", result)
        self.assertIn("kobold", result)

    def test_llm_backend_hosts_excludes_tts_stt_embeddings_comfyui(self) -> None:
        with patch.dict(proxy.config, {"backends": self.backends}, clear=False):
            result = proxy._build_llm_backend_hosts()
        for excluded in ("tts", "stt", "embeddings", "comfyui"):
            self.assertNotIn(excluded, result, f"{excluded} should not be in LLM_BACKEND_HOSTS")

    def test_today_vision_backends_is_tabby_only(self) -> None:
        """For today's deployment, only tabby is vision-capable."""
        with patch.dict(proxy.config, {"backends": self.backends}, clear=False):
            hosts = proxy._build_llm_backend_hosts()
            with patch.object(proxy, "LLM_BACKEND_HOSTS", hosts):
                result = proxy.vision_capable_llm_backends()
        self.assertIn("tabby", result)
        self.assertNotIn("kobold", result)


if __name__ == "__main__":
    unittest.main()
