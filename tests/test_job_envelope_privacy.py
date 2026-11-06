"""The envelope holds definitions, never caller content (S2 design, D1; S2.1b canary test).

Canary values go into every kind of input: a neutral string, an enum, an integer, a number, a boolean, a nested value,
a filename and a URL. None of them may appear in any envelope row or definition blob. Two carriers are checked: the
whole job-creation call with a hostile request (every kind of input at once), and the real audio submit route.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

import pytest

import proxy
from gateway import media_jobs
from tests.support.access import access_context, authenticated

CANARIES = {
    "string": "canary-string-6d41a9",
    "enum": "canary-enum-b83f10",
    "integer": 918273645,
    "number": 7341.8125,
    "boolean-key": "canary_flag_9c2e17",
    "nested-key": "canary_nested_5a07d2",
    "nested-value": "canary-nested-value-e19b64",
    "filename": "canary-file-4c7a82.wav",
    "url": "https://canary-host-3f9d60.example/path?token=canary-query-a2c581",
}


def hostile_request() -> dict[str, Any]:
    return {
        "prompt": CANARIES["string"],
        "response_format": CANARIES["enum"],
        "seed": CANARIES["integer"],
        "guidance": CANARIES["number"],
        CANARIES["boolean-key"]: True,
        "options": {CANARIES["nested-key"]: [{"deep": CANARIES["nested-value"]}]},
        "file_name": CANARIES["filename"],
        "reference_url": CANARIES["url"],
    }


def carrier_text(db: Path) -> str:
    """Everything the envelope tables hold, as one string: the envelope JSON, the blob bytes, the reference rows."""
    parts: list[str] = []
    with sqlite3.connect(db) as conn:
        for (body,) in conn.execute("SELECT envelope_json FROM job_envelope"):
            parts.append(body)
        for sha, body, created in conn.execute("SELECT sha, body, created FROM definition_blob"):
            parts += [sha, bytes(body).decode("utf-8", "replace"), str(created)]
        for row in conn.execute("SELECT * FROM definition_ref"):
            parts += [str(c) for c in row]
    return "\n".join(parts)


def assert_no_canary(db: Path) -> str:
    text = carrier_text(db)
    assert text, "no envelope was recorded, so there was nothing to check"
    for kind, canary in CANARIES.items():
        assert str(canary) not in text, f"the {kind} canary reached the envelope tables"
    return text


def test_a_hostile_request_reaches_no_envelope_table(tmp_path: Path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    manifest = proxy.GATEWAY_WORKFLOW_REGISTRY.get("audio.generate.tangoflux").manifest
    built = proxy._manifest_job_envelope(manifest, proxy._AUDIO_GRAPH_CAPABILITY)
    body = hostile_request()
    job = media_jobs.MediaJob(
        intent="audio.generate", request_id="canary-request", metadata={"mode": "x"},
    )
    created = media_jobs.create(job, input_data=body, envelope=built, path=db)
    text = assert_no_canary(db)
    assert created.job_id in text, "the envelope row is keyed by the job"


class _StubJsonRequest:
    method = "POST"
    scope: dict[str, Any] = {"auth_context": access_context()}
    query_params: dict[str, str] = {}

    def __init__(self) -> None:
        self.headers = {"host": "gateway.test"}
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")

    async def json(self) -> dict[str, Any]:
        return {}


@pytest.mark.usefixtures("isolated_receipts")
class AudioRouteCanaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "media_jobs.db"
        media_jobs.init_db(self.db)
        for patcher in (
            patch.object(media_jobs, "DB_PATH", self.db),
            patch.object(proxy, "IMAGE_CACHE_DIR", root / "images"),
            patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            patch.object(proxy, "_schedule_audio_media_job", lambda job_id: True),
            authenticated(),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_the_audio_submit_pins_definitions_and_no_caller_content(self) -> None:
        body: Mapping[str, Any] = {
            "model": "tangoflux",
            "prompt": CANARIES["string"],
            "duration": 17.3571,
            "seed": CANARIES["integer"],
            "guidance": CANARIES["number"],
            "batch_size": 2,
        }
        response = await proxy._dispatch_gateway_media_job_submission(
            request=_StubJsonRequest(),  # type: ignore[arg-type]
            payload={"intent": "music.generate", "parameters": dict(body)},
            audience="client",
        )
        assert response is not None and response.status_code == 202, getattr(response, "body", b"")
        job_id = json.loads(response.body.decode("utf-8"))["job_id"]
        text = assert_no_canary(self.db)
        assert job_id in text
        assert "17.3571" not in text
        # The envelope is the real one: it names the workflow the route resolved and pins its graph.
        with sqlite3.connect(self.db) as conn:
            (envelope_json,) = conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id = ?", (job_id,)).fetchone()
            assert conn.execute("SELECT COUNT(*) FROM definition_ref WHERE job_id = ?", (job_id,)).fetchone()[0] == 3
        data = json.loads(envelope_json)
        assert data["source"]["id"] == "audio.generate.tangoflux"
        assert data["generated"] == {}


@pytest.mark.parametrize("kind", sorted(CANARIES))
def test_each_canary_is_distinct_and_absent_from_the_manifests(kind: str):
    """A canary the shipped definitions already contain would make the absence check meaningless."""
    root = Path(__file__).resolve().parent.parent
    haystack = "".join(p.read_text(errors="replace") for p in (root / "manifests").rglob("*.yml"))
    assert str(CANARIES[kind]) not in haystack
