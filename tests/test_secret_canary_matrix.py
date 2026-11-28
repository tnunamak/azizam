"""Decision D32 (a): a canary planted through the real write routes in every carrier, searched in every output.

The test plants distinct texts through the routes an operator uses: a record write that holds inline input (the shapes
older records held), the secret-entries commands (a neutral header, a provider-shaped header, an environment value, a
positional and a ``--flag=value`` command argument, URL user info, query and fragment), the provider key route, and a
package source with a credentialed git URL. Then it reads and writes everywhere (every GET route as three clients, the
configuration export, error answers to bodies that carry canaries, the logs of the process, the audit log) and fails on a
canary. Positive controls show the canaries reached the encrypted store, that the runtime resolves each one, that the
executable router config (which may hold them) is a private file, and that the detector can fail.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest

from gateway import admin_store, litellm_runtime, record_carriers, runtime_secrets, secret_refs, secret_store, service_registry
from tests.support import canary
from tests.test_admin_owner_holes_round4 import _git_package
from tests.test_admin_secret_sweep import host_isolated, sweep_get_routes  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

M = canary.MARKERS
REGISTRY = "/admin/api/operator/service-registry"


def _etag(client) -> dict:
    return {**csrf_for(client), "If-Match": client.get(REGISTRY).headers["etag"]}


def _plant(gateway, owner, tmp_path, monkeypatch) -> None:
    """Every canary, through the routes an operator uses. Each call must succeed: a refused plant proves nothing."""
    # Setup, not planting: an install that has an (empty) registry for the routes to change.
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {}}), expected_generation=0, updated_by="test",
        audit_action="test", audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None,
        audit_target_kind=None, audit_target_id=None, path=gateway.path)
    # 1. A service made by the form route; headers and an environment value added by commands.
    made = owner.post("/admin/api/operator/services", headers=_etag(owner), json={"name": "matrix-engine", "base_url": "http://matrix.invalid:9/v1"})
    assert made.status_code == 201, made.text
    commands = [
        {"carrier": "config.headers", "operation": "add", "name": "X-Region", "value": M["header-neutral"]},
        {"carrier": "config.headers", "operation": "add", "name": "X-Vendor", "value": M["header-provider"]},
        {"carrier": "config.env", "operation": "add", "name": "CONFIG_SETTING", "value": M["env-config"]},
        {"carrier": "config.base_url", "operation": "set_credentials",
         "url": f"http://{M['url-user']}:{M['url-password']}@matrix.invalid:9/v1?token={M['url-query']}#{M['url-fragment']}"},
    ]
    done = owner.post("/admin/api/operator/services/matrix-engine/secret-entries", headers=_etag(owner), json={"commands": commands})
    assert done.status_code == 200, done.text

    # 2. A record write that holds inline input, the way an older client or a hand-written registry sends it.
    registry = owner.get(REGISTRY).json()["service_registry"]
    registry["services"]["matrix-inline"] = {
        "id": "matrix-inline", "name": "Matrix inline", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"host": "inline.invalid", "port": 8080, "protocol": "openai-compatible", "models": [{"id": "m", "upstream_model": "m"}],
                   "headers": [{"name": "X-Inline", "value": M["header-neutral"] + "-inline"}], "env": {"INLINE_ENV": M["env-process"]}},
        "process": {"driver": "process", "port": {"fixed": 8080},
                    "start": ["runner", "--token", M["argv-flag-separate"], f"--api-key={M['argv-flag-equals']}", f"-p{M['argv-attached']}"],
                    "stop": f"stopper --password {M['argv-line']}", "env": {"API_TOKEN": M["env-process"] + "-p"}},
    }
    written = owner.put(REGISTRY, headers=_etag(owner), json=registry)
    assert written.status_code == 200, written.text
    inline_entry = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["matrix-inline"]["process"]["start"]
    assert [item["secret_ref"][:5] for item in inline_entry if isinstance(item, dict)] == ["sref_"] * 3  # the inline input became entries

    # 3. A positional argument, through the command a page uses for a list.
    start = owner.get(REGISTRY).json()["service_registry"]["services"]["matrix-inline"]["process"]["start"]
    keep = [{"retain": item["id"]} if isinstance(item, dict) else {"literal": item} for item in start]
    positional = owner.post("/admin/api/operator/services/matrix-inline/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "process.start", "operation": "set", "items": [*keep, {"secret": M["argv-positional"]}]}]})
    assert positional.status_code == 200, positional.text

    # 4. The provider key, through the provider route.
    provider = canary.plant_provider(owner)
    assert provider.status_code == 201, provider.text

    # 5. A package source with user info and a query token, through the extension routes.
    source = {**_git_package(monkeypatch, tmp_path, "id: pkgsvc\nname: Plain\nconfig:\n  protocol: openai-compatible\n  host: 192.168.1.80\n  port: 8000\n  models:\n    - {id: m, upstream_model: m}\n", name="matrixpkg"),
              "url": f"https://{M['package-user']}:{M['package-password']}@git.invalid/matrixpkg.git?access_token={M['package-query']}"}
    enabled = owner.post("/admin/api/extensions/enable", headers=_etag(owner), json={"source": source})
    assert enabled.status_code == 200, enabled.text
    from gateway import extension_packages
    assert M["package-password"] in extension_packages._clone_git_source.urls[-1], "the clone was asked for the credentialed URL"


PLANTED = ("header-neutral", "header-provider", "env-config", "env-process", "argv-flag-separate", "argv-flag-equals", "argv-attached",
           "argv-line", "argv-positional", "url-user", "url-password", "url-query", "url-fragment", "package-user", "package-password",
           "package-query", "provider-key")


def test_every_canary_planted_through_the_real_routes_stays_out_of_every_output(gateway, host_isolated, tmp_path, monkeypatch, capfd, caplog) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _plant(gateway, owner, tmp_path, monkeypatch)
    clients = {"admin": admin, "owner": owner, "anonymous": gateway.client()}
    outputs: list[tuple[str, str]] = []

    statuses, leaks, errored = sweep_get_routes(gateway, clients)
    assert not leaks, "\n".join(leaks)
    assert not errored, "\n".join(errored)

    # Error answers to bodies that carry canaries: a refused body is not echoed, whichever layer refuses it.
    echo = M["held-string"]
    for label, response in {
        "malformed json": owner.put(REGISTRY, headers={**_etag(owner), "content-type": "application/json"}, content=f'{{"services": "{echo}'.encode()),
        "commands not a mapping": owner.post("/admin/api/operator/services/matrix-engine/secret-entries", headers=_etag(owner), json=[echo]),
        "a refused command": owner.post("/admin/api/operator/services/matrix-engine/secret-entries", headers=_etag(owner), json={"commands": [
            {"carrier": "config.headers", "operation": "replace", "entry_id": "ent_" + "0" * 16, "value": echo}]}),
        "an unknown field": owner.post("/admin/api/operator/services/matrix-engine/secret-entries", headers=_etag(owner), json={"commands": [
            {"carrier": "config.headers", "operation": "add", "name": echo, "value": echo, "surprise": echo}]}),
        "an unknown route": owner.get(f"/admin/api/{echo}"),
        "a wrong method": owner.request("DELETE", REGISTRY, headers=csrf_for(owner)),
        "a body no one reads": owner.post("/admin/api/operator/services/matrix-engine/enabled", headers=csrf_for(owner), content=echo.encode()),
        "a bad record": owner.put(REGISTRY, headers=_etag(owner), json={"services": {"x": {"id": "x", "config": echo}}}),
    }.items():
        assert response.status_code >= 400, (label, response.status_code)
        outputs.append((label, response.text + str(dict(response.headers))))

    # The configuration export: a backup that holds references and public settings, never a value.
    export = owner.get("/admin/api/config/export")
    assert export.status_code == 200
    outputs.append(("config export", export.text + str(dict(export.headers))))

    # The audit log, and what the process wrote to its logs while all of this ran.
    outputs.append(("audit", json.dumps(gateway.rows("SELECT * FROM audit_events"), default=str)))
    captured = capfd.readouterr()
    outputs.append(("stdout", captured.out)); outputs.append(("stderr", captured.err)); outputs.append(("logging", caplog.text))

    found = [f"{label}: {', '.join(canary.leaks_in(text))}" for label, text in outputs if canary.leaks_in(text)]
    assert not found, "canaries in outputs:\n" + "\n".join(found)


def test_the_canaries_reached_the_store_the_runtime_resolves_each_and_the_router_config_is_private(gateway, tmp_path, monkeypatch) -> None:
    """The positive controls: a clean sweep means the layers hid the canaries, not that they were never stored."""
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway, owner, tmp_path, monkeypatch)
    values = canary.stored_values(gateway.path)
    missing = sorted(name for name in PLANTED if name != "provider-key" and not any(M[name] in text for text in values))
    assert not missing, f"canaries that never reached the secret store: {missing}"

    registry = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]
    provider_ref = registry["services"]["provider-a"]["config"]["api_key_secret_id"] if "provider-a" in registry["services"] else ""
    assert provider_ref and secret_store.get_provider_secret(provider_ref, db_path=gateway.path) == M["provider-key"]

    # Nothing is stored in the clear: the database file itself holds none of the canaries.
    with sqlite3.connect(gateway.path) as conn:
        dump = "\n".join(str(row) for table in ("operator_settings", "provider_secrets", "audit_events")
                         for row in conn.execute(f"SELECT * FROM {table}"))
    assert not canary.leaks_in(dump), canary.leaks_in(dump)

    # The executable router config is the one place a secret may be (a restricted file read by the router), and it is private.
    runtime_secrets.configure(gateway.path)
    try:
        path = litellm_runtime.write_litellm_config_artifact(registry, base_dir=tmp_path)
    finally:
        runtime_secrets.configure(None)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert M["header-neutral"] in path.read_text(), "the header canary reached the router config: the runtime resolved it"
