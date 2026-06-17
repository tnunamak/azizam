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


def test_local_swap_backends_are_exactly_kobold_tabby():
    assert eq.LOCAL_SWAP_BACKENDS == frozenset({"kobold", "tabby"})
    assert eq.is_local_swap_backend("kobold")
    assert eq.is_local_swap_backend("tabby")
    assert not eq.is_local_swap_backend("vllm")
    assert not eq.is_local_swap_backend("")


def test_default_model_fallback_tabby_only():
    assert eq.has_default_model_fallback("tabby")
    assert not eq.has_default_model_fallback("kobold")
    assert not eq.has_default_model_fallback("vllm")


def test_activation_shape_predicates():
    # tabby: unset activation model comes from the upstream id
    assert eq.activation_from_upstream_id("tabby")
    assert not eq.activation_from_upstream_id("kobold")
    # kobold: activation model must be an absolute on-disk path
    assert eq.activation_requires_abs_path("kobold")
    assert not eq.activation_requires_abs_path("tabby")


def test_loaded_model_probe_kind_is_disjoint():
    # kobold reports a filesystem path; tabby reports an http model id.
    assert eq.loaded_model_is_path("kobold")
    assert not eq.loaded_model_is_path("tabby")
    assert eq.loaded_model_is_http_id("tabby")
    assert not eq.loaded_model_is_http_id("kobold")
    # A backend cannot be both kinds.
    for b in ("kobold", "tabby", "vllm"):
        assert not (eq.loaded_model_is_path(b) and eq.loaded_model_is_http_id(b))


def test_aux_mmproj_and_vision_via_aux_kobold_only():
    assert eq.has_aux_mmproj("kobold")
    assert not eq.has_aux_mmproj("tabby")
    assert eq.vision_via_aux("kobold")
    assert not eq.vision_via_aux("tabby")


def test_roles_may_be_unpinned_tabby_only():
    assert eq.roles_may_be_unpinned("tabby")
    assert not eq.roles_may_be_unpinned("kobold")


def test_context_window_source_predicates():
    assert eq.context_window_from_model_params("tabby")
    assert not eq.context_window_from_model_params("kobold")
    assert eq.context_window_from_live_probe("kobold")
    assert not eq.context_window_from_live_probe("tabby")


def test_unknown_backend_is_false_everywhere():
    # A backend the quarantine doesn't know about gets the agnostic default
    # (False) from every predicate — so an added engine inherits no hidden
    # behaviour until explicitly given a property here (or a config field).
    for pred in (
        eq.is_local_swap_backend,
        eq.has_default_model_fallback,
        eq.activation_from_upstream_id,
        eq.activation_requires_abs_path,
        eq.has_aux_mmproj,
        eq.loaded_model_is_path,
        eq.loaded_model_is_http_id,
        eq.roles_may_be_unpinned,
        eq.vision_via_aux,
        eq.context_window_from_model_params,
        eq.context_window_from_live_probe,
    ):
        assert pred("some-new-engine") is False, pred.__name__
        assert pred("") is False, pred.__name__
