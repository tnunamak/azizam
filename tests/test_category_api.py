"""Owner authorization, snapshot codec and durable category API contracts."""
from copy import deepcopy
import json

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
        return AdminContext(subject=request.headers.get("test-owner", "tester"),
                            roles=frozenset({role}))

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
    history = client.get(PREFIX + "/history").json()["history"]
    assert active["activated_at"] == history[-1]["created_at"]
    assert history[-1]["action"] == "activate"
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
    assert restored["reason"] == "undo restriction"
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


def _with_service(base, service_id, helper_id):
    """A copy of ``base`` whose only service is ``service_id`` with a given helper."""
    root = base.services["service@1"].members[0]
    helper = policy.Service(helper_id, (policy.Subject("helper-root@1", "workflow"),))
    service = policy.Service(service_id, (root,), (helper,))
    plan = policy.Plan("plan", (policy.Grant("run", service_id),))
    return policy.PolicyVersion(base.id, base.categories, {"person": (plan,)}, {"app": (plan,)},
                                {service_id: service})


def test_a_historical_service_revision_id_cannot_be_reused_for_another_closure(categories):
    client, _ = categories
    first = create(client)
    assert activate(client, first["revision"]).status_code == 200
    moved = _with_service(snapshot(), "service@2", "helper@1")
    second = client.post(PREFIX + "/candidates", json={
        "base_revision": first["revision"], "reason": "new revision",
        "snapshot": snapshot_to_json(moved)})
    assert second.status_code == 200, second.text
    assert activate(client, second.json()["revision"], first["revision"]).status_code == 200
    # service@1 is no longer active, but history still holds its closure.
    reused = _with_service(snapshot(), "service@1", "helper@2")
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": second.json()["revision"], "reason": "recycle id",
        "snapshot": snapshot_to_json(reused)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def assertion_rows(body):
    def visit(service):
        for member in service["members"]:
            yield from member["assertions"]
        for dependency in service["dependencies"]:
            yield from visit(dependency)

    for group in (body["services"], body["retained"]):
        for service in group.values():
            yield from visit(service)


def test_new_assertions_ignore_forged_accepting_owner_and_time(categories):
    from datetime import datetime, timezone

    client, path = categories
    body = snapshot_to_json(snapshot())
    for group in (body["services"], body["retained"]):
        for service in group.values():
            service["dependencies"][0]["members"][0]["assertions"] = deepcopy(
                service["members"][0]["assertions"])
    for assertion in assertion_rows(body):
        assertion.update(accepting_owner="forged-owner", accepted_at="1900-01-01T00:00:00Z")
    started = datetime.now(timezone.utc)
    response = client.post(PREFIX + "/candidates", headers={"test-owner": "session-owner"}, json={
        "base_revision": None, "reason": "owner edit", "snapshot": body})
    finished = datetime.now(timezone.utc)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["author"] == "session-owner"
    assertions = list(assertion_rows(candidate["snapshot"]))
    assert len(assertions) == 4
    for assertion in assertions:
        assert assertion["accepting_owner"] == "session-owner"
        assert started <= datetime.fromisoformat(assertion["accepted_at"]) <= finished
        assert assertion["observed_at"] == "observed"
        assert assertion["mapping_version"] == "mapping@1"
    assert len({assertion["accepted_at"] for assertion in assertions}) == 1
    assert CategoryStore(path).get(candidate["revision"]).body == candidate["snapshot"]


def test_unchanged_assertions_keep_all_stored_provenance(categories):
    client, _ = categories
    first = create(client)
    assert activate(client, first["revision"]).status_code == 200
    body = deepcopy(first["snapshot"])
    for assertion in assertion_rows(body):
        assertion.update(accepting_owner="forged-owner", accepted_at="forged-time",
                         observed_at="rewritten-observation", mapping_version="rewritten-mapping")
    response = client.post(PREFIX + "/candidates", headers={"test-owner": "second-owner"}, json={
        "base_revision": first["revision"], "reason": "unchanged facts", "snapshot": body})
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate["author"] == "second-owner"
    assert candidate["snapshot"] == first["snapshot"]


def test_unchanged_retained_assertions_keep_their_own_provenance(categories):
    client, path = categories
    body = snapshot_to_json(snapshot())
    # The same fact can have distinct acceptance history in an output bundle.
    retained = body["retained"]["output"]
    retained["members"][0]["assertions"][0].update(
        accepting_owner="historical-owner", accepted_at="historical-time")
    store = CategoryStore(path)
    first = store.create_candidate(body, None, "original-owner", "trusted history")
    store.activate(first.revision, None)
    edited = deepcopy(body)
    for assertion in assertion_rows(edited):
        assertion.update(accepting_owner="forged-owner", accepted_at="forged-time")
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": first.revision, "reason": "unchanged facts", "snapshot": edited})
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == body


