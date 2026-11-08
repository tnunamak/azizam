"""Interface version 2: one start outcome for synchronous and asynchronous engines (S2.5a; design D3, D5, acceptance 1, 4).

Two engines run the same contracts through the same runner (``jobs.run``) and the same pinned runtime:
- a synchronous http-v1 engine, described as data (a description made at test time, with a selector form);
- a graph engine behind the node-graph adapter, against an in-process fake that answers /prompt, /history and /view.

Every name that a contract or a description uses (the operation, the workflow, the inputs, the results, the nodes, the
binding of the http engine) is generated at random when a test builds its rig, so no module can know one. Each contract
is JSON-only or mixed (a JSON value and an audio file). The positive controls put a branch on an operation name and on a
modality back into the runner and require the same checks to fail.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import tokenize
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway import media_jobs, output_selectors, pinned_runtime, result_bundle
from gateway.definition_store import DefinitionUnavailable
from gateway.engine_protocol import description, jobs
from gateway.engine_protocol.adapter import AdapterRegistry
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import DescriptionError, ProtocolError, SelectionRefused
from gateway.engine_protocol.http_adapter import HttpV1Adapter
from gateway.engine_protocol.node_graph import NodeGraphAdapter
from gateway.engine_protocol.transport import HttpxTransport
from gateway.engine_protocol.types import (
    AssetRef, AssetsResult, CanonicalRequest, Completed, Connection, JobRef, JsonResult, Pending, Reply, ResultBundle, ResultContext, Timeouts,
)
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture
from tests.test_operation_runner import WAV, jobs_db  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parent.parent
TIMEOUTS = Timeouts(connect_s=10, first_byte_s=30, total_s=30)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(56)
BODY_TEXT = "a value the caller sent"
MAKER = "person-" + uuid.uuid4().hex[:8]


def rnd(prefix: str) -> str:
    """A fresh name that nothing in the repository can spell: made when the test builds its rig."""
    return f"{prefix}{uuid.uuid4().hex[:10]}"


@dataclass
class Names:
    op: str = field(default_factory=lambda: f"{rnd('o')}.{rnd('p')}")
    workflow: str = field(default_factory=lambda: rnd("wf"))
    input: str = field(default_factory=lambda: rnd("in"))
    json_result: str = field(default_factory=lambda: rnd("jr"))
    file_result: str = field(default_factory=lambda: rnd("fr"))
    protocol: str = field(default_factory=lambda: rnd("pr"))
    binding: str = field(default_factory=lambda: rnd("bd"))
    route: str = field(default_factory=lambda: rnd("rt"))
    meta_source: str = field(default_factory=lambda: rnd("ms"))
    clip_source: str = field(default_factory=lambda: rnd("cs"))
    node_loader: str = field(default_factory=lambda: rnd("n"))
    node_sampler: str = field(default_factory=lambda: rnd("n"))
    node_json: str = field(default_factory=lambda: rnd("n"))
    node_file: str = field(default_factory=lambda: rnd("n"))
    sample_input: str = field(default_factory=lambda: rnd("k"))


META = {"scores": [1, 2, 3], "label": "ok"}
CANONICAL_META = json.dumps(META, sort_keys=True, separators=(",", ":")).encode()


def contract(names: Names, shape: str) -> dict[str, Any]:
    outputs: dict[str, Any] = {names.json_result: {"media_type": "application/json"}}
    if shape == "mixed":
        outputs[names.file_result] = {"media_type": "audio/wav"}
    return {"schema_version": 2, "id": names.workflow, "intent": names.op, "capability": "probe",
            "inputs": {names.input: {"type": "string", "required": True}}, "outputs": outputs}


@dataclass
class Rig:
    names: Names
    shape: str
    engine: str
    manifest: dict[str, Any]
    adapter: Any
    protocol: Any
    address: str
    store: MemoryAssetStore
    facts: dict[str, Any]
    binding_key: str
    template: bytes | None
    template_path: Path | None
    db: Path
    context: ResultContext = field(default_factory=lambda: ResultContext(result_bundle.ResultLimits()))
    live_address: str = ""

    def __post_init__(self) -> None:
        self.live_address = self.address

    def accept(self, *, source_kind: str = "workflow", template: bytes | None = None) -> str:
        pinned = pinned_runtime.build_envelope(
            self.manifest, self.binding_key, template=self.template if template is None else template,
            protocol=self.protocol, adapter=self.adapter, service_id="svc", origin=self.address, source_kind=source_kind)
        job = media_jobs.create(
            media_jobs.MediaJob(intent=self.names.op, request_id=rnd("req"), maker_person_id=MAKER),
            input_data={self.names.input: BODY_TEXT}, path=self.db, maker_from_request=False, envelope=pinned,
            require_envelope=True)
        return job.job_id

    def runtime(self, job_id: str, *, protocol: Any = None, adapter: Any = None) -> pinned_runtime.PinnedRuntime:
        return pinned_runtime.PinnedRuntime(
            job_id=job_id, adapter=adapter or self.adapter, protocol=protocol or self.protocol,
            connection=lambda: Connection(address=self.live_address, timeouts=TIMEOUTS), timing=(0.01, 5.0),
            jobs_path=self.db, context=self.context)

    def run(self, job_id: str, runtime: Any = None) -> Any:
        return asyncio.run(jobs.run(runtime or self.runtime(job_id), self.names.op, {self.names.input: BODY_TEXT}, job_id=job_id))

    def job(self, job_id: str) -> dict[str, Any]:
        found = media_jobs.get(job_id, audience="internal", path=self.db)
        assert found is not None
        return found

    def reader(self, **kwargs: Any) -> result_bundle.Reader:
        return result_bundle.Reader(owner_scope="unscoped", **({"person_id": MAKER} | kwargs))


# --- the synchronous http-v1 engine, described as data ----------------------------------------------------------


def http_description_text(names: Names, *, shape: str, binary: bool = False, extra: str = "") -> str:
    selectors = """
