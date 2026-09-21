"""Offline, fabricated evidence tests for the #164 checker, never recovery proof.

All identities, operators, counts and retained records below are synthetic and
confined to pytest temporary storage. No faults or external services are used.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def checker():
    path = Path(__file__).resolve().parents[2] / "scripts/recovery_qualification.py"
    spec = importlib.util.spec_from_file_location("recovery_qualification", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _restores(data):
    return [run for run in data["runs"] if run["kind"].startswith("restore-")]


def _incomplete(report):
    assert report["status"] == "incomplete"
    assert report["production_qualified"] is False
    assert report["resume_authorized"] is False
    assert report["faults_injected"] is False
    assert not all(report["checks"].values())


@pytest.fixture
def campaign(checker, tmp_path):
    """Build input to exercise validation only, not a purported staging report."""
    directory = tmp_path / "fabricated-checker-input"
    directory.mkdir()
    artifacts = directory / "artifacts"
    artifacts.mkdir()
    data = checker.template()
    identity = {
        "source_commit": "1" * 40,
        "image_digest": "sha256:" + "2" * 64,
        "candidate_manifest_sha256": "3" * 64,
        "configuration_sha256": "4" * 64,
        "environment_id": "synthetic-offline-checker-fixture",
        "specification_sha256": "5" * 64,
    }
    data["identity"] = identity

    def record(name):
        path = artifacts / f"{name}.txt"
        raw = f"FABRICATED OFFLINE CHECKER TEST ONLY: {name}\n".encode()
        path.write_bytes(raw)
        data["artifacts"][name] = {
            "path": str(path.relative_to(directory)), "sha256": _sha(raw),
        }
        return name

    for key in data["prerequisites"]:
        data["prerequisites"][key] = {"observed": True, "evidence": [record(key)]}
    for key in data["identity_artifacts"]:
        name = record(key)
        data["identity_artifacts"][key] = name
        identity[key] = data["artifacts"][name]["sha256"]
    for index, run in enumerate(data["runs"]):
        started = datetime(2024, 1, 2, tzinfo=timezone.utc) + timedelta(days=index)
        run.update({
            "identity": identity.copy(), "operator": "synthetic-test-operator",
            "started_at": _stamp(started),
            "completed_at": _stamp(started + timedelta(minutes=30)),
            "result": "passed", "evidence": [record(run["id"])],
            "checks": dict.fromkeys(run["checks"], True),
        })
        run["tenant_evidence"] = {
            tenant: run["evidence"][:] for tenant in ("pilot-a", "pilot-b")
        }
        run["connection_evidence"] = {
            connection: run["evidence"][:] for connection in run.get("connection_evidence", {})
        }
        if "restore" in run:
            run["restore"].update({
                "recovery_set_id": f"synthetic-set-{index}",
                "target_id": f"synthetic-isolated-target-{index}",
                "boundary_at": _stamp(started - timedelta(hours=1)),
                "incident_at": _stamp(started),
                "reconciled_at": _stamp(started + timedelta(minutes=10)),
                "writers_resumed_at": _stamp(started + timedelta(minutes=15)),
                "acknowledged_changes_lost": 0, "loss_manifest": run["evidence"][0],
                "tenants": {
                    tenant: {
                        "objects_expected": 20, "objects_verified": 20,
                        "holds_expected": 2, "holds_verified": 2,
                        "audit_events_expected": 40, "audit_events_verified": 40,
                        "pending_jobs_expected": 3, "pending_jobs_reconciled": 3,
                        "missing_objects": 0, "extra_objects": 0, "hash_mismatches": 0,
                    } for tenant in ("pilot-a", "pilot-b")
                },
            })
    return directory / "campaign.json", data


def test_complete_synthetic_input_only_completes_review(checker, campaign):
    path, data = campaign
    report = checker.inspect(_write(path, data))
    assert report["status"] == "complete-for-review"
    assert all(report["checks"].values())
    assert report["production_qualified"] is False
    assert report["resume_authorized"] is False
    assert report["faults_injected"] is False
    assert report["hosted_ci"] == (
        "skipped: user instruction; known GitHub billing/spending restriction"
    )
    assert report["campaign_sha256"] == _sha(path.read_bytes())
    assert report["valid_repetitions"] == {
        "worker-transfer-kill": 3, "worker-publication-kill": 3,
        "database-disconnect": 3, "broker-disconnect": 3,
        "posix-disconnect": 3, "s3-disconnect": 3, "lost-response-retry": 3,
        "credential-rotation": 1, "ca-rotation": 1,
        "rbac-revocation": 1, "tenant-revocation": 1,
        "restore-pending": 1, "restore-retained-keys": 1, "restore-upgrade-rollback": 1,
    }
    assert len(report["restore_measurements"]) == 3


def test_template_cannot_be_confused_with_observed_evidence(checker, tmp_path):
    data = checker.template()
    report = checker.inspect(_write(tmp_path / "campaign.json", data))
    _incomplete(report)
    assert all(value == 0 for value in report["valid_repetitions"].values())
    assert all(run["result"] == "pending" for run in data["runs"])
    assert all(value is None for run in data["runs"] for value in run["checks"].values())


@pytest.mark.parametrize("kind", [
    "worker-transfer-kill", "worker-publication-kill", "database-disconnect",
    "broker-disconnect", "posix-disconnect", "s3-disconnect", "lost-response-retry",
    "credential-rotation", "ca-rotation", "rbac-revocation", "tenant-revocation",
    "restore-pending", "restore-retained-keys", "restore-upgrade-rollback",
])
def test_every_required_repetition_must_be_present(checker, campaign, kind):
    path, data = campaign
    data["runs"].remove(next(run for run in data["runs"] if run["kind"] == kind))
    report = checker.inspect(_write(path, data))
    _incomplete(report)
    assert report["checks"]["repetitions." + kind] is False


@pytest.mark.parametrize("field", [
    "source_commit", "image_digest", "candidate_manifest_sha256",
    "configuration_sha256", "environment_id", "specification_sha256",
])
@pytest.mark.parametrize("scope", ["campaign", "run"])
def test_identity_is_complete_and_consistent(checker, campaign, field, scope):
    path, data = campaign
    target = data if scope == "campaign" else data["runs"][0]
    target["identity"].pop(field)
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("field", [
    "candidate_manifest_sha256", "configuration_sha256", "specification_sha256",
])
@pytest.mark.parametrize("change", ["omitted", "missing-artifact", "mismatch"])
def test_identity_hashes_bind_to_retained_files(checker, campaign, field, change):
    path, data = campaign
    if change == "omitted":
        data["identity_artifacts"].pop(field)
    elif change == "missing-artifact":
        data["identity_artifacts"][field] = "absent-synthetic-artifact"
    else:
        data["identity"][field] = "a" * 64
        for run in data["runs"]:
            run["identity"][field] = "a" * 64
    report = checker.inspect(_write(path, data))
    _incomplete(report)
    assert report["checks"]["identity_artifact." + field] is False


@pytest.mark.parametrize("tenant", ["pilot-a", "pilot-b"])
@pytest.mark.parametrize("change", ["omitted", "empty", "another-trial"])
def test_each_trial_has_evidence_for_both_pilot_tenants(checker, campaign, tenant, change):
    path, data = campaign
    run = data["runs"][0]
    if change == "omitted":
        run["tenant_evidence"].pop(tenant)
    elif change == "empty":
        run["tenant_evidence"][tenant] = []
    else:
        run["tenant_evidence"][tenant] = data["runs"][1]["evidence"][:]
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("kind,connection", [
    ("credential-rotation", "api-oidc"), ("credential-rotation", "postgresql"),
    ("credential-rotation", "jetstream"), ("credential-rotation", "s3-workload-identity"),
    ("ca-rotation", "api-proxy"), ("ca-rotation", "postgresql"),
    ("ca-rotation", "jetstream"), ("ca-rotation", "s3"), ("ca-rotation", "oidc"),
])
@pytest.mark.parametrize("change", ["omitted", "empty", "another-trial"])
def test_rotation_covers_every_connection(checker, campaign, kind, connection, change):
    path, data = campaign
    run = next(run for run in data["runs"] if run["kind"] == kind)
    if change == "omitted":
        run["connection_evidence"].pop(connection)
    elif change == "empty":
        run["connection_evidence"][connection] = []
    else:
        run["connection_evidence"][connection] = data["runs"][0]["evidence"][:]
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("field", [
    "specification_accepted", "candidate_frozen", "staging_verified", "tenant_inventory_complete",
])
def test_prerequisites_need_observation_and_retained_evidence(checker, campaign, field):
    path, data = campaign
    data["prerequisites"][field]["evidence"] = []
    _incomplete(checker.inspect(_write(path, data)))
    data["prerequisites"][field]["observed"] = False
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("change", ["campaign-missing", "restore-missing", "extra", "duplicate"])
def test_every_declared_tenant_must_reconcile(checker, campaign, change):
    path, data = campaign
    if change == "campaign-missing":
        data["tenants"].remove("pilot-b")
    elif change == "restore-missing":
        _restores(data)[0]["restore"]["tenants"].pop("pilot-b")
    elif change == "extra":
        data["tenants"].append("synthetic-extra-tenant")
    else:
        data["tenants"].append("pilot-a")
    _incomplete(checker.inspect(_write(path, data)))


def test_empty_retained_tenant_can_be_inventoried_and_reconciled(checker, campaign):
    path, data = campaign
    data["tenants"].append("retained-empty-tenant")
    for run in _restores(data):
        run["restore"]["tenants"]["retained-empty-tenant"] = dict.fromkeys(
            run["restore"]["tenants"]["pilot-a"], 0,
        )
    assert checker.inspect(_write(path, data))["status"] == "complete-for-review"


@pytest.mark.parametrize("component", [
    "all_tenant_catalogs", "object_versions_and_sidecars", "encryption_key_access",
    "main_and_dlq_streams", "consumer_state", "configuration_and_secret_versions",
    "independent_audit_checkpoints", "scheduler_disabled_verified",
    "generation_mismatch_quarantined", "isolated_target", "writers_fenced",
    "acknowledged_loss_accounted", "owner_resume_approved",
])
def test_restore_requires_every_cross_plane_component(checker, campaign, component):
    path, data = campaign
    _restores(data)[0]["checks"].pop(component)
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("field", [
    "objects_expected", "objects_verified", "holds_expected", "holds_verified",
    "audit_events_expected", "audit_events_verified", "pending_jobs_expected",
    "pending_jobs_reconciled", "missing_objects", "extra_objects", "hash_mismatches",
])
@pytest.mark.parametrize("invalid", [True, False, -1, 1.0, "1", None])
def test_inventory_counts_are_integers_not_coercible_values(checker, campaign, field, invalid):
    path, data = campaign
    _restores(data)[0]["restore"]["tenants"]["pilot-a"][field] = invalid
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("field", ["objects_verified", "holds_verified", "audit_events_verified"])
def test_inventory_mismatch_is_not_a_success(checker, campaign, field):
    path, data = campaign
    _restores(data)[0]["restore"]["tenants"]["pilot-a"][field] += 1
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("kind", ["restore-pending", "restore-upgrade-rollback"])
def test_nonterminal_work_must_be_present_not_only_asserted(checker, campaign, kind):
    path, data = campaign
    run = next(run for run in data["runs"] if run["kind"] == kind)
    for tenant in run["restore"]["tenants"].values():
        tenant["pending_jobs_expected"] = tenant["pending_jobs_reconciled"] = 0
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("rpo,rto", [(86400, 3600), (0, 1)])
def test_recovery_objective_exact_bounds_are_inclusive(checker, campaign, rpo, rto):
    path, data = campaign
    run = _restores(data)[0]
    incident = datetime(2024, 4, 1, tzinfo=timezone.utc)
    run["started_at"] = _stamp(incident)
    run["completed_at"] = _stamp(incident + timedelta(seconds=rto))
    run["restore"].update({
        "boundary_at": _stamp(incident - timedelta(seconds=rpo)),
        "incident_at": _stamp(incident),
        "reconciled_at": _stamp(incident + timedelta(seconds=rto)),
        "writers_resumed_at": _stamp(incident + timedelta(seconds=rto)),
    })
    report = checker.inspect(_write(path, data))
    assert report["status"] == "complete-for-review"
    assert report["restore_measurements"][0]["rpo_seconds"] == rpo
    assert report["restore_measurements"][0]["rto_seconds"] == rto


@pytest.mark.parametrize("rpo,rto", [(86401, 60), (0, 3601), (-1, 60), (0, 0)])
def test_one_failed_restore_cannot_be_averaged_away(checker, campaign, rpo, rto):
    path, data = campaign
    run = _restores(data)[0]
    incident = datetime(2024, 4, 1, tzinfo=timezone.utc)
    run["started_at"] = _stamp(incident)
    run["completed_at"] = _stamp(incident + timedelta(hours=2))
    run["restore"].update({
        "boundary_at": _stamp(incident - timedelta(seconds=rpo)),
        "incident_at": _stamp(incident),
        "reconciled_at": _stamp(incident + timedelta(seconds=max(rto, 0))),
        "writers_resumed_at": _stamp(incident + timedelta(seconds=rto)),
    })
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("value", [
    "2024-01-02T00:00:00", "2024-01-02T00:00:00+00:00",
    "2024-01-02T00:00:00+01:00", "2024-01-02Z", "invalid", None, 1, [], {},
])
def test_timestamps_fail_safe_without_utc_instants(checker, campaign, value):
    path, data = campaign
    data["runs"][0]["started_at"] = value
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("change", ["backwards", "future", "resume-before-reconciliation",
                                     "incident-before-start", "resume-after-completion"])
def test_observation_order_and_future_times_cannot_pass(checker, campaign, change):
    path, data = campaign
    run = _restores(data)[0]
    if change == "backwards":
        run["started_at"], run["completed_at"] = run["completed_at"], run["started_at"]
    elif change == "future":
        run["completed_at"] = "9999-01-01T00:00:00Z"
    elif change == "resume-before-reconciliation":
        run["restore"]["writers_resumed_at"] = run["restore"]["incident_at"]
    elif change == "incident-before-start":
        run["started_at"] = run["completed_at"]
    else:
        run["completed_at"] = run["restore"]["incident_at"]
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("field", ["recovery_set_id", "target_id"])
def test_full_restores_need_independent_sets_and_targets(checker, campaign, field):
    path, data = campaign
    first, second, _ = _restores(data)
    second["restore"][field] = first["restore"][field]
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("change", ["run-id", "window", "overlap", "evidence"])
def test_fault_trials_need_independent_records(checker, campaign, change):
    path, data = campaign
    first, second = data["runs"][:2]
    if change == "run-id":
        second["id"] = first["id"]
    elif change == "window":
        second["started_at"], second["completed_at"] = first["started_at"], first["completed_at"]
    elif change == "overlap":
        second["started_at"] = first["started_at"].replace("00:00:00Z", "00:00:00.000001Z")
        second["completed_at"] = first["completed_at"].replace("00:30:00Z", "00:30:00.000001Z")
    else:
        second["evidence"] = first["evidence"][:]
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("invalid", [True, False, -1, 0.0, "0", None])
def test_loss_counts_reject_ambiguous_values(checker, campaign, invalid):
    path, data = campaign
    _restores(data)[0]["restore"]["acknowledged_changes_lost"] = invalid
    _incomplete(checker.inspect(_write(path, data)))


def test_loss_accounting_manifest_must_be_retained_for_that_trial(checker, campaign):
    path, data = campaign
    first, second, _ = _restores(data)
    first["restore"]["loss_manifest"] = second["evidence"][0]
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("change", ["missing", "tampered", "bad-hash", "absolute", "traversal",
                                     "duplicate-path", "hardlink", "symlink", "escape-symlink"])
def test_retained_artifacts_reject_missing_tampered_or_aliased_paths(checker, campaign, change):
    path, data = campaign
    first = data["artifacts"][data["runs"][0]["evidence"][0]]
    second = data["artifacts"][data["runs"][1]["evidence"][0]]
    original = path.parent / first["path"]
    other = path.parent / second["path"]
    if change == "missing":
        original.unlink()
    elif change == "tampered":
        original.write_bytes(b"altered synthetic evidence")
    elif change == "bad-hash":
        first["sha256"] = "f" * 64
    elif change == "absolute":
        first["path"] = str(original)
    elif change == "traversal":
        first["path"] = "artifacts/../" + first["path"]
    elif change == "duplicate-path":
        second.update(first)
    elif change == "hardlink":
        other.unlink()
        os.link(original, other)
        second["sha256"] = first["sha256"]
    elif change == "symlink":
        other.unlink()
        other.symlink_to(original)
        second["sha256"] = first["sha256"]
    else:
        original_bytes = original.read_bytes()
        original.unlink()
        destination = path.parent / "synthetic-target.txt"
        if change == "escape-symlink":
            destination = path.parent.parent / "synthetic-outside.txt"
        destination.write_bytes(original_bytes)
        original.symlink_to(destination)
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("payload", [
    b'{"schema_version": 1, "schema_version": 1}',
    b'{"nested": {"duplicate": 1, "duplicate": 2}}',
    b'{"value": NaN}', b'{"value": Infinity}', b'{"value": -Infinity}',
    b'{"unfinished":', b'\xff', b'null', b'[]', b'"text"',
])
def test_bad_json_fails_safe_without_echoing_input(checker, tmp_path, payload):
    path = tmp_path / "malformed.json"
    path.write_bytes(payload)
    report = checker.inspect(path)
    _incomplete(report)
    assert report["checks"]["input.readable_unique_finite_json"] is False


@pytest.mark.parametrize("field,value", [
    ("identity", []), ("identity", None), ("tenants", [None]), ("tenants", [{}]),
    ("artifacts", [1]), ("artifacts", {"bad": []}), ("artifacts", {"bad": None}),
    ("prerequisites", []), ("runs", {}), ("runs", [None]), ("runs", [1]),
    ("schema_version", True), ("specification_revision", True), ("scheduler_enabled", 0),
])
def test_malformed_campaign_shapes_do_not_crash(checker, campaign, field, value):
    path, data = campaign
    data[field] = value
    _incomplete(checker.inspect(_write(path, data)))


@pytest.mark.parametrize("field,value", [
    ("kind", []), ("id", {}), ("identity", []), ("evidence", [[]]),
    ("checks", []), ("operator", {}), ("restore", []),
])
def test_malformed_run_shapes_do_not_crash(checker, campaign, field, value):
    path, data = campaign
    _restores(data)[0][field] = value
    _incomplete(checker.inspect(_write(path, data)))


def test_reports_never_echo_untrusted_identifiers_or_raw_artifacts(checker, campaign):
    path, data = campaign
    sensitive = "synthetic-secret-DO-NOT-ECHO"
    data["identity"]["environment_id"] = sensitive
    data["runs"][0]["id"] = sensitive
    data["runs"][0]["operator"] = sensitive
    data["runs"][0]["checks"][sensitive] = False
    data["tenants"].append(sensitive)
    data["artifacts"][sensitive] = {"path": sensitive, "sha256": sensitive}
    _write(path, data)
    report = checker.inspect(path)
    _incomplete(report)
    assert sensitive not in json.dumps(report)


def _cli(checker, *args):
    return subprocess.run(
        [sys.executable, checker.__file__, *map(str, args)],
        text=True, capture_output=True, check=False,
    )


def test_cli_init_and_incomplete_check(checker, tmp_path):
    campaign = tmp_path / "unobserved.json"
    initialized = _cli(checker, "init", "--output", campaign)
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(campaign.read_text()) == checker.template()
    output = tmp_path / "report.json"
    checked = _cli(checker, "check", "--campaign", campaign, "--output", output)
    assert checked.returncode == 1, checked.stderr
    _incomplete(json.loads(output.read_text()))


def test_cli_complete_input_still_does_not_authorize_production(checker, campaign):
    path, data = campaign
    output = path.parent / "report.json"
    result = _cli(checker, "check", "--campaign", _write(path, data), "--output", output)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["status"] == "complete-for-review"
    assert report["production_qualified"] is report["resume_authorized"] is False
    assert report["faults_injected"] is False


@pytest.mark.parametrize("command", ["init", "check"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink", "dangling-symlink"])
def test_cli_refuses_to_overwrite_retained_evidence(checker, campaign, command, alias):
    path, data = campaign
    _write(path, data)
    previous = path.read_bytes()
    output = path.parent / "existing.json"
    if alias == "direct":
        output = path
    elif alias == "symlink":
        output.symlink_to(path)
    elif alias == "hardlink":
        os.link(path, output)
    else:
        output.symlink_to(path.parent / "absent-target.json")
    args = [command, "--output", output]
    if command == "check":
        args.extend(["--campaign", path])
    result = _cli(checker, *args)
    assert result.returncode == 2
    assert "output already exists" in result.stderr
    assert path.read_bytes() == previous
    if alias == "dangling-symlink":
        assert output.is_symlink()
        assert not output.exists()
