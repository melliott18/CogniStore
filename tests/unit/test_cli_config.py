from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cognistore.cli.config import (
    CliConfigError,
    CliConfigResolution,
    json_requested,
    resolve_cli_config,
)


def _write_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _basic_config(path: Path) -> None:
    _write_config(
        path,
        """\
version: 1
default_profile: local
defaults:
  base: /defaults
  catalog_db: defaults.sqlite
  job_stream: DEFAULTS
  ack_wait: 12
  json: false
profiles:
  local:
    base: /local
    job_stream: LOCAL
    dry_run: true
  production:
    base: /production
    job_stream: PRODUCTION
""",
    )


def test_value_precedence_and_sources(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _basic_config(path)

    resolution = resolve_cli_config(
        ["put", "--config", str(path), "--profile", "production"],
        {
            "COGNISTORE_BASE": "/environment",
            "COGNISTORE_ACK_WAIT": "45.5",
        },
    )

    assert resolution.path == path
    assert resolution.profile == "production"
    assert resolution.values == {
        "base": "/environment",
        "catalog_db": "defaults.sqlite",
        "job_stream": "PRODUCTION",
        "ack_wait": 45.5,
        "json": False,
    }
    assert resolution.sources == {
        "base": "environment:COGNISTORE_BASE",
        "catalog_db": "config:defaults",
        "job_stream": "profile:production",
        "ack_wait": "environment:COGNISTORE_ACK_WAIT",
        "json": "config:defaults",
    }


def test_explicit_config_path_precedes_environment_path(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.yaml"
    environment = tmp_path / "environment.yaml"
    _write_config(explicit, "version: 1\ndefaults:\n  base: /explicit\n")
    _write_config(environment, "version: 1\ndefaults:\n  base: /environment\n")

    resolution = resolve_cli_config(
        [f"--config={explicit}", "put"],
        {"COGNISTORE_CONFIG": str(environment)},
    )

    assert resolution.path == explicit
    assert resolution.values["base"] == "/explicit"


@pytest.mark.parametrize(
    ("argv", "environ", "expected"),
    [
        (["--profile", "production"], {"COGNISTORE_PROFILE": "local"}, "production"),
        ([], {"COGNISTORE_PROFILE": "production"}, "production"),
        ([], {}, "local"),
    ],
)
def test_profile_precedence(
    tmp_path: Path,
    argv: list[str],
    environ: dict[str, str],
    expected: str,
) -> None:
    path = tmp_path / "config.yaml"
    _basic_config(path)
    environ["COGNISTORE_CONFIG"] = str(path)

    assert resolve_cli_config(argv, environ).profile == expected


def test_environment_values_are_parsed_to_cli_types(tmp_path: Path) -> None:
    missing_default = tmp_path / "home"
    environ = {
        "HOME": str(missing_default),
        "COGNISTORE_BASE": "/data",
        "COGNISTORE_NATS_URL": "nats://one:4222, nats://two:4222",
        "COGNISTORE_ACK_WAIT": "1.25",
        "COGNISTORE_DEAD_LETTER_MAX_AGE": "3600",
        "COGNISTORE_STREAM_MAX_MESSAGES": "123",
        "COGNISTORE_STREAM_MAX_BYTES": "456",
        "COGNISTORE_JSON": "YES",
        "COGNISTORE_DRY_RUN": "0",
        "COGNISTORE_VERBOSE": "on",
    }

    resolution = resolve_cli_config([], environ)

    assert resolution.path is None
    assert resolution.values == {
        "base": "/data",
        "nats_url": ["nats://one:4222", "nats://two:4222"],
        "ack_wait": 1.25,
        "stream_max_messages": 123,
        "stream_max_bytes": 456,
        "dead_letter_max_age": 3600.0,
        "json": True,
        "dry_run": False,
        "verbose": True,
    }
    assert all(
        source == f"environment:COGNISTORE_{key.upper()}"
        for key, source in resolution.sources.items()
    )


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("COGNISTORE_JSON", "not-a-secret-bool", "must be a boolean"),
        ("COGNISTORE_STREAM_MAX_BYTES", "not-a-secret-int", "must be an integer"),
        ("COGNISTORE_ACK_WAIT", "not-a-secret-float", "must be a finite number"),
        ("COGNISTORE_NATS_URL", "nats://ok,,not-a-secret-url", "comma-separated"),
    ],
)
def test_invalid_environment_values_are_rejected_without_echoing_values(
    tmp_path: Path,
    variable: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(CliConfigError, match=message) as exc_info:
        resolve_cli_config([], {"HOME": str(tmp_path), variable: value})

    assert value not in str(exc_info.value)


def test_no_config_bypasses_file_and_environment_profile_but_keeps_value_env(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.yaml"

    resolution = resolve_cli_config(
        ["--no-config", "put"],
        {
            "COGNISTORE_CONFIG": str(missing),
            "COGNISTORE_PROFILE": "production",
            "COGNISTORE_BASE": "/environment",
        },
    )

    assert resolution.path is None
    assert resolution.profile is None
    assert resolution.values == {"base": "/environment"}


@pytest.mark.parametrize(
    "argv",
    [
        ["--no-config", "--config", "config.yaml"],
        ["--config=config.yaml", "--no-config"],
        ["--no-config", "--profile", "local"],
    ],
)
def test_no_config_rejects_conflicting_explicit_selectors(argv: list[str]) -> None:
    with pytest.raises(CliConfigError):
        resolve_cli_config(argv, {})


def test_missing_explicit_or_environment_config_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(CliConfigError, match="does not exist"):
        resolve_cli_config(["--config", str(missing)], {})
    with pytest.raises(CliConfigError, match="does not exist"):
        resolve_cli_config([], {"COGNISTORE_CONFIG": str(missing)})


def test_missing_implicit_default_config_is_ignored(tmp_path: Path) -> None:
    resolution = resolve_cli_config([], {"HOME": str(tmp_path)})

    assert resolution == CliConfigResolution(None, None, {}, {})


def test_environment_profile_requires_a_loaded_config(tmp_path: Path) -> None:
    with pytest.raises(CliConfigError, match="requires a CLI configuration file"):
        resolve_cli_config(
            [],
            {"HOME": str(tmp_path), "COGNISTORE_PROFILE": "local"},
        )


def test_xdg_default_config_path_is_loaded(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    path = xdg / "cognistore" / "config.yaml"
    _write_config(path, "version: 1\ndefaults:\n  base: /xdg\n")

    resolution = resolve_cli_config([], {"XDG_CONFIG_HOME": str(xdg)})

    assert resolution.path == path
    assert resolution.values == {"base": "/xdg"}


def test_home_default_config_path_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / ".config" / "cognistore" / "config.yaml"
    _write_config(path, "version: 1\ndefaults:\n  base: /home\n")

    resolution = resolve_cli_config([], {"HOME": str(tmp_path)})

    assert resolution.path == path
    assert resolution.values == {"base": "/home"}


@pytest.mark.parametrize(
    "body",
    [
        "version: 2\n",
        "version: 1\nunknown: true\n",
        "version: 1\ndefaults:\n  unknown: true\n",
        "version: 1\ndefaults:\n  json: yes-as-a-string\n",
        "version: 1\ndefaults:\n  stream_max_bytes: 1.5\n",
        "version: 1\ndefaults:\n  ack_wait: .nan\n",
        "version: 1\ndefaults:\n  nats_url: nats://one:4222\n",
        "version: 1\nprofiles:\n  invalid profile: {}\n",
        "version: 1\nprofiles:\n  local:\n    unknown: true\n",
    ],
)
def test_schema_is_strictly_validated(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, body)

    with pytest.raises(CliConfigError):
        resolve_cli_config(["--config", str(path)], {})


@pytest.mark.parametrize(
    "body",
    [
        "version: 1\ndefaults:\n  base: first\n  base: duplicate\n",
        "!!python/object/apply:os.system ['not-a-secret-command']\n",
        "version: [unterminated-not-a-secret\n",
    ],
)
def test_unsafe_duplicate_or_invalid_yaml_is_rejected_without_scalar_details(
    tmp_path: Path,
    body: str,
) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, body)

    with pytest.raises(CliConfigError, match="invalid CLI configuration YAML") as exc_info:
        resolve_cli_config(["--config", str(path)], {})

    assert "not-a-secret" not in str(exc_info.value)
    assert "base: duplicate" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("--profile", "missing"),
        ("COGNISTORE_PROFILE", "missing"),
    ],
)
def test_unknown_selected_profile_is_rejected(
    tmp_path: Path,
    selector: str,
    value: str,
) -> None:
    path = tmp_path / "config.yaml"
    _basic_config(path)
    argv = ["--config", str(path)]
    environ: dict[str, str] = {}
    if selector == "--profile":
        argv.extend([selector, value])
    else:
        environ[selector] = value

    with pytest.raises(CliConfigError, match="profile is not defined"):
        resolve_cli_config(argv, environ)


