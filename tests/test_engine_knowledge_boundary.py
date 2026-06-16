"""Static boundary guard: engine knowledge must not creep into core code.

This test enforces the principle documented in
docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md.

Engine-specific knowledge is acceptable as DATA (transform rows, presets,
seed config) but NOT as CODE (if-engine-name branches, engine-named classes,
engine CLI flag literals).

HOW THIS TEST WORKS
-------------------
Five static checks run over four core files:
  - proxy.py
  - gateway/transforms.py
  - gateway/service_registry.py
  - gateway/toolcall_detection.py

Each check has a FROZEN ALLOWLIST.  If a new match appears outside the
allowlist, the test fails with a message pointing here.

ALLOWLIST KEY FORMAT (content-keyed, drift-proof)
--------------------------------------------------
Allowlists are keyed by (relative_file, stripped_line_text) — NOT by line
number.  This makes them DRIFT-PROOF: inserting code above an allowlisted
site no longer shifts every entry and causes spurious failures.  Only a
change to the actual content of an allowlisted line (or a genuinely new
pattern) causes a failure.

Where two legitimate sites share identical stripped text (e.g. the same
`if backend == "kobold":` appears at two locations), one allowlist entry
covers both — they represent the same reviewed pattern.

ADDING TO AN ALLOWLIST
-----------------------
If you believe a new match is a legitimate exception, add it to the
appropriate allowlist with a justification comment.  The allowlist is the
choke point: adding to it is a reviewable diff that forces justification at
the exact moment someone types ``if backend ==``.

See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md for the full design rationale.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_PROXY = _REPO_ROOT / "proxy.py"
_TRANSFORMS = _REPO_ROOT / "gateway" / "transforms.py"
_SERVICE_REGISTRY = _REPO_ROOT / "gateway" / "service_registry.py"
_TOOLCALL = _REPO_ROOT / "gateway" / "toolcall_detection.py"

_CORE_FILES = [_PROXY, _TRANSFORMS, _SERVICE_REGISTRY, _TOOLCALL]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _fail_message(check_name: str, violations: list[str]) -> str:
    found = "\n".join(f"  {v}" for v in violations)
    return textwrap.dedent(f"""
        ENGINE KNOWLEDGE BOUNDARY VIOLATION — {check_name}

        {found}

        Engine knowledge belongs in DATA (a transform row / preset), not code.
        If this is genuinely irreducible, add a reviewed named built-in via
        register_builtin() and attach it by id — then add it to the allowlist
        in this test file with a justification comment.

        See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md for the full rationale.
    """).strip()


# ---------------------------------------------------------------------------
# Check 1: Engine-named branches
#
# Pattern: string literals that are engine names appearing in comparison
# expressions ("vllm" in hint, preset == "kobold", etc.)
#
# ENGINE_NAMES is the denylist.  Any appearance in an `if` condition (or a
# loop guard) that contains one of these strings as a literal is flagged.
# ---------------------------------------------------------------------------

_ENGINE_NAMES: frozenset[str] = frozenset({
    "vllm", "sglang", "ollama", "mistral", "qwen", "gemma", "hermes",
    "command_r", "tgi", "lmstudio", "lm-studio",
    "llama-cpp-python", "llama_cpp_python", "llama-server", "llamacpp",
    "llama.cpp", "tabbyapi", "tabby", "kobold", "koboldcpp",
})

# Pattern: a string literal containing an engine name (as a substring check or
# equality).  We look for these inside conditional lines (if/elif/for).
_ENGINE_BRANCH_PATTERN = re.compile(
    r'(?:if|elif|for)\b.*(?:in\s+hint|==\s*["\']|["\'\s]in\s+["\'])',
    re.IGNORECASE,
)

# Allowlisted entries: {(relative_file, stripped_line_text): reason}
#
# Keyed on NORMALIZED LINE CONTENT (line.strip()) instead of line number so
# the allowlist is DRIFT-PROOF.  Code insertions above an allowlisted site
# no longer shift entries and cause spurious failures.  A genuinely new
# engine branch at any site still fails — only previously-reviewed patterns
# are recorded here.
#
# Where two legitimate sites share identical stripped text, one entry covers
# both — they represent the same reviewed pattern.
_ENGINE_BRANCH_ALLOWLIST: dict[tuple[str, str], str] = {
    # -------------------------------------------------------------------
    # Bucket B: _LOCAL_SWAP_BACKENDS lifecycle infra (proxy.py)
    # These branches handle managed-local-swap lifecycle (start/stop/restart,
    # GGUF paths, health routing).  They are deployment-pattern code, not
    # request-shape knowledge.  The set is FROZEN at {kobold, tabby} — see
    # Check 4.  Each distinct stripped line pattern is enumerated here; a new
    # pattern forces a new entry and a reviewer to sign off.
    # -------------------------------------------------------------------
    ("proxy.py", "if target == 'tabby':"):
        "Bucket B: local-swap target routing — deployment-pattern, not engine knowledge",
    ("proxy.py", "elif target == 'kobold':"):
        "Bucket B: local-swap target routing — deployment-pattern, not engine knowledge",
    ("proxy.py", "target_host = TABBY_HOST if target == 'tabby' else KOBOLD_HOST"):
        "Bucket B: local-swap host selection — deployment-pattern, not engine knowledge",
    ("proxy.py", "health_path = TABBY_HEALTH_ENDPOINT if target == 'tabby' else KOBOLD_HEALTH_ENDPOINT"):
        "Bucket B: local-swap health path — deployment-pattern, not engine knowledge",
    ("proxy.py", "return TABBY_HOST if active == 'tabby' else KOBOLD_HOST if active == 'kobold' else None"):
        "Bucket B: local-swap active host resolution — deployment-pattern, not engine knowledge",
    ("proxy.py", "return TABBY_HOST if needed == 'tabby' else KOBOLD_HOST"):
        "Bucket B: local-swap needed host resolution — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if backend == "kobold":'):
        "Bucket B: local-swap kobold branch — deployment-pattern, not engine knowledge",
    ("proxy.py", 'elif backend == "tabby":'):
        "Bucket B: local-swap tabby branch — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if service == "tabby":'):
        "Bucket B: local-swap service tabby check — deployment-pattern, not engine knowledge",
    ("proxy.py", 'return DEFAULT_MODEL if backend == "tabby" else None'):
        "Bucket B: local-swap default model tabby — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if backend == "tabby" and not activation_model:'):
        "Bucket B: local-swap activation model check tabby — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if backend == "kobold" and activation_model and not os.path.isabs(activation_model):'):
        "Bucket B: local-swap activation model check kobold — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if backend == "tabby":'):
        "Bucket B: local-swap backend tabby guard — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if active == "kobold":'):
        "Bucket B: local-swap active kobold check — deployment-pattern, not engine knowledge",
    ("proxy.py", 'elif active == "tabby":'):
        "Bucket B: local-swap active tabby check — deployment-pattern, not engine knowledge",
    ("proxy.py", 'elif backend == "tabby" and loaded_model_id:'):
        "Bucket B: local-swap tabby loaded model check — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if active == "tabby":'):
        "Bucket B: local-swap active tabby check — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if service == "tabby" and (API_KEY or ADMIN_KEY):'):
        "Bucket B: local-swap tabby API key check — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if target_service == "tabby" and await restart_tabby_if_needed():'):
        "Bucket B: local-swap target_service tabby restart — deployment-pattern, not engine knowledge",
    ("proxy.py", "if backend not in (\"kobold\", \"tabby\"):"):
        "Bucket B: local-swap backend guard (not-in check) — deployment-pattern, not engine knowledge",
    ("proxy.py", 'if key in {"kobold", "tabby"}:'):
        "Bucket B: local-swap key-in-set guard — deployment-pattern, not engine knowledge",
    # The next two are false-positive branch-pattern matches on non-branch lines
    # (docstring and error message) that happen to contain engine name literals.
    # Previously reviewed in the old line-number allowlist.
    ("proxy.py", "Returns 'tabby', 'kobold', or None if not found.\"\"\""):
        "Bucket B: local-swap docstring (detect_model_backend) — not a real branch, reviewed false positive",
    ("proxy.py", "\"Configured backend 'tabby' is unavailable for this catch-all route. \""):
        "Bucket B: local-swap error message string — not a real branch, reviewed false positive",

    # -------------------------------------------------------------------
    # D-3 migration is COMPLETE: advisory_for_backend is now engine-agnostic.
    # No toolcall_detection.py entries needed here.
    # -------------------------------------------------------------------
}


def _check_engine_named_branches(
    _source_lines: list[tuple[str, list[str]]] | None = None,
) -> list[str]:
    """Fail if an engine-named string literal appears in a branch condition
    outside the allowlist.

    The optional *_source_lines* parameter allows tests to inject synthetic
    source content without touching the real files (used by the self-test).
    """
    violations: list[str] = []
    if _source_lines is None:
        sources: list[tuple[str, list[str]]] = [
            (str(path.relative_to(_REPO_ROOT)), _lines(path))
            for path in _CORE_FILES
        ]
    else:
        sources = _source_lines
    for rel, file_lines in sources:
        for lineno, line in enumerate(file_lines, start=1):
            # Only look at lines that contain a branch keyword
            if not re.search(r'\b(?:if|elif|for)\b', line):
                continue
            # Check if any engine name appears as a string literal on this line
            for engine in _ENGINE_NAMES:
                # Match engine name as a quoted string literal
                if re.search(r'["\']' + re.escape(engine) + r'["\']', line):
                    key = (rel, line.strip())
                    if key not in _ENGINE_BRANCH_ALLOWLIST:
                        violations.append(
                            f"{rel}:{lineno}: engine literal {engine!r} in branch: "
                            f"{line.strip()[:100]}"
                        )
                    break  # one engine match per line is enough to flag/allowlist
    return violations


# ---------------------------------------------------------------------------
# Check 2: Engine-named Transform classes
#
# Class names matching *<Engine>*Transform indicate an engine-specific code
# abstraction (the treadmill).  New matches must be on the allowlist.
# ---------------------------------------------------------------------------

_ENGINE_CLASS_PATTERN = re.compile(
    r'\bclass\s+\w*(?:Ollama|VLLM|Vllm|SGLang|Sglang|Mistral|Qwen|Gemma|'
    r'Kobold|KoboldCpp|Tabby|Llama|LMStudio|Lmstudio|Hermes|CommandR)\w*'
    r'Transform\b',
    re.IGNORECASE,
)

# Allowlisted entries: {(relative_file, stripped_line_text): reason}
# Content-keyed for drift-proofness (same rationale as _ENGINE_BRANCH_ALLOWLIST).
# Allowlisted until the migrations described in §D-1 are complete.
# D-2 (KoboldCppNullStripTransform) is DONE: class renamed to DropNullParamsTransform,
# id renamed to "drop-null-params" — no longer engine-named, no longer needs an allowlist entry.
_ENGINE_CLASS_ALLOWLIST: dict[tuple[str, str], str] = {
    ("gateway/transforms.py", "class Qwen3ThinkingTransform:"):
        "D-1 residue: Qwen3ThinkingTransform is a 400-validation sentinel only; "
        "suppression is now the engine-agnostic enable_thinking kwarg (no prompt-string injection); "
        "remove entry when the 400 validator is generalised away from a named class",
}


def _check_engine_named_classes() -> list[str]:
    violations: list[str] = []
    for path in _CORE_FILES:
        rel = str(path.relative_to(_REPO_ROOT))
        for lineno, line in enumerate(_lines(path), start=1):
            if _ENGINE_CLASS_PATTERN.search(line):
                key = (rel, line.strip())
                if key not in _ENGINE_CLASS_ALLOWLIST:
                    violations.append(
                        f"{rel}:{lineno}: engine-named Transform class: "
                        f"{line.strip()[:120]}"
                    )
    return violations


# ---------------------------------------------------------------------------
# Check 3: Engine CLI flag literals (the red-team gap)
#
# Engine-knowledge-as-data-literal: flag strings that embed engine CLI
# knowledge in code.  These belong in operator-authored service config
# (tool_call_advisory field), not in gateway code.
# ---------------------------------------------------------------------------

_ENGINE_FLAG_LITERALS: list[str] = [
    "--tool-call-parser",
    "--enable-auto-tool-choice",
    "--chat_format",
    "chatml-function-calling",
    # --jinja is a flag only meaningful in llama-server context; a generic
    # flag of this name in code is engine knowledge.
    # We check for it only in function bodies (not docstrings/comments) via
    # the quoted-string form.
    '"--jinja"',
    "'--jinja'",
]

# Allowlisted until D-3 migration cuts advisory_for_backend.
# EMPTY by design. Decision D-3 (cut the per-engine advisory flags) is DONE:
# advisory_for_backend is now engine-agnostic and contains zero CLI flag
# literals. Any new engine CLI flag appearing in a core file is a real
# boundary violation — do NOT add an allowlist entry to silence it; remove the
# flag (it belongs in an operator-authored service-config field, as data).
#
# Content-keyed: {(relative_file, stripped_line_text): reason}
_ENGINE_FLAG_ALLOWLIST: dict[tuple[str, str], str] = {}


def _check_engine_flag_literals() -> list[str]:
    violations: list[str] = []
    for path in _CORE_FILES:
        rel = str(path.relative_to(_REPO_ROOT))
        for lineno, line in enumerate(_lines(path), start=1):
            # Skip pure comment lines and docstring lines (heuristic: leading #)
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for flag in _ENGINE_FLAG_LITERALS:
                if flag in line:
                    key = (rel, stripped)
                    if key not in _ENGINE_FLAG_ALLOWLIST:
                        violations.append(
                            f"{rel}:{lineno}: engine CLI flag literal {flag!r}: "
                            f"{stripped[:100]}"
                        )
                    break  # one flag match per line is enough to flag/allowlist
    return violations


# ---------------------------------------------------------------------------
# Check 4: _LOCAL_SWAP_BACKENDS is frozen at {kobold, tabby}
#
# If a third managed engine is added, the test fails — forcing an explicit
# allowlist edit and reviewer sign-off before the lifecycle infra grows.
# ---------------------------------------------------------------------------

_EXPECTED_LOCAL_SWAP_BACKENDS: frozenset[str] = frozenset({"kobold", "tabby"})

_LOCAL_SWAP_PATTERN = re.compile(
    r'_LOCAL_SWAP_BACKENDS\s*(?::\s*\S+)?\s*=\s*frozenset\s*\(\s*\{([^}]*)\}\s*\)'
)


def _check_local_swap_backends_frozen() -> list[str]:
    text = _PROXY.read_text(encoding="utf-8")
    match = _LOCAL_SWAP_PATTERN.search(text)
    if match is None:
        return [
            "proxy.py: _LOCAL_SWAP_BACKENDS definition not found — "
            "did it move or get renamed?"
        ]
    # Parse the frozenset contents
    contents = match.group(1)
    found = {s.strip().strip('"\'') for s in contents.split(",") if s.strip()}
    if found != _EXPECTED_LOCAL_SWAP_BACKENDS:
        added = found - _EXPECTED_LOCAL_SWAP_BACKENDS
        removed = _EXPECTED_LOCAL_SWAP_BACKENDS - found
        parts = []
        if added:
            parts.append(f"added: {sorted(added)}")
        if removed:
            parts.append(f"removed: {sorted(removed)}")
        return [
            f"proxy.py: _LOCAL_SWAP_BACKENDS changed ({', '.join(parts)}). "
            f"Expected exactly {sorted(_EXPECTED_LOCAL_SWAP_BACKENDS)}. "
            "Adding a third managed engine requires updating the allowlist in this "
            "test with a justification, and generalizing the lifecycle infra to a "
            "DB capability flag (see docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md §4-E)."
        ]
    return []


# ---------------------------------------------------------------------------
# Check 5: Primitive vocabulary is closed
#
# New primitive kinds are welcome (agnostic is fine) but must be general, not
# engine-named.  Adding a kind forces a deliberate allowlist edit here.
# ---------------------------------------------------------------------------

_EXPECTED_PRIMITIVE_KINDS: frozenset[str] = frozenset({
    "value-map",
    "drop-params",
    "default-params",
    "param-map",
    "message-inject",
    "clamp",
})

def _check_primitive_vocabulary_closed() -> list[str]:
    lines = _lines(_TRANSFORMS)
    # Find the _PRIMITIVE_KINDS assignment and collect lines until the closing }
    start = None
    for i, line in enumerate(lines):
        if re.match(r'\s*_PRIMITIVE_KINDS\s*(?::[^=]+)?\s*=\s*\{', line):
            start = i
            break
    if start is None:
        return [
            "gateway/transforms.py: _PRIMITIVE_KINDS dict not found — "
            "did it move or get renamed?"
        ]
    # Collect lines from the opening brace until we find a line that is just '}'
    body_lines: list[str] = []
    for line in lines[start:]:
        body_lines.append(line)
        # A lone closing brace on a line ends the dict
        if line.strip() == "}":
            break
    body = "\n".join(body_lines)
    # Extract the string keys: lines of the form `"kind-name": SomeClass,`
    found = set(re.findall(r'^\s*["\']([^"\']+)["\']\s*:', body, re.MULTILINE))
    if found != _EXPECTED_PRIMITIVE_KINDS:
        added = found - _EXPECTED_PRIMITIVE_KINDS
        removed = _EXPECTED_PRIMITIVE_KINDS - found
        parts = []
        if added:
            parts.append(f"added: {sorted(added)}")
        if removed:
            parts.append(f"removed: {sorted(removed)}")
        return [
            f"gateway/transforms.py: _PRIMITIVE_KINDS changed ({', '.join(parts)}). "
            f"Expected exactly {sorted(_EXPECTED_PRIMITIVE_KINDS)}. "
            "New agnostic primitives are welcome — update _EXPECTED_PRIMITIVE_KINDS "
            "in this test with a one-line justification comment. "
            "Engine-named primitives are NOT welcome (check 2 already polices that)."
        ]
    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEngineKnowledgeBoundary:
    """Static boundary guard — these are the checks that keep the treadmill off."""

    def test_no_new_engine_named_branches(self) -> None:
        """No engine-named string literal should appear in a branch condition
        outside the allowlist.

        Engine names in `if`/`elif`/`for` conditions mean per-engine code paths.
        New engine quirks belong in DATA (a transform row / preset).
        """
        violations = _check_engine_named_branches()
        assert not violations, _fail_message("engine-named branches", violations)

    def test_no_new_engine_named_transform_classes(self) -> None:
        """No engine-named *Transform class should appear outside the allowlist.

        Engine-named classes are the archetypal treadmill: every new engine
        tempts a new class.  New irreducible mutations go in register_builtin()
        with a generic name and a DATA-attached id.
        """
        violations = _check_engine_named_classes()
        assert not violations, _fail_message("engine-named Transform classes", violations)

    def test_no_engine_flag_literals_in_code(self) -> None:
        """Engine CLI flag strings (--tool-call-parser, --jinja, etc.) must not
        appear in core gateway code outside the allowlist.

        These literals are the 'engine-knowledge-as-data-literal' anti-pattern:
        they look like strings but they embed engine changelog knowledge in code.
        Per-backend remediation advice belongs in the operator-authored
        tool_call_advisory field on the service record (DATA), not in code.
        """
        violations = _check_engine_flag_literals()
        assert not violations, _fail_message("engine CLI flag literals", violations)

    def test_local_swap_backends_frozen(self) -> None:
        """_LOCAL_SWAP_BACKENDS must equal exactly {'kobold', 'tabby'}.

        Adding a third managed engine without review would extend the lifecycle
        infra treadmill silently.  A third entry here forces an explicit
        allowlist edit and reviewer sign-off.  The long-term fix is a DB
        capability flag (see docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md §4-E).
        """
        violations = _check_local_swap_backends_frozen()
        assert not violations, _fail_message("_LOCAL_SWAP_BACKENDS frozen set", violations)

    def test_primitive_vocabulary_closed(self) -> None:
        """_PRIMITIVE_KINDS must equal the documented fixed set.

        New agnostic primitives are welcome — update _EXPECTED_PRIMITIVE_KINDS
        in this file with a justification comment.  The check prevents silent
        addition of engine-specific 'primitives' (check 2 would catch an
        engine-named class, but a snake_case kind string would slip past it).
        """
        violations = _check_primitive_vocabulary_closed()
        assert not violations, _fail_message("primitive vocabulary", violations)

    def test_guard_still_catches_new_engine_branches(self) -> None:
        """The guard must flag a synthetic new engine branch not in the allowlist.

        This self-test proves the check has not been neutered by the content-keyed
        refactor: a genuinely new engine branch (never reviewed, not in any
        allowlist entry) must still be reported as a violation.
        """
        synthetic_lines = [
            # An engine-agnostic line — must NOT be flagged
            'if backend == "tabby":',           # IS allowlisted → not a violation
            # A synthetic new engine branch — must BE flagged
            'if backend == "vllm":',            # not in allowlist → violation
        ]
        violations = _check_engine_named_branches(
            _source_lines=[("proxy.py", synthetic_lines)]
        )
        # The allowlisted tabby line must NOT appear in violations
        allowlisted_reported = [v for v in violations if "'tabby'" in v]
        assert not allowlisted_reported, (
            "Guard incorrectly flagged an allowlisted line: "
            + "\n".join(allowlisted_reported)
        )
        # The synthetic vllm line MUST appear in violations
        vllm_reported = [v for v in violations if "'vllm'" in v]
        assert vllm_reported, (
            "Guard failed to catch a synthetic new engine branch "
            '(`if backend == "vllm":`) — the boundary check is broken!'
        )
