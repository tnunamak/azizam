"""Dated, owner-editable facts for the offline request-retention ledger.

Facts describe observed or documented behavior. They do not drive an engine,
fetch evidence, or clear another system's history. An explicitly stored empty
list is authoritative: it is never replaced by a later bundled seed.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import jsonschema
import yaml

from gateway.service_registry import is_remote_provider_config


_ROOT = Path(__file__).resolve().parent.parent
_FACT_SCHEMA = json.loads((_ROOT / "registry/retention_fact.schema.json").read_text(encoding="utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(_FACT_SCHEMA, format_checker=jsonschema.FormatChecker())
_DATA_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_READER_DESCRIPTIONS = {
    "maker": "The maker and holders of their personal App keys",
    "scope": "The app that saved it, within its access scope",
}


def validate_facts(facts: Any, *, for_write: bool = False, now: date | None = None) -> list[dict[str, Any]]:
    """Return JSON-safe facts; reject future evidence dates only on new writes."""
    if not isinstance(facts, list):
        raise ValueError("retention_facts must be a list")
    if len(facts) > 100:
        raise ValueError("retention_facts has more than 100 entries")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(facts):
        row = dict(value) if isinstance(value, Mapping) else value
        if isinstance(row, dict) and isinstance(row.get("checked"), date):
            row["checked"] = row["checked"].isoformat()
        errors = list(_VALIDATOR.iter_errors(row))
        if errors:
            error = sorted(errors, key=lambda item: str(item.path))[0]
            raise ValueError(f"retention_facts[{index}]: {error.message}")
        try:
            checked = date.fromisoformat(row["checked"])
        except ValueError:
            raise ValueError(f"retention_facts[{index}].checked must be a real date") from None
        if for_write and checked > (now or date.today()):
            raise ValueError(f"retention_facts[{index}].checked cannot be in the future")
        if row["id"] in seen:
            raise ValueError(f"retention_facts[{index}].id is duplicated")
        seen.add(row["id"])
        result.append(copy.deepcopy(row))
    return result


def revision(fact: Mapping[str, Any]) -> str:
    """SHA-256 of canonical fact JSON, independent of map insertion order."""
    normalized = validate_facts([fact])[0]
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=None)
def _file_data(path: Path) -> Mapping[str, Any]:
    # Bundled data is read-only here; all public fact/kind rows are copied.
    # File changes take effect after restart, matching the retention contract.
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _owner_facts(overrides: Mapping[Any, Any], target_type: str, target_id: str) -> Any:
    """Distinguish missing overrides from an intentional empty list."""
    key = (target_type, target_id)
    if key in overrides:
        return overrides[key]
    nested = overrides.get(target_type)
    if isinstance(nested, Mapping) and target_id in nested:
        return nested[target_id]
    return None


def _address(config: Mapping[str, Any]) -> str | None:
    connection = config.get("connection")
    connection = connection if isinstance(connection, Mapping) else {}
    for source in (connection, config):
        for key in ("base_url", "api_base", "address", "url"):
            if source.get(key):
                raw = str(source[key])
                try:
                    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
                    host = parsed.hostname
                    port = parsed.port
                except ValueError:
                    continue
                if not host:
                    continue
                netloc = f"[{host}]" if ":" in host else host
                if port:
                    netloc += f":{port}"
                if parsed.scheme:
                    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
                return netloc + parsed.path
        if source.get("host"):
            host = str(source["host"])
            return f"{host}:{source['port']}" if source.get("port") else host
    return None


def _address_reach(address: str | None) -> str | None:
    if not address:
        return None
    parsed = urlsplit(address if "://" in address else f"//{address}")
    try:
        return "Azizam reaches it on this computer" if ipaddress.ip_address(parsed.hostname or "").is_loopback else "Azizam reaches it on your network"
    except ValueError:
        return "Azizam reaches it at this address; who else can reach it depends on its network settings"


def _with_status(facts: list[dict[str, Any]], *, now: date, stale_after_days: int, owner: bool) -> list[dict[str, Any]]:
    result = []
    for fact in facts:
        row = copy.deepcopy(fact)
        row["revision"] = revision(fact)
        row["stale"] = (now - date.fromisoformat(fact["checked"])).days > stale_after_days
        row["stale_label"] = f"This was last checked on {fact['checked']}." if row["stale"] else None
        row["source"] = "set by the owner" if owner else "bundled evidence"
        result.append(row)
    return result


def ledger(
    registry: Mapping[str, Any], *, kinds: Mapping[str, Any] | None = None,
    overrides: Mapping[Any, Any] | None = None, now: date | datetime | None = None,
    service_id: str | None = None,
) -> dict[str, Any]:
    """Build a read-only ledger from the supplied registry and local data files.

    ``overrides`` accepts ``{(target_type, target_id): facts}`` or nested
    ``{target_type: {target_id: facts}}``. Target types are provider and engine.
    ``[]`` wins over every bundled fact. No outbound I/O occurs.
    """
    contract = _file_data(_ROOT / "registry/retention.yml")
    kinds = kinds if kinds is not None else contract["kinds"]
    overrides = overrides or {}
    today = now.date() if isinstance(now, datetime) else (now or date.today())
    stale_days = contract["stale_after_days"]
    destinations: list[dict[str, Any]] = []

    for system_id, bundled in contract.get("system_facts", {}).items():
        owner_facts = _owner_facts(overrides, "system", system_id)
        facts = _with_status(
            validate_facts(owner_facts if owner_facts is not None else bundled["facts"]),
            now=today, stale_after_days=stale_days, owner=owner_facts is not None,
        )
        destinations.append({"id": system_id, "name": bundled["name"], "kind": "system", "facts": facts})

    catalog = _file_data(_ROOT / "registry/engine_definitions.json")
    definitions = {row["definition"]: row for row in catalog["definitions"]}
    for item_id, service in (registry.get("services") or {}).items():
        if service_id is not None and str(item_id) != str(service_id):
            continue
        if not isinstance(service, Mapping):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        target_type = "provider" if is_remote_provider_config(config) else "engine"
        target_id = str(item_id)
        owner_facts = _owner_facts(overrides, target_type, target_id)
        address = _address(config)
        if target_type == "provider":
            preset_id = str(config.get("preset_id") or config.get("provider_id") or "")
            preset_path = _ROOT / "providers/catalog" / f"{preset_id}.yml"
            preset = _file_data(preset_path) if _DATA_ID.fullmatch(preset_id) and preset_path.is_file() else {}
            bundled = preset.get("retention_facts", contract.get("provider_fact_catalog", {}).get(preset_id, []))
        else:
            definition_id = str(config.get("definition") or config.get("definition_id") or "")
            connection = config.get("connection") if isinstance(config.get("connection"), Mapping) else {}
            protocol_id = str(connection.get("protocol") or config.get("protocol") or service.get("protocol") or "")
            protocol_path = _ROOT / "registry/protocols" / f"{protocol_id}.yml"
            bundled = _file_data(protocol_path).get("retention_facts", []) if _DATA_ID.fullmatch(protocol_id) and protocol_path.is_file() else []
            definition = definitions.get(definition_id, {})
            if "retention_facts" in definition:
                bundled = definition["retention_facts"]
        if "retention_facts" in config:
            bundled = config["retention_facts"]
        facts = validate_facts(owner_facts if owner_facts is not None else bundled)
        row: dict[str, Any] = {
            "id": target_id, "name": str(service.get("name") or item_id), "kind": target_type,
            "facts": _with_status(facts, now=today, stale_after_days=stale_days, owner=owner_facts is not None or "retention_facts" in config),
        }
        if address:
            row["address"] = address
            if target_type == "engine":
                row["reach"] = _address_reach(address)
        destinations.append(row)

    result = {
        "kinds": [
            {"id": name, **copy.deepcopy(values),
             "reader_explanation": _READER_DESCRIPTIONS.get(values.get("readers"), "Reader access is unknown")}
            for name, values in kinds.items()
        ],
        "destinations": destinations,
        "stale_after_days": stale_days,
    }
    result["never_save_text"] = assemble_never_save(result)
    return result


def assemble_never_save(view: Mapping[str, Any]) -> str:
    """Explain remaining copies from the same dated fact rows shown by ledger."""
    lines = ["Never save stops Azizam from intentionally keeping a new request. It cannot erase copies kept by other systems."]
    for destination in view.get("destinations", []):
        place = f" Azizam reaches it at {destination['address']}" if destination.get("address") else ""
        port_reach = f"; {destination['reach']}" if destination.get("reach") else ""
        for fact in destination.get("facts", []):
            condition = f" When: {fact['condition']}." if fact.get("condition") else ""
            lines.append(
                f"{destination['name']}{place}{port_reach}: {fact['holds']} can remain in {fact['where']} "
                f"({fact['how_long']}; default {fact['default']}). {fact['reach']}. "
                f"To change this: {fact['control']}. Checked {fact['checked']}." + condition
            )
    return "\n\n".join(lines)
