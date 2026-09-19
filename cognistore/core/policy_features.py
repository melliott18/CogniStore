"""Backend-neutral, ephemeral feature projections for placement policies.

The projection in this module is deliberately not a persistence model.  It is
assembled from one detached catalog snapshot, access history, and optional indexing services so
policy code can reason about feature state without importing those backends.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from cognistore.auth.tenancy import DEFAULT_TENANT_ID, require_tenant

from .access import AccessConfig, AccessFeatures, compute_access_features
from .catalog import CatalogStore, ObjectRecord
from .embedding_index import (
    EmbeddingIndexer,
    SimilaritySearchFilters,
    SimilaritySearchResult,
)
from .estimation import (
    EstimationWorkload,
    ObjectPlacementEstimates,
    StorageImpactEstimator,
)
from .pii import ClassifiedPIIFinding, parse_pii_classification
from .tenant_dependencies import for_tenant
from .topology import PlacementConstraints

POLICY_FEATURE_SCHEMA_VERSION = 1
POLICY_FEATURE_SOURCE_VERSION = 1

JSONScalar = str | int | float | bool | None
PolicyFeatureCoordinate = tuple[str, str]

_LOWERCASE_HEX = frozenset("0123456789abcdef")
_GENERIC_CONTENT_MIMES = frozenset(
    {"application/octet-stream", "application/x-empty", "inode/x-empty"}
)
_MIME_PATTERN = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$"
)
_MIME_DETAIL_FIELDS = (
    "detector",
    "provenance",
    "confidence",
    "content_mime",
    "filename_mime",
    "filename_encoding",
    "disagreement",
    "status",
    "fallback_reason",
)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty text without outer whitespace")
    if "\0" in value:
        raise ValueError(f"{field_name} must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    return value


def _optional_sha256(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _LOWERCASE_HEX for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256 digest")
    return value


def _json_scalar(value: object, *, field_name: str) -> JSONScalar:
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"{field_name} must contain valid UTF-8") from exc
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{field_name} must be a finite JSON scalar")


def _current_content_sha256(record: ObjectRecord) -> str | None:
    identity = record.metadata.get("content_identity")
    if not isinstance(identity, Mapping):
        return None
    digest = identity.get("sha256")
    try:
        normalized = _optional_sha256(digest, field_name="content_identity.sha256")
    except ValueError:
        return None
    if normalized is None:
        return None
    schema_version = identity.get("schema_version")
    identity_size = identity.get("size")
    if (
        isinstance(schema_version, bool)
        or schema_version != 1
        or identity.get("representation") != "source-bytes"
        or identity.get("digest_algorithm") != "sha256"
        or isinstance(identity_size, bool)
        or not isinstance(identity_size, int)
        or identity_size != record.size
    ):
        return None
    compatibility_digest = record.metadata.get("sha256")
    if compatibility_digest is not None and compatibility_digest != normalized:
        return None
    return normalized


def _valid_mime(value: object) -> str | None:
    if not isinstance(value, str) or value != value.strip().lower():
        return None
    if _MIME_PATTERN.fullmatch(value) is None:
        return None
    return value


def _valid_mime_detection_details(details: Mapping[str, object]) -> bool:
    try:
        _required_text(details.get("detector"), field_name="MIME detector")
    except ValueError:
        return False
    if details.get("provenance") not in {"content", "filename", "none"}:
        return False
    if details.get("confidence") not in {"high", "low", "none"}:
        return False
    if details.get("status") not in {"detected", "fallback", "unclassified"}:
        return False
    if not isinstance(details.get("disagreement"), bool):
        return False
    for field_name in ("content_mime", "filename_mime"):
        value = details.get(field_name)
        if value is not None and _valid_mime(value) is None:
            return False
    for field_name in ("filename_encoding", "fallback_reason"):
        value = details.get(field_name)
        if value is not None:
            try:
                _required_text(value, field_name=f"MIME {field_name}")
            except ValueError:
                return False
    return True


def _valid_mime_detection_semantics(
    envelope: Mapping[str, object],
    details: Mapping[str, object],
) -> bool:
    """Validate cross-field invariants emitted by ``MimeDetectionAdapter``."""

    selected = envelope.get("mime")
    status = details["status"]
    if status == "detected":
        mime = _valid_mime(selected)
        expected_disagreement = (
            mime is not None
            and details["filename_mime"] is not None
            and details["filename_mime"] != mime
        )
        return bool(
            mime is not None
            and details["provenance"] == "content"
            and details["content_mime"] == mime
            and details["confidence"]
            == ("low" if mime in _GENERIC_CONTENT_MIMES else "high")
            and details["disagreement"] is expected_disagreement
            and details["fallback_reason"] is None
        )
    if status == "fallback":
        mime = _valid_mime(selected)
        return bool(
            mime is not None
            and details["detector"] == "filename"
            and details["provenance"] == "filename"
            and details["confidence"] == "low"
            and details["content_mime"] is None
            and details["filename_mime"] == mime
            and details["disagreement"] is False
            and isinstance(details["fallback_reason"], str)
        )
    return bool(
        status == "unclassified"
        and selected is None
        and details["detector"] == "none"
        and details["provenance"] == "none"
        and details["confidence"] == "none"
        and details["content_mime"] is None
        and details["filename_mime"] is None
        and details["disagreement"] is False
        and isinstance(details["fallback_reason"], str)
    )


class FeatureState(str, Enum):
    """Freshness/availability state shared by every policy feature."""

    FRESH = "fresh"
    MISSING = "missing"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class EmbeddingFeatureRequest:
    """One named semantic query configured as a placement-policy signal."""

    name: str
    query: str

    def __post_init__(self) -> None:
        _required_text(self.name, field_name="embedding feature name")
        _required_text(self.query, field_name="embedding feature query")


@dataclass(frozen=True)
class PolicyFeatureProvenance:
    """Versioned origin and content identity for one projected feature."""

    source: str
    source_version: int
    content_sha256: str | None
    details: Mapping[str, JSONScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.source, field_name="feature provenance source")
        if (
            isinstance(self.source_version, bool)
            or not isinstance(self.source_version, int)
            or self.source_version < 1
        ):
            raise ValueError("feature provenance source_version must be a positive integer")
        _optional_sha256(
            self.content_sha256,
            field_name="feature provenance content_sha256",
        )
        if not isinstance(self.details, Mapping):
            raise ValueError("feature provenance details must be a mapping")
        if any(not isinstance(key, str) for key in self.details):
            raise ValueError("feature provenance detail keys must be strings")
        detached: dict[str, JSONScalar] = {}
        for key in sorted(self.details):
            _required_text(key, field_name="feature provenance detail key")
            detached[key] = _json_scalar(
                self.details[key],
                field_name=f"feature provenance detail {key!r}",
            )
        object.__setattr__(self, "details", MappingProxyType(detached))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_version": self.source_version,
            "content_sha256": self.content_sha256,
            "details": dict(self.details),
        }


def _provenance_with_reason(
    provenance: PolicyFeatureProvenance,
    reason: str,
    **extra_details: JSONScalar,
) -> PolicyFeatureProvenance:
    details = dict(provenance.details)
    details.update(extra_details)
    details["reason"] = reason
    return PolicyFeatureProvenance(
        source=provenance.source,
        source_version=provenance.source_version,
        content_sha256=provenance.content_sha256,
        details=details,
    )


@dataclass(frozen=True)
class MimePolicyFeature:
    """Canonical MIME signal plus its freshness and detector provenance."""

    state: FeatureState
    provenance: PolicyFeatureProvenance
    value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, FeatureState):
            raise ValueError("MIME feature state must be FeatureState")
        if not isinstance(self.provenance, PolicyFeatureProvenance):
            raise ValueError("MIME feature provenance must be PolicyFeatureProvenance")
        if self.state is FeatureState.FRESH:
            if _valid_mime(self.value) is None:
                raise ValueError("fresh MIME features require a canonical MIME value")
        elif self.value is not None:
            raise ValueError("non-fresh MIME features must not expose a MIME value")

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "value": self.value,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class EmbeddingPolicyFeature:
    """One named max-passage similarity signal."""

    name: str
    query: str
    state: FeatureState
    provenance: PolicyFeatureProvenance
    similarity: float | None = None

    def __post_init__(self) -> None:
        _required_text(self.name, field_name="embedding feature name")
        _required_text(self.query, field_name="embedding feature query")
        if not isinstance(self.state, FeatureState):
            raise ValueError("embedding feature state must be FeatureState")
        if not isinstance(self.provenance, PolicyFeatureProvenance):
            raise ValueError("embedding feature provenance must be PolicyFeatureProvenance")
        if self.state is FeatureState.FRESH:
            if (
                isinstance(self.similarity, bool)
                or not isinstance(self.similarity, (int, float))
                or not math.isfinite(float(self.similarity))
                or not -1.0 <= float(self.similarity) <= 1.0
            ):
                raise ValueError(
                    "fresh embedding features require similarity between -1 and 1"
                )
            object.__setattr__(self, "similarity", float(self.similarity))
        elif self.similarity is not None:
            raise ValueError("non-fresh embedding features must not expose similarity")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "query": self.query,
            "state": self.state.value,
            "similarity": self.similarity,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class PIIPolicyFeature:
    """Validated redacted findings bound to the current source content."""

    state: FeatureState
    provenance: PolicyFeatureProvenance
    findings: tuple[ClassifiedPIIFinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, FeatureState):
            raise ValueError("PII feature state must be FeatureState")
        if not isinstance(self.provenance, PolicyFeatureProvenance):
            raise ValueError("PII feature provenance must be PolicyFeatureProvenance")
        findings = tuple(self.findings)
        if len(findings) > 10_000 or any(
            type(finding) is not ClassifiedPIIFinding for finding in findings
        ):
            raise ValueError("PII feature findings must be normalized redacted findings")
        if self.state is not FeatureState.FRESH and findings:
            raise ValueError("non-fresh PII features must not expose findings")
        object.__setattr__(self, "findings", findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "findings": [
                {
                    "type": finding.type,
                    "confidence": finding.confidence,
                    "provenance": finding.provenance,
                    "detector": finding.detector,
                    "detector_version": finding.detector_version,
                }
                for finding in self.findings
            ],
            "provenance": self.provenance.to_dict(),
        }


def _pii_feature(record: ObjectRecord) -> PIIPolicyFeature | None:
    """Reject malformed and stale evidence without reflecting raw metadata."""
    if "pii_detection" not in record.metadata:
        return None
    digest = _current_content_sha256(record)
    provenance = PolicyFeatureProvenance("catalog.pii_detection", 1, digest)
    classification = (
        parse_pii_classification(record.metadata["pii_detection"], content_sha256=digest)
        if digest is not None else None
    )
    if classification is None:
        return PIIPolicyFeature(
            FeatureState.STALE,
            _provenance_with_reason(provenance, "pii_evidence_invalid_or_stale"),
        )
    if classification.status != "succeeded":
        return PIIPolicyFeature(
            FeatureState.UNAVAILABLE,
            _provenance_with_reason(provenance, "pii_detection_not_succeeded"),
        )
    return PIIPolicyFeature(FeatureState.FRESH, provenance, classification.findings)


@dataclass(frozen=True)
class PolicyFeatures:
    """Schema-v1 immutable policy feature projection for one catalog object."""

    mime: MimePolicyFeature
    embeddings: tuple[EmbeddingPolicyFeature, ...] = ()
    schema_version: int = POLICY_FEATURE_SCHEMA_VERSION
    access: AccessFeatures | None = None
    placement_estimates: ObjectPlacementEstimates | None = None
    pii: PIIPolicyFeature | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != POLICY_FEATURE_SCHEMA_VERSION
        ):
            raise ValueError(
                f"unsupported policy feature schema_version: {self.schema_version!r}"
            )
        if not isinstance(self.mime, MimePolicyFeature):
            raise ValueError("policy MIME feature must be MimePolicyFeature")
        if self.pii is not None and not isinstance(self.pii, PIIPolicyFeature):
            raise ValueError("policy PII feature must be PIIPolicyFeature")
        if self.access is not None and not isinstance(self.access, AccessFeatures):
            raise ValueError("policy access feature must be AccessFeatures")
        if self.placement_estimates is not None and not isinstance(
            self.placement_estimates, ObjectPlacementEstimates
        ):
            raise ValueError("policy placement estimates must be ObjectPlacementEstimates")
        embeddings = tuple(self.embeddings)
        if any(not isinstance(item, EmbeddingPolicyFeature) for item in embeddings):
            raise ValueError("policy embeddings must contain EmbeddingPolicyFeature values")
        if len({item.name for item in embeddings}) != len(embeddings):
            raise ValueError("policy embedding feature names must be unique")
        object.__setattr__(
            self,
            "embeddings",
            tuple(sorted(embeddings, key=lambda item: (item.name, item.query))),
        )

    def to_dict(self) -> dict[str, object]:
        """Return a fresh, deterministically ordered JSON-safe projection."""

        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "mime": self.mime.to_dict(),
            "embeddings": [item.to_dict() for item in self.embeddings],
        }
        if self.access is not None:
            result["access"] = self.access.to_dict()
        if self.placement_estimates is not None:
            result["placement_estimates"] = self.placement_estimates.to_dict()
        if self.pii is not None:
            result["pii"] = self.pii.to_dict()
        return result


class EmbeddingPolicyFeatureProvider(Protocol):
    """Backend-neutral batch boundary used by the catalog projection loader."""

    def load(
        self,
        records: Sequence[ObjectRecord],
        requests: Sequence[EmbeddingFeatureRequest],
    ) -> Mapping[PolicyFeatureCoordinate, Sequence[EmbeddingPolicyFeature]]: ...


def _attempt_provenance(
    *,
    source: str,
    content_sha256: str | None,
    reason: str,
    error_type: str | None = None,
    source_version: int = POLICY_FEATURE_SOURCE_VERSION,
) -> PolicyFeatureProvenance:
    details: dict[str, JSONScalar] = {"reason": reason}
    if error_type is not None:
        details["error_type"] = error_type
    return PolicyFeatureProvenance(
        source=source,
        source_version=source_version,
        content_sha256=content_sha256,
        details=details,
    )


def _stale_embedding_feature(
    request: EmbeddingFeatureRequest,
    provenance: PolicyFeatureProvenance,
) -> EmbeddingPolicyFeature:
    return EmbeddingPolicyFeature(
        name=request.name,
        query=request.query,
        state=FeatureState.STALE,
        provenance=provenance,
    )


def _missing_embedding_feature(
    request: EmbeddingFeatureRequest,
    provenance: PolicyFeatureProvenance,
) -> EmbeddingPolicyFeature:
    return EmbeddingPolicyFeature(
        name=request.name,
        query=request.query,
        state=FeatureState.MISSING,
        provenance=provenance,
    )


def _unavailable_embedding_feature(
    request: EmbeddingFeatureRequest,
    provenance: PolicyFeatureProvenance,
) -> EmbeddingPolicyFeature:
    return EmbeddingPolicyFeature(
        name=request.name,
        query=request.query,
        state=FeatureState.UNAVAILABLE,
        provenance=provenance,
    )


def _mime_feature(record: ObjectRecord) -> MimePolicyFeature:
    envelope = record.metadata.get("mime_detection")
    if envelope is None:
        content_sha256 = _current_content_sha256(record)
        selected = record.metadata.get("mime")
        mime = _valid_mime(selected)
        if mime is not None:
            return MimePolicyFeature(
                FeatureState.STALE,
                provenance=_attempt_provenance(
                    source="catalog_metadata",
                    content_sha256=content_sha256,
                    reason=(
                        "mime_detection_missing"
                        if content_sha256 is not None
                        else "current_content_identity_missing"
                    ),
                ),
            )
        state = FeatureState.MISSING if selected is None else FeatureState.STALE
        reason = "mime_missing" if selected is None else "mime_malformed"
        return MimePolicyFeature(
            state,
            provenance=_attempt_provenance(
                source="catalog_metadata",
                content_sha256=content_sha256,
                reason=reason,
            ),
        )
    if not isinstance(envelope, Mapping):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_attempt_provenance(
                source="mime_detection",
                content_sha256=_current_content_sha256(record),
                reason="metadata_malformed",
            ),
        )
    schema_version = envelope.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_attempt_provenance(
                source="mime_detection",
                content_sha256=_current_content_sha256(record),
                reason="source_version_malformed",
            ),
        )
    content_sha256 = _current_content_sha256(record)
    if any(field_name not in envelope for field_name in _MIME_DETAIL_FIELDS):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_attempt_provenance(
                source="mime_detection",
                source_version=schema_version,
                content_sha256=content_sha256,
                reason="metadata_incomplete",
            ),
        )
    details = {field_name: envelope[field_name] for field_name in _MIME_DETAIL_FIELDS}
    if not _valid_mime_detection_details(details):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_attempt_provenance(
                source="mime_detection",
                source_version=schema_version,
                content_sha256=content_sha256,
                reason="metadata_malformed",
            ),
        )
    try:
        provenance = PolicyFeatureProvenance(
            source="mime_detection",
            source_version=schema_version,
            content_sha256=content_sha256,
            details=details,
        )
    except ValueError:
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_attempt_provenance(
                source="mime_detection",
                source_version=schema_version,
                content_sha256=content_sha256,
                reason="metadata_malformed",
            ),
        )
    if not _valid_mime_detection_semantics(envelope, details):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_provenance_with_reason(
                provenance,
                "metadata_inconsistent",
            ),
        )
    if schema_version != 1:
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_provenance_with_reason(
                provenance,
                "source_version_unsupported",
            ),
        )
    if content_sha256 is None:
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_provenance_with_reason(
                provenance,
                "current_content_identity_missing",
            ),
        )

    selected = envelope.get("mime")
    status = envelope.get("status")
    if selected is None:
        if (
            status != "unclassified"
            or details["provenance"] != "none"
            or details["confidence"] != "none"
            or record.metadata.get("mime") is not None
        ):
            return MimePolicyFeature(
                FeatureState.STALE,
                provenance=_provenance_with_reason(
                    provenance,
                    "selection_inconsistent",
                ),
            )
        return MimePolicyFeature(
            FeatureState.MISSING,
            provenance=_provenance_with_reason(provenance, "mime_unclassified"),
        )
    mime = _valid_mime(selected)
    if (
        mime is None
        or status not in {"detected", "fallback"}
        or record.metadata.get("mime") != mime
    ):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_provenance_with_reason(
                provenance,
                "canonical_mime_mismatch",
            ),
        )
    if status == "detected" and (
        details["provenance"] != "content" or details["content_mime"] != mime
    ):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_provenance_with_reason(
                provenance,
                "content_detection_inconsistent",
            ),
        )
    if status == "fallback" and (
        details["provenance"] != "filename" or details["filename_mime"] != mime
    ):
        return MimePolicyFeature(
            FeatureState.STALE,
            provenance=_provenance_with_reason(
                provenance,
                "filename_fallback_inconsistent",
            ),
        )
    return MimePolicyFeature(
        FeatureState.FRESH,
        provenance=provenance,
        value=mime,
    )


class CatalogPolicyFeatureLoader:
    """Project catalog snapshots with access, embeddings, and optional estimates.

    Impact estimates require an explicit workload factory. Observed access
    history is never silently promoted into a prediction of future demand.
    """

    def __init__(
        self,
        embedding_provider: EmbeddingPolicyFeatureProvider | None = None,
        *,
        access_catalog: CatalogStore | None = None,
        access_config: AccessConfig | None = None,
        impact_estimator: StorageImpactEstimator | None = None,
        estimation_catalog: CatalogStore | None = None,
        estimation_workload_factory: Callable[[ObjectRecord], EstimationWorkload] | None = None,
        estimation_constraints: PlacementConstraints | None = None,
    ) -> None:
        self.tenant_id = getattr(
            access_catalog or estimation_catalog or embedding_provider,
            "tenant_id", DEFAULT_TENANT_ID,
        )
        self.embedding_provider = for_tenant(embedding_provider, self.tenant_id)
        self.access_catalog = for_tenant(access_catalog, self.tenant_id)
        self.access_config = access_config or AccessConfig()
        estimation_inputs = (impact_estimator, estimation_catalog, estimation_workload_factory)
        if any(value is not None for value in estimation_inputs) and any(
            value is None for value in estimation_inputs
        ):
            raise ValueError(
                "impact estimates require an estimator, catalog, and workload factory"
            )
        if estimation_workload_factory is not None and not callable(estimation_workload_factory):
            raise ValueError("estimation_workload_factory must be callable")
        if estimation_constraints is not None:
            if not isinstance(estimation_constraints, PlacementConstraints):
                raise ValueError("estimation_constraints must be PlacementConstraints")
            if impact_estimator is None:
                raise ValueError("estimation_constraints require an impact estimator")
        self.impact_estimator = impact_estimator
        self.estimation_catalog = for_tenant(estimation_catalog, self.tenant_id)
        self.estimation_workload_factory = estimation_workload_factory
        self.estimation_constraints = estimation_constraints

    def for_tenant(self, tenant_id: str) -> CatalogPolicyFeatureLoader:
        loader = CatalogPolicyFeatureLoader(
            for_tenant(self.embedding_provider, tenant_id),
            access_catalog=for_tenant(self.access_catalog, tenant_id),
            access_config=self.access_config,
            impact_estimator=self.impact_estimator,
            estimation_catalog=for_tenant(self.estimation_catalog, tenant_id),
            estimation_workload_factory=self.estimation_workload_factory,
            estimation_constraints=self.estimation_constraints,
        )
        loader.tenant_id = tenant_id
        return loader

    def load(
        self,
        records: Iterable[ObjectRecord],
        requests: Iterable[EmbeddingFeatureRequest] = (),
        *,
        as_of: str | datetime | None = None,
    ) -> dict[PolicyFeatureCoordinate, PolicyFeatures]:
        require_tenant(self.tenant_id)
        detached_records = tuple(records)
        detached_requests = tuple(requests)
        if any(not isinstance(record, ObjectRecord) for record in detached_records):
            raise ValueError("records must contain ObjectRecord values")
        if any(
            not isinstance(request, EmbeddingFeatureRequest)
            for request in detached_requests
        ):
            raise ValueError("requests must contain EmbeddingFeatureRequest values")
        coordinates = [(record.bucket, record.key) for record in detached_records]
        if len(set(coordinates)) != len(coordinates):
            raise ValueError("records must have unique bucket/key coordinates")
        if len({request.name for request in detached_requests}) != len(detached_requests):
            raise ValueError("embedding feature request names must be unique")

        ordered_records = tuple(
            record
            for _coordinate, record in sorted(
                zip(coordinates, detached_records),
                key=lambda item: item[0],
            )
        )
        ordered_requests = tuple(
            sorted(detached_requests, key=lambda request: (request.name, request.query))
        )
        embedding_features = self._embedding_features(ordered_records, ordered_requests)
        evaluated_at = as_of if as_of is not None else datetime.now(timezone.utc)
        return {
            (record.bucket, record.key): PolicyFeatures(
                mime=_mime_feature(record),
                pii=_pii_feature(record),
                embeddings=embedding_features[(record.bucket, record.key)],
                access=compute_access_features(
                    self.access_catalog,
                    record.bucket,
                    record.key,
                    config=self.access_config,
                    as_of=evaluated_at,
                ),
                placement_estimates=self._placement_estimates(record, as_of=evaluated_at),
            )
            for record in ordered_records
        }

    def _placement_estimates(
        self,
        record: ObjectRecord,
        *,
        as_of: str | datetime,
    ) -> ObjectPlacementEstimates | None:
        if self.impact_estimator is None:
            return None
        assert self.estimation_catalog is not None
        assert self.estimation_workload_factory is not None
        workload = self.estimation_workload_factory(record)
        return self.impact_estimator.estimate_object(
            self.estimation_catalog,
            record,
            workload,
            constraints=self.estimation_constraints,
            as_of=as_of,
        )

    def _embedding_features(
        self,
        records: Sequence[ObjectRecord],
        requests: Sequence[EmbeddingFeatureRequest],
    ) -> dict[PolicyFeatureCoordinate, tuple[EmbeddingPolicyFeature, ...]]:
        if not requests:
            return {(record.bucket, record.key): () for record in records}
        if self.embedding_provider is None:
            return {
                (record.bucket, record.key): tuple(
                    _missing_embedding_feature(
                        request,
                        _attempt_provenance(
                            source="embedding_similarity",
                            content_sha256=_current_content_sha256(record),
                            reason="provider_missing",
                        ),
                    )
                    for request in requests
                )
                for record in records
            }
        try:
            loaded = self.embedding_provider.load(records, requests)
            raw: object = dict(loaded) if isinstance(loaded, Mapping) else loaded
        except Exception as exc:
            return {
                (record.bucket, record.key): tuple(
                    _unavailable_embedding_feature(
                        request,
                        _attempt_provenance(
                            source="embedding_similarity",
                            content_sha256=_current_content_sha256(record),
                            reason="provider_unavailable",
                            error_type=type(exc).__name__,
                        ),
                    )
                    for request in requests
                )
                for record in records
            }
        if not isinstance(raw, Mapping):
            return {
                (record.bucket, record.key): tuple(
                    _stale_embedding_feature(
                        request,
                        _attempt_provenance(
                            source="embedding_similarity",
                            content_sha256=_current_content_sha256(record),
                            reason="provider_result_malformed",
                        ),
                    )
                    for request in requests
                )
                for record in records
            }

        projected: dict[PolicyFeatureCoordinate, tuple[EmbeddingPolicyFeature, ...]] = {}
        for record in records:
            coordinate = (record.bucket, record.key)
            values = raw.get(coordinate)
            if values is None:
                projected[coordinate] = tuple(
                    _stale_embedding_feature(
                        request,
                        _attempt_provenance(
                            source="embedding_similarity",
                            content_sha256=_current_content_sha256(record),
                            reason="coordinate_omitted",
                        ),
                    )
                    for request in requests
                )
                continue
            if isinstance(values, (str, bytes, bytearray)) or not isinstance(
                values, Sequence
            ):
                projected[coordinate] = tuple(
                    _stale_embedding_feature(
                        request,
                        _attempt_provenance(
                            source="embedding_similarity",
                            content_sha256=_current_content_sha256(record),
                            reason="provider_result_malformed",
                        ),
                    )
                    for request in requests
                )
                continue
            supplied: dict[str, EmbeddingPolicyFeature] = {}
            malformed = False
            for value in values:
                if not isinstance(value, EmbeddingPolicyFeature) or value.name in supplied:
                    malformed = True
                    break
                supplied[value.name] = value
            if malformed or set(supplied).difference(request.name for request in requests):
                projected[coordinate] = tuple(
                    _stale_embedding_feature(
                        request,
                        _attempt_provenance(
                            source="embedding_similarity",
                            content_sha256=_current_content_sha256(record),
                            reason="provider_result_malformed",
                        ),
                    )
                    for request in requests
                )
                continue
            current_sha256 = _current_content_sha256(record)
            normalized: list[EmbeddingPolicyFeature] = []
            for request in requests:
                feature = supplied.get(request.name)
                if feature is None:
                    normalized.append(
                        _stale_embedding_feature(
                            request,
                            _attempt_provenance(
                                source="embedding_similarity",
                                content_sha256=current_sha256,
                                reason="feature_omitted",
                            ),
                        )
                    )
                    continue
                if feature.query != request.query:
                    normalized.append(
                        _stale_embedding_feature(
                            request,
                            _attempt_provenance(
                                source=feature.provenance.source,
                                source_version=feature.provenance.source_version,
                                content_sha256=feature.provenance.content_sha256,
                                reason="query_mismatch",
                            ),
                        )
                    )
                    continue
                provenance = feature.provenance
                if current_sha256 is None or provenance.content_sha256 != current_sha256:
                    evidence_sha256 = provenance.content_sha256
                    stale_provenance = PolicyFeatureProvenance(
                        source=provenance.source,
                        source_version=provenance.source_version,
                        content_sha256=current_sha256,
                        details={
                            **dict(provenance.details),
                            "evidence_content_sha256": evidence_sha256,
                            "reason": "content_sha256_mismatch",
                        },
                    )
                    normalized.append(
                        _stale_embedding_feature(
                            request,
                            stale_provenance,
                        )
                    )
                    continue
                normalized.append(feature)
            projected[coordinate] = tuple(normalized)
        return projected


class EmbeddingSimilarityFeatureProvider:
    """Project exact max-passage similarity from an :class:`EmbeddingIndexer`."""

    def __init__(self, indexer: EmbeddingIndexer) -> None:
        if not isinstance(indexer, EmbeddingIndexer):
            raise ValueError("indexer must be an EmbeddingIndexer")
        self.indexer = indexer
        self.tenant_id = indexer.tenant_id

    def for_tenant(self, tenant_id: str) -> EmbeddingSimilarityFeatureProvider:
        return EmbeddingSimilarityFeatureProvider(self.indexer.for_tenant(tenant_id))

    def load(
        self,
        records: Sequence[ObjectRecord],
        requests: Sequence[EmbeddingFeatureRequest],
    ) -> Mapping[PolicyFeatureCoordinate, Sequence[EmbeddingPolicyFeature]]:
        require_tenant(self.tenant_id)
        query_vectors: dict[str, tuple[float, ...]] = {}
        for request in requests:
            if request.query not in query_vectors:
                query_vectors[request.query] = self.indexer.query_vector(request.query)

        projected: dict[PolicyFeatureCoordinate, tuple[EmbeddingPolicyFeature, ...]] = {}
        for record in records:
            coordinate = (record.bucket, record.key)
            current_sha256 = _current_content_sha256(record)
            features: list[EmbeddingPolicyFeature] = []
            for request in requests:
                if current_sha256 is None:
                    features.append(
                        self._stale(
                            request,
                            content_sha256=None,
                            reason="current_content_identity_malformed",
                        )
                    )
                    continue
                hits = self.indexer.repository.search(
                    self.indexer.provider.space,
                    query_vectors[request.query],
                    filters=SimilaritySearchFilters(
                        buckets=frozenset({record.bucket}),
                        object_keys=frozenset({record.key}),
                    ),
                    limit=1,
                    exact=True,
                )
                if (
                    not isinstance(hits, list)
                    or len(hits) > 1
                    or any(not isinstance(hit, SimilaritySearchResult) for hit in hits)
                ):
                    features.append(
                        self._stale(
                            request,
                            content_sha256=current_sha256,
                            reason="malformed_search_result",
                        )
                    )
                    continue
                if not hits:
                    features.append(
                        EmbeddingPolicyFeature(
                            name=request.name,
                            query=request.query,
                            state=FeatureState.MISSING,
                            provenance=self._provenance(
                                current_sha256,
                                reason="embedding_not_indexed",
                            ),
                        )
                    )
                    continue
                hit = hits[0]
                if hit.space_id != self.indexer.provider.space.space_id:
                    features.append(
                        self._stale(
                            request,
                            content_sha256=current_sha256,
                            reason="hit_space_mismatch",
                            hit=hit,
                        )
                    )
                    continue
                if (hit.bucket, hit.key) != coordinate:
                    features.append(
                        self._stale(
                            request,
                            content_sha256=current_sha256,
                            reason="hit_coordinate_mismatch",
                            hit=hit,
                        )
                    )
                    continue
                if hit.source_sha256 != current_sha256:
                    features.append(
                        self._stale(
                            request,
                            content_sha256=current_sha256,
                            reason="hit_content_sha256_mismatch",
                            hit=hit,
                        )
                    )
                    continue
                distance = hit.cosine_distance
                if (
                    isinstance(distance, bool)
                    or not isinstance(distance, (int, float))
                    or not math.isfinite(float(distance))
                ):
                    similarity = math.nan
                else:
                    similarity = 1.0 - float(distance)
                if (
                    not math.isfinite(similarity)
                    or not -1.0 <= similarity <= 1.0
                    or (hit.indexed_at is not None and not isinstance(hit.indexed_at, str))
                ):
                    features.append(
                        self._stale(
                            request,
                            content_sha256=current_sha256,
                            reason="malformed_similarity",
                            hit=hit,
                        )
                    )
                    continue
                features.append(
                    EmbeddingPolicyFeature(
                        name=request.name,
                        query=request.query,
                        state=FeatureState.FRESH,
                        similarity=similarity,
                        provenance=self._provenance(current_sha256, hit=hit),
                    )
                )
            projected[coordinate] = tuple(features)
        return MappingProxyType(projected)

    def _provenance(
        self,
        content_sha256: str | None,
        *,
        hit: SimilaritySearchResult | None = None,
        reason: str | None = None,
    ) -> PolicyFeatureProvenance:
        details: dict[str, JSONScalar] = {
            "aggregation": "max_passage_similarity",
        }
        for key, value in self.indexer.provider.space.to_metadata().items():
            details[key] = _json_scalar(
                value,
                field_name=f"embedding space metadata {key!r}",
            )
        if hit is not None:
            candidates: tuple[tuple[str, object], ...] = (
                ("document_id", str(hit.document_id)),
                ("document_text_sha256", hit.document_text_sha256),
                ("passage_id", str(hit.passage_id)),
                ("passage_index", hit.passage_index),
                ("passage_text_sha256", hit.text_sha256),
                ("hit_content_sha256", hit.source_sha256),
                ("indexed_at", hit.indexed_at),
            )
            for key, value in candidates:
                if value is None:
                    continue
                try:
                    details[key] = _json_scalar(
                        value,
                        field_name=f"embedding hit evidence {key!r}",
                    )
                except ValueError:
                    # Malformed repository evidence is reflected by the stale
                    # feature state; it must not escalate the whole batch to an
                    # unavailable provider merely while building provenance.
                    continue
        if reason is not None:
            details["reason"] = reason
        return PolicyFeatureProvenance(
            source="embedding_similarity",
            source_version=POLICY_FEATURE_SOURCE_VERSION,
            content_sha256=content_sha256,
            details=details,
        )

    def _stale(
        self,
        request: EmbeddingFeatureRequest,
        *,
        content_sha256: str | None,
        reason: str,
        hit: SimilaritySearchResult | None = None,
    ) -> EmbeddingPolicyFeature:
        return EmbeddingPolicyFeature(
            name=request.name,
            query=request.query,
            state=FeatureState.STALE,
            provenance=self._provenance(
                content_sha256,
                hit=hit,
                reason=reason,
            ),
        )


__all__ = [
    "POLICY_FEATURE_SCHEMA_VERSION",
    "CatalogPolicyFeatureLoader",
    "EmbeddingFeatureRequest",
    "EmbeddingPolicyFeature",
    "EmbeddingPolicyFeatureProvider",
    "EmbeddingSimilarityFeatureProvider",
    "FeatureState",
    "JSONScalar",
    "MimePolicyFeature",
    "PIIPolicyFeature",
    "PolicyFeatureCoordinate",
    "PolicyFeatureProvenance",
    "PolicyFeatures",
]
