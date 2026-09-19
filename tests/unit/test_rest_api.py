from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from cognistore.api.app import (
    MAX_JSON_BODY_BYTES,
    MAX_OBJECT_UPLOAD_BYTES,
    create_app,
)
from cognistore.api.errors import ResourceNotFoundError
from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import ObjectResource
from cognistore.api.openapi import render_document
from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
)
from cognistore.core.catalog import Catalog
from cognistore.core.policy_features import (
    EmbeddingFeatureRequest,
    EmbeddingPolicyFeature,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from cognistore.jobs.handlers import CATALOG_SCAN_JOB, POLICY_RUN_JOB
from cognistore.jobs.models import (
    JOB_SCHEMA_VERSION_V1,
    JOB_SCHEMA_VERSION_V2,
    STATUS_TRACKING_METADATA,
    BusState,
    EnqueueReceipt,
    JobEnvelope,
    QueueHealth,
    QueueSaturatedError,
)
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig


class _StaticPolicyFeatureLoader:
    def __init__(self, features: PolicyFeatures) -> None:
        self.features = features
        self.requests: tuple[EmbeddingFeatureRequest, ...] = ()

    def load(self, records, requests=(), *, as_of=None):
        detached_records = tuple(records)
        self.requests = tuple(requests)
        return {
            (record.bucket, record.key): self.features
            for record in detached_records
        }


def _assert_error(
    response,
    *,
    status_code: int,
    code: str,
    retryable: bool = False,
) -> dict[str, Any]:
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"schema_version", "request_id", "error"}
    assert body["schema_version"] == 1
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is retryable
    assert isinstance(body["error"]["message"], str)
    assert isinstance(body["error"]["details"], list)
    return body


def test_object_and_catalog_success_contracts(tmp_path: Path) -> None:
    catalog = Catalog()
    gateway = CogniStoreGateway(
        catalog,
        {"hot": PosixDriver(str(tmp_path / "hot"))},
    )

    with TestClient(create_app(gateway)) as client:
        created = client.put(
            "/v1/objects/hot/documents/folder/report.txt?overwrite=false",
            content=b"hello api",
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "X-Request-ID": "object-contract-1",
            },
        )

        assert created.status_code == 201
        assert created.headers["X-Request-ID"] == "object-contract-1"
        assert created.headers["Location"] == (
            "/v1/objects/hot/documents/folder/report.txt"
        )
        body = created.json()
        assert body == {
            "schema_version": 1,
            "tier": "hot",
            "bucket": "documents",
            "key": "folder/report.txt",
            "size": 9,
            "generation": body["generation"],
            "metadata": body["metadata"],
            "metadata_truncated": False,
        }
        assert body["generation"]
        assert body["metadata"]["mime"] == "text/plain"
        expected_etag = (
            '"'
            + hashlib.sha256(body["generation"].encode("utf-8")).hexdigest()
            + '"'
        )
        assert created.headers["ETag"] == expected_etag
        assert re.fullmatch(r'"[0-9a-f]{64}"', expected_etag)

        head = client.head("/v1/objects/hot/documents/folder/report.txt")
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["Accept-Ranges"] == "bytes"
        assert head.headers["Content-Length"] == "9"
        assert head.headers["Content-Type"].startswith("text/plain")
        assert head.headers["ETag"] == created.headers["ETag"]
        assert head.headers["Content-Disposition"] == "attachment"
        assert head.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
        assert head.headers["X-Content-Type-Options"] == "nosniff"

        downloaded = client.get("/v1/objects/hot/documents/folder/report.txt")
        assert downloaded.status_code == 200
        assert downloaded.content == b"hello api"
        assert downloaded.headers["Accept-Ranges"] == "bytes"
        assert downloaded.headers["ETag"] == created.headers["ETag"]
        assert downloaded.headers["Content-Disposition"] == "attachment"
        assert downloaded.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
        assert downloaded.headers["X-Content-Type-Options"] == "nosniff"

        partial = client.get(
            "/v1/objects/hot/documents/folder/report.txt",
            headers={"Range": "bytes=1-4"},
        )
        assert partial.status_code == 206
        assert partial.content == b"ello"
        assert partial.headers["Content-Range"] == "bytes 1-4/9"
        assert partial.headers["Content-Length"] == "4"
        assert partial.headers["Accept-Ranges"] == "bytes"
        assert partial.headers["ETag"] == expected_etag
        assert partial.headers["Content-Disposition"] == "attachment"
        assert partial.headers["X-Content-Type-Options"] == "nosniff"

        for invalid_range in ("items=0-1", "bytes=99-100"):
            invalid = client.get(
                "/v1/objects/hot/documents/folder/report.txt",
                headers={"Range": invalid_range},
            )
            _assert_error(
                invalid,
                status_code=416,
                code="range_not_satisfiable",
            )
            assert invalid.headers["Content-Range"] == "bytes */9"

        catalog_object = client.get(
            "/v1/catalog/objects/documents/folder/report.txt"
        )
        assert catalog_object.status_code == 200
        assert catalog_object.json() == {
            "schema_version": 1,
            "bucket": "documents",
            "key": "folder/report.txt",
            "size": 9,
            "tier": "hot",
            "metadata": body["metadata"],
            "metadata_truncated": False,
        }

        conflict = client.put(
            "/v1/objects/hot/documents/folder/report.txt?overwrite=false",
            content=b"replacement",
            headers={"Content-Type": "application/octet-stream"},
        )
        _assert_error(conflict, status_code=409, code="resource_conflict")

        too_large = client.put(
            "/v1/objects/hot/documents/too-large.bin",
            content=b"",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(MAX_OBJECT_UPLOAD_BYTES + 1),
            },
        )
        _assert_error(
            too_large,
            status_code=413,
            code="payload_too_large",
        )
        assert catalog.get("documents", "too-large.bin") is None

        deleted = client.delete("/v1/objects/hot/documents/folder/report.txt")
        assert deleted.status_code == 204
        assert deleted.content == b""

        missing = client.get("/v1/catalog/objects/documents/folder/report.txt")
        _assert_error(missing, status_code=404, code="resource_not_found")


