"""Tests for the engine-quarantine module.

The quarantine isolates the last residual engine-specific predicates in one
place (gateway/engine_quarantine.py). These tests pin each predicate's table so
a change to "which engine has property X" is a visible, reviewed diff — and
document the exact behaviour proxy.py relies on after the inline `if backend ==
"..."` branches were replaced by predicate calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import engine_quarantine as eq  # noqa: E402


def test_aux_mmproj_and_vision_via_aux_kobold_only():
    assert eq.has_aux_mmproj("kobold")
    assert not eq.has_aux_mmproj("vllm")
    assert eq.vision_via_aux("kobold")
    assert not eq.vision_via_aux("vllm")


def test_context_window_from_live_probe_kobold_only():
    assert eq.context_window_from_live_probe("kobold")
    assert not eq.context_window_from_live_probe("vllm")


def test_removed_tabby_predicates_stay_removed():
    for name in (
        "has_default_model_fallback", "activation_from_upstream_id",
        "loaded_model_is_http_id", "roles_may_be_unpinned",
        "context_window_from_model_params",
    ):
        assert not hasattr(eq, name), name


def test_unknown_backend_is_false_everywhere():
    # A backend the quarantine doesn't know about gets the agnostic default
    # (False) from every predicate — so an added engine inherits no hidden
    # behaviour until explicitly given a property here (or a config field).
    for pred in (
        eq.has_aux_mmproj,
        eq.vision_via_aux,
        eq.context_window_from_live_probe,
    ):
        assert pred("some-new-engine") is False, pred.__name__
        assert pred("") is False, pred.__name__
