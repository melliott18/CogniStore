from copy import copy
from math import nextafter
from random import Random

import pytest

from cognistore.core.catalog import ObjectRecord
from cognistore.core.policy import ContentAwarePolicy, EmbeddingPolicyRule, LLMPolicy, SimplePolicy
from cognistore.core.policy_factory import ThresholdProvider
from cognistore.core.policy_features import (
    EmbeddingPolicyFeature,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)
from cognistore.core.policy_snapshot import capture_policy_snapshot, replay_policy_snapshot


def size_policy(kind: str, *, band: int = 10, threshold: int = 100, tiers=("hot", "warm")):
    if kind == "llm":
        return LLMPolicy(
            ThresholdProvider(threshold, tiers), tiers, size_hysteresis_bytes=band,
        )
    policy_type = SimplePolicy if kind == "simple" else ContentAwarePolicy
    return policy_type(
        size_threshold=threshold, allowed_tiers=tiers, size_hysteresis_bytes=band,
    )


def semantic_policy(*, band: float = 0.125, rules=None, **kwargs):
    return ContentAwarePolicy(
        size_threshold=10,
        allowed_tiers=("hot", "warm", "cold"),
        embedding_rules=rules or (EmbeddingPolicyRule("archive", "old records", 0.5, "cold"),),
        similarity_hysteresis=band,
        **kwargs,
    )


def features(*scores: float | FeatureState) -> PolicyFeatures:
    provenance = PolicyFeatureProvenance(source="test", source_version=1, content_sha256=None)
    return PolicyFeatures(
        mime=MimePolicyFeature(state=FeatureState.MISSING, provenance=provenance),
        embeddings=tuple(
            EmbeddingPolicyFeature(
                name="archive" if index == 0 else "recent",
                query="old records" if index == 0 else "recent records",
                state=score if isinstance(score, FeatureState) else FeatureState.FRESH,
                similarity=None if isinstance(score, FeatureState) else score,
                provenance=provenance,
            )
            for index, score in enumerate(scores)
        ),
    )


def semantic_decision(policy, tier: str, *scores: float | FeatureState):
    return policy.evaluate_features(
        ObjectRecord(bucket="test", key="object.bin", size=100, tier=tier),
        features(*scores),
    )


@pytest.mark.parametrize("kind", ("simple", "content", "llm"))
@pytest.mark.parametrize(
    ("tier", "size", "destination"),
    [("hot", 110, None), ("hot", 111, "warm"), ("warm", 91, None), ("warm", 90, "hot")],
)
def test_size_boundaries_are_directional_and_inclusive(kind, tier, size, destination):
    decision = size_policy(kind).evaluate(tier, size)

    assert decision.dst_tier == destination
    assert decision.action == ("move" if destination else "stay")
    assert decision.hysteresis is not None
    check = decision.hysteresis["checks"][0]
    assert check["baseline_threshold"] == 100
    assert check["effective_threshold"] == (110 if tier == "hot" else 90)
    assert check["configured_band"] == 10
    assert decision.hysteresis["suppressed"] == (destination is None)


@pytest.mark.parametrize("kind", ("simple", "content", "llm"))
@pytest.mark.parametrize("initial_tier", ("hot", "warm"))
def test_noisy_size_sequences_do_not_flap_and_real_crossings_move(kind, initial_tier):
    for seed in range(12):
        random = Random(seed)
        policy = size_policy(kind)
        unguarded = size_policy(kind, band=0)
        tier = raw_tier = initial_tier
        raw_moves = 0
        for size in [random.randint(91, 109) for _ in range(250)]:
            decision = policy.evaluate(tier, size)
            raw_decision = unguarded.evaluate(raw_tier, size)
            assert decision.action == "stay"
            if raw_decision.dst_tier:
                raw_moves += 1
                raw_tier = raw_decision.dst_tier
        assert raw_moves > 50

        # Entry and exit happen once at their distinct boundaries.
        for size, expected in ((111, "warm"), (100, "warm"), (90, "hot"), (100, "hot")):
            decision = policy.evaluate(tier, size)
            tier = decision.dst_tier or tier
            assert tier == expected


@pytest.mark.parametrize("kind", ("simple", "content", "llm"))
def test_size_initial_placement_uses_base_threshold_and_respects_allowed_tiers(kind):
    policy = size_policy(kind, tiers=("hot", "warm", "cold"))
    assert policy.evaluate("cold", 100).dst_tier == "hot"
    assert policy.evaluate("cold", 101).dst_tier == "warm"
    assert size_policy(kind, tiers=("hot",)).evaluate("hot", 1000).action == "stay"
    assert size_policy(kind, tiers=("warm",)).evaluate("warm", 0).action == "stay"


