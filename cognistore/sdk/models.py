"""Typed, forward-compatible models for the public CogniStore HTTP contract.

These models are deliberately maintained independently from ``cognistore.api``.
The SDK can therefore be imported and used without importing server, catalog, or
storage-driver implementation details.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JsonValue
RetrievalSignal: TypeAlias = Literal["metadata", "keyword", "vector"]
PassageSignal: TypeAlias = Literal["keyword", "vector"]
RetrievalMode: TypeAlias = Literal[
    "metadata",
    "metadata+keyword",
    "metadata+vector",
    "metadata+keyword+vector",
]
JobState: TypeAlias = Literal[
    "queued",
    "running",
    "retrying",
    "succeeded",
    "failed",
]

Bucket = Annotated[str, Field(min_length=1, max_length=1024)]
ObjectKey = Annotated[str, Field(min_length=1, max_length=8192)]
Tier = Annotated[str, Field(min_length=1, max_length=256)]
PageLimit = Annotated[int, Field(ge=1, le=200)]
PolicyPattern = Annotated[str, Field(min_length=1, max_length=1024)]
MimePrefix = Annotated[str, Field(min_length=1, max_length=255)]
EmbeddingRuleName = Annotated[str, Field(min_length=1, max_length=256)]
EmbeddingRuleQuery = Annotated[str, Field(min_length=1, max_length=16_384)]
PIIFindingType: TypeAlias = Literal[
    "EMAIL_ADDRESS", "US_SSN", "PHONE_NUMBER", "CREDIT_CARD_NUMBER", "IP_ADDRESS",
    "PERSON", "LOCATION", "DATE_OF_BIRTH", "ACCOUNT_NUMBER", "TAX_ID",
    "PASSPORT_NUMBER", "DRIVER_LICENSE_NUMBER",
]
MAX_POLICY_CONFIG_BYTES = 64 * 1024
ImportanceLevel: TypeAlias = Literal["low", "normal", "high", "critical"]


class SDKRequest(BaseModel):
    """Strict request base so invalid calls fail before network I/O."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SDKResponse(BaseModel):
    """Response base that permits backward-compatible additions within API v1."""

    model_config = ConfigDict(extra="ignore", strict=True)


class AskFilters(SDKRequest):
    bucket: Bucket | None = None
    key_prefix: Annotated[str, Field(max_length=8192)] = ""
    tier: Tier | None = None
    mime: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    size: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    object_metadata: dict[str, JSONScalar] = Field(default_factory=dict)
    document_metadata: dict[str, JSONScalar] = Field(default_factory=dict)


class AskRequest(SDKRequest):
    text: Annotated[str, Field(min_length=1, max_length=16_384)]
    filters: AskFilters = Field(default_factory=AskFilters)
    limit: Annotated[int, Field(ge=1, le=100)] = 10
    candidate_limit: Annotated[int, Field(ge=1, le=1000)] = 100
    passages_per_result: Annotated[int, Field(ge=1, le=10)] = 3
    synthesize: bool = False
    exact_vector: bool = False
    retrieval_mode: RetrievalMode = "metadata+keyword+vector"


class PIIPolicyRuleConfig(SDKRequest):
    """Restrict content placement when a sanitized PII finding is present."""

    finding_type: PIIFindingType
    destination_tier: Tier
    minimum_confidence: Annotated[
        float, Field(ge=0.0, le=1.0, allow_inf_nan=False),
    ] = 0.5

    @model_validator(mode="after")
    def _validate_destination(self) -> PIIPolicyRuleConfig:
        value = self.destination_tier
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("PII rule destination_tier must not have whitespace or control characters")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("PII rule destination_tier must be valid UTF-8") from exc
        if len(encoded) > 256:
            raise ValueError("PII rule destination_tier must be at most 256 UTF-8 bytes")
        return self


