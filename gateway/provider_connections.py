"""Connection contracts and static implementations selected by provider rows."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from gateway.subscription_kinds import SubscriptionKind
    from gateway.subscription_store import Attempt, SubscriptionStore
    from gateway.session_guard import BrowserSession


class SubscriptionError(Exception):
    """A stable refusal containing only gateway-authored text."""
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True)
class Credential:
    kind: str
    value: Any = field(repr=False)
    connection_id: str | None = None
    generation: int | None = None


@dataclass(frozen=True)
class ConnectionFacts:
    metadata: Mapping[str, Any]
    credential: Credential
    secret_state: Mapping[str, Any] | None = field(default=None, repr=False)
    origin: str = ""


@dataclass(frozen=True)
class ConnectStart:
    owner_action: Mapping[str, Any]
    state: str
    mechanism_state: Mapping[str, Any] = field(repr=False)
    ttl: int = 600


@dataclass(frozen=True)
class ConnectionContext:
    kind: SubscriptionKind
    store: SubscriptionStore
    client_context: Callable
    clock: Callable[[], float]
    lock: asyncio.Lock
    runtime: dict[str, Any]
    session: BrowserSession | None = None
    rejected: Credential | None = None


class ConnectMechanism(Protocol):
    def validate(self, row: Mapping[str, Any], *, source: Any) -> None: ...
    async def start(self, ctx: ConnectionContext, attempt: Attempt) -> ConnectStart | None: ...
    async def finish(self, ctx: ConnectionContext, attempt: Attempt, values: Any) -> ConnectionFacts: ...
    async def acquire(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Credential: ...
    def status(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def configured(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> bool: ...
    async def disconnect(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallbackMechanism(Protocol):
    """Optional local-listener capability; ordinary mechanisms need not implement it."""
    async def listen_for_callback(self, ctx: ConnectionContext, finish: Callable, **options: Any) -> Any: ...


CONNECT_MECHANISMS: dict[str, ConnectMechanism] = {}

# The implementation registers one instance after its class is defined. This
# static import also permits importing the implementation before this module.
from gateway.connections import oauth_pkce as _oauth_pkce
from gateway.connections import cli_sign_in as _cli_sign_in


def connection_service(*, db_path):
    from gateway.subscription_kinds import load_kinds
    from gateway.subscription_oauth import SubscriptionService
    from gateway.subscription_store import SubscriptionStore, store_path
    return SubscriptionService(SubscriptionStore(lambda: store_path(db_path), secrets_path=lambda: db_path), load_kinds())


def configured_source(config: Mapping[str, Any], *, db_path) -> bool:
    source = config.get("credential_source") or {"kind": "static-key"}
    if source.get("kind") not in {"connection", "cli-session"}:
        return False
    return connection_service(db_path=db_path).configured(str(source.get("connection_id") or ""))


def _kinds() -> Mapping[str, Any]:
    from gateway.subscription_kinds import load_kinds
    return load_kinds()


def uses_connection(config: Mapping[str, Any]) -> bool:
    """True when the service's credential is a connection (a sign-in), not a stored key."""
    source = config.get("credential_source") or {}
    return source.get("kind") in {"connection", "cli-session"}


def connection_status(config: Mapping[str, Any], *, db_path) -> str:
    """The connection's own status (connected, needs_reconnect, disconnected) or ``not_connected``; reads, never creates."""
    from gateway.subscription_store import SubscriptionStore, store_path
    source = config.get("credential_source") or {}
    row = SubscriptionStore(lambda: store_path(db_path), secrets_path=lambda: db_path).connection(str(source.get("connection_id") or ""))
    return str(row["status"]) if row else "not_connected"


def plan_billed(config: Mapping[str, Any]) -> bool:
    """True when requests on this service are included in a subscription, not billed per token (kind data)."""
    from gateway.subscription_kinds import BillingMode
    return uses_connection(config) and config.get("billing_mode") == BillingMode.PLAN.value


def plan_label(config: Mapping[str, Any]) -> str | None:
    """The subscription's display name, from the kind data; None when no kind is known."""
    kind = _kinds().get(str(config.get("provider_id") or ""))
    return kind.display_name if kind is not None else None


def connection_labels(db_path) -> dict[str, str]:
    """Connection id -> the subscription kind's display name, for naming a connection to people (never its id)."""
    from gateway.subscription_store import SubscriptionStore, store_path
    store = SubscriptionStore(lambda: store_path(db_path), secrets_path=lambda: db_path)
    return {store.connection_id(kind.id): kind.display_name for kind in _kinds().values()}


def plan_connection_labels(access_plan, db_path) -> dict[str, str]:
    """Labels for the subscriptions an access plan grants; empty (no store read) when it grants none."""
    return connection_labels(db_path) if access_plan.connections else {}
