from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.policy import MAX_EMBEDDING_RULES, MAX_POLICY_CONFIG_BYTES
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.models import (
    JOB_SCHEMA_VERSION_V1,
    JOB_SCHEMA_VERSION_V2,
    EnqueueReceipt,
    QueueSaturatedError,
)

BUCKET = "bk"
KEY = "small.txt"
DATA = b"12345"


def _configured_policy_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Catalog, PosixDriver, PosixDriver]:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    warm.put_object(BUCKET, KEY, DATA)

    catalog = Catalog()
    catalog.upsert(BUCKET, KEY, len(DATA), tier="warm")

    monkeypatch.setattr(cognistore_cli, "Catalog", lambda **_kwargs: catalog)
    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": hot, "warm": warm},
    )
    return catalog, hot, warm


def _forbid_write(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("dry-run attempted a write")


@pytest.mark.parametrize(
    ("dry_run", "json_output", "expected_status"),
    [
        (True, False, "planned"),
        (False, False, "completed"),
        (True, True, "planned"),
        (False, True, "completed"),
    ],
)
def test_policy_output_distinguishes_planned_from_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    json_output: bool,
    expected_status: str,
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)

    if dry_run:
        monkeypatch.setattr(hot, "put_object", _forbid_write)
        monkeypatch.setattr(warm, "delete_object", _forbid_write)
        monkeypatch.setattr(catalog, "upsert", _forbid_write)
        monkeypatch.setattr(catalog, "update_placement", _forbid_write)
        monkeypatch.setattr(catalog, "delete", _forbid_write)

    argv = [
        "--drivers",
        "ignored.yaml",
        "policy-run",
        BUCKET,
        "--threshold",
        str(len(DATA)),
        "--sync",
    ]
    if dry_run:
        argv.append("--dry-run")
    if json_output:
        argv.append("--json")

    assert cognistore_cli.main(argv) == 0
    captured = capsys.readouterr()

    if json_output:
        payload = json.loads(captured.out)
        feature_projection = {
            "schema_version": 1,
            "mime": {
                "state": "missing",
                "value": None,
                "provenance": {
                    "source": "catalog_metadata",
                    "source_version": 1,
                    "content_sha256": None,
                    "details": {"reason": "mime_missing"},
                },
            },
            "embeddings": [],
        }
        action = {
            "bucket": BUCKET,
            "from_tier": "warm",
            "key": KEY,
            "reason": "small object -> hot tier",
            "status": expected_status,
            "to_tier": "hot",
        }
        expected = {
            "schema": "cognistore.cli",
            "schema_version": 1,
            "command": "policy-run",
            "status": "planned" if dry_run else "completed",
            "actions": [action],
            "count": 1,
            "dry_run": dry_run,
        }
        if dry_run:
            access = payload["evaluations"][0]["features"]["access"]
            assert access["freshness"] == "missing"
            assert access["missing"] is True
            assert access["partial"] is True
            assert access["observed_events"] == 0
            assert access["recency_seconds"] is None
            feature_projection["access"] = access
            action["features"] = feature_projection
            expected["evaluations"] = [
                {
                    "bucket": BUCKET,
                    "key": KEY,
                    "current_tier": "warm",
                    "size": len(DATA),
                    "action": "move",
                    "destination_tier": "hot",
                    "reason": "small object -> hot tier",
                    "features": feature_projection,
                }
            ]
        assert payload == expected
    else:
        verb = "planned" if dry_run else "moved"
        summary = "planned_actions" if dry_run else "completed_actions"
        lines = captured.out.splitlines()
        if dry_run:
            assert lines[0] == (
                f"{verb} {BUCKET}/{KEY} warm->hot : small object -> hot tier"
            )
            assert lines[1].startswith(
                f"evaluated {BUCKET}/{KEY} warm action=move destination=hot"
            )
            assert '"schema_version":1' in lines[1]
            assert lines[2] == f"{summary}=1"
        else:
            assert lines == [
                f"{verb} {BUCKET}/{KEY} warm->hot : small object -> hot tier",
                f"{summary}=1",
            ]

    record = catalog.get(BUCKET, KEY)
    assert record is not None
    if dry_run:
        assert record.tier == "warm"
        assert warm.get_object(BUCKET, KEY) == DATA
        with pytest.raises(FileNotFoundError):
            hot.get_object(BUCKET, KEY)
        assert catalog.list_audit_events() == []
    else:
        assert record.tier == "hot"
        assert hot.get_object(BUCKET, KEY) == DATA
        with pytest.raises(FileNotFoundError):
            warm.get_object(BUCKET, KEY)
        events = catalog.list_audit_events()
        assert events[0].event_type == AuditEventType.MANUAL_ACTION.value
        assert any(event.event_type == AuditEventType.POLICY_DECISION.value for event in events)
        assert events[-1].event_type == AuditEventType.MOVE_COMPLETED.value


