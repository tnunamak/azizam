"""Request-protocol contracts and static implementations selected by provider data."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    import httpx
    from gateway.provider_connections import Credential
    from gateway.subscription_kinds import Refusal, SubscriptionKind


class RequestError(Exception):
    def __init__(self, code: str, message: str, *, param: str | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.param, self.status = code, message, param, status


class DiscoveryError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ReplyDecoder(Protocol):
    @property
    def usage(self) -> Mapping[str, Any] | None: ...
    @property
    def terminal(self) -> bool: ...
    @property
    def successful(self) -> bool: ...
    @property
    def has_response(self) -> bool: ...
    committed: bool
    event_seen: bool
    def quota_rejected(self, kind: SubscriptionKind) -> bool: ...
    translated: bool
    done: bytes
    def feed(self, chunk: bytes) -> list[bytes]: ...
    def failure(self, kind: SubscriptionKind, *, streaming: bool) -> Refusal: ...
    def error_event(self, refusal: Refusal) -> bytes: ...
    def result(self) -> Mapping[str, Any]: ...


class RequestProtocol(Protocol):
    def validate(self, row: Mapping[str, Any], *, source: Any) -> None: ...
    def check_request(self, surface: str, body: Mapping[str, Any], kind: SubscriptionKind, slug: str) -> dict[str, Any]: ...
    def apply_model_facts(self, surface: str, payload: dict[str, Any], advertised: list[str],
                          body: Mapping[str, Any], *, top_effort: str | None = None) -> Mapping[str, Any]: ...
    def prepare(self, payload: Mapping[str, Any], kind: SubscriptionKind, namespace: str, slug: str, *,
                caller: str = "") -> dict[str, Any]: ...
    async def send(self, kind: SubscriptionKind, payload: Mapping[str, Any], credential: Credential,
                   new_client: Callable, extra_headers: Mapping[str, str] | None = None
                   ) -> tuple[httpx.AsyncClient, httpx.Response]: ...
    def turn_state_header(self, kind: SubscriptionKind) -> str | None: ...
    def continues_turn(self, items: list[Any], start: int) -> bool: ...
    def quota_rejection(self, kind: SubscriptionKind, status: int, raw: bytes) -> bool: ...
    def classify_error(self, kind: SubscriptionKind, status: int, raw: bytes) -> Refusal: ...
    async def discover_models(self, kind: SubscriptionKind, credential: Credential, client_context: Callable) -> dict[str, list[str]]: ...
    def open_reply(self, surface: str, model: str, body: Mapping[str, Any]) -> ReplyDecoder: ...
    def usage_facts(self, usage: Mapping[str, Any]) -> dict[str, int]: ...


from gateway.protocols import responses_plan

REQUEST_PROTOCOLS: dict[str, RequestProtocol] = {"openai-responses-plan": responses_plan}

# A protocol kept as an instance registers itself after its class is defined (as connection mechanisms do). This
# static import also permits importing that module before this one.
from gateway.protocols import claude_code_runner as _claude_code_runner  # noqa: E402


@dataclass
class AttemptResult:
    """Transport completion facts. Only the dispatcher may advance a route."""

    response: Any
    sent: bool = False
    committed: bool = False
    advance: bool = False
    safe_error_code: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)   # receipt facts true only if this attempt serves the client
