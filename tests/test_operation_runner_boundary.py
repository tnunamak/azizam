"""Criterion 3: the runner (``gateway/engine_protocol/jobs.py``) names no engine, adapter, node or backend.

The check reads the module's syntax tree. Docstrings are skipped (they may describe the boundary). Everything else is
code: imports, names, attributes and string literals. The runner may reach an adapter only through the interface the
caller hands it, so it must not import a concrete adapter, nor spell a backend, a node id or a prompt id.
"""

from __future__ import annotations

import ast

import pytest
import re
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "gateway" / "engine_protocol" / "jobs.py"

# Concrete adapter modules and classes the runner must never import or name.
FORBIDDEN_MODULES = ("node_graph", "executor", "inline_tts", "chunked_stt", "audio_graph", "dispatch")
FORBIDDEN_NAMES = ("NodeGraphAdapter", "HttpV1Executor", "InlineTts", "ChunkedStt", "ComfyUI", "Comfy")
# Words that name a backend, a protocol, or a node/prompt id in a literal.
FORBIDDEN_LITERAL = re.compile(r"comfy|prompt_id|node_id|node-graph|node_graph|http-v1|inline-tts|chunked-stt", re.I)
DIGITS_ONLY = re.compile(r"^\d+$")
FORBIDDEN_IDENTIFIER = re.compile(r"comfy|node_graph|nodegraph|http_?v1|inline_?tts|chunked_?stt|prompt_id|node_id", re.I)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                skipped.add(id(body[0].value))
    return skipped