class _QuotedGenerationGateway:
    generation = 'version-7:"backend-etag"'

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def _resource(self, tier: str, bucket: str, key: str) -> ObjectResource:
        return ObjectResource(
            tier=tier,
            bucket=bucket,
            key=key,
            size=4,
            generation=self.generation,
            metadata={"mime": "application/octet-stream"},
        )

    def put_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        data: bytes,
        *,
        overwrite: bool,
        content_type: str | None,
    ) -> ObjectResource:
        assert data == b"data"
        return self._resource(tier, bucket, key)

    def stat_object(self, tier: str, bucket: str, key: str) -> ObjectResource:
        return self._resource(tier, bucket, key)


def test_etag_hashes_unsafe_backend_generation_into_valid_stable_syntax() -> None:
    gateway = _QuotedGenerationGateway()
    expected = f'"{hashlib.sha256(gateway.generation.encode()).hexdigest()}"'

    with TestClient(create_app(gateway)) as client:  # type: ignore[arg-type]
        put = client.put(
            "/v1/objects/hot/bucket/object",
            content=b"data",
            headers={"Content-Type": "application/octet-stream"},
        )
        head = client.head("/v1/objects/hot/bucket/object")

    assert put.status_code == 201
    assert put.json()["generation"] == gateway.generation
    assert put.headers["ETag"] == expected
    assert head.status_code == 200
    assert head.headers["ETag"] == expected
    assert re.fullmatch(r'"[0-9a-f]{64}"', expected)


class _ReaderOpenFailureDriver(PosixDriver):
    def open_object_reader_if_generation(
        self,
        bucket: str,
        key: str,
        generation: str,
        range: str | None = None,
    ) -> Any:
        raise ObjectGenerationMismatchError(
            "fake-secret: generation changed before reader open"
        )


def test_reader_open_failure_is_enveloped_before_streaming_starts(
    tmp_path: Path,
) -> None:
    driver = _ReaderOpenFailureDriver(str(tmp_path / "unstable"))
    driver.put_object("documents", "unstable.txt", b"data")
    catalog = Catalog()
    catalog.upsert(
        "documents",
        "unstable.txt",
        size=4,
        tier="hot",
        metadata={"mime": "text/plain"},
    )
    gateway = CogniStoreGateway(catalog, {"hot": driver})

    with TestClient(create_app(gateway)) as client:
        response = client.get("/v1/objects/hot/documents/unstable.txt")

    body = _assert_error(
        response,
        status_code=409,
        code="object_generation_changed",
        retryable=True,
    )
    assert body["error"]["message"] == "The object changed during the request"
    assert "fake-secret" not in response.text
    assert response.headers["Content-Type"] == "application/json"
    assert "Accept-Ranges" not in response.headers
    assert "ETag" not in response.headers


def test_object_responses_reject_unsafe_persisted_media_types(tmp_path: Path) -> None:
    driver = PosixDriver(str(tmp_path / "unsafe-media"))
    driver.put_object("documents", "unsafe.txt", b"data")
    catalog = Catalog()
    catalog.upsert(
        "documents",
        "unsafe.txt",
        size=4,
        tier="hot",
        metadata={"mime": "text/plain\r\nX-Injected: yes"},
    )
    gateway = CogniStoreGateway(catalog, {"hot": driver})

    with TestClient(create_app(gateway)) as client:
        head = client.head("/v1/objects/hot/documents/unsafe.txt")
        downloaded = client.get("/v1/objects/hot/documents/unsafe.txt")

    assert head.status_code == 200
    assert downloaded.status_code == 200
    assert downloaded.content == b"data"
    assert head.headers["Content-Type"] == "application/octet-stream"
    assert downloaded.headers["Content-Type"] == "application/octet-stream"
    assert "X-Injected" not in head.headers
    assert "X-Injected" not in downloaded.headers


