"""Prompt text has one maker-scoped durable home, independent of media bytes."""

import base64
import sqlite3
import time
from unittest.mock import patch

import proxy
from gateway import admin_store, media_delivery, media_jobs, media_metadata, prompt_memory


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


def test_get_person_prompts_batches_visible_rows(tmp_path):
    db = tmp_path / "data" / "media_jobs.db"
    prompt_memory.put("img_a", "person-a", "first", kept_until=None, path=db)
    prompt_memory.put("img_b", "person-a", "second", kept_until=None, path=db)
    prompt_memory.put("img_c", "person-b", "private", kept_until=None, path=db)

    records = prompt_memory.get_person_prompts("person-a", path=db)
    assert set(records) == {"img_a", "img_b"}
    assert records["img_a"]["prompt_text"] == "first"


def test_safe_put_fails_closed_without_logging_prompt(monkeypatch, caplog, capsys):
    import logging

    prompt = "private-prompt-log-canary-519b"
    metadata = "private-workflow-log-canary-708c"

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("locked: " + prompt + " " + metadata)

    monkeypatch.setattr(prompt_memory, "put", fail)
    with caplog.at_level(logging.DEBUG):
        result = prompt_memory.safe_put(
            "img_failed", "person-a", prompt, kept_until=None,
            metadata={"workflow": metadata},
        )
    assert result is False
    captured = capsys.readouterr()
    observed = captured.out + captured.err + caplog.text
    assert prompt not in observed
    assert metadata not in observed


def test_forget_decision_is_enforced_by_storage_route(tmp_path):
    sentinel = "opaque-sentinel-7e31"
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    data.mkdir()
    prompt_db = data / "media_jobs.db"
    admin_db = data / "admin.db"
    admin_store.init_db(admin_db)
    decision = prompt_memory.decide(
        person_id="person-a", member={"remember": True, "retention": "never"},
        household={"default_remember": True, "never_keep": False, "retention_cap": "never"},
        request_body={"prompt": sentinel, "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}},
    )
    assert not decision.keep
    assert decision.reason == "request_forget"
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    source = media_metadata.embed_png_text(png, {"prompt": sentinel, "workflow": sentinel})
    context = media_delivery.begin(profile_default="none")
    context.prompt_decision = decision
    context.prompt_text = sentinel
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), \
         patch.object(proxy.ADMIN_STORE, "path", admin_db), \
         patch.object(media_jobs, "DB_PATH", prompt_db):
        proxy.store_asset_bytes(source, kind="img", suffix=".png")
    assert all(sentinel.encode() not in p.read_bytes()
               for root in (data, cache) for p in root.rglob("*") if p.is_file())


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


def test_household_default_flip_does_not_change_existing_member_choice(tmp_path):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    admin_store.put_operator_setting(
        "prompt_memory", {"default_remember": False, "never_keep": False, "retention_cap": "never"},
        path=admin_db,
    )
    user = admin_store.upsert_oidc_user("iss", "sam", email="sam@example.test", path=admin_db)
    # Simulate an upgraded database with a member who has no preference row.
    with admin_store._conn(admin_db) as conn:
        conn.execute("DELETE FROM prompt_memory_preferences WHERE user_id=?", (user["id"],))
        conn.commit()
    admin_store.init_db(admin_db)
    assert prompt_memory.decide_for_person(user["id"], {"prompt": "x"}, admin_path=admin_db).reason == "member_disabled"

    admin_store.put_operator_setting(
        "prompt_memory", {"default_remember": True, "never_keep": False, "retention_cap": "never"},
        path=admin_db,
    )
    result = prompt_memory.decide_for_person(user["id"], {"prompt": "x"}, admin_path=admin_db)
    assert not result.keep and result.reason == "member_disabled"


def test_move_deletes_pending_prompt_when_policy_denies_storage(tmp_path):
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "media_jobs.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("iss", "sam", email="sam@example.test", path=admin_db)
    prompt_memory.put("video_pending", user["id"], "private video prompt", kept_until=None, path=prompt_db)
    admin_store.put_prompt_memory_preference(user["id"], remember=False, retention="never", path=admin_db)

    assert not prompt_memory.move("video_pending", "vid_finished", user["id"], path=prompt_db, admin_path=admin_db)
    assert prompt_memory.get("video_pending", user["id"], path=prompt_db) is None


def test_removed_or_disabled_person_cannot_persist_in_flight_prompt(tmp_path):
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "media_jobs.db"
    admin_store.init_db(admin_db)
    disabled = admin_store.upsert_oidc_user("iss", "disabled", path=admin_db)
    removed = admin_store.upsert_oidc_user("iss", "removed", path=admin_db)
    with admin_store._conn(admin_db) as conn:
        conn.execute(
            "CREATE TABLE account_access (user_id TEXT PRIMARY KEY, disabled_at INTEGER, removed_at INTEGER)",
        )
        conn.commit()
    decisions = {
        user["id"]: prompt_memory.decide_for_person(
            user["id"], {"prompt": "private prompt"}, admin_path=admin_db,
        )
        for user in (disabled, removed)
    }
    assert all(decision.keep for decision in decisions.values())
    with admin_store._conn(admin_db) as conn:
        conn.executemany(
            "INSERT INTO account_access(user_id,disabled_at,removed_at) VALUES(?,?,?)",
            [(disabled["id"], 100, None), (removed["id"], None, 100)],
        )
        conn.commit()

    for user in (disabled, removed):
        assert not prompt_memory.put(
            "video_in_flight", user["id"], "private prompt",
            kept_until=decisions[user["id"]].kept_until,
            created_at=decisions[user["id"]].created_at,
            admin_path=admin_db, path=prompt_db,
        )
    assert prompt_memory.get("video_in_flight", disabled["id"], path=prompt_db) is None
    assert prompt_memory.get("video_in_flight", removed["id"], path=prompt_db) is None


