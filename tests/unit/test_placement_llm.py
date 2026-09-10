from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from cognistore.core import placement_llm
from cognistore.core.placement_llm import (
    MAX_RESPONSE_BYTES,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    CallablePlacementProvider,
    FakePlacementProvider,
    PlacementInference,
    PlacementProviderUnavailable,
    TransientPlacementError,
)

INPUTS = {"current_tier": "hot", "size": 123, "allowed_tiers": ["hot", "warm", "cold"]}
MOVE = '{"action":"move","dst_tier":"cold","reason":"Lower storage cost"}'
STAY = '{"action":"stay","dst_tier":null,"reason":"Insufficient evidence"}'


def evaluate(response: str):
    return PlacementInference(FakePlacementProvider(response)).evaluate(**INPUTS)


def test_fake_is_deterministic_without_network_and_records_provenance():
    first, second = evaluate(MOVE), evaluate(MOVE)
    assert first == second
    assert (first.action, first.dst_tier, first.reason) == ("move", "cold", "Lower storage cost")
    audit = first.audit
    assert audit["provider"] == "fake"
    assert audit["model"] == "deterministic"
    assert audit["provider_version"] == "1"
    assert audit["model_version"] is None
    assert audit["prompt_version"] == PROMPT_VERSION
    assert audit["schema_version"] == SCHEMA_VERSION
    assert audit["prompt_hash"] == hashlib.sha256(audit["prompt"].encode()).hexdigest()
    encoded_schema = json.dumps(audit["schema"], sort_keys=True, separators=(",", ":"))
    assert audit["schema_hash"] == hashlib.sha256(encoded_schema.encode()).hexdigest()
    assert audit["response"] == MOVE
    assert audit["validation_errors"] == []
    assert audit["fallback_reason"] is None
    assert audit["attempts"] == 1
    assert audit["final_proposal"] == json.loads(MOVE)
    assert PlacementInference(FakePlacementProvider()).evaluate(**INPUTS).action == "stay"


def test_callable_receives_only_minimal_trusted_inputs_and_an_isolated_schema():
    calls = []

    def complete(prompt, schema, timeout_seconds):
        calls.append((prompt, deepcopy(schema), timeout_seconds))
        schema.clear()
        return STAY

    service = PlacementInference(
        CallablePlacementProvider(complete, provider_id="sdk", model="test", version="2")
    )
    result = service.evaluate(**INPUTS)
    assert result.action == "stay"
    assert len(calls) == 1
    prompt, schema, remaining = calls[0]
    assert json.loads(prompt.rsplit("\n", 1)[1]) == INPUTS
    assert set(schema["properties"]) == {"action", "dst_tier", "reason"}
    assert set(schema["required"]) == {"action", "dst_tier", "reason"}
    assert schema["additionalProperties"] is False
    assert schema["oneOf"][1]["properties"]["dst_tier"]["enum"] == ["warm", "cold"]
    assert result.audit["schema"] == schema
    assert 0 < remaining <= 10


@pytest.mark.parametrize("tiers", [["hot"], ["hot", "cold"], ["cold"]])
def test_sent_schema_is_valid_and_matches_stay_move_contract(tiers):
    result = PlacementInference(FakePlacementProvider(STAY)).evaluate(
        current_tier="hot", size=123, allowed_tiers=tiers
    )
    schema = result.audit["schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(json.loads(STAY))
    assert validator.is_valid(json.loads(MOVE)) == ("cold" in tiers)
    assert not validator.is_valid({"action": "move", "dst_tier": "hot", "reason": "same"})
    assert not validator.is_valid({"action": "stay", "dst_tier": "cold", "reason": "bad"})


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ('{"action":"move","dst_tier":"cold"}', "invalid_fields"),
        ('{"action":"stay","reason":"ok"}', "invalid_fields"),
        ('{"action":"stay","dst_tier":null,"reason":"ok","extra":1}', "invalid_fields"),
        ('{"action":"stay","action":"move","dst_tier":"cold","reason":"ok"}', "duplicate_key"),
        ('{"action":"stay","dst_tier":NaN,"reason":"ok"}', "non_finite_number"),
        ('{"action":"stay","dst_tier":Infinity,"reason":"ok"}', "non_finite_number"),
        ('{"action":"stay","dst_tier":-Infinity,"reason":"ok"}', "non_finite_number"),
        ('{"action":"stay","dst_tier":"cold","reason":"ok"}', "stay_requires_null_destination"),
        ('{"action":"move","dst_tier":"hot","reason":"ok"}', "invalid_destination"),
        ('{"action":"move","dst_tier":"elsewhere","reason":"ok"}', "invalid_destination"),
        ('{"action":"move","dst_tier":null,"reason":"ok"}', "invalid_destination"),
        ('{"action":"move","dst_tier":42,"reason":"ok"}', "invalid_destination"),
        ('{"action":"move","dst_tier":["cold"],"reason":"ok"}', "invalid_destination"),
        ('{"action":true,"dst_tier":null,"reason":"ok"}', "invalid_action"),
        ('{"action":"delete","dst_tier":null,"reason":"ok"}', "invalid_action"),
        ('{"action":"MOVE","dst_tier":"cold","reason":"ok"}', "invalid_action"),
        ('{"action":"stay","dst_tier":null,"reason":12}', "invalid_reason"),
        ('{"action":"stay","dst_tier":null,"reason":"  "}', "invalid_reason"),
        (json.dumps({"action": "stay", "dst_tier": None, "reason": "x" * 513}), "invalid_reason"),
        ('{"action":"stay","dst_tier":null,"reason":"\\ud800"}', "invalid_unicode"),
        ("[]", "response_not_object"),
        ("null", "response_not_object"),
        ("true", "response_not_object"),
        ("```json\n" + STAY + "\n```", "invalid_json"),
        (STAY + STAY, "invalid_json"),
        ("{", "invalid_json"),
        ("[" * 2_000, "invalid_json"),
        ("x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
        ("é" * MAX_RESPONSE_BYTES, "response_too_large"),
        ("\ud800", "invalid_unicode"),
        ({"action": "stay", "dst_tier": None, "reason": "ok"}, "response_not_string"),
    ],
    ids=lambda value: str(value)[:48],
)
def test_strict_responses_fail_closed_without_retry(response, error):
    result = evaluate(response)
    assert (result.action, result.dst_tier, result.reason) == ("stay", None, "llm_invalid_response")
    assert result.audit["validation_errors"] == [error]
    assert result.audit["attempts"] == 1
    assert result.audit["fallback_reason"] == result.reason
    assert result.audit["final_proposal"] == {
        "action": "stay", "dst_tier": None, "reason": "llm_invalid_response"
    }


