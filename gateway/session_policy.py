"""How long a browser sign-in lasts, when it renews, and when it asks again.

A sign-in is a signed cookie (gateway.admin_oidc) plus a server-side record
(gateway.accounts, ``account_sessions``). Three owner settings govern it:

* idle timeout: a session unused for this long ends. Any request renews it, but
  only once a quarter of this time has passed since the last renewal, so a busy
  session does not re-issue its cookie on every request.
* absolute lifetime: a session ends this long after the original sign-in,
  however busy it is. The person signs in again.
* step-up window: a risky owner action (see ``gateway.step_up``) needs a sign-in
  or password confirmation within this window.

Everything here is pure: callers pass the policy and the clock. The settings
store is read in ``load_policy`` only.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store
from gateway.admin_oidc import AdminSession

SESSION_POLICY_SETTING_KEY = "session_policy"

DAY = 86400
MINUTE = 60

# Owner-facing units and bounds. Days and minutes are what an owner can reason
# about; the policy below is in seconds.
BOUNDS = {
    "idle_timeout_days": (1, 90),
    "absolute_lifetime_days": (1, 365),
    "step_up_minutes": (1, 1440),
}
DEFAULT_SETTING = {"idle_timeout_days": 30, "absolute_lifetime_days": 90, "step_up_minutes": 15}

# A session renews once more than this fraction of the idle window has passed
# since it was last renewed.
RENEW_AFTER_FRACTION = 4


@dataclass(frozen=True)
class SessionPolicy:
    idle_seconds: int = DEFAULT_SETTING["idle_timeout_days"] * DAY
    absolute_seconds: int = DEFAULT_SETTING["absolute_lifetime_days"] * DAY
    step_up_seconds: int = DEFAULT_SETTING["step_up_minutes"] * MINUTE


class InvalidSessionPolicy(ValueError):
    """The submitted settings are outside their bounds."""


def policy_from_setting(setting: Mapping[str, int]) -> SessionPolicy:
    return SessionPolicy(
        idle_seconds=int(setting["idle_timeout_days"]) * DAY,
        absolute_seconds=int(setting["absolute_lifetime_days"]) * DAY,
        step_up_seconds=int(setting["step_up_minutes"]) * MINUTE,
    )


def validate_setting(value: Any) -> dict[str, int]:
    """The three settings as whole numbers inside their bounds, or a clear refusal."""
    if not isinstance(value, Mapping) or set(value) != set(BOUNDS):
        raise InvalidSessionPolicy("Supply idle_timeout_days, absolute_lifetime_days and step_up_minutes.")
    checked: dict[str, int] = {}
    for key, (low, high) in BOUNDS.items():
        number = value[key]
        if type(number) is not int or not low <= number <= high:
            raise InvalidSessionPolicy(f"{key} must be a whole number from {low} to {high}.")
        checked[key] = number
    if checked["idle_timeout_days"] > checked["absolute_lifetime_days"]:
        raise InvalidSessionPolicy("The idle timeout cannot be longer than the absolute lifetime.")
    return checked


def stored_setting(path: Path) -> dict[str, int]:
    """The owner's saved settings, or the defaults. A damaged row falls back to the defaults."""
    record = admin_store.get_operator_setting(SESSION_POLICY_SETTING_KEY, path=path)
    if record:
        try:
            return validate_setting(record["value"])
        except InvalidSessionPolicy:
            pass
    return dict(DEFAULT_SETTING)


def load_policy(path: Path) -> SessionPolicy:
    return policy_from_setting(stored_setting(path))


# One gateway process: saves in this process invalidate after commit; external
# writers become visible within five seconds. Cache policy, never usability.
_asset_policy_lock = threading.Lock()
_asset_policy_cache: tuple[Path, float, SessionPolicy] | None = None


def invalidate_asset_policy() -> None:
    global _asset_policy_cache
    with _asset_policy_lock:
        _asset_policy_cache = None


def load_asset_policy(path: Path) -> SessionPolicy:
    global _asset_policy_cache
    with _asset_policy_lock:
        now = time.monotonic()
        if _asset_policy_cache is not None:
            saved_path, loaded_at, policy = _asset_policy_cache
            if saved_path == path and 0 <= now - loaded_at < 5.0:
                return policy
        policy = load_policy(path)
        _asset_policy_cache = (path, now, policy)
        return policy


# ---------------------------------------------------------------------------
# Decisions.
# ---------------------------------------------------------------------------


def absolute_deadline(session: AdminSession, policy: SessionPolicy) -> float:
    """When the session ends however recently it was used.

    The nearer of the deadline its cookie carries and the owner's current
    setting, so shortening the setting reaches sessions already signed in. A
    cookie from before sliding sessions carries none and is held to the setting.
    """
    from_setting = session.issued_at + policy.absolute_seconds
    if session.absolute_expires_at is None:
        return from_setting
    return min(session.absolute_expires_at, from_setting)


def is_usable(session: AdminSession, policy: SessionPolicy, now: float) -> bool:
    """Not expired, not idle for too long, not past its absolute deadline."""
    return (
        now < session.expires_at
        and now < session.last_renewed + policy.idle_seconds
        and now < absolute_deadline(session, policy)
    )


def renewal(session: AdminSession, policy: SessionPolicy, now: float) -> AdminSession | None:
    """The same session with a new expiry, when it is due and that extends it; else None.

    A cookie from before sliding sessions is always due: its first request
    upgrades it, so the person is not signed out when its short lifetime ends.
    """
    if not session.renewable:
        return None
    due = session.renewed_at is None or now - session.renewed_at > policy.idle_seconds / RENEW_AFTER_FRACTION
    if not due:
        return None
    deadline = absolute_deadline(session, policy)
    expires_at = min(now + policy.idle_seconds, deadline)
    if expires_at <= session.expires_at:
        return None  # already at its absolute deadline: nothing to extend
    return replace(
        session,
        expires_at=expires_at,
        absolute_expires_at=deadline,
        renewed_at=now,
        authenticated_at=session.last_authenticated,
    )


def confirmed(session: AdminSession, policy: SessionPolicy, now: float) -> AdminSession:
    """The same session after the person proved who they are again."""
    deadline = absolute_deadline(session, policy)
    return replace(
        session,
        expires_at=min(now + policy.idle_seconds, deadline),
        absolute_expires_at=deadline,
        renewed_at=now,
        authenticated_at=now,
    )


def is_recently_authenticated(session: AdminSession, policy: SessionPolicy, now: float) -> bool:
    return now - session.last_authenticated <= policy.step_up_seconds


def lifetime_claims(session: AdminSession) -> dict[str, float | None]:
    """The cookie facts needed to apply the current lifetime policy to a derived credential."""
    return {"issued_at": session.issued_at, "renewed_at": session.last_renewed,
            "expires_at": session.expires_at, "absolute_expires_at": session.absolute_expires_at}
