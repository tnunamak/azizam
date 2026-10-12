"""Tests pinning role-model gate generalization (ENGINE_REGISTRY_DESIGN §2.3 + §8 Step 4).

Covers:
- _normalize_models_catalog: kobold entries with paths still parse identically
  (golden / byte-identical proof).
- _normalize_models_catalog: a NEW enabled backend's entry now parses (was dropped).
- _normalize_models_catalog: an UNKNOWN/disabled backend is still skipped.
- _normalize_models_catalog: a swap-backend entry without a path is still skipped.
- _build_role_catalog_entries: kobold roles identical to before.
- _build_role_catalog_entries: a new backend's role now lists.
- _build_role_catalog_entries: unknown backend role still skipped.
- _build_role_catalog_entries: remote role with no model field skipped (malformed).
- role-is-alarm-only pin: in the admin health projection a standby failure is
  still observed "down" but warns instead of alarming; the role field does NOT
  affect routing/resolution functions.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.admin_api import derive_admin_health_projection  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backends_with(*ids: str, **extra: dict) -> dict:
    """Return a config["backends"] dict containing the given backend ids."""
    out: dict[str, Any] = {}
    for bid in ids:
        out[bid] = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "health_endpoint": "/health",
            "capabilities": ["chat", "completions"],
        }
    out.update(extra)
    return out


def _swap_backends(*ids: str) -> dict:
    """Backends whose lifecycle driver swaps models, declared as service data."""
    out = _backends_with(*ids)
    for bid in ids:
        out[bid]["lifecycle"] = {"driver": "systemd", "driver_config": {"unit": f"{bid}.service"}}
    return out


def _normalize(raw_models: list[dict], backends: dict | None = None) -> dict:
    """Call _normalize_models_catalog under a patched config["backends"]."""
    with patch.dict(proxy.config, {"backends": backends or {}}):
        return proxy._normalize_models_catalog(raw_models)


def _role_entries(roles: dict, backends: dict | None = None) -> list[dict]:
    """Call _build_role_catalog_entries under patched ROLES + config["backends"]."""
    with (
        patch.dict(proxy.config, {"backends": backends or {}}),
        patch.object(proxy, "ROLES", roles),
        patch.object(proxy, "MODELS_CATALOG", {}),
        patch.object(proxy, "EMBEDDINGS_ENABLED", False),
    ):
        return proxy._build_role_catalog_entries()


# ---------------------------------------------------------------------------
# 1. _normalize_models_catalog
# ---------------------------------------------------------------------------

class TestNormalizeModelsCatalog(unittest.TestCase):
    """Gate-1: config["models"] GGUF alias catalog."""

    # --- Golden / byte-identical for kobold ----------------------------

    def test_kobold_entry_with_path_accepted(self) -> None:
        """kobold entry with a path parses exactly as before."""
        raw = [{"id": "my-kobold", "backend": "kobold", "path": "/models/q4.gguf"}]
        result = _normalize(raw, backends=_swap_backends("kobold"))
        self.assertIn("my-kobold", result)
        entry = result["my-kobold"]
        self.assertEqual(entry["backend"], "kobold")
        self.assertEqual(entry["path"], "/models/q4.gguf")
        self.assertIsNone(entry["mmproj"])

    def test_backend_absent_from_registry_is_skipped_whatever_its_name(self) -> None:
        """No backend name is special: one absent from config["backends"] is skipped."""
        raw = [{"id": "bare-kobold", "backend": "kobold", "path": "/models/q5.gguf"}]
        self.assertNotIn("bare-kobold", _normalize(raw, backends={}))

    def test_kobold_mmproj_preserved(self) -> None:
        raw = [
            {
                "id": "vision-kobold",
                "backend": "kobold",
                "path": "/models/vis.gguf",
                "mmproj": "/models/clip.gguf",
            }
        ]
        result = _normalize(raw, backends=_swap_backends("kobold"))
        self.assertEqual(result["vision-kobold"]["mmproj"], "/models/clip.gguf")

    # --- New backend now participates ----------------------------------------

    def test_new_enabled_backend_with_path_accepted(self) -> None:
        """A new enabled registry backend (e.g. vllm) now parses — was dropped before."""
        raw = [{"id": "coder", "backend": "my-vllm", "path": "/models/coder.gguf"}]
        backends = _backends_with("my-vllm")
        result = _normalize(raw, backends=backends)
        self.assertIn("coder", result)
        self.assertEqual(result["coder"]["backend"], "my-vllm")

    # --- Unknown/disabled backend still skipped ------------------------------

    def test_unknown_backend_skipped(self) -> None:
        """A backend not in config["backends"] is skipped."""
        raw = [{"id": "ghost", "backend": "ghost-backend", "path": "/models/ghost.gguf"}]
        result = _normalize(raw, backends={})  # ghost-backend not registered
        self.assertNotIn("ghost", result)

    def test_empty_id_skipped(self) -> None:
        raw = [{"id": "", "backend": "kobold", "path": "/models/q4.gguf"}]
        result = _normalize(raw, backends={})
        self.assertEqual(result, {})

    # --- Swap-backend entry without a path still skipped ---------------------

    def test_kobold_without_path_skipped(self) -> None:
        """kobold entry with no path is still skipped (path is mandatory)."""
        raw = [{"id": "pathless", "backend": "kobold"}]
        result = _normalize(raw, backends={})
        self.assertNotIn("pathless", result)

    def test_new_backend_without_path_skipped(self) -> None:
        """New backend with no path: still skipped (GGUF catalog requires path)."""
        raw = [{"id": "remote-no-path", "backend": "my-vllm"}]
        result = _normalize(raw, backends=_backends_with("my-vllm"))
        self.assertNotIn("remote-no-path", result)

    def test_non_list_input_returns_empty(self) -> None:
        self.assertEqual(_normalize(None), {})  # type: ignore[arg-type]
        self.assertEqual(_normalize({}), {})    # type: ignore[arg-type]

    def test_id_normalized_to_lowercase(self) -> None:
        raw = [{"id": "My-Kobold", "backend": "kobold", "path": "/models/q4.gguf"}]
        result = _normalize(raw, backends=_swap_backends("kobold"))
        self.assertIn("my-kobold", result)


# ---------------------------------------------------------------------------
# 2. _build_role_catalog_entries (ROLES gate)
# ---------------------------------------------------------------------------

class TestBuildRoleCatalogEntries(unittest.TestCase):
    """Gate-2: ROLES dict → /v1/models listing."""

    # --- Golden for kobold (byte-identical) ----------------------------

    def test_kobold_role_with_model_path_listed(self) -> None:
        """kobold role with model path produces azizam_model_path (unchanged)."""
        roles = {
            "default": {
                "backend": "kobold",
                "model": "/models/q4.gguf",
                "description": "Default model",
            }
        }
        entries = _role_entries(roles, backends=_swap_backends("kobold"))
        ids = {e["id"] for e in entries}
        self.assertIn("default", ids)
        entry = next(e for e in entries if e["id"] == "default")
        self.assertEqual(entry["azizam_backend"], "kobold")
        self.assertEqual(entry["azizam_model_path"], "/models/q4.gguf")
        self.assertTrue(entry["azizam_role"])

    def test_kobold_role_without_model_listed(self) -> None:
        """A local-swap role without a pinned model is still listed (unpinned backend role)."""
        roles = {"assistant": {"backend": "kobold"}}
        entries = _role_entries(roles, backends=_swap_backends("kobold"))
        ids = {e["id"] for e in entries}
        self.assertIn("assistant", ids)
        entry = next(e for e in entries if e["id"] == "assistant")
        self.assertNotIn("azizam_model_path", entry)
        self.assertEqual(entry["azizam_backend"], "kobold")

    # --- New backend now participates ----------------------------------------

    def test_new_backend_role_with_model_listed(self) -> None:
        """A new enabled registry backend role now appears — was silently dropped before."""
        roles = {
            "coder": {
                "backend": "my-vllm",
                "model": "meta-llama/Llama-3-8B-Instruct",
                "description": "Code assistant",
            }
        }
        entries = _role_entries(roles, backends=_backends_with("my-vllm"))
        ids = {e["id"] for e in entries}
        self.assertIn("coder", ids)
        entry = next(e for e in entries if e["id"] == "coder")
        self.assertEqual(entry["azizam_backend"], "my-vllm")
        self.assertEqual(entry["azizam_model"], "meta-llama/Llama-3-8B-Instruct")
        self.assertNotIn("azizam_model_path", entry)
        self.assertTrue(entry["azizam_role"])

    # --- Unknown backend still skipped ----------------------------------------

    def test_unknown_backend_role_skipped(self) -> None:
        """Role pointing at a backend not in the registry is silently skipped."""
        roles = {
            "ghost": {
                "backend": "ghost-backend",
                "model": "ghost-model",
            }
        }
        entries = _role_entries(roles, backends={})
        ids = {e["id"] for e in entries}
        self.assertNotIn("ghost", ids)

    def test_none_backend_role_skipped(self) -> None:
        roles = {"nobackend": {"model": "/models/q4.gguf"}}
        entries = _role_entries(roles, backends=_swap_backends("kobold"))
        ids = {e["id"] for e in entries}
        self.assertNotIn("nobackend", ids)

    # --- Remote role without model field skipped (malformed) ------------------

    def test_remote_role_without_model_skipped(self) -> None:
        """Remote backend role with no model name is skipped — nothing to route to."""
        roles = {"no-model-remote": {"backend": "my-vllm"}}
        entries = _role_entries(roles, backends=_backends_with("my-vllm"))
        ids = {e["id"] for e in entries}
        self.assertNotIn("no-model-remote", ids)

    def test_remote_role_with_empty_model_skipped(self) -> None:
        roles = {"empty-model": {"backend": "my-vllm", "model": "  "}}
        entries = _role_entries(roles, backends=_backends_with("my-vllm"))
        self.assertNotIn("empty-model", {e["id"] for e in entries})

    def test_non_dict_role_def_skipped(self) -> None:
        roles = {"bad": "not-a-dict", "good": {"backend": "kobold"}}
        entries = _role_entries(roles, backends=_swap_backends("kobold"))
        ids = {e["id"] for e in entries}
        self.assertNotIn("bad", ids)
        self.assertIn("good", ids)

    def test_swap_lifecycle_data_makes_any_service_id_a_local_swap_role(self) -> None:
        """A new service id with a swap driver gets the kobold role shape (data, not the name)."""
        roles = {"mine": {"backend": "brand-new-llm", "model": "/models/a.gguf"}}
        entry = _role_entries(roles, backends=_swap_backends("brand-new-llm"))[0]
        self.assertEqual(entry["azizam_model_path"], "/models/a.gguf")
        self.assertNotIn("azizam_model", entry)

    def test_non_swap_lifecycle_keeps_the_remote_role_shape_even_when_named_kobold(self) -> None:
        roles = {"mine": {"backend": "kobold", "model": "remote-name"}}
        entry = _role_entries(roles, backends=_backends_with("kobold"))[0]
        self.assertEqual(entry["azizam_model"], "remote-name")
        self.assertNotIn("azizam_model_path", entry)

    def test_empty_roles_produces_no_role_entries(self) -> None:
        entries = _role_entries({}, backends=_swap_backends("kobold"))
        self.assertEqual(entries, [])


# ---------------------------------------------------------------------------
# 3. role-is-alarm-only pin
# ---------------------------------------------------------------------------

class TestRoleIsAlarmOnly(unittest.TestCase):
    """PIN: role == 'standby' / 'required' affects ONLY the display/alarm path.

    Routing and resolution functions (backends_for_capability,
    _configured_model_record_for_id) must NOT key on the 'role' field of a
    service config entry to pick or skip a backend.
    """

    def _make_service_cfg(self, *, enabled: bool = True, role: str | None = None) -> dict:
        """Return a minimal config["backends"] entry optionally carrying 'role'."""
        out: dict[str, Any] = {
            "enabled": enabled,
            "host": "127.0.0.1",
            "port": 8080,
            "health_endpoint": "/health",
            "capabilities": ["chat", "completions"],
        }
        if role is not None:
            out["role"] = role
        return out

    # --- Display path: the production health projection -----------------------

    def test_role_changes_only_the_alarm_in_the_health_projection(self) -> None:
        """For the same live state, role sets the tone; it never hides a failure.

        A standby failure is still observed "down" but warns instead of
        alarming; the same failure on a required service alarms ("bad"); a
        healthy standby is plain "up". A stopped on-demand service is the one
        role-specific observation ("standby_stopped" for standby, else "idle").
        """
        on_demand = {"lifecycle": {"driver": "systemd"}}
        registry = {"services": {
            "standby-down": {"role": "standby"},
            "required-down": {"role": "required"},
            "standby-up": {"role": "standby"},
            "standby-stopped": {"role": "standby", "config": on_demand},
            "required-stopped": {"role": "required", "config": on_demand},
        }}
        payload = {"backends": [
            {"key": "standby-down", "status": "down"},
            {"key": "required-down", "status": "down"},
            {"key": "standby-up", "status": "up"},
            {"key": "standby-stopped", "status": "stopped"},
            {"key": "required-stopped", "status": "stopped"},
        ]}

        projection = derive_admin_health_projection(registry, payload, observation_available=True)

        rows = [(row["service_id"], row["observation"], row["tone"]) for row in projection["services"]]
        self.assertEqual(rows, [
            ("standby-down", "down", "warn"),
            ("required-down", "down", "bad"),
            ("standby-up", "up", "ok"),
            ("standby-stopped", "standby_stopped", "neutral"),
            ("required-stopped", "idle", "neutral"),
        ])

    # --- Routing functions ignore role field ----------------------------------

    def test_backends_for_capability_ignores_role_field(self) -> None:
        """backends_for_capability returns both required AND standby backends —
        routing sees them identically; only health/priority matter."""
        backends = {
            "svc-required": self._make_service_cfg(role="required"),
            "svc-standby": self._make_service_cfg(role="standby"),
        }
        with patch.dict(proxy.config, {"backends": backends}):
            result = proxy.backends_for_capability("chat")
        self.assertIn("svc-required", result)
        self.assertIn("svc-standby", result)

    def test_backends_for_capability_disabled_excluded_regardless_of_role(self) -> None:
        """A disabled backend is excluded by the enabled flag, not by role."""
        backends = {
            "enabled-standby": self._make_service_cfg(enabled=True, role="standby"),
            "disabled-required": self._make_service_cfg(enabled=False, role="required"),
        }
        with patch.dict(proxy.config, {"backends": backends}):
            result = proxy.backends_for_capability("chat")
        self.assertIn("enabled-standby", result)
        self.assertNotIn("disabled-required", result)

    def test_configured_model_record_ignores_role_field(self) -> None:
        """_configured_model_record_for_id resolves by model_id, not backend role."""
        backends = {
            "standby-svc": {
                **self._make_service_cfg(role="standby"),
                "model_id": "special-model",
            }
        }
        with (
            patch.dict(proxy.config, {"backends": backends}),
            patch.object(proxy, "ROLES", {}),
            patch.object(proxy, "MODELS_CATALOG", {}),
        ):
            backend, record = proxy._configured_model_record_for_id("special-model")
        # The standby service is still resolvable — role does not gate resolution.
        self.assertEqual(backend, "standby-svc")
        self.assertIsNotNone(record)

    def test_role_field_absent_same_as_required(self) -> None:
        """Backends with no role field route identically to role==required ones."""
        backends = {
            "no-role": self._make_service_cfg(),
            "explicit-required": self._make_service_cfg(role="required"),
        }
        with patch.dict(proxy.config, {"backends": backends}):
            result = proxy.backends_for_capability("chat")
        self.assertIn("no-role", result)
        self.assertIn("explicit-required", result)


if __name__ == "__main__":
    unittest.main()
