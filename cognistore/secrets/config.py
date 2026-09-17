"""Strict configuration for secret references; bootstrap material is external."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .core import SecretConfigurationError, SecretProvider, SecretReference, SecretResolver
from .diagnostics import secret_operation


def parse_secret_reference(config: object) -> SecretReference:
    if not isinstance(config, Mapping) or set(config) - {"provider", "name", "version", "field"}:
        raise SecretConfigurationError("Secret reference must be a mapping of reference fields")
    if "provider" not in config or "name" not in config:
        raise SecretConfigurationError("Secret reference requires provider and name")
    return SecretReference(
        provider=config["provider"], name=config["name"],
        version=config.get("version"), field=config.get("field"),
    )


def build_secret_resolver(config: object) -> SecretResolver:
    from .providers import AWSSecretsManagerProvider, VaultKVProvider

    if not isinstance(config, Mapping) or set(config) - {
        "providers", "cache_ttl_seconds", "max_entries",
    }:
        raise SecretConfigurationError("Invalid secret resolver configuration")
    declarations = config.get("providers", {})
    if not isinstance(declarations, Mapping):
        raise SecretConfigurationError("Secret providers must be a mapping")
    # Validate cache controls before constructing any network clients.
    cache_options: dict[str, Any] = {
        "cache_ttl_seconds": config.get("cache_ttl_seconds", 300),
        "max_entries": config.get("max_entries", 256),
    }
    SecretResolver({}, **cache_options)
    providers: dict[str, SecretProvider] = {}
    try:
        for name, declaration in declarations.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(declaration, Mapping):
                raise SecretConfigurationError("Invalid secret provider declaration")
            kind = declaration.get("type")
            if kind == "vault_kv":
                if set(declaration) - {
                    "type", "url", "token_file", "mount", "namespace", "timeout_seconds",
                } or not {"url", "token_file"} <= set(declaration):
                    raise SecretConfigurationError("Invalid Vault provider configuration")
                providers[name] = VaultKVProvider(
                    address=declaration["url"], token_file=declaration["token_file"],
                    **{key: declaration[key] for key in (
                        "mount", "namespace", "timeout_seconds",
                    ) if key in declaration},
                )
            elif kind == "aws_secrets_manager":
                if set(declaration) - {"type", "region_name", "timeout_seconds"}:
                    raise SecretConfigurationError("Invalid AWS secret provider configuration")
                providers[name] = AWSSecretsManagerProvider(**{
                    key: declaration[key] for key in ("region_name", "timeout_seconds")
                    if key in declaration
                })
            else:
                raise SecretConfigurationError("Unsupported secret provider type")
        return SecretResolver(providers, **cache_options)
    except Exception:
        for provider in providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    with secret_operation():
                        close()
                except Exception:
                    pass
        raise
