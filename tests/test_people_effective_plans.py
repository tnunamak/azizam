"""People and authorization must resolve the same access plan (design §2.4)."""

import pytest

from gateway import accounts, admin_runtime, admin_store
from tests.support.access import store_plan


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    return path


def test_people_shows_the_plan_that_authorizes_the_persons_apps(db):
    user = admin_store.upsert_oidc_user("issuer", "subject", email="person@example.test", path=db)
    family = store_plan("Family", path=db, caps=("chat",))
    accounts.change_person(user["id"], "owner", path=db, plan_id=family["id"])
    app = admin_store.ensure_session_app_for_user(user["id"], name="Browser", path=db)

    person = accounts.people(path=db)[0]
    identity = admin_runtime.runtime_identity_for_session_app(app["id"], user["id"], path=db)

    assert (person["plan_id"], person["plan_name"]) == (family["id"], "Family")
    assert identity.access.plan_ids == (family["id"],)


def test_person_without_a_plan_shows_none_and_their_apps_are_denied(db):
    user = admin_store.upsert_oidc_user("issuer", "subject", path=db)
    app = admin_store.ensure_session_app_for_user(user["id"], name="Browser", path=db)

    person = accounts.people(path=db)[0]
    identity = admin_runtime.runtime_identity_for_session_app(app["id"], user["id"], path=db)

    assert person["plan_id"] is None
    assert not identity.access.complete
