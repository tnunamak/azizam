"""S2 acceptance matrix, item 2: privacy (docs/design/CODE_TO_DATA_S2.md).

Canary values are synthetic words that no manifest, fixture or definition contains. Each test says which bullet it
closes. A test looks at every durable place it can reach (the envelope tables, the job row, its events, the receipts,
the spool files) and checks first that the place holds something, so an empty list cannot pass for "no canary".
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import proxy
from gateway import accounts, admin_store, media_jobs, prompt_memory, receipt_store
from gateway.definition_store import DefinitionStore
from gateway.job_envelope import JobEnvelope, build_job_envelope
from gateway.workflow_call import build_request
from tests.support.access import auth_identity, store_plan
from tests.support.app_keys import accountable_person
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401

ORIGIN = "http://engine.test:1"


def make_envelope() -> JobEnvelope:
    return build_job_envelope(
        source_kind="workflow",
        source_id="wf-privacy",
        contract={"inputs": {"value": {"type": "string"}}},
        binding={"map": {"value": [{"node": "1", "input": "value"}]}},
        template=b"graph-bytes-privacy",
        protocol_id="proto-x",
        adapter_id="adapter-x",
        interface_version=1,
        service_id="svc-1",
        origin=ORIGIN,
    )


def dump(path: Path, table: str) -> str:
    """Every column of every row of one table, as text."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - test table names
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        rows = []  # the table is created by its first writer
    finally:
        conn.close()
    return "\n".join(c.decode("utf-8", "replace") if isinstance(c, bytes) else str(c) for row in rows for c in row)


def blob_bytes(path: Path) -> dict[str, bytes]:
    conn = sqlite3.connect(path)
    try:
        return {sha: bytes(body) for sha, body in conn.execute("SELECT sha, body FROM definition_blob")}
    finally:
        conn.close()


def envelope_text(path: Path) -> str:
    return "\n".join(dump(path, t) for t in ("job_envelope", "definition_ref", "definition_blob"))


# --- 2.1: canaries in neutral inputs ---------------------------------------------------------------------------------

NEUTRAL = {
    "string": "kestrel-string-71d3a0",
    "integer": 640519283,
    "number": 4417.90625,
    "nested": "kestrel-nested-0be94c",
    "filename": "kestrel-file-52ac19.wav",
    "url": "https://kestrel-host-8e2d41.example/p?token=kestrel-query-c07a33",
}


def neutral_input(flag: bool, choice: str) -> dict[str, Any]:
    return {
        "value": NEUTRAL["string"],
        "choice": choice,
        "count": NEUTRAL["integer"],
        "ratio": NEUTRAL["number"],
        "flag": flag,
        "options": {"inner": [{"deep": NEUTRAL["nested"]}]},
        "filename": NEUTRAL["filename"],
        "link": NEUTRAL["url"],
    }


def test_neutral_input_values_do_not_change_what_the_envelope_pins(tmp_path: Path):
    """2.1: the string, enum, integer, number, boolean, nested, filename and URL values reach no envelope row.

    Searching for a boolean's value or an enum's literal proves nothing (``True`` and the enum choice may appear in a
    definition legitimately), so each job's whole stored envelope and every blob is compared with the baseline the
    store would hold had the caller sent nothing. A value copied under any key changes that comparison.
    """
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    built = make_envelope()
    baseline = media_jobs.create(media_jobs.MediaJob(intent="image.generate", request_id="req-baseline"),
                                 input_data={"value": "x"}, envelope=built, path=db)
    store = DefinitionStore(db)
    baseline_json = store.envelope(baseline.job_id)
    assert baseline_json == built.envelope
    blobs_before = blob_bytes(db)

    jobs = []
    for index, (flag, choice) in enumerate([(False, "alpha"), (True, "beta")]):
        jobs.append(media_jobs.create(
            media_jobs.MediaJob(intent="image.generate", request_id=f"req-neutral-{index}"),
            input_data=neutral_input(flag, choice), envelope=built, path=db))
    assert len({job.job_id for job in jobs}) == 2
    for job in jobs:
        assert store.envelope(job.job_id) == built.envelope, "the pinned envelope is the one that was built"
    assert blob_bytes(db) == blobs_before and len(blobs_before) == 3, "the same three blobs; no job added one"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM job_envelope").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM definition_ref").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(DISTINCT envelope_json) FROM job_envelope").fetchone()[0] == 1
    finally:
        conn.close()
    text = envelope_text(db)
    for kind in ("string", "integer", "number", "nested", "filename", "url"):
        assert str(NEUTRAL[kind]) not in text, f"the {kind} value reached the envelope tables"