result_selectors:
  version: 1
  identity: [source]
  kind_field: decode
  fields:
    source: {type: text, required: true}
    decode: {type: enum, default: json, values: [json, base64, binary], kinds: {json: json, base64: assets, binary: assets}}
"""
    framing = "framing: binary" if binary else f"framing: json\n      select: {{{names.meta_source}: /meta, {names.clip_source}: /clip/data}}"
    return f"""
protocol_version: 1
id: {names.protocol}
title: Synchronous results engine (test fixture)
executor: http-v1
binding: {names.binding}
{selectors}{extra}
operations:
  {names.op}:
    class: generate
    request:
      method: POST
      path: /v1/{names.route}
      body:
        encoding: json
        base: empty
        map:
          - copy: {{from: /body/{names.input}, to: /payload}}
    response:
      {framing}
"""


def http_manifest(names: Names, shape: str) -> dict[str, Any]:
    manifest = contract(names, shape)
    selectors: dict[str, Any] = {names.json_result: {"source": names.meta_source, "decode": "json"}}
    if shape == "mixed":
        selectors[names.file_result] = {"source": names.clip_source, "decode": "base64"}
    manifest["bindings"] = {names.binding: {"selectors": selectors}}
    return manifest


def http_rig(fake_upstreams, jobs_db, *, shape: str, clip: bytes = WAV, meta: Any = META, text: str | None = None) -> Rig:
    names = Names()
    protocol = description.parse_description_text(text or http_description_text(names, shape=shape), source="sync.yml")
    facts: dict[str, Any] = {"posts": [], "requests": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        facts["requests"] += 1
        if request.method == "POST" and request.url.path == f"/v1/{names.route}":
            facts["posts"].append(json.loads(request.content))
            body: dict[str, Any] = {"meta": meta}
            if shape == "mixed":
                body["clip"] = {"data": base64.b64encode(clip).decode()}
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    store = MemoryAssetStore()
    transport = HttpxTransport.for_tests(HandlerTransport(answer))
    adapter = AdapterRegistry({protocol.id: protocol}, transport=transport, assets=store).adapter_for(protocol.id)
    return Rig(names, shape, "http", http_manifest(names, shape), adapter, protocol, "http://sync.test:9000", store, facts,
               names.binding, None, None, jobs_db)


# --- the graph engine behind the node-graph adapter -------------------------------------------------------------


def graph_template(names: Names, shape: str) -> dict[str, Any]:
    template: dict[str, Any] = {
        names.node_loader: {"class_type": rnd("C"), "inputs": {}},
        names.node_sampler: {"class_type": rnd("C"), "inputs": {names.sample_input: "", "src": [names.node_loader, 0]}},
        names.node_json: {"class_type": rnd("C"), "inputs": {"in": [names.node_sampler, 0]}},
    }
    if shape == "mixed":
        template[names.node_file] = {"class_type": rnd("C"), "inputs": {"in": [names.node_sampler, 0]}}
    return template


def graph_manifest(names: Names, shape: str, relative: str) -> dict[str, Any]:
    manifest = contract(names, shape)
    selectors: dict[str, Any] = {names.json_result: {"node": names.node_json, "key": "images", "from": "output_dir",
                                                    "decode": "json", "take": "single"}}
    if shape == "mixed":
        selectors[names.file_result] = {"node": names.node_file, "key": "audios", "from": "output_dir", "take": "first"}
    manifest["bindings"] = {"comfyui": {
        "workflow": relative, "map": {names.input: [{"node": names.node_sampler, "input": names.sample_input}]},
        "selectors": selectors}}
    return manifest


def graph_rig(fake_upstreams, jobs_db, tmp_path, *, shape: str, host: str = "graph.test:7890", file_view: Any = None,
              meta: Any = META) -> Rig:
    names = Names()
    relative = f"{rnd('w')}.json"
    template = graph_template(names, shape)
    template_bytes = json.dumps(template).encode()
    (tmp_path / relative).write_bytes(template_bytes)
    manifest = graph_manifest(names, shape, relative)
    protocol = description.load_registry(description.REGISTRY_DIR)["node-graph"]
    facts: dict[str, Any] = {"posts": [], "requests": 0, "fetches": 0, "states": ["done"]}

    def answer(request: httpx.Request) -> httpx.Response:
        facts["requests"] += 1
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            facts["posts"].append(json.loads(request.content))
            graph = facts["posts"][-1]["prompt"]
            if graph.get(names.node_sampler, {}).get("inputs", {}).get(names.sample_input) != BODY_TEXT:
                return httpx.Response(400, json={"error": "a bound input is missing or wrong"})
            return httpx.Response(200, json={"prompt_id": "job-1"})
        if request.method == "GET" and path == "/history/job-1":
            outputs = {names.node_json: {"images": [{"filename": "r.json", "subfolder": "", "type": "output"}]}}
            if shape == "mixed":
                outputs[names.node_file] = {"audios": [{"filename": "t.wav", "subfolder": "", "type": "output"}]}
            entry = {"status": {"completed": True, "status_str": "success", "messages": []}, "outputs": outputs}
            return httpx.Response(200, json={"job-1": entry})
        if request.method == "GET" and path == "/view":
            facts["fetches"] += 1
            if file_view is not None and request.url.params.get("filename") == "t.wav":
                return file_view(request)
            if request.url.params.get("filename") == "r.json":
                return httpx.Response(200, content=json.dumps(meta).encode(), headers={"content-type": "application/json"})
            return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})
        return httpx.Response(404)

    fake_upstreams.router.add_transport(host, HandlerTransport(answer))
    store = MemoryAssetStore()
    adapter = NodeGraphAdapter(protocol, assets=store, workflow_root=tmp_path, client_id="v2-test")
    return Rig(names, shape, "graph", manifest, adapter, protocol, f"http://{host}", store, facts, "comfyui",
               template_bytes, tmp_path / relative, jobs_db)


@pytest.fixture(params=["http", "graph"])
def engine(request) -> str:
    return request.param


@pytest.fixture(params=["json", "mixed"])
def shape(request) -> str:
    return request.param


def build(engine: str, shape: str, fake_upstreams, jobs_db, tmp_path, **kwargs: Any) -> Rig:
    if engine == "http":
        return http_rig(fake_upstreams, jobs_db, shape=shape, **kwargs)
    return graph_rig(fake_upstreams, jobs_db, tmp_path, shape=shape, **kwargs)


def check_published(rig: Rig, job_id: str) -> None:
    """The one set of checks for a finished contract: names, kinds, values and readers all come from the contract."""
    names = rig.names
    summary = result_bundle.bundle_summary(job_id, path=rig.db)
    assert summary is not None, "no bundle was published"
    kinds = {row["name"]: row["kind"] for row in summary["results"]}
    expected = {names.json_result: "json"} | ({names.file_result: "assets"} if rig.shape == "mixed" else {})
    assert kinds == expected
    read = result_bundle.read_json(job_id, names.json_result, rig.reader(), path=rig.db)
    assert read is not None and json.loads(read.body) == META
    assert result_bundle.read_json(job_id, names.json_result, rig.reader(person_id="someone-else"), path=rig.db) is None
    assert rig.job(job_id)["status"] == media_jobs.STATUS_SUCCEEDED
    if rig.shape == "mixed":
        (row,) = [r for r in summary["results"] if r["name"] == names.file_result]
        assert (row["count"], row["size_bytes"]) == (1, len(WAV))


# --- acceptance 1: data only ------------------------------------------------------------------------------------


def test_a_contract_runs_to_a_published_bundle_on_each_engine(engine, shape, fake_upstreams, jobs_db, tmp_path):
    rig = build(engine, shape, fake_upstreams, jobs_db, tmp_path)
    job_id = rig.accept()
    outcome = rig.run(job_id)
    assert isinstance(outcome, ResultBundle)
    assert set(outcome.results) == set(contract(rig.names, shape)["outputs"])
    check_published(rig, job_id)
    assert len(rig.facts["posts"]) == 1, "the engine was started more than once"
    if engine == "http":
        assert rig.job(job_id)["metadata"]["protocol_submitting"] is False, "the marker was not cleared in the commit"


def job_envelope_of(rig: Rig, job_id: str):
    from gateway.definition_store import DefinitionStore

    envelope = DefinitionStore(rig.db).envelope(job_id)
    assert envelope is not None
    return envelope


def test_a_non_workflow_offering_envelope_runs_too(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
    job_id = rig.accept(source_kind="offering")
    rig.run(job_id)
    check_published(rig, job_id)
    assert job_envelope_of(rig, job_id).source_kind == "offering"


def test_a_binary_answer_is_one_asset_result(fake_upstreams, jobs_db):
    names = Names()
    text = http_description_text(names, shape="json", binary=True)
    protocol = description.parse_description_text(text, source="binary.yml")
    manifest = contract(names, "json")
    manifest["outputs"] = {names.file_result: {"media_type": "audio/wav"}}
    manifest["bindings"] = {names.binding: {"selectors": {names.file_result: {"source": "body", "decode": "binary"}}}}
    store = MemoryAssetStore()
    transport = HttpxTransport.for_tests(HandlerTransport(lambda _r: httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})))
    adapter = AdapterRegistry({protocol.id: protocol}, transport=transport, assets=store).adapter_for(protocol.id)
    request = CanonicalRequest(op=names.op, body={names.input: "x"}, params={"manifest": manifest})
    outcome = asyncio.run(adapter.start(Connection(address="http://b.test:1", timeouts=TIMEOUTS), names.op, request))
    assert isinstance(outcome, Completed)
    assert isinstance(outcome.bundle.results[names.file_result], AssetsResult)
    (asset,) = outcome.bundle.results[names.file_result].assets
    assert asset.content_type == "audio/wav" and asset.size == len(WAV)


def test_a_job_pattern_operation_starts_as_pending_and_keeps_its_marker_flow(fake_upstreams, jobs_db):
    doc = description.parse_description_text(
        (ROOT / "tests" / "fixtures" / "engine_protocol" / "d0" / "async-audio.yml").read_text(encoding="utf-8"), source="a.yml")
    transport = HttpxTransport.for_tests(HandlerTransport(lambda _r: httpx.Response(200, json={"ticket": {"id": "t-1"}})))
    adapter = AdapterRegistry({doc.id: doc}, transport=transport, assets=MemoryAssetStore()).adapter_for(doc.id)
    outcome = asyncio.run(adapter.start(Connection(address="http://a.test:1", timeouts=TIMEOUTS), "audio.generate",
                                        CanonicalRequest(op="audio.generate", body={"prompt": "p"})))
    assert outcome == Pending(JobRef("audio.generate", "t-1"))


# --- D3: the table, for a synchronous Completed reply -------------------------------------------------------------


def test_a_finished_job_is_replayed_not_run_again(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
    job_id = rig.accept()
    rig.run(job_id)
    requests = rig.facts["requests"]
    assert rig.run(job_id) is None
    assert rig.facts["requests"] == requests, "a terminal job was sent again"
    check_published(rig, job_id)


def test_a_crash_between_send_and_commit_is_submission_unknown_and_never_resent(fake_upstreams, jobs_db, monkeypatch):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
    job_id = rig.accept()

    def crash(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the process died before the commit")

    with monkeypatch.context() as patched:
        patched.setattr(result_bundle, "complete_job", crash)
        with pytest.raises(RuntimeError):
            rig.run(job_id)
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None
    with pytest.raises(ProtocolError) as again:
        rig.run(job_id)
    assert again.value.code == "submission_unknown"
    assert len(rig.facts["posts"]) == 1


def test_a_run_with_no_marker_resumes_from_the_pin_after_a_restart(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="json")
    job_id = rig.accept()
    assert "protocol_submitting" not in (rig.job(job_id).get("metadata") or {})
    rig.run(job_id, rig.runtime(job_id))   # a new runtime, as after a restart
    check_published(rig, job_id)


def test_a_refused_result_publishes_nothing_and_leaves_the_marker(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
    rig.context = ResultContext(dataclasses.replace(result_bundle.ResultLimits(), json_max_bytes=4))
    job_id = rig.accept()
    with pytest.raises(result_bundle.ResultRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "json_too_large"
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None
    assert result_bundle.read_json(job_id, rig.names.json_result, rig.reader(), path=rig.db) is None
    assert rig.job(job_id)["status"] == media_jobs.STATUS_RUNNING
    with media_jobs.connection(rig.db) as conn:
        states = {row[0] for row in conn.execute("SELECT state FROM result_stage WHERE job_id = ?", (job_id,))}
    assert states <= {"aborted"}, "a staged result was left readable"
    assert rig.job(job_id)["metadata"]["protocol_submitting"] is True


@pytest.mark.parametrize("meta, clip, code", [
    (None, WAV, "result_missing"),
    (META, PNG, "result_wrong_type"),
    (META, b"", "result_missing"),
], ids=["json-value-null", "file-is-an-image", "file-is-empty"])
def test_an_answer_that_does_not_fit_the_contract_is_refused_whole(meta, clip, code, fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed", clip=clip, meta=meta)
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == code
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


def test_an_inline_file_that_is_not_base64_is_refused(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
    request = CanonicalRequest(op=rig.names.op, params={"manifest": rig.manifest})
    reply = Reply(status=200, headers={}, selected={rig.names.meta_source: META, rig.names.clip_source: "!!not base64!!"})
    with pytest.raises(SelectionRefused) as refused:
        asyncio.run(rig.adapter._bundle(reply, request))
    assert refused.value.code == "result_malformed"


def test_a_binding_without_selectors_is_refused_on_a_synchronous_answer(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="json")
    del rig.manifest["bindings"][rig.names.binding]["selectors"]
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "selectors_missing"


# --- the http-v1 selector descriptor ------------------------------------------------------------------------------


def test_the_http_descriptor_loads_with_its_description_and_checks_a_manifest():
    names = Names()
    protocol = description.parse_description_text(http_description_text(names, shape="mixed"), source="d.yml")
    assert protocol.result_selectors is not None and protocol.binding == names.binding
    manifest = http_manifest(names, "mixed")
    output_selectors.check_manifest(manifest, {names.binding: protocol.result_selectors})
    saved = output_selectors.check_for_save(protocol.result_selectors, manifest, names.binding)
    assert set(saved) == {names.json_result, names.file_result}
    del manifest["bindings"][names.binding]["selectors"][names.file_result]
    with pytest.raises(output_selectors.SelectorError) as missing:
        output_selectors.check_for_save(protocol.result_selectors, manifest, names.binding)
    assert missing.value.code == "selector_missing_result"


def test_a_description_with_selectors_must_name_its_binding():
    names = Names()
    text = http_description_text(names, shape="json").replace(f"binding: {names.binding}\n", "")
    with pytest.raises(DescriptionError):
        description.parse_description_text(text, source="nb.yml")


def test_a_description_revision_follows_its_content_not_its_comments():
    names = Names()
    base = http_description_text(names, shape="json")
    first = description.parse_description_text(base, source="r.yml")
    commented = description.parse_description_text("# a comment\n" + base, source="r.yml")
    edited = description.parse_description_text(base.replace("(test fixture)", "(edited)"), source="r.yml")
    assert first.revision == commented.revision != edited.revision


# --- acceptance 4: pins ------------------------------------------------------------------------------------------


def edit_live(rig: Rig) -> None:
    """Change every live definition of a contract the job already pinned: selectors, result names, inputs, graph."""
    block = rig.manifest["bindings"][rig.binding_key]
    block["selectors"] = {rnd("x"): {"source": rnd("x")} if rig.engine == "http" else {"node": rnd("x"), "key": "images", "from": "output_dir"}}
    rig.manifest["outputs"] = {rnd("x"): {"media_type": "text/plain"}}
    rig.manifest["inputs"] = {rnd("x"): {"type": "integer"}}
    if rig.template_path is not None:
        rig.template_path.write_text(json.dumps({rnd("n"): {"class_type": "Other", "inputs": {}}}))


def test_changing_every_live_definition_after_accept_gives_the_pinned_run(engine, shape, fake_upstreams, jobs_db, tmp_path):
    """Manifest, graph file, selectors, outputs and inputs change (and the graph file is deleted) between accept and the
    start of the run, and again for a runtime built after a restart: both runs use what the job pinned."""
    rig = build(engine, shape, fake_upstreams, jobs_db, tmp_path)
    job_id = rig.accept()
    edit_live(rig)
    if rig.template_path is not None:
        rig.template_path.unlink()
    rig.run(job_id)
    check_published(rig, job_id)
    if rig.engine == "graph":
        assert set(rig.facts["posts"][0]["prompt"]) == set(graph_template(rig.names, shape)), "the engine did not receive the pinned graph"


class OtherVersion:
    """The same adapter, declaring another interface version than the one the job pinned."""

    interface_version = 1

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.parametrize("change", ["protocol", "address", "blob", "adapter"])
def test_a_pin_that_no_longer_holds_is_definition_unavailable_and_sends_nothing(change, engine, fake_upstreams, jobs_db, tmp_path):
    rig = build(engine, "mixed", fake_upstreams, jobs_db, tmp_path)
    job_id = rig.accept()
    build_runtime = lambda: rig.runtime(job_id)   # noqa: E731 - built inside the raises block: a constructor may refuse too
    if change == "protocol":
        build_runtime = lambda: rig.runtime(job_id, protocol=dataclasses.replace(rig.protocol, revision="sha256:" + "0" * 64))  # noqa: E731
    elif change == "address":
        rig.live_address = "http://moved.test:1234"
    elif change == "adapter":
        build_runtime = lambda: rig.runtime(job_id, adapter=OtherVersion(rig.adapter))  # noqa: E731
    else:
        with media_jobs.connection(rig.db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM definition_blob")
            conn.commit()
    with pytest.raises(DefinitionUnavailable):
        rig.run(job_id, build_runtime())
    assert rig.facts["requests"] == 0
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


def test_a_graph_job_already_submitted_reads_its_results_by_the_pinned_selectors_after_a_restart(fake_upstreams, jobs_db, tmp_path):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="mixed")
    job_id = rig.accept()
    media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": True, "protocol_job": "job-1"},
                             path=rig.db)
    edit_live(rig)
    rig.run(job_id)
    check_published(rig, job_id)
    assert rig.facts["posts"] == [], "a job with a saved id was submitted again"


def test_a_graph_job_already_submitted_does_not_poll_a_moved_engine(fake_upstreams, jobs_db, tmp_path):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="json")
    job_id = rig.accept()
    media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": True, "protocol_job": "job-1"},
                             path=rig.db)
    rig.live_address = "http://moved.test:1234"
    with pytest.raises(DefinitionUnavailable):
        rig.run(job_id)
    assert rig.facts["requests"] == 0


# --- D6: the JSON fetch goes through the fetch gate ---------------------------------------------------------------


def test_a_standalone_json_fetch_is_bound_by_the_fetch_gate(fake_upstreams, jobs_db, tmp_path):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="json")
    limits = dataclasses.replace(result_bundle.ResultLimits(), max_concurrent_fetches=1, fetch_timeout_s=1)
    file = {"filename": "r.json", "subfolder": "", "type": "output"}
    conn = Connection(address=rig.address, timeouts=TIMEOUTS)

    async def scenario() -> None:
        assert await rig.adapter.fetch_json(conn, file, max_bytes=1000, limits=limits)
        async with output_selectors.FETCH_GATE.slot(dataclasses.replace(limits, fetch_timeout_s=30)):   # the only slot is held
            with pytest.raises(SelectionRefused) as refused:
                await rig.adapter.fetch_json(conn, file, max_bytes=1000, limits=limits)
        assert refused.value.code == "fetch_timeout"

    asyncio.run(scenario())


def test_a_result_read_waits_for_the_fetch_gate_and_times_out(fake_upstreams, jobs_db, tmp_path):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="json")
    rig.context = ResultContext(dataclasses.replace(result_bundle.ResultLimits(), max_concurrent_fetches=1, fetch_timeout_s=1))
    job_id = rig.accept()
    media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": True, "protocol_job": "job-1"},
                             path=rig.db)

    async def scenario() -> None:
        async with output_selectors.FETCH_GATE.slot(dataclasses.replace(rig.context.limits, fetch_timeout_s=30)):   # the only slot is held
            await jobs.run(rig.runtime(job_id), rig.names.op, {rig.names.input: BODY_TEXT}, job_id=job_id)

    with pytest.raises(SelectionRefused) as refused:
        asyncio.run(scenario())
    assert refused.value.code == "fetch_timeout"
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


# --- the adapter must be the one built from the pinned description ---------------------------------------------------


def second_http_adapter(rig: Rig, requests: list[str]) -> tuple[Any, Any]:
    """A same-interface-version adapter built from a changed description of the same protocol (another request path)."""
    names = rig.names
    text = http_description_text(names, shape=rig.shape).replace(f"/v1/{names.route}", f"/v1/{rnd('moved')}")
    changed = description.parse_description_text(text, source="changed.yml")

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"meta": META})

    transport = HttpxTransport.for_tests(HandlerTransport(answer))
    return changed, AdapterRegistry({changed.id: changed}, transport=transport, assets=MemoryAssetStore()).adapter_for(changed.id)


@pytest.mark.parametrize("given", ["old-protocol", "changed-protocol"])
def test_an_adapter_built_from_a_changed_description_is_refused_and_sends_nothing(given, fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="json")
    job_id = rig.accept()
    requests: list[str] = []
    changed, other_adapter = second_http_adapter(rig, requests)
    assert (changed.id, other_adapter.interface_version) == (rig.protocol.id, rig.adapter.interface_version)
    assert changed.revision != rig.protocol.revision
    with pytest.raises(DefinitionUnavailable):
        # Either way one input disagrees: the adapter was built from another description than the one handed over, or than
        # the one the job pinned.
        rig.run(job_id, rig.runtime(job_id, protocol=rig.protocol if given == "old-protocol" else changed, adapter=other_adapter))
    assert requests == [] and rig.facts["requests"] == 0
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


def test_an_adapter_that_cannot_say_what_it_was_built_from_is_refused(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="json")
    job_id = rig.accept()

    class Unlabelled:
        interface_version = rig.adapter.interface_version

    with pytest.raises(DefinitionUnavailable):
        rig.runtime(job_id, adapter=Unlabelled())
    assert rig.facts["requests"] == 0


# --- the http-v1 form is checked before anything is sent ---------------------------------------------------------------


def test_a_descriptor_the_http_adapter_cannot_read_does_not_load(fake_upstreams, jobs_db):
    names = Names()
    text = http_description_text(names, shape="json")
    source_only = text.replace("""    decode: {type: enum, default: json, values: [json, base64, binary], kinds: {json: json, base64: assets, binary: assets}}
