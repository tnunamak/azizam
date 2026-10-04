"""Portable product response contracts, also used to generate browser types.

These describe caller-visible data. They do not import the running gateway or
its stores; generation and contract validation need no instance credentials.
"""
from typing import Literal
from typing_extensions import NotRequired, TypedDict

AvailabilityStatus = Literal["ready", "idle", "starting", "busy", "unknown", "unavailable"]
SetupEvent = Literal["settings_copied", "external_client_confirmed", "receipt_viewed"]


class AvailabilityEvidence(TypedDict, total=False):
    source: str
    observed_at_s: float
    receipt_status: str
    kind: str


class Availability(TypedDict):
    available: bool
    status: AvailabilityStatus
    reason: str
    measured: bool
    enabled: bool
    configured: bool
    up: bool | None
    idle_startable: bool
    evidence: AvailabilityEvidence


class CatalogAvailability(TypedDict):
    available: bool
    status: AvailabilityStatus
    reason: str
    measured: bool
    enabled: NotRequired[bool]
    configured: NotRequired[bool]
    up: NotRequired[bool | None]
    idle_startable: NotRequired[bool]
    evidence: NotRequired[AvailabilityEvidence]


class CapabilitySummary(TypedDict):
    intent: str
    input_modalities: list[str]
    output_modalities: list[str]
    execution: str
    streaming: bool
    exposure: str
    trust_tier: str
    resource_tier: str
    tags: list[str]
    available: bool
    caveats: list[str]
    availability: Availability
    model: NotRequired[str]
    workflow: NotRequired[str]
    backend_kind: NotRequired[str]
    cost_class: NotRequired[str]
    engine: NotRequired[str]
    latency_class: NotRequired[str]
    calibrated: NotRequired[bool]
    reason: NotRequired[str]
    limits: NotRequired[dict[str, object]]


class CapabilitiesResponse(TypedDict):
    schema_version: Literal[1]
    audience: str
    capabilities: list[CapabilitySummary]


class HomeCapability(CapabilitySummary):
    capability: str
    status: AvailabilityStatus
    reason: str


class HomeAlias(TypedDict):
    id: str
    operation: str
    available: bool
    status: AvailabilityStatus
    reason: str


class StatusSummary(TypedDict):
    state: AvailabilityStatus
    reason: str


class HomeReceiptSource(TypedDict):
    label: str
    kind: NotRequired[str | None]
    app_id: NotRequired[str | None]
    client_hint: NotRequired[str | None]


class HomeReceipt(TypedDict):
    request_id: str
    intent: NotRequired[str | None]
    path: NotRequired[str | None]
    requested_model: NotRequired[str | None]
    status: NotRequired[str | None]
    elapsed_ms: NotRequired[float | None]
    latency_ms: NotRequired[float | None]
    _stored_at: NotRequired[float | None]
    accepted_at_ms: NotRequired[float | None]
    source_label: NotRequired[str | None]
    source: NotRequired[HomeReceiptSource]


class HomeProjection(TypedDict):
    csrf_token: str
    public_base_url: str
    api_base_url: str
    capabilities: list[HomeCapability]
    aliases: list[HomeAlias]
    credential_aliases: dict[str, list[HomeAlias]]
    new_app_aliases: list[HomeAlias]
    status: StatusSummary
    receipts: list[HomeReceipt]
    creations: None


class SetupObservation(TypedDict):
    recorded: bool
    source: str
    observed_at_s: NotRequired[float | None]


class SetupEvidence(TypedDict):
    gateway_request: SetupObservation
    settings_copied: SetupObservation
    external_client_confirmed: SetupObservation
    receipt_viewed: SetupObservation


class SetupStep(TypedDict):
    id: str
    done: bool
    optional: bool
    reason: str
    href: str


class SetupProgress(TypedDict):
    complete: bool
    steps: list[SetupStep]
    evidence: SetupEvidence


class SetupEvidenceRequest(TypedDict):
    event: SetupEvent
    app_id: NotRequired[str]
    request_id: NotRequired[str]


class SetupEvidenceResponse(TypedDict):
    event: SetupEvent
    recorded: Literal[True]
    source: Literal["user_confirmation"]


class MemberStatusCapability(TypedDict):
    capability: str
    intent: str
    model: str | None
    workflow: str | None
    status: AvailabilityStatus
    available: bool
    reason: str
    measured: bool
    observed_at_s: float | None


class MemberStatusReceipt(TypedDict):
    request_id: str
    status: str | None
    intent: str | None
    created_at: float | None
    href: str


class MemberStatus(TypedDict):
    scope: Literal["member"]
    status: StatusSummary
    capabilities: list[MemberStatusCapability]
    receipts: list[MemberStatusReceipt]
    help: str


class CatalogCapability(TypedDict):
    intent: str
    label: str
    input_modalities: list[str]
    output_modalities: list[str]
    streaming: bool
    available: bool


class CatalogTiming(TypedDict):
    typical_seconds: float | None
    source: str | None
    sample_count: NotRequired[int]


class CatalogCost(TypedDict):
    kind: Literal["free", "paid", "unknown"]
    price: object


class CatalogIO(TypedDict):
    input: list[str]
    output: list[str]


class CatalogExample(TypedDict):
    kind: str
    body: dict[str, object] | None
    files: NotRequired[list[str]]


class CatalogUse(TypedDict):
    model: str
    path: str | None
    curl: str | None
    python: dict[str, str] | None
    javascript: dict[str, str] | None


class CatalogRow(TypedDict):
    id: str
    object: Literal["model_catalog.alias", "model_catalog.model"]
    display_name: str
    name: str
    alias: bool
    operations: list[str]
    resolves_to: dict[str, str]
    description: str | None
    capabilities: list[CatalogCapability]
    provider: str | None
    engine_label: str | None
    locality: Literal["local", "remote", "unknown"]
    cost: CatalogCost
    timing: CatalogTiming
    availability: CatalogAvailability
    io: CatalogIO
    examples: list[CatalogExample]
    recent_receipts: list[dict[str, object]]
    use: CatalogUse


class CatalogCounts(TypedDict):
    aliases: int
    models: int
    capabilities: int


class ModelsCatalog(TypedDict):
    schema_version: Literal[1]
    object: Literal["models.catalog"]
    aliases: list[CatalogRow]
    models: list[CatalogRow]
    counts: CatalogCounts
