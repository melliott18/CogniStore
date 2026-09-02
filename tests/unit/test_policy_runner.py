from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.move_jobs import MoveJobConflictError, MoveJobState
from cognistore.core.mover import (
    MoveGenerationMismatchError,
    Mover,
    MoveSourceContentMismatchError,
)
from cognistore.core.policy import (
    ContentAwarePolicy,
    EmbeddingPolicyRule,
    PolicyDecision,
    SimplePolicy,
)
from cognistore.core.policy_features import (
    CatalogPolicyFeatureLoader,
    EmbeddingPolicyFeature,
    FeatureState,
    PolicyFeatureProvenance,
)
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver


class UnsafePolicy:
    """Policy stub that deliberately ignores all placement constraints."""

    def __init__(self, destination: str):
        self.destination = destination

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        return PolicyDecision(
            action="move",
            dst_tier=self.destination,
            reason="unsafe test decision",
        )


def make_runner(tmp_path: Path, policy, *, allowed_tiers=None):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()
    mover = Mover(drivers, catalog)
    runner = PolicyRunner(
        catalog,
        drivers,
        mover,
        policy,
        allowed_tiers=allowed_tiers,
    )
    return runner, catalog, hot, warm


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
    assert {result.status for result in results} == {"completed"}

    # Validate storage side effects
    assert "small.txt" in list(hot.list_objects(bucket))
    assert "big.txt" in list(warm.list_objects(bucket))


def test_simple_policy_respects_allowed_destination_tiers():
    hot_only = SimplePolicy(size_threshold=5, allowed_tiers=("hot",))
    warm_only = SimplePolicy(size_threshold=5, allowed_tiers=("warm",))

    disallowed_warm = hot_only.evaluate(current_tier="hot", size=6)
    disallowed_hot = warm_only.evaluate(current_tier="warm", size=5)
    allowed_hot = hot_only.evaluate(current_tier="warm", size=5)

    assert disallowed_warm.action == "stay"
    assert disallowed_warm.dst_tier is None
    assert disallowed_hot.action == "stay"
    assert disallowed_hot.dst_tier is None
    assert allowed_hot.action == "move"
    assert allowed_hot.dst_tier == "hot"


def test_policy_runner_rejects_disallowed_destination_from_unsafe_policy(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        UnsafePolicy("warm"),
        allowed_tiers=("hot",),
    )
    bucket = "bk"
    key = "object.bin"
    data = b"source data"
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, size=len(data), tier="hot")

    results = runner.run_once(bucket)

    assert results == []
    assert hot.get_object(bucket, key) == data
    assert list(warm.list_objects(bucket)) == []
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "hot"


def test_policy_runner_rejects_unknown_allowed_tiers(tmp_path: Path):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()

    with pytest.raises(ValueError, match=r"Unknown allowed tier\(s\): archive, ghost"):
        PolicyRunner(
            catalog,
            drivers,
            Mover(drivers, catalog),
            SimplePolicy(),
            allowed_tiers=("ghost", "hot", "archive"),
        )


def test_policy_runner_dry_run_is_planned_and_has_no_mutations(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        SimplePolicy(size_threshold=5, allowed_tiers=("hot", "warm")),
        allowed_tiers=("hot", "warm"),
    )
    bucket = "bk"
    key = "large.bin"
    data = b"123456"
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, size=len(data), tier="hot")

    results = runner.run_once(bucket, dry_run=True)

    assert len(results) == 1
    assert results[0].status == "planned"
    assert results[0].from_tier == "hot"
    assert results[0].to_tier == "warm"
    assert hot.get_object(bucket, key) == data
    assert list(warm.list_objects(bucket)) == []
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "hot"
    assert record.size == len(data)
    assert results[0].features is not None
    assert results[0].features.schema_version == 1


def test_policy_runner_execution_is_completed_and_mutates_placement(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        SimplePolicy(size_threshold=5, allowed_tiers=("hot", "warm")),
        allowed_tiers=("hot", "warm"),
    )
    bucket = "bk"
    key = "large.bin"
    data = b"123456"
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, size=len(data), tier="hot")

    results = runner.run_once(bucket)

    assert len(results) == 1
    assert results[0].status == "completed"
    with pytest.raises(FileNotFoundError):
        hot.get_object(bucket, key)
    assert warm.get_object(bucket, key) == data
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "warm"
    assert record.size == len(data)