""", "").replace("  kind_field: decode\n", "")
    wrong_kind = text.replace("kinds: {json: json, base64: assets, binary: assets}", "kinds: {json: assets, base64: assets, binary: assets}")
    other_values = text.replace("values: [json, base64, binary]", "values: [json, base64, binary, hex]").replace(
        "binary: assets}", "binary: assets, hex: assets}")
    for broken in (source_only, wrong_kind, other_values):
        assert broken != text
        with pytest.raises(DescriptionError):
            description.parse_description_text(broken, source="bad.yml")
    # A description made by hand, not loaded, is held to the same form when an adapter is built from it.
    good = description.parse_description_text(text, source="good.yml")
    handmade = dataclasses.replace(good, result_selectors=output_selectors.parse_descriptor(
        {"version": 1, "identity": ["source"], "fields": {"source": {"type": "text", "required": True}}}))
    with pytest.raises(DescriptionError):
        AdapterRegistry({handmade.id: handmade}, transport=None, assets=MemoryAssetStore()).adapter_for(handmade.id)


@pytest.mark.parametrize("selectors", [None, {}, "x", "short"], ids=["null", "empty", "not-a-mapping", "missing-an-output"])
def test_a_binding_whose_selectors_do_not_cover_the_outputs_is_refused_before_any_request(selectors, fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
    block = rig.manifest["bindings"][rig.names.binding]
    if selectors == "short":
        del block["selectors"][rig.names.file_result]
    else:
        block["selectors"] = selectors
    job_id = rig.accept()
    with pytest.raises((SelectionRefused, DefinitionUnavailable)):   # the pin's own manifest check or the adapter's
        rig.run(job_id)
    assert rig.facts["requests"] == 0 and rig.facts["posts"] == []
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


def test_a_binding_with_null_selectors_names_the_missing_selectors(fake_upstreams, jobs_db):
    rig = http_rig(fake_upstreams, jobs_db, shape="json")
    rig.manifest["bindings"][rig.names.binding]["selectors"] = None
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "selectors_missing" and rig.facts["requests"] == 0


# --- a result file is read as a stream the limits stop ----------------------------------------------------------------


class CountingBody(httpx.AsyncByteStream):
    """An engine's file that is far bigger than the limit; it counts the chunks it was asked for."""

    def __init__(self, head: bytes, chunk: int, total_chunks: int) -> None:
        self.head, self.chunk, self.total_chunks, self.yielded = head, chunk, total_chunks, 0

    async def __aiter__(self):
        yield self.head
        self.yielded += 1
        for _ in range(self.total_chunks):
            self.yielded += 1
            yield bytes(self.chunk)


