"""The capability-selected service contract, once per row in ROLES.

Role-specific routing, voices, translation, and compatibility overrides remain
in test_speech_service_identity.py. No real health requests are sent here.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pytest

import proxy
from gateway import engine_record_defaults
from tests.support.config_snapshot import derived_patch
from tests.support.speech_roles import without_capability


@dataclass(frozen=True)
class Value:
    record_key: str
    derived_key: str
    candidate: Any
    live: Any
    from_default: Callable[[Any], Any]


@dataclass(frozen=True)
class Role:
    name: str
    capability: str
    legacy_id: str
    renamed_id: str
    host_key: str
    enabled_key: str
    health_function: str
    values: tuple[Value, ...]
    # Canonical, differently cased canonical, token list, cased token list.
    declarations: tuple[tuple[str, ...], ...]


ROLES = (
    Role(
        name="speech",
        capability="audio.speech",
        legacy_id="tts",
        renamed_id="voice-one",
        host_key="TTS_HOST",
        enabled_key="TTS_ENABLED",
        health_function="check_tts_health",
        values=(Value("timeout", "TTS_TIMEOUT", 45, 99, int),),
        declarations=(
            ("audio.speech",),
            ("Audio.Speech",),
            ("audio", "speech"),
            ("AuDiO", "SpEeCh"),
        ),
    ),
    Role(
        name="transcription",
        capability="audio.transcriptions",
        legacy_id="stt",
        renamed_id="ears-one",
        host_key="STT_HOST",
        enabled_key="STT_ENABLED",
        health_function="check_stt_health",
        values=(Value("timeout", "STT_TIMEOUT", 45, 99, int),),
        declarations=(
            ("audio.transcriptions",),
            ("Audio.Transcriptions",),
            ("audio", "transcriptions"),
            ("AuDiO", "TrAnScRiPtIoNs"),
        ),
    ),
    Role(
        name="embeddings",
        capability="embeddings",
        legacy_id="embeddings",
        renamed_id="vectors-one",
        host_key="EMBEDDINGS_HOST",
        enabled_key="EMBEDDINGS_ENABLED",
        health_function="check_embeddings_health",
        values=(
            Value("timeout", "EMBEDDINGS_TIMEOUT", 45, 99, int),
            Value(
                "health_endpoint", "EMBEDDINGS_HEALTH_ENDPOINT",
                "/ready", "/live-ready", str,
            ),
            Value(
                "model_id", "EMBEDDINGS_BACKEND_MODEL",
                "vectors-model", "live-model", str,
            ),
        ),
        declarations=(
            ("embeddings",),
            ("Embeddings",),
            ("openai", "embeddings"),
            ("OpEnAi", "EmBeDdInGs"),
        ),
    ),
)


@pytest.fixture(params=ROLES, ids=lambda value: value.name)
def role(request) -> Role:
    return request.param


def _config(role: Role, records: Mapping[str, Any]) -> dict[str, Any]:
    cfg = without_capability(proxy.config, role.capability)
    # Match the existing identity fixtures: a legacy id with unrelated
    # capabilities must not survive as accidental test setup.
    cfg["backends"].pop(role.legacy_id, None)
    cfg["backends"].update(copy.deepcopy(dict(records)))
    return cfg


def _record(role: Role, port: int, **overrides: Any) -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": port,
        "capabilities": [role.capability],
        **overrides,
    }


def _values(role: Role, *, live: bool = False) -> dict[str, Any]:
    return {
        value.record_key: value.live if live else value.candidate
        for value in role.values
    }


def _derived(role: Role, cfg: dict[str, Any]) -> dict[str, Any]:
    raw = proxy._derive_backend_constants(cfg)
    keys = (
        role.host_key,
        role.enabled_key,
        *(value.derived_key for value in role.values),
    )
    return {key: raw[key] for key in keys}


def _expected(
    role: Role, port: int, *, sparse: bool = False
) -> dict[str, Any]:
    expected = {
        role.host_key: f"http://127.0.0.1:{port}",
        role.enabled_key: True,
    }
    defaults = (
        engine_record_defaults.release_defaults()[role.capability]  # the defaults are keyed by capability
        if sparse else {}
    )
    expected.update({
        value.derived_key: (
            value.from_default(defaults[value.record_key])
            if sparse else value.candidate
        )
        for value in role.values
    })
    return expected


def _assert_absent(role: Role, derived: Mapping[str, Any]) -> None:
    assert derived[role.host_key] == ""
    assert derived[role.enabled_key] is False


def test_a_renamed_service_derives_the_same_settings(role):
    record = _record(role, 18101, **_values(role))
    legacy = _derived(role, _config(role, {role.legacy_id: record}))
    renamed = _derived(role, _config(role, {role.renamed_id: record}))
    assert legacy == renamed == _expected(role, 18101)
    assert renamed[role.enabled_key] is True


def test_a_sparse_renamed_record_uses_role_release_defaults(role):
    cfg = _config(role, {role.renamed_id: _record(role, 18101)})
    derived = _derived(role, cfg)
    assert derived == _expected(role, 18101, sparse=True)
    assert derived[role.enabled_key] is True


def test_a_legacy_id_without_the_capability_is_not_selected(role):
    cfg = _config(role, {
        role.legacy_id: _record(
            role, 18101, capabilities=["contract.unrelated"]
        ),
    })
    _assert_absent(role, _derived(role, cfg))


def test_lower_numeric_priority_wins_and_a_tie_uses_the_lower_id(role):
    # Reverse lexical order of the winner to distinguish the two rules.
    priority_cfg = _config(role, {
        "service-a": _record(role, 18101, priority=100),
        "service-z": _record(role, 18102, priority=50),
    })
    assert _derived(role, priority_cfg)[role.host_key] == (
        "http://127.0.0.1:18102"
    )

    # Reverse insertion order so insertion order cannot satisfy the tie test.
    tied_cfg = _config(role, {
        "service-z": _record(role, 18103, priority=50),
        "service-a": _record(role, 18104, priority=50),
    })
    assert _derived(role, tied_cfg)[role.host_key] == (
        "http://127.0.0.1:18104"
    )


def test_a_disabled_winner_does_not_fall_through(role):
    cfg = _config(role, {
        role.renamed_id: _record(
            role, 18101, priority=10, enabled=False
        ),
        role.legacy_id: _record(role, 18102, priority=20),
    })
    derived = _derived(role, cfg)
    assert derived[role.host_key] == "http://127.0.0.1:18101"
    assert derived[role.enabled_key] is False


def test_an_addressless_winner_does_not_fall_through(role):
    cfg = _config(role, {
        role.renamed_id: {
            "capabilities": [role.capability], "priority": 10,
        },
        role.legacy_id: _record(role, 18102, priority=20),
    })
    _assert_absent(role, _derived(role, cfg))


def test_derivation_uses_the_candidate_not_live_config(role, monkeypatch):
    # Build both before patching the live configuration.
    live = _config(role, {
        role.legacy_id: _record(role, 18101, **_values(role, live=True)),
    })
    candidate = _config(role, {
        role.renamed_id: _record(role, 18201, **_values(role)),
    })
    candidate_before = copy.deepcopy(candidate)
    monkeypatch.setattr(proxy, "config", live)

    derived = _derived(role, candidate)
    assert derived == _expected(role, 18201)
    assert derived[role.enabled_key] is True
    assert candidate == candidate_before


def test_health_probes_the_selected_id_and_none_when_missing(role, monkeypatch):
    probed: list[str] = []

    async def probe(service_id: str) -> bool:
        probed.append(service_id)
        return True

    selected = _config(role, {
        role.renamed_id: _record(role, 18101, priority=10),
        role.legacy_id: _record(role, 18102, priority=20),
    })
    missing = _config(role, {})
    monkeypatch.setattr(proxy, "check_service_health", probe)
    monkeypatch.setattr(proxy, "config", selected)
    with derived_patch(role.enabled_key, True):
        assert asyncio.run(getattr(proxy, role.health_function)()) is True
    assert probed == [role.renamed_id]

    monkeypatch.setattr(proxy, "config", missing)
    with derived_patch(role.enabled_key, True):
        assert asyncio.run(getattr(proxy, role.health_function)()) is False
    assert probed == [role.renamed_id]


@pytest.mark.parametrize(
    "declaration_index", range(4),
    ids=("canonical", "canonical-case", "tokens", "tokens-case"),
)
def test_equivalent_capability_spellings_select_the_same_record(
    role, declaration_index
):
    cfg = _config(role, {
        role.renamed_id: _record(
            role, 18101,
            capabilities=list(role.declarations[declaration_index]),
            **_values(role),
        ),
    })
    derived = _derived(role, cfg)
    assert derived == _expected(role, 18101)
    assert derived[role.enabled_key] is True


@pytest.mark.parametrize("kind", ("none", "string"))
def test_a_none_or_string_capabilities_field_does_not_select_a_record(role, kind):
    malformed = {"none": None, "string": role.capability}[kind]
    # Give the malformed record the legacy id to catch name-based fallback.
    cfg = _config(role, {
        role.legacy_id: _record(role, 18101, capabilities=malformed),
    })
    _assert_absent(role, _derived(role, cfg))


@pytest.mark.parametrize("kind", ("tuple", "mapping"))
def test_a_tuple_or_mapping_capabilities_field_selects_the_record_today(role, kind):
    # TODAY'S BEHAVIOUR, not a contract: backends_for_capability iterates whatever
    # the field holds, so a tuple of the capability, or a mapping keyed by it,
    # selects the record as a list would. The design expected these to leave the
    # role unselected; production code is unchanged and this pins what happens.
    malformed = {
        "tuple": (role.capability,),
        "mapping": {role.capability: True},
    }[kind]
    cfg = _config(role, {
        role.legacy_id: _record(role, 18101, capabilities=malformed, **_values(role)),
    })
    assert _derived(role, cfg) == _expected(role, 18101)