class EmbeddingPolicyRuleConfig(SDKRequest):
    """One named semantic classification rule for content placement."""

    name: EmbeddingRuleName
    query: EmbeddingRuleQuery
    minimum_similarity: Annotated[
        float,
        Field(ge=-1.0, le=1.0, allow_inf_nan=False),
    ]
    destination_tier: Tier

    @model_validator(mode="after")
    def _validate_text_identity(self) -> EmbeddingPolicyRuleConfig:
        for field_name, byte_limit in (
            ("name", 256),
            ("query", 16_384),
            ("destination_tier", 256),
        ):
            value = getattr(self, field_name)
            if value != value.strip():
                raise ValueError(
                    f"embedding rule {field_name} must not have outer whitespace"
                )
            if "\0" in value or any(ord(character) < 32 for character in value):
                raise ValueError(
                    f"embedding rule {field_name} must not contain control characters"
                )
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"embedding rule {field_name} must be valid UTF-8"
                ) from exc
            if len(encoded) > byte_limit:
                raise ValueError(
                    f"embedding rule {field_name} must be at most "
                    f"{byte_limit} UTF-8 bytes"
                )
        return self


def _validate_control_text(value: str, name: str, maximum_bytes: int) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty text without outer whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if length > maximum_bytes:
        raise ValueError(f"{name} must be at most {maximum_bytes} UTF-8 bytes")


def _default_importance_tiers() -> dict[ImportanceLevel, list[str]]:
    return {"high": ["hot", "warm"], "critical": ["hot"]}


class StabilityOverrideConfig(SDKRequest):
    """An attributable exception to cooldown and numerical hysteresis."""

    kind: Literal["emergency", "compliance"]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def _validate_reason(self) -> StabilityOverrideConfig:
        _validate_control_text(self.reason, "override reason", 2048)
        return self


class MovementConstraintsConfig(SDKRequest):
    minimum_residency_seconds: dict[
        Tier, Annotated[int, Field(ge=0, le=315360000)]
    ] = Field(default_factory=dict)
    importance_tiers: dict[
        ImportanceLevel, Annotated[list[Tier], Field(max_length=32)]
    ] = Field(default_factory=_default_importance_tiers)
    cooldown_seconds: Annotated[int, Field(ge=0, le=315360000)] = 0
    size_hysteresis_bytes: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0
    similarity_hysteresis: Annotated[
        float, Field(ge=0, le=2, allow_inf_nan=False)
    ] = 0.0
    stability_override: StabilityOverrideConfig | None = None

    @model_validator(mode="after")
    def _validate_constraints(self) -> MovementConstraintsConfig:
        if len(self.minimum_residency_seconds) > 100:
            raise ValueError("minimum_residency_seconds supports at most 100 tiers")
        for tier in self.minimum_residency_seconds:
            _validate_control_text(tier, "tier", 256)
        for tiers in self.importance_tiers.values():
            if len(set(tiers)) != len(tiers):
                raise ValueError("importance_tiers must not contain duplicate tiers")
            for tier in tiers:
                _validate_control_text(tier, "importance tier", 256)
        return self