# --- 2.1 and 2.2: one real job, every durable carrier, both views ----------------------------------------------------

CANARY_PROMPT = "kestrel-prompt-9f41b2 a door creaks"
CANARY_SEED = 735018264
CANARY_DURATION = 87.40625  # inside the workflow's 1..120 range
PROVENANCE = {"source": "hand-written",
              "reference": "ComfyUI server routes POST /prompt, GET /history/{prompt_id}, GET /view (ComfyUI server.py)"}


def _tone_flac() -> bytes:
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tone.flac"
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                               "-i", "sine=d=0.3:r=22050", str(path)], capture_output=True)
        if proc.returncode != 0:
            raise unittest.SkipTest(f"ffmpeg cannot write flac: {proc.stderr[-200:]!r}")
        return path.read_bytes()


def _audio_scenario() -> Any:
    return scenario_from_data({
        "protocol": "comfyui", "name": "audio-job-succeeds", "provenance": PROVENANCE,
        "exchanges": [
            {"id": "prompt", "match": {"method": "POST", "path": "/prompt"},
             "response": {"json": {"prompt_id": "p1"}}},
            {"id": "history", "match": {"method": "GET", "path": "/history/p1"},
             "response": {"json": {"p1": {"status": {"status_str": "success"},
                                          "outputs": {"8": {"audio": [{"filename": "sfx_00001_.flac",
                                                                       "type": "output"}]}}}}}},
            {"id": "view", "match": {"method": "GET", "path": "/view"},
             "response": {"headers": {"content-type": "audio/flac"}, "body": [{"hex": _tone_flac().hex()}]}},
        ],
    })


class _Request:
    method = "POST"
    query_params: dict[str, str] = {}

    def __init__(self, auth_context: proxy.AuthContext) -> None:
        self.scope = {"auth_context": auth_context}
        self.headers = {"host": "gateway.test"}
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")
        self.client = SimpleNamespace(host="127.0.0.1", port=40000)

    async def json(self) -> dict[str, Any]:
        return {}


