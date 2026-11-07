"""Binding descriptors (CODE_TO_DATA_S3, S3.3): the key names an adapter, a description closes the block, a role names
reviewed code, and an unknown binding or role never executes."""

from __future__ import annotations

import ast
import asyncio
import copy
import glob
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gateway import binding_descriptors as bd
from gateway import binding_registry
from gateway.definition_store import DefinitionUnavailable
from gateway.engine_protocol import description
from gateway.manifests import ContractErrors, ErrorCollector, validate_manifest
from gateway.pinned_definitions import PinnedDefinitions
from tests.support.access import access_context
from tests.support.config_snapshot import derived_patch
from tests.support.fake_upstream.async_client_stub import install_async_client_stub

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "manifests" / "workflows"
TILE = "flux2-klein-4b-seamless-tile"


def manifest_of(name: str) -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8")))


def places(manifest: dict[str, Any], **keywords: Any) -> list[tuple[tuple[Any, ...], str]]:
    errors = ErrorCollector()
    bd.collect_binding_errors(manifest, errors, **keywords)
    return [(error.path, error.code) for error in errors.errors]


def converted_mesh() -> dict[str, Any]:
    """The mesh manifest in the shape the schema 1 to 2 converter makes: the key names the adapter, the engine is a
    service reference, and the transport block is flat. Written here by hand, so this step does not need that branch."""
    manifest = manifest_of("mesh.generate.trellis-2")
    old_key, block = next(iter(manifest["bindings"].items()))
    manifest["bindings"] = {"http-multipart": {"service": old_key, "path": block["transport"]["path"], "map": block["map"]}}
    return manifest


# ----------------------------------------------------------------------------------------------- the descriptions

def test_the_shipped_descriptions_close_three_binding_keys() -> None:
    shipped = bd.shipped_binding_descriptors()
    assert set(shipped) == {"comfyui", "gateway", "http-multipart"}
    assert shipped["comfyui"].adapter_id == "node-graph" and shipped["comfyui"].inputs_via == "map"
    assert {key: d.check_targets for key, d in shipped.items()} == {"comfyui": "graph", "gateway": "graph", "http-multipart": "form"}
    assert shipped["gateway"].adapter_id == "gateway-compose" and shipped["gateway"].inputs_via == "role"
    assert shipped["http-multipart"].adapter_id == "http-multipart" and shipped["http-multipart"].inputs_via == "map"
    # The registry loader compiles the same blocks: a description that fails the schema does not load.
    loaded = description.load_registry(description.REGISTRY_DIR)
    assert loaded["gateway-compose"].binding_descriptor == shipped["gateway"]
    assert loaded["http-multipart"].binding_descriptor == shipped["http-multipart"]


def test_the_compositions_list_the_fields_their_code_reads() -> None:
    """The lists below are what the readers in proxy.py, identity_api.py, workflow_openapi.py and models_catalog.py read."""
    gateway = bd.shipped_binding_descriptors()["gateway"]
    assert gateway.role_field == "compose"
    assert {name for name in gateway.fields} == {"compose", "model_id", "route", "requires"}
    assert {role: set(fields) for role, fields in gateway.roles.items()} == {
        "identity_scene": {"workflow", "image_nodes", "prompt_node", "seed_node"},
        "offset_inpaint_tile": {"base_model", "inpaint_model", "map"},
        "face_likeness": set(),
    }


@pytest.mark.parametrize("change, message", [
    ({"binding_fields": {"x": {"kind": "code"}}}, "is not one of"),
    ({"binding_fields": {"x": {"kind": "text", "extra": 1}}}, "extra"),
    ({"binding_fields": {"x": {"kind": "enum"}}}, "needs a list of values"),
    ({"binding_fields": {"X": {"kind": "text"}}}, "does not match"),
    ({"roles": {"r": {}}}, "no field is of kind role"),
    ({"binding_fields": {"c": {"kind": "role"}}}, "needs roles"),
    ({"inputs_via": "role"}, "inputs_via role needs roles"),
    ({"inputs_via": "magic"}, "is not one of"),
    ({"executor": "http-v1"}, "only a reviewed adapter has a binding"),
])
def test_a_malformed_description_does_not_load(change: dict[str, Any], message: str) -> None:
    doc = {"protocol_version": 1, "id": "x", "executor": "adapter:x", "binding": "x", "operations": {"o": {"class": "generate"}},
           "binding_fields": {"a": {"kind": "text"}}}
    doc.update(change)
    with pytest.raises(Exception) as raised:
        description.parse_description(doc, source="x.yml")
    assert message in str(raised.value)


