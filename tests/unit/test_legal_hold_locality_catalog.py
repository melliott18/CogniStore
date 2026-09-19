"""Both independent preservation constraints survive catalog commit/retry."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from cognistore.core.audit import AuditContext, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.legal_holds import LegalHoldError
from cognistore.core.locality import LocalityConstraintError
from cognistore.core.move_jobs import MoveJobState
from cognistore.db.catalog import SQLCatalog


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request, tmp_path):
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLCatalog(tmp_path / "catalog.db") as value:
            yield value


def test_releasing_hold_does_not_bypass_locality_commit_revalidation(
    catalog, tmp_path, monkeypatch,
):
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    expiry = (now + timedelta(seconds=30)).isoformat()
    context = AuditContext("combined-controls", "user", "operator")
    policy = {"version": 1, "tenants": {"default": {
        "allowed_regions": ["eu-west-1"], "required_localities": ["EU"],
        "tier_pools": {"hot": "hot-eu", "warm": "warm-eu"},
        "objects": [], "exceptions": [],
    }}}
    path = tmp_path / "locality.json"
    path.write_text(json.dumps(policy))
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(path))
    for tier in ("hot", "warm"):
        catalog.register_tier(tier)
        catalog.register_pool(
            f"{tier}-eu", tier, region="eu-west-1", members=("storage",),
            localities=("EU",), metadata={"locality_evidence": {
                "observed_at": now.isoformat(), "max_age_seconds": 3600,
                "source": "operator-attestation",
            }},
        )
    catalog.upsert("bucket", "key", size=4, tier="hot")
    job = catalog.claim_move_job(
        "combined", src_tier="hot", dst_tier="warm", bucket="bucket", key="key",
        expected_size=4, source_metadata={"cognistore_expected_destination_pool_id": "warm-eu"},
        owner_id="worker", now=now.isoformat(), lease_expires_at=expiry,
        audit_context=context,
    )
    for next_state in (MoveJobState.TRANSFERRED, MoveJobState.VERIFIED):
        job = catalog.transition_move_job(
            job.idempotency_key, owner_id="worker", expected_state=job.state,
            to_state=next_state, reason="verified fixture", now=now.isoformat(),
            lease_expires_at=expiry, audit_context=context,
        )
    original = catalog.get("bucket", "key")
    hold = catalog.place_legal_hold("bucket", key="key", reason="case 62", context=context)
    policy["tenants"]["default"]["allowed_regions"] = []
    path.write_text(json.dumps(policy))

    def commit():
        return catalog.commit_move_job_placement(
            job.idempotency_key, owner_id="worker", size=4, tier="warm", checksum="a" * 64,
            now=now.isoformat(), lease_expires_at=expiry, audit_context=context,
        )

    with pytest.raises(LegalHoldError):
        commit()
    assert catalog.get_move_job(job.idempotency_key) == job
    assert catalog.get("bucket", "key") == original
    catalog.release_legal_hold(hold.hold_id, reason="case closed", context=context)
    with pytest.raises(LocalityConstraintError):
        commit()
    assert catalog.get_move_job(job.idempotency_key) == job
    assert catalog.get("bucket", "key") == original

    policy["tenants"]["default"]["allowed_regions"] = ["eu-west-1"]
    path.write_text(json.dumps(policy))
    with catalog.destructive_operation("bucket", "key", operation="move", context=context):
        committed = commit()
    assert committed.state == MoveJobState.COMMITTED
    assert committed.source_metadata["cognistore_locality"]["configured"]
    assert catalog.get("bucket", "key").pool_id == "warm-eu"
    assert len(catalog.list_audit_events(AuditQuery(
        event_types=frozenset({"legal_hold.denied"}),
    ))) == 1
