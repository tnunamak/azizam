"""A corpus of bad documents for the contract error type (CODE_TO_DATA_S3, S3.1).

Each case builds one bad document and the call that checks it. ``FIRST_MESSAGES`` holds the message the check raised
before the error type existed: captured from the code as it was, it is the oracle for ``str()`` of the raised error.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

import yaml

from gateway import output_selectors as sel
from gateway.manifests import apply_deployment_inputs, bind_backend_request, validate_manifest
from gateway.mesh_manifest import validate_mesh_manifest
from gateway.workflow_requirements import validate_workflow_requirements

ROOT = Path(__file__).resolve().parent.parent
MESH_MANIFEST = ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml"

DESCRIPTOR_DOC: dict[str, Any] = {
    "version": 1,
    "identity": ["node", "key", "from"],
    "template_node": "node",
    "kind_field": "decode",
    "fields": {
        "node": {"type": "text", "required": True},
        "key": {"type": "enum", "required": True, "values": ["files", "others"]},
        "from": {"type": "enum", "required": True, "values": ["out_dir"]},
        "decode": {"type": "enum", "default": "none", "values": ["none", "json"], "kinds": {"none": "assets", "json": "json"}},
        "take": {"type": "enum", "default": "all", "values": ["all", "first", "single"]},
    },
}
TEMPLATE = {"1": {"class_type": "A", "inputs": {"text": "hi"}}, "9": {"class_type": "B", "inputs": {"in": ["1", 0]}}}


def descriptor() -> sel.SelectorDescriptor:
    return sel.parse_descriptor(copy.deepcopy(DESCRIPTOR_DOC))


def selector(**changes: Any) -> dict[str, str]:
    return {"node": "9", "key": "files", "from": "out_dir", **changes}


def base_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1, "id": "x.y", "intent": "x.y",
        "inputs": {"prompt": {"type": "string", "required": True}, "count": {"type": "integer", "default": 1}},
        "outputs": {"first": {"media_type": "a/b"}},
        "bindings": {"eng": {"workflow": "w.json", "map": {"prompt": [{"path": "a.b"}]}, "selectors": {"first": selector()}}},
    }


def edited(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = base_manifest()
    change(doc)
    return doc


def mesh_doc(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = copy.deepcopy(yaml.safe_load(MESH_MANIFEST.read_text(encoding="utf-8")))
    change(doc)
    return doc


def requirements_doc(requires: Any) -> dict[str, Any]:
    return {"id": "x.y", "requires": requires}


def check_manifest(doc: Any) -> Callable[[], Any]:
    return lambda: validate_manifest(doc, {"eng": descriptor()})


def check_binding(outputs: Any, block: Any, **kw: Any) -> Callable[[], Any]:
    return lambda: sel.check_binding(descriptor(), outputs, block, **kw)


def check_save(doc: Any, binding_key: str = "eng", parsed: Any = "default", template: Any = TEMPLATE) -> Callable[[], Any]:
    return lambda: sel.check_for_save(descriptor() if parsed == "default" else parsed, doc, binding_key, template=template)


def check_requirements(requires: Any) -> Callable[[], Any]:
    return lambda: validate_workflow_requirements(requirements_doc(requires), {})


def check_mesh(doc: Any) -> Callable[[], Any]:
    return lambda: validate_mesh_manifest(doc)


def check_narrowing(narrowing: Any) -> Callable[[], Any]:
    return lambda: apply_deployment_inputs(
        {"schema_version": 1, "id": "x.y", "intent": "x.y",
         "inputs": {"count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}, "prompt": {"type": "string"}},
         "outputs": {}, "bindings": {}}, narrowing)


def check_bind(binding: Any, backend: str = "eng", request: Any = None) -> Callable[[], Any]:
    doc = {"schema_version": 1, "id": "x.y", "intent": "x.y", "inputs": {"prompt": {"type": "string"}},
           "outputs": {}, "bindings": {"eng": binding}}
    return lambda: bind_backend_request(doc, backend, request or {}, {"prompt": "hi"})


def _set(path: tuple[Any, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(doc: dict[str, Any]) -> None:
        cursor = doc
        for part in path[:-1]:
            cursor = cursor[part]
        cursor[path[-1]] = value
    return apply


def _drop(*keys: str) -> Callable[[dict[str, Any]], None]:
    return lambda doc: [doc.pop(key) for key in keys]


def _both(*changes: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def apply(doc: dict[str, Any]) -> None:
        for change in changes:
            change(doc)
    return apply


def _two_unreal_nodes(doc: dict[str, Any]) -> None:
    doc["outputs"] = {"first": {"media_type": "a/b"}, "second": {"media_type": "c/d"}}
    doc["bindings"]["eng"]["selectors"] = {"first": selector(node="7"), "second": selector(node="8", key="others")}


def _rename_output(name: str) -> Callable[[dict[str, Any]], None]:
    def apply(doc: dict[str, Any]) -> None:
        doc["outputs"] = {name: {"media_type": "a/b"}}
        doc["bindings"]["eng"]["selectors"] = {name: selector()}
    return apply


BAD_INPUT = _set(("inputs", "prompt", "label"), "")
BAD_BINDING = _set(("bindings", "eng", "edge_only_inputs"), ["nothing"])
BAD_SELECTOR = _set(("bindings", "eng", "selectors", "first", "key"), "unlisted")
TWO_OUTPUTS = {"first": {}, "second": {}}

# case id -> (call, ...). The ids that start with ``multi`` hold more than one error on purpose.
CASES: dict[str, Callable[[], Any]] = {
    # validate_manifest
    "manifest-missing-field": check_manifest(edited(_drop("intent"))),
    "manifest-missing-two-fields": check_manifest(edited(_drop("intent", "outputs"))),
    "manifest-inputs-not-mapping": check_manifest(edited(_set(("inputs",), []))),
    "manifest-bindings-not-mapping": check_manifest(edited(_set(("bindings",), []))),
    "manifest-output-name": check_manifest(edited(_rename_output("bad name"))),
    "manifest-purpose-empty": check_manifest(edited(_set(("purpose",), " "))),
    "manifest-purpose-long": check_manifest(edited(_set(("purpose",), "one two three four five six seven eight nine"))),
    "manifest-summary-empty": check_manifest(edited(_set(("summary",), ""))),
    "manifest-summary-long": check_manifest(edited(_set(("summary",), " ".join(["word"] * 41)))),
    "manifest-license-type": check_manifest(edited(_set(("license",), 3))),
    "manifest-input-label": check_manifest(edited(BAD_INPUT)),
    "manifest-input-help": check_manifest(edited(_set(("inputs", "prompt", "help"), 7))),
    "manifest-input-default-nan": check_manifest(edited(_set(("inputs", "count"), {"type": "number", "default": float("nan")}))),
    "manifest-edge-only": check_manifest(edited(BAD_BINDING)),
    "manifest-selector-key": check_manifest(edited(BAD_SELECTOR)),
    "manifest-selector-no-adapter-form": lambda: validate_manifest(edited(lambda d: None), {}),
    # first message must stay the input's even when a binding is also bad
    "multi-input-and-binding": check_manifest(edited(_both(BAD_BINDING, BAD_INPUT))),
    "multi-input-binding-selector": check_manifest(edited(_both(BAD_SELECTOR, BAD_BINDING, BAD_INPUT))),
    # the primary is the first found, not the first by path: "bindings" sorts before "inputs"
    "multi-two-inputs": check_manifest(edited(_both(_set(("inputs", "prompt", "label"), ""), _set(("inputs", "count", "help"), "")))),
    "multi-missing-and-purpose": check_manifest(edited(_both(_drop("intent"), _set(("purpose",), "")))),
    "multi-binding-then-selector": check_manifest(edited(_both(BAD_SELECTOR, BAD_BINDING))),
    # selectors
    "binding-not-mapping": check_binding({"first": {}}, {"selectors": []}),
    "binding-empty": check_binding({"first": {}}, {"selectors": {}}),
    "binding-too-many": check_binding({"a": {}, "b": {}, "c": {}, "d": {}}, {"selectors": {k: selector(node=str(i)) for i, k in enumerate("abcd")}}, max_selectors=3),
    "binding-unknown-result": check_binding({"first": {}}, {"selectors": {"first": selector(), "ghost": selector(node="3")}}),
    "binding-missing-result": check_binding({"first": {}, "second": {}}, {"selectors": {"first": selector()}}),
    "binding-selector-not-mapping": check_binding({"first": {}}, {"selectors": {"first": "x"}}),
    "binding-selector-unknown-field": check_binding({"first": {}}, {"selectors": {"first": selector(extra="x")}}),
    "binding-selector-needs-field": check_binding({"first": {}}, {"selectors": {"first": {"node": "9", "from": "out_dir"}}}),
    "binding-selector-long": check_binding({"first": {}}, {"selectors": {"first": selector(node="n" * 200)}}),
    "binding-selector-enum": check_binding({"first": {}}, {"selectors": {"first": selector(key="unlisted")}}),
    "binding-duplicate": check_binding({"a": {}, "b": {}}, {"selectors": {"a": selector(), "b": selector()}}),
    "multi-binding-two-selectors": check_binding({"a": {}, "b": {}}, {"selectors": {"a": selector(key="x"), "b": selector(node=5)}}),
    "multi-binding-unknown-and-missing": check_binding({"first": {}, "second": {}}, {"selectors": {"first": selector(), "ghost": selector(node="3")}}),
    # check_for_save
    "save-no-binding": check_save(base_manifest(), binding_key="other"),
    "save-adapter-takes-none": check_save(base_manifest(), parsed=None),
    "save-ambiguous": check_save(edited(lambda d: (d.update(outputs={"a": {"media_type": "a/b"}, "b": {"media_type": "a/b"}}),
                                                    d["bindings"]["eng"].pop("selectors")))),
    "save-selectors-missing": check_save(edited(lambda d: d["bindings"]["eng"].pop("selectors"))),
    "save-unknown-node": check_save(base_manifest(), template={"1": TEMPLATE["1"]}),
    "save-echoes-source": check_save(edited(_set(("bindings", "eng", "selectors", "first", "node"), "1"))),
    "multi-save-two-nodes": check_save(edited(_two_unreal_nodes)),
    # workflow_requirements
    "requires-not-mapping": check_requirements([]),
    "requires-unknown-field": check_requirements({"gpus": 1}),
    "requires-nodes-not-list": check_requirements({"custom_nodes": "X"}),
    "requires-node-unknown-field": check_requirements({"custom_nodes": [{"class": "X", "extra": 1}]}),
    "requires-node-type": check_requirements({"custom_nodes": [3]}),
    "requires-node-class": check_requirements({"custom_nodes": [{"class": ""}]}),
    "requires-models-not-list": check_requirements({"models": {}}),
    "requires-model-not-mapping": check_requirements({"models": ["x"]}),
    "requires-model-unknown-field": check_requirements({"models": [{"role": "a", "path": "p", "zzz": 1, "aaa": 2}]}),
    "requires-model-role": check_requirements({"models": [{"path": "p"}]}),
    "requires-model-path-or-pattern": check_requirements({"models": [{"role": "a", "path": "p", "pattern": "q"}]}),
    "requires-model-neither": check_requirements({"models": [{"role": "a"}]}),
    "requires-model-path": check_requirements({"models": [{"role": "a", "path": " "}]}),
    "requires-model-pattern": check_requirements({"models": [{"role": "a", "pattern": 4}]}),
    "requires-model-sha": check_requirements({"models": [{"role": "a", "path": "p", "sha256": ""}]}),
    "multi-requires-nodes-and-models": check_requirements({"custom_nodes": [3], "models": [{"path": "p"}, "x"]}),
    # mesh
    "mesh-not-mapping": check_mesh([]),
    "mesh-unknown-top": check_mesh(mesh_doc(lambda m: m.update(surprise=True))),
    "mesh-missing-top": check_mesh(mesh_doc(lambda m: m.pop("outputs"))),
    "mesh-schema-version": check_mesh(mesh_doc(lambda m: m.update(schema_version=2))),
    "mesh-intent": check_mesh(mesh_doc(lambda m: m.update(intent="images.generate"))),
    "mesh-id": check_mesh(mesh_doc(lambda m: m.update(id=""))),
    "mesh-inputs-empty": check_mesh(mesh_doc(lambda m: m.update(inputs={}))),
    "mesh-input-name": check_mesh(mesh_doc(lambda m: m["inputs"].update({"Bad Name": {"type": "string"}}))),
    "mesh-input-not-mapping": check_mesh(mesh_doc(lambda m: m["inputs"].update(extra=4))),
    "mesh-input-unknown-key": check_mesh(mesh_doc(lambda m: m["inputs"]["seed"].update(zzz=1))),
    "mesh-input-type": check_mesh(mesh_doc(lambda m: m["inputs"]["seed"].update(type="video"))),
    "mesh-image-modality": check_mesh(mesh_doc(lambda m: m["inputs"]["image"].update(modality="video"))),
    "mesh-modality-on-other": check_mesh(mesh_doc(lambda m: m["inputs"]["seed"].update(modality="image"))),
    "mesh-integer-spec": check_mesh(mesh_doc(lambda m: m["inputs"]["seed"].update(minimum="x"))),
    "mesh-enum-empty": check_mesh(mesh_doc(lambda m: m["inputs"]["resolution"].update(enum=[]))),
    "mesh-enum-type": check_mesh(mesh_doc(lambda m: m["inputs"]["resolution"].update(enum=["a"]))),
    "mesh-default-invalid": check_mesh(mesh_doc(lambda m: m["inputs"]["seed"].update(default=10**30))),
    "mesh-image-required": check_mesh(mesh_doc(lambda m: m["inputs"]["image"].update(required=False))),
    "mesh-outputs-two": check_mesh(mesh_doc(lambda m: m["outputs"].update(extra={"modality": "mesh", "media_type": "model/gltf-binary"}))),
    "mesh-output-keys": check_mesh(mesh_doc(lambda m: m["outputs"]["mesh"].update(extra=1))),
    "mesh-output-type": check_mesh(mesh_doc(lambda m: m["outputs"]["mesh"].update(media_type="model/obj"))),
    "mesh-bindings-two": check_mesh(mesh_doc(lambda m: m["bindings"].update(other={}))),
    "mesh-binding-key": check_mesh(mesh_doc(lambda m: m.update(bindings={"Bad Id": next(iter(m["bindings"].values()))}))),
    "mesh-binding-not-mapping": check_mesh(mesh_doc(lambda m: m.update(bindings={next(iter(m["bindings"])): 1}))),
    "mesh-binding-unknown-key": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values())).update(zzz=1))),
    "mesh-transport-not-mapping": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values())).update(transport="x"))),
    "mesh-transport-kind": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values()))["transport"].update(kind="rpc"))),
    "mesh-transport-path": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values()))["transport"].update(path="/a/../b"))),
    "mesh-map-empty": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values())).update(map={}))),
    "mesh-map-unknown-input": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values()))["map"].update(ghost="g"))),
    "mesh-map-target": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values()))["map"].update(seed="Bad Field"))),
    "mesh-map-twice": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values()))["map"].update(seed="image"))),
    "mesh-map-image": check_mesh(mesh_doc(lambda m: next(iter(m["bindings"].values()))["map"].pop("image"))),
    "multi-mesh-input-and-binding": check_mesh(mesh_doc(lambda m: (m["inputs"]["seed"].update(minimum="x"),
                                                                    next(iter(m["bindings"].values()))["transport"].update(kind="rpc")))),
    "multi-mesh-two-inputs": check_mesh(mesh_doc(lambda m: (m["inputs"]["seed"].update(minimum="x"), m["inputs"]["resolution"].update(enum=[])))),
    "multi-mesh-top-and-output": check_mesh(mesh_doc(lambda m: (m.update(surprise=True), m["outputs"]["mesh"].update(media_type="model/obj")))),
    # deployment narrowing and runtime binding
    "narrow-unknown-input": check_narrowing({"ghost": {"minimum": 1}}),
    "narrow-not-number": check_narrowing({"prompt": {"minimum": 1}}),
    "narrow-unknown-key": check_narrowing({"count": {"zzz": 1}}),
    "narrow-not-finite": check_narrowing({"count": {"minimum": float("inf")}}),
    "narrow-min-below": check_narrowing({"count": {"minimum": 0}}),
    "narrow-max-above": check_narrowing({"count": {"maximum": 11}}),
    "narrow-empty": check_narrowing({"count": {"minimum": 8, "maximum": 6, "default": 7}}),
    "narrow-default-out": check_narrowing({"count": {"default": 50}}),
    "multi-narrow-two-inputs": check_narrowing({"ghost": {"minimum": 1}, "prompt": {"minimum": 1}}),
    "bind-no-backend": check_bind({"map": {}}, backend="other"),
    "bind-bad-map": check_bind({"map": []}),
    "bind-map-not-list": check_bind({"map": {"prompt": {"path": "a"}}}),
    "bind-mapping-not-mapping": check_bind({"map": {"prompt": ["x"]}}),
    "bind-node-without-input": check_bind({"map": {"prompt": [{"node": "1"}]}}),
    "bind-target-missing": check_bind({"map": {"prompt": [{"node": "1", "input": "text"}]}}),
    "bind-path-type": check_bind({"map": {"prompt": [{"path": 5}]}}),
    "bind-path-crosses": check_bind({"map": {"prompt": [{"path": "a.b.c.d"}]}}, request={"a": {"b": 5}}),
    "bind-path-target": check_bind({"map": {"prompt": [{"path": "a.b"}]}}, request={"a": 5}),
    "bind-nothing": check_bind({"map": {"prompt": [{}]}}),
}


# Captured from origin/main before the error type existed: (class name, message) of what each case raised first.
FIRST_MESSAGES: dict[str, tuple[str, str]] = {
    'manifest-missing-field': ('ManifestError', 'manifest missing required field: intent'),
    'manifest-missing-two-fields': ('ManifestError', 'manifest missing required field: intent'),
    'manifest-inputs-not-mapping': ('ManifestError', 'manifest inputs must be a mapping'),
    'manifest-bindings-not-mapping': ('ManifestError', 'manifest bindings must be a mapping'),
    'manifest-output-name': ('ManifestError', "output 'bad name': a result name starts with a letter or digit and holds only letters, digits, '.', '_' and '-' (at most 128)"),
    'manifest-purpose-empty': ('ManifestError', 'manifest purpose must be a non-empty string'),
    'manifest-purpose-long': ('ManifestError', 'manifest purpose must contain at most 8 words'),
    'manifest-summary-empty': ('ManifestError', 'manifest summary must be a non-empty string'),
    'manifest-summary-long': ('ManifestError', 'manifest summary must contain at most 40 words'),
    'manifest-license-type': ('ManifestError', 'manifest license must be a non-empty string'),
    'manifest-input-label': ('ManifestError', 'input prompt label must be a non-empty string'),
    'manifest-input-help': ('ManifestError', 'input prompt help must be a non-empty string'),
    'manifest-input-default-nan': ('ManifestError', 'input count default must be a finite number'),
    'manifest-edge-only': ('ManifestError', 'binding eng edge_only_inputs must name declared inputs'),
    'manifest-selector-key': ('SelectorError', "key of result first must be one of ['files', 'others']"),
    'manifest-selector-no-adapter-form': ('SelectorError', 'binding eng has selectors, but its adapter takes none'),
    'multi-input-and-binding': ('ManifestError', 'input prompt label must be a non-empty string'),
    'multi-input-binding-selector': ('ManifestError', 'input prompt label must be a non-empty string'),
    'multi-two-inputs': ('ManifestError', 'input prompt label must be a non-empty string'),
    'multi-missing-and-purpose': ('ManifestError', 'manifest missing required field: intent'),
    'multi-binding-then-selector': ('ManifestError', 'binding eng edge_only_inputs must name declared inputs'),
    'binding-not-mapping': ('SelectorError', 'selectors must be a non-empty mapping of result name to selector'),
    'binding-empty': ('SelectorError', 'selectors must be a non-empty mapping of result name to selector'),
    'binding-too-many': ('SelectorError', 'a binding has 4 selectors; the limit is 3'),
    'binding-unknown-result': ('SelectorError', 'selectors name result ghost, which the contract does not declare'),
    'binding-missing-result': ('SelectorError', 'result second has no selector; give every declared result one'),
    'binding-selector-not-mapping': ('SelectorError', 'the selector of result first must be a mapping'),
    'binding-selector-unknown-field': ('SelectorError', "the selector of result first has fields its adapter does not take: ['extra']"),
    'binding-selector-needs-field': ('SelectorError', 'the selector of result first needs key'),
    'binding-selector-long': ('SelectorError', 'node of result first must be a short non-empty string'),
    'binding-selector-enum': ('SelectorError', "key of result first must be one of ['files', 'others']"),
    'binding-duplicate': ('SelectorError', 'results a and b read the same place; each result needs its own selector'),
    'multi-binding-two-selectors': ('SelectorError', "key of result a must be one of ['files', 'others']"),
    'multi-binding-unknown-and-missing': ('SelectorError', 'selectors name result ghost, which the contract does not declare'),
    'save-no-binding': ('SelectorError', 'the manifest has no binding other'),
    'save-adapter-takes-none': ('SelectorError', 'binding eng has selectors, but its adapter takes none'),
    'save-ambiguous': ('SelectorError', "results ['a', 'b'] have the same type and nothing tells them apart; give binding eng a selector for each"),
    'save-selectors-missing': ('SelectorError', 'binding eng declares no output selectors; a new contract must say where each result comes from'),
    'save-unknown-node': ('SelectorError', 'the selector of result first names a node the template does not have'),
    'save-echoes-source': ('SelectorError', 'the selector of result first reads a node that holds a source, not one that makes a result'),
    'multi-save-two-nodes': ('SelectorError', 'the selector of result first names a node the template does not have'),
    'requires-not-mapping': ('ManifestError', 'manifest requires must be a mapping'),
    'requires-unknown-field': ('ManifestError', 'manifest requires has unknown field: gpus'),
    'requires-nodes-not-list': ('ManifestError', 'manifest requires.custom_nodes must be a list'),
    'requires-node-unknown-field': ('ManifestError', 'manifest requires.custom_nodes[0] has unknown field'),
    'requires-node-type': ('ManifestError', 'manifest requires.custom_nodes[0] must be a string or mapping'),
    'requires-node-class': ('ManifestError', 'manifest requires.custom_nodes[0].class must be a non-empty string'),
    'requires-models-not-list': ('ManifestError', 'manifest requires.models must be a list'),
    'requires-model-not-mapping': ('ManifestError', 'manifest requires.models[0] must be a mapping'),
    'requires-model-unknown-field': ('ManifestError', 'manifest requires.models[0] has unknown field: aaa'),
    'requires-model-role': ('ManifestError', 'manifest requires.models[0].role must be a non-empty string'),
    'requires-model-path-or-pattern': ('ManifestError', 'manifest requires.models[0] must set exactly one of path or pattern'),
    'requires-model-neither': ('ManifestError', 'manifest requires.models[0] must set exactly one of path or pattern'),
    'requires-model-path': ('ManifestError', 'manifest requires.models[0].path must be a non-empty string'),
    'requires-model-pattern': ('ManifestError', 'manifest requires.models[0].pattern must be a non-empty string'),
    'requires-model-sha': ('ManifestError', 'manifest requires.models[0].sha256 must be a non-empty string'),
    'multi-requires-nodes-and-models': ('ManifestError', 'manifest requires.custom_nodes[0] must be a string or mapping'),
    'mesh-not-mapping': ('MeshManifestError', 'mesh manifest must be a mapping'),
    'mesh-unknown-top': ('MeshManifestError', "manifest has unknown keys ['surprise']"),
    'mesh-missing-top': ('MeshManifestError', "manifest is missing ['outputs']"),
    'mesh-schema-version': ('MeshManifestError', 'mesh manifest schema_version must be 1'),
    'mesh-intent': ('MeshManifestError', "mesh manifest intent must be 'mesh.generate'"),
    'mesh-id': ('MeshManifestError', 'id must be a non-empty string'),
    'mesh-inputs-empty': ('MeshManifestError', 'mesh manifest inputs must be a non-empty mapping'),
    'mesh-input-name': ('MeshManifestError', "input name 'Bad Name' is not a plain field name"),
    'mesh-input-not-mapping': ('MeshManifestError', 'input extra must be a mapping'),
    'mesh-input-unknown-key': ('MeshManifestError', "input seed has unknown keys ['zzz']"),
    'mesh-input-type': ('MeshManifestError', "input seed type must be one of ['image', 'integer', 'object', 'string']"),
    'mesh-image-modality': ('MeshManifestError', 'input image of type image must declare modality image'),
    'mesh-modality-on-other': ('MeshManifestError', 'input seed may declare a modality only when it is an image'),
    'mesh-integer-spec': ('MeshManifestError', 'input seed minimum must be an integer'),
    'mesh-enum-empty': ('MeshManifestError', 'input resolution enum must be a non-empty list'),
    'mesh-enum-type': ('MeshManifestError', 'input resolution enum holds a value of the wrong type'),
    'mesh-default-invalid': ('MeshManifestError', 'input seed default is invalid: seed must be an integer'),
    'mesh-image-required': ('MeshManifestError', 'mesh manifest needs exactly one required image input'),
    'mesh-outputs-two': ('MeshManifestError', 'mesh manifest outputs must name exactly one output'),
    'mesh-output-keys': ('MeshManifestError', 'output mesh must declare modality and media_type only'),
    'mesh-output-type': ('MeshManifestError', 'output mesh must be modality mesh with media type model/gltf-binary'),
    'mesh-bindings-two': ('MeshManifestError', 'mesh manifest must hold exactly one binding'),
    'mesh-binding-key': ('MeshManifestError', "binding key 'Bad Id' is not a service id"),
    'mesh-binding-not-mapping': ('MeshManifestError', 'binding must be a mapping'),
    'mesh-binding-unknown-key': ('MeshManifestError', "binding trellis has unknown keys ['zzz']"),
    'mesh-transport-not-mapping': ('MeshManifestError', 'transport must be a mapping'),
    'mesh-transport-kind': ('MeshManifestError', "transport kind must be 'http-multipart'"),
    'mesh-transport-path': ('MeshManifestError', 'transport path must be a canonical slash-prefixed relative path'),
    'mesh-map-empty': ('MeshManifestError', 'binding map must be a non-empty mapping'),
    'mesh-map-unknown-input': ('MeshManifestError', "map names 'ghost', which is not an input"),
    'mesh-map-target': ('MeshManifestError', 'map target for seed must be a plain form field name'),
    'mesh-map-twice': ('MeshManifestError', "map target 'image' is used twice"),
    'mesh-map-image': ('MeshManifestError', 'the image input must be mapped'),
    'multi-mesh-input-and-binding': ('MeshManifestError', 'input seed minimum must be an integer'),
    'multi-mesh-two-inputs': ('MeshManifestError', 'input seed minimum must be an integer'),
    'multi-mesh-top-and-output': ('MeshManifestError', "manifest has unknown keys ['surprise']"),
    'narrow-unknown-input': ('ManifestError', 'deployment narrows input ghost, which the manifest does not declare'),
    'narrow-not-number': ('ManifestError', 'deployment narrows input prompt, which is not a number'),
    'narrow-unknown-key': ('ManifestError', "deployment sets ['zzz'] for input count; only ['default', 'minimum', 'maximum'] apply"),
    'narrow-not-finite': ('ManifestError', 'deployment value minimum for input count must be a finite number'),
    'narrow-min-below': ('ManifestError', "deployment minimum for count is below the manifest's 1"),
    'narrow-max-above': ('ManifestError', "deployment maximum for count is above the manifest's 10"),
    'narrow-empty': ('ManifestError', 'effective bounds for count are empty: 8 > 6'),
    'narrow-default-out': ('ManifestError', 'default for count is outside its effective bounds: count must be <= 10'),
    'multi-narrow-two-inputs': ('ManifestError', 'deployment narrows input ghost, which the manifest does not declare'),
    'bind-no-backend': ('ManifestError', 'manifest has no binding for backend: other'),
    'bind-bad-map': ('ManifestError', 'manifest binding for eng has invalid map'),
    'bind-map-not-list': ('ManifestError', 'binding map for prompt must be a list'),
    'bind-mapping-not-mapping': ('ManifestError', 'binding mapping must be a mapping'),
    'bind-node-without-input': ('ManifestError', 'binding mapping needs both node and input, or path'),
    'bind-target-missing': ('ManifestError', 'ComfyUI binding target missing: node 1 input text'),
    'bind-path-type': ('ManifestError', 'binding mapping path must be a dotted string'),
    'bind-path-crosses': ('ManifestError', 'binding path crosses non-mapping value at c'),
    'bind-path-target': ('ManifestError', 'binding path target is not a mapping'),
    'bind-nothing': ('ManifestError', 'binding mapping needs node/input or path'),
}


# The full list each case gives now: (path, code), sorted by path. Reviewed by hand against the document of each case.
EXPECTED_ERRORS: dict[str, list[tuple[tuple[str | int, ...], str]]] = {
    'manifest-missing-field': [(('intent',), 'required_field_missing')],
    'manifest-missing-two-fields': [(('intent',), 'required_field_missing'), (('outputs',), 'required_field_missing')],
    'manifest-inputs-not-mapping': [(('inputs',), 'type_mismatch')],
    'manifest-bindings-not-mapping': [(('bindings',), 'type_mismatch')],
    'manifest-output-name': [(('outputs', 'bad name'), 'result_name_invalid')],
    'manifest-purpose-empty': [(('purpose',), 'value_empty')],
    'manifest-purpose-long': [(('purpose',), 'too_many_words')],
    'manifest-summary-empty': [(('summary',), 'value_empty')],
    'manifest-summary-long': [(('summary',), 'too_many_words')],
    'manifest-license-type': [(('license',), 'value_empty')],
    'manifest-input-label': [(('inputs', 'prompt', 'label'), 'value_empty')],
    'manifest-input-help': [(('inputs', 'prompt', 'help'), 'value_empty')],
    'manifest-input-default-nan': [(('inputs', 'count', 'default'), 'number_not_finite')],
    'manifest-edge-only': [(('bindings', 'eng', 'edge_only_inputs'), 'reference_unknown')],
    'manifest-selector-key': [(('bindings', 'eng', 'selectors', 'first', 'key'), 'selector_malformed')],
    'manifest-selector-no-adapter-form': [(('bindings', 'eng', 'selectors'), 'selector_malformed')],
    'multi-input-and-binding': [(('bindings', 'eng', 'edge_only_inputs'), 'reference_unknown'), (('inputs', 'prompt', 'label'), 'value_empty')],
    'multi-input-binding-selector': [(('bindings', 'eng', 'edge_only_inputs'), 'reference_unknown'), (('bindings', 'eng', 'selectors', 'first', 'key'), 'selector_malformed'), (('inputs', 'prompt', 'label'), 'value_empty')],
    'multi-two-inputs': [(('inputs', 'count', 'help'), 'value_empty'), (('inputs', 'prompt', 'label'), 'value_empty')],
    'multi-missing-and-purpose': [(('intent',), 'required_field_missing'), (('purpose',), 'value_empty')],
    'multi-binding-then-selector': [(('bindings', 'eng', 'edge_only_inputs'), 'reference_unknown'), (('bindings', 'eng', 'selectors', 'first', 'key'), 'selector_malformed')],
    'binding-not-mapping': [(('selectors',), 'selector_malformed')],
    'binding-empty': [(('selectors',), 'selector_malformed')],
    'binding-too-many': [(('selectors',), 'selector_count')],
    'binding-unknown-result': [(('selectors', 'ghost'), 'selector_unknown_result')],
    'binding-missing-result': [(('selectors', 'second'), 'selector_missing_result')],
    'binding-selector-not-mapping': [(('selectors', 'first'), 'selector_malformed')],
    'binding-selector-unknown-field': [(('selectors', 'first'), 'selector_malformed')],
    'binding-selector-needs-field': [(('selectors', 'first', 'key'), 'selector_malformed')],
    'binding-selector-long': [(('selectors', 'first', 'node'), 'selector_malformed')],
    'binding-selector-enum': [(('selectors', 'first', 'key'), 'selector_malformed')],
    'binding-duplicate': [(('selectors', 'b'), 'selector_duplicate')],
    'multi-binding-two-selectors': [(('selectors', 'a', 'key'), 'selector_malformed'), (('selectors', 'b', 'node'), 'selector_malformed')],
    'multi-binding-unknown-and-missing': [(('selectors', 'ghost'), 'selector_unknown_result'), (('selectors', 'second'), 'selector_missing_result')],
    'save-no-binding': [(('bindings', 'other'), 'selector_malformed')],
    'save-adapter-takes-none': [(('bindings', 'eng', 'selectors'), 'selector_malformed')],
    'save-ambiguous': [(('bindings', 'eng'), 'results_ambiguous')],
    'save-selectors-missing': [(('bindings', 'eng'), 'selectors_missing')],
    'save-unknown-node': [(('bindings', 'eng', 'selectors', 'first', 'node'), 'selector_unknown_node')],
    'save-echoes-source': [(('bindings', 'eng', 'selectors', 'first', 'node'), 'selector_echoes_source')],
    'multi-save-two-nodes': [(('bindings', 'eng', 'selectors', 'first', 'node'), 'selector_unknown_node'), (('bindings', 'eng', 'selectors', 'second', 'node'), 'selector_unknown_node')],
    'requires-not-mapping': [(('requires',), 'type_mismatch')],
    'requires-unknown-field': [(('requires', 'gpus'), 'unknown_field')],
    'requires-nodes-not-list': [(('requires', 'custom_nodes'), 'type_mismatch')],
    'requires-node-unknown-field': [(('requires', 'custom_nodes', 0), 'unknown_field')],
    'requires-node-type': [(('requires', 'custom_nodes', 0), 'type_mismatch')],
    'requires-node-class': [(('requires', 'custom_nodes', 0, 'class'), 'value_empty')],
    'requires-models-not-list': [(('requires', 'models'), 'type_mismatch')],
    'requires-model-not-mapping': [(('requires', 'models', 0), 'type_mismatch')],
    'requires-model-unknown-field': [(('requires', 'models', 0, 'aaa'), 'unknown_field')],
    'requires-model-role': [(('requires', 'models', 0, 'role'), 'value_empty')],
    'requires-model-path-or-pattern': [(('requires', 'models', 0), 'exactly_one_of')],
    'requires-model-neither': [(('requires', 'models', 0), 'exactly_one_of')],
    'requires-model-path': [(('requires', 'models', 0, 'path'), 'value_empty')],
    'requires-model-pattern': [(('requires', 'models', 0, 'pattern'), 'value_empty')],
    'requires-model-sha': [(('requires', 'models', 0, 'sha256'), 'value_empty')],
    'multi-requires-nodes-and-models': [(('requires', 'custom_nodes', 0), 'type_mismatch'), (('requires', 'models', 0, 'role'), 'value_empty'), (('requires', 'models', 1), 'type_mismatch')],
    'mesh-not-mapping': [((), 'type_mismatch')],
    'mesh-unknown-top': [((), 'unknown_field')],
    'mesh-missing-top': [((), 'required_field_missing')],
    'mesh-schema-version': [(('schema_version',), 'version_unsupported')],
    'mesh-intent': [(('intent',), 'intent_mismatch')],
    'mesh-id': [(('id',), 'value_empty')],
    'mesh-inputs-empty': [(('inputs',), 'type_mismatch')],
    'mesh-input-name': [(('inputs', 'Bad Name'), 'name_invalid')],
    'mesh-input-not-mapping': [(('inputs', 'extra'), 'type_mismatch')],
    'mesh-input-unknown-key': [(('inputs', 'seed'), 'unknown_field')],
    'mesh-input-type': [(('inputs', 'seed', 'type'), 'type_unsupported')],
    'mesh-image-modality': [(('inputs', 'image', 'modality'), 'modality_invalid')],
    'mesh-modality-on-other': [(('inputs', 'seed', 'modality'), 'modality_invalid')],
    'mesh-integer-spec': [(('inputs', 'seed', 'minimum'), 'type_mismatch')],
    'mesh-enum-empty': [(('inputs', 'resolution', 'enum'), 'enum_invalid')],
    'mesh-enum-type': [(('inputs', 'resolution', 'enum', 0), 'type_mismatch')],
    'mesh-default-invalid': [(('inputs', 'seed', 'default'), 'default_invalid')],
    'mesh-image-required': [(('inputs',), 'image_input_count')],
    'mesh-outputs-two': [(('outputs',), 'output_count')],
    'mesh-output-keys': [(('outputs', 'mesh'), 'unknown_field')],
    'mesh-output-type': [(('outputs', 'mesh'), 'output_type_invalid')],
    'mesh-bindings-two': [(('bindings',), 'binding_count')],
    'mesh-binding-key': [(('bindings', 'Bad Id'), 'name_invalid')],
    'mesh-binding-not-mapping': [(('bindings', 'trellis'), 'type_mismatch')],
    'mesh-binding-unknown-key': [(('bindings', 'trellis'), 'unknown_field')],
    'mesh-transport-not-mapping': [(('bindings', 'trellis', 'transport'), 'type_mismatch')],
    'mesh-transport-kind': [(('bindings', 'trellis', 'transport', 'kind'), 'transport_kind_invalid')],
    'mesh-transport-path': [(('bindings', 'trellis', 'transport', 'path'), 'transport_path_invalid')],
    'mesh-map-empty': [(('bindings', 'trellis', 'map'), 'type_mismatch')],
    'mesh-map-unknown-input': [(('bindings', 'trellis', 'map', 'ghost'), 'reference_unknown')],
    'mesh-map-target': [(('bindings', 'trellis', 'map', 'seed'), 'name_invalid')],
    'mesh-map-twice': [(('bindings', 'trellis', 'map', 'seed'), 'target_duplicate')],
    'mesh-map-image': [(('bindings', 'trellis', 'map'), 'image_unmapped')],
    'multi-mesh-input-and-binding': [(('bindings', 'trellis', 'transport', 'kind'), 'transport_kind_invalid'), (('inputs', 'seed', 'minimum'), 'type_mismatch')],
    'multi-mesh-two-inputs': [(('inputs', 'resolution', 'enum'), 'enum_invalid'), (('inputs', 'seed', 'minimum'), 'type_mismatch')],
    'multi-mesh-top-and-output': [((), 'unknown_field'), (('outputs', 'mesh'), 'output_type_invalid')],
    'narrow-unknown-input': [(('inputs', 'ghost'), 'input_unknown')],
    'narrow-not-number': [(('inputs', 'prompt'), 'input_not_numeric')],
    'narrow-unknown-key': [(('inputs', 'count'), 'limit_unknown')],
    'narrow-not-finite': [(('inputs', 'count', 'minimum'), 'number_not_finite')],
    'narrow-min-below': [(('inputs', 'count', 'minimum'), 'bound_widened')],
    'narrow-max-above': [(('inputs', 'count', 'maximum'), 'bound_widened')],
    'narrow-empty': [(('inputs', 'count'), 'bounds_empty')],
    'narrow-default-out': [(('inputs', 'count', 'default'), 'default_out_of_bounds')],
    'multi-narrow-two-inputs': [(('inputs', 'ghost'), 'input_unknown'), (('inputs', 'prompt'), 'input_not_numeric')],
    'bind-no-backend': [(('bindings', 'other'), 'binding_missing')],
    'bind-bad-map': [(('bindings', 'eng', 'map'), 'type_mismatch')],
    'bind-map-not-list': [(('bindings', 'eng', 'map', 'prompt'), 'type_mismatch')],
    'bind-mapping-not-mapping': [(('bindings', 'eng', 'map', 'prompt', 0), 'type_mismatch')],
    'bind-node-without-input': [(('bindings', 'eng', 'map', 'prompt', 0), 'mapping_incomplete')],
    'bind-target-missing': [(('bindings', 'eng', 'map', 'prompt', 0), 'target_missing')],
    'bind-path-type': [(('bindings', 'eng', 'map', 'prompt', 0, 'path'), 'type_mismatch')],
    'bind-path-crosses': [(('bindings', 'eng', 'map', 'prompt', 0, 'path'), 'path_invalid')],
    'bind-path-target': [(('bindings', 'eng', 'map', 'prompt', 0, 'path'), 'path_invalid')],
    'bind-nothing': [(('bindings', 'eng', 'map', 'prompt', 0), 'mapping_incomplete')],
}
