from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml


class CliConfigError(ValueError):
    """Raised when global CLI configuration cannot be resolved safely."""


@dataclass(frozen=True)
class CliConfigResolution:
    """Resolved file/profile/environment defaults and their provenance."""

    path: Path | None
    profile: str | None
    values: dict[str, object]
    sources: dict[str, str]


_TOP_LEVEL_KEYS = frozenset({"version", "default_profile", "defaults", "profiles"})
_GLOBAL_KEYS = (
    "base",
    "drivers",
    "catalog_db",
    "schedule_db",
    "nats_url",
    "job_stream",
    "job_subject",
    "job_consumer",
    "ack_wait",
    "stream_max_messages",
    "stream_max_bytes",
    "dead_letter_stream",
    "dead_letter_subject",
    "dead_letter_max_age",
    "json",
    "dry_run",
    "verbose",
)
_GLOBAL_KEY_SET = frozenset(_GLOBAL_KEYS)
_STRING_KEYS = frozenset(
    {
        "base",
        "drivers",
        "catalog_db",
        "schedule_db",
        "job_stream",
        "job_subject",
        "job_consumer",
        "dead_letter_stream",
        "dead_letter_subject",
    }
)
_FLOAT_KEYS = frozenset({"ack_wait", "dead_letter_max_age"})
_INTEGER_KEYS = frozenset({"stream_max_messages", "stream_max_bytes"})
_BOOLEAN_KEYS = frozenset({"json", "dry_run", "verbose"})
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class _Selectors:
    config: str | None
    profile: str | None
    no_config: bool


def _read_option_value(argv: Sequence[str], index: int, option: str) -> tuple[str, int]:
    argument = argv[index]
    prefix = f"{option}="
    if argument.startswith(prefix):
        value = argument[len(prefix) :]
        if not value:
            raise CliConfigError(f"{option} requires a non-empty value")
        return value, index
    if index + 1 >= len(argv) or argv[index + 1] in {
        "--config",
        "--no-config",
        "--profile",
    }:
        raise CliConfigError(f"{option} requires a value")
    value = argv[index + 1]
    if not isinstance(value, str) or not value:
        raise CliConfigError(f"{option} requires a non-empty value")
    return value, index + 1


