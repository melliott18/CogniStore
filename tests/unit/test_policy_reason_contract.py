"""Fail-closed readers and the content-free projection of policy evidence."""

import copy
import json

import pytest

from cognistore.core.catalog import ObjectRecord
from cognistore.core.placement_controls import (
    ImportanceTag,
    MovementConstraints,
    StabilityOverride,
    evaluate_movement_constraints,
)
from cognistore.core.policy_reasons import (
    capture_policy_reason,
    reason_from_audit_details,
    validate_policy_reason,
)

AS_OF = "2026-09-10T12:00:00.000000Z"


def capture(*, code="size_threshold", signals=None, evidence=None, trusted=True):
    if evidence is None:
        evidence = evaluate_movement_constraints(
            ObjectRecord("private-bucket", "private-key", 101, "hot"), as_of=AS_OF,
        )
    # These are the destination/suppression projections supplied by the runner.
    evidence = dict(evidence)
    evidence.setdefault("importance_allowed_tiers", evidence["allowed_destination_tiers"])
    evidence["allowed_destination_tiers"] = ["hot", "warm"]
    evidence.setdefault("suppression_reason", None)
    return capture_policy_reason(
        code=code,
        signals=signals if signals is not None else [{
            "name": "size_bytes", "value": 101, "operator": ">",
            "threshold": 100, "rule_index": None,
        }],
        constraints=evidence,
        policy_metadata={"name": "simple", "version": "49-test", "model": None},
        action="move", destination="warm", current_tier="hot", trusted_policy=trusted,
        embedding_rule_names=("private-rule",),
    )


def set_path(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def test_reason_round_trip_detaches_inputs_and_preserves_unavailable_confidence():
    source = capture()
    validated = validate_policy_reason(json.loads(json.dumps(source, allow_nan=False)))

    assert validated == source
    assert validated["confidence"] == {"value": None, "source": "not_applicable"}
    assert validated["constraints"]["evaluated_at"] == AS_OF
    source["decisive_signals"][0]["value"] = 999
    source["constraints"]["allowed_destination_tiers"].append("cold")
    assert validated["decisive_signals"][0]["value"] == 101
    assert validated["constraints"]["allowed_destination_tiers"] == ["hot", "warm"]


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None, 0, 2])
def test_reader_rejects_unsupported_or_noninteger_versions(version):
    reason = capture()
    reason["schema_version"] = version
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


@pytest.mark.parametrize("path", [
    (), ("decisive_signals", 0), ("constraints",), ("policy",),
    ("policy", "model"), ("confidence",),
])
def test_reader_rejects_unknown_fields_at_each_contract_boundary(path):
    reason = capture()
    reason["policy"]["model"] = {"identity": "model", "version": None}
    target = reason
    for key in path:
        target = target[key]
    target["raw_content"] = "private object text"
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


@pytest.mark.parametrize("path,value", [
    (("code",), "arbitrary provider explanation"),
    (("disposition",), "arbitrary provider explanation"),
    (("decisive_signals", 0, "name"), "raw_content"),
    (("decisive_signals", 0, "value"), "private object text"),
    (("decisive_signals", 0, "threshold"), "private query"),
    (("decisive_signals", 0, "value"), {"content": "private object text"}),
    (("constraints", "importance_level"), "private provenance"),
    (("constraints", "stability_override_kind"), "private override justification"),
    (("constraints", "candidate_action"), "private provider response"),
])
def test_reader_rejects_raw_contents_in_signals_and_constraint_values(path, value):
    reason = capture()
    set_path(reason, path, value)
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


@pytest.mark.parametrize("path", [
    ("decisive_signals", 0, "value"),
    ("decisive_signals", 0, "threshold"),
    ("decisive_signals", 0, "rule_index"),
    ("constraints", "importance_revision"),
    ("constraints", "minimum_residency_seconds"),
    ("constraints", "cooldown_seconds"),
    ("constraints", "size_hysteresis_bytes"),
    ("constraints", "similarity_hysteresis"),
])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_numeric_contract_rejects_boolean_and_nonfinite_values(path, value):
    reason = capture()
    set_path(reason, path, value)
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


