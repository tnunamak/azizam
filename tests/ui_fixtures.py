"""A fake web export and signed-in principals for the UI-at-root tests."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import proxy
from gateway import accounts, admin_store, ui_routes
from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims
from gateway.ui_static import UIStaticFiles

ASSET = "/_next/static/chunks/app-0123456789abcdef.js"
SIGNING_KEY = b"s" * 32


def write_export(root: Path) -> Path:
    for name in ui_routes.PAGES:
        page = root / name if name else root
        page.mkdir(parents=True, exist_ok=True)
        (page / "index.html").write_text(f"<!doctype html><title>page {name or 'home'}</title>")
        (page / "index.txt").write_text(f"payload {name or 'home'}")
    (root / "404.html").write_text("<!doctype html><title>Not found</title>")
    asset = root / ASSET.lstrip("/")
    asset.parent.mkdir(parents=True)
    asset.write_text("console.log('chunk');\n" * 20)
    return root


def use_export(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    route = proxy.WEB_UI_ROUTE
    monkeypatch.setattr(route, "directory", directory)
    monkeypatch.setattr(route, "files", UIStaticFiles(directory=str(directory), html=False, check_dir=False))


class Principals:
    """Cookies and keys for every caller kind in the admin page guard table."""

    def __init__(self, case, app_key: str) -> None:
        self.case = case
        self.client = case.client
        self.admin_key = case.admin_key
        self.app_key = app_key
        self.cookie = CookieConfig(secure=False)

    def session_cookie(self, role: str, *, revoked: bool = False, email: str | None = None) -> str:
        subject = f"ui-root-{role}-{time.monotonic_ns()}"
        user = admin_store.upsert_oidc_user("https://idp.example", subject,
                                            email=email or f"{subject}@example.test", path=self.case.db)
        if role in {"owner", "admin", "superadmin"}:
            admin_store.assign_admin_role(role=role, subject_kind="user", subject_id=user["id"], path=self.case.db)
        # Sessions carry owner/admin/none; superadmin exists only as a stored grant.
        session_role = "admin" if role == "superadmin" else role
        session = session_from_claims({"sub": user["id"], "email": user["email"]}, role=session_role,
                                      cookie=self.cookie)
        if revoked:
            accounts.revoke_session(session.session_id, int(session.expires_at), path=self.case.db)
        return encode_session_cookie(session, signing_key=SIGNING_KEY)