def _duplicate_evidence_body():
    body = snapshot_to_json(snapshot())
    assertions = body["services"]["service@1"]["members"][0]["assertions"]
    assertions.append({**assertions[0], "observed_at": "observation-B", "mapping_version": "mapping@2"})
    body["retained"] = {}
    return body


def _create_and_activate_body(client, body):
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": None, "reason": "initial evidence", "snapshot": body})
    assert response.status_code == 200, response.text
    first = response.json()
    assert activate(client, first["revision"]).status_code == 200
    return first


def _resave_body(client, first, body):
    return client.post(PREFIX + "/candidates", headers={"test-owner": "second-owner"}, json={
        "base_revision": first["revision"], "reason": "preserve evidence", "snapshot": body})


def test_noop_save_keeps_duplicate_evidence_byte_for_byte(categories):
    client, path = categories
    first = _create_and_activate_body(client, _duplicate_evidence_body())
    response = _resave_body(client, first, first["snapshot"])
    assert response.status_code == 200, response.text
    saved = response.json()
    assert json.dumps(saved["snapshot"], sort_keys=True) == json.dumps(first["snapshot"], sort_keys=True)
    assert CategoryStore(path).get(saved["revision"]).body == first["snapshot"]


def test_reordering_duplicate_evidence_keeps_each_records_provenance(categories):
    client, _ = categories
    first = _create_and_activate_body(client, _duplicate_evidence_body())
    body = deepcopy(first["snapshot"])
    body["services"]["service@1"]["members"][0]["assertions"].reverse()
    response = _resave_body(client, first, body)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == body


def test_duplicate_observations_ignore_forged_acceptance_fields(categories):
    client, _ = categories
    first = _create_and_activate_body(client, _duplicate_evidence_body())
    body = deepcopy(first["snapshot"])
    for assertion in assertion_rows(body):
        assertion.update(accepting_owner="forged-owner", accepted_at="forged-time")
    response = _resave_body(client, first, body)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == first["snapshot"]


def test_identical_duplicate_evidence_preserves_multiplicity(categories):
    client, _ = categories
    body = _duplicate_evidence_body()
    assertions = body["services"]["service@1"]["members"][0]["assertions"]
    assertions[1] = deepcopy(assertions[0])
    first = _create_and_activate_body(client, body)
    response = _resave_body(client, first, first["snapshot"])
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == first["snapshot"]


def _two_service_evidence_body():
    body = snapshot_to_json(snapshot())
    a = body["services"].pop("service@1")
    a["id"] = "a@1"
    b = deepcopy(a)
    b["id"] = "b@1"
    b["members"][0]["assertions"][0].update(observed_at="observation-B", mapping_version="mapping@2")
    body["services"] = {"a@1": a, "b@1": b}
    body["retained"] = {}
    for group in (body["people"], body["apps"]):
        for plans in group.values():
            plans[0]["grants"] = [{"capability": "run", "service": name} for name in body["services"]]
    return body


def _move_b_to_retained(body):
    moved = deepcopy(body)
    b = moved["services"].pop("b@1")
    moved["retained"]["old-b"] = deepcopy(b)
    b["id"] = "b@2"
    moved["services"]["b@2"] = b
    for group in (moved["people"], moved["apps"]):
        for plans in group.values():
            plans[0]["grants"][1]["service"] = "b@2"
    return moved


def test_retained_move_and_successor_keep_source_bundle_provenance(categories):
    client, _ = categories
    first = _create_and_activate_body(client, _two_service_evidence_body())
    body = _move_b_to_retained(first["snapshot"])
    response = _resave_body(client, first, body)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == body


@pytest.mark.parametrize("operation", ["noop", "reorder", "move"])
def test_trusted_historical_duplicate_evidence_stays_distinct(categories, operation):
    client, path = categories
    body = _two_service_evidence_body() if operation == "move" else _duplicate_evidence_body()
    assertions = list(assertion_rows(body))
    # For noop/reorder only the server-owned acceptance fields distinguish records.
    assertions[1].update(observed_at=assertions[0]["observed_at"],
                         mapping_version=assertions[0]["mapping_version"],
                         accepting_owner="historical-owner", accepted_at="historical-time")
    store = CategoryStore(path)
    row = store.create_candidate(body, None, "original-owner", "trusted history")
    store.activate(row.revision, None)
    first = {"revision": row.revision, "snapshot": body}
    edited = _move_b_to_retained(body) if operation == "move" else deepcopy(body)
    if operation == "reorder":
        edited["services"]["service@1"]["members"][0]["assertions"].reverse()
    if operation == "move":
        for assertion in assertion_rows(edited):
            assertion.update(accepting_owner="forged-owner", accepted_at="forged-time")
        expected = _move_b_to_retained(body)
    else:
        expected = edited
    response = _resave_body(client, first, edited)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"] == expected