def test_an_oversized_result_file_is_refused_while_it_is_read(fake_upstreams, jobs_db, tmp_path):
    body = CountingBody(WAV, chunk=512, total_chunks=2000)
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="mixed",
                    file_view=lambda request: httpx.Response(200, headers={"content-type": "audio/wav"}, stream=body))
    rig.context = ResultContext(dataclasses.replace(result_bundle.ResultLimits(), max_asset_bytes=len(WAV) + 1024))
    job_id = rig.accept()
    with pytest.raises(result_bundle.ResultRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "asset_too_large"
    assert body.yielded <= 4, f"{body.yielded} of {body.total_chunks + 1} chunks were read before the refusal"
    assert rig.store.count() == 0
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


# --- version 1 stays as it is ------------------------------------------------------------------------------------


def test_an_adapter_that_declares_version_one_is_submitted_and_never_started(fake_upstreams, jobs_db):
    started: list[str] = []

    class Old:
        interface_version = 1

        def start(self, *_a: Any) -> Any:  # would be wrong to call
            started.append("start")

        async def submit(self, conn, op, request):
            return JobRef(op, "old-1")

        async def poll(self, conn, ref):
            from gateway.engine_protocol.types import JobState, JobStatus
            return JobStatus(ref, JobState.succeeded)

        async def fetch_assets(self, conn, ref, status):
            return (AssetRef("a", "audio/wav", 1),)

        async def cancel(self, conn, ref):
            return None

    class Runtime:
        adapter = Old()

        def connection_for(self, _op):
            return Connection(address="http://old.test:1", timeouts=TIMEOUTS)

        def poll_timing(self, _op):
            return (0.01, 5.0)

        def request(self, op, body):
            return CanonicalRequest(op=op, body=dict(body))

    job = media_jobs.create(media_jobs.MediaJob(intent="x.y", request_id=rnd("req")), input_data={}, path=jobs_db, maker_from_request=False)
    refs = asyncio.run(jobs.run(Runtime(), "x.y", {}, job_id=job.job_id))
    assert refs == (AssetRef("a", "audio/wav", 1),) and started == []


def test_a_version_two_adapter_without_start_is_refused_before_any_request(jobs_db):
    class Broken:
        interface_version = 2

    class Runtime:
        adapter = Broken()

        def connection_for(self, _op):
            raise AssertionError("nothing may be resolved")

    job = media_jobs.create(media_jobs.MediaJob(intent="x.y", request_id=rnd("req")), input_data={}, path=jobs_db, maker_from_request=False)
    with pytest.raises(ProtocolError) as refused:
        asyncio.run(jobs.run(Runtime(), "x.y", {}, job_id=job.job_id))
    assert refused.value.code == "adapter_version_mismatch"


# --- positive controls: a branch on a name or a modality must make the checks fail ----------------------------------


def run_and_check(engine: str, shape: str, fake_upstreams, jobs_db, tmp_path) -> None:
    rig = build(engine, shape, fake_upstreams, jobs_db, tmp_path)
    job_id = rig.accept()
    rig.run(job_id)
    check_published(rig, job_id)


def test_positive_control_a_branch_on_an_operation_name_is_caught(engine, fake_upstreams, jobs_db, tmp_path, monkeypatch):
    real = jobs._commit_bundle

    def branched(runtime, job_id, bundle, **kwargs):
        # An identity branch put back: only the operation names a legacy route knew get their results published.
        intent = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)["intent"]
        if intent not in ("legacy.one", "legacy.two"):
            return bundle
        return real(runtime, job_id, bundle, **kwargs)

    monkeypatch.setattr(jobs, "_commit_bundle", branched)
    with pytest.raises(AssertionError):
        run_and_check(engine, "json", fake_upstreams, jobs_db, tmp_path)


