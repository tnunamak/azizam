"""Round 3 of the W1 redaction fix: the findings of the second independent review.

Each test is a finding's input, run through the real routes or the real functions. They fail on the
round 2 code (c6f6330a) and pass on this branch.
"""

from __future__ import annotations

import copy

import pytest

from gateway import admin_store, engine_admin, engine_redaction as redaction, service_registry
from gateway.engine_redaction import REDACTED as M
from tests.test_engine_redaction import SECRET, _command_record, _put, _seed, _stored, _write
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401


def _plain_record(headers=None, address="http://engine.invalid"):
    record = _command_record()
    record.pop("lifecycle", None)
    record["config"].pop("lifecycle", None)
    record["config"]["address"] = address
    if headers is not None:
        record["config"]["headers"] = headers
    return record


def _etag(client):
    return client.get("/admin/api/operator/service-registry").headers["etag"]


def _patch(client, body, service_id="engine-a"):
    return client.patch(f"/admin/api/operator/services/{service_id}",
                        headers={**csrf_for(client), "If-Match": _etag(client)}, json=body)


def _view(client, service_id="engine-a"):
    return client.get(f"/admin/api/engines/{service_id}").json()["engine"]


class Captured(Exception):
    """Raised by the check spy so a save stops at the moment a check would send the request."""


@pytest.fixture
def spy(monkeypatch):
    seen: list[dict] = []

    async def fake_run_check(catalog, definition, clean, **kwargs):
        seen.append({"address": clean.get("address"), "headers": copy.deepcopy(clean.get("headers") or [])})
        raise Captured()

    monkeypatch.setattr(engine_admin, "run_check", fake_run_check)
    return seen


# -- HIGH 3: what the W5 page posts for an argument list, and a marker inside a word -----------------


def test_a_comma_joined_list_posted_as_a_string_is_refused_not_stored_as_one_word(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner)
    view = _view(owner)
    joined = ",".join(view["values"]["lifecycle_start"])
    assert joined == f"runner,--token,{M}"
    refused = _patch(owner, {"definition": view["definition"], "values": {"lifecycle_start": joined}})
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["process"]["legacy_lifecycle"]["driver_config"]["start"] == ["runner", "--token", SECRET]


def test_a_comma_joined_list_without_a_secret_is_refused_too() -> None:
    with pytest.raises(ValueError, match="comma-joined"):
        redaction.restore_command("runner,--port,80", ["runner", "--port", "80"])


@pytest.mark.parametrize("sent", [f"--token={M}x", f"x{M}", f"--token=a{M}b"])
def test_a_command_argument_that_keeps_a_marker_inside_a_longer_word_is_refused(sent) -> None:
    with pytest.raises(ValueError, match="inside a longer value"):
        redaction.restore_command(["runner", sent], ["runner", "--token", "T1"])


def test_an_env_value_that_keeps_a_marker_inside_a_longer_word_is_refused() -> None:
    with pytest.raises(ValueError, match="inside a longer value"):
        redaction.restore_env({"API_KEY": f"{M}x"}, {"API_KEY": "a"})