# --------------------------------------------------------------------------------------------------- refused at save

def test_an_unknown_binding_key_is_refused_with_its_path_and_code() -> None:
    manifest = manifest_of(TILE)
    manifest["bindings"]["nonesuch"] = {"anything": 1}
    assert places(manifest) == [(("bindings", "nonesuch"), "binding_unknown")]


def test_an_unknown_field_is_refused_with_its_path_and_code() -> None:
    manifest = manifest_of("flux2-klein-4b-edit")
    manifest["inputs"]["seed"] = {"type": "integer"}
    manifest["bindings"]["comfyui"]["surprise"] = True
    assert places(manifest) == [(("bindings", "comfyui", "surprise"), "binding_field_unknown")]


def test_a_field_of_another_role_is_an_unknown_field() -> None:
    manifest = manifest_of(TILE)
    manifest["bindings"]["gateway"]["prompt_node"] = "7"   # a field of identity_scene, not of this role
    assert places(manifest) == [(("bindings", "gateway", "prompt_node"), "binding_field_unknown")]


def test_an_unknown_role_is_refused_with_its_path_and_code() -> None:
    manifest = manifest_of(TILE)
    manifest["bindings"]["gateway"]["compose"] = "no_such_role"
    # The fields of an unknown role cannot be told, so the role is the one error and the fields are not a cascade.
    assert places(manifest) == [(("bindings", "gateway", "compose"), "role_unknown")]


def test_a_missing_required_field_is_named_at_the_binding() -> None:
    manifest = manifest_of("image-identity")
    del manifest["bindings"]["gateway"]["seed_node"]
    assert places(manifest) == [(("bindings", "gateway"), "required_field_missing")]


def test_save_refusals_arrive_through_the_collector_and_keep_the_old_first_message() -> None:
    manifest = manifest_of(TILE)
    manifest["bindings"]["gateway"]["compose"] = "no_such_role"
    manifest["bindings"]["nonesuch"] = {}
    validate_manifest(manifest)   # the loaders do not close bindings yet: S3.5 enforces
    with pytest.raises(ContractErrors) as raised:
        validate_manifest(manifest, binding_descriptors=bd.shipped_binding_descriptors())
    assert [(e.path, e.code) for e in raised.value.errors] == [
        (("bindings", "gateway", "compose"), "role_unknown"),
        (("bindings", "nonesuch"), "binding_unknown"),
    ]
    assert all("no_such_role" not in e.message and "nonesuch" not in e.message for e in raised.value.errors)   # a message holds no value


def test_binding_errors_come_after_every_other_error() -> None:
    manifest = manifest_of(TILE)
    manifest["bindings"]["nonesuch"] = {}
    del manifest["id"]
    with pytest.raises(ContractErrors) as raised:
        validate_manifest(manifest, binding_descriptors=bd.shipped_binding_descriptors())
    assert raised.value.primary.code == "required_field_missing"
    # `errors` is sorted by path; `primary` is the first found. Both the earlier error and the binding error are kept.
    assert [(e.path, e.code) for e in raised.value.errors] == [
        (("bindings", "nonesuch"), "binding_unknown"), (("id",), "required_field_missing"),
    ]
    assert raised.value.primary.path == ("id",)
    assert type(raised.value) is ContractErrors   # not a selector error: no selector was at fault


@pytest.mark.parametrize("field, value, code", [
    ("workflow", "../up.json", "template_invalid"),
    ("workflow", "/abs.json", "template_invalid"),
    ("workflow", 3, "template_invalid"),
    ("mask_to_source_alpha", "yes", "type_mismatch"),
    ("edge_only_inputs", "prompt", "type_mismatch"),
    ("edge_only_inputs", ["nonesuch"], "reference_unknown"),
    ("map", [], "type_mismatch"),
    ("selectors", "x", "type_mismatch"),
])
def test_core_checks_a_field_by_its_kind(field: str, value: Any, code: str) -> None:
    manifest = manifest_of("flux2-klein-4b-edit")
    manifest["inputs"]["seed"] = {"type": "integer"}
    manifest["bindings"]["comfyui"][field] = value
    found = places(manifest)
    assert any(path == ("bindings", "comfyui", field) or path[:3] == ("bindings", "comfyui", field) for path, got in found if got == code), found


def test_a_target_list_is_bounded() -> None:
    manifest = manifest_of("image-identity")
    manifest["bindings"]["gateway"]["image_nodes"] = [str(index) for index in range(40)]
    assert places(manifest) == [(("bindings", "gateway", "image_nodes"), "list_too_long")]


