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


_UNFOLDED = object()


def _folded_value(node: ast.AST, bindings=None):
    """Fold syntax using known literal bindings, never execute source code."""
    bindings = {} if bindings is None else bindings
    if isinstance(node, ast.Name):
        return bindings.get(node.id, _UNFOLDED)
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded_value(node.left, bindings), _folded_value(node.right, bindings)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
                continue
            if not isinstance(part, ast.FormattedValue):
                return _UNFOLDED
            value = _folded_value(part.value, bindings)
            spec = "" if part.format_spec is None else _folded_text(part.format_spec, bindings)
            if value is _UNFOLDED or spec is None:
                return _UNFOLDED
            if part.conversion == ord("s"):
                value = str(value)
            elif part.conversion == ord("r"):
                value = repr(value)
            elif part.conversion == ord("a"):
                value = ascii(value)
            elif part.conversion != -1:
                return _UNFOLDED
            try:
                parts.append(format(value, spec))
            except (ValueError, TypeError):
                return _UNFOLDED
        return "".join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = _folded_text(node.func.value, bindings)
        if receiver is None:
            return _UNFOLDED
        if node.func.attr == "join" and len(node.args) == 1 and not node.keywords:
            values = node.args[0]
            if isinstance(values, (ast.List, ast.Tuple)):
                parts = [_folded_text(value, bindings) for value in values.elts]
                if all(isinstance(part, str) for part in parts):
                    return receiver.join(parts)
        if node.func.attr == "format":
            args = [_folded_value(value, bindings) for value in node.args]
            kwargs = {item.arg: _folded_value(item.value, bindings) for item in node.keywords}
            if None in kwargs or any(value is _UNFOLDED for value in [*args, *kwargs.values()]):
                return _UNFOLDED
            try:
                return receiver.format(*args, **kwargs)
            except (ValueError, TypeError, IndexError, KeyError, AttributeError):
                return _UNFOLDED
    return _UNFOLDED


def _folded_text(node: ast.AST, bindings=None) -> str | None:
    """Fold literal string construction without importing or executing source code."""
    value = _folded_value(node, bindings)
    return value if isinstance(value, str) else None



def _bound_names(nodes):
    """Names made local by bindings; nested scopes own their bindings."""
    pending = list(nodes)
    names = set()
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        pending.extend(ast.iter_child_nodes(node))
    return names


