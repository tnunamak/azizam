"""A marker sent back takes the stored value of the same credential flag, never of the same position.

The reviewer's input: stored ``runner --token T1 --key K1``; the owner removes the ``--token`` pair and
saves ``runner --key [redacted]``. A positional restore stored ``--key T1``: the wrong secret behind the
right flag. A marker binds to the flag before it (or the name before ``=``), and a marker whose flag has
no stored value is refused.
"""

from __future__ import annotations

import pytest

from gateway import engine_redaction as redaction
from gateway.engine_redaction import REDACTED as M
from tests.test_engine_redaction import (
    SECRET, _command_record, _put, _seed, _stored, _write,
)
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

TWO = ["runner", "--token", "T1", "--key", "K1"]


@pytest.mark.parametrize("stored, edited, want", [
    (TWO, ["runner", "--key", M], ["runner", "--key", "K1"]),
    (TWO, ["runner", "--token", M], ["runner", "--token", "T1"]),
    (TWO, ["runner", "--key", M, "--token", M], ["runner", "--key", "K1", "--token", "T1"]),
    (TWO, ["runner", "-v", "--token", M, "--key", M], ["runner", "-v", "--token", "T1", "--key", "K1"]),
    (["r", "--token", "A", "--token", "B"], ["r", "--token", M, "--token", M], ["r", "--token", "A", "--token", "B"]),
    (["r", "--token", "A", "--token", "B"], ["r", "--token", "new", "--token", M], ["r", "--token", "new", "--token", "B"]),
    (["r", "--token=T1", "--key=K1"], ["r", f"--key={M}"], ["r", "--key=K1"]),
    (["r", "--token=T1", "--key", "K1"], ["r", "--key", M, f"--token={M}"], ["r", "--key", "K1", "--token=T1"]),
    ("r --token T1 --key K1", f"r --key {M}", "r --key K1"),
    (["r", "--token=T1"], ["r", "--token", M], ["r", "--token", "T1"]),  # one flag, either spelling
    (["API_KEY=a", "run"], ["run", f"API_KEY={M}"], ["run", "API_KEY=a"]),
])
def test_a_marker_takes_the_stored_value_of_its_own_flag(stored, edited, want) -> None:
    assert redaction.restore_command(edited, stored) == want


@pytest.mark.parametrize("stored, edited", [
    (["r", "--token", "T1", "--name", "x"], ["r", "--name", M]),  # the marker moved to a flag that held no secret
    (["r", "--token", "T1"], ["r", "--key", M]),
    (["r", "--token", "T1"], ["r", f"--key={M}"]),
    (["r", "--token", "T1"], ["r", "--token", M, "--token", M]),  # one stored value, two markers
])
def test_a_marker_whose_flag_has_no_stored_value_is_refused(stored, edited) -> None:
    with pytest.raises(ValueError, match="stored value"):
        redaction.restore_command(edited, stored)


def test_the_reviewers_input_through_the_registry_route_keeps_the_right_secret(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    record = _command_record()
    for block in (record["lifecycle"], record["config"]["lifecycle"]):
        block["driver_config"]["start"] = list(TWO)
    _write(gateway, record)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    assert body["services"]["engine-a"]["process"]["start"] == ["runner", "--token", M, "--key", M]
    body["services"]["engine-a"]["process"]["start"] = ["runner", "--key", M]
    assert _put(owner, body).status_code == 200
    assert _stored(gateway)["process"]["start"] == ["runner", "--key", "K1"]


def test_a_marker_for_a_flag_with_no_stored_value_is_a_400_for_the_owner(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["process"]["start"] = ["runner", "--name", M]
    refused = _put(owner, body)
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["process"]["start"] == ["runner", "--token", SECRET]


@pytest.mark.parametrize("path", ["start", "env"])
def test_an_administrator_gets_the_owner_answer_whether_or_not_a_secret_is_stored_at_the_marker(gateway, path) -> None:
    """403 owner_required either way: the status must not say whether a stored secret sits at the marker."""
    owner, _, _, admin = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    answers = []
    for edit in ({"start": ["runner", "--token", M], "env": {"API_KEY": M, "MODE": "prod"}},  # secret stored here
                 {"start": ["runner", "--name", M], "env": {"OTHER": M, "MODE": "prod"}}):  # none stored here
        body = admin.get("/admin/api/operator/service-registry").json()["service_registry"]
        body["services"]["engine-a"]["process"][path] = edit[path]
        reply = _put(admin, body)
        answers.append((reply.status_code, reply.json().get("error", {}).get("code")))
    assert answers[1] == (403, "owner_required")
    assert answers[0] in {(200, None), (403, "owner_required")}
    assert _stored(gateway)["process"]["start"] == ["runner", "--token", SECRET]


def test_an_engine_page_save_by_an_administrator_with_an_unmatched_marker_is_403_not_400(gateway) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    record = _command_record()
    record["lifecycle"]["driver_config"]["start"] = "runner --port 80"
    record["config"]["lifecycle"] = {**record["config"]["lifecycle"], "driver_config": {"start": "runner --port 80", "status": ["status"]}}
    _write(gateway, record)
    view = admin.get("/admin/api/engines/engine-a").json()["engine"]
    etag = admin.get("/admin/api/operator/service-registry").headers["etag"]
    reply = admin.patch("/admin/api/operator/services/engine-a", headers={**csrf_for(admin), "If-Match": etag},
                        json={"definition": view["definition"], "values": {"lifecycle_start": f"runner --token {M}"}})
    assert reply.status_code == 403, reply.text
    assert reply.json()["error"]["code"] == "owner_required"


@pytest.mark.parametrize("argv, expected", [
    (["run", "--pass", "x"], ["run", "--pass", M]),
    (["run", "--pass=x"], ["run", f"--pass={M}"]),
    (["run", "--passphrase", "x"], ["run", "--passphrase", M]),
    (["run", "--pwd", "x"], ["run", "--pwd", M]),
    (["run", "--pwd=x"], ["run", f"--pwd={M}"]),
    (["run", "-u", "alice:pw"], ["run", "-u", M]),
    (["run", "--user", "alice:pw"], ["run", "--user", M]),
    (["run", "--user=alice:pw"], ["run", f"--user={M}"]),
    (["run", "--cookie", "sid=abc"], ["run", "--cookie", M]),
    (["run", "--cookie=sid=abc"], ["run", f"--cookie={M}"]),
    (["run", "-u", "alice"], ["run", "-u", "alice"]),  # a name alone is not a credential
    (["run", "--user", "alice"], ["run", "--user", "alice"]),
    (["run", "--passthrough", "x"], ["run", "--passthrough", "x"]),
])
def test_the_credential_flag_rule_covers_pass_pwd_user_pairs_and_cookies(argv, expected) -> None:
    assert redaction.redact_argv(argv) == expected
    assert redaction.redact_argv(expected) == expected


def test_a_user_pair_marker_takes_back_its_stored_pair() -> None:
    stored = ["run", "-u", "alice:pw", "--cookie", "sid=abc"]
    assert redaction.restore_command(["run", "-u", M, "--cookie", M], stored) == stored