def test_erasure_cutoff_blocks_old_in_flight_write_but_allows_later_request(tmp_path):
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "media_jobs.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("iss", "sam", path=admin_db)
    decision = prompt_memory.decide_for_person(
        user["id"], {"prompt": "before delete"}, admin_path=admin_db,
    )
    assert decision.keep and decision.created_at is not None

    cutoff = prompt_memory.mark_person_erasure(
        user["id"], admin_path=admin_db, now=decision.created_at,
    )
    assert cutoff == decision.created_at
    assert not prompt_memory.put(
        "img_in_flight", user["id"], "before delete", kept_until=None,
        created_at=decision.created_at, admin_path=admin_db, path=prompt_db,
    )
    assert prompt_memory.put(
        "img_after_delete", user["id"], "after delete", kept_until=None,
        created_at=cutoff + 1, admin_path=admin_db, path=prompt_db,
    )
    assert prompt_memory.get("img_in_flight", user["id"], path=prompt_db) is None
    assert prompt_memory.get("img_after_delete", user["id"], path=prompt_db)


def test_prune_person_before_reconciles_erasure_cutoff(tmp_path):
    db = tmp_path / "media_jobs.db"
    prompt_memory.put("img_old", "person-a", "old", kept_until=None, created_at=100, path=db)
    prompt_memory.put("img_new", "person-a", "new", kept_until=None, created_at=101, path=db)

    assert prompt_memory.prune_person_before("person-a", 100, path=db) == 1
    assert prompt_memory.get("img_old", "person-a", path=db) is None
    assert prompt_memory.get("img_new", "person-a", path=db)


def test_maintenance_keeps_old_video_and_missing_asset_prompts(tmp_path):
    db = tmp_path / "media_jobs.db"
    now = 2_000_000_000
    prompt_memory.put("video_old", "person-a", "old video", kept_until=None, created_at=now - 15 * 86400, path=db)
    prompt_memory.put("video_new", "person-a", "new video", kept_until=None, created_at=now - 2 * 86400, path=db)
    prompt_memory.put("img_orphan", "person-a", "orphan", kept_until=None, created_at=now - 1, path=db)
    prompt_memory.put("img_present", "person-a", "present", kept_until=None, created_at=now - 1, path=db)

    assert prompt_memory.prune(path=db, now=now) == 0
    assert prompt_memory.get("video_old", "person-a", path=db)
    assert prompt_memory.get("img_orphan", "person-a", path=db)
    assert prompt_memory.get("video_new", "person-a", path=db)
    assert prompt_memory.get("img_present", "person-a", path=db)


def test_video_move_preserves_workflow_when_clean_cache_wrote_empty_destination(tmp_path):
    db = tmp_path / "jobs.db"
    prompt_memory.put("video_source", "maker", "original", kept_until=None, metadata={"workflow": "full graph"}, path=db)
    prompt_memory.put("vid_cached", "maker", "original", kept_until=None, metadata={}, path=db)
    assert prompt_memory.move("video_source", "vid_cached", "maker", path=db)
    assert prompt_memory.get("vid_cached", "maker", path=db)["metadata"] == {"workflow": "full graph"}
    assert prompt_memory.get("video_source", "maker", path=db) is None


def test_video_move_never_replaces_or_relaxes_destination_history(tmp_path):
    db = tmp_path / "jobs.db"
    deadline = int(time.time()) + 3600
    prompt_memory.put("vid_same", "maker", "earlier words", kept_until=deadline, path=db)
    prompt_memory.put("video_new", "maker", "later words", kept_until=None, path=db)
    assert not prompt_memory.move("video_new", "vid_same", "maker", path=db)
    kept = prompt_memory.get("vid_same", "maker", path=db)
    assert (kept["prompt_text"], kept["kept_until"]) == ("earlier words", deadline)
    assert prompt_memory.get("video_new", "maker", path=db)["prompt_text"] == "later words"

    prompt_memory.put("video_repeat", "maker", "earlier words", kept_until=None, path=db)
    assert prompt_memory.move("video_repeat", "vid_same", "maker", path=db)
    assert prompt_memory.get("vid_same", "maker", path=db)["kept_until"] == deadline
    assert prompt_memory.get("video_repeat", "maker", path=db) is None

    assert not prompt_memory.move("video_new", "video_new", "maker", path=db)
    assert prompt_memory.get("video_new", "maker", path=db)["prompt_text"] == "later words"


def test_fresh_household_fence_read_does_not_request_a_second_write_lock(tmp_path):
    from gateway import admin_store

    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    # The outer transaction can create its fence table before committing it.
    # A read on another connection must not try to create that table again.
    with prompt_memory.retention_guard(admin_db):
        assert prompt_memory.household_erasure_cutoff(admin_path=admin_db) is None