def test_unavailable_provider_always_stays_with_auditable_fixed_reason():
    result = PlacementInference().evaluate(**INPUTS)
    assert result.action == "stay"
    assert result.reason == "llm_provider_unavailable"
    assert result.audit["attempts"] == 0
    assert result.audit["provider"] == "unavailable"
    assert result.audit["fallback_reason"] == result.reason
    assert result == PlacementInference().evaluate(**INPUTS)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (RuntimeError("password=unprintable"), "llm_provider_error"),
        (TimeoutError("password=unprintable"), "llm_provider_timeout"),
        (PlacementProviderUnavailable("password=unprintable"), "llm_provider_unavailable"),
    ],
)
def test_errors_never_leak_diagnostics_or_retry(failure, reason):
    calls = []

    def complete(*args):
        calls.append(args)
        raise failure

    service = PlacementInference(CallablePlacementProvider(complete, provider_id="test", model="x"))
    result = service.evaluate(**INPUTS)
    assert result.reason == reason
    assert len(calls) == 1
    assert result.audit["fallback_reason"] == reason
    assert "unprintable" not in json.dumps(result.audit)


def test_only_explicit_transient_errors_retry_within_shared_budget():
    budgets = []
    timestamps = []

    def complete(prompt, schema, timeout_seconds):
        budgets.append(timeout_seconds)
        timestamps.append(time.monotonic())
        if len(budgets) == 1:
            raise TransientPlacementError("password=private")
        return MOVE

    service = PlacementInference(
        CallablePlacementProvider(complete, provider_id="test", model="x"),
        timeout_seconds=1,
        max_attempts=2,
    )
    result = service.evaluate(**INPUTS)
    assert result.action == "move"
    assert result.audit["attempts"] == 2
    assert result.audit["provider_errors"] == ["transient_error"]
    assert 0 < budgets[1] < budgets[0] <= 1
    assert timestamps[1] - timestamps[0] >= 0.045
    assert "private" not in json.dumps(result.audit)


def test_insufficient_budget_for_retry_backoff_returns_timeout_without_retry():
    calls = []

    def complete(*args):
        calls.append(args)
        raise TransientPlacementError("provider busy")

    service = PlacementInference(
        CallablePlacementProvider(complete, provider_id="test", model="x"),
        timeout_seconds=0.01,
        max_attempts=5,
    )
    result = service.evaluate(**INPUTS)
    assert len(calls) == 1
    assert result.reason == "llm_provider_timeout"
    assert result.audit["fallback_reason"] == result.reason


def test_transient_retry_count_is_bounded():
    calls = []

    def complete(*args):
        calls.append(args)
        raise TransientPlacementError("private detail")

    service = PlacementInference(
        CallablePlacementProvider(complete, provider_id="test", model="x"), max_attempts=3
    )
    result = service.evaluate(**INPUTS)
    assert result.reason == "llm_provider_error"
    assert len(calls) == result.audit["attempts"] == 3
    assert result.audit["provider_errors"] == ["transient_error"] * 3


