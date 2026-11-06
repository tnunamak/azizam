"""Ratchet: gateway code that names a workflow, an intent, a modality family, an engine binding or a protocol.

A review signal for the code-to-data plan (docs/design/CODE_TO_DATA.md), not its oracle: the oracle is the
unfamiliar-workflow acceptance matrix. Core must not select behaviour by a workflow, model or engine identity or by an
intent or modality spelling. This test counts the places that could.

What it counts, in proxy.py and every module under gateway/ (UI code is out of scope):
- string literals (not leading docstrings) that contain a declared name, or equal a modality family or start with
  ``family.``; explicit ``+`` chains of literals are folded first, so a name cut into pieces is seen whole;
- identifiers (names, attributes, defs, arguments) equal to a declared name.
What it does not see: names built at run time (format calls, joins, data read by a hard-coded key), bytes, and UI code.

The vocabulary is read from versioned data at test time: workflow manifests (``manifests/**/*.yml|yaml``: intent,
id, the id's short form, binding keys, required model file names) and protocol descriptions (``registry/protocols``:
ids). Changing that data changes the instrument; ``python tests/test_domain_literal_ratchet.py`` prints every site so a
reviewer can tell a vocabulary change from a code change.

The total is ``[ratchets] domain_literals`` in tests/budgets.toml. The gate refuses a value above the target branch's,
so it can only fall. When a change lowers the count, lower the ratchet in the same change.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUDGETS = Path(__file__).resolve().parent / "budgets.toml"
MANIFEST_ROOT = ROOT / "manifests"
PROTOCOL_DIR = ROOT / "registry" / "protocols"
# Binding keys that name the gateway itself, not an engine.
_NOT_ENGINE_BINDINGS = {"gateway"}
# Shorter names match too much unrelated text ("run", "fast") to be a useful signal.
_MIN_NAME = 4


def _yaml_files(root: Path) -> list[Path]:
    return sorted([*root.rglob("*.yml"), *root.rglob("*.yaml")])


def vocabulary() -> tuple[set[str], set[str]]:
    """(names matched anywhere in a literal, families matched as a whole literal or a ``family.`` prefix)."""
    names: set[str] = set()
    families: set[str] = set()
    for path in _yaml_files(MANIFEST_ROOT):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("intent"), str):
            continue
        intent = manifest["intent"]
        if "." in intent:
            names.add(intent.lower())
            families.add(intent.split(".", 1)[0].lower())
        workflow_id = manifest.get("id")
        if isinstance(workflow_id, str):
            names.add(workflow_id.lower())
            if workflow_id.startswith(intent + "."):
                names.add(workflow_id[len(intent) + 1:].lower())
        for binding in (manifest.get("bindings") or {}):
            if isinstance(binding, str) and binding not in _NOT_ENGINE_BINDINGS:
                names.add(binding.lower())
        for model in ((manifest.get("requires") or {}).get("models") or []):
            if isinstance(model, dict) and isinstance(model.get("path"), str):
                names.add(Path(model["path"]).name.lower())
    for path in _yaml_files(PROTOCOL_DIR):
        description = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(description, dict) and isinstance(description.get("id"), str):
            names.add(description["id"].lower())
    return {name for name in names if len(name) >= _MIN_NAME}, families


def _docstrings(tree: ast.AST) -> set[int]:
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                skipped.add(id(body[0].value))
    return skipped


def _folded(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _match(text: str, names: set[str], families: set[str]) -> str | None:
    value = text.strip().lower()
    if not value:
        return None
    for family in sorted(families):
        if value == family or value.startswith(family + "."):
            return family
    return next((name for name in sorted(names) if name in value), None)


def sites(source: str, names: set[str], families: set[str]) -> list[tuple[int, str, str]]:
    """(line, text, matched name) for each counted literal, folded chain or identifier."""
    tree = ast.parse(source)
    skipped = _docstrings(tree)
    inside_chain: set[int] = set()
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and id(node) not in inside_chain:
            text = _folded(node)
            if text is not None:
                inside_chain.update(id(part) for part in ast.walk(node))
                matched = _match(text, names, families)
                if matched:
                    found.append((node.lineno, text, matched))
                continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped \
                and id(node) not in inside_chain:
            matched = _match(node.value, names, families)
            if matched:
                found.append((node.lineno, node.value, matched))
        identifier = None
        if isinstance(node, ast.Name):
            identifier = node.id
        elif isinstance(node, ast.Attribute):
            identifier = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifier = node.name
        elif isinstance(node, ast.arg):
            identifier = node.arg
        if identifier and identifier.lower() in names:
            found.append((getattr(node, "lineno", 0), identifier, identifier.lower()))
    return sorted(found)


def scanned_files() -> list[Path]:
    return [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]


def all_sites() -> dict[str, list[tuple[int, str, str]]]:
    names, families = vocabulary()
    result: dict[str, list[tuple[int, str, str]]] = {}
    for path in scanned_files():
        found = sites(path.read_text(encoding="utf-8"), names, families)
        if found:
            result[str(path.relative_to(ROOT))] = found
    return result


def _ratchet() -> int:
    value = tomllib.loads(BUDGETS.read_text(encoding="utf-8"))["ratchets"]["domain_literals"]
    assert isinstance(value, int) and value >= 0, "[ratchets] domain_literals must be a non-negative integer"
    return value


def test_the_vocabulary_is_read_from_versioned_data():
    names, families = vocabulary()
    assert {"audio", "image", "video"} <= families
    assert {"audio.generate", "comfyui"} <= names
    assert all(len(name) >= _MIN_NAME for name in names)


def test_the_rule_counts_names_prefixes_chains_and_identifiers_but_not_docstrings():
    names, families = {"audio.generate", "comfyui"}, {"audio"}
    def count(source: str) -> int:
        return len(sites(source, names, families))
    assert count('x = "audio.generate"\n') == 1
    assert count('x = s.startswith("audio.")\n') == 1
    assert count('x = "audio"\n') == 1
    assert count('x = "audiobook"\n') == 0
    assert count('x = backends.get("comfyui")\n') == 1
    assert count('x = "au" + "dio.generate"\n') == 1
    assert count('x = "com" + "fy" + "ui"\n') == 1
    assert count("comfyui = 1\n") == 1
    assert count("x = engines.comfyui\n") == 1
    assert count('"""audio.generate in a docstring is fine"""\n') == 0


def test_the_count_matches_the_ratchet():
    found = all_sites()
    total = sum(len(rows) for rows in found.values())
    ceiling = _ratchet()
    assert total <= ceiling, (
        f"{total} sites name a workflow, intent, modality family, engine binding or protocol; the ratchet is {ceiling}. "
        "Read the name from manifest, engine or service data instead. Run this file as a script to list every site."
    )
    assert total == ceiling, f"The count fell to {total}: lower [ratchets] domain_literals in tests/budgets.toml."


if __name__ == "__main__":
    rows = all_sites()
    for file, found in rows.items():
        for line, text, matched in found:
            print(f"{file}:{line}\t{matched}\t{text[:100]!r}")
    print(f"total\t{sum(len(found) for found in rows.values())}\tfiles\t{len(rows)}")
