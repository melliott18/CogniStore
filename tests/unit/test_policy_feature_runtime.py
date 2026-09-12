from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from typing import cast

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.embeddings import (
    OpenAICompatibleEmbeddingProvider,
    SentenceTransformersEmbeddingProvider,
)
from cognistore.core.policy_features import EmbeddingSimilarityFeatureProvider
from cognistore.db.catalog import SQLCatalog
from cognistore.db.embeddings import PgVectorEmbeddingStore
from cognistore.policy_feature_runtime import (
    PolicyFeatureRuntimeConfigError,
    load_policy_feature_loader,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return path


def _postgres_catalog() -> SQLCatalog:
    catalog = object.__new__(SQLCatalog)
    catalog._engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    return cast(SQLCatalog, catalog)


def test_missing_config_path_uses_safe_default_loader(tmp_path: Path) -> None:
    loader = load_policy_feature_loader(tmp_path / "missing.yaml", Catalog())

    assert loader.embedding_provider is None


def test_omitted_embedding_block_uses_safe_default_with_in_memory_catalog(
    tmp_path: Path,
) -> None:
    config = _write(tmp_path / "drivers.yaml", "tiers: {}\n")

    loader = load_policy_feature_loader(config, Catalog())

    assert loader.embedding_provider is None


@pytest.mark.parametrize(
    ("body", "provider_type"),
    [
        (
            """
            tiers: {}
            embedding:
              provider: sentence-transformers
              model: sentence-transformers/all-MiniLM-L6-v2
              revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
              dimensions: 384
              max_batch_size: 16
            """,
            SentenceTransformersEmbeddingProvider,
        ),
        (
            """
            tiers: {}
            embedding:
              provider: openai-compatible
              base_url: https://embeddings.example.com
              model: text-embedding-model
              deployment_version: release-2026-09-01
              dimensions: 1536
              request_dimensions: true
            """,
            OpenAICompatibleEmbeddingProvider,
        ),
    ],
)
def test_configured_provider_composes_pgvector_feature_loader(
    tmp_path: Path,
    body: str,
    provider_type: type[object],
) -> None:
    config = _write(tmp_path / "drivers.yaml", body)

    loader = load_policy_feature_loader(config, _postgres_catalog())

    feature_provider = loader.embedding_provider
    assert isinstance(feature_provider, EmbeddingSimilarityFeatureProvider)
    assert isinstance(feature_provider.indexer.provider, provider_type)
    assert isinstance(feature_provider.indexer.repository, PgVectorEmbeddingStore)


def test_openai_compatible_provider_resolves_only_named_environment_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "top-secret-token"
    monkeypatch.setenv("TEST_EMBEDDING_API_KEY", secret)
    config = _write(
        tmp_path / "drivers.yaml",
        """
        embedding:
          provider: openai-compatible
          base_url: https://embeddings.example.com
          api_key_env: TEST_EMBEDDING_API_KEY
          model: text-embedding-model
          deployment_version: latest
          dimensions: 1536
        """,
    )

    with pytest.raises(PolicyFeatureRuntimeConfigError) as exc_info:
        load_policy_feature_loader(config, _postgres_catalog())

    assert "deployment_version must identify an immutable" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_missing_openai_environment_secret_names_variable_without_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_EMBEDDING_API_KEY", raising=False)
    config = _write(
        tmp_path / "drivers.yaml",
        """
        embedding:
          provider: openai-compatible
          base_url: https://embeddings.example.com
          api_key_env: MISSING_EMBEDDING_API_KEY
          model: text-embedding-model
          deployment_version: release-2026-09-01
          dimensions: 1536
        """,
    )

    with pytest.raises(PolicyFeatureRuntimeConfigError) as exc_info:
        load_policy_feature_loader(config, _postgres_catalog())

    assert "MISSING_EMBEDDING_API_KEY" in str(exc_info.value)


def test_embedding_config_rejects_duplicate_and_unknown_fields(tmp_path: Path) -> None:
    duplicate = _write(
        tmp_path / "duplicate.yaml",
        """
        embedding:
          provider: sentence-transformers
          provider: openai-compatible
        """,
    )
    with pytest.raises(PolicyFeatureRuntimeConfigError, match="duplicate mapping key"):
        load_policy_feature_loader(duplicate, Catalog())

    unknown = _write(
        tmp_path / "unknown.yaml",
        """
        embedding:
          provider: sentence-transformers
          model: sentence-transformers/all-MiniLM-L6-v2
          revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
          dimensions: 384
          typo: true
        """,
    )
    with pytest.raises(PolicyFeatureRuntimeConfigError, match="unsupported fields: typo"):
        load_policy_feature_loader(unknown, _postgres_catalog())


def test_embedding_config_rejects_malformed_and_partial_blocks(tmp_path: Path) -> None:
    malformed = _write(tmp_path / "malformed.yaml", "embedding: []\n")
    with pytest.raises(PolicyFeatureRuntimeConfigError, match="must be a mapping"):
        load_policy_feature_loader(malformed, Catalog())

    partial = _write(tmp_path / "partial.yaml", "embedding: {}\n")
    with pytest.raises(PolicyFeatureRuntimeConfigError, match="requires field 'provider'"):
        load_policy_feature_loader(partial, _postgres_catalog())


def test_embedding_config_rejects_incompatible_catalog_before_provider_use(
    tmp_path: Path,
) -> None:
    config = _write(
        tmp_path / "drivers.yaml",
        """
        embedding:
          provider: sentence-transformers
          model: sentence-transformers/all-MiniLM-L6-v2
          revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
          dimensions: 384
        """,
    )

    with pytest.raises(
        PolicyFeatureRuntimeConfigError,
        match="PostgreSQL catalog with pgvector",
    ):
        load_policy_feature_loader(config, Catalog())


def test_api_server_injects_loader_and_closes_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognistore.api import server

    seen: dict[str, object] = {}
    feature_loader = object()

    class FakeCatalog:
        closed = False

        def close(self) -> None:
            self.closed = True

    catalog = FakeCatalog()
    monkeypatch.setattr(server, "load_drivers", lambda path: {"hot": object()})
    monkeypatch.setattr(server, "open_catalog", lambda locator: catalog)

    def load_loader(path: str, opened: object) -> object:
        seen["loader_path"] = path
        seen["loader_catalog"] = opened
        return feature_loader

    def gateway(opened: object, drivers: object, **kwargs: object) -> object:
        seen["gateway_catalog"] = opened
        seen["gateway_drivers"] = drivers
        seen["gateway_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(server, "load_policy_feature_loader", load_loader)
    monkeypatch.setattr(server, "NatsJetStreamQueue", lambda *args, **kwargs: object())
    monkeypatch.setattr(server, "CogniStoreGateway", gateway)
    monkeypatch.setattr(server, "create_app", lambda gateway, **kwargs: gateway)
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: None)

    assert server.main(["--drivers", "runtime.yaml", "--catalog-db", "catalog.db"]) == 0
    assert seen["loader_path"] == "runtime.yaml"
    assert seen["loader_catalog"] is catalog
    assert cast(dict[str, object], seen["gateway_kwargs"])["feature_loader"] is feature_loader
    assert catalog.closed is True


def test_api_server_closes_catalog_when_runtime_composition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognistore.api import server

    class FakeCatalog:
        closed = False

        def close(self) -> None:
            self.closed = True

    catalog = FakeCatalog()
    monkeypatch.setattr(server, "load_drivers", lambda path: {"hot": object()})
    monkeypatch.setattr(server, "open_catalog", lambda locator: catalog)

    def fail_runtime(path: str, opened: object) -> object:
        raise PolicyFeatureRuntimeConfigError("invalid embedding runtime")

    monkeypatch.setattr(server, "load_policy_feature_loader", fail_runtime)

    with pytest.raises(PolicyFeatureRuntimeConfigError, match="invalid embedding runtime"):
        server.main(["--drivers", "runtime.yaml", "--catalog-db", "catalog.db"])
    assert catalog.closed is True
