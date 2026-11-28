"""Sliding sessions: when a cookie renews, goes idle, hits its absolute deadline, and asks again.

Pure decisions (gateway.session_policy) on explicit clocks; no server, no database
except the settings round trip.
"""
import pytest

from gateway import admin_store, session_policy
from gateway.admin_oidc import (
    AdminSession, CookieConfig, SessionError, decode_session_cookie, encode_session_cookie, session_from_claims,
)
from gateway.session_policy import DAY, MINUTE, SessionPolicy

KEY = b"k" * 32
POLICY = SessionPolicy()  # 30 days idle, 90 days absolute, 15 minutes step-up
T0 = 1_800_000_000.0
CLAIMS = {"sub": "user_1", "email": "Owner@Example.test"}


def new_session(now=T0, policy=POLICY):
    return session_from_claims(
        CLAIMS, role="owner", cookie=CookieConfig(),
        now=now, idle_seconds=policy.idle_seconds, absolute_seconds=policy.absolute_seconds,
    )


def legacy_session(now=T0):
    """What the gateway issued before sliding sessions: eight hours, no sliding fields."""
    return session_from_claims(CLAIMS, role="owner", cookie=CookieConfig(), now=now)


def test_a_new_session_lasts_the_idle_window_and_records_its_absolute_deadline():
    session = new_session()

    assert session.expires_at == T0 + 30 * DAY
    assert session.absolute_expires_at == T0 + 90 * DAY
    assert session.renewed_at == session.authenticated_at == T0
    assert session.email == "owner@example.test"


def test_the_idle_window_is_capped_by_the_absolute_lifetime():
    session = new_session(policy=SessionPolicy(idle_seconds=30 * DAY, absolute_seconds=10 * DAY))

    assert session.expires_at == T0 + 10 * DAY


@pytest.mark.parametrize("elapsed, due", [
    (0, False),
    (7 * DAY, False),                    # 7 days < a quarter of 30 days (7.5)
    (7.5 * DAY, False),                  # exactly a quarter is not "more than"
    (7.5 * DAY + 1, True),
    (20 * DAY, True),
])
def test_renewal_is_due_once_more_than_a_quarter_of_the_idle_window_has_passed(elapsed, due):
    session = new_session()

    renewed = session_policy.renewal(session, POLICY, T0 + elapsed)

    assert (renewed is not None) is due


def test_renewal_keeps_the_session_identity_and_extends_only_the_expiry():
    session = new_session()
    now = T0 + 8 * DAY

    renewed = session_policy.renewal(session, POLICY, now)

    assert renewed.session_id == session.session_id
    assert (renewed.subject, renewed.email, renewed.role, renewed.issued_at) == (
        session.subject, session.email, session.role, session.issued_at)
    assert renewed.expires_at == now + 30 * DAY
    assert renewed.renewed_at == now
    assert renewed.authenticated_at == T0          # renewing is not proving who you are
    assert renewed.absolute_expires_at == T0 + 90 * DAY


def test_a_renewal_is_not_due_again_until_another_quarter_has_passed():
    session = new_session()
    first = session_policy.renewal(session, POLICY, T0 + 8 * DAY)

    assert session_policy.renewal(first, POLICY, T0 + 8 * DAY + 7 * DAY) is None
    assert session_policy.renewal(first, POLICY, T0 + 8 * DAY + 7.6 * DAY) is not None


def test_renewal_never_runs_past_the_absolute_deadline():
    renewed = session_policy.renewal(new_session(), POLICY, T0 + 70 * DAY)

    assert renewed.expires_at == T0 + 90 * DAY       # not 70 + 30 = 100 days


def test_a_session_already_at_its_absolute_deadline_has_nothing_to_renew():
    near_end = session_policy.renewal(new_session(), POLICY, T0 + 70 * DAY)

    assert session_policy.renewal(near_end, POLICY, T0 + 80 * DAY) is None


def test_idle_expiry():
    session = new_session()

    assert session_policy.is_usable(session, POLICY, T0 + 30 * DAY - 1)
    assert not session_policy.is_usable(session, POLICY, T0 + 30 * DAY)