class PolicyConfig(SDKRequest):
    policy: Literal["simple", "content", "llm"] = "simple"
    movement_constraints: MovementConstraintsConfig | None = None
    threshold: Annotated[int, Field(ge=0, le=2**63 - 1)] = 1_048_576
    llm_threshold: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None
    allowed_tiers: Annotated[list[Tier], Field(min_length=1, max_length=32)] = Field(
        default_factory=lambda: ["hot", "warm"]
    )
    hot_name_patterns: Annotated[list[PolicyPattern], Field(max_length=100)] = Field(
        default_factory=list
    )
    warm_name_patterns: Annotated[list[PolicyPattern], Field(max_length=100)] = Field(
        default_factory=list
    )
    cold_name_patterns: Annotated[list[PolicyPattern], Field(max_length=100)] = Field(
        default_factory=list
    )
    hot_mime_prefixes: Annotated[list[MimePrefix], Field(max_length=100)] = Field(
        default_factory=list
    )
    warm_mime_prefixes: Annotated[list[MimePrefix], Field(max_length=100)] = Field(
        default_factory=list
    )
    cold_mime_prefixes: Annotated[list[MimePrefix], Field(max_length=100)] = Field(
        default_factory=list
    )
    embedding_rules: Annotated[
        list[EmbeddingPolicyRuleConfig],
        Field(max_length=100),
    ] = Field(default_factory=list)

    pii_rules: Annotated[
        list[PIIPolicyRuleConfig], Field(max_length=100),
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bound_aggregate_payload(self) -> PolicyConfig:
        if self.embedding_rules and self.policy != "content":
            raise ValueError("embedding rules require the content policy")
        if self.pii_rules and self.policy != "content":
            raise ValueError("PII rules require the content policy")
        disallowed_pii_tiers = sorted({
            rule.destination_tier for rule in self.pii_rules
            if rule.destination_tier not in self.allowed_tiers
        })
        if disallowed_pii_tiers:
            raise ValueError(
                "PII rule destination tier(s) must be allowed: " + ", ".join(disallowed_pii_tiers)
            )
        duplicate_names = sorted(
            name
            for name in {rule.name for rule in self.embedding_rules}
            if sum(rule.name == name for rule in self.embedding_rules) > 1
        )
        if duplicate_names:
            raise ValueError(
                "embedding rule names must be unique: "
                + ", ".join(duplicate_names)
            )
        unknown_destinations = sorted(
            {
                rule.destination_tier
                for rule in self.embedding_rules
                if rule.destination_tier not in self.allowed_tiers
            }
        )
        if unknown_destinations:
            raise ValueError(
                "embedding rule destination tier(s) must be allowed: "
                + ", ".join(unknown_destinations)
            )
        values = [
            *self.allowed_tiers,
            *(
                value for rule in self.pii_rules
                for value in (rule.finding_type, rule.destination_tier)
            ),
            *self.hot_name_patterns,
            *self.warm_name_patterns,
            *self.cold_name_patterns,
            *self.hot_mime_prefixes,
            *self.warm_mime_prefixes,
            *self.cold_mime_prefixes,
            *(
                value
                for rule in self.embedding_rules
                for value in (rule.name, rule.query, rule.destination_tier)
            ),
        ]
        try:
            payload_bytes = sum(len(value.encode("utf-8")) for value in values)
        except UnicodeEncodeError as exc:
            raise ValueError("policy strings must be valid UTF-8") from exc
        if payload_bytes > MAX_POLICY_CONFIG_BYTES:
            raise ValueError(
                f"policy strings must total at most {MAX_POLICY_CONFIG_BYTES} bytes"
            )
        return self


class PolicyEvaluationRequest(SDKRequest):
    bucket: Bucket
    key: ObjectKey
    config: PolicyConfig = Field(default_factory=PolicyConfig)


class ImportanceChangeRequest(SDKRequest):
    bucket: Bucket
    key: ObjectKey
    level: ImportanceLevel | None
    actor_id: Annotated[str, Field(min_length=1, max_length=256)]
    provenance: Annotated[str, Field(min_length=1, max_length=1024)]
    config: PolicyConfig = Field(default_factory=PolicyConfig)

    @model_validator(mode="after")
    def _validate_attribution(self) -> ImportanceChangeRequest:
        _validate_control_text(self.actor_id, "actor_id", 256)
        _validate_control_text(self.provenance, "provenance", 2048)
        return self


class CatalogScanRequest(SDKRequest):
    tier: Tier
    bucket: Bucket
    prefix: Annotated[str, Field(max_length=8192)] = ""


class PolicyRunRequest(SDKRequest):
    bucket: Bucket
    prefix: Annotated[str, Field(max_length=8192)] = ""
    config: PolicyConfig = Field(default_factory=PolicyConfig)


class HealthResponse(SDKResponse):
    status: Literal["ok"] = "ok"
    api_version: Literal[1] = 1


class ObjectResource(SDKResponse):
    schema_version: Literal[1] = 1
    tier: Tier
    bucket: Bucket
    key: ObjectKey
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    generation: str
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    metadata_truncated: bool = False


class CatalogObject(SDKResponse):
    schema_version: Literal[1] = 1
    bucket: Bucket
    key: ObjectKey
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    tier: Tier
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    metadata_truncated: bool = False


class PageMetadata(SDKResponse):
    limit: PageLimit
    next_cursor: str | None = None


class AuditEventResource(SDKResponse):
    event_id: str
    schema_version: int
    event_type: str
    outcome: str
    occurred_at: str
    recorded_at: str
    correlation_id: str
    actor_type: str
    actor_id: str
    expires_at: str | None = None
    causation_id: str | None = None
    bucket: str | None = None
    object_key: str | None = None
    job_id: str | None = None
    move_id: str | None = None
    policy_name: str | None = None
    policy_version: str | None = None
    details: dict[str, JSONValue]


class AuditEventPage(SDKResponse):
    schema_version: Literal[1] = 1
    items: list[AuditEventResource]
    page: PageMetadata


class AuditCheckpointResource(SDKResponse):
    tenant_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
    sequence: Annotated[int, Field(ge=0)]
    entry_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    algorithm: Literal["sha256-v1"] = "sha256-v1"


class AuditVerificationRequest(SDKRequest):
    checkpoint: AuditCheckpointResource | None = None


class AuditVerificationResponse(SDKResponse):
    valid: bool
    tenant_id: str
    checked_entries: int
    checked_events: int
    pruned_events: int
    checkpoint: AuditCheckpointResource
    anchored: bool
    issues: list[str]


class AuditExportResponse(SDKResponse):
    schema_version: Literal[1] = 1
    records: list[dict[str, JSONValue]]
    checkpoint: AuditCheckpointResource
    next_sequence: int
    complete: bool
    page: PageMetadata


class CatalogObjectPage(SDKResponse):
    schema_version: Literal[1] = 1
    items: list[CatalogObject]
    page: PageMetadata


class LegalHoldRequest(SDKRequest):
    """Protect an exact object, literal key prefix, or an entire bucket."""

    bucket: Bucket
    key: ObjectKey | None = None
    prefix: Annotated[str, Field(max_length=8192)] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def validate_scope(self) -> LegalHoldRequest:
        if self.key is not None and self.prefix is not None:
            raise ValueError("Specify key or prefix, never both")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


class LegalHoldReleaseRequest(SDKRequest):
    reason: Annotated[str, Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def validate_reason(self) -> LegalHoldReleaseRequest:
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


class LegalHoldResource(SDKResponse):
    schema_version: Literal[1] = 1
    hold_id: str
    tenant_id: str
    bucket: Bucket
    key: ObjectKey | None
    prefix: str | None
    reason: str
    created_at: str
    actor_type: str
    actor_id: str
    correlation_id: str
    active: bool
    released_at: str | None
    released_reason: str | None
    released_actor_type: str | None
    released_actor_id: str | None
    released_correlation_id: str | None


class LegalHoldList(SDKResponse):
    schema_version: Literal[1] = 1
    items: list[LegalHoldResource]


class ObjectCitation(SDKResponse):
    citation_id: str
    object_id: str
    bucket: Bucket
    key: ObjectKey
    tier: Tier
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    mime: str | None = None
    content_sha256: str | None = None
    object_metadata: dict[str, JSONValue]
    document_metadata: dict[str, JSONValue]
    object_metadata_truncated: bool
    document_metadata_truncated: bool


class PassageCitation(SDKResponse):
    citation_id: str
    object_id: str
    bucket: Bucket
    key: ObjectKey
    source: PassageSignal
    passage_id: str
    passage_index: Annotated[int, Field(ge=0)]
    start_codepoint: Annotated[int, Field(ge=0)]
    end_codepoint: Annotated[int, Field(ge=0)]
    text_sha256: str
    source_sha256: str
    document_text_sha256: str
    document_id: str | None = None
    space_id: str | None = None


class PassageMatch(SDKResponse):
    signal: PassageSignal
    rank: Annotated[int, Field(ge=1)]
    raw_score: float


class RetrievedPassage(SDKResponse):
    citation: PassageCitation
    text: str
    match: PassageMatch


class ScoreComponent(SDKResponse):
    signal: RetrievalSignal
    rank: Annotated[int, Field(ge=1)]
    raw_score: float
    weight: Annotated[float, Field(ge=0)]
    contribution: Annotated[float, Field(ge=0)]


class RetrievalResult(SDKResponse):
    citation: ObjectCitation
    score: Annotated[float, Field(ge=0)]
    score_components: list[ScoreComponent]
    passages: list[RetrievedPassage]


class ProviderDiagnostic(SDKResponse):
    component: Literal["metadata", "keyword", "vector", "generation"]
    state: Literal[
        "succeeded",
        "missing",
        "unavailable",
        "not_requested",
        "no_context",
    ]
    error_type: str | None = None


class GeneratedAnswer(SDKResponse):
    text: str
    citations: list[str]


class AskResponse(SDKResponse):
    schema_version: Literal[1]
    mode: RetrievalMode
    active_signals: list[RetrievalSignal]
    results: list[RetrievalResult]
    providers: list[ProviderDiagnostic]
    generation_status: Literal[
        "not_requested",
        "provider_missing",
        "no_context",
        "succeeded",
        "provider_unavailable",
    ]
    answer: GeneratedAnswer | None = None


PolicyFeatureState: TypeAlias = Literal[
    "fresh",
    "missing",
    "stale",
    "unavailable",
]


class PolicyFeatureProvenance(SDKResponse):
    source: str
    source_version: Annotated[int, Field(ge=1)]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    details: dict[str, JSONScalar]


class MimePolicyFeature(SDKResponse):
    state: PolicyFeatureState
    value: str | None
    provenance: PolicyFeatureProvenance


class PIIFinding(SDKResponse):
    type: PIIFindingType
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    provenance: Literal["regex", "rule", "ner", "classifier"]
    detector: str
    detector_version: str


class PIIPolicyFeature(SDKResponse):
    state: PolicyFeatureState
    findings: list[PIIFinding]
    provenance: PolicyFeatureProvenance


class EmbeddingPolicyFeature(SDKResponse):
    name: EmbeddingRuleName
    query: EmbeddingRuleQuery
    state: PolicyFeatureState
    similarity: Annotated[
        float,
        Field(ge=-1.0, le=1.0, allow_inf_nan=False),
    ] | None
    provenance: PolicyFeatureProvenance


class AccessWindow(SDKResponse):
    read: Annotated[int, Field(ge=0)]
    write: Annotated[int, Field(ge=0)]
    list: Annotated[int, Field(ge=0)]
    touch: Annotated[int, Field(ge=0)]


class EstimatedAccessWindow(SDKResponse):
    read: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    write: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    list: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    touch: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class AccessSampling(SDKResponse):
    configured_rate: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    minimum_rate: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] | None
    sampled: bool


class AccessPolicyFeature(SDKResponse):
    schema_version: Literal[1]
    as_of: str
    bucket: str
    key: str | None
    windows: dict[str, AccessWindow]
    estimated_windows: dict[str, EstimatedAccessWindow]
    last_access_at: str | None
    recency_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    observed_events: Annotated[int, Field(ge=0)]
    observed_since: str | None
    freshness: Literal["fresh", "missing", "stale", "unavailable"]
    freshness_seconds: Annotated[int, Field(gt=0)]
    freshness_basis: Literal["latest_observed_event"]
    sampling: AccessSampling
    missing: bool
    partial: Literal[True]
    coverage: Literal["observed_operations_only"]
    retention_seconds: Annotated[int, Field(gt=0)]
    reason: str | None = None


class EstimateComponent(SDKResponse):
    name: str
    unit: str
    state: Literal["available", "unavailable"]
    value: str | None
    lower: str | None
    upper: str | None
    uncertainty: Literal["bounded", "unquantified"]
    reasons: list[str]


class ImpactTotal(SDKResponse):
    unit: str
    state: Literal["available", "unavailable"]
    value: str | None
    known_subtotal: str
    lower: str | None
    upper: str | None
    uncertainty: Literal["bounded", "unquantified"]
    components: list[EstimateComponent]


class PlacementEstimate(SDKResponse):
    schema_version: Literal[1]
    formulas: dict[str, JSONValue]
    tier: str | None
    pool_id: str | None
    region: str | None
    backend: str | None
    profile_version: str | None
    as_of: str
    workload: dict[str, str | None]
    cost: ImpactTotal
    carbon: ImpactTotal
    rates: list[dict[str, JSONValue]]
    reasons: list[str]


class ObjectPlacementEstimates(SDKResponse):
    schema_version: Literal[1]
    current: PlacementEstimate
    candidates: list[PlacementEstimate]


class PolicyFeatures(SDKResponse):
    schema_version: Literal[1]
    mime: MimePolicyFeature
    embeddings: Annotated[list[EmbeddingPolicyFeature], Field(max_length=100)]
    access: AccessPolicyFeature | None = None
    pii: PIIPolicyFeature | None = None
    placement_estimates: ObjectPlacementEstimates | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class PolicyEvaluationResponse(SDKResponse):
    schema_version: Literal[1] = 1
    bucket: Bucket
    key: ObjectKey
    current_tier: Tier
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    action: Literal["move", "stay"]
    destination_tier: Tier | None = None
    reason: str
    features: PolicyFeatures | None = None
    constraints: dict[str, JSONValue] = Field(default_factory=dict)
    llm_audit: dict[str, JSONValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class JobStatus(SDKResponse):
    schema_version: Literal[1] = 1
    job_id: UUID
    correlation_id: UUID
    job_type: Literal["catalog.scan", "policy.run"]
    status: JobState
    created_at: datetime
    updated_at: datetime
    status_url: Annotated[
        str,
        Field(
            pattern=(
                r"^/v1/jobs/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            )
        ),
    ]
    attempt: Annotated[int, Field(ge=0)] = 0
    retryable: bool | None = None
    error_type: str | None = None


class DecisiveSignal(SDKResponse):
    name: Literal[
        "size_bytes", "name_match", "mime_match", "mime_state", "embedding_state",
        "features_evaluated", "embedding_similarity", "pii_state", "pii_match",
        "pii_confidence",
    ]
    value: (
        Annotated[int | float, Field(allow_inf_nan=False)]
        | bool | Literal["fresh", "stale", "missing", "unavailable"] | None
    )
    operator: Literal["<=", ">", ">=", "=="] | None
    threshold: Annotated[int | float, Field(allow_inf_nan=False)] | None
    rule_index: Annotated[int, Field(ge=0)] | None


class HysteresisCheck(SDKResponse):
    kind: Literal["size", "similarity"]
    configured_band: Annotated[int | float, Field(allow_inf_nan=False)]
    baseline_threshold: Annotated[int | float, Field(allow_inf_nan=False)]
    effective_threshold: Annotated[int | float, Field(allow_inf_nan=False)]
    value: Annotated[int | float, Field(allow_inf_nan=False)] | None
    rule_index: Annotated[int, Field(ge=0)] | None


class ReasonConstraints(SDKResponse):
    evaluated_at: str
    importance_level: ImportanceLevel | None
    importance_revision: Annotated[int, Field(ge=0)]
    importance_allowed_tiers: list[Annotated[str, Field(min_length=1)]] | None
    allowed_destination_tiers: list[Annotated[str, Field(min_length=1)]]
    placement_started_at: str | None
    minimum_residency_seconds: Annotated[int, Field(ge=0)]
    residency_expires_at: str | None
    residency_active: bool
    last_tier_move_at: str | None
    cooldown_seconds: Annotated[int, Field(ge=0)]
    cooldown_expires_at: str | None
    cooldown_active: bool
    size_hysteresis_bytes: Annotated[int, Field(ge=0)]
    similarity_hysteresis: Annotated[float, Field(ge=0, le=2)]
    stability_override_kind: Literal["emergency", "compliance"] | None
    rejected_destination_tier: Annotated[str, Field(min_length=1)] | None
    candidate_action: Literal["move", "stay"] | None
    candidate_destination_tier: Annotated[str, Field(min_length=1)] | None
    hysteresis_checks: list[HysteresisCheck]
    budgets: list[dict[str, Any]] = Field(default_factory=list)
    objectives: dict[str, Any] | None = None
    legal_hold: bool = False
    legal_hold_ids: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    locality: dict[str, Any] | None = None


class ReasonModel(SDKResponse):
    identity: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)] | None


class ReasonPolicy(SDKResponse):
    name: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)]
    model: ReasonModel | None