def test_policy_preview_reports_missing_features_and_fails_closed(
    tmp_path: Path,
) -> None:
    policy = ContentAwarePolicy(
        size_threshold=5,
        allowed_tiers=("hot", "warm"),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="active",
                query="frequently used material",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
        ),
    )
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        policy,
        allowed_tiers=("hot", "warm"),
    )
    data = b"small"
    warm.put_object("bk", "unknown.bin", data)
    catalog.upsert("bk", "unknown.bin", len(data), tier="warm")

    evaluations = runner.preview_once("bk")
    actions = runner.run_once("bk", dry_run=True)

    assert actions == []
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.action == "stay"
    assert evaluation.destination_tier is None
    assert evaluation.features.schema_version == 1
    assert evaluation.features.embeddings[0].state is FeatureState.MISSING
    assert evaluation.features.embeddings[0].provenance is not None
    assert "embedding:active=missing" in evaluation.reason
    assert warm.get_object("bk", "unknown.bin") == data
    assert list(hot.list_objects("bk")) == []
    assert catalog.list_audit_events() == []


def test_policy_decision_replay_is_idempotent_before_execution(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()
    data = b"123456"
    hot.put_object("bk", "large.bin", data)
    catalog.upsert("bk", "large.bin", len(data), tier="hot")
    context = AuditContext(
        correlation_id="replayed-policy-31",
        actor_type="worker",
        actor_id="00000000-0000-4000-8000-000000000031",
        job_id="00000000-0000-4000-8000-000000000031",
    )
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        SimplePolicy(size_threshold=5),
        idempotency_namespace=context.job_id,
        policy_name="simple",
        policy_version="1",
        audit_context=context,
        audit_occurred_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )

    first = runner.plan_once("bk")
    second = runner.plan_once("bk")

    assert first[0].decision_event_id == second[0].decision_event_id
    decisions = catalog.list_audit_events(
        AuditQuery(
            correlation_id=context.correlation_id,
            event_types=frozenset({AuditEventType.POLICY_DECISION}),
        )
    )
    assert len(decisions) == 1


def test_policy_runner_preflights_entire_batch_before_moving(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        SimplePolicy(size_threshold=5, allowed_tiers=("hot", "warm")),
        allowed_tiers=("hot", "warm"),
    )
    bucket = "bk"
    first_key = "first.bin"
    colliding_key = "second.bin"
    first_data = b"first-source"
    second_data = b"second-source"
    collision_data = b"existing-destination"

    hot.put_object(bucket, first_key, first_data)
    hot.put_object(bucket, colliding_key, second_data)
    warm.put_object(bucket, colliding_key, collision_data)
    catalog.upsert(bucket, first_key, size=len(first_data), tier="hot")
    catalog.upsert(bucket, colliding_key, size=len(second_data), tier="hot")

    with pytest.raises(FileExistsError, match="Destination object already exists"):
        runner.run_once(bucket)

    assert hot.get_object(bucket, first_key) == first_data
    assert hot.get_object(bucket, colliding_key) == second_data
    assert list(warm.list_objects(bucket)) == [colliding_key]
    assert warm.get_object(bucket, colliding_key) == collision_data
    assert catalog.get(bucket, first_key).tier == "hot"  # type: ignore[union-attr]
    assert catalog.get(bucket, colliding_key).tier == "hot"  # type: ignore[union-attr]


def test_policy_runner_fences_selected_move_to_evaluated_source_bytes(
    tmp_path: Path,
) -> None:
    bucket = "bk"
    key = "selected.bin"
    evaluated_bytes = b"old bytes"
    replacement_bytes = b"new bytes"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(evaluated_bytes),
        expected_size=len(evaluated_bytes),
    )
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()
    warm.put_object(bucket, key, evaluated_bytes)
    source_metadata = warm.stat_object(bucket, key)
    generation = source_metadata["generation"]
    assert isinstance(generation, str)
    catalog.upsert_scan_observation(
        bucket,
        key,
        size=len(evaluated_bytes),
        tier="warm",
        generation=generation,
        metadata={},
        fence=catalog.capture_scan_fence(bucket, key),
        content=content,
    )

    class ReplacingFeatureProvider:
        def load(self, records, requests):
            # Simulate replacement after the detached catalog snapshot has
            # been evaluated but before the mover performs its preflight.
            warm.put_object(bucket, key, replacement_bytes)
            return {
                (record.bucket, record.key): tuple(
                    EmbeddingPolicyFeature(
                        name=request.name,
                        query="stale query",
                        state=FeatureState.STALE,
                        provenance=PolicyFeatureProvenance(
                            source="test-provider",
                            source_version=1,
                            content_sha256="0" * 64,
                            details={"reason": "stale evidence"},
                        ),
                    )
                    for request in requests
                )
                for record in records
            }

    policy = ContentAwarePolicy(
        size_threshold=0,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=("*.bin",),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="active",
                query="active query",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
        ),
    )
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        policy,
        feature_loader=CatalogPolicyFeatureLoader(ReplacingFeatureProvider()),
    )

    with pytest.raises(MoveSourceContentMismatchError):
        runner.run_once(bucket)

    assert warm.get_object(bucket, key) == replacement_bytes
    with pytest.raises(FileNotFoundError):
        hot.get_object(bucket, key)
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "warm"


