"""Join real adapter admission, listing, request resolution and graph binding locally."""

import asyncio
import json
import struct
from types import SimpleNamespace

from gateway import adapter_choices as choices
from gateway import adapter_upload as uploads
from gateway.manifests import ManifestIdentity, bind_backend_request


def test_member_lora_upload_reaches_the_image_graph(tmp_path, monkeypatch):
    person, label = "synthetic-member", "Soft Light.safetensors"
    architecture, folder, binding = "synthetic-base", "loras", "graph"
    table = {architecture: {"ss_base_model_version": ("synthetic-v1",)}}
    asset = tmp_path / "upload.safetensors"
    loras = tmp_path / folder
    loras.mkdir()
    header = json.dumps({
        "__metadata__": {"ss_base_model_version": "synthetic-v1"},
        "test.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
    }).encode("utf-8")
    header += b" " * (-len(header) % 8)
    data = struct.pack("<Q", len(header)) + header + struct.pack("<f", 1.0)
    asset.write_bytes(data)

    rules = uploads.Policy(enabled=True)
    uploads.admit(rules, size=len(data), engine_local=True)
    checked = uploads.validate(asset, rules)
    assert checked.size == len(data)
    assert uploads.architectures(checked.header.metadata, table) == (architecture,)
    asset_id = "ast_" + uploads.asset_digest(person, checked.digest)
    library = uploads.Library(
        asset_ids=lambda: [asset_id],
        metadata=lambda key: {"adapter": {
            "label": uploads.label_of(label), "content_sha256": checked.digest,
        }} if key == asset_id else {},
        path=lambda key: asset if key == asset_id else None,
        holds=lambda who, key: (who, key) == (person, asset_id),
        policy=lambda: rules,
        lora_root=lambda key: loras if key == folder else None,
        note_copy=lambda *_: None,
        table=table,
    )
    monkeypatch.setattr(choices, "UPLOADS", library)
    monkeypatch.setattr(choices, "MARKS", lambda: None)
    monkeypatch.setattr(choices, "architecture_table", lambda: table)
    empty_list = lambda *_: []
    monkeypatch.setattr(choices, "default_list_files", empty_list)

    # The ModelLoader -> Sampler -> Decode fixture and generic chain shape
    # from test_adapter_graph.py, with the public loras input and loader class.
    template = {
        "1": {"class_type": "ModelLoader", "inputs": {"model_name": "base.safetensors"}},
        "7": {"class_type": "Sampler", "inputs": {"model": ["1", 0], "steps": 8}},
        "8": {"class_type": "Decode", "inputs": {"samples": ["7", 0]}},
    }
    contract = {
        "id": "demo", "version": "1",
        "inputs": {"loras": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string", "required": True},
            "weight": {"type": "number", "default": 1.0},
        }}}},
        "bindings": {binding: {
            "adapter_architecture": architecture, "adapter_lora_folder": folder,
            "map": {"loras": [{"node": "7", "input": "model", "chain": {
                "node_type": "LoraLoader", "link": "model",
                "fields": {"name": "lora_name", "weight": "strength"},
            }}]},
        }},
    }
    ident = ManifestIdentity("demo", "1", "hash", True, "local")
    context = {"service_id": f"synthetic:{tmp_path}", "origin": "http://unused.invalid", "person_id": person}

    async def journey():
        picked = await choices.offered(
            [SimpleNamespace(id="demo", contract=contract)], binding, list_files=empty_list, **context,
        )
        name = picked["demo"]["loras"]["items"]["properties"]["name"]
        assert name["enum"] == [asset_id]
        assert name["choice_labels"] == {asset_id: label}

        resolved = await choices.current(contract, binding, supplied={"loras": [{"name": asset_id}]}, **context)
        assert resolved is not None
        engine_name = f"{uploads.SUBFOLDER}/{checked.digest}.safetensors"
        try:
            request = bind_backend_request(
                resolved.contract, binding, template, resolved.supplied, identity=ident,
            ).request
            assert (loras / engine_name).read_bytes() == data
            assert request["7-model-chain-0"]["class_type"] == "LoraLoader"
            assert request["7-model-chain-0"]["inputs"]["lora_name"] == engine_name
            assert request["7"]["inputs"]["model"] == ["7-model-chain-0", 0]
        finally:
            choices.release(resolved.hold)

        unchosen = await choices.current(contract, binding, supplied={"loras": []}, **context)
        assert unchosen is not None
        assert bind_backend_request(
            unchosen.contract, binding, template, unchosen.supplied, identity=ident,
        ).request == template

    asyncio.run(journey())
