import json

import pytest

from cognistore.core.catalog import ObjectRecord
from cognistore.core.placement_llm import PlacementProviderUnavailable
from cognistore.core.policy import (
    ContentAwarePolicy,
    EmbeddingPolicyRule,
    LLMPolicy,
    PolicyDecision,
    SimplePolicy,
)
from cognistore.core.policy_factory import ThresholdProvider
from cognistore.core.policy_features import (
    EmbeddingPolicyFeature,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)
from cognistore.core.policy_reasons import DecisiveSignal


def signal(name, value, operator=None, threshold=None, rule_index=None):
    return {
        "name": name,
        "value": value,
        "operator": operator,
        "threshold": threshold,
        "rule_index": rule_index,
    }


def features(*embeddings, mime=None, state=FeatureState.MISSING):
    return PolicyFeatures(
        mime=MimePolicyFeature(
            state=state,
            provenance=PolicyFeatureProvenance("private-mime-provider", 1, None),
            value=mime,
        ),
        embeddings=embeddings,
    )


def embedding(rule, similarity=None, state=FeatureState.FRESH):
    return EmbeddingPolicyFeature(
        name=rule.name,
        query=rule.query,
        state=state,
        provenance=PolicyFeatureProvenance("private-embedding-provider", 1, None),
        similarity=similarity,
    )


def assert_safe_signals(decision):
    for item in decision.decisive_signals:
        assert DecisiveSignal.model_validate(item).model_dump() == item
    serialized = json.dumps(decision.decisive_signals, allow_nan=False)
    assert "private" not in serialized


def test_decision_preserves_legacy_position_equality_and_separate_signal_lists():
    audit = {"fallback_reason": None}
    legacy = PolicyDecision("stay", "unchanged", None, {"suppressed": False}, audit)
    explained = PolicyDecision(
        "stay", "unchanged", None, {"suppressed": False}, audit,
        reason_code="size_threshold",
        decisive_signals=[signal("size_bytes", 5, "<=", 10)],
    )
    assert legacy == explained
    assert legacy.reason_code is None
    assert legacy.proposed_dst_tier is None
    assert legacy.hysteresis == {"suppressed": False}
    assert legacy.llm_audit is audit
    assert legacy.decisive_signals == []
    another = PolicyDecision("stay", "another")
    legacy.decisive_signals.append(signal("name_match", True, rule_index=0))
    assert another.decisive_signals == []
    explained.proposed_dst_tier = "warm"
    legacy.decisive_signals.clear()
    assert legacy == explained


@pytest.mark.parametrize("kind", ["simple", "content", "threshold-provider"])
@pytest.mark.parametrize(
    "tier,size,band,action,operator,boundary",
    [
        ("warm", 10, 0, "move", "<=", 10),
        ("hot", 11, 0, "move", ">", 10),
        ("hot", 10, 0, "stay", "<=", 10),
        ("hot", 11, 2, "stay", "<=", 12),
    ],
)
def test_size_signals_use_the_effective_boundary(kind, tier, size, band, action, operator, boundary):
    if kind == "simple":
        policy = SimplePolicy(size_threshold=10, size_hysteresis_bytes=band)
    elif kind == "content":
        policy = ContentAwarePolicy(size_threshold=10, size_hysteresis_bytes=band)
    else:
        policy = LLMPolicy(ThresholdProvider(10, ("hot", "warm")), size_hysteresis_bytes=band)
    decision = policy.evaluate(tier, size)
    assert decision.action == action
    assert decision.reason_code == "size_threshold"
    assert decision.decisive_signals == [signal("size_bytes", size, operator, boundary)]
    if band:
        assert decision.hysteresis["suppressed"] is True
    assert_safe_signals(decision)


@pytest.mark.parametrize("tier,action", [("hot", "move"), ("warm", "stay")])
def test_name_evidence_uses_family_ordinal_and_preserves_precedence(tier, action):
    policy = ContentAwarePolicy(
        hot_name_patterns=["private-no-match-*"],
        warm_name_patterns=["private-other-*", "private-*.zip"],
        hot_mime_prefixes=["private/missing"],
    )
    decision = policy.evaluate_features(
        ObjectRecord("private-bucket", "private-secret.zip", 1, tier), features(),
    )
    assert decision.action == action
    assert decision.reason_code == "name_rule"
    assert decision.decisive_signals == [signal("name_match", True, rule_index=2)]
    assert_safe_signals(decision)


def test_mime_evidence_exposes_match_and_freshness_without_mime_text():
    policy = ContentAwarePolicy(
        hot_mime_prefixes=["private/unmatched"],
        warm_mime_prefixes=["private/other", "private/mime"],
    )
    decision = policy.evaluate_features(
        ObjectRecord("private-bucket", "private-key", 1, "hot"),
        features(mime="private/mime-secret", state=FeatureState.FRESH),
    )
    assert decision.action == "move"
    assert decision.reason == "mime private/mime-secret -> warm"
    assert decision.reason_code == "mime_rule"
    assert decision.decisive_signals == [
        signal("mime_state", "fresh"), signal("mime_match", True, rule_index=2),
    ]
    assert_safe_signals(decision)