def test_unknown_default_profile_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, "version: 1\ndefault_profile: absent\nprofiles: {}\n")

    with pytest.raises(CliConfigError, match="does not name a defined profile"):
        resolve_cli_config(["--config", str(path)], {})


def test_explicit_profile_does_not_hide_invalid_config_default(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(
        path,
        "version: 1\ndefault_profile: absent\nprofiles:\n  local: {}\n",
    )

    with pytest.raises(CliConfigError, match="does not name a defined profile"):
        resolve_cli_config(
            ["--config", str(path), "--profile", "local"],
            {},
        )


def test_json_requested_honors_explicit_argument_and_resolution_default() -> None:
    disabled = CliConfigResolution(None, None, {"json": False}, {})
    enabled = CliConfigResolution(None, None, {"json": True}, {})

    assert json_requested(["put", "--json"], disabled)
    assert json_requested([], enabled)
    assert not json_requested(["put", "--no-json"], enabled)
    assert not json_requested(["--json", "put", "--no-json"], disabled)
    assert json_requested(["--no-json", "put", "--json"], enabled)
    assert not json_requested(["--", "--json"], disabled)


def test_resolution_selectors_are_immutable() -> None:
    resolution = CliConfigResolution(None, None, {}, {})

    with pytest.raises(FrozenInstanceError):
        resolution.path = Path("other.yaml")  # type: ignore[misc]
