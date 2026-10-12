"""Static boundary guard: engine knowledge must not creep into core code.

This test enforces the principle documented in
docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md.

Engine-specific knowledge is acceptable as DATA (transform rows, presets,
seed config) but NOT as CODE (if-engine-name branches, engine-named classes,
engine CLI flag literals).

HOW THIS TEST WORKS
-------------------
The engine checks scan proxy.py, all gateway Python modules and the fake
upstream package under tests/support/fake_upstream/. This is a
syntactic tripwire, not proof that all backend policy is operator-owned data.

A retirement check also scans proxy.py and every Python file under gateway/
for the removed executor, loader and retry constants.

The engine checks have FROZEN ALLOWLISTS. If a new match appears outside an
allowlist, the test fails with a message pointing here. The retirement check
has no allowlist.

ALLOWLIST KEY FORMAT (content-keyed, drift-proof)
--------------------------------------------------
Branch exceptions are keyed by (relative_file, normalized AST expression/pattern);
class and flag exceptions use (relative_file, stripped_line_text). Inserting
code above a reviewed site does not change its key.

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
# The residual engine literals are isolated in the quarantine module; the
# LOCAL_SWAP_BACKENDS frozen set now lives there (proxy aliases it).
_QUARANTINE = _REPO_ROOT / "gateway" / "engine_quarantine.py"

_FAKE_UPSTREAM = _REPO_ROOT / "tests" / "support" / "fake_upstream"


def _core_files() -> list[Path]:
    """Discover new gateway modules too; no module has a blanket exemption.

    The fake upstream package is scanned like core code: fakes model wire
    protocols, never named engines (docs/design/TEST_STRATEGY.md 4.6).
    """
    return [
        _PROXY,
        *sorted((_REPO_ROOT / "gateway").rglob("*.py")),
        *sorted(_FAKE_UPSTREAM.rglob("*.py")),
    ]


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
# Pattern: engine-name string literals in conditions, comparisons, loop
# iterables, or match cases ("vllm" in hint, preset == "kobold", etc.).
#
# ENGINE_NAMES is a supplementary syntactic denylist, not a semantic policy
# check. P2-1 voice compiler, P2-2 reasoning dialect, P2-4 voice catalog,
# P2-5 audio intake, P2-6 STT diagnostics, P2-7 UI fallback, P2-8 public
# design alias, P2-9 STT planning, and P2-10 think format need separate
# config/behavior ownership review. P2-3 quarantine is frozen below.
# ---------------------------------------------------------------------------

_ENGINE_NAMES: frozenset[str] = frozenset({
    "vllm", "sglang", "ollama", "mistral", "qwen", "gemma", "hermes",
    "functionary",
    "command_r", "tgi", "lmstudio", "lm-studio",
    "llama-cpp-python", "llama_cpp_python", "llama-server", "llamacpp",
    "llama.cpp", "tabbyapi", "tabby", "kobold", "koboldcpp",
})

# Allowlisted entries: {(relative_file, ast.unparse(expression_or_pattern)): reason}
#
# Keyed on normalized expression, not line number. Adding a condition changes
# the expression and fails the guard; a line move does not.
#
# Where two legitimate sites share identical stripped text, one entry covers
# both — they represent the same reviewed pattern.
_ENGINE_BRANCH_ALLOWLIST: dict[tuple[str, str], str] = {
    # -------------------------------------------------------------------
    # proxy.py is now CLEAN of inline engine-named branches. Every residual
    # engine literal was moved into the quarantine module
    # (gateway/engine_quarantine.py); proxy.py calls named predicates
    # (engine_quarantine.is_local_swap_backend(...), etc.) instead of branching
    # on a backend id inline. So there are no proxy.py allowlist entries — if a
    # new `if backend == "..."` ever appears in proxy.py, this guard fails;
    # put the property in operator-owned service data.
    #
    # P2-3: quarantine tables are data-shaped transitional debt; G2 freezes
    # every table and predicate. This is an exact expression exception only.
    # Resource admission's protocol discriminator parses the Ollama HTTP error
    # envelope. It is a declared transport protocol, not backend-ID routing.
    ("gateway/resource_admission_http.py", "protocol == 'ollama' and isinstance(body.get('error'), str)"): "P2-G1 reviewed protocol error envelope",
    ("gateway/resource_admission_http.py", "protocol == 'ollama'"): "P2-G1 reviewed protocol error envelope",
    #
    # D-3 migration is COMPLETE: advisory_for_backend is engine-agnostic.
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
            for path in _core_files()
        ]
    else:
        sources = _source_lines
    for rel, file_lines in sources:
        try:
            tree = ast.parse("\n".join(file_lines), filename=rel)
        except SyntaxError as exc:
            violations.append(f"{rel}:{exc.lineno}: cannot parse Python source: {exc.msg}")
            continue
        covered: set[int] = set()
        candidates: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                candidates.append(node.test)
                covered.update(id(child) for child in ast.walk(node.test))
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                if id(node.iter) not in covered:
                    candidates.append(node.iter)
                    covered.update(id(child) for child in ast.walk(node.iter))
            elif isinstance(node, ast.match_case):
                candidates.append(node.pattern)
                covered.update(id(child) for child in ast.walk(node.pattern))
                if node.guard is not None:
                    candidates.append(node.guard)
                    covered.update(id(child) for child in ast.walk(node.guard))
        candidates.extend(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and id(node) not in covered
        )
        for expr in candidates:
            engines = sorted({
                node.value.lower() for node in ast.walk(expr)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.lower() in _ENGINE_NAMES
            })
            if not engines:
                continue
            expression = ast.unparse(expr)
            if (rel, expression) not in _ENGINE_BRANCH_ALLOWLIST:
                violations.append(
                    f"{rel}:{expr.lineno}: engine literal {engines!r} in condition/compare: {expression[:100]}"
                )
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

}


def _check_engine_named_classes() -> list[str]:
    violations: list[str] = []
    for path in _core_files():
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
    for path in _core_files():
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
# Check 4: quarantine tables and predicates are frozen
#
# This includes LOCAL_SWAP_BACKENDS == {kobold}: a second managed engine
# fails here and forces an explicit, reviewed edit of this inventory.
# ---------------------------------------------------------------------------

# P2-3 transitional inventory. Every table and its consumer predicate is
# explicit; adding a capability must be reviewed as a service-data migration.
_QUARANTINE_TABLES: dict[str, frozenset[str]] = {
    "_HAS_AUX_MMPROJ": frozenset({"kobold"}),
    "_VISION_VIA_AUX": frozenset({"kobold"}),
    "_CONTEXT_WINDOW_FROM_LIVE_PROBE": frozenset({"kobold"}),
}

_QUARANTINE_PREDICATES: dict[str, str] = {
    "has_aux_mmproj": "_HAS_AUX_MMPROJ",
    "vision_via_aux": "_VISION_VIA_AUX",
    "context_window_from_live_probe": "_CONTEXT_WINDOW_FROM_LIVE_PROBE",
}


def _check_quarantine_frozen(source: str | None = None) -> list[str]:
    """Freeze the 6 literal sets and their exact one-lookup predicate shapes."""
    tree = ast.parse(source if source is not None else _QUARANTINE.read_text(encoding="utf-8"))
    violations: list[str] = []
    tables: dict[str, ast.expr] = {}
    predicates: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # module docstring
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in tables:
                violations.append(f"engine_quarantine.py:{node.lineno}: duplicate table {node.target.id}")
            tables[node.target.id] = node.value
        elif isinstance(node, ast.FunctionDef):
            if node.name in predicates:
                violations.append(f"engine_quarantine.py:{node.lineno}: duplicate predicate {node.name}")
            predicates[node.name] = node
        else:
            violations.append(f"engine_quarantine.py:{node.lineno}: unreviewed top-level code")
    if tables.keys() != _QUARANTINE_TABLES.keys():
        violations.append(f"quarantine tables changed: expected {sorted(_QUARANTINE_TABLES)}, found {sorted(tables)}")
    for name, expected in _QUARANTINE_TABLES.items():
        value = tables.get(name)
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset" and len(value.args) == 1
                and not value.keywords and isinstance(value.args[0], ast.Set)
                and all(isinstance(item, ast.Constant) and isinstance(item.value, str)
                        for item in value.args[0].elts)
                and frozenset(item.value for item in value.args[0].elts) == expected):
            violations.append(f"quarantine table {name} changed value or definition")
    if predicates.keys() != _QUARANTINE_PREDICATES.keys():
        violations.append(f"quarantine predicates changed: expected {sorted(_QUARANTINE_PREDICATES)}, found {sorted(predicates)}")
    for name, table in _QUARANTINE_PREDICATES.items():
        node = predicates.get(name)
        if node is None:
            continue
        body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                   and isinstance(node.body[0].value, ast.Constant)
                                   and isinstance(node.body[0].value.value, str)) else node.body
        expected_return = ast.parse(f"return backend in {table}").body[0]
        if (node.decorator_list or len(node.args.args) != 1 or node.args.args[0].arg != "backend"
                or node.args.posonlyargs or node.args.kwonlyargs or node.args.vararg
                or node.args.kwarg or node.args.defaults or node.args.kw_defaults
                or len(body) != 1 or ast.dump(body[0], include_attributes=False)
                != ast.dump(expected_return, include_attributes=False)):
            violations.append(f"quarantine predicate {name} changed shape")
    return violations


# Reviewed semantic findings from the audit. These anchors make deletion or
# relocation visible in this guard. They do not prove the policies are safe;
# behavior/config tests are needed for each migration. P2-7 lives in web TSX
# and is outside this Python-only guard.
_SEMANTIC_POLICY_ANCHORS: dict[str, dict[str, set[str]]] = {
    "P2-2 reasoning dialect": {
        "gateway/transforms.py": {"ReasoningEffortTransform", "apply_thinking_suppression"},
    },
    "P2-5 audio intake": {
        "gateway/voice_audio.py": {"SampleLimits", "process_sample"},
        "proxy.py": {"_probe_voice_quality"},
    },
    "P2-8 public design alias": {"gateway/voices_api.py": {"register_voice_routes"}},
    "P2-9 generic STT planning": {
        "gateway/stt_chunking.py": {"SttExecutionConfig", "plan_silence_only_segments"},
    },
    "P2-10 think format": {
        "gateway/transforms.py": {"split_reasoning_visible_text", "StreamingThinkSplitTransform"},
    },
}


# Findings migrated to service data. Their compiled tables must not return;
# the differential proof is tests/test_engine_p2_differential.py.
_MIGRATED_POLICY_SYMBOLS: dict[str, dict[str, set[str]]] = {
    "P2-1 voice compiler": {
        "gateway/voice_design.py": {"_CATEGORIES", "_ORDER", "_NOISE", "VOCABULARY"},
    },
    "P2-4 voice catalog": {
        "gateway/transforms.py": {
            "DEFAULT_TTS_VOICE", "CANONICAL_TTS_VOICES", "LEGACY_TTS_VOICE_ALIASES", "TTS_LANGUAGE_PRESET_VOICES",
        },
        "gateway/custom_voices.py": {"RESERVED_NAMES"},
        "proxy.py": {"_voxtral_voice_map", "_voice_metadata", "_engine_voice", "_LANG_NAMES"},
    },
    "P2-6 STT diagnostics": {"gateway/stt_chunking.py": {"_ENGINE_DIAGNOSTIC_FIELDS"}},
}


def _top_level_names(rel: str) -> set[str]:
    tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    found = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    found.update(
        node.target.id for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )
    found.update(
        target.id for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    )
    return found


def _check_migrated_policy_symbols() -> list[str]:
    return [
        f"{finding}: {rel} defines migrated policy {sorted(returned)}; the policy is service data now"
        for finding, files in _MIGRATED_POLICY_SYMBOLS.items()
        for rel, symbols in files.items()
        if (returned := symbols & _top_level_names(rel))
    ]


def _check_semantic_policy_anchors() -> list[str]:
    violations: list[str] = []
    for finding, files in _SEMANTIC_POLICY_ANCHORS.items():
        for rel, expected in files.items():
            if missing := expected - _top_level_names(rel):
                violations.append(f"{finding}: {rel} missing reviewed anchors {sorted(missing)}")
    return violations


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
# Check 6: engine-knowledge RATCHET (no allowlist, counts only fall)
#
# Check 1 only sees engine literals inside branch conditions and only for LLM
# engine names. The audit (engine-knowledge-audit-2026-10-12) found ~95 items
# of engine knowledge that pass it: media engines, literals passed as
# arguments, `engine_id == "omnivoice"`, engine-named identifiers.
#
# This check counts EVERY engine-name hit in production Python, per file, and
# compares the count with tests/engine_knowledge_baseline.json:
#   - a count above its baseline fails (new engine knowledge);
#   - a file with hits that is not in the baseline fails (new file);
#   - a count below its baseline fails too, until the baseline is lowered, so
#     a deletion cannot be given back later.
# Lower the baseline with:  python tests/test_engine_knowledge_boundary.py --lower-baseline
# The tool refuses to raise a count. There is no allowlist: the target is an
# empty baseline.
#
# A "hit" is one AST node, outside docstrings, whose text names an engine:
# a string literal, a name, an attribute, a def/class name, an argument name
# or a keyword name. Comments are not code and are not counted.
# Served client protocols (openai, anthropic, ollama, a1111, mcp) and the
# gateway's own LiteLLM library are not engine names here. Branches on
# `ollama` stay under check 1.
# ---------------------------------------------------------------------------

_BASELINE_PATH = Path(__file__).parent / "engine_knowledge_baseline.json"

_ENGINE_KNOWLEDGE_PATTERN = re.compile(
    r"(?<![a-z])(?:"
    r"comfy|vllm|sglang|kobold|tabby|llama|gguf|mmproj|lmstudio|lm-studio|"
    r"qwen|gemma|mistral|hermes|functionary|command_r|tgi(?![a-z])|"
    r"voxtral|parakeet|chatterbox|omnivoice|kokoro|supertonic|whisper|"
    r"flux|ltx|ace[-_]?step|ideogram|minimax|seedvr"
    r")",
    re.IGNORECASE,
)


def _production_files() -> list[Path]:
    """Every production Python module: proxy.py, root modules, gateway/**."""
    return [
        *sorted(_REPO_ROOT.glob("*.py")),
        *sorted((_REPO_ROOT / "gateway").rglob("*.py")),
        *sorted(_FAKE_UPSTREAM.rglob("*.py")),
    ]


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _count_engine_knowledge(source: str, filename: str = "<source>") -> int:
    tree = ast.parse(source, filename=filename)
    skip = _docstring_ids(tree)
    hits = 0
    for node in ast.walk(tree):
        texts: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            texts.append(node.value)
        elif isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.arg):
            texts.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(node.arg)
        if any(_ENGINE_KNOWLEDGE_PATTERN.search(text) for text in texts):
            hits += 1
    return hits


def _engine_knowledge_counts(
    _sources: dict[str, str] | None = None,
) -> dict[str, int]:
    """Hits per file; files without hits are omitted."""
    if _sources is None:
        _sources = {
            path.relative_to(_REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in _production_files()
        }
    counts = {rel: _count_engine_knowledge(src, rel) for rel, src in sorted(_sources.items())}
    return {rel: n for rel, n in counts.items() if n}


def _load_baseline() -> dict[str, int]:
    import json
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))["files"]


def _ratchet_violations(counts: dict[str, int], baseline: dict[str, int]) -> list[str]:
    violations: list[str] = []
    for rel, count in sorted(counts.items()):
        if rel not in baseline:
            violations.append(f"{rel}: new file with {count} engine-knowledge hit(s)")
        elif count > baseline[rel]:
            violations.append(f"{rel}: engine-knowledge hits rose {baseline[rel]} -> {count}")
        elif count < baseline[rel]:
            violations.append(
                f"{rel}: engine-knowledge hits fell {baseline[rel]} -> {count}; "
                "lower the baseline (python tests/test_engine_knowledge_boundary.py --lower-baseline)"
            )
    for rel, base in sorted(baseline.items()):
        if rel not in counts:
            violations.append(
                f"{rel}: baseline lists {base} hit(s) but the file has none or is gone; "
                "lower the baseline (python tests/test_engine_knowledge_boundary.py --lower-baseline)"
            )
    return violations


def _hit_lines(rel: str, source: str) -> list[str]:
    """Locate hits for a failure message."""
    tree = ast.parse(source, filename=rel)
    skip = _docstring_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            text = node.value
        elif isinstance(node, ast.Name):
            text = node.id
        elif isinstance(node, ast.Attribute):
            text = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = node.name
        elif isinstance(node, ast.arg):
            text = node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            text = node.arg
        if text is not None and _ENGINE_KNOWLEDGE_PATTERN.search(text):
            found.append(f"{rel}:{getattr(node, 'lineno', '?')}: {text[:80]!r}")
    return found


def _lower_baseline() -> None:
    import json
    counts = _engine_knowledge_counts()
    baseline = _load_baseline() if _BASELINE_PATH.exists() else {}
    raised = [rel for rel, n in counts.items() if n > baseline.get(rel, 0)]
    if raised:
        raise SystemExit(f"refusing to raise the baseline for: {', '.join(raised)}")
    _BASELINE_PATH.write_text(
        json.dumps({"files": counts}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"baseline written: {sum(counts.values())} hits in {len(counts)} files")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_RETIRED_EXECUTOR_PATTERN = re.compile(
    r"\b(?:proxy_request|load_model|TABBY_LOAD_TIMEOUT|MAX_LOAD_FAILURES|"
    r"LOAD_FAILURE_BACKOFF)\b"
)


def _check_retired_executor_symbols(
    _source_lines: list[tuple[str, list[str]]] | None = None,
) -> list[str]:
    if _source_lines is None:
        paths = [_PROXY, *sorted((_REPO_ROOT / "gateway").rglob("*.py"))]
        _source_lines = [
            (str(path.relative_to(_REPO_ROOT)), _lines(path)) for path in paths
        ]
    return [
        f"{rel}:{lineno}: retired symbol {match.group()!r}"
        for rel, lines in _source_lines
        for lineno, line in enumerate(lines, start=1)
        for match in _RETIRED_EXECUTOR_PATTERN.finditer(line)
    ]


def test_retired_executor_symbols_do_not_return_to_core() -> None:
    violations = _check_retired_executor_symbols()
    assert not violations, (
        "The retired executor and loader must not return to core:\n"
        + "\n".join(violations)
    )


def test_retirement_guard_catches_calls_definitions_and_dynamic_names() -> None:
    violations = _check_retired_executor_symbols([("gateway/example.py", [
        "await proxy_request(request, path)",
        "async def load_model (",
        'getattr(proxy, "TABBY_LOAD_TIMEOUT")',
        "MAX_LOAD_FAILURES = 3",
        "retry = LOAD_FAILURE_BACKOFF",
        "await unload_model()",
        "await _forward_litellm_proxy_request(request)",
        'config["load_timeout_s"] = 300',
    ])])
    assert violations == [
        f"gateway/example.py:{lineno}: retired symbol {name!r}"
        for lineno, name in enumerate([
            "proxy_request", "load_model", "TABBY_LOAD_TIMEOUT",
            "MAX_LOAD_FAILURES", "LOAD_FAILURE_BACKOFF",
        ], start=1)
    ]


class TestEngineKnowledgeBoundary:
    """Static boundary guard — these are the checks that keep the treadmill off."""

    def test_no_new_engine_named_branches(self) -> None:
        """No engine-named string literal should appear in a branch condition
        outside the allowlist.

        Engine names in conditions or comparisons suggest per-engine paths.
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

    def test_quarantine_tables_and_predicates_frozen(self) -> None:
        violations = _check_quarantine_frozen()
        assert not violations, _fail_message("quarantine inventory", violations)

    def test_reviewed_semantic_policy_anchors_are_tracked(self) -> None:
        violations = _check_semantic_policy_anchors()
        assert not violations, _fail_message("semantic policy inventory", violations)

    def test_migrated_policy_tables_do_not_return(self) -> None:
        violations = _check_migrated_policy_symbols()
        assert not violations, _fail_message("migrated policy tables", violations)

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
            # An engine-agnostic line (calls the quarantine predicate, no engine
            # literal) — must NOT be flagged.
            'if engine_quarantine.has_aux_mmproj(backend): pass',
            # A synthetic new inline engine branch — must BE flagged.
            'if backend == "vllm": pass',            # not in allowlist → violation
        ]
        violations = _check_engine_named_branches(
            _source_lines=[("proxy.py", synthetic_lines)]
        )
        # The quarantine-predicate line must NOT appear in violations
        agnostic_reported = [v for v in violations if "has_aux_mmproj" in v]
        assert not agnostic_reported, (
            "Guard incorrectly flagged an engine-agnostic predicate call: "
            + "\n".join(agnostic_reported)
        )
        # The synthetic vllm line MUST appear in violations
        vllm_reported = [v for v in violations if "'vllm'" in v]
        assert vllm_reported, (
            "Guard failed to catch a synthetic new engine branch "
            '(`if backend == "vllm":`) — the boundary check is broken!'
        )

    def test_guard_scans_the_fake_upstream_package(self) -> None:
        """A fake upstream that branches on an engine name fails like core code."""
        scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in _core_files()}
        fake_modules = {
            path.relative_to(_REPO_ROOT).as_posix() for path in _FAKE_UPSTREAM.rglob("*.py")
        }
        assert "tests/support/fake_upstream/app.py" in fake_modules
        assert fake_modules <= scanned
        violations = _check_engine_named_branches(
            _source_lines=[("tests/support/fake_upstream/app.py", ['if upstream.engine == "ollama": pass'])]
        )
        assert [v.split(": ", 1)[0] for v in violations] == ["tests/support/fake_upstream/app.py:1"]

    def test_multiline_and_expression_engine_branches_are_caught(self) -> None:
        sources = [("gateway/new_adapter.py", [
            "if (backend ==",
            '        "vllm"): pass',
            'selected = "tabby" if backend == "tabby" else "other"',
            'while backend in ("kobold",): break',
            'is_special = backend == "gemma"',
            'name = "ollama"',
        ])]
        violations = _check_engine_named_branches(sources)
        assert len(violations) == 4
        assert all("gateway/new_adapter.py" in item for item in violations)

    def test_loop_and_comprehension_iterables_with_engine_literals_are_caught(self) -> None:
        sources = [("gateway/new_adapter.py", [
            'for backend in ("vllm",): pass',
            'async def check():',
            '    async for backend in stream("tabby"): pass',
            'engines = [backend for backend in ("kobold",)]',
            'engines = (backend for backend in (item for item in ("functionary",)))',
        ])]
        violations = _check_engine_named_branches(sources)
        assert len(violations) == 4
        assert all("gateway/new_adapter.py" in item for item in violations)

    def test_match_cases_and_guards_with_engine_literals_are_caught(self) -> None:
        sources = [("gateway/new_adapter.py", [
            "match backend:",
            '    case "vllm": pass',
            '    case "tabby" | "kobold": pass',
            '    case "generic": pass',
            "match backend:",
            '    case _ if supports("functionary"): pass',
        ])]
        violations = _check_engine_named_branches(sources)
        assert len(violations) == 3
        assert all("gateway/new_adapter.py" in item for item in violations)

    def test_quarantine_freeze_rejects_new_backend_table_and_predicate(self) -> None:
        source = _QUARANTINE.read_text(encoding="utf-8")
        assert not _check_quarantine_frozen(source)
        for name in _QUARANTINE_TABLES:
            pattern = rf'({re.escape(name)}: frozenset\[str\] = frozenset\(\{{)'
            changed, count = re.subn(pattern, r'\g<1>"vllm", ', source, count=1)
            assert count == 1, name
            assert any(name in violation for violation in _check_quarantine_frozen(changed)), name
        assert _check_quarantine_frozen(source + '\n_NEW_BACKENDS = frozenset({"vllm"})\n')
        assert _check_quarantine_frozen(source.replace(
            "return backend in _HAS_AUX_MMPROJ",
            "return True",
        ))


class TestEngineKnowledgeRatchet:
    """Per-file engine-name counts may only fall (check 6)."""

    def test_counts_match_the_checked_in_baseline(self) -> None:
        counts = _engine_knowledge_counts()
        violations = _ratchet_violations(counts, _load_baseline())
        if violations:
            rising = {v.split(":", 1)[0] for v in violations if "rose" in v or "new file" in v}
            for rel in sorted(rising):
                violations.extend(
                    "    " + line for line in _hit_lines(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8"))[:40]
                )
        assert not violations, _fail_message("engine-knowledge ratchet", violations)

    def test_scope_covers_root_modules_gateway_and_media_engine_names(self) -> None:
        scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in _production_files()}
        assert {"proxy.py", "status_endpoint.py", "firehose_render.py",
                "responses_translator.py", "metrics_sampler.py"} <= scanned
        assert "gateway/transforms.py" in scanned
        for name in ("comfyui", "voxtral", "omnivoice", "chatterbox", "kokoro",
                     "parakeet", "whisper", "flux2-klein-4b", "ltx-2.5", "ace-step", "kobold", "tabby"):
            assert _ENGINE_KNOWLEDGE_PATTERN.search(name), name
        for protocol in ("openai", "anthropic", "ollama", "a1111", "litellm", "mcp"):
            assert not _ENGINE_KNOWLEDGE_PATTERN.search(protocol), protocol

    def test_counts_literals_outside_if_conditions_and_identifiers(self) -> None:
        source = "\n".join([
            '"""docstring that mentions comfyui is not code"""',
            "# comment about voxtral is not code",
            'def pick(engine_id):',
            '    """tabby in a docstring is not counted"""',
            '    if engine_id == "omnivoice": return 1',   # 1 literal
            '    return call(host="comfyui")',             # 1 literal
            "def currently_loaded_kobold_model(): pass",   # 1 def name
            "x = TABBY_HOST",                              # 1 name
            'y = "plain"',
        ])
        assert _count_engine_knowledge(source) == 4

    def test_ratchet_rejects_a_rise_a_new_file_and_a_stale_baseline(self) -> None:
        baseline = {"proxy.py": 3, "gateway/a.py": 2}
        assert _ratchet_violations({"proxy.py": 3, "gateway/a.py": 2}, baseline) == []
        assert any("rose 3 -> 4" in v for v in _ratchet_violations({"proxy.py": 4, "gateway/a.py": 2}, baseline))
        assert any("new file" in v for v in _ratchet_violations(
            {"proxy.py": 3, "gateway/a.py": 2, "gateway/new.py": 1}, baseline))
        assert any("fell 3 -> 2" in v for v in _ratchet_violations({"proxy.py": 2, "gateway/a.py": 2}, baseline))
        assert any("gateway/a.py" in v for v in _ratchet_violations({"proxy.py": 3}, baseline))

    def test_mutant_new_engine_literal_branch_fails_the_ratchet(self) -> None:
        sources = {
            rel: (_REPO_ROOT / rel).read_text(encoding="utf-8")
            for rel in _load_baseline()
        }
        assert _ratchet_violations(_engine_knowledge_counts(sources), _load_baseline()) == []
        sources["gateway/lifecycle_drivers.py"] += '\n\ndef _mutant(engine_id):\n    if engine_id == "newengine-flux9":\n        return 1\n'
        sources["gateway/brand_new_module.py"] = 'DEFAULT = "chatterbox"\n'
        violations = _ratchet_violations(_engine_knowledge_counts(sources), _load_baseline())
        assert any("gateway/lifecycle_drivers.py" in v and "rose" in v for v in violations)
        assert any("gateway/brand_new_module.py" in v and "new file" in v for v in violations)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--lower-baseline"]:
        _lower_baseline()
    else:
        raise SystemExit("usage: python tests/test_engine_knowledge_boundary.py --lower-baseline")