class ReasonConfidence(SDKResponse):
    value: None
    source: Literal["not_reported", "not_applicable"]


class PolicyReason(SDKResponse):
    schema_version: Literal[1]
    code: Literal[
        "size_threshold", "name_rule", "mime_rule", "embedding_rule", "pii_rule",
        "required_features_unavailable", "provider_decision", "provider_error",
        "provider_invalid_response", "provider_invalid_input", "custom_policy", "minimum_residency",
        "importance_restriction", "cooldown", "hysteresis", "destination_not_allowed",
        "destination_missing", "already_in_tier", "invalid_action", "budget_constraint",
        "legal_hold",
        "locality_constraint",
    ]
    disposition: Literal["move", "stay", "suppressed", "rejected"]
    decisive_signals: list[DecisiveSignal]
    constraints: ReasonConstraints
    policy: ReasonPolicy
    confidence: ReasonConfidence


class DecisionPlacement(SDKResponse):
    """A tier frozen at evaluation time; null means evidence is unavailable."""

    tier: Tier | None


class DecisionExplanation(SDKResponse):
    state: Literal["available", "legacy", "unavailable"]
    structured_reason: PolicyReason | None
    model_details: Literal["available", "not_applicable", "unavailable"]


class DecisionExecution(SDKResponse):
    """Observed execution evidence, independent from the proposed placement."""

    mode: Literal["preview", "persisted"]
    state: Literal[
        "dry_run", "not_requested", "planned", "running", "retrying",
        "completed", "failed", "unavailable",
    ]
    job_id: str | None = None
    correlation_id: str | None = None
    move_id: str | None = None
    job: JobStatus | None = None
    event_id: str | None = None
    updated_at: str | None = None


