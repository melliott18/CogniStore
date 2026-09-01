"""Focused unit tests for S3 protocol details that conformance cannot observe."""

from __future__ import annotations

import asyncio
import tracemalloc
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import pytest
from botocore.exceptions import ClientError

from cognistore.drivers.s3_driver import MultipartUploadCleanupError, S3Driver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError

_MIB = 1024 * 1024


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _CloseTrackingBody(BytesIO):
    closed_by_driver = False

    def close(self) -> None:
        self.closed_by_driver = True
        super().close()


class _ReadTrackingStream(BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1, /) -> bytes:
        self.request_sizes.append(size)
        return super().read(size)


class _FakePaginator:
    def __init__(self, client: "_FakeS3Client") -> None:
        self.client = client

    def paginate(self, **kwargs: Any):
        self.client.paginate_calls.append(kwargs)
        yield {"Contents": [{"Key": "alpha/one"}], "IsTruncated": True}
        yield {
            "Contents": [{"Key": "alpha/two"}, {"Key": "alpha/three"}],
            "IsTruncated": False,
        }


class _FakeS3Client:
    """Small protocol fake supporting paginator and manual-list implementations."""

    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.head_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.paginate_calls: list[dict[str, Any]] = []
        self.paginator_operations: list[str] = []
        self.head_bucket_calls: list[dict[str, Any]] = []
        self.create_bucket_calls: list[dict[str, Any]] = []
        self.create_multipart_calls: list[dict[str, Any]] = []
        self.upload_part_calls: list[dict[str, Any]] = []
        self.complete_multipart_calls: list[dict[str, Any]] = []
        self.abort_multipart_calls: list[dict[str, Any]] = []
        self.put_error: ClientError | None = None
        self.put_errors: list[ClientError] = []
        self.get_error: ClientError | None = None
        self.head_error: ClientError | None = None
        self.delete_error: ClientError | None = None
        self.create_multipart_error: ClientError | None = None
        self.upload_part_error: BaseException | None = None
        self.complete_multipart_error: ClientError | None = None
        self.abort_multipart_error: Exception | None = None
        self.get_payload = b"payload"
        self.last_body: _CloseTrackingBody | None = None

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        recorded = dict(kwargs)
        body = recorded.get("Body")
        if hasattr(body, "read"):
            recorded["Body"] = body.read()
        self.put_calls.append(recorded)
        if self.put_errors:
            raise self.put_errors.pop(0)
        if self.put_error is not None:
            raise self.put_error
        return {"ETag": '"etag"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        if self.get_error is not None:
            raise self.get_error
        self.last_body = _CloseTrackingBody(self.get_payload)
        return {"Body": self.last_body}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls.append(kwargs)
        if self.head_error is not None:
            raise self.head_error
        return {
            "ContentLength": len(self.get_payload),
            "LastModified": datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            "ETag": '"etag"',
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {}

    def get_paginator(self, operation: str) -> _FakePaginator:
        self.paginator_operations.append(operation)
        assert operation == "list_objects_v2"
        return _FakePaginator(self)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls.append(kwargs)
        if "ContinuationToken" not in kwargs:
            return {
                "Contents": [{"Key": "alpha/one"}],
                "IsTruncated": True,
                "NextContinuationToken": "next-page",
            }
        assert kwargs["ContinuationToken"] == "next-page"
        return {
            "Contents": [
                {"Key": "alpha/two"},
                {"Key": "alpha/three"},
            ],
            "IsTruncated": False,
        }

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.head_bucket_calls.append(kwargs)
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.create_bucket_calls.append(kwargs)
        return {}

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.create_multipart_calls.append(kwargs)
        if self.create_multipart_error is not None:
            raise self.create_multipart_error
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self.upload_part_calls.append(kwargs)
        if self.upload_part_error is not None:
            raise self.upload_part_error
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.complete_multipart_calls.append(kwargs)
        if self.complete_multipart_error is not None:
            raise self.complete_multipart_error
        return {"ETag": '"complete"'}

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.abort_multipart_calls.append(kwargs)
        if self.abort_multipart_error is not None:
            raise self.abort_multipart_error
        return {}


class _InitiallyMissingBucketClient(_FakeS3Client):
    """Behave correctly with either preflight or optimistic bucket creation."""

    def __init__(self) -> None:
        super().__init__()
        self.bucket_exists = False

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.head_bucket_calls.append(kwargs)
        if not self.bucket_exists:
            raise _client_error("NoSuchBucket", 404, "HeadBucket")
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.create_bucket_calls.append(kwargs)
        self.bucket_exists = True
        return {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        if not self.bucket_exists:
            raise _client_error("NoSuchBucket", 404, "PutObject")
        return {"ETag": '"etag"'}

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.create_multipart_calls.append(kwargs)
        if not self.bucket_exists:
            raise _client_error("NoSuchBucket", 404, "CreateMultipartUpload")
        return {"UploadId": "upload-1"}


class _VersionedFakeS3Client(_FakeS3Client):
    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        response = super().head_object(**kwargs)
        response["VersionId"] = "version-1"
        return response


def test_s3_capabilities_match_supported_protocol_operations() -> None:
    driver = S3Driver(client=_FakeS3Client())

    assert driver.capabilities.range_reads is True
    assert driver.capabilities.range_writes is False
    assert driver.capabilities.atomic_no_overwrite is True


def test_ranged_get_forwards_http_range_and_reads_stream() -> None:
    client = _FakeS3Client()
    client.get_payload = b"23456"
    driver = S3Driver(client=client)

    result = driver.get_object("bucket", "key", range="bytes=2-6")

    assert result == b"23456"
    assert len(client.get_calls) == 1
    assert client.get_calls[0]["Bucket"] == "bucket"
    assert client.get_calls[0]["Key"] == "key"
    assert client.get_calls[0]["Range"] == "bytes=2-6"
    assert client.last_body is not None
    assert client.last_body.closed_by_driver is True


def test_generation_bound_get_uses_atomic_etag_precondition() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)
    generation = driver.object_generation("bucket", "key")

    with driver.open_object_reader_if_generation(
        "bucket",
        "key",
        generation,
    ) as source:
        assert source.read() == b"payload"

    assert client.get_calls == [
        {"Bucket": "bucket", "Key": "key", "IfMatch": '"etag"'}
    ]
    assert client.last_body is not None
    assert client.last_body.closed_by_driver is True


def test_generation_bound_get_pins_a_versioned_object() -> None:
    client = _VersionedFakeS3Client()
    driver = S3Driver(client=client)
    generation = driver.object_generation("bucket", "key")

    with driver.open_object_reader_if_generation(
        "bucket",
        "key",
        generation,
        range="bytes=1-3",
    ) as source:
        assert source.read() == b"payload"

    assert client.get_calls == [
        {
            "Bucket": "bucket",
            "Key": "key",
            "IfMatch": '"etag"',
            "VersionId": "version-1",
            "Range": "bytes=1-3",
        }
    ]


def test_generation_bound_get_rejects_stale_preflight_generation() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)

    with pytest.raises(ObjectGenerationMismatchError):
        with driver.open_object_reader_if_generation(
            "bucket",
            "key",
            "stale-generation",
        ):
            pytest.fail("a stale response body was yielded")

    assert client.get_calls == []


def test_generation_bound_get_maps_precondition_failure_to_mismatch() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)
    generation = driver.object_generation("bucket", "key")
    client.get_error = _client_error("PreconditionFailed", 412, "GetObject")

    with pytest.raises(ObjectGenerationMismatchError):
        with driver.open_object_reader_if_generation(
            "bucket",
            "key",
            generation,
        ):
            pytest.fail("a failed conditional GET yielded a response body")


