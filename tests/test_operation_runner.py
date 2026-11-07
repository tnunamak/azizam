"""One outer runner (``jobs.run`` over the adapter interface) drives three adapters through the same tests.

The three adapters are the node-graph adapter (against an in-process fake graph engine that answers /prompt, /history
and /view), the http-v1 executor (against an in-process fake async job engine described by a protocol file under
tests/fixtures), and a scripted streaming engine (a test-only ``EngineAdapter`` with a push channel). Jobs are real
``media_jobs`` rows in a temporary database, and ``jobs.run`` writes their durable state there.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import io
import json
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway import media_jobs
from gateway.engine_protocol import description, jobs
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION, AdapterRegistry
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import ProtocolError, TransportFailure, UnsupportedByAdapter
from gateway.engine_protocol.node_graph import NodeGraphAdapter
from gateway.engine_protocol.transport import HttpxTransport
from gateway.engine_protocol.types import CanonicalRequest, Connection, JobEvent, JobRef, JobState, JobStatus, Timeouts
from gateway.manifests import load_workflow_manifest
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "engine_protocol" / "d0"
OP = "audio.generate"
BODY = {"prompt": "a door creaks", "duration": 4, "seed": 1234}
GRAPH_HOST = "graph.test:7890"
JOBS_HOST = "jobs.test:8080"
TIMEOUTS = Timeouts(connect_s=10, first_byte_s=30, total_s=30)


def _conn(host: str) -> Connection:
    return Connection(address=f"http://{host}", timeouts=TIMEOUTS)


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x01\x00\x02\x00" * 400)
    return out.getvalue()


WAV = _wav()
WAV_OUTCOME = {"assets": [{"content_type": "audio/wav", "size": len(WAV), "sha256": hashlib.sha256(WAV).hexdigest()}]}


# --- the runner's runtime: what jobs.run asks of any runtime --------------------------------------------------------


@dataclass
class Runtime:
    adapter: Any
    conn: Connection
    params: dict[str, Any]
    timing: tuple[float, float]

    def connection_for(self, _op: str) -> Connection:
        return self.conn

    def poll_timing(self, _op: str) -> tuple[float, float]:
        return self.timing

    def request(self, op: str, body: dict[str, Any]) -> CanonicalRequest:
        return CanonicalRequest(op=op, body=copy.deepcopy(dict(body)), params=dict(self.params))


@dataclass
class Harness:
    name: str
    runtime: Runtime
    store: MemoryAssetStore
    facts: dict[str, Any]
    follows_events: bool


def _new_job(path: Path) -> str:
    job = media_jobs.create(media_jobs.MediaJob(intent=OP, request_id=f"req_{uuid.uuid4().hex}"),
                            input_data=BODY, path=path, maker_from_request=False)
    return job.job_id


@pytest.fixture
def jobs_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, "DB_PATH", path)
    return path


def _counters() -> dict[str, Any]:
    return {"submits": 0, "posts": [], "lose_submit": False, "cancels": 0, "fetches": 0, "refuse": 0}


# --- engine 1: the node-graph adapter against an in-process graph engine --------------------------------------


def _rename_graph(template: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """The template with node ids renamed: keys and links."""
    def link(value: Any) -> Any:
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in names:
            return [names[value[0]], value[1]]
        return value

    out: dict[str, Any] = {}
    for key, node in template.items():
        if key in names:
            renamed = copy.deepcopy(node)
            renamed["inputs"] = {k: link(v) for k, v in renamed.get("inputs", {}).items()}
            out[names[key]] = renamed
        else:
            out[key] = node
    return out


GRAPH_NAMES = {"1": "n-loader", "2": "n-sampler", "3": "n-save"}


# Where the TangoFlux graph takes each bound input, written out by hand: the oracle does not read the manifest map that
# the compiler reads, so a wrong map is caught.
TANGOFLUX_TARGETS = (("2", "prompt", "prompt"), ("2", "duration", "duration"), ("2", "seed", "seed"))


def _bound_values(names: dict[str, str]) -> list[tuple[str, str, Any]]:
    """Every input the request binds, where the graph takes it. The fake engine checks each one."""
    return [(names.get(node, node), key, BODY[field]) for node, key, field in TANGOFLUX_TARGETS]


def _graph_harness(fake_upstreams, tmp_path, script: list[str], *, host: str = GRAPH_HOST, rename: bool = False,
                   lose_submit: bool = False) -> Harness:
    template_path = ROOT / "workflows" / "audio" / "tangoflux.json"
    manifest = load_workflow_manifest(ROOT / "manifests" / "workflows" / "tangoflux.yml")
    doc = description.load_registry(description.REGISTRY_DIR)["node-graph"]
    root = ROOT
    names = GRAPH_NAMES
    if rename:
        root = tmp_path / "renamed-root"
        (root / "workflows" / "audio").mkdir(parents=True)
        template = json.loads(template_path.read_text(encoding="utf-8"))
        (root / "workflows" / "audio" / "tangoflux.json").write_text(json.dumps(_rename_graph(template, names)))
        for node in manifest["bindings"]["comfyui"]["map"].values():
            for entry in node:
                entry["node"] = names.get(entry["node"], entry["node"])
        for selector in manifest["bindings"]["comfyui"]["selectors"].values():
            selector["node"] = names.get(selector["node"], selector["node"])
        manifest["bindings"]["engine-b"] = manifest["bindings"].pop("comfyui")
        doc = dataclasses.replace(doc, binding="engine-b", label="Engine B")
    facts = _counters()
    steps = list(script)
    save_node = names["3"] if rename else "3"
    # The fake engine accepts a prompt only when every bound input holds its request value at the node the template names.
    bound = _bound_values(names if rename else {})

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            facts["submits"] += 1
            facts["posts"].append(json.loads(request.content))
            if lose_submit or facts["lose_submit"]:
                raise httpx.ReadError("the reply to the submission was lost", request=request)
            if facts["refuse"]:
                return httpx.Response(facts["refuse"], json={"error": "the engine refused the prompt"})
            graph = facts["posts"][-1]["prompt"]
            if not bound or any(graph.get(node, {}).get("inputs", {}).get(key) != value for node, key, value in bound):
                return httpx.Response(400, json={"error": "a bound input is missing or wrong"})
            return httpx.Response(200, json={"prompt_id": "job-1"})
        if request.method == "GET" and path == "/history/job-1":
            state = steps.pop(0) if len(steps) > 1 else steps[0]
            if state == "pending":
                return httpx.Response(200, json={})
            if state == "running":
                return httpx.Response(200, json={"job-1": {"status": {"completed": False}, "outputs": {}}})
            if state == "done":
                entry = {"status": {"completed": True, "status_str": "success", "messages": []},
                         "outputs": {save_node: {"audio": [{"filename": "tone.wav", "subfolder": "", "type": "output"}]}}}
                return httpx.Response(200, json={"job-1": entry})
            return httpx.Response(200, json={"job-1": {"status": {"status_str": "error", "messages": [
                ["execution_error", {"node_id": save_node}]]}, "outputs": {}}})
        if request.method == "GET" and path == "/view":
            facts["fetches"] += 1
            return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})
        return httpx.Response(404)

    fake_upstreams.router.add_transport(host, HandlerTransport(answer))
    store = MemoryAssetStore()
    adapter = NodeGraphAdapter(doc, assets=store, workflow_root=root, client_id="d0-test-client")
    params = {"manifest": manifest}
    runtime = Runtime(adapter, _conn(host), params, (0.5, 5.0))
    return Harness("graph", runtime, store, facts, follows_events=False)


# --- engine 2: the http-v1 executor against an in-process async job engine ------------------------------------


def _jobs_harness(fake_upstreams, script: list[str], *, host: str = JOBS_HOST, rename: bool = False,
                  lose_submit: bool = False, timing: tuple[float, float] = (0.5, 5.0)) -> Harness:
    doc = description.parse_description_text((FIXTURES / "async-audio.yml").read_text(encoding="utf-8"),
                                             source="async-audio.yml")
    if rename:
        doc = dataclasses.replace(doc, id="renamed-async-engine")
    facts = _counters()
    steps = list(script)
    states = {"pending": "queued", "running": "rendering", "done": "done", "broken": "broken"}

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/jobs":
            facts["submits"] += 1
            facts["posts"].append(json.loads(request.content))
            if lose_submit or facts["lose_submit"]:
                raise httpx.ReadError("the reply to the submission was lost", request=request)
            return httpx.Response(200, json={"ticket": {"id": "job-1"}})
        if request.method == "GET" and path == "/v1/jobs/job-1":
            state = steps.pop(0) if len(steps) > 1 else steps[0]
            body: dict[str, Any] = {"state": states[state]}
            if state == "done":
                body["outputs"] = [{"key": "out-1"}]
            if state == "broken":
                body["problem"] = "the render failed"
            return httpx.Response(200, json=body)
        if request.method == "GET" and path == "/v1/jobs/job-1/outputs/out-1":
            facts["fetches"] += 1
            return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})
        if request.method == "DELETE" and path == "/v1/jobs/job-1":
            facts["cancels"] += 1
            return httpx.Response(200, json={})
        return httpx.Response(404)

    store = MemoryAssetStore()
    transport = HttpxTransport.for_tests(HandlerTransport(answer))
    adapter = AdapterRegistry({doc.id: doc}, transport=transport, assets=store).adapter_for(doc.id)
    runtime = Runtime(adapter, _conn(host), {}, timing)
    return Harness("http-v1", runtime, store, facts, follows_events=False)


# --- engine 3: a scripted streaming engine (test-only adapter with a push channel) ---------------------------


# The shared script vocabulary: the same words name the same engine state in all three fakes.
_STREAM_STATES = {"pending": JobState.pending, "running": JobState.running, "done": JobState.succeeded,
                  "broken": JobState.failed}


class ScriptedStreamEngine:
    """An in-process streaming engine: a submit, a poll that reads its status, and a push channel that then closes.

    ``script`` is the status each poll returns, in order (the last one repeats). ``events`` is what the push channel
    sends before it closes. ``saw_success`` records whether a poll has reported success, so a test can prove the
    artifacts were fetched only after one did.
    """

    interface_version = ADAPTER_INTERFACE_VERSION

    def __init__(self, *, script: list[str], events: list[str], store: MemoryAssetStore, facts: dict[str, Any]) -> None:
        self._script = list(script)
        self._events = list(events)
        self._store = store
        self._facts = facts
        self.saw_success = False

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef:
        self._facts["submits"] += 1
        self._facts["posts"].append(dict(request.body))
        if self._facts["lose_submit"]:
            raise TransportFailure("aborted", "The reply to the submission was lost.")
        return JobRef(op, "stream-1")

    async def poll(self, conn: Connection, ref: JobRef) -> JobStatus:
        state = _STREAM_STATES[self._script.pop(0) if len(self._script) > 1 else self._script[0]]
        if state is JobState.succeeded:
            self.saw_success = True
        return JobStatus(ref, state, assets=("stream-out",) if state is JobState.succeeded else ())

    async def events(self, _conn: Connection, ref: JobRef):
        for name in self._events:
            yield JobEvent(ref, "status", _STREAM_STATES[name])
        # then the push channel closes: no terminal event was sent

    async def cancel(self, _conn: Connection, ref: JobRef) -> None:
        self._facts["cancels"] += 1

    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus):
        self._facts["fetches"] += 1
        self._facts["fetched_after_success"] = self.saw_success
        return (self._store.put(WAV, "audio/wav", "tone.wav"),)

    async def invoke(self, conn: Connection, op: str, request: CanonicalRequest):
        raise UnsupportedByAdapter("The streaming test engine has no request operations.")


def _stream_harness(script: list[str], events: list[str], *, lose_submit: bool = False,
                    timing: tuple[float, float] = (0.01, 5.0), label: str = "Streaming test engine") -> Harness:
    store = MemoryAssetStore()
    facts = _counters()
    facts["lose_submit"] = lose_submit
    adapter = ScriptedStreamEngine(script=script, events=events, store=store, facts=facts)
    runtime = Runtime(adapter, _conn(JOBS_HOST), {}, timing)
    return Harness(label, runtime, store, facts, follows_events=True)


ENGINES = ["graph", "http-v1", "stream"]


def build(engine: str, fake_upstreams, jobs_db, tmp_path, *, script, events=(), rename=False, lose_submit=False,
          timing=None, host=None) -> Harness:
    if engine == "graph":
        return _graph_harness(fake_upstreams, tmp_path, script, host=host or GRAPH_HOST, rename=rename,
                              lose_submit=lose_submit)
    if engine == "http-v1":
        return _jobs_harness(fake_upstreams, script, host=host or JOBS_HOST, rename=rename, lose_submit=lose_submit,
                             timing=timing or (0.5, 5.0))
    return _stream_harness(script, list(events), lose_submit=lose_submit, timing=timing or (0.01, 5.0))


async def _outcome(harness: Harness, job_id: str, body: dict[str, Any] = BODY) -> dict[str, Any]:
    try:
        refs = await jobs.run(harness.runtime, OP, body, job_id=job_id, follow_events=harness.follows_events)
    except ProtocolError as error:
        return {"error": error.code}
    assets = []
    for ref in refs:
        data = await harness.store.read(ref, max_bytes=10_000_000)
        assets.append({"content_type": ref.content_type, "size": ref.size, "sha256": hashlib.sha256(data).hexdigest()})
    return {"assets": assets}


def drive(harness: Harness, job_id: str, body: dict[str, Any] = BODY) -> dict[str, Any]:
    return asyncio.run(_outcome(harness, job_id, body))


# --- criterion 2: the same runner, the same test, three adapters ----------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_one_runner_completes_the_same_workflow_on_each_adapter(engine, fake_upstreams, jobs_db, tmp_path):
    harness = build(engine, fake_upstreams, jobs_db, tmp_path, script=["pending", "running", "done"],
                    events=["running"])
    job_id = _new_job(jobs_db)
    assert drive(harness, job_id) == WAV_OUTCOME
    assert harness.facts["submits"] == 1
    assert harness.facts["fetches"] == 1
    stored = media_jobs.get(job_id, audience="internal", path=jobs_db) or {}
    assert (stored.get("metadata") or {}).get("protocol_state") == "succeeded"


# --- criterion 4: lost submit, monotone events, closed streams --------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_lost_submit_reply_ends_submission_unknown_and_is_never_resubmitted(engine, fake_upstreams, jobs_db, tmp_path):
    harness = build(engine, fake_upstreams, jobs_db, tmp_path, script=["done"], lose_submit=True)
    job_id = _new_job(jobs_db)
    assert drive(harness, job_id) == {"error": "submission_unknown"}
    assert harness.facts["submits"] == 1
    assert drive(harness, job_id) == {"error": "submission_unknown"}
    assert harness.facts["submits"] == 1, "a later run resubmitted a job whose submit reply was lost"


def assert_monotone(states: list[str]) -> None:
    """Recorded states only move forward, and a job is written succeeded once, at the end."""
    order = ["pending", "running", "succeeded"]
    ranks = [order.index(state) for state in states]
    assert ranks == sorted(ranks), states
    assert states and states[-1] == "succeeded" and states.count("succeeded") == 1, states


@pytest.fixture
def recorded_states(monkeypatch) -> list[str]:
    """Every ``protocol_state`` the runner wrote to a job, in order."""
    seen: list[str] = []
    real = media_jobs.update_status

    def recording(job_id, status, **kwargs):
        state = (kwargs.get("metadata_patch") or {}).get("protocol_state")
        if state is not None:
            seen.append(state)
        return real(job_id, status, **kwargs)

    monkeypatch.setattr(media_jobs, "update_status", recording)
    return seen


def test_duplicate_and_out_of_order_events_never_move_a_job_backwards(fake_upstreams, jobs_db, tmp_path, recorded_states):
    harness = build("stream", fake_upstreams, jobs_db, tmp_path,
                    script=["running", "running", "done"], events=["running", "pending", "running", "done"])
    assert drive(harness, _new_job(jobs_db)) == WAV_OUTCOME
    assert recorded_states == ["running", "succeeded"]
    assert harness.facts["fetched_after_success"] is True


def test_a_closed_stream_without_a_terminal_event_is_not_completion(fake_upstreams, jobs_db, tmp_path, recorded_states):
    harness = build("stream", fake_upstreams, jobs_db, tmp_path,
                    script=["pending", "running", "done"], events=["running"])
    assert drive(harness, _new_job(jobs_db)) == WAV_OUTCOME
    assert harness.facts["posts"] and harness.facts["fetched_after_success"] is True
    assert_monotone(recorded_states)


def test_a_local_validation_error_writes_no_submit_marker_so_the_same_job_can_submit_later(fake_upstreams, jobs_db, tmp_path):
    harness = build("graph", fake_upstreams, jobs_db, tmp_path, script=["done"])
    job_id = _new_job(jobs_db)
    assert drive(harness, job_id, body={**BODY, "seed": -1}) == {"error": "invalid_request"}
    assert harness.facts["submits"] == 0
    stored = media_jobs.get(job_id, audience="internal", path=jobs_db) or {}
    assert not (stored.get("metadata") or {}).get("protocol_submitting"), "a request that never left the gateway left a marker"
    assert drive(harness, job_id) == WAV_OUTCOME
    assert harness.facts["submits"] == 1


def test_a_definite_refusal_is_not_reported_as_submission_unknown_on_the_next_run(fake_upstreams, jobs_db, tmp_path):
    harness = build("graph", fake_upstreams, jobs_db, tmp_path, script=["done"])
    job_id = _new_job(jobs_db)
    harness.facts["refuse"] = 400
    assert drive(harness, job_id) == {"error": "upstream_error"}
    harness.facts["refuse"] = 0
    assert drive(harness, job_id) == WAV_OUTCOME, "a refused submission was reported as unknown on the next run"
    assert harness.facts["submits"] == 2


def test_a_server_error_on_submit_keeps_the_marker_and_is_never_resubmitted(fake_upstreams, jobs_db, tmp_path):
    harness = build("graph", fake_upstreams, jobs_db, tmp_path, script=["done"])
    job_id = _new_job(jobs_db)
    harness.facts["refuse"] = 503
    assert drive(harness, job_id) == {"error": "upstream_error"}
    harness.facts["refuse"] = 0
    assert drive(harness, job_id) == {"error": "submission_unknown"}
    assert harness.facts["submits"] == 1


def test_a_later_run_never_moves_a_persisted_state_backwards(fake_upstreams, jobs_db, tmp_path, recorded_states):
    job_id = _new_job(jobs_db)
    first = build("stream", fake_upstreams, jobs_db, tmp_path, script=["running"], events=[], timing=(0.01, 0.2))
    assert drive(first, job_id) == {"error": "budget_exceeded"}
    second = build("stream", fake_upstreams, jobs_db, tmp_path, script=["pending", "done"], events=[])
    assert drive(second, job_id) == WAV_OUTCOME
    assert recorded_states == ["running", "succeeded"], "a stale pending poll moved a persisted running job back"


class _BlockingChannel(ScriptedStreamEngine):
    """A push channel that stays open until it is cancelled, then awaits cleanup before it ends."""

    def __init__(self, cleaned: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cleaned = cleaned

    async def events(self, _conn, ref):
        try:
            await asyncio.Event().wait()
            yield JobEvent(ref, "status", JobState.running)  # never reached
        finally:
            await asyncio.sleep(0.05)
            self._cleaned.append("done")


def _blocking_harness(fake_upstreams, jobs_db, tmp_path, cleaned, script, timing):
    harness = build("stream", fake_upstreams, jobs_db, tmp_path, script=script, events=[], timing=timing)
    harness.runtime.adapter = _BlockingChannel(cleaned, script=script, events=[], store=harness.store, facts=harness.facts)
    return harness


def test_run_joins_its_follower_before_it_returns_the_assets(fake_upstreams, jobs_db, tmp_path):
    cleaned: list[str] = []
    harness = _blocking_harness(fake_upstreams, jobs_db, tmp_path, cleaned, ["running", "done"], (0.01, 5.0))
    loop = asyncio.new_event_loop()
    try:
        refs = loop.run_until_complete(jobs.run(harness.runtime, OP, BODY, job_id=_new_job(jobs_db), follow_events=True))
        assert len(refs) == 1
        assert cleaned == ["done"], "run returned before its follower finished cleanup"
    finally:
        loop.close()


def test_run_joins_its_follower_and_keeps_the_budget_error(fake_upstreams, jobs_db, tmp_path):
    cleaned: list[str] = []
    harness = _blocking_harness(fake_upstreams, jobs_db, tmp_path, cleaned, ["running"], (0.01, 0.2))
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ProtocolError) as raised:
            loop.run_until_complete(jobs.run(harness.runtime, OP, BODY, job_id=_new_job(jobs_db), follow_events=True))
        assert raised.value.code == "budget_exceeded"
        assert cleaned == ["done"], "run returned before its follower finished cleanup"
    finally:
        loop.close()


def test_a_closed_stream_alone_never_completes_a_job(fake_upstreams, jobs_db, tmp_path):
    harness = build("stream", fake_upstreams, jobs_db, tmp_path, script=["running"], events=[],
                    timing=(0.01, 0.2))
    assert drive(harness, _new_job(jobs_db)) == {"error": "budget_exceeded"}
    assert harness.facts["fetches"] == 0
    assert harness.facts["cancels"] >= 1


def test_a_failed_push_channel_leaves_the_job_to_polling(fake_upstreams, jobs_db, tmp_path):
    async def dropped():
        for _ in ():
            yield None  # an async generator that fails before it yields an event
        raise ConnectionResetError("the push channel dropped")

    class BrokenChannel(ScriptedStreamEngine):
        def events(self, conn, ref):
            return dropped()

    harness = build("stream", fake_upstreams, jobs_db, tmp_path, script=["running", "done"], events=[])
    harness.runtime.adapter = BrokenChannel(script=["running", "done"], events=[], store=harness.store, facts=harness.facts)
    assert drive(harness, _new_job(jobs_db)) == WAV_OUTCOME


# --- criterion 5: renaming the backend, its label and every node id changes nothing but labels --------------------


@pytest.mark.parametrize("engine", ["graph", "http-v1"])
def test_renaming_backend_label_and_node_ids_leaves_the_result_equal(engine, fake_upstreams, jobs_db, tmp_path):
    results = {}
    posted = {}
    for variant in ("base", "renamed"):
        sub = tmp_path / variant
        sub.mkdir()
        host = f"{engine}-{variant}.test:{'7890' if engine == 'graph' else '8080'}"
        harness = build(engine, fake_upstreams, jobs_db, sub, script=["pending", "done"], rename=(variant == "renamed"),
                        host=host)
        results[variant] = drive(harness, _new_job(jobs_db))
        posted[variant] = harness.facts["posts"][0]
    assert results["base"] == results["renamed"] == WAV_OUTCOME
    if engine == "graph":
        assert set(posted["base"]["prompt"]) != set(posted["renamed"]["prompt"]), "the renamed graph kept its node ids"
        inverse = {new: old for old, new in GRAPH_NAMES.items()}
        assert _rename_graph(posted["renamed"]["prompt"], inverse) == posted["base"]["prompt"], "the renamed graph differs beyond its ids"


def test_renaming_the_stream_engine_label_changes_nothing(fake_upstreams, jobs_db, tmp_path):
    results = []
    for label in ("Streaming test engine", "Renamed stream engine"):
        harness = _stream_harness(["done"], ["running"], label=label)
        results.append(drive(harness, _new_job(jobs_db)))
    assert results[0] == results[1] == WAV_OUTCOME


def test_graph_failure_detail_differs_only_by_the_label_and_node_id(fake_upstreams, tmp_path):
    normalised = []
    for variant in ("base", "renamed"):
        sub = tmp_path / variant
        sub.mkdir()
        harness = _graph_harness(fake_upstreams, sub, ["broken"], host=f"graph-{variant}.test:7890",
                                 rename=(variant == "renamed"))
        status = asyncio.run(harness.runtime.adapter.poll(harness.runtime.conn, JobRef(OP, "job-1")))
        assert status.state is JobState.failed
        label = harness.runtime.adapter.label
        node = "n-save" if variant == "renamed" else "3"
        normalised.append((status.detail or "").replace(label, "<label>").replace(node, "<node>"))
    assert normalised[0] == normalised[1] != ""


# --- the advance rule, on its own ------------------------------------------------------------------------------


def test_advance_is_monotone_and_terminal_states_are_final():
    advance = jobs.advance
    assert advance(None, JobState.pending) is JobState.pending
    assert advance(JobState.running, JobState.pending) is JobState.running
    assert advance(JobState.running, JobState.running) is JobState.running
    assert advance(JobState.pending, JobState.running) is JobState.running
    assert advance(JobState.running, JobState.succeeded) is JobState.succeeded
    assert advance(JobState.succeeded, JobState.running) is JobState.succeeded
    assert advance(JobState.failed, JobState.succeeded) is JobState.failed
    assert advance(JobState.cancelled, JobState.running) is JobState.cancelled