@pytest.mark.parametrize("kind", ("simple", "content", "llm"))
def test_size_boundaries_are_not_clamped(kind):
    decision = size_policy(kind, threshold=5, band=10).evaluate("warm", 0)
    assert decision.action == "stay"
    assert decision.hysteresis["checks"][0]["effective_threshold"] == -5


@pytest.mark.parametrize("kind", ("simple", "content", "llm"))
def test_zero_size_band_preserves_threshold_behavior_and_has_no_evidence(kind):
    policy = size_policy(kind, band=0)
    for size in range(90, 112):
        for tier in ("hot", "warm", "cold"):
            decision = policy.evaluate(tier, size)
            expected = "hot" if size <= 100 else "warm"
            assert decision.dst_tier == (None if tier == expected else expected)
            assert decision.hysteresis is None


@pytest.mark.parametrize(
    ("tier", "score", "destination"),
    [
        ("warm", nextafter(0.625, 0), None),
        ("warm", 0.625, "cold"),
        ("cold", 0.375, None),
        ("cold", nextafter(0.375, 0), "warm"),
    ],
)
def test_similarity_has_distinct_entry_and_exit_boundaries(tier, score, destination):
    decision = semantic_decision(semantic_policy(), tier, score)
    assert decision.dst_tier == destination
    check = decision.hysteresis["checks"][0]
    assert check["baseline_threshold"] == 0.5
    assert check["effective_threshold"] == (0.375 if tier == "cold" else 0.625)


@pytest.mark.parametrize("initial_tier", ("warm", "cold"))
def test_noisy_similarity_sequences_do_not_flap_and_real_crossings_move(initial_tier):
    for seed in range(12):
        random = Random(seed)
        policy = semantic_policy()
        unguarded = semantic_policy(band=0)
        tier = raw_tier = initial_tier
        raw_moves = 0
        for score in [random.uniform(0.4, 0.6) for _ in range(250)]:
            decision = semantic_decision(policy, tier, score)
            raw_decision = semantic_decision(unguarded, raw_tier, score)
            assert decision.action == "stay"
            if raw_decision.dst_tier:
                raw_moves += 1
                raw_tier = raw_decision.dst_tier
        assert raw_moves > 50
        for score, expected in ((0.625, "cold"), (0.4, "cold"), (0.374, "warm"), (0.6, "warm")):
            decision = semantic_decision(policy, tier, score)
            tier = decision.dst_tier or tier
            assert tier == expected


def test_similarity_preserves_order_of_rules_and_retained_current_tier():
    rules = (
        EmbeddingPolicyRule("archive", "old records", 0.5, "cold"),
        EmbeddingPolicyRule("recent", "recent records", 0.5, "hot"),
    )
    policy = semantic_policy(rules=rules)
    first_match = semantic_decision(policy, "warm", 0.75, 1.0)
    retained = semantic_decision(policy, "cold", 0.4, 1.0)
    second_match = semantic_decision(policy, "warm", 0.6, 0.75)

    assert first_match.dst_tier == "cold"
    assert retained.action == "stay"
    assert len(retained.hysteresis["checks"]) == 1
    assert second_match.dst_tier == "hot"
    assert [check["rule"] for check in second_match.hysteresis["checks"]] == ["archive", "recent"]


@pytest.mark.parametrize("state", (FeatureState.MISSING, FeatureState.STALE, FeatureState.UNAVAILABLE))
def test_similarity_fails_closed_before_lower_priority_rules_and_size(state):
    policy = semantic_policy(rules=(
        EmbeddingPolicyRule("archive", "old records", 0.5, "cold"),
        EmbeddingPolicyRule("recent", "recent records", 0.5, "hot"),
    ))
    decision = semantic_decision(policy, "warm", state, 1.0)
    assert decision.action == "stay"
    assert decision.hysteresis is None
    assert f"embedding:archive={state.value}" in decision.reason


def test_similarity_hysteresis_does_not_affect_filename_precedence():
    policy = semantic_policy(hot_name_patterns=("*.bin",))
    decision = semantic_decision(policy, "cold", 1.0)
    assert decision.dst_tier == "hot"
    assert decision.hysteresis is None


def test_similarity_and_size_fallback_both_explain_their_boundaries():
    policy = semantic_policy(size_hysteresis_bytes=100)
    decision = semantic_decision(policy, "hot", 0.6)
    assert decision.action == "stay"
    assert [check["kind"] for check in decision.hysteresis["checks"]] == ["similarity", "size"]