def test_active_content_is_always_served_as_a_sandboxed_attachment(
    tmp_path: Path,
) -> None:
    gateway = CogniStoreGateway(
        Catalog(),
        {"hot": PosixDriver(str(tmp_path / "active-content"))},
    )

    with TestClient(create_app(gateway)) as client:
        uploaded = client.put(
            "/v1/objects/hot/documents/untrusted.html",
            content=b"<script>document.cookie</script>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        full = client.get("/v1/objects/hot/documents/untrusted.html")
        partial = client.get(
            "/v1/objects/hot/documents/untrusted.html",
            headers={"Range": "bytes=0-7"},
        )

    assert uploaded.status_code == 201
    for response in (full, partial):
        assert response.headers["Content-Disposition"] == "attachment"
        assert response.headers["Content-Security-Policy"] == (
            "sandbox; default-src 'none'"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Type"].startswith("text/html")


def test_catalog_pagination_is_opaque_stable_and_query_bound() -> None:
    catalog = Catalog()
    for key, tier in (
        ("reports/03.txt", "warm"),
        ("reports/01.txt", "hot"),
        ("reports/04.txt", "hot"),
        ("reports/02.txt", "warm"),
    ):
        catalog.upsert("documents", key, size=len(key), tier=tier)
    gateway = CogniStoreGateway(catalog, {})

    with TestClient(create_app(gateway)) as client:
        first = client.get(
            "/v1/catalog/objects",
            params={"bucket": "documents", "prefix": "reports/", "limit": 2},
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [item["key"] for item in first_body["items"]] == [
            "reports/01.txt",
            "reports/02.txt",
        ]
        assert first_body["page"]["limit"] == 2
        cursor = first_body["page"]["next_cursor"]
        assert isinstance(cursor, str) and cursor
        assert "reports/02.txt" not in cursor

        second = client.get(
            "/v1/catalog/objects",
            params={
                "bucket": "documents",
                "prefix": "reports/",
                "limit": 2,
                "cursor": cursor,
            },
        )
        assert second.status_code == 200
        assert [item["key"] for item in second.json()["items"]] == [
            "reports/03.txt",
            "reports/04.txt",
        ]
        assert second.json()["page"] == {"limit": 2, "next_cursor": None}

        mismatch = client.get(
            "/v1/catalog/objects",
            params={
                "bucket": "documents",
                "prefix": "reports/0",
                "limit": 2,
                "cursor": cursor,
            },
        )
        mismatch_body = _assert_error(
            mismatch,
            status_code=422,
            code="invalid_cursor",
        )
        assert mismatch_body["error"]["message"] == (
            "cursor does not match the requested filters"
        )

        malformed = client.get(
            "/v1/catalog/objects",
            params={"bucket": "documents", "cursor": "not-a-cursor"},
        )
        _assert_error(malformed, status_code=422, code="invalid_cursor")


def test_validation_errors_use_the_versioned_envelope() -> None:
    gateway = CogniStoreGateway(Catalog(), {})

    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/ask",
            json={"text": "", "unexpected": True},
            headers={"X-Request-ID": "validation-contract-1"},
        )

    body = _assert_error(response, status_code=422, code="validation_error")
    assert body["request_id"] == "validation-contract-1"
    assert body["error"]["message"] == "Request validation failed"
    issues = {issue["location"]: issue for issue in body["error"]["details"]}
    assert set(issues) == {"body.text", "body.unexpected"}
    assert issues["body.text"]["type"] == "string_too_short"
    assert issues["body.unexpected"]["type"] == "extra_forbidden"


def test_ask_domain_validation_maps_request_constraints_to_422() -> None:
    app = create_app(CogniStoreGateway(Catalog(), {}))

    with TestClient(app) as client:
        limits = client.post(
            "/v1/ask",
            json={"text": "valid query", "limit": 100, "candidate_limit": 1},
        )
        internal_filter = client.post(
            "/v1/ask",
            json={
                "text": "valid query",
                "filters": {
                    "object_metadata": {
                        "content_identity": "fake-secret-filter-value"
                    }
                },
            },
        )

    limits_body = _assert_error(
        limits,
        status_code=422,
        code="validation_error",
    )
    assert limits_body["error"]["message"] == (
        "candidate_limit must be greater than or equal to limit"
    )

    filter_body = _assert_error(
        internal_filter,
        status_code=422,
        code="validation_error",
    )
    assert filter_body["error"]["message"] == (
        "object_metadata cannot filter internal field(s): content_identity"
    )
    assert "fake-secret-filter-value" not in internal_filter.text


def test_ask_retrieval_mode_controls_optional_provider_selection() -> None:
    app = create_app(CogniStoreGateway(Catalog(), {}))

    with TestClient(app) as client:
        metadata_only = client.post(
            "/v1/ask",
            json={"text": "metadata query", "retrieval_mode": "metadata"},
        )
        keyword_requested = client.post(
            "/v1/ask",
            json={
                "text": "keyword query",
                "retrieval_mode": "metadata+keyword",
            },
        )
        invalid = client.post(
            "/v1/ask",
            json={"text": "invalid mode", "retrieval_mode": "keyword"},
        )

    assert metadata_only.status_code == 200
    assert metadata_only.json()["mode"] == "metadata"
    assert {
        provider["component"]: provider["state"]
        for provider in metadata_only.json()["providers"]
    } == {
        "metadata": "succeeded",
        "keyword": "not_requested",
        "vector": "not_requested",
        "generation": "not_requested",
    }

    assert keyword_requested.status_code == 200
    assert keyword_requested.json()["mode"] == "metadata"
    assert {
        provider["component"]: provider["state"]
        for provider in keyword_requested.json()["providers"]
    } == {
        "metadata": "succeeded",
        "keyword": "missing",
        "vector": "not_requested",
        "generation": "not_requested",
    }

    _assert_error(invalid, status_code=422, code="validation_error")


def test_json_body_limit_rejects_oversized_content_length() -> None:
    app = create_app(CogniStoreGateway(Catalog(), {}))

    with TestClient(app) as client:
        response = client.post(
            "/v1/ask",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_JSON_BODY_BYTES + 1),
            },
        )

    body = _assert_error(
        response,
        status_code=413,
        code="payload_too_large",
    )
    assert str(MAX_JSON_BODY_BYTES) in body["error"]["message"]
    assert response.headers["Connection"] == "close"