def _nodes_with_bindings(tree):
    """Visit lexical scopes in source order, keeping only proven literal values."""
    def forget(target, bindings):
        for child in ast.walk(target):
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                bindings.pop(child.id, None)

    def walk(node, bindings, deferred_bindings=None):
        def descend(child, local):
            yield from walk(child, local, deferred_bindings)

        yield node, dict(bindings)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Defaults and annotations belong to the enclosing scope.
            for child in ast.iter_child_nodes(node):
                if child is node.args or child in getattr(node, "decorator_list", ()) or child is getattr(node, "returns", None):
                    yield from descend(child, bindings)
            # Deferred bodies cannot assume enclosing values at call time.
            # Methods also do not resolve bare names through class locals.
            local = dict(deferred_bindings) if isinstance(node, ast.Lambda) and deferred_bindings is not None else {}
            if isinstance(node, ast.Lambda):
                for argument in ast.walk(node.args):
                    if isinstance(argument, ast.arg):
                        local.pop(argument.arg, None)
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            if not isinstance(node, ast.Lambda):
                bindings.pop(node.name, None)
            for child in body:
                yield from walk(child, local)
            return
        if isinstance(node, ast.ClassDef):
            for child in [*node.decorator_list, *node.bases, *node.keywords]:
                yield from descend(child, bindings)
            bindings.pop(node.name, None)
            local = dict(bindings)
            for child in node.body:
                yield from descend(child, local)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            if node.value is not None:
                yield from descend(node.value, bindings)
            if isinstance(node, ast.AnnAssign):
                yield from descend(node.annotation, bindings)
            value = _folded_value(node.value, bindings) if node.value is not None else _UNFOLDED
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                yield from descend(target, bindings)
                forget(target, bindings)
                if isinstance(target, ast.Name) and value is not _UNFOLDED:
                    bindings[target.id] = value
            return
        if isinstance(node, (ast.AugAssign, ast.Delete)):
            for child in ast.iter_child_nodes(node):
                yield from descend(child, bindings)
            for target in node.targets if isinstance(node, ast.Delete) else [node.target]:
                forget(target, bindings)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bindings.pop(alias.asname or alias.name.split(".")[0], None)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            changed = _bound_names(ast.iter_child_nodes(node))
            # Only eager iterable parts run before the target/body can rebind names.
            if isinstance(node, (ast.For, ast.AsyncFor)):
                deferred = {name: value for name, value in bindings.items() if name not in changed}
                yield from walk(node.iter, dict(bindings), deferred)
            # Any iteration may follow writes from a previous iteration.
            # Neither a while test nor the else suite can rely on entry literals.
            for name in changed:
                bindings.pop(name, None)
            if isinstance(node, ast.While):
                yield from descend(node.test, dict(bindings))
            else:
                yield from descend(node.target, dict(bindings))
            for suite in (node.body, node.orelse):
                local = dict(bindings)
                for child in suite:
                    yield from descend(child, local)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                yield from descend(item.context_expr, bindings)
                if item.optional_vars is not None:
                    forget(item.optional_vars, bindings)
                    yield from descend(item.optional_vars, bindings)
            for child in node.body:
                yield from descend(child, bindings)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local = dict(bindings)
            deferred_bindings = None if deferred_bindings is None else dict(deferred_bindings)
            for index, generator in enumerate(node.generators):
                yield from descend(generator.iter, local)
                # A generator evaluates its first iterable now, all remaining parts on next().
                if isinstance(node, ast.GeneratorExp) and index == 0 and deferred_bindings is not None:
                    local = dict(deferred_bindings)
                forget(generator.target, local)
                if deferred_bindings is not None:
                    forget(generator.target, deferred_bindings)
                yield from descend(generator.target, local)
                for condition in generator.ifs:
                    yield from descend(condition, local)
            for child in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                yield from descend(child, local)
            return
        if isinstance(node, (ast.If, ast.Try)):
            # Branch-dependent writes cannot establish constants after a join.
            if hasattr(node, "test"):
                yield from descend(node.test, bindings)
            for field in ("body", "orelse", "handlers", "finalbody"):
                local = dict(bindings)
                if isinstance(node, ast.Try) and field != "body":
                    changed = list(node.body)
                    if field == "finalbody":
                        # Finally follows either success or an exception,
                        # including writes made by an else suite or handler.
                        changed.extend(node.handlers)
                        changed.extend(node.orelse)
                    for name in _bound_names(changed):
                        local.pop(name, None)
                for child in getattr(node, field, ()):
                    yield from descend(child, local)
            for name in _bound_names(ast.iter_child_nodes(node)):
                bindings.pop(name, None)
            return
        if isinstance(node, ast.ExceptHandler) and node.name:
            bindings.pop(node.name, None)
        for child in ast.iter_child_nodes(node):
            yield from descend(child, bindings)

    yield from walk(tree, {})

def _violations(source: str) -> list[str]:
    tree = ast.parse(source)
    skipped = _docstring_nodes(tree)
    found: list[str] = []
    for node, bindings in _nodes_with_bindings(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"getattr", "setattr"}:
            attribute = node.args[1] if len(node.args) > 1 else None
            if attribute is None or _folded_text(attribute, bindings) is None:
                found.append(f"line {node.lineno}: {node.func.id} attribute argument does not fold to a literal")
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
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if FORBIDDEN_IDENTIFIER.search(node.name):
                found.append(f"line {node.lineno}: declaration {node.name!r}")
        elif isinstance(node, ast.keyword):
            if node.arg is not None and FORBIDDEN_IDENTIFIER.search(node.arg):
                found.append(f"line {node.lineno}: keyword {node.arg!r}")
        elif isinstance(node, ast.arg):
            if FORBIDDEN_IDENTIFIER.search(node.arg):
                found.append(f"line {node.lineno}: parameter {node.arg!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            value = node.value
            if FORBIDDEN_LITERAL.search(value):
                found.append(f"line {node.lineno}: literal {value!r}")
            elif DIGITS_ONLY.match(value):
                found.append(f"line {node.lineno}: literal {value!r} is a bare id")
        elif isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)) and _folded_text(node, bindings) is not None:
            value = _folded_text(node, bindings)
            if FORBIDDEN_LITERAL.search(value):
                found.append(f"line {node.lineno}: literal {value!r} (folded)")
            elif DIGITS_ONLY.match(value):
                found.append(f"line {node.lineno}: literal {value!r} is a bare id (folded)")
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

