"""Who made a stored row, from trusted attribution (D51, D52).

Storage reads live here so the decision in ``gateway.content_access`` stays pure. A row's maker is the
person its writer recorded. Failing that, a personal App names its accountable person, because the App's
keys carry exactly that person (O5). A service App names no person. Owning an App is never a maker.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from gateway import admin_store, ownership
from gateway.admin_runtime import person_for_app_credential
from gateway.content_access import makers_of


def job_receipt_ids(job: Mapping[str, Any]) -> list[str]:
    """Try immutable creating provenance first, then legacy lookup candidates.

    Request IDs can be derived or reused; none is a receipt until lookup finds
    it. The durable creating link survives replay release and prompt erasure.
    """
    ids = [str(job.get(name) or "") for name in
           ("creating_receipt_id", "parent_request_id", "request_id")]
    return [value for index, value in enumerate(ids)
            if value and not value.startswith("imgjob_seeded_") and value not in ids[:index]]


class Apps:
    """Apps and their credentials, read once per instance. Make one per request, not per row."""

    def __init__(self, admin_db_path: Path) -> None:
        self._path = admin_db_path
        self._by_id: dict[str, dict[str, Any]] | None = None
        self._person_by_key: dict[str, str] | None = None

    def get(self, app_id: str) -> dict[str, Any] | None:
        if self._by_id is None:
            self._by_id = {str(app["id"]): app for app in admin_store.list_apps(path=self._path) if app.get("id")}
        return self._by_id.get(app_id)

    def person_by_key(self) -> dict[str, str]:
        """The person that each personal App principal and each of its proven credentials names (O5).

        A service App, and a credential whose creator is not the App's person, names no one.
        """
        if self._person_by_key is None:
            apps = {str(app["id"]): app for app in admin_store.list_apps(path=self._path) if app.get("id")}
            out: dict[str, str] = {
                f"app:{app_id}": str(app["owner_user_id"])
                for app_id, app in apps.items() if app.get("kind") == "personal" and app.get("owner_user_id")
            }
            for credential in admin_store.list_credentials(path=self._path):
                key_hash = credential.get("hash")
                if not isinstance(key_hash, str) or not key_hash:
                    continue
                principal = ownership.principal_key_for_credential(credential) or ""
                app = apps.get(principal.removeprefix("app:")) if principal.startswith("app:") else None
                person = person_for_app_credential(app, credential) if app else None
                if person:
                    out[key_hash] = str(person)
            self._person_by_key = out
        return self._person_by_key

    def app_person(self, app_id: str) -> str | None:
        """The accountable person of a personal App, or None for a service App or an unknown one."""
        app = self.get(app_id)
        return str(app["owner_user_id"]) if app and app.get("kind") == "personal" and app.get("owner_user_id") else None


def row_makers(*, explicit: Iterable[str], app_id: str | None, owner_keys: Iterable[str], apps: Apps) -> tuple[str, ...]:
    """The trusted maker(s) of one stored row.

    An App-attributed row takes its App's person and ignores owner keys, so a conflicting owner key cannot
    name a maker. An App-less row takes the person its owner keys name.
    """
    if app_id:
        return makers_of(explicit, app_person=apps.app_person(app_id))
    persons = apps.person_by_key()
    named = [persons.get(key) or (key.removeprefix("user:") if key.startswith("user:") else None)
             for key in owner_keys if key]
    return makers_of(explicit, key_persons=[person for person in named if person])
