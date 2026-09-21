#!/usr/bin/env python3
"""Check the #164 evidence contract offline; never inject faults or approve resume.

A complete report means supplied observations and hashed files are internally
consistent. Operators must review the raw evidence; this is not certification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

COUNTS = {
    "worker-transfer-kill": 3, "worker-publication-kill": 3,
    "database-disconnect": 3, "broker-disconnect": 3,
    "posix-disconnect": 3, "s3-disconnect": 3, "lost-response-retry": 3,
    "credential-rotation": 1, "ca-rotation": 1,
    "rbac-revocation": 1, "tenant-revocation": 1,
    "restore-pending": 1, "restore-retained-keys": 1, "restore-upgrade-rollback": 1,
}
CONNECTIONS = {
    "credential-rotation": ("api-oidc", "postgresql", "jetstream", "s3-workload-identity"),
    "ca-rotation": ("api-proxy", "postgresql", "jetstream", "s3", "oidc"),
}
IDENTITY = ("source_commit", "image_digest", "candidate_manifest_sha256",
            "configuration_sha256", "environment_id", "specification_sha256")
PREREQUISITES = ("specification_accepted", "candidate_frozen", "staging_verified",
                 "tenant_inventory_complete")
COMMON = ("source_preserved", "hashes_match", "placement_reconciled", "holds_preserved",
          "audit_continuous", "durable_job_identity", "no_duplicate_effects",
          "tenant_isolation_preserved",
          "no_unsafe_cleanup", "quarantines_reviewed")
RESTORE = ("all_tenant_catalogs", "object_versions_and_sidecars", "encryption_key_access",
           "main_and_dlq_streams", "consumer_state", "configuration_and_secret_versions",
           "independent_audit_checkpoints", "scheduler_disabled_verified",
           "generation_mismatch_quarantined", "isolated_target", "writers_fenced",
           "acknowledged_loss_accounted", "owner_resume_approved")
SPECIAL = {
    "worker-transfer-kill": ("interrupted_during_transfer", "retry_or_quarantine"),
    "worker-publication-kill": ("interrupted_during_publication", "retry_or_quarantine"),
    "database-disconnect": ("dependency_failed_under_work", "retry_or_quarantine"),
    "broker-disconnect": ("dependency_failed_under_work", "dlq_redrive_reconciled"),
    "posix-disconnect": ("dependency_failed_under_work", "retry_or_quarantine"),
    "s3-disconnect": ("dependency_failed_under_work", "retry_or_quarantine"),
    "lost-response-retry": ("uncertain_submission_reconciled", "same_logical_job",
                            "dlq_redrive_reconciled"),
    "credential-rotation": ("rotation_under_work", "new_credential_succeeds",
                            "old_credential_denied", "no_secret_leak"),
    "ca-rotation": ("rotation_under_work", "new_trust_succeeds", "old_trust_denied",
                    "untrusted_peer_denied", "hostname_verified", "reconnect_verified"),
    "rbac-revocation": ("queued_work_revalidated", "revoked_work_denied_before_effect",
                        "denial_audited_and_dead_lettered", "token_expiry_not_cancellation"),
    "tenant-revocation": ("queued_work_revalidated", "revoked_work_denied_before_effect",
                          "denial_audited_and_dead_lettered", "token_expiry_not_cancellation"),
    "restore-pending": ("pending_nonterminal_work_restored",),
    "restore-retained-keys": ("pre_rotation_key_used", "unavailable_key_denied"),
    "restore-upgrade-rollback": ("upgrade_tested", "rollback_tested",
                                 "application_schema_compatible", "pending_moves_preserved",
                                 "outage_measured"),
}
COUNT_PAIRS = (("objects_expected", "objects_verified"), ("holds_expected", "holds_verified"),
               ("audit_events_expected", "audit_events_verified"),
               ("pending_jobs_expected", "pending_jobs_reconciled"))
HOSTED_CI = "skipped: user instruction; known GitHub billing/spending restriction"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("nonfinite JSON number")


def read_json(path: Path):
    raw = path.read_bytes()
    return raw, json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def label(value) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and not re.search(r"REPLACE|TODO|CHANGEME", value, re.I))


def hexdigest(value, length=64) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{%d}" % length, value))


def instant(value) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value,
    ):
        raise ValueError("UTC Z timestamp required")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    return parsed


def assertions(kind):
    return COMMON + SPECIAL[kind] + (RESTORE if kind.startswith("restore-") else ())


def template() -> dict:
    """Unobserved records deliberately cannot pass; no example result is evidence."""
    identity = dict.fromkeys(IDENTITY)
    runs = []
    for kind, count in COUNTS.items():
        for trial in range(1, count + 1):
            run = {
                "id": f"{kind}-{trial}", "kind": kind, "identity": identity.copy(),
                "operator": None, "started_at": None, "completed_at": None,
                "result": "pending", "evidence": [], "checks": dict.fromkeys(assertions(kind)),
                "tenant_evidence": {tenant: [] for tenant in ("pilot-a", "pilot-b")},
                "connection_evidence": {key: [] for key in CONNECTIONS.get(kind, ())},
            }
            if kind.startswith("restore-"):
                run["restore"] = {
                    "recovery_set_id": None, "target_id": None,
                    "boundary_at": None, "incident_at": None, "reconciled_at": None,
                    "writers_resumed_at": None, "acknowledged_changes_lost": None,
                    "loss_manifest": None,
                    "tenants": {tenant: dict.fromkeys(
                        [field for pair in COUNT_PAIRS for field in pair]
                        + ["missing_objects", "extra_objects", "hash_mismatches"]
                    ) for tenant in ("pilot-a", "pilot-b")},
                }
            runs.append(run)
    return {
        "schema_version": 1, "mode": "live-staging", "specification": "m5-pilot-v1",
        "specification_revision": 1, "identity": identity, "scheduler_enabled": False,
        "identity_artifacts": {key: None for key in IDENTITY if key.endswith("sha256")},
        "tenants": ["pilot-a", "pilot-b"],
        "prerequisites": {key: {"observed": None, "evidence": []} for key in PREREQUISITES},
        "artifacts": {}, "runs": runs,
    }


def inspect(campaign: Path) -> dict:
    checks: dict[str, bool] = {}
    measurements: list[dict] = []
    report = {
        "schema_version": 1, "status": "incomplete", "production_qualified": False,
        "resume_authorized": False, "faults_injected": False, "hosted_ci": HOSTED_CI,
        "checks": checks, "restore_measurements": measurements,
    }
    try:
        raw, data = read_json(campaign)
        report["campaign_sha256"] = digest(raw)
        if not isinstance(data, dict):
            raise ValueError("campaign must be an object")
    except (OSError, ValueError, UnicodeError):
        checks["input.readable_unique_finite_json"] = False
        return report
    checks["input.readable_unique_finite_json"] = True
    checks["profile"] = (type(data.get("schema_version")) is int and data["schema_version"] == 1
                         and data.get("mode") == "live-staging"
                         and data.get("specification") == "m5-pilot-v1"
                         and type(data.get("specification_revision")) is int
                         and data["specification_revision"] == 1
                         and data.get("scheduler_enabled") is False)
    identity = data.get("identity")
    checks["identity"] = (isinstance(identity, dict) and set(identity) == set(IDENTITY)
                          and hexdigest(identity.get("source_commit"), 40)
                          and isinstance(identity.get("image_digest"), str)
                          and identity["image_digest"].startswith("sha256:")
                          and hexdigest(identity["image_digest"][7:])
                          and all(hexdigest(identity.get(key)) for key in IDENTITY
                                  if key.endswith("sha256"))
                          and label(identity.get("environment_id")))
    tenants = data.get("tenants")
    checks["tenants"] = (isinstance(tenants, list) and all(label(t) for t in tenants)
                         and len(tenants) == len(set(tenants))
                         and {"pilot-a", "pilot-b"} <= set(tenants))
    artifacts = data.get("artifacts")
    valid_artifacts = set()
    artifact_paths = set()
    artifact_hashes = set()
    checks["artifacts.present"] = isinstance(artifacts, dict) and bool(artifacts)
    if isinstance(artifacts, dict):
        for i, (name, artifact) in enumerate(artifacts.items()):
            valid = False
            try:
                relative = Path(artifact["path"])
                path = (campaign.parent / relative).resolve(strict=True)
                valid = (label(name) and not relative.is_absolute() and ".." not in relative.parts
                         and path.is_relative_to(campaign.parent.resolve())
                         and path != campaign.resolve() and path.is_file()
                         and hexdigest(artifact["sha256"])
                         and file_digest(path) == artifact["sha256"]
                         and path not in artifact_paths and artifact["sha256"] not in artifact_hashes)
                if valid:
                    valid_artifacts.add(name)
                    artifact_paths.add(path)
                    artifact_hashes.add(artifact["sha256"])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass
            checks[f"artifact.{i}"] = valid

    def refs(value):
        return (isinstance(value, list) and bool(value) and all(
            isinstance(item, str) and item in valid_artifacts for item in value
        ) and len(value) == len(set(value)))

    identity_artifacts = data.get("identity_artifacts")
    for key in IDENTITY:
        if key.endswith("sha256"):
            name = identity_artifacts.get(key) if isinstance(identity_artifacts, dict) else None
            checks["identity_artifact." + key] = (checks["identity"] and refs([name])
                and artifacts[name]["sha256"] == identity[key])

    prerequisites = data.get("prerequisites", {})
    for key in PREREQUISITES:
        value = prerequisites.get(key, {}) if isinstance(prerequisites, dict) else {}
        checks[f"prerequisite.{key}"] = (isinstance(value, dict)
                                            and value.get("observed") is True
                                            and refs(value.get("evidence")))
    runs = data.get("runs")
    checks["runs.present"] = isinstance(runs, list) and bool(runs)
    seen_ids, seen_evidence, seen_sets, seen_targets, seen_windows = set(), set(), set(), set(), set()
    counts = dict.fromkeys(COUNTS, 0)
    if not isinstance(runs, list):
        runs = []
    for i, run in enumerate(runs):
        prefix = f"run.{i}"
        if not isinstance(run, dict):
            checks[prefix + ".shape"] = False
            continue
        kind, run_id = run.get("kind"), run.get("id")
        known = isinstance(kind, str) and kind in COUNTS
        checks[prefix + ".kind"] = known
        checks[prefix + ".identity"] = checks["identity"] and run.get("identity") == identity
        valid_id = label(run_id) and run_id not in seen_ids
        checks[prefix + ".id"] = valid_id
        if valid_id:
            seen_ids.add(run_id)
        checks[prefix + ".result"] = run.get("result") == "passed"
        checks[prefix + ".operator"] = label(run.get("operator"))
        evidence = run.get("evidence")
        valid_refs = refs(evidence)
        # Every trial needs its own raw record, not aliases of another trial's file.
        checks[prefix + ".evidence"] = valid_refs and not (set(evidence) & seen_evidence)
        if valid_refs:
            seen_evidence.update(evidence)
        for field, required in (
            ("tenant_evidence", ("pilot-a", "pilot-b")),
            ("connection_evidence", CONNECTIONS.get(kind, ()) if known else ()),
        ):
            coverage = run.get(field)
            checks[prefix + "." + field] = (isinstance(coverage, dict)
                and set(coverage) == set(required)
                and all(refs(value) and valid_refs and set(value) <= set(evidence)
                        for value in coverage.values()))
        started = completed = None
        try:
            started, completed = instant(run["started_at"]), instant(run["completed_at"])
            window = (kind, started, completed)
            checks[prefix + ".window"] = (started < completed <= datetime.now(timezone.utc)
                                            and not any(previous_kind == kind and started < previous_end
                                                        and previous_start < completed
                                                        for previous_kind, previous_start, previous_end
                                                        in seen_windows))
            seen_windows.add(window)
        except (KeyError, ValueError, TypeError, OverflowError):
            checks[prefix + ".window"] = False
        observed = run.get("checks")
        if known:
            checks[prefix + ".observations"] = (isinstance(observed, dict)
                and set(observed) == set(assertions(kind))
                and all(observed[key] is True for key in assertions(kind)))
            if kind.startswith("restore-"):
                restore = run.get("restore")
                valid_restore = isinstance(restore, dict)
                checks[prefix + ".restore"] = valid_restore
                if valid_restore:
                    for field, seen in (("recovery_set_id", seen_sets), ("target_id", seen_targets)):
                        value = restore.get(field)
                        valid = label(value) and value not in seen
                        checks[prefix + "." + field] = valid
                        if valid:
                            seen.add(value)
                    try:
                        boundary, incident, reconciled, resumed = (
                            instant(restore[key]) for key in
                            ("boundary_at", "incident_at", "reconciled_at", "writers_resumed_at"))
                        rpo, rto = (incident - boundary).total_seconds(), (resumed - incident).total_seconds()
                        measurements.append({"run_index": i, "rpo_seconds": rpo, "rto_seconds": rto})
                        checks[prefix + ".recovery_times"] = (
                            started is not None and completed is not None
                            and boundary <= incident and started <= incident < reconciled <= resumed <= completed
                            and 0 <= rpo <= 86400 and 0 < rto <= 3600)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        checks[prefix + ".recovery_times"] = False
                    loss = restore.get("acknowledged_changes_lost")
                    checks[prefix + ".loss_accounting"] = (
                        type(loss) is int and loss >= 0 and refs([restore.get("loss_manifest")])
                        and restore.get("loss_manifest") in (evidence if valid_refs else []))
                    partitions = restore.get("tenants")
                    valid_partitions = (checks["tenants"] and isinstance(partitions, dict)
                                        and set(partitions) == set(tenants))
                    checks[prefix + ".tenant_inventory"] = valid_partitions
                    if valid_partitions:
                        for index, tenant in enumerate(tenants):
                            observed_tenant = partitions[tenant]
                            valid = isinstance(observed_tenant, dict)
                            if valid:
                                valid = all(type(observed_tenant.get(expected)) is int
                                    and type(observed_tenant.get(actual)) is int
                                    and observed_tenant[expected] == observed_tenant[actual]
                                    and observed_tenant[expected] >= (1 if tenant in {"pilot-a", "pilot-b"}
                                        and (expected != "pending_jobs_expected" or kind in
                                             {"restore-pending", "restore-upgrade-rollback"}) else 0)
                                    for expected, actual in COUNT_PAIRS)
                                valid = valid and all(type(observed_tenant.get(key)) is int
                                    and observed_tenant[key] == 0 for key in
                                    ("missing_objects", "extra_objects", "hash_mismatches"))
                            checks[f"{prefix}.tenant.{index}"] = valid
            if all(value for key, value in checks.items() if key.startswith(prefix + ".")):
                counts[kind] += 1
    for kind, count in COUNTS.items():
        checks["repetitions." + kind] = counts[kind] >= count
    report["valid_repetitions"] = counts
    if all(checks.values()):
        report["status"] = "complete-for-review"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write an unobserved campaign template")
    init.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("check", help="verify retained evidence and measured gates offline")
    check.add_argument("--campaign", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    # Refuse to destroy prior evidence, including an input file or symlink target.
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists; choose a new evidence path")
    result = template() if args.command == "init" else inspect(args.campaign)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return 0 if args.command == "init" or result["status"] == "complete-for-review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
