"""Runtime composition for access history and ephemeral policy features."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from cognistore.core.access import AccessConfig
from cognistore.core.catalog import CatalogStore
from cognistore.core.embedding_index import EmbeddingIndexer
from cognistore.core.embeddings import (
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    SentenceTransformersEmbeddingProvider,
)
from cognistore.core.policy_features import (
    CatalogPolicyFeatureLoader,
    EmbeddingSimilarityFeatureProvider,
)
from cognistore.db.catalog import SQLCatalog
from cognistore.db.embeddings import PgVectorEmbeddingStore

_SENTENCE_TRANSFORMERS_FIELDS = frozenset(
    {
        "provider",
        "model",
        "revision",
        "dimensions",
        "max_batch_size",
    }
)
_OPENAI_COMPATIBLE_FIELDS = frozenset(
    {
        "provider",
        "base_url",
        "api_key_env",
        "model",
        "deployment_version",
        "dimensions",
        "timeout_seconds",
        "max_batch_size",
        "max_request_bytes",
        "max_response_bytes",
        "request_dimensions",
    }
)


class PolicyFeatureRuntimeConfigError(ValueError):
    """The configured runtime cannot safely provide policy features."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects silently shadowed runtime fields."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
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
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _runtime_config(config_path: str | Path) -> dict[str, Any]:
    try:
        with open(config_path, encoding="utf-8") as stream:
            loader = _UniqueKeyLoader(stream)
            try:
                raw = loader.get_single_data()
            finally:
                loader.dispose()
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        raise PolicyFeatureRuntimeConfigError(
            f"invalid driver configuration YAML: {exc}"
        ) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise PolicyFeatureRuntimeConfigError("driver configuration must be a mapping")
    if any(not isinstance(field, str) for field in raw):
        raise PolicyFeatureRuntimeConfigError(
            "driver configuration field names must be strings"
        )
    unknown_top_level = sorted(set(raw).difference({"tiers", "embedding", "access_history"}))
    if unknown_top_level:
        raise PolicyFeatureRuntimeConfigError(
            "driver configuration has unsupported top-level fields: "
            + ", ".join(unknown_top_level)
        )
    return dict(raw)


def _embedding_config(config_path: str | Path) -> dict[str, Any] | None:
    raw = _runtime_config(config_path)
    if "embedding" not in raw:
        return None
    block = raw["embedding"]
    if not isinstance(block, Mapping):
        raise PolicyFeatureRuntimeConfigError(
            "driver configuration 'embedding' must be a mapping"
        )
    if any(not isinstance(field, str) for field in block):
        raise PolicyFeatureRuntimeConfigError(
            "driver configuration 'embedding' field names must be strings"
        )
    return dict(block)


def load_access_config(config_path: str | Path) -> AccessConfig:
    """Read the shared capture and policy-window configuration."""

    block = _runtime_config(config_path).get("access_history", {})
    if not isinstance(block, Mapping):
        raise PolicyFeatureRuntimeConfigError("access_history must be a mapping")
    allowed = {"windows_seconds", "retention_seconds", "sample_rate", "freshness_seconds"}
    if any(not isinstance(name, str) or name not in allowed for name in block):
        raise PolicyFeatureRuntimeConfigError("access_history has unsupported fields")
    values = dict(block)
    if "windows_seconds" in values:
        windows = values["windows_seconds"]
        if not isinstance(windows, list):
            raise PolicyFeatureRuntimeConfigError("access_history windows_seconds must be a list")
        values["windows_seconds"] = tuple(windows)
    try:
        return AccessConfig(**values)
    except (TypeError, ValueError) as exc:
        raise PolicyFeatureRuntimeConfigError(f"invalid access_history configuration: {exc}") from exc


def _required(config: Mapping[str, Any], field: str) -> Any:
    if field not in config:
        raise PolicyFeatureRuntimeConfigError(
            f"embedding configuration requires field '{field}'"
        )
    return config[field]


def _reject_unknown_fields(config: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(config).difference(allowed))
    if unknown:
        raise PolicyFeatureRuntimeConfigError(
            "embedding configuration has unsupported fields: " + ", ".join(unknown)
        )


def _optional(config: Mapping[str, Any], *fields: str) -> dict[str, Any]:
    return {field: config[field] for field in fields if field in config}


def _api_key(config: Mapping[str, Any]) -> str | None:
    if "api_key_env" not in config:
        return None
    variable_name = config["api_key_env"]
    if (
        not isinstance(variable_name, str)
        or not variable_name
        or variable_name != variable_name.strip()
        or "=" in variable_name
        or "\0" in variable_name
    ):
        raise PolicyFeatureRuntimeConfigError(
            "embedding configuration field 'api_key_env' must name an environment variable"
        )
    value = os.environ.get(variable_name)
    if not value:
        raise PolicyFeatureRuntimeConfigError(
            f"embedding configuration requires environment variable '{variable_name}'"
        )
    return value


def _provider(config: Mapping[str, Any]) -> EmbeddingProvider:
    provider_name = _required(config, "provider")
    try:
        if provider_name == "sentence-transformers":
            _reject_unknown_fields(config, _SENTENCE_TRANSFORMERS_FIELDS)
            return SentenceTransformersEmbeddingProvider(
                model=_required(config, "model"),
                revision=_required(config, "revision"),
                dimensions=_required(config, "dimensions"),
                **_optional(config, "max_batch_size"),
            )
        if provider_name == "openai-compatible":
            _reject_unknown_fields(config, _OPENAI_COMPATIBLE_FIELDS)
            return OpenAICompatibleEmbeddingProvider(
                base_url=_required(config, "base_url"),
                api_key=_api_key(config),
                model=_required(config, "model"),
                deployment_version=_required(config, "deployment_version"),
                dimensions=_required(config, "dimensions"),
                **_optional(
                    config,
                    "timeout_seconds",
                    "max_batch_size",
                    "max_request_bytes",
                    "max_response_bytes",
                    "request_dimensions",
                ),
            )
    except PolicyFeatureRuntimeConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise PolicyFeatureRuntimeConfigError(
            f"invalid embedding configuration: {exc}"
        ) from exc
    raise PolicyFeatureRuntimeConfigError(
        "embedding configuration field 'provider' must be "
        "'sentence-transformers' or 'openai-compatible'"
    )


def load_policy_feature_loader(
    config_path: str | Path,
    catalog: CatalogStore,
) -> CatalogPolicyFeatureLoader:
    """Compose the policy feature loader declared beside storage drivers.

    Embedding policy features are opt-in. An omitted ``embedding`` block keeps
    the loader's deterministic missing-provider behavior and works with every
    catalog implementation. Configured similarity lookup requires the existing
    PostgreSQL/pgvector embedding repository.
    """

    config = _embedding_config(config_path)
    access_config = load_access_config(config_path)
    if config is None:
        return CatalogPolicyFeatureLoader(access_catalog=catalog, access_config=access_config)
    if not isinstance(catalog, SQLCatalog) or catalog.backend != "postgresql":
        raise PolicyFeatureRuntimeConfigError(
            "embedding policy features require a PostgreSQL catalog with pgvector"
        )
    provider = _provider(config)
    indexer = EmbeddingIndexer(PgVectorEmbeddingStore(catalog), provider)
    return CatalogPolicyFeatureLoader(
        EmbeddingSimilarityFeatureProvider(indexer),
        access_catalog=catalog,
        access_config=access_config,
    )


__all__ = [
    "PolicyFeatureRuntimeConfigError",
    "load_access_config",
    "load_policy_feature_loader",
]
