"""Maker-scoped prompt retention for generated assets.

This is the only gateway store for media prompt text. Receipts, job metadata,
asset sidecars and diagnostics must never carry the text or a workflow graph.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store, media_jobs, media_metadata

DB_PATH = media_jobs.DB_PATH
NAMESPACE = "com.azizam.prompts"
RETENTION_SECONDS = {"never": None, "3_months": 90 * 86400, "1_year": 365 * 86400}
MEMBER_DEFAULTS = {"remember": True, "retention": "3_months"}
HOUSEHOLD_DEFAULTS = {"default_remember": True, "never_keep": False, "retention_cap": "1_year"}

_DDL = """
CREATE TABLE IF NOT EXISTS asset_prompts (
  asset_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  metadata_json TEXT,
  created_at INTEGER NOT NULL,
  kept_until INTEGER,
  PRIMARY KEY (asset_id, person_id)
);
CREATE INDEX IF NOT EXISTS asset_prompts_expiry ON asset_prompts(kept_until);
CREATE INDEX IF NOT EXISTS asset_prompts_person ON asset_prompts(person_id);
"""


@dataclass(frozen=True)
class Decision:
    person_id: str | None
    keep: bool
    reason: str
    kept_until: int | None = None


class PromptModeError(ValueError):
    pass


def requested_mode(body: Mapping[str, Any] | None, headers: Mapping[str, Any] | None = None) -> str | None:
    if headers:
        for key, value in headers.items():
            if str(key).lower() != "x-azizam-retain-prompt":
                continue
            normalized = str(value).strip().lower()
            if normalized in {"false", "0", "no", "off"}:
                return "forget"
            if normalized in {"true", "1", "yes", "on"}:
                break
            raise PromptModeError("X-Azizam-Retain-Prompt must be true or false")
    if not isinstance(body, Mapping):
        return None
    extensions = body.get("extensions")
    if extensions is None:
        return None
    if not isinstance(extensions, Mapping):
        raise PromptModeError("extensions must be an object")
    block = extensions.get(NAMESPACE)
    if block is None:
        return None
    if not isinstance(block, Mapping) or set(block) != {"mode"} or block.get("mode") != "forget":
        raise PromptModeError(f"extensions.{NAMESPACE} must be {{'mode':'forget'}}")
    return "forget"


def prompt_text(body: Mapping[str, Any] | None) -> str | None:
    """Extract the maker's original words, not an engine-rewritten prompt."""
    if not isinstance(body, Mapping):
        return None
    for field in ("prompt", "input", "tags"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    shots = body.get("shots")
    if isinstance(shots, list):
        texts = [shot["prompt"].strip() for shot in shots
                 if isinstance(shot, Mapping) and isinstance(shot.get("prompt"), str) and shot["prompt"].strip()]
        return "\n\n".join(texts) if texts else None
    return None


def capture_metadata(content: bytes, suffix: str) -> dict[str, str]:
    """Capture workflow data in the sole prompt record before store-time strip."""
    kind = media_metadata.media_kind(suffix)
    if kind == "png":
        return media_metadata.png_text_chunks(content)
    if kind in {"jpeg", "webp"}:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(content)) as image:
            exif = image.getexif()
            values: dict[str, str] = {}
            for key, label in ((0x010F, "workflow"), (0x0110, "prompt"), (0x010E, "azizam_provenance")):
                value = exif.get(key)
                if isinstance(value, str) and value:
                    values[label] = value.removeprefix(f"{label}:")
            return values
    if kind in {"video", "audio"}:
        return media_metadata.read_media_tags(content, suffix)
    return {}


def restore_metadata(content: bytes, suffix: str, values: Mapping[str, str]) -> bytes:
    """Build a maker-only full download from clean media plus one prompt row."""
    kind = media_metadata.media_kind(suffix)
    if not values:
        return content
    if kind == "png":
        return media_metadata.embed_png_text(content, values)
    if kind in {"video", "audio"}:
        return media_metadata.embed_media_tags(content, suffix, values)
    if kind in {"jpeg", "webp"}:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(content)) as image:
            out = BytesIO()
            image.save(out, format=image.format, exif=media_metadata.exif_bytes(values))
            return out.getvalue()
    return content