# ------------------------------------------------------------------------------------------------- bundled bindings

# Where a bundled document does not pass yet, and why. S3.5 rewrites these files; this list names the differences.
NAMED_DIFFERENCES = {
    "flux2-klein-4b-edit": [(("bindings", "comfyui", "map", "seed"), "reference_unknown")],   # maps a `seed` it does not declare
    "flux2-klein-edit": [(("bindings", "comfyui", "map", "seed"), "reference_unknown")],
    "ace-step-1.5-xl-turbo": [(("bindings", "comfyui", "map", "tags", 0), "target_duplicate")],   # `prompt` and `tags` both write node 5
    "mesh.generate.trellis-2": [(("bindings", "trellis"), "binding_unknown")],   # the key is a service id until S3.5
}


def bundled_templates(manifest: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, block in manifest["bindings"].items():
        if isinstance(block.get("workflow"), str):
            out[key] = json.loads((ROOT / block["workflow"]).read_text(encoding="utf-8"))
    return out


def test_every_bundled_binding_passes_but_the_named_differences() -> None:
    seen = {}
    for path in sorted(glob.glob(str(WORKFLOWS / "*.yml"))):
        manifest = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        found = places(manifest, templates=bundled_templates(manifest))
        if found:
            seen[Path(path).stem] = found
    assert seen == NAMED_DIFFERENCES


def test_the_converted_mesh_binding_passes() -> None:
    assert places(converted_mesh(), services={"trellis"}) == []


def test_a_service_field_names_an_engine_record_when_the_records_are_given() -> None:
    manifest = converted_mesh()
    assert places(manifest, services={"other"}) == [(("bindings", "http-multipart", "service"), "service_unknown")]
    assert places(manifest) == []   # without the records, the field is not resolved


def test_the_multipart_target_check_keeps_the_rules_the_mesh_validator_had() -> None:
    manifest = converted_mesh()
    manifest["inputs"]["extras"] = {"type": "object", "opaque": True}
    block = manifest["bindings"]["http-multipart"]
    block["map"] = {"image": "image", "seed": "image", "resolution": "Bad-Name", "extras": "extras"}
    assert places(manifest) == [
        (("bindings", "http-multipart", "map", "seed"), "target_duplicate"),
        (("bindings", "http-multipart", "map", "resolution"), "name_invalid"),
        (("bindings", "http-multipart", "map", "extras"), "input_not_mappable"),
    ]
    block["map"] = {"image": "image", "ghost": "ghost"}
    assert places(manifest) == [(("bindings", "http-multipart", "map", "ghost"), "reference_unknown")]
    block["map"] = {"image": "image"}
    block["path"] = "/a/../b"
    assert places(manifest) == [(("bindings", "http-multipart", "path"), "relative_path_invalid")]


def test_the_graph_target_check_reads_the_template_when_it_is_given() -> None:
    manifest = manifest_of("image-identity")
    graph = bundled_templates(manifest)["gateway"]
    manifest["bindings"]["gateway"]["prompt_node"] = "999"
    assert places(manifest) == []   # no template given: the part that needs it is skipped
    assert places(manifest, templates={"gateway": graph}) == [(("bindings", "gateway", "prompt_node"), "target_missing")]
    comfy = manifest_of("flux2-klein-4b-edit")
    comfy["inputs"]["seed"] = {"type": "integer"}
    comfy["bindings"]["comfyui"]["map"]["prompt"] = [{"node": "999", "input": "text"}, {"node": "x"}, {"path": ""}]
    found = places(comfy, templates=bundled_templates(comfy))
    assert [code for _, code in found] == ["target_missing", "mapping_incomplete", "mapping_incomplete"]


def test_an_adapter_with_no_registered_target_check_is_unavailable_at_save() -> None:
    saved = binding_registry._TARGET_CHECKS.pop("form")
    try:
        assert places(converted_mesh()) == [(("bindings", "http-multipart"), "binding_unavailable")]
    finally:
        binding_registry.register_target_check("form", saved)


# ------------------------------------------------------------------------------------------------------ at run

def test_every_listed_role_has_registered_code() -> None:
    import proxy
    from gateway import identity_scene, likeness

    expected = {
        "identity_scene": (binding_registry.Entry.EDIT, identity_scene.run_identity_scene),
        "offset_inpaint_tile": (binding_registry.Entry.GENERATION, proxy._run_offset_inpaint_tile_role),
        "face_likeness": (binding_registry.Entry.ANALYSIS, likeness.check_likeness),
    }
    listed = {role for _key, role in bd.listed_roles()}
    assert listed == set(expected)
    for role, (entry, run) in expected.items():
        found = binding_registry.role_implementation(role, entry)
        assert found is not None and found.serves is entry and found.run is run, role
        assert bd.resolve_role("gateway", {"compose": role}, entry).run is run   # type: ignore[union-attr]


def tile_body() -> dict[str, Any]:
    return {"model": TILE, "prompt": "painted rock texture", "size": "64x64", "n": 1, "response_format": "b64_json"}


class JsonRequest:
    headers: dict[str, str] = {}
    url = SimpleNamespace(scheme="http", netloc="gateway.test")

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def run_tile_route(proxy: Any, counts: dict[str, int]) -> Any:
    async def counting_post(*_a: Any, **_k: Any) -> Any:
        counts["posts"] += 1
        raise AssertionError("an engine was called")

    async def counting_round(*_a: Any, **_k: Any) -> Any:
        counts["rounds"] += 1
        raise AssertionError("an engine was called")

    receipt = proxy.start_image_route_receipt("/v1/images/generations", access_context())
    with pytest.MonkeyPatch.context() as mp, derived_patch("COMFY_ENABLED", True), patch.object(
        proxy, "configured_downstream_headers", lambda _auth, _service: {}
    ), patch.object(proxy, "_graph_run_round", counting_round):
        install_async_client_stub(mp, counting_post)
        return asyncio.run(proxy.proxy_graph_image_request(JsonRequest(tile_body()), access_context(), receipt=receipt))


def test_a_listed_role_with_no_registered_code_fails_binding_unavailable_with_no_engine_call() -> None:
    import proxy

    counts = {"posts": 0, "rounds": 0}
    removed = binding_registry.unregister_role("offset_inpaint_tile")
    assert removed is not None
    try:
        response = run_tile_route(proxy, counts)
    finally:
        binding_registry.register_role(removed.role, removed.serves, removed.run)
    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "binding_unavailable"
    assert counts == {"posts": 0, "rounds": 0}


def test_a_role_registered_for_another_route_does_not_run_here() -> None:
    import proxy

    counts = {"posts": 0, "rounds": 0}
    removed = binding_registry.unregister_role("offset_inpaint_tile")
    assert removed is not None
    binding_registry.register_role(removed.role, binding_registry.Entry.EDIT, removed.run)
    try:
        response = run_tile_route(proxy, counts)
    finally:
        binding_registry.register_role(removed.role, removed.serves, removed.run)
    assert json.loads(response.body)["error"]["code"] == "binding_unavailable"
    assert counts == {"posts": 0, "rounds": 0}


def test_the_identity_route_refuses_an_unregistered_role_before_any_engine_work() -> None:
    from fastapi import FastAPI, Response
    from fastapi.testclient import TestClient

    from gateway import identity_api

    calls: list[str] = []

    async def ensure() -> Response | None:
        calls.append("face service started")
        return None

    async def run_workflow(*_args: Any) -> bytes:
        calls.append("engine called")
        return b""

    class Faces:
        async def analyze(self, *_args: Any) -> list[dict[str, Any]]:
            calls.append("faces analyzed")
            return []

    binding = {"compose": "identity_scene", "image_nodes": ["1"], "prompt_node": "2", "seed_node": "3"}
    app = FastAPI()
    identity_api.register_identity_routes(app, identity_api.IdentityApiDeps(
        authenticate=lambda _request, _path: "auth", enforce_policy=lambda _auth, _path: None, ensure_face_service=ensure,
        face_client=lambda: Faces(), run_workflow=run_workflow, template=lambda: ({}, binding),
        store_image=lambda *_args: {},
    ))
    client = TestClient(app)

    def post() -> Any:
        return client.post(identity_api.IDENTITY_PATH, files=[("image", ("p.png", b"\x89PNG", "image/png"))], data={"prompt": "x"})

    removed = binding_registry.unregister_role("identity_scene")
    assert removed is not None
    try:
        response = post()
    finally:
        binding_registry.register_role(removed.role, removed.serves, removed.run)
    assert response.status_code == 503 and response.json()["error"]["code"] == "binding_unavailable"
    assert calls == []
    post()   # with the role registered, the same request gets past the check and reaches the face service
    assert calls and calls[0] == "face service started"


def pinned(binding: dict[str, Any], contract: dict[str, Any]) -> PinnedDefinitions:
    return PinnedDefinitions(envelope=SimpleNamespace(source_id="job-1"), contract=contract, binding=binding, template=None)  # type: ignore[arg-type]


def test_a_pinned_job_whose_role_was_removed_fails_definition_unavailable() -> None:
    import proxy  # noqa: F401

    manifest = manifest_of(TILE)
    contract = {key: manifest[key] for key in ("schema_version", "id", "intent", "inputs", "outputs")}
    job = pinned(manifest["bindings"]["gateway"], contract)
    assert job.manifest("gateway")["id"] == manifest["id"]
    removed = binding_registry.unregister_role("offset_inpaint_tile")
    assert removed is not None
    try:
        with pytest.raises(DefinitionUnavailable):
            job.manifest("gateway")
    finally:
        binding_registry.register_role(removed.role, removed.serves, removed.run)
    assert job.manifest("gateway")["id"] == manifest["id"]


def test_a_pinned_job_with_no_role_field_is_not_affected() -> None:
    manifest = manifest_of("flux2-klein-4b-edit")
    contract = {key: manifest[key] for key in ("schema_version", "id", "intent", "inputs", "outputs")}
    assert pinned(manifest["bindings"]["comfyui"], contract).manifest("comfyui")["id"] == manifest["id"]


# ---------------------------------------------------------------------------------------------------- the dispatch

def compose_dispatch() -> tuple[ast.If, ast.AST]:
    """The ``if`` that dispatches on the gateway binding's ``compose`` in the image generate route, and its function."""
    tree = ast.parse((ROOT / "proxy.py").read_text(encoding="utf-8"))
    for function in ast.walk(tree):
        if not isinstance(function, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) \
                    and node.test.left.id == "gateway_binding" and isinstance(node.test.ops[0], ast.IsNot):
                return node, function
    raise AssertionError("the compose dispatch is not where this test reads it")


def test_the_compose_dispatch_reads_the_role_registry_and_has_no_fallback_branch() -> None:
    dispatch, function = compose_dispatch()
    # No second way out: the `if` has no else, and nothing in the route compares `compose` to a role name.
    assert dispatch.orelse == [], "the compose dispatch has an else branch"
    names = [node for node in ast.walk(function) if isinstance(node, ast.Compare)
             and any(isinstance(part, ast.Name) and part.id in ("gateway_binding", "role") for part in [node.left, *node.comparators])
             and any(isinstance(part, ast.Constant) and isinstance(part.value, str) for part in [node.left, *node.comparators])]
    assert names == [], "the route names a role"
    # The block looks the role up in the registry, and a missing role is refused by `_binding_unavailable_response`, only.
    lookups = [node for node in ast.walk(dispatch) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "resolve_role"]
    assert len(lookups) == 1
    refusals = [node for node in dispatch.body if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "role"]
    assert len(refusals) == 1
    (refusal,) = refusals
    assert refusal.orelse == []
    assert len(refusal.body) == 1 and isinstance(refusal.body[0], ast.Return)
    call = refusal.body[0].value
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_binding_unavailable_response"
    # The dispatch ends in a return: control never falls through to the graph path with a role set.
    assert isinstance(dispatch.body[-1], ast.Return)


# ------------------------------------------------------------------------- one resolver for every dispatcher

COMPOSE_CASES = [
    pytest.param(None, id="missing"), pytest.param("", id="empty"), pytest.param(5, id="not-text"),
    pytest.param(["identity_scene"], id="list"), pytest.param("unlisted_role", id="not-listed-by-the-description"),
]


def with_compose(binding: dict[str, Any], compose: Any) -> dict[str, Any]:
    out = {key: value for key, value in binding.items() if key != "compose"}
    if compose is not None:
        out["compose"] = compose
    return out


@pytest.mark.parametrize("compose", COMPOSE_CASES)
def test_resolve_role_wants_a_present_text_role_the_description_lists(compose: Any) -> None:
    import proxy  # noqa: F401  (registers the roles)

    ran: list[str] = []
    binding_registry.register_role("unlisted_role", binding_registry.Entry.GENERATION, lambda **_k: ran.append("ran"))
    try:
        assert bd.resolve_role("gateway", {"compose": compose} if compose is not None else {}, binding_registry.Entry.GENERATION) is None
    finally:
        binding_registry.unregister_role("unlisted_role")
    assert ran == []


def test_resolve_role_finds_nothing_for_an_unknown_binding_key_or_a_binding_with_no_role_field() -> None:
    assert bd.resolve_role("nonesuch", {"compose": "identity_scene"}, binding_registry.Entry.EDIT) is None
    assert bd.resolve_role("comfyui", {"compose": "identity_scene"}, binding_registry.Entry.EDIT) is None
    assert bd.resolve_role("gateway", {"compose": "identity_scene"}, binding_registry.Entry.GENERATION) is None   # other entry


@pytest.mark.parametrize("compose", COMPOSE_CASES)
def test_the_generation_route_refuses_a_compose_it_cannot_resolve_before_any_effect(compose: Any) -> None:
    import proxy

    ran: list[str] = []

    async def unlisted(**_keywords: Any) -> Any:
        ran.append("ran")

    binding_registry.register_role("unlisted_role", binding_registry.Entry.GENERATION, unlisted)
    manifest = manifest_of(TILE)
    manifest["bindings"]["gateway"] = with_compose(manifest["bindings"]["gateway"], compose)
    counts = {"posts": 0, "rounds": 0}
    try:
        with patch.object(proxy, "_find_image_manifest", lambda *_a, **_k: manifest):
            response = run_tile_route(proxy, counts)
    finally:
        binding_registry.unregister_role("unlisted_role")
    assert response.status_code == 503 and json.loads(response.body)["error"]["code"] == "binding_unavailable"
    assert ran == [] and counts == {"posts": 0, "rounds": 0}


class IdentityHarness:
    """The identity routes over doubles that record every effect. ``calls`` stays empty when a request is refused first."""

    def __init__(self, binding: dict[str, Any] | None = None) -> None:
        from fastapi import FastAPI, Response
        from fastapi.testclient import TestClient

        from gateway import identity_api

        self.api = identity_api
        self.calls: list[str] = []
        harness = self

        async def ensure() -> Response | None:
            harness.calls.append("face service started")
            return None

        async def run_workflow(*_args: Any) -> bytes:
            harness.calls.append("engine called")
            return b""

        async def submit_job(*_args: Any) -> Response:
            harness.calls.append("job enqueued")
            return Response(status_code=202)

        class Faces:
            async def analyze(self, *_args: Any) -> list[dict[str, Any]]:
                harness.calls.append("faces analyzed")
                return []

        self.binding = binding or {"compose": "identity_scene", "image_nodes": ["1"], "prompt_node": "2", "seed_node": "3"}
        app = FastAPI()
        identity_api.register_identity_routes(app, identity_api.IdentityApiDeps(
            authenticate=lambda _request, _path: "auth", enforce_policy=lambda _auth, _path: None, ensure_face_service=ensure,
            face_client=lambda: Faces(), run_workflow=run_workflow, template=lambda: ({}, self.binding),
            store_image=lambda *_args: {}, submit_job=submit_job,
        ))
        self.client = TestClient(app)

    def identity(self, **data: str) -> Any:
        return self.client.post(self.api.IDENTITY_PATH, files=[("image", ("p.png", b"\x89PNG", "image/png"))],
                                data={"prompt": "x", **data})

    def likeness(self) -> Any:
        png = ("p.png", b"\x89PNG", "image/png")
        return self.client.post(self.api.LIKENESS_PATH, files=[("reference", png), ("candidate", png)])


def without_role(role: str) -> Any:
    removed = binding_registry.unregister_role(role)
    assert removed is not None

    class Restore:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_exc: Any) -> None:
            binding_registry.register_role(removed.role, removed.serves, removed.run)

    return Restore()


