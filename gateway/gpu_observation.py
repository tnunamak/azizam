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


def display_config(path=DEFAULT_REGISTRY):
    """Validate display data separately so observation-only callers stay compatible."""
    config = load_registry(path)
    devices, runtime = config.get("devices"), config.get("runtime_display")
    if not isinstance(devices, list) or not devices:
        raise ValueError("observation_devices_invalid")
    indexes, orders = set(), set()
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError("observation_device_invalid")
        index, order = device.get("index"), device.get("order")
        if type(index) is not int or index < 0 or index in indexes:
            raise ValueError("observation_device_index_invalid")
        if type(order) is not int or order < 0 or order in orders:
            raise ValueError("observation_device_order_invalid")
        indexes.add(index)
        orders.add(order)
        if any(not isinstance(device.get(key), str) or not device[key]
               for key in ("label", "budget_key")):
            raise ValueError("observation_device_label_invalid")
        roles = device.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ValueError("observation_device_roles_invalid")
        for role in roles:
            if not isinstance(role, dict) or any(
                not isinstance(role.get(key), str) or not role[key] for key in ("label", "key")
            ):
                raise ValueError("observation_device_role_invalid")
            if ("{" in role["label"] or "}" in role["label"]) and role["label"] != "{label}":
                raise ValueError("observation_device_role_template_invalid")
            if ("{" in role["key"] or "}" in role["key"]) and role["key"] != "{gpu_budget_key}":
                raise ValueError("observation_device_role_template_invalid")
    if (not isinstance(runtime, dict) or type(runtime.get("device_index")) is not int
            or runtime["device_index"] not in indexes
            or any(not isinstance(runtime.get(key), str) or not runtime[key]
                   for key in ("device_label", "python_label", "runtime_label"))):
        raise ValueError("observation_runtime_display_invalid")
    return {"devices": sorted(devices, key=lambda row: row["order"]), "runtime": runtime}
