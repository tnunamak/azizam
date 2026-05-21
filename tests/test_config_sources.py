from gateway.config_sources import inventory_config_sources


def test_secrets_are_redacted_for_config_and_env():
    rows = inventory_config_sources(
        {
            "api_keys": {"primary": "sk-live-secret"},
            "oidc": {"client_secret": "oidc-secret", "issuer": "https://issuer.example"},
        },
        env={"OPENAI_API_KEY": "sk-env-secret", "OIDC_CLIENT_ID": "client-id"},
        env_names=["OPENAI_API_KEY", "OIDC_CLIENT_ID"],
    )

    by_id = {row["id"]: row for row in rows}

    assert by_id["config:api_keys"]["secret"] is True
    assert by_id["config:api_keys"]["current_value_summary"] == "<redacted>"
    assert by_id["env:OPENAI_API_KEY"]["secret"] is True
    assert by_id["env:OPENAI_API_KEY"]["current_value_summary"] == "<redacted>"
    assert by_id["env:OIDC_CLIENT_ID"]["secret"] is False
    assert by_id["env:OIDC_CLIENT_ID"]["current_value_summary"] == "client-id"
    assert "sk-live-secret" not in repr(rows)
    assert "sk-env-secret" not in repr(rows)


def test_env_overrides_are_represented_without_reading_process_env():
    rows = inventory_config_sources(
        {"proxy": {"host": "127.0.0.1", "port": 8080}},
        env={
            "DATABASE_URL": "sqlite:///admin.db",
            "REPORTING_DSN": "postgres://reporter:secret@example.test/db",
        },
        env_names=["DATABASE_URL", "ANTHROPIC_API_KEY", "REPORTING_DSN"],
    )

    by_id = {row["id"]: row for row in rows}

    assert by_id["env:DATABASE_URL"] == {
        "id": "env:DATABASE_URL",
        "label": "Database environment: DATABASE_URL",
        "domain": "auth/admin",
        "source": "env",
        "source_ref": "DATABASE_URL",
        "ui_owner": "env",
        "editability": "restart",
        "secret": True,
        "current_value_summary": "<redacted>",
        "recommendation": "Keep bootstrap storage wiring in env.",
    }
    assert by_id["env:REPORTING_DSN"]["secret"] is True
    assert by_id["env:REPORTING_DSN"]["current_value_summary"] == "<redacted>"
    assert by_id["env:ANTHROPIC_API_KEY"]["current_value_summary"] == "<not set>"
    assert by_id["env:ANTHROPIC_API_KEY"]["secret"] is True


def test_ui_file_env_and_manifest_categories_are_sane():
    rows = inventory_config_sources(
        {
            "proxy": {"host": "0.0.0.0"},
            "backends": {"openai": {"enabled": True}},
            "model_routing": {"gpt-4.1": "openai"},
            "images": {"signed_url_secret": "super-secret"},
            "roles": {"admin": ["gpt-4.1"]},
            "image_cache": {"max_entries": 100},
            "resource_admission": {"max_concurrent": 4},
            "model_compatibility": {"gpt-image-1": ["image"]},
            "auth": {"mode": "oidc"},
            "user_defaults": {"model": "gpt-4.1-mini"},
            "workflows": {"video": {"manifest": "workflows/video.yml"}},
        },
        env={"OIDC_CLIENT_SECRET": "secret"},
        env_names=["OIDC_CLIENT_SECRET"],
        manifest_rows=[
            {"id": "video-default", "kind": "video", "path": "workflows/video.yml"},
            {"id": "tts-default", "kind": "audio", "path": "workflows/audio.yml"},
        ],
        admin_db_summary={"runtime_overrides": 3, "admins": 2},
    )

    by_id = {row["id"]: row for row in rows}

    assert by_id["config:proxy"]["ui_owner"] == "file"
    assert by_id["config:proxy"]["editability"] == "restart"
    assert by_id["config:model_routing"]["ui_owner"] == "mixed"
    assert by_id["config:model_routing"]["editability"] == "advanced"
    assert by_id["config:images.signed_url_secret"]["secret"] is True
    assert by_id["config:images.signed_url_secret"]["current_value_summary"] == "<redacted>"
    assert by_id["config:roles"]["ui_owner"] == "ui"
    assert by_id["config:resource_admission"]["ui_owner"] == "ui"
    assert by_id["config:backends"]["ui_owner"] == "mixed"
    assert by_id["config:image_cache"]["ui_owner"] == "mixed"
    assert by_id["config:model_compatibility"]["ui_owner"] == "file"
    assert by_id["config:workflows"]["ui_owner"] == "manifest"
    assert by_id["env:OIDC_CLIENT_SECRET"]["ui_owner"] == "env"
    assert by_id["manifest:video-default"]["ui_owner"] == "manifest"
    assert by_id["manifest:video-default"]["domain"] == "video workflows"
    assert by_id["manifest:tts-default"]["domain"] == "audio workflows"
    assert by_id["admin-db"]["ui_owner"] == "ui"


def test_rows_are_jsonable_stable_and_sorted():
    rows = inventory_config_sources(
        {"roles": {"user": ["gpt-4.1-mini"]}, "proxy": {"port": 8080}},
        env={"BETA_FLAG": "1"},
        env_names=["BETA_FLAG", "BETA_FLAG"],
        manifest_rows=[{"id": "audio-z", "kind": "audio"}],
    )

    assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)
    assert [row["id"] for row in rows].count("env:BETA_FLAG") == 1
    assert rows == inventory_config_sources(
        {"roles": {"user": ["gpt-4.1-mini"]}, "proxy": {"port": 8080}},
        env={"BETA_FLAG": "1"},
        env_names=["BETA_FLAG", "BETA_FLAG"],
        manifest_rows=[{"id": "audio-z", "kind": "audio"}],
    )

    for row in rows:
        assert set(row) == {
            "id",
            "label",
            "domain",
            "source",
            "source_ref",
            "ui_owner",
            "editability",
            "secret",
            "current_value_summary",
            "recommendation",
        }