@pytest.mark.parametrize("source", [
    pytest.param('x = f"{\'com\'}{\'fy\'}"\n', id="constant-fstring"),
    pytest.param('x = "".join(["com", "fy"])\n', id="constant-join"),
    pytest.param('x = "{}{}".format("com", "fy")\n', id="constant-format"),
    pytest.param('x = getattr(obj, field)\n', id="dynamic-getattr"),
    pytest.param('setattr(obj, field, value)\n', id="dynamic-setattr"),
    pytest.param('from settings import FIELD\nx = getattr(obj, FIELD)\n', id="imported-attribute"),
    pytest.param('call(comfy=True)\n', id="keyword-name"),
    pytest.param('def comfy(): pass\n', id="function-name"),
    pytest.param('async def comfy(): pass\n', id="async-function-name"),
    pytest.param('class Comfy: pass\n', id="class-name"),
])
def test_runner_boundary_closes_syntax_gaps(source):
    findings = _violations(source)
    assert len(findings) == 1, (source, findings)




@pytest.mark.parametrize("expression", ["A + B", "f'{A}{B}'", "'{}{}'.format(A, B)"])
def test_runner_boundary_folds_names_in_source_order(expression):
    source = "A = 'node'\nB = '_id'\nvalue = result[" + expression + "]\n"
    findings = _violations(source)
    assert len(findings) == 1, findings
    assert "node_id" in findings[0]


@pytest.mark.parametrize("source", [
    "A = 'sta'\nB = 'tus'\nvalue = result[A + B]\n",
    "A = 'node'\ndef f(A):\n    return result[A + '_id']\n",
    "A = 'node'\nA = 'sta'\nvalue = result[A + '_id']\n",
    "A = 'node'\nA = dynamic()\nvalue = result[A + '_id']\n",
    "A = 'node'\nA += suffix\nvalue = result[A + '_id']\n",
    "A = 'node'\ndel A\nvalue = result[A + '_id']\n",
    "A = 'node'\nfor A in values:\n    value = result[A + '_id']\n",
    "A = 'node'\nwith context() as A:\n    value = result[A + '_id']\n",
    "A = 'node'\nimport settings as A\nvalue = result[A + '_id']\n",
    "A = 'node'\nfrom settings import A\nvalue = result[A + '_id']\n",
    "A = 'node'\nvalue = [result[A + '_id'] for A in values]\n",
])
def test_runner_boundary_invalidates_shadowed_or_rebound_names(source):
    assert _violations(source) == []


def test_runner_boundary_function_bindings_do_not_escape():
    source = "A = 'sta'\ndef f():\n    A = 'node'\n    return result[A + '_id']\nvalue = result[A + 'tus']\n"
    findings = _violations(source)
    assert len(findings) == 1 and "node_id" in findings[0], findings


def test_runner_boundary_plain_names_and_folded_attributes_are_clean():
    source = """class Runner:
    def run(self, item):
        call(value=item)
        getattr(item, "sta" + "tus")
        setattr(item, "{}".format("status"), "ready")
        label = f"{'safe'}{' text'}"
        text = "-".join(["safe", "text"])
"""
    assert _violations(source) == []


@pytest.mark.parametrize("source", [
    'getattr(obj, field)\n',
    'setattr(obj, field, value)\n',
])
def test_runner_boundary_reports_unresolved_attribute(source):
    assert "attribute argument does not fold to a literal" in _violations(source)[0]


def test_runner_boundary_folds_derived_bindings_and_keeps_comprehension_scope():
    source = ("A = 'no'\nB = A + 'de'\nC = '_id'\n"
              "items = [result[B + C] for B in values]\n"
              "value = result[''.join([B, C])]\n")
    findings = _violations(source)
    assert len(findings) == 1 and "node_id" in findings[0], findings

