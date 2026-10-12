"""QUARANTINE — the last residual engine-specific knowledge, in ONE place.

THE BINDING PRINCIPLE (see AGENTS.md): the gateway tracks engines as DATA, never
as CODE. The churning per-engine code — swap classes, parsers, spawn/restart,
the cold-start chain, auth injection — is gone (deleted or moved to data). What
remains is a small set of engine-named *predicates*: places where a behaviour
genuinely differs by backend and the difference has not (yet) been expressed as
a service-config field.

Rather than leave those `if backend == "kobold"`
literals woven through a dozen functions in proxy.py, they are ISOLATED here.
This module quarantines the reviewed property tables below; it is not an
inventory of every remaining engine literal. proxy.py calls these predicates
instead of branching on those backend ids inline.

WHY THIS EXISTS AS A SEAM, NOT A FIX:
Each predicate documents *what property* the literal stands for (e.g. "this
backend reports its loaded model as a filesystem path", "this backend has an
mmproj aux"). When a property becomes a real service-config field, delete its
table entry here and read the field instead — a single-file change, with the
boundary test (tests/test_engine_knowledge_boundary.py) pointing every reviewer
straight here.

HOW TO REMOVE AN ENTRY (the migration recipe per property):
  - activation shape         -> the LifecycleDriver already owns build_activation;
                                fold the absolute-path rule into driver_config.
  - loaded-model probe kind  -> drive the probe off service config instead of
                                a backend id (see the note in
                                _BACKEND_STATE_STRATEGIES in proxy.py).
  - aux/mmproj               -> add `aux: {probe: pgrep}` to the service config.
  - local-swap membership    -> derive from "has a swap-capable lifecycle.driver".

These are independent migrations, not permission to grow quarantine. The
boundary guard scans this module and freezes all six tables and their
one-line membership predicates. New engine knowledge must use operator-owned
configuration, never another ID, table or branch here. Migrate each consumer
with its data field and delete the corresponding frozen entry.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Frozen compiled policy debt, NOT operator-editable data. No new backend IDs,
# tables or predicate behavior. Replace each property with service configuration
# and remove its table entry together with the consumer migration.
# ---------------------------------------------------------------------------

# Backends that carry an aux/mmproj concept (vision projector loaded alongside
# the model) and probe it via process inspection.
_HAS_AUX_MMPROJ: frozenset[str] = frozenset({"kobold"})

# Backends that are vision-capable only when an mmproj/aux is loaded at runtime
# (as opposed to statically vision-capable via their `capabilities` list). The
# caller still checks the runtime mmproj presence; this just says "this backend
# gains vision dynamically via its aux".
_VISION_VIA_AUX: frozenset[str] = frozenset({"kobold"})

# Backends whose context-window size is read from a live probe of the backend
# (the /v1/models endpoint) rather than a plain config field. The caller still
# owns the probe call; this only flags which backends need that special path.
_CONTEXT_WINDOW_FROM_LIVE_PROBE: frozenset[str] = frozenset({"kobold"})


# ---------------------------------------------------------------------------
# Predicates. proxy.py calls these instead of inlining `if backend == "..."`.
# Each is a one-line lookup; the engine knowledge is the table it reads.
# ---------------------------------------------------------------------------

def has_aux_mmproj(backend: str) -> bool:
    """True if `backend` carries an aux/mmproj (vision projector) concept."""
    return backend in _HAS_AUX_MMPROJ


def vision_via_aux(backend: str) -> bool:
    """True if `backend` becomes vision-capable via a runtime-loaded aux/mmproj.

    The caller is still responsible for checking that the aux is actually
    present right now; this only identifies which backends gain vision that way.
    """
    return backend in _VISION_VIA_AUX


def context_window_from_live_probe(backend: str) -> bool:
    """True if `backend`'s context window is read from a live backend probe."""
    return backend in _CONTEXT_WINDOW_FROM_LIVE_PROBE