def test_range_write_is_rejected_before_sending_a_request() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)

    with pytest.raises(NotImplementedError):
        driver.put_object("bucket", "key", b"replacement", range="bytes=0-2")

    assert client.put_calls == []


def test_no_overwrite_uses_atomic_conditional_put() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)

    driver.put_object("bucket", "key", b"new", overwrite=False)

    assert len(client.put_calls) == 1
    assert client.put_calls[0]["Bucket"] == "bucket"
    assert client.put_calls[0]["Key"] == "key"
    assert client.put_calls[0]["Body"] == b"new"
    assert client.put_calls[0]["IfNoneMatch"] == "*"


def test_conditional_put_precondition_failure_maps_to_file_exists() -> None:
    client = _FakeS3Client()
    client.put_error = _client_error("PreconditionFailed", 412, "PutObject")
    driver = S3Driver(client=client)

    with pytest.raises(FileExistsError):
        driver.put_object("bucket", "key", b"new", overwrite=False)


def test_conditional_put_retries_a_transient_conflict() -> None:
    client = _FakeS3Client()
    client.put_errors = [
        _client_error("ConditionalRequestConflict", 409, "PutObject")
    ]
    driver = S3Driver(client=client)

    driver.put_object("bucket", "key", b"new", overwrite=False)

    assert len(client.put_calls) == 2
    assert all(call["IfNoneMatch"] == "*" for call in client.put_calls)


