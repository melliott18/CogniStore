from __future__ import annotations

import json
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.estimation import (
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
    StorageImpactEstimator,
)
from cognistore.core.topology import AttributeValue, PlacementConstraints, Pool

AS_OF = "2026-09-09T12:00:00Z"
START = "2026-09-01T00:00:00Z"
END = "2026-09-30T23:59:59Z"
GIB = 1073741824
RATE_UNITS = {
    "storage_price": "USD/GiB-month",
    "read_request_price": "USD/request",
    "write_request_price": "USD/request",
    "transfer_price": "USD/GiB",
    "retrieval_price": "USD/GiB",
    "storage_energy": "kWh/GiB-month",
    "read_request_energy": "kWh/request",
    "write_request_energy": "kWh/request",
    "transfer_energy": "kWh/GiB",
    "retrieval_energy": "kWh/GiB",
    "carbon_intensity": "gCO2e/kWh",
}
GOLDEN = json.loads(
    (Path(__file__).parents[1] / "fixtures/estimation/storage_impact_v1.json").read_text()
)


def rate(value="0", unit="USD/GiB-month", **changes):
    return RateAssumption(
        **{
            "value": value,
            "unit": unit,
            "source": "synthetic-fixture",
            "source_version": "2026-09-v1",
            "effective_from": START,
            "effective_until": END,
            **changes,
        }
    )


def profile(**changes):
    values = {
        "version": "synthetic-profile-v1",
        "backend": "s3",
        "rates": {name: rate(unit=unit) for name, unit in RATE_UNITS.items()},
    }
    values.update(changes)
    return EstimationProfile(**values)


def pool(**changes):
    return Pool(**{
        "pool_id": "west",
        "tier": "hot",
        "region": "us-west-2",
        "members": ("synthetic-bucket",),
        "localities": ("US",),
        **changes,
    })


def workload(**changes):
    return EstimationWorkload(**{
        "stored_bytes": GIB,
        "duration_hours": "730",
        "read_requests": 0,
        "write_requests": 0,
        "transfer_bytes": 0,
        "retrieval_bytes": 0,
        **changes,
    })


def estimate(value=None, usage=None, destination=None, *, as_of=AS_OF):
    return StorageImpactEstimator({"west": value or profile()}).estimate(
        destination or pool(), usage or workload(), as_of=as_of
    ).to_dict()


def components(total):
    return {component["name"]: component for component in total["components"]}


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda case: case["name"])
def test_hand_calculated_golden_totals_components_and_bounds(case):
    configured = profile(
        backend=case["backend"],
        rates={name: rate(**value) for name, value in GOLDEN["rates"].items()},
        calibration={
            name: rate(**value, source="synthetic-calibration", source_version="calibration-v2")
            for name, value in case["calibration"].items()
        },
    )
    result = estimate(configured, EstimationWorkload(**GOLDEN["workload"]))
    for objective in ("cost", "carbon"):
        actual = result[objective]
        expected = case["expected"][objective]
        assert actual["state"] == "available"
        assert actual["known_subtotal"] == expected["value"]
        assert {key: actual[key] for key in ("value", "unit", "lower", "upper")} == {
            key: expected[key] for key in ("value", "unit", "lower", "upper")
        }
        actual_components = components(actual)
        assert actual_components.keys() == expected["components"].keys()
        for name, expected_component in expected["components"].items():
            actual_component = actual_components[name]
            assert actual_component["state"] == "available"
            assert actual_component["unit"] == expected["unit"]
            assert {key: actual_component[key] for key in ("value", "lower", "upper")} == (
                expected_component
            )
    assert json.loads(json.dumps(result)) == result
    assert estimate(configured, EstimationWorkload(**GOLDEN["workload"])) == result


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "NaN", "Infinity", "nope"])
def test_invalid_numeric_rate_never_enters_estimator(value):
    with pytest.raises(ValueError):
        rate(value)


@pytest.mark.parametrize("changes", [
    {"unit": ""},
    {"source": ""},
    {"source_version": ""},
    {"effective_from": "2026-09-01T00:00:00"},
    {"effective_until": "2026-09-30T23:59:59"},
    {"effective_from": "tomorrow"},
    {"effective_from": END, "effective_until": START},
    {"lower": "0"},
    {"upper": "2"},
    {"lower": "2", "upper": "3"},
    {"lower": "0", "upper": "0.5"},
    {"lower": "-1", "upper": "2"},
])
def test_rate_rejects_invalid_provenance_dates_and_intervals(changes):
    with pytest.raises(ValueError):
        rate("1", **changes)


