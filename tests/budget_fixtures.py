"""Deterministic one-dollar-per-move budget fixtures shared by catalog tests."""

from dataclasses import replace

from cognistore.core.audit import AuditContext
from cognistore.core.budgets import BudgetDefinition
from cognistore.core.estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
)

NOW = "2026-09-11T00:00:00.000000Z"
END = "2026-10-11T00:00:00.000000Z"
USER = AuditContext("budget-test", "user", "operator")


def definition(limit="1", *, budget_id="monthly", prefix=""):
    rates = {
        name: RateAssumption(
            value="1" if name == "write_request_price" else "0", unit=unit,
            source="test", source_version="1",
            effective_from="2026-01-01T00:00:00Z", effective_until="2027-01-01T00:00:00Z",
        )
        for name, unit in RATE_UNITS.items()
    }
    return BudgetDefinition(
        budget_id, NOW, END, cost_limit_usd=limit, bucket="bucket", prefix=prefix,
        tier_pools={"hot": "hot-pool", "warm": "warm-pool"},
        profiles={
            "hot-pool": EstimationProfile("1", "posix", {
                **rates, "write_request_price": replace(rates["write_request_price"], value="0"),
            }),
            "warm-pool": EstimationProfile("1", "posix", rates),
        },
        workload=EstimationWorkload(read_requests=0, write_requests=0,
                                    transfer_bytes=0, retrieval_bytes=0),
    )



def claim(catalog, key="a", *, move_id=None, owner="worker", metadata=None, context=None, now=NOW):
    return catalog.claim_move_job(
        move_id or f"move-{key}", src_tier="hot", dst_tier="warm", bucket="bucket", key=key,
        expected_size=4, source_metadata={"generation": "source", **(metadata or {})},
        owner_id=owner, now=now, lease_expires_at="2026-09-11T00:01:00.000000Z",
        audit_context=context,
    )