def test_conditional_put_propagates_a_persistent_conflict_after_bounded_retries(
) -> None:
    client = _FakeS3Client()
    client.put_error = _client_error(
        "ConditionalRequestConflict", 409, "PutObject"
    )
    driver = S3Driver(client=client)

    with pytest.raises(ClientError) as captured:
        driver.put_object("bucket", "key", b"new", overwrite=False)

    assert captured.value.response["Error"]["Code"] == "ConditionalRequestConflict"
    assert len(client.put_calls) == 3


@pytest.mark.parametrize(
    ("method", "error_code", "operation"),
    [
        ("get_object", "NoSuchKey", "GetObject"),
        ("stat_object", "NotFound", "HeadObject"),
    ],
)
def test_missing_object_client_errors_map_to_file_not_found(
    method: str, error_code: str, operation: str
) -> None:
    client = _FakeS3Client()
    error = _client_error(error_code, 404, operation)
    if method == "get_object":
        client.get_error = error
    else:
        client.head_error = error
    driver = S3Driver(client=client)

    with pytest.raises(FileNotFoundError):
        getattr(driver, method)("bucket", "missing")


def test_delete_treats_missing_bucket_as_idempotent() -> None:
    client = _FakeS3Client()
    client.delete_error = _client_error("NoSuchBucket", 404, "DeleteObject")
    driver = S3Driver(client=client)

    driver.delete_object("missing-bucket", "missing-key")


def test_conditional_delete_uses_atomic_etag_precondition() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)
    generation = driver.object_generation("bucket", "key")

    assert driver.delete_object_if_generation("bucket", "key", generation)

    assert client.delete_calls == [
        {"Bucket": "bucket", "Key": "key", "IfMatch": '"etag"'}
    ]


def test_conditional_delete_maps_precondition_failure_to_generation_mismatch() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)
    generation = driver.object_generation("bucket", "key")
    client.delete_error = _client_error(
        "PreconditionFailed", 412, "DeleteObject"
    )

    with pytest.raises(ObjectGenerationMismatchError):
        driver.delete_object_if_generation("bucket", "key", generation)


def test_auto_create_bucket_recovers_a_missing_bucket_for_put() -> None:
    client = _InitiallyMissingBucketClient()
    driver = S3Driver(client=client, auto_create_bucket=True)

    driver.put_object("new-bucket", "key", b"payload")

    assert len(client.create_bucket_calls) == 1
    assert client.create_bucket_calls[0]["Bucket"] == "new-bucket"
    assert client.bucket_exists is True
    assert client.put_calls[-1]["Body"] == b"payload"


@pytest.mark.parametrize(
    ("region_name", "expected_configuration"),
    [
        ("us-east-1", None),
        ("us-west-2", {"LocationConstraint": "us-west-2"}),
    ],
)
def test_auto_create_bucket_uses_aws_region_rules(
    region_name: str, expected_configuration: dict[str, str] | None
) -> None:
    client = _InitiallyMissingBucketClient()
    driver = S3Driver(
        client=client,
        region_name=region_name,
        auto_create_bucket=True,
    )

    driver.put_object("new-bucket", "key", b"payload")

    request = client.create_bucket_calls[0]
    assert request["Bucket"] == "new-bucket"
    if expected_configuration is None:
        assert "CreateBucketConfiguration" not in request
    else:
        assert request["CreateBucketConfiguration"] == expected_configuration