def test_json_body_limit_rejects_oversized_chunked_stream() -> None:
    app = create_app(CogniStoreGateway(Catalog(), {}))

    def oversized_chunks():
        yield b'{"text":"'
        yield b"x" * (MAX_JSON_BODY_BYTES // 2)
        yield b"x" * (MAX_JSON_BODY_BYTES // 2 + 1)
        yield b'"}'

    with TestClient(app) as client:
        response = client.post(
            "/v1/ask",
            content=oversized_chunks(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

    body = _assert_error(
        response,
        status_code=413,
        code="payload_too_large",
    )
    assert str(MAX_JSON_BODY_BYTES) in body["error"]["message"]
    assert response.headers["Connection"] == "close"


def test_public_metadata_redacts_internals_and_bounds_oversized_values() -> None:
    catalog = Catalog()
    internal_metadata = {
        "classification": "internal",
        "mime": "text/plain",
        "path": "/srv/private/quarterly-report.txt",
        "content_identity": {"sha256": "a" * 64, "size": 42},
        "document_extraction": {"text": "private extracted content"},
        "mime_detection": {"sample": "private bytes"},
        "sample_len": 4096,
        "mtime": 1_700_000_000.0,
        "etag": "private-storage-tag",
        "version_id": "private-storage-version",
    }
    catalog.upsert(
        "documents",
        "quarterly-report.txt",
        size=42,
        tier="warm",
        metadata=internal_metadata,
    )
    catalog.upsert(
        "documents",
        "oversized.txt",
        size=1,
        tier="warm",
        metadata={"classification": "internal", "oversized": "x" * (16 * 1024)},
    )
    gateway = CogniStoreGateway(catalog, {})

    with TestClient(create_app(gateway)) as client:
        safe = client.get(
            "/v1/catalog/objects/documents/quarterly-report.txt"
        )
        oversized = client.get("/v1/catalog/objects/documents/oversized.txt")
        ask = client.post(
            "/v1/ask",
            json={
                "text": "quarterly report",
                "filters": {"bucket": "documents", "key_prefix": "quarterly-"},
            },
        )

    assert safe.status_code == 200
    assert safe.json()["metadata"] == {
        "classification": "internal",
        "mime": "text/plain",
    }
    assert safe.json()["metadata_truncated"] is False
    assert set(safe.json()["metadata"]).isdisjoint(
        {
            "path",
            "content_identity",
            "document_extraction",
            "mime_detection",
            "sample_len",
            "mtime",
            "etag",
            "version_id",
        }
    )

    assert oversized.status_code == 200
    assert oversized.json()["metadata"] == {}
    assert oversized.json()["metadata_truncated"] is True

    assert ask.status_code == 200
    assert len(ask.json()["results"]) == 1
    assert ask.json()["results"][0]["citation"]["object_metadata"] == {
        "classification": "internal",
        "mime": "text/plain",
    }


def test_ask_and_policy_evaluation_contracts(tmp_path: Path) -> None:
    catalog = Catalog()
    catalog.upsert(
        "documents",
        "quarterly-report.txt",
        size=42,
        tier="warm",
        metadata={"mime": "text/plain", "classification": "internal"},
    )
    catalog.upsert(
        "documents",
        "quarterly-report-public.txt",
        size=21,
        tier="warm",
        metadata={"mime": "text/plain", "classification": "public"},
    )
    catalog.upsert("documents", "archive.bin", size=11, tier="hot")
    gateway = CogniStoreGateway(
        catalog,
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
    )

    with TestClient(create_app(gateway)) as client:
        ask = client.post(
            "/v1/ask",
            json={
                "text": "quarterly report",
                "filters": {
                    "bucket": "documents",
                    "object_metadata": {"classification": "internal"},
                },
                "limit": 5,
                "candidate_limit": 20,
                "passages_per_result": 2,
                "synthesize": False,
                "exact_vector": True,
            },
        )
        assert ask.status_code == 200
        ask_body = ask.json()
        assert set(ask_body) == {
            "schema_version",
            "mode",
            "active_signals",
            "results",
            "providers",
            "generation_status",
            "answer",
        }
        assert ask_body["schema_version"] == 1
        assert ask_body["mode"] == "metadata"
        assert ask_body["active_signals"] == ["metadata"]
        assert ask_body["generation_status"] == "not_requested"
        assert ask_body["answer"] is None
        assert len(ask_body["results"]) == 1
        result = ask_body["results"][0]
        assert result["citation"]["bucket"] == "documents"
        assert result["citation"]["key"] == "quarterly-report.txt"
        assert result["citation"]["object_metadata"] == {
            "classification": "internal",
            "mime": "text/plain",
        }
        assert result["score"] > 0
        assert result["score_components"][0]["signal"] == "metadata"
        assert result["passages"] == []
        provider_states = {
            provider["component"]: provider["state"]
            for provider in ask_body["providers"]
        }
        assert provider_states == {
            "metadata": "succeeded",
            "keyword": "missing",
            "vector": "missing",
            "generation": "not_requested",
        }

        evaluation = client.post(
            "/v1/policies/evaluate",
            json={
                "bucket": "documents",
                "key": "archive.bin",
                "config": {
                    "policy": "simple",
                    "threshold": 10,
                    "allowed_tiers": ["hot", "warm"],
                },
            },
        )
        assert evaluation.status_code == 200
        evaluation_body = evaluation.json()
        access = evaluation_body["features"].pop("access")
        assert access["freshness"] == "missing"
        assert access["missing"] is True
        assert access["partial"] is True
        assert access["recency_seconds"] is None
        constraints = evaluation_body.pop("constraints")
        assert constraints["importance"] is None
        assert constraints["residency_active"] is False
        assert constraints["allowed_destination_tiers"] == ["hot", "warm"]
        assert evaluation_body == {
            "schema_version": 1,
            "bucket": "documents",
            "key": "archive.bin",
            "current_tier": "hot",
            "size": 11,
            "action": "move",
            "destination_tier": "warm",
            "reason": "large object -> warm tier",
            "features": {
                "schema_version": 1,
                "mime": {
                    "state": "missing",
                    "value": None,
                    "provenance": {
                        "source": "catalog_metadata",
                        "source_version": 1,
                        "content_sha256": None,
                        "details": {"reason": "mime_missing"},
                    },
                },
                "embeddings": [],
            },
        }


def test_policy_evaluation_exposes_fresh_feature_provenance(
    tmp_path: Path,
) -> None:
    catalog = Catalog()
    catalog.upsert(
        "documents",
        "archive.pdf",
        size=42,
        tier="warm",
    )
    content_sha256 = "a" * 64
    features = PolicyFeatures(
        mime=MimePolicyFeature(
            state=FeatureState.FRESH,
            value="application/pdf",
            provenance=PolicyFeatureProvenance(
                source="mime_detection",
                source_version=1,
                content_sha256=content_sha256,
                details={
                    "confidence": "high",
                    "detector": "libmagic",
                    "provenance": "content",
                },
            ),
        ),
        embeddings=(
            EmbeddingPolicyFeature(
                name="archive",
                query="historical archive material",
                state=FeatureState.FRESH,
                similarity=0.92,
                provenance=PolicyFeatureProvenance(
                    source="embedding_similarity",
                    source_version=1,
                    content_sha256=content_sha256,
                    details={
                        "aggregation": "max_passage_similarity",
                        "model_revision": "fixture-model-commit-a",
                    },
                ),
            ),
        ),
    )
    feature_loader = _StaticPolicyFeatureLoader(features)
    gateway = CogniStoreGateway(
        catalog,
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
        feature_loader=feature_loader,  # type: ignore[arg-type]
    )

    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/policies/evaluate",
            json={
                "bucket": "documents",
                "key": "archive.pdf",
                "config": {
                    "policy": "content",
                    "allowed_tiers": ["hot", "warm"],
                    "embedding_rules": [
                        {
                            "name": "archive",
                            "query": "historical archive material",
                            "minimum_similarity": 0.8,
                            "destination_tier": "hot",
                        }
                    ],
                },
            },
        )

    assert response.status_code == 200
    assert feature_loader.requests == (
        EmbeddingFeatureRequest(
            name="archive",
            query="historical archive material",
        ),
    )
    body = response.json()
    constraints = body.pop("constraints")
    assert constraints["residency_active"] is False
    assert constraints["importance"] is None
    assert body == {
        "schema_version": 1,
        "bucket": "documents",
        "key": "archive.pdf",
        "current_tier": "warm",
        "size": 42,
        "action": "move",
        "destination_tier": "hot",
        "reason": "embedding archive similarity 0.920000 >= 0.800000 -> hot",
        "features": {**features.to_dict(), "access": None},
    }


def test_policy_evaluation_missing_embedding_provider_fails_closed(
    tmp_path: Path,
) -> None:
    catalog = Catalog()
    catalog.upsert("documents", "archive.bin", size=11, tier="hot")
    gateway = CogniStoreGateway(
        catalog,
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
    )

    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/policies/evaluate",
            json={
                "bucket": "documents",
                "key": "archive.bin",
                "config": {
                    "policy": "content",
                    "threshold": 10,
                    "allowed_tiers": ["hot", "warm"],
                    "embedding_rules": [
                        {
                            "name": "archive",
                            "query": "historical archive material",
                            "minimum_similarity": 0.8,
                            "destination_tier": "warm",
                        }
                    ],
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "stay"
    assert body["destination_tier"] is None
    assert body["reason"] == "required policy features unavailable: embedding:archive=missing"
    embedding = body["features"]["embeddings"][0]
    assert {key: value for key, value in embedding.items() if key != "provenance"} == {
        "name": "archive",
        "query": "historical archive material",
        "state": "missing",
        "similarity": None,
    }
    provenance = embedding["provenance"]
    assert provenance["source"]
    assert provenance["source_version"] == 1
    assert provenance["content_sha256"] is None
    assert isinstance(provenance["details"], dict)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {
                "policy": "simple",
                "embedding_rules": [
                    {
                        "name": "archive",
                        "query": "historical archive material",
                        "minimum_similarity": 0.8,
                        "destination_tier": "hot",
                    }
                ],
            },
            "embedding rules require the content policy",
        ),
        (
            {
                "policy": "content",
                "embedding_rules": [
                    {
                        "name": "duplicate",
                        "query": "first",
                        "minimum_similarity": 0.8,
                        "destination_tier": "hot",
                    },
                    {
                        "name": "duplicate",
                        "query": "second",
                        "minimum_similarity": 0.7,
                        "destination_tier": "warm",
                    },
                ],
            },
            "embedding rule names must be unique",
        ),
        (
            {
                "policy": "content",
                "allowed_tiers": ["hot", "warm"],
                "embedding_rules": [
                    {
                        "name": "archive",
                        "query": "historical archive material",
                        "minimum_similarity": 0.8,
                        "destination_tier": "cold",
                    }
                ],
            },
            "embedding rule destination tier(s) must be allowed",
        ),
        (
            {
                "policy": "content",
                "embedding_rules": [
                    {
                        "name": "archive",
                        "query": "historical archive material",
                        "minimum_similarity": 1.01,
                        "destination_tier": "hot",
                    }
                ],
            },
            "Input should be less than or equal to 1",
        ),
    ],
    ids=("wrong-policy", "duplicate-names", "disallowed-tier", "bad-threshold"),
)
def test_policy_embedding_rule_configuration_is_strict(
    config: dict[str, object],
    message: str,
) -> None:
    with TestClient(create_app(CogniStoreGateway(Catalog(), {}))) as client:
        response = client.post(
            "/v1/policies/evaluate",
            json={"bucket": "documents", "key": "archive.bin", "config": config},
        )

    body = _assert_error(response, status_code=422, code="validation_error")
    assert message in json.dumps(body["error"]["details"])


class _RecordingSubmissionQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[JobEnvelope, str | None]] = []

    async def enqueue(
        self,
        job: JobEnvelope,
        *,
        message_id: str | None = None,
    ) -> EnqueueReceipt:
        self.enqueued.append((job, message_id))
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=len(self.enqueued),
        )