class PolicyDecision(SDKResponse):
    schema_version: Literal[1] = 1
    decision_id: str | None
    bucket: Bucket
    key: ObjectKey
    evaluated_at: str
    action: Literal["move", "stay"] | None
    disposition: Literal["move", "stay", "suppressed", "rejected", "unavailable"]
    current: DecisionPlacement
    proposed: DecisionPlacement
    changed_fields: list[Literal["tier"]]
    explanation: DecisionExplanation
    execution: DecisionExecution


class PolicyDecisionPage(SDKResponse):
    schema_version: Literal[1] = 1
    items: list[PolicyDecision]
    page: PageMetadata


class ValidationIssue(SDKResponse):
    location: str
    message: str
    type: str


class ErrorBody(SDKResponse):
    code: str
    message: str
    retryable: bool = False
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorEnvelope(SDKResponse):
    schema_version: Literal[1] = 1
    request_id: str
    error: ErrorBody


class HeadObjectResponse(SDKResponse):
    content_length: Annotated[int, Field(ge=0)]
    content_type: str
    etag: str
    accept_ranges: Literal["bytes"]
    request_id: str | None = None


class ObjectDownload(SDKResponse):
    content: bytes
    content_length: Annotated[int, Field(ge=0)]
    content_type: str
    etag: str
    accept_ranges: Literal["bytes"]
    content_range: str | None = None
    status_code: Literal[200, 206]
    request_id: str | None = None


