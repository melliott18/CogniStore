import hashlib

import pytest

from cognistore.core.catalog import ObjectRecord
from cognistore.core.policy import (
    ContentAwarePolicy,
    EmbeddingPolicyRule,
)
from cognistore.core.policy_features import (
    EmbeddingPolicyFeature,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)


def make_rec(key: str, tier: str, size: int, mime: str | None = None):
    metadata: dict[str, object] = {}
    if mime is not None:
        digest = hashlib.sha256(f"{key}:{size}".encode("utf-8")).hexdigest()
        metadata = {
            "mime": mime,
            "sha256": digest,
            "content_identity": {
                "schema_version": 1,
                "representation": "source-bytes",
                "digest_algorithm": "sha256",
                "sha256": digest,
                "size": size,
            },
            "mime_detection": {
                "schema_version": 1,
                "mime": mime,
                "detector": "libmagic",
                "provenance": "content",
                "confidence": "high",
                "content_mime": mime,
                "filename_mime": mime,
                "filename_encoding": None,
                "disagreement": False,
                "status": "detected",
                "fallback_reason": None,
            },
        }
    return ObjectRecord(
        bucket="b",
        key=key,
        size=size,
        tier=tier,
        metadata=metadata,
    )


def _provenance(source: str = "test") -> PolicyFeatureProvenance:
    return PolicyFeatureProvenance(
        source=source,
        source_version=1,
        content_sha256=None,
        details={},
    )


def _features(
    *embeddings: EmbeddingPolicyFeature,
    mime: str | None = None,
    mime_state: FeatureState = FeatureState.MISSING,
) -> PolicyFeatures:
    return PolicyFeatures(
        mime=MimePolicyFeature(
            state=mime_state,
            value=mime,
            provenance=_provenance("mime-test"),
        ),
        embeddings=embeddings,
    )


def test_content_policy_name_patterns_first_match():
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=["*.hot.txt"],
        warm_name_patterns=["*.zip"],
        size_threshold=10,
    )
    # Matches hot name pattern even if size would suggest warm
    rec1 = make_rec("a.hot.txt", tier="warm", size=100, mime="text/plain")
    dec1 = policy.evaluate_record(rec1)
    assert dec1.action == "move" and dec1.dst_tier == "hot"

    # Matches warm name pattern despite small size
    rec2 = make_rec("archive.zip", tier="hot", size=1, mime="application/zip")
    dec2 = policy.evaluate_record(rec2)
    assert dec2.action == "move" and dec2.dst_tier == "warm"


def test_content_policy_mime_prefixes():
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        hot_mime_prefixes=["text/"],
        warm_mime_prefixes=["application/zip"],
        size_threshold=10,
    )
    # text/plain should go hot
    rec1 = make_rec("notes.txt", tier="warm", size=100, mime="text/plain")
    dec1 = policy.evaluate_record(rec1)
    assert dec1.action == "move" and dec1.dst_tier == "hot"

    # application/zip should go warm
    rec2 = make_rec("archive.bin", tier="hot", size=1, mime="application/zip")
    dec2 = policy.evaluate_record(rec2)
    assert dec2.action == "move" and dec2.dst_tier == "warm"


def test_content_policy_fails_closed_for_unbound_bare_mime() -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        hot_mime_prefixes=("text/",),
        size_threshold=10,
    )
    record = ObjectRecord(
        bucket="b",
        key="notes.txt",
        size=100,
        tier="warm",
        metadata={"mime": "text/plain"},
    )

    decision = policy.evaluate_record(record)

    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason == "required policy features unavailable: mime=stale"


def test_content_policy_size_fallback():
    policy = ContentAwarePolicy(allowed_tiers=("hot", "warm"), size_threshold=5)
    # small -> hot
    rec1 = make_rec("x.bin", tier="warm", size=5)
    dec1 = policy.evaluate_record(rec1)
    assert dec1.action == "move" and dec1.dst_tier == "hot"

    # large -> warm
    rec2 = make_rec("x.bin", tier="hot", size=6)
    dec2 = policy.evaluate_record(rec2)
    assert dec2.action == "move" and dec2.dst_tier == "warm"


