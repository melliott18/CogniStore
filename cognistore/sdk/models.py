"""Typed, forward-compatible models for the public CogniStore HTTP contract.

These models are deliberately maintained independently from ``cognistore.api``.
The SDK can therefore be imported and used without importing server, catalog, or
storage-driver implementation details.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias
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
MAX_POLICY_CONFIG_BYTES = 64 * 1024


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


class PolicyConfig(SDKRequest):
    policy: Literal["simple", "content", "llm"] = "simple"
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

    @model_validator(mode="after")
    def _bound_aggregate_payload(self) -> PolicyConfig:
        if self.embedding_rules and self.policy != "content":
            raise ValueError("embedding rules require the content policy")
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


class CatalogObjectPage(SDKResponse):
    schema_version: Literal[1] = 1
    items: list[CatalogObject]
    page: PageMetadata


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


class EmbeddingPolicyFeature(SDKResponse):
    name: EmbeddingRuleName
    query: EmbeddingRuleQuery
    state: PolicyFeatureState
    similarity: Annotated[
        float,
        Field(ge=-1.0, le=1.0, allow_inf_nan=False),
    ] | None
    provenance: PolicyFeatureProvenance


class PolicyFeatures(SDKResponse):
    schema_version: Literal[1]
    mime: MimePolicyFeature
    embeddings: Annotated[list[EmbeddingPolicyFeature], Field(max_length=100)]


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
MimePolicyFeatureResponse: TypeAlias = MimePolicyFeature
EmbeddingPolicyFeatureResponse: TypeAlias = EmbeddingPolicyFeature
PolicyFeaturesResponse: TypeAlias = PolicyFeatures
JobStatusResponse: TypeAlias = JobStatus
