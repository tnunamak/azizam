from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from gateway import provider_configuration
from gateway.litellm_substrate import secret_env_name_for_provider
from gateway.provider_sidecar import (
    build_sidecar_env,
    main,
    provider_secret_env_map_from_db,
    provider_secret_env_map_from_registry,
    provider_secret_fingerprint,
)
from gateway.secret_store import delete_provider_secret, put_provider_secret


def test_registry_contract_maps_api_key_env_to_encrypted_secret_id() -> None:
    registry = {
        "services": {
            "openrouter": {
                "kind": "provider",
                "config": {
                    "provider_id": "openrouter",
                    "api_key_secret_id": "provider:openrouter:api_key",
                },
                "secret_refs": {"api_key_secret_id": "provider:openrouter:api_key"},
            },
            "env-only": {
                "kind": "provider",
                "config": {"api_key_env": "OPENAI_API_KEY"},
                "secret_refs": {"api_key_env": "OPENAI_API_KEY"},
            },
        }
    }

    assert provider_secret_env_map_from_registry(registry) == {
        provider_configuration.provider_secret_env("openrouter"): "provider:openrouter:api_key"
    }


def test_build_sidecar_env_injects_decrypted_secret_without_printing_it(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    put_provider_secret("sec_openrouter", "sk-live-secret", db_path=db_path, now=1000)

    env = build_sidecar_env(
        db_path=db_path,
        base_env={"PATH": os.environ.get("PATH", "")},
        env_map={"OPENROUTER_API_KEY": "sec_openrouter"},
    )

    assert env["OPENROUTER_API_KEY"] == "sk-live-secret"
    assert env["PATH"] == os.environ.get("PATH", "")


def test_created_provider_service_maps_to_sidecar_env_and_rotates(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    service = provider_configuration.provider_service_from_body(
        {
            "preset_id": "openrouter",
            "models": [{"id": "anthropic/claude-3-haiku", "price": {"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "2"}}],
            "api_key": "sk-not-stored-here",
        }
    )
    secret_id = service["config"]["api_key_secret_id"]
    env_name = provider_configuration.provider_secret_env("openrouter")
    _write_registry_json(
        db_path,
        {"services": {"openrouter": service}},
    )

    empty = provider_secret_fingerprint(db_path)
    put_provider_secret(secret_id, "sk-first", db_path=db_path, now=1000, approved_origin=service["config"]["credential_origin"])
    first = provider_secret_fingerprint(db_path)
    first_env = build_sidecar_env(db_path=db_path, base_env={})
    put_provider_secret(secret_id, "sk-second", db_path=db_path, now=1001, approved_origin=service["config"]["credential_origin"])
    second = provider_secret_fingerprint(db_path)
    second_env = build_sidecar_env(db_path=db_path, base_env={})
    delete_provider_secret(secret_id, db_path=db_path)
    deleted = provider_secret_fingerprint(db_path)
    deleted_env = build_sidecar_env(db_path=db_path, base_env={env_name: "stale"})

    assert len({empty, first, second, deleted}) == 4
    assert first_env[env_name] == "sk-first"
    assert second_env[env_name] == "sk-second"
    assert env_name not in deleted_env


def test_sidecar_refuses_stored_secret_after_provider_origin_retarget(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    service = provider_configuration.provider_service_from_body(
        {
            "preset_id": "openrouter",
            "base_url": "https://evil.test/api/v1",
            "models": [{"id": "anthropic/claude-3-haiku", "price": {"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "2"}}],
            "api_key": "sk-not-stored-here",
        }
    )
    secret_id = service["config"]["api_key_secret_id"]
    env_name = provider_configuration.provider_secret_env("openrouter")
    _write_registry_json(db_path, {"services": {"openrouter": service}})
    put_provider_secret(
        secret_id,
        "sk-openrouter-origin",
        db_path=db_path,
        approved_origin="https://openrouter.ai/api/v1",
    )

    env = build_sidecar_env(db_path=db_path, base_env={env_name: "stale"})

    assert env_name not in env


def test_sidecar_uses_actual_base_url_not_stale_credential_origin(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    service = provider_configuration.provider_service_from_body(
        {
            "preset_id": "openrouter",
            "models": [{"id": "anthropic/claude-3-haiku", "price": {"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "2"}}],
            "api_key": "sk-not-stored-here",
        }
    )
    secret_id = service["config"]["api_key_secret_id"]
    env_name = provider_configuration.provider_secret_env("openrouter")
    service["config"]["base_url"] = "https://evil.test/api/v1"
    service["config"]["credential_origin"] = "https://openrouter.ai:443"
    _write_registry_json(db_path, {"services": {"openrouter": service}})
    put_provider_secret(
        secret_id,
        "sk-openrouter-origin",
        db_path=db_path,
        approved_origin="https://openrouter.ai/api/v1",
    )

    env = build_sidecar_env(db_path=db_path, base_env={env_name: "stale"})

    assert env_name not in env


def test_sidecar_omits_registry_secret_when_actual_base_url_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    _write_registry_json(
        db_path,
        {
            "services": {
                "openrouter": {
                    "config": {
                        "provider_id": "openrouter",
                        "api_key_secret_id": "sec_openrouter",
                        "credential_origin": "https://openrouter.ai:443",
                    },
                    "secret_refs": {"api_key_secret_id": "sec_openrouter"},
                }
            }
        },
    )
    put_provider_secret(
        "sec_openrouter",
        "sk-live-secret",
        db_path=db_path,
        approved_origin="https://openrouter.ai/api/v1",
    )
    env_name = secret_env_name_for_provider("openrouter", "sec_openrouter")

    env = build_sidecar_env(db_path=db_path, base_env={env_name: "stale"})

    assert env_name not in env


def test_env_map_can_come_from_admin_db_service_registry(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    _write_registry_json(
        db_path,
        {
            "schema_version": 1,
            "services": {
                "openrouter": {
                    "kind": "provider",
                    "capabilities": ["openai"],
                    "config": {
                        "remote_provider": True,
                        "provider_type": "paid-remote",
                        "provider_id": "openrouter",
                        "api_key_secret_id": "sec_openrouter",
                        "models": [],
                    },
                    "secret_refs": {"api_key_secret_id": "sec_openrouter"},
                }
            },
        },
    )

    assert provider_secret_env_map_from_db(db_path) == {
        secret_env_name_for_provider("openrouter", "sec_openrouter"): "sec_openrouter"
    }


def test_run_exec_uses_secret_env_without_putting_secret_in_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    _write_registry_json(
        db_path,
        {
            "services": {
                "openrouter": {
                    "config": {
                        "provider_id": "openrouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key_secret_id": "sec_openrouter",
                    },
                    "secret_refs": {"api_key_secret_id": "sec_openrouter"},
                }
            }
        },
    )
    put_provider_secret("sec_openrouter", "sk-live-secret", db_path=db_path, now=1000, approved_origin="https://openrouter.ai/api/v1")
    captured: dict[str, object] = {}

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        captured["file"] = file
        captured["args"] = args
        captured["env"] = env
        raise SystemExit(23)

    monkeypatch.setattr(os, "execvpe", fake_execvpe)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "run",
                "--db",
                str(db_path),
                "--",
                "litellm",
                "--config",
                "litellm.config.yaml",
            ]
        )

    assert exc.value.code == 23
    assert captured["file"] == "litellm"
    assert captured["args"] == ["litellm", "--config", "litellm.config.yaml"]
    assert "sk-live-secret" not in repr(captured["args"])
    env_name = secret_env_name_for_provider("openrouter", "sec_openrouter")
    assert captured["env"][env_name] == "sk-live-secret"  # type: ignore[index]


def test_missing_referenced_secret_omits_env_without_blocking_start(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    _write_registry_json(
        db_path,
        {
            "services": {
                "openrouter": {
                    "config": {
                        "provider_id": "openrouter",
                        "api_key_secret_id": "sec_missing",
                    },
                    "secret_refs": {"api_key_secret_id": "sec_missing"},
                }
            }
        },
    )

    env = build_sidecar_env(
        db_path=db_path,
        base_env={
            secret_env_name_for_provider("openrouter", "sec_missing"): "stale",
            "PATH": os.environ.get("PATH", ""),
        },
    )

    assert secret_env_name_for_provider("openrouter", "sec_missing") not in env


def test_missing_encryption_key_file_omits_provider_env_without_blocking_locals(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    _write_registry_json(
        db_path,
        {
            "services": {
                "openrouter": {
                    "config": {
                        "provider_id": "openrouter",
                        "api_key_secret_id": "sec_openrouter",
                    },
                    "secret_refs": {"api_key_secret_id": "sec_openrouter"},
                }
            }
        },
    )
    put_provider_secret("sec_openrouter", "sk-live-secret", db_path=db_path, now=1000)
    (tmp_path / "provider-secrets.key").unlink()

    env = build_sidecar_env(
        db_path=db_path,
        base_env={"PATH": os.environ.get("PATH", "")},
    )

    assert env == {"PATH": os.environ.get("PATH", "")}


def _write_registry_json(db_path: Path, registry: dict[str, object]) -> None:
    import json

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_settings (
              key TEXT PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              updated_by TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO operator_settings(key, value_json, updated_at, updated_by)
            VALUES ('service_registry', ?, 1, 'test')
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """,
            (json.dumps(registry, sort_keys=True),),
        )