def test_activity_pushes_idle_expiry_out_but_not_past_the_absolute_deadline():
    session = new_session()
    for day in range(8, 90, 8):                       # renewed every 8 days: always active
        session = session_policy.renewal(session, POLICY, T0 + day * DAY) or session
        assert session_policy.is_usable(session, POLICY, T0 + day * DAY)

    assert session_policy.is_usable(session, POLICY, T0 + 90 * DAY - 1)
    assert not session_policy.is_usable(session, POLICY, T0 + 90 * DAY)     # busy or not, sign in again


def test_absolute_expiry_holds_even_when_the_cookie_says_otherwise():
    session = AdminSession(
        subject="u", email="e", role="owner", issued_at=T0, expires_at=T0 + 200 * DAY, session_id="s",
        absolute_expires_at=T0 + 200 * DAY, renewed_at=T0 + 100 * DAY, authenticated_at=T0,
    )

    assert not session_policy.is_usable(session, POLICY, T0 + 91 * DAY)      # the setting's 90 days applies


def test_shortening_the_policy_reaches_sessions_already_signed_in():
    session = new_session()
    stricter = SessionPolicy(idle_seconds=2 * DAY, absolute_seconds=5 * DAY)

    assert session_policy.is_usable(session, stricter, T0 + 1 * DAY)
    assert not session_policy.is_usable(session, stricter, T0 + 3 * DAY)      # idle for 3 days
    busy = AdminSession(**{**session.__dict__, "renewed_at": T0 + 4.5 * DAY})
    assert session_policy.is_usable(busy, stricter, T0 + 5 * DAY - 1)
    assert not session_policy.is_usable(busy, stricter, T0 + 5 * DAY)         # past 5 days absolute


# -- an existing eight-hour cookie, from before this change ----------------

def test_a_cookie_from_before_sliding_sessions_still_decodes_and_is_valid_until_its_old_expiry():
    cookie = encode_session_cookie(legacy_session(), signing_key=KEY)

    session = decode_session_cookie(cookie, signing_key=KEY, now=T0 + 8 * 3600 - 1)

    assert session.expires_at == T0 + 8 * 3600
    assert (session.absolute_expires_at, session.renewed_at, session.authenticated_at) == (None, None, None)
    assert session.last_renewed == session.last_authenticated == T0
    with pytest.raises(SessionError):
        decode_session_cookie(cookie, signing_key=KEY, now=T0 + 8 * 3600)


def test_the_old_cookie_payload_is_unchanged_so_a_rollback_still_reads_it():
    assert set(legacy_session().to_dict()) == {"sub", "email", "role", "iat", "exp", "sid"}
    assert {"aexp", "rat", "aat"} <= set(new_session().to_dict())


def test_a_legacy_cookie_is_upgraded_on_first_use_with_the_same_identity():
    legacy = decode_session_cookie(encode_session_cookie(legacy_session(), signing_key=KEY), signing_key=KEY, now=T0)
    now = T0 + 3600

    upgraded = session_policy.renewal(legacy, POLICY, now)

    assert upgraded is not None and upgraded.session_id == legacy.session_id
    assert upgraded.expires_at == now + 30 * DAY
    assert upgraded.absolute_expires_at == T0 + 90 * DAY          # counted from the original sign-in
    assert upgraded.authenticated_at == T0                         # it was not a fresh sign-in


def test_a_legacy_cookie_without_a_sid_keeps_one_stable_id_across_the_upgrade():
    payload = legacy_session().to_dict()
    payload.pop("sid")
    old = AdminSession.from_dict(payload)
    upgraded = session_policy.renewal(old, POLICY, T0 + 60)

    assert old.session_id.startswith("legacy_")
    assert upgraded.session_id == old.session_id
    again = decode_session_cookie(encode_session_cookie(upgraded, signing_key=KEY), signing_key=KEY, now=T0 + 61)
    assert again.session_id == old.session_id


