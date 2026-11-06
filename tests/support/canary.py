"""Canary secrets for the leak oracles (decision D32).

A canary is a distinct text planted in one carrier. The oracles plant canaries, read and write through the real app, and
fail when any canary appears in an output. Two ways of planting exist:

- ``seed_direct`` writes records shaped like the ones older versions stored (an inline header value, a plain environment
  value, a command argument after a credential flag, URL user info and query) and moves them with the same function the
  schema 11 migration and every record write use. The per-route sweeps use it because it is quick.
- ``tests/test_secret_canary_matrix.py`` plants through the HTTP write routes themselves.

Provider-shaped canaries (``sk-...``) and unremarkable ones are both used: a scanner that only knows one pattern would
pass. Nothing here decides what is secret; a canary is secret because the test says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gateway import admin_store, record_carriers, secret_refs, service_registry

# One text per place a secret can live. Each is unique, so a hit names the carrier.
MARKERS: dict[str, str] = {
    "header-neutral": "xvq-region-eu-9f3k2",
    "header-provider": "sk-live-CANARYheader0001abcdef",
    "env-process": "envcanary-process-7731",
    "env-config": "envcanary-config-7732",
    "env-driver": "envcanary-driver-7733",
    "argv-flag-separate": "argvcanary-separate-4401",
    "argv-flag-equals": "argvcanary-equals-4402",
    "argv-attached": "argvcanary-attached-4403",
    "argv-line": "argvcanary-line-4404",
    "argv-url": "argvcanary-url-4405",
    "argv-positional": "argvcanary-positional-4406",
    "url-user": "canuser-6613",
    "url-password": "canpass-6614",
    "url-query": "canquery-8815",
    "url-fragment": "canfrag-8816",
    "probe-query": "canprobe-9917",
    "health-query": "canhealth-9918",
    "package-user": "canpkguser-5521",
    "package-password": "canpkgpass-5522",
    "package-query": "canpkgquery-5523",
    "provider-key": "sk-or-v1-CANARYprovider0002abcdef",
    "held-header": "canheld-header-1101",
    "held-env": "canheld-env-1102",
    "held-url": "canheld-url-1104",
    "held-string": "canheld-string-1105",
}

# Canaries planted by tests/test_review_w1_r3_cases.py, from the independent review of the earlier projection: values no
# name or shape recognizer knows. They are searched for in every output, but the seed does not plant them.
REVIEW_MARKERS: dict[str, str] = {
    "review-opaque": "hunter2abc-r3o",
    "review-basic": "Basic dTpwcjNiYXNpYw",
    "review-jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyM2p3dCJ9.c3ludGhldGljcjNqd3Q",
    "review-flag": "flagged-r3-4471",
    "review-map": "mapped-r3-4472",
    "review-submitted": "sk-synthetic-access-credential-r3x",
    "review-signature": "canreviewsig-7701",
    "review-amz": "canreviewamz-7702",
    "review-stored-short": "pw-r3",
}

# A key-shaped text no canary accounts for is a secret the planting missed: a tripwire, never a rule.
SHAPE_TRIPWIRE = re.compile(r"\b(?:opk_|oak_|sk-)[A-Za-z0-9_-]{16,}")


def legacy_services() -> dict[str, dict[str, Any]]:
    """Records shaped like the ones stored before decision D26: every carrier holds its canary inline."""
    m = MARKERS
    native = {
        "id": "engine-a", "name": "Engine A", "kind": "provider", "source": "ui", "enabled": True, "capabilities": ["chat"],
        "config": {
            "address": f"http://{m['url-user']}:{m['url-password']}@engine.invalid:8080/v1?token={m['url-query']}#{m['url-fragment']}",
            "protocol": "openai-compatible", "models": [{"id": "first", "upstream_model": "upstream-a"}],
            "headers": [{"name": "X-Region", "value": m["header-neutral"]}, {"name": "X-Vendor", "value": m["header-provider"]}],
            "env": {"CONFIG_SETTING": m["env-config"]},
        },
        "process": {
            "driver": "process", "port": {"fixed": 8080},
            "start": ["runner", "--token", m["argv-flag-separate"], f"--api-key={m['argv-flag-equals']}", f"-p{m['argv-attached']}",
                      "--upstream", f"http://u:{m['argv-url']}@argv.invalid/x", "--tokenizer", "plain-tokenizer", "--max-tokens", "4096"],
            "stop": f"stopper --password {m['argv-line']} --now",
            "env": {"API_TOKEN": m["env-process"], "MODE": "prod"},
            "health_endpoint": f"/health?access_token={m['health-query']}",
        },
        "observations": {"ready": {"http": {"path": f"/ready?sig={m['probe-query']}", "expect": 200}}},
    }
    legacy = {
        "id": "engine-b", "name": "Engine B", "kind": "provider", "source": "ui", "enabled": True, "capabilities": ["chat"],
        "config": {"address": "http://engine-b.invalid:9090/v1", "protocol": "openai-compatible",
                   "models": [{"id": "second", "upstream_model": "upstream-b"}]},
        "lifecycle": {"driver": "command", "type": "command", "managed": True, "service_name": "",
                      "driver_config": {"start": f"legacy-runner --token {m['argv-line']}-b", "stop": ["legacy-stop"],
                                        "env": {"LEGACY_PASSWORD": m["env-driver"]}}},
    }
    loader = {
        "id": "engine-d", "name": "Engine D", "kind": "provider", "source": "ui", "enabled": True, "capabilities": ["chat"],
        "config": {"address": "http://engine-d.invalid:7070/v1", "protocol": "openai-compatible",
                   "models": [{"id": "fourth", "upstream_model": "upstream-d"}]},
        "lifecycle": {"driver": "http-load-unload", "type": "http-load-unload", "managed": True, "service_name": "",
                      "driver_config": {"load_url": "http://engine-d.invalid:7070/load"}},
    }
    return {"engine-a": native, "engine-b": legacy, "engine-d": loader}


def held_records() -> dict[str, Any]:
    """Records the registry leaves out, written raw as a hand edit could leave them, with canaries in the carriers."""
    m = MARKERS

    def held(service_id: str, **config: Any) -> dict[str, Any]:
        return {"id": service_id, "name": service_id, "kind": "engine", "enabled": True, "capabilities": ["chat"],
                "config": {"host": "10.0.0.9", "port": 8080, "models": ["not a model record"], **config}}

    return {
        "held-header": held("held-header", headers=[{"name": "X-Held", "value": m["held-header"]}]),
        "held-env": held("held-env", env={"HELD_ENV": m["held-env"]}),
        "held-url": held("held-url", base_url=f"http://svc:{m['held-url']}@127.0.0.1:9/v1"),
        "held-string": f"password={m['held-string']}",
    }


def seed_direct(gateway: Any) -> dict[str, Any]:
    """Store the legacy-shaped records moved into the secret store, plus the left-out ones, as one registry.

    Returns the stored registry document, so a test can compare it with what a read shows.
    """
    put = secret_refs.putter(gateway.path)
    services: dict[str, Any] = {}
    for service_id, record in legacy_services().items():
        moved, _ = record_carriers.externalize_service(record, put)
        services[service_id] = moved
    document = service_registry.normalize_service_registry({"schema_version": 2, "services": services})
    document["services"].update(held_records())
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    admin_store.write_service_registry_with_audit(
        document, expected_generation=int(current["operator_settings_generation"]) if current else 0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)
    index = {
        "schema_version": 1,
        "sources": [{"type": "git", "url": f"https://{MARKERS['package-user']}:{MARKERS['package-password']}@git.invalid/repo.git?access_token={MARKERS['package-query']}"}],
        "packages": {}, "updated_at": 1,
    }
    index, _ = record_carriers.externalize_package_index(index, put)
    admin_store.put_operator_setting("extension_package_index", index, path=gateway.path)
    return document


def stored_values(db_path: Path) -> set[str]:
    """Every secret value the store holds, resolved: the positive control that a canary reached the store."""
    from gateway import runtime_secrets

    runtime_secrets.configure(db_path)
    try:
        registry = admin_store.get_service_registry_with_generation(path=db_path)["value"]
        index = (admin_store.get_operator_setting("extension_package_index", path=db_path) or {"value": {}})["value"]
        found: set[str] = set()
        for ref in record_carriers.refs_in(registry) + record_carriers.refs_in(index):
            try:
                found.add(runtime_secrets.resolve(ref))
            except runtime_secrets.SecretUnavailable:
                pass
        return found
    finally:
        runtime_secrets.configure(None)


def leaks_in(text: str, markers: dict[str, str] | None = None) -> list[str]:
    """The names of the canaries that appear in ``text``, plus any key-shaped text no canary accounts for."""
    markers = {**MARKERS, **REVIEW_MARKERS} if markers is None else markers
    found = sorted(name for name, value in markers.items() if value in text)
    values = set(markers.values())
    found += [f"key-shaped:{hit[:14]}" for hit in SHAPE_TRIPWIRE.findall(text) if not any(value in hit or hit in value for value in values)]
    return found


def dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


MODEL = {"id": "test/model", "upstream_model": "test/model", "enabled": True,
         "price": {"input_usd_per_million_tokens": "3.00", "output_usd_per_million_tokens": "15.00"}}


def plant_provider(owner: Any, provider_id: str = "provider-a") -> Any:
    """Create a provider through the real route with a provider-shaped key: the key goes to the store, never a record."""
    from tests.test_signin_who_can_enter import csrf_for

    return owner.post("/admin/api/providers", headers=csrf_for(owner), json={
        "preset_id": "openrouter", "provider_id": provider_id, "key_mode": "secret",
        "api_key": MARKERS["provider-key"], "models": [MODEL], "caps": {"daily_usd": 5}})
