"""Owner authorization, snapshot codec and durable category API contracts."""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import category_policy as policy
from gateway.category_api import create_category_router, snapshot_from_json, snapshot_to_json
from gateway.category_store import CategoryStore
from gateway.admin_api import AdminAuthError, AdminContext

PREFIX = "/admin/api/categories"
QUERY = {"person": "person", "app": "app", "capability": "run", "service": "service@1"}


def snapshot(allow=True):
    fact = policy.Assertion("dimension", "accepted", "owner", True,
                            "observed", "accepted", "tester", "mapping@1")
    root = policy.Subject("root@1", "release", (fact,))
    helper = policy.Service("helper@1", (policy.Subject("helper-root@1", "workflow"),))
    service = policy.Service("service@1", (root,), (helper,))
    plan = policy.Plan("plan", (policy.Grant("run", "service@1"),) if allow else ())
    return policy.PolicyVersion(
        "body", {"dimension": policy.Category("dimension", "Private label", values=("accepted",))},
        {"person": (plan,)}, {"app": (plan,)}, {"service@1": service},
        retained={"output": service})


@pytest.fixture
def categories(tmp_path):
    path = tmp_path / "categories.db"

    def auth(request):
        role = request.headers.get("test-role", "owner")
        if role == "anonymous":
            raise AdminAuthError("Sign in", status_code=401)
        return AdminContext(subject="tester", roles=frozenset({role}))

    app = FastAPI()
    app.include_router(create_category_router(auth=auth, store_path=path))
    return TestClient(app), path


def create(client, allow=True, base=None):
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": base, "reason": "reviewed edit", "snapshot": snapshot_to_json(snapshot(allow))})
    assert response.status_code == 200, response.text
    return response.json()


def activate(client, revision, base=None):
    return client.post(PREFIX + f"/candidates/{revision}/activate",
                       json={"expected_active_revision": base})


ROUTES = [
    ("GET", "/active"), ("GET", "/history"), ("GET", "/revisions/1"),
    ("POST", "/candidates"), ("POST", "/candidates/1/activate"),
    ("POST", "/revisions/1/restore"), ("POST", "/preview-as"),
]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("role,status", [("member", 403), ("admin", 403), ("anonymous", 401)])
def test_every_route_is_owner_only(categories, method, path, role, status):
    client, store_path = categories
    # Malformed bodies must never bypass or run before authorization.
    response = client.request(method, PREFIX + path, content="{",
                              headers={"test-role": role})
    assert response.status_code == status
    assert "error" in response.json()
    if status == 403:
        assert response.json()["error"]["code"] == ("owner_required" if role == "admin" else "admin_forbidden")
    assert "Private label" not in response.text
    assert not store_path.exists()


def test_codec_round_trip_all_nested_fields():
    version = snapshot()
    encoded = snapshot_to_json(version)
    assert snapshot_from_json(encoded) == version
    assert snapshot_to_json(snapshot_from_json(encoded)) == encoded
    assert encoded["services"]["service@1"]["dependencies"]
    assert encoded["retained"]["output"]["members"][0]["assertions"][0]["mapping_version"] == "mapping@1"


@pytest.mark.parametrize("path", [
    (), ("categories", "dimension"), ("people", "person", 0),
    ("people", "person", 0, "grants", 0),
    ("services", "service@1"),
    ("services", "service@1", "members", 0),
    ("services", "service@1", "members", 0, "assertions", 0),
    ("services", "service@1", "dependencies", 0),
    ("retained", "output"),
])
def test_codec_rejects_unknown_fields_at_every_level(path):
    encoded = snapshot_to_json(snapshot())
    target = encoded
    for key in path:
        target = target[key]
    target["unknown"] = True
    with pytest.raises(ValueError):
        snapshot_from_json(encoded)


def test_codec_rejects_unknown_rule_field():
    encoded = snapshot_to_json(snapshot())
    encoded["people"]["person"][0]["require"] = [
        {"id": "rule", "category": "dimension", "op": "exists", "unknown": True}]
    with pytest.raises(ValueError):
        snapshot_from_json(encoded)


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_codec_rejects_unsafe_or_coerced_gate(value):
    encoded = snapshot_to_json(snapshot())
    encoded["completeness_gate"] = value
    with pytest.raises(ValueError):
        snapshot_from_json(encoded)


def test_create_preview_activate_and_read(categories):
    client, _ = categories
    candidate = create(client)
    assert candidate["preview"]["gains"]
    assert candidate["preview"]["reason_changes"]
    assert candidate["preview_base_revision"] is None
    assert candidate["active_counter"] is None
    assert client.get(PREFIX + "/active").status_code == 409
    preview = client.post(PREFIX + "/preview-as", json={**QUERY, "revision": candidate["revision"]})
    assert preview.json()["outcome"] == "allow"
    result = activate(client, candidate["revision"])
    assert result.status_code == 200
    assert result.json() == {"revision": candidate["revision"], "active_counter": 1}
    active = client.get(PREFIX + "/active").json()
    assert active["snapshot"] == candidate["snapshot"]
    assert active["active_counter"] == 1
    assert client.get(PREFIX + f'/revisions/{candidate["revision"]}').json()["snapshot"] == candidate["snapshot"]
    assert client.get(PREFIX + "/history").json()["history"][0]["action"] == "activate"


