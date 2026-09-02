from __future__ import annotations

import hashlib
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path

import pytest

from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import (
    EmbeddingPolicyRuleConfig,
    PolicyConfig,
    PolicyEvaluationRequest,
)
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.embedding_index import EmbeddingIndexer
from cognistore.core.embeddings import EmbeddingSpace
from cognistore.core.policy_features import (
    CatalogPolicyFeatureLoader,
    EmbeddingSimilarityFeatureProvider,
)
from cognistore.db import PgVectorEmbeddingStore, SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.integration


class _PolicySemanticProvider:
    space = EmbeddingSpace(
        provider_implementation="cognistore-policy-feature-test",
        provider_implementation_version="1",
        model="cognistore/policy-feature-keywords",
        model_revision="policy-feature-fixture-v1",
        dimensions=2,
        preprocessing="fixture-keyword-projection",
    )

    @staticmethod
    def _vector(text: str) -> tuple[float, float]:
        lowered = text.casefold()
        return (
            2.0 if "orchard" in lowered or "apple" in lowered else 0.1,
            2.0 if "database" in lowered or "backup" in lowered else 0.1,
        )

    def embed_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(text) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)


def _publish_document(
    catalog: SQLCatalog,
    *,
    text: str,
) -> str:
    bucket = "policy"
    key = "reevaluation/document.pdf"
    mime = "application/pdf"
    payload = text.encode("utf-8")
    content = ContentIdentityBuilder(chunk_size=8).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    fence = catalog.capture_scan_fence(bucket, key)
    assert catalog.upsert_scan_observation(
        bucket,
        key,
        size=len(payload),
        tier="warm",
        generation=f"warm:{content.sha256}",
        metadata={
            "mime": mime,
            "mime_detection": {
                "schema_version": 1,
                "mime": mime,
                "detector": "libmagic",
                "provenance": "content",
                "confidence": "high",
                "content_mime": mime,
                "filename_mime": mime,
                "filename_encoding": None,
                "disagreement": False,
                "status": "detected",
                "fallback_reason": None,
            },
            "document_extraction": {
                "schema_version": 1,
                "status": "succeeded",
                "source_mime": mime,
                "source_size": len(payload),
                "parser": {
                    "name": "fixture-parser",
                    "implementation_version": "1",
                    "runtime_version": "1.0",
                },
                "normalization_version": 1,
                "text": text,
                "text_bytes": len(payload),
                "output_bytes": len(payload) + 2,
                "document_metadata": {},
                "failure_code": None,
            },
        },
        fence=fence,
        content=content,
    )
    return content.sha256


def test_reindexing_changes_policy_feature_freshness_and_reevaluation(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        first_sha256 = _publish_document(
            catalog,
            text="Apple trees in the orchard need winter pruning.",
        )
        indexer = EmbeddingIndexer(
            PgVectorEmbeddingStore(catalog),
            _PolicySemanticProvider(),
        )
        indexer.index_object("policy", "reevaluation/document.pdf")
        gateway = CogniStoreGateway(
            catalog,
            {
                "hot": PosixDriver(str(tmp_path / "hot")),
                "warm": PosixDriver(str(tmp_path / "warm")),
            },
            feature_loader=CatalogPolicyFeatureLoader(
                EmbeddingSimilarityFeatureProvider(indexer)
            ),
        )
        request = PolicyEvaluationRequest(
            bucket="policy",
            key="reevaluation/document.pdf",
            config=PolicyConfig(
                policy="content",
                threshold=0,
                allowed_tiers=["hot", "warm"],
                embedding_rules=[
                    EmbeddingPolicyRuleConfig(
                        name="active-orchard",
                        query="orchard maintenance",
                        minimum_similarity=0.8,
                        destination_tier="hot",
                    )
                ],
            ),
        )

        matching = gateway.evaluate_policy(request)

        assert (matching.action, matching.destination_tier) == ("move", "hot")
        assert matching.features.schema_version == 1
        assert matching.features.embeddings[0].state == "fresh"
        assert matching.features.embeddings[0].similarity is not None
        assert matching.features.embeddings[0].similarity >= 0.8
        matching_provenance = matching.features.embeddings[0].provenance
        assert matching_provenance.content_sha256 == first_sha256
        assert matching_provenance.details["space_id"] == str(
            _PolicySemanticProvider.space.space_id
        )
        assert matching_provenance.details["model_revision"] == (
            _PolicySemanticProvider.space.model_revision
        )
        assert isinstance(matching_provenance.details["indexed_at"], str)

        replacement_sha256 = _publish_document(
            catalog,
            text="The database backup is restored during a recovery drill.",
        )
        assert replacement_sha256 != first_sha256

        awaiting_reindex = gateway.evaluate_policy(request)

        assert awaiting_reindex.action == "stay"
        assert awaiting_reindex.destination_tier is None
        assert awaiting_reindex.features.embeddings[0].state in {"missing", "stale"}
        assert "embedding:active-orchard=" in awaiting_reindex.reason

        indexer.index_object("policy", "reevaluation/document.pdf")
        reevaluated = gateway.evaluate_policy(request)

        assert reevaluated.action == "stay"
        assert reevaluated.destination_tier is None
        assert reevaluated.features.embeddings[0].state == "fresh"
        assert reevaluated.features.embeddings[0].similarity is not None
        assert reevaluated.features.embeddings[0].similarity < 0.8
        assert (
            reevaluated.features.embeddings[0].provenance.content_sha256
            == replacement_sha256
        )
        assert reevaluated.features.embeddings[0].provenance.content_sha256 != (
            matching_provenance.content_sha256
        )
        assert hashlib.sha256(
            b"The database backup is restored during a recovery drill."
        ).hexdigest() == replacement_sha256
