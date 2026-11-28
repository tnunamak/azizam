"""Fixture records for the speech, transcription and embeddings services.

The gateway selects these services by the capability a record declares (``audio.speech``, ``audio.transcriptions``,
``embeddings``), not by the ids ``tts``, ``stt`` and ``embeddings``. Address and timeout tests name their records by
those ids for readability; ``declared`` gives each such record the capability its name stands for, unless the record
names its own.
"""
from __future__ import annotations

from typing import Any, Mapping

ROLE_CAPABILITIES = {"tts": ["audio", "speech"], "stt": ["audio", "transcriptions"], "embeddings": ["openai", "embeddings"],
                   "comfyui": ["images", "generations", "videos", "audio.generate"]}


def declared(backends: Mapping[str, Any]) -> dict[str, Any]:
    """*backends* with the ``tts``, ``stt``, ``embeddings`` and ``comfyui`` records declaring their capability."""
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


def without_capability(base_config: Mapping[str, Any], capability: str) -> dict[str, Any]:
    """A deep copy of *base_config* without any record that declares *capability*, disabled ones included.

    Unrelated records and all other configuration stay. The live config is never mutated. A legacy id is not removed
    for its name; a caller that needs that pops it itself.
    """
    import copy

    import proxy

    cfg = copy.deepcopy(dict(base_config))
    backends = dict(cfg.get("backends") or {})
    for service_id in proxy.backends_for_capability(capability, backends=backends, include_disabled=True):
        backends.pop(service_id, None)
    cfg["backends"] = backends
    return cfg
