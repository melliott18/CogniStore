import pytest

from cognistore.core.policy import ContentAwarePolicy
from cognistore.core.catalog import ObjectRecord


def make_rec(key: str, tier: str, size: int, mime: str | None = None):
    return ObjectRecord(bucket="b", key=key, size=size, tier=tier, metadata={"mime": mime} if mime else {})


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