def test_the_registry_put_refuses_a_marker_inside_a_word_with_a_400(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _seed(gateway, owner, native=True)
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["process"]["start"] = ["runner", f"--token={M}x"]
    refused = _put(owner, body)
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["process"]["start"] == ["runner", "--token", SECRET]


# -- MED 4: the credential flag rule reads whole words of a name -------------------------------------


@pytest.mark.parametrize("argv, hidden", [
    (["run", "-pPASSWORD"], ["run", f"-p{M}"]),
    (["run", "-P", "PASSWORD"], ["run", "-P", M]),
    (["run", "-uadmin:PASSWORD"], ["run", f"-u{M}"]),
    (["run", "--jwt", "J"], ["run", "--jwt", M]),
    (["run", "--pin", "1234"], ["run", "--pin", M]),
    (["run", "--otp", "123456"], ["run", "--otp", M]),
    (["run", "--sessionid", "S"], ["run", "--sessionid", M]),
    (["run", "--session-id", "S"], ["run", "--session-id", M]),
    (["run", "--sig", "S"], ["run", "--sig", M]),
    (["run", "--signature", "S"], ["run", "--signature", M]),
    (["run", "--signature=S"], ["run", f"--signature={M}"]),
    (["run", "--api-key", "K"], ["run", "--api-key", M]),
    (["run", "--auth-token", "K"], ["run", "--auth-token", M]),
    (["run", "--hf-token=K"], ["run", f"--hf-token={M}"]),
    (["API_TOKEN=K", "run"], [f"API_TOKEN={M}", "run"]),
    # a name that only contains a credential word as part of another word stays visible
    (["run", "--tokenizer", "bpe"], ["run", "--tokenizer", "bpe"]),
    (["run", "--max-tokens", "4096"], ["run", "--max-tokens", "4096"]),
    (["run", "--keyfile", "/etc/key.pem"], ["run", "--keyfile", "/etc/key.pem"]),
    (["TOKENIZERS_PARALLELISM=false", "run"], ["TOKENIZERS_PARALLELISM=false", "run"]),
    (["run", "--passthrough", "x"], ["run", "--passthrough", "x"]),
    (["run", "-u", "alice"], ["run", "-u", "alice"]),
])
def test_the_flag_rule_reads_whole_name_words(argv, hidden) -> None:
    assert redaction.redact_argv(argv) == hidden


@pytest.mark.parametrize("stored, edited, want", [
    (["run", "-pPW"], ["run", f"-p{M}", "-v"], ["run", "-pPW", "-v"]),
    (["run", "-uadmin:PW"], ["run", f"-u{M}"], ["run", "-uadmin:PW"]),
    (["run", "--sig", "S"], ["run", "--sig", M, "-v"], ["run", "--sig", "S", "-v"]),
])
def test_an_attached_or_new_flag_marker_takes_the_stored_value(stored, edited, want) -> None:
    assert redaction.restore_command(edited, stored) == want


# -- MED 6: an owner saves an engine whose stored address has a user name or a query ----------------


def test_the_owner_saves_an_engine_whose_stored_address_has_user_info_and_a_query(gateway, spy) -> None:
    owner, *_ = owner_and_admin(gateway)
    raw = "http://user:pw-ECHO1@engine.invalid/v1?token=QRY-ECHO2"
    _write(gateway, _plain_record(address=raw))
    view = _view(owner)
    shown = view["values"]["address"]
    assert "pw-ECHO1" not in shown and "QRY-ECHO2" not in shown
    saved = _patch(owner, {"definition": view["definition"], "disabled": True, "values": {"address": shown}})
    assert saved.status_code == 200, saved.text
    assert _stored(gateway)["config"]["address"] == raw


def test_the_owner_still_may_not_type_user_info_into_the_address(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record())
    view = _view(owner)
    refused = _patch(owner, {"definition": view["definition"], "disabled": True,
                             "values": {"address": "http://user:pw@engine.invalid"}})
    assert refused.status_code == 400, refused.text


# -- MED 7: a refused mixed record leaves one service out, not the gateway down ----------------------


def _mixed_registry():
    mixed = _command_record(native=True)
    mixed["config"]["lifecycle"] = {"driver": "command", "driver_config": {"start": ["legacy-start"]}}
    mixed["lifecycle"] = {"driver": "command", "type": "command", "managed": True, "service_name": "",
                          "driver_config": {"start": ["legacy-start"]}}
    other = _command_record(native=True)
    other["id"] = "engine-b"
    other["name"] = "Engine B"
    other["process"] = {"driver": "none"}
    return {"schema_version": 1, "services": {"engine-a": mixed, "engine-b": other}}


def test_the_migration_leaves_a_refused_service_out_and_migrates_the_rest() -> None:
    registry = _mixed_registry()
    refused: dict[str, str] = {}
    changed = service_registry.migrate_engine_records_v2(registry, refused=refused)
    assert changed == ["engine-b"] and list(refused) == ["engine-a"]
    assert registry["schema_version"] == 2
    assert registry["services"]["engine-a"]["migration_refused"] == refused["engine-a"]
    assert "keep one" in refused["engine-a"].lower() and SECRET not in refused["engine-a"]
    assert "legacy-start" not in refused["engine-a"]


def test_a_left_out_service_does_not_validate_and_boot_serves_the_others() -> None:
    registry = _mixed_registry()
    service_registry.migrate_engine_records_v2(registry, refused={})
    with pytest.raises(ValueError, match="left out by the schema 2 migration"):
        service_registry.normalize_service_registry(registry)
    served, left_out = service_registry.servable_part_of_registry(registry)
    assert list(served["services"]) == ["engine-b"] and list(left_out) == ["engine-a"]
    assert "keep one" in left_out["engine-a"].lower()


def test_keeping_one_block_and_dropping_the_field_returns_the_service() -> None:
    registry = _mixed_registry()
    service_registry.migrate_engine_records_v2(registry, refused={})
    record = registry["services"]["engine-a"]
    for key in ("migration_refused", "lifecycle"):
        record.pop(key)
    record["config"].pop("lifecycle")
    assert sorted(service_registry.normalize_service_registry(registry)["services"]) == ["engine-a", "engine-b"]


# -- LOW 8 to 12: save edges --------------------------------------------------------------------------


def test_an_engine_create_with_a_literal_marker_in_a_header_value_is_refused(gateway) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _write(gateway, _plain_record())
    view = _view(admin)
    body = {"name": "Probe Two", "definition": view["definition"], "disabled": True,
            "values": {"address": "http://second.invalid:9", "headers": [{"name": "X-Vendor", "value": M}]}}
    refused = admin.post("/admin/api/operator/services", headers={**csrf_for(admin), "If-Match": _etag(admin)}, json=body)
    assert refused.status_code == 400, refused.text
    assert sorted(admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]) == ["engine-a"]


