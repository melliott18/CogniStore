"""Azure configuration boundaries do not require network access or Azure SDKs."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from cognistore.drivers import driver_loader


class _ConstructedAzureBlobDriver:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def azure_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("cognistore.drivers.azure_blob_driver")
    setattr(module, "AzureBlobDriver", _ConstructedAzureBlobDriver)
    monkeypatch.setitem(sys.modules, module.__name__, module)


def _write_config(path: Path, **fields: Any) -> Path:
    path.write_text(yaml.safe_dump({"tiers": {"archive": {"driver": "azure_blob", **fields}}}))
    return path


@pytest.mark.usefixtures("azure_constructor")
def test_loader_resolves_connection_string_and_transfer_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_string = "AccountName=test;AccountKey=loader-test-secret"
    monkeypatch.setenv("COGNISTORE_TEST_AZURE_CONNECTION", connection_string)
    config = _write_config(
        tmp_path / "drivers.yaml",
        connection_string_env="COGNISTORE_TEST_AZURE_CONNECTION",
        auto_create_container=True,
        chunk_size=1048576,
        list_page_size=7,
    )

    loaded = driver_loader.load_drivers(str(config))

    assert loaded["archive"].kwargs == {
        "account_url": None,
        "connection_string": connection_string,
        "credential": None,
        "auto_create_container": True,
        "chunk_size": 1048576,
        "list_page_size": 7,
    }


@pytest.mark.usefixtures("azure_constructor")
@pytest.mark.parametrize("strategy", ["default", "credential", "credential_env"])
def test_loader_account_url_authentication_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strategy: str
) -> None:
    fields = {"account_url": "https://cognistoretest.blob.core.windows.net"}
    credential = "loader-test-secret"
    if strategy == "credential":
        fields[strategy] = credential
    elif strategy == "credential_env":
        monkeypatch.setenv("COGNISTORE_TEST_AZURE_KEY", credential)
        fields[strategy] = "COGNISTORE_TEST_AZURE_KEY"
    config = _write_config(tmp_path / "drivers.yaml", **fields)

    driver = driver_loader.load_drivers(str(config))["archive"]

    assert driver.kwargs == {
        "account_url": fields["account_url"],
        "connection_string": None,
        "credential": None if strategy == "default" else credential,
        "auto_create_container": False,
        "list_page_size": None,
    }


@pytest.mark.usefixtures("azure_constructor")
@pytest.mark.parametrize("field", ["connection_string", "credential"])
def test_loader_rejects_ambiguous_secret_sources_without_disclosing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    secret = "literal-secret-must-not-be-disclosed"
    resolved_secret = "environment-secret-must-not-be-disclosed"
    monkeypatch.setenv("COGNISTORE_TEST_AZURE_SECRET", resolved_secret)
    config = _write_config(
        tmp_path / "drivers.yaml",
        **{field: secret, f"{field}_env": "COGNISTORE_TEST_AZURE_SECRET"},
    )

    with pytest.raises(ValueError, match="must use only one") as captured:
        driver_loader.load_drivers(str(config))

    assert secret not in str(captured.value)
    assert resolved_secret not in str(captured.value)


@pytest.mark.usefixtures("azure_constructor")
@pytest.mark.parametrize("field", ["connection_string", "credential"])
def test_loader_missing_environment_variable_does_not_reflect_other_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    variable = "COGNISTORE_TEST_AZURE_MISSING"
    monkeypatch.delenv(variable, raising=False)
    secret = "sig=endpoint-secret-must-not-be-disclosed"
    config = _write_config(
        tmp_path / "drivers.yaml",
        account_url=f"https://example.blob.core.windows.net?{secret}",
        **{f"{field}_env": variable},
    )

    with pytest.raises(ValueError, match=variable) as captured:
        driver_loader.load_drivers(str(config))

    assert secret not in str(captured.value)


@pytest.mark.usefixtures("azure_constructor")
@pytest.mark.parametrize("field", ["connection_string", "credential"])
@pytest.mark.parametrize("value", [None, True, 42, "", "  ", {"secret": "redacted"}])
def test_loader_rejects_non_string_or_empty_secrets(
    tmp_path: Path, field: str, value: Any
) -> None:
    config = _write_config(tmp_path / "drivers.yaml", **{field: value})

    with pytest.raises(ValueError, match=f"field '{field}' must be a non-empty string"):
        driver_loader.load_drivers(str(config))


@pytest.mark.usefixtures("azure_constructor")
@pytest.mark.parametrize("field", ["connection_string_env", "credential_env"])
@pytest.mark.parametrize("value", ["sig=literal-secret", "bad name", "1INVALID"])
def test_loader_rejects_malformed_environment_names_without_disclosing_them(
    tmp_path: Path, field: str, value: str
) -> None:
    config = _write_config(tmp_path / "drivers.yaml", **{field: value})

    with pytest.raises(ValueError, match="must name an environment variable") as captured:
        driver_loader.load_drivers(str(config))

    assert value not in str(captured.value)


@pytest.mark.usefixtures("azure_constructor")
def test_loader_rejects_unknown_azure_configuration_fields(tmp_path: Path) -> None:
    config = _write_config(tmp_path / "drivers.yaml", endpoint_url="secret-invalid-option")

    with pytest.raises(ValueError, match="unsupported configuration fields") as captured:
        driver_loader.load_drivers(str(config))

    assert "secret-invalid-option" not in str(captured.value)
