from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import (
    PolicyConfig,
    PolicyEvaluationRequest,
    PolicyFeaturesResponse,
)
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
    StorageImpactEstimator,
)
from cognistore.core.mover import Mover
from cognistore.core.policy import PolicyDecision
from cognistore.core.policy_features import CatalogPolicyFeatureLoader
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.topology import PlacementConstraints
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.sdk.models import PolicyFeatures as SDKPolicyFeatures

AS_OF = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def test_default_loader_omits_placement_estimates() -> None:
    record = ObjectRecord("documents", "notes.txt", 20, "hot")
    features = CatalogPolicyFeatureLoader().load([record], as_of=AS_OF)[
        (record.bucket, record.key)
    ]
    assert features.placement_estimates is None
    assert "placement_estimates" not in features.to_dict()
    api = PolicyFeaturesResponse.model_validate(features.to_dict())
    assert "placement_estimates" not in api.model_dump(mode="json")
    sdk = SDKPolicyFeatures.model_validate(api.model_dump(mode="json"))
    assert sdk.placement_estimates is None


@pytest.mark.parametrize(
    "configuration",
    [
        {"impact_estimator": StorageImpactEstimator({})},
        {"estimation_catalog": Catalog()},
        {"estimation_workload_factory": lambda record: None},
        {"estimation_constraints": PlacementConstraints()},
    ],
)
def test_incomplete_estimation_injection_is_rejected(configuration: dict) -> None:
    with pytest.raises(ValueError, match="impact estimate|impact estimator"):
        CatalogPolicyFeatureLoader(**configuration)


def _catalog_with_placements() -> Catalog:
    catalog = Catalog()
    for tier in ("hot", "warm"):
        catalog.register_tier(tier)
    catalog.register_pool(
        "hot-us",
        "hot",
        region="us-west-2",
        members=("primary",),
    )
    catalog.register_pool(
        "warm-us",
        "warm",
        region="us-west-2",
        members=("archive",),
    )
    catalog.register_pool(
        "warm-eu",
        "warm",
        region="eu-west-1",
        members=("outside-allowed-region",),
    )
    catalog.upsert("documents", "notes.txt", 20, tier="hot")
    catalog.assign_pool("documents", "notes.txt", "hot-us")
    return catalog


def _estimation_loader(catalog: Catalog) -> CatalogPolicyFeatureLoader:
    return CatalogPolicyFeatureLoader(
        access_catalog=catalog,
        impact_estimator=StorageImpactEstimator({}),
        estimation_catalog=catalog,
        estimation_workload_factory=lambda record: EstimationWorkload(duration_hours=730),
        estimation_constraints=PlacementConstraints(allowed_regions=("us-west-2",)),
    )


def test_projection_preserves_unknowns_constraints_and_typed_explanations() -> None:
    catalog = _catalog_with_placements()
    loader = _estimation_loader(catalog)
    record = catalog.get("documents", "notes.txt")
    assert record is not None

    features = loader.load([record], as_of=AS_OF)[("documents", "notes.txt")]
    estimates = features.placement_estimates
    assert estimates is not None
    assert estimates.current.pool_id == "hot-us"
    assert [candidate.pool_id for candidate in estimates.candidates] == ["hot-us", "warm-us"]
    assert estimates.current.cost.value is None
    assert estimates.current.carbon.value is None
    assert estimates.current.workload.stored_bytes == record.size
    assert estimates.current.workload.read_requests is None
    assert estimates.current.as_of == features.access.as_of
    assert features.to_dict() == loader.load([record], as_of=AS_OF)[
        (record.bucket, record.key)
    ].to_dict()

    api = PolicyFeaturesResponse.model_validate(features.to_dict())
    sdk = SDKPolicyFeatures.model_validate(api.model_dump(mode="json"))
    assert sdk.placement_estimates is not None
    assert sdk.placement_estimates.model_dump(mode="json") == estimates.to_dict()
    assert sdk.placement_estimates.current.cost.state == "unavailable"
    assert sdk.placement_estimates.current.cost.known_subtotal == "0"
    assert sdk.placement_estimates.current.cost.value is None


