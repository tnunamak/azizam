"""Goldens for the config-homes program (docs/design/CONFIG_HOMES.md, C0).

Three inputs go through the real boot path, ``proxy._load_effective_runtime_config``, on a scratch admin.db:
the deployment fixture (``tests/fixtures/config_homes/deployment_config.yml``, the tracked ``proxy_config.yml`` as it was
before C10), the first-run startup file (the tracked ``proxy_config.yml`` since C10; ``first-run.yml`` before), and a
live-like input (the deployment fixture plus the live host's alias edit,
``tests/fixtures/config_homes/live_like_alias_edit.yml``). The goldens in ``goldens.json`` were captured before C10 and
are not regenerated since: ``test_config_homes_goldens.py`` holds the effective config to them, and names the entries C10
changes on purpose. For each input the goldens hold

- the digest of the effective config, with one digest per top-level section to localise a change;
- the alias resolution table: every stable name and one sample per OpenAI pattern, for every operation;
- the gateway-owned ``/v1/models`` ids (stable aliases and gateway workflows) and the role list.

The config-homes slices move readers and add layers without changing what these show. A slice that must change a
golden says so in its report and regenerates it on purpose:

    uv venv from tests/requirements-hermetic.lock, then  python tests/config_homes_golden.py --write
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "config_homes" / "goldens.json"
LIVE_EDIT_PATH = ROOT / "tests" / "fixtures" / "config_homes" / "live_like_alias_edit.yml"
INPUT_FILES = {
    "tracked": "tests/fixtures/config_homes/deployment_config.yml",
    "first_run": "proxy_config.yml",
    "live_like": "tests/fixtures/config_homes/deployment_config.yml",
}


def digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _merge(base: dict[str, Any], edit: dict[str, Any]) -> None:
    for key, value in edit.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def raw_config(name: str) -> dict[str, Any]:
    """The startup-file mapping for one golden input, as ``load_bootstrap_config`` returns it."""
    loaded = yaml.safe_load((ROOT / INPUT_FILES[name]).read_text(encoding="utf-8")) or {}
    if name == "live_like":
        _merge(loaded, yaml.safe_load(LIVE_EDIT_PATH.read_text(encoding="utf-8")))
    return loaded


def startup_part(raw: dict[str, Any]) -> dict[str, Any]:
    """The keys of ``raw`` the startup file may hold (registry/startup.schema.json), as the deployment's reduced file."""
    from gateway import startup_config

    paths = startup_config.startup_paths()

    def project(node: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in node.items():
            path = f"{prefix}{key}"
            if path not in paths:
                continue
            if isinstance(value, dict) and any(other.startswith(path + ".") for other in paths):
                inner = project(value, path + ".")
                if inner:
                    out[key] = inner
            else:
                out[key] = copy.deepcopy(value)
        return out

    return project(raw)


def effective_config(proxy: Any, name: str, admin_db: Path) -> dict[str, Any]:
    """The effective config for ``name``, built by the boot loader on a scratch admin.db.

    An install whose sections were imported (the suite's own run holds the same): the engines of ``backends`` are the
    owner's registry records, each section block is imported once (``registry_test_helpers.import_deployment_sections``),
    and the boot loader then gets the startup keys only, as the gateway reads them since C10.
    """
    from gateway import config_runtime
    from tests.registry_test_helpers import import_deployment_sections, seed_registry_from_backends

    raw = raw_config(name)
    seed_registry_from_backends(admin_db, raw)
    import_deployment_sections(admin_db, raw)
    with patch.object(proxy, "ADMIN_DB_PATH", admin_db):
        effective = proxy._load_effective_runtime_config(startup_part(raw), boot=True)
    layer, _generation = config_runtime.carried(effective)
    assert not layer.problems, f"{name}: the effective config reports problems: {layer.problems}"
    return effective


def _alias_samples(section: Any) -> list[tuple[str, str]]:
    """(kind, name) for each stable alias, each engine id and one probe per OpenAI pattern."""
    if not isinstance(section, dict):
        return []
    samples: list[tuple[str, str]] = [("stable", str(name)) for name in (section.get("stable") or {})]
    samples += [("engine", str(name)) for name in (section.get("engines") or {})]
    for pattern in section.get("openai") or []:
        if isinstance(pattern, dict) and pattern.get("match"):
            samples.append(("openai", str(pattern["match"]).replace("*", "x").replace("?", "x")))
    return samples


def alias_table(proxy: Any, effective: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per sample, sorted by (operation, kind, name): the order of the keys in a dict is not a behaviour."""
    from gateway import stable_aliases

    rows: list[dict[str, Any]] = []
    aliases = effective.get("model_aliases")
    with patch.object(proxy, "config", effective):
        for operation in stable_aliases.OPERATIONS:
            section = (aliases or {}).get(operation) if isinstance(aliases, dict) else None
            available = proxy._alias_engine_available_for(operation)
            for kind, name in _alias_samples(section):
                row: dict[str, Any] = {"operation": operation, "kind": kind, "requested": name}
                try:
                    resolution = stable_aliases.resolve_alias(aliases, operation, name, engine_available=available)
                except stable_aliases.AliasUnavailableError as error:
                    row["error"] = error.code
                else:
                    row["resolution"] = resolution.to_dict() if resolution is not None else None
                rows.append(row)
    return sorted(rows, key=lambda row: (stable_aliases.OPERATIONS.index(row["operation"]), row["kind"], row["requested"]))


def models_view(proxy: Any, effective: dict[str, Any]) -> dict[str, Any]:
    """Gateway-owned ``/v1/models`` ids with what each stable alias resolves to, and the role ids."""
    with patch.object(proxy, "config", effective):
        alias_entries = proxy._stable_alias_model_entries()
        workflow_ids = sorted({entry["id"] for entry in proxy._build_workflow_model_entries()})
    return {
        "ids": sorted({*workflow_ids, *(entry["id"] for entry in alias_entries)}),
        "alias_targets": {
            entry["id"]: dict(sorted(entry["azizam_resolves_to"].items())) for entry in sorted(alias_entries, key=lambda e: e["id"])
        },
        "roles": sorted((effective.get("roles") or {}).keys()),
    }


def capture(proxy: Any, scratch: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"inputs": {}}
    for name in INPUT_FILES:
        effective = effective_config(proxy, name, scratch / f"admin-{name}.db")
        result["inputs"][name] = {
            "effective_digest": digest(effective),
            "section_digests": {key: digest(effective[key]) for key in sorted(effective)},
            "aliases": alias_table(proxy, effective),
            "models": models_view(proxy, effective),
        }
    return result


def load_golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def render(golden: dict[str, Any]) -> str:
    return json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    """``--write`` regenerates the goldens; without it, prints the digests. Uses scratch stores only."""
    scratch = Path(tempfile.mkdtemp(prefix="config-homes-golden-", dir=os.path.expanduser("~/.tmp")))
    for key, filename in (("AZIZAM_ADMIN_DB", "admin.db"), ("AZIZAM_METRICS_DB", "metrics.db"),
                          ("AZIZAM_RESPONSES_DB", "responses.db"), ("AZIZAM_RECEIPTS_DB", "receipts.db"),
                          ("AZIZAM_MEDIA_JOBS_DB", "media_jobs.db"), ("AZIZAM_ARBITRATION_DB", "arbitration.db"),
                          ("AZIZAM_KEY_ACTIVITY_LOG", "key_activity.jsonl")):
        os.environ[key] = str(scratch / filename)
    os.environ["AZIZAM_CONFIG_PATH"] = str(ROOT / "tests" / "fixtures" / "config_homes" / "deployment_startup.yml")
    sys.path.insert(0, str(ROOT))
    import proxy  # noqa: E402

    try:
        golden = capture(proxy, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if "--write" in argv:
        GOLDEN_PATH.write_text(render(golden), encoding="utf-8")
        print(f"wrote {GOLDEN_PATH}")
    for name, view in golden["inputs"].items():
        print(name, view["effective_digest"], len(view["aliases"]), "alias rows", len(view["models"]["ids"]), "model ids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