class SubmittedCanaryCarrierTests(unittest.IsolatedAsyncioTestCase):
    """An audio job from the MCP request to its settled result, with the real worker and a fake engine."""

    @pytest.fixture(autouse=True)
    def _inject(self, fake_upstreams, isolated_receipts):
        self.fakes = fake_upstreams
        self.receipts = isolated_receipts

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.admin = root / "admin.db"
        self.jobs = root / "media_jobs.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        accounts.initialize(self.admin)
        owner = admin_store.upsert_oidc_user("https://idp.test", "ann", email="ann@example.test", path=self.admin)
        plan = store_plan("Everything", path=self.admin)
        accounts.change_person(owner["id"], "test", path=self.admin, plan_id=plan["id"])
        self.local = admin_store.create_app("This computer", kind="service", owner_user_id=owner["id"],
                                            access_plan=plan["id"], path=self.admin)
        admin_store.add_trusted_address(self.local["id"], "127.0.0.1", max_entries=10, path=self.admin,
                                        created_by=accountable_person(self.admin, self.local["id"]))
        self.scheduled: list[str] = []
        for target, name, value in (
            (proxy.ADMIN_STORE, "path", self.admin),
            (media_jobs, "DB_PATH", self.jobs),
            (proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            (proxy, "IMAGE_CACHE_DIR", root / "cache"),
            (proxy, "COMFY_ENABLED", True),
            (proxy, "_image_url_signing_secret", b"test-secret"),
            (proxy, "_schedule_audio_media_job", lambda job_id: self.scheduled.append(job_id) or True),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        overrides = patch.object(proxy.ADMIN_STORE, "operator_overrides",
                                 return_value={"service_availability": {}, "media_job_admission": {}})
        overrides.start()
        self.addCleanup(overrides.stop)

    async def test_the_submitted_values_reach_the_engine_and_no_durable_carrier(self) -> None:
        """2.1 and 2.2: the worker gets the inputs; the envelope, job, events, receipts and views never hold them."""
        identity = proxy.runtime_identity_for_trusted_address(ipaddress.ip_address("127.0.0.1"), path=self.admin)
        assert identity is not None and identity.denial is None
        auth = proxy.AuthContext(identity=identity)
        entry = next(e for e in proxy._mcp_catalog() if "audio.generate.sa3-small-sfx" in (e["id"], e["short_id"]))
        route = entry["execution"]["async"]
        submit = {**route["submit"], "fields": route["fields"], "content_type": route["submit"].get("content_type", "")}
        built = await build_request(
            lambda *_: None, None, auth, entry, submit,
            {"prompt": CANARY_PROMPT, "seed": CANARY_SEED, "duration": CANARY_DURATION},
            wrap_intent=route["submit"]["body"]["intent"])
        fake = self.fakes.asgi(_audio_scenario())
        with patch.object(proxy, "COMFY_HOST", fake.base_url), patch.object(proxy.asyncio, "sleep", AsyncMock()):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_Request(auth), payload=built.json, audience="client")  # type: ignore[arg-type]
            assert response is not None and response.status_code == 202, getattr(response, "body", b"")
            job_id = json.loads(response.body)["job_id"]
            assert self.scheduled == [job_id], "the job was handed to the scheduler once"
            await asyncio.wait_for(proxy._run_audio_media_job(job_id), 20)

        # The worker was given the original input: privacy is not reached by dropping it before execution.
        sent = [r for r in fake.requests if r.method == "POST" and r.path == "/prompt"]
        assert len(sent) == 1, "one generation call"
        body = sent[0].body.decode("utf-8", "replace")
        assert CANARY_PROMPT in body and str(CANARY_SEED) in body and str(CANARY_DURATION) in body
        settled = media_jobs.get(job_id, audience="internal", path=self.jobs)
        assert settled is not None and settled["status"] == media_jobs.STATUS_SUCCEEDED, settled

        carriers = {
            "envelope tables": envelope_text(self.jobs),
            "job row": dump(self.jobs, "media_jobs"),
            "job events": dump(self.jobs, "media_job_events"),
            "receipts": dump(self.receipts, "receipts"),
        }
        for audience in ("client", "admin"):
            carriers[f"{audience} job view"] = json.dumps(
                media_jobs.get(job_id, audience=audience, path=self.jobs), sort_keys=True, default=str)
            carriers[f"{audience} event view"] = json.dumps(
                media_jobs.events(job_id, audience=audience, path=self.jobs), sort_keys=True, default=str)
            request_ids = [r[0] for r in sqlite3.connect(self.receipts).execute("SELECT request_id FROM receipts")]
            carriers[f"{audience} receipt view"] = json.dumps(
                [receipt_store.get(rid, audience=audience, path=self.receipts) for rid in request_ids],
                sort_keys=True, default=str)
        for name in ("envelope tables", "job row", "job events", "client job view", "admin job view",
                     "client event view", "admin event view"):
            assert carriers[name].strip() not in ("", "null", "[]"), f"{name} holds nothing, so it proves nothing"
        assert job_id in carriers["envelope tables"], "the envelope is the job's own"
        assert carriers["receipts"].strip(), "the run left a receipt to check"
        canaries = [CANARY_PROMPT, "kestrel-prompt-9f41b2", str(CANARY_SEED), str(CANARY_DURATION)]
        # The audio route keeps the seed and the duration in the internal job row on purpose (``_safe_request_metadata``
        # in gateway/audio_media_jobs.py: they reproduce a result and feed Library provenance). That is the only place
        # either may be, and only under those two metadata keys; every other carrier and both views must lack them.
        row = json.loads(sqlite3.connect(self.jobs).execute(
            "SELECT job_json FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()[0])
        assert row["metadata"].get("seed") == CANARY_SEED and row["metadata"].get("duration") == CANARY_DURATION
        row["metadata"].pop("seed"), row["metadata"].pop("duration")
        carriers["job row"] = json.dumps(row, sort_keys=True)
        for name, text in carriers.items():
            for canary in canaries:
                assert canary not in text, f"{canary!r} reached the {name}"


# --- 2.3: a stale worker rewrite after erasure ----------------------------------------------------------------------

OWNER = "app-A"
OTHER_OWNER = "app-B"
FINGERPRINT = "kestrel-fingerprint-3d9a71"
SEEDED = "imgjob_seeded_kestrel-3d9a71"
SEEDED_PARENT = "imgjob_seeded_kestrel-parent-3d9a71"
INPUT_CANARY = "kestrel-input-aa05c8"


@pytest.fixture
def jobs_db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


def _stored(path: Path, job_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT job_json, request_id, parent_request_id FROM media_jobs WHERE job_id = ?",
                           (job_id,)).fetchone()
    finally:
        conn.close()
    data = json.loads(row[0])
    data["_column_request_id"], data["_column_parent_request_id"] = row[1], row[2]
    return data


def test_a_stale_worker_rewrite_cannot_restore_what_erasure_removed(jobs_db: Path, monkeypatch: pytest.MonkeyPatch):
    """2.3: a worker holds a pre-erasure copy and writes it after erasure committed; no fingerprint comes back."""
    owned = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", request_id=SEEDED, parent_request_id=SEEDED_PARENT,
                            app_id=OWNER, created_at=100, metadata={"request_hash": FINGERPRINT}),
        input_data={"prompt": INPUT_CANARY}, envelope=make_envelope(), path=jobs_db)
    control = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", request_id="imgjob_seeded_control", app_id=OTHER_OWNER,
                            created_at=100, metadata={"request_hash": "control-fingerprint"}),
        input_data={"prompt": "control"}, path=jobs_db)
    stale = copy.deepcopy(owned)
    assert stale.input_hash and stale.metadata.get("request_hash") == FINGERPRINT

    # The worker is paused immediately before it writes; erasure commits while it waits; then it resumes.
    real_write = media_jobs._write_job
    state = {"paused": False}

    def erase_then_write(conn: sqlite3.Connection, job: media_jobs.MediaJob, **kw: Any) -> None:
        if job is stale and not state["paused"]:
            state["paused"] = True
            erased = media_jobs.erase_prompt_derivatives({f"app:{OWNER}"}, before=100, path=jobs_db)
            assert erased == 1, "erasure committed while the stale worker was paused"
        real_write(conn, job, **kw)

    monkeypatch.setattr(media_jobs, "_write_job", erase_then_write)
    media_jobs.upsert(stale, path=jobs_db, maker_from_request=False)
    assert state["paused"], "the stale write reached the pause point"

    after = _stored(jobs_db, owned.job_id)
    assert after["input_hash"] is None
    assert "request_hash" not in after["metadata"]
    assert after["request_id"] == "" and after["_column_request_id"] is None
    assert after["parent_request_id"] is None and after["_column_parent_request_id"] is None
    assert media_jobs.get_by_request_id(SEEDED, path=jobs_db) is None, "the erased key finds no job"
    conn = sqlite3.connect(jobs_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM media_jobs WHERE job_id = ?", (owned.job_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0] == 2
    finally:
        conn.close()
    for audience in ("client", "admin"):
        view = json.dumps(media_jobs.get(owned.job_id, audience=audience, path=jobs_db), sort_keys=True)
        events = json.dumps(media_jobs.events(owned.job_id, audience=audience, path=jobs_db), sort_keys=True)
        for text in (view, events):
            assert FINGERPRINT not in text and SEEDED not in text and INPUT_CANARY not in text
    row = dump(jobs_db, "media_jobs")
    assert FINGERPRINT not in row and SEEDED_PARENT not in row
    kept = _stored(jobs_db, control.job_id)
    assert kept["input_hash"] and kept["metadata"]["request_hash"] == "control-fingerprint", "another owner is untouched"
    assert kept["request_id"] == "imgjob_seeded_control"


# --- 2.3: forget, member-off, the kill switch and person erasure ----------------------------------------------------

PRIVATE = "kestrel-private-body-5be207"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    admin = tmp_path / "data/admin.db"
    jobs = tmp_path / "data/media_jobs.db"
    responses = tmp_path / "data/responses.db"
    admin_store.init_db(admin)
    media_jobs.init_db(jobs)
    proxy.responses_store.init_db(responses)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {})
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {})
    people = []
    for name in ("maker", "bystander"):
        user = admin_store.upsert_oidc_user("issuer", name, email=f"{name}@example.test", path=admin)
        app = admin_store.create_app(f"App {name}", kind="personal", owner_user_id=user["id"], access_plan="person",
                                     path=admin)
        admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=f"sha256:{name}",
                                           prefix="test", app_id=app["id"], created_by=user["id"], path=admin)
        auth = proxy.AuthContext(identity=auth_identity(
            name=name, key_hash=f"sha256:{name}", app_id=app["id"], person_id=user["id"]))
        people.append((user, app, auth))
    return tmp_path, admin, jobs, people


