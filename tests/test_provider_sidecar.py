from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway import provider_configuration
from gateway.litellm_content import profile
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

    env = build_sidecar_env(
        db_path=db_path,
        base_env={"PATH": os.environ.get("PATH", "")},
        env_map={secret_env_name_for_provider("openrouter", "sec_openrouter"): "sec_openrouter"},
    )

    assert env[secret_env_name_for_provider("openrouter", "sec_openrouter")] == "sk-live-secret"
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


def test_explicit_env_map_still_requires_registered_origin(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    put_provider_secret(
        "sec_openrouter",
        "sk-live-secret",
        db_path=db_path,
        approved_origin="https://openrouter.ai/api/v1",
    )

    env = build_sidecar_env(
        db_path=db_path,
        base_env={"OPENROUTER_API_KEY": "stale"},
        env_map={"OPENROUTER_API_KEY": "sec_openrouter"},
    )

    assert "OPENROUTER_API_KEY" not in env


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
    config_path = tmp_path / "litellm.config.yaml"
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
                "--config",
                str(config_path),
                "--",
                "litellm",
                "--config",
                str(config_path),
            ]
        )

    assert exc.value.code == 23
    assert captured["file"] == sys.executable
    assert Path(captured["args"][1]).name == "litellm_launcher.py"
    assert captured["args"][-2:] == ["--config", str(config_path)]
    assert "sk-live-secret" not in repr(captured["args"])
    env_name = secret_env_name_for_provider("openrouter", "sec_openrouter")
    assert captured["env"][env_name] == "sk-live-secret"  # type: ignore[index]


def test_run_replaces_stale_config_and_uses_one_registry_snapshot_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    import yaml
    from gateway import provider_sidecar

    db_path = tmp_path / "admin.db"
    service = provider_configuration.provider_service_from_body(
        {
            "preset_id": "openrouter",
            "models": [{"id": "anthropic/claude-3-haiku", "price": {"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "2"}}],
            "api_key": "sk-unused",
        }
    )
    registry = {"services": {"openrouter": service}}
    _write_registry_json(db_path, registry)
    secret_id = service["config"]["api_key_secret_id"]
    secret = "sk-snapshot-test"
    put_provider_secret(secret_id, secret, db_path=db_path, approved_origin=service["config"]["credential_origin"])
    env_name = secret_env_name_for_provider("openrouter", secret_id)
    config_path = tmp_path / "selected.yaml"
    config_path.write_text("model_list:\n- litellm_params:\n    api_key: os.environ/AI_GATEWAY_PROVIDER_SECRET_STALE\n")
    reads = []
    real_read = provider_sidecar._read_service_registry

    def read_once(path: Path):
        reads.append(path)
        assert len(reads) == 1, "config and env must share one registry read"
        return real_read(path)

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        compiled = yaml.safe_load(config_path.read_text())
        key_refs = {entry["litellm_params"]["api_key"] for entry in compiled["model_list"]}
        assert key_refs == {"os.environ/" + env_name}
        assert env_name.startswith("AZIZAM_PROVIDER_SECRET_")
        assert env[env_name] == secret
        assert "AI_GATEWAY_PROVIDER_SECRET_" not in config_path.read_text()
        assert secret not in config_path.read_text()
        assert secret not in json.dumps(args)
        assert file == sys.executable
        assert Path(args[1]).name == "litellm_launcher.py"
        assert args[-2:] == ["--config", str(config_path)]
        raise SystemExit(23)

    monkeypatch.setattr(provider_sidecar, "_read_service_registry", read_once)
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    with pytest.raises(SystemExit) as exc:
        main(["run", "--db", str(db_path), "--config", str(config_path), "--", "litellm", "--config", str(config_path)])
    assert exc.value.code == 23
    assert reads == [db_path]
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_run_does_not_exec_when_config_compile_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from gateway import provider_sidecar

    def fail(*args, **kwargs):
        raise ValueError("invalid registry")

    def unexpected_exec(*args, **kwargs):
        pytest.fail("LiteLLM must not start with a stale config")

    monkeypatch.setattr(provider_sidecar, "write_litellm_config_artifact", fail)
    monkeypatch.setattr(os, "execvpe", unexpected_exec)
    _write_registry_json(tmp_path / "admin.db", {"services": {}})
    with pytest.raises(ValueError, match="invalid registry"):
        main(["run", "--db", str(tmp_path / "admin.db"), "--config", str(tmp_path / "selected.yaml"), "--", "litellm"])


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


def test_sidecar_resolver_failures_remove_stale_env_and_hide_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from gateway import provider_sidecar

    db_path = tmp_path / "admin.db"
    secret_id = "sec_failure_fixture"
    env_name = secret_env_name_for_provider("fixture", secret_id)
    _write_registry_json(
        db_path,
        {
            "services": {
                "fixture": {
                    "kind": "provider",
                    "config": {
                        "provider_id": "fixture",
                        "base_url": "https://provider.test/v1",
                        "api_key_secret_id": secret_id,
                    },
                }
            }
        },
    )
    base_env = {"PATH": "/synthetic/bin", env_name: "stale-value"}

    for failure_type in (KeyError, FileNotFoundError, ValueError):
        seen: list[tuple[str, Path, str]] = []

        def fail_resolution(secret, *, db_path, expected_origin, seen=seen, failure_type=failure_type):
            seen.append((secret, db_path, expected_origin))
            raise failure_type("synthetic-sensitive-error-detail")

        monkeypatch.setattr(provider_sidecar, "get_provider_secret", fail_resolution)
        result = build_sidecar_env(db_path=db_path, base_env=base_env)

        assert seen == [(secret_id, db_path, "https://provider.test:443")]
        assert result == {"PATH": "/synthetic/bin", **profile()["environment"]}
        assert base_env[env_name] == "stale-value"
        output = capsys.readouterr()
        assert failure_type.__name__ in output.err
        assert "synthetic-sensitive-error-detail" not in output.out + output.err
        assert "stale-value" not in output.out + output.err


def test_missing_encryption_key_file_omits_provider_env_without_blocking_locals(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    secret_id = "sec_openrouter"
    base_url = "https://openrouter.ai/api/v1"
    env_name = secret_env_name_for_provider("openrouter", secret_id)
    _write_registry_json(
        db_path,
        {
            "services": {
                "openrouter": {
                    "config": {
                        "provider_id": "openrouter",
                        "base_url": base_url,
                        "api_key_secret_id": secret_id,
                    },
                    "secret_refs": {"api_key_secret_id": secret_id},
                }
            }
        },
    )
    put_provider_secret(
        secret_id, "synthetic-sidecar-key",
        db_path=db_path, now=1000, approved_origin=base_url,
    )
    before = build_sidecar_env(db_path=db_path, base_env={"PATH": "/synthetic/bin"})
    assert before[env_name] == "synthetic-sidecar-key"

    (tmp_path / "provider-secrets.key").unlink()

    after = build_sidecar_env(
        db_path=db_path,
        base_env={"PATH": "/synthetic/bin", env_name: "stale-value"},
    )
    assert after == {"PATH": "/synthetic/bin", **profile()["environment"]}


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
