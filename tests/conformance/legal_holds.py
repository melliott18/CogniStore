"""Legal hold contract shared by memory, SQLite, and PostgreSQL."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from io import BytesIO

import pytest

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.core.audit import AuditContext, AuditQuery
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.legal_holds import LegalHoldError


def actor(name="hold-manager"):
    return AuditContext("case-62", "user", name)


class LegalHoldConformance:
    def test_shared_mutations_and_reads_progress_while_hold_waits(self, catalog):
        if os.name == "nt" and getattr(catalog, "backend", None) == "sqlite":
            pytest.skip("Windows SQLite uses conservative exclusive CRT file locks")
        first_entered, second_entered = threading.Event(), threading.Event()
        first_done, second_done = threading.Event(), threading.Event()
        hold_started = threading.Event()

        def mutate(key, entered, finish):
            with catalog.destructive_operation("bucket", key, operation="overwrite"):
                entered.set()
                assert finish.wait(10)
                catalog.upsert("bucket", key, size=4, tier="hot")

        def place():
            hold_started.set()
            return catalog.place_legal_hold("bucket", reason="preserve", context=actor())

        with ThreadPoolExecutor(max_workers=3) as workers:
            first = workers.submit(mutate, "first", first_entered, first_done)
            second = workers.submit(mutate, "second", second_entered, second_done)
            try:
                assert first_entered.wait(5)
                assert second_entered.wait(5)
                # Ordinary catalog reads/heartbeats must stay available while
                # storage streams are inside their destructive spans.
                assert workers.submit(catalog.get, "bucket", "first").result(timeout=5) is None
                hold = workers.submit(place)
                assert hold_started.wait(5)
                first_done.set()
                first.result(timeout=5)
                assert not hold.done()
                second_done.set()
                second.result(timeout=5)
                assert hold.result(timeout=5).active
            finally:
                first_done.set()
                second_done.set()

    def test_same_thread_hold_upgrade_fails_without_deadlock(self, catalog):
        with catalog.destructive_operation("bucket", "key", operation="overwrite"):
            with pytest.raises(RuntimeError, match="during a destructive operation"):
                catalog.place_legal_hold("bucket", reason="preserve", context=actor())
            catalog.upsert("bucket", "key", size=4, tier="hot")
        assert catalog.list_legal_holds() == []

    def test_lifecycle_is_immutable_and_retained_after_release(self, catalog):
        hold = catalog.place_legal_hold("bucket", key="evidence", reason="case 62", context=actor())
        assert hold.tenant_id == catalog.tenant_id
        assert hold.active
        with pytest.raises(FrozenInstanceError):
            hold.reason = "replace evidence"
        snapshot = hold.to_dict()
        snapshot["reason"] = "replace evidence"
        assert catalog.list_legal_holds()[0].reason == "case 62"
        released = catalog.release_legal_hold(
            hold.hold_id, reason="case closed", context=actor("hold-releaser"),
        )
        assert not released.active and hold.active
        assert released.reason == "case 62"
        assert released.actor_id == "hold-manager"
        assert released.released_actor_id == "hold-releaser"
        assert released.released_reason == "case closed"
        assert catalog.list_legal_holds(active_only=True) == []
        assert catalog.list_legal_holds() == [released]
        with pytest.raises(LegalHoldError, match="already released"):
            catalog.release_legal_hold(hold.hold_id, reason="rewrite", context=actor())
        assert catalog.prune_expired_audit_events("2999-01-01T00:00:00Z") == 0
        assert catalog.prune_audit_events("2999-01-01T00:00:00Z") == 0
        events = catalog.list_audit_events(AuditQuery(limit=100))
        assert [event.event_type for event in events] == ["legal_hold.placed", "legal_hold.released"]
        assert all(event.expires_at is None for event in events)

    def test_scopes_overlap_and_include_future_keys_with_literal_matching(self, catalog):
        exact = catalog.place_legal_hold("bucket", key="a%_/file", reason="exact", context=actor())
        prefix = catalog.place_legal_hold("bucket", prefix="a%_/", reason="prefix", context=actor())
        bucket = catalog.place_legal_hold("other", reason="bucket", context=actor())
        assert catalog.list_legal_holds(bucket="bucket", key="a%_/file", active_only=True) == [exact, prefix]
        assert catalog.list_legal_holds(bucket="bucket", key="aaa/file", active_only=True) == []
        assert catalog.list_legal_holds(bucket="other", key="future", active_only=True) == [bucket]
        catalog.release_legal_hold(exact.hold_id, reason="partial release", context=actor())
        with pytest.raises(LegalHoldError):
            catalog.upsert("bucket", "a%_/file", size=1, tier="hot")
        catalog.upsert("bucket", "aaa/file", size=1, tier="hot")
        with pytest.raises(LegalHoldError):
            catalog.delete("other", "future")

    def test_hold_is_not_metadata_and_catalog_mutations_cannot_bypass_it(self, catalog):
        catalog.register_tier("hot")
        catalog.register_pool("hot-pool", "hot", region="test", members=("node",))
        catalog.upsert("bucket", "key", size=4, tier="hot", metadata={"legal_hold": False})
        before = catalog.get("bucket", "key")
        hold = catalog.place_legal_hold("bucket", key="key", reason="preserve", context=actor())
        mutations = [
            lambda: catalog.delete("bucket", "key"),
            lambda: catalog.upsert("bucket", "key", size=9, tier="hot", metadata={}),
            lambda: catalog.upsert_placement("bucket", "key", size=9, tier="hot"),
            lambda: catalog.update_placement("bucket", "key", "cold"),
            lambda: catalog.assign_pool("bucket", "key", "hot-pool"),
        ]
        for mutation in mutations:
            with pytest.raises(LegalHoldError, match="active legal hold"):
                mutation()
            assert catalog.get("bucket", "key") == before
        assert catalog.list_legal_holds(active_only=True) == [hold]
        denials = catalog.list_audit_events(AuditQuery(event_types=frozenset({"legal_hold.denied"})))
        assert len(denials) == len(mutations)
        assert all(event.details["hold_ids"] == [hold.hold_id] for event in denials)

    def test_scan_cannot_replace_held_content_or_reclaim_orphan_cas(self, catalog):
        builder = ContentIdentityBuilder(chunk_size=2)
        content = builder.build(BytesIO(b"evidence"), expected_size=8)
        assert catalog.upsert_scan_observation(
            "bucket", "key", size=content.size, tier="hot", generation="v1", metadata={},
            fence=catalog.capture_scan_fence("bucket", "key"), content=content,
        )
        catalog.upsert("orphan", "old", size=1, tier="hot")
        # One held tenant scope conservatively protects every CAS candidate,
        # including historical/orphan evidence with no remaining logical edge.
        hold = catalog.place_legal_hold("bucket", key="key", reason="preserve", context=actor())
        assert not catalog.upsert_scan_observation(
            "bucket", "key", size=0, tier="cold", generation="v2", metadata={},
            fence=catalog.capture_scan_fence("bucket", "key"), content=None,
        )
        assert catalog.get_object_content("bucket", "key") == content
        report = catalog.reconcile_content_references(grace_period_seconds=0)
        assert report.entries and all(entry.legal_hold for entry in report.entries)
        assert not any(entry.reclamation_eligible for entry in report.entries)
        catalog.release_legal_hold(hold.hold_id, reason="done", context=actor())
        catalog.delete("bucket", "key")
        report = catalog.reconcile_content_references(grace_period_seconds=0)
        assert not any(entry.legal_hold for entry in report.entries)

    def test_holds_and_release_ids_cannot_cross_tenants(self, catalog):
        alice, bob = catalog.for_tenant("alice"), catalog.for_tenant("bob")
        hold = alice.place_legal_hold("same", key="same", reason="private case", context=actor())
        assert bob.list_legal_holds() == []
        bob.upsert("same", "same", size=1, tier="hot")
        with pytest.raises(KeyError, match="Legal hold not found"):
            bob.release_legal_hold(hold.hold_id, reason="no", context=actor())
        assert alice.list_legal_holds(active_only=True) == [hold]
        with tenant_context("bob"):
            with pytest.raises(TenantIsolationError):
                alice.list_legal_holds()

    @pytest.mark.parametrize("fields", [
        {"reason": ""}, {"key": "key", "prefix": "prefix"}, {"prefix": 12},
        {"context": AuditContext("run", "worker", "worker")},
    ])
    def test_invalid_lifecycle_is_rejected_without_audit(self, catalog, fields):
        values = {"reason": "preserve", "context": actor(), **fields}
        with pytest.raises(ValueError):
            catalog.place_legal_hold("bucket", **values)
        assert catalog.list_legal_holds() == []
        assert catalog.list_audit_events() == []
