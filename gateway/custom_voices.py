"""Per-API-key store for user-created voices and consent recordings.

Layout under ``root``::

    <owner>/voices/<voice_id>/{reference.wav, original.<ext>, meta.json, <engine caches>}
    <owner>/consents/<consent_id>/{recording.<ext>, meta.json}

``<owner>`` is the first 32 hex digits of sha256 of the credential hash (``local`` when auth is
off), so no key material is on disk. A voice or consent that belongs to another key is reported as
not found, never as forbidden. Records are written to a temporary directory and renamed, so a
crash leaves no half-created voice. Deleting a voice removes its directory, engine caches included.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from gateway.transforms import CANONICAL_TTS_VOICES, LEGACY_TTS_VOICE_ALIASES

VOICE_ID_RE = re.compile(r"^voice_[0-9a-f]{24}$")
CONSENT_ID_RE = re.compile(r"^cons_[0-9a-f]{24}$")
LOCAL_OWNER = "local"
RESERVED_NAMES = frozenset(CANONICAL_TTS_VOICES) | frozenset(LEGACY_TTS_VOICE_ALIASES)
CONSENT_MODES = ("optional", "required")


class VoiceError(Exception):
    """A request the voice API refuses. ``code`` is stable; ``status`` is the HTTP status."""

    def __init__(self, status: int, code: str, message: str, *, param: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param


@dataclass(frozen=True)
class VoiceLimits:
    max_voices: int = 20
    max_consents: int = 50
    max_bytes: int = 200 * 1024 * 1024
    consent_mode: str = "optional"
    max_labels: int = 10


def is_voice_id(value: Any) -> bool:
    return isinstance(value, str) and VOICE_ID_RE.match(value) is not None


def owner_dir_name(owner_key: str | None) -> str:
    if not owner_key:
        return LOCAL_OWNER
    return hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:32]


def validate_name(value: Any, *, param: str = "name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise VoiceError(400, "invalid_name", "name must be a non-empty string.", param=param)
    name = value.strip()
    if len(name) > 256 or any(ord(ch) < 32 for ch in name):
        raise VoiceError(400, "invalid_name", "name must be 1-256 printable characters.", param=param)
    if name.lower() in RESERVED_NAMES:
        raise VoiceError(400, "voice_name_reserved", f"'{name}' is the name of a built-in voice; choose another name.", param=param)
    return name


def validate_labels(value: Any, limits: VoiceLimits) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > limits.max_labels:
        raise VoiceError(400, "invalid_labels", f"labels must be an object with at most {limits.max_labels} entries.", param="labels")
    labels: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not (1 <= len(key) <= 40) or len(item) > 100:
            raise VoiceError(400, "invalid_labels", "labels keys are 1-40 characters and values are strings of at most 100.", param="labels")
        labels[key] = item
    return labels


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


class VoiceStore:
    def __init__(self, root: Path | str, limits: VoiceLimits | None = None):
        self.root = Path(root)
        self.limits = limits or VoiceLimits()
        self._lock = threading.Lock()

    # ---- paths -------------------------------------------------------------
    def _owner(self, owner_key: str | None) -> Path:
        return self.root / owner_dir_name(owner_key)

    def _voices(self, owner_key: str | None) -> Path:
        return self._owner(owner_key) / "voices"

    def _consents(self, owner_key: str | None) -> Path:
        return self._owner(owner_key) / "consents"

    def reference_path(self, owner_key: str | None, voice_id: str) -> Path:
        return self._voices(owner_key) / voice_id / "reference.wav"

    # ---- generic record helpers -------------------------------------------
    def _load(self, base: Path, record_id: str, id_re: re.Pattern[str], what: str) -> dict[str, Any]:
        if not id_re.match(record_id or ""):
            raise VoiceError(404, f"{what}_not_found", f"The {what.replace('_', ' ')} '{record_id}' was not found.")
        meta = _read_json(base / record_id / "meta.json")
        if meta is None:
            raise VoiceError(404, f"{what}_not_found", f"The {what.replace('_', ' ')} '{record_id}' was not found.")
        return meta

    def _list(self, base: Path, after: str | None, limit: int, id_re: re.Pattern[str], what: str) -> tuple[list[dict[str, Any]], bool]:
        records: list[dict[str, Any]] = []
        if base.is_dir():
            for entry in base.iterdir():
                meta = _read_json(entry / "meta.json") if id_re.match(entry.name) else None
                if meta is not None:
                    records.append(meta)
        records.sort(key=lambda m: (m.get("created_at", 0), m.get("id", "")), reverse=True)
        if after:
            ids = [m["id"] for m in records]
            if after not in ids:
                raise VoiceError(404, f"{what}_not_found", f"The cursor '{after}' was not found.", param="after")
            records = records[ids.index(after) + 1 :]
        return records[:limit], len(records) > limit

    def _commit(self, base: Path, record_id: str, files: dict[str, bytes], meta: dict[str, Any]) -> None:
        base.mkdir(parents=True, exist_ok=True)
        staging = base / f".{record_id}.new"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        try:
            for name, content in files.items():
                (staging / name).write_bytes(content)
            _write_json(staging / "meta.json", meta)
            staging.rename(base / record_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _check_bytes(self, owner_key: str | None, extra: int) -> None:
        if _dir_bytes(self._owner(owner_key)) + extra > self.limits.max_bytes:
            raise VoiceError(400, "voice_quota_exceeded", "This key has used its voice storage quota; delete a voice or consent first.")

    # ---- consents ----------------------------------------------------------
    def create_consent(self, owner_key: str | None, *, name: str, language: str, recording: bytes, extension: str) -> dict[str, Any]:
        with self._lock:
            records, _ = self._list(self._consents(owner_key), None, self.limits.max_consents + 1, CONSENT_ID_RE, "consent")
            if len(records) >= self.limits.max_consents:
                raise VoiceError(400, "voice_quota_exceeded", f"At most {self.limits.max_consents} consent recordings can be stored per key.")
            self._check_bytes(owner_key, len(recording))
            consent_id = f"cons_{secrets.token_hex(12)}"
            meta = {"object": "audio.voice_consent", "id": consent_id, "name": name, "language": language, "created_at": int(time.time())}
            self._commit(self._consents(owner_key), consent_id, {f"recording.{extension}": recording}, meta)
            return meta

    def get_consent(self, owner_key: str | None, consent_id: str) -> dict[str, Any]:
        return self._load(self._consents(owner_key), consent_id, CONSENT_ID_RE, "consent")

    def list_consents(self, owner_key: str | None, *, after: str | None, limit: int) -> tuple[list[dict[str, Any]], bool]:
        return self._list(self._consents(owner_key), after, limit, CONSENT_ID_RE, "consent")

    def rename_consent(self, owner_key: str | None, consent_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            meta = self.get_consent(owner_key, consent_id)
            meta["name"] = name
            _write_json(self._consents(owner_key) / consent_id / "meta.json", meta)
            return meta

    def delete_consent(self, owner_key: str | None, consent_id: str) -> None:
        with self._lock:
            self.get_consent(owner_key, consent_id)
            shutil.rmtree(self._consents(owner_key) / consent_id)

    # ---- voices ------------------------------------------------------------
    def create_voice(
        self,
        owner_key: str | None,
        *,
        name: str,
        voice_type: str,
        reference_wav: bytes,
        duration_seconds: float,
        original: bytes | None,
        original_extension: str,
        consent_id: str | None,
        description: str | None,
        labels: dict[str, str],
        measurements: dict[str, Any],
        warnings: Iterable[str],
        seed: int | None = None,
        prompt: str | None = None,
        transcript: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.limits.consent_mode == "required" and voice_type == "audio_sample" and not consent_id:
                raise VoiceError(400, "consent_required", "This gateway requires a consent recording id (consent) to create a voice from an audio sample.", param="consent")
            if consent_id is not None:
                if not CONSENT_ID_RE.match(consent_id):
                    raise VoiceError(400, "invalid_consent", f"consent '{consent_id}' is not a consent recording id.", param="consent")
                self.get_consent(owner_key, consent_id)  # 404 unless it is this key's
            records, _ = self._list(self._voices(owner_key), None, self.limits.max_voices + 1, VOICE_ID_RE, "voice")
            if len(records) >= self.limits.max_voices:
                raise VoiceError(400, "voice_quota_exceeded", f"At most {self.limits.max_voices} voices can be stored per key; delete one first.")
            self._check_bytes(owner_key, len(reference_wav) + len(original or b""))
            voice_id = f"voice_{secrets.token_hex(12)}"
            meta = {
                "object": "audio.voice",
                "id": voice_id,
                "type": voice_type,
                "name": name,
                "created_at": int(time.time()),
                "description": description,
                "labels": labels,
                "duration_seconds": round(duration_seconds, 2),
                "consent": consent_id,
                "seed": seed,
                "prompt": prompt,
                "transcript": transcript,
                "measurements": measurements,
                "warnings": list(warnings),
                "quality": None,
                "preprocessing": "voice_audio-1",
            }
            files = {"reference.wav": reference_wav}
            if original is not None:
                files[f"original.{original_extension}"] = original
            self._commit(self._voices(owner_key), voice_id, files, meta)
            return meta

    def get_voice(self, owner_key: str | None, voice_id: str) -> dict[str, Any]:
        return self._load(self._voices(owner_key), voice_id, VOICE_ID_RE, "voice")

    def list_voices(self, owner_key: str | None, *, after: str | None = None, limit: int = 20) -> tuple[list[dict[str, Any]], bool]:
        return self._list(self._voices(owner_key), after, limit, VOICE_ID_RE, "voice")

    def update_voice(self, owner_key: str | None, voice_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            meta = self.get_voice(owner_key, voice_id)
            meta.update(changes)
            _write_json(self._voices(owner_key) / voice_id / "meta.json", meta)
            return meta

    def delete_voice(self, owner_key: str | None, voice_id: str) -> None:
        with self._lock:
            self.get_voice(owner_key, voice_id)
            shutil.rmtree(self._voices(owner_key) / voice_id)

    def usage(self, owner_key: str | None) -> dict[str, int]:
        voices, _ = self._list(self._voices(owner_key), None, 10_000, VOICE_ID_RE, "voice")
        consents, _ = self._list(self._consents(owner_key), None, 10_000, CONSENT_ID_RE, "consent")
        return {"voices": len(voices), "consents": len(consents), "bytes": _dir_bytes(self._owner(owner_key))}

    # ---- lifecycle ---------------------------------------------------------
    def purge_owner(self, owner_key: str | None) -> bool:
        """Delete everything a key owns. Called when its credential is revoked."""
        with self._lock:
            target = self._owner(owner_key)
            if not target.is_dir():
                return False
            shutil.rmtree(target)
            return True

    def purge_orphans(self, active_owner_keys: Iterable[str | None]) -> list[str]:
        """Delete owner directories whose key is not in ``active_owner_keys``. Never run automatically."""
        keep = {owner_dir_name(key) for key in active_owner_keys}
        removed: list[str] = []
        with self._lock:
            if self.root.is_dir():
                for entry in sorted(self.root.iterdir()):
                    if entry.is_dir() and entry.name not in keep:
                        shutil.rmtree(entry)
                        removed.append(entry.name)
        return removed