@pytest.mark.parametrize("location", ["local", "global"])
def test_ambiguous_duplicate_evidence_is_rejected_without_insertion(categories, location):
    client, path = categories
    first = _create_and_activate_body(client, _duplicate_evidence_body())
    body = deepcopy(first["snapshot"])
    if location == "global":
        service = body["services"].pop("service@1")
        service["id"] = "unrelated@1"
        body["services"][service["id"]] = service
        for group in (body["people"], body["apps"]):
            for plans in group.values():
                plans[0]["grants"][0]["service"] = service["id"]
    for assertion in assertion_rows(body):
        assertion.update(observed_at="unmatched-observation", mapping_version="unmatched-mapping",
                         accepting_owner="forged-owner", accepted_at="forged-time")
    history = CategoryStore(path).history()
    response = _resave_body(client, first, body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert "ambiguous" in response.json()["error"]["message"].lower()
    assert CategoryStore(path).history() == history


def test_forged_acceptance_fields_cannot_resolve_ambiguous_history(categories):
    client, path = categories
    body = _duplicate_evidence_body()
    assertions = list(assertion_rows(body))
    assertions[1].update(observed_at=assertions[0]["observed_at"],
                         mapping_version=assertions[0]["mapping_version"],
                         accepting_owner="historical-owner", accepted_at="historical-time")
    store = CategoryStore(path)
    row = store.create_candidate(body, None, "original-owner", "trusted history")
    store.activate(row.revision, None)
    for assertion in assertion_rows(body):
        assertion.update(accepting_owner="forged-owner", accepted_at="forged-time")
    response = _resave_body(client, {"revision": row.revision}, body)
    assert response.status_code == 400, response.text
    assert "ambiguous" in response.json()["error"]["message"].lower()


@pytest.mark.parametrize("field,value", [
    ("category", "other-dimension"), ("value", "other-value"), ("source", "evidence@2"),
    ("subject", "root@2"), ("accepted", True),
])
def test_changed_assertions_stamp_the_current_owner(categories, field, value):
    client, path = categories
    body = snapshot_to_json(snapshot())
    if field == "accepted":
        for assertion in assertion_rows(body):
            assertion["accepted"] = False
    # Seed trusted history independently of the candidate endpoint.
    store = CategoryStore(path)
    first = store.create_candidate(body, None, "original-owner", "original facts")
    store.activate(first.revision, None)
    body["categories"]["dimension"]["values"].append("other-value")
    body["categories"]["other-dimension"] = {
        "id": "other-dimension", "label": "Other", "values": ["accepted"]}
    body["retained"] = {}
    service = body["services"].pop("service@1")
    service["id"] = "service@2"
    body["services"]["service@2"] = service
    for group in (body["people"], body["apps"]):
        for plans in group.values():
            plans[0]["grants"][0]["service"] = "service@2"
    member = service["members"][0]
    assertion = member["assertions"][0]
    if field == "subject":
        member["id"] = value
    else:
        assertion[field] = value
    assertion.update(accepting_owner="forged-owner", accepted_at="forged-time")
    response = client.post(PREFIX + "/candidates", headers={"test-owner": "accepting-owner"}, json={
        "base_revision": first.revision, "reason": "changed fact", "snapshot": body})
    assert response.status_code == 200, response.text
    changed = response.json()["snapshot"]["services"]["service@2"]["members"][0]["assertions"][0]
    assert changed["accepting_owner"] == "accepting-owner"
    assert changed["accepted_at"] not in ("forged-time", "accepted")
    assert changed["accepted"] is True


def test_server_provenance_can_replace_empty_client_placeholders(categories):
    client, _ = categories
    body = snapshot_to_json(snapshot())
    for assertion in assertion_rows(body):
        assertion.update(accepting_owner="", accepted_at="")
    response = client.post(PREFIX + "/candidates", json={
        "base_revision": None, "reason": "owner edit", "snapshot": body})
    assert response.status_code == 200, response.text
    for assertion in assertion_rows(response.json()["snapshot"]):
        assert assertion["accepting_owner"] == "tester"
        assert assertion["accepted_at"]


def test_restore_keeps_old_provenance_and_records_restoring_owner(categories):
    client, path = categories
    store = CategoryStore(path)
    body = snapshot_to_json(snapshot())
    first = store.create_candidate(body, None, "original-owner", "original acceptance")
    store.activate(first.revision, None)
    second = create(client, False, first.revision)
    assert activate(client, second["revision"], first.revision).status_code == 200
    response = client.post(PREFIX + f"/revisions/{first.revision}/restore",
                           headers={"test-owner": "restoring-owner"},
                           json={"base_revision": second["revision"], "reason": "restore original"})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["snapshot"] == body
    assert restored["author"] == "restoring-owner"
    audit = client.get(PREFIX + "/history").json()["history"][-1]
    assert audit["action"] == "restore"
    assert audit["author"] == "restoring-owner"
    assert audit["source_revision"] == first.revision