@pytest.mark.parametrize(
    ("policy_kwargs", "record"),
    [
        (
            {"warm_name_patterns": ["*.zip"], "size_threshold": 5},
            make_rec("archive.zip", tier="hot", size=6),
        ),
        (
            {"warm_mime_prefixes": ["application/zip"], "size_threshold": 5},
            make_rec("archive.bin", tier="hot", size=6, mime="application/zip"),
        ),
        (
            {"size_threshold": 5},
            make_rec("large.bin", tier="hot", size=6),
        ),
    ],
    ids=("name-rule", "mime-rule", "size-fallback"),
)
def test_content_policy_rejects_disallowed_destination(policy_kwargs, record):
    policy = ContentAwarePolicy(allowed_tiers=("hot",), **policy_kwargs)

    decision = policy.evaluate_record(record)

    assert decision.action == "stay"
    assert decision.dst_tier is None


def test_content_policy_rejects_disallowed_cold_destination():
    policy = ContentAwarePolicy(allowed_tiers=("hot",), size_threshold=5)
    policy.cold_name_patterns = ["*.archive"]

    decision = policy.evaluate_record(
        make_rec("logs.archive", tier="hot", size=6),
    )

    assert decision.action == "stay"
    assert decision.dst_tier is None


def test_content_policy_legacy_allowed_set_remains_mutable_and_synchronized():
    policy = ContentAwarePolicy(allowed_tiers=("hot",), size_threshold=5)
    record = make_rec("large.bin", tier="hot", size=6)

    assert policy.evaluate_record(record).action == "stay"

    policy.allowed.add("warm")
    decision = policy.evaluate_record(record)

    assert policy.allowed_tiers == {"hot", "warm"}
    assert decision.action == "move"
    assert decision.dst_tier == "warm"


def test_feature_projection_drives_mime_and_embedding_rules() -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm", "cold"),
        size_threshold=0,
        hot_mime_prefixes=("text/",),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="archive",
                query="historical records",
                minimum_similarity=0.75,
                destination_tier="cold",
            ),
        ),
    )
    record = make_rec("report.bin", tier="warm", size=10)

    mime_decision = policy.evaluate_features(
        record,
        _features(mime="text/plain", mime_state=FeatureState.FRESH),
    )
    embedding_decision = policy.evaluate_features(
        record,
        _features(
            EmbeddingPolicyFeature(
                name="archive",
                query="historical records",
                state=FeatureState.FRESH,
                similarity=0.9,
                provenance=_provenance("embedding-test"),
            ),
            mime="application/pdf",
            mime_state=FeatureState.FRESH,
        ),
    )

    assert (mime_decision.action, mime_decision.dst_tier) == ("move", "hot")
    assert (embedding_decision.action, embedding_decision.dst_tier) == (
        "move",
        "cold",
    )


@pytest.mark.parametrize(
    "state",
    [FeatureState.MISSING, FeatureState.STALE, FeatureState.UNAVAILABLE],
)
def test_nonfresh_mime_blocks_a_lower_priority_embedding_match(
    state: FeatureState,
) -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm", "cold"),
        hot_mime_prefixes=("text/",),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="archive",
                query="historical records",
                minimum_similarity=0.75,
                destination_tier="cold",
            ),
        ),
    )
    record = make_rec("report.bin", tier="warm", size=10)

    decision = policy.evaluate_features(
        record,
        _features(
            EmbeddingPolicyFeature(
                name="archive",
                query="historical records",
                state=FeatureState.FRESH,
                similarity=0.9,
                provenance=_provenance("embedding-test"),
            ),
            mime_state=state,
        ),
    )

    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason == f"required policy features unavailable: mime={state.value}"