def decide(
    *, person_id: str | None, member: Mapping[str, Any] | None,
    household: Mapping[str, Any] | None, request_body: Mapping[str, Any] | None,
    request_headers: Mapping[str, Any] | None = None,
    responses_store: bool | None = None, now: int | None = None,
) -> Decision:
    """Resolve policy before any payload, media or Responses persistence."""
    mode = requested_mode(request_body, request_headers)
    policy = {**HOUSEHOLD_DEFAULTS, **(household or {})}
    if not person_id:
        return Decision(None, False, "no_person")
    if policy["never_keep"]:
        return Decision(person_id, False, "server_disabled")
    if mode == "forget" or responses_store is False:
        return Decision(person_id, False, "request_forget")
    preference = {**MEMBER_DEFAULTS, "remember": policy["default_remember"], **(member or {})}
    if not preference.get("remember", policy["default_remember"]):
        return Decision(person_id, False, "member_disabled")
    member_period = RETENTION_SECONDS.get(preference.get("retention", "never"))
    cap_period = RETENTION_SECONDS.get(policy.get("retention_cap", "never"))
    finite = [period for period in (member_period, cap_period) if period is not None]
    expiry = (int(time.time()) if now is None else now) + min(finite) if finite else None
    return Decision(person_id, True, "remembered", expiry)


def household_settings(*, admin_path: Path | None = None) -> dict[str, Any]:
    row = admin_store.get_operator_setting(
        "prompt_memory", path=admin_path or admin_store.DB_PATH,
    )
    return {**HOUSEHOLD_DEFAULTS, **(row.get("value") or {} if row else {})}


def person_for_app(app_id: str | None, *, admin_path: Path | None = None) -> str | None:
    if not app_id:
        return None
    app = admin_store.get_app(app_id, path=admin_path or admin_store.DB_PATH)
    if not app:
        return None
    person_id = app.get("owner_user_id")
    return str(person_id) if isinstance(person_id, str) and person_id else None


def decide_for_app(
    app_id: str | None, body: Mapping[str, Any] | None, *,
    admin_path: Path | None = None,
    request_headers: Mapping[str, Any] | None = None,
    responses_store: bool | None = None,
) -> Decision:
    path = admin_path or admin_store.DB_PATH
    person_id = person_for_app(app_id, admin_path=path)
    member = admin_store.get_prompt_memory_preference(person_id, path=path) if person_id else None
    return decide(
        person_id=person_id, member=member, household=household_settings(admin_path=path),
        request_body=body, request_headers=request_headers, responses_store=responses_store,
    )


def get_member(person_id: str, *, path: Path | None = None) -> dict[str, Any]:
    record = admin_store.get_prompt_memory_preference(person_id, path=path or admin_store.DB_PATH)
    if not record:
        return dict(MEMBER_DEFAULTS)
    return {
        "remember": bool(record.get("remember")),
        "retention": str(record.get("retention") or MEMBER_DEFAULTS["retention"]),
    }


def put_member(
    person_id: str,
    *,
    remember: bool,
    retention: str,
    path: Path | None = None,
) -> dict[str, Any]:
    return admin_store.put_prompt_memory_preference(
        person_id,
        remember=bool(remember),
        retention=retention,
        updated_by=person_id,
        path=path or admin_store.DB_PATH,
    )