@pytest.mark.parametrize("source,line", [
    pytest.param("A = 'status'\ndef f():\n    return getattr(obj, A)\nA = dynamic()\n", 3, id="deferred-function"),
    pytest.param("A = 'status'\nasync def f():\n    return getattr(obj, A)\nA = dynamic()\n", 3, id="deferred-async-function"),
    pytest.param("A = 'status'\nf = lambda: getattr(obj, A)\nA = dynamic()\n", 2, id="deferred-lambda"),
    pytest.param("class C:\n    A = 'status'\n    def m(self):\n        return getattr(obj, A)\n", 4, id="class-local-shadow"),
    pytest.param("A = 'status'\nfor item in values:\n    getattr(obj, A)\n    A = dynamic()\n", 3, id="loop-carried-binding"),
    pytest.param("A = 'status'\nwhile getattr(obj, A):\n    A = dynamic()\n", 2, id="while-test-binding"),
    pytest.param("A = 'status'\ntry:\n    A = dynamic()\nfinally:\n    getattr(obj, A)\n", 5, id="try-finally-binding"),
    pytest.param("A = 'status'\ntry:\n    A = dynamic()\nexcept Exception:\n    getattr(obj, A)\n", 5, id="try-handler-binding"),
    pytest.param("A = 'status'\ntry:\n    pass\nexcept Exception:\n    A = dynamic()\nfinally:\n    getattr(obj, A)\n", 7, id="handler-finally-binding"),
])
def test_runner_boundary_rejects_unproven_deferred_or_carried_bindings(source, line):
    assert _violations(source) == [
        f"line {line}: getattr attribute argument does not fold to a literal"
    ]


def test_runner_boundary_keeps_local_literals_and_eager_defaults():
    source = ("A = 'status'\n"
              "def f(value=getattr(obj, A)):\n"
              "    B = 'status'\n"
              "    return getattr(obj, B)\n"
              "g = lambda value=getattr(obj, A): getattr(obj, 'status')\n")
    assert _violations(source) == []


def test_runner_boundary_for_iterable_uses_entry_bindings():
    source = "A = 'node'\nfor item in result[A + '_id']:\n    A = dynamic()\n"
    assert _violations(source) == ["line 2: literal 'node" + "_id' (folded)"]


def test_runner_boundary_for_safe_iterable_keeps_entry_binding():
    source = "A = 'status'\nfor item in getattr(obj, A):\n    A = dynamic()\n"
    assert _violations(source) == []


def test_runner_boundary_async_for_iterable_uses_entry_bindings():
    source = ("async def run():\n"
              "    A = 'node'\n"
              "    async for item in result[A + '_id']:\n"
              "        A = dynamic()\n")
    assert _violations(source) == ["line 3: literal 'node" + "_id' (folded)"]


@pytest.mark.parametrize("expression,reported", [
    pytest.param("(getattr(obj, A) for value in values)", True, id="generator-element"),
    pytest.param("(value for value in values if getattr(obj, A))", True, id="generator-filter"),
    pytest.param("(later for value in values for later in getattr(obj, A))", True, id="generator-later-iterable"),
    pytest.param("[getattr(obj, A) for value in values]", False, id="eager-list"),
    pytest.param("{getattr(obj, A) for value in values}", False, id="eager-set"),
    pytest.param("{value: getattr(obj, A) for value in values}", False, id="eager-dict"),
    pytest.param("map(lambda value: getattr(obj, A), values)", True, id="lambda-body"),
    pytest.param("map(lambda A: getattr(obj, A), values)", True, id="lambda-shadow"),
    pytest.param("map(lambda value=getattr(obj, A): value, values)", False, id="lambda-default"),
    pytest.param("iter([getattr(obj, A) for value in values])", False, id="nested-eager-list"),
    pytest.param("iter((getattr(obj, A) for value in values))", True, id="nested-generator"),
])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["for", "async-for"])
def test_runner_boundary_iterable_defers_only_lazy_parts(expression, reported, asynchronous):
    if asynchronous:
        source = ("async def run():\n    A = 'status'\n"
                  f"    async for item in {expression}:\n        A = dynamic()\n")
        line = 3
    else:
        source = f"A = 'status'\nfor item in {expression}:\n    A = dynamic()\n"
        line = 2
    assert _violations(source) == (
        [f"line {line}: getattr attribute argument does not fold to a literal"] if reported else []
    )


def test_runner_boundary_generator_first_iterable_keeps_entry_binding():
    source = "A = 'node'\nfor item in (value for value in result[A + '_id']):\n    A = dynamic()\n"
    assert _violations(source) == ["line 2: literal 'node" + "_id' (folded)"]


def test_runner_boundary_iterable_lazy_parts_keep_unchanged_names():
    source = ("A = 'status'\nB = 'status'\n"
              "for item in ((lambda: getattr(obj, B)) for value in values if getattr(obj, B)):\n"
              "    A = dynamic()\n")
    assert _violations(source) == []
