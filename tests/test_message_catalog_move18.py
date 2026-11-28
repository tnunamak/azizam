"""Move 18: person-facing gateway copy lives in registry/gateway_messages.yml, byte for byte.

``tests/fixtures/message_catalog_move18.json`` holds the text as the code produced it before the move (captured by
running the code, not copied from source). Codes, HTTP statuses and decisions stay in code and are pinned here too.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

import proxy
from gateway import client_errors, gateway_messages, public_failure, startup_config
from gateway.engine_protocol.errors import ProtocolError
from gateway.transition_gate import TransitionUnavailable
from tests.test_stt_chunking import _config as stt_config

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "registry" / "gateway_messages.yml"
PINS = json.loads((Path(__file__).parent / "fixtures" / "message_catalog_move18.json").read_text(encoding="utf-8"))
PUBLIC_VALUES = PINS["public_values"]
# Source files whose person-facing sentences moved out. A sentence that comes back here is a second copy.
MOVED_FROM = ("proxy.py", "gateway/client_errors.py", "gateway/public_failure.py", "gateway/stt_chunking.py",
              "gateway/startup_config.py")


def _body(response):
    return {"status": response.status_code, "body": json.loads(response.body), "retry_after": response.headers.get("Retry-After")}


# --- the text is unchanged ---------------------------------------------------------------------------------------

def test_tables_keep_their_text_and_order():
    assert list(proxy.REMOVED_AUTH_SETTINGS.items()) == [tuple(row) for row in PINS["removed_auth_settings"]]
    assert list(proxy.REMOVED_AUTH_ENV.items()) == [tuple(row) for row in PINS["removed_auth_env"]]
    assert list(startup_config.REMOVED_AUTH_SETTINGS.items()) == [tuple(row) for row in PINS["startup_removed_auth_settings"]]
    assert list(proxy.ACCESS_ERROR_MESSAGES.items()) == [tuple(row) for row in PINS["access_errors"]]
    assert list(client_errors.REASONS.items()) == [tuple(row) for row in PINS["reasons"]]
    assert list(client_errors.STATUS_REASONS.items()) == [tuple(row) for row in PINS["status_reasons"]]
    assert list(client_errors.CODE_REASONS.items()) == [tuple(row) for row in PINS["code_reasons"]]
    assert client_errors._SERVER_REASON == PINS["server_reason"]
    assert [[key, list(row)] for key, row in public_failure._COPY.items()] == PINS["public_copy"]
    assert proxy._DEFINITION_UNAVAILABLE == PINS["definition_unavailable"]


def test_public_failure_renders_are_unchanged_for_two_sets_of_values():
    assert PUBLIC_VALUES and len(PUBLIC_VALUES) == 2
    rendered = [[key, audience, list(public_failure.describe(key, audience=audience, **values))]
                for key in public_failure._COPY for audience in ("api", "ui") for values in PUBLIC_VALUES]
    assert rendered == PINS["public_renders"]


def test_proxy_responses_keep_their_text_status_code_and_retry_header():
    unsupported = TransitionUnavailable("d", reason="activation_not_supported", retry_after_s=3)
    transitioning = TransitionUnavailable("d", reason="busy", retry_after_s=3)
    assert _body(proxy._transition_unavailable_response(unsupported)) == PINS["transition"]["unsupported"]
    assert _body(proxy._transition_unavailable_response(transitioning)) == PINS["transition"]["transitioning"]
    assert _body(proxy.audio_translation_unsupported_response()) == PINS["audio_translation"]
    assert proxy.ASSETS_DELETE_OPENAPI["responses"]["404"] == PINS["asset_delete_404"]


def test_protocol_failures_keep_their_text_and_status():
    for code, pinned in PINS["protocol_failures"].items():
        assert _body(proxy._protocol_failure(ProtocolError("x", code=code))) == pinned, code
    refused = proxy._protocol_failure(proxy.ProtocolUpstreamError(502, "The engine answered 502."))
    assert _body(refused) == PINS["protocol_upstream"]


def test_sentences_moved_out_of_function_bodies_are_the_pinned_ones():
    messages = gateway_messages.GATEWAY_MESSAGES
    sources = PINS["source_strings"]
    assert messages["mcp_reconnect_required"] == sources["mcp_reconnect"]
    assert messages["voice_design_not_configured"] == sources["voice_design"]
    assert messages["measurement_backend_unavailable"] == sources["measurement"]
    assert messages["video_extend_not_configured"] == sources["video_extend_not_configured"]
    assert messages["video_extend_shots_unsupported"] == sources["video_extend_shots"]


def test_stt_chunking_note_keeps_both_texts():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        undeclared = stt_config(root, target_seconds=30, threshold_seconds=60)
        declared = stt_config(root, target_seconds=30, threshold_seconds=40, search_seconds=2, max_segment_seconds=40)
        assert undeclared.chunking_view()["note"] == PINS["chunking_notes"]["defaults"]
        assert declared.chunking_view()["note"] == PINS["chunking_notes"]["saved"]


# --- one source for each sentence ----------------------------------------------------------------------------------

def _moved_sentences() -> list[str]:
    sentences = [row[1] for row in PINS["removed_auth_settings"]] + [row[1] for row in PINS["removed_auth_env"]]
    sentences += [row[1] for row in PINS["access_errors"]] + [row[1] for row in PINS["reasons"]]
    sentences += [row[1] for row in PINS["status_reasons"]] + [row[1] for row in PINS["code_reasons"]]
    for _key, row in PINS["public_copy"]:
        sentences += row[1:]
    sentences += [body["body"]["error"]["message"] for body in PINS["protocol_failures"].values()]
    sentences += [PINS["protocol_upstream"]["body"]["error"]["message"], PINS["definition_unavailable"],
                  PINS["asset_delete_404"]["description"].split(": ", 1)[1], *PINS["source_strings"].values(),
                  PINS["transition"]["unsupported"]["body"]["error"]["message"],
                  PINS["transition"]["transitioning"]["body"]["error"]["message"],
                  PINS["audio_translation"]["body"]["error"]["message"], *PINS["chunking_notes"].values()]
    # A template is checked up to its first placeholder, when that leaves a sentence long enough to be its own.
    heads = {sentence.split("{")[0] for sentence in sentences if "{" in sentence}
    return sorted({sentence for sentence in sentences if "{" not in sentence} | {head for head in heads if len(head) >= 30})


def test_no_moved_sentence_is_a_literal_in_the_code_again():
    sentences = _moved_sentences()
    assert len(sentences) > 150
    again = []
    for relative in MOVED_FROM:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for sentence in sentences:
                # A short sentence is a whole string; a longer one may sit inside a bigger string.
                if node.value == sentence or (len(sentence) >= 30 and sentence in node.value):
                    again.append((relative, node.lineno, sentence))
    assert again == []


def test_startup_check_and_gateway_refuse_with_the_same_words():
    assert dict(startup_config.REMOVED_AUTH_SETTINGS) == proxy.REMOVED_AUTH_SETTINGS
    assert startup_config.REMOVED_AUTH_SETTINGS is gateway_messages.GATEWAY_MESSAGE_TABLES["removed_auth_settings"]


# --- the code follows the data -----------------------------------------------------------------------------------

def _changed_flat(**changes):
    return {**gateway_messages.GATEWAY_MESSAGES, **changes}


def test_proxy_responses_follow_the_catalog(monkeypatch):
    monkeypatch.setattr(gateway_messages, "GATEWAY_MESSAGES", _changed_flat(
        transition_unsupported="Edited: transition unsupported.", engine_transitioning="Edited: transitioning.",
        audio_translation_unsupported="Edited: no translation.", protocol_failure_default="Edited: protocol default.",
        protocol_upstream_refused="Edited: upstream refused."))
    assert _body(proxy._transition_unavailable_response(
        TransitionUnavailable("d", reason="activation_not_supported")))["body"]["error"]["message"] == "Edited: transition unsupported."
    assert _body(proxy._transition_unavailable_response(
        TransitionUnavailable("d", reason="busy")))["body"]["error"]["message"] == "Edited: transitioning."
    assert _body(proxy.audio_translation_unsupported_response())["body"]["error"]["message"] == "Edited: no translation."
    assert _body(proxy._protocol_failure(ProtocolError("x", code="other")))["body"]["error"]["message"] == "Edited: protocol default."
    upstream = proxy._protocol_failure(proxy.ProtocolUpstreamError(502, "The engine answered 502."))
    assert _body(upstream)["body"]["error"]["message"] == "Edited: upstream refused."


def test_protocol_failure_table_is_read_at_call_time(monkeypatch):
    tables = {name: dict(rows) for name, rows in gateway_messages.GATEWAY_MESSAGE_TABLES.items()}
    tables["protocol_failures"]["ambiguous_model"] = "Edited: ambiguous."
    monkeypatch.setattr(gateway_messages, "GATEWAY_MESSAGE_TABLES", tables)
    assert _body(proxy._protocol_failure(ProtocolError("x", code="ambiguous_model")))["body"]["error"]["message"] == "Edited: ambiguous."


_PROBE = """
import json, sys
from gateway import gateway_messages
gateway_messages.GATEWAY_MESSAGE_TABLES = gateway_messages.load_gateway_message_tables(sys.argv[1])
from gateway import client_errors, public_failure, startup_config
print(json.dumps({
    "status": client_errors.STATUS_REASONS[401], "code": client_errors.CODE_REASONS["model_not_found"],
    "reason": client_errors.REASONS["proxy_error"], "server": client_errors._SERVER_REASON,
    "api": public_failure.describe("too_many_jobs", maximum=3), "ui_input": public_failure.describe("input_not_accepted"),
    "api_input": public_failure.describe("input_not_accepted", audience="api", input="x"),
    "removed": startup_config.REMOVED_AUTH_SETTINGS["path_tokens"],
}))
"""


def test_import_time_tables_follow_an_edited_copy_of_the_catalog(tmp_path):
    document = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    tables = document["tables"]
    tables["client_status_reasons"][401] = "Edited: status."
    tables["client_code_reasons"]["model_not_found"] = "Edited: code."
    tables["client_code_reasons"]["server_error"] = "Edited: server."
    tables["client_reasons"]["proxy_error"] = "Edited: reason."
    tables["public_failures"]["too_many_jobs"]["message"] = "Edited: only {maximum}."
    tables["public_failures"]["input_not_accepted"]["layer"] = "gateway"
    tables["public_failures"]["input_not_accepted"]["api_message"] = "Edited: no '{input}'."
    tables["removed_auth_settings"]["path_tokens"] = "Edited: path tokens."
    changed = tmp_path / "messages.yml"
    changed.write_text(yaml.safe_dump(document), encoding="utf-8")
    run = subprocess.run([sys.executable, "-c", _PROBE, str(changed)], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    seen = json.loads(run.stdout.strip().splitlines()[-1])
    unchanged_ui = dict(PINS["public_copy"])["input_not_accepted"][1]
    assert seen == {
        "status": "Edited: status.", "code": "Edited: code.", "reason": "Edited: reason.", "server": "Edited: server.",
        "api": ["account", "Edited: only 3."], "ui_input": ["gateway", unchanged_ui],
        "api_input": ["gateway", "Edited: no 'x'."], "removed": "Edited: path tokens.",
    }


# --- the reader checks the shape ---------------------------------------------------------------------------------

def _write(tmp_path, document):
    path = tmp_path / "messages.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _document():
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


def test_shipped_tables_pass_the_reader_and_are_immutable():
    tables = gateway_messages.load_gateway_message_tables(CATALOG)
    assert set(tables) == {"removed_auth_settings", "removed_auth_env", "access_errors", "protocol_failures",
                           "client_reasons", "client_status_reasons", "client_code_reasons", "public_failures"}
    with pytest.raises(TypeError):
        tables["access_errors"]["key_required"] = "replacement"


@pytest.mark.parametrize("mutate, match", [
    (lambda d: d.pop("tables"), "tables must be a mapping"),
    (lambda d: d["tables"].pop("access_errors"), "tables must be exactly"),
    (lambda d: d["tables"].update(plugin={"a": "b"}), "tables must be exactly"),
    (lambda d: d["tables"]["access_errors"].update(extra="Text."), "keys must be exactly"),
    (lambda d: d["tables"]["access_errors"].pop("key_required"), "keys must be exactly"),
    (lambda d: d["tables"]["access_errors"].update(key_required=""), "non-empty text"),
    (lambda d: d["tables"]["access_errors"].update(key_required="Needs {a}."), "no placeholders"),
    (lambda d: d["tables"]["client_status_reasons"].pop(400), "missing keys"),
    (lambda d: d["tables"]["client_status_reasons"].update({"418": "Teapot."}), "keys must be int"),
    (lambda d: d["tables"]["client_code_reasons"].update(model_not_found=None), "non-empty text"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "request"}), "layer and message"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "request", "message": "A.", "code": "x"}), "layer and message"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "", "message": "A."}), "layer"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "request", "message": "{unknown}"}), "unsupported placeholder"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "request", "message": "{maximum.real}"}), "unsupported placeholder"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "request", "message": "{maximum:>5}"}), "unsupported placeholder"),
    (lambda d: d["tables"]["public_failures"].update(x={"layer": "request", "message": "{maximum!r}"}), "unsupported placeholder"),
    (lambda d: d["tables"]["public_failures"].pop("unknown"), "missing keys"),
])
def test_reader_refuses_a_malformed_table(tmp_path, mutate, match):
    document = _document()
    mutate(document)
    with pytest.raises(ValueError, match=match):
        gateway_messages.load_gateway_message_tables(_write(tmp_path, document))


def test_a_public_failure_may_use_the_thousands_format_and_the_flat_loader_ignores_tables(tmp_path):
    document = _document()
    document["tables"]["public_failures"]["prompt_too_long"]["message"] = "At most {max_chars:,} characters."
    tables = gateway_messages.load_gateway_message_tables(_write(tmp_path, document))
    assert tables["public_failures"]["prompt_too_long"][1] == "At most {max_chars:,} characters."
    assert set(gateway_messages.load_gateway_messages(_write(tmp_path, document))) == set(gateway_messages.GATEWAY_MESSAGES)


def test_a_public_failure_with_a_layer_outside_layers_is_refused_at_import():
    assert {row[0] for row in public_failure._COPY.values()} <= public_failure.LAYERS


def test_the_asset_delete_template_takes_exactly_the_error_code(tmp_path):
    for template in ("No code here.", "{error_code} and {error_code}", "{code}", "{other}"):
        document = _document()
        document["asset_delete_not_found_description"] = template
        with pytest.raises(ValueError, match="asset_delete_not_found_description"):
            gateway_messages.load_gateway_messages(_write(tmp_path, document))
