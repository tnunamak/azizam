"""A fresh gateway contacts no engine: no record means not configured, never a guessed loopback port (S8).

Acceptance test of docs/design/CODE_TO_DATA.md S8. The record-level cases (absent, partial, disabled) are pinned in
tests/test_engine_address_from_record.py; this file drives the real app with the derived values of an empty registry.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import proxy
from tests.support.speech_roles import declared

ORACLE = Path(__file__).parent / "support" / "fresh_gateway_oracle.py"


def run_oracle(tmp_path, inject=None):
    """Run the oracle in a fresh interpreter whose network guards precede every application import."""
    command = [sys.executable, str(ORACLE), str(tmp_path)] + (["--inject", inject] if inject else [])
    done = subprocess.run(command, capture_output=True, text=True, cwd=ORACLE.parents[2], timeout=300, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _forbidden(report):
    """Every guarded call except a connection to the release LiteLLM address (registry/services.yml data)."""
    allowed = urlsplit(report["released"]).netloc
    return [a for a in report["attempts"]
            if not (a["call"] == "connect" and a["to"].strip("()").replace("'", "").replace(", ", ":") == allowed)]


def test_fresh_gateway_has_zero_engine_egress(tmp_path):
    report = run_oracle(tmp_path)
    assert report["preloaded_at_guard"] == [], "an application module was imported before the guards"
    for key in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "GRAPH_HOST"):
        assert report["derived"][key] == "", key
    for key in ("TTS_ENABLED", "STT_ENABLED", "EMBEDDINGS_ENABLED", "GRAPH_ENABLED"):
        assert report["derived"][key] is False, key
    assert all(status >= 400 for _, status in report["statuses"]), report["statuses"]
    allowed = urlsplit(report["released"]).netloc
    assert {urlsplit(f"//{to}").netloc for to in report["sent"]} <= {allowed}, report["sent"]
    assert not _forbidden(report), report["attempts"]


@pytest.mark.parametrize("kind,call", [
    ("connect", "connect"), ("dns", "getaddrinfo"), ("gethostbyname", "gethostbyname"), ("udp", "sendto"),
    ("connect_ex", "connect_ex"), ("gethostbyname_ex", "gethostbyname_ex"), ("sendmsg", "sendmsg"),
])
def test_an_import_time_attempt_that_swallows_its_error_still_fails(tmp_path, kind, call):
    report = run_oracle(tmp_path, inject=kind)
    assert [a for a in _forbidden(report) if a["call"] == call], report["attempts"]


@pytest.mark.parametrize("record", [
    {}, {"enabled": True}, {"host": "203.0.113.9"}, {"port": 9999},
    {"host": "203.0.113.9", "port": 9999, "enabled": False},
], ids=["empty", "enabled-only", "no-port", "no-host", "disabled"])
def test_absent_partial_and_disabled_records_never_dial(record):
    derived = proxy._derive_backend_constants({"backends": declared({
        "tts": dict(record), "stt": dict(record), "embeddings": dict(record), "comfyui": dict(record)})})
    complete_enabled = bool(record.get("host") and record.get("port")) and record.get("enabled", True) is not False
    for key in ("TTS_ENABLED", "STT_ENABLED", "GRAPH_ENABLED"):
        assert derived[key] is complete_enabled, key
    if not (record.get("host") and record.get("port")):
        assert {derived[k] for k in ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "GRAPH_HOST")} == {""}


def test_removed_on_reload_record_is_unconfigured():
    before = proxy._derive_backend_constants({"backends": declared({"tts": {"host": "203.0.113.9", "port": 9999}})})
    after = proxy._derive_backend_constants({"backends": {}})
    assert before["TTS_ENABLED"] is True and before["TTS_HOST"] == "http://203.0.113.9:9999"
    assert after["TTS_ENABLED"] is False and after["TTS_HOST"] == ""


def test_explicit_loopback_address_from_the_operator_stays_allowed():
    derived = proxy._derive_backend_constants({"backends": declared({"tts": {"host": "127.0.0.1", "port": 8880}})})
    assert derived["TTS_HOST"] == "http://127.0.0.1:8880" and derived["TTS_ENABLED"] is True