def household_default(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {**HOUSEHOLD_DEFAULTS, **(value or {})}


def effective(member: Mapping[str, Any] | None, household: Mapping[str, Any] | None) -> dict[str, Any]:
    prefs = {**MEMBER_DEFAULTS, **(member or {})}
    policy = household_default(household)
    remember = bool(prefs.get("remember", True)) and not bool(policy.get("never_keep"))
    return {
        "remember": remember,
        "retention": _effective_retention(str(prefs.get("retention") or "3_months"), str(policy.get("retention_cap") or "1_year")),
    }


def _effective_retention(member_retention: str, cap_retention: str) -> str:
    finite = [value for value in (member_retention, cap_retention) if RETENTION_SECONDS.get(value) is not None]
    if not finite:
        return "never"
    return min(finite, key=lambda value: RETENTION_SECONDS[value] or 0)


def _connect(path: Path | None) -> sqlite3.Connection:
    target = Path(path or media_jobs.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5)
    conn.row_factory = sqlite3.Row
    media_jobs._apply_pragmas(conn)
    conn.executescript(_DDL)
    return conn


def init_db(*, path: Path | None = None) -> None:
    conn = _connect(path)
    conn.close()


def put(
    asset_id: str, person_id: str, prompt_text: str, *, kept_until: int | None,
    metadata: Mapping[str, str] | None = None, path: Path | None = None,
) -> None:
    if not asset_id or not person_id or not isinstance(prompt_text, str) or not prompt_text:
        raise ValueError("asset, maker and nonempty prompt text are required")
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT INTO asset_prompts(asset_id,person_id,prompt_text,metadata_json,created_at,kept_until) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id,person_id) DO UPDATE SET "
            "prompt_text=excluded.prompt_text,metadata_json=excluded.metadata_json,"
            "created_at=excluded.created_at,kept_until=excluded.kept_until",
            (asset_id, person_id, prompt_text,
             json.dumps(dict(metadata), ensure_ascii=False) if metadata else None,
             int(time.time()), kept_until),
        )
        conn.commit()
    finally:
        conn.close()


def get(asset_id: str, person_id: str | None, *, path: Path | None = None) -> dict[str, Any] | None:
    if not person_id:
        return None
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT asset_id,person_id,prompt_text,metadata_json,created_at,kept_until "
            "FROM asset_prompts WHERE asset_id=? AND person_id=? AND "
            "(kept_until IS NULL OR kept_until>?)",
            (asset_id, person_id, int(time.time())),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data
    finally:
        conn.close()


def delete_person(person_id: str, *, path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        count = conn.execute("DELETE FROM asset_prompts WHERE person_id=?", (person_id,)).rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def delete_asset(asset_id: str, *, path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        count = conn.execute("DELETE FROM asset_prompts WHERE asset_id=?", (asset_id,)).rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def move(source_id: str, asset_id: str, person_id: str, *, metadata: Mapping[str, str] | None = None, path: Path | None = None) -> bool:
    """Move an in-flight video prompt to its finished asset in one transaction."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT prompt_text,metadata_json,created_at,kept_until FROM asset_prompts "
            "WHERE asset_id=? AND person_id=?", (source_id, person_id),
        ).fetchone()
        if row is None:
            return False
        merged = dict(metadata) if metadata else json.loads(row["metadata_json"] or "{}")
        conn.execute(
            "INSERT OR REPLACE INTO asset_prompts(asset_id,person_id,prompt_text,metadata_json,created_at,kept_until) "
            "VALUES(?,?,?,?,?,?)",
            (asset_id, person_id, row["prompt_text"], json.dumps(merged, ensure_ascii=False), row["created_at"], row["kept_until"]),
        )
        conn.execute("DELETE FROM asset_prompts WHERE asset_id=? AND person_id=?", (source_id, person_id))
        conn.commit()
        return True
    finally:
        conn.close()


def prune(*, path: Path | None = None, now: int | None = None) -> int:
    conn = _connect(path)
    try:
        count = conn.execute(
            "DELETE FROM asset_prompts WHERE kept_until IS NOT NULL AND kept_until<=?",
            (int(time.time()) if now is None else now,),
        ).rowcount
        conn.commit()
        return count
    finally:
        conn.close()