@pytest.mark.parametrize("late_failure", [False, True])
def test_ignoring_timeout_cannot_delay_result_publish_late_or_retry(late_failure):
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def complete(*args):
        calls.append(args)
        try:
            release.wait(2)
            if late_failure:
                raise TransientPlacementError("retry after deadline")
            return MOVE
        finally:
            finished.set()

    service = PlacementInference(
        CallablePlacementProvider(complete, provider_id="test", model="x"),
        timeout_seconds=0.03,
        max_attempts=5,
    )
    started = time.monotonic()
    try:
        result = service.evaluate(**INPUTS)
        assert time.monotonic() - started < 0.5
        assert result.reason == "llm_provider_timeout"
        assert result.action == "stay"
        before = deepcopy(result.audit)
    finally:
        release.set()
        assert finished.wait(1)
    assert result.audit == before
    assert result.audit["response"] is None
    assert len(calls) == 1


def test_abandoned_calls_are_globally_bounded_across_service_instances(monkeypatch):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(placement_llm, "_CALL_SLOTS", slots)
    release = threading.Event()
    calls = []
    workers = []

    def complete(*args):
        calls.append(args)
        workers.append(threading.current_thread())
        release.wait(2)
        return MOVE

    provider = CallablePlacementProvider(complete, provider_id="test", model="x")
    try:
        results = [
            PlacementInference(provider, timeout_seconds=0.01).evaluate(**INPUTS)
            for _ in range(10)
        ]
        assert len(calls) == 2
        assert all(worker.daemon for worker in workers)
        assert [result.reason for result in results[:2]] == ["llm_provider_timeout"] * 2
        assert [result.reason for result in results[2:]] == ["llm_provider_busy"] * 8
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
    assert PlacementInference(FakePlacementProvider(MOVE)).evaluate(**INPUTS).action == "move"


def test_credentials_are_redacted_from_response_reason_and_provider_metadata():
    response = json.dumps({
        "action": "move", "dst_tier": "cold",
        "reason": "password=reason-secret endpoint=https://user:pass@example.com",
    })
    provider = CallablePlacementProvider(
        lambda *args: response,
        provider_id="api_key=provider-secret",
        model="token=model-secret",
        version="secret=version-secret",
        model_version="password=model-version-secret",
    )
    result = PlacementInference(provider).evaluate(**INPUTS)
    assert result.action == "move"
    assert "[REDACTED]" in result.reason
    encoded = json.dumps(result.audit)
    for secret in [
        "reason-secret", "user:pass", "provider-secret", "model-secret", "version-secret",
        "model-version-secret",
    ]:
        assert secret not in encoded
    assert result.audit["final_proposal"]["reason"] == result.reason
    assert result.audit["model_version"] == "password=[REDACTED]"


@pytest.mark.parametrize("response", [
    '{"action":"stay","dst_tier":null,"reason":"api_key=sec\\u0072et-value"}',
    '{"action":"stay","dst_tier":null,"reason":"ok","pa\\u0073sword":"secret-value"}',
    '{"action":"stay","dst_tier":null,"reason":"password=secret-value",',
])
def test_audit_decodes_json_escapes_before_redaction_and_omits_malformed_json(response):
    result = evaluate(response)
    serialized = json.dumps(result.audit)
    assert "secret-value" not in serialized
    assert "0072et-value" not in serialized
    if result.audit["validation_errors"] == ["invalid_json"]:
        assert result.audit["response"] == "[response omitted: invalid json]"
    else:
        assert "[REDACTED]" in result.audit["response"]


def test_credential_bearing_tier_is_redacted_and_never_used_as_a_routing_identifier():
    calls = []
    provider = CallablePlacementProvider(
        lambda *args: calls.append(args) or MOVE, provider_id="test", model="x"
    )
    result = PlacementInference(provider).evaluate(
        current_tier="hot", size=1, allowed_tiers=["hot", "password=do-not-send"]
    )
    assert result.reason == "llm_invalid_input"
    assert not calls
    assert "do-not-send" not in json.dumps(result.audit)
    assert "[REDACTED]" in result.audit["prompt"]


@pytest.mark.parametrize("size", [True, -1, "123", 1.0, None])
def test_invalid_size_is_not_coerced(size):
    result = PlacementInference(FakePlacementProvider(MOVE)).evaluate(**(INPUTS | {"size": size}))
    assert result.reason == "llm_invalid_input"
    assert result.audit["attempts"] == 0


@pytest.mark.parametrize("tiers", [[], "cold", ["hot", None], [""], ["x"] * 65])
def test_invalid_tiers_fail_closed(tiers):
    result = PlacementInference(FakePlacementProvider(MOVE)).evaluate(
        **(INPUTS | {"allowed_tiers": tiers})
    )
    assert result.reason == "llm_invalid_input"
    assert result.audit["attempts"] == 0


@pytest.mark.parametrize("timeout", [True, 0, -1, 61, float("nan"), float("inf"), "5"])
def test_timeout_configuration_is_finite_and_bounded(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        PlacementInference(timeout_seconds=timeout)


@pytest.mark.parametrize("attempts", [True, 0, -1, 6, 1.0, "2"])
def test_retry_configuration_is_bounded(attempts):
    with pytest.raises(ValueError, match="max_attempts"):
        PlacementInference(max_attempts=attempts)
