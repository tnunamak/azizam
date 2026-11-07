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
    assert _problems(["x"])["(file)"].startswith("The file must be a mapping")


def test_a_block_that_holds_only_comments_is_valid():
    assert startup_config.validate({"mcp": None, "request_body_limits": None, "admin": {"oidc": None}}) == []


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


def test_every_startup_only_name_of_a_section_is_a_startup_key_or_a_deliberate_move():
    """A section's ``x-azizam-startup-only`` names the keys its old file block held that the section never owns. Each
    must still have a home: a key of the startup schema, or an ``x-azizam-moved`` entry that says where it went. A name
    in neither would be refused at start with no place to put it."""
    from gateway import config_runtime

    keys = {key.path for key in startup_config.iter_keys()}
    moved = startup_config.load_schema()["x-azizam-moved"]
    homeless = []
    for section_id, section in config_runtime.load_sections().items():
        for name in section.schema.get("x-azizam-startup-only") or []:
            path = f"{section_id}.{name}"
            if path not in keys and path not in moved:
                homeless.append(path)
    assert homeless == []


def test_the_tracked_startup_file_is_valid_and_is_the_first_run_file():
    """A fresh install starts from the tracked file: local only, no sign-in provider, setup page claims the first owner.
    (The fresh-install journey, scripts/e2e/fresh_instance.sh, boots a gateway from it with no admin.db.)"""
    from pathlib import Path

    import yaml

    raw = yaml.safe_load((Path(__file__).resolve().parent.parent / "proxy_config.yml").read_text(encoding="utf-8"))
    startup_config.check(raw, source="proxy_config.yml")
    assert raw["proxy"]["host"] == "127.0.0.1"
    assert raw["admin"]["oidc"]["cookie"]["secure"] is False
    assert not (raw["admin"]["oidc"].keys() - {"cookie"}), "no sign-in provider ships in the tracked file"
    assert not (raw.keys() - {"proxy", "admin"}), "the tracked file holds only the listener and the cookie rule"


REMOVED_ENV_OVERRIDES = (
    "UPSTREAM_READ_TIMEOUT", "EMBEDDINGS_TIMEOUT", "AZIZAM_MODEL_COMPAT_ENABLED", "OPENAI_MODEL_ALIASES_FILE",
    "OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE", "IMAGE_MEDIA_JOB_RESUME_ON_START", "AZIZAM_PUBLIC_URL", "AZIZAM_BRAND",
    "COMFY_ENABLED", "COMFY_HOST", "AZIZAM_USER_SELF_SERVICE", "AZIZAM_AUTO_CREATE_PERSONAL_APP",
)


def test_no_gateway_code_reads_an_environment_override_of_a_setting_that_lives_in_admin_db():
    """F4 (docs/design/CONFIG_HOMES.md): an env var that overrides an owner setting is a second writable home. The
    variables listed in docs/CONFIG.md as removed must stay out of the gateway. (``scripts/safe-restart`` still reads
    ``AZIZAM_PUBLIC_URL`` from the operator's shell; it is not gateway code.)"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sources = [root / "proxy.py", *sorted((root / "gateway").glob("*.py"))]
    found = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for name in REMOVED_ENV_OVERRIDES:
            for quote in ('"', "'"):
                if f"environ.get({quote}{name}{quote}" in text or f"getenv({quote}{name}{quote}" in text \
                        or f"get_env({quote}{name}{quote}" in text or f"environ[{quote}{name}{quote}]" in text:
                    found.append(f"{path.name}: {name}")
    assert found == []
    documented = (root / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    assert [name for name in REMOVED_ENV_OVERRIDES if f"`{name}`" not in documented] == [], "list each removed variable in docs/CONFIG.md"
