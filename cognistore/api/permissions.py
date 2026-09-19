"""Explicit permissions shared by HTTP and application service boundaries."""

from types import MappingProxyType

from starlette.routing import compile_path

from cognistore.auth.authorization import Permission

OPERATION_PERMISSIONS = MappingProxyType({
    "put_object": (Permission.WRITE,),
    "stat_object": (Permission.READ,),
    "open_object": (Permission.READ,),
    "delete_object": (Permission.WRITE,),
    "get_catalog_object": (Permission.READ,),
    "list_catalog_objects": (Permission.READ,),
    "ask": (Permission.READ,),
    "set_importance": (Permission.WRITE, Permission.POLICY),
    "evaluate_policy": (Permission.POLICY,),
    "preview_policy": (Permission.POLICY,),
    "get_policy_decision": (Permission.AUDIT,),
    "list_policy_decisions": (Permission.AUDIT,),
    "submit_catalog_scan": (Permission.ADMIN,),
    "submit_policy_run": (Permission.POLICY, Permission.MOVEMENT),
    "get_job": (Permission.READ,),
    "list_legal_holds": (Permission.LEGAL_HOLD_INSPECT,),
    "place_legal_hold": (Permission.LEGAL_HOLD_MANAGE,),
    "release_legal_hold": (Permission.LEGAL_HOLD_RELEASE,),
})

# Hold lifecycle changes always require a verified identity and explicit grant,
# including deployments that otherwise allow anonymous local HTTP operations.
AUTHENTICATED_OPERATIONS = frozenset({"place_legal_hold", "release_legal_hold"})

ENDPOINT_OPERATIONS = MappingProxyType({
    ("PUT", "/v1/objects/{tier}/{bucket}/{key:path}"): "put_object",
    ("HEAD", "/v1/objects/{tier}/{bucket}/{key:path}"): "stat_object",
    ("GET", "/v1/objects/{tier}/{bucket}/{key:path}"): "open_object",
    ("DELETE", "/v1/objects/{tier}/{bucket}/{key:path}"): "delete_object",
    ("GET", "/v1/catalog/objects"): "list_catalog_objects",
    ("GET", "/v1/catalog/objects/{bucket}/{key:path}"): "get_catalog_object",
    ("POST", "/v1/ask"): "ask",
    ("POST", "/v1/catalog/importance"): "set_importance",
    ("POST", "/v1/policies/evaluate"): "evaluate_policy",
    ("POST", "/v1/policies/preview"): "preview_policy",
    ("GET", "/v1/policy-decisions"): "list_policy_decisions",
    ("GET", "/v1/policy-decisions/{decision_id}"): "get_policy_decision",
    ("POST", "/v1/actions/catalog-scans"): "submit_catalog_scan",
    ("POST", "/v1/actions/policy-runs"): "submit_policy_run",
    ("GET", "/v1/jobs/{job_id}"): "get_job",
    ("GET", "/v1/legal-holds"): "list_legal_holds",
    ("POST", "/v1/legal-holds"): "place_legal_hold",
    ("POST", "/v1/legal-holds/{hold_id}/release"): "release_legal_hold",
})

_ENDPOINT_PATTERNS = tuple(
    (method, compile_path(path)[0], operation)
    for (method, path), operation in ENDPOINT_OPERATIONS.items()
)


def endpoint_operation(method: str, path: str) -> str:
    """Resolve only known operations, including framework slash redirects."""
    for expected_method, pattern, operation in _ENDPOINT_PATTERNS:
        if method == expected_method and pattern.fullmatch(path.rstrip("/")):
            return operation
    return "unknown"