def test_direct_move_records_manual_and_terminal_move_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "move",
                "warm",
                "hot",
                BUCKET,
                KEY,
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    move_id = payload["idempotency_key"]
    assert payload["idempotency_key"] == move_id
    events = catalog.list_audit_events(AuditQuery(correlation_id=move_id))
    assert events[0].event_type == AuditEventType.MANUAL_ACTION.value
    assert events[0].actor_type == "operator"
    move_events = [event for event in events if event.move_id == move_id][1:]
    assert move_events[0].causation_id == events[0].event_id
    assert move_events[-1].event_type == AuditEventType.MOVE_COMPLETED.value
    assert hot.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        warm.get_object(BUCKET, KEY)


def test_direct_move_dry_run_is_planned_and_does_not_create_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)
    catalog_path = tmp_path / "missing.sqlite"

    monkeypatch.setattr(hot, "put_object", _forbid_write)
    monkeypatch.setattr(warm, "delete_object", _forbid_write)
    monkeypatch.setattr(catalog, "upsert", _forbid_write)
    monkeypatch.setattr(catalog, "update_placement", _forbid_write)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "--catalog-db",
                str(catalog_path),
                "move",
                "warm",
                "hot",
                BUCKET,
                KEY,
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["count"] == 1
    assert payload["actions"][0]["status"] == "planned"
    assert warm.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        hot.get_object(BUCKET, KEY)
    assert catalog.get(BUCKET, KEY).tier == "warm"  # type: ignore[union-attr]
    assert not catalog_path.exists()


def test_policy_dry_run_rejects_missing_sqlite_catalog_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = tmp_path / "missing.sqlite"
    config_path = tmp_path / "drivers.yaml"
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    _write_driver_config(config_path, hot, warm)

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                str(config_path),
                "--catalog-db",
                str(catalog_path),
                "policy-run",
                BUCKET,
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 2
    assert "must already exist" in capsys.readouterr().err
    assert not catalog_path.exists()
    assert not hot.exists()
    assert not warm.exists()