@pytest.mark.parametrize("mode", ["async", "sync"])
def test_both_identity_paths_refuse_an_unregistered_role_before_any_effect(mode: str) -> None:
    harness = IdentityHarness()
    with without_role("identity_scene"):
        response = harness.identity(mode=mode)
    assert response.status_code == 503 and response.json()["error"]["code"] == "binding_unavailable"
    assert harness.calls == []


@pytest.mark.parametrize("mode", ["async", "sync"])
@pytest.mark.parametrize("compose", COMPOSE_CASES)
def test_both_identity_paths_refuse_a_compose_that_does_not_resolve(mode: str, compose: Any) -> None:
    harness = IdentityHarness({**with_compose({"image_nodes": ["1"], "prompt_node": "2", "seed_node": "3"}, compose)})
    binding_registry.register_role("unlisted_role", binding_registry.Entry.EDIT, lambda *_a, **_k: harness.calls.append("ran"))
    try:
        response = harness.identity(mode=mode)
    finally:
        binding_registry.unregister_role("unlisted_role")
    assert response.status_code == 503 and response.json()["error"]["code"] == "binding_unavailable"
    assert harness.calls == []


def test_the_identity_route_runs_the_registered_implementation_not_a_fixed_function() -> None:
    from gateway import identity_scene
    from gateway.identity_scene import IdentityError

    harness = IdentityHarness()
    seen: list[dict[str, Any]] = []

    async def replacement(req: Any, deps: Any, **keywords: Any) -> Any:
        seen.append(keywords)
        raise IdentityError("the replacement ran", param="image")

    original = binding_registry.role_implementation("identity_scene", binding_registry.Entry.EDIT)
    assert original is not None and original.run is identity_scene.run_identity_scene
    binding_registry.register_role("identity_scene", binding_registry.Entry.EDIT, replacement)
    try:
        with patch.object(identity_scene, "run_identity_scene", side_effect=AssertionError("the fixed function was called")):
            response = harness.identity(mode="sync")
    finally:
        binding_registry.register_role(original.role, original.serves, original.run)
    assert response.status_code == 400 and "the replacement ran" in response.json()["error"]["message"]
    assert seen and seen[0]["prompt_node"] == "2"


