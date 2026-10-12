"""The engine-quarantine module holds no table and no predicate any more."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import engine_quarantine as eq  # noqa: E402


def test_every_property_table_and_predicate_stays_removed():
    for name in (
        "has_default_model_fallback", "activation_from_upstream_id",
        "loaded_model_is_http_id", "roles_may_be_unpinned",
        "context_window_from_model_params", "is_local_swap_backend",
        "activation_requires_abs_path", "has_aux_mmproj", "loaded_model_is_path",
        "vision_via_aux", "context_window_from_live_probe",
        "LOCAL_SWAP_BACKENDS",
    ):
        assert not hasattr(eq, name), name
