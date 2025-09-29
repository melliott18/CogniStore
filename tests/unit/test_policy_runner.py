from pathlib import Path

from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver


def test_policy_runner_moves_objects(tmp_path: Path):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}

    catalog = Catalog()
    mover = Mover(drivers, catalog)
    # Threshold 5 bytes: <=5 goes to hot, >5 goes to warm
    policy = SimplePolicy(size_threshold=5)
    runner = PolicyRunner(catalog, drivers, mover, policy)

    bucket = "bk"
    # Prepare objects in storage to match the catalog's current tier
    warm.put_object(bucket, "small.txt", b"12345")  # size 5, currently in warm
    hot.put_object(bucket, "big.txt", b"123456")   # size 6, currently in hot
    catalog.upsert(bucket, "small.txt", 5, tier="warm")  # policy should move to hot
    catalog.upsert(bucket, "big.txt", 6, tier="hot")     # policy should move to warm

    results = runner.run_once(bucket)
    # Expect 2 moves: small -> hot, big -> warm
    keys = sorted([(r.key, r.from_tier, r.to_tier) for r in results])
    assert keys == [("big.txt", "hot", "warm"), ("small.txt", "warm", "hot")]

    # Validate storage side effects
    assert "small.txt" in list(hot.list_objects(bucket))
    assert "big.txt" in list(warm.list_objects(bucket))