class DeleteObjectResponse(SDKResponse):
    request_id: str | None = None


HealthState: TypeAlias = Literal["ready", "unavailable", "not_configured", "unverified"]
RepairState: TypeAlias = Literal["ready", "running", "completed", "review_required"]
RepairIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class AdminSession(SDKResponse):
    schema_version: Literal[1] = 1
    tenant_id: str
    actor_id: str
    operations: list[str]


class DependencyHealth(SDKResponse):
    status: HealthState


class DriverCapabilityView(SDKResponse):
    range_reads: bool = False
    range_writes: bool = False
    atomic_no_overwrite: bool = False
    conditional_delete: bool = False


class DriverEncryptionView(SDKResponse):
    source: str = "unknown"
    mode: str = "unknown"
    key_configured: bool = False


class PoolView(SDKResponse):
    pool_id: str
    region: str | None
    localities: list[str]
    member_count: int
    active: bool


class TierView(SDKResponse):
    name: str
    active: bool | None
    driver: str | None
    capabilities: DriverCapabilityView
    encryption: DriverEncryptionView
    health: DependencyHealth
    pools: list[PoolView] = Field(default_factory=list)


class AdminStorage(SDKResponse):
    schema_version: Literal[1] = 1
    tenant_id: str
    observed_at: str
    catalog: DependencyHealth
    queue: DependencyHealth
    tiers: list[TierView]


