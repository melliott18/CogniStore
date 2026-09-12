"""Configuration-loader coverage for the Google Cloud Storage backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from cognistore.drivers import GCSDriver, driver_loader


@pytest.fixture
def captured_options(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    constructed: list[dict[str, Any]] = []

    def make_gcs_driver(**kwargs: Any) -> object:
        constructed.append(kwargs)
        return object()

    monkeypatch.setattr(driver_loader, "GCSDriver", make_gcs_driver)
    return constructed


def _config(tmp_path: Path, **fields: Any) -> str:
    path = tmp_path / "drivers.yaml"
    path.write_text(yaml.safe_dump({"tiers": {"archive": {"driver": "gcs", **fields}}}))
    return str(path)


def test_loader_selects_gcs_and_resolves_credential_file_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_options: list[dict[str, Any]],
) -> None:
    credentials_path = str(tmp_path / "external-credentials.json")
    monkeypatch.setenv("COGNISTORE_TEST_GCS_CREDENTIALS", credentials_path)
    loaded = driver_loader.load_drivers(
        _config(
            tmp_path,
            project="example-project",
            credentials_file_env="COGNISTORE_TEST_GCS_CREDENTIALS",
            auto_create_bucket=True,
            chunk_size=262144,
            list_page_size=7,
            max_retries=5,
            timeout=15.5,
        )
    )

    assert set(loaded) == {"archive"}
    assert captured_options == [{
        "project": "example-project",
        "credentials_file": credentials_path,
        "auto_create_bucket": True,
        "chunk_size": 262144,
        "list_page_size": 7,
        "max_retries": 5,
        "timeout": 15.5,
    }]


def test_loader_leaves_adc_selection_to_driver(
    tmp_path: Path, captured_options: list[dict[str, Any]]
) -> None:
    driver_loader.load_drivers(_config(tmp_path))

    assert captured_options == [{"credentials_file": None}]


def test_loader_accepts_explicit_credentials_file_path(
    tmp_path: Path, captured_options: list[dict[str, Any]]
) -> None:
    credentials_path = str(tmp_path / "credentials.json")
    driver_loader.load_drivers(_config(tmp_path, credentials_file=credentials_path))

    assert captured_options == [{"credentials_file": credentials_path}]


def test_loader_passes_explicit_emulator_endpoint(
    tmp_path: Path, captured_options: list[dict[str, Any]]
) -> None:
    driver_loader.load_drivers(
        _config(tmp_path, emulator_endpoint="http://127.0.0.1:4443", project="test-project")
    )

    assert captured_options == [{
        "credentials_file": None,
        "emulator_endpoint": "http://127.0.0.1:4443",
        "project": "test-project",
    }]


@pytest.mark.parametrize(
    "field", ["credentials", "credentials_json", "access_token", "client", "endpoint_url"]
)
def test_loader_rejects_unknown_fields_without_echoing_values(
    tmp_path: Path, field: str, captured_options: list[dict[str, Any]]
) -> None:
    sensitive_value = "do-not-print-this-secret"
    with pytest.raises(ValueError, match="unsupported configuration fields") as captured:
        driver_loader.load_drivers(_config(tmp_path, **{field: sensitive_value}))

    assert field in str(captured.value)
    assert sensitive_value not in str(captured.value)
    assert captured_options == []


def test_loader_rejects_ambiguous_credential_file_selection(
    tmp_path: Path, captured_options: list[dict[str, Any]]
) -> None:
    with pytest.raises(ValueError, match="only one of") as captured:
        driver_loader.load_drivers(
            _config(
                tmp_path,
                credentials_file="secret-path.json",
                credentials_file_env="COGNISTORE_TEST_GCS_CREDENTIALS",
            )
        )

    assert "secret-path.json" not in str(captured.value)
    assert captured_options == []


@pytest.mark.parametrize("env_value", [None, ""])
def test_loader_requires_nonempty_credential_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    captured_options: list[dict[str, Any]],
) -> None:
    variable_name = "COGNISTORE_TEST_GCS_CREDENTIALS"
    if env_value is None:
        monkeypatch.delenv(variable_name, raising=False)
    else:
        monkeypatch.setenv(variable_name, env_value)

    with pytest.raises(ValueError, match=variable_name):
        driver_loader.load_drivers(_config(tmp_path, credentials_file_env=variable_name))

    assert captured_options == []


@pytest.mark.parametrize("variable_name", ["", "   ", None, False, 123, {"path": "secret"}])
def test_loader_rejects_invalid_credential_environment_variable_names(
    tmp_path: Path, variable_name: Any, captured_options: list[dict[str, Any]]
) -> None:
    with pytest.raises(ValueError, match="must name an environment variable"):
        driver_loader.load_drivers(_config(tmp_path, credentials_file_env=variable_name))

    assert captured_options == []


@pytest.mark.parametrize("credential_field", ["credentials_file", "credentials_file_env"])
def test_loader_rejects_emulator_combined_with_explicit_credentials(
    tmp_path: Path, credential_field: str, captured_options: list[dict[str, Any]]
) -> None:
    with pytest.raises(ValueError, match="cannot combine emulator_endpoint") as captured:
        driver_loader.load_drivers(
            _config(
                tmp_path,
                emulator_endpoint="http://127.0.0.1:4443",
                **{credential_field: "secret-value"},
            )
        )

    assert "secret-value" not in str(captured.value)
    assert captured_options == []


def test_gcs_driver_is_publicly_exported() -> None:
    from cognistore.drivers.gcs_driver import GCSDriver as implementation

    assert GCSDriver is implementation


def test_loader_constructs_gcs_emulator_alongside_posix(tmp_path: Path) -> None:
    config = tmp_path / "drivers.yaml"
    config.write_text(yaml.safe_dump({"tiers": {
        "hot": {"driver": "posix", "path": str(tmp_path / "hot")},
        "cloud": {
            "driver": "gcs",
            "project": "local-project",
            "emulator_endpoint": "http://127.0.0.1:4443/",
            "auto_create_bucket": True,
            "chunk_size": 262144,
            "list_page_size": 2,
            "max_retries": 0,
            "timeout": 10,
        },
    }}))

    loaded = driver_loader.load_drivers(str(config))
    driver = loaded["cloud"]
    assert isinstance(driver, GCSDriver)
    try:
        assert set(loaded) == {"hot", "cloud"}
        assert driver.endpoint_url == "http://127.0.0.1:4443"
        assert driver.chunk_size == 262144
        assert driver.list_page_size == 2
        assert driver.auto_create_bucket is True
        assert driver.project == "local-project"
        assert driver.max_retries == 0
        assert driver.timeout == 10
        assert not (tmp_path / "hot").exists()
    finally:
        driver.close()
