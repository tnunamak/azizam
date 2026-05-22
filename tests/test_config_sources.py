import re
from pathlib import Path

from gateway.config_sources import CONFIG_SPECS, inventory_config_sources


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
        "plane": "bootstrap",
        "plane_label": "Bootstrap config",
        "change_path": "Set DATABASE_URL in the service environment and restart.",
        "restart_required": True,
        "secret": True,
        "current_value_summary": "<redacted>",
        "recommendation": "Keep bootstrap storage wiring in env.",
        "authored_via": "Service environment",
        "stored_in": "service environment",
        "writable": {"dashboard": False, "api": False},
        "provisioning": "secret_bootstrap",
        "apply_mode": "restart",
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
    assert by_id["config:proxy"]["plane"] == "bootstrap"
    assert by_id["config:proxy"]["restart_required"] is True
    assert by_id["config:model_routing"]["ui_owner"] == "mixed"
    assert by_id["config:model_routing"]["editability"] == "advanced"
    assert by_id["config:model_routing"]["plane"] == "split"
    assert by_id["config:images.signed_url_secret"]["secret"] is True
    assert by_id["config:images.signed_url_secret"]["current_value_summary"] == "<redacted>"
    assert by_id["config:roles"]["ui_owner"] == "ui"
    assert by_id["config:roles"]["plane"] == "dashboard"
    assert by_id["config:roles"]["restart_required"] is False
    assert by_id["config:resource_admission"]["ui_owner"] == "ui"
    assert by_id["config:backends"]["ui_owner"] == "mixed"
    assert by_id["config:image_cache"]["ui_owner"] == "mixed"
    assert by_id["config:model_compatibility"]["ui_owner"] == "file"
    assert by_id["config:workflows"]["ui_owner"] == "manifest"
    assert by_id["env:OIDC_CLIENT_SECRET"]["ui_owner"] == "env"
    assert by_id["env:OIDC_CLIENT_SECRET"]["change_path"] == (
        "Set OIDC_CLIENT_SECRET in the service environment and restart."
    )
    assert by_id["manifest:video-default"]["ui_owner"] == "manifest"
    assert by_id["manifest:video-default"]["plane"] == "provisioned"
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
            "plane",
            "plane_label",
            "change_path",
            "restart_required",
            "secret",
            "current_value_summary",
            "recommendation",
            "authored_via",
            "stored_in",
            "writable",
            "provisioning",
            "apply_mode",
        }


