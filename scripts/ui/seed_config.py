"""Give a disposable UI server the settings its browser tests assume, in the places the gateway reads them.

The gateway reads only startup keys from its startup file (docs/design/CONFIG_HOMES.md, C10). The fixtures of the browser
tests still describe a deployment in the old single-file shape: engines under ``backends``, ``model_aliases``, ``videos``.
This module splits such a document in two. The startup keys go to the startup file. Everything else goes into the
server's admin.db through the one-time legacy import, so the server holds it the way an install that imported its
old file does.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]


def split_startup(document: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(startup, rest)``: the leaves of ``document`` the startup schema lists, and every other key, both nested as given."""
    from gateway import startup_config

    allowed = startup_config.startup_paths()
    startup: dict[str, Any] = {}
    rest: dict[str, Any] = {}

    def walk(node: Mapping[str, Any], prefix: str, into_startup: dict[str, Any], into_rest: dict[str, Any]) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if path not in allowed:
                into_rest[key] = value
            elif isinstance(value, Mapping) and any(name.startswith(path + ".") for name in allowed):
                kept: dict[str, Any] = {}
                moved: dict[str, Any] = {}
                walk(value, f"{path}.", kept, moved)
                if kept:
                    into_startup[key] = kept
                if moved:
                    into_rest[key] = moved
            else:
                into_startup[key] = value

    walk(document, "", startup, rest)
    return startup, rest


def merge(into: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge ``extra`` into ``into``; a scalar or list in ``extra`` replaces."""
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(into.get(key), dict):
            merge(into[key], value)
        else:
            into[key] = value
    return into


def seed_admin_db(admin_db: Path, legacy: Mapping[str, Any], *, seed_registry: bool) -> None:
    """Import ``legacy`` (old-shape section blocks) into ``admin_db``. With ``seed_registry`` the ``backends`` become the
    registry's owner services first; without it the registry is already there (a household seed wrote its own)."""
    sys.path.insert(0, str(REPO / "tests"))  # the setup helpers that the hermetic suite uses
    from registry_test_helpers import import_deployment_sections, seed_registry_from_backends

    if seed_registry:
        seed_registry_from_backends(admin_db, legacy)
    import_deployment_sections(admin_db, {key: value for key, value in legacy.items() if key != "backends"})


def main(argv: list[str]) -> int:
    """``seed_config.py CONFIG ADMIN_DB``: rewrite CONFIG, a file in the old single-file shape, as a startup file, and
    import the rest into ADMIN_DB (its ``backends`` become registry services). For a harness that builds a deployment."""
    import yaml

    if len(argv) != 3:
        print("usage: seed_config.py CONFIG ADMIN_DB", file=sys.stderr)
        return 2
    sys.path.insert(0, str(REPO))
    config, admin_db = Path(argv[1]), Path(argv[2])
    startup, legacy = split_startup(yaml.safe_load(config.read_text(encoding="utf-8")) or {})
    config.write_text(yaml.safe_dump(startup), encoding="utf-8")
    if legacy:
        seed_admin_db(admin_db, legacy, seed_registry=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