def test_stale_activation_preserves_committed_pointer_and_counter(categories):
    client, _ = categories
    first, stale = create(client), create(client, False)
    assert activate(client, first["revision"]).status_code == 200
    response = activate(client, stale["revision"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_base"
    # Supplying the new pointer cannot rebase an unpreviewed candidate.
    assert activate(client, stale["revision"], first["revision"]).status_code == 409
    active = client.get(PREFIX + "/active").json()
    assert (active["revision"], active["active_counter"]) == (first["revision"], 1)


def test_restore_previews_reverted_access_without_activation(categories):
    client, _ = categories
    first = create(client)
    activate(client, first["revision"])
    second = create(client, False, first["revision"])
    assert second["preview"]["losses"]
    activate(client, second["revision"], first["revision"])
    response = client.post(PREFIX + f'/revisions/{first["revision"]}/restore',
                           json={"base_revision": second["revision"], "reason": "undo restriction"})
    assert response.status_code == 200
    restored = response.json()
    assert restored["revision"] not in (first["revision"], second["revision"])
    assert restored["snapshot"] == first["snapshot"]
    assert restored["reason"] == f'restore revision {first["revision"]}'
    audit = client.get(PREFIX + "/history").json()["history"][-1]
    assert audit["action"] == "restore"
    assert audit["source_revision"] == first["revision"]
    assert audit["revision"] == restored["revision"]
    assert audit["active_counter"] == 2
    assert restored["restored_from_revision"] == first["revision"]
    assert restored["preview"]["gains"]
    assert restored["preview_base_revision"] == second["revision"]
    assert client.get(PREFIX + "/active").json()["revision"] == second["revision"]


def test_preview_as_allows_and_denies_with_kernel_reasons(categories):
    client, _ = categories
    first = create(client)
    activate(client, first["revision"])
    allowed = client.post(PREFIX + "/preview-as", json=QUERY).json()
    assert (allowed["outcome"], allowed["reason"]) == ("allow", "ALLOW")
    denied = client.post(PREFIX + "/preview-as", json={**QUERY, "capability": "other"}).json()
    assert (denied["outcome"], denied["reason"], denied["rule"]) == ("deny", "NO_GRANT", "plan:grant")
    assert "member" in denied


def test_empty_store_fails_closed(categories):
    client, _ = categories
    response = client.get(PREFIX + "/active")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_active_snapshot"
    decision = client.post(PREFIX + "/preview-as", json=QUERY).json()
    assert (decision["outcome"], decision["reason"]) == ("deny", "POLICY_UNAVAILABLE")
    assert client.get(PREFIX + "/history").json() == {"history": []}


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/candidates", {"base_revision": None, "snapshot": {}, "reason": "x", "extra": True}),
    ("POST", "/candidates/1/activate", {"expected_active_revision": False}),
    ("POST", "/revisions/1/restore", {"base_revision": None, "reason": "x", "extra": True}),
    ("POST", "/preview-as", {**QUERY, "extra": True}),
    ("GET", "/revisions/0", None),
])
def test_invalid_requests_use_error_envelope(categories, method, path, body):
    client, _ = categories
    response = client.request(method, PREFIX + path, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_missing_revision_and_duplicate_json_fields(categories):
    client, _ = categories
    assert client.get(PREFIX + "/revisions/123").status_code == 404
    response = client.post(PREFIX + "/preview-as",
                           content='{"person":"a","person":"b","app":"a","capability":"c","service":"s"}')
    assert response.status_code == 400


def test_concurrent_activation_during_preview_rejects_candidate(categories, monkeypatch):
    client, path = categories
    first = create(client)
    original = policy.change_preview

    def intervening_activation(before, after):
        result = original(before, after)
        CategoryStore(path).activate(first["revision"], None)
        return result

    monkeypatch.setattr(policy, "change_preview", intervening_activation)
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": None, "reason": "racing edit", "snapshot": snapshot_to_json(snapshot(False))})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_base"
    assert client.get(PREFIX + "/active").json()["revision"] == first["revision"]


def test_changed_execution_graph_requires_new_service_revision(categories):
    client, _ = categories
    first = create(client)
    activate(client, first["revision"])
    changed = deepcopy(first["snapshot"])
    changed["services"]["service@1"]["dependencies"] = []
    changed["retained"] = {}
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": first["revision"], "reason": "changed closure", "snapshot": changed})
    assert response.status_code == 400


def test_corrupt_policy_body_denies_preview_and_cannot_activate(categories):
    client, path = categories
    store = CategoryStore(path)
    row = store.create_candidate({"not": "a policy"}, None, "tester", "invalid fixture")
    response = activate(client, row.revision)
    assert response.status_code == 503
    store.activate(row.revision, None)
    assert client.get(PREFIX + "/active").status_code == 503
    decision = client.post(PREFIX + "/preview-as", json=QUERY).json()
    assert (decision["outcome"], decision["reason"]) == ("deny", "POLICY_UNAVAILABLE")


def test_preview_denial_reports_failing_rule_and_member(categories):
    client, _ = categories
    encoded = snapshot_to_json(snapshot())
    encoded["people"]["person"][0]["deny"] = [
        {"id": "blocked", "category": "dimension", "op": "in", "values": ["accepted"]}]
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": None, "reason": "deny fixture", "snapshot": encoded})
    assert response.status_code == 200
    decision = client.post(PREFIX + "/preview-as",
                           json={**QUERY, "revision": response.json()["revision"]}).json()
    assert (decision["outcome"], decision["reason"]) == ("deny", "EXPLICIT_DENY")
    assert (decision["rule"], decision["member"]) == ("plan:blocked", "root@1")


def test_non_owner_cannot_leak_existing_snapshot(categories):
    client, _ = categories
    candidate = create(client)
    activate(client, candidate["revision"])
    for method, path in ROUTES:
        response = client.request(method, PREFIX + path, json=QUERY,
                                  headers={"test-role": "member"})
        assert response.status_code == 403
        assert "Private label" not in response.text
        assert "snapshot" not in response.text


def test_revision_outside_sqlite_integer_range_is_invalid(categories):
    client, _ = categories
    response = client.get(PREFIX + "/revisions/9223372036854775808")
    assert response.status_code == 400
