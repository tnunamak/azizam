"""Fixture records for the speech and transcription services.

The gateway selects these services by the capability a record declares (``audio.speech``, ``audio.transcriptions``),
not by the ids ``tts`` and ``stt``. Address and timeout tests name their records ``tts`` and ``stt`` for readability;
``declared`` gives each such record the capability its name stands for, unless the record names its own.
"""
from __future__ import annotations

from typing import Any, Mapping

ROLE_CAPABILITIES = {"tts": ["audio", "speech"], "stt": ["audio", "transcriptions"]}


def declared(backends: Mapping[str, Any]) -> dict[str, Any]:
    """*backends* with the ``tts`` and ``stt`` records declaring their capability."""
    out = {}
    for service_id, record in backends.items():
        record = dict(record)
        if service_id in ROLE_CAPABILITIES:
            record.setdefault("capabilities", list(ROLE_CAPABILITIES[service_id]))
        out[service_id] = record
    return out


def sole_transcription(backends: Mapping[str, Any], record: Mapping[str, Any], service_id: str = "stt") -> dict[str, Any]:
    """*backends* with *record*, as *service_id*, the only transcription service; other records stay as they are.

    Patch it in with ``patch.dict(..., clear=True)``: a transcription record the base config holds would otherwise stay
    selectable beside the fixture's record.
    """
    import proxy

    others = set(proxy.backends_for_capability("audio.transcriptions", backends=backends, include_disabled=True))
    out = {key: value for key, value in backends.items() if key not in others}
    out.update(declared({service_id: record}))
    return out