@pytest.mark.parametrize("name,unit", RATE_UNITS.items())
def test_profiles_require_documented_rate_units(name, unit):
    assert profile(rates={name: rate(unit=unit)})
    with pytest.raises(ValueError):
        profile(rates={name: rate(unit="seconds")})


@pytest.mark.parametrize("changes", [
    {"version": ""},
    {"backend": ""},
    {"rates": {"typo": rate()}},
    {"rates": {"storage_price": {"value": "1"}}},
    {"calibration": {"typo": rate(unit="multiplier")}},
    {"calibration": {"storage_price": rate(unit="USD/GiB-month")}},
])
def test_profiles_reject_unknown_or_invalid_assumptions(changes):
    with pytest.raises(ValueError):
        profile(**changes)


@pytest.mark.parametrize("field", [
    "stored_bytes", "duration_hours", "read_requests", "write_requests", "transfer_bytes",
    "retrieval_bytes",
])
@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
def test_invalid_workload_is_not_coerced_to_zero(field, value):
    with pytest.raises(ValueError):
        workload(**{field: value})


@pytest.mark.parametrize("field", [
    "stored_bytes", "read_requests", "write_requests", "transfer_bytes", "retrieval_bytes",
])
def test_bytes_and_request_counts_cannot_be_fractional(field):
    with pytest.raises(ValueError):
        workload(**{field: "0.5"})


@pytest.mark.parametrize("objective,rate_name,component_name", [
    ("cost", "storage_price", "storage"),
    ("cost", "read_request_price", "read_requests"),
    ("cost", "write_request_price", "write_requests"),
    ("cost", "transfer_price", "transfer"),
    ("cost", "retrieval_price", "retrieval"),
    ("carbon", "storage_energy", "storage"),
    ("carbon", "read_request_energy", "read_requests"),
    ("carbon", "write_request_energy", "write_requests"),
    ("carbon", "transfer_energy", "transfer"),
    ("carbon", "retrieval_energy", "retrieval"),
    ("carbon", "carbon_intensity", "storage"),
])
def test_missing_rate_remains_unavailable_even_for_explicit_zero_usage(
    objective, rate_name, component_name
):
    configured = profile()
    rates = {name: value for name, value in configured.rates.items() if name != rate_name}
    result = estimate(replace(configured, rates=rates), workload(stored_bytes=0))
    total = result[objective]
    assert total["value"] is None
    assert total["state"] == "unavailable"
    component = components(total)[component_name]
    assert component["value"] is None
    assert component["state"] == "unavailable"
    assert component["reasons"]


@pytest.mark.parametrize("field,component_name", [
    ("stored_bytes", "storage"),
    ("duration_hours", "storage"),
    ("read_requests", "read_requests"),
    ("write_requests", "write_requests"),
    ("transfer_bytes", "transfer"),
    ("retrieval_bytes", "retrieval"),
])
def test_unknown_workload_remains_unavailable_even_with_explicit_zero_rates(field, component_name):
    result = estimate(usage=workload(**{field: None}))
    for objective in ("cost", "carbon"):
        assert result[objective]["value"] is None
        assert result[objective]["state"] == "unavailable"
        assert components(result[objective])[component_name]["state"] == "unavailable"


def test_explicit_zero_rates_and_usage_produce_available_zero_but_no_invented_bounds():
    result = estimate(usage=workload(stored_bytes=0))
    for objective in ("cost", "carbon"):
        assert result[objective]["state"] == "available"
        assert result[objective]["value"] == "0"
        assert result[objective]["lower"] is None
        assert result[objective]["upper"] is None


def test_known_subtotal_is_not_presented_as_total_when_other_rates_are_unknown():
    result = estimate(profile(rates={"storage_price": rate("2.5")}))
    assert result["cost"]["known_subtotal"] == "2.5"
    assert result["cost"]["value"] is None
    assert result["cost"]["state"] == "unavailable"
    assert result["carbon"]["value"] is None
    assert result["carbon"]["state"] == "unavailable"