@pytest.mark.parametrize("value", [0, 0.95, True, "high", float("nan")])
def test_reader_cannot_promote_scores_to_calibrated_confidence(value):
    reason = capture(code="embedding_rule", signals=[{
        "name": "embedding_similarity", "value": 0.95, "operator": ">=",
        "threshold": 0.8, "rule_index": 0,
    }])
    assert reason["confidence"]["value"] is None
    reason["confidence"]["value"] = value
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


@pytest.mark.parametrize("code", [
    "provider_decision", "provider_error", "provider_invalid_response", "custom_policy",
])
def test_unreported_provider_confidence_remains_explicitly_null(code):
    reason = capture(code=code, signals=[])
    assert reason["confidence"] == {"value": None, "source": "not_reported"}


@pytest.mark.parametrize("field", ["value", "source"])
def test_reader_requires_explicit_confidence_availability(field):
    reason = capture()
    reason["confidence"].pop(field)
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


@pytest.mark.parametrize("details", [None, [], {}, {"reason": "legacy free-text reason"}])
def test_legacy_details_do_not_fabricate_a_reason(details):
    assert reason_from_audit_details(details) is None


@pytest.mark.parametrize("invalid", [None, {}, [], {"schema_version": 2}])
def test_present_but_invalid_reason_is_not_misclassified_as_legacy(invalid):
    with pytest.raises(ValueError):
        reason_from_audit_details({"structured_reason": invalid})


def test_capture_excludes_free_text_provenance_overrides_and_hysteresis_rule_names():
    record = ObjectRecord(
        "private-bucket", "private-key", 101, "hot",
        importance=ImportanceTag(
            "normal", "user", "private-actor", "private provenance", AS_OF,
        ),
    )
    evidence = evaluate_movement_constraints(
        record,
        MovementConstraints(stability_override=StabilityOverride(
            "emergency", "private override justification",
        )),
        as_of=AS_OF,
    )
    evidence["raw_content"] = "private object text"
    evidence["hysteresis"] = {
        "candidate_action": "move", "candidate_destination_tier": "warm",
        "checks": [{
            "kind": "similarity", "configured_band": 0.1,
            "baseline_threshold": 0.8, "effective_threshold": 0.9, "value": 0.85,
            "rule": "private-rule", "query": "private query",
        }],
    }

    reason = capture(code="embedding_rule", evidence=evidence)

    encoded = json.dumps(reason)
    assert "private" not in encoded
    assert reason["constraints"]["importance_level"] == "normal"
    assert reason["constraints"]["stability_override_kind"] == "emergency"
    check = reason["constraints"]["hysteresis_checks"][0]
    assert check["rule_index"] == 0
    assert check["value"] == 0.85
    modified = copy.deepcopy(reason)
    modified["constraints"]["hysteresis_checks"][0]["query"] = "private query"
    with pytest.raises(ValueError):
        validate_policy_reason(modified)


def test_custom_policy_cannot_claim_trusted_signals_or_rule_codes():
    reason = capture(code="name_rule", signals=[{"raw_content": "private content"}], trusted=False)
    assert reason["code"] == "custom_policy"
    assert reason["decisive_signals"] == []
    assert "private" not in json.dumps(reason)


@pytest.mark.parametrize("field", [
    "configured_band", "baseline_threshold", "effective_threshold", "value", "rule_index",
])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_hysteresis_checks_reject_boolean_and_nonfinite_numbers(field, value):
    reason = capture()
    reason["constraints"]["hysteresis_checks"] = [{
        "kind": "size", "configured_band": 10, "baseline_threshold": 100,
        "effective_threshold": 110, "value": 101, "rule_index": None,
    }]
    reason["constraints"]["hysteresis_checks"][0][field] = value
    with pytest.raises(ValueError):
        validate_policy_reason(reason)


def test_reader_rejects_credentials_in_application_metadata():
    reason = capture()
    reason["policy"]["name"] = "password=do-not-persist"
    with pytest.raises(ValueError, match="unredacted credentials"):
        validate_policy_reason(reason)


@pytest.mark.parametrize("field", ["evaluated_at", "placement_started_at", "last_tier_move_at"])
def test_constraint_timestamps_require_timezones_and_canonicalize_offsets(field):
    reason = capture()
    reason["constraints"][field] = "2026-09-10T05:00:00-07:00"
    assert validate_policy_reason(reason)["constraints"][field] == AS_OF
    reason["constraints"][field] = "2026-09-10T12:00:00"
    with pytest.raises(ValueError):
        validate_policy_reason(reason)
