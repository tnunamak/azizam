"""Mounted video-create boundaries and a source-only, shrinking return-site ratchet.

No engine or legacy implementation runs. The only production source read by the
ratchet is gateway/frontend_shapes/video_create.py.
"""

from __future__ import annotations

import ast
import base64
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.responses import Response
from starlette.testclient import TestClient

from gateway import row_routes
from gateway.frontend_shapes import _base, shapes
from gateway.workflow_runtime_types import RunJobView, RunRequest
from tests.test_video_create_shape import FakeSpool, PNG, error, read, snap, video_contract


PATHS = ("/v1/videos", "/openai/v1/videos")
IMAGE_BASE64 = base64.b64encode(PNG).decode()
IMAGE_DATA = "data:image/png;base64," + IMAGE_BASE64
SHAPE_SOURCE = Path(__file__).resolve().parents[1] / "gateway/frontend_shapes/video_create.py"


@pytest.fixture(autouse=True)
def no_deployment_hooks(monkeypatch):
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", None, raising=False)


def snapshot(case="ordinary"):
    main = video_contract("m", image=True, variants={
        "long": "video.generate.long", "image": "video.generate.i2v",
    })
    image = video_contract("i2v", image=True, requires=["image"])
    long = video_contract("long", chain=True)
    lone = video_contract("lone")
    keyframes = video_contract("keyframes", requires=["extensions"])
    keyframes["inputs"]["frames"] = {"type": "integer", "minimum": 9, "maximum": 241}
    keyframes["video"]["roles"]["length"] = "frames"
    del keyframes["inputs"]["length"]
    contracts = [main, image, long, lone, keyframes]
    if case == "no-video":
        del main["video"]
    elif case.startswith("no-"):
        del main["video"][case.removeprefix("no-")]
    elif case == "chain":
        main["video"]["chain"] = True
    elif case in ("requires-source", "requires-extensions"):
        main["video"]["requires"] = [case.removeprefix("requires-")]
    elif case == "select-subject":
        main["video"]["select_subject"] = True
    elif case == "picture":
        main["inputs"]["picture"] = {"type": "asset", "accepts": ["image/*"]}
    elif case.startswith("image-no-"):
        del image["video"][case.removeprefix("image-no-")]
    elif case == "image-chain":
        image["video"]["chain"] = True
    elif case == "image-unavailable":
        contracts.remove(image)
    else:
        assert case == "ordinary", case
    return snap(*contracts)


def mounted(snapshot):
    calls = {key: [] for key in ("auth", "policy", "receipt", "report", "submit", "legacy", "spools")}
    old_answer = Response(
        b'{"legacy":true,"opaque":"keep this body"}\n', status_code=207,
        headers={"Content-Type": "application/x-legacy-video", "X-Legacy": "unchanged",
                 "Location": "/legacy/video", "Cache-Control": "no-store"},
    )

    async def legacy(request, caller, body):
        calls["legacy"].append((caller, deepcopy(body)))
        return old_answer

    class Core:
        async def submit_run(self, run_request, caller, *, snapshot):
            calls["submit"].append(run_request)
            return RunJobView(
                job_id="mj_matrix", status="queued", disposition="new",
                workflow_id=run_request.workflow_id, record_revision=1,
                binding_key="comfyui", created_at_s=1, updated_at_s=1,
            )

    class Owner:
        def report(self, **outcome):
            calls["report"].append(outcome)

        def handoff(self):
            pass

        def dispatching(self, backend):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def spool():
        value = FakeSpool()
        calls["spools"].append(value)
        return value

    deps = row_routes.RowRouteDeps(
        authenticate=lambda request, path: calls["auth"].append(path) or "caller-matrix",
        enforce_policy=lambda caller, path: calls["policy"].append(path),
        core=lambda: Core(), snapshot=lambda: snapshot, jobs=lambda: None, new_spool=spool,
        scope=lambda request, spool: nullcontext(),
        refused=lambda refused: Response("unexpected core refusal", status_code=refused.http_status),
        new_request_id=lambda: "req-matrix",
        open_receipt=lambda *args: calls["receipt"].append(args) or Owner(),
        name_receipt=lambda *args: None, pinned=lambda job_id: None,
        legacy={"videos.create": legacy},
    )
    app = FastAPI()
    row_routes.mount_rows(app, deps, ("videos-create",))
    return app, calls, old_answer