def _write_driver_config(config_path: Path, hot: Path, warm: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "tiers:",
                "  hot:",
                "    driver: posix",
                f"    path: {hot}",
                "  warm:",
                "    driver: posix",
                f"    path: {warm}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_policy_dry_run_does_not_create_missing_posix_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    config_path = tmp_path / "drivers.yaml"
    _write_driver_config(config_path, hot, warm)

    assert (
        cognistore_cli.main(
            ["--drivers", str(config_path), "policy-run", BUCKET, "--dry-run"]
        )
        == 0
    )

    assert capsys.readouterr().out == "planned_actions=0\n"
    assert not hot.exists()
    assert not warm.exists()


def test_auto_discover_dry_run_does_not_refresh_cache_or_profile_tiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    cache = tmp_path / "cache"
    config_path = tmp_path / "drivers.yaml"
    _write_driver_config(config_path, hot, warm)

    monkeypatch.setattr(cognistore_cli, "profile_path", _forbid_write)
    monkeypatch.setattr(cognistore_cli, "discover_device_for_tier", _forbid_write)
    monkeypatch.setattr(cognistore_cli, "save_metrics_json", _forbid_write)
    monkeypatch.setattr(cognistore_cli, "save_hardware_json", _forbid_write)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                str(config_path),
                "policy-run",
                BUCKET,
                "--dry-run",
                "--auto-discover",
                "--cache-dir",
                str(cache),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "planned_actions=0\n"
    assert "skipped auto-discovery refresh" in captured.err
    assert not cache.exists()
    assert not hot.exists()
    assert not warm.exists()


@pytest.mark.parametrize("policy_mode", ["simple", "llm", "content"])
def test_each_policy_mode_respects_allowed_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    policy_mode: str,
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--policy",
                policy_mode,
                "--threshold",
                str(len(DATA)),
                "--allowed-tiers",
                "warm",
                "--dry-run",
            ]
        )
        == 0
    )

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(
        f"evaluated {BUCKET}/{KEY} warm action=stay destination=-"
    )
    assert '"schema_version":1' in lines[0]
    assert lines[1] == "planned_actions=0"
    assert warm.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        hot.get_object(BUCKET, KEY)
    assert catalog.get(BUCKET, KEY).tier == "warm"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("allowed_tiers", "message"),
    [
        (",,", "must contain at least one tier"),
        ("hot,archive", "unknown allowed tier(s): archive"),
    ],
)
def test_policy_run_rejects_invalid_or_unknown_allowed_tiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    allowed_tiers: str,
    message: str,
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--allowed-tiers",
                allowed_tiers,
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            [
                "--policy",
                "content",
                "--embedding-rule",
                "invoice",
                "invoice accounts payable",
                "2.0",
                "hot",
            ],
            "minimum_similarity must be between -1 and 1",
        ),
        (
            [
                "--policy",
                "content",
                "--embedding-rule",
                "invoice",
                "first query",
                "0.8",
                "hot",
                "--embedding-rule",
                "invoice",
                "second query",
                "0.7",
                "warm",
            ],
            "--embedding-rule names must be unique",
        ),
        (
            [
                "--embedding-rule",
                "invoice",
                "invoice accounts payable",
                "0.8",
                "hot",
            ],
            "--embedding-rule requires --policy content",
        ),
        (
            [
                "--policy",
                "content",
                "--allowed-tiers",
                "warm",
                "--embedding-rule",
                "invoice",
                "invoice accounts payable",
                "0.8",
                "hot",
            ],
            "--embedding-rule contains disallowed destination tier(s): hot",
        ),
    ],
)
def test_policy_run_rejects_invalid_embedding_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    message: str,
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                *options,
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


def test_policy_run_rejects_more_than_the_rule_limit_before_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)
    options = ["--policy", "content"]
    for index in range(MAX_EMBEDDING_RULES + 1):
        options.extend(
            [
                "--embedding-rule",
                f"rule-{index}",
                f"query-{index}",
                "0.8",
                "hot",
            ]
        )

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                *options,
            ]
        )

    assert exc_info.value.code == 2
    assert "--embedding-rule may be repeated at most 100 times" in (
        capsys.readouterr().err
    )


def test_policy_run_rejects_oversized_aggregate_policy_strings_before_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)
    options = ["--policy", "content"]
    query = "q" * (MAX_POLICY_CONFIG_BYTES // 5 + 1)
    for index in range(5):
        options.extend(
            [
                "--embedding-rule",
                f"rule-{index}",
                query,
                "0.8",
                "hot",
            ]
        )

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                *options,
            ]
        )

    assert exc_info.value.code == 2
    assert "policy strings must total at most 65536 bytes" in (
        capsys.readouterr().err
    )


def test_policy_run_parses_embedding_rule_into_queued_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)
    captured_jobs = []

    async def enqueue(_config, job):
        captured_jobs.append(job)
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=17,
        )

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--policy",
                "content",
                "--embedding-rule",
                "invoice",
                "invoice accounts payable",
                "0.8",
                "hot",
                "--json",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert captured_jobs[0].schema_version == JOB_SCHEMA_VERSION_V2
    assert captured_jobs[0].payload["embedding_rules"] == [
        {
            "name": "invoice",
            "query": "invoice accounts payable",
            "minimum_similarity": 0.8,
            "destination_tier": "hot",
        }
    ]