# -- step-up --------------------------------------------------------------

def test_a_sign_in_is_recent_for_the_step_up_window_and_not_after():
    session = new_session()

    assert session_policy.is_recently_authenticated(session, POLICY, T0 + 15 * MINUTE)
    assert not session_policy.is_recently_authenticated(session, POLICY, T0 + 15 * MINUTE + 1)


def test_renewing_does_not_make_a_stale_sign_in_fresh_but_confirming_does():
    session = new_session()
    later = T0 + 8 * DAY
    renewed = session_policy.renewal(session, POLICY, later)

    assert not session_policy.is_recently_authenticated(renewed, POLICY, later)
    again = session_policy.confirmed(renewed, POLICY, later)
    assert session_policy.is_recently_authenticated(again, POLICY, later)
    assert again.session_id == session.session_id


def test_a_legacy_cookie_counts_from_its_issue_time_for_step_up():
    legacy = decode_session_cookie(encode_session_cookie(legacy_session(), signing_key=KEY), signing_key=KEY, now=T0)

    assert session_policy.is_recently_authenticated(legacy, POLICY, T0 + 10 * MINUTE)
    assert not session_policy.is_recently_authenticated(legacy, POLICY, T0 + 20 * MINUTE)


# -- owner settings ---------------------------------------------------------

def test_defaults_are_thirty_days_ninety_days_and_fifteen_minutes(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)

    assert session_policy.load_policy(path) == SessionPolicy(30 * DAY, 90 * DAY, 15 * MINUTE)


@pytest.mark.parametrize("change", [
    {"idle_timeout_days": 0}, {"idle_timeout_days": 91}, {"absolute_lifetime_days": 0},
    {"absolute_lifetime_days": 366}, {"step_up_minutes": 0}, {"step_up_minutes": 1441},
    {"idle_timeout_days": 30, "absolute_lifetime_days": 29},       # idle longer than absolute
    {"idle_timeout_days": True}, {"step_up_minutes": 15.5}, {"step_up_minutes": "15"},
])
def test_settings_outside_their_bounds_are_refused(change):
    with pytest.raises(session_policy.InvalidSessionPolicy):
        session_policy.validate_setting({**session_policy.DEFAULT_SETTING, **change})


def test_settings_with_missing_or_extra_keys_are_refused():
    with pytest.raises(session_policy.InvalidSessionPolicy):
        session_policy.validate_setting({"idle_timeout_days": 30})
    with pytest.raises(session_policy.InvalidSessionPolicy):
        session_policy.validate_setting({**session_policy.DEFAULT_SETTING, "extra": 1})


def test_the_bounds_edges_are_accepted():
    session_policy.validate_setting({"idle_timeout_days": 1, "absolute_lifetime_days": 1, "step_up_minutes": 1})
    session_policy.validate_setting({"idle_timeout_days": 90, "absolute_lifetime_days": 365, "step_up_minutes": 1440})


def test_a_saved_setting_is_what_load_policy_returns_and_a_damaged_row_falls_back(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.put_operator_setting(
        session_policy.SESSION_POLICY_SETTING_KEY,
        {"idle_timeout_days": 7, "absolute_lifetime_days": 14, "step_up_minutes": 5}, path=path)

    assert session_policy.load_policy(path) == SessionPolicy(7 * DAY, 14 * DAY, 5 * MINUTE)

    admin_store.put_operator_setting(session_policy.SESSION_POLICY_SETTING_KEY, {"idle_timeout_days": "x"}, path=path)
    assert session_policy.load_policy(path) == SessionPolicy()


def test_a_session_that_is_not_renewable_never_renews_however_old():
    from dataclasses import replace
    fixed = replace(legacy_session(), renewable=False)

    assert session_policy.renewal(fixed, POLICY, T0 + 7 * 3600) is None
    assert fixed.to_dict()["rn"] == 0
    assert AdminSession.from_dict(fixed.to_dict()).renewable is False
    assert AdminSession.from_dict(legacy_session().to_dict()).renewable is True