@pytest.mark.parametrize("state", [FeatureState.MISSING, FeatureState.STALE, FeatureState.UNAVAILABLE])
@pytest.mark.parametrize("kind", ["mime", "embedding"])
def test_required_features_expose_only_feature_state(kind, state):
    rule = EmbeddingPolicyRule("private-rule", "private query", 0.5, "warm")
    policy = ContentAwarePolicy(
        hot_mime_prefixes=["private/mime"] if kind == "mime" else [],
        embedding_rules=[rule] if kind == "embedding" else [],
    )
    projection = features(state=state) if kind == "mime" else features(embedding(rule, state=state))
    decision = policy.evaluate_features(ObjectRecord("b", "private-key", 100, "hot"), projection)
    assert decision.action == "stay"
    assert decision.reason_code == "required_features_unavailable"
    assert decision.decisive_signals == [
        signal(f"{kind}_state", state.value, rule_index=0 if kind == "embedding" else None),
    ]
    assert_safe_signals(decision)


def test_bypassed_features_are_distinct_from_size_fallback():
    policy = ContentAwarePolicy(hot_mime_prefixes=["private/mime"])
    decision = policy.evaluate("hot", 100_000_000)
    assert decision.reason_code == "required_features_unavailable"
    assert decision.decisive_signals == [signal("features_evaluated", False)]
    assert_safe_signals(decision)


def test_embedding_signal_uses_ordinal_and_effective_similarity_threshold():
    rules = [
        EmbeddingPolicyRule("private-first", "private query one", 0.5, "hot"),
        EmbeddingPolicyRule("private-second", "private query two", 0.5, "warm"),
    ]
    policy = ContentAwarePolicy(embedding_rules=rules, similarity_hysteresis=0.1)
    decision = policy.evaluate_features(
        ObjectRecord("private-bucket", "private-key", 1, "hot"),
        features(embedding(rules[0], 0.1), embedding(rules[1], 0.7)),
    )
    assert decision.action == "move"
    assert decision.reason_code == "embedding_rule"
    assert decision.decisive_signals == [
        signal("embedding_state", "fresh", rule_index=1),
        signal("embedding_similarity", 0.7, ">=", 0.6, 1),
    ]
    assert len(decision.hysteresis["checks"]) == 2
    assert_safe_signals(decision)


def test_fresh_nonmatching_embedding_retains_size_fallback_and_hysteresis():
    rule = EmbeddingPolicyRule("private-rule", "private query", 0.5, "warm")
    policy = ContentAwarePolicy(
        embedding_rules=[rule], size_threshold=10, similarity_hysteresis=0.1,
    )
    decision = policy.evaluate_features(
        ObjectRecord("b", "private-key", 11, "hot"), features(embedding(rule, 0.55)),
    )
    assert decision.action == "move"
    assert decision.reason_code == "size_threshold"
    assert decision.decisive_signals == [signal("size_bytes", 11, ">", 10)]
    assert decision.hysteresis["suppressed"] is True
    assert_safe_signals(decision)


class Provider:
    provider_id = "test"
    model = "private-provider"
    version = "1"

    def __init__(self, result):
        self.result = result

    def complete(self, prompt, schema, timeout_seconds):
        if isinstance(self.result, Exception):
            raise self.result
        return json.dumps(self.result, allow_nan=False)


@pytest.mark.parametrize(
    "result,action,code",
    [
        ({"action": "stay", "dst_tier": None, "reason": "private provider reason"}, "stay", "provider_decision"),
        ({"action": "stay", "reason": "private provider reason"}, "stay", "provider_invalid_response"),
        ({"action": "stay", "dst_tier": None}, "stay", "provider_invalid_response"),
        ({"action": "stay", "dst_tier": "hot", "reason": "private"}, "stay", "provider_invalid_response"),
        ({"action": "stay", "dst_tier": 123, "reason": "private"}, "stay", "provider_invalid_response"),
        ({"action": "stay", "dst_tier": True, "reason": "private"}, "stay", "provider_invalid_response"),
        ({"action": "stay", "dst_tier": [], "reason": "private"}, "stay", "provider_invalid_response"),
        ({"action": "stay", "dst_tier": {}, "reason": "private"}, "stay", "provider_invalid_response"),
        ({"action": "move", "dst_tier": "warm", "reason": "private provider reason"}, "move", "provider_decision"),
        ({"action": "invalid"}, "stay", "provider_invalid_response"),
        ({"action": "move"}, "stay", "provider_invalid_response"),
        ({"action": "move", "dst_tier": "private-tier"}, "stay", "provider_invalid_response"),
        ({"action": "move", "dst_tier": "hot"}, "stay", "provider_invalid_response"),
        ({}, "stay", "provider_invalid_response"),
        (None, "stay", "provider_invalid_response"),
        (["private output"], "stay", "provider_invalid_response"),
        ("private output", "stay", "provider_invalid_response"),
        (42, "stay", "provider_invalid_response"),
        (RuntimeError("private credentials"), "stay", "provider_error"),
        (TimeoutError("private credentials"), "stay", "provider_error"),
        (PlacementProviderUnavailable("private credentials"), "stay", "provider_error"),
    ],
)
def test_external_provider_reason_codes_distinguish_valid_stay_invalid_and_error(result, action, code):
    decision = LLMPolicy(Provider(result)).evaluate("hot", 1)
    assert decision.action == action
    assert decision.reason_code == code
    assert decision.decisive_signals == []
    assert decision.llm_audit["final_proposal"]["action"] == action
    assert (decision.llm_audit["fallback_reason"] is None) == (code == "provider_decision")
    assert_safe_signals(decision)


