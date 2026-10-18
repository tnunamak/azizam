"""Every catalog ComfyUI workflow must be able to carry its UI graph.

Tim's rule: a generated file on disk reopens in ComfyUI. That needs the UI
``workflow`` next to the API ``prompt``. These tests run the gateway's own
derivation over every catalog template against a recorded ``/object_info``
(refresh it with ``scripts/audit_comfy_workflow_metadata.py --write-fixture``).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_metadata  # noqa: E402
from gateway.engine_protocol import node_catalog as catalog  # noqa: E402
from gateway.engine_protocol import node_info, node_ui_graph  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "comfy_object_info_catalog.json"
OBJECT_INFO = json.loads(FIXTURE.read_text())
_REAL_LOOKUP = node_info.lookup  # tests/conftest.py stubs the module attribute per test
PATHS = catalog.catalog_workflow_paths(ROOT, "comfyui")

# Known gaps, each with the reason. A listed workflow that starts passing fails
# the test (strict xfail), which forces this list to shrink.
KNOWN_GAPS = {
    "workflows/audio/tangoflux.json": (
        "TangoFluxVAEDecodeAndPlay (custom node) writes the wav itself and ignores extra_pnginfo; "
        "needs a SaveAudio node or a patch to the custom node"
    ),
}


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_catalog_is_not_empty() -> None:
    assert len(PATHS) >= 30
    assert {"video-ltx-2.5-long.json", "qwen-image-2.1-identity.json", "qwen-image-2.1-edit.json"} <= {
        p.name for p in PATHS
    }


def test_fixture_covers_every_catalog_node_type() -> None:
    needed: set[str] = set()
    for path in PATHS:
        needed |= catalog.class_types(json.loads(path.read_text()))
    assert sorted(needed - set(OBJECT_INFO)) == [], "refresh the fixture: scripts/audit_comfy_workflow_metadata.py --write-fixture"


@pytest.mark.parametrize("path", PATHS, ids=_rel)
def test_workflow_derives_a_complete_ui_graph(path: Path, request: pytest.FixtureRequest) -> None:
    if _rel(path) in KNOWN_GAPS:
        request.applymarker(pytest.mark.xfail(strict=True, reason=KNOWN_GAPS[_rel(path)]))
    assert catalog.audit_workflow(path, OBJECT_INFO.get) == []


@pytest.mark.parametrize("path", PATHS, ids=_rel)
def test_submit_payload_embeds_workflow(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real submit helper, on the template as the gateway loads it."""
    monkeypatch.setattr(proxy, "_graph_node_info", OBJECT_INFO.get)
    workflow = proxy._load_graph_workflow_file(path)
    assert workflow is not None, "gateway rejects this workflow"
    payload = proxy._build_graph_submit_payload(workflow)
    ui = payload["extra_data"]["extra_pnginfo"]["workflow"]
    assert ui["nodes"] and not any(k.startswith("__") for k in payload["prompt"])


def test_readable_node_ids_get_integer_ids() -> None:
    """Catalog templates name nodes like 's0_text'; the frontend needs integers."""
    api = {
        "7": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a"}},
        "clip_text": {"class_type": "CLIPTextEncode", "inputs": {"text": "hi", "clip": ["7", 1]}},
        "a_first": {"class_type": "CLIPTextEncode", "inputs": {"text": "yo", "clip": ["7", 1]}},
    }
    ui = node_ui_graph.derive_ui_workflow(api, OBJECT_INFO.get)
    assert ui is not None
    ids = {n["properties"].get("api_node_id", str(n["id"])): n["id"] for n in ui["nodes"]}
    assert ids == {"7": 7, "a_first": 8, "clip_text": 9}
    assert len(ui["links"]) == 2 and ui["last_node_id"] == 9
    assert node_ui_graph.validate_ui_workflow(api, ui) == []


def test_validator_reports_a_dropped_link() -> None:
    api = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "hi", "clip": ["1", 1]}},
    }
    ui = node_ui_graph.derive_ui_workflow(api, OBJECT_INFO.get)
    ui["links"] = []
    assert any("link" in p for p in node_ui_graph.validate_ui_workflow(api, ui))


def test_object_info_falls_back_to_last_good_disk_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = node_info
    monkeypatch.setattr(module, "lookup", _REAL_LOOKUP)
    info = {"input": {"required": {}}, "output": [], "output_name": []}
    calls = {"ok": True}

    class Resp:
        def __init__(self, ok: bool) -> None:
            self.status_code = 200 if ok else 503

        def json(self) -> dict:
            return {"KSampler": info}

    class Client:
        def __init__(self, *a, **k) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return Resp(calls["ok"])

    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "DISK_CACHE_DIR", tmp_path)
    monkeypatch.setattr(module, "_CACHE", {})
    monkeypatch.setattr(module, "_FAILED_AT", {})
    assert module.lookup("http://h", "KSampler") == info
    module._CACHE.clear(); module._FAILED_AT.clear()  # simulate a restart
    calls["ok"] = False
    assert module.lookup("http://h", "KSampler") == info
    assert module.lookup("http://h", "KSampler") == info  # inside the failure window too
    assert module.lookup("http://h", "Unknown") is None
    module._CACHE.clear(); module._FAILED_AT.clear()


def _png_with_chunks(color: int = 0) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (color, 0, 0)).save(buf, format="PNG")
    return media_metadata.embed_png_text(buf.getvalue(), {"prompt": '{"1": {}}', "workflow": '{"nodes": []}'})


def test_gateway_png_reencodes_keep_comfy_chunks() -> None:
    """Pillow drops text chunks on save; the offset and mask-composite steps must carry them."""
    from PIL import Image

    source = _png_with_chunks()
    plain = io.BytesIO()
    Image.open(io.BytesIO(source)).save(plain, format="PNG")
    assert media_metadata.png_text_chunks(plain.getvalue()) == {}  # the failure this guards

    shifted = proxy._offset_image_bytes(source, x_fraction=0.25, y_fraction=0.25)
    assert media_metadata.png_text_chunks(shifted) == {"prompt": '{"1": {}}', "workflow": '{"nodes": []}'}

    context = proxy.ImageEditMaskContext(
        source_image=_png_with_chunks(200), edit_mask=proxy._image_to_png_bytes(Image.new("L", (8, 8), 255)),
        source_size=(8, 8), mask_mode="alpha",
    )
    composed = proxy._apply_image_edit_mask_composite(context, source)
    assert media_metadata.png_text_chunks(composed) == {"prompt": '{"1": {}}', "workflow": '{"nodes": []}'}


def test_full_delivery_keeps_workflow_chunks_and_none_strips_them() -> None:
    from gateway import media_delivery, provenance

    stored = _png_with_chunks()
    assert media_delivery.deliver_bytes(stored, ".png", provenance.METADATA_FULL) == stored
    assert media_metadata.png_text_chunks(media_delivery.deliver_bytes(stored, ".png", provenance.METADATA_NONE)) == {}
