"""Focused unit tests for S3 protocol details that conformance cannot observe."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import pytest
from botocore.exceptions import ClientError

from cognistore.drivers.s3_driver import S3Driver


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
        self.put_error: ClientError | None = None
        self.put_errors: list[ClientError] = []
        self.get_error: ClientError | None = None
        self.head_error: ClientError | None = None
        self.delete_error: ClientError | None = None
        self.get_payload = b"payload"
        self.last_body: _CloseTrackingBody | None = None

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
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