def test_lifecycle_semantics_per_ownership_category():
    rows = inventory_config_sources(
        {
            "proxy": {"host": "0.0.0.0"},
            "roles": {"admin": ["gpt-4.1"]},
            "backends": {"openai": {"enabled": True}},
            "images": {"signed_url_secret": "super-secret"},
            "workflows": {"video": {"manifest": "workflows/video.yml"}},
        },
        env={"OIDC_CLIENT_SECRET": "shh", "DATABASE_URL": "sqlite:///admin.db"},
        env_names=["OIDC_CLIENT_SECRET", "DATABASE_URL"],
        manifest_rows=[{"id": "video-default", "kind": "video", "path": "workflows/video.yml"}],
        admin_db_summary={"runtime_overrides": 1},
    )
    by_id = {row["id"]: row for row in rows}

    # Dashboard-owned: UI authors it, it lives in admin.db, both dashboard and API can write it,
    # it belongs in a provisioning bundle, and changes take effect live.
    dashboard = by_id["config:roles"]
    assert dashboard["authored_via"] == "Dashboard/API"
    assert dashboard["stored_in"] == "admin.db plus proxy_config.yml defaults"
    assert dashboard["writable"] == {"dashboard": True, "api": True}
    assert dashboard["provisioning"] == "bundle_candidate"
    assert dashboard["apply_mode"] == "live"

    # Admin-db summary row should report the same lifecycle as other dashboard-owned rows.
    admin_db = by_id["admin-db"]
    assert admin_db["authored_via"] == "Dashboard/API"
    assert admin_db["stored_in"] == "admin.db"
    assert admin_db["writable"] == {"dashboard": True, "api": True}
    assert admin_db["provisioning"] == "bundle_candidate"
    assert admin_db["apply_mode"] == "live"

    # File-owned bootstrap: authored in proxy_config.yml, not dashboard-writable,
    # bootstrap_only (never in a provisioning bundle), restart to apply.
    file_row = by_id["config:proxy"]
    assert file_row["authored_via"] == "proxy_config.yml"
    assert file_row["stored_in"] == "proxy_config.yml"
    assert file_row["writable"] == {"dashboard": False, "api": False}
    assert file_row["provisioning"] == "bootstrap_only"
    assert file_row["apply_mode"] == "restart"

    # File-owned secret: stays bootstrap-only but flagged as secret_bootstrap so the
    # provisioning bundle generator can exclude it.
    secret_file_row = by_id["config:images.signed_url_secret"]
    assert secret_file_row["secret"] is True
    assert secret_file_row["writable"] == {"dashboard": False, "api": False}
    assert secret_file_row["provisioning"] == "secret_bootstrap"
    assert secret_file_row["apply_mode"] == "restart"

    # Env-owned secret: env-managed, secret_bootstrap, restart.
    env_secret = by_id["env:OIDC_CLIENT_SECRET"]
    assert env_secret["authored_via"] == "Service environment"
    assert env_secret["stored_in"] == "service environment"
    assert env_secret["writable"] == {"dashboard": False, "api": False}
    assert env_secret["provisioning"] == "secret_bootstrap"
    assert env_secret["apply_mode"] == "restart"

    # Env-owned non-secret would be plain bootstrap_only — but DATABASE_URL is detected
    # as secret (creds-bearing URL), so we lock the secret variant here too.
    env_db = by_id["env:DATABASE_URL"]
    assert env_db["provisioning"] == "secret_bootstrap"

    # Mixed/split: authored in both planes, dashboard+API writable for the runtime slice,
    # provisioning=split, apply_mode=split so renderers can call out both paths.
    mixed = by_id["config:backends"]
    assert mixed["authored_via"] == "Dashboard plus proxy_config.yml"
    assert mixed["stored_in"] == "admin.db plus proxy_config.yml"
    assert mixed["writable"] == {"dashboard": True, "api": True}
    assert mixed["provisioning"] == "split"
    assert mixed["apply_mode"] == "split"

    # Manifest-owned workflow definitions are read-only artifacts; dashboard shows status only.
    manifest_yaml = by_id["config:workflows"]
    assert manifest_yaml["authored_via"] == "Workflow manifest"
    assert manifest_yaml["stored_in"] == "workflow manifest plus proxy_config.yml pointer"
    assert manifest_yaml["writable"] == {"dashboard": False, "api": False}
    assert manifest_yaml["provisioning"] == "source_manifest"
    assert manifest_yaml["apply_mode"] == "read_only"

    manifest_row = by_id["manifest:video-default"]
    assert manifest_row["provisioning"] == "source_manifest"
    assert manifest_row["apply_mode"] == "read_only"


def test_legacy_compat_fields_are_preserved():
    rows = inventory_config_sources(
        {"proxy": {"host": "0.0.0.0"}, "roles": {"admin": ["gpt-4.1"]}},
    )
    by_id = {row["id"]: row for row in rows}

    # plane, change_path, and restart_required keep their previous semantics.
    assert by_id["config:proxy"]["plane"] == "bootstrap"
    assert by_id["config:proxy"]["plane_label"] == "Bootstrap config"
    assert by_id["config:proxy"]["restart_required"] is True
    assert "Edit proxy_config.yml field proxy and restart." == by_id["config:proxy"]["change_path"]
    assert by_id["config:roles"]["plane"] == "dashboard"
    assert by_id["config:roles"]["restart_required"] is False
    assert by_id["config:roles"]["change_path"] == "Manage in this dashboard."


def test_doc_and_inventory_agree_on_domains():
    """Pin docs/CONFIG_SOURCE_OF_TRUTH.md to CONFIG_SPECS so neither drifts.

    The doc is the operator-facing source of truth for ownership policy; the
    inventory is the machine-facing source. They must list the same domains.
    """

    doc_path = Path(__file__).resolve().parent.parent / "docs" / "CONFIG_SOURCE_OF_TRUTH.md"
    doc_text = doc_path.read_text(encoding="utf-8")

    inventory_domains = {spec.domain for spec in CONFIG_SPECS}
    doc_domains = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", doc_text, flags=re.MULTILINE))

    assert doc_domains == inventory_domains
