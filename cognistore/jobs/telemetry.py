"""Finite job operation vocabulary shared by producers and workers."""


def job_operation(job_type: str) -> str:
    # User-defined handler names must never become metric labels or span names.
    return {
        "catalog.scan": "scan",
        "policy.run": "run_policy",
        "object.move": "move",
    }.get(job_type, "other")
