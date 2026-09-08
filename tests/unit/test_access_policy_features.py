from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognistore.api.models import PolicyFeaturesResponse
from cognistore.core.access import AccessConfig, AccessEvent
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.policy_features import CatalogPolicyFeatureLoader
from cognistore.policy_feature_runtime import (
    PolicyFeatureRuntimeConfigError,
    load_access_config,
    load_policy_feature_loader,
)
from cognistore.sdk.models import PolicyFeatures as SDKPolicyFeatures

AS_OF = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def test_projection_exposes_deterministic_access_and_typed_contracts() -> None:
    catalog = Catalog()
    record = ObjectRecord("documents", "notes.txt", 20, "hot")
    catalog.append_access_event(
        AccessEvent.create(
            kind="read",
            bucket=record.bucket,
            key=record.key,
            operation_id="fixture-read",
            occurred_at="2026-09-08T11:45:00Z",
            sample_rate=0.5,
        )
    )
    loader = CatalogPolicyFeatureLoader(
        access_catalog=catalog,
        access_config=AccessConfig(windows_seconds=(600, 3600)),
    )

    features = loader.load([record], as_of=AS_OF)[("documents", "notes.txt")]
    assert (
        features.to_dict()
        == loader.load([record], as_of=AS_OF)[("documents", "notes.txt")].to_dict()
    )
    response = PolicyFeaturesResponse.model_validate(features.to_dict())
    sdk = SDKPolicyFeatures.model_validate(response.model_dump(mode="json"))
    assert sdk.access is not None
    assert sdk.access.freshness == "fresh"
    assert sdk.access.recency_seconds == 900
    assert sdk.access.windows["600"].read == 0
    assert sdk.access.windows["3600"].read == 1
    assert sdk.access.estimated_windows["3600"].read == 2
    assert sdk.access.sampling.sampled
    assert sdk.access.partial


def test_missing_and_unavailable_history_remain_explicit_and_do_not_leak_errors() -> None:
    class UnavailableCatalog(Catalog):
        def aggregate_access_events(self, *args, **kwargs):
            raise RuntimeError("secret backend credentials")

    record = ObjectRecord("documents", "notes.txt", 20, "hot")
    for catalog, state in [
        (None, "missing"),
        (Catalog(), "missing"),
        (UnavailableCatalog(), "unavailable"),
    ]:
        loader = CatalogPolicyFeatureLoader(access_catalog=catalog)
        feature = loader.load([record], as_of=AS_OF)[("documents", "notes.txt")].access
        assert feature is not None
        assert feature.freshness == state
        assert feature.missing and feature.partial
        assert feature.recency_seconds is None
        assert "secret" not in str(feature.to_dict())


def test_all_records_use_one_evaluation_instant() -> None:
    records = [ObjectRecord("b", key, 1, "hot") for key in ("a", "b")]
    result = CatalogPolicyFeatureLoader().load(records)
    assert result[("b", "a")].access.as_of == result[("b", "b")].access.as_of


def test_runtime_configures_access_for_policy_loader(tmp_path: Path) -> None:
    path = tmp_path / "drivers.yaml"
    path.write_text("""tiers: {}
access_history:
  windows_seconds: [3600, 60]
  retention_seconds: 86400
  sample_rate: 0.25
  freshness_seconds: 120
""")
    catalog = Catalog()
    loader = load_policy_feature_loader(path, catalog)
    assert loader.access_catalog is catalog
    assert loader.access_config == AccessConfig(
        windows_seconds=(60, 3600),
        retention_seconds=86400,
        sample_rate=0.25,
        freshness_seconds=120,
    )


@pytest.mark.parametrize(
    "block",
    [
        "null",
        "[]",
        "{unknown: 1}",
        "{windows_seconds: 60}",
        "{windows_seconds: [true]}",
        "{windows_seconds: [60, 60]}",
        "{windows_seconds: [100], retention_seconds: 60}",
        "{sample_rate: .nan}",
        "{sample_rate: 0}",
        "{sample_rate: true}",
        "{freshness_seconds: -1}",
        "{sample_rate: 0.5, sample_rate: 1}",
    ],
)
def test_invalid_access_config_is_rejected(tmp_path: Path, block: str) -> None:
    path = tmp_path / "drivers.yaml"
    path.write_text(f"access_history: {block}\n")
    with pytest.raises(PolicyFeatureRuntimeConfigError):
        load_access_config(path)


def test_missing_access_config_uses_documented_defaults(tmp_path: Path) -> None:
    assert load_access_config(tmp_path / "absent.yaml") == AccessConfig()
