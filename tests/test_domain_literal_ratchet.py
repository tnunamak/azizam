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

Reviewed protocol-edge allowances live in tests/domain_literal_reviewed.yml, keyed by file and exact literal
with an explicit count, category and reason. Only unreviewed sites count toward ``[ratchets] domain_literals``
in tests/budgets.toml. Extra occurrences remain unreviewed; allowances exceeding the current scan fail as stale.
The gate refuses a value above the target branch's, so it can only fall. When a change lowers the unreviewed
count, lower the ratchet in the same change. Script output lists reviewed and unreviewed sites separately.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BUDGETS = Path(__file__).resolve().parent / "budgets.toml"
REVIEWED = Path(__file__).resolve().parent / "domain_literal_reviewed.yml"
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
        bindings = manifest.get("bindings") or {}
        for key, body in bindings.items():
            if isinstance(key, str) and key not in _NOT_ENGINE_BINDINGS:
                names.add(key.lower())
            if isinstance(body, dict) and isinstance(body.get("service"), str):
                names.add(body["service"].lower())
        # Schema 1 kept ``requires`` at the root; schema 2 keeps it inside the one binding.
        requirements = [manifest.get("requires")] + [body.get("requires") for body in bindings.values() if isinstance(body, dict)]
        for required in requirements:
            for model in ((required or {}).get("models") or []):
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


