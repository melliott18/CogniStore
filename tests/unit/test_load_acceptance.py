"""Synthetic evidence exercises arithmetic; no fixture claims a staging run."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import load_acceptance as acceptance
from scripts import load_qualification as base
from scripts.load_workload import SIZE_CYCLE


def at(seconds):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def metadata():
    return {
        "run_id": "synthetic-math-run",
        "bindings": {"source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
                     "configuration_sha256": "c" * 64, "specification_sha256": "d" * 64,
                     "dependency_manifest_sha256": "e" * 64, "corpus_manifest_sha256": "f" * 64,
                     "environment_id": "synthetic-local-fixture"},
        "profile_sha256": base.digest(base.PROFILE.read_bytes()),
        "execution_scope": "synthetic", "started_at": at(0), "ended_at": at(300000),
        "status": "observations-collected", "stop_latched": False, "uncertain_mutations": 0,
        "phases": {
            "nominal": {"started_at": at(1800), "ended_at": at(261000), "duration_seconds": 259200,
                        "actual_ended_at": at(261001), "completed": True,
                        "warmup_seconds": 1800, "object_count": 100000},
            "burst": {"started_at": at(262000), "ended_at": at(262900), "duration_seconds": 900,
                      "actual_ended_at": at(262901), "completed": True,
                      "warmup_seconds": 0, "object_count": 100000},
            "capacity": {"started_at": at(265000), "ended_at": at(272200), "duration_seconds": 7200,
                         "actual_ended_at": at(272201), "completed": True,
                         "warmup_seconds": 1800, "object_count": 200000},
        },
        "movement_windows": [
            {"window_id": f"window-{i}", "direction": direction, "started_at": at(275000 + i * 4000),
             "ended_at": at(278600 + i * 4000), "dedicated": True}
            for i, direction in enumerate(acceptance.DIRECTIONS)],
        "required_faults": [{"fault_id": kind, "kind": kind} for kind in acceptance.FAULT_KINDS],
        "retry_limit": 3, "induced_alert_ids": [f"alert-{kind}" for kind in acceptance.FAULT_KINDS],
    }


def bind(campaign, row):
    return {"run_id": campaign["run_id"], "binding_sha256": acceptance.binding_digest(campaign), **row}


def write_rows(directory, name, campaign, records):
    with (directory / name).open("w") as destination:
        for row in records:
            destination.write(json.dumps(bind(campaign, row)) + "\n")


def seal(directory, campaign):
    (directory / "campaign.json").write_text(json.dumps(campaign))
    hashes = {name: base.digest((directory / name).read_bytes()) for name in acceptance.FILES}
    (directory / "acceptance-manifest.json").write_text(json.dumps({
        "schema_version": 1, "run_id": campaign["run_id"], "campaign_sha256": hashes["campaign.json"],
        "artifacts": hashes,
    }))


def full_bundle(directory):
    """All supplemental gates use exact production targets, with fake observations.

    Foreground/resource coverage remains the existing calculator's concern;
    tests of supplemental integration replace that calculator explicitly.
    No production profile values or durations are weakened here.
    """
    campaign = metadata()
    records = {name: [] for name in acceptance.FILES if name.endswith(".jsonl")}
    artifacts = records["observed-artifacts.jsonl"]

    def proof(name, instant, kind):
        content = bind(campaign, {"synthetic": True, "observation_id": name, "kind": kind})
        artifacts.append({"evidence_id": name, "observed_at": at(instant), "kind": kind, "content": content})
        return name

    def job(job_id, tenant, kind, published, completed):
        records["jobs.jsonl"].append({
            "job_id": job_id, "tenant": tenant, "kind": kind, "published_at": at(published),
            "accepted_at": at(published), "completed_at": at(completed), "outcome": "succeeded",
            "object_count": 100 if kind == "scan" else 1000, "attempt_count": 1,
        })

    for seconds in range(0, 259200, 300):
        for tenant in ("pilot-a", "pilot-b"):
            job_id = f"scan-{tenant}-{seconds}"
            start = 1800 + seconds
            job(job_id, tenant, "scan", start, start + 1)
            records["scans.jsonl"].append({"attempt_id": job_id, "job_id": job_id,
                                          "published_at": at(start), "completed_at": at(start + 1),
                                          "attempt_number": 1, "success": True})
    for seconds in range(0, 259200, 3600):
        for tenant in ("pilot-a", "pilot-b"):
            start = 1802 + seconds
            job(f"policy-{tenant}-{seconds}", tenant, "policy", start, start + (25 if seconds == 0 else 1))
    for index, direction in enumerate(acceptance.DIRECTIONS):
        for cohort in ("nominal", "movement"):
            if cohort == "nominal":
                job_id = f"policy-pilot-{'ab'[index]}-0"
                start = 1802
            else:
                start = 275000 + index * 4000
                job_id = f"movement-{index}"
                job(job_id, "pilot-a", "move", start, start + 3600)
            for number in range(10000):
                if cohort == "nominal":
                    job_id = f"policy-pilot-{'ab'[index]}-{(number // 1000) * 3600}"
                instant = start + ((number // 1000) * 3600 + (number % 1000) * .0005
                                   if cohort == "nominal" else number * .359)
                call = f"{cohort}-{index}-{number}"
                records["movement.jsonl"].append({
                    "call_id": call, "logical_move_id": call, "job_id": job_id,
                    "cohort": cohort, "direction": direction, "size_bytes": SIZE_CYCLE[number % 100],
                    "started_at": at(instant), "completed_at": at(instant + .001), "success": True,
                    "verified": True, "attempt_number": 1, "terminal_state": "succeeded",
                    "movement_window_id": f"window-{index}" if cohort == "movement" else None,
                })
        job_id = f"capacity-{index}"
        start = 265000 + index * 10
        job(job_id, "pilot-a", "move", start, start + 1)
        records["movement.jsonl"].append({
            "call_id": job_id, "logical_move_id": job_id, "job_id": job_id,
            "cohort": "capacity", "direction": direction, "size_bytes": 4096,
            "started_at": at(start), "completed_at": at(start + 1), "success": True,
            "verified": True, "attempt_number": 1, "terminal_state": "succeeded", "movement_window_id": None,
        })
        for seconds in range(0, 3600, 15):
            instant = 275000 + index * 4000 + seconds
            records["movement-backlog.jsonl"].append({
                "window_id": f"window-{index}", "direction": direction, "observed_at": at(instant),
                "pending_moves": 100, "in_flight_moves": 2,
                "source_evidence_id": proof(f"backlog-{index}-{seconds}", instant, "directional-worker-backlog"),
            })
    scopes = {f"phase:{name}": (base.timestamp(phase["started_at"]) - base.timestamp(at(0)),
                                base.timestamp(phase["ended_at"]) - base.timestamp(at(0)))
              for name, phase in campaign["phases"].items()}
    for index, kind in enumerate(acceptance.FAULT_KINDS):
        start, cleared, recovered = (262000, 262900, 263000) if kind == "burst" else (
            285000 + index * 1000, 285010 + index * 1000, 285020 + index * 1000)
        observations = {
            "saturation": {"queue_cap": 10000, "rejected_submissions": 1, "stop_submissions_observed": True},
            "backend_throttle": {"throttled_calls": 1},
            "bounded_retry": {"attempts": 3, "max_attempts": 3, "unresolved": 0},
            "burst": {"nominal_latency_restored": True, "queue_age_restored": True},
        }[kind]
        records["faults.jsonl"].append({
            "fault_id": kind, "kind": kind, "started_at": at(start), "cleared_at": at(cleared),
            "recovered_at": at(recovered), "source_evidence_id": proof(f"fault-proof-{kind}", recovered, "fault"),
            "observations": observations, "alert_ids": [f"alert-{kind}"],
        })
        scopes[f"fault:{kind}"] = (start, recovered)
        receipt_id = proof(f"receipt-{kind}", start + 2, "notification-receiver")
        recovery_id = proof(f"recovery-{kind}", cleared + 1, "notification-receiver")
        acknowledgement_id = proof(f"acknowledgement-{kind}", start + 3, "operator-acknowledgement")
        runbook_evidence_id = proof(f"runbook-{kind}", start + 4, "operator-runbook")
        receipt_content = next(row["content"] for row in artifacts if row["evidence_id"] == receipt_id)
        records["alerts.jsonl"].append({
            "alert_id": f"alert-{kind}", "condition_started_at": at(start), "fired_at": at(start + 1),
            "received_at": at(start + 2), "acknowledged_at": at(start + 3), "runbook_executed_at": at(start + 4),
            "cleared_at": at(cleared), "recovery_received_at": at(cleared + 1), "receiver_id": "fixture-owner",
            "runbook_url": "https://example.invalid/runbook", "receipt_id": receipt_id,
            "acknowledgement_id": acknowledgement_id, "runbook_evidence_id": runbook_evidence_id,
            "recovery_receipt_id": recovery_id, "source_evidence_sha256": base.digest(acceptance.canonical(receipt_content)),
        })
    for scope, (start, end) in scopes.items():
        for boundary, instant in (("before", start), ("after", end)):
            for tenant in ("pilot-a", "pilot-b"):
                checkpoint_id = f"{scope}-{boundary}-{tenant}"
                records["reconciliations.jsonl"].append({
                    "checkpoint_id": checkpoint_id, "scope": scope, "boundary": boundary, "tenant": tenant,
                    "observed_at": at(instant), "expected_objects": 50000, "observed_objects": 50000,
                    "checks": dict.fromkeys(acceptance.RECONCILIATION_CHECKS, 0),
                    "source_evidence_id": proof(checkpoint_id, instant, "reconciliation"),
                })
    for cohort in ("nominal", "capacity"):
        for stage in ("baseline", "drain"):
            instant = scopes[f"phase:{cohort}"][int(stage == "drain")]
            records["resource-checkpoints.jsonl"].append({
                "cohort": cohort, "stage": stage, "observed_at": at(instant),
                "values": dict.fromkeys(acceptance.RESOURCE_VALUES, 100 if stage == "baseline" else 110),
                "incidents": dict.fromkeys(acceptance.INCIDENTS, 0),
                "source_evidence_id": proof(f"resource-{cohort}-{stage}", instant, "resources"),
            })
    cost = bind(campaign, {
        "observed_at": at(290000), "source_evidence_id": proof("cost-proof", 290000, "physical-consumption"),
        "physical_usage": dict.fromkeys(("hot_bytes", "catalog_bytes", "broker_bytes", "ingress_bytes", "egress_bytes"), 1),
        "cost_inputs": {"currency": "USD", "compute_hours": 72, "storage_gib_hours": 10,
                        "egress_gib": 1, "observed_cost": 1},
        "scaling_recommendation": "Synthetic arithmetic fixture only; no real capacity recommendation.",
    })
    by_id = {row["evidence_id"]: row for row in artifacts}
    for row in [cost, *(row for rows in records.values() for row in rows if "source_evidence_id" in row)]:
        by_id[row["source_evidence_id"]]["content"]["observation"] = {
            key: value for key, value in row.items()
            if key not in ("source_evidence_id", "run_id", "binding_sha256")}
    for row in records["alerts.jsonl"]:
        for reference, instant, extras in (
            ("receipt_id", "received_at", ()), ("recovery_receipt_id", "recovery_received_at", ()),
            ("acknowledgement_id", "acknowledged_at", ()),
            ("runbook_evidence_id", "runbook_executed_at", ("runbook_url",)),
        ):
            by_id[row[reference]]["content"]["observation"] = {
                field: row[field] for field in ("alert_id", "receiver_id", reference, instant, *extras)}
        row["source_evidence_sha256"] = base.digest(acceptance.canonical(by_id[row["receipt_id"]]["content"]))
    for name, rows in records.items():
        write_rows(directory, name, campaign, rows)
    (directory / "capacity-cost.json").write_text(json.dumps(cost))
    seal(directory, campaign)
    return campaign, records


@pytest.fixture(scope="module")
def complete_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("acceptance-math")
    campaign, records = full_bundle(directory)
    return directory, campaign, records


def test_complete_supplemental_math_passes_without_claiming_live_qualification(complete_bundle, monkeypatch):
    directory, _, _ = complete_bundle
    # Existing calculator has its own exact-quantile/coverage tests. This stub
    # isolates supplemental acceptance, and the reported scope stays synthetic.
    monkeypatch.setattr(base, "evaluate", lambda _: {"checks": {"synthetic_component_stub": {"status": "passed"}}, "errors": []})
    report = acceptance.evaluate(directory)
    assert report["errors"] == []
    assert {name: result for name, result in report["checks"].items()
            if result["status"] != "passed"} == {"campaign.staging_scope": {"status": "incomplete"}}
    assert report["status"] == "incomplete"
    assert report["execution_scope"] == "synthetic"
    assert report["production_qualified"] is False
    assert report["checks"]["movement.success"]["denominator"] == 40002
    assert report["checks"]["scans.publication_to_completion"]["denominator"] == 1728


def evidence_for(tmp_path, campaign=None):
    campaign = campaign or metadata()
    report = {"checks": {}}
    return acceptance.Evidence(tmp_path, campaign, report), report


def test_real_foreground_evaluator_is_required_and_synthetic_never_qualifies(complete_bundle):
    directory, _, _ = complete_bundle
    report = acceptance.evaluate(directory)
    assert report["status"] != "passed"
    assert report["checks"]["client_resource.identity.staging_scope"]["status"] == "incomplete"
    assert report["checks"]["client_resource.nominal.client_coverage"]["status"] == "failed"
    assert report["production_qualified"] is False


def test_manifest_rejects_missing_path_traversal_digest_mismatch_and_symlink(tmp_path):
    assert acceptance.evaluate(tmp_path)["errors"] == ["missing_or_unsafe_artifact"]
    manifest = {"schema_version": 1, "artifacts": {"../outside.json": "0" * 64}}
    (tmp_path / "acceptance-manifest.json").write_text(json.dumps(manifest))
    assert acceptance.evaluate(tmp_path)["errors"] == ["invalid_artifact_manifest"]
    manifest["artifacts"] = dict.fromkeys(acceptance.FILES, "0" * 64)
    (tmp_path / "acceptance-manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "campaign.json").write_text("{}")
    assert acceptance.evaluate(tmp_path)["errors"] == ["artifact_digest_mismatch"]
    (tmp_path / "campaign.json").unlink()
    (tmp_path / "campaign.json").symlink_to(tmp_path / "acceptance-manifest.json")
    assert acceptance.evaluate(tmp_path)["errors"] == ["missing_or_unsafe_artifact"]


def test_duration_cannot_be_forged_from_declared_metadata(tmp_path):
    campaign = metadata()
    campaign["phases"]["nominal"]["ended_at"] = at(1801)
    evidence, _ = evidence_for(tmp_path, campaign)
    with pytest.raises(base.InvalidEvidence, match="phase_duration_mismatch"):
        acceptance.phases(evidence)


def test_rows_reject_cross_run_and_candidate_replay(tmp_path):
    evidence, _ = evidence_for(tmp_path)
    row = bind(evidence.campaign, {"run_id": "stale-run"})
    (tmp_path / "scans.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(base.InvalidEvidence, match="record_identity_mismatch"):
        list(evidence.rows("scans.jsonl"))


def test_duplicate_and_wrapped_stale_observation_artifacts_rejected(tmp_path):
    evidence, _ = evidence_for(tmp_path)
    row = {"evidence_id": "proof", "observed_at": at(5), "kind": "test",
           "content": bind(evidence.campaign, {"result": True})}
    write_rows(tmp_path, "observed-artifacts.jsonl", evidence.campaign, [row, row])
    with pytest.raises(base.InvalidEvidence, match="duplicate_observation_artifact"):
        evidence.load_artifacts()
    evidence, _ = evidence_for(tmp_path)
    row["content"]["run_id"] = "old-run"
    write_rows(tmp_path, "observed-artifacts.jsonl", evidence.campaign, [row])
    with pytest.raises(base.InvalidEvidence, match="record_identity_mismatch"):
        evidence.load_artifacts()


def test_unknown_original_scan_publication_is_incomplete_not_client_submission(tmp_path):
    evidence, _ = evidence_for(tmp_path)
    row = {"job_id": "job", "tenant": "pilot-a", "kind": "scan", "published_at": None,
           "accepted_at": at(10), "completed_at": at(11), "outcome": "succeeded",
           "object_count": 100, "attempt_count": 1}
    write_rows(tmp_path, "jobs.jsonl", evidence.campaign, [row])
    with pytest.raises(base.InvalidEvidence, match="invalid_timestamp"):
        acceptance.jobs_and_scans(evidence)


def test_duplicate_scan_attempt_cannot_inflate_denominator(tmp_path):
    evidence, _ = evidence_for(tmp_path)
    job = {"job_id": "job", "tenant": "pilot-a", "kind": "scan", "published_at": at(10),
           "accepted_at": at(10), "completed_at": at(11), "outcome": "succeeded",
           "object_count": 100, "attempt_count": 1}
    row = {"attempt_id": "attempt", "job_id": "job", "published_at": at(10), "completed_at": at(11),
           "success": True, "attempt_number": 1}
    write_rows(tmp_path, "jobs.jsonl", evidence.campaign, [job])
    write_rows(tmp_path, "scans.jsonl", evidence.campaign, [row, row])
    with pytest.raises(base.InvalidEvidence, match="duplicate_scan_attempt"):
        acceptance.jobs_and_scans(evidence)


def test_retry_failure_remains_in_movement_denominators(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    evidence, report = evidence_for(tmp_path, campaign)
    jobs = {row["job_id"]: row for row in original["jobs.jsonl"]}
    first = copy.deepcopy(original["movement.jsonl"][0])
    final = copy.deepcopy(first)
    first.update(success=False, verified=False, terminal_state=None)
    final.update(call_id="retry", attempt_number=2, started_at=at(1802.001), completed_at=at(1802.002))
    write_rows(tmp_path, "movement.jsonl", campaign, [first, final])
    acceptance.movements(evidence, jobs, acceptance.phases(evidence))
    assert report["checks"]["movement.success"] == {"status": "failed", "numerator": 1, "denominator": 2}


def test_stalled_window_is_in_denominator_and_size_mix_is_exact(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    evidence, report = evidence_for(tmp_path, campaign)
    jobs = {row["job_id"]: row for row in original["jobs.jsonl"]}
    rows = copy.deepcopy(original["movement.jsonl"])
    # Collapse all successful first-direction movement into a single five-minute
    # window. The eleven empty windows still count and fail the throughput gate.
    for row in rows:
        if row["movement_window_id"] == "window-0":
            row["started_at"], row["completed_at"] = at(275001), at(275002)
    rows[10000]["size_bytes"] = 65536
    write_rows(tmp_path, "movement.jsonl", campaign, rows)
    acceptance.movements(evidence, jobs, acceptance.phases(evidence))
    result = report["checks"]["movement.hot-to-warm.0.throughput"]
    assert result == {"status": "failed", "numerator": 1, "denominator": 12}
    assert report["checks"]["movement.hot-to-warm.0.size_mix"]["status"] == "failed"


def test_missing_and_duplicate_logical_attempts_are_rejected(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    jobs = {row["job_id"]: row for row in original["jobs.jsonl"]}
    for change, message in (({"attempt_number": 2}, "missing_or_duplicate_movement_attempt"),
                            ({"terminal_state": None}, "unresolved_logical_move"),
                            ({"success": True, "verified": False}, "unverified_movement_success")):
        evidence, _ = evidence_for(tmp_path, campaign)
        row = {**original["movement.jsonl"][0], **change}
        write_rows(tmp_path, "movement.jsonl", campaign, [row])
        with pytest.raises(base.InvalidEvidence, match=message):
            acceptance.movements(evidence, jobs, acceptance.phases(evidence))


def test_missing_phase_and_tenant_reconciliation_fails(tmp_path):
    evidence, report = evidence_for(tmp_path)
    write_rows(tmp_path, "reconciliations.jsonl", evidence.campaign, [])
    acceptance.reconciliations(evidence, acceptance.phases(evidence), {})
    assert report["checks"]["integrity.coverage"]["status"] == "failed"
    assert report["checks"]["integrity.zero_discrepancies"]["status"] == "incomplete"


def test_resource_growth_over_110_percent_fails(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    evidence, report = evidence_for(tmp_path, campaign)
    evidence.artifacts = {row["evidence_id"]: row for row in original["observed-artifacts.jsonl"]}
    rows = copy.deepcopy(original["resource-checkpoints.jsonl"])
    rows[1]["values"]["api_rss_bytes"] = 111
    evidence.artifacts = copy.deepcopy(evidence.artifacts)
    evidence.artifacts[rows[1]["source_evidence_id"]]["content"]["observation"]["values"]["api_rss_bytes"] = 111
    write_rows(tmp_path, "resource-checkpoints.jsonl", campaign, rows)
    directory = complete_bundle[0]
    (tmp_path / "capacity-cost.json").write_bytes((directory / "capacity-cost.json").read_bytes())
    acceptance.resources_and_cost(evidence, acceptance.phases(evidence))
    assert report["checks"]["resources.nominal.api_rss_bytes.drained"]["status"] == "failed"
    assert report["checks"]["resources.nominal.worker_rss_bytes.drained"]["status"] == "passed"


def test_nonfinite_negative_and_boolean_counts_rejected():
    for value in (-1, True, float("nan"), float("inf")):
        with pytest.raises(base.InvalidEvidence):
            base.number(value, integer=True)


def test_normalized_clean_report_cannot_disagree_with_raw_artifact(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    evidence, _ = evidence_for(tmp_path, campaign)
    evidence.artifacts = copy.deepcopy({row["evidence_id"]: row for row in original["observed-artifacts.jsonl"]})
    row = copy.deepcopy(original["reconciliations.jsonl"][0])
    evidence.artifacts[row["source_evidence_id"]]["content"]["observation"]["checks"]["checksum_mismatches"] = 1
    write_rows(tmp_path, "reconciliations.jsonl", campaign, [row])
    with pytest.raises(base.InvalidEvidence, match="observation_artifact_content_mismatch"):
        acceptance.reconciliations(evidence, acceptance.phases(evidence), {})


def test_burst_recovery_over_ten_minutes_fails(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    evidence, report = evidence_for(tmp_path, campaign)
    rows = copy.deepcopy(original["faults.jsonl"])
    burst = next(row for row in rows if row["kind"] == "burst")
    burst["recovered_at"] = at(263501)
    evidence.artifacts = copy.deepcopy({row["evidence_id"]: row for row in original["observed-artifacts.jsonl"]})
    evidence.artifacts[burst["source_evidence_id"]]["content"]["observation"] = {
        key: value for key, value in burst.items() if key != "source_evidence_id"}
    write_rows(tmp_path, "faults.jsonl", campaign, rows)
    acceptance.fault_observations(evidence, acceptance.phases(evidence))
    assert report["checks"]["fault.4.observations"]["status"] == "failed"


def test_missing_acknowledgement_artifact_is_incomplete(tmp_path, complete_bundle):
    _, campaign, original = complete_bundle
    evidence, _ = evidence_for(tmp_path, campaign)
    write_rows(tmp_path, "observed-artifacts.jsonl", campaign, original["observed-artifacts.jsonl"])
    evidence.load_artifacts()
    rows = original["alerts.jsonl"]
    evidence.artifacts.pop(rows[0]["acknowledgement_id"])
    write_rows(tmp_path, "alerts.jsonl", campaign, rows)
    with pytest.raises(base.InvalidEvidence, match="alert_artifact_mismatch"):
        acceptance.alerts(evidence)


def test_actual_stop_before_scheduled_boundary_is_incomplete(tmp_path):
    campaign = metadata()
    campaign["phases"]["nominal"]["actual_ended_at"] = at(2000)
    evidence, _ = evidence_for(tmp_path, campaign)
    with pytest.raises(base.InvalidEvidence, match="phase_not_completed"):
        acceptance.phases(evidence)


def test_missing_retry_cannot_improve_scan_denominator(tmp_path):
    evidence, _ = evidence_for(tmp_path)
    job = {"job_id": "job", "tenant": "pilot-a", "kind": "scan", "published_at": at(10),
           "accepted_at": at(10), "completed_at": at(20), "outcome": "succeeded",
           "object_count": 100, "attempt_count": 2}
    row = {"attempt_id": "attempt", "attempt_number": 2, "job_id": "job", "published_at": at(10),
           "completed_at": at(20), "success": True}
    write_rows(tmp_path, "jobs.jsonl", evidence.campaign, [job])
    write_rows(tmp_path, "scans.jsonl", evidence.campaign, [row])
    with pytest.raises(base.InvalidEvidence, match="missing_or_duplicate_scan_attempt"):
        acceptance.jobs_and_scans(evidence)


def test_background_scans_cannot_use_unbounded_prefixes(tmp_path):
    evidence, _ = evidence_for(tmp_path)
    job = {"job_id": "job", "tenant": "pilot-a", "kind": "scan", "published_at": at(10),
           "accepted_at": at(10), "completed_at": at(20), "outcome": "succeeded",
           "object_count": 101, "attempt_count": 1}
    write_rows(tmp_path, "jobs.jsonl", evidence.campaign, [job])
    with pytest.raises(base.InvalidEvidence, match="background_prefix_exceeds_limit"):
        acceptance.jobs_and_scans(evidence)


@pytest.mark.parametrize("field,value,check,status", [
    ("execution_scope", "local-rehearsal", "staging_scope", "incomplete"),
    ("status", "failed", "completed", "failed"),
    ("status", "interrupted", "completed", "failed"),
    ("stop_latched", True, "no_stop_latch", "failed"),
    ("uncertain_mutations", 1, "no_uncertain_mutations", "failed"),
])
def test_rehearsal_interruption_and_adapter_uncertainty_cannot_qualify(tmp_path, field, value, check, status):
    campaign = metadata()
    campaign[field] = value
    evidence, report = evidence_for(tmp_path, campaign)
    acceptance.campaign_state(evidence)
    assert report["checks"][f"campaign.{check}"]["status"] == status


def backlog_evidence(tmp_path, complete_bundle, transform):
    _, campaign, original = complete_bundle
    evidence, report = evidence_for(tmp_path, campaign)
    rows = transform(copy.deepcopy(original["movement-backlog.jsonl"]))
    evidence.artifacts = copy.deepcopy({row["evidence_id"]: row for row in original["observed-artifacts.jsonl"]})
    for row in rows:
        artifact = evidence.artifacts[row["source_evidence_id"]]
        artifact["observed_at"] = row["observed_at"]
        artifact["content"]["observation"] = {key: value for key, value in row.items() if key != "source_evidence_id"}
    write_rows(tmp_path, "movement-backlog.jsonl", campaign, rows)
    return evidence, report


def test_paced_batches_with_no_actual_pending_work_fail_backlog(tmp_path, complete_bundle):
    def paced(rows):
        for index, row in enumerate(rows):
            row["pending_moves"] = 100 if index % 7 == 0 else 0
            row["in_flight_moves"] = 2
        return rows
    evidence, report = backlog_evidence(tmp_path, complete_bundle, paced)
    acceptance.movement_backlog(evidence)
    assert report["checks"]["movement.backlog.0.coverage"]["status"] == "passed"
    assert report["checks"]["movement.backlog.0.nonempty"]["status"] == "failed"


def test_sparse_positive_samples_cannot_prove_hour_of_backlog(tmp_path, complete_bundle):
    evidence, report = backlog_evidence(tmp_path, complete_bundle, lambda rows: rows[::8])
    acceptance.movement_backlog(evidence)
    assert report["checks"]["movement.backlog.0.coverage"]["status"] == "failed"
    assert report["checks"]["movement.backlog.0.gap"]["status"] == "failed"
    assert report["checks"]["movement.backlog.0.nonempty"]["status"] == "passed"


def test_backlog_unknown_and_duplicate_source_samples_are_rejected(tmp_path, complete_bundle):
    def unknown(rows):
        rows[0]["pending_moves"] = None
        return rows
    evidence, _ = backlog_evidence(tmp_path, complete_bundle, unknown)
    with pytest.raises(base.InvalidEvidence, match="invalid_number"):
        acceptance.movement_backlog(evidence)

    def duplicate(rows):
        rows[1]["observed_at"] = rows[0]["observed_at"]
        return rows
    evidence, _ = backlog_evidence(tmp_path, complete_bundle, duplicate)
    with pytest.raises(base.InvalidEvidence, match="duplicate_unordered_backlog_sample"):
        acceptance.movement_backlog(evidence)


def test_backlog_source_timestamp_cannot_be_relabelled(tmp_path, complete_bundle):
    evidence, _ = backlog_evidence(tmp_path, complete_bundle, lambda rows: rows)
    evidence.artifacts["backlog-0-0"]["observed_at"] = at(274999)
    with pytest.raises(base.InvalidEvidence, match="backlog_artifact_time_mismatch"):
        acceptance.movement_backlog(evidence)


def test_missing_direction_backlog_cannot_borrow_other_window_coverage(tmp_path, complete_bundle):
    evidence, report = backlog_evidence(tmp_path, complete_bundle,
                                       lambda rows: [row for row in rows if row["window_id"] == "window-0"])
    acceptance.movement_backlog(evidence)
    assert report["checks"]["movement.backlog.0.coverage"]["status"] == "passed"
    assert report["checks"]["movement.backlog.1.coverage"]["status"] == "failed"
    assert report["checks"]["movement.backlog.1.nonempty"]["status"] == "incomplete"
