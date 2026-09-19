"""Locality is revalidated at durable movement boundaries on both catalog DALs."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy, authorization_context
from cognistore.auth.principal import Principal, principal_context
from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog
from cognistore.core.locality import LocalityConstraintError
from cognistore.core.move_jobs import MoveJobConflictError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.db.catalog import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
BUCKET = "bucket"
KEY = "regulated/object.bin"
PAYLOAD = b"bytes that must remain within their approved locality"
PRINCIPAL = Principal("https://issuer.example", "approved-operator")


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request, tmp_path):
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLCatalog(tmp_path / "catalog.sqlite3") as value:
            yield value


def _register_pool(catalog, pool_id, tier, *, region="eu-west-1", evidence=True):
    metadata = {}
    if evidence:
        metadata["locality_evidence"] = {
            "observed_at": NOW.isoformat(),
            "max_age_seconds": 3600,
            "source": "operator-attestation",
        }
    catalog.register_pool(
        pool_id, tier, metadata=metadata, region=region,
        members=(f"{pool_id}-storage",),
        localities=("EU",) if region.startswith("eu-") else ("US",),
    )


@pytest.fixture
def system(catalog, tmp_path, monkeypatch):
    configuration = {
        "version": 1,
        "tenants": {"default": {
            "allowed_regions": ["eu-west-1"],
            "required_localities": ["EU"],
            "tier_pools": {"hot": "hot-eu", "warm": "warm-eu"},
            "objects": [],
            "exceptions": [],
        }},
    }
    path = tmp_path / "locality.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(path))
    for tier in ("hot", "warm"):
        catalog.register_tier(tier)
        _register_pool(catalog, f"{tier}-eu", tier)
    _register_pool(catalog, "warm-us", "warm", region="us-east-1")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object(BUCKET, KEY, PAYLOAD)
    catalog.upsert(BUCKET, KEY, len(PAYLOAD), "hot", {"user_note": "retained"})
    return SimpleNamespace(
        catalog=catalog, drivers=drivers, path=path, configuration=configuration,
        context=AuditContext("locality-test", "user", "operator"),
    )


def _save(system):
    system.path.write_text(json.dumps(system.configuration), encoding="utf-8")


def _mover(system, **kwargs):
    return Mover(
        system.drivers, system.catalog, clock=lambda: NOW,
        audit_context=system.context, **kwargs,
    )


def _move(mover, **kwargs):
    return mover.move("hot", "warm", BUCKET, KEY, **kwargs)


def _assert_source_retained(system):
    assert system.drivers["hot"].get_object(BUCKET, KEY) == PAYLOAD


def _assert_no_destination(system):
    with pytest.raises(FileNotFoundError):
        system.drivers["warm"].stat_object(BUCKET, KEY)
    assert system.catalog.get(BUCKET, KEY).tier == "hot"
    _assert_source_retained(system)


def _deny_new_writes(system, monkeypatch):
    monkeypatch.setattr(
        system.drivers["warm"], "put_object_stream",
        lambda *args, **kwargs: pytest.fail("a denied locality move wrote object bytes"),
    )


def test_allowed_move_preserves_bytes_and_commits_once(system):
    mover = _mover(system)
    result = _move(mover, idempotency_key="permitted")
    assert result.verified
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD
    with pytest.raises(FileNotFoundError):
        system.drivers["hot"].stat_object(BUCKET, KEY)
    assert system.catalog.get(BUCKET, KEY).tier == "warm"
    transitions = mover.get_job_transitions("permitted")
    assert _move(mover, idempotency_key="permitted") == result
    assert mover.get_job_transitions("permitted") == transitions


def test_tenant_denial_precedes_any_storage_write_and_is_audited(system, monkeypatch):
    system.configuration["tenants"]["default"]["tier_pools"]["warm"] = "warm-us"
    _save(system)
    _deny_new_writes(system, monkeypatch)
    with pytest.raises(LocalityConstraintError):
        _move(_mover(system), idempotency_key="denied")
    _assert_no_destination(system)
    assert system.catalog.get_move_job("denied") is None
    events = [event for event in system.catalog.list_audit_events() if event.event_type == "locality.decision" and event.outcome == "rejected"]
    assert events
    assert any(event.correlation_id == "locality-test" for event in events)


def test_object_rule_cannot_be_weakened_by_tenant_allowance_or_user_metadata(system, monkeypatch):
    tenant = system.configuration["tenants"]["default"]
    tenant["allowed_regions"] = ["eu-west-1", "us-east-1"]
    tenant["required_localities"] = []
    tenant["tier_pools"]["warm"] = "warm-us"
    tenant["objects"] = [{
        "bucket": BUCKET, "key_prefix": "regulated/", "allowed_regions": ["eu-west-1"],
    }]
    _save(system)
    system.catalog.upsert(BUCKET, KEY, len(PAYLOAD), "hot", {
        "allowed_regions": ["us-east-1"],
        "locality": {"allowed_regions": ["us-east-1"], "exception_id": "forged"},
        "cognistore_locality_exception_id": "forged",
    })
    _deny_new_writes(system, monkeypatch)
    with pytest.raises(LocalityConstraintError):
        _move(_mover(system))
    _assert_no_destination(system)


@pytest.mark.parametrize("evidence", ["missing", "stale"])
def test_unsubstantiated_destination_locality_fails_closed(system, monkeypatch, evidence):
    if evidence == "missing":
        _register_pool(system.catalog, "warm-eu", "warm", evidence=False)
    else:
        pool = system.catalog.get_pool("warm-eu")
        metadata = deepcopy(pool.metadata)
        metadata["locality_evidence"]["observed_at"] = (NOW - timedelta(days=1)).isoformat()
        system.catalog.register_pool(
            pool.pool_id, pool.tier, metadata=metadata, region=pool.region,
            members=pool.members, localities=pool.localities,
        )
    _deny_new_writes(system, monkeypatch)
    with pytest.raises(LocalityConstraintError):
        _move(_mover(system))
    _assert_no_destination(system)


def test_caller_pool_id_cannot_redirect_authoritative_tier_binding(system, monkeypatch):
    system.configuration["tenants"]["default"]["tier_pools"]["warm"] = "warm-us"
    _save(system)
    _deny_new_writes(system, monkeypatch)
    mover = _mover(system)
    with pytest.raises(LocalityConstraintError):
        mover.plan("hot", "warm", BUCKET, KEY, destination_pool_id="warm-eu")
    with pytest.raises(LocalityConstraintError):
        _move(mover, destination_pool_id="warm-eu")
    _assert_no_destination(system)


@pytest.mark.parametrize("change", ["configuration", "region"])
def test_move_rechecks_locality_after_a_successful_plan(system, monkeypatch, change):
    mover = _mover(system)
    assert mover.plan("hot", "warm", BUCKET, KEY).size == len(PAYLOAD)
    if change == "configuration":
        system.configuration["tenants"]["default"]["allowed_regions"] = ["us-east-1"]
        _save(system)
    else:
        _register_pool(system.catalog, "warm-eu", "warm", region="us-east-1")
    _deny_new_writes(system, monkeypatch)
    with pytest.raises(LocalityConstraintError):
        _move(mover)
    _assert_no_destination(system)


class SimulatedCrash(BaseException):
    pass


def _crash_at(system, checkpoint, **move_options):
    def interrupt(job):
        if job.state == checkpoint:
            raise SimulatedCrash(checkpoint.value)

    mover = _mover(system, transition_hook=interrupt, owner_id="worker", lease_seconds=1)
    with pytest.raises(SimulatedCrash):
        _move(mover, idempotency_key="recover-locality", **move_options)
    assert mover.get_job("recover-locality").state == checkpoint
    return mover


@pytest.mark.parametrize("checkpoint", [
    MoveJobState.PREPARED, MoveJobState.VERIFIED, MoveJobState.COMMITTED,
])
def test_recovery_rechecks_locality_before_transfer_commit_or_source_cleanup(
    system, monkeypatch, checkpoint,
):
    _crash_at(system, checkpoint)
    _register_pool(system.catalog, "warm-eu", "warm", region="us-east-1")
    _deny_new_writes(system, monkeypatch)
    recovered = _mover(system, owner_id="worker", lease_seconds=1)
    with pytest.raises(LocalityConstraintError):
        recovered.recover_incomplete()
    _assert_source_retained(system)
    assert system.catalog.get(BUCKET, KEY).tier == (
        "warm" if checkpoint == MoveJobState.COMMITTED else "hot"
    )
    if checkpoint == MoveJobState.PREPARED:
        _assert_no_destination(system)


@pytest.mark.parametrize("checkpoint", [MoveJobState.TRANSFERRED, MoveJobState.COMMITTED])
def test_configuration_change_during_move_fences_next_irreversible_step(system, checkpoint):
    def revoke(job):
        if job.state == checkpoint:
            system.configuration["tenants"]["default"]["allowed_regions"] = ["us-east-1"]
            _save(system)

    with pytest.raises(LocalityConstraintError):
        _move(_mover(system, transition_hook=revoke))
    _assert_source_retained(system)
    assert system.catalog.get(BUCKET, KEY).tier == (
        "warm" if checkpoint == MoveJobState.COMMITTED else "hot"
    )


@pytest.mark.parametrize("change", ["rebind", "remove-config"])
def test_durable_pool_binding_cannot_be_bypassed_by_a_retry(system, monkeypatch, change):
    _crash_at(system, MoveJobState.PREPARED)
    if change == "remove-config":
        monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    else:
        _register_pool(system.catalog, "warm-eu-replacement", "warm")
        system.configuration["tenants"]["default"]["tier_pools"]["warm"] = "warm-eu-replacement"
        _save(system)
    _deny_new_writes(system, monkeypatch)
    with pytest.raises((LocalityConstraintError, MoveJobConflictError)):
        _move(_mover(system, owner_id="worker"), idempotency_key="recover-locality")
    _assert_no_destination(system)


def test_locality_controls_are_not_copied_into_destination_storage_metadata(system, monkeypatch):
    received = []
    original = system.drivers["warm"].put_object_stream

    def capture(*args, **kwargs):
        received.append(deepcopy(kwargs.get("metadata")))
        return original(*args, **kwargs)

    monkeypatch.setattr(system.drivers["warm"], "put_object_stream", capture)
    assert _move(_mover(system), idempotency_key="metadata").verified
    assert len(received) == 1
    assert not any("locality" in key for key in received[0])
    assert not any(key.startswith("cognistore_") for key in received[0])


def _exception(system):
    tenant = system.configuration["tenants"]["default"]
    tenant["tier_pools"]["warm"] = "warm-us"
    tenant["exceptions"] = [{
        "id": "approval-64", "bucket": BUCKET, "key": KEY,
        "destination_pool_id": "warm-us", "issuer": PRINCIPAL.issuer,
        "subject": PRINCIPAL.subject, "reason": "approved disaster recovery drill",
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
    }]
    _save(system)
    return RBACAuthorizer(RBACPolicy.from_dict({"bindings": [{
        "issuer": PRINCIPAL.issuer, "subject": PRINCIPAL.subject, "roles": ["admin"],
    }]}))


def test_explicit_approved_exception_moves_and_records_the_approval(system):
    authorizer = _exception(system)
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        result = _move(_mover(system), locality_exception_id="approval-64")
    assert result.verified
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD
    events = system.catalog.list_audit_events()
    exceptions = [event for event in events if event.event_type == "locality.exception"]
    assert exceptions
    for event in exceptions:
        assert event.outcome == "allowed"
        assert event.actor_id == PRINCIPAL.actor_id
        approval = event.details["locality"]["exception"]
        assert approval["id"] == "approval-64"
        assert approval["actor_id"] == PRINCIPAL.actor_id
    serialized = json.dumps([event.details for event in events])
    assert PRINCIPAL.issuer not in serialized
    assert PRINCIPAL.subject not in serialized


@pytest.mark.parametrize("request_kind", ["omitted", "anonymous", "wrong-principal"])
def test_exception_requires_explicit_selection_and_bound_authenticated_identity(
    system, monkeypatch, request_kind,
):
    authorizer = _exception(system)
    _deny_new_writes(system, monkeypatch)
    principal = (
        Principal(PRINCIPAL.issuer, "unapproved-operator")
        if request_kind == "wrong-principal" else PRINCIPAL
    )
    if request_kind == "anonymous":
        principal = None
    kwargs = {} if request_kind == "omitted" else {"locality_exception_id": "approval-64"}
    with principal_context(principal), authorization_context(authorizer):
        with pytest.raises(LocalityConstraintError):
            _move(_mover(system), **kwargs)
    _assert_no_destination(system)


def test_revoked_exception_blocks_resumed_source_cleanup(system):
    authorizer = _exception(system)

    def crash(job):
        if job.state == MoveJobState.COMMITTED:
            raise SimulatedCrash()

    with principal_context(PRINCIPAL), authorization_context(authorizer):
        with pytest.raises(SimulatedCrash):
            _move(
                _mover(system, owner_id="worker", transition_hook=crash),
                idempotency_key="exception-recovery", locality_exception_id="approval-64",
            )
        system.configuration["tenants"]["default"]["exceptions"] = []
        _save(system)
        with pytest.raises(LocalityConstraintError):
            _move(_mover(system, owner_id="worker"), idempotency_key="exception-recovery")
    _assert_source_retained(system)
    assert system.catalog.get(BUCKET, KEY).tier == "warm"


@pytest.mark.parametrize("binding", [None, "warm-us"])
def test_catalog_claim_rejects_missing_or_forbidden_binding_atomically(system, binding):
    metadata = dict(system.drivers["hot"].stat_object(BUCKET, KEY))
    if binding is not None:
        metadata["cognistore_expected_destination_pool_id"] = binding
    before = system.catalog.get(BUCKET, KEY)
    with pytest.raises(LocalityConstraintError):
        system.catalog.claim_move_job(
            "catalog-claim", src_tier="hot", dst_tier="warm", bucket=BUCKET,
            key=KEY, expected_size=len(PAYLOAD), source_metadata=metadata,
            owner_id="worker", now=NOW.isoformat(),
            lease_expires_at=(NOW + timedelta(seconds=30)).isoformat(),
        )
    assert system.catalog.get_move_job("catalog-claim") is None
    assert system.catalog.list_move_job_transitions("catalog-claim") == []
    assert system.catalog.get(BUCKET, KEY) == before
    _assert_no_destination(system)


@pytest.mark.parametrize("change", ["region", "configuration", "remove-config"])
def test_catalog_commit_rechecks_locality_inside_placement_transaction(
    system, monkeypatch, change,
):
    _crash_at(system, MoveJobState.VERIFIED)
    before = system.catalog.get(BUCKET, KEY)
    job = system.catalog.get_move_job("recover-locality")
    transitions = system.catalog.list_move_job_transitions(job.idempotency_key)
    if change == "region":
        _register_pool(system.catalog, "warm-eu", "warm", region="us-east-1")
    elif change == "configuration":
        system.configuration["tenants"]["default"]["allowed_regions"] = []
        _save(system)
    else:
        monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    with pytest.raises(LocalityConstraintError):
        system.catalog.commit_move_job_placement(
            job.idempotency_key, owner_id="worker", size=len(PAYLOAD),
            tier="warm", checksum=job.destination_checksum, now=NOW.isoformat(),
            lease_expires_at=(NOW + timedelta(seconds=30)).isoformat(),
        )
    assert system.catalog.get_move_job(job.idempotency_key) == job
    assert system.catalog.list_move_job_transitions(job.idempotency_key) == transitions
    assert system.catalog.get(BUCKET, KEY) == before
    _assert_source_retained(system)


@pytest.mark.parametrize("changed_contract", ["pool", "exception"])
def test_claim_race_cannot_replace_durable_locality_contract(system, monkeypatch, changed_contract):
    _crash_at(system, MoveJobState.PREPARED)
    original_get = system.catalog.get_move_job
    durable = original_get("recover-locality")
    reads = 0

    def stale_preflight(move_id):
        nonlocal reads
        reads += 1
        return None if reads == 1 else original_get(move_id)

    kwargs = {}
    tenant = system.configuration["tenants"]["default"]
    if changed_contract == "pool":
        _register_pool(system.catalog, "warm-eu-replacement", "warm")
        tenant["tier_pools"]["warm"] = "warm-eu-replacement"
    else:
        tenant["exceptions"] = [{
            "id": "new-approval", "bucket": BUCKET, "key": KEY,
            "destination_pool_id": "warm-eu", "issuer": PRINCIPAL.issuer,
            "subject": PRINCIPAL.subject, "reason": "separately approved request",
            "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        }]
        kwargs["locality_exception_id"] = "new-approval"
    _save(system)
    authorizer = RBACAuthorizer(RBACPolicy.from_dict({"bindings": [{
        "issuer": PRINCIPAL.issuer, "subject": PRINCIPAL.subject, "roles": ["admin"],
    }]}))
    _deny_new_writes(system, monkeypatch)
    monkeypatch.setattr(system.catalog, "get_move_job", stale_preflight)
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        with pytest.raises(MoveJobConflictError):
            _move(_mover(system, owner_id="worker"), idempotency_key="recover-locality", **kwargs)
    assert original_get("recover-locality") == durable
    _assert_no_destination(system)


@pytest.mark.parametrize("marker", ["missing", "forged"])
def test_catalog_claim_persists_authoritative_locality_for_recovery(system, monkeypatch, marker):
    metadata = dict(system.drivers["hot"].stat_object(BUCKET, KEY))
    metadata["cognistore_expected_destination_pool_id"] = "warm-eu"
    if marker == "forged":
        metadata["cognistore_locality"] = {
            "configured": False, "tier_pools": {"warm": "caller-controlled"},
        }
    job = system.catalog.claim_move_job(
        "direct-catalog", src_tier="hot", dst_tier="warm", bucket=BUCKET,
        key=KEY, expected_size=len(PAYLOAD), source_metadata=metadata,
        owner_id="worker", now=NOW.isoformat(),
        lease_expires_at=(NOW + timedelta(seconds=30)).isoformat(),
    )
    recorded = job.source_metadata["cognistore_locality"]
    assert recorded["configured"] is True
    assert recorded["tier_pools"]["warm"] == "warm-eu"
    assert recorded["rules"][0]["allowed_regions"] == ["eu-west-1"]
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    _deny_new_writes(system, monkeypatch)
    with pytest.raises(LocalityConstraintError):
        _move(_mover(system, owner_id="worker"), idempotency_key="direct-catalog")
    _assert_no_destination(system)


def test_legacy_catalog_job_records_locality_when_reclaimed_under_policy(system, monkeypatch):
    metadata = dict(system.drivers["hot"].stat_object(BUCKET, KEY))
    metadata["cognistore_expected_destination_pool_id"] = "warm-eu"
    arguments = {
        "src_tier": "hot", "dst_tier": "warm", "bucket": BUCKET, "key": KEY,
        "expected_size": len(PAYLOAD), "source_metadata": metadata,
        "owner_id": "worker", "now": NOW.isoformat(),
        "lease_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    }
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    original = system.catalog.claim_move_job("legacy-catalog", **arguments)
    assert "cognistore_locality" not in original.source_metadata
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(system.path))
    reclaimed = system.catalog.claim_move_job("legacy-catalog", **arguments)
    assert reclaimed.source_metadata["cognistore_locality"]["configured"] is True
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    _deny_new_writes(system, monkeypatch)
    with pytest.raises(LocalityConstraintError):
        _move(_mover(system, owner_id="worker"), idempotency_key="legacy-catalog")
    _assert_no_destination(system)


def test_legacy_verified_job_records_locality_when_directly_committed_under_policy(
    system, monkeypatch,
):
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    _crash_at(system, MoveJobState.VERIFIED, destination_pool_id="warm-eu")
    verified = system.catalog.get_move_job("recover-locality")
    assert "cognistore_locality" not in verified.source_metadata
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(system.path))
    committed = system.catalog.commit_move_job_placement(
        verified.idempotency_key, owner_id="worker", size=len(PAYLOAD), tier="warm",
        checksum=verified.destination_checksum, now=NOW.isoformat(),
        lease_expires_at=(NOW + timedelta(seconds=30)).isoformat(),
    )
    assert committed.state == MoveJobState.COMMITTED
    evidence = committed.source_metadata["cognistore_locality"]
    assert evidence["configured"] is True
    assert evidence["tier_pools"]["warm"] == "warm-eu"
    assert system.catalog.get(BUCKET, KEY).tier == "warm"
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG")
    with pytest.raises(LocalityConstraintError):
        _move(_mover(system, owner_id="worker"), idempotency_key=verified.idempotency_key)
    _assert_source_retained(system)
    assert system.catalog.get_move_job(verified.idempotency_key).state == MoveJobState.COMMITTED