def _folded_text(node: ast.AST) -> str | None:
    """The text a chain of ``+`` between string literals builds, or ``None`` when any part is not a literal.

    Implicit concatenation (``"a" "b"``) is one Constant in the tree already; explicit ``+`` is folded here, so a name
    cut into pieces is seen whole.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded_text(node.left), _folded_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)
    skipped = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(part in module for part in FORBIDDEN_MODULES):
                found.append(f"line {node.lineno}: imports from {module!r}")
            for alias in node.names:
                if FORBIDDEN_NAMES.count(alias.name) or FORBIDDEN_IDENTIFIER.search(alias.name):
                    found.append(f"line {node.lineno}: imports {alias.name!r}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(part in alias.name for part in FORBIDDEN_MODULES):
                    found.append(f"line {node.lineno}: imports {alias.name!r}")
        elif isinstance(node, ast.Name):
            if FORBIDDEN_NAMES.count(node.id) or FORBIDDEN_IDENTIFIER.search(node.id):
                found.append(f"line {node.lineno}: names {node.id!r}")
        elif isinstance(node, ast.Attribute):
            if FORBIDDEN_IDENTIFIER.search(node.attr):
                found.append(f"line {node.lineno}: attribute {node.attr!r}")
        elif isinstance(node, ast.arg):
            if FORBIDDEN_IDENTIFIER.search(node.arg):
                found.append(f"line {node.lineno}: parameter {node.arg!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            value = node.value
            if FORBIDDEN_LITERAL.search(value):
                found.append(f"line {node.lineno}: literal {value!r}")
            elif DIGITS_ONLY.match(value):
                found.append(f"line {node.lineno}: literal {value!r} is a bare id")
        elif isinstance(node, ast.BinOp) and _folded_text(node) is not None:
            value = _folded_text(node)
            if FORBIDDEN_LITERAL.search(value):
                found.append(f"line {node.lineno}: literal {value!r} (concatenated)")
            elif DIGITS_ONLY.match(value):
                found.append(f"line {node.lineno}: literal {value!r} is a bare id (concatenated)")
    return found


def test_the_runner_names_no_engine_adapter_node_or_backend():
    source = RUNNER.read_text(encoding="utf-8")
    assert ast.parse(source).body, "the runner module is empty"
    assert _violations(source) == []


def test_the_rule_catches_each_kind_of_violation():
    """The check is only as good as its detectors: each kind of name it forbids must be found."""
    samples = {
        "from gateway.engine_protocol.node_graph import NodeGraphAdapter\n": "imports from",
        "x = NodeGraphAdapter()\n": "names",
        "x = 'comfy'\n": "literal",
        "x = {'prompt_id': 1}\n": "literal",
        "x = '42'\n": "bare id",
        "y = obj.node_id\n": "attribute",
        "def f(http_v1): pass\n": "parameter",
        "x = 'com' + 'fy'\n": "literal",
    }
    for source, kind in samples.items():
        assert any(kind in finding for finding in _violations(source)), (source, _violations(source))


def test_docstrings_may_describe_the_boundary():
    assert _violations('"""Mentions ComfyUI and node_id and 42."""\n') == []


# --- criterion 7 (D1): the adapter and the runner name no intent or workflow id ---------------------------------------

ADAPTER = Path(__file__).resolve().parent.parent / "gateway" / "engine_protocol" / "node_graph.py"
MANIFEST_DIRS = (Path(__file__).resolve().parent.parent / "manifests" / "workflows",
                 Path(__file__).resolve().parent / "fixtures" / "engine_protocol" / "d1")
# Modules that name no workflow: the adapter and the runner, and nothing else that a caller could use to reach one.
NAMELESS_MODULES = (ADAPTER, RUNNER)
# Module names that carry one workflow's compile rules; the adapter must not import them.
RETIRED_COMPILE_MODULES = ("audio_graph",)


def _workflow_names() -> set[str]:
    """Every intent and workflow id declared by a manifest: data, read at test time, never written into the code."""
    import yaml

    names: set[str] = set()
    for directory in MANIFEST_DIRS:
        for path in directory.glob("*.yml"):
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            for key in ("intent", "id"):
                if isinstance(manifest, dict) and isinstance(manifest.get(key), str):
                    names.add(manifest[key])
    return names


def _workflow_violations(source: str, names: set[str]) -> list[str]:
    tree = ast.parse(source)
    skipped = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = [node.module or "", *(alias.name for alias in node.names)]
            if any(part in name for part in RETIRED_COMPILE_MODULES for name in imported):
                found.append(f"line {node.lineno}: imports from {node.module!r}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(part in alias.name for part in RETIRED_COMPILE_MODULES):
                    found.append(f"line {node.lineno}: imports {alias.name!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            for name in names:
                if name.lower() in node.value.lower():
                    found.append(f"line {node.lineno}: literal {node.value!r} names {name!r}")
        elif isinstance(node, (ast.Name, ast.Attribute, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.arg)):
            # A declared name that is also a legal identifier must not appear as one either.
            identifier = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None) \
                or getattr(node, "arg", None)
            if identifier in names:
                found.append(f"line {node.lineno}: identifier {identifier!r} names a workflow")
        elif isinstance(node, ast.BinOp) and _folded_text(node) is not None:
            value = _folded_text(node)
            for name in names:
                if name.lower() in value.lower():
                    found.append(f"line {node.lineno}: literal {value!r} (concatenated) names {name!r}")
    return found


def test_the_adapter_and_runner_name_no_intent_or_workflow_id():
    names = _workflow_names()
    assert "audio.generate" in names and "smell.infer" in names, "the manifest scan found too few names"
    for module in NAMELESS_MODULES:
        assert _workflow_violations(module.read_text(encoding="utf-8"), names) == [], module.name


def test_the_workflow_rule_catches_a_named_workflow():
    names = {"audio.generate", "audio.generate.tangoflux"}
    assert _workflow_violations('x = "audio.generate"\n', names)
    assert _workflow_violations("from gateway.engine_protocol import audio_graph\n", names)
    assert _workflow_violations('"""audio.generate in a docstring is fine"""\n', names) == []


@pytest.mark.parametrize("source", [
    pytest.param("import gateway.engine_protocol.audio_graph\n", id="import-retired-module"),
    pytest.param("import gateway.engine_protocol.audio_graph as legacy\n", id="import-retired-module-aliased"),
    pytest.param('x = "audio." + "generate"\n', id="concatenated-operators"),
    pytest.param('x = "audio" + "." + "generate"\n', id="concatenated-three-parts"),
    pytest.param('x = ("audio." "generate")\n', id="implicit-concatenation"),
    pytest.param('x = "smell" + ".infer"\n', id="concatenated-workflow-id"),
    pytest.param("x = custom_workflow()\n", id="identifier-call"),
    pytest.param("def custom_workflow():\n    pass\n", id="identifier-def"),
    pytest.param("x = registry.custom_workflow\n", id="identifier-attribute"),
])
def test_the_workflow_rule_catches_import_and_literal_concatenation(source):
    names = {"audio.generate", "smell.infer", "custom_workflow"}
    assert _workflow_violations(source, names), source


@pytest.mark.parametrize("source", [
    pytest.param('x = "com" + "fy"\n', id="runner-concatenated-backend"),
    pytest.param('x = "node" "_id"\n', id="runner-implicit-concatenation"),
])
def test_the_runner_rule_catches_concatenated_backend_words(source):
    assert _violations(source), source