class JobHistoryError(SDKResponse):
    job_id: str
    message: str


class JobHistoryPage(SDKResponse):
    schema_version: Literal[1] = 1
    items: list[JobStatus]
    page: PageMetadata
    errors: list[JobHistoryError] = Field(default_factory=list)


class RepairScope(SDKRequest):
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    bucket: Bucket
    prefix: Annotated[str, Field(max_length=8192)]
    tiers: Annotated[list[Tier], Field(min_length=1, max_length=256)]


class RepairPreviewRequest(SDKRequest):
    repair_id: RepairIdentifier


class RepairSubmitRequest(RepairPreviewRequest):
    preview_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    confirmation: RepairScope


class RepairPreviewResponse(SDKResponse):
    repair_id: RepairIdentifier
    scope: RepairScope
    plan_only: Literal[True] = True
    counts: dict[str, int]
    actions: list[dict[str, JSONValue]]
    preview_token: str


class RepairAttempt(SDKResponse):
    occurred_at: str
    actor_id: str
    status: RepairState
    counts: dict[str, int] = Field(default_factory=dict)


class RepairStatusResponse(SDKResponse):
    repair_id: RepairIdentifier
    scope: RepairScope
    status: RepairState
    counts: dict[str, int] = Field(default_factory=dict)
    actions: list[dict[str, JSONValue]] = Field(default_factory=list)
    history: list[RepairAttempt] = Field(default_factory=list)


