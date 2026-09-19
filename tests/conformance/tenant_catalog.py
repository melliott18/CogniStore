"""Isolation guarantees shared by memory, SQLite, and PostgreSQL catalogs."""
from __future__ import annotations

from io import BytesIO
from uuid import UUID

import pytest

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.core.access import AccessConfig, AccessEvent
from cognistore.core.audit import AuditContext, AuditEvent
from cognistore.core.content_identity import ContentIdentityBuilder
from tests.budget_fixtures import END, NOW, claim, definition


class TenantCatalogConformance:
    def test_every_catalog_domain_is_scoped_even_when_identifiers_collide(self, tenant_catalogs):
        root, alice, bob = tenant_catalogs
        shared_content = ContentIdentityBuilder(chunk_size=4).build(BytesIO(b"shared!!"), expected_size=8)
        audit_id = str(UUID(int=58))
        for tenant in (alice, bob):
            owner = tenant.tenant_id
            for tier in ("hot", "warm"):
                tenant.register_tier(tier, {"owner": owner})
                tenant.register_pool(f"{tier}-pool", tier, {"owner": owner}, members=(owner,), region="us-test")
            tenant.upsert("bucket", "private", size=1, tier="hot", metadata={"owner": owner})
            fence = tenant.capture_scan_fence("bucket", "shared")
            assert tenant.upsert_scan_observation(
                "bucket", "shared", size=shared_content.size, tier="hot",
                generation=owner, metadata={"owner": owner}, fence=fence, content=shared_content,
            )
            tenant.assign_pool("bucket", "shared", "hot-pool")
            actor = AuditContext("request", "user", owner)
            tenant.configure_budget(definition("2"), audit_context=actor, occurred_at=NOW)
            claim(tenant, "shared", move_id="same-move", owner=owner)
            tenant.append_audit_event(AuditEvent.create(
                "policy.decision", "succeeded", actor, event_id=audit_id,
                occurred_at=NOW, recorded_at=NOW, details={"owner": owner},
            ))
            tenant.append_access_event(AccessEvent.create(
                kind="read", bucket="bucket", key="shared", operation_id="same-access",
                occurred_at=NOW, source=owner,
            ))
        # Distinct tenants may reuse every external ID, digest, and unique key.
        assert root.list("bucket") == []
        for tenant in (alice, bob):
            owner = tenant.tenant_id
            assert tenant.get("bucket", "private").metadata == {"owner": owner}
            assert [item.key for item in tenant.list_page("bucket", limit=10)] == ["private", "shared"]
            assert len(list(tenant.iter_objects(batch_size=1))) == 2
            assert tenant.get_tier("hot").metadata == {"owner": owner}
            assert tenant.get_pool("hot-pool").metadata == {"owner": owner}
            assert tenant.get_move_job("same-move").owner_id == owner
            assert len(tenant.list_move_job_transitions("same-move")) == 1
            assert len(tenant.list_budget_reservations()) == 1
            assert tenant.get_audit_event(audit_id).details == {"owner": owner}
            assert tenant.get_object_content("bucket", "shared") == shared_content
            snapshot = tenant.aggregate_access_events(
                "bucket", "shared", config=AccessConfig(), as_of=NOW,
            )
            assert snapshot.observed_events == 1
            report = tenant.reconcile_content_references(grace_period_seconds=0, now=END)
            assert report.consistent
            assert all(item.stored_reference_count == 1 for item in report.entries)
        # A destructive mutation/reclamation on Alice does not change Bob.
        alice.delete("bucket", "shared")
        alice.prune_audit_events(END)
        assert alice.get_object_content("bucket", "shared") is None
        assert alice.get_audit_event(audit_id) is None
        assert bob.get_object_content("bucket", "shared") == shared_content
        assert bob.get_audit_event(audit_id).details == {"owner": "bob"}
        assert bob.get("bucket", "shared").pool_id == "hot-pool"

    def test_active_tenant_cannot_reuse_another_bound_catalog(self, tenant_catalogs):
        root, alice, bob = tenant_catalogs
        event = AccessEvent.create(kind="read", bucket="bucket", key="key")
        operations = (
            lambda: alice.get("bucket", "key"),
            lambda: alice.upsert("bucket", "key", size=1, tier="hot"),
            lambda: alice.delete("bucket", "key"),
            lambda: alice.list_page("bucket"),
            lambda: list(alice.iter_objects()),
            lambda: alice.get_object_content("bucket", "key"),
            lambda: alice.reconcile_content_references(grace_period_seconds=0),
            lambda: alice.list_tiers(),
            lambda: alice.register_tier("hot"),
            lambda: alice.list_pools(),
            lambda: alice.list_budgets(),
            lambda: alice.list_budget_reservations(),
            lambda: alice.get_move_job("same-move"),
            lambda: alice.list_move_jobs_page("bucket"),
            lambda: alice.list_move_job_transitions("same-move"),
            lambda: alice.list_audit_events(),
            lambda: alice.append_access_event(event),
            lambda: root.for_tenant("alice"),
        )
        with tenant_context("bob"):
            for operation in operations:
                with pytest.raises(TenantIsolationError, match="^Operation not permitted$"):
                    operation()
            assert root.for_tenant("bob") is bob
            assert bob.list("bucket") == []
        assert root.for_tenant("default") is root
        assert alice.for_tenant("bob") is bob
        assert bob.for_tenant("default") is root
        with pytest.raises(AttributeError):
            alice.tenant_id = "bob"

    def test_foreign_ids_are_indistinguishable_from_missing(self, tenant_catalogs):
        _, alice, bob = tenant_catalogs
        alice.upsert("secret-bucket", "secret-key", size=12, tier="secret-tier")
        alice.register_pool("secret-pool", "secret-tier", members=("disk",), region="us-test")
        claim(alice, "key", move_id="secret-move")
        audit = AuditEvent.create("policy.decision", "succeeded", AuditContext("request", "user", "alice"))
        alice.append_audit_event(audit)
        with tenant_context("bob"):
            assert bob.get("secret-bucket", "secret-key") is None
            assert bob.get("missing-bucket", "missing-key") is None
            assert bob.get_tier("secret-tier") is None
            assert bob.get_pool("secret-pool") is None
            assert bob.get_move_job("secret-move") is None
            assert bob.get_audit_event(audit.event_id) is None
            assert bob.get_object_content("secret-bucket", "secret-key") is None
            assert bob.list_move_job_transitions("secret-move") == []
            assert bob.list("secret-bucket") == []

    def test_lazy_object_iterator_checks_each_resumption(self, tenant_catalogs):
        _, alice, _ = tenant_catalogs
        for key in ("a", "b", "c"):
            alice.upsert("bucket", key, size=1, tier="hot")
        for batch_size in (1, 3):
            with tenant_context("alice"):
                records = alice.iter_objects(batch_size=batch_size)
                assert next(records).key == "a"
            with tenant_context("bob"):
                with pytest.raises(TenantIsolationError):
                    next(records)