def test_the_likeness_route_needs_its_role_and_runs_the_registered_implementation() -> None:
    from gateway import identity_scene, likeness

    harness = IdentityHarness()
    with without_role("face_likeness"):
        response = harness.likeness()
    assert response.status_code == 503 and response.json()["error"]["code"] == "binding_unavailable"
    assert harness.calls == []

    async def no_people(*_a: Any, **_k: Any) -> list[Any]:
        return []

    original = binding_registry.role_implementation("face_likeness", binding_registry.Entry.ANALYSIS)
    assert original is not None
    binding_registry.register_role("face_likeness", binding_registry.Entry.ANALYSIS, lambda *_a, **_k: {"replacement": True})
    try:
        with patch.object(identity_scene, "resolve_people", no_people), patch.object(identity_scene, "_faces", no_people), \
                patch.object(likeness, "check_likeness", side_effect=AssertionError("the fixed function was called")):
            response = harness.likeness()
    finally:
        binding_registry.register_role(original.role, original.serves, original.run)
    assert response.status_code == 200 and response.json()["replacement"] is True


# ---------------------------------------------------------------------------------------- graph targets, strictly

def comfy_places(map_value: Any, template: Any) -> list[tuple[tuple[Any, ...], str]]:
    manifest = manifest_of("flux2-klein-4b-edit")
    manifest["inputs"]["seed"] = {"type": "integer"}
    manifest["bindings"]["comfyui"]["map"] = map_value
    return places(manifest, templates={"comfyui": template})