def test_auto_create_bucket_does_not_mutate_during_reads() -> None:
    client = _FakeS3Client()
    client.get_error = _client_error("NoSuchBucket", 404, "GetObject")
    driver = S3Driver(client=client, auto_create_bucket=True)

    with pytest.raises(FileNotFoundError):
        driver.get_object("missing-bucket", "key")

    assert client.create_bucket_calls == []


def test_stat_normalizes_size_and_timestamp() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client)

    metadata = driver.stat_object("bucket", "key")

    assert metadata["size"] == len(client.get_payload)
    assert metadata["mtime"] == pytest.approx(1735787045.0)


def test_listing_uses_page_size_without_limiting_total_results() -> None:
    client = _FakeS3Client()
    driver = S3Driver(client=client, list_page_size=2)

    assert list(driver.list_objects("bucket", prefix="alpha/")) == [
        "alpha/one",
        "alpha/two",
        "alpha/three",
    ]

    if client.paginate_calls:
        assert len(client.paginate_calls) == 1
        call = client.paginate_calls[0]
        assert call["Bucket"] == "bucket"
        assert call["Prefix"] == "alpha/"
        page_size = call.get("PaginationConfig", {}).get(
            "PageSize", call.get("MaxKeys")
        )
        assert page_size == 2
    else:
        assert len(client.list_calls) == 2
        assert all(call["MaxKeys"] == 2 for call in client.list_calls)


def test_same_backend_uses_aws_partition_for_default_endpoints() -> None:
    commercial_east = S3Driver(
        client=_FakeS3Client(),
        region_name="us-east-1",
    )
    commercial_west = S3Driver(
        client=_FakeS3Client(),
        region_name="us-west-2",
    )
    govcloud = S3Driver(
        client=_FakeS3Client(),
        region_name="us-gov-west-1",
    )

    assert commercial_east.same_backend(commercial_west) is True
    assert commercial_east.same_backend(govcloud) is False


def test_same_backend_uses_normalized_custom_endpoint() -> None:
    first = S3Driver(
        client=_FakeS3Client(),
        endpoint_url="HTTP://MINIO.EXAMPLE:9000/",
    )
    alias = S3Driver(
        client=_FakeS3Client(),
        endpoint_url="http://minio.example:9000",
    )
    other = S3Driver(
        client=_FakeS3Client(),
        endpoint_url="http://other.example:9000",
    )

    assert first.same_backend(alias) is True
    assert first.same_backend(other) is False


@pytest.mark.parametrize(
    "chunk_size",
    [False, 0, 5 * _MIB - 1, 5 * 1024 * _MIB + 1],
)
def test_multipart_chunk_size_is_validated(chunk_size: Any) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        S3Driver(client=_FakeS3Client(), chunk_size=chunk_size)


@pytest.mark.parametrize("threshold", [False, 0, -1, "8 MiB"])
def test_multipart_threshold_is_validated(threshold: Any) -> None:
    with pytest.raises(ValueError, match="multipart_threshold"):
        S3Driver(client=_FakeS3Client(), multipart_threshold=threshold)


def test_stream_below_threshold_uses_bounded_conditional_put() -> None:
    client = _FakeS3Client()
    driver = S3Driver(
        client=client,
        chunk_size=5 * _MIB,
        multipart_threshold=10 * _MIB,
    )
    payload = b"small streaming payload"

    transferred = driver.put_object_stream(
        "bucket",
        "key",
        BytesIO(payload),
        size=len(payload),
        overwrite=False,
        metadata={"content_type": "text/plain", "metadata": {"owner": "test"}},
    )

    assert transferred == len(payload)
    assert client.create_multipart_calls == []
    assert client.put_calls == [
        {
            "Bucket": "bucket",
            "Key": "key",
            "Body": payload,
            "ContentType": "text/plain",
            "Metadata": {"owner": "test"},
            "ContentLength": len(payload),
            "IfNoneMatch": "*",
        }
    ]


