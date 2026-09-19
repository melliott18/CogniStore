from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timezone
from subprocess import run
from sys import executable
from typing import Literal
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from cognistore.api import models as api_models
from cognistore.api.app import create_app
from cognistore.api.errors import (
    RangeNotSatisfiableError as APIRangeNotSatisfiableError,
)
from cognistore.api.errors import ResourceNotFoundError
from cognistore.api.gateway import ObjectDownload as APIObjectDownload
from cognistore.sdk import (
    SUPPORTED_OPERATION_IDS,
    APIError,
    AskRequest,
    AskResponse,
    AuthenticationError,
    CatalogObject,
    CatalogObjectPage,
    CatalogScanRequest,
    ClientConfig,
    CogniStoreClient,
    ConflictError,
    DeleteObjectResponse,
    EmbeddingPolicyFeature,
    EmbeddingPolicyRuleConfig,
    HeadObjectResponse,
    HealthResponse,
    ImportanceChangeRequest,
    JobFailedError,
    JobStatus,
    MimePolicyFeature,
    MovementConstraintsConfig,
    NotFoundError,
    ObjectDownload,
    ObjectResource,
    PayloadTooLargeError,
    PermissionDeniedError,
    PolicyConfig,
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    PolicyFeatureProvenance,
    PolicyFeatures,
    PolicyRunRequest,
    PollingTimeoutError,
    RangeNotSatisfiableError,
    RequestTimeoutError,
    ResponseContractError,
    ServerError,
    ServiceUnavailableError,
    TransportError,
    ValidationError,
    ValidationIssue,
)
from cognistore.sdk import models as sdk_models

_SCAN_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
_POLICY_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
_CORRELATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_MODEL_SCHEMA_PAIRS = {
    "ObjectResource": "ObjectResource",
    "CatalogObject": "CatalogObject",
    "PageMetadata": "PageMetadata",
    "CatalogObjectPage": "CatalogObjectPage",
    "AskFiltersRequest": "AskFilters",
    "AskRequest": "AskRequest",
    "ObjectCitationResponse": "ObjectCitation",
    "PassageCitationResponse": "PassageCitation",
    "PassageMatchResponse": "PassageMatch",
    "RetrievedPassageResponse": "RetrievedPassage",
    "ScoreComponentResponse": "ScoreComponent",
    "RetrievalResultResponse": "RetrievalResult",
    "ProviderDiagnosticResponse": "ProviderDiagnostic",
    "GeneratedAnswerResponse": "GeneratedAnswer",
    "AskResponse": "AskResponse",
    "EmbeddingPolicyRuleConfig": "EmbeddingPolicyRuleConfig",
    "PIIPolicyRuleConfig": "PIIPolicyRuleConfig",
    "PIIFindingResponse": "PIIFinding",
    "PIIPolicyFeatureResponse": "PIIPolicyFeature",
    "PolicyConfig": "PolicyConfig",
    "MovementConstraintsConfig": "MovementConstraintsConfig",
    "StabilityOverrideConfig": "StabilityOverrideConfig",
    "ImportanceChangeRequest": "ImportanceChangeRequest",
    "PolicyEvaluationRequest": "PolicyEvaluationRequest",
    "PolicyFeatureProvenanceResponse": "PolicyFeatureProvenance",
    "MimePolicyFeatureResponse": "MimePolicyFeature",
    "EmbeddingPolicyFeatureResponse": "EmbeddingPolicyFeature",
    "AccessPolicyFeatureResponse": "AccessPolicyFeature",
    "AccessWindowResponse": "AccessWindow",
    "EstimatedAccessWindowResponse": "EstimatedAccessWindow",
    "AccessSamplingResponse": "AccessSampling",
    "EstimateComponentResponse": "EstimateComponent",
    "ImpactTotalResponse": "ImpactTotal",
    "PlacementEstimateResponse": "PlacementEstimate",
    "ObjectPlacementEstimatesResponse": "ObjectPlacementEstimates",
    "PolicyFeaturesResponse": "PolicyFeatures",
    "PolicyEvaluationResponse": "PolicyEvaluationResponse",
    "PolicyReason": "PolicyReason",
    "DecisionPlacement": "DecisionPlacement",
    "DecisionExplanation": "DecisionExplanation",
    "DecisionExecution": "DecisionExecution",
    "PolicyDecisionResource": "PolicyDecision",
    "PolicyDecisionPage": "PolicyDecisionPage",
    "CatalogScanRequest": "CatalogScanRequest",
    "PolicyRunRequest": "PolicyRunRequest",
    "JobStatusResponse": "JobStatus",
    "HealthResponse": "HealthResponse",
    "ValidationIssue": "ValidationIssue",
    "ErrorBody": "ErrorBody",
    "ErrorEnvelope": "ErrorEnvelope",
}


def _normalized_model_schema(value: object) -> object:
    """Ignore model naming and intentional response-extra policy differences."""

    if isinstance(value, list):
        return [_normalized_model_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, object] = {}
    for key, item in value.items():
        if key == "title" or (key == "additionalProperties" and item is False):
            continue
        if key == "$ref" and isinstance(item, str):
            name = item.rsplit("/", 1)[-1]
            normalized[key] = f"#/$defs/{_MODEL_SCHEMA_PAIRS.get(name, name)}"
        elif key == "$defs" and isinstance(item, dict):
            normalized[key] = {
                _MODEL_SCHEMA_PAIRS.get(name, name): _normalized_model_schema(schema)
                for name, schema in item.items()
            }
        else:
            normalized[key] = _normalized_model_schema(item)
    return normalized


def test_sdk_import_does_not_load_server_database_or_driver_modules() -> None:
    script = """
import sys
import cognistore.sdk

forbidden = ("cognistore.api", "cognistore.db", "cognistore.drivers")
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
assert not loaded, loaded
"""

    run([executable, "-c", script], check=True)


