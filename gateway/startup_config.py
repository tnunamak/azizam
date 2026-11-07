"""The startup file's closed schema, and the check that refuses a key it does not list (docs/design/CONFIG_HOMES.md, C10).

``registry/startup.schema.json`` names every key the startup file may hold. A startup setting is one the gateway needs
before it opens admin.db, or one a running gateway cannot change: a listener, a storage root, a login boot rule, a
secret. Every other setting lives in admin.db. A file that holds one anyway is refused at start, with the key and the
place the setting lives now, so a stale block never looks live.

This module reads only the schema file and the names of the release's section files. It imports no gateway code, so
every process that reads the startup file can use it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import jsonschema

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
SCHEMA_PATH = REGISTRY_DIR / "startup.schema.json"
DEFAULTS_DIR = REGISTRY_DIR / "defaults"

CONFIG_API = "PATCH /admin/api/config/changes"

# Keyless access that earlier releases offered and the gateway now refuses (design v4, 2.8). The message of each is
# what the operator needs to read; ``proxy.removed_auth_settings`` and the startup check both use them.
REMOVED_AUTH_SETTINGS: Mapping[str, str] = {
    "enabled": "Auth is always on. Remove auth.enabled.",
    "lan_bypass_enabled": "Keyless LAN access is removed. Give each device an app and a key in Apps.",
    "lan_bypass_cidrs": "Keyless LAN access is removed. Give each device an app and a key in Apps.",
    "path_tokens": "Path tokens are removed. Give each device an app and a key in Apps.",
    "ollama_compat_discovery": "Public Ollama discovery is removed. Ollama clients send an app key.",
}

# Where a whole setting moved when the section has its own write route (the rest use the config API).
_SECTION_ROUTES: Mapping[str, str] = {
    "instance": "PUT /admin/api/settings/instance",
    "resource_admission": "PUT /admin/api/operator/media-job-admission",
    "service_availability": "PUT /admin/api/operator/service-availability",
}


class StartupConfigError(ValueError):
    """The startup file holds a key or a value it may not. ``problems`` has one line per fault."""

    def __init__(self, source: str, problems: list["StartupProblem"]) -> None:
        self.source = source
        self.problems = problems
        lines = "\n".join(f"  - {problem.message}" for problem in problems)
        super().__init__(f"{source} is not a valid startup file:\n{lines}")


@dataclass(frozen=True)
class StartupProblem:
    path: str  # dotted, for example "images.signed_url_ttl"
    message: str


@dataclass(frozen=True)
class StartupKey:
    """One leaf key the startup file may hold."""

    path: str
    types: tuple[str, ...]
    description: str
    env: str | None = None
    secret: bool = False


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def section_ids() -> frozenset[str]:
    """The config sections the release ships: the stems of registry/defaults/*.schema.json."""
    return frozenset(path.name.removesuffix(".schema.json") for path in DEFAULTS_DIR.glob("*.schema.json"))


def iter_keys(schema: Mapping[str, Any] | None = None, prefix: str = "") -> Iterator[StartupKey]:
    """Every leaf key of the startup schema, in file order."""
    node = load_schema() if schema is None else schema
    for name, child in (node.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if child.get("type") == "object" and "properties" in child:
            yield from iter_keys(child, f"{path}.")
        else:
            kind = child.get("type", "object")
            yield StartupKey(
                path=path,
                types=tuple(kind) if isinstance(kind, list) else (str(kind),),
                description=str(child.get("description", "")),
                env=child.get("x-azizam-env"),
                secret=bool(child.get("x-azizam-secret")),
            )


def startup_paths() -> frozenset[str]:
    """Every leaf path of the schema (``proxy.port``) and every object on the way to one (``proxy``)."""
    paths: set[str] = set()
    for key in iter_keys():
        parts = key.path.split(".")
        paths.update(".".join(parts[:count]) for count in range(1, len(parts) + 1))
    return frozenset(paths)


def startup_env_names() -> frozenset[str]:
    """The environment variables that may override a startup key."""
    return frozenset(key.env for key in iter_keys() if key.env)


def where_it_lives(path: str, *, sections: Iterable[str] | None = None) -> str:
    """Where the setting at the dotted ``path`` lives now. The longest entry of ``x-azizam-moved`` wins; a key under a
    section's own name lives in that section."""
    moved: Mapping[str, str] = load_schema().get("x-azizam-moved") or {}
    parts = path.split(".")
    for count in range(len(parts), 0, -1):
        head = ".".join(parts[:count])
        if head in moved:
            return moved[head]
    deeper = [f"{head} is {where}" for head, where in moved.items() if head.startswith(path + ".")]
    if deeper:
        return "several places: " + " ".join(deeper)
    known = section_ids() if sections is None else frozenset(sections)
    if parts[0] in known:
        route = _SECTION_ROUTES.get(parts[0], CONFIG_API)
        entry = "" if len(parts) == 1 else ", entry /" + "/".join(parts[1:])
        return f"config section `{parts[0]}`{entry} ({route}; GET /admin/api/config/sections/{parts[0]} shows it)."
    return "nowhere: Azizam does not read it. Delete it."


def _type_label(value: Any) -> str:
    return type(value).__name__


def _check(node: Mapping[str, Any], value: Any, path: str, sections: frozenset[str], found: list[StartupProblem]) -> None:
    properties = node.get("properties") or {}
    if not isinstance(value, Mapping):
        found.append(StartupProblem(path, f"`{path}` must be a mapping (got {_type_label(value)})."))
        return
    for key, item in value.items():
        child_path = f"{path}.{key}" if path else str(key)
        child = properties.get(key)
        if child is None:
            if path == "auth" and key in REMOVED_AUTH_SETTINGS:
                found.append(StartupProblem(child_path, f"`{child_path}` is removed. {REMOVED_AUTH_SETTINGS[key]}"))
            else:
                found.append(StartupProblem(
                    child_path, f"`{child_path}` is not a startup key. It lives in {where_it_lives(child_path, sections=sections)}"))
            continue
        if child.get("type") == "object" and "properties" in child:
            _check(child, item, child_path, sections, found)
            continue
        try:
            jsonschema.Draft202012Validator(child).validate(item)
        except jsonschema.ValidationError as error:
            found.append(StartupProblem(child_path, f"`{child_path}` is not valid: {error.message}."))


def validate(raw: Any, *, sections: Iterable[str] | None = None) -> list[StartupProblem]:
    """Every fault of a parsed startup file. An empty file (None) is valid: every startup setting has a default."""
    if raw is None:
        return []
    found: list[StartupProblem] = []
    _check(load_schema(), raw, "", frozenset(section_ids() if sections is None else sections), found)
    return found


def check(raw: Any, *, source: str = "the startup file") -> None:
    """Raise ``StartupConfigError`` naming every fault, or return."""
    problems = validate(raw)
    if problems:
        raise StartupConfigError(source, problems)