def test_unavailable_provider_has_error_code_and_keeps_inference_audit():
    decision = LLMPolicy().evaluate("hot", 1)
    assert decision.action == "stay"
    assert decision.reason_code == "provider_error"
    assert decision.llm_audit["fallback_reason"] == "llm_provider_unavailable"
    assert decision.decisive_signals == []


def test_invalid_inference_input_has_distinct_code_and_keeps_inference_audit():
    decision = LLMPolicy(Provider({
        "action": "move", "dst_tier": "warm", "reason": "private provider reason",
    })).evaluate("hot", -1)
    assert decision.action == "stay"
    assert decision.reason_code == "provider_invalid_input"
    assert decision.llm_audit["fallback_reason"] == "llm_invalid_input"
    assert decision.llm_audit["attempts"] == 0
    assert decision.decisive_signals == []


def test_external_provider_cannot_supply_trusted_evidence():
    decision = LLMPolicy(Provider({
        "action": "move",
        "dst_tier": "warm",
        "reason": "private provider text",
        "reason_code": "embedding_rule",
        "decisive_signals": [{"private": "secret"}],
        "hysteresis": {"private": "secret"},
        "confidence": 1.0,
    })).evaluate("hot", 1)
    assert decision.action == "stay"
    assert decision.reason == "llm_invalid_response"
    assert decision.reason_code == "provider_invalid_response"
    assert decision.llm_audit["validation_errors"] == ["invalid_fields"]
    assert decision.decisive_signals == []
    assert decision.hysteresis is None


@pytest.mark.parametrize("kind", ["simple", "content", "threshold-provider", "threshold-wrapper"])
def test_disallowed_size_destination_retains_its_proposal_without_changing_action(kind):
    if kind == "simple":
        policy = SimplePolicy(size_threshold=10, allowed_tiers=("hot",))
    elif kind == "content":
        policy = ContentAwarePolicy(size_threshold=10, allowed_tiers=("hot",))
    else:
        provider_tiers = ("hot",) if kind == "threshold-provider" else ("hot", "warm")
        policy = LLMPolicy(ThresholdProvider(10, provider_tiers), allowed_tiers=("hot",))
    decision = policy.evaluate("hot", 11)
    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason_code == "destination_not_allowed"
    assert decision.proposed_dst_tier == "warm"
    assert decision.decisive_signals == [signal("size_bytes", 11, ">", 10)]
    assert_safe_signals(decision)


@pytest.mark.parametrize("kind", ["name", "mime", "embedding"])
@pytest.mark.parametrize("current,code,proposal", [
    ("hot", "destination_not_allowed", "warm"),
    ("warm", None, None),
])
def test_content_rule_distinguishes_disallowed_destination_from_current_tier(kind, current, code, proposal):
    rule = EmbeddingPolicyRule("private-rule", "private query", 0.5, "warm")
    policy = ContentAwarePolicy(
        warm_name_patterns=["private-*"] if kind == "name" else [],
        warm_mime_prefixes=["private/mime"] if kind == "mime" else [],
        embedding_rules=[rule] if kind == "embedding" else [],
    )
    policy.allowed_tiers = ("hot",)
    decision = policy.evaluate_features(
        ObjectRecord("b", "private-key", 100, current),
        features(
            embedding(rule, 0.9), mime="private/mime", state=FeatureState.FRESH,
        ),
    )
    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason_code == (code or f"{kind}_rule")
    assert decision.proposed_dst_tier == proposal
    assert_safe_signals(decision)


@pytest.mark.parametrize("kind", ["simple", "content", "threshold-provider"])
def test_size_current_tier_stay_is_not_a_disallowed_proposal(kind):
    if kind == "simple":
        policy = SimplePolicy(size_threshold=10, allowed_tiers=("warm",))
    elif kind == "content":
        policy = ContentAwarePolicy(size_threshold=10, allowed_tiers=("warm",))
    else:
        policy = LLMPolicy(ThresholdProvider(10, ("warm",)), allowed_tiers=("warm",))
    decision = policy.evaluate("hot", 1)
    assert decision.action == "stay"
    assert decision.reason_code == "size_threshold"
    assert decision.proposed_dst_tier is None
