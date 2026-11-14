"""Configured host observation; command and column knowledge live in data."""
from pathlib import Path

import yaml


DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "registry" / "gpu_observation.yml"
_TYPES = frozenset({"str", "int", "optional_int", "optional_float"})


def load_registry(path=DEFAULT_REGISTRY):
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("observation_registry_invalid")
    argv, fields = config.get("argv"), config.get("fields")
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
        raise ValueError("observation_argv_invalid")
    if not isinstance(fields, list) or not fields:
        raise ValueError("observation_fields_invalid")
    keys = set()
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("observation_field_invalid")
        key, kind = field.get("key"), field.get("type")
        if not isinstance(key, str) or not key or key in keys:
            raise ValueError("observation_field_key_invalid")
        if not isinstance(kind, str) or kind not in _TYPES:
            raise ValueError("observation_field_type_unknown")
        keys.add(key)
    return config


def executable(path=DEFAULT_REGISTRY):
    """The observation command's executable, shared by related probes and labels."""
    return load_registry(path)["argv"][0]


def _convert(value, kind):
    if kind == "str":
        return value
    if kind == "int":
        return int(value)
    if kind not in {"optional_int", "optional_float"}:
        raise ValueError("observation_field_type_unknown")
    try:
        return int(value) if kind == "optional_int" else float(value)
    except (ValueError, TypeError):
        return None


def observe(runner, path=DEFAULT_REGISTRY):
    config = load_registry(path)
    try:
        rc, out, _err = runner(list(config["argv"]))
    except FileNotFoundError:
        return None
    if rc != 0 or not out.strip():
        return None
    rows = []
    fields = config["fields"]
    for line in out.strip().splitlines():
        # Preserve the existing unquoted, comma-separated output contract.
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < len(fields):
            continue
        try:
            rows.append({field["key"]: _convert(parts[index], field["type"])
                         for index, field in enumerate(fields)})
        except ValueError:
            continue
    return rows or None