@pytest.mark.parametrize(
    "state",
    [FeatureState.MISSING, FeatureState.STALE, FeatureState.UNAVAILABLE],
)
def test_nonfresh_embedding_blocks_a_lower_priority_embedding_match(
    state: FeatureState,
) -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm", "cold"),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="active",
                query="frequently used material",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
            EmbeddingPolicyRule(
                name="archive",
                query="historical records",
                minimum_similarity=0.75,
                destination_tier="cold",
            ),
        ),
    )
    record = make_rec("report.bin", tier="warm", size=10)

    decision = policy.evaluate_features(
        record,
        _features(
            EmbeddingPolicyFeature(
                name="active",
                query="frequently used material",
                state=state,
                provenance=_provenance("embedding-test"),
            ),
            EmbeddingPolicyFeature(
                name="archive",
                query="historical records",
                state=FeatureState.FRESH,
                similarity=0.9,
                provenance=_provenance("embedding-test"),
            ),
        ),
    )

    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason == (
        f"required policy features unavailable: embedding:active={state.value}"
    )


@pytest.mark.parametrize(
    "state",
    [FeatureState.MISSING, FeatureState.STALE, FeatureState.UNAVAILABLE],
)
def test_embedding_feature_failure_fails_closed_before_size_fallback(
    state: FeatureState,
) -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        size_threshold=100,
        embedding_rules=(
            EmbeddingPolicyRule(
                name="active",
                query="frequently used material",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
        ),
    )
    record = make_rec("unknown.bin", tier="warm", size=1)

    decision = policy.evaluate_features(
        record,
        _features(
            EmbeddingPolicyFeature(
                name="active",
                query="frequently used material",
                state=state,
                provenance=_provenance("embedding-test"),
            )
        ),
    )

    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert f"embedding:active={state.value}" in decision.reason


def test_fresh_embedding_nonmatch_uses_deterministic_size_fallback() -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        size_threshold=5,
        embedding_rules=(
            EmbeddingPolicyRule(
                name="active",
                query="frequently used material",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
        ),
    )
    record = make_rec("large.bin", tier="hot", size=6)

    decision = policy.evaluate_features(
        record,
        _features(
            EmbeddingPolicyFeature(
                name="active",
                query="frequently used material",
                state=FeatureState.FRESH,
                similarity=0.2,
                provenance=_provenance("embedding-test"),
            )
        ),
    )

    assert (decision.action, decision.dst_tier) == ("move", "warm")


def test_legacy_evaluation_paths_fail_closed_for_unevaluated_embedding_rules() -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        size_threshold=5,
        embedding_rules=(
            EmbeddingPolicyRule(
                name="active",
                query="frequently used material",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
        ),
    )
    record = make_rec("small.bin", tier="warm", size=1)

    direct = policy.evaluate(record.tier, record.size)
    record_aware = policy.evaluate_record(record)

    assert direct.action == "stay"
    assert direct.dst_tier is None
    assert direct.reason == (
        "required policy features unavailable: features not evaluated"
    )
    assert record_aware.action == "stay"
    assert record_aware.dst_tier is None
    assert record_aware.reason == (
        "required policy features unavailable: embedding:active=missing"
    )


def test_legacy_record_path_rejects_stale_mime_envelope() -> None:
    policy = ContentAwarePolicy(
        allowed_tiers=("hot", "warm"),
        hot_mime_prefixes=("text/",),
        size_threshold=5,
    )
    record = make_rec("small.txt", tier="warm", size=1, mime="text/plain")
    record.metadata["mime_detection"] = {"schema_version": 1}

    decision = policy.evaluate_record(record)

    assert decision.action == "stay"
    assert decision.dst_tier is None
    assert decision.reason == "required policy features unavailable: mime=stale"
    direct = policy.evaluate(record.tier, record.size)
    assert direct.action == "stay"
    assert direct.reason == (
        "required policy features unavailable: features not evaluated"
    )
