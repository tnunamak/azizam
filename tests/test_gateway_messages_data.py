"""Freeze gateway copy before extraction; message data must never select behavior."""

import copy
from pathlib import Path

import pytest
import yaml

from gateway import extension_catalog as catalog
from gateway import toolcall_detection as detection
from tests.support import c2d_parity


MESSAGES = Path(__file__).resolve().parents[1] / "registry" / "gateway_messages.yml"
EXPECTED_METADATA = {
    "name": "metadata",
    "values": ["full", "none"],
    "default": "the app profile's media_metadata.default",
    "description": (
        "Whether delivered media keeps its embedded ComfyUI workflow "
        "and gateway provenance (full) or has all metadata removed "
        "losslessly (none). Valid on every route that returns generated media."
    ),
}
MESSAGE_FIELDS = {
    "metadata_description", "tool_call_advisory", "backend_label", "unknown_family",
}


def _expected_advisory(backend, family):
    # Literal snapshot of the base, not regenerated from the message registry.
    return (
        f"Detected {family or 'unknown'}-format tool calls in the response content, but tool_calls "
        "is empty; this backend"
        + (f" ({backend})" if backend else "")
        + " is not lifting tool calls into the OpenAI tool_calls field. Enable "
        "tool-call parsing in the backend's own configuration (most OpenAI-"
        "compatible servers have a flag or template setting for this), and confirm "
        "the loaded model's chat template supports tools."
    )


def _metadata(summary):
    workflow = next(row for row in summary["extensions"] if row["namespace"] == "com.azizam.workflow")
    return next(control for control in workflow["gateway_controls"] if control["name"] == "metadata")


def _document():
    return yaml.safe_load(MESSAGES.read_text(encoding="utf-8"))


def _write(tmp_path, document):
    path = tmp_path / "messages.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_shipped_catalog_and_advisory_copy_are_byte_identical():
    registry = c2d_parity.shipped_registry()
    golden = c2d_parity.load_golden("extension_catalog")
    for audience in catalog.VALID_AUDIENCES:
        summary = catalog.summarize_extensions(registry, audience)
        assert summary == golden["summaries"][audience]
        assert _metadata(summary) == EXPECTED_METADATA
        assert _metadata(summary)["description"].encode("utf-8") == EXPECTED_METADATA["description"].encode("utf-8")

    for backend in ("vllm", "VLLM", "my-vllm-gpu", "ollama", "sglang", "unfamiliar-server", "", "{family}"):
        for family in ("", "unfamiliar-family", "{backend_hint}", *(family for _, family in detection.TOOLCALL_MARKERS)):
            expected = _expected_advisory(backend, family)
            assert detection.advisory_for_backend(backend, family).encode("utf-8") == expected.encode("utf-8")
            for override in (None, "", "   ", "  Restart the configured parser.  "):
                appended = f" {override.strip()}" if override else ""
                assert detection.advisory_for_backend(backend, family, override) == expected + appended


def test_catalog_and_advisory_copy_are_data(tmp_path, monkeypatch):
    registry = c2d_parity.shipped_registry()
    before = {audience: catalog.summarize_extensions(registry, audience) for audience in catalog.VALID_AUDIENCES}
    routes = catalog.EXTENSION_ROUTES
    markers = list(detection.TOOLCALL_MARKERS)
    changed = _document()
    changed.update({
        "metadata_description": "Operator-written metadata help.",
        "tool_call_advisory": "Check {family} calls{backend_label}; use the configured parser.",
        "backend_label": " on [{backend_hint}]",
        "unknown_family": "unidentified",
    })
    monkeypatch.setattr(detection, "GATEWAY_MESSAGES", detection.load_gateway_messages(_write(tmp_path, changed)))

    for audience, original in before.items():
        result = catalog.summarize_extensions(registry, audience)
        assert _metadata(result)["description"] == changed["metadata_description"]
        _metadata(result)["description"] = _metadata(original)["description"]
        assert result == original  # Every non-copy catalog fact stays unchanged.
    assert catalog.EXTENSION_ROUTES is routes
    assert detection.TOOLCALL_MARKERS == markers

    for backend in ("vllm", "VLLM", "ollama", "unfamiliar-server", "", "{family}"):
        for marker, family in markers:
            payload = {"choices": [{"message": {"content": marker, "tool_calls": []}}]}
            original = copy.deepcopy(payload)
            assert detection.detect_unlifted_toolcalls(payload, True) == family
            assert detection.detect_unlifted_toolcalls(payload, False) is None
            assert payload == original
            label = f" on [{backend}]" if backend else ""
            expected = f"Check {family} calls{label}; use the configured parser."
            assert detection.advisory_for_backend(backend, family) == expected
            assert detection.advisory_for_backend(backend, family, "  Operator override.  ") == expected + " Operator override."
    assert detection.advisory_for_backend("", "") == "Check unidentified calls; use the configured parser."


