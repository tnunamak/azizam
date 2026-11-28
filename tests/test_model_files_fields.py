"""Behavioral checks for registry-owned local file field declarations."""
import importlib.util
import yaml
from pathlib import Path
import sys
import types

from gateway import strict_yaml

def _load(tmp_path, monkeypatch, *, mutate=False):
    root = Path(__file__).resolve().parents[1]
    source = root / "gateway" / "model_files.py"
    declaration = root / "registry" / "model_file_fields.yml"
    document = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    groups = {key: list(value) for key, value in document.items() if key != "schema_version"}
    if mutate:
        names = sorted({name for value in groups.values() for name in value})
        renamed = {name: "custom_" + str(index) for index, name in enumerate(names)}
        groups = {key: [renamed[name] for name in value] for key, value in groups.items()}
    (tmp_path / "gateway").mkdir()
    (tmp_path / "registry").mkdir()
    target = tmp_path / "gateway" / "model_files.py"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "registry" / "model_file_fields.yml").write_text(
        yaml.safe_dump({"schema_version": 1, **groups}), encoding="utf-8")
    # Isolate collaborators, not the field loading or path traversal under test.
    registry = types.SimpleNamespace(legacy_lifecycle_from_service=lambda service: service.get("lifecycle"))
    requests = types.SimpleNamespace(memo=lambda key, fn: fn())
    package = types.ModuleType("gateway")
    package.service_registry = registry
    package.request_connections = requests
    package.strict_yaml = strict_yaml
    monkeypatch.setitem(sys.modules, "gateway", package)
    monkeypatch.setitem(sys.modules, "gateway.strict_yaml", strict_yaml)
    spec = importlib.util.spec_from_file_location("isolated_file_fields", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, groups

def test_registry_mutation_changes_host_and_nested_fields(tmp_path, monkeypatch):
    module, rows = _load(tmp_path, monkeypatch, mutate=True)
    host = rows["host"][0]
    nested = rows["nested"][0]
    missing = tmp_path / "absent"
    result = module.model_file_availability({host: {nested: str(missing)}})
    assert result["evidence"]["missing_file_count"] == 1
    assert module._model_file_paths({host: {nested: str(missing)}}, (), service=None) == (missing,)

def test_registry_defaults_preserve_scope_order_and_dedup(tmp_path, monkeypatch):
    module, rows = _load(tmp_path, monkeypatch)
    host, managed, required = rows["host"], rows["managed"], rows["required"]
    first, second = tmp_path / "first", tmp_path / "second"
    # Nested keys are read in data order (path, file, model_path), as before the move.
    nested = rows["nested"]
    third = tmp_path / "third"
    assert module._paths_from_value({nested[0]: str(third), nested[1]: str(first), nested[2]: str(second)}) == [third, first, second]
    model = {host[0]: str(first), host[1]: [str(first)], required[0]: str(second)}
    assert module._model_file_paths(model, (), service=None) == (first, second)
    assert module._model_file_paths({**model, "model_file_scope": "backend"}, (), service=None) == ()
    assert module._model_file_paths({managed[0]: str(first)}, (), service=None) == ()
    assert module._model_file_paths({managed[0]: str(first), "model_file_scope": "gateway"}, (), service=None) == (first,)
    first.write_bytes(b"fixture")
    assert module.model_file_availability({host[0]: str(first)}) is None
    result = module.model_file_availability(model)
    assert result == {"available": False, "status": "unavailable", "reason": "Model file is not installed.",
                      "measured": True, "enabled": True, "configured": True, "up": None,
                      "idle_startable": False, "evidence": {"source": "registry", "kind": "missing_model_file",
                                                         "missing_file_count": 1}}
