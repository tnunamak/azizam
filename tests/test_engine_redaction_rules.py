"""The rules that decide which command argument and which environment value is a secret."""

from __future__ import annotations

import pytest

from gateway import engine_redaction as redaction
from gateway.engine_redaction import REDACTED


@pytest.mark.parametrize("argv, expected", [
    (["run", "--token=x"], ["run", f"--token={REDACTED}"]),
    (["run", "-k", "x"], ["run", "-k", REDACTED]),
    (["run", "--api-key", "x"], ["run", "--api-key", REDACTED]),
    (["run", "--API_KEY", "x"], ["run", "--API_KEY", REDACTED]),
    (["run", "--Password=hunter2"], ["run", f"--Password={REDACTED}"]),
    (["run", "--client-secret", "s3"], ["run", "--client-secret", REDACTED]),
    (["run", "--auth", "u:p"], ["run", "--auth", REDACTED]),
    (["run", "--bearer", "b"], ["run", "--bearer", REDACTED]),
    (["run", "sk-abcdefgh12345678"], ["run", REDACTED]),
    (["run", "opk_abc123def456"], ["run", REDACTED]),
    (["run", "Bearer abcdef"], ["run", REDACTED]),
    (["API_KEY=x", "run"], [f"API_KEY={REDACTED}", "run"]),
    (["run", "--url=http://h/v1?token=abc123"], ["run", REDACTED]),
    (["run", "--port", "8080", "--model", "m", "--keep-alive", "5", "--author", "x"],
     ["run", "--port", "8080", "--model", "m", "--keep-alive", "5", "--author", "x"]),
    (["run", "--token"], ["run", "--token"]),
    (["run", "--token", "--verbose"], ["run", "--token", "--verbose"]),
])
def test_an_argument_that_carries_a_credential_is_hidden(argv, expected) -> None:
    assert redaction.redact_argv(argv) == expected
    assert redaction.redact_argv(expected) == expected


def test_an_env_keeps_its_names_and_hides_every_value() -> None:
    assert redaction.redact_env({"API_KEY": "x", "MODE": "prod", "EMPTY": ""}) == {
        "API_KEY": REDACTED, "MODE": REDACTED, "EMPTY": REDACTED}
    assert redaction.redact_env({}) == {}


def test_a_command_line_is_hidden_the_same_way_and_left_alone_when_clean() -> None:
    assert redaction.redact_command("runner --token abc --port 80") == f"runner --token {REDACTED} --port 80"
    assert redaction.redact_command("runner --api-key=abc") == f"runner --api-key={REDACTED}"
    assert redaction.redact_command("runner --port   80") == "runner --port   80"
    assert redaction.redact_command("runner --token 'abc") == REDACTED  # does not parse, looks credential-bearing


@pytest.mark.parametrize("stored, shown, edited, want", [
    (["r", "--token", "S"], ["r", "--token", REDACTED], ["r", "-v", "--token", REDACTED], ["r", "-v", "--token", "S"]),
    (["r", "--token=S"], ["r", f"--token={REDACTED}"], ["r", f"--token={REDACTED}", "-v"], ["r", "--token=S", "-v"]),
    (["r", "-k", "S1", "--password", "S2"], None, ["r", "-k", REDACTED, "--password", REDACTED], ["r", "-k", "S1", "--password", "S2"]),
    (["r", "--token", "S"], None, ["r", "--token", "new"], ["r", "--token", "new"]),
    ("r --token S --port 1", None, f"r --token {REDACTED} --port 2", "r --token S --port 2"),
    ("r --token S", None, f"r --token {REDACTED}", "r --token S"),
])
def test_a_marker_sent_back_takes_the_stored_value(stored, shown, edited, want) -> None:
    assert redaction.restore_command(edited, stored) == want


@pytest.mark.parametrize("stored, edited", [
    (["r"], ["r", "--token", REDACTED]),
    (["r", "--token", "S"], ["r", "--token", REDACTED, "--key", REDACTED]),
    (None, ["r", f"--token={REDACTED}"]),
    ("r", f"r --token {REDACTED}"),
])
def test_a_marker_with_no_stored_value_is_refused(stored, edited) -> None:
    with pytest.raises(ValueError, match="stored value"):
        redaction.restore_command(edited, stored)


def test_env_markers_take_the_stored_value_by_name_and_refuse_an_unknown_name() -> None:
    assert redaction.restore_env({"A": REDACTED, "B": "new"}, {"A": "1", "B": "2"}) == {"A": "1", "B": "new"}
    with pytest.raises(ValueError, match="'C'"):
        redaction.restore_env({"C": REDACTED}, {"A": "1"})


def test_the_public_process_is_an_allow_list_and_hides_commands() -> None:
    process = {"driver": "process", "start": ["r", "--token", "S"], "env": {"K": "S"}, "port": {"fixed": 1},
               "legacy_lifecycle": {"driver": "command", "driver_config": {"start": ["r", "--token", "S"]}},
               "profile_apply": {"kind": "write_env_file_then_restart", "file": "/f", "reload": ["sc", "--password", "S"]},
               "internal_note": "S", "ready_timeout_s": 3}
    public = redaction.public_process(process)
    assert "S" not in repr(public)
    assert "legacy_lifecycle" not in public and "internal_note" not in public
    assert public["start"] == ["r", "--token", REDACTED]
    assert public["profile_apply"]["reload"] == ["sc", "--password", REDACTED]
    assert public["port"] == {"fixed": 1} and public["ready_timeout_s"] == 3
    assert process["start"] == ["r", "--token", "S"]  # the stored block is not touched
