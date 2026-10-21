"""Pure content policy from OWNER_VISIBILITY v3, section 3.1.

Roles grant an operational view, never content. Callers supply trusted identity
and resolved share rows; this module performs no storage reads or side effects.
Step 1 callers retain explicitly marked legacy exceptions until step 3.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING, TypeAlias

from gateway.public_failure import PublicFailure

if TYPE_CHECKING:
    from gateway.owner_projection import JobView, ProjectionNames, ReceiptView, StorageView

Kind = Literal["creation", "prompt", "chat", "voice", "receipt", "job", "request_history", "debug_capture"]
Action = Literal["read", "delete", "reuse", "share", "link", "cancel"]


@dataclass(frozen=True)
class Viewer:
    person_id: str | None = None
    app_id: str | None = None
    key_hash: str | None = None  # appless credentials only
    owner_keys: frozenset[str] = frozenset()
    is_owner: bool = False
    privacy_permission: bool = False
    delegated: bool = False  # App keys and MCP grants; sessions are not delegated


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
    kind: Literal["creation"] = field(default="creation", init=False)


@dataclass(frozen=True)
class PromptRecord:
    asset_id: str
    person_id: str | None
    shares: tuple[Share, ...] = ()
    app_id: str | None = None  # trusted creator App, not current accountability
    available: bool = True
    kind: Literal["prompt"] = field(default="prompt", init=False)


@dataclass(frozen=True)
class ChatRecord:
    person_id: str | None
    app_id: str | None = None
    owner_key_hash: str | None = None
    available: bool = True
    kind: Literal["chat"] = field(default="chat", init=False)


@dataclass(frozen=True)
class VoiceRecord:
    owner_keys: frozenset[str] = frozenset()
    available: bool = True
    kind: Literal["voice"] = field(default="voice", init=False)


@dataclass(frozen=True)
class ReceiptRecord:
    owner_keys: frozenset[str] = frozenset()
    person_id: str | None = None
    app_id: str | None = None
    request_id: str | None = None
    available: bool = True
    kind: Literal["receipt"] = field(default="receipt", init=False)


@dataclass(frozen=True)
class JobRecord:
    owner_keys: frozenset[str] = frozenset()
    app_id: str | None = None
    job_id: str | None = None
    available: bool = True
    kind: Literal["job"] = field(default="job", init=False)


@dataclass(frozen=True)
class RequestHistoryRecord:
    person_id: str | None
    app_id: str | None = None
    available: bool = True
    kind: Literal["request_history"] = field(default="request_history", init=False)


@dataclass(frozen=True)
class DebugCaptureRecord:
    person_id: str | None
    app_id: str | None = None
    available: bool = True
    kind: Literal["debug_capture"] = field(default="debug_capture", init=False)


ContentRecord: TypeAlias = CreationRecord | PromptRecord | ChatRecord | VoiceRecord | ReceiptRecord | JobRecord | RequestHistoryRecord | DebugCaptureRecord


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


def _maker_access(
    viewer: Viewer, record: PromptRecord | ChatRecord | RequestHistoryRecord | DebugCaptureRecord,
) -> bool:
    same_app = bool(viewer.app_id and record.app_id == viewer.app_id)
    if record.person_id is None:
        # An App-only row is never any person's content, including the App's
        # accountable person. A privacy grant cannot turn it into Self.
        return viewer.person_id is None and same_app
    if not viewer.person_id or record.person_id != viewer.person_id:
        return False
    return not viewer.delegated or same_app or viewer.privacy_permission


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
    elif isinstance(record, (PromptRecord, ChatRecord, RequestHistoryRecord, DebugCaptureRecord)):
        allowed_actions = {"read", "delete"} if isinstance(record, DebugCaptureRecord) else {"read", "delete", "reuse"}
        if action in allowed_actions and _maker_access(viewer, record):
            return OWN
        if (isinstance(record, PromptRecord) and action == "read"
                and (not viewer.delegated or viewer.privacy_permission)):
            return _shared(viewer, record)
    return DENY


def require(viewer: Viewer, record: ContentRecord, action: Action) -> Access:
    access = decide(viewer, record, action)
    if isinstance(access, Deny):
        raise PublicFailure("not_found", 404)
    return access


def operational(viewer: Viewer) -> bool:
    return viewer.is_owner


def project(
    kind: Literal["receipt", "job", "storage"], record: Mapping[str, Any], *,
    names: ProjectionNames | None = None,
) -> ReceiptView | JobView | StorageView:
    """Unserved operational projection; content access and serialization are separate."""
    from gateway.owner_projection import ProjectionNames, project as project_record
    return project_record(kind, record, names=names if names is not None else ProjectionNames())
