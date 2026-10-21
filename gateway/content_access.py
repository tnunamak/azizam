"""Pure content policy from OWNER_VISIBILITY v4.2 at c6968dc8, section 3.1.

Roles grant an operational view, never content. Callers supply trusted identity
and resolved share rows; the decision functions perform no storage reads or side effects.
Step 1 callers retain explicitly marked legacy exceptions until step 3.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING, TypeAlias

from gateway.public_failure import PublicFailure

if TYPE_CHECKING:
    from gateway.runtime_auth import RuntimeIdentity
    from proxy import AuthContext
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
    auth_kind: Literal["session", "app_key", "mcp_grant", "trusted_address", "service_key"] = field(kw_only=True)
    credential_id: str | None = None
    trusted_address: str | None = None
    connection_id: str | None = None  # trusted MCP grant, never the shared session App

    def __post_init__(self):
        if self.auth_kind not in {"session", "app_key", "mcp_grant", "trusted_address", "service_key"}:
            raise ValueError("unknown authentication kind")
        if self.auth_kind in {"trusted_address", "service_key"} and self.person_id is not None:
            raise ValueError("this authentication kind has no person")
        if self.auth_kind == "mcp_grant" and not self.connection_id:
            raise ValueError("an MCP grant needs a non-empty connection_id")
        object.__setattr__(self, "owner_keys", frozenset(self.owner_keys) - {""})


def viewer_from_auth(
    auth: AuthContext | RuntimeIdentity | None, *,
    owner_keys: Iterable[str] | None = None, path: Path | None = None,
) -> Viewer:
    """Normalize trusted authentication, preserving the caller's principal scope.

    Resolved scope keys may be supplied by a caller that already read aliases.
    Legacy hash-only contexts supply no person or credential-origin authority.
    Privacy permission is not issued in step 1. Missing identity adds no authority.
    """
    from gateway import ownership
    from gateway.runtime_auth import RuntimeIdentity
    identity = auth if isinstance(auth, RuntimeIdentity) else getattr(auth, "identity", None)
    if owner_keys is None:
        # ownership accepts the AuthContext shape, not a bare RuntimeIdentity.
        from types import SimpleNamespace
        owner_keys = ownership.readable_owner_keys(SimpleNamespace(identity=identity), path=path) if identity else ()
    return Viewer(
        person_id=getattr(identity, "person_id", None),
        app_id=getattr(identity, "app_id", None),
        key_hash=getattr(identity, "key_hash", None) if not getattr(identity, "app_id", None) else None,
        owner_keys=frozenset(owner_keys), auth_kind=getattr(identity, "auth_kind", "app_key"),
        credential_id=getattr(identity, "credential_id", None),
        trusted_address=getattr(identity, "trusted_address", None),
        connection_id=getattr(identity, "connection_id", None),
    )


@dataclass(frozen=True)
class ContentOrigin:
    """Trusted writer attribution; missing origin is legacy and never OwnOutput."""
    auth_kind: Literal["session", "app_key", "mcp_grant"]
    credential_id: str | None = None
    connection_id: str | None = None


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
    origin: ContentOrigin | None = None
    kind: Literal["prompt"] = field(default="prompt", init=False)


@dataclass(frozen=True)
class ChatRecord:
    person_id: str | None
    app_id: str | None = None
    owner_key_hash: str | None = None
    available: bool = True
    origin: ContentOrigin | None = None
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
    origin: ContentOrigin | None = None
    kind: Literal["request_history"] = field(default="request_history", init=False)


@dataclass(frozen=True)
class DebugCaptureRecord:
    person_id: str | None
    app_id: str | None = None
    available: bool = True
    origin: ContentOrigin | None = None
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


def _self(viewer: Viewer, record: PromptRecord | ChatRecord | RequestHistoryRecord | DebugCaptureRecord) -> bool:
    return bool(viewer.person_id and record.person_id == viewer.person_id)


def _content_read(viewer: Viewer, record: PromptRecord | ChatRecord | RequestHistoryRecord) -> bool:
    if record.person_id is None:
        return (viewer.person_id is None and viewer.auth_kind in {"app_key", "trusted_address", "service_key"}
                and bool(viewer.app_id) and record.app_id == viewer.app_id)
    if not _self(viewer, record):
        return False
    if viewer.auth_kind == "session" or viewer.privacy_permission:
        return True
    origin = record.origin
    if origin is None:
        return False
    if viewer.auth_kind == "app_key":
        return (origin.auth_kind == "app_key" and bool(origin.credential_id)
                and bool(viewer.app_id) and record.app_id == viewer.app_id)
    if viewer.auth_kind == "mcp_grant":
        return (origin.auth_kind == "mcp_grant" and bool(viewer.connection_id)
                and origin.connection_id == viewer.connection_id)
    return False


def decide_own(viewer: Viewer, principals: Iterable[str]) -> bool:
    """Principal-only Own check; no per-row resource or set allocation.

    Resource availability, actions and explicit-App precedence stay with callers.
    The Viewer's keys exclude the empty legacy alias at construction.
    """
    if isinstance(principals, str):
        raise TypeError("principals must be an iterable of strings, not a string")
    return not viewer.owner_keys.isdisjoint(principals)


def decide(viewer: Viewer, record: ContentRecord, action: Action) -> Access:
    if not record.available:
        return DENY
    if isinstance(record, (CreationRecord, VoiceRecord, ReceiptRecord, JobRecord)):
        own = decide_own(viewer, record.owner_keys)
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
    elif isinstance(record, PromptRecord):
        if action == "read":
            if _content_read(viewer, record):
                return OWN
            # D52: another person's included prompt is "elsewhere" for a
            # delegated credential, even when the creation was shared with it.
            if viewer.auth_kind == "session" or viewer.privacy_permission:
                return _shared(viewer, record)
        elif action == "delete" and _self(viewer, record):
            return OWN
        elif action == "reuse" and _self(viewer, record) and _content_read(viewer, record):
            return OWN
    elif isinstance(record, ChatRecord):
        if action == "read" and _content_read(viewer, record):
            return OWN
        if action in {"delete", "reuse"} and _self(viewer, record):
            same_scope = (bool(viewer.app_id) and record.app_id == viewer.app_id if record.app_id else
                          bool(viewer.key_hash) and record.owner_key_hash == viewer.key_hash)
            if same_scope:
                return OWN
    elif isinstance(record, RequestHistoryRecord):
        if action == "read" and _content_read(viewer, record):
            return OWN
        if (action in {"delete", "reuse"} and _self(viewer, record)
                and (viewer.auth_kind == "session" or viewer.privacy_permission)):
            return OWN
    elif isinstance(record, DebugCaptureRecord):
        if (action in {"read", "delete"} and _self(viewer, record)
                and (viewer.auth_kind == "session" or viewer.privacy_permission)):
            return OWN
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
