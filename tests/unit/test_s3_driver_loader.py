"""Configuration-loader coverage for the S3 storage backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("boto3")

from cognistore.drivers import driver_loader


class _ConstructedS3Driver:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def same_backend(self, other: object) -> bool:
        return self is other


def test_loader_selects_s3_and_resolves_indirected_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access_key = "loader-test-access"
    secret_key = "loader-test-secret"
    session_token = "loader-test-session"
    monkeypatch.setenv("COGNISTORE_TEST_S3_ACCESS", access_key)
    monkeypatch.setenv("COGNISTORE_TEST_S3_SECRET", secret_key)
    monkeypatch.setenv("COGNISTORE_TEST_S3_SESSION", session_token)
    constructed: list[_ConstructedS3Driver] = []

    def make_s3_driver(**kwargs: Any) -> _ConstructedS3Driver:
        driver = _ConstructedS3Driver(**kwargs)
        constructed.append(driver)
        return driver

    monkeypatch.setattr(driver_loader, "S3Driver", make_s3_driver)
    config = tmp_path / "drivers.yaml"
    config.write_text(
        "tiers:\n"
        "  archive:\n"
        "    driver: s3\n"
        "    endpoint_url: http://127.0.0.1:9000\n"
        "    region_name: us-west-2\n"
        "    access_key_env: COGNISTORE_TEST_S3_ACCESS\n"
        "    secret_key_env: COGNISTORE_TEST_S3_SECRET\n"
        "    session_token_env: COGNISTORE_TEST_S3_SESSION\n"
        "    addressing_style: path\n"
        "    auto_create_bucket: true\n"
        "    chunk_size: 5242880\n"
        "    list_page_size: 7\n"
        "    multipart_threshold: 10485760\n"
    )

    loaded = driver_loader.load_drivers(str(config))

    assert loaded == {"archive": constructed[0]}
    kwargs = constructed[0].kwargs
    assert kwargs["endpoint_url"] == "http://127.0.0.1:9000"
    assert kwargs["region_name"] == "us-west-2"
    assert kwargs["access_key"] == access_key
    assert kwargs["secret_key"] == secret_key
    assert kwargs["session_token"] == session_token
    assert kwargs["addressing_style"] == "path"
    assert kwargs["auto_create_bucket"] is True
    assert kwargs["chunk_size"] == 5 * 1024 * 1024
    assert kwargs["list_page_size"] == 7
    assert kwargs["multipart_threshold"] == 10 * 1024 * 1024


def test_loader_missing_credential_environment_variable_is_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variable_name = "COGNISTORE_TEST_MISSING_SECRET"
    monkeypatch.delenv(variable_name, raising=False)
    config = tmp_path / "drivers.yaml"
    config.write_text(
        "tiers:\n"
        "  archive:\n"
        "    driver: s3\n"
        f"    secret_key_env: {variable_name}\n"
    )

    with pytest.raises(ValueError) as captured:
        driver_loader.load_drivers(str(config))

    message = str(captured.value)
    assert variable_name in message
