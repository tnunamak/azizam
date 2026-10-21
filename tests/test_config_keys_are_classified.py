"""Decision D34: production code may read a service's config key only if a projection type classifies it.

A key of a service record's ``config`` that no type classifies is stored unchanged and never shown (only counted), so
a key production reads and nobody classified would be a setting the operator can set and never see. The scan below
reads the production source (no import, no run) for the keys it reads from a service's config and fails for each one
``projection_types.CONFIG`` does not name.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from gateway import projection, projection_types, record_carriers, secret_refs

ROOT = Path(__file__).resolve().parent.parent

# Where production code holds a service record's ``config`` mapping, and the names it binds it to there. A file that
# is not listed reads some other mapping (the YAML file, a transform's own settings) under the same names.
SERVICE_CONFIG_READERS: dict[str, frozenset[str]] = {
    "proxy.py": frozenset({"service_config", "backend_cfg", "backend_config"}),
    "gateway/service_registry.py": frozenset({"config"}),
    "gateway/engine_admin.py": frozenset({"config"}),
    "gateway/engine_control.py": frozenset({"config"}),
    "gateway/engine_contract_view.py": frozenset({"config"}),
    "gateway/litellm_substrate.py": frozenset({"config"}),
    "gateway/backends/llm_execution.py": frozenset({"config"}),
    "gateway/providers.py": frozenset({"config"}),
    "gateway/provider_admin.py": frozenset({"config"}),
    "gateway/provider_configuration.py": frozenset({"config"}),
    "gateway/provider_execution.py": frozenset({"config"}),
    "gateway/provider_listing.py": frozenset({"config"}),
    "gateway/provider_model_catalog.py": frozenset({"config"}),
    "gateway/provider_reconciliation.py": frozenset({"config"}),
    "gateway/decision_provider_execution.py": frozenset({"config"}),
    "gateway/model_presentation.py": frozenset({"config"}),
    "gateway/admin_catalog.py": frozenset({"config"}),
    "gateway/availability.py": frozenset({"config"}),
}

# Keys production reads from the mapping above that are not settings of a service at all, with the reason.
NOT_A_SERVICE_SETTING = {
    "backends": "the YAML file's backend table",
    "llm": "the YAML file's own section", "proxy": "the YAML file's own section", "admin": "the YAML file's own section",
    "images": "the YAML file's own section", "videos": "the YAML file's own section", "audio": "the YAML file's own section",
    "identity": "the YAML file's own section", "mcp": "the YAML file's own section", "gateway": "the YAML file's own section",
    "model_routing": "the YAML file's own section", "model_aliases": "the YAML file's own section",
    "model_compatibility": "the YAML file's own section", "roles": "the YAML file's own section",
    "voices": "the YAML file's own section", "routing": "the YAML file's own section",
    # the record's own sections, read through ``config`` by a helper that looks in the record first
    "observations": "a record section (classified on the record)",
}


def _reads() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def receiver(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    for relative, names in SERVICE_CONFIG_READERS.items():
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            key = who = None
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop", "setdefault")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                key, who = node.args[0].value, receiver(node.func.value)
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                key, who = node.slice.value, receiver(node.value)
            if key is not None and who in names:
                found.setdefault(key, set()).add(relative)
    return found


def _classified() -> set[str]:
    return set(projection_types.CONFIG.fields)


def test_every_config_key_production_reads_is_classified():
    unclassified = {key: sorted(files) for key, files in _reads().items()
                    if key not in _classified() and key not in NOT_A_SERVICE_SETTING}
    assert not unclassified, (
        "production code reads these keys of a service's config and no projection type classifies them (decision D34): "
        f"{unclassified}. Classify each in gateway/projection_types.py (CONFIG_SCALAR_KEYS, CONFIG_DESCRIPTOR_KEYS or its own "
        "type), or hide it by leaving it out.")


def test_the_scan_finds_the_keys_and_would_catch_a_new_one(tmp_path, monkeypatch):
    """The scan is not vacuous: it sees a key production reads, and it flags one nobody classified."""
    reads = _reads()
    assert {"host", "port", "protocol", "models", "api_key_env"} <= set(reads)
    source = tmp_path / "fake.py"
    source.write_text("def f(config):\n    return config.get('brand_new_setting') or config['another_new_one']\n")
    monkeypatch.setattr(__import__(__name__), "ROOT", tmp_path, raising=False)
    monkeypatch.setitem(SERVICE_CONFIG_READERS, "fake.py", frozenset({"config"}))
    monkeypatch.setattr(__import__(__name__), "SERVICE_CONFIG_READERS", {"fake.py": frozenset({"config"})}, raising=False)
    assert set(_reads()) == {"brand_new_setting", "another_new_one"}
    assert not {"brand_new_setting", "another_new_one"} & _classified()


def _record(**config):
    return {"id": "e", "name": "E", "kind": "engine", "enabled": True, "capabilities": ["chat"],
            "config": {"host": "10.0.0.5", "port": 8000, "protocol": "openai-compatible", **config}}


def test_an_unclassified_key_is_omitted_and_counted_and_never_shown():
    shown = projection.project("ServiceRecord", _record(zz_note="visible?", zz_nested={"a": {"token": "TOPSECRET-1"}}, zz_list=["TOPSECRET-2"]))
    config = shown.tree["config"]
    assert config["hidden_settings"] == 3
    assert not {"zz_note", "zz_nested", "zz_list"} & set(config)
    assert "TOPSECRET" not in repr(shown.tree)
    assert sorted(shown.hidden) == ["config.zz_list", "config.zz_nested", "config.zz_note"]
    assert config["host"] == "10.0.0.5" and config["port"] == 8000  # a classified key is shown


def test_a_classified_key_is_shown_and_does_not_count_as_hidden():
    shown = projection.project("ServiceRecord", _record(base_path="/v1", transforms=["a"], timeout=30))
    assert "hidden_settings" not in shown.tree["config"]
    assert shown.tree["config"]["base_path"] == "/v1" and shown.tree["config"]["transforms"] == ["a"]


@pytest.mark.parametrize("block,where", [
    ({"provenance": {"package_id": "p", "package_version": "1", "token": "TOPSECRET-3"}}, ("provenance",)),
    ({"residency": {"load": {"op": "load", "args": {"key": "TOPSECRET-4"}}, "note": "TOPSECRET-5"}}, ("residency",)),
    ({"resources": {"gpu_required": True, "note": "password=TOPSECRET-6"}}, ("resources",)),
])
def test_provenance_residency_and_resources_show_only_their_classified_fields(block, where):
    shown = projection.project("ServiceRecord", {**_record(), **block})
    assert "TOPSECRET" not in repr(shown.tree)
    assert shown.tree[where[0]]["hidden_settings"] >= 1


def test_unchanged_write_of_a_read_keeps_every_hidden_setting():
    stored = _record(zz_note="kept", zz_nested={"a": 1})
    stored["provenance"] = {"package_id": "p", "extra_hidden": "x"}
    read = projection.project("ServiceRecord", stored).tree
    assert "zz_note" not in read["config"]
    put = lambda value, origin=None: secret_refs.new_ref()  # noqa: E731
    written, _ = record_carriers.ingest_service(copy.deepcopy(read), stored, put)
    assert written["config"]["zz_note"] == "kept" and written["config"]["zz_nested"] == {"a": 1}
    assert "hidden_settings" not in written["config"] and "hidden_settings" not in written["provenance"]
    assert written["provenance"]["extra_hidden"] == "x"


def test_a_write_that_names_a_hidden_key_replaces_it_and_a_stale_count_is_never_stored():
    stored = _record(zz_note="old")
    incoming = _record(zz_note="new", hidden_settings=7)
    put = lambda value, origin=None: secret_refs.new_ref()  # noqa: E731
    written, _ = record_carriers.ingest_service(incoming, stored, put)
    assert written["config"]["zz_note"] == "new" and "hidden_settings" not in written["config"]
    first, _ = record_carriers.ingest_service(_record(hidden_settings=1), None, put)
    assert "hidden_settings" not in first["config"]


# ---------------------------------------------------------------------------------------------------------------------
# Review L2: the scan above names the receivers `config` and `cfg` in a short list of files. A key read through any other
# name (`lifecycle.get("api_key")`, `bcfg.get("preset")`) or in any other file escaped it. These two scans do not depend on
# the receiver being named config: one follows the other blocks of a record by the names production gives them, in every
# production file; the other sweeps every file for a key that names a credential.
# ---------------------------------------------------------------------------------------------------------------------

# Receiver names that hold one block of a service record anywhere in production, with the type that classifies the block.
BLOCK_RECEIVERS = {
    "lifecycle": projection_types.LIFECYCLE, "legacy_lifecycle": projection_types.LIFECYCLE,
    "dc": projection_types.DRIVER_CONFIG, "driver_config": projection_types.DRIVER_CONFIG,
    "bcfg": projection_types.BACKEND_VIEW, "raw_cfg": projection_types.BACKEND_VIEW,
    "backend_cfg": projection_types.BACKEND_VIEW, "backend_config": projection_types.BACKEND_VIEW,
    "service_config": projection_types.BACKEND_VIEW,
}
# A name from the table that holds some other mapping in one file, with the reason.
NOT_A_RECORD_BLOCK = {
    ("gateway/config_sources.py", "lifecycle"): "the descriptor of a config source (authored_via, stored_in), not a record's lifecycle",
}
# Functions that read one key of a mapping given as their first argument (`runtime_secrets.scalar(lifecycle, "api_key")`).
KEY_READING_CALLS = frozenset({"scalar", "url"})
# Keys that name a credential and are read from a mapping that is not a service record or one of its blocks, with the reason.
# Everything else a production file reads under a credential-like name must be a field some projection type classifies.
CREDENTIAL_NAMES_OF_OTHER_THINGS = {
    "AZIZAM_SUPERVISOR_REDACT_ENV": "the name of an environment variable read from os.environ",
    "client_secret_env": "a setting of the OIDC provider (admin settings), not of a service",
    "signing_key_env": "a setting of the OIDC provider (admin settings), not of a service",
    "session_signing_key_env": "a setting of the OIDC provider (admin settings), not of a service",
    "provider_secret_id": "a provider's secret id read by the router sidecar from the provider record it was handed",
    "secret_env": "a native credential's environment variable, inside connection.credentials (classified as a whole)",
    "secret_ref": "the reference an entry holds; every projection reads it through its entry policies",
    "source_ref": "a config source's own descriptor",
    "voice_ref": "a voice reference in a request, not a service setting",
}


def _production_files() -> list[str]:
    roots = [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py")), *sorted(ROOT.glob("*.py"))]
    return sorted({str(path.relative_to(ROOT)) for path in roots})


def _every_read(source: str):
    """(receiver name or None, key, line) for each `x.get("k")`, `x["k"]`, `x.pop("k")` and `runtime_secrets.scalar(x, "k")`."""
    def receiver(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop", "setdefault")
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            yield receiver(node.func.value), node.args[0].value, node.lineno
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            yield receiver(node.value), node.slice.value, node.lineno
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in KEY_READING_CALLS
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "runtime_secrets"
              and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            yield receiver(node.args[0]), node.args[1].value, node.lineno


def _block_reads(read_file=lambda relative: (ROOT / relative).read_text(), files=None) -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for relative in files or _production_files():
        for who, key, _line in _every_read(read_file(relative)):
            if who in BLOCK_RECEIVERS and (relative, who) not in NOT_A_RECORD_BLOCK:
                found.setdefault((who, key), set()).add(relative)
    return found


def test_every_key_production_reads_from_a_record_block_under_any_file_is_classified():
    unclassified = {f"{who}.{key}": sorted(files) for (who, key), files in _block_reads().items()
                    if key not in BLOCK_RECEIVERS[who].fields}
    assert not unclassified, (
        f"production code reads these keys of a record block and its projection type does not classify them: {unclassified}. "
        "Add each to the type in gateway/projection_types.py (decision D34), or list the receiver in NOT_A_RECORD_BLOCK.")


def test_the_block_scan_sees_the_driver_keys_and_would_flag_an_unclassified_one():
    reads = _block_reads()
    assert {("lifecycle", "admin_key"), ("lifecycle", "api_key"), ("dc", "admin_key_env"), ("dc", "api_key_env"),
            ("dc", "load_url")} <= set(reads), sorted(reads)
    fake = "def f(lifecycle, dc):\n    return lifecycle.get('brand_new_key'), dc['another_new_key'], runtime_secrets.scalar(lifecycle, 'third_new_key')\n"
    seen = _block_reads(read_file=lambda _relative: fake, files=["fake.py"])
    assert {key for _who, key in seen} == {"brand_new_key", "another_new_key", "third_new_key"}
    assert not {key for _who, key in seen} & (set(projection_types.LIFECYCLE.fields) | set(projection_types.DRIVER_CONFIG.fields))


def _every_field_name() -> set[str]:
    from gateway.projection import Alt, Dict, Obj, Seq

    names: set[str] = set()
    seen: set[int] = set()

    def walk(policy) -> None:
        if policy is None or id(policy) in seen:
            return
        seen.add(id(policy))
        if isinstance(policy, Obj):
            names.update(policy.fields)
            for field in policy.fields.values():
                walk(field)
            walk(policy.extras)
        elif isinstance(policy, Seq):
            walk(policy.item)
        elif isinstance(policy, Dict):
            walk(policy.value)
        elif isinstance(policy, Alt):
            for option in policy.options:
                walk(option)

    for obj in projection.TYPES.values():
        walk(obj)
    return names


def test_every_credential_named_key_production_reads_is_classified_by_some_type_or_reviewed():
    from gateway.service_registry import _names_credential

    classified = _every_field_name()
    unreviewed: dict[str, list[str]] = {}
    for relative in _production_files():
        for _who, key, _line in _every_read((ROOT / relative).read_text()):
            if _names_credential(key) and key not in classified and key not in CREDENTIAL_NAMES_OF_OTHER_THINGS:
                unreviewed.setdefault(key, []).append(relative)
    assert not unreviewed, (
        f"production code reads these credential-named keys and no projection type has a field of that name: {unreviewed}. "
        "Declare the carrier in gateway/projection_types.py, or add the key to CREDENTIAL_NAMES_OF_OTHER_THINGS with the reason.")


def test_the_credential_review_list_has_no_stale_entry():
    from gateway.service_registry import _names_credential

    read = {key for relative in _production_files() for _who, key, _line in _every_read((ROOT / relative).read_text())}
    assert set(CREDENTIAL_NAMES_OF_OTHER_THINGS) <= read, sorted(set(CREDENTIAL_NAMES_OF_OTHER_THINGS) - read)
    assert all(_names_credential(key) or key.startswith("AZIZAM_") for key in CREDENTIAL_NAMES_OF_OTHER_THINGS)