HANDOFFS = [
    pytest.param("ordinary", {"model": "m", "shots": [{"prompt": "a"}]}, id="shots"),
    pytest.param("ordinary", {"model": "m", "shots": []}, id="empty-shots"),
    pytest.param("ordinary", {"model": "m", "shots": False}, id="false-shots"),
    pytest.param("ordinary", {"model": "m", "shots": 0}, id="zero-shots"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "seconds": 11}, id="long-seconds"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "seconds": "11"}, id="long-seconds-string"),
    pytest.param("ordinary", {"model": "long", "prompt": "x"}, id="long-model"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "inputs": {"a": 1}}, id="workflow-inputs"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "input_reference": "img_abc"}, id="image-id"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "input_reference": {"id": "img_abc"}}, id="image-id-object"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "extensions": {"com.azizam.workflow": {"bogus": 1}}}, id="undeclared-extension"),
    pytest.param("picture", {"model": "m", "prompt": "x", "extensions": {"com.azizam.workflow": {"picture": "img_abc"}}}, id="asset-extension"),
    pytest.param("no-video", {"model": "m", "prompt": "x"}, id="no-video-block"),
    pytest.param("no-length", {"model": "m", "prompt": "x"}, id="no-length"),
    pytest.param("no-grid", {"model": "m", "prompt": "x"}, id="no-grid"),
    pytest.param("no-size", {"model": "m", "prompt": "x"}, id="no-size"),
    pytest.param("chain", {"model": "m", "prompt": "x"}, id="chain"),
    pytest.param("requires-source", {"model": "m", "prompt": "x"}, id="requires-source"),
    pytest.param("select-subject", {"model": "m", "prompt": "x"}, id="select-subject"),
    pytest.param("requires-extensions", {"model": "m", "prompt": "x"}, id="requires-extensions"),
    pytest.param("image-no-length", {"model": "m", "prompt": "x", "input_reference": IMAGE_DATA}, id="selected-image-no-length"),
    pytest.param("image-no-grid", {"model": "m", "prompt": "x", "input_reference": IMAGE_DATA}, id="selected-image-no-grid"),
    pytest.param("image-no-size", {"model": "m", "prompt": "x", "input_reference": IMAGE_DATA}, id="selected-image-no-size"),
    pytest.param("image-chain", {"model": "m", "prompt": "x", "input_reference": IMAGE_DATA}, id="selected-image-chain"),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("contract,body", HANDOFFS)
def test_handoff_preserves_the_whole_body_and_legacy_response(contract, body, path):
    before = deepcopy(body)
    registry = snapshot(contract)
    direct = read(deepcopy(body), registry)
    assert isinstance(direct, _base.LegacyRoute)
    assert direct.name == "videos.create" and direct.body == before
    app, calls, old_answer = mounted(registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post(path, json=deepcopy(body))
    assert calls["legacy"] == [("caller-matrix", before)]
    assert calls["submit"] == [] and calls["receipt"] == [] and calls["report"] == []
    assert calls["policy"] == [] and len(calls["auth"]) == 1
    assert all(spool.staged == [] for spool in calls["spools"])
    assert (answer.status_code, answer.content, dict(answer.headers)) == (
        old_answer.status_code, old_answer.body, dict(old_answer.headers),
    )
    assert body == before


NO_HANDOFFS = [
    pytest.param("ordinary", {"model": "m", "prompt": "x"}, "video.generate.m", id="plain"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "seconds": 10, "shots": None, "inputs": {}}, "video.generate.m", id="native-boundary"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "extensions": {"com.azizam.workflow": {"seed": 7}}}, "video.generate.m", id="declared-seed"),
    pytest.param("ordinary", {"model": "keyframes", "prompt": "x", "extensions": {"com.azizam.workflow": {"frames": 49}}}, "video.generate.keyframes", id="declared-frames"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "extensions": {"com.azizam.video": {"audio": False}}}, "video.generate.m", id="silent"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "input_reference": IMAGE_DATA}, "video.generate.i2v", id="data-url"),
    pytest.param("ordinary", {"model": "m", "prompt": "x", "input_reference": IMAGE_BASE64}, "video.generate.i2v", id="base64"),
    pytest.param("image-unavailable", {"model": "m", "prompt": "x", "input_reference": IMAGE_DATA}, "video.generate.m", id="unavailable-image-is-not-incomplete-image"),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("contract,body,workflow", NO_HANDOFFS)