GRAPH = {"1": {"inputs": {"text": "", "seed": 0}}, "2": {"inputs": {"image": ""}}}
MAP_AT = ("bindings", "comfyui", "map")


def test_a_dotted_path_must_start_at_a_node_of_the_supplied_graph() -> None:
    assert comfy_places({"prompt": [{"path": "999.inputs.text"}]}, GRAPH) == [(MAP_AT + ("prompt", 0, "path"), "target_missing")]
    assert comfy_places({"prompt": [{"path": "1.inputs.text"}]}, GRAPH) == []
    assert comfy_places({"prompt": [{"path": "1.inputs.text.deeper"}]}, GRAPH) == [(MAP_AT + ("prompt", 0, "path"), "path_invalid")]
    assert comfy_places({"prompt": [{"path": "1..text"}]}, GRAPH) == [(MAP_AT + ("prompt", 0, "path"), "path_invalid")]
    assert comfy_places({"prompt": [{"path": "999.inputs.text"}]}, None) == []   # no template: deferred, as before


def test_a_node_list_may_not_repeat_a_node() -> None:
    manifest = manifest_of("image-identity")
    manifest["bindings"]["gateway"]["image_nodes"] = ["1", "1"]
    graph = bundled_templates(manifest)["gateway"]
    node = next(iter(graph))
    manifest["bindings"]["gateway"]["image_nodes"] = [node, node]
    assert places(manifest, templates={"gateway": graph}) == [(("bindings", "gateway", "image_nodes", 1), "target_duplicate")]
    assert places(manifest) == [(("bindings", "gateway", "image_nodes", 1), "target_duplicate")]   # needs no template