def test_similarity_boundaries_remain_unclamped():
    upper = semantic_policy(rules=(EmbeddingPolicyRule("archive", "old records", 0.95, "cold"),))
    upper_decision = semantic_decision(upper, "warm", 1.0)
    assert upper_decision.action == "stay"
    assert upper_decision.hysteresis["checks"][0]["effective_threshold"] > 1

    lower = semantic_policy(rules=(EmbeddingPolicyRule("archive", "old records", -0.95, "cold"),))
    lower_decision = semantic_decision(lower, "cold", -1.0)
    assert lower_decision.action == "stay"
    assert lower_decision.hysteresis["checks"][0]["effective_threshold"] < -1


def test_zero_similarity_band_preserves_negative_zero_threshold_and_reason():
    policy = semantic_policy(
        band=0, rules=(EmbeddingPolicyRule("archive", "old records", -0.0, "cold"),),
    )
    decision = semantic_decision(policy, "warm", 0.0)
    assert decision.reason == "embedding archive similarity 0.000000 >= -0.000000 -> cold"
    assert decision.hysteresis is None


@pytest.mark.parametrize("kind", ("simple", "content", "llm", "content-semantic"))
@pytest.mark.parametrize("enabled", (True, False))
def test_snapshot_v1_does_not_claim_replay_for_direct_constructor_bands(kind, enabled):
    policy = (
        semantic_policy(band=0.125 if enabled else 0)
        if kind == "content-semantic"
        else size_policy(kind, band=10 if enabled else 0)
    )
    record = ObjectRecord(bucket="test", key="object.bin", size=100, tier="warm")
    projection = features(0.6)
    decision = (
        policy.evaluate_features(record, projection)
        if isinstance(policy, ContentAwarePolicy)
        else policy.evaluate(record.tier, record.size)
    )
    snapshot = capture_policy_snapshot(
        record=record,
        features=projection,
        policy=policy,
        allowed_tiers=tuple(policy.allowed_tiers),
        decision=decision,
        outcome="selected" if decision.action == "move" else "stayed",
        policy_name=kind,
        policy_version="1",
        decision_at="2026-09-10T12:00:00Z",
    )
    if enabled:
        assert snapshot["replay"] == {
            "supported": False, "reason": "hysteresis_not_in_snapshot_v1",
        }
        with pytest.raises(ValueError, match="hysteresis_not_in_snapshot_v1"):
            replay_policy_snapshot(snapshot)
    else:
        assert snapshot["replay"] == {"supported": True, "reason": None}
        assert replay_policy_snapshot(snapshot) == decision


def test_arbitrary_llm_does_not_receive_bands_or_supply_trusted_evidence():
    class Provider:
        def decide(self, inputs):
            assert inputs == {"current_tier": "hot", "size": 100, "allowed_tiers": ["hot", "warm"]}
            return {
                "action": "move", "dst_tier": "warm", "reason": "provider choice",
                "hysteresis": {"suppressed": True},
            }

    policy = LLMPolicy(Provider(), size_hysteresis_bytes=10)
    decision = policy.evaluate("hot", 100)
    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason == "llm_invalid_response"
    assert decision.hysteresis is None


def test_per_object_numeric_provider_copies_do_not_mutate_shared_provider():
    policy = size_policy("llm", band=0)
    guarded = copy(policy)
    guarded.size_hysteresis_bytes = 10
    assert guarded.provider is policy.provider
    assert guarded.evaluate("hot", 101).action == "stay"
    assert policy.evaluate("hot", 101).dst_tier == "warm"


@pytest.mark.parametrize("kind", ("simple", "content", "llm"))
@pytest.mark.parametrize("band", (-1, 1.5, True, "10", None, 2**63, 10**1000))
def test_size_band_rejects_invalid_values_at_construction_and_assignment(kind, band):
    with pytest.raises(ValueError, match="size_hysteresis_bytes"):
        size_policy(kind, band=band)
    policy = size_policy(kind)
    with pytest.raises(ValueError, match="size_hysteresis_bytes"):
        policy.size_hysteresis_bytes = band


@pytest.mark.parametrize("band", (-0.1, 2.1, float("inf"), float("nan"), True, "0.1", None, 10**1000))
def test_similarity_band_rejects_invalid_values_at_construction_and_assignment(band):
    with pytest.raises(ValueError, match="similarity_hysteresis"):
        semantic_policy(band=band)
    policy = semantic_policy()
    with pytest.raises(ValueError, match="similarity_hysteresis"):
        policy.similarity_hysteresis = band