def test_policy_fence_rejects_replacement_between_hash_and_transfer(
    tmp_path: Path,
) -> None:
    bucket = "bk"
    key = "selected.bin"
    evaluated_bytes = b"old bytes"
    replacement_bytes = b"new bytes"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(evaluated_bytes),
        expected_size=len(evaluated_bytes),
    )

    class ReplacingPosixDriver(PosixDriver):
        bound_opens = 0

        def open_object_reader_if_generation(
            self,
            bucket: str,
            key: str,
            generation: str,
            range: str | None = None,
        ):
            self.bound_opens += 1
            if self.bound_opens == 2:
                self.put_object(bucket, key, replacement_bytes)
            return super().open_object_reader_if_generation(
                bucket,
                key,
                generation,
                range,
            )

    warm = ReplacingPosixDriver(str(tmp_path / "warm"))
    hot = PosixDriver(str(tmp_path / "hot"))
    catalog = Catalog()
    warm.put_object(bucket, key, evaluated_bytes)

    with pytest.raises(MoveGenerationMismatchError, match="source generation"):
        Mover({"hot": hot, "warm": warm}, catalog).move(
            "warm",
            "hot",
            bucket,
            key,
            idempotency_key="policy-fence-race",
            expected_source_sha256=content.sha256,
        )

    assert warm.get_object(bucket, key) == replacement_bytes
    with pytest.raises(FileNotFoundError):
        hot.get_object(bucket, key)


def test_policy_fence_leaves_destination_residue_when_source_changes_after_transfer(
    tmp_path: Path,
) -> None:
    bucket = "bk"
    key = "selected.bin"
    evaluated_bytes = b"old bytes"
    replacement_bytes = b"new bytes"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(evaluated_bytes),
        expected_size=len(evaluated_bytes),
    )

    class ReplacingAfterTransferPosixDriver(PosixDriver):
        bound_opens = 0

        def open_object_reader_if_generation(
            self,
            bucket: str,
            key: str,
            generation: str,
            range: str | None = None,
        ):
            self.bound_opens += 1
            reader = super().open_object_reader_if_generation(
                bucket,
                key,
                generation,
                range,
            )
            if self.bound_opens != 2:
                return reader

            @contextmanager
            def replace_after_reader_closes():
                with reader as stream:
                    yield stream
                self.put_object(bucket, key, replacement_bytes)

            return replace_after_reader_closes()

    warm = ReplacingAfterTransferPosixDriver(str(tmp_path / "warm"))
    hot = PosixDriver(str(tmp_path / "hot"))
    catalog = Catalog()
    warm.put_object(bucket, key, evaluated_bytes)
    catalog.upsert(bucket, key, len(evaluated_bytes), "warm")

    with pytest.raises(MoveGenerationMismatchError, match="source generation"):
        Mover({"hot": hot, "warm": warm}, catalog).move(
            "warm",
            "hot",
            bucket,
            key,
            idempotency_key="policy-fence-post-transfer-race",
            expected_source_sha256=content.sha256,
        )

    assert warm.get_object(bucket, key) == replacement_bytes
    assert hot.get_object(bucket, key) == evaluated_bytes
    placement = catalog.get(bucket, key)
    assert placement is not None
    assert placement.tier == "warm"


def test_policy_fence_never_discards_a_replaced_destination_generation(
    tmp_path: Path,
) -> None:
    bucket = "bk"
    key = "selected.bin"
    evaluated_bytes = b"old bytes"
    replacement_bytes = b"new source bytes"
    concurrent_destination_bytes = b"concurrent destination bytes"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(evaluated_bytes),
        expected_size=len(evaluated_bytes),
    )
    replacement_destination_generation: str | None = None

    class ReplacingSourceAfterTransfer(PosixDriver):
        bound_opens = 0

        def open_object_reader_if_generation(
            self,
            bucket: str,
            key: str,
            generation: str,
            range: str | None = None,
        ):
            self.bound_opens += 1
            reader = super().open_object_reader_if_generation(
                bucket,
                key,
                generation,
                range,
            )
            if self.bound_opens != 2:
                return reader

            @contextmanager
            def replace_after_reader_closes():
                with reader as stream:
                    yield stream
                self.put_object(bucket, key, replacement_bytes)

            return replace_after_reader_closes()

    class ReplacingDestinationBeforeReturn(PosixDriver):
        def put_object_stream(
            self,
            bucket: str,
            key: str,
            source,
            *,
            size: int,
            overwrite: bool = True,
            metadata=None,
        ) -> int:
            nonlocal replacement_destination_generation
            transferred = PosixDriver.put_object_stream(
                self,
                bucket,
                key,
                source,
                size=size,
                overwrite=overwrite,
                metadata=metadata,
            )
            PosixDriver.put_object_stream(
                self,
                bucket,
                key,
                BytesIO(concurrent_destination_bytes),
                size=len(concurrent_destination_bytes),
                overwrite=True,
            )
            replacement_destination_generation = self.object_generation(bucket, key)
            return transferred

    warm = ReplacingSourceAfterTransfer(str(tmp_path / "warm"))
    hot = ReplacingDestinationBeforeReturn(str(tmp_path / "hot"))
    catalog = Catalog()
    warm.put_object(bucket, key, evaluated_bytes)

    with pytest.raises(MoveGenerationMismatchError, match="source generation"):
        Mover({"hot": hot, "warm": warm}, catalog).move(
            "warm",
            "hot",
            bucket,
            key,
            idempotency_key="policy-fence-destination-replacement",
            expected_source_sha256=content.sha256,
        )

    assert warm.get_object(bucket, key) == replacement_bytes
    assert hot.get_object(bucket, key) == concurrent_destination_bytes
    assert replacement_destination_generation is not None
    assert hot.object_generation(bucket, key) == replacement_destination_generation