def test_positive_control_a_branch_on_a_modality_is_caught(engine, fake_upstreams, jobs_db, tmp_path, monkeypatch):
    real = jobs._commit_bundle

    def branched(runtime, job_id, bundle, **kwargs):
        # A modality branch put back: files whose type starts with "audio/" are dropped from the bundle.
        kept = {name: res for name, res in bundle.results.items()
                if isinstance(res, JsonResult) or not all(a.content_type.startswith("audio/") for a in res.assets)}
        return real(runtime, job_id, ResultBundle(kept), **kwargs)

    monkeypatch.setattr(jobs, "_commit_bundle", branched)
    with pytest.raises(AssertionError):
        run_and_check(engine, "mixed", fake_upstreams, jobs_db, tmp_path)


def test_the_unbranched_runner_passes_the_same_check(engine, fake_upstreams, jobs_db, tmp_path):
    run_and_check(engine, "mixed", fake_upstreams, jobs_db, tmp_path)


def test_no_new_core_code_names_a_modality_or_an_engine():
    banned = ("audio", "image", "video", "music", "speech", "voice", "comfy", "wav", "mp3")
    hits = []
    for relative in ("gateway/engine_protocol/jobs.py", "gateway/pinned_runtime.py", "gateway/engine_protocol/http_adapter.py"):
        with (ROOT / relative).open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type == tokenize.COMMENT or (token.type == tokenize.STRING and token.string.startswith(('"""', "'''"))):
                    continue
                if any(word in token.string.lower() for word in banned):
                    hits.append(f"{relative}:{token.start[0]}: {token.string[:50]!r}")
    assert hits == []