def test_a_registry_post_with_a_literal_marker_in_a_new_service_is_refused(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record())
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    new = copy.deepcopy(body["services"]["engine-a"])
    new["id"], new["name"] = "engine-c", "Engine C"
    new["config"]["headers"] = [{"name": "X-Vendor", "value": M}]
    body["services"]["engine-c"] = new
    refused = _put(owner, body)
    assert refused.status_code == 400, refused.text


def test_header_rows_of_one_name_keep_their_own_values_in_order(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Vendor", "value": "sk-live-D1"},
                                           {"name": "X-Vendor", "value": "sk-live-D2"}]))
    view = _view(owner)
    assert view["values"]["headers"] == [{"name": "X-Vendor", "value": M}, {"name": "X-Vendor", "value": M}]
    saved = _patch(owner, {"definition": view["definition"], "disabled": True,
                           "values": {"headers": view["values"]["headers"]}})
    assert saved.status_code == 200, saved.text
    assert [row["value"] for row in _stored(gateway)["config"]["headers"]] == ["sk-live-D1", "sk-live-D2"]


def test_dropping_one_of_two_header_rows_of_a_name_is_refused_not_rebound(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Vendor", "value": "sk-live-D1"},
                                           {"name": "X-Vendor", "value": "sk-live-D2"}]))
    view = _view(owner)
    refused = _patch(owner, {"definition": view["definition"], "disabled": True,
                             "values": {"headers": [{"name": "X-Vendor", "value": M}]}})
    assert refused.status_code == 400, refused.text
    assert [row["value"] for row in _stored(gateway)["config"]["headers"]] == ["sk-live-D1", "sk-live-D2"]