@pytest.mark.parametrize(
    ("api_model_name", "sdk_model_name"),
    _MODEL_SCHEMA_PAIRS.items(),
)
def test_sdk_model_schemas_match_the_checked_api_contract(
    api_model_name: str,
    sdk_model_name: str,
) -> None:
    api_model = getattr(api_models, api_model_name)
    sdk_model = getattr(sdk_models, sdk_model_name)

    assert _normalized_model_schema(
        api_model.model_json_schema()
    ) == _normalized_model_schema(sdk_model.model_json_schema())


def test_sdk_budget_reason_preserves_unknown_amounts_and_objective_evidence() -> None:
    payload = {
        "schema_version": 1, "code": "budget_constraint", "disposition": "suppressed",
        "decisive_signals": [],
        "constraints": {
            "evaluated_at": "2026-09-11T00:00:00Z", "importance_level": None,
            "importance_revision": 0, "importance_allowed_tiers": None,
            "allowed_destination_tiers": ["hot", "warm"], "placement_started_at": None,
            "minimum_residency_seconds": 0, "residency_expires_at": None,
            "residency_active": False, "last_tier_move_at": None, "cooldown_seconds": 0,
            "cooldown_expires_at": None, "cooldown_active": False,
            "size_hysteresis_bytes": 0, "similarity_hysteresis": 0.0,
            "stability_override_kind": None, "rejected_destination_tier": "warm",
            "candidate_action": None, "candidate_destination_tier": None,
            "hysteresis_checks": [],
            "budgets": [{
                "budget_id": "september", "allowed": False,
                "binding_constraints": ["carbon_gco2e_unavailable"],
                "after": {"cost_usd": "0.004", "carbon_gco2e": None},
            }],
            "objectives": {
                "weights": {"cost": "1", "carbon": "0", "latency": "0", "locality": "0"},
                "selected": {"pool_id": "warm-east", "tier": "warm", "score": "0"},
            },
        },
        "policy": {"name": "estimate", "version": "1", "model": None},
        "confidence": {"value": None, "source": "not_applicable"},
    }
    sdk_reason = sdk_models.PolicyReason.model_validate(payload)
    assert sdk_reason.model_dump(mode="json") == payload
    assert sdk_reason.constraints.budgets[0]["after"]["carbon_gco2e"] is None


def test_default_ask_mode_is_omitted_for_older_strict_v1_servers() -> None:
    request_bodies: list[bytes] = []

    def ask_handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(request.content)
        return httpx.Response(
            200,
            request=request,
            headers={"X-Request-ID": "ask-mode-compatibility"},
            json={
                "schema_version": 1,
                "mode": "metadata",
                "active_signals": ["metadata"],
                "results": [],
                "providers": [],
                "generation_status": "not_requested",
                "answer": None,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(ask_handler)) as http_client:
        with CogniStoreClient("https://sdk.test", http_client=http_client) as sdk:
            sdk.ask(AskRequest(text="implicit hybrid default"))
            sdk.ask(
                AskRequest(
                    text="explicit hybrid mode",
                    retrieval_mode="metadata+keyword+vector",
                )
            )

    assert b'"retrieval_mode"' not in request_bodies[0]
    assert b'"retrieval_mode":"metadata+keyword+vector"' in request_bodies[1]


def test_empty_embedding_rules_are_omitted_for_older_strict_v1_servers() -> None:
    request_bodies: dict[str, list[bytes]] = {
        "/v1/policies/evaluate": [],
        "/v1/actions/policy-runs": [],
    }

    def policy_handler(request: httpx.Request) -> httpx.Response:
        request_bodies[request.url.path].append(request.content)
        if request.url.path == "/v1/policies/evaluate":
            return httpx.Response(
                200,
                request=request,
                headers={"X-Request-ID": "policy-feature-compatibility"},
                json={
                    "schema_version": 1,
                    "bucket": "documents",
                    "key": "report.txt",
                    "current_tier": "warm",
                    "size": 10,
                    "action": "stay",
                    "destination_tier": None,
                    "reason": "compatibility fixture",
                    "features": {
                        "schema_version": 1,
                        "mime": {
                            "state": "missing",
                            "value": None,
                            "provenance": {
                                "source": "catalog_metadata",
                                "source_version": 1,
                                "content_sha256": None,
                                "details": {"reason": "mime_missing"},
                            },
                        },
                        "embeddings": [],
                    },
                },
            )
        return httpx.Response(
            202,
            request=request,
            headers={"X-Request-ID": "policy-feature-compatibility"},
            json={
                "schema_version": 1,
                "job_id": str(_POLICY_JOB_ID),
                "correlation_id": str(_CORRELATION_ID),
                "job_type": "policy.run",
                "status": "queued",
                "created_at": _NOW.isoformat().replace("+00:00", "Z"),
                "updated_at": _NOW.isoformat().replace("+00:00", "Z"),
                "status_url": f"/v1/jobs/{_POLICY_JOB_ID}",
                "attempt": 0,
                "retryable": None,
                "error_type": None,
            },
        )

    configured_rule = EmbeddingPolicyRuleConfig(
        name="active-report",
        query="frequently used project report",
        minimum_similarity=0.8,
        destination_tier="hot",
    )
    with httpx.Client(transport=httpx.MockTransport(policy_handler)) as http_client:
        with CogniStoreClient("https://sdk.test", http_client=http_client) as sdk:
            sdk.evaluate_policy(
                PolicyEvaluationRequest(bucket="documents", key="report.txt")
            )
            sdk.evaluate_policy(
                PolicyEvaluationRequest(
                    bucket="documents",
                    key="report.txt",
                    config=PolicyConfig(
                        policy="content",
                        embedding_rules=[configured_rule],
                    ),
                )
            )
            sdk.submit_policy_run(PolicyRunRequest(bucket="documents"))
            sdk.submit_policy_run(
                PolicyRunRequest(
                    bucket="documents",
                    config=PolicyConfig(
                        policy="content",
                        embedding_rules=[configured_rule],
                    ),
                )
            )

    for bodies in request_bodies.values():
        assert b'"embedding_rules"' not in bodies[0]
        assert b'"pii_rules"' not in bodies[0]
        assert b'"embedding_rules":[{' in bodies[1]
        assert b'"name":"active-report"' in bodies[1]


def test_policy_evaluation_accepts_an_older_v1_response_without_features() -> None:
    def policy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"X-Request-ID": "old-policy-response"},
            json={
                "schema_version": 1,
                "bucket": "documents",
                "key": "report.txt",
                "current_tier": "warm",
                "size": 10,
                "action": "stay",
                "destination_tier": None,
                "reason": "legacy policy response",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(policy_handler)) as http_client:
        with CogniStoreClient("https://sdk.test", http_client=http_client) as sdk:
            evaluation = sdk.evaluate_policy(
                PolicyEvaluationRequest(bucket="documents", key="report.txt")
            )

    assert evaluation.reason == "legacy policy response"
    assert evaluation.features is None
    assert evaluation.constraints == {}