def test_calibrated_values_bounds_and_evidence_survive_api_sdk_roundtrip() -> None:
    catalog = _catalog_with_placements()
    catalog.upsert("documents", "notes.txt", 1073741824, tier="hot")

    def assumption(unit, value=1, lower=1, upper=1):
        return RateAssumption(
            value=value,
            unit=unit,
            source="synthetic-integration-fixture",
            source_version="fixture-v1",
            effective_from="2026-09-01T00:00:00Z",
            effective_until="2026-10-01T00:00:00Z",
            lower=lower,
            upper=upper,
        )

    profile = EstimationProfile(
        version="calibrated-fixture-v1",
        backend="synthetic-backend",
        rates={name: assumption(unit) for name, unit in RATE_UNITS.items()},
        calibration={"storage_price": assumption("multiplier", 2, 1, 3)},
    )
    loader = CatalogPolicyFeatureLoader(
        impact_estimator=StorageImpactEstimator({"hot-us": profile, "warm-us": profile}),
        estimation_catalog=catalog,
        estimation_workload_factory=lambda record: EstimationWorkload(
            duration_hours=730,
            read_requests=0,
            write_requests=0,
            transfer_bytes=0,
            retrieval_bytes=0,
        ),
        estimation_constraints=PlacementConstraints(allowed_regions=("us-west-2",)),
    )
    record = catalog.get("documents", "notes.txt")
    assert record is not None
    features = loader.load([record], as_of=AS_OF)[(record.bucket, record.key)]
    api = PolicyFeaturesResponse.model_validate(features.to_dict())
    sdk = SDKPolicyFeatures.model_validate(api.model_dump(mode="json"))
    assert features.placement_estimates is not None
    assert sdk.placement_estimates is not None
    assert sdk.placement_estimates.model_dump(mode="json") == (
        features.placement_estimates.to_dict()
    )
    current = sdk.placement_estimates.current
    assert (current.cost.value, current.cost.lower, current.cost.upper) == ("2", "1", "3")
    assert current.cost.state == "available"
    assert current.cost.uncertainty == "bounded"
    assert current.cost.components[0].value == "2"
    assert (current.carbon.value, current.carbon.lower, current.carbon.upper) == ("1", "1", "1")
    assert current.rates[0]["calibration"]["source_version"] == "fixture-v1"


def test_estimates_reach_policy_inputs_and_preview_explanations(tmp_path: Path) -> None:
    catalog = _catalog_with_placements()
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    observed = []

    class ImpactAwarePolicy:
        def evaluate_features(self, record, features):
            observed.append(features.placement_estimates)
            assert features.placement_estimates is not None
            if features.placement_estimates.current.cost.value is None:
                return PolicyDecision("stay", "current cost unavailable")
            raise AssertionError("unknown current cost must not become zero")

    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        ImpactAwarePolicy(),
        feature_loader=_estimation_loader(catalog),
    )
    evaluations = runner.preview_once("documents")
    assert len(evaluations) == 1
    assert evaluations[0].reason == "current cost unavailable"
    assert observed[0] is evaluations[0].features.placement_estimates
    assert evaluations[0].to_mapping()["features"]["placement_estimates"] == (
        observed[0].to_dict()
    )
    assert runner.plan_once("documents") == []
    events = catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
    )
    assert len(events) == 1
    assert events[0].details["dataset"]["features"]["placement_estimates"] == (
        observed[-1].to_dict()
    )


def test_gateway_evaluation_keeps_complete_estimation_evidence(tmp_path: Path) -> None:
    catalog = _catalog_with_placements()
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    gateway = CogniStoreGateway(
        catalog,
        drivers,
        feature_loader=_estimation_loader(catalog),
    )
    response = gateway.evaluate_policy(
        PolicyEvaluationRequest(
            bucket="documents",
            key="notes.txt",
            config=PolicyConfig(allowed_tiers=["hot", "warm"]),
        )
    )
    assert response.features is not None
    estimates = response.features.placement_estimates
    assert estimates is not None
    assert estimates.current.cost.value is None
    assert estimates.current.carbon.value is None
    assert estimates.current.rates
    assert estimates.current.formulas["version"] == "storage-impact-v1"
    assert [candidate.pool_id for candidate in estimates.candidates] == ["hot-us", "warm-us"]