def _selectors(argv: Sequence[str]) -> _Selectors:
    config: str | None = None
    profile: str | None = None
    no_config = False
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            break
        if argument == "--config" or argument.startswith("--config="):
            config, index = _read_option_value(argv, index, "--config")
        elif argument == "--profile" or argument.startswith("--profile="):
            profile, index = _read_option_value(argv, index, "--profile")
        elif argument == "--no-config":
            no_config = True
        index += 1
    if no_config and config is not None:
        raise CliConfigError("--no-config cannot be combined with --config")
    return _Selectors(config=config, profile=profile, no_config=no_config)


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _expand_home(value: str, environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    if home and (value == "~" or value.startswith("~/")):
        return Path(home) / value[2:] if value != "~" else Path(home)
    return Path(value).expanduser()


def _default_config_path(environ: Mapping[str, str]) -> Path:
    xdg_home = environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        return _expand_home(xdg_home, environ) / "cognistore" / "config.yaml"
    home = environ.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".config" / "cognistore" / "config.yaml"


def _config_path(
    selectors: _Selectors,
    environ: Mapping[str, str],
) -> tuple[Path | None, bool]:
    if selectors.no_config:
        return None, False
    if selectors.config is not None:
        return _expand_home(selectors.config, environ), True
    if "COGNISTORE_CONFIG" in environ:
        configured = environ["COGNISTORE_CONFIG"]
        if not isinstance(configured, str) or not configured:
            raise CliConfigError("COGNISTORE_CONFIG requires a non-empty path")
        return _expand_home(configured, environ), True
    return _default_config_path(environ), False


def _mapping(value: object, location: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise CliConfigError(f"{location} must be a mapping")
    return value


def _reject_unknown_keys(
    value: Mapping[object, object],
    allowed: frozenset[str],
    location: str,
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise CliConfigError(f"{location} keys must be strings")
    unknown = sorted(
        key for key in value if isinstance(key, str) and key not in allowed
    )
    if unknown:
        rendered = ", ".join(unknown)
        raise CliConfigError(f"{location} contains unknown key(s): {rendered}")


def _profile_name(value: object, location: str) -> str:
    if not isinstance(value, str) or _PROFILE_NAME.fullmatch(value) is None:
        raise CliConfigError(
            f"{location} must use only letters, digits, dots, underscores, and hyphens"
        )
    return value


def _yaml_value(key: str, value: object, location: str) -> object:
    field = f"{location}.{key}"
    if key in _STRING_KEYS:
        if not isinstance(value, str) or not value.strip():
            raise CliConfigError(f"{field} must be a non-empty string")
        return value
    if key == "nats_url":
        if not isinstance(value, list) or not value:
            raise CliConfigError(f"{field} must be a non-empty list of strings")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise CliConfigError(f"{field} must be a non-empty list of strings")
        return [item.strip() for item in value]
    if key in _FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CliConfigError(f"{field} must be a finite number")
        try:
            normalized = float(value)
        except OverflowError as exc:
            raise CliConfigError(f"{field} must be a finite number") from exc
        if not math.isfinite(normalized):
            raise CliConfigError(f"{field} must be a finite number")
        return normalized
    if key in _INTEGER_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CliConfigError(f"{field} must be an integer")
        return value
    if key in _BOOLEAN_KEYS:
        if not isinstance(value, bool):
            raise CliConfigError(f"{field} must be a boolean")
        return value
    raise AssertionError(f"missing validator for global key {key}")


def _global_values(value: object, location: str) -> dict[str, object]:
    fields = _mapping(value, location)
    _reject_unknown_keys(fields, _GLOBAL_KEY_SET, location)
    return {
        key: _yaml_value(key, raw_value, location)
        for key, raw_value in fields.items()
        if isinstance(key, str)
    }


def _load_config(path: Path) -> tuple[dict[str, object], dict[str, dict[str, object]], str | None]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            loader = _UniqueKeySafeLoader(stream)
            try:
                loaded = loader.get_single_data()
            finally:
                loader.dispose()
    except (OSError, UnicodeError) as exc:
        raise CliConfigError(f"unable to read CLI configuration file: {path}") from exc
    except yaml.YAMLError as exc:
        # PyYAML diagnostics can include scalar contents. Keep user-provided
        # values out of errors because URLs may contain credentials.
        raise CliConfigError(f"invalid CLI configuration YAML: {path}") from exc

    document = _mapping(loaded, "configuration")
    _reject_unknown_keys(document, _TOP_LEVEL_KEYS, "configuration")
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise CliConfigError("configuration.version must be the integer 1")

    defaults = _global_values(document.get("defaults", {}), "configuration.defaults")
    raw_profiles = _mapping(document.get("profiles", {}), "configuration.profiles")
    profiles: dict[str, dict[str, object]] = {}
    for raw_name, raw_profile in raw_profiles.items():
        name = _profile_name(raw_name, "configuration profile name")
        profiles[name] = _global_values(
            raw_profile,
            f"configuration.profiles.{name}",
        )

    default_profile: str | None = None
    if "default_profile" in document:
        default_profile = _profile_name(
            document["default_profile"],
            "configuration.default_profile",
        )
        if default_profile not in profiles:
            raise CliConfigError(
                "configuration.default_profile does not name a defined profile"
            )
    return defaults, profiles, default_profile


def _parse_boolean(value: str, variable: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CliConfigError(f"{variable} must be a boolean")


def _parse_integer(value: str, variable: str) -> int:
    try:
        return int(value.strip(), 10)
    except ValueError as exc:
        raise CliConfigError(f"{variable} must be an integer") from exc


def _parse_float(value: str, variable: str) -> float:
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise CliConfigError(f"{variable} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise CliConfigError(f"{variable} must be a finite number")
    return parsed


def _parse_string(value: str, variable: str) -> str:
    if not value.strip():
        raise CliConfigError(f"{variable} must be a non-empty string")
    return value


def _parse_nats_urls(value: str, variable: str) -> list[str]:
    urls = [item.strip() for item in value.split(",")]
    if not urls or any(not item for item in urls):
        raise CliConfigError(f"{variable} must be a comma-separated list of NATS URLs")
    return urls


def _environment_parser(key: str) -> Callable[[str, str], object]:
    if key in _STRING_KEYS:
        return _parse_string
    if key == "nats_url":
        return _parse_nats_urls
    if key in _FLOAT_KEYS:
        return _parse_float
    if key in _INTEGER_KEYS:
        return _parse_integer
    if key in _BOOLEAN_KEYS:
        return _parse_boolean
    raise AssertionError(f"missing environment parser for global key {key}")


def _apply_environment(
    values: dict[str, object],
    sources: dict[str, str],
    environ: Mapping[str, str],
) -> None:
    for key in _GLOBAL_KEYS:
        variable = f"COGNISTORE_{key.upper()}"
        if variable not in environ:
            continue
        raw_value = environ[variable]
        if not isinstance(raw_value, str):
            raise CliConfigError(f"{variable} must be a string")
        values[key] = _environment_parser(key)(raw_value, variable)
        sources[key] = f"environment:{variable}"


def resolve_cli_config(
    argv: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> CliConfigResolution:
    """Resolve config defaults using file, profile, and environment precedence.

    Explicit CLI values are intentionally not parsed here; callers apply the
    returned values as argparse defaults so normal argument parsing remains the
    final precedence layer.
    """

    active_environment = _environment(environ)
    selectors = _selectors(argv)
    if selectors.no_config and selectors.profile is not None:
        raise CliConfigError("--profile cannot be used with --no-config")

    candidate_path, required = _config_path(selectors, active_environment)
    loaded_path: Path | None = None
    defaults: dict[str, object] = {}
    profiles: dict[str, dict[str, object]] = {}
    configured_default_profile: str | None = None
    if candidate_path is not None:
        if candidate_path.exists():
            defaults, profiles, configured_default_profile = _load_config(candidate_path)
            loaded_path = candidate_path
        elif required:
            raise CliConfigError(f"CLI configuration file does not exist: {candidate_path}")

    selected_profile: str | None = None
    if loaded_path is not None:
        if selectors.profile is not None:
            selected_profile = _profile_name(selectors.profile, "--profile")
        elif "COGNISTORE_PROFILE" in active_environment:
            selected_profile = _profile_name(
                active_environment["COGNISTORE_PROFILE"],
                "COGNISTORE_PROFILE",
            )
        else:
            selected_profile = configured_default_profile
        if selected_profile is not None and selected_profile not in profiles:
            raise CliConfigError("selected CLI configuration profile is not defined")
    elif selectors.profile is not None:
        raise CliConfigError("--profile requires a CLI configuration file")
    elif not selectors.no_config and "COGNISTORE_PROFILE" in active_environment:
        raise CliConfigError(
            "COGNISTORE_PROFILE requires a CLI configuration file"
        )

    values = dict(defaults)
    sources = {key: "config:defaults" for key in defaults}
    if selected_profile is not None:
        profile_values = profiles[selected_profile]
        values.update(profile_values)
        sources.update(
            {key: f"profile:{selected_profile}" for key in profile_values}
        )
    _apply_environment(values, sources, active_environment)
    return CliConfigResolution(
        path=loaded_path,
        profile=selected_profile,
        values=values,
        sources=sources,
    )


def json_requested(argv: Sequence[str], resolution: CliConfigResolution) -> bool:
    """Return whether JSON output is requested explicitly or by defaults."""

    requested = bool(resolution.values.get("json", False))
    for argument in argv:
        if argument == "--":
            break
        if argument == "--json":
            requested = True
        elif argument == "--no-json":
            requested = False
    return requested
