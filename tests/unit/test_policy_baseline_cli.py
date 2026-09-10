from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli


def _forbid(*args: object, **kwargs: object) -> None:
    raise AssertionError("baseline commands must not open catalogs, drivers, or services")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if variable.startswith("COGNISTORE_"):
            monkeypatch.delenv(variable)
    for name in (
        "open_catalog", "load_drivers", "load_policy_feature_loader",
        "NatsJetStreamQueue", "PosixDriver", "Catalog",
    ):
        monkeypatch.setattr(cognistore_cli, name, _forbid)


@pytest.fixture(params=["train", "evaluate"])
def baseline_command(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], dict[str, object]]:
    dataset = tmp_path / "dataset.json"
    artifact_input = tmp_path / ("config.json" if request.param == "train" else "model.json")
    dataset.write_text('{"dataset": "fixture"}', encoding="utf-8")
    artifact_input.write_text('{"input": "fixture"}', encoding="utf-8")
    artifact: dict[str, object] = {"model_version": "baseline-sha256:fixture"}
    if request.param == "evaluate":
        artifact.update({
            "report_version": "report-sha256:fixture",
            "metrics": {"accuracy": 0.75},
            "checks": [{"name": "accuracy", "passed": False}],
            "promotion": {"eligible": False},
        })

    def compute(data: object, configuration_or_model: object, *, code_version: str) -> dict:
        assert data == {"dataset": "fixture"}
        assert configuration_or_model == {"input": "fixture"}
        assert code_version == "test-source-version"
        return artifact

    monkeypatch.setattr(cognistore_cli, "code_version", lambda: "test-source-version")
    monkeypatch.setattr(cognistore_cli, f"{request.param}_baseline", compute)
    return [
        "--no-config", f"policy-baseline-{request.param}",
        "--input", str(dataset),
        "--training-config" if request.param == "train" else "--model", str(artifact_input),
        "--output", str(tmp_path / "output.json"), "--json",
    ], artifact


def _report(capsys: pytest.CaptureFixture[str]) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _output(arguments: list[str]) -> Path:
    return Path(arguments[arguments.index("--output") + 1])