class RepairListError(SDKResponse):
    repair_id: RepairIdentifier
    message: str


class RepairListResponse(SDKResponse):
    items: list[RepairStatusResponse]
    errors: list[RepairListError] = Field(default_factory=list)


# OpenAPI schema-name aliases keep generated-contract terminology available
# while the shorter names remain the ergonomic SDK surface.
AskFiltersRequest: TypeAlias = AskFilters
ObjectCitationResponse: TypeAlias = ObjectCitation
PassageCitationResponse: TypeAlias = PassageCitation
PassageMatchResponse: TypeAlias = PassageMatch
RetrievedPassageResponse: TypeAlias = RetrievedPassage
ScoreComponentResponse: TypeAlias = ScoreComponent
RetrievalResultResponse: TypeAlias = RetrievalResult
ProviderDiagnosticResponse: TypeAlias = ProviderDiagnostic
GeneratedAnswerResponse: TypeAlias = GeneratedAnswer
PolicyFeatureProvenanceResponse: TypeAlias = PolicyFeatureProvenance
PIIPolicyFeatureResponse: TypeAlias = PIIPolicyFeature
PIIFindingResponse: TypeAlias = PIIFinding
MimePolicyFeatureResponse: TypeAlias = MimePolicyFeature
EmbeddingPolicyFeatureResponse: TypeAlias = EmbeddingPolicyFeature
AccessPolicyFeatureResponse: TypeAlias = AccessPolicyFeature
EstimateComponentResponse: TypeAlias = EstimateComponent
ImpactTotalResponse: TypeAlias = ImpactTotal
PlacementEstimateResponse: TypeAlias = PlacementEstimate
ObjectPlacementEstimatesResponse: TypeAlias = ObjectPlacementEstimates
PolicyFeaturesResponse: TypeAlias = PolicyFeatures
JobStatusResponse: TypeAlias = JobStatus
PolicyDecisionResource: TypeAlias = PolicyDecision