# --- S3.8 (D5): count and JSON shape, checked after selection, on each engine ------------------------------------------

SHAPE = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "required": True, "max_items": 3, "items": {"type": "integer"}},
        "label": {"type": "string", "required": True, "enum": ["ok"]},
    },
}


def second_host(engine: str) -> dict[str, str]:
    """A test that builds two graph rigs gives the second its own host: the fake router refuses a host routed twice."""
    return {"host": "graph.test:7891"} if engine == "graph" else {}


def declare(rig: Rig, result: str, **changes: Any) -> None:
    """Add keys to one output of the rig's contract before the job is accepted (and so pinned)."""
    rig.manifest["outputs"][result].update(changes)


def single_item_rig(kind: str, fake_upstreams, jobs_db, **count: int) -> tuple[Any, str]:
    """A synchronous engine whose only result is read by ``kind`` (json, base64 or binary) and so holds exactly one item.
    Returns a ``start`` call (one request, one answer, read into a bundle) and the result name."""
    if kind == "binary":
        names = Names()
        protocol = description.parse_description_text(http_description_text(names, shape="json", binary=True), source="b.yml")
        manifest = contract(names, "json")
        manifest["outputs"] = {names.file_result: {"media_type": "audio/wav"}}
        manifest["bindings"] = {names.binding: {"selectors": {names.file_result: {"source": "body", "decode": "binary"}}}}
        transport = HttpxTransport.for_tests(HandlerTransport(lambda _r: httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})))
        adapter = AdapterRegistry({protocol.id: protocol}, transport=transport, assets=MemoryAssetStore()).adapter_for(protocol.id)
        result = names.file_result
    else:
        rig = http_rig(fake_upstreams, jobs_db, shape="mixed")
        adapter, names, manifest = rig.adapter, rig.names, rig.manifest
        result = names.json_result if kind == "json" else names.file_result
        for other in [name for name in manifest["outputs"] if name != result]:
            del manifest["outputs"][other]
            del manifest["bindings"][names.binding]["selectors"][other]
    if count:
        manifest["outputs"][result]["count"] = count

    def start() -> Any:
        request = CanonicalRequest(op=names.op, body={names.input: BODY_TEXT}, params={"manifest": manifest})
        return asyncio.run(adapter.start(Connection(address="http://sync.test:9000", timeouts=TIMEOUTS), names.op, request))

    return start, result