def test_gateway_messages_schema_is_closed(tmp_path):
    valid = _document()
    for key in ("unexpected", "python_import", "backend_overrides"):
        with pytest.raises(ValueError, match="unknown keys"):
            detection.load_gateway_messages(_write(tmp_path, {**valid, key: "package.module:handler"}))
    for key in {"schema_version", *MESSAGE_FIELDS}:
        missing = dict(valid)
        del missing[key]
        with pytest.raises(ValueError, match="missing keys"):
            detection.load_gateway_messages(_write(tmp_path, missing))


def test_gateway_messages_require_mapping_and_exact_version(tmp_path):
    for document in (None, [], "messages", 1):
        with pytest.raises(ValueError, match="mapping"):
            detection.load_gateway_messages(_write(tmp_path, document))
    for version in (True, False, None, "1", 1.0, 0, 2):
        with pytest.raises(ValueError, match="schema_version"):
            detection.load_gateway_messages(_write(tmp_path, {**_document(), "schema_version": version}))


def test_gateway_messages_require_nonempty_text(tmp_path):
    for field in MESSAGE_FIELDS:
        for value in (None, False, 1, [], {}, "", "   "):
            with pytest.raises(ValueError, match=field):
                detection.load_gateway_messages(_write(tmp_path, {**_document(), field: value}))


def test_gateway_message_templates_refuse_nonliteral_fields(tmp_path):
    invalid = (
        ("tool_call_advisory", "No labels."),
        ("tool_call_advisory", "{family}"),
        ("tool_call_advisory", "{family}{backend_label}{family}"),
        ("tool_call_advisory", "{family}{backend_label}{unknown}"),
        ("tool_call_advisory", "{family.__class__}{backend_label}"),
        ("tool_call_advisory", "{family[0]}{backend_label}"),
        ("tool_call_advisory", "{family!r}{backend_label}"),
        ("tool_call_advisory", "{family:>20}{backend_label}"),
        ("tool_call_advisory", "{family:{backend_label}}"),
        ("tool_call_advisory", "{}{backend_label}"),
        ("tool_call_advisory", "{0}{backend_label}"),
        ("tool_call_advisory", "{family}{backend_label"),
        ("backend_label", "No backend."),
        ("backend_label", "{backend_hint.__class__}"),
        ("backend_label", "{backend_hint!s}"),
        ("backend_label", "{backend_hint:>20}"),
        ("backend_label", "{backend_hint}{backend_hint}"),
    )
    for field, template in invalid:
        with pytest.raises(ValueError, match=field):
            detection.load_gateway_messages(_write(tmp_path, {**_document(), field: template}))
    valid = _document()
    valid["tool_call_advisory"] = "{{literal}} {family}{backend_label}"
    valid["backend_label"] = " ({backend_hint})"
    messages = detection.load_gateway_messages(_write(tmp_path, valid))
    assert messages["tool_call_advisory"].format(family="new", backend_label=" (server)") == "{literal} new (server)"


def test_gateway_messages_are_immutable_and_missing_file_fails_closed(tmp_path):
    messages = detection.load_gateway_messages(MESSAGES)
    assert set(messages) == MESSAGE_FIELDS
    with pytest.raises(TypeError):
        messages["metadata_description"] = "replacement"
    with pytest.raises(FileNotFoundError):
        detection.load_gateway_messages(tmp_path / "missing.yml")
