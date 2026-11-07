"""One scripted journey through the three folded owner settings, recorded step by step.

The journey drives the real routers (``create_settings_router``, ``create_admin_router`` over ``AdminSQLiteStore``) on a
scratch admin.db and records ``(method, path, status, body)`` for every request, then the audit rows and the effective
settings view. ``tests/test_config_fold_responses.py`` compares the record with
``tests/fixtures/config_homes/c6_responses.json``.

The fixture was captured on the code before slice C6 (instance, resource admission and service availability read and
written ``operator_settings`` directly). The only change C6 makes to it is the conflict status of two steps, 409 to 412.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import accounts, admin_store
from gateway.admin_api import AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore
from gateway.settings_api import create_settings_router

INSTANCE = "/admin/api/settings/instance"
MEDIA = "/admin/api/operator/media-job-admission"
AVAILABILITY = "/admin/api/operator/service-availability"
OVERRIDES = "/admin/api/operator/overrides"


def _client(path: Path) -> TestClient:
    admin_store.init_db(path)
    accounts.initialize(path)
    store = AdminSQLiteStore(path)
    actor = AdminContext(subject="fixture-owner", roles=frozenset({"owner"}), auth_method="bearer")
    app = FastAPI()
    app.include_router(create_admin_router(store, auth=lambda _request: actor))
    app.include_router(create_settings_router(auth=lambda _request: actor, admin_path=path, config_provider=lambda: {}))
    return TestClient(app)


def _body(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def run(path: Path) -> dict[str, Any]:
    client = _client(path)
    steps: list[dict[str, Any]] = []

    def call(method: str, url: str, *, json: Any = None, headers: dict[str, str] | None = None, name: str = "") -> Any:
        response = client.request(method, url, json=json, headers=headers or {})
        body = _body(response)
        steps.append({"step": name or f"{method} {url}", "method": method, "path": url, "status": response.status_code, "body": body})
        return body

    # -- instance -----------------------------------------------------------------------------------------------------
    fresh = call("GET", INSTANCE, name="instance: fresh")
    call("PUT", INSTANCE, json={"name": "Home", "base_url": "https://gw.example", "theme_default": "dark"},
         name="instance: no If-Match")
    saved = call("PUT", INSTANCE, json={"name": " Home ", "base_url": "https://gw.example/", "theme_default": "dark"},
                 headers={"If-Match": f'"{fresh["revision"]}"'}, name="instance: first save")
    call("GET", INSTANCE, name="instance: after save")
    call("PUT", INSTANCE, json={"name": "Other", "base_url": "", "theme_default": "light"},
         headers={"If-Match": f'"{fresh["revision"]}"'}, name="instance: stale write")
    call("PUT", INSTANCE, json={"name": "Home", "base_url": "https://gw.example", "theme_default": "dark"},
         headers={"If-Match": saved["revision"]}, name="instance: identical re-save")
    current = call("GET", INSTANCE, name="instance: after identical re-save")
    call("PUT", INSTANCE, json={"name": "", "base_url": "", "theme_default": "dark"},
         headers={"If-Match": current["revision"]}, name="instance: empty name")
    call("PUT", INSTANCE, json={"name": "X", "base_url": "ftp://gw.example", "theme_default": "dark"},
         headers={"If-Match": current["revision"]}, name="instance: bad url")
    call("PUT", INSTANCE, json={"name": "X", "theme_default": "dark"},
         headers={"If-Match": current["revision"]}, name="instance: missing field")
    call("PUT", INSTANCE, json={"name": "Home", "base_url": "https://gw.example", "theme_default": "sepia"},
         headers={"If-Match": current["revision"]}, name="instance: bad theme")

    # -- media job admission ------------------------------------------------------------------------------------------
    overrides = call("GET", OVERRIDES, name="overrides: fresh")
    media_rev = overrides["overrides"]["revisions"]["media_job_admission_overrides"]
    call("PUT", MEDIA, json={"max_queue_depth": 3}, name="media: no If-Match")
    first = call("PUT", MEDIA, json={"max_queue_depth": 3, "disabled_intents": [" a ", "b", "a"]},
                 headers={"If-Match": media_rev}, name="media: first save")
    call("GET", OVERRIDES, name="overrides: after media save")
    call("PUT", MEDIA, json={"max_queue_depth": 5}, headers={"If-Match": media_rev}, name="media: stale write")
    withapps = call("PUT", MEDIA, json={"max_app_concurrency": {"mine": 2}, "retry_after_seconds": 7},
                    headers={"If-Match": f'"{first["revision"]}"'}, name="media: with app limits")
    replaced = call("PUT", MEDIA, json={"reserved_tokens": ["t1"]}, headers={"If-Match": withapps["revision"]},
                    name="media: replace whole document")
    call("PUT", MEDIA, json={"max_queue_depth": -1}, headers={"If-Match": replaced["revision"]}, name="media: negative depth")
    call("PUT", MEDIA, json={"bogus": 1}, headers={"If-Match": replaced["revision"]}, name="media: unknown field")
    call("PUT", MEDIA, json={"max_app_concurrency": {"mine": -1}}, headers={"If-Match": replaced["revision"]},
         name="media: bad app limit")
    call("PUT", MEDIA, json={"disabled_intents": "nope"}, headers={"If-Match": replaced["revision"]}, name="media: bad list")
    call("PUT", MEDIA, json={}, headers={"If-Match": replaced["revision"]}, name="media: empty document")
    call("GET", OVERRIDES, name="overrides: after media empty save")

    # -- service availability -----------------------------------------------------------------------------------------
    call("PUT", AVAILABILITY, json={"engine-a": {"state": "maintenance", "reason": " fixing ", "retry_after_seconds": 30}},
         name="availability: set one")
    call("PUT", AVAILABILITY, json={"overrides": {"engine-b": {"status": "draining"}}}, name="availability: add second, wrapped body")
    call("GET", OVERRIDES, name="overrides: after availability")
    call("PUT", AVAILABILITY, json={"engine-a": {"state": "available"}}, name="availability: clear one")
    call("PUT", AVAILABILITY, json={"engine-b": {"state": "bogus"}}, name="availability: bad state")
    call("PUT", AVAILABILITY, json={"engine-b": {"state": "disabled", "extra": 1}}, name="availability: unknown field")
    call("PUT", AVAILABILITY, json={"engine-b": {"state": "disabled", "reason": 3}}, name="availability: bad reason")
    call("PUT", AVAILABILITY, json={"engine-b": {"state": "disabled", "retry_after_seconds": -2}}, name="availability: bad retry")
    call("PUT", AVAILABILITY, json={"": {"state": "disabled"}}, name="availability: empty id")
    call("PUT", AVAILABILITY, json={"a/b~c": {"state": "degraded"}}, name="availability: id with slash and tilde")
    call("PUT", AVAILABILITY, json={"engine-b": {"state": "available"}, "a/b~c": {"state": "available"}},
         name="availability: clear the rest")
    call("GET", OVERRIDES, name="overrides: after availability cleared")

    # Events of one second have no stable order, so the audit is compared as a sorted list.
    audit = sorted(
        ({key: row.get(key) for key in ("action", "actor_kind", "actor_id", "target_kind", "target_id", "metadata")}
         for row in admin_store.list_audit(path=path)),
        key=lambda row: json.dumps(row, sort_keys=True),
    )
    return {"steps": steps, "audit": audit}
