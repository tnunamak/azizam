"""C8c of the config-homes program: model_compatibility and identity move into config sections (CONFIG_HOMES.md, 10.1).

The generated OpenAI model list is release data under ``registry/``; its path is no longer a setting.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

import proxy
from gateway import admin_store, config_legacy_import
from tests.config_homes_golden import effective_config, raw_config
from tests.support.config_snapshot import section_published

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "registry" / "defaults"
C8C_SECTIONS = ("identity", "model_compatibility")
ABSOLUTE_HOST_PATH = re.compile(r"(?<![\w.:])/(?:media|mnt|home|Users|opt|srv|var|root|etc)(?:/|\b)")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
MOVED_CONSTANTS = (
    "MODEL_COMPAT_ENABLED", "OPENAI_MODEL_ALIASES_FILE", "OPENAI_MODELS_API_URL",
    "OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE", "MODEL_COMPAT_DEFAULTS", "MODEL_COMPAT_ALIASES",
    "TTS_BACKEND_MODEL", "STT_BACKEND_MODEL", "EMBEDDINGS_MODEL", "IMAGE_EDIT_DEFAULT_MODEL", "IDENTITY_CFG",
)


def _owner_set(db: Path, section: str, pointer: str, value, base) -> None:
    state = admin_store.read_config_state(path=db)
    item = admin_store.ConfigItem(section, pointer, "set", json.dumps(value), json.dumps(base))
    admin_store.apply_config_change(
        [item], if_match={section: str(state["revisions"][section])}, kind="edit", origin="api",
        validate=proxy.CONFIG_HOME.write_validator(None), path=db,
    )


def _import(db: Path, startup: dict) -> dict[str, str]:
    admin_store.init_db(db)
    found = config_legacy_import.import_at_boot(proxy.CONFIG_HOME, copy.deepcopy(startup), path=db, registry=None,
                                                log=lambda line: None)
    return {name: outcome.status for name, outcome in found.outcomes.items()}


# -- the release files -----------------------------------------------------------------------------------------------

def test_each_c8c_section_ships_its_two_release_files_and_is_registered():
    for name in C8C_SECTIONS:
        assert (DEFAULTS / f"{name}.yml").is_file(), name
        assert (DEFAULTS / f"{name}.schema.json").is_file(), name
        assert name in proxy.CONFIG_HOME.sections, name


def test_the_release_files_hold_no_host_path_and_no_address():
    for name in C8C_SECTIONS:
        text = (DEFAULTS / f"{name}.yml").read_text(encoding="utf-8") + (DEFAULTS / f"{name}.schema.json").read_text(encoding="utf-8")
        assert not ABSOLUTE_HOST_PATH.search(text), name
        assert not IPV4.search(text), name


def test_the_release_names_no_model_and_no_face_service():
    compat = yaml.safe_load((DEFAULTS / "model_compatibility.yml").read_text(encoding="utf-8"))
    assert compat["defaults"] == {} and compat["aliases"] == {}
    assert "generated_aliases_file" not in compat
    identity = yaml.safe_load((DEFAULTS / "identity.yml").read_text(encoding="utf-8"))
    assert identity == {}


def test_the_release_ships_no_secret_value():
    """Neither section holds a credential: the schemas mark no secret and the files carry no key-like value."""
    for name in C8C_SECTIONS:
        text = (DEFAULTS / f"{name}.yml").read_text(encoding="utf-8").lower()
        for word in ("password", "secret", "token", "api_key", "sref_"):
            assert word not in text, (name, word)


# -- the generated model list is release data under registry/ ---------------------------------------------------------

def test_the_generated_model_list_lives_under_registry_and_nowhere_else():
    assert (ROOT / "registry" / "openai_model_aliases.generated.yml").is_file()
    assert not (ROOT / "openai_model_aliases.generated.yml").exists()


def test_the_generated_list_path_is_the_release_file_unless_the_environment_names_one(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL_ALIASES_FILE", raising=False)
    assert proxy.openai_model_aliases_file() == ROOT / "registry" / "openai_model_aliases.generated.yml"
    monkeypatch.setenv("OPENAI_MODEL_ALIASES_FILE", "/does/not/exist.yml")
    assert proxy.openai_model_aliases_file() == Path("/does/not/exist.yml")


def test_the_reader_and_the_generator_use_the_release_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_MODEL_ALIASES_FILE", raising=False)
    assert proxy.load_generated_openai_model_aliases(), "the release list is read from registry/"
    out = tmp_path / "list.yml"
    monkeypatch.setenv("OPENAI_MODEL_ALIASES_FILE", str(out))
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    class _Reply:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-x", "owned_by": "openai"}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            _Client.url = url
            return _Reply()

    monkeypatch.setattr(proxy.engine_egress, "async_client", lambda **kw: _Client())
    with section_published("model_compatibility", {"openai_models_api_url": "https://models.example.test/v1/models"}):
        assert asyncio.run(proxy.refresh_openai_model_aliases()) == 0
    assert _Client.url == "https://models.example.test/v1/models"
    written = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert written["source"] == "https://models.example.test/v1/models"
    assert [m["id"] for m in written["models"]] == ["gpt-x"]


# -- the legacy import gives the startup block -----------------------------------------------------------------------

def test_the_legacy_import_gives_each_section_its_startup_block(tmp_path):
    effective = effective_config(proxy, "tracked", tmp_path / "admin.db")
    startup = raw_config("tracked")
    expected = {key: value for key, value in startup["model_compatibility"].items() if key != "generated_aliases_file"}
    assert effective["model_compatibility"] == expected
    assert effective["identity"] == startup["identity"]


def test_a_first_run_has_the_release_values(tmp_path):
    effective = effective_config(proxy, "first_run", tmp_path / "admin.db")
    assert effective["model_compatibility"] == yaml.safe_load((DEFAULTS / "model_compatibility.yml").read_text(encoding="utf-8"))
    assert effective["identity"] == {}


def test_the_import_runs_once_and_a_second_boot_keeps_the_owner_edit(tmp_path):
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    first = _import(db, startup)
    assert {first[name] for name in C8C_SECTIONS} <= {"imported", "nothing_to_import"}
    _owner_set(db, "model_compatibility", "/defaults/speech", "owner-voice-model", base=None)
    second = _import(db, startup)
    assert {second[name] for name in C8C_SECTIONS} == {"already_imported"}
    rows = {row["pointer"]: row for row in admin_store.read_config_state(path=db)["rows"] if row["section"] == "model_compatibility"}
    assert json.loads(rows["/defaults/speech"]["value_json"]) == "owner-voice-model"


# -- the readers read the snapshot, and an owner edit is live --------------------------------------------------------

def test_the_moved_constants_are_gone_from_the_module():
    for name in MOVED_CONSTANTS:
        assert not hasattr(proxy, name), name


def test_the_compat_switch_follows_the_snapshot_and_the_environment_still_wins(monkeypatch):
    monkeypatch.delenv("AZIZAM_MODEL_COMPAT_ENABLED", raising=False)
    with section_published("model_compatibility", {"enabled": False}):
        assert proxy.model_compat_enabled() is False
    with section_published("model_compatibility", {"enabled": True}):
        assert proxy.model_compat_enabled() is True
    monkeypatch.setenv("AZIZAM_MODEL_COMPAT_ENABLED", "false")
    with section_published("model_compatibility", {"enabled": True}):
        assert proxy.model_compat_enabled() is False


def test_the_default_models_follow_the_snapshot():
    block = {"defaults": {"speech": "s-model", "transcriptions": "t-model", "embeddings": "e-model",
                          "image_edits": "i-model", "chat": "c-model"}}
    with section_published("model_compatibility", block):
        assert proxy.tts_backend_model() == "s-model"
        assert proxy.stt_backend_model() == "t-model"
        assert proxy.embeddings_model() == "e-model"
        assert proxy.image_edit_default_model() == "i-model"
        assert proxy.default_model_for_operation("chat", "asked") == "c-model"
        assert proxy.default_model_for_operation("speech") == "s-model"
    with section_published("model_compatibility", {"defaults": {}}):
        assert proxy.default_model_for_operation("chat", "asked") == "asked"
        assert proxy.image_edit_default_model() == "qwen-image-2.1-edit"


def test_the_configured_alias_categories_follow_the_snapshot():
    with section_published("model_compatibility", {"aliases": {"my-embedder": {"category": "embeddings"}, "plain": "images"}}):
        assert proxy.infer_openai_model_category("my-embedder") == "embeddings"
        assert proxy.infer_openai_model_category("plain") == "images"
    with section_published("model_compatibility", {"aliases": {}}):
        assert proxy.configured_model_alias_category("my-embedder") is None


def test_an_owner_edit_reaches_the_readers_through_the_layer_and_a_publish(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    startup = raw_config("tracked")
    _import(db, startup)
    _owner_set(db, "model_compatibility", "/defaults/speech", "edited-speech-model", base=startup["model_compatibility"]["defaults"]["speech"])
    _owner_set(db, "identity", "/max_wait_s", 99, base=None)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    effective = proxy._load_effective_runtime_config(copy.deepcopy(startup))
    monkeypatch.setattr(proxy, "config", effective)
    proxy.CONFIG_HOME.publish(proxy.CONFIG_HOME.snapshot(effective))
    assert proxy.tts_backend_model() == "edited-speech-model"
    assert proxy._identity_cfg()["max_wait_s"] == 99
    assert proxy.runtime().config["model_compatibility"]["defaults"]["transcriptions"] == startup["model_compatibility"]["defaults"]["transcriptions"]


def test_the_identity_readers_follow_the_snapshot():
    face = {"host": "face.example.test", "port": 9, "timeout": 7}
    with section_published("identity", {"max_wait_s": 12, "face_service": face}):
        assert proxy._identity_cfg()["max_wait_s"] == 12
        assert dict(proxy._identity_face_cfg()) == face
        assert proxy._workflow_availability_services({"services": {}})["services"]["faces"]["config"] == face
    with section_published("identity", {}):
        assert "max_wait_s" not in proxy._identity_cfg()
        assert dict(proxy._identity_face_cfg()) == {}
        assert "faces" not in proxy._workflow_availability_services({"services": {}})["services"]


def test_an_unconfigured_face_service_answers_not_configured():
    with section_published("identity", {}):
        response = asyncio.run(proxy._identity_ensure_face_service())
    assert response is not None and response.status_code == 503
    assert "not configured" in json.loads(response.body)["error"]["message"]


def test_the_status_page_reads_the_generated_model_list_where_the_gateway_does(monkeypatch, tmp_path):
    import proxy
    import status_endpoint

    monkeypatch.delenv("OPENAI_MODEL_ALIASES_FILE", raising=False)
    assert status_endpoint.openai_aliases_file() == proxy.openai_model_aliases_file()
    assert status_endpoint.openai_aliases_file().is_file()
    monkeypatch.setenv("OPENAI_MODEL_ALIASES_FILE", str(tmp_path / "list.yml"))
    assert status_endpoint.openai_aliases_file() == proxy.openai_model_aliases_file() == tmp_path / "list.yml"
