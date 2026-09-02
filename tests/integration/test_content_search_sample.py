from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cognistore.api import create_app
from cognistore.core.embedding_index import HnswIndexConfig
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.samples.content_search import (
    ContentSearchManifest,
    ContentSearchRuntime,
    SampleQuery,
    read_sample_corpus,
)

pytestmark = pytest.mark.integration


def _ask(
    client: TestClient,
    query: SampleQuery,
    *,
    retrieval_mode: str,
    synthesize: bool = False,
) -> dict[str, Any]:
    response = client.post(
        "/v1/ask",
        json={
            "text": query.text,
            "filters": dict(query.filters),
            "retrieval_mode": retrieval_mode,
            "limit": 10,
            "candidate_limit": 20,
            "passages_per_result": 3,
            "synthesize": synthesize,
            "exact_vector": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _assert_expected_prefix(body: dict[str, Any], query: SampleQuery) -> None:
    observed = [result["citation"]["key"] for result in body["results"]]
    assert observed[: len(query.expected_keys)] == list(query.expected_keys)


def _query(manifest: ContentSearchManifest, name: str) -> SampleQuery:
    return manifest.queries[name]


def test_fresh_sample_ingests_indexes_queries_and_opens_a_citation(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    corpus = read_sample_corpus()
    drivers = {
        tier: PosixDriver(str(tmp_path / "tiers" / tier))
        for tier in ("hot", "warm", "cold")
    }

    with SQLCatalog(postgres_dsn) as catalog:
        with ContentSearchRuntime(
            catalog,
            drivers,
            keyword_index_path=tmp_path / "keyword",
            # The acceptance workflow is intentionally tiny. Exact vector
            # queries still exercise pgvector without building an approximate
            # graph whose quality cannot be meaningful for four objects.
            hnsw=HnswIndexConfig(enabled=False),
        ) as runtime:
            report = runtime.load(corpus)

            assert report.object_count == len(corpus.manifest.objects) == 4
            assert report.scan_observations == 4
            assert report.keyword_objects_indexed == 4
            assert report.keyword_passages_indexed >= 4
            assert report.embedded_passages > 0
            assert report.reused_passages > 0
            assert report.duplicate_content_groups == (
                (
                    "runbooks/database-backups.docx",
                    "z-archive/database-backups-copy.docx",
                ),
            )

            loaded_by_key = {item.key: item for item in report.objects}
            primary = loaded_by_key["runbooks/database-backups.docx"]
            duplicate = loaded_by_key["z-archive/database-backups-copy.docx"]
            assert primary.document_id == duplicate.document_id
            assert primary.sha256 == duplicate.sha256
            assert catalog.get_object_content(
                corpus.manifest.bucket, primary.key
            ) == catalog.get_object_content(corpus.manifest.bucket, duplicate.key)

            gateway = runtime.create_gateway()
            with TestClient(create_app(gateway)) as client:
                keyword_query = _query(corpus.manifest, "keyword")
                keyword = _ask(
                    client,
                    keyword_query,
                    retrieval_mode="metadata+keyword",
                )
                _assert_expected_prefix(keyword, keyword_query)
                assert keyword["mode"] == "metadata+keyword"
                assert {
                    component["signal"]
                    for result in keyword["results"]
                    for component in result["score_components"]
                } >= {"keyword"}
                assert {
                    item["component"]: item["state"] for item in keyword["providers"]
                }["vector"] == "not_requested"

                vector_query = _query(corpus.manifest, "vector")
                vector = _ask(
                    client,
                    vector_query,
                    retrieval_mode="metadata+vector",
                )
                _assert_expected_prefix(vector, vector_query)
                assert vector["mode"] == "metadata+vector"
                assert {
                    component["signal"]
                    for result in vector["results"]
                    for component in result["score_components"]
                } >= {"vector"}
                assert {
                    item["component"]: item["state"] for item in vector["providers"]
                }["keyword"] == "not_requested"

                ask_query = _query(corpus.manifest, "ask")
                answer = _ask(
                    client,
                    ask_query,
                    retrieval_mode="metadata+keyword+vector",
                    synthesize=True,
                )
                _assert_expected_prefix(answer, ask_query)
                assert answer["mode"] == "metadata+keyword+vector"
                assert answer["generation_status"] == "succeeded"
                assert answer["answer"] is not None
                assert "a signed legal hold suspends scheduled deletion" in answer[
                    "answer"
                ]["text"].casefold()
                assert {
                    passage["match"]["signal"]
                    for passage in answer["results"][0]["passages"]
                } == {"keyword", "vector"}
                provider_states = {
                    item["component"]: item["state"] for item in answer["providers"]
                }
                assert provider_states == {
                    "metadata": "succeeded",
                    "keyword": "succeeded",
                    "vector": "succeeded",
                    "generation": "succeeded",
                }
                valid_citations = {
                    citation
                    for result in answer["results"]
                    for citation in (
                        result["citation"]["citation_id"],
                        *(passage["citation"]["citation_id"] for passage in result["passages"]),
                    )
                }
                assert set(answer["answer"]["citations"]) <= valid_citations

                citation = answer["results"][0]["citation"]
                cited_manifest_object = corpus.object_for_key(citation["key"])
                opened = client.get(
                    f"/v1/objects/{citation['tier']}/{citation['bucket']}/{citation['key']}"
                )
                assert opened.status_code == 200
                assert opened.content == corpus.payload_for(cited_manifest_object)
                assert hashlib.sha256(opened.content).hexdigest() == citation["content_sha256"]
                assert opened.headers["Content-Type"].startswith(
                    cited_manifest_object.media_type
                )

            # Reloading the checked corpus repairs indexes without duplicating
            # vectors and leaves both duplicate coordinates independently live.
            replay = runtime.load(corpus)
            assert replay.embedded_passages == 0
            assert replay.reused_passages == sum(
                item.total_passages for item in replay.objects
            )
