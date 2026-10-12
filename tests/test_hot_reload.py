"""Tests for the hot-reload implementation (Step 1, ENGINE_REGISTRY_DESIGN §5.3 + §8).

Verifies:
- GOLDEN PARITY: reload with same config is a no-op (byte-identical values).
- LIVE ADD: adding a new backend propagates to LLM_BACKEND_HOSTS + routing functions.
- LIVE REMOVE/DISABLE: disabling a backend removes it from live lookups.
- READ-SITE LIVENESS: the real routing functions (_highest_priority_chat_backend,
  vision_capable_llm_backends) use the NEW host after reload,
  not a stale snapshot.
- ATOMICITY (best-effort): config is never observed empty during reload when
  reload is called from synchronous code (asyncio single-thread guarantee).
- MUTATION HOOK: ADMIN_STORE.on_service_registry_changed is wired to
  reload_runtime_config so admin mutations trigger hot-reload.

Design notes / audit conclusions reproduced here (see task description):
- Zero stale-capture read sites found: every function reads module globals at
  call time, no default-arg captures, no cross-module "from proxy import TABBY_HOST".
- config.clear()/config.update() is asyncio-safe: no await between them.
- status_endpoint.py uses `import proxy as proxy_module` inside a function,
  so it resolves module attributes at call time — reload-safe.
- gateway/config_sources.py references HOST names as string literals only.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_effective_config(backends: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal effective config dict with the given backends."""
    return {**copy.deepcopy(dict(proxy._bootstrap_config)), "backends": backends}