def test_two_inputs_may_not_write_one_node_and_input() -> None:
    two_inputs = {"prompt": [{"node": "1", "input": "text"}], "seed": [{"node": "1", "input": "text"}]}
    assert comfy_places(two_inputs, GRAPH) == [(MAP_AT + ("seed", 0), "target_duplicate")]
    by_path = {"prompt": [{"node": "1", "input": "text"}], "seed": [{"path": "1.inputs.text"}]}
    assert comfy_places(by_path, GRAPH) == [(MAP_AT + ("seed", 0), "target_duplicate")]
    assert comfy_places(two_inputs, None) == [(MAP_AT + ("seed", 0), "target_duplicate")]   # needs no template
    assert comfy_places({"prompt": [{"node": "1", "input": "text"}], "seed": [{"node": "1", "input": "seed"}]}, GRAPH) == []


@pytest.mark.parametrize("template, expected", [
    ({"1": {"inputs": 7}}, [(MAP_AT + ("prompt", 0, "node"), "template_node_invalid")]),
    ({"1": {"inputs": []}}, [(MAP_AT + ("prompt", 0, "node"), "template_node_invalid")]),
    ({"1": 7}, [(MAP_AT + ("prompt", 0, "node"), "template_node_invalid")]),
    ({"1": None}, [(MAP_AT + ("prompt", 0, "node"), "template_node_invalid")]),
    ({"1": {}}, [(MAP_AT + ("prompt", 0, "input"), "target_missing")]),
])
def test_a_malformed_template_node_is_an_error_not_a_crash(template: Any, expected: list[Any]) -> None:
    assert comfy_places({"prompt": [{"node": "1", "input": "text"}]}, template) == expected
    assert comfy_places({"prompt": [{"path": "1.inputs.text"}]}, {"1": 7}) == [(MAP_AT + ("prompt", 0, "path"), "template_node_invalid")]