@pytest.mark.parametrize("kind", ["json", "base64", "binary"])
def test_each_http_decode_checks_its_single_result_count(kind, fake_upstreams, jobs_db):
    start, _ = single_item_rig(kind, fake_upstreams, jobs_db, min=1, max=1)
    assert isinstance(start(), Completed)
    start, _ = single_item_rig(kind, fake_upstreams, jobs_db, min=2, max=3)
    with pytest.raises(SelectionRefused) as refused:
        start()                        # a save refuses this count for http; the run defends itself too
    assert refused.value.code == "result_count"


@pytest.mark.parametrize("kind", ["json", "base64", "binary"])
def test_an_absent_count_keeps_each_http_selector_style(kind, fake_upstreams, jobs_db):
    start, result = single_item_rig(kind, fake_upstreams, jobs_db)
    outcome = start()
    assert isinstance(outcome, Completed) and set(outcome.bundle.results) == {result}
    assert output_selectors.effective_count({"media_type": "x/y"}, 64) == (1, 64)


@pytest.mark.parametrize("kind", ["json", "base64", "binary"])
def test_each_http_selector_can_save_one_item_but_never_two(kind):
    names = Names()
    protocol = description.parse_description_text(http_description_text(names, shape="json"), source="s.yml")
    decode = {"source": names.meta_source, "decode": kind}
    hook = HttpV1Adapter.selector_cardinality
    assert hook(decode) == (1, 1)
    media = "application/json" if kind == "json" else "audio/wav"

    def save(**count: int) -> None:
        manifest = contract(names, "json")
        manifest["outputs"] = {names.json_result: {"media_type": media, "count": count}}
        manifest["bindings"] = {names.binding: {"selectors": {names.json_result: decode}}}
        output_selectors.check_for_save(protocol.result_selectors, manifest, names.binding, cardinality=hook)

    save(min=1, max=2)
    with pytest.raises(output_selectors.SelectorError) as refused:
        save(min=2, max=3)
    assert refused.value.code == "count_unreachable" and refused.value.path == ("outputs", names.json_result, "count")


