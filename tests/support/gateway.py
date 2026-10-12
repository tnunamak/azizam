"""Point the in-process gateway at fake upstreams through its real registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import admin_store
from tests.registry_test_helpers import write_service_registry_for_test

LITELLM_KEY_ENV = "FK_LITELLM_MASTER_KEY"
LITELLM_MASTER_KEY = "fk-litellm-master-key"


def use_admin_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Give the gateway a fresh, real admin store for this test."""
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    return db


def register_litellm(monkeypatch: pytest.MonkeyPatch, db: Path, base_url: str, **config: Any) -> None:
    """Register the LLM execution substrate (an openai-compatible upstream) at ``base_url``."""
    monkeypatch.setenv(LITELLM_KEY_ENV, LITELLM_MASTER_KEY)
    write_service_registry_for_test(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": base_url, "api_key_env": LITELLM_KEY_ENV, **config},
                }
            }
        },
        path=db,
    )