@pytest.mark.parametrize("as_of,available", [
    (START, True),
    (END, True),
    ("2026-08-31T23:59:59.999999Z", False),
    ("2026-09-30T23:59:59.000001Z", False),
])
def test_rate_effective_interval_is_inclusive_and_never_extrapolated(as_of, available):
    result = estimate(as_of=as_of)
    for objective in ("cost", "carbon"):
        assert (result[objective]["state"] == "available") is available
        if not available:
            assert result[objective]["value"] is None


@pytest.mark.parametrize("dates,state", [
    ({"effective_until": "2026-09-08T00:00:00Z"}, "stale"),
    ({"effective_from": "2026-09-10T00:00:00Z"}, "future"),
])
def test_outdated_calibration_invalidates_its_component_without_affecting_unrelated_values(
    dates, state
):
    configured = profile(calibration={
        "storage_price": rate("2", unit="multiplier", **dates)
    })
    result = estimate(configured)
    assert result["cost"]["value"] is None
    assert components(result["cost"])["storage"]["state"] == "unavailable"
    assert f"storage_price:calibration_{state}" in components(result["cost"])["storage"]["reasons"]
    assert components(result["cost"])["read_requests"]["state"] == "available"
    assert result["carbon"]["state"] == "available"


def test_unbounded_calibration_keeps_estimate_available_without_inventing_interval():
    configured = profile(
        rates={name: rate(**value) for name, value in GOLDEN["rates"].items()},
        calibration={"storage_price": rate("2", unit="multiplier")},
    )
    result = estimate(configured, EstimationWorkload(**GOLDEN["workload"]))
    assert result["cost"]["value"] == "0.1884"
    assert result["cost"]["state"] == "available"
    assert result["cost"]["lower"] is None
    assert result["cost"]["upper"] is None
    assert components(result["cost"])["read_requests"]["lower"] == "0.00032"
    assert result["carbon"]["lower"] == "54.48"


def test_absent_profile_never_implies_free_storage_or_zero_emissions():
    result = StorageImpactEstimator({}).estimate(pool(), workload(), as_of=AS_OF).to_dict()
    assert result["cost"]["value"] is None
    assert result["carbon"]["value"] is None
    assert result["cost"]["state"] == result["carbon"]["state"] == "unavailable"


def test_precision_and_serialization_are_independent_of_callers_decimal_context():
    configured = profile(rates={
        **profile().rates,
        "storage_price": rate(Decimal("0.123456789012345678901234567890123456789")),
    })
    result = estimate(configured)
    with localcontext() as context:
        context.prec = 5
        context.rounding = ROUND_DOWN
        low_precision_result = estimate(configured)
        assert context.prec == 5
        assert context.rounding == ROUND_DOWN
    assert low_precision_result == result
    assert result["cost"]["value"] == "0.123456789012345678901234567890123456789"


@pytest.mark.parametrize("value,canonical", [
    ("1e50", "1" + "0" * 50),
    ("1e100", "1" + "0" * 100),
    ("1" * 50 + "e51", "1" * 50 + "0" * 51),
])
def test_large_scientific_inputs_replay_after_canonical_serialization(value, canonical):
    assumption = rate(value, lower=value, upper=value)
    usage = workload(stored_bytes=value)
    configured = profile(rates={**profile().rates, "storage_price": assumption})
    expected = estimate(configured, usage)
    serialized_rate = json.loads(json.dumps(assumption.to_dict()))
    serialized_workload = json.loads(json.dumps(usage.to_dict()))
    assert serialized_rate["value"] == serialized_rate["lower"] == serialized_rate["upper"] == (
        canonical
    )
    assert serialized_workload["stored_bytes"] == canonical
    with localcontext() as context:
        context.prec = 5
        context.rounding = ROUND_DOWN
        replay_rate = RateAssumption.from_mapping(serialized_rate)
        replay_workload = EstimationWorkload.from_mapping(serialized_workload)
        replay_profile = EstimationProfile.from_mapping(configured.to_dict())
        assert replay_rate == assumption
        assert replay_workload == usage
        assert estimate(replay_profile, replay_workload) == expected
        assert context.prec == 5
        assert context.rounding == ROUND_DOWN


