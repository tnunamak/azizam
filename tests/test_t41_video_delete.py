"""Both the current and historical video routes use the published delete type."""
import pytest

from gateway import openai_media_schemas
from tests.support.legacy_video import legacy_video_record
from tests.support.video_rig import run


@pytest.mark.parametrize("legacy", [False, True])
def test_video_delete_discriminator_on_both_route_implementations(tmp_path, legacy):
    async def exercise(world):
        video_id = legacy_video_record(world, "old-delete") if legacy else await world.create()
        response = await world.delete(video_id)
        assert response.status_code == 200
        assert response.json() == {"id": video_id, "object": "video.deleted", "deleted": True}
    run(tmp_path, exercise)


def test_video_delete_schema_uses_published_discriminator():
    schema = openai_media_schemas.VIDEOS_DELETE_OPENAPI["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["properties"]["object"]["enum"] == ["video.deleted"]