def _request():
    return SimpleNamespace(headers={"host": "testserver"}, url=SimpleNamespace(scheme="http", netloc="testserver"))


def _seed_person(jobs: Path, admin: Path, user: dict, app: dict, auth: Any, *, tag: str, spool: bool) -> dict[str, str]:
    """A job with its fingerprint and envelope, a saved prompt, and (if ``spool``) a durable execution spool."""
    job = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", workflow="test", request_id=f"imgjob_seeded_{tag}",
                            app_id=app["id"], created_at=int(time.time()) - 100, status=media_jobs.STATUS_RUNNING,
                            metadata={"request_hash": f"{tag}-fingerprint"}),
        input_data={"prompt": f"{tag}-{PRIVATE}"}, envelope=make_envelope(), path=jobs, maker_from_request=False)
    prompt_memory.put(f"asset_{tag}", user["id"], f"{tag}-{PRIVATE}", kept_until=None, path=jobs, admin_path=admin,
                      created_at=int(time.time()) - 100)
    if spool:
        proxy._persist_image_media_job_payload(job.job_id, body={"prompt": f"{tag}-{PRIVATE}"}, request=_request(),
                                               auth_context=auth)
        assert proxy._image_media_job_payload_path(job.job_id).exists(), "the normal spool is on disk"
    return {"job_id": job.job_id, "asset": f"asset_{tag}"}