def test_smallest_supported_scientific_rate_and_duration_replay_exactly():
    canonical = "0." + "0" * 99 + "1"
    assumption = rate("1e-100")
    usage = workload(duration_hours="1e-100")
    assert assumption.to_dict()["value"] == canonical
    assert usage.to_dict()["duration_hours"] == canonical
    assert RateAssumption.from_mapping(assumption.to_dict()) == assumption
    assert EstimationWorkload.from_mapping(usage.to_dict()) == usage


@pytest.mark.parametrize("value", ["1e101", "1e-101", "1" * 51, "1" + "0" * 49 + "1"])
def test_decimal_range_and_significant_digit_limits_reject_unsupported_inputs(value):
    with pytest.raises(ValueError, match="supported decimal range"):
        rate(value)
    with pytest.raises(ValueError, match="supported decimal range"):
        workload(duration_hours=value)
    with pytest.raises(ValueError, match="supported decimal range"):
        workload(stored_bytes=value)


def test_byte_units_and_fractional_months_are_applied_once():
    configured = profile(rates={**profile().rates, "storage_price": rate("2")})
    result = estimate(configured, workload(stored_bytes=GIB // 2, duration_hours="365"))
    assert result["cost"]["value"] == "0.5"


def test_output_preserves_versioned_formulas_units_sources_effective_dates_and_calibration():
    calibration = rate(
        "2", unit="multiplier", source="lab/storage-v2", source_version="calibration-2026-09"
    )
    configured = profile(
        rates={**profile().rates, "storage_price": rate("0.25")},
        calibration={"storage_price": calibration},
    )
    result = estimate(configured)
    assert result["schema_version"] == 1
    assert result["profile_version"] == "synthetic-profile-v1"
    assert result["backend"] == "s3"
    assert result["as_of"] == "2026-09-09T12:00:00.000000Z"
    formulas = result["formulas"]
    assert formulas["version"] == "storage-impact-v1"
    assert formulas["source"]
    assert formulas["effective_from"]
    assert formulas["bytes_per_GiB"] == "1073741824"
    assert formulas["hours_per_month"] == "730"
    assert formulas["decimal_precision"] == 50
    assert formulas["rounding"] == "ROUND_HALF_EVEN"
    assert formulas["rate_units"] == RATE_UNITS
    assert formulas["cost"] and formulas["carbon"] and formulas["calibration"]
    resolved_rate = {item["name"]: item for item in result["rates"]}["storage_price"]
    assert resolved_rate["value"] == "0.5"
    assert resolved_rate["origin"] == "profile"
    assert resolved_rate["assumption"] == {
        "value": "0.25", "unit": "USD/GiB-month", "source": "synthetic-fixture",
        "source_version": "2026-09-v1", "effective_from": "2026-09-01T00:00:00.000000Z",
        "effective_until": "2026-09-30T23:59:59.000000Z", "lower": None, "upper": None,
    }
    assert resolved_rate["calibration"] == calibration.to_dict()


def test_configuration_roundtrip_and_input_mutations_do_not_change_estimates():
    rates = {**profile().rates, "storage_price": rate("0.125")}
    configured = profile(rates=rates)
    profiles = {"west": configured}
    estimator = StorageImpactEstimator(profiles)
    result = estimator.estimate(pool(), workload(), as_of=AS_OF).to_dict()
    rates.clear()
    profiles.clear()
    serialized = configured.to_dict()
    assert EstimationProfile.from_mapping(json.loads(json.dumps(serialized))) == configured
    assert EstimationWorkload.from_mapping(workload().to_dict()) == workload()
    serialized["rates"]["storage_price"]["value"] = "999"
    result["rates"][0]["assumption"]["value"] = "888"
    replay = estimator.estimate(pool(), workload(), as_of=AS_OF).to_dict()
    assert replay["cost"]["value"] == "0.125"
    assert {item["name"]: item for item in replay["rates"]}["storage_price"]["value"] == "0.125"


@pytest.mark.parametrize("observed_at,available", [
    ("2026-09-09T11:59:00Z", True),
    ("2026-09-09T11:58:59.999999Z", False),
    ("2026-09-09T12:00:00.000001Z", False),
])
def test_topology_fallback_honors_attribute_freshness(observed_at, available):
    destination = pool(attributes={
        "price": AttributeValue(
            value=2.5, unit="USD/GiB-month", source="topology-pricing", kind="configured",
            observed_at=observed_at, max_age_seconds=60,
        ),
        "carbon_intensity": AttributeValue(
            value=400, unit="gCO2e/kWh", source="topology-grid", kind="measured",
            observed_at=observed_at, max_age_seconds=60,
        ),
    })
    configured = profile(rates={
        name: (rate("0.1", unit="kWh/GiB-month") if name == "storage_energy" else value)
        for name, value in profile().rates.items()
        if name not in ("storage_price", "carbon_intensity")
    })
    result = estimate(configured, destination=destination)
    assert (result["cost"]["state"] == "available") is available
    assert (result["carbon"]["state"] == "available") is available
    if available:
        assert result["cost"]["value"] == "2.5"
        assert result["carbon"]["value"] == "40"


def test_topology_expiration_is_not_extended_by_timestamp_rounding():
    price = AttributeValue(
        value=2.5, unit="USD/GiB-month", source="topology-pricing", kind="configured",
        observed_at=AS_OF, max_age_seconds=0.0000015,
    )
    as_of = "2026-09-09T12:00:00.000002Z"
    assert price.freshness(now=as_of) == "stale"
    configured = profile(rates={
        name: value for name, value in profile().rates.items() if name != "storage_price"
    })
    result = estimate(configured, destination=pool(attributes={"price": price}), as_of=as_of)
    assert result["cost"]["value"] is None
    assert components(result["cost"])["storage"]["reasons"] == ["storage_price:stale"]


def test_stale_explicit_rate_does_not_fall_back_to_fresh_topology_price():
    destination = pool(attributes={"price": AttributeValue(
        value=1, unit="USD/GiB-month", source="topology", kind="configured",
        observed_at=AS_OF, max_age_seconds=60,
    )})
    configured = profile(rates={
        **profile().rates,
        "storage_price": rate("2", effective_until="2026-09-08T00:00:00Z"),
    })
    result = estimate(configured, destination=destination)
    assert result["cost"]["value"] is None
    assert components(result["cost"])["storage"]["state"] == "unavailable"


def test_object_estimates_use_catalog_size_and_only_eligible_destinations_without_mutations():
    catalog = Catalog()
    catalog.register_tier("hot")
    catalog.register_tier("warm")
    for destination in (
        pool(),
        pool(pool_id="archive", tier="warm"),
        pool(pool_id="europe", region="eu-west-1", localities=("EU",)),
    ):
        catalog.register_pool(
            destination.pool_id, destination.tier, region=destination.region,
            members=destination.members, localities=destination.localities,
        )
    record = ObjectRecord("bucket", "key", 2 * GIB, "hot", pool_id="west")
    configured = profile(rates={**profile().rates, "storage_price": rate("2")})
    estimator = StorageImpactEstimator({name: configured for name in ("west", "archive", "europe")})
    usage = workload(stored_bytes=999 * GIB)
    with patch.object(catalog, "upsert", wraps=catalog.upsert) as upsert:
        result = estimator.estimate_object(
            catalog, record, usage,
            PlacementConstraints(allowed_regions=("us-west-2",), required_localities=("US",)),
            as_of=AS_OF,
        ).to_dict()
        upsert.assert_not_called()
    assert result["current"]["cost"]["value"] == "4"
    assert [candidate["pool_id"] for candidate in result["candidates"]] == ["west", "archive"]
    assert all(candidate["cost"]["value"] == "4" for candidate in result["candidates"])
    assert usage.stored_bytes == 999 * GIB
    assert record.size == 2 * GIB


@pytest.mark.parametrize("pool_id", [None, "unregistered"])
def test_tier_only_or_missing_current_pool_is_explicit_without_selecting_arbitrary_pool(pool_id):
    catalog = Catalog()
    catalog.register_tier("hot")
    catalog.register_pool("west", "hot", region="us-west-2", members=("bucket",))
    record = ObjectRecord("bucket", "key", GIB, "hot", pool_id=pool_id)
    estimator = StorageImpactEstimator({"west": profile()})
    result = estimator.estimate_object(catalog, record, workload(), as_of=AS_OF).to_dict()
    assert result["current"]["pool_id"] == pool_id
    assert result["current"]["tier"] == "hot"
    assert result["current"]["cost"]["state"] == "unavailable"
    assert result["current"]["carbon"]["state"] == "unavailable"
    assert "placement_pool_unavailable" in result["current"]["reasons"]
    assert [candidate["pool_id"] for candidate in result["candidates"]] == ["west"]