@pytest.mark.parametrize("initially_fenced", [False, True])
def test_expected_source_digest_is_part_of_durable_move_contract(
    tmp_path: Path,
    initially_fenced: bool,
) -> None:
    bucket = "bk"
    key = "selected.bin"
    payload = b"source bytes"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    warm = PosixDriver(str(tmp_path / "warm"))
    hot = PosixDriver(str(tmp_path / "hot"))
    catalog = Catalog()
    warm.put_object(bucket, key, payload)

    class StopAfterClaim(RuntimeError):
        pass

    def stop_after_claim(_job) -> None:
        raise StopAfterClaim

    with pytest.raises(StopAfterClaim):
        Mover(
            {"hot": hot, "warm": warm},
            catalog,
            transition_hook=stop_after_claim,
        ).move(
            "warm",
            "hot",
            bucket,
            key,
            idempotency_key="durable-policy-fence",
            expected_source_sha256=(content.sha256 if initially_fenced else None),
        )

    retry_digest = "0" * 64 if initially_fenced else content.sha256
    with pytest.raises(MoveSourceContentMismatchError):
        Mover({"hot": hot, "warm": warm}, catalog).move(
            "warm",
            "hot",
            bucket,
            key,
            idempotency_key="durable-policy-fence",
            expected_source_sha256=retry_digest,
        )

    assert warm.get_object(bucket, key) == payload
    with pytest.raises(FileNotFoundError):
        hot.get_object(bucket, key)


def test_expected_digest_is_revalidated_after_atomic_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bucket = "bk"
    key = "selected.bin"
    evaluated_bytes = b"old bytes"
    replacement_bytes = b"new bytes"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(evaluated_bytes),
        expected_size=len(evaluated_bytes),
    )
    warm = PosixDriver(str(tmp_path / "warm"))
    hot = PosixDriver(str(tmp_path / "hot"))
    catalog = Catalog()
    warm.put_object(bucket, key, evaluated_bytes)
    original_claim = catalog.claim_move_job
    injected = False

    def substitute_contract(idempotency_key: str, **arguments):
        nonlocal injected
        if not injected:
            injected = True
            warm.put_object(bucket, key, replacement_bytes)
            replacement_metadata = warm.stat_object(bucket, key)
            original_claim(
                idempotency_key,
                src_tier=arguments["src_tier"],
                dst_tier=arguments["dst_tier"],
                bucket=arguments["bucket"],
                key=arguments["key"],
                expected_size=len(replacement_bytes),
                source_metadata=replacement_metadata,
                owner_id="competing-owner",
                now=arguments["now"],
                lease_expires_at=arguments["lease_expires_at"],
                audit_context=arguments["audit_context"],
            )
        return original_claim(idempotency_key, **arguments)

    monkeypatch.setattr(catalog, "claim_move_job", substitute_contract)

    with pytest.raises(MoveJobConflictError, match="different expected source digest"):
        Mover({"hot": hot, "warm": warm}, catalog).move(
            "warm",
            "hot",
            bucket,
            key,
            idempotency_key="claim-substitution-race",
            expected_source_sha256=content.sha256,
        )

    assert warm.get_object(bucket, key) == replacement_bytes
    with pytest.raises(FileNotFoundError):
        hot.get_object(bucket, key)
    claimed = catalog.get_move_job("claim-substitution-race")
    assert claimed is not None
    assert claimed.state == MoveJobState.PREPARED
    assert claimed.owner_id == "competing-owner"