@dataclass
class _WorkerDelivery:
    job: JobEnvelope
    attempt: int = 1
    source_stream: str = "TEST_JOBS"
    source_published_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source_consumer: str = "test-api-worker"
    stream_sequence: int = 1
    consumer_sequence: int = 1
    headers: dict[str, str] = field(default_factory=dict)
    ack_count: int = 0
    nack_count: int = 0

    @property
    def raw_data(self) -> bytes:
        return self.job.to_bytes()

    async def ack(self) -> None:
        self.ack_count += 1

    async def nack(self, delay: float | None = None) -> None:
        self.nack_count += 1

    async def in_progress(self) -> None:
        return None


class _SingleDeliveryQueue:
    def __init__(self, delivery: _WorkerDelivery) -> None:
        self.delivery = delivery
        self.connected = False
        self.claimed = False

    async def connect(self) -> None:
        self.connected = True

    async def claim(self, timeout: float) -> _WorkerDelivery | None:
        if not self.claimed:
            self.claimed = True
            return self.delivery
        await asyncio.sleep(timeout)
        return None

    async def probe(self) -> QueueHealth:
        return QueueHealth(
            state=BusState.CONNECTED if self.connected else BusState.DISCONNECTED,
            ready=self.connected,
            jetstream=self.connected,
            stream="TEST_JOBS",
            consumer="test-api-worker",
        )

    async def close(self, *, graceful: bool = True) -> None:
        self.connected = False