def _durable_text(root: Path, jobs: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix not in {".db", ".lock", ".db-wal", ".db-shm"} and "media_jobs" not in path.name:
            files[str(path.relative_to(root))] = path.read_bytes().decode("utf-8", "replace")
    return {
        "spool files": "\n".join(files.values()),
        "job rows": dump(jobs, "media_jobs"),
        "job events": dump(jobs, "media_job_events"),
        "envelope tables": envelope_text(jobs),
        "saved prompts": dump(jobs, "asset_prompts"),
    }


def _turn_off(modes: str, admin: Path, user: dict) -> None:
    if modes == "member_off":
        prompt_memory.put_member(user["id"], remember=False, retention="never", path=admin)
        proxy._maintain_prompt_memory()
    elif modes == "kill":
        assert proxy._purge_prompt_memory_on_kill()["pending"] is True
        proxy._drain_prompt_purges()
    elif modes == "person_erasure":
        assert proxy._purge_person_prompt_memory(user["id"])["pending"] is True
        proxy._drain_prompt_purges()
    else:
        raise AssertionError(modes)


@pytest.mark.parametrize("mode", ["member_off", "kill", "person_erasure"])
def test_a_privacy_mode_leaves_no_durable_copy_of_the_body(stores, mode: str):
    """2.3: member-off, the kill switch and person erasure clear spool, prompt, fingerprint and job carriers."""
    root, admin, jobs, people = stores
    (user, app, auth), (other_user, other_app, other_auth) = people
    mine = _seed_person(jobs, admin, user, app, auth, tag="mine", spool=True)
    theirs = _seed_person(jobs, admin, other_user, other_app, other_auth, tag="theirs", spool=True)
    before = _durable_text(root, jobs)
    assert all(text.strip() for name, text in before.items() if name != "job events"), "everything starts populated"
    assert f"mine-{PRIVATE}" in before["spool files"] and "mine-fingerprint" in before["job rows"]

    _turn_off(mode, admin, user)

    after = _durable_text(root, jobs)
    for name, text in after.items():
        assert f"mine-{PRIVATE}" not in text, f"the body survived {mode} in the {name}"
        assert "mine-fingerprint" not in text, f"the fingerprint survived {mode} in the {name}"
    assert not proxy._image_media_job_payload_path(mine["job_id"]).exists()
    assert prompt_memory.get(mine["asset"], user["id"], path=jobs) is None
    assert media_jobs.get(mine["job_id"], path=jobs) is not None, "the job itself is kept"
    assert DefinitionStore(jobs).envelope(mine["job_id"]) is not None, "its pins are kept"
    # A write by a worker that started before the cutoff cannot put the prompt back.
    assert prompt_memory.put(mine["asset"], user["id"], f"mine-{PRIVATE}", kept_until=None, path=jobs,
                             admin_path=admin, created_at=int(time.time()) - 100) is False
    assert prompt_memory.get(mine["asset"], user["id"], path=jobs) is None

    if mode == "kill":
        return
    # The normal-memory control: another person's spool, prompt and fingerprint are untouched.
    proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS.clear()
    assert proxy._load_image_media_job_payload(theirs["job_id"])["body"] == {"prompt": f"theirs-{PRIVATE}"}
    assert prompt_memory.get(theirs["asset"], other_user["id"], path=jobs)["prompt_text"] == f"theirs-{PRIVATE}"
    assert "theirs-fingerprint" in _durable_text(root, jobs)["job rows"]


def test_forget_keeps_the_body_in_memory_only_and_it_dies_with_the_process(stores):
    """2.3: an explicit forget never reaches disk; the live worker still has the body; a restart does not."""
    root, admin, jobs, people = stores
    (user, app, auth), _other = people
    job = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", workflow="test", app_id=app["id"],
                            status=media_jobs.STATUS_RUNNING),
        input_data={"prompt": "x"}, envelope=make_envelope(), path=jobs, maker_from_request=False)
    forget = {"prompt": f"forget-{PRIVATE}", "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}
    proxy._persist_image_media_job_payload(job.job_id, body=forget, request=_request(), auth_context=auth)

    assert not proxy._image_media_job_payload_path(job.job_id).exists()
    assert proxy._load_image_media_job_payload(job.job_id)["body"]["prompt"] == f"forget-{PRIVATE}", \
        "the worker that is running now still has its input"
    durable = _durable_text(root, jobs)
    for name, text in durable.items():
        assert f"forget-{PRIVATE}" not in text, f"the forgotten body is in the {name}"
    assert "job rows" in durable and durable["job rows"].strip() and durable["envelope tables"].strip()

    proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS.clear()  # the process restarts
    assert proxy._load_image_media_job_payload(job.job_id) is None, "nothing durable is left to resume from"