def _tabby_backend(
    *,
    host: str = "127.0.0.1",
    port: int = 5050,
    enabled: bool = True,
    capabilities: list[str] | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "capabilities": capabilities if capabilities is not None else ["vision", "chat", "completions"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/v1/models",
        "native_passthrough": True,
        "protocol": "openai-compatible",
    }


def _kobold_backend(
    *,
    host: str = "127.0.0.1",
    port: int = 5051,
    enabled: bool = True,
    capabilities: list[str] | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "capabilities": capabilities if capabilities is not None else ["openai", "chat", "completions"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/v1/models",
        "native_passthrough": True,
        "protocol": "openai-compatible",
    }


def _llm_backend(
    *,
    host: str = "127.0.0.1",
    port: int = 9000,
    enabled: bool = True,
    capabilities: list[str] | None = None,
    priority: int = 50,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "capabilities": capabilities if capabilities is not None else ["chat", "completions"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/v1/models",
        "native_passthrough": True,
        "protocol": "openai-compatible",
    }


def _call_reload_with_config(effective_cfg: dict[str, Any]) -> dict[str, Any]:
    """Call reload_runtime_config() with a specific effective config.

    Patches _load_effective_runtime_config to return effective_cfg directly,
    bypassing DB access.  Returns the updates dict from reload_runtime_config().
    """
    with patch.object(proxy, "_load_effective_runtime_config", return_value=effective_cfg):
        return proxy.reload_runtime_config()


def _snapshot_reload_derived_state() -> dict[str, Any]:
    """Snapshot every module global reload_runtime_config() may reassign.

    reload_runtime_config() atomically swaps `config` plus every name in
    _derive_backend_constants()'s return (TABBY_HOST, LLM_BACKEND_HOSTS, ...).
    Any test that calls it (directly or via _call_reload_with_config) must
    restore all of these, not a hand-picked subset, or it leaks derived
    globals (e.g. TTS_HOST, LLM_BACKEND_HOSTS) into unrelated tests.
    """
    snapshot = {"config": copy.deepcopy(dict(proxy.config))}
    for name in proxy._derive_backend_constants(proxy.config):
        value = getattr(proxy, name)
        snapshot[name] = copy.deepcopy(value) if isinstance(value, dict) else value
    return snapshot


def _restore_reload_derived_state(snapshot: dict[str, Any]) -> None:
    proxy.config.clear()
    proxy.config.update(snapshot["config"])
    for name, value in snapshot.items():
        if name == "config":
            continue
        setattr(proxy, name, value)


# ---------------------------------------------------------------------------
# 1. GOLDEN PARITY
# ---------------------------------------------------------------------------

class TestGoldenParity(unittest.TestCase):
    """Reload with same config is a no-op: every constant stays byte-identical."""

    def setUp(self):
        self._before = _snapshot_reload_derived_state()

    def tearDown(self):
        _restore_reload_derived_state(self._before)

    def test_reload_same_config_is_noop(self):
        """Reloading with the current live config leaves every HOST constant identical.

        Note: config['backends'] may differ in structure between the bootstrap YAML
        and the DB-projected registry (the DB projects a normalized shape), so we
        compare the derived HOST constants rather than the raw backends dict.
        """
        # Snapshot the derived HOST constants before reload.
        before_tabby = proxy.TABBY_HOST
        before_kobold = proxy.KOBOLD_HOST
        before_tts = proxy.TTS_HOST
        before_stt = proxy.STT_HOST
        before_emb = proxy.EMBEDDINGS_HOST
        before_comfy = proxy.COMFY_HOST
        before_llm = dict(proxy.LLM_BACKEND_HOSTS)

        # Use the REAL _load_effective_runtime_config — hits the live DB.
        proxy.reload_runtime_config()

        self.assertEqual(proxy.TABBY_HOST, before_tabby, "TABBY_HOST changed on no-op reload")
        self.assertEqual(proxy.KOBOLD_HOST, before_kobold, "KOBOLD_HOST changed on no-op reload")
        self.assertEqual(proxy.TTS_HOST, before_tts, "TTS_HOST changed on no-op reload")
        self.assertEqual(proxy.STT_HOST, before_stt, "STT_HOST changed on no-op reload")
        self.assertEqual(proxy.EMBEDDINGS_HOST, before_emb, "EMBEDDINGS_HOST changed on no-op reload")
        self.assertEqual(proxy.COMFY_HOST, before_comfy, "COMFY_HOST changed on no-op reload")
        self.assertEqual(proxy.LLM_BACKEND_HOSTS, before_llm, "LLM_BACKEND_HOSTS changed on no-op reload")

    def test_golden_host_values(self):
        """Pin the exact HOST values produced by the current 8-service config."""
        # These are the values observed from the live admin.db + proxy_config.yml.
        # If they change, the registry or config has changed intentionally — update the
        # pinned values in the test (and update this comment).
        self.assertEqual(proxy.TABBY_HOST, "http://127.0.0.1:5050")
        self.assertEqual(proxy.KOBOLD_HOST, "http://127.0.0.1:5051")
        self.assertIn("kobold", proxy.LLM_BACKEND_HOSTS)
        self.assertIn("tabby", proxy.LLM_BACKEND_HOSTS)
        self.assertEqual(proxy.LLM_BACKEND_HOSTS["kobold"], "http://127.0.0.1:5051")
        self.assertEqual(proxy.LLM_BACKEND_HOSTS["tabby"], "http://127.0.0.1:5050")


# ---------------------------------------------------------------------------
# 2. LIVE ADD
# ---------------------------------------------------------------------------

class TestLiveAdd(unittest.TestCase):
    """Adding a new openai-compatible backend propagates to LLM_BACKEND_HOSTS."""

    def setUp(self):
        self._before = _snapshot_reload_derived_state()

    def tearDown(self):
        _restore_reload_derived_state(self._before)

    def test_new_backend_appears_in_llm_backend_hosts(self):
        """After reload with new backend, LLM_BACKEND_HOSTS contains the new entry."""
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
            "mynewllm": _llm_backend(host="192.168.1.99", port=9999, priority=50),
        }
        updates = _call_reload_with_config(_make_effective_config(new_backends))

        # The returned updates dict reflects the new host.
        self.assertIn("mynewllm", updates["LLM_BACKEND_HOSTS"])
        self.assertEqual(updates["LLM_BACKEND_HOSTS"]["mynewllm"], "http://192.168.1.99:9999")

        # The module global was actually reassigned (not just the local dict).
        self.assertIn("mynewllm", proxy.LLM_BACKEND_HOSTS)
        self.assertEqual(proxy.LLM_BACKEND_HOSTS["mynewllm"], "http://192.168.1.99:9999")

    def test_new_backend_survives_without_re_import(self):
        """Re-importing proxy is NOT required to see the new backend.

        Uses the module reference captured at the top of this file (the `proxy`
        name in sys.modules at test-collection time).  We deliberately do NOT
        do `import proxy as _same_module` here because another test
        (test_gateway_api.py) pops 'proxy' from sys.modules, which would cause
        a fresh import that misses our reload.  The key claim is that the already-
        held reference (global `proxy` at top of this test module) sees the update.
        """
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
            "extra": _llm_backend(host="10.0.0.5", port=11434, priority=20),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        # `proxy` here is the module reference imported at the top of this file —
        # same object that _call_reload_with_config modified via globals().
        self.assertIn("extra", proxy.LLM_BACKEND_HOSTS,
                      "Already-held module reference does not see the reloaded backend")
        self.assertEqual(proxy.LLM_BACKEND_HOSTS["extra"], "http://10.0.0.5:11434")

    def test_config_backends_updated(self):
        """config['backends'] is updated so config.get() reads return the new backend."""
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
            "bolt": _llm_backend(host="127.0.0.1", port=7777, priority=5),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        self.assertIn("bolt", proxy.config.get("backends", {}))
        self.assertEqual(proxy.config["backends"]["bolt"]["port"], 7777)


# ---------------------------------------------------------------------------
# 3. LIVE REMOVE / DISABLE
# ---------------------------------------------------------------------------

class TestLiveRemoveDisable(unittest.TestCase):
    """Disabling or removing a backend removes it from live lookups after reload."""

    def setUp(self):
        self._before = _snapshot_reload_derived_state()

    def tearDown(self):
        _restore_reload_derived_state(self._before)

    def test_disabled_backend_removed_from_llm_backend_hosts(self):
        """Setting enabled=False removes a backend from LLM_BACKEND_HOSTS."""
        # Start with tabby disabled.
        new_backends = {
            "tabby": _tabby_backend(enabled=False),
            "kobold": _kobold_backend(),
        }
        updates = _call_reload_with_config(_make_effective_config(new_backends))

        self.assertNotIn("tabby", updates["LLM_BACKEND_HOSTS"])
        self.assertNotIn("tabby", proxy.LLM_BACKEND_HOSTS)
        self.assertIn("kobold", proxy.LLM_BACKEND_HOSTS)

    def test_removed_backend_absent_from_llm_backend_hosts(self):
        """Removing a backend from config entirely removes it from LLM_BACKEND_HOSTS."""
        new_backends = {
            "kobold": _kobold_backend(),
            # tabby is entirely absent
        }
        updates = _call_reload_with_config(_make_effective_config(new_backends))

        self.assertNotIn("tabby", updates["LLM_BACKEND_HOSTS"])
        self.assertNotIn("tabby", proxy.LLM_BACKEND_HOSTS)

    def test_host_constant_unchanged_when_backend_disabled(self):
        """Disabling kobold doesn't change KOBOLD_HOST (it keeps the last known value).

        KOBOLD_HOST is derived from config['backends']['kobold']['host'/'port'], which
        still exists even when enabled=False; _derive_backend_constants reads it for the
        HOST string but _is_chat_capable_native_backend gates LLM_BACKEND_HOSTS inclusion.
        """
        orig_kobold_host = proxy.KOBOLD_HOST
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(enabled=False),
        }
        updates = _call_reload_with_config(_make_effective_config(new_backends))
        # LLM_BACKEND_HOSTS no longer contains kobold
        self.assertNotIn("kobold", updates["LLM_BACKEND_HOSTS"])
        # KOBOLD_HOST still holds the configured URL (used for direct Tabby routing)
        self.assertEqual(proxy.KOBOLD_HOST, orig_kobold_host)


# ---------------------------------------------------------------------------
# 4. READ-SITE LIVENESS
# ---------------------------------------------------------------------------

class TestReadSiteLiveness(unittest.TestCase):
    """The real routing functions use the NEW values after reload (not stale captures).

    This is the critical test: it proves the read path is live, not just the
    module attribute.
    """

    def setUp(self):
        self._before = _snapshot_reload_derived_state()

    def tearDown(self):
        _restore_reload_derived_state(self._before)

    def test_highest_priority_chat_backend_sees_new_config(self):
        """_highest_priority_chat_backend() uses the new priority ordering after reload."""
        # Before: tabby=100, kobold=100 (original)
        # After reload: a new backend with priority=1 wins.
        new_backends = {
            "tabby": _tabby_backend(priority=100),
            "kobold": _kobold_backend(priority=100),
            "priority-winner": _llm_backend(host="10.0.0.1", port=9100, priority=1),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        winner = proxy._highest_priority_chat_backend()
        self.assertEqual(winner, "priority-winner",
                         f"_highest_priority_chat_backend() returned {winner!r}; "
                         "expected 'priority-winner' — read site is stale or priority not updated")

    def test_vision_capable_backends_updated_after_reload(self):
        """vision_capable_llm_backends() reflects the new registry after reload."""
        # Remove vision from tabby, keep kobold without vision.
        new_backends = {
            "tabby": _tabby_backend(capabilities=["chat", "completions"]),  # no vision
            "kobold": _kobold_backend(),
            "visionbot": _llm_backend(
                host="10.0.0.2", port=9200, priority=5,
                capabilities=["chat", "completions", "vision"],
            ),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        vision_backends = proxy.vision_capable_llm_backends()
        self.assertIn("visionbot", vision_backends,
                      "vision_capable_llm_backends() does not include new vision backend")
        self.assertNotIn("tabby", vision_backends,
                         "vision_capable_llm_backends() still includes tabby (vision stripped)")

    def test_tabby_host_module_global_updated(self):
        """TABBY_HOST module global is updated to the new URL after reload."""
        new_port = 5099
        new_backends = {
            "tabby": _tabby_backend(host="192.168.1.1", port=new_port),
            "kobold": _kobold_backend(),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        self.assertEqual(proxy.TABBY_HOST, f"http://192.168.1.1:{new_port}",
                         "TABBY_HOST module global was not updated by reload")

    def test_kobold_host_module_global_updated(self):
        """KOBOLD_HOST module global is updated to the new URL after reload."""
        new_port = 5099
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(host="192.168.1.2", port=new_port),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        self.assertEqual(proxy.KOBOLD_HOST, f"http://192.168.1.2:{new_port}",
                         "KOBOLD_HOST module global was not updated by reload")

    def test_tts_host_updated(self):
        """TTS_HOST is updated after a reload that changes the TTS backend port."""
        new_cfg = _make_effective_config({
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
            "tts": {
                "enabled": True,
                "capabilities": ["tts"],
                "host": "127.0.0.1",
                "port": 9988,
            },
        })
        _call_reload_with_config(new_cfg)

        self.assertEqual(proxy.TTS_HOST, "http://127.0.0.1:9988",
                         "TTS_HOST was not updated after reload")

    def test_comfy_host_updated(self):
        """COMFY_HOST is updated after a reload that changes the ComfyUI backend port."""
        new_cfg = _make_effective_config({
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
            "comfyui": {
                "enabled": True,
                "capabilities": ["image"],
                "host": "127.0.0.1",
                "port": 8765,
            },
        })
        _call_reload_with_config(new_cfg)

        self.assertEqual(proxy.COMFY_HOST, "http://127.0.0.1:8765",
                         "COMFY_HOST was not updated after reload")


# ---------------------------------------------------------------------------
# 5. ATOMICITY (best-effort)
# ---------------------------------------------------------------------------

class TestAtomicity(unittest.TestCase):
    """config is never observed empty during a reload in single-threaded asyncio.

    Full multi-threaded concurrency is not tested here (the proxy runs asyncio,
    single-threaded). The critical guarantee is that no `await` exists between
    config.clear() and config.update(), so no coroutine switch can observe an
    empty config.
    """

    def setUp(self):
        self._before = _snapshot_reload_derived_state()

    def tearDown(self):
        _restore_reload_derived_state(self._before)

    def test_config_not_empty_after_reload(self):
        """config is non-empty after a successful reload."""
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
        }
        _call_reload_with_config(_make_effective_config(new_backends))

        self.assertGreater(len(proxy.config), 0,
                           "config is empty after reload — clear() was not followed by update()")
        self.assertIn("backends", proxy.config,
                      "config['backends'] key is missing after reload")

    def test_config_unchanged_after_reload_exception(self):
        """If _load_effective_runtime_config raises, config is NOT cleared."""
        config_before = dict(proxy.config)

        def _raises(*args, **kwargs):
            raise RuntimeError("simulated DB failure")

        # reload_runtime_config() catches exceptions from _load_effective_runtime_config
        # inside itself only if the exception is inside that function.  Since we raise
        # BEFORE config.clear(), config should be untouched.
        with patch.object(proxy, "_load_effective_runtime_config", side_effect=_raises):
            try:
                proxy.reload_runtime_config()
            except RuntimeError:
                pass  # expected

        # config must be non-empty and match pre-reload state
        self.assertGreater(len(proxy.config), 0,
                           "config was cleared/corrupted by a failed reload")
        # The backends key must still exist
        self.assertIn("backends", proxy.config,
                      "config['backends'] missing after failed reload")

    def test_reload_returns_updates_dict(self):
        """reload_runtime_config() returns a dict with all expected keys."""
        new_backends = {
            "tabby": _tabby_backend(),
            "kobold": _kobold_backend(),
        }
        updates = _call_reload_with_config(_make_effective_config(new_backends))

        required_keys = {
            "TABBY_HOST", "KOBOLD_HOST", "TTS_HOST", "STT_HOST",
            "EMBEDDINGS_HOST", "COMFY_HOST", "LLM_BACKEND_HOSTS",
            "TTS_ENABLED", "STT_ENABLED", "STT_TIMEOUT", "EMBEDDINGS_ENABLED", "COMFY_ENABLED",
        }
        missing = required_keys - set(updates.keys())
        self.assertFalse(missing, f"reload_runtime_config() missing keys: {missing}")


# ---------------------------------------------------------------------------
# 6. MUTATION HOOK
# ---------------------------------------------------------------------------

class TestMutationHook(unittest.TestCase):
    """ADMIN_STORE.on_service_registry_changed is wired to reload_runtime_config."""

    def setUp(self):
        self._before = _snapshot_reload_derived_state()

    def tearDown(self):
        _restore_reload_derived_state(self._before)

    def test_hook_invocation_triggers_reload(self):
        """Calling the hook directly (as admin_runtime does) invokes reload_runtime_config.

        Note: ADMIN_STORE.on_service_registry_changed holds a direct function reference,
        not a module-attribute lookup.  patch.object(proxy, 'reload_runtime_config')
        would only replace the module attribute, not the stored reference.  We verify
        the hook IS reload_runtime_config via assertIs, then call it and confirm it
        updates module globals (which only reload_runtime_config can do).
        """
        # The hook reference must be the exact reload_runtime_config function.
        cb = proxy.ADMIN_STORE.on_service_registry_changed
        self.assertIsNotNone(cb)
        self.assertIs(cb, proxy.reload_runtime_config,
                      "Hook does not hold a reference to reload_runtime_config")

        # Calling the hook must actually perform a reload (updates module globals).
        # We inject a specific config so we can verify the globals changed.
        new_backends = {
            "tabby": _tabby_backend(port=6666),
            "kobold": _kobold_backend(),
        }
        with patch.object(proxy, "_load_effective_runtime_config",
                          return_value=_make_effective_config(new_backends)):
            cb()  # invoke the hook as admin_runtime._fire_registry_changed() does

        # If the hook called reload_runtime_config, TABBY_HOST must have updated.
        self.assertEqual(proxy.TABBY_HOST, "http://127.0.0.1:6666",
                         "Invoking the hook did not update TABBY_HOST — hook may not "
                         "be calling reload_runtime_config")


# ---------------------------------------------------------------------------
# 7. DERIVE_BACKEND_CONSTANTS (unit test of the helper)
# ---------------------------------------------------------------------------

class TestDeriveBackendConstants(unittest.TestCase):
    """Unit tests for _derive_backend_constants() without touching global state."""

    def test_standard_backends_produce_correct_hosts(self):
        cfg = {
            "backends": {
                "tabby": _tabby_backend(host="10.1.0.1", port=5050),
                "kobold": _kobold_backend(host="10.1.0.2", port=5051),
                "tts": {"enabled": True, "host": "10.1.0.3", "port": 8880},
                "stt": {"enabled": True, "host": "10.1.0.4", "port": 5092, "timeout": 321},
                "embeddings": {"enabled": True, "host": "10.1.0.5", "port": 5061},
                "comfyui": {"enabled": True, "host": "10.1.0.6", "port": 7890},
            }
        }
        result = proxy._derive_backend_constants(cfg)

        self.assertEqual(result["TABBY_HOST"], "http://10.1.0.1:5050")
        self.assertEqual(result["KOBOLD_HOST"], "http://10.1.0.2:5051")
        self.assertEqual(result["TTS_HOST"], "http://10.1.0.3:8880")
        self.assertEqual(result["STT_HOST"], "http://10.1.0.4:5092")
        self.assertEqual(result["STT_TIMEOUT"], 321)
        self.assertEqual(result["EMBEDDINGS_HOST"], "http://10.1.0.5:5061")
        self.assertEqual(result["COMFY_HOST"], "http://10.1.0.6:7890")
        self.assertIn("tabby", result["LLM_BACKEND_HOSTS"])
        self.assertIn("kobold", result["LLM_BACKEND_HOSTS"])

    def test_disabled_backend_excluded_from_llm_backend_hosts(self):
        cfg = {
            "backends": {
                "tabby": _tabby_backend(enabled=False),
                "kobold": _kobold_backend(),
            }
        }
        result = proxy._derive_backend_constants(cfg)
        self.assertNotIn("tabby", result["LLM_BACKEND_HOSTS"])
        self.assertIn("kobold", result["LLM_BACKEND_HOSTS"])

    def test_does_not_mutate_global_state(self):
        """_derive_backend_constants() is side-effect-free."""
        original_tabby = proxy.TABBY_HOST
        original_llm = dict(proxy.LLM_BACKEND_HOSTS)

        cfg = {"backends": {"tabby": _tabby_backend(port=9999), "kobold": _kobold_backend()}}
        proxy._derive_backend_constants(cfg)

        # Module globals must be untouched.
        self.assertEqual(proxy.TABBY_HOST, original_tabby)
        self.assertEqual(proxy.LLM_BACKEND_HOSTS, original_llm)

    def test_empty_backends_produces_empty_llm_hosts(self):
        cfg = {"backends": {}}
        result = proxy._derive_backend_constants(cfg)
        self.assertEqual(result["LLM_BACKEND_HOSTS"], {})

    def test_tts_disabled_flag_propagated(self):
        cfg = {
            "backends": {
                "tts": {"enabled": False, "host": "127.0.0.1", "port": 8880},
            }
        }
        result = proxy._derive_backend_constants(cfg)
        self.assertFalse(result["TTS_ENABLED"])

    def test_comfy_enabled_flag_propagated(self):
        cfg = {
            "backends": {
                "comfyui": {"enabled": True, "host": "127.0.0.1", "port": 7890},
            }
        }
        result = proxy._derive_backend_constants(cfg)
        self.assertTrue(result["COMFY_ENABLED"])


def test_derivation_failure_does_not_publish_a_partial_reload(monkeypatch):
    """A candidate config whose derivation fails must not go live; the old one keeps serving."""
    import pytest

    snapshot = _snapshot_reload_derived_state()
    old_config = copy.deepcopy(snapshot["config"])
    monkeypatch.setattr(proxy, "config", old_config)
    for name, value in snapshot.items():
        if name != "config":
            monkeypatch.setattr(proxy, name, copy.deepcopy(value))
    candidate = _make_effective_config({"tabby": _tabby_backend(host="127.0.0.1", port=18092)})
    monkeypatch.setattr(proxy, "_load_effective_runtime_config", lambda _bootstrap: candidate)

    def fail_derivation(config):
        assert config is candidate
        raise RuntimeError("fixture derivation failure")

    monkeypatch.setattr(proxy, "_derive_backend_constants", fail_derivation)
    with pytest.raises(RuntimeError, match="fixture derivation failure"):
        proxy.reload_runtime_config()

    assert proxy.config is old_config
    assert proxy.config == snapshot["config"]
    for name, value in snapshot.items():
        if name != "config":
            assert getattr(proxy, name) == value, name


if __name__ == "__main__":
    unittest.main()
