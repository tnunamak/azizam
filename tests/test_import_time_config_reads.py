"""Ratchet: config values that proxy.py reads at import, and the modules that read the startup file.

A review signal for the config-homes program (docs/design/CONFIG_HOMES.md, 1.2 and 10.1), not its oracle: the oracle is
the per-slice acceptance tests. A value read once at import cannot see a reload, so each section slice moves its
readers to the per-request snapshot and lowers its key.

Count. ``scripts/config_import_reads.py`` scans the statements that run at module level in proxy.py and counts the
named values they build from the config, with three hand-listed ones the scan cannot see. Each key
``import_time_config_reads.<section>`` in ``[ratchets]`` of tests/budgets.toml is the count for one source section.
The count must equal the key (the rule of tests/test_domain_literal_ratchet.py): a rise fails, and a fall fails until
the key is lowered in the same change. ``python scripts/config_import_reads.py`` prints every name.

Startup-file readers. A second test pins the modules that read the startup file (``proxy_config.yml`` /
``first-run.yml``) or take its contents as ``proxy_config``. The final design has one: ``gateway/bootstrap_config.py``
(done-gate item 10). The list may shrink; a new reader fails until a reviewer adds it on purpose.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUDGETS = Path(__file__).resolve().parent / "budgets.toml"
PREFIX = "import_time_config_reads."

_spec = importlib.util.spec_from_file_location("config_import_reads", ROOT / "scripts" / "config_import_reads.py")
assert _spec is not None and _spec.loader is not None
scanner = importlib.util.module_from_spec(_spec)
sys.modules["config_import_reads"] = scanner  # the dataclasses resolve their annotations through sys.modules
_spec.loader.exec_module(scanner)


def _inventory():
    return scanner.scan((ROOT / "proxy.py").read_text(encoding="utf-8"))


def _ratchets() -> dict[str, int]:
    table = tomllib.loads(BUDGETS.read_text(encoding="utf-8"))["ratchets"]
    keys = {name[len(PREFIX):]: value for name, value in table.items() if name.startswith(PREFIX)}
    assert keys, f"[ratchets] has no {PREFIX}<section> key"
    assert all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in keys.values()), \
        f"each {PREFIX}<section> must be a whole number; a key whose count reached zero stays at 0 (the policy check " \
        "refuses a removed ratchet)"
    return keys


def test_the_scan_reproduces_the_design_inventory_shape():
    inventory = _inventory()
    assert inventory.statements >= inventory.direct_statements > 0
    assert inventory.scanned + len(scanner.HIDDEN) == len(inventory.names)
    assert set(scanner.HIDDEN) <= set(inventory.names)


def test_every_hand_listed_name_is_still_assigned_at_module_level():
    assigned = scanner.assigned_at_module_level((ROOT / "proxy.py").read_text(encoding="utf-8"))
    missing = sorted(set(scanner.HIDDEN) - assigned)
    assert not missing, f"scripts/config_import_reads.py HIDDEN names a constant proxy.py no longer assigns: {missing}"


def test_every_exclusion_still_matches_a_name_the_scan_reaches():
    stale = sorted(scanner.NOT_SETTINGS - _inventory().assigned)
    assert not stale, f"scripts/config_import_reads.py NOT_SETTINGS lists names the closure no longer assigns: {stale}"


def test_each_section_count_matches_its_ratchet_key():
    per_section = scanner.per_section(_inventory())
    keys = _ratchets()
    assert set(per_section) == set(keys), (
        f"sections with a count but no key: {sorted(set(per_section) - set(keys))}; "
        f"keys with no count: {sorted(set(keys) - set(per_section))}. One [ratchets] key per section."
    )
    for section, count in per_section.items():
        ceiling = keys[section]
        assert count <= ceiling, (
            f"{count} config values are read at import for section {section!r}; {PREFIX}{section} is {ceiling}. "
            "Read the setting from the runtime snapshot per request. Run scripts/config_import_reads.py to list them."
        )
        assert count == ceiling, f"The count for {section!r} fell to {count}: lower {PREFIX}{section} in tests/budgets.toml."


def test_the_total_is_the_sum_of_the_keys():
    assert len(_inventory().names) == sum(_ratchets().values())


def test_the_rule_follows_names_and_counts_constants_not_holders():
    source = (
        'cfg_root = None\n'
        'config = {}\n'
        'images_cfg = config.get("images", {})\n'
        'IMAGE_TTL = images_cfg.get("ttl", 5)\n'
        'IMAGE_TTL_DOUBLE = IMAGE_TTL * 2\n'
        'ADMIN_STORE = Store(config)\n'
        'def later():\n'
        '    return config.get("roles")\n'
        'UNRELATED = 4\n'
    )
    inventory = scanner.scan(source, hidden={})
    assert set(inventory.names) == {"IMAGE_TTL", "IMAGE_TTL_DOUBLE"}
    assert inventory.names["IMAGE_TTL_DOUBLE"] == "images", "a name carries the section of the names it reads"


def _names(source: str) -> dict[str, str]:
    return scanner.scan("config = {}\n" + source, hidden={}).names


def test_a_read_inside_a_lambda_or_a_function_body_is_deferred_and_not_counted():
    # `continue` on a Lambda node does not prune: ast.walk still yields its children
    assert _names('PROVIDER = lambda: config.get("roles")\n') == {}
    assert _names('PICK = make(lambda: config, key=lambda item: config["roles"])\n') == {}
    assert _names('def f():\n    return config.get("roles")\n') == {}
    assert _names('async def g():\n    return [config.get("roles") for _ in range(3)]\n') == {}


def test_the_parts_of_a_lambda_or_a_def_that_run_at_import_are_counted():
    assert _names('def f(limit=config.get("roles")):\n    return limit\n') == {"def f@2": "roles"}
    assert _names('@route(config["roles"])\ndef f():\n    return 1\n') == {"def f@3": "roles"}
    assert _names('WITH_DEFAULT = lambda x=config["roles"]: x\n') == {"WITH_DEFAULT": "roles"}


def test_a_class_body_runs_at_import_and_its_reads_are_counted():
    names = _names('class K:\n    VALUE = config.get("roles")\n    def method(self):\n        return config.get("images")\n')
    assert names == {"K.VALUE": "roles"}
    assert _names('class K(make_base(config["images"])):\n    pass\n') == {"class K@2": "images"}


def test_a_call_through_the_snapshot_is_a_root():
    assert _names('SNAP_ROLES = runtime().config.get("roles")\n') == {"SNAP_ROLES": "roles"}
    assert _names('SNAP_ALL = dict(runtime().config)\n') == {"SNAP_ALL": "whole_config"}
    assert _names('HOME_VIEW = CONFIG_HOME.view()\n') == {"HOME_VIEW": "whole_config"}
    assert _names('NOT_A_READ = make(current=runtime)\n') == {}, "handing over the function reads nothing"
    assert _names('LATER = lambda: runtime().config["roles"]\n') == {}


def test_a_call_that_is_handed_a_root_keeps_what_it_read_and_is_one_counted_value():
    names = _names('app.add_middleware(Limits, limits=Limits.from_config(config))\nREGISTER(config["roles"])\napp.use(other)\n')
    assert names == {"app.add_middleware@2": "whole_config", "REGISTER@3": "roles"}
    assert scanner.scan('config = {}\nclient.push(RequestBodyLimits.from_config(config))\n', hidden={}).names == {
        "client.push@2": "request_body_limits"}, "a callee that reads one named section is listed in CALLEE_SECTIONS"


def test_a_container_filled_at_import_and_a_walrus_and_loop_targets_are_followed():
    assert _names('TABLE = {}\nTABLE.update(config.get("roles", {}))\nTABLE2 = dict(TABLE)\n') == {
        "TABLE": "roles", "TABLE2": "roles"}
    assert _names('TABLE = {}\nfor key, value in config.get("roles", {}).items():\n    TABLE[key] = value\n') == {
        "TABLE": "roles", "key": "roles", "value": "roles"}
    assert _names('if (found := config.get("roles")):\n    pass\nUSES = found\n') == {"found": "roles", "USES": "roles"}
    assert _names('with open(config["roles"]) as handle:\n    DATA = handle.read()\n') == {"handle": "roles", "DATA": "roles"}


def test_the_rule_reads_into_if_and_try_blocks_and_ignores_function_bodies():
    source = (
        'config = {}\n'
        'if config.get("llm", {}).get("pin"):\n'
        '    PIN = config["llm"]["pin"]\n'
        'try:\n'
        '    TIMEOUT = int(config.get("model_routing", {}).get("timeout"))\n'
        'except Exception:\n'
        '    TIMEOUT = 5\n'
        'def f():\n'
        '    HIDDEN_LOCAL = config.get("roles")\n'
    )
    inventory = scanner.scan(source, hidden={})
    assert inventory.names == {"PIN": "llm", "TIMEOUT": "model_routing"}


def test_a_name_that_sees_several_sections_belongs_to_the_first_in_order():
    source = 'config = {}\nBOTH = (config.get("roles"), config.get("images"))\n'
    assert scanner.scan(source, hidden={}).names == {"BOTH": "images"}


# ---------------------------------------------------------------------------------------------------------------------
# Startup-file readers
# ---------------------------------------------------------------------------------------------------------------------

_SKIP_DIRS = {"tests", "web", "node_modules", "venv", "cache", "data", "e2e", "docs", "__pycache__"}
_STARTUP_NAMES = {"load_bootstrap_config", "bootstrap_config_path", "resolve_admin_db_path", "proxy_config"}
_STARTUP_STRINGS = ("proxy_config.yml", "AZIZAM_CONFIG_PATH", "first-run.yml")

# Modules that read the startup file or receive its contents today, and why. The final design keeps only
# gateway/bootstrap_config.py. A module may leave this list; a new one needs a reviewer's decision.
STARTUP_FILE_READERS = {
    "bench/run.py": "names AZIZAM_CONFIG_PATH for its benchmark gateway",
    "gateway/admin_catalog.py": "takes proxy_config to find the catalog directory",
    "gateway/bootstrap_config.py": "the loader: the one module that should read the file",
    "gateway/config_sources.py": "hand-kept ownership table; names proxy_config.yml",
    "gateway/provider_sidecar.py": "resolve_admin_db_path reads the file for the admin db path",
    "gateway/route_registry.py": "takes proxy_config to seed protocol frontends",
    "gateway/service_registry.py": "takes proxy_config to seed and backfill the registry from backends",
    "proxy.py": "loads the startup file at import",
    "scripts/doctor.py": "operator diagnostic names proxy_config.yml",
    "scripts/local/register_service_from_config.py": "one-off operator script",
    "scripts/local/sync_model_metadata_from_config.py": "one-off operator script",
    "scripts/perf/serve.py": "perf harness sets AZIZAM_CONFIG_PATH",
    "scripts/prove-decisions-gateway.py": "proof script sets AZIZAM_CONFIG_PATH",
    "scripts/stt_seam_check/gateway.py": "seam check sets AZIZAM_CONFIG_PATH",
    "scripts/ui/live_data_copy.py": "UI fixture copies the startup file",
    "scripts/ui/live_data_seed.py": "UI fixture sets AZIZAM_CONFIG_PATH",
    "scripts/ui/serve.py": "UI fixture serves first-run.yml",
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def reads_startup_file(source: str) -> bool:
    """True when the module names a startup-file loader, takes ``proxy_config``, or names the file or its env variable.

    Lexical on purpose: it errs toward listing a module so that a reviewer decides, and docstrings do not count.
    """
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _STARTUP_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _STARTUP_NAMES:
            return True
        if isinstance(node, (ast.arg, ast.keyword)) and node.arg in _STARTUP_NAMES:
            return True
        if isinstance(node, ast.alias) and node.name in _STARTUP_NAMES:
            return True
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                and len(node.value) < 80 and any(name in node.value for name in _STARTUP_STRINGS)):
            return True
    return False


def startup_file_readers() -> set[str]:
    found: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if _SKIP_DIRS & set(relative.parts) or any(part.startswith(".") for part in relative.parts):
            continue
        if reads_startup_file(path.read_text(encoding="utf-8")):
            found.add(relative.as_posix())
    return found


def test_only_the_listed_modules_read_the_startup_file():
    found = startup_file_readers()
    new = sorted(found - set(STARTUP_FILE_READERS))
    gone = sorted(set(STARTUP_FILE_READERS) - found)
    assert not new, (
        f"{new} read the startup file (proxy_config.yml) or take its contents. A setting has one home: read the "
        "effective config from the runtime snapshot, or add the module to STARTUP_FILE_READERS with a reason if the "
        "design needs it."
    )
    assert not gone, f"{gone} no longer read the startup file: remove them from STARTUP_FILE_READERS."


@pytest.mark.parametrize(("source", "expected"), [
    ("from gateway.bootstrap_config import load_bootstrap_config\n", True),
    ("def f(proxy_config):\n    return proxy_config\n", True),
    ('P = "proxy_config.yml"\n', True),
    ('"""Mentions proxy_config.yml in a docstring."""\nx = 1\n', False),
    ("import os\nx = os.environ.get('HOME')\n", False),
])
def test_the_detector_sees_loaders_arguments_and_names_but_not_docstrings(source, expected):
    assert reads_startup_file(source) is expected


def test_a_tuple_assignment_attributes_each_name_to_its_own_section():
    inventory = scanner.scan("X, Y = config['roles'], config['images']\nZ, W = 1, config['audio']\n", hidden={})
    assert inventory.names == {"X": "roles", "Y": "images", "W": "audio"}, "Z reads nothing and is not counted"