def test_a_header_row_renamed_with_its_marker_is_a_400_not_a_silent_drop(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Vendor", "value": "sk-live-R1"}]))
    view = _view(owner)
    refused = _patch(owner, {"definition": view["definition"], "disabled": True,
                             "values": {"headers": [{"name": "X-Renamed", "value": M}]}})
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["config"]["headers"] == [{"name": "X-Vendor", "value": "sk-live-R1"}]


def test_a_literal_marker_in_a_plain_header_is_refused(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Team", "value": "team-public"}]))
    view = _view(owner)
    refused = _patch(owner, {"definition": view["definition"], "disabled": True,
                             "values": {"headers": [{"name": "X-Team", "value": M}]}})
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["config"]["headers"] == [{"name": "X-Team", "value": "team-public"}]


@pytest.mark.parametrize("stored, edited", [
    (["r", "--token", "A", "--token", "B"], ["r", "--token", M]),  # a pair removed: the survivor would take A
    (["r", "--token", "A"], ["r", "--token", M, "--token", "new"]),  # a pair added
    (["r", "--token", "A"], ["r", "--token", "new", "--token", M]),  # a pair inserted before the marker
])
def test_a_save_that_changes_how_many_values_a_flag_holds_cannot_rebind_a_marker(stored, edited) -> None:
    with pytest.raises(ValueError, match="stored value"):
        redaction.restore_command(edited, stored)


def test_a_save_that_keeps_every_value_of_a_flag_and_types_one_anew_is_allowed() -> None:
    stored = ["r", "--token", "A", "--token", "B"]
    assert redaction.restore_command(["r", "--token", "new", "--token", M], stored) == ["r", "--token", "new", "--token", "B"]


# -- D21 point 3: a restored stored secret does not follow a new host to a non-owner ------------------


def test_an_administrator_cannot_move_a_header_credential_to_a_new_host_with_its_marker(gateway, spy) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Vendor", "value": "sk-live-EXF1"}]))
    view = _view(admin)
    assert view["values"]["headers"] == [{"name": "X-Vendor", "value": M}]
    body = {"definition": view["definition"], "disabled": True,
            "values": {"address": "http://attacker.invalid:8443", "headers": [{"name": "X-Vendor", "value": M}]}}
    refused = _patch(admin, body)
    assert refused.status_code == 403, refused.text
    assert _stored(gateway)["config"]["address"] == "http://engine.invalid"
    assert not spy


def test_an_administrator_can_still_change_a_header_that_holds_no_credential(gateway) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Team", "value": "team-public"}]))
    view = _view(admin)
    body = {"definition": view["definition"], "disabled": True,
            "values": {"address": "http://elsewhere.invalid:8443", "headers": [{"name": "X-Team", "value": "other"}]}}
    saved = _patch(admin, body)
    assert saved.status_code == 200, saved.text


def test_the_owner_may_move_a_header_credential_with_its_marker(gateway, spy) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record(headers=[{"name": "X-Vendor", "value": "sk-live-EXF2"}]))
    view = _view(owner)
    body = {"definition": view["definition"], "disabled": True,
            "values": {"address": "http://moved.invalid:8443", "headers": [{"name": "X-Vendor", "value": M}]}}
    saved = _patch(owner, body)
    assert saved.status_code == 200, saved.text
    assert _stored(gateway)["config"]["headers"] == [{"name": "X-Vendor", "value": "sk-live-EXF2"}]


def test_a_credential_in_a_record_counts_as_a_reference_to_the_host_check() -> None:
    record = _command_record(native=True)
    record["config"]["headers"] = [{"name": "X-Vendor", "value": "sk-live-HDR"}]
    refs = service_registry.credential_references(record)
    assert any("X-Vendor".lower() in path or "x-vendor" in path for path in refs), refs
    assert any(path.startswith("secret:.process.start[--token#0]") for path in refs), refs
    assert any(path.startswith("secret:.process.env.API_KEY") for path in refs), refs


def test_a_form_that_sends_back_a_shown_address_keeps_the_stored_one() -> None:
    stored = {"base_url": "https://api.invalid/v1?api-version=2024", "name": "x"}
    shown = redaction.public_value(stored)
    assert shown["base_url"] == "https://api.invalid/v1"
    assert redaction.restore_shown_scalars({"base_url": shown["base_url"], "name": "y"}, stored) == {
        "base_url": stored["base_url"], "name": "y"}
    assert redaction.restore_shown_scalars({"base_url": "https://other.invalid/v1"}, stored) == {"base_url": "https://other.invalid/v1"}


def test_a_check_that_ran_against_a_stored_address_does_not_repeat_its_secrets(gateway, monkeypatch) -> None:
    """The save re-checks the engine at its stored address; a refusal quotes it, and the response must not."""
    owner, *_ = owner_and_admin(gateway)
    raw = "http://user:pw-CHECK1@engine.invalid/v1?token=QRY-CHECK2"
    _write(gateway, _plain_record(address=raw, headers=[{"name": "X-Team", "value": "team-public"}]))
    view = _view(owner)

    async def refusing_check(catalog, definition, clean, **kwargs):
        return {"outcome": "error", "findings": [{
            "severity": "error", "field": "address",
            "what": f"Azizam could not reach {clean['address']} (user pw-CHECK1, token QRY-CHECK2)."}]}

    monkeypatch.setattr(engine_admin, "run_check", refusing_check)
    refused = _patch(owner, {"definition": view["definition"],
                             "values": {"headers": [{"name": "X-Team", "value": "changed"}]}})
    assert refused.status_code == 422, refused.text
    assert "pw-CHECK1" not in refused.text and "QRY-CHECK2" not in refused.text
    assert "could not reach" in refused.text


def test_a_url_with_user_info_in_an_argument_list_is_hidden_and_restored_by_position() -> None:
    stored = ["run", "--upstream", "http://u:pw-ARGV1@host.invalid/x", "--name", "plain"]
    shown = redaction.redact_argv(stored)
    assert shown == ["run", "--upstream", M, "--name", "plain"]
    assert redaction.restore_argv(shown, stored) == stored


def test_a_form_save_of_a_remote_provider_keeps_the_stored_base_url_the_registry_view_shortened(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    record = _plain_record()
    record["config"].pop("address")
    record["config"].update({"remote_provider": True, "base_url": "https://api.invalid/v1?api-version=2024",
                             "api_key_env": "SOME_PROVIDER_KEY", "credential_origin": "https://api.invalid:443"})
    _write(gateway, record)
    shown = owner.get("/admin/api/operator/service-registry").json()["service_registry"]["services"]["engine-a"]["config"]["base_url"]
    assert shown == "https://api.invalid/v1"
    saved = _patch(owner, {"name": "Renamed", "base_url": shown})
    assert saved.status_code == 200, saved.text
    assert _stored(gateway)["config"]["base_url"] == "https://api.invalid/v1?api-version=2024"


def test_the_marker_text_sent_for_a_value_that_was_never_hidden_is_refused(gateway) -> None:
    owner, *_ = owner_and_admin(gateway)
    _write(gateway, _plain_record())
    body = owner.get("/admin/api/operator/service-registry").json()["service_registry"]
    body["services"]["engine-a"]["name"] = "<redacted>"
    refused = _put(owner, body)
    assert refused.status_code == 400, refused.text
    assert _stored(gateway)["name"] == "Engine A"
