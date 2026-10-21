"""Reviewer HTTP differential: current checks and the original Library predicates."""
from __future__ import annotations

import json
import pytest
from collections.abc import Mapping
from typing import Any
from unittest.mock import patch

import proxy  # noqa: F401  (import order, as the suite does)
from tests import test_me_media as media_helpers
from gateway import admin_store, me_media, media_jobs, ownership, receipt_store


def _main_receipt_in_scope(receipt: Mapping[str, Any] | None, scope) -> bool:
    if scope.app_ids is None:
        return True
    if receipt is None:
        return False
    if receipt.get("app_id"):
        return str(receipt["app_id"]) in set(scope.app_ids)
    owner_key = receipt.get("owner_key_hash")
    return isinstance(owner_key, str) and owner_key in (scope.owner_keys or frozenset())


def _main_job_in_scope(job: Mapping[str, Any], scope) -> bool:
    if scope.app_ids is None:
        return True
    if job.get("app_id"):
        return str(job["app_id"]) in set(scope.app_ids)
    owner_key = job.get("owner_key_hash")
    return isinstance(owner_key, str) and owner_key in (scope.owner_keys or frozenset())


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in {"polled_at", "generated_at", "now"}}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


@pytest.fixture
def media_world():
    world = media_helpers.MeMediaRouterTests(methodName="runTest")
    world.setUp()
    try:
        yield world
    finally:
        world.tearDown()


# Imported unittest classes are fixture helpers, not additional collected cases.

def test_ov_rv_same_requests_same_answers(media_world) -> None:
    self = media_world
    admin = admin_store.upsert_oidc_user("issuer", "admin-sub", email="admin@example.com", path=self.admin_db)
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=admin["id"], path=self.admin_db)
    svc_owner = {"id": self.service_app["owner_user_id"], "email": "svc@example.com"}
    apps = [self.app_a["id"], self.app_b["id"], self.service_app["id"]]
    n = 0
    for app_id in [*apps, None]:
        for key in ("sha256:alice", "sha256:bob", "", None):
            n += 1
            rid = f"req_{n}"
            fields = {"request_id": rid, "status": "succeeded", "intent": "image.generate"}
            if app_id:
                fields["app_id"] = app_id
            if key is not None:
                fields["owner_key_hash"] = key
            receipt_store.record(fields, path=self.receipts_db)
            asset = "img_" + f"{n:064x}"
            owners = [key] if key else ([ownership.principal_key("app", app_id)] if app_id else [])
            self._write_asset(asset, owners, request_id=rid)
            media_jobs.create(media_jobs.MediaJob(
                request_id=rid, intent="image.generate", workflow="wf.image", status=media_jobs.STATUS_SUCCEEDED,
                progress=100, app_id=app_id, owner_key_hash=key or None,
                assets=[{"asset_id": asset, "media_type": "image/png"}]), path=self.jobs_db)
    personas = {"member-alice": self.alice, "member-bob": self.bob, "owner": self.owner, "admin": admin,
                "service-accountable": svc_owner}
    paths = ["/me/api/media", "/me/api/media?scope=everyone", "/me/api/media?person=all",
             f"/me/api/media?person={self.alice['id']}", "/me/api/media?person=local-network",
             "/me/api/usage/summary", "/me/api/usage/summary?person=all",
             f"/me/api/usage/summary?person={self.bob['id']}", "/me/api/jobs", "/me/api/jobs?scope=everyone"]
    paths += [f"/me/api/media/img_{i:064x}" for i in range(1, n + 1)]
    compared = 0
    for label, user in personas.items():
        self.current_user = user
        for path in paths:
            new = self.client.get(path)
            with patch.object(me_media, "_receipt_in_scope", _main_receipt_in_scope), \
                    patch.object(me_media, "_job_in_scope", _main_job_in_scope):
                old = self.client.get(path)
            body_new = _strip(new.json()) if new.headers.get("content-type", "").startswith("application/json") else new.content
            body_old = _strip(old.json()) if old.headers.get("content-type", "").startswith("application/json") else old.content
            assert (new.status_code, body_new) == (old.status_code, body_old), (label, path)
            compared += 1
    self.current_user = self.alice
    mine = self.client.get("/me/api/media").json()["assets"]
    self.current_user = self.owner
    everyone = self.client.get("/me/api/media?scope=everyone").json()["assets"]
    usage = self.client.get("/me/api/usage/summary?person=all").json()["requests"]
    print(f"OV-RV sizes: alice mine={len(mine)} owner everyone={len(everyone)} owner usage requests={usage}")
    with patch.object(me_media, "_job_in_scope", lambda job, scope: True), patch.object(me_media, "_receipt_in_scope", lambda r, scope: True):
        self.current_user = self.alice
        widened = self.client.get("/me/api/media").json()["assets"]
    print(f"OV-RV sensitivity: alice mine with always-true scope={len(widened)}")
    print(f"OV-RV differential: {compared} request pairs identical across {len(personas)} personas")
    assert compared == len(personas) * len(paths)
