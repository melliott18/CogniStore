from cognistore.core.policy import ContentAwarePolicy, PolicyDecision
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