@pytest.mark.parametrize("template", [7, ["1"], "1"])
@pytest.mark.parametrize("target", [{"path": "1.inputs.text"}, {"node": "1", "input": "text"}])
def test_a_template_that_is_not_a_mapping_of_nodes_is_an_error_not_a_crash(template: Any, target: Any) -> None:
    assert comfy_places({"prompt": [target]}, template) == [(MAP_AT, "template_node_invalid")]


def test_a_template_that_is_not_a_mapping_still_lets_the_duplicate_check_run() -> None:
    twice = {"prompt": [{"node": "1", "input": "text"}], "seed": [{"node": "1", "input": "text"}]}
    assert comfy_places(twice, 7) == [(MAP_AT, "template_node_invalid"), (MAP_AT + ("seed", 0), "target_duplicate")]


def test_a_target_node_that_is_not_a_mapping_is_an_error_for_a_single_target_too() -> None:
    manifest = manifest_of("image-identity")
    graph = bundled_templates(manifest)["gateway"]
    graph[manifest["bindings"]["gateway"]["prompt_node"]] = 7
    assert places(manifest, templates={"gateway": graph}) == [(("bindings", "gateway", "prompt_node"), "template_node_invalid")]


# ----------------------------------------------------------------------------- the shipped reader is the validated one

def shipped_copy(tmp_path: Path, edit: Any) -> Path:
    directory = tmp_path / "protocols"
    directory.mkdir()
    for source in (ROOT / "registry" / "protocols").glob("*.yml"):
        text = source.read_text(encoding="utf-8")
        (directory / source.name).write_text(edit(source.name, text), encoding="utf-8")
    return directory


@pytest.mark.parametrize("old, new", [
    ("  face_likeness:\n    fields: {}\n", "  face_likeness:\n    fields: []\n"),
    ("  face_likeness:\n    fields: {}\n", "  face_likeness:\n    fields: {}\n    surprise: 1\n"),
    ("  face_likeness:\n    fields: {}\n", "  face_likeness: []\n"),
])
def test_the_shipped_reader_refuses_a_role_body_the_schema_refuses(tmp_path: Path, old: str, new: str) -> None:
    directory = shipped_copy(tmp_path, lambda name, text: text.replace(old, new) if name == "gateway-compose.yml" else text)
    with pytest.raises(bd.BindingDescriptorError, match="gateway-compose.yml"):
        bd.shipped_binding_descriptors(directory)
    assert set(bd.shipped_binding_descriptors()) == {"comfyui", "gateway", "http-multipart"}


def test_a_falsey_malformed_role_fields_value_is_not_an_empty_mapping() -> None:
    for fields in ([], "", 0, None, False):
        with pytest.raises(bd.BindingDescriptorError):
            bd.parse_binding_descriptor(
                binding="b", executor="adapter:x", binding_fields={"c": {"kind": "role"}}, roles={"r": {"fields": fields}})
    assert bd.parse_binding_descriptor(
        binding="b", executor="adapter:x", binding_fields={"c": {"kind": "role"}}, roles={"r": {}}).roles == {"r": {}}