def test_single_put_staging_stays_chunk_bounded_when_threshold_is_large() -> None:
    class GeneratedStream:
        def __init__(self, size: int) -> None:
            self.remaining = size

        def read(self, size: int = -1, /) -> bytes:
            amount = self.remaining if size < 0 else min(size, self.remaining)
            self.remaining -= amount
            return b"x" * amount

    class ConsumingClient(_FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.received = 0

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            body = kwargs["Body"]
            while chunk := body.read(64 * 1024):
                self.received += len(chunk)
            return {"ETag": '"etag"'}

    chunk_size = 5 * _MIB
    object_size = 6 * chunk_size
    client = ConsumingClient()
    driver = S3Driver(
        client=client,
        chunk_size=chunk_size,
        multipart_threshold=64 * _MIB,
    )

    tracemalloc.start()
    try:
        transferred = driver.put_object_stream(
            "bucket",
            "large-single-put.bin",
            GeneratedStream(object_size),
            size=object_size,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert transferred == object_size
    assert client.received == object_size
    assert peak < 6 * chunk_size


def test_short_stream_reads_do_not_accumulate_per_fragment_overhead() -> None:
    class OneByteReader:
        def __init__(self, size: int) -> None:
            self.remaining = size

        def read(self, size: int = -1, /) -> bytes:
            if self.remaining == 0:
                return b""
            self.remaining -= 1
            return b"x"

    logical_chunk_size = 256 * 1024
    driver = S3Driver(client=_FakeS3Client())

    tracemalloc.start()
    try:
        chunk = driver._read_exact_chunk(
            OneByteReader(logical_chunk_size), logical_chunk_size
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert chunk == b"x" * logical_chunk_size
    assert peak < 4 * logical_chunk_size


def test_stream_rejects_objects_requiring_more_than_ten_thousand_parts() -> None:
    client = _FakeS3Client()
    chunk_size = 5 * _MIB
    driver = S3Driver(client=client, chunk_size=chunk_size)

    with pytest.raises(ValueError, match="more than 10,000"):
        driver.put_object_stream(
            "bucket",
            "too-large.bin",
            BytesIO(),
            size=chunk_size * 10_000 + 1,
        )

    assert client.put_calls == []
    assert client.create_multipart_calls == []


def test_object_above_single_put_limit_uses_multipart_even_with_high_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeS3Client()
    driver = S3Driver(
        client=client,
        chunk_size=5 * _MIB,
        multipart_threshold=50 * 1024**4,
    )
    object_size = 5 * 1024**3 + 1
    observed: dict[str, Any] = {}

    def record_multipart(*args: Any, **kwargs: Any) -> int:
        observed.update(kwargs)
        return kwargs["size"]

    monkeypatch.setattr(driver, "_put_multipart_stream", record_multipart)

    transferred = driver.put_object_stream(
        "bucket",
        "multipart-required.bin",
        BytesIO(),
        size=object_size,
    )

    assert transferred == object_size
    assert observed["size"] == object_size


def test_staged_single_put_is_rewound_for_conditional_retry() -> None:
    client = _FakeS3Client()
    client.put_errors = [
        _client_error("ConditionalRequestConflict", 409, "PutObject")
    ]
    driver = S3Driver(
        client=client,
        chunk_size=5 * _MIB,
        multipart_threshold=10 * _MIB,
    )
    payload = b"retry the staged body"

    driver.put_object_stream(
        "bucket",
        "retry.bin",
        BytesIO(payload),
        size=len(payload),
        overwrite=False,
    )

    assert [call["Body"] for call in client.put_calls] == [payload, payload]


def test_stream_exactly_at_threshold_uses_multipart_upload() -> None:
    client = _FakeS3Client()
    driver = S3Driver(
        client=client,
        chunk_size=5 * _MIB,
        multipart_threshold=1,
    )

    transferred = driver.put_object_stream(
        "bucket",
        "threshold.bin",
        BytesIO(b"x"),
        size=1,
    )

    assert transferred == 1
    assert len(client.create_multipart_calls) == 1
    assert [call["ContentLength"] for call in client.upload_part_calls] == [1]
    assert len(client.complete_multipart_calls) == 1


def test_stream_above_threshold_uses_ordered_conditional_multipart_upload() -> None:
    client = _FakeS3Client()
    chunk_size = 5 * _MIB
    payload = b"a" * chunk_size + b"tail"
    driver = S3Driver(
        client=client,
        chunk_size=chunk_size,
        multipart_threshold=chunk_size,
    )
    source = _ReadTrackingStream(payload)

    transferred = driver.put_object_stream(
        "bucket",
        "large.bin",
        source,
        size=len(payload),
        overwrite=False,
        metadata={"content_type": "application/octet-stream"},
    )

    assert transferred == len(payload)
    assert client.create_multipart_calls == [
        {
            "Bucket": "bucket",
            "Key": "large.bin",
            "ContentType": "application/octet-stream",
        }
    ]
    assert [call["PartNumber"] for call in client.upload_part_calls] == [1, 2]
    assert [call["ContentLength"] for call in client.upload_part_calls] == [
        chunk_size,
        len(b"tail"),
    ]
    assert source.request_sizes == [chunk_size, len(b"tail"), 1]
    assert client.complete_multipart_calls == [
        {
            "Bucket": "bucket",
            "Key": "large.bin",
            "UploadId": "upload-1",
            "MultipartUpload": {
                "Parts": [
                    {"PartNumber": 1, "ETag": '"part-1"'},
                    {"PartNumber": 2, "ETag": '"part-2"'},
                ]
            },
            "IfNoneMatch": "*",
        }
    ]
    assert client.abort_multipart_calls == []


def test_cancelled_multipart_stream_is_aborted_and_cancellation_propagates() -> None:
    class InterruptingReader:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, size: int = -1, /) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"x" * size
            raise asyncio.CancelledError

    client = _FakeS3Client()
    chunk_size = 5 * _MIB
    driver = S3Driver(
        client=client,
        chunk_size=chunk_size,
        multipart_threshold=chunk_size,
    )

    with pytest.raises(asyncio.CancelledError):
        driver.put_object_stream(
            "bucket",
            "cancelled.bin",
            InterruptingReader(),
            size=chunk_size + 1,
        )

    assert len(client.upload_part_calls) == 1
    assert client.complete_multipart_calls == []
    assert client.abort_multipart_calls == [
        {"Bucket": "bucket", "Key": "cancelled.bin", "UploadId": "upload-1"}
    ]


def test_conditional_multipart_collision_aborts_and_maps_to_file_exists() -> None:
    client = _FakeS3Client()
    client.complete_multipart_error = _client_error(
        "PreconditionFailed", 412, "CompleteMultipartUpload"
    )
    chunk_size = 5 * _MIB
    driver = S3Driver(
        client=client,
        chunk_size=chunk_size,
        multipart_threshold=chunk_size,
    )

    with pytest.raises(FileExistsError):
        driver.put_object_stream(
            "bucket",
            "existing.bin",
            BytesIO(b"x" * chunk_size),
            size=chunk_size,
            overwrite=False,
        )

    assert len(client.complete_multipart_calls) == 1
    assert len(client.abort_multipart_calls) == 1


def test_abort_failure_is_visible_to_the_caller() -> None:
    client = _FakeS3Client()
    client.upload_part_error = RuntimeError("upload failed")
    client.abort_multipart_error = RuntimeError("abort failed")
    chunk_size = 5 * _MIB
    driver = S3Driver(
        client=client,
        chunk_size=chunk_size,
        multipart_threshold=chunk_size,
    )

    with pytest.raises(MultipartUploadCleanupError, match="Failed to abort"):
        driver.put_object_stream(
            "bucket",
            "cleanup.bin",
            BytesIO(b"x" * chunk_size),
            size=chunk_size,
        )


def test_auto_create_bucket_recovers_multipart_initiation() -> None:
    client = _InitiallyMissingBucketClient()
    chunk_size = 5 * _MIB
    driver = S3Driver(
        client=client,
        auto_create_bucket=True,
        chunk_size=chunk_size,
        multipart_threshold=chunk_size,
    )

    driver.put_object_stream(
        "new-bucket",
        "large.bin",
        BytesIO(b"x" * chunk_size),
        size=chunk_size,
    )

    assert len(client.create_bucket_calls) == 1
    assert len(client.create_multipart_calls) == 2
    assert len(client.complete_multipart_calls) == 1
