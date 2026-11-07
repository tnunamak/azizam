"""C10: the startup file's closed schema (registry/startup.schema.json) and the check that refuses a key it lacks."""
from __future__ import annotations

import json

import pytest

from gateway import startup_config


def _problems(raw):
    return {problem.path: problem.message for problem in startup_config.validate(raw)}


def test_an_empty_file_and_a_file_of_startup_keys_are_valid():
    assert startup_config.validate(None) == []
    assert startup_config.validate({}) == []
    assert startup_config.validate({
        "proxy": {"host": "127.0.0.1", "port": 8000},
        "admin": {"db_path": "admin.db", "oidc": {"enabled": False, "cookie": {"secure": False}}},
        "auth": {"public_paths": ["/health"], "trusted_proxies": []},
        "images": {"cache_dir": "cache", "media_jobs": {"payload_dir": "jobs"}},
    }) == []


def test_a_key_that_moved_is_refused_with_its_new_home():
    problems = _problems({
        "backends": {}, "providers": {}, "routing": {}, "roles": {}, "llm": {"pin_loaded_model": True},
        "models": {}, "workflow_execution": {},
        "proxy": {"public_url": "https://example.test"},
        "admin": {"brand": {"product": "x"}, "user_defaults": {}},
        "gateway": {"resource_admission": {"media_jobs": {}}},
    })
    assert "service registry" in problems["backends"]
    assert "service registry" in problems["providers"]
    assert "delete it" in problems["routing"]
    assert "config section `roles`" in problems["roles"]
    assert "config section `llm`" in problems["llm"]
    assert "config section `models`" in problems["models"]
    assert "config section `workflow_execution`" in problems["workflow_execution"]
    assert "/base_url" in problems["proxy.public_url"]
    assert "/name" in problems["admin.brand.product"]
    assert "Access plans" in problems["admin.user_defaults"]
    assert "media-job-admission" in problems["gateway"]


def test_a_parent_of_a_moved_key_names_each_moved_child():
    message = _problems({"gateway": {"resource_admission": {}}})["gateway"]
    assert "gateway.resource_admission.media_jobs is config section `resource_admission`" in message


def test_a_key_under_a_section_names_the_entry_and_the_write_route():
    message = _problems({"instance": {"name": "x"}})["instance"]
    assert "PUT /admin/api/settings/instance" in message
    message = _problems({"llm": {"pin_loaded_model": False}})["llm"]
    assert "PATCH /admin/api/config/changes" in message


def test_an_unknown_key_says_nothing_reads_it():
    assert "Delete it" in _problems({"proxy": {"workers": 4}})["proxy.workers"]
    assert "Delete it" in _problems({"surprise": 1})["surprise"]


def test_a_removed_auth_key_is_named_as_removed():
    problems = _problems({"auth": {"enabled": True, "lan_bypass_enabled": True, "path_tokens": []}})
    for key in ("enabled", "lan_bypass_enabled", "path_tokens"):
        assert "is removed" in problems[f"auth.{key}"]


def test_a_wrong_type_is_refused_at_the_key():
    problems = _problems({"proxy": {"port": "8000"}, "auth": {"cloudflare_mode": "yes"}, "admin": "x"})
    assert "proxy.port" in problems and "auth.cloudflare_mode" in problems
    assert "must be a mapping" in problems["admin"]
    assert startup_config.validate({"proxy": {"port": 70000}})


def test_check_raises_one_line_per_fault():
    with pytest.raises(startup_config.StartupConfigError) as caught:
        startup_config.check({"backends": {}, "proxy": {"workers": 1}}, source="proxy_config.yml")
    text = str(caught.value)
    assert text.startswith("proxy_config.yml is not a valid startup file:")
    assert "`backends`" in text and "`proxy.workers`" in text


def test_a_root_that_is_not_a_mapping_is_refused():
    assert startup_config.validate(["x"])


def test_the_schema_is_closed_at_every_object():
    def walk(node, path):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, path
            for name, child in node["properties"].items():
                walk(child, f"{path}.{name}")

    walk(startup_config.load_schema(), "")


def test_every_moved_entry_names_a_real_place():
    moved = startup_config.load_schema()["x-azizam-moved"]
    sections = startup_config.section_ids()
    for head, where in moved.items():
        assert not startup_config.startup_paths() & {head}, f"{head} is both a startup key and a moved key"
        if "config section `" in where:
            name = where.split("config section `")[1].split("`")[0]
            assert name in sections, (head, name)
        assert where.strip(), head


def test_no_section_name_is_also_a_startup_top_level_key_except_the_listener_style_ones():
    """A top-level name belongs to one home: a section of admin.db or the startup schema, never both."""
    top = set(startup_config.load_schema()["properties"])
    overlap = top & startup_config.section_ids()
    assert overlap <= {"images", "voices", "audio", "model_routing"}, overlap  # sections that keep a storage root here


def test_every_env_name_is_declared_once():
    names = [key.env for key in startup_config.iter_keys() if key.env]
    assert len(names) == len(set(names))
    assert startup_config.startup_env_names() == frozenset(names)


def test_secrets_are_marked():
    secrets = {key.path for key in startup_config.iter_keys() if key.secret}
    assert {"admin.session_signing_key", "admin.oidc.client_secret", "admin.oidc.signing_key",
            "images.signed_url_secret"} <= secrets


def test_where_it_lives_prefers_the_longest_moved_entry():
    assert "instance" in startup_config.where_it_lives("admin.brand.product")
    assert startup_config.where_it_lives("nothing.here").startswith("nowhere")


def test_the_schema_file_is_valid_json_schema():
    json.loads(startup_config.SCHEMA_PATH.read_text(encoding="utf-8"))
    startup_config.load_schema()
