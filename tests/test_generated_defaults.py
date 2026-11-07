"""Generated defaults (CODE_TO_DATA_S3 D6, step S3.6): the reviewed generators, one draw per request, the stored value.

Four things are held here:

- the module: a draw fills only an input the caller left empty, stays inside the rule's range intersected with the input's
  bounds, reads a stored value back, and takes an injected source;
- the routes: for every bundled workflow, a route draws exactly where it drew before this change, with the same range
  (the old rule is written out below as the oracle), and no route names the input it generates;
- the job: a value is drawn once when the job is accepted, kept in its envelope, read by every retry and restart, and
  emptied by erasure;
- the draws that stay: every other integer draw in the gateway is listed with its reason.
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import re
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

import pytest
import yaml

import proxy
from gateway import generated_defaults, media_jobs
from gateway.definition_store import DefinitionStore
from gateway.engine_protocol import audio_graph
from gateway.job_envelope import build_job_envelope
from gateway.manifests import ManifestError
from gateway.workflow_contract import GENERATORS
from tests.support.access import access_context, authenticated

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_LOW, PUBLIC_HIGH = 0, 2**32 - 1


def rule(low: Any = PUBLIC_LOW, high: Any = PUBLIC_HIGH) -> dict[str, Any]:
    return {"generator": "random_integer", "minimum": low, "maximum": high}


def inputs_with(**spec: Any) -> dict[str, Any]:
    return {"n": {"type": "integer", "generate": rule(), **spec}}


class Recorder:
    """An injected source that records the range it was asked for and gives the lowest value in it."""

    def __init__(self, value: int | None = None) -> None:
        self.asked: list[tuple[int, int]] = []
        self.value = value

    def __call__(self, low: int, high: int) -> int:
        self.asked.append((low, high))
        return low if self.value is None else self.value


def refuse_a_draw(low: int, high: int) -> int:
    raise AssertionError(f"a value was drawn from {low}..{high}; it should have been read or given")


# ---------------------------------------------------------------------------------------------------------- module


@pytest.mark.parametrize(("spec", "expected"), [
    ({}, (PUBLIC_LOW, PUBLIC_HIGH)),
    ({"minimum": 3, "maximum": 9}, (3, 9)),
    ({"minimum": 3.5, "maximum": 9.5}, (4, 9)),
    ({"minimum": -10, "maximum": 2**40}, (PUBLIC_LOW, PUBLIC_HIGH)),
], ids=["no-input-bounds", "narrower-input", "fractional-bounds", "wider-input"])
def test_a_draw_comes_from_the_rule_bounds_intersected_with_the_input_bounds(spec, expected):
    source = Recorder()
    generated_defaults.resolve_generated(inputs_with(**spec), {}, source=source)
    assert source.asked == [expected]


def test_a_narrower_rule_narrows_the_range():
    source = Recorder()
    generated_defaults.resolve_generated({"n": {"type": "integer", "generate": rule(10, 20)}}, {}, source=source)
    assert source.asked == [(10, 20)]


def test_a_draw_is_stored_with_its_generator_id():
    block = generated_defaults.resolve_generated(inputs_with(), {}, source=Recorder(7))
    assert block == {"n": {"generator": "random_integer", "value": 7}}


def test_nothing_is_drawn_for_a_value_the_caller_gave():
    assert generated_defaults.resolve_generated(inputs_with(), {"n": 5}, source=refuse_a_draw) == {}
    assert generated_defaults.resolved_values(inputs_with(), {"n": 5}, source=refuse_a_draw) == {"n": 5}


def test_a_caller_cannot_set_an_input_only_the_gateway_fills():
    inputs = inputs_with(settable=False)
    source = Recorder(9)
    assert generated_defaults.resolved_values(inputs, {"n": 5}, source=source) == {"n": 9}
    assert len(source.asked) == 1


def test_a_stored_value_is_read_back_and_never_drawn_again():
    stored = {"n": {"generator": "random_integer", "value": 41}}
    block = generated_defaults.resolve_generated(inputs_with(), {}, stored=stored, source=refuse_a_draw)
    assert block == stored


def test_a_stored_value_from_another_generator_is_not_read():
    stored = {"n": {"generator": "something_else", "value": 41}}
    source = Recorder(3)
    assert generated_defaults.resolve_generated(inputs_with(), {}, stored=stored, source=source)["n"]["value"] == 3


def test_a_range_with_no_integer_is_a_fault_of_the_document():
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs_with(minimum=2**32, maximum=2**33), {}, source=Recorder())
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs_with(minimum=3.2, maximum=3.8), {}, source=Recorder())


def test_a_generator_no_reviewed_code_backs_is_refused_at_run():
    inputs = {"n": {"type": "integer", "generate": {"generator": "unreviewed"}}}
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs, {}, source=Recorder())


def test_a_drawn_value_the_input_does_not_accept_is_refused():
    with pytest.raises(ManifestError):
        generated_defaults.resolve_generated(inputs_with(step=2, offset=0), {}, source=lambda low, high: 3)


def test_the_default_source_is_operating_system_randomness(monkeypatch):
    asked: list[int] = []
    monkeypatch.setattr(secrets, "randbelow", lambda bound: asked.append(bound) or 4)
    assert generated_defaults.secure_source(10, 19) == 14
    assert asked == [10]
    value = generated_defaults.resolve_generated(inputs_with(), {})["n"]["value"]
    assert PUBLIC_LOW <= value <= PUBLIC_HIGH


def test_the_reviewed_generators_are_the_ones_the_contract_checks_at_save():
    assert set(generated_defaults.REVIEWED) == set(GENERATORS)
    assert generated_defaults.persistence() == {"random_integer": "value"}


def test_a_route_that_records_one_value_reads_the_first_generated_input():
    inputs = {"a": {"type": "string"}, "b": {"type": "integer", "generate": rule()}, "c": {"type": "integer", "generate": rule()}}
    assert generated_defaults.first_value(inputs, {"c": 3, "b": 2, "a": "x"}) == 2
    assert generated_defaults.first_value(inputs, {"a": "x"}) is None


# ---------------------------------------------------------------------------------------- the routes, differential


def bundled(*intents: str) -> list[pytest.param]:
    found = []
    for path in sorted((ROOT / "manifests" / "workflows").glob("*.yml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        binding = manifest["bindings"].get("comfyui") or {}
        if manifest["intent"] in intents and binding.get("workflow") and "compose" not in binding and "role" not in binding:
            found.append(pytest.param(manifest, id=path.stem))
    return found


def drew_before_by_audio_route(manifest: Mapping[str, Any]) -> tuple[int, int] | None:
    """The old audio edge: it drew when the manifest declared `seed`, from the public range and the declared bounds."""
    spec = manifest["inputs"].get("seed")
    if spec is None:
        return None
    return max(0, math.ceil(spec.get("minimum", 0))), min(2**32 - 1, math.floor(spec.get("maximum", 2**32 - 1)))


def drew_before_by_image_route(manifest: Mapping[str, Any]) -> tuple[int, int] | None:
    """The old image rounds: they drew from 0 to 2**32-1 when the graph binding mapped `seed`."""
    mapped = (manifest["bindings"]["comfyui"].get("map") or {}).get("seed")
    return (0, 2**32 - 1) if mapped else None


def draw_now_by_audio_route(manifest: Mapping[str, Any], body: Mapping[str, Any]) -> list[tuple[int, int]]:
    source = Recorder()
    effective = audio_graph.effective_manifest(manifest, audio_graph.AudioLimits())
    audio_graph.manifest_inputs({"prompt": "x", **body}, manifest=effective, source=source)
    return source.asked


def draw_now_by_image_route(manifest: Mapping[str, Any], body: Mapping[str, Any]) -> list[tuple[int, int]]:
    source = Recorder()
    with patch.object(generated_defaults, "secure_source", source):
        proxy._image_generated_values(manifest, body)
    return source.asked


@pytest.mark.parametrize("manifest", bundled("audio.generate"))
def test_the_audio_route_draws_exactly_where_it_drew_before_with_the_same_range(manifest):
    before = drew_before_by_audio_route(manifest)
    assert draw_now_by_audio_route(manifest, {}) == ([before] if before else [])
    assert draw_now_by_audio_route(manifest, {"seed": 5}) == [], "a caller's own value is never drawn over"


@pytest.mark.parametrize("manifest", bundled("image.generate"))
def test_the_image_generation_route_draws_exactly_where_it_drew_before_with_the_same_range(manifest):
    before = drew_before_by_image_route(manifest)
    assert draw_now_by_image_route(manifest, {}) == ([before] if before else [])
    given = {"extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 5}}}
    expected_after_giving = [before] if before and manifest["inputs"]["seed"].get("settable") is False else []
    assert draw_now_by_image_route(manifest, given) == expected_after_giving


@pytest.mark.parametrize("manifest", bundled("image.edit"))
def test_the_image_edit_route_draws_exactly_where_it_drew_before_with_the_same_range(manifest):
    before = drew_before_by_image_route(manifest)
    assert draw_now_by_image_route(manifest, {}) == ([before] if before else [])
    given = {"extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 5}}}
    expected_after_giving = [before] if before and manifest["inputs"]["seed"].get("settable") is False else []
    assert draw_now_by_image_route(manifest, given) == expected_after_giving


def test_the_mesh_route_still_sends_no_value_when_the_caller_gives_none():
    manifest = yaml.safe_load((ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml").read_text(encoding="utf-8"))
    assert generated_defaults.generated_inputs(manifest["inputs"]) == {}


# ------------------------------------------------------------------------------------------------- the token test


def word_hits(text: str, word: str) -> list[str]:
    return re.findall(r"(?<![a-z0-9_-])" + re.escape(word) + r"(?![a-z0-9_-])", text.lower())


def function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{path.name} has no function {name}")


# Code that reads or fills a generated input for a route. None may name the input; the document names it.
GENERIC_PATHS = {
    ROOT / "proxy.py": [
        "_image_generated_values", "_bind_generated_image_inputs", "_accepted_generated", "_stored_generated",
        "_generate_images_with_adaptive_batching", "proxy_audio_generate", "_submit_audio_media_job",
        "gateway_image_generations_media_job", "_run_audio_media_job", "_run_image_media_job",
    ],
    ROOT / "gateway" / "engine_protocol" / "audio_graph.py": ["manifest_inputs", "generated_block"],
    ROOT / "gateway" / "definition_store.py": ["clear_generated_in"],
    ROOT / "gateway" / "media_jobs.py": ["_clear_generated_in"],
}


def test_the_token_test_sees_the_word():
    assert word_hits('if name == "seed":', "seed") == ["seed"]
    assert word_hits("legacy_seed_field and Re-seed and seeded", "seed") == []


def test_no_core_code_names_seed():
    assert word_hits((ROOT / "gateway" / "generated_defaults.py").read_text(encoding="utf-8"), "seed") == []
    for path, functions in GENERIC_PATHS.items():
        for name in functions:
            assert word_hits(function_source(path, name), "seed") == [], f"{path.name}:{name} names the input it generates"


# ----------------------------------------------------------------------------------------------- the draws that stay

# Every integer draw in proxy.py and gateway/: (file, function) -> (how many, why it stays).
LISTED_DRAWS = {
    ("proxy.py", "_proxy_graph_video_create"): (2, "the video route draws its own value; video moves with the video workflow slice"),
    ("proxy.py", "_submit_video_chain"): (1, "the video chain route draws its own value; video moves with the video workflow slice"),
    ("proxy.py", "_reseed_image_workflow"): (2, "a graph with no contract (graph metadata path) has no generate rule; the offset tile composition draws per tile; both go with their roles"),
    ("proxy.py", "_proxy_offset_inpaint_tile_request"): (1, "the offset tile composition's seam pass draws its own value; the composition is a reviewed role"),
    ("gateway/a1111_api.py", "map_payload"): (1, "an A1111 client's own frontend field, mapped for the route; a frontend profile, not a document"),
    ("gateway/avatar_generation.py", "new_seed"): (1, "a person's avatar number, not a workflow input"),
    ("gateway/generated_defaults.py", "secure_source"): (1, "the reviewed generators' own source, the one place a generated value is drawn"),
    ("gateway/stt_measure.py", "paired_excess"): (1, "bootstrap resampling in a measurement, not an input"),
    ("gateway/stt_measure.py", "rate_interval"): (1, "bootstrap resampling in a measurement, not an input"),
    ("gateway/stt_measure.py", "difference"): (2, "bootstrap resampling in a measurement, not an input"),
    ("gateway/workflow_equality.py", "generated_value"): (1, "the equality tool's reproducible stand-in for a generator, seeded by workflow and input"),
}
DRAW_CALLS = {"randint", "randrange", "getrandbits", "randbelow"}


def integer_draws() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in [ROOT / "proxy.py", *sorted((ROOT / "gateway").rglob("*.py"))]:
        def walk(node: ast.AST, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                scope = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in DRAW_CALLS:
                    key = (str(path.relative_to(ROOT)), owner)
                    found[key] = found.get(key, 0) + 1
                walk(child, scope)

        walk(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def test_every_integer_draw_in_the_gateway_is_listed_with_its_reason():
    assert integer_draws() == {key: count for key, (count, _why) in LISTED_DRAWS.items()}
    assert all(why for _count, why in LISTED_DRAWS.values())


# ------------------------------------------------------------------------------------------------------------ the job


class StubRequest:
    method = "POST"
    scope: dict[str, Any] = {"auth_context": access_context()}
    query_params: dict[str, str] = {}

    def __init__(self, body: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> None:
        self._body = dict(body)
        self.headers = dict(headers or {"host": "gateway.test"})
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")

    async def json(self) -> dict[str, Any]:
        return dict(self._body)


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    with authenticated():
        yield db


def submit_image(body: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> str:
    scheduled: list[str] = []
    with patch.object(proxy, "_schedule_image_media_job", lambda job_id: scheduled.append(job_id) or True):
        response = asyncio.run(proxy.gateway_image_generations_media_job(
            StubRequest(body, {"host": "gateway.test", **(headers or {})})))
    assert response.status_code == 202, response.body
    return json.loads(response.body)["job_id"]


def submit_audio(body: Mapping[str, Any]) -> str:
    with patch.object(proxy, "_schedule_audio_media_job", lambda job_id: True):
        response = asyncio.run(proxy._dispatch_gateway_media_job_submission(
            request=StubRequest({}), payload={"intent": "audio.generate", "parameters": dict(body)}, audience="client"))
    assert response is not None and response.status_code == 202, response and response.body
    return json.loads(response.body)["job_id"]


IMAGE_BODY = {"model": "flux2-klein-4b", "prompt": "a lighthouse"}
AUDIO_BODY = {"model": "tangoflux", "prompt": "rain on a tin roof", "duration": 8}


def kept(db: Path, job_id: str) -> Mapping[str, Mapping[str, Any]]:
    envelope = DefinitionStore(db).envelope(job_id)
    assert envelope is not None
    return envelope.generated


def test_an_accepted_image_job_draws_once_and_keeps_the_value_in_its_envelope(jobs_db):
    source = Recorder(12345)
    with patch.object(generated_defaults, "secure_source", source):
        job_id = submit_image(IMAGE_BODY)
    assert source.asked == [(PUBLIC_LOW, PUBLIC_HIGH)]
    assert kept(jobs_db, job_id) == {"seed": {"generator": "random_integer", "value": 12345}}


def test_an_accepted_audio_job_draws_once_and_keeps_the_value_in_its_envelope(jobs_db):
    source = Recorder(777)
    with patch.object(generated_defaults, "secure_source", source):
        job_id = submit_audio(AUDIO_BODY)
    assert source.asked == [(PUBLIC_LOW, PUBLIC_HIGH)]
    assert kept(jobs_db, job_id) == {"seed": {"generator": "random_integer", "value": 777}}


def test_a_job_whose_caller_gave_the_value_keeps_none(jobs_db):
    with patch.object(generated_defaults, "secure_source", refuse_a_draw):
        image = submit_image({**IMAGE_BODY, "extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 9}}})
        audio = submit_audio({**AUDIO_BODY, "seed": 9})
    assert kept(jobs_db, image) == {} and kept(jobs_db, audio) == {}


def test_a_replay_of_the_same_idempotency_key_draws_nothing_and_answers_the_same_job(jobs_db):
    source = Recorder(31)
    headers = {"idempotency-key": "same-key"}
    with patch.object(generated_defaults, "secure_source", source):
        first = submit_image(IMAGE_BODY, headers)
        second = submit_image(IMAGE_BODY, headers)
    assert first == second
    assert len(source.asked) == 1
    assert kept(jobs_db, first)["seed"]["value"] == 31


def test_a_restart_reads_the_stored_value_and_never_draws_again(jobs_db):
    with patch.object(generated_defaults, "secure_source", Recorder(2468)):
        job_id = submit_image(IMAGE_BODY)
    # A restart: nothing of the first process remains but the database.
    stored = proxy._stored_generated(job_id)
    token = proxy._JOB_GENERATED.set(stored)
    try:
        with patch.object(generated_defaults, "secure_source", refuse_a_draw):
            first = proxy._image_generated_values(proxy._find_image_manifest("image.generate", "flux2-klein-4b"), IMAGE_BODY)
            retry = proxy._image_generated_values(proxy._find_image_manifest("image.generate", "flux2-klein-4b"), IMAGE_BODY)
    finally:
        proxy._JOB_GENERATED.reset(token)
    assert first == retry == {"seed": 2468}


def test_the_audio_route_reads_the_stored_value_on_a_retry(jobs_db):
    with patch.object(generated_defaults, "secure_source", Recorder(1357)):
        job_id = submit_audio(AUDIO_BODY)
    manifest = proxy._load_audio_workflow_manifest("tangoflux")
    effective = audio_graph.effective_manifest(manifest, audio_graph.AudioLimits())
    for _attempt in range(2):
        inputs = audio_graph.manifest_inputs(
            {"prompt": "x"}, manifest=effective, stored=proxy._stored_generated(job_id), source=refuse_a_draw)
        assert inputs["seed"] == 1357


def test_the_job_runners_hand_the_stored_value_to_their_route(jobs_db, monkeypatch):
    with patch.object(generated_defaults, "secure_source", Recorder(8642)):
        job_id = submit_image(IMAGE_BODY)
    seen: list[Any] = []

    async def route(request, auth_context, receipt=None):
        seen.append(proxy._JOB_GENERATED.get())
        return proxy.Response(status_code=500, content=b"{}", media_type="application/json")

    monkeypatch.setattr(proxy, "proxy_image_request", route)
    monkeypatch.setattr(proxy, "_image_media_job_max_attempts", lambda: 1)
    asyncio.run(proxy._run_image_media_job(job_id))
    assert seen and seen[0]["seed"]["value"] == 8642
    assert proxy._JOB_GENERATED.get() is None, "the value is not left set after the run"


def test_the_stored_value_survives_a_second_envelope_read_through_the_result_identity(jobs_db):
    from gateway import result_bundle

    with patch.object(generated_defaults, "secure_source", Recorder(5)):
        job_id = submit_image(IMAGE_BODY)
    with media_jobs.connection(jobs_db) as conn:
        assert result_bundle._envelope_identity(conn, job_id) is not None


# --------------------------------------------------------------------------------------------------------- erasure


def envelope_with(value: int):
    return build_job_envelope(
        source_kind="workflow", source_id="wf-erase", contract={"inputs": {"n": {"type": "integer"}}},
        binding={"map": {"n": [{"node": "1", "input": "n"}]}}, template=b"graph", protocol_id="p", adapter_id="a",
        interface_version=1, service_id="s", origin="http://engine.test:1",
        generated={"n": {"generator": "random_integer", "value": value}})


def make_job(db: Path, app_id: str, value: int) -> str:
    job = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", workflow="wf-erase", app_id=app_id, request_id=f"req-{app_id}"),
        envelope=envelope_with(value), path=db, maker_from_request=False)
    return job.job_id


def test_erasure_empties_a_non_empty_generated_block_and_keeps_the_pins_and_other_people(tmp_path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    mine, theirs = make_job(db, "app_mine", 611223344), make_job(db, "app_theirs", 655443322)
    assert kept(db, mine)["n"]["value"] == 611223344
    owner = media_jobs.stored_owner_key(None, app_id="app_mine")

    media_jobs.erase_prompt_derivatives({owner}, before=int(__import__("time").time()) + 5, path=db)

    assert kept(db, mine) == {}
    assert DefinitionStore(db).envelope(mine).contract == envelope_with(1).envelope.contract, "its pins are kept"
    assert kept(db, theirs)["n"]["value"] == 655443322, "another person is untouched"
    text = "\n".join(str(row) for row in sqlite_rows(db, "job_envelope"))
    assert "611223344" not in text and "655443322" in text


def test_the_kill_switch_wildcard_empties_every_block(tmp_path):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    ids = [make_job(db, "app_a", 1111), make_job(db, "app_b", 2222)]
    media_jobs.erase_prompt_derivatives({"*"}, before=int(__import__("time").time()) + 5, path=db)
    assert [kept(db, job_id) for job_id in ids] == [{}, {}]


def sqlite_rows(db: Path, table: str) -> list[tuple]:
    import sqlite3

    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - test table names
