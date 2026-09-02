"""Explicit version 1 HTTP request and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    model_validator,
)

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JsonValue
RetrievalSignalValue: TypeAlias = Literal["metadata", "keyword", "vector"]
PassageSignalValue: TypeAlias = Literal["keyword", "vector"]
RetrievalModeValue: TypeAlias = Literal[
    "metadata",
    "metadata+keyword",
    "metadata+vector",
    "metadata+keyword+vector",
]

Bucket = Annotated[str, Field(min_length=1, max_length=1024)]
ObjectKey = Annotated[str, Field(min_length=1, max_length=8192)]
Tier = Annotated[str, Field(min_length=1, max_length=256)]
PageLimit = Annotated[int, Field(ge=1, le=200)]
PolicyPattern = Annotated[str, Field(min_length=1, max_length=1024)]
MimePrefix = Annotated[str, Field(min_length=1, max_length=255)]
MAX_POLICY_CONFIG_BYTES = 64 * 1024


class APIModel(BaseModel):
    """Strict, forward-compatible base for the checked public contract."""

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        strict=True,
    )


class ObjectResource(APIModel):
    schema_version: Literal[1] = 1
    tier: Tier
    bucket: Bucket
    key: ObjectKey
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    generation: str
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    metadata_truncated: bool = False


class CatalogObject(APIModel):
    schema_version: Literal[1] = 1
    bucket: Bucket
    key: ObjectKey
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    tier: Tier
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    metadata_truncated: bool = False


class PageMetadata(APIModel):
    limit: PageLimit
    next_cursor: str | None = None


class CatalogObjectPage(APIModel):
    schema_version: Literal[1] = 1
    items: list[CatalogObject]
    page: PageMetadata


class AskFiltersRequest(APIModel):
    bucket: Bucket | None = None
    key_prefix: Annotated[str, Field(max_length=8192)] = ""
    tier: Tier | None = None
    mime: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    size: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    object_metadata: dict[str, JSONScalar] = Field(default_factory=dict)
    document_metadata: dict[str, JSONScalar] = Field(default_factory=dict)


class AskRequest(APIModel):
    text: Annotated[str, Field(min_length=1, max_length=16_384)]
    filters: AskFiltersRequest = Field(default_factory=AskFiltersRequest)
    limit: Annotated[int, Field(ge=1, le=100)] = 10
    candidate_limit: Annotated[int, Field(ge=1, le=1000)] = 100
    passages_per_result: Annotated[int, Field(ge=1, le=10)] = 3
    synthesize: bool = False
    exact_vector: bool = False
    retrieval_mode: RetrievalModeValue = "metadata+keyword+vector"


class ObjectCitationResponse(APIModel):
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


class PassageCitationResponse(APIModel):
    citation_id: str
    object_id: str
    bucket: Bucket
    key: ObjectKey
    source: PassageSignalValue
    passage_id: str
    passage_index: Annotated[int, Field(ge=0)]
    start_codepoint: Annotated[int, Field(ge=0)]
    end_codepoint: Annotated[int, Field(ge=0)]
    text_sha256: str
    source_sha256: str
    document_text_sha256: str
    document_id: str | None = None
    space_id: str | None = None


class PassageMatchResponse(APIModel):
    signal: PassageSignalValue
    rank: Annotated[int, Field(ge=1)]
    raw_score: float


class RetrievedPassageResponse(APIModel):
    citation: PassageCitationResponse
    text: str
    match: PassageMatchResponse


class ScoreComponentResponse(APIModel):
    signal: RetrievalSignalValue
    rank: Annotated[int, Field(ge=1)]
    raw_score: float
    weight: Annotated[float, Field(ge=0)]
    contribution: Annotated[float, Field(ge=0)]


class RetrievalResultResponse(APIModel):
    citation: ObjectCitationResponse
    score: Annotated[float, Field(ge=0)]
    score_components: list[ScoreComponentResponse]
    passages: list[RetrievedPassageResponse]


class ProviderDiagnosticResponse(APIModel):
    component: Literal["metadata", "keyword", "vector", "generation"]
    state: Literal[
        "succeeded",
        "missing",
        "unavailable",
        "not_requested",
        "no_context",
    ]
    error_type: str | None = None


class GeneratedAnswerResponse(APIModel):
    text: str
    citations: list[str]


class AskResponse(APIModel):
    schema_version: Literal[1]
    mode: RetrievalModeValue
    active_signals: list[RetrievalSignalValue]
    results: list[RetrievalResultResponse]
    providers: list[ProviderDiagnosticResponse]
    generation_status: Literal[
        "not_requested",
        "provider_missing",
        "no_context",
        "succeeded",
        "provider_unavailable",
    ]
    answer: GeneratedAnswerResponse | None = None


class PolicyConfig(APIModel):
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

    @model_validator(mode="after")
    def _bound_aggregate_payload(self) -> PolicyConfig:
        values = [
            *self.allowed_tiers,
            *self.hot_name_patterns,
            *self.warm_name_patterns,
            *self.cold_name_patterns,
            *self.hot_mime_prefixes,
            *self.warm_mime_prefixes,
            *self.cold_mime_prefixes,
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


class PolicyEvaluationRequest(APIModel):
    bucket: Bucket
    key: ObjectKey
    config: PolicyConfig = Field(default_factory=PolicyConfig)


class PolicyEvaluationResponse(APIModel):
    schema_version: Literal[1] = 1
    bucket: Bucket
    key: ObjectKey
    current_tier: Tier
    size: Annotated[int, Field(ge=0, le=2**63 - 1)]
    action: Literal["move", "stay"]
    destination_tier: Tier | None = None
    reason: str


class CatalogScanRequest(APIModel):
    tier: Tier
    bucket: Bucket
    prefix: Annotated[str, Field(max_length=8192)] = ""


class PolicyRunRequest(APIModel):
    bucket: Bucket
    prefix: Annotated[str, Field(max_length=8192)] = ""
    config: PolicyConfig = Field(default_factory=PolicyConfig)


JobState = Literal[
    "queued",
    "running",
    "retrying",
    "succeeded",
    "failed",
]


class JobStatusResponse(APIModel):
    schema_version: Literal[1] = 1
    job_id: UUID
    correlation_id: UUID
    job_type: Literal["catalog.scan", "policy.run"]
    status: JobState
    created_at: Annotated[datetime, Field(json_schema_extra={"format": "date-time"})]
    updated_at: Annotated[datetime, Field(json_schema_extra={"format": "date-time"})]
    status_url: Annotated[
        str,
        Field(pattern=r"^/v1/jobs/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
    ]
    attempt: Annotated[int, Field(ge=0)] = 0
    retryable: bool | None = None
    error_type: str | None = None

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"
    api_version: Literal[1] = 1


class ValidationIssue(APIModel):
    location: str
    message: str
    type: str


class ErrorBody(APIModel):
    code: str
    message: str
    retryable: bool = False
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorEnvelope(APIModel):
    schema_version: Literal[1] = 1
    request_id: str
    error: ErrorBody


class MessageResponse(APIModel):
    schema_version: Literal[1] = 1
    message: str