def _run_tracked_job(job: JobEnvelope, catalog: Catalog) -> _WorkerDelivery:
    async def scenario() -> _WorkerDelivery:
        delivery = _WorkerDelivery(job)
        queue = _SingleDeliveryQueue(delivery)

        async def handler(delivered: JobEnvelope, _context) -> None:
            assert delivered == job

        worker = AsyncWorker(
            queue,  # type: ignore[arg-type]
            {job.job_type: handler},
            config=WorkerConfig(
                fetch_timeout=0.01,
                heartbeat_interval=0,
                shutdown_grace=0.2,
                settlement_timeout=0.2,
                stop_after_jobs=1,
                retry_jitter=0,
            ),
            audit_catalog=catalog,
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()
        assert report.completed == 1
        return delivery

    return asyncio.run(scenario())


def test_async_actions_return_202_and_worker_updates_status(tmp_path: Path) -> None:
    catalog = Catalog()
    submission_queue = _RecordingSubmissionQueue()
    gateway = CogniStoreGateway(
        catalog,
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
        queue=submission_queue,  # type: ignore[arg-type]
    )

    with TestClient(create_app(gateway)) as client:
        scan = client.post(
            "/v1/actions/catalog-scans",
            json={"tier": "hot", "bucket": "documents", "prefix": "reports/"},
        )
        assert scan.status_code == 202
        scan_body = scan.json()
        UUID(scan_body["job_id"])
        UUID(scan_body["correlation_id"])
        assert scan_body["job_type"] == CATALOG_SCAN_JOB
        assert scan_body["status"] == "queued"
        assert scan_body["attempt"] == 0
        assert scan_body["status_url"] == f'/v1/jobs/{scan_body["job_id"]}'
        assert scan.headers["Location"] == scan_body["status_url"]
        assert scan.headers["Retry-After"] == "1"
        queued_status = client.get(scan_body["status_url"])
        assert queued_status.status_code == 200
        assert queued_status.json() == scan_body

        policy_run = client.post(
            "/v1/actions/policy-runs",
            json={
                "bucket": "documents",
                "prefix": "reports/",
                "config": {
                    "policy": "content",
                    "allowed_tiers": ["hot", "warm"],
                    "embedding_rules": [
                        {
                            "name": "archive",
                            "query": "historical archive material",
                            "minimum_similarity": 0.8,
                            "destination_tier": "warm",
                        }
                    ],
                },
            },
        )
        assert policy_run.status_code == 202
        assert policy_run.json()["job_type"] == POLICY_RUN_JOB
        assert policy_run.json()["status"] == "queued"
        assert policy_run.headers["Location"] == policy_run.json()["status_url"]

    assert len(submission_queue.enqueued) == 2
    scan_job, scan_message_id = submission_queue.enqueued[0]
    policy_job, policy_message_id = submission_queue.enqueued[1]
    assert scan_message_id == scan_job.job_id
    assert policy_message_id == policy_job.job_id
    assert scan_job.schema_version == JOB_SCHEMA_VERSION_V1
    assert policy_job.schema_version == JOB_SCHEMA_VERSION_V2
    assert scan_job.metadata[STATUS_TRACKING_METADATA] == "1"
    assert policy_job.metadata[STATUS_TRACKING_METADATA] == "1"
    assert policy_job.payload["embedding_rules"] == [
        {
            "name": "archive",
            "query": "historical archive material",
            "minimum_similarity": 0.8,
            "destination_tier": "warm",
        }
    ]

    delivery = _run_tracked_job(scan_job, catalog)
    assert delivery.ack_count == 1
    assert delivery.nack_count == 0

    events = catalog.list_audit_events(AuditQuery(job_id=scan_job.job_id))
    event_types = {event.event_type for event in events}
    assert event_types == {
        AuditEventType.JOB_QUEUED.value,
        AuditEventType.JOB_STARTED.value,
        AuditEventType.JOB_SUCCEEDED.value,
    }
    for event_type in (
        AuditEventType.JOB_STARTED.value,
        AuditEventType.JOB_SUCCEEDED.value,
    ):
        event = next(item for item in events if item.event_type == event_type)
        assert event.details["job_type"] == CATALOG_SCAN_JOB
        assert event.details["attempt"] == 1
        assert event.details["cumulative_attempt"] == 1

    with TestClient(create_app(gateway)) as client:
        completed_status = client.get(scan_body["status_url"])
    assert completed_status.status_code == 200
    assert completed_status.json()["status"] == "succeeded"
    assert completed_status.json()["attempt"] == 1


def test_job_status_uses_latest_transition_and_clears_stale_failure() -> None:
    catalog = Catalog()
    gateway = CogniStoreGateway(catalog, {})
    job_id = "10000000-0000-4000-8000-000000000039"
    context = AuditContext(
        correlation_id="20000000-0000-4000-8000-000000000039",
        actor_type="worker",
        actor_id="test-worker",
        job_id=job_id,
    )

    def append(
        event_type: AuditEventType,
        outcome: AuditOutcome,
        occurred_at: str,
        details: dict[str, object],
    ) -> AuditEvent:
        return catalog.append_audit_event(
            AuditEvent.create(
                event_type,
                outcome,
                context,
                occurred_at=occurred_at,
                recorded_at=occurred_at,
                details={"job_type": CATALOG_SCAN_JOB, **details},
            )
        )

    append(
        AuditEventType.JOB_QUEUED,
        AuditOutcome.REQUESTED,
        "2030-01-01T00:00:00Z",
        {},
    )
    append(
        AuditEventType.JOB_FAILURE,
        AuditOutcome.FAILED,
        "2030-01-01T00:00:01Z",
        {
            "attempt": 1,
            "cumulative_attempt": 1,
            "retryable": True,
            "exception_type": "builtins.TimeoutError",
        },
    )
    append(
        AuditEventType.JOB_RETRY,
        AuditOutcome.RETRYING,
        "2030-01-01T00:00:02Z",
        {"attempt": 1, "cumulative_attempt": 1, "next_attempt": 2},
    )
    append(
        AuditEventType.JOB_STARTED,
        AuditOutcome.STARTED,
        "2030-01-01T00:00:03Z",
        {"attempt": 2, "cumulative_attempt": 2},
    )

    with TestClient(create_app(gateway)) as client:
        running = client.get(f"/v1/jobs/{job_id}")
    assert running.status_code == 200
    assert running.json()["status"] == "running"
    assert running.json()["attempt"] == 2
    assert running.json()["retryable"] is None
    assert running.json()["error_type"] is None
    assert running.json()["updated_at"] == "2030-01-01T00:00:03.000000Z"

    append(
        AuditEventType.JOB_SUCCEEDED,
        AuditOutcome.SUCCEEDED,
        "2030-01-01T00:00:04Z",
        {"attempt": 2, "cumulative_attempt": 2},
    )

    with TestClient(create_app(gateway)) as client:
        succeeded = client.get(f"/v1/jobs/{job_id}")
    assert succeeded.status_code == 200
    assert succeeded.json()["status"] == "succeeded"
    assert succeeded.json()["attempt"] == 2
    assert succeeded.json()["retryable"] is None
    assert succeeded.json()["error_type"] is None
    assert succeeded.json()["updated_at"] == "2030-01-01T00:00:04.000000Z"


class _RaisingGateway:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def get_catalog_object(self, bucket: str, key: str):
        raise self.error


def test_backend_value_error_is_sanitized_as_internal_failure() -> None:
    secret = "fake-api-key=super-secret-value"
    gateway = _RaisingGateway(ValueError(secret))

    with TestClient(
        create_app(gateway),  # type: ignore[arg-type]
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/v1/catalog/objects/documents/failing")

    body = _assert_error(
        response,
        status_code=500,
        code="backend_failure",
    )
    assert body["error"]["message"] == "A backend operation failed"
    assert secret not in response.text


@pytest.mark.parametrize(
    ("error", "status_code", "code", "retryable"),
    [
        (
            ResourceNotFoundError("catalog object", "documents/missing"),
            404,
            "resource_not_found",
            False,
        ),
        (FileExistsError("already exists"), 409, "resource_conflict", False),
        (
            QueueSaturatedError("TEST_JOBS", "stream is full"),
            503,
            "queue_saturated",
            True,
        ),
        (TimeoutError("database timeout"), 503, "backend_unavailable", True),
        (RuntimeError("secret backend diagnostic"), 500, "backend_failure", False),
    ],
)
def test_backend_exceptions_map_to_stable_public_errors(
    error: Exception,
    status_code: int,
    code: str,
    retryable: bool,
) -> None:
    with TestClient(
        create_app(_RaisingGateway(error)),  # type: ignore[arg-type]
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/v1/catalog/objects/documents/missing")

    body = _assert_error(
        response,
        status_code=status_code,
        code=code,
        retryable=retryable,
    )
    if type(error) is RuntimeError:
        assert "secret backend diagnostic" not in json.dumps(body)
    if status_code == 503:
        assert response.headers["Retry-After"] == "1"


def test_authorization_hook_protects_v1_but_not_health() -> None:
    calls: list[str | None] = []

    def require_api_key(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        calls.append(x_api_key)
        if x_api_key != "test-secret":
            raise HTTPException(status_code=401, detail="unauthorized")

    app = create_app(
        CogniStoreGateway(Catalog(), {}),
        authorization_hook=require_api_key,
    )
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "api_version": 1}
        assert calls == []

        denied = client.get("/v1/catalog/objects", params={"bucket": "documents"})
        _assert_error(denied, status_code=401, code="http_error")

        allowed = client.get(
            "/v1/catalog/objects",
            params={"bucket": "documents"},
            headers={"X-API-Key": "test-secret"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["items"] == []

    assert calls == [None, "test-secret"]


@pytest.mark.parametrize("failure", [None, "catalog", "queue", "timeout"])
def test_readiness_probes_dependencies_without_changing_liveness(failure, monkeypatch) -> None:
    class Queue:
        async def probe(self):
            if failure == "timeout":
                raise asyncio.TimeoutError("private broker address")
            return SimpleNamespace(ready=failure != "queue")

    catalog = Catalog()
    if failure == "catalog":
        def failed_catalog(_name):
            raise RuntimeError("private database credentials")
        monkeypatch.setattr(catalog, "get_tier", failed_catalog)

    def protected():
        raise HTTPException(status_code=401)

    gateway = CogniStoreGateway(catalog, {}, queue=Queue())
    with TestClient(create_app(gateway, authorization_hook=protected)) as client:
        response = client.get("/readyz")
        assert response.status_code == (200 if failure is None else 503)
        assert "private" not in response.text
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/catalog/objects?bucket=docs").status_code == 401


def test_unconfigured_api_is_live_but_not_ready() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_timed_out_readiness_reuses_one_blocked_catalog_probe(monkeypatch) -> None:
    release = threading.Event()
    calls = []
    catalog = Catalog()

    def blocked_query(name):
        calls.append(name)
        assert release.wait(timeout=5)
        return None

    monkeypatch.setattr(catalog, "get_tier", blocked_query)
    gateway = CogniStoreGateway(catalog, {})

    async def scenario():
        try:
            for _ in range(3):
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(gateway.check_readiness(), timeout=0.01)
            assert calls == ["cognistore-readiness"]
        finally:
            release.set()
        assert await gateway.check_readiness()
        await gateway.shutdown()

    asyncio.run(scenario())


def test_openapi_contract_is_deterministic_and_checked_in() -> None:
    first = render_document()
    second = render_document()
    checked_contract = Path(__file__).parents[2] / "docs" / "openapi" / "v1.json"

    assert first == second
    assert first == checked_contract.read_text(encoding="utf-8")

    document = json.loads(first)
    assert document["openapi"] == "3.1.0"
    schemas = document["components"]["schemas"]
    assert set(schemas["PolicyFeaturesResponse"]["required"]) == {
        "schema_version",
        "mime",
        "embeddings",
    }
    assert set(schemas["MimePolicyFeatureResponse"]["required"]) == {
        "state",
        "value",
        "provenance",
    }
    assert set(schemas["EmbeddingPolicyFeatureResponse"]["required"]) == {
        "name",
        "query",
        "state",
        "similarity",
        "provenance",
    }
    assert set(schemas["PolicyFeatureProvenanceResponse"]["required"]) == {
        "source",
        "source_version",
        "content_sha256",
        "details",
    }
    operation_ids = sorted(
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    )
    assert operation_ids == sorted(
        [
            "ask",
            "listAuditEvents",
            "getAuditEvent",
            "exportAuditEvents",
            "verifyAuditIntegrity",
            "deleteObject",
            "evaluatePolicy",
            "getCatalogObject",
            "getHealth",
            "getReadiness",
            "getJobStatus",
            "getObject",
            "getPolicyDecision",
            "headObject",
            "listCatalogObjects",
            "listPolicyDecisions",
            "previewPolicyDecision",
            "putObject",
            "setObjectImportance",
            "submitCatalogScan",
            "submitPolicyRun",
        ]
    )
