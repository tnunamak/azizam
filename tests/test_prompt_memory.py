"""Prompt text has one maker-scoped durable home, independent of media bytes."""

import time

from gateway import admin_store, media_delivery, prompt_memory


def test_asset_prompt_is_maker_only_and_expires(tmp_path):
    db = tmp_path / "data" / "media_jobs.db"
    prompt_memory.put("img_abc", "person-a", "a private blue dragon", kept_until=None, path=db)
    assert prompt_memory.get("img_abc", "person-a", path=db)["prompt_text"] == "a private blue dragon"
    assert prompt_memory.get("img_abc", "person-b", path=db) is None
    assert not prompt_memory.put("img_expired", "person-a", "expired text", kept_until=int(time.time()) - 1, path=db)
    assert prompt_memory.get("img_expired", "person-a", path=db) is None
    assert prompt_memory.prune(path=db) == 0


def test_delete_person_preserves_other_prompts_and_media(tmp_path):
    db = tmp_path / "data" / "media_jobs.db"
    media = tmp_path / "cache" / "image.png"
    media.parent.mkdir()
    media.write_bytes(b"media stays")
    prompt_memory.put("img_shared", "person-a", "first prompt", kept_until=None, path=db)
    prompt_memory.put("img_shared", "person-b", "second prompt", kept_until=None, path=db)
    assert prompt_memory.delete_person("person-a", path=db) == 1
    assert prompt_memory.get("img_shared", "person-a", path=db) is None
    assert prompt_memory.get("img_shared", "person-b", path=db)["prompt_text"] == "second prompt"
    assert media.read_bytes() == b"media stays"


def test_forget_decision_never_writes_prompt_bytes(tmp_path):
    db = tmp_path / "data" / "media_jobs.db"
    cache = tmp_path / "cache"
    cache.mkdir()
    decision = prompt_memory.decide(
        person_id="person-a", member={"remember": True, "retention": "never"},
        household={"default_remember": True, "never_keep": True, "retention_cap": "never"},
        request_body={"prompt": "opaque-sentinel-7e31"},
    )
    assert not decision.keep
    assert decision.reason == "server_disabled"
    assert not db.exists()
    assert all(b"opaque-sentinel-7e31" not in p.read_bytes() for p in (db.parent, cache) for p in p.rglob("*") if p.is_file())


def test_request_forget_overrides_remember_and_validates_shape():
    base = dict(
        person_id="person-a", member={"remember": True, "retention": "never"},
        household={"default_remember": True, "never_keep": False, "retention_cap": "never"},
    )
    result = prompt_memory.decide(**base, request_body={"extensions": {"com.azizam.prompts": {"mode": "forget"}}})
    assert result.reason == "request_forget" and not result.keep
    assert prompt_memory.decide(**base, request_body={"prompt": "hello"}).keep


def test_default_retention_is_never_and_owner_default_controls_new_member():
    remembered = prompt_memory.decide(
        person_id="person-a", member=None, household=None, request_body={"prompt": "hello"}, now=100,
    )
    assert remembered.keep and remembered.kept_until is None
    disabled = prompt_memory.decide(
        person_id="person-a", member=None, household={"default_remember": False},
        request_body={"prompt": "hello"}, now=100,
    )
    assert not disabled.keep and disabled.reason == "member_disabled"


def test_server_purge_deletes_prompts_for_every_person_but_not_media(tmp_path):
    db = tmp_path / "data" / "media_jobs.db"
    media = tmp_path / "cache" / "image.png"
    media.parent.mkdir()
    media.write_bytes(b"media stays")
    for person in ("person-a", "person-b"):
        prompt_memory.put("img_shared", person, f"{person} secret", kept_until=None, path=db)
    assert prompt_memory.delete_all(path=db) == 2
    assert prompt_memory.get("img_shared", "person-a", path=db) is None
    assert prompt_memory.get("img_shared", "person-b", path=db) is None
    assert media.read_bytes() == b"media stays"


def test_anonymous_direct_output_keeps_its_profile_mode_but_forget_is_clean():
    context = media_delivery.begin(profile_default="full")
    context.prompt_decision = prompt_memory.decide(person_id=None, member=None, household=None, request_body={"prompt": "hello"})
    assert context.mode == "full"
    context.prompt_decision = prompt_memory.decide(
        person_id=None, member=None, household=None,
        request_body={"extensions": {"com.azizam.prompts": {"mode": "forget"}}},
    )
    assert context.mode == "none"
    context.prompt_decision = prompt_memory.decide(
        person_id=None, member=None, household={"never_keep": True}, request_body={"prompt": "hello"},
    )
    assert context.mode == "none"


def test_in_flight_decision_cannot_write_after_owner_kill_switch(tmp_path):
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "media_jobs.db"
    admin_store.init_db(admin_db)
    decision = prompt_memory.decide(
        person_id="person-a", member=None, household=None,
        request_body={"prompt": "late-sentinel"}, now=int(time.time()),
    )
    assert decision.keep
    admin_store.put_operator_setting(
        "prompt_memory", {"default_remember": True, "never_keep": True, "retention_cap": "never"},
        path=admin_db,
    )
    assert not prompt_memory.put(
        "img_late", "person-a", "late-sentinel", kept_until=decision.kept_until,
        created_at=decision.created_at, admin_path=admin_db, path=prompt_db,
    )
    assert not prompt_db.exists()


def test_shorter_retention_applies_to_existing_never_records(tmp_path):
    db = tmp_path / "media_jobs.db"
    prompt_memory.put("img_old", "person-a", "old", kept_until=None, created_at=100, path=db)
    prompt_memory.put("img_other", "person-b", "other", kept_until=None, created_at=100, path=db)
    prompt_memory.tighten_retention("3_months", person_id="person-a", path=db, now=100)
    assert prompt_memory.get("img_old", "person-a", path=db) is None
    assert prompt_memory.get("img_other", "person-b", path=db)


def test_video_prompt_move_keeps_finished_asset_workflow_metadata(tmp_path):
    db = tmp_path / "media_jobs.db"
    prompt_memory.put("video_pending", "person-a", "maker words", kept_until=None, path=db)
    prompt_memory.put("vid_finished", "person-a", "maker words", kept_until=None,
                      metadata={"workflow": "private graph"}, path=db)
    assert prompt_memory.move("video_pending", "vid_finished", "person-a", path=db)
    assert prompt_memory.get("video_pending", "person-a", path=db) is None
    assert prompt_memory.get("vid_finished", "person-a", path=db)["metadata"] == {"workflow": "private graph"}
