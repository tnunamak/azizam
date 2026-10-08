"""Portable product response contracts, also used to generate browser types.

These describe caller-visible data. They do not import the running gateway or
its stores; generation and contract validation need no instance credentials.
"""
from typing import Literal
from typing_extensions import NotRequired, TypedDict

AvailabilityStatus = Literal["ready", "idle", "starting", "busy", "unknown", "unavailable"]
CapabilityIconKey = Literal["chat", "image", "video", "music", "sfx", "speech", "transcribe", "decide", "embed", "rerank"]
SetupEvent = Literal["settings_copied", "external_client_confirmed", "receipt_viewed"]
# The `/v1/models` metadata vocabulary (gateway/model_metadata.py, docs/api/MODEL_METADATA.md).
ModelType = Literal["language", "embedding", "reranking", "image", "video", "audio", "decision"]
InputModality = Literal["text", "image", "audio", "video", "file"]
OutputModality = Literal["text", "image", "audio", "video", "embedding"]
ModelTag = Literal["tool-use", "reasoning", "vision", "file-input"]


class AvailabilityEvidence(TypedDict, total=False):
    source: str
    # When the check or request behind this evidence happened (a failed check
    # is observed at its attempt time). Absent when nothing was checked.
    observed_at_s: float
    last_attempt_at_s: float
    # Latest known success of this source; absent when none is known.
    last_success_at_s: float
    receipt_status: str
    kind: str


class Availability(TypedDict):
    available: bool
    status: AvailabilityStatus
    reason: NotRequired[str]
    reason_code: NotRequired[str]
    measured: bool
    enabled: bool
    configured: bool
    up: bool | None
    idle_startable: bool
    evidence: AvailabilityEvidence


class CatalogAvailability(TypedDict):
    available: bool
    status: AvailabilityStatus
    reason: NotRequired[str]
    reason_code: NotRequired[str]
    measured: bool
    enabled: NotRequired[bool]
    configured: NotRequired[bool]
    up: NotRequired[bool | None]
    idle_startable: NotRequired[bool]
    evidence: NotRequired[AvailabilityEvidence]


class CapabilitySummary(TypedDict):
    capability_title: NotRequired[str]
    icon_key: NotRequired[CapabilityIconKey]
    recommended_model: NotRequired[str]
    use_names: NotRequired[list[str]]
    summary: NotRequired[str]
    secondary_profile: NotRequired[bool]
    title: NotRequired[str]
    capability: NotRequired[str]
    use_name: NotRequired[str]
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
    reason_code: str


class HomeAlias(TypedDict):
    local: NotRequired[bool]
    id: str
    operation: str
    available: bool
    status: AvailabilityStatus
    reason_code: str


class StatusSummary(TypedDict):
    state: AvailabilityStatus
    reason_code: str
    affected_capability_ids: list[str]
    counts: dict[str, int]


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


class GatewayFacts(TypedDict):
    # Display name of the gateway owner; null when the owner has none.
    owner_name: str | None


class HomeProjection(TypedDict):
    gateway: GatewayFacts
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
    observed_external_client: NotRequired[SetupObservation]
    checklist_dismissed: NotRequired[SetupObservation]


class SetupStep(TypedDict):
    id: str
    done: bool
    optional: bool
    reason: str
    href: str


class SetupProgress(TypedDict):
    complete: bool
    dismissed: NotRequired[bool]
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


class MemberStatusAvailability(TypedDict):
    evidence: AvailabilityEvidence


class MemberStatusCapability(TypedDict):
    capability_title: NotRequired[str]
    icon_key: NotRequired[CapabilityIconKey]
    recommended_model: NotRequired[str]
    use_names: NotRequired[list[str]]
    summary: NotRequired[str]
    secondary_profile: NotRequired[bool]
    title: NotRequired[str]
    use_name: NotRequired[str]
    capability: str
    intent: str
    model: str | None
    workflow: str | None
    status: AvailabilityStatus
    available: bool
    reason_code: str
    measured: bool
    observed_at_s: NotRequired[float | None]
    availability: MemberStatusAvailability


class MemberStatusReceipt(TypedDict):
    request_id: str
    status: str | None
    intent: str | None
    created_at: float | None
    href: str


class MemberStatus(TypedDict):
    scope: Literal["member"]
    gateway: GatewayFacts
    status: StatusSummary
    capabilities: list[MemberStatusCapability]
    receipts: list[MemberStatusReceipt]
    help: str


class CatalogCapability(TypedDict):
    icon_key: NotRequired[CapabilityIconKey]
    capability: str
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


class CatalogSummary(TypedDict, total=False):
    typical_seconds: float
    cost_label: str
    price: object


class ModelArchitecture(TypedDict):
    input_modalities: list[InputModality]
    output_modalities: list[OutputModality]


class CatalogRow(TypedDict):
    capability: NotRequired[str]
    recommended: NotRequired[bool]
    capability_title: NotRequired[str]
    icon_key: NotRequired[CapabilityIconKey]
    recommended_model: NotRequired[str]
    use_names: NotRequired[list[str]]
    title: NotRequired[str]
    secondary_profile: NotRequired[bool]
    id: str
    object: Literal["model_catalog.alias", "model_catalog.model"]
    display_name: str
    name: str
    alias: bool
    operations: list[str]
    resolves_to: dict[str, str]
    description: str | None
    technical_description: NotRequired[str]
    capabilities: list[CatalogCapability]
    provider: str | None
    engine_label: str | None
    locality: Literal["local", "remote", "unknown"]
    cost: CatalogCost
    summary: NotRequired[CatalogSummary]
    timing: CatalogTiming
    availability: CatalogAvailability
    io: CatalogIO
    examples: list[CatalogExample]
    recent_receipts: list[dict[str, object]]
    use: CatalogUse
    # The same values `/v1/models` reports for this id, when it lists the id.
    type: NotRequired[ModelType]
    architecture: NotRequired[ModelArchitecture]
    max_tokens: NotRequired[int]
    tags: NotRequired[list[ModelTag]]


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