def test_embedding_rule_dry_run_reports_every_evaluation_and_stays_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)
    second_key = "z-second.txt"
    second_data = b"another object"
    warm.put_object(BUCKET, second_key, second_data)
    catalog.upsert(BUCKET, second_key, len(second_data), tier="warm")

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--policy",
                "content",
                "--threshold",
                str(max(len(DATA), len(second_data))),
                "--embedding-rule",
                "invoice",
                "invoice accounts payable",
                "0.8",
                "hot",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "planned"
    assert output["count"] == 0
    assert output["actions"] == []
    assert [evaluation["key"] for evaluation in output["evaluations"]] == [
        KEY,
        second_key,
    ]
    for evaluation in output["evaluations"]:
        assert evaluation["action"] == "stay"
        assert evaluation["destination_tier"] is None
        assert evaluation["reason"] == (
            "required policy features unavailable: embedding:invoice=missing"
        )
        features = evaluation["features"]
        assert features["schema_version"] == 1
        assert features["mime"]["state"] == "missing"
        assert features["mime"]["provenance"] == {
            "source": "catalog_metadata",
            "source_version": 1,
            "content_sha256": None,
            "details": {"reason": "mime_missing"},
        }
        assert features["embeddings"] == [
            {
                "name": "invoice",
                "query": "invoice accounts payable",
                "state": "missing",
                "similarity": None,
                "provenance": {
                    "source": "embedding_similarity",
                    "source_version": 1,
                    "content_sha256": None,
                    "details": {"reason": "provider_missing"},
                },
            }
        ]

    assert [record.tier for record in catalog.list(BUCKET)] == ["warm", "warm"]
    assert sorted(warm.list_objects(BUCKET)) == [
        KEY,
        second_key,
    ]
    assert list(hot.list_objects(BUCKET)) == []
    assert catalog.list_audit_events() == []


def test_embedding_rule_human_dry_run_includes_full_feature_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--policy",
                "content",
                "--embedding-rule",
                "invoice",
                "invoice accounts payable",
                "0.8",
                "hot",
                "--dry-run",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"evaluated {BUCKET}/{KEY} warm action=stay destination=-" in output
    assert "embedding:invoice=missing" in output
    assert '"schema_version":1' in output
    assert '"state":"missing"' in output
    assert '"source":"embedding_similarity"' in output
    assert '"content_sha256":null' in output
    assert "planned_actions=0" in output


def test_policy_run_enqueues_by_default_without_moving_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)
    captured_jobs = []

    async def enqueue(_config, job):
        captured_jobs.append(job)
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=12,
        )

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--threshold",
                str(len(DATA)),
                "--correlation-id",
                "request-18",
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "queued"
    assert output["job_type"] == "policy.run"
    assert output["correlation_id"] == "request-18"
    assert output["sequence"] == 12
    assert len(captured_jobs) == 1
    assert captured_jobs[0].schema_version == JOB_SCHEMA_VERSION_V1
    assert captured_jobs[0].payload["bucket"] == BUCKET
    assert captured_jobs[0].payload["allowed_tiers"] == ["hot", "warm"]
    assert "embedding_rules" not in captured_jobs[0].payload
    assert catalog.get(BUCKET, KEY).tier == "warm"  # type: ignore[union-attr]
    assert warm.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        hot.get_object(BUCKET, KEY)


def test_catalog_scan_enqueues_job_and_correlation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": hot})
    captured_jobs = []

    async def enqueue(_config, job):
        captured_jobs.append(job)
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=4,
        )

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "catalog-scan",
                "hot",
                BUCKET,
                "--prefix",
                "reports/",
                "--correlation-id",
                "scan-request",
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["job_type"] == "catalog.scan"
    assert output["correlation_id"] == "scan-request"
    assert captured_jobs[0].payload == {
        "tier": "hot",
        "bucket": BUCKET,
        "prefix": "reports/",
    }


def test_catalog_scan_reports_queue_saturation_as_retryable_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": hot})

    async def saturated(config, _job):
        raise QueueSaturatedError(config.stream, "maximum messages exceeded")

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", saturated)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "catalog-scan",
                "hot",
                BUCKET,
                "--json",
            ]
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert output["operation"] == "enqueue"
    assert output["error_type"] == "QueueSaturatedError"
    assert output["retryable"] is True


@pytest.mark.parametrize("option", ["--stream-max-messages", "--stream-max-bytes"])
def test_queue_capacity_flags_must_be_positive(
    option: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                option,
                "0",
                "--drivers",
                "unused.yaml",
                "catalog-scan",
                "hot",
                BUCKET,
            ]
        )

    assert exc_info.value.code == 2
    assert "must be a positive integer" in capsys.readouterr().err