def partition_reviewed_sites(
    found: dict[str, list[tuple[int, str, str]]], reviewed_path: Path = REVIEWED,
) -> tuple[dict[str, list[tuple[int, str, str]]], dict[str, list[tuple[int, str, str]]]]:
    """Split a scan using exact file/literal allowances, never line-number exemptions."""
    document = yaml.safe_load(reviewed_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{reviewed_path}: expected a mapping"
    unknown = set(document) - {"schema_version", "sites"}
    assert not unknown, f"{reviewed_path}: unknown keys {sorted(map(str, unknown))}"
    assert type(document.get("schema_version")) is int and document["schema_version"] == 1, (
        f"{reviewed_path}: schema_version must be 1"
    )
    assert isinstance(document.get("sites"), list), f"{reviewed_path}: sites must be a list"
    remaining: dict[tuple[str, str], int] = {}
    fields = {"file", "literal", "count", "category", "reason"}
    categories = {"media_type", "engine_node_input", "wire_enum"}
    for index, entry in enumerate(document["sites"]):
        label = f"{reviewed_path.name} sites[{index}]"
        assert isinstance(entry, dict), f"{label}: expected a mapping"
        unknown = set(entry) - fields
        assert not unknown, f"{label}: unknown keys {sorted(map(str, unknown))}"
        assert fields <= set(entry), f"{label}: missing keys {sorted(fields - set(entry))}"
        for field in ("file", "literal", "category", "reason"):
            assert isinstance(entry[field], str) and entry[field].strip(), f"{label}: {field} must be non-empty text"
        file, literal, count = entry["file"], entry["literal"], entry["count"]
        label += f" ({file}, {literal!r}, count={count!r})"
        file_path = PurePosixPath(file)
        assert not file_path.is_absolute() and ".." not in file_path.parts and str(file_path) == file \
            and "\\" not in file and file != ".", f"{label}: file must be a repository-relative path"
        assert type(count) is int and count > 0, f"{label}: count must be a positive integer"
        assert entry["category"] in categories, f"{label}: unknown category {entry['category']!r}"
        key = (file, literal)
        assert key not in remaining, f"{label}: duplicate file/literal allowance for {file} {literal!r}"
        actual = sum(text == literal for _line, text, _matched in found.get(file, []))
        assert count <= actual, f"{label}: stale entry reviews {count} sites but only {actual} were found"
        remaining[key] = count

    reviewed: dict[str, list[tuple[int, str, str]]] = {}
    unreviewed: dict[str, list[tuple[int, str, str]]] = {}
    for file, rows in found.items():
        for row in rows:
            key = (file, row[1])
            if remaining.get(key, 0):
                reviewed.setdefault(file, []).append(row)
                remaining[key] -= 1
            else:
                unreviewed.setdefault(file, []).append(row)
    return reviewed, unreviewed


def print_site_report(found: dict[str, list[tuple[int, str, str]]], reviewed_path: Path = REVIEWED) -> None:
    reviewed, unreviewed = partition_reviewed_sites(found, reviewed_path)
    for label, rows in (("reviewed", reviewed), ("unreviewed", unreviewed)):
        print(f"{label} sites")
        for file, occurrences in rows.items():
            for line, text, matched in occurrences:
                print(f"{file}:{line}\t{matched}\t{text!r}")
        print(f"total\t{sum(len(occurrences) for occurrences in rows.values())}\tfiles\t{len(rows)}")


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


def _synthetic_review(tmp_path: Path):
    source = tmp_path / "sample.py"
    source.write_text('\nfirst = "audio"\nsecond = "audio"\nother = "video"\n', encoding="utf-8")
    found = {"gateway/sample.py": sites(source.read_text(encoding="utf-8"), set(), {"audio", "video"})}
    entry = {"file": "gateway/sample.py", "literal": "audio", "count": 1,
             "category": "media_type", "reason": "Names the media type family of a file."}
    return found, tmp_path / "reviewed.yml", entry


def _write_review(path: Path, entries: list[dict], **extra) -> None:
    path.write_text(yaml.safe_dump({"schema_version": 1, "sites": entries, **extra}), encoding="utf-8")


def test_reviewed_entry_fails_as_stale_and_names_the_entry(tmp_path):
    found, path, entry = _synthetic_review(tmp_path)
    for changes in ({"count": 3}, {"file": "gateway/missing.py"}, {"literal": "Audio"}):
        stale = {**entry, **changes}
        _write_review(path, [stale])
        with pytest.raises(AssertionError) as exc:
            partition_reviewed_sites(found, path)
        message = str(exc.value)
        assert "stale" in message
        assert stale["file"] in message
        assert repr(stale["literal"]) in message
        assert f"count={stale['count']}" in message


def test_sites_above_reviewed_count_remain_unreviewed(tmp_path):
    found, path, entry = _synthetic_review(tmp_path)
    other = tmp_path / "other.py"
    other.write_text('kind = "audio"\n', encoding="utf-8")
    found["gateway/other.py"] = sites(other.read_text(encoding="utf-8"), set(), {"audio"})
    _write_review(path, [entry])
    reviewed, unreviewed = partition_reviewed_sites(found, path)
    assert reviewed == {"gateway/sample.py": [(2, "audio", "audio")]}
    assert unreviewed == {"gateway/sample.py": [(3, "audio", "audio"), (4, "video", "video")],
                          "gateway/other.py": [(1, "audio", "audio")]}
    assert len(found["gateway/sample.py"]) == 3  # Partitioning must not mutate the scan.


def test_unknown_review_category_is_rejected(tmp_path):
    found, path, entry = _synthetic_review(tmp_path)
    _write_review(path, [{**entry, "category": "intent"}])
    with pytest.raises(AssertionError, match="category.*intent"):
        partition_reviewed_sites(found, path)


def test_unknown_review_keys_are_rejected(tmp_path):
    found, path, entry = _synthetic_review(tmp_path)
    _write_review(path, [{**entry, "line": 2}])
    with pytest.raises(AssertionError, match="unknown keys.*line"):
        partition_reviewed_sites(found, path)
    _write_review(path, [entry], extra_sites=[])
    with pytest.raises(AssertionError, match="unknown keys.*extra_sites"):
        partition_reviewed_sites(found, path)


def test_review_schema_requires_valid_fields_and_unique_allowances(tmp_path):
    found, path, entry = _synthetic_review(tmp_path)
    for field, value in (("count", True), ("count", 0), ("count", -1), ("count", 1.5),
                         ("literal", ""), ("reason", " "), ("file", "../sample.py")):
        _write_review(path, [{**entry, field: value}])
        with pytest.raises(AssertionError, match=field):
            partition_reviewed_sites(found, path)
    _write_review(path, [entry, entry])
    with pytest.raises(AssertionError, match="duplicate.*gateway/sample.py"):
        partition_reviewed_sites(found, path)
    _write_review(path, [entry], schema_version=True)
    with pytest.raises(AssertionError, match="schema_version"):
        partition_reviewed_sites(found, path)
    for category in ("media_type", "engine_node_input", "wire_enum"):
        _write_review(path, [{**entry, "category": category, "count": 2}])
        reviewed, unreviewed = partition_reviewed_sites(found, path)
        assert reviewed == {"gateway/sample.py": [(2, "audio", "audio"), (3, "audio", "audio")]}
        assert unreviewed == {"gateway/sample.py": [(4, "video", "video")]}


def test_script_report_separates_reviewed_and_unreviewed_sites(tmp_path, capsys):
    found, path, entry = _synthetic_review(tmp_path)
    _write_review(path, [entry])
    print_site_report(found, path)
    reviewed, unreviewed = capsys.readouterr().out.split("unreviewed sites\n")
    assert reviewed.startswith("reviewed sites\n")
    assert "gateway/sample.py:2\taudio\t'audio'" in reviewed
    assert "total\t1\tfiles\t1" in reviewed
    assert "gateway/sample.py:3\taudio\t'audio'" in unreviewed
    assert "gateway/sample.py:4\tvideo\t'video'" in unreviewed
    assert "total\t2\tfiles\t1" in unreviewed


def test_the_count_matches_the_ratchet():
    _reviewed, found = partition_reviewed_sites(all_sites())
    total = sum(len(rows) for rows in found.values())
    ceiling = _ratchet()
    assert total <= ceiling, (
        f"{total} unreviewed sites name a workflow, intent, modality family, engine binding or protocol; the ratchet is {ceiling}. "
        "Read the name from manifest, engine or service data instead. Run this file as a script to list every site."
    )
    assert total == ceiling, f"The count fell to {total}: lower [ratchets] domain_literals in tests/budgets.toml."


if __name__ == "__main__":
    print_site_report(all_sites())