@pytest.mark.parametrize("level", ["critical", None])
def test_importance_assignment_and_clear_round_trip(
    sdk_server: tuple[_ContractGateway, CogniStoreClient], level: Literal["critical"] | None,
) -> None:
    gateway, sdk = sdk_server
    sdk.put_object("hot", "documents", "report", b"retained")
    evaluation = sdk.set_importance(ImportanceChangeRequest(
        bucket="documents", key="report", level=level,
        actor_id="operator-44", provenance="approved classification",
        config=PolicyConfig(movement_constraints=MovementConstraintsConfig(
            minimum_residency_seconds={"hot": 3600},
            importance_tiers={"critical": ["hot"]},
        )),
    ))
    assert evaluation.action == "stay"
    assert evaluation.constraints["residency_active"] is False
    assert gateway.importance_requests[0].level == level
    controls = gateway.importance_requests[0].config.movement_constraints
    assert controls is not None
    assert controls.minimum_residency_seconds == {"hot": 3600}
    assert sdk.get_object("hot", "documents", "report").content == b"retained"


@pytest.mark.parametrize("invalid", [
    {"minimum_residency_seconds": {"hot": True}},
    {"minimum_residency_seconds": {"hot": -1}},
    {"minimum_residency_seconds": {"hot": 315360001}},
    {"minimum_residency_seconds": {" hot": 1}},
    {"importance_tiers": {"critical": ["hot", "hot"]}},
    {"importance_tiers": {"urgent": ["hot"]}},
])
def test_sdk_rejects_invalid_movement_controls_before_transport(invalid) -> None:
    with pytest.raises(PydanticValidationError):
        MovementConstraintsConfig.model_validate(invalid)


@pytest.mark.parametrize("values", [
    {"actor_id": " operator"}, {"provenance": "control\ncharacter"},
    {"actor_id": "é" * 129}, {"provenance": "€" * 700}, {"level": "urgent"},
])
def test_sdk_rejects_invalid_importance_attribution(values) -> None:
    request = {
        "bucket": "documents", "key": "report", "level": "high",
        "actor_id": "operator", "provenance": "classification",
    }
    with pytest.raises(PydanticValidationError):
        ImportanceChangeRequest.model_validate(request | values)


