"""Keys for test Apps, issued the way the product issues them: by the App's accountable person.

An App key records the person it answers for as its creator (owner visibility step 2), and the
database refuses a key without that provenance. Fixtures that only need a live key use these helpers
instead of an owner's control-plane key, which answers for no person.
"""

from __future__ import annotations

from pathlib import Path

from gateway import admin_store
from gateway.admin_runtime import generate_app_api_key
from gateway.runtime_auth import hash_api_key


def issue_app_credential(path: Path, app_id: str, *, label: str = "test key") -> dict:
    """Create a key on the App. Returns its ``id``, its one-time ``api_key`` and its ``prefix``."""
    app = admin_store.get_app(app_id, path=path)
    api_key = generate_app_api_key()
    record = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app_id, app_id=app_id, hash=hash_api_key(api_key),
        prefix=api_key[:18], display_name=label, created_by=app["owner_user_id"], path=path,
    )
    return {"id": record["id"], "api_key": api_key, "prefix": record["prefix"]}


def issue_app_key(path: Path, app_id: str, *, label: str = "test key") -> str:
    """The plaintext of a new key on the App, with the App's accountable person as creator."""
    return issue_app_credential(path, app_id, label=label)["api_key"]


def accountable_person(path: Path, app_id: str) -> str:
    """The person the App answers for: the creator a key or trusted address on it must record."""
    return admin_store.get_app(app_id, path=path)["owner_user_id"]
