"""Pure content policy from OWNER_VISIBILITY v3, section 3.1.

Roles grant an operational view, never content. Callers supply trusted identity
and resolved share rows; this module performs no storage reads or side effects.
Step 1 callers retain explicitly marked legacy exceptions until step 3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from gateway.public_failure import PublicFailure

Kind = Literal["creation", "prompt", "chat", "voice", "receipt", "job"]
Action = Literal["read", "delete", "reuse", "share", "link", "cancel"]


@dataclass(frozen=True)
class Viewer:
    person_id: str | None = None
    app_id: str | None = None
    key_hash: str | None = None  # appless credentials only
    owner_keys: frozenset[str] = frozenset()
    is_owner: bool = False


@dataclass(frozen=True)
class Share:
    share_id: str
    asset_id: str
    maker_person_id: str
    recipient_person_id: str
    include_prompt: bool = False
    poster_asset_id: str | None = None
    active: bool = True


@dataclass(frozen=True)
class CreationRecord:
    asset_id: str
    owner_keys: frozenset[str] = frozenset()
    maker_person_ids: tuple[str, ...] = ()
    app_id: str | None = None
    shares: tuple[Share, ...] = ()
    available: bool = True
    kind: Literal["creation"] = "creation"


@dataclass(frozen=True)
class PromptRecord:
    asset_id: str
    person_id: str | None
    shares: tuple[Share, ...] = ()
    available: bool = True
    kind: Literal["prompt"] = "prompt"


@dataclass(frozen=True)
class ChatRecord:
    person_id: str | None
    app_id: str | None = None
    owner_key_hash: str | None = None
    available: bool = True
    kind: Literal["chat"] = "chat"


@dataclass(frozen=True)
class VoiceRecord:
    owner_keys: frozenset[str] = frozenset()
    available: bool = True
    kind: Literal["voice"] = "voice"


@dataclass(frozen=True)
class ReceiptRecord:
    owner_keys: frozenset[str] = frozenset()
    person_id: str | None = None
    app_id: str | None = None
    request_id: str | None = None
    available: bool = True
    kind: Literal["receipt"] = "receipt"


@dataclass(frozen=True)
class JobRecord:
    owner_keys: frozenset[str] = frozenset()
    app_id: str | None = None
    job_id: str | None = None
    available: bool = True
    kind: Literal["job"] = "job"


ContentRecord: TypeAlias = CreationRecord | PromptRecord | ChatRecord | VoiceRecord | ReceiptRecord | JobRecord


@dataclass(frozen=True)
class Own:
    pass


@dataclass(frozen=True)
class Shared:
    share_id: str
    include_prompt: bool


@dataclass(frozen=True)
class Deny:
    pass


Access: TypeAlias = Own | Shared | Deny
OWN = Own()
DENY = Deny()


def _shared(viewer: Viewer, record: CreationRecord | PromptRecord) -> Shared | Deny:
    for share in record.shares:
        if (share.active and share.share_id and share.maker_person_id
                and viewer.person_id and share.recipient_person_id == viewer.person_id):
            if isinstance(record, CreationRecord):
                matches = record.asset_id in (share.asset_id, share.poster_asset_id)
            else:
                # Prompt authority is the share's maker row, never a sibling row
                # or the poster's prompt. The stored prompt is read live by callers.
                matches = (record.asset_id == share.asset_id and share.include_prompt
                           and record.person_id == share.maker_person_id)
            if matches:
                return Shared(share.share_id, share.include_prompt)
    return DENY


def decide(viewer: Viewer, record: ContentRecord, action: Action) -> Access:
    if not record.available:
        return DENY
    if isinstance(record, (CreationRecord, VoiceRecord, ReceiptRecord, JobRecord)):
        own = bool((viewer.owner_keys - {""}) & (record.owner_keys - {""}))
        if isinstance(record, CreationRecord):
            if own and action in {"read", "delete", "reuse", "link"}:
                return OWN
            if (own and action == "share" and viewer.person_id
                    and record.maker_person_ids == (viewer.person_id,)):
                return OWN
            if action in {"read", "link"}:
                return _shared(viewer, record)
        elif isinstance(record, VoiceRecord):
            if own and action in {"read", "delete", "reuse"}:
                return OWN
        elif isinstance(record, ReceiptRecord):
            if own and action == "read":
                return OWN
        elif own and action in {"read", "cancel"}:
            return OWN
    elif isinstance(record, (PromptRecord, ChatRecord)):
        self = bool(viewer.person_id and record.person_id == viewer.person_id)
        if isinstance(record, PromptRecord):
            if self and action in {"read", "delete", "reuse"}:
                return OWN
            if action == "read":
                return _shared(viewer, record)
        elif self and action in {"read", "delete", "reuse"}:
            if viewer.app_id:
                same_scope = record.app_id == viewer.app_id
            else:
                same_scope = bool(viewer.key_hash and not record.app_id
                                  and record.owner_key_hash == viewer.key_hash)
            if same_scope:
                return OWN
    return DENY


def require(viewer: Viewer, record: ContentRecord, action: Action) -> Access:
    access = decide(viewer, record, action)
    if isinstance(access, Deny):
        raise PublicFailure("not_found", 404)
    return access


def operational(viewer: Viewer) -> bool:
    return viewer.is_owner
