"""Tests for the gateway-native ``/gateway/v1/media/jobs`` endpoints."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import media_jobs  # noqa: E402
from gateway.api import create_gateway_router  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402


SIMPLE_MANIFEST_YAML = textwrap.dedent(
    """
    schema_version: 1
    version: "0.1.0"
    id: image.generate.simple.example
    intent: image.generate
    source: local
    trusted: false
    inputs:
      prompt:
        type: string
        required: true
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/simple.json
        map:
          prompt:
            - node: "6"
              input: text
    """
).strip()


def _build_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    registry.add_manifest(
        yaml.safe_load(SIMPLE_MANIFEST_YAML),
        source_path="/srv/azizam/manifests/workflows/simple.yml",
    )
    return registry


def _make_client(
    *,
    audience: str | None = None,
    media_job_store_path: Path | None = None,
    **overrides: Any,
) -> TestClient:
    registry = _build_registry()
    resolver = None
    if audience is not None:
        resolver = lambda _request, _a=audience: _a  # noqa: E731
    app = FastAPI()
    app.include_router(
        create_gateway_router(
            registry,
            audience_resolver=resolver,
            media_job_store_path=media_job_store_path,
            **overrides,
        )
    )
    return TestClient(app)


class GatewayMediaJobsNoStoreTests(unittest.TestCase):
    def test_create_returns_503_without_store(self) -> None:
        client = _make_client()
        response = client.post(
            "/gateway/v1/media/jobs",
            json={"intent": "image.generate"},
        )
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["code"], "media_jobs_unavailable")

    def test_list_returns_empty_without_store(self) -> None:
        client = _make_client()
        response = client.get("/gateway/v1/media/jobs")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["audience"], "client")
        self.assertEqual(body["jobs"], [])

    def test_get_returns_404_without_store(self) -> None:
        client = _make_client()
        response = client.get("/gateway/v1/media/jobs/mj_unknown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "media_job_not_found"
        )

    def test_list_events_returns_404_without_store(self) -> None:
        client = _make_client()
        response = client.get("/gateway/v1/media/jobs/mj_unknown/events")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "media_job_not_found"
        )

    def test_event_returns_503_without_store(self) -> None:
        client = _make_client()
        response = client.post(
            "/gateway/v1/media/jobs/mj_x/events",
            json={"event_type": "progress"},
        )
        self.assertEqual(response.status_code, 503)

    def test_cancel_returns_503_without_store(self) -> None:
        client = _make_client()
        response = client.post("/gateway/v1/media/jobs/mj_x/cancel", json={})
        self.assertEqual(response.status_code, 503)


class GatewayMediaJobsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "media_jobs.db"
        media_jobs.init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self, audience: str | None = None) -> TestClient:
        return _make_client(
            audience=audience, media_job_store_path=self.db
        )

    def _seed(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "intent": "image.generate",
            "workflow": "image.generate.simple.example",
            "backend": "comfyui.local",
            "request_id": overrides.pop("request_id", "req_seed"),
            "app_profile": "alpha",
            "api_key_name": "alice",
            "metadata": {"prompt_id": "abc"},
        }
        payload.update(overrides)
        record = media_jobs.create(payload, path=self.db)
        return record.to_dict("internal")

    def test_create_persists_and_returns_client_view(self) -> None:
        client = self._client()
        response = client.post(
            "/gateway/v1/media/jobs",
            json={
                "intent": "image.generate",
                "request_id": "req_create_1",
                "workflow": "image.generate.simple.example",
                "backend": "comfyui.local",
                "app_profile": "alpha",
                "api_key_name": "alice",
                "metadata": {"prompt_id": "abc"},
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        # Client audience: no backend, workflow, app_profile, api_key_name, metadata.
        self.assertIn("job_id", body)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["intent"], "image.generate")
        for forbidden in (
            "backend",
            "workflow",
            "app_profile",
            "api_key_name",
            "metadata",
        ):
            self.assertNotIn(forbidden, body, f"client leaked {forbidden!r}")

        stored = media_jobs.get(
            body["job_id"], audience="internal", path=self.db
        )
        assert stored is not None
        self.assertEqual(stored["request_id"], "req_create_1")
        self.assertEqual(stored["backend"], "comfyui.local")

    def test_create_is_idempotent_on_request_id(self) -> None:
        client = self._client()
        body = {"intent": "image.generate", "request_id": "req_same"}
        r1 = client.post("/gateway/v1/media/jobs", json=body)
        r2 = client.post("/gateway/v1/media/jobs", json=body)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r1.json()["job_id"], r2.json()["job_id"])

    def test_create_missing_intent_returns_400(self) -> None:
        client = self._client()
        response = client.post("/gateway/v1/media/jobs", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_media_job")

    def test_create_rejects_forbidden_metadata_keys(self) -> None:
        client = self._client()
        response = client.post(
            "/gateway/v1/media/jobs",
            json={
                "intent": "image.generate",
                "metadata": {"prompt": "raw forbidden prompt"},
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_media_job")

    def test_list_returns_recent_jobs_client_filtered(self) -> None:
        self._seed(request_id="req_a")
        self._seed(request_id="req_b", intent="video.generate")
        client = self._client()
        response = client.get("/gateway/v1/media/jobs")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["audience"], "client")
        self.assertEqual(len(body["jobs"]), 2)
        for row in body["jobs"]:
            self.assertNotIn("backend", row)
            self.assertNotIn("workflow", row)
            self.assertNotIn("metadata", row)
            self.assertNotIn("app_profile", row)

    def test_list_filters_by_app_profile_and_status(self) -> None:
        self._seed(request_id="req_a", app_profile="alpha")
        self._seed(request_id="req_b", app_profile="beta")
        client = self._client(audience="admin")
        response = client.get(
            "/gateway/v1/media/jobs",
            params={"app_profile": "alpha"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        ids = [row["request_id"] for row in body["jobs"]]
        self.assertEqual(ids, ["req_a"])

        response = client.get(
            "/gateway/v1/media/jobs",
            params={"status": "queued"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["jobs"]), 2)

    def test_list_rejects_invalid_status_with_400(self) -> None:
        client = self._client()
        response = client.get(
            "/gateway/v1/media/jobs", params={"status": "bogus"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["code"], "invalid_media_job_query"
        )

    def test_get_returns_filtered_job(self) -> None:
        seeded = self._seed()
        client = self._client()
        response = client.get(f"/gateway/v1/media/jobs/{seeded['job_id']}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["job_id"], seeded["job_id"])
        self.assertNotIn("metadata", body)
        self.assertNotIn("backend", body)

        admin = self._client(audience="admin")
        response = admin.get(f"/gateway/v1/media/jobs/{seeded['job_id']}")
        body = response.json()
        self.assertEqual(body["backend"], "comfyui.local")
        self.assertNotIn("metadata", body)

        internal = self._client(audience="internal")
        response = internal.get(f"/gateway/v1/media/jobs/{seeded['job_id']}")
        body = response.json()
        self.assertEqual(body["metadata"], {"prompt_id": "abc"})

    def test_get_returns_404_for_unknown_job(self) -> None:
        client = self._client()
        response = client.get("/gateway/v1/media/jobs/mj_missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "media_job_not_found"
        )

    def test_append_event_persists_and_filters_payload(self) -> None:
        seeded = self._seed()
        client = self._client()
        response = client.post(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/events",
            json={
                "event_type": "progress",
                "payload": {
                    "progress": 42,
                    "queue_position": 3,
                },
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["event_type"], "progress")
        self.assertEqual(body["job_id"], seeded["job_id"])
        self.assertNotIn("id", body)  # callers never see the household-wide event id
        self.assertGreaterEqual(body["sequence"], 1)

        # Stored internally we keep the full payload.
        rows = media_jobs.events(
            seeded["job_id"], audience="internal", path=self.db
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["queue_position"], 3)

        # Client audience strips queue_position but keeps progress.
        rows = media_jobs.events(
            seeded["job_id"], audience="client", path=self.db
        )
        self.assertEqual(rows[0]["payload"], {"progress": 42})

    def test_append_event_unknown_job_returns_404(self) -> None:
        client = self._client()
        response = client.post(
            "/gateway/v1/media/jobs/mj_missing/events",
            json={"event_type": "progress"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "media_job_not_found"
        )

    def test_list_events_returns_filtered_events(self) -> None:
        seeded = self._seed()
        media_jobs.append_event(
            seeded["job_id"],
            "queued",
            {"queue_position": 4, "status": "queued"},
            path=self.db,
        )
        media_jobs.append_event(
            seeded["job_id"],
            "progress",
            {"progress": 42, "queue_position": 3, "phase": "sampling"},
            path=self.db,
        )

        client = self._client()
        response = client.get(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/events"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["audience"], "client")
        self.assertEqual(body["job_id"], seeded["job_id"])
        self.assertEqual(
            [row["event_type"] for row in body["events"]],
            ["queued", "progress"],
        )
        self.assertEqual(body["events"][0]["payload"], {"status": "queued"})
        self.assertEqual(
            body["events"][1]["payload"],
            {"progress": 42, "phase": "sampling"},
        )

    def test_list_events_filters_after_sequence(self) -> None:
        seeded = self._seed()
        first_sequence = media_jobs.append_event(
            seeded["job_id"], "queued", {"status": "queued"}, path=self.db
        )
        media_jobs.append_event(
            seeded["job_id"], "progress", {"progress": 10}, path=self.db
        )
        client = self._client(audience="internal")

        response = client.get(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/events",
            params={"after_sequence": first_sequence},
        )

        self.assertEqual(response.status_code, 200)
        events = response.json()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "progress")
        self.assertEqual(events[0]["payload"], {"progress": 10})

    def test_list_events_unknown_job_returns_404(self) -> None:
        client = self._client()
        response = client.get("/gateway/v1/media/jobs/mj_missing/events")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "media_job_not_found"
        )

    def test_append_event_rejects_forbidden_payload(self) -> None:
        seeded = self._seed()
        client = self._client()
        response = client.post(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/events",
            json={
                "event_type": "progress",
                "payload": {"prompt": "leaked secret"},
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["code"], "invalid_media_job_event"
        )

    def test_append_event_missing_type_returns_400(self) -> None:
        seeded = self._seed()
        client = self._client()
        response = client.post(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/events",
            json={"payload": {"progress": 1}},
        )
        self.assertEqual(response.status_code, 400)

    def test_cancel_transitions_job_and_returns_filtered(self) -> None:
        seeded = self._seed()
        client = self._client()
        response = client.post(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/cancel",
            json={"reason": "user_requested"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "canceled")
        self.assertEqual(body["error"], "user_requested")
        # Client audience should not include metadata.
        self.assertNotIn("metadata", body)

    def test_cancel_terminal_job_returns_409(self) -> None:
        seeded = self._seed()
        media_jobs.update_status(
            seeded["job_id"],
            media_jobs.STATUS_RUNNING,
            path=self.db,
        )
        media_jobs.update_status(
            seeded["job_id"],
            media_jobs.STATUS_SUCCEEDED,
            path=self.db,
        )
        client = self._client()
        response = client.post(
            f"/gateway/v1/media/jobs/{seeded['job_id']}/cancel",
            json={},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"], "media_job_cancel_conflict"
        )

    def test_cancel_unknown_job_returns_404(self) -> None:
        client = self._client()
        response = client.post(
            "/gateway/v1/media/jobs/mj_missing/cancel", json={}
        )
        self.assertEqual(response.status_code, 404)

    def test_client_redaction_persists_across_listing(self) -> None:
        self._seed()
        client = self._client()
        response = client.get("/gateway/v1/media/jobs")
        raw = response.text
        for forbidden in (
            "comfyui.local",
            "image.generate.simple.example",
            "alice",
            "alpha",
            "prompt_id",
        ):
            self.assertNotIn(
                forbidden, raw, f"client leaked {forbidden!r}"
            )


class GatewayMediaJobsExplicitProviderTests(unittest.TestCase):
    def test_explicit_providers_override_store_path(self) -> None:
        captured: dict[str, Any] = {}

        def creator(payload, *, audience):
            captured["create_audience"] = audience
            return {"job_id": "stub", "intent": payload["intent"], "status": "queued"}

        def lister(*, audience, limit, app_profile=None, status=None, since_s=None):
            captured["list_audience"] = audience
            captured["limit"] = limit
            return [{"job_id": "stub", "status": "queued"}]

        def getter(job_id, *, audience):
            captured["get_audience"] = audience
            return {"job_id": job_id, "status": "queued"}

        def event_appender(job_id, event_type, payload, *, audience):
            captured["event_audience"] = audience
            captured["event_payload"] = dict(payload or {})
            return 99

        def canceler(job_id, *, audience, reason=None):
            captured["cancel_reason"] = reason
            return {"job_id": job_id, "status": "canceled"}

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "unused.db"
            media_jobs.init_db(db)
            client = _make_client(
                media_job_store_path=db,
                media_job_creator=creator,
                media_job_lister=lister,
                media_job_getter=getter,
                media_job_event_appender=event_appender,
                media_job_canceler=canceler,
            )

            r = client.post(
                "/gateway/v1/media/jobs", json={"intent": "image.generate"}
            )
            self.assertEqual(r.status_code, 201)
            self.assertEqual(r.json()["job_id"], "stub")

            r = client.get("/gateway/v1/media/jobs", params={"limit": 7})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(captured["limit"], 7)

            r = client.get("/gateway/v1/media/jobs/whatever")
            self.assertEqual(r.json()["job_id"], "whatever")

            r = client.post(
                "/gateway/v1/media/jobs/whatever/events",
                json={"event_type": "progress", "payload": {"progress": 1}},
            )
            self.assertEqual(r.status_code, 201)
            self.assertNotIn("id", r.json())  # the provider's global id stays internal
            self.assertEqual(r.json()["event_type"], "progress")
            self.assertEqual(captured["event_payload"], {"progress": 1})

            r = client.post(
                "/gateway/v1/media/jobs/whatever/cancel",
                json={"reason": "user"},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(captured["cancel_reason"], "user")

    def test_creator_error_is_protocol_shaped(self) -> None:
        def creator(_payload, *, audience):
            raise RuntimeError("creator boom")

        client = _make_client(media_job_creator=creator)
        response = client.post(
            "/gateway/v1/media/jobs", json={"intent": "image.generate"}
        )
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error"]["code"], "gateway_internal_error")
        self.assertNotIn("creator boom", response.text)

    def test_submitter_can_handle_executable_media_job_before_creator(self) -> None:
        captured: dict[str, Any] = {}

        def submitter(*, request, payload, audience):
            captured["payload"] = dict(payload)
            captured["audience"] = audience
            return JSONResponse(
                status_code=202,
                content={"job_id": "mj_exec", "status": "queued"},
            )

        def creator(_payload, *, audience):
            raise AssertionError("generic creator should not run")

        client = _make_client(
            media_job_creator=creator,
            media_job_submitter=submitter,
        )
        response = client.post(
            "/gateway/v1/media/jobs",
            json={
                "intent": "image.generate",
                "request": {"model": "flux2-klein-4b", "prompt": "private"},
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "mj_exec")
        self.assertEqual(captured["audience"], "client")
        self.assertEqual(captured["payload"]["intent"], "image.generate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