def test_plain_requests_reach_the_new_runner_not_the_old_handler(contract, body, workflow, path):
    registry = snapshot(contract)
    spool = FakeSpool()
    direct = read(deepcopy(body), registry, spool=spool)
    assert isinstance(direct, RunRequest), direct
    assert direct.workflow_id == workflow
    if "input_reference" in body:
        assert [(name, blob) for name, blob, *_ in spool.staged] == [("input_reference", PNG)]
    block = body.get("extensions", {}).get("com.azizam.workflow", {})
    for field, value in block.items():
        assert direct.inputs[field] == value
    if body.get("extensions", {}).get("com.azizam.video", {}).get("audio") is False:
        assert direct.options["audio"] is False and direct.facts["audio"] is False
    app, calls, _ = mounted(registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post(path, json=deepcopy(body))
    assert calls["legacy"] == []
    assert len(calls["submit"]) == 1 and calls["submit"][0].workflow_id == workflow
    assert len(calls["receipt"]) == 1 and len(calls["policy"]) == 1
    assert answer.status_code == 200 and answer.json()["status"] == "queued"


# Full literal refusal bodies are below; none is derived from the shape under test.
REFUSALS = [
    pytest.param({"model": "missing", "prompt": "x"}, 404,
                 {"message": "Model 'missing' is not available for video generation.", "type": "invalid_request_error",
                  "param": "model", "code": "model_not_found"}, id="unknown-model"),
    pytest.param({"model": "i2v", "prompt": "x"}, 400,
                 {"message": "Model video-i2v is image-to-video and requires input_reference", "type": "invalid_request_error",
                  "param": "input_reference"}, id="missing-reference"),
    pytest.param({"model": "m", "prompt": 5}, 400,
                 {"message": "prompt must be a string", "type": "invalid_request_error", "param": "prompt"}, id="prompt-type"),
    pytest.param({"model": "m", "prompt": "x", "seconds": "bad"}, 400,
                 {"message": "seconds must be a number", "type": "invalid_request_error"}, id="seconds-type"),
    pytest.param({"model": "m", "prompt": "x", "extensions": {"com.azizam.video": {"audio": "no"}}}, 400,
                 {"message": "audio must be true or false", "type": "invalid_request_error",
                  "param": "extensions.com.azizam.video.audio", "code": "invalid_video_control"}, id="audio-type"),
    pytest.param({"model": "m", "prompt": "x", "bogus": 1}, 400,
                 {"message": "unknown top-level field; move gateway-native controls under extensions.com.azizam.workflow",
                  "type": "invalid_request_error", "param": "bogus", "code": "unknown_parameter"}, id="unknown-field"),
    pytest.param({"model": "lone", "shots": [{"prompt": "a"}]}, 400,
                 {"message": "Model video-lone cannot render shots or clips longer than 10 s; use a long-video model",
                  "type": "invalid_request_error", "param": "shots", "code": "long_video_not_supported"}, id="shots-no-long"),
    pytest.param({"model": "lone", "prompt": "x", "seconds": 11}, 400,
                 {"message": "seconds must be between 1.0 and 10.0 for model video-lone", "type": "invalid_request_error"},
                 id="seconds-no-long"),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("body,status,expected_error", REFUSALS)
def test_refusals_keep_the_current_status_and_whole_error_body(body, status, expected_error, path):
    registry = snapshot()
    direct = read(deepcopy(body), registry)
    assert isinstance(direct, Response) and direct.status_code == status
    assert error(direct) == expected_error
    app, calls, _ = mounted(registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post(path, json=deepcopy(body))
    assert calls["legacy"] == [] and calls["submit"] == []
    assert answer.status_code == status and answer.json() == {"error": expected_error}


# Literal semantic sites, not line numbers. Parentheses/quotes/line wrapping are
# normalized by parsing the predicate text; neither the list nor its size comes
# from production. Counter subtraction also catches a second identical site.
RETURN_SITES = [
    ("select", "legacy", ("manifest is None or manifest.chain or manifest.length is None",)),
    ("select", "legacy", ("manifest.chain or manifest.length is None or manifest.grid is None or manifest.size is None",)),
    ("select", "legacy", ('shots or (_beyond(seconds, limit) and manifest.variants.get("long"))',
                           'manifest.variants.get("long")')),
    ("select", "legacy", ('(manifest.requires - {kind, "extensions"}) or manifest.select_subject or body.get("inputs") '
                           'or ("extensions" in manifest.requires and not block) '
                           'or (reference is not None and _reference_bytes(reference) is None) '
                           'or any(not _wire.declared(chosen, key) or _is_asset(chosen, key) for key in block)',)),
    ("legacy_route", "legacy", ("isinstance(picked, LegacyRoute)",)),
    ("to_run_request", "legacy", ("isinstance(picked, (Response, LegacyRoute))",)),
    ("to_run_request", "run", ()),
]


def _predicate(source):
    return ast.dump(ast.parse(source, mode="eval").body, include_attributes=False)


def _allowed_sites():
    return Counter((function, kind, tuple(map(_predicate, predicates)))
                   for function, kind, predicates in RETURN_SITES)


def _return_sites(source):
    """Small conservative dataflow census: constructors, local aliases, forwards.

    Function calls and assigned results propagate return kinds to a fixed point.
    A new helper or imported constructor alias therefore cannot hide a site.
    This is a syntax ratchet, not a proof of arbitrary dynamic Python behavior.
    """
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    functions = [node for node in ast.walk(tree) if isinstance(node, function_types)]
    constructors = {"LegacyRoute": {"legacy"}, "RunRequest": {"run"}}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in constructors:
                    constructors[alias.asname or alias.name] = constructors[alias.name]

    def local_nodes(function):
        def walk(node):
            yield node
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (*function_types, ast.ClassDef, ast.Lambda)):
                    yield from walk(child)
        return list(walk(function))

    nodes = {function: local_nodes(function) for function in functions}
    returns = {function.name: set() for function in functions}

    def kinds(node, variables):
        if isinstance(node, ast.Name):
            return variables.get(node.id, set())
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else "")
            return constructors.get(name, set()) | returns.get(name, set())
        if isinstance(node, ast.Await):
            return kinds(node.value, variables)
        if isinstance(node, ast.IfExp):
            return kinds(node.body, variables) | kinds(node.orelse, variables)
        return set()

    def variables_for(function):
        variables = {}
        changed = True
        while changed:
            changed = False
            for node in nodes[function]:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    value = kinds(node.value, variables)
                    old = variables.get(target.id, set())
                    if value - old:
                        variables[target.id] = old | value
                        changed = True
        return variables

    changed = True
    while changed:
        changed = False
        for function in functions:
            variables = variables_for(function)
            found = set().union(*(kinds(node.value, variables) for node in nodes[function]
                                  if isinstance(node, ast.Return)))
            if found - returns[function.name]:
                returns[function.name] |= found
                changed = True

    sites = []
    for function in functions:
        variables = variables_for(function)
        for node in nodes[function]:
            if not isinstance(node, ast.Return):
                continue
            predicates = []
            child = node
            while child is not function:
                parent = parents[child]
                if isinstance(parent, ast.If):
                    test = ast.unparse(parent.test)
                    predicates.append("not (" + test + ")" if child in parent.orelse else test)
                child = parent
            predicates.reverse()

            def collect(value, guards):
                if isinstance(value, ast.IfExp):
                    test = ast.unparse(value.test)
                    collect(value.body, [*guards, test])
                    collect(value.orelse, [*guards, "not (" + test + ")"])
                else:
                    sites.extend((function.name, kind, tuple(map(_predicate, guards)))
                                 for kind in sorted(kinds(value, variables)))

            collect(node.value, predicates)
    return Counter(sites)


def test_video_create_return_sites_can_only_shrink():
    found = _return_sites(SHAPE_SOURCE.read_text(encoding="utf-8"))
    assert not found - _allowed_sites(), found - _allowed_sites()


def test_return_site_census_sees_constructors_aliases_and_forwarded_values():
    source = """
from example import LegacyRoute as Old
def choose(flag):
    if flag:
        return Old("videos.create", {})
def forward(flag):
    picked = choose(flag)
    return picked if flag else None
def run():
    request = RunRequest()
    return request
"""
    assert _return_sites(source) == Counter({
        ("choose", "legacy", (_predicate("flag"),)): 1,
        ("forward", "legacy", (_predicate("flag"),)): 1,
        ("run", "run", ()): 1,
    })


@pytest.mark.parametrize("mutation", ["new-helper", "duplicate-site", "broader-guard"])
def test_return_site_ratchet_rejects_added_or_broadened_sites(mutation):
    source = SHAPE_SOURCE.read_text(encoding="utf-8")
    if mutation == "new-helper":
        source += '\nfrom example import LegacyRoute as Old\n' + (
            'def extra(body):\n    result = Old("videos.create", body)\n    return result\n')
    elif mutation == "duplicate-site":
        source = source.replace("return LegacyRoute(LEGACY_CREATE, body)",
                                "return LegacyRoute(LEGACY_CREATE, body)\n        return LegacyRoute(LEGACY_CREATE, body)", 1)
    else:
        source = source.replace("if manifest is None or manifest.chain or manifest.length is None:",
                                "if True:", 1)
    assert _return_sites(source) - _allowed_sites()


def test_return_site_ratchet_allows_removing_a_handoff():
    source = SHAPE_SOURCE.read_text(encoding="utf-8")
    removed = source.replace("return LegacyRoute(LEGACY_CREATE, body)", "return None", 1)
    assert removed != source
    assert not _return_sites(removed) - _allowed_sites()