def test_a_json_document_that_breaks_its_schema_is_refused_on_each_engine_and_one_that_fits_is_published(
        engine, fake_upstreams, jobs_db, tmp_path):
    fits = build(engine, "json", fake_upstreams, jobs_db, tmp_path)
    declare(fits, fits.names.json_result, schema=SHAPE)
    job_id = fits.accept()
    fits.run(job_id)
    check_published(fits, job_id)
    broken = build(engine, "json", fake_upstreams, jobs_db, tmp_path, **second_host(engine))
    declare(broken, broken.names.json_result, schema={**SHAPE, "properties": {
        **SHAPE["properties"], "label": {"type": "string", "required": True, "enum": ["different"]}}})
    job_id = broken.accept()
    with pytest.raises(SelectionRefused) as refused:
        broken.run(job_id)
    assert refused.value.code == "result_schema" and "ok" not in str(refused.value)
    assert result_bundle.bundle_summary(job_id, path=broken.db) is None


def test_a_json_array_is_one_result_on_each_engine(engine, fake_upstreams, jobs_db, tmp_path):
    array = [1, 2, 3]
    rig = build(engine, "json", fake_upstreams, jobs_db, tmp_path, meta=array)
    declare(rig, rig.names.json_result, count={"min": 1, "max": 1}, schema={"type": "array", "max_items": 3, "items": {"type": "integer"}})
    job_id = rig.accept()
    rig.run(job_id)
    read = result_bundle.read_json(job_id, rig.names.json_result, rig.reader(), path=rig.db)
    assert read is not None and json.loads(read.body) == array
    tight = build(engine, "json", fake_upstreams, jobs_db, tmp_path, meta=array, **second_host(engine))
    declare(tight, tight.names.json_result, schema={"type": "array", "max_items": 2, "items": {"type": "integer"}})
    job_id = tight.accept()
    with pytest.raises(SelectionRefused) as refused:
        tight.run(job_id)
    assert refused.value.code == "result_schema"


def test_a_late_schema_refusal_publishes_nothing_on_either_engine(engine, fake_upstreams, jobs_db, tmp_path):
    rig = build(engine, "mixed", fake_upstreams, jobs_db, tmp_path)
    names = rig.names
    declare(rig, names.json_result, schema={"type": "object", "properties": {"label": {"type": "string", "required": True, "enum": ["different"]}}})
    # The file is processed first and saved; the JSON value that breaks its schema comes after it.
    rig.manifest["outputs"] = {k: rig.manifest["outputs"][k] for k in (names.file_result, names.json_result)}
    block = rig.manifest["bindings"][rig.binding_key]
    block["selectors"] = {k: block["selectors"][k] for k in (names.file_result, names.json_result)}
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "result_schema"
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None
    assert result_bundle.read_json(job_id, names.json_result, rig.reader(), path=rig.db) is None
    with media_jobs.connection(rig.db) as conn:
        states = {row[0] for row in conn.execute("SELECT state FROM result_stage WHERE job_id = ?", (job_id,))}
    assert states <= {"aborted"}, "a staged result was left readable"
    if engine == "http":
        assert rig.job(job_id)["metadata"]["protocol_submitting"] is True


def test_a_count_refusal_publishes_nothing_even_after_a_good_json_result(fake_upstreams, jobs_db, tmp_path):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="mixed")
    declare(rig, rig.names.file_result, count={"min": 2, "max": 3})     # the selector takes the first file, the fixture has one
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "result_count"
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None
    assert result_bundle.read_json(job_id, rig.names.json_result, rig.reader(), path=rig.db) is None


def test_a_file_of_the_wrong_family_stays_wrong_type_when_its_count_is_also_short(fake_upstreams, jobs_db, tmp_path):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="mixed")
    # The fixture returns one audio file; the contract asks for at least two images. The family is the first fault.
    declare(rig, rig.names.file_result, media_type="image/png", count={"min": 2, "max": 3})
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "result_wrong_type"
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None


def test_the_graph_results_boundary_checks_count_even_if_selection_was_precomputed(fake_upstreams, jobs_db, tmp_path, monkeypatch):
    rig = graph_rig(fake_upstreams, jobs_db, tmp_path, shape="mixed")
    declare(rig, rig.names.file_result, count={"min": 1, "max": 1})
    original = rig.adapter.collect

    def two_files(*args: Any, **kwargs: Any) -> Any:
        picked = original(*args, **kwargs)
        (only,) = picked[rig.names.file_result]
        return {**picked, rig.names.file_result: (only, {**only, "filename": "t2.wav"})}

    monkeypatch.setattr(rig.adapter, "collect", two_files)
    job_id = rig.accept()
    with pytest.raises(SelectionRefused) as refused:
        rig.run(job_id)
    assert refused.value.code == "result_count"
    assert result_bundle.bundle_summary(job_id, path=rig.db) is None
