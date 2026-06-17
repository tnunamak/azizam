"""QUARANTINE — the last residual engine-specific knowledge, in ONE place.

THE BINDING PRINCIPLE (see AGENTS.md): the gateway tracks engines as DATA, never
as CODE. The churning per-engine code — swap classes, parsers, spawn/restart,
the cold-start chain, auth injection — is gone (deleted or moved to data). What
remains is a small set of engine-named *predicates*: places where a behaviour
genuinely differs by backend and the difference has not (yet) been expressed as
a service-config field.

Rather than leave those `if backend == "kobold"` / `if backend == "tabby"`
literals woven through a dozen functions in proxy.py, they are ISOLATED here.
This module is the single quarantine: every remaining engine literal lives in
the tables below, and proxy.py calls the named predicates instead of branching
on a backend id inline.

WHY THIS EXISTS AS A SEAM, NOT A FIX:
Each predicate documents *what property* the literal stands for (e.g. "this
backend reports its loaded model as a filesystem path", "this backend has an
mmproj aux"). When a property becomes a real service-config field, delete its
table entry here and read the field instead — a single-file change, with the
boundary test (tests/test_engine_knowledge_boundary.py) pointing every reviewer
straight here.

HOW TO REMOVE AN ENTRY (the migration recipe per property):
  - default-model fallback   -> add `default_model: <id>` to the service config.
  - activation shape         -> the LifecycleDriver already owns build_activation;
                                fold these last two cases into driver_config.
  - loaded-model probe kind  -> add a `state_type` strategy that distinguishes
                                "model is a path" vs "http id" + an auth header,
                                then drive the probe off it (see the note in
                                _BACKEND_STATE_STRATEGIES in proxy.py).
  - aux/mmproj               -> add `aux: {probe: pgrep}` to the service config.
  - local-swap membership    -> derive from "has a swap-capable lifecycle.driver".

These are all small, independent moves. Nothing else in the codebase needs to
branch on an engine name; if you find one that does, route it through here.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# The residue, as DATA. Each table is one engine-specific property. Adding a
# backend that shares a property = add its id here (or, better, give the backend
# a config field and delete the property — see the recipe in the module docstring).
# ---------------------------------------------------------------------------

# Backends whose lifecycle is local + model-swap-managed (vs always-on external).
# Mirrors proxy._LOCAL_SWAP_BACKENDS; kept here so the literal lives in one place.
LOCAL_SWAP_BACKENDS: frozenset[str] = frozenset({"kobold", "tabby"})

# Backends that fall back to DEFAULT_MODEL when no model is otherwise resolvable.
_HAS_DEFAULT_MODEL_FALLBACK: frozenset[str] = frozenset({"tabby"})

# Backends whose activation model, when unset, is taken from the upstream_model
# id (an http-loaded model name) rather than a local path.
_ACTIVATION_FROM_UPSTREAM_ID: frozenset[str] = frozenset({"tabby"})

# Backends whose activation model MUST be an absolute on-disk path (a GGUF file);
# a non-absolute value is rejected (cleared) before activation.
_ACTIVATION_REQUIRES_ABS_PATH: frozenset[str] = frozenset({"kobold"})

# Backends that carry an aux/mmproj concept (vision projector loaded alongside
# the model) and probe it via process inspection.
_HAS_AUX_MMPROJ: frozenset[str] = frozenset({"kobold"})

# Backends whose loaded model is reported as a filesystem PATH (compared against
# the GGUF path being activated), via the cached local probe — rather than an
# http model-id read.
_LOADED_MODEL_IS_PATH: frozenset[str] = frozenset({"kobold"})

# Backends reachable over HTTP for a loaded-model id probe (model name, not path).
_LOADED_MODEL_IS_HTTP_ID: frozenset[str] = frozenset({"tabby"})

# Backends whose roles may name the backend without pinning a specific model
# path; when the backend is hot, such a role is a valid handle for the loaded
# model.
_ROLES_MAY_BE_UNPINNED: frozenset[str] = frozenset({"tabby"})

# Backends that are vision-capable only when an mmproj/aux is loaded at runtime
# (as opposed to statically vision-capable via their `capabilities` list). The
# caller still checks the runtime mmproj presence; this just says "this backend
# gains vision dynamically via its aux".
_VISION_VIA_AUX: frozenset[str] = frozenset({"kobold"})

# Backends whose context-window size comes from a backend-specific source rather
# than a plain config field: tabby reads it from its loaded model params,
# kobold reads it from the live /props endpoint. The caller still owns the
# source call; this only flags which backends need that special path.
_CONTEXT_WINDOW_FROM_MODEL_PARAMS: frozenset[str] = frozenset({"tabby"})
_CONTEXT_WINDOW_FROM_LIVE_PROBE: frozenset[str] = frozenset({"kobold"})


# ---------------------------------------------------------------------------
# Predicates. proxy.py calls these instead of inlining `if backend == "..."`.
# Each is a one-line lookup; the engine knowledge is the table it reads.
# ---------------------------------------------------------------------------

def is_local_swap_backend(backend: str) -> bool:
    """True if `backend` is a local, model-swap-managed LLM (not always-on)."""
    return backend in LOCAL_SWAP_BACKENDS


def has_default_model_fallback(backend: str) -> bool:
    """True if `backend` falls back to DEFAULT_MODEL when none is resolvable."""
    return backend in _HAS_DEFAULT_MODEL_FALLBACK


def activation_from_upstream_id(backend: str) -> bool:
    """True if an unset activation model is taken from the upstream_model id."""
    return backend in _ACTIVATION_FROM_UPSTREAM_ID


def activation_requires_abs_path(backend: str) -> bool:
    """True if the activation model must be an absolute on-disk path."""
    return backend in _ACTIVATION_REQUIRES_ABS_PATH


def has_aux_mmproj(backend: str) -> bool:
    """True if `backend` carries an aux/mmproj (vision projector) concept."""
    return backend in _HAS_AUX_MMPROJ


def loaded_model_is_path(backend: str) -> bool:
    """True if `backend` reports its loaded model as a filesystem path."""
    return backend in _LOADED_MODEL_IS_PATH


def loaded_model_is_http_id(backend: str) -> bool:
    """True if `backend` reports its loaded model as an http model id."""
    return backend in _LOADED_MODEL_IS_HTTP_ID


def roles_may_be_unpinned(backend: str) -> bool:
    """True if roles for `backend` may name it without pinning a model path."""
    return backend in _ROLES_MAY_BE_UNPINNED


def vision_via_aux(backend: str) -> bool:
    """True if `backend` becomes vision-capable via a runtime-loaded aux/mmproj.

    The caller is still responsible for checking that the aux is actually
    present right now; this only identifies which backends gain vision that way.
    """
    return backend in _VISION_VIA_AUX


def context_window_from_model_params(backend: str) -> bool:
    """True if `backend`'s context window comes from its loaded-model params."""
    return backend in _CONTEXT_WINDOW_FROM_MODEL_PARAMS


def context_window_from_live_probe(backend: str) -> bool:
    """True if `backend`'s context window is read from a live backend probe."""
    return backend in _CONTEXT_WINDOW_FROM_LIVE_PROBE
