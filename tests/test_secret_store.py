from __future__ import annotations

import stat
import sqlite3
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gateway.secret_store import (
    delete_provider_secret,
    export_provider_secret_env,
    get_provider_secret,
    provider_secret_status,
    put_provider_secret,
)
import pytest
from cryptography.exceptions import InvalidTag


def test_provider_secret_round_trip_is_encrypted_and_key_file_is_0600(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    status = put_provider_secret(
        "provider:openrouter:api_key",
        "sk-test-1234",
        db_path=db_path,
        now=1_700_000_000,
    )

    assert status.to_dict() == {
        "id": "provider:openrouter:api_key",
        "state": "set",
        "suffix": "1234",
        "updated_at": 1_700_000_000,
    }
    assert get_provider_secret("provider:openrouter:api_key", db_path=db_path) == "sk-test-1234"
    assert b"sk-test-1234" not in db_path.read_bytes()
    assert stat.S_IMODE((tmp_path / "provider-secrets.key").stat().st_mode) == 0o600
    assert export_provider_secret_env(
        db_path,
        {"AZIZAM_PROVIDER_SECRET_OPENROUTER": "provider:openrouter:api_key"},
    ) == {"AZIZAM_PROVIDER_SECRET_OPENROUTER": "sk-test-1234"}


def test_provider_secret_delete_returns_write_only_missing_status(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    put_provider_secret("provider:openai:api_key", "sk-test-abcd", db_path=db_path)

    status = delete_provider_secret("provider:openai:api_key", db_path=db_path)

    assert status.to_dict()["state"] == "missing"
    assert provider_secret_status("provider:openai:api_key", db_path=db_path).to_dict()["state"] == "missing"


def test_secret_reads_fail_closed_when_key_file_is_missing_and_short_values_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "admin.db"
    put_provider_secret("provider:test:api_key", "sk-test-long", db_path=db_path)
    (tmp_path / "provider-secrets.key").unlink()

    with pytest.raises(FileNotFoundError):
        get_provider_secret("provider:test:api_key", db_path=db_path)
    assert not (tmp_path / "provider-secrets.key").exists()

    with pytest.raises(ValueError):
        put_provider_secret("provider:test:api_key", "1234", db_path=db_path)


def test_concurrent_first_writes_share_one_complete_key(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    def put(index: int) -> None:
        put_provider_secret(f"provider:{index}:api_key", f"synthetic-concurrent-key-{index}", db_path=db)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(put, range(12)))
    for index in range(12):
        assert get_provider_secret(f"provider:{index}:api_key", db_path=db) == f"synthetic-concurrent-key-{index}"


def test_secret_store_refuses_a_symlink_key_file(tmp_path: Path) -> None:
    target = tmp_path / "other-file"
    target.write_bytes(b"operator-owned")
    (tmp_path / "provider-secrets.key").symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        put_provider_secret("provider:test:api_key", "synthetic-key", db_path=tmp_path / "admin.db")
    assert target.read_bytes() == b"operator-owned"


def test_ciphertext_is_authenticated_and_bound_to_record_identity(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    put_provider_secret("provider:first:api_key", "synthetic-authenticated-key", db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE provider_secrets SET id = ?", ("provider:other:api_key",))
    with pytest.raises(InvalidTag):
        get_provider_secret("provider:other:api_key", db_path=db)
    with sqlite3.connect(db) as conn:
        encoded = conn.execute("SELECT ciphertext_b64 FROM provider_secrets").fetchone()[0]
        data = bytearray(base64.b64decode(encoded))
        data[0] ^= 1
        conn.execute("UPDATE provider_secrets SET id = ?, ciphertext_b64 = ?", ("provider:first:api_key", base64.b64encode(data).decode()))
    with pytest.raises(InvalidTag):
        get_provider_secret("provider:first:api_key", db_path=db)


def test_provider_secret_can_be_bound_to_approved_origin(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    put_provider_secret(
        "provider:openrouter:api_key",
        "synthetic-origin-key",
        db_path=db,
        approved_origin="https://OpenRouter.ai/api/v1",
    )

    assert get_provider_secret(
        "provider:openrouter:api_key",
        db_path=db,
        expected_origin="https://openrouter.ai:443/other-path",
    ) == "synthetic-origin-key"
    with pytest.raises(PermissionError):
        get_provider_secret(
            "provider:openrouter:api_key",
            db_path=db,
            expected_origin="https://evil.test/api/v1",
        )
    with pytest.raises(PermissionError):
        get_provider_secret(
            "provider:openrouter:api_key",
            db_path=db,
            expected_origin="http://openrouter.ai/api/v1",
        )


def test_provider_secret_origin_column_is_authenticated(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    put_provider_secret(
        "provider:openrouter:api_key",
        "synthetic-origin-key",
        db_path=db,
        approved_origin="https://openrouter.ai/api/v1",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE provider_secrets SET approved_origin = ? WHERE id = ?",
            ("https://evil.test:443", "provider:openrouter:api_key"),
        )

    with pytest.raises(InvalidTag):
        get_provider_secret(
            "provider:openrouter:api_key",
            db_path=db,
            expected_origin="https://evil.test/api/v1",
        )


def test_ipv6_credential_origin_normalization_is_idempotent() -> None:
    from gateway.secret_store import credential_origin_from_url

    origin = credential_origin_from_url("https://[::1]:8443/v1")

    assert origin == "https://[::1]:8443"
    assert credential_origin_from_url(origin) == origin
