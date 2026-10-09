"""Prompt text has one maker-scoped durable home, independent of media bytes."""

import time

from gateway import prompt_memory


def test_asset_prompt_is_maker_only_and_expires(tmp_path):
    db = tmp_path / "data" / "media_jobs.db"
    prompt_memory.put("img_abc", "person-a", "a private blue dragon", kept_until=None, path=db)
    assert prompt_memory.get("img_abc", "person-a", path=db)["prompt_text"] == "a private blue dragon"
    assert prompt_memory.get("img_abc", "person-b", path=db) is None
    prompt_memory.put("img_expired", "person-a", "expired text", kept_until=int(time.time()) - 1, path=db)
    assert prompt_memory.get("img_expired", "person-a", path=db) is None
    assert prompt_memory.prune(path=db) == 1


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
