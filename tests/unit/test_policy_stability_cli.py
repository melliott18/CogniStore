"""CLI stability previews, validation, and durable submission contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.models import JOB_SCHEMA_VERSION_V3, EnqueueReceipt, JobEnvelope


def _drivers_config(tmp_path: Path) -> Path:
    path = tmp_path / "drivers.yaml"
    path.write_text(
        f"tiers:\n  hot:\n    driver: posix\n    path: {tmp_path / 'hot'}\n"
        f"  warm:\n    driver: posix\n    path: {tmp_path / 'warm'}\n",
        encoding="utf-8",
    )
    return path


def test_cli_stability_dry_run_exposes_guards_and_override_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    drivers_path = _drivers_config(tmp_path)
    hot = PosixDriver(tmp_path / "hot")
    for key in ("initial", "moved"):
        hot.put_object("bucket", key, b"x" * 101)
    database = tmp_path / "catalog.sqlite"
    with SQLiteCatalog(database) as catalog:
        catalog.upsert("bucket", "initial", 101, "hot")
        catalog.upsert("bucket", "moved", 101, "warm")
        catalog.upsert("bucket", "moved", 101, "hot")
        initial = catalog.get("bucket", "initial")
        moved = catalog.get("bucket", "moved")
    before_bytes = database.read_bytes()
    command = [
        "--no-config", "--drivers", str(drivers_path), "--catalog-db", str(database),
        "policy-run", "bucket", "--dry-run", "--json", "--threshold", "100",
        "--cooldown-seconds", "3600", "--size-hysteresis-bytes", "10",
        "--similarity-hysteresis", "0.05",
    ]

    assert cognistore_cli.main(command) == 0
    output = capsys.readouterr()
    assert output.err == ""
    preview = json.loads(output.out)
    assert preview["dry_run"] is True
    evaluations = {item["key"]: item for item in preview["evaluations"]}
    for evaluation in evaluations.values():
        assert evaluation["action"] == "stay"
        controls = evaluation["constraints"]
        assert controls["cooldown_seconds"] == 3600
        assert controls["size_hysteresis_bytes"] == 10
        assert controls["similarity_hysteresis"] == 0.05
        assert controls["stability_override"] is None
    initial_controls = evaluations["initial"]["constraints"]
    assert initial_controls["last_tier_move_at"] is None
    assert initial_controls["cooldown_active"] is False
    assert initial_controls["suppression_reason"] == "hysteresis"
    size_check = initial_controls["hysteresis"]["checks"][0]
    assert size_check["value"] == 101
    assert size_check["baseline_threshold"] == 100
    assert size_check["effective_threshold"] == 110
    assert initial_controls["hysteresis"]["candidate_destination_tier"] == "warm"
    moved_controls = evaluations["moved"]["constraints"]
    assert moved_controls["last_tier_move_at"] == moved.last_tier_move_at
    assert moved_controls["cooldown_active"] is True
    assert moved_controls["cooldown_expires_at"] is not None
    assert moved_controls["suppression_reason"] == "cooldown"

    assert cognistore_cli.main([
        *command, "--stability-override", "emergency",
        "--stability-override-reason", "Approved incident response 45",
    ]) == 0
    overridden = json.loads(capsys.readouterr().out)
    for evaluation in overridden["evaluations"]:
        assert evaluation["action"] == "move"
        assert evaluation["destination_tier"] == "warm"
        assert evaluation["constraints"]["stability_override"] == {
            "kind": "emergency", "reason": "Approved incident response 45",
        }
    assert database.read_bytes() == before_bytes
    with SQLiteCatalog(database, read_only=True) as catalog:
        assert catalog.get("bucket", "initial") == initial
        assert catalog.get("bucket", "moved") == moved
        assert catalog.list_audit_events() == []
        assert catalog.list_move_jobs() == []
    assert hot.get_object("bucket", "initial") == b"x" * 101
    assert hot.get_object("bucket", "moved") == b"x" * 101
    assert list(PosixDriver(tmp_path / "warm").list_objects("bucket")) == []


@pytest.mark.parametrize("flags, message", [
    (["--cooldown-seconds", "-1"], "cooldown_seconds"),
    (["--cooldown-seconds", "315360001"], "cooldown_seconds"),
    (["--size-hysteresis-bytes", "-1"], "size_hysteresis_bytes"),
    (["--size-hysteresis-bytes", str(2**63)], "size_hysteresis_bytes"),
    (["--similarity-hysteresis", "-0.1"], "similarity_hysteresis"),
    (["--similarity-hysteresis", "nan"], "similarity_hysteresis"),
    (["--similarity-hysteresis", "inf"], "similarity_hysteresis"),
    (["--similarity-hysteresis", "2.1"], "similarity_hysteresis"),
    (["--stability-override", "emergency"], "require each other"),
    (["--stability-override-reason", "request 45"], "require each other"),
    (["--stability-override-reason", ""], "require each other"),
    (["--stability-override", "compliance", "--stability-override-reason", " "],
     "without outer whitespace"),
])
def test_cli_rejects_invalid_stability_before_importance_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str], message: str,
) -> None:
    drivers_path = _drivers_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    with SQLiteCatalog(database) as catalog:
        catalog.upsert("bucket", "key", 101, "hot")
        before = catalog.get("bucket", "key")
    before_bytes = database.read_bytes()

    with pytest.raises(SystemExit) as error:
        cognistore_cli.main([
            "--no-config", "--drivers", str(drivers_path), "--catalog-db", str(database),
            "importance-set", "bucket", "key", "critical",
            "--actor", "operator", "--provenance", "request 45", "--json", *flags,
        ])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.err == ""
    failure = json.loads(output.out)
    assert failure["error_type"] == "UsageError"
    assert message in failure["error"]
    assert database.read_bytes() == before_bytes
    with SQLiteCatalog(database, read_only=True) as catalog:
        assert catalog.get("bucket", "key") == before
        assert catalog.list_audit_events() == []


def test_cli_background_policy_retains_stability_controls_in_v3_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    drivers_path = _drivers_config(tmp_path)
    published: list[JobEnvelope] = []

    async def enqueue(_config, job: JobEnvelope) -> EnqueueReceipt:
        published.append(JobEnvelope.from_bytes(job.to_bytes()))
        return EnqueueReceipt(
            job_id=job.job_id, correlation_id=job.correlation_id,
            stream="TEST_JOBS", sequence=1,
        )

    def forbid_catalog(*args, **kwargs):
        pytest.fail("background policy submission must not open a catalog")

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)
    monkeypatch.setattr(cognistore_cli, "open_catalog", forbid_catalog)
    monkeypatch.setattr(cognistore_cli, "Catalog", forbid_catalog)
    assert cognistore_cli.main([
        "--no-config", "--drivers", str(drivers_path), "policy-run", "bucket", "--json",
        "--threshold", "100", "--prefix", "reports/",
        "--cooldown-seconds", "3600", "--size-hysteresis-bytes", "10",
        "--similarity-hysteresis", "0.05", "--stability-override", "compliance",
        "--stability-override-reason", "Approved retention request 45",
    ]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["status"] == "queued"
    assert len(published) == 1
    envelope = published[0]
    assert envelope.job_type == "policy.run"
    assert envelope.schema_version == JOB_SCHEMA_VERSION_V3
    assert envelope.payload["bucket"] == "bucket"
    assert envelope.payload["prefix"] == "reports/"
    assert envelope.payload["movement_constraints"] == {
        "minimum_residency_seconds": {},
        "importance_tiers": {"high": ["hot", "warm"], "critical": ["hot"]},
        "cooldown_seconds": 3600,
        "size_hysteresis_bytes": 10,
        "similarity_hysteresis": 0.05,
        "stability_override": {"kind": "compliance", "reason": "Approved retention request 45"},
    }
