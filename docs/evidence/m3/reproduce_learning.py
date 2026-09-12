from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from copy import deepcopy
from pathlib import Path

from cognistore.core.placement_llm import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    CallablePlacementProvider,
    FakePlacementProvider,
    PlacementInference,
    PlacementProviderUnavailable,
    TransientPlacementError,
)
from cognistore.core.policy_baseline import code_version, evaluate_baseline, train_baseline

root = Path.cwd()
out = root / "docs/evidence/m3"
out.mkdir(parents=True, exist_ok=True)
revision = "fe700326f3cc99ba498536afd07b897575709d53"
assert not subprocess.check_output(["git", "diff", revision, "--", "cognistore"], text=True)
# Bind unchanged Python source to the qualified implementation, even when HEAD
# also contains this documentation closeout.
implementation = "git:" + revision + ";" + code_version().split(";", 1)[1]
assert implementation.startswith("git:" + revision + ";")
dataset_path = root / "tests/fixtures/policy_baseline/dataset-v1.json"
config_path = root / "configs/policy-baseline-v1.json"
dataset = json.loads(dataset_path.read_text())
config = json.loads(config_path.read_text())
model = train_baseline(dataset, config, code_version=implementation)
evaluation = evaluate_baseline(dataset, model, code_version=implementation)
assert model == train_baseline(dataset, config, code_version=implementation)
assert evaluation == evaluate_baseline(dataset, model, code_version=implementation)


def write(name, value):
    data = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    (out / name).write_text(data)
    return {
        "path": str((out / name).relative_to(root)),
        "bytes": len(data.encode()),
        "sha256": hashlib.sha256(data.encode()).hexdigest(),
    }


files = [write("baseline-model.json", model), write("baseline-evaluation.json", evaluation)]
inputs = {"current_tier": "hot", "size": 123, "allowed_tiers": ["hot", "warm", "cold"]}
move = '{"action":"move","dst_tier":"cold","reason":"Synthetic eligible move"}'
stay = '{"action":"stay","dst_tier":null,"reason":"Synthetic stay"}'
cases = []


def record(name, setup, result, expected_action, expected_fallback, expected_attempts, **checks):
    assert result.action == expected_action
    assert result.audit["fallback_reason"] == expected_fallback
    assert result.audit["attempts"] == expected_attempts
    assert result.dst_tier == ("cold" if expected_action == "move" else None)
    assert all(checks.values())
    cases.append(
        {
            "name": name,
            "provider_setup": setup,
            "expected": {
                "action": expected_action,
                "fallback_reason": expected_fallback,
                "attempts": expected_attempts,
            },
            "observed": {
                "action": result.action,
                "dst_tier": result.dst_tier,
                "reason": result.reason,
                "attempts": result.audit["attempts"],
                "fallback_reason": result.audit["fallback_reason"],
                "validation_errors": result.audit["validation_errors"],
                "provider_errors": result.audit["provider_errors"],
                "redacted_response": result.audit["response"],
            },
            "additional_checks": checks,
            "passed": True,
        }
    )


for name, response, action, fallback in (
    ("eligible_move", move, "move", None),
    ("explicit_stay", stay, "stay", None),
    ("malformed_json", "{", "stay", "llm_invalid_response"),
    (
        "duplicate_json_key",
        '{"action":"stay","action":"move","dst_tier":"cold","reason":"duplicate"}',
        "stay",
        "llm_invalid_response",
    ),
    (
        "ineligible_destination",
        '{"action":"move","dst_tier":"archive","reason":"unknown tier"}',
        "stay",
        "llm_invalid_response",
    ),
):
    service = PlacementInference(FakePlacementProvider(response))
    result = service.evaluate(**inputs)
    record(
        name,
        {"kind": "FakePlacementProvider", "response": response},
        result,
        action,
        fallback,
        1,
        repeated_evaluation_identical=result == service.evaluate(**inputs),
    )

record(
    "no_provider",
    {"kind": "none"},
    PlacementInference().evaluate(**inputs),
    "stay",
    "llm_provider_unavailable",
    0,
)

for name, exception, fallback in (
    ("unavailable_provider", PlacementProviderUnavailable, "llm_provider_unavailable"),
    ("provider_timeout", TimeoutError, "llm_provider_timeout"),
    ("provider_error", RuntimeError, "llm_provider_error"),
):

    def fail(*args, exception=exception):
        raise exception("synthetic provider diagnostic")

    provider = CallablePlacementProvider(fail, provider_id="offline-fixture", model="deterministic")
    result = PlacementInference(provider).evaluate(**inputs)
    record(
        name,
        {"kind": "CallablePlacementProvider", "behavior": "raise " + exception.__name__},
        result,
        "stay",
        fallback,
        1,
        raw_diagnostic_excluded="synthetic provider diagnostic" not in json.dumps(result.audit),
    )

attempts = []


def transient_once(*args):
    attempts.append(None)
    if len(attempts) == 1:
        raise TransientPlacementError("synthetic retryable failure")
    return move


provider = CallablePlacementProvider(
    transient_once, provider_id="offline-fixture", model="deterministic"
)
record(
    "transient_then_success",
    {
        "kind": "CallablePlacementProvider",
        "behavior": "raise TransientPlacementError once, then return eligible_move response",
    },
    PlacementInference(provider, max_attempts=2).evaluate(**inputs),
    "move",
    None,
    2,
)

release = threading.Event()
finished = threading.Event()


def late(*args):
    try:
        release.wait(2)
        return move
    finally:
        finished.set()


provider = CallablePlacementProvider(late, provider_id="offline-fixture", model="deterministic")
try:
    result = PlacementInference(provider, timeout_seconds=0.05).evaluate(**inputs)
    before = deepcopy(result)
finally:
    release.set()
    assert finished.wait(1)
record(
    "late_eligible_move",
    {
        "kind": "CallablePlacementProvider",
        "behavior": "wait for release until after inference returns, then return eligible_move response",
        "timeout_seconds": 0.05,
    },
    result,
    "stay",
    "llm_provider_timeout",
    1,
    late_reply_does_not_change_result=result == before,
)

report = {
    "schema_version": 1,
    "kind": "offline_llm_adapter_boundary_evaluation",
    "implementation_code_version": implementation,
    "inputs": inputs,
    "prompt_version": PROMPT_VERSION,
    "response_schema_version": SCHEMA_VERSION,
    "default_timeout_seconds": 10.0,
    "default_max_attempts": 2,
    "cases": cases,
    "summary": {
        "cases": len(cases),
        "passed": sum(case["passed"] for case in cases),
        "failed": 0,
        "external_provider_calls": 0,
        "storage_mutations": 0,
        "catalog_mutations": 0,
    },
    "limits": [
        "Synthetic deterministic adapters evaluate boundary correctness, not model quality.",
        "Eligible move results are proposals; no PolicyRunner or storage action is executed.",
        "Runner guardrails, HTTP privacy, and API/job integration are covered by the separately reported focused test suite.",
    ],
}
files.append(write("llm-offline-evaluation.json", report))
print(
    json.dumps(
        {
            "implementation": implementation,
            "files": files,
            "baseline_promotion": evaluation["promotion"],
            "llm_summary": report["summary"],
        },
        indent=2,
    )
)
