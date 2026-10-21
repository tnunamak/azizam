"""Merged workflow metadata, private publication and output availability contracts."""
import asyncio
import errno
import io
import json

import pytest
from PIL import Image, PngImagePlugin

import proxy
from gateway import media_jobs
from tests.test_image_media_jobs import authenticated, _StubJsonRequest
from tests.test_private_modes import mode, permissive_umask  # noqa: F401


@pytest.mark.parametrize("name", ["moss-image", "moss-image-fast", "moss-image-quality"])
@pytest.mark.parametrize("remove_background", [False, True])
def test_workflow_output_publication_and_content_availability(
    tmp_path, monkeypatch, permissive_umask, name, remove_background,
):
    workflow = proxy.load_graph_workflow(name)
    meta = workflow.pop("_meta")
    assert proxy.set_workflow_background_removal(workflow, meta, remove_background)
    background = meta["background_removal"]
    # The selected SaveImage input must agree with the data's removal toggle.
    assert any(node["class_type"] == "RMBG" for node in workflow.values())
    assert workflow[background["save_node"]]["inputs"]["images"] == background[
        "transparent_image" if remove_background else "plain_image"
    ]

    cache = tmp_path / "images"
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy.media_delivery, "current", lambda: None)
    database = tmp_path / "jobs.db"
    media_jobs.init_db(database)
    monkeypatch.setattr(media_jobs, "DB_PATH", database)
    encoded = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", json.dumps(workflow))
    Image.new("RGBA", (2, 2), (1, 2, 3, 0 if remove_background else 255)).save(
        encoded, format="PNG", pnginfo=info,
    )
    asset_id = proxy._store_asset_bytes(encoded.getvalue(), maker_person_id=None)
    path = proxy.asset_cache_path(asset_id)
    assert path is not None
    assert mode(path) == 0o600 and mode(cache) == 0o700
    assert not list(cache.glob("*.tmp"))
    with Image.open(path) as image:
        assert "prompt" not in image.info
        assert image.getpixel((0, 0))[3] == (0 if remove_background else 255)

    job = media_jobs.create(media_jobs.MediaJob(
        app_id="app_test", intent="image.generate", request_id="fixture",
        status="succeeded", progress=100, assets=[{"asset_id": asset_id}],
    ), path=database)
    with authenticated():
        response = asyncio.run(proxy.gateway_media_job_content(job.job_id, _StubJsonRequest({})))
        assert response.status_code == 200
        path.unlink()
        response = asyncio.run(proxy.gateway_media_job_content(job.job_id, _StubJsonRequest({})))
        assert response.status_code == 410
        assert json.loads(response.body)["output_state"] == "missing"

        def inaccessible(_asset_id):
            raise OSError(errno.EIO, "fixture")

        monkeypatch.setattr(proxy, "asset_cache_path", inaccessible)
        response = asyncio.run(proxy.gateway_media_job_content(job.job_id, _StubJsonRequest({})))
        assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert media_jobs.get(job.job_id, audience="internal", path=database)["status"] == "succeeded"