class _ContractGateway:
    """Small in-memory implementation of the API gateway protocol."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], tuple[bytes, str, str]] = {}
        self.path_calls: list[tuple[str, str, str, str]] = []
        self.page_cursors: list[str | None] = []
        self.ask_requests: list[api_models.AskRequest] = []
        self.policy_requests: list[api_models.PolicyEvaluationRequest] = []
        self.importance_requests: list[api_models.ImportanceChangeRequest] = []
        self.job_get_count = 0
        self.jobs = {
            _SCAN_JOB_ID: self._job(_SCAN_JOB_ID, "catalog.scan"),
            _POLICY_JOB_ID: self._job(_POLICY_JOB_ID, "policy.run"),
        }

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    @staticmethod
    def _job(
        job_id: UUID,
        job_type: Literal["catalog.scan", "policy.run"],
        *,
        status: Literal["queued", "running", "retrying", "succeeded", "failed"] = (
            "queued"
        ),
        retryable: bool | None = None,
        error_type: str | None = None,
    ) -> api_models.JobStatusResponse:
        return api_models.JobStatusResponse(
            job_id=job_id,
            correlation_id=_CORRELATION_ID,
            job_type=job_type,
            status=status,
            created_at=_NOW,
            updated_at=_NOW,
            status_url=f"/v1/jobs/{job_id}",
            attempt=0,
            retryable=retryable,
            error_type=error_type,
        )

    def set_job_status(
        self,
        job_id: UUID,
        status: Literal["queued", "running", "retrying", "succeeded", "failed"],
        *,
        retryable: bool | None = None,
        error_type: str | None = None,
    ) -> None:
        current = self.jobs[job_id]
        self.jobs[job_id] = self._job(
            job_id,
            current.job_type,
            status=status,
            retryable=retryable,
            error_type=error_type,
        )

    def seed_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.objects[(tier, bucket, key)] = (
            content,
            content_type,
            f"generation-{len(self.objects) + 1}",
        )

    def _stored(self, tier: str, bucket: str, key: str) -> tuple[bytes, str, str]:
        try:
            return self.objects[(tier, bucket, key)]
        except KeyError as exc:
            raise ResourceNotFoundError("object", f"{tier}/{bucket}/{key}") from exc

    @staticmethod
    def _resource(
        tier: str,
        bucket: str,
        key: str,
        stored: tuple[bytes, str, str],
    ) -> api_models.ObjectResource:
        content, content_type, generation = stored
        return api_models.ObjectResource(
            tier=tier,
            bucket=bucket,
            key=key,
            size=len(content),
            generation=generation,
            metadata={"mime": content_type},
        )

    def put_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        data: bytes,
        *,
        overwrite: bool,
        content_type: str | None,
    ) -> api_models.ObjectResource:
        self.path_calls.append(("put", tier, bucket, key))
        assert overwrite is True
        stored = (data, content_type or "application/octet-stream", "generation-put")
        self.objects[(tier, bucket, key)] = stored
        return self._resource(tier, bucket, key, stored)

    def stat_object(
        self, tier: str, bucket: str, key: str
    ) -> api_models.ObjectResource:
        self.path_calls.append(("head", tier, bucket, key))
        return self._resource(tier, bucket, key, self._stored(tier, bucket, key))

    def open_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        *,
        byte_range: str | None,
    ) -> APIObjectDownload:
        self.path_calls.append(("get", tier, bucket, key))
        stored = self._stored(tier, bucket, key)
        content, content_type, _generation = stored
        selected = content
        content_range = None
        if byte_range is not None:
            if not byte_range.startswith("bytes=") or "-" not in byte_range:
                raise APIRangeNotSatisfiableError(len(content))
            first_text, last_text = byte_range.removeprefix("bytes=").split("-", 1)
            try:
                first = int(first_text)
                last = int(last_text)
            except ValueError as exc:
                raise APIRangeNotSatisfiableError(len(content)) from exc
            if first < 0 or last < first or first >= len(content):
                raise APIRangeNotSatisfiableError(len(content))
            last = min(last, len(content) - 1)
            selected = content[first : last + 1]
            content_range = f"bytes {first}-{last}/{len(content)}"
        return APIObjectDownload(
            resource=self._resource(tier, bucket, key, stored),
            chunks=iter((selected,)),
            content_type=content_type,
            content_length=len(selected),
            content_range=content_range,
        )

    def delete_object(self, tier: str, bucket: str, key: str) -> None:
        self.path_calls.append(("delete", tier, bucket, key))
        self._stored(tier, bucket, key)
        del self.objects[(tier, bucket, key)]

    def _catalog_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        stored: tuple[bytes, str, str],
    ) -> api_models.CatalogObject:
        content, content_type, _generation = stored
        return api_models.CatalogObject(
            bucket=bucket,
            key=key,
            size=len(content),
            tier=tier,
            metadata={"mime": content_type},
        )

    def get_catalog_object(self, bucket: str, key: str) -> api_models.CatalogObject:
        for (tier, candidate_bucket, candidate_key), stored in self.objects.items():
            if (candidate_bucket, candidate_key) == (bucket, key):
                return self._catalog_object(tier, bucket, key, stored)
        raise ResourceNotFoundError("catalog object", f"{bucket}/{key}")

    def list_catalog_objects(
        self,
        *,
        bucket: str,
        prefix: str,
        tier: str | None,
        limit: int,
        cursor: str | None,
    ) -> api_models.CatalogObjectPage:
        self.page_cursors.append(cursor)
        if prefix == "repeat-cursor":
            return api_models.CatalogObjectPage(
                items=[],
                page=api_models.PageMetadata(limit=limit, next_cursor="loop"),
            )

        matches = sorted(
            (
                self._catalog_object(item_tier, item_bucket, key, stored)
                for (item_tier, item_bucket, key), stored in self.objects.items()
                if item_bucket == bucket
                and key.startswith(prefix)
                and (tier is None or item_tier == tier)
            ),
            key=lambda item: item.key,
        )
        start = int(cursor) if cursor is not None else 0
        selected = matches[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(matches) else None
        return api_models.CatalogObjectPage(
            items=selected,
            page=api_models.PageMetadata(limit=limit, next_cursor=next_cursor),
        )

    def ask(self, request: api_models.AskRequest) -> api_models.AskResponse:
        self.ask_requests.append(request)
        return api_models.AskResponse(
            schema_version=1,
            mode="metadata",
            active_signals=["metadata"],
            results=[],
            providers=[
                api_models.ProviderDiagnosticResponse(
                    component="metadata", state="succeeded"
                ),
                api_models.ProviderDiagnosticResponse(
                    component="keyword", state="missing"
                ),
                api_models.ProviderDiagnosticResponse(
                    component="vector", state="missing"
                ),
                api_models.ProviderDiagnosticResponse(
                    component="generation", state="not_requested"
                ),
            ],
            generation_status="not_requested",
        )

    def evaluate_policy(
        self, request: api_models.PolicyEvaluationRequest
    ) -> api_models.PolicyEvaluationResponse:
        self.policy_requests.append(request)
        catalog_object = self.get_catalog_object(request.bucket, request.key)
        should_move = catalog_object.size > request.config.threshold
        return api_models.PolicyEvaluationResponse(
            bucket=catalog_object.bucket,
            key=catalog_object.key,
            current_tier=catalog_object.tier,
            size=catalog_object.size,
            action="move" if should_move else "stay",
            destination_tier="warm" if should_move else None,
            reason="contract fixture decision",
            features=api_models.PolicyFeaturesResponse(
                schema_version=1,
                mime=api_models.MimePolicyFeatureResponse(
                    state="missing",
                    value=None,
                    provenance=api_models.PolicyFeatureProvenanceResponse(
                        source="catalog_metadata",
                        source_version=1,
                        content_sha256=None,
                        details={"reason": "mime_missing"},
                    ),
                ),
                embeddings=[
                    api_models.EmbeddingPolicyFeatureResponse(
                        name=rule.name,
                        query=rule.query,
                        state="missing",
                        similarity=None,
                        provenance=api_models.PolicyFeatureProvenanceResponse(
                            source="embedding_similarity",
                            source_version=1,
                            content_sha256=None,
                            details={"reason": "provider_missing"},
                        ),
                    )
                    for rule in request.config.embedding_rules
                ],
            ),
        )

    def set_importance(
        self, request: api_models.ImportanceChangeRequest, *, correlation_id: str,
    ) -> api_models.PolicyEvaluationResponse:
        self.importance_requests.append(request)
        catalog_object = self.get_catalog_object(request.bucket, request.key)
        return api_models.PolicyEvaluationResponse(
            bucket=request.bucket, key=request.key, current_tier=catalog_object.tier,
            size=catalog_object.size, action="stay", reason="importance reevaluated",
            constraints={
                "importance": None if request.level is None else {
                    "level": request.level, "actor_id": request.actor_id,
                    "provenance": request.provenance,
                },
                "residency_active": False,
            },
        )

    async def submit_catalog_scan(
        self, _request: api_models.CatalogScanRequest
    ) -> api_models.JobStatusResponse:
        self.jobs[_SCAN_JOB_ID] = self._job(_SCAN_JOB_ID, "catalog.scan")
        return self.jobs[_SCAN_JOB_ID]

    async def submit_policy_run(
        self, _request: api_models.PolicyRunRequest
    ) -> api_models.JobStatusResponse:
        self.jobs[_POLICY_JOB_ID] = self._job(_POLICY_JOB_ID, "policy.run")
        return self.jobs[_POLICY_JOB_ID]

    def get_job(self, job_id: str) -> api_models.JobStatusResponse:
        self.job_get_count += 1
        try:
            return self.jobs[UUID(job_id)]
        except (KeyError, ValueError) as exc:
            raise ResourceNotFoundError("job", job_id) from exc


@pytest.fixture
def sdk_server() -> Iterator[tuple[_ContractGateway, CogniStoreClient]]:
    gateway = _ContractGateway()
    with TestClient(create_app(gateway)) as test_client:
        sdk = CogniStoreClient(
            "http://testserver",
            http_client=test_client,
            default_headers={"X-Request-ID": "sdk-contract-request"},
            poll_interval=0.001,
            max_poll_interval=0.001,
        )
        yield gateway, sdk
        sdk.close()


def test_all_public_api_operations_round_trip_as_typed_models(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    gateway, sdk = sdk_server
    content = b"hello sdk"

    health = sdk.get_health()
    created = sdk.put_object(
        "hot",
        "documents",
        "reports/example.txt",
        content,
        content_type="text/plain",
    )
    head = sdk.head_object("hot", "documents", "reports/example.txt")
    downloaded = sdk.get_object("hot", "documents", "reports/example.txt")
    page = sdk.list_catalog_objects("documents", prefix="reports/")
    catalog_object = sdk.get_catalog_object("documents", "reports/example.txt")
    answer = sdk.ask(
        AskRequest(
            text="find the example",
            retrieval_mode="metadata+vector",
        )
    )
    evaluation = sdk.evaluate_policy(
        PolicyEvaluationRequest(
            bucket="documents",
            key="reports/example.txt",
            config=PolicyConfig(
                policy="content",
                threshold=5,
                allowed_tiers=["hot", "warm"],
                embedding_rules=[
                    EmbeddingPolicyRuleConfig(
                        name="active-report",
                        query="frequently used project report",
                        minimum_similarity=0.8,
                        destination_tier="hot",
                    )
                ],
            ),
        )
    )
    scan = sdk.submit_catalog_scan(
        CatalogScanRequest(tier="hot", bucket="documents", prefix="reports/")
    )
    policy_run = sdk.submit_policy_run(
        PolicyRunRequest(
            bucket="documents",
            prefix="reports/",
            config=PolicyConfig(allowed_tiers=["hot", "warm"]),
        )
    )
    fetched_job = sdk.get_job_status(scan.job_id)
    deleted = sdk.delete_object("hot", "documents", "reports/example.txt")

    assert isinstance(health, HealthResponse)
    assert (health.status, health.api_version) == ("ok", 1)
    assert isinstance(created, ObjectResource)
    assert (created.key, created.size) == ("reports/example.txt", len(content))
    assert isinstance(head, HeadObjectResponse)
    assert (head.content_length, head.accept_ranges) == (len(content), "bytes")
    assert head.request_id == "sdk-contract-request"
    assert isinstance(downloaded, ObjectDownload)
    assert (downloaded.content, downloaded.status_code) == (content, 200)
    assert isinstance(page, CatalogObjectPage)
    assert len(page.items) == 1 and isinstance(page.items[0], CatalogObject)
    assert isinstance(catalog_object, CatalogObject)
    assert catalog_object.metadata == {"mime": "text/plain"}
    assert isinstance(answer, AskResponse)
    assert answer.active_signals == ["metadata"]
    assert gateway.ask_requests[0].text == "find the example"
    assert gateway.ask_requests[0].retrieval_mode == "metadata+vector"
    assert isinstance(evaluation, PolicyEvaluationResponse)
    assert (evaluation.action, evaluation.destination_tier) == ("move", "warm")
    assert isinstance(evaluation.features, PolicyFeatures)
    assert isinstance(evaluation.features.mime, MimePolicyFeature)
    assert isinstance(
        evaluation.features.mime.provenance,
        PolicyFeatureProvenance,
    )
    assert len(evaluation.features.embeddings) == 1
    assert isinstance(evaluation.features.embeddings[0], EmbeddingPolicyFeature)
    assert evaluation.features.embeddings[0].name == "active-report"
    assert evaluation.features.embeddings[0].state == "missing"
    assert len(gateway.policy_requests) == 1
    assert gateway.policy_requests[0].config.embedding_rules[0].name == "active-report"
    assert isinstance(scan, JobStatus) and scan.job_type == "catalog.scan"
    assert isinstance(policy_run, JobStatus) and policy_run.job_type == "policy.run"
    assert isinstance(fetched_job, JobStatus) and fetched_job.job_id == scan.job_id
    assert isinstance(deleted, DeleteObjectResponse)
    assert deleted.request_id == "sdk-contract-request"


def test_object_paths_encode_reserved_unicode_and_dot_segments(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    gateway, sdk = sdk_server
    tier = "hot +#%"
    bucket = "bucket ?#%"
    key = "folder name/café +?#%/../."

    created = sdk.put_object(tier, bucket, key, b"encoded")
    downloaded = sdk.get_object(tier, bucket, key)
    catalog_object = sdk.get_catalog_object(bucket, key)

    assert (created.tier, created.bucket, created.key) == (tier, bucket, key)
    assert downloaded.content == b"encoded"
    assert catalog_object.key == key
    assert gateway.path_calls[:2] == [
        ("put", tier, bucket, key),
        ("get", tier, bucket, key),
    ]


def test_object_range_and_required_header_contracts(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    _gateway, sdk = sdk_server
    sdk.put_object("hot", "documents", "range.bin", b"012345", content_type="x/test")

    head = sdk.head_object("hot", "documents", "range.bin")
    partial = sdk.get_object(
        "hot", "documents", "range.bin", byte_range="bytes=1-3"
    )

    assert head.content_length == 6
    assert head.content_type == "x/test"
    assert head.etag.startswith('"') and head.etag.endswith('"')
    assert partial.content == b"123"
    assert partial.content_length == 3
    assert partial.content_range == "bytes 1-3/6"
    assert partial.status_code == 206
    assert partial.accept_ranges == "bytes"

    with pytest.raises(RangeNotSatisfiableError) as captured:
        sdk.get_object(
            "hot", "documents", "range.bin", byte_range="bytes=20-30"
        )
    assert captured.value.code == "range_not_satisfiable"
    assert captured.value.status_code == 416
    assert captured.value.headers["content-range"] == "bytes */6"


def test_bodyless_head_error_still_uses_typed_status_and_response_metadata(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    _gateway, sdk = sdk_server

    with pytest.raises(NotFoundError) as captured:
        sdk.head_object("hot", "documents", "missing.txt")

    error = captured.value
    assert error.status_code == 404
    assert error.code == "http_404"
    assert error.request_id == "sdk-contract-request"
    assert error.details == ()
    assert error.headers["x-request-id"] == "sdk-contract-request"


def test_catalog_iterator_follows_pages_and_rejects_a_repeated_cursor(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    gateway, sdk = sdk_server
    for index in range(5):
        gateway.seed_object(
            "hot",
            "documents",
            f"reports/{index}.txt",
            str(index).encode(),
        )

    items = list(
        sdk.iter_catalog_objects(
            "documents", prefix="reports/", tier="hot", limit=2
        )
    )

    assert [item.key for item in items] == [
        "reports/0.txt",
        "reports/1.txt",
        "reports/2.txt",
        "reports/3.txt",
        "reports/4.txt",
    ]
    assert gateway.page_cursors == [None, "2", "4"]

    gateway.page_cursors.clear()
    with pytest.raises(ResponseContractError, match="repeated catalog cursor"):
        list(
            sdk.iter_catalog_objects(
                "documents", prefix="repeat-cursor", limit=2
            )
        )
    assert gateway.page_cursors == [None, "loop"]


def test_server_validation_error_exposes_typed_details(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    _gateway, sdk = sdk_server

    with pytest.raises(ValidationError) as captured:
        sdk.list_catalog_objects("", limit=0)

    error = captured.value
    assert error.status_code == 422
    assert error.code == "validation_error"
    assert error.request_id == "sdk-contract-request"
    assert len(error.details) == 2
    assert all(isinstance(issue, ValidationIssue) for issue in error.details)
    assert {issue.location for issue in error.details} == {
        "query.bucket",
        "query.limit",
    }


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (413, PayloadTooLargeError),
        (416, RangeNotSatisfiableError),
        (422, ValidationError),
        (429, APIError),
        (500, ServerError),
        (503, ServiceUnavailableError),
    ],
)
def test_error_statuses_map_to_typed_exceptions_with_metadata(
    status_code: int,
    error_type: type[APIError],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "schema_version": 1,
                "request_id": "body-request",
                "error": {
                    "code": "fixture_error",
                    "message": "fixture message",
                    "retryable": True,
                    "details": [
                        {
                            "location": "body.value",
                            "message": "invalid value",
                            "type": "fixture",
                        }
                    ],
                },
            },
            headers={"X-Request-ID": "header-request", "Retry-After": "2.5"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = CogniStoreClient("https://sdk.test", http_client=http_client)
        with pytest.raises(error_type) as captured:
            sdk.get_health()

    error = captured.value
    assert type(error) is error_type
    assert error.code == "fixture_error"
    assert error.message == "fixture message"
    assert error.request_id == "header-request"
    assert error.retryable is True
    assert error.retry_after == 2.5
    assert error.details == (
        ValidationIssue(
            location="body.value", message="invalid value", type="fixture"
        ),
    )
    assert error.headers["x-request-id"] == "header-request"


def test_forward_additions_are_ignored_but_malformed_successes_fail() -> None:
    def additive_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "future_server_field": {"new": True},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(additive_handler)) as http_client:
        sdk = CogniStoreClient("https://sdk.test", http_client=http_client)
        response = sdk.get_health()

    assert response == HealthResponse(status="ok", api_version=1)
    assert response.model_dump() == {"status": "ok", "api_version": 1}

    for malformed_body in (b"not-json", b'{"status":"ok","api_version":2}'):
        def malformed_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=malformed_body,
                headers={"X-Request-ID": "malformed-request"},
            )

        with httpx.Client(
            transport=httpx.MockTransport(malformed_handler)
        ) as http_client:
            sdk = CogniStoreClient("https://sdk.test", http_client=http_client)
            with pytest.raises(ResponseContractError) as captured:
                sdk.get_health()

        assert captured.value.status_code == 200
        assert captured.value.request_id == "malformed-request"
        assert isinstance(captured.value.cause, (PydanticValidationError, ValueError))


def test_malformed_binary_and_head_headers_raise_contract_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        common_headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": "1",
            "ETag": '"etag"',
            "Accept-Ranges": "bytes",
            "X-Request-ID": "headers-request",
        }
        if request.method == "HEAD":
            common_headers["Accept-Ranges"] = "items"
            return httpx.Response(200, headers=common_headers)
        return httpx.Response(206, content=b"x", headers=common_headers)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = CogniStoreClient("https://sdk.test", http_client=http_client)
        with pytest.raises(ResponseContractError, match="HEAD response headers"):
            sdk.head_object("hot", "bucket", "key")
        with pytest.raises(ResponseContractError, match="missing Content-Range"):
            sdk.get_object("hot", "bucket", "key", byte_range="bytes=0-0")


@pytest.mark.parametrize(
    ("failure_type", "sdk_error_type"),
    [
        (httpx.ReadTimeout, RequestTimeoutError),
        (httpx.ConnectError, TransportError),
    ],
)
def test_timeout_and_transport_failures_preserve_their_causes(
    failure_type: type[httpx.HTTPError],
    sdk_error_type: type[TransportError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure_type("network fixture failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = CogniStoreClient("https://sdk.test", http_client=http_client)
        with pytest.raises(sdk_error_type) as captured:
            sdk.get_health()

    assert type(captured.value) is sdk_error_type
    assert isinstance(captured.value.cause, failure_type)
    assert captured.value.__cause__ is captured.value.cause


def test_action_polling_succeeds_fails_and_times_out(
    sdk_server: tuple[_ContractGateway, CogniStoreClient],
) -> None:
    gateway, sdk = sdk_server

    scan = sdk.submit_catalog_scan(
        CatalogScanRequest(tier="hot", bucket="documents")
    )
    gateway.set_job_status(scan.job_id, "succeeded")
    succeeded = sdk.wait_for_job(scan, timeout=0.1)
    assert succeeded.status == "succeeded"
    assert succeeded.job_id == scan.job_id

    policy_run = sdk.submit_policy_run(
        PolicyRunRequest(
            bucket="documents",
            config=PolicyConfig(allowed_tiers=["hot", "warm"]),
        )
    )
    gateway.set_job_status(
        policy_run.job_id,
        "failed",
        retryable=False,
        error_type="PolicyFixtureError",
    )
    with pytest.raises(JobFailedError) as failed:
        sdk.wait_for_job(policy_run, timeout=0.1)
    assert failed.value.job.status == "failed"
    assert failed.value.job.error_type == "PolicyFixtureError"
    assert sdk.wait_for_job(failed.value.job, raise_on_failure=False).status == "failed"

    gateway.set_job_status(scan.job_id, "running")
    calls_before_timeout = gateway.job_get_count
    with pytest.raises(PollingTimeoutError) as timed_out:
        sdk.wait_for_job(scan.job_id, timeout=0)
    assert timed_out.value.timeout == 0
    assert timed_out.value.last_status is None
    assert gateway.job_get_count == calls_before_timeout


def test_polling_deadline_rejects_a_terminal_response_that_arrives_too_late() -> None:
    terminal_job = _ContractGateway._job(
        _SCAN_JOB_ID,
        "catalog.scan",
        status="succeeded",
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        request_timeouts = request.extensions["timeout"]
        assert set(request_timeouts) == {"connect", "read", "write", "pool"}
        assert all(
            0 < component <= 0.005 for component in request_timeouts.values()
        )
        time.sleep(0.02)
        return httpx.Response(200, json=terminal_job)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = CogniStoreClient(
            "https://sdk.test",
            timeout=30,
            http_client=http_client,
        )
        with pytest.raises(PollingTimeoutError) as captured:
            sdk.wait_for_job(_SCAN_JOB_ID, timeout=0.005)

    assert captured.value.timeout == 0.005
    assert captured.value.last_status is not None
    assert captured.value.last_status.status == "succeeded"


def test_client_configuration_is_validated_normalized_and_immutable() -> None:
    config = ClientConfig(
        "https://sdk.test/root/",
        timeout=2,
        default_headers={"Authorization": "Bearer token"},
        poll_interval=0.25,
        max_poll_interval=1,
    )

    assert config.base_url == "https://sdk.test/root"
    assert config.timeout == 2.0
    assert config.default_headers == {"Authorization": "Bearer token"}
    with pytest.raises(TypeError):
        config.default_headers["X-New"] = "value"  # type: ignore[index]

    with pytest.raises(ValueError, match="absolute HTTP or HTTPS URL"):
        ClientConfig("sdk.test")
    for malformed_url in (
        "https://",
        "https:///path",
        "https://sdk.test/path with space",
        "https://sdk.test:abc",
        "https://[::1",
        "https://sdk.test/\x00",
    ):
        with pytest.raises(ValueError, match="valid absolute|absolute HTTP"):
            ClientConfig(malformed_url)
    with pytest.raises(ValueError, match="invalid port"):
        ClientConfig("https://sdk.test:65536")
    for malformed_url in (
        "https://sdk.test?",
        "https://sdk.test?query=value",
        "https://sdk.test#",
        "https://sdk.test#fragment",
    ):
        with pytest.raises(ValueError, match="query string or fragment"):
            ClientConfig(malformed_url)
    for invalid_headers in (
        {"Bad Header": "value"},
        {"X-Test": "line-one\r\nline-two"},
        {"X-Test": "nul\x00value"},
        {"X-Test": " leading"},
        {"X-Test": "trailing\t"},
    ):
        with pytest.raises(ValueError, match="invalid"):
            ClientConfig("https://sdk.test", default_headers=invalid_headers)

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = CogniStoreClient("https://sdk.test", http_client=http_client)
        with pytest.raises(ValueError, match="request headers"):
            sdk.put_object(
                "hot",
                "bucket",
                "key",
                b"content",
                content_type="text/plain\r\nX-Injected: value",
            )
    assert calls == 0
    with pytest.raises(ValueError, match="timeout must be a finite positive"):
        ClientConfig("https://sdk.test", timeout=0)
    for invalid_timeout in (
        httpx.Timeout(-1),
        httpx.Timeout(float("nan")),
        httpx.Timeout(connect=0, read=1, write=1, pool=1),
    ):
        with pytest.raises(ValueError, match=r"timeout\..*finite positive"):
            ClientConfig("https://sdk.test", timeout=invalid_timeout)
    with pytest.raises(TypeError, match="timeout.connect must be seconds"):
        ClientConfig(
            "https://sdk.test",
            timeout=httpx.Timeout(connect="1", read=1, write=1, pool=1),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="greater than or equal"):
        ClientConfig(
            "https://sdk.test", poll_interval=2, max_poll_interval=1
        )


def test_request_models_are_strict_and_forbid_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        AskRequest.model_validate({"text": "query", "limit": "1"})
    with pytest.raises(PydanticValidationError):
        AskRequest.model_validate({"text": "query", "future_option": True})
    with pytest.raises(PydanticValidationError):
        AskRequest.model_validate({"text": "query", "retrieval_mode": "keyword"})


def test_sdk_embedding_policy_rules_enforce_the_api_request_constraints() -> None:
    rule = EmbeddingPolicyRuleConfig(
        name="active-report",
        query="frequently used project report",
        minimum_similarity=0.8,
        destination_tier="hot",
    )

    config = PolicyConfig(
        policy="content",
        allowed_tiers=["hot", "warm"],
        embedding_rules=[rule],
    )

    assert config.embedding_rules == [rule]
    with pytest.raises(PydanticValidationError, match="require the content policy"):
        PolicyConfig(embedding_rules=[rule])
    with pytest.raises(PydanticValidationError, match="names must be unique"):
        PolicyConfig(
            policy="content",
            allowed_tiers=["hot", "warm"],
            embedding_rules=[rule, rule],
        )
    with pytest.raises(PydanticValidationError, match="must be allowed"):
        PolicyConfig(
            policy="content",
            allowed_tiers=["warm"],
            embedding_rules=[rule],
        )
    with pytest.raises(PydanticValidationError, match="outer whitespace"):
        EmbeddingPolicyRuleConfig(
            name=" active-report",
            query="frequently used project report",
            minimum_similarity=0.8,
            destination_tier="hot",
        )


def test_openapi_operation_ids_and_http_methods_match_sdk_method_coverage() -> None:
    expected = {
        "getHealth": ("get", "/healthz", "get_health"),
        "putObject": (
            "put",
            "/v1/objects/{tier}/{bucket}/{key}",
            "put_object",
        ),
        "headObject": (
            "head",
            "/v1/objects/{tier}/{bucket}/{key}",
            "head_object",
        ),
        "getObject": (
            "get",
            "/v1/objects/{tier}/{bucket}/{key}",
            "get_object",
        ),
        "deleteObject": (
            "delete",
            "/v1/objects/{tier}/{bucket}/{key}",
            "delete_object",
        ),
        "listCatalogObjects": (
            "get",
            "/v1/catalog/objects",
            "list_catalog_objects",
        ),
        "getCatalogObject": (
            "get",
            "/v1/catalog/objects/{bucket}/{key}",
            "get_catalog_object",
        ),
        "setObjectImportance": ("post", "/v1/catalog/importance", "set_importance"),
        "ask": ("post", "/v1/ask", "ask"),
        "evaluatePolicy": (
            "post",
            "/v1/policies/evaluate",
            "evaluate_policy",
        ),
        "previewPolicyDecision": (
            "post", "/v1/policies/preview", "preview_policy_decision",
        ),
        "listPolicyDecisions": (
            "get", "/v1/policy-decisions", "list_policy_decisions",
        ),
        "getPolicyDecision": (
            "get", "/v1/policy-decisions/{decision_id}", "get_policy_decision",
        ),
        "submitCatalogScan": (
            "post",
            "/v1/actions/catalog-scans",
            "submit_catalog_scan",
        ),
        "submitPolicyRun": (
            "post",
            "/v1/actions/policy-runs",
            "submit_policy_run",
        ),
        "getJobStatus": ("get", "/v1/jobs/{job_id}", "get_job_status"),
    }
    schema = create_app(_ContractGateway()).openapi()
    actual: dict[str, tuple[str, str]] = {}
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method in {"get", "put", "post", "delete", "head", "patch"}:
                actual[operation["operationId"]] = (method, path)

    assert set(actual) == SUPPORTED_OPERATION_IDS
    assert set(expected) == SUPPORTED_OPERATION_IDS
    assert actual == {
        operation_id: (method, path)
        for operation_id, (method, path, _sdk_method) in expected.items()
    }
    assert all(
        callable(getattr(CogniStoreClient, sdk_method, None))
        for _method, _path, sdk_method in expected.values()
    )