def test_commands_are_standalone_and_publish_reproducible_artifacts(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, artifact = baseline_command
    database = tmp_path / "unused.sqlite"
    arguments[1:1] = ["--catalog-db", str(database), "--drivers", "missing-drivers.yaml"]
    assert cognistore_cli.main(arguments) == 0
    report = _report(capsys)
    assert report["status"] == "success"
    version_field = "model_version" if "--training-config" in arguments else "report_version"
    assert report["artifact_version"] == artifact[version_field]
    assert report["artifact"] == artifact
    assert report["output"] == str(_output(arguments))
    for field in ("metrics", "checks", "promotion"):
        if field in artifact:
            assert report[field] == artifact[field]
    first = _output(arguments).read_bytes()
    assert json.loads(first) == artifact
    assert cognistore_cli.main(arguments) == 0
    _report(capsys)
    assert _output(arguments).read_bytes() == first
    assert not database.exists()


def test_dry_run_computes_artifact_without_creating_output_or_directories(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, artifact = baseline_command
    output = tmp_path / "absent" / "artifact.json"
    arguments[arguments.index("--output") + 1] = str(output)
    assert cognistore_cli.main(arguments + ["--dry-run"]) == 0
    report = _report(capsys)
    assert report["status"] == "planned"
    assert report["dry_run"] is True
    assert report["artifact"] == artifact
    assert not output.parent.exists()


@pytest.mark.parametrize("input_index", ["--input", "auxiliary"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_commands_protect_inputs_against_output_aliasing(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str], input_index: str, alias: str,
) -> None:
    arguments, _ = baseline_command
    option = input_index
    if option == "auxiliary":
        option = "--training-config" if "--training-config" in arguments else "--model"
    protected = Path(arguments[arguments.index(option) + 1])
    original = protected.read_bytes()
    if alias == "direct":
        output = protected
    else:
        output = tmp_path / "alias.json"
        if alias == "symlink":
            output.symlink_to(protected)
        else:
            output.hardlink_to(protected)
    arguments[arguments.index("--output") + 1] = str(output)
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(arguments)
    assert failure.value.code == 2
    assert _report(capsys)["error_type"] == "UsageError"
    assert protected.read_bytes() == original


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("locator_option", ["--catalog-db", "--schedule-db"])
def test_commands_protect_configured_sqlite_files_even_when_absent(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str], suffix: str, locator_option: str,
) -> None:
    arguments, _ = baseline_command
    database = tmp_path / "unused.sqlite"
    arguments[1:1] = [locator_option, str(database)]
    arguments[arguments.index("--output") + 1] = str(database) + suffix
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(arguments)
    assert failure.value.code == 2
    assert _report(capsys)["error_type"] == "UsageError"
    assert not _output(arguments).exists()


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_commands_protect_aliases_of_sqlite_journals(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str], alias: str,
) -> None:
    arguments, _ = baseline_command
    database = tmp_path / "catalog.sqlite"
    journal = tmp_path / "catalog.sqlite-wal"
    journal.write_bytes(b"journal evidence")
    if alias == "symlink":
        _output(arguments).symlink_to(journal)
    else:
        _output(arguments).hardlink_to(journal)
    arguments[1:1] = ["--catalog-db", str(database)]
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(arguments)
    assert failure.value.code == 2
    _report(capsys)
    assert journal.read_bytes() == b"journal evidence"


def test_commands_protect_journals_beside_resolved_catalog_symlink(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, _ = baseline_command
    database = tmp_path / "catalog.sqlite"
    database.write_bytes(b"catalog evidence")
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(database)
    output = tmp_path / "catalog.sqlite-journal"
    arguments[1:1] = ["--catalog-db", str(alias)]
    arguments[arguments.index("--output") + 1] = str(output)
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(arguments)
    assert failure.value.code == 2
    _report(capsys)
    assert database.read_bytes() == b"catalog evidence"
    assert not output.exists()


def test_commands_protect_loaded_cli_configuration(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, _ = baseline_command
    config = tmp_path / "cli.yaml"
    config.write_text("version: 1\ndefaults:\n  json: true\n", encoding="utf-8")
    original = config.read_bytes()
    arguments[:1] = ["--config", str(config)]
    arguments[arguments.index("--output") + 1] = str(config)
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(arguments)
    assert failure.value.code == 2
    _report(capsys)
    assert config.read_bytes() == original


@pytest.mark.parametrize("input_index", ["--input", "auxiliary"])
@pytest.mark.parametrize("invalid_json", ['{"value":', '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}'])
def test_bad_json_fails_before_computation_or_publication(
    baseline_command: tuple[list[str], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    input_index: str, invalid_json: str,
) -> None:
    arguments, _ = baseline_command
    option = input_index
    if option == "auxiliary":
        option = "--training-config" if "--training-config" in arguments else "--model"
    Path(arguments[arguments.index(option) + 1]).write_text(invalid_json, encoding="utf-8")
    monkeypatch.setattr(cognistore_cli, "train_baseline", _forbid)
    monkeypatch.setattr(cognistore_cli, "evaluate_baseline", _forbid)
    _output(arguments).write_bytes(b"previous artifact")
    assert cognistore_cli.main(arguments) == 1
    assert _report(capsys)["error_type"] in {"JSONDecodeError", "ValueError"}
    assert _output(arguments).read_bytes() == b"previous artifact"


def test_validation_failure_preserves_previous_artifact(
    baseline_command: tuple[list[str], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, _ = baseline_command

    def invalid(*args: object, **kwargs: object) -> dict:
        raise ValueError("incompatible dataset or model")

    monkeypatch.setattr(cognistore_cli, "train_baseline", invalid)
    monkeypatch.setattr(cognistore_cli, "evaluate_baseline", invalid)
    _output(arguments).write_bytes(b"previous artifact")
    assert cognistore_cli.main(arguments) == 1
    assert _report(capsys)["error_type"] == "ValueError"
    assert _output(arguments).read_bytes() == b"previous artifact"


def test_failed_publication_preserves_previous_artifact_and_removes_temporary_file(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, artifact = baseline_command
    output = _output(arguments)
    output.write_bytes(b"previous artifact")

    def fail_replace(source: Path, destination: Path) -> None:
        assert json.loads(source.read_text()) == artifact
        assert destination == output
        raise OSError("publication failed")

    monkeypatch.setattr(cognistore_cli.os, "replace", fail_replace)
    assert cognistore_cli.main(arguments) == 1
    assert _report(capsys)["error_type"] == "OSError"
    assert output.read_bytes() == b"previous artifact"
    assert list(tmp_path.glob(".output.json.*.tmp")) == []


def test_nonfinite_computed_artifact_is_never_published(
    baseline_command: tuple[list[str], dict[str, object]],
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    arguments, artifact = baseline_command
    artifact["invalid_metric"] = float("nan")
    _output(arguments).write_bytes(b"previous artifact")
    assert cognistore_cli.main(arguments) == 1
    assert _report(capsys)["error_type"] == "ValueError"
    assert _output(arguments).read_bytes() == b"previous artifact"
    assert list(tmp_path.glob(".output.json.*.tmp")) == []


def test_fixture_training_and_evaluation_are_reproducible_and_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[2]
    dataset = root / "tests" / "fixtures" / "policy_baseline" / "dataset-v1.json"
    config = root / "configs" / "policy-baseline-v1.json"
    model = tmp_path / "model.json"
    evaluation = tmp_path / "evaluation.json"
    monkeypatch.setattr(cognistore_cli, "code_version", lambda: "fixture-source-version")
    train_arguments = [
        "--no-config", "policy-baseline-train", "--input", str(dataset),
        "--training-config", str(config), "--output", str(model), "--json",
    ]
    assert cognistore_cli.main(train_arguments) == 0
    trained = _report(capsys)
    first_model = model.read_bytes()
    model_artifact = json.loads(first_model)
    assert trained["artifact_version"] == model_artifact["model_version"]
    assert model_artifact["code_version"] == "fixture-source-version"
    assert cognistore_cli.main(train_arguments) == 0
    _report(capsys)
    assert model.read_bytes() == first_model

    evaluate_arguments = [
        "--no-config", "policy-baseline-evaluate", "--input", str(dataset),
        "--model", str(model), "--output", str(evaluation), "--json",
    ]
    assert cognistore_cli.main(evaluate_arguments) == 0
    evaluated = _report(capsys)
    first_evaluation = evaluation.read_bytes()
    report_artifact = json.loads(first_evaluation)
    assert evaluated["artifact_version"] == report_artifact["report_version"]
    assert report_artifact["model_version"] == model_artifact["model_version"]
    assert evaluated["checks"]["temporal_leakage"] == "passed"
    assert set(evaluated["metrics"]) == {"validation", "test"}
    assert evaluated["promotion"]["automatic_promotion"] is False
    assert evaluated["promotion"]["production_eligible"] is False
    for comparison in evaluated["metrics"].values():
        assert set(comparison) == {"learned_gate", "recorded_rules", "suppress_all"}
        assert comparison["learned_gate"]["movement"]["cost_unit"] == "planned_bytes_proxy"
    assert cognistore_cli.main(evaluate_arguments) == 0
    _report(capsys)
    assert evaluation.read_bytes() == first_evaluation

    model_artifact["dataset_sha256"] = "mismatched-dataset"
    model.write_text(json.dumps(model_artifact), encoding="utf-8")
    assert cognistore_cli.main(evaluate_arguments) == 1
    assert _report(capsys)["error_type"] == "ValueError"
    assert evaluation.read_bytes() == first_evaluation
