"""A video record of the old kind, for the tests of the M7 legacy reader.

``video_<engine prompt id>`` is the id of a video the gateway made before ``POST /v1/videos`` created jobs. The legacy reader
still serves it: from the in-memory table and the ``video_job_records`` rows the old create wrote, and from the engine on every
GET. ``legacy_video_record`` writes such a record through the old store (``video_job_records.save``) and the table the old
reader hydrates, so a test can assert that the old output is byte-identical.
"""
from __future__ import annotations

from typing import Any

import proxy
from gateway import media_jobs, video_job_records
from tests.support.video_rig import ALICE, World


def legacy_video_record(world: World, prompt_id: str, *, owner=ALICE, **fields: Any) -> str:
    """Store one old-kind record for ``owner`` and return its wire id (``video_<prompt_id>``). ``fields`` override the
    record keys (``seconds``, ``size``, ``deleted_at``, ``remixed_from_video_id``, ...)."""
    now = int(world.now)
    context = proxy.AuthContext(identity=owner)
    record: dict[str, Any] = {
        "model": "ltx-2.5-t2v", "resolved_model": "ltx-2.5-t2v", "size": "960x512", "seconds": "5", "actual_seconds": 5.0417,
        "created_at": now, "completed_at": None, "expires_at": now + 86400, "error": None, "remixed_from_video_id": None,
        "metadata_mode": None, "provenance": None,
        "app_profile": proxy.plan_ref(owner), "app_id": owner.app_id, "person_id": owner.person_id,
        "api_key_name": owner.name, "owner_key_hash": proxy._asset_owner_key(context), "deleted_at": None,
    }
    record.update(fields)
    proxy._video_job_records[prompt_id] = record
    video_job_records.save(prompt_id, record, path=media_jobs.DB_PATH)
    return f"video_{prompt_id}"
