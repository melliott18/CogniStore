from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from tempfile import SpooledTemporaryFile
from typing import Any, Dict, Generator, Iterator, Optional, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.session import Session as BotocoreSession

from .storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    DriverCapabilities,
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
    StorageListingPage,
    decode_listing_cursor,
    encode_listing_cursor,
    validate_listing_request,
    validate_object_generation,
)

_NOT_FOUND_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NotFound"})
_MISSING_BUCKET_CODES = frozenset({"NoSuchBucket", "NotFound"})
_CONDITIONAL_CONFLICT_CODE = "ConditionalRequestConflict"
_PRECONDITION_FAILED_CODE = "PreconditionFailed"
_MAX_CONDITIONAL_PUT_ATTEMPTS = 3
_MIN_MULTIPART_CHUNK_SIZE = 5 * 1024 * 1024
_MAX_MULTIPART_CHUNK_SIZE = 5 * 1024 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000
_MAX_SINGLE_PUT_SIZE = 5 * 1024 * 1024 * 1024


class MultipartUploadCleanupError(RuntimeError):
    """Raised when an incomplete multipart upload cannot be aborted."""


class _WritableStream(Protocol):
    def write(self, data: bytes, /) -> int: ...


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _error_status(error: ClientError) -> Optional[int]:
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def _is_not_found(error: ClientError) -> bool:
    return _error_status(error) == 404 or _error_code(error) in _NOT_FOUND_CODES


def _is_missing_bucket(error: ClientError) -> bool:
    return _error_code(error) in _MISSING_BUCKET_CODES


def _is_precondition_failed(error: ClientError) -> bool:
    return (
        _error_status(error) == 412
        or _error_code(error) == _PRECONDITION_FAILED_CODE
    )


def _is_conditional_conflict(error: ClientError) -> bool:
    return (
        _error_status(error) == 409
        or _error_code(error) == _CONDITIONAL_CONFLICT_CODE
    )


def _normalized_endpoint(endpoint_url: Optional[str]) -> Optional[str]:
    if endpoint_url is None:
        return None
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise ValueError("S3 endpoint URL must be a non-empty string")

    parsed = urlsplit(endpoint_url.strip())
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("S3 endpoint URL must not contain credentials")
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("S3 endpoint URL must include a scheme and host")

    path = parsed.path.rstrip("/")
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


class S3Driver(StorageDriver):
    """AWS S3 and MinIO-compatible implementation of ``StorageDriver``.

    Credentials are handed directly to a boto3 session and are never retained
    as public driver attributes.  When no explicit credentials or profile are
    supplied, boto3's normal credential provider chain is used.
    """

    capabilities = DriverCapabilities(
        range_reads=True,
        range_writes=False,
        atomic_no_overwrite=True,
        conditional_delete=True,
    )

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        region_name: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        session_token: Optional[str] = None,
        profile_name: Optional[str] = None,
        addressing_style: str = "auto",
        auto_create_bucket: bool = False,
        list_page_size: Optional[int] = None,
        client: Any = None,
        chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
        multipart_threshold: int = DEFAULT_STREAM_CHUNK_SIZE,
    ) -> None:
        self.endpoint_url = _normalized_endpoint(endpoint_url)
        self.addressing_style = addressing_style
        self.auto_create_bucket = auto_create_bucket
        self.list_page_size = list_page_size
        self.chunk_size = chunk_size
        self.multipart_threshold = multipart_threshold

        self._validate_configuration(
            region_name=region_name,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            profile_name=profile_name,
        )

        if client is not None:
            client_meta = getattr(client, "meta", None)
            client_region = getattr(client_meta, "region_name", None)
            self.region_name = region_name or client_region or "us-east-1"
            self.partition = getattr(client_meta, "partition", None) or (
                BotocoreSession().get_partition_for_region(self.region_name)
            )
            self._client = client
            return

        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            region_name=region_name,
            profile_name=profile_name,
        )
        self.region_name = region_name or session.region_name or "us-east-1"
        self.partition = session.get_partition_for_region(self.region_name)
        client_options: Dict[str, Any] = {
            "region_name": self.region_name,
            "config": Config(
                signature_version="s3v4",
                retries={"total_max_attempts": 4, "mode": "standard"},
                s3=cast(Any, {"addressing_style": addressing_style}),
                user_agent_extra="CogniStore",
            ),
        }
        if self.endpoint_url is not None:
            client_options["endpoint_url"] = self.endpoint_url
        self._client = session.client("s3", **client_options)

    def _validate_configuration(
        self,
        *,
        region_name: Optional[str],
        access_key: Optional[str],
        secret_key: Optional[str],
        session_token: Optional[str],
        profile_name: Optional[str],
    ) -> None:
        string_values = {
            "region name": region_name,
            "access key": access_key,
            "secret key": secret_key,
            "session token": session_token,
            "profile name": profile_name,
        }
        for label, value in string_values.items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"S3 {label} must be a non-empty string")

        has_access_key = access_key is not None
        has_secret_key = secret_key is not None
        if has_access_key != has_secret_key:
            raise ValueError("S3 access key and secret key must be configured together")
        if session_token is not None and not has_access_key:
            raise ValueError("S3 session token requires an access key and secret key")
        if profile_name is not None and has_access_key:
            raise ValueError("S3 profile and explicit credentials cannot be combined")

        if self.addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("S3 addressing style must be auto, path, or virtual")
        if not isinstance(self.auto_create_bucket, bool):
            raise ValueError("S3 auto_create_bucket must be a boolean")
        if self.list_page_size is not None and (
            isinstance(self.list_page_size, bool)
            or not isinstance(self.list_page_size, int)
            or not 1 <= self.list_page_size <= 1000
        ):
            raise ValueError("S3 list_page_size must be an integer from 1 to 1000")
        if (
            isinstance(self.chunk_size, bool)
            or not isinstance(self.chunk_size, int)
            or not _MIN_MULTIPART_CHUNK_SIZE
            <= self.chunk_size
            <= _MAX_MULTIPART_CHUNK_SIZE
        ):
            raise ValueError(
                "S3 chunk_size must be an integer from 5 MiB through 5 GiB"
            )
        if (
            isinstance(self.multipart_threshold, bool)
            or not isinstance(self.multipart_threshold, int)
            or self.multipart_threshold <= 0
        ):
            raise ValueError("S3 multipart_threshold must be a positive integer")

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        range: Optional[str] = None,
        overwrite: bool = True,
        **opts: Any,
    ) -> None:
        if range is not None:
            raise NotImplementedError("S3 does not support in-place ranged writes")

        request = dict(opts)
        request.update({"Bucket": bucket, "Key": key, "Body": data})
        if not overwrite:
            # S3 evaluates this condition atomically with PutObject.  A
            # separate HeadObject preflight would leave a write race.
            request["IfNoneMatch"] = "*"

        self._put_object_request(request, bucket, key, overwrite)

    def _put_object_request(
        self,
        request: Dict[str, Any],
        bucket: str,
        key: str,
        overwrite: bool,
        *,
        body_position: Optional[int] = None,
    ) -> None:
        """Send a prepared PutObject request, rewinding staged bodies on retry."""

        try:
            self._put_with_conditional_retry(
                request,
                bucket,
                key,
                overwrite,
                body_position=body_position,
            )
            return
        except ClientError as error:
            if not self.auto_create_bucket or not _is_missing_bucket(error):
                raise

        self._create_bucket(bucket)
        self._put_with_conditional_retry(
            request,
            bucket,
            key,
            overwrite,
            body_position=body_position,
        )

    def get_object(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> bytes:
        with self.open_object_reader(bucket, key, range=range) as body:
            return body.read()

    @contextmanager
    def open_object_reader(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> Iterator[ReadableStream]:
        """Open an S3 response body and always return its connection to the pool."""

        request: Dict[str, Any] = {"Bucket": bucket, "Key": key}
        if range is not None:
            request["Range"] = range

        with self._open_requested_reader(bucket, key, request) as body:
            yield body

    @contextmanager
    def open_object_reader_if_generation(
        self,
        bucket: str,
        key: str,
        generation: str,
        range: Optional[str] = None,
    ) -> Iterator[ReadableStream]:
        """Open one S3 generation using an atomic GET precondition."""

        generation = validate_object_generation(generation)
        current = self.stat_object(bucket, key)
        if current["generation"] != generation:
            raise ObjectGenerationMismatchError(
                f"Object generation changed: {bucket}/{key}"
            )
        request: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "IfMatch": current["etag"],
        }
        version_id = current.get("version_id")
        if isinstance(version_id, str) and version_id and version_id != "null":
            request["VersionId"] = version_id
        if range is not None:
            request["Range"] = range

        with self._open_requested_reader(bucket, key, request) as body:
            yield body

    @contextmanager
    def _open_requested_reader(
        self,
        bucket: str,
        key: str,
        request: Dict[str, Any],
    ) -> Iterator[ReadableStream]:
        """Issue one prepared GET and contain its response-body lifecycle."""

        try:
            response = self._client.get_object(**request)
        except ClientError as error:
            if _is_precondition_failed(error):
                raise ObjectGenerationMismatchError(
                    f"Object generation changed: {bucket}/{key}"
                ) from error
            if _is_not_found(error):
                raise FileNotFoundError(f"Object not found: {bucket}/{key}") from error
            raise

        body = response["Body"]
        try:
            yield cast(ReadableStream, body)
        finally:
            body.close()

    def put_object_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool = True,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Upload a stream with memory bounded by configured transfer sizes.

        Objects below ``multipart_threshold`` and within S3's single-put limit
        use a conditional ``PutObject``. Larger objects are uploaded as
        sequential parts so only one part is retained in memory. Any exception
        before a successful completion aborts the upload, including cancellation
        exceptions derived from ``BaseException``.
        """

        self._validate_stream_size(size)
        request_options = self._stream_request_options(metadata)
        if size == 0 or (
            size < self.multipart_threshold and size <= _MAX_SINGLE_PUT_SIZE
        ):
            return self._put_single_stream(
                bucket,
                key,
                source,
                size=size,
                overwrite=overwrite,
                request_options=request_options,
            )

        if size > self.chunk_size * _MAX_MULTIPART_PARTS:
            raise ValueError(
                "Object requires more than 10,000 multipart upload parts; "
                "configure a larger S3 chunk_size"
            )

        return self._put_multipart_stream(
            bucket,
            key,
            source,
            size=size,
            overwrite=overwrite,
            request_options=request_options,
        )

    def delete_object(self, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except ClientError as error:
            # DeleteObject is already idempotent for a missing key.  Some
            # compatible servers return 404 when the bucket itself is absent.
            if not _is_not_found(error):
                raise

    def delete_object_if_generation(
        self, bucket: str, key: str, generation: str
    ) -> bool:
        """Conditionally hide the current S3 generation.

        Omitting ``VersionId`` is deliberate. Versioned buckets create a
        delete marker, retaining the accepted source generation for crash-safe
        recovery; unversioned buckets still perform a physical delete.
        """

        try:
            current = self.stat_object(bucket, key)
        except FileNotFoundError:
            return False
        if current["generation"] != generation:
            raise ObjectGenerationMismatchError(
                f"Object generation changed: {bucket}/{key}"
            )

        request: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "IfMatch": current["etag"],
        }
        try:
            self._client.delete_object(**request)
        except ClientError as error:
            if _is_precondition_failed(error) or _is_conditional_conflict(error):
                raise ObjectGenerationMismatchError(
                    f"Object generation changed: {bucket}/{key}"
                ) from error
            if _is_not_found(error):
                return False
            raise
        return True

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        paginator = self._client.get_paginator("list_objects_v2")
        request: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if self.list_page_size is not None:
            request["PaginationConfig"] = {"PageSize": self.list_page_size}

        try:
            for page in paginator.paginate(**request):
                for item in page.get("Contents", []):
                    yield item["Key"]
        except ClientError as error:
            if not _is_not_found(error):
                raise

    def list_objects_page(
        self,
        bucket: str,
        prefix: str = "",
        *,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> StorageListingPage:
        """Request exactly one native ListObjectsV2 page, with SDK retries.

        Unlike the all-pages listing, even a missing bucket is an error: an
        inventory scan must not mistake an inaccessible namespace for empty.
        The caller controls page scheduling and durable checkpointing.
        """

        validate_listing_request(bucket, prefix, cursor, limit)
        backend = f"s3:{self.endpoint_url or self.partition}"
        token = decode_listing_cursor(cursor, backend, bucket, prefix) if cursor else None
        request: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": limit}
        if token is not None:
            request["ContinuationToken"] = token
        response = self._client.list_objects_v2(**request)
        contents = response.get("Contents", [])
        truncated = response.get("IsTruncated")
        if not isinstance(contents, list) or not isinstance(truncated, bool):
            raise RuntimeError("S3 returned a malformed inventory page")
        if len(contents) > limit or any(
            not isinstance(item, dict)
            or not isinstance(item.get("Key"), str)
            or not item["Key"].startswith(prefix)
            for item in contents
        ):
            raise RuntimeError("S3 returned invalid inventory keys")
        keys = tuple(item["Key"] for item in contents)
        next_cursor = None
        if truncated:
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token or next_token == token:
                raise RuntimeError("S3 returned no advancing inventory continuation token")
            next_cursor = encode_listing_cursor(backend, bucket, prefix, next_token)
        elif response.get("NextContinuationToken"):
            raise RuntimeError("S3 returned an inconsistent inventory continuation token")
        return StorageListingPage(keys, next_cursor)

    def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
        try:
            response = self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                raise FileNotFoundError(f"Object not found: {bucket}/{key}") from error
            raise

        metadata: Dict[str, Any] = {
            "size": int(response["ContentLength"]),
            "mtime": float(response["LastModified"].timestamp()),
        }
        if "ETag" in response:
            metadata["etag"] = response["ETag"]
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise RuntimeError(f"S3 returned no ETag for {bucket}/{key}")
        version_id = response.get("VersionId")
        generation = etag
        if isinstance(version_id, str) and version_id and version_id != "null":
            metadata["version_id"] = version_id
            generation = f"{version_id}:{etag}"
        metadata["generation"] = generation
        if "ContentType" in response:
            metadata["content_type"] = response["ContentType"]
        if "Metadata" in response:
            metadata["metadata"] = response["Metadata"]
        return metadata

    @staticmethod
    def _validate_stream_size(size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Object stream size must be a non-negative integer")

    @staticmethod
    def _stream_request_options(
        metadata: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Translate normalized driver metadata into safe S3 write headers."""

        if metadata is None:
            return {}

        options: Dict[str, Any] = {}
        content_type = metadata.get("content_type")
        if content_type is not None:
            if not isinstance(content_type, str) or not content_type:
                raise ValueError("Object content_type metadata must be a string")
            options["ContentType"] = content_type

        user_metadata = metadata.get("metadata")
        if user_metadata is not None:
            if not isinstance(user_metadata, Mapping) or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in user_metadata.items()
            ):
                raise ValueError("Object user metadata must contain string values")
            options["Metadata"] = dict(user_metadata)
        return options

    @staticmethod
    def _read_bytes(source: ReadableStream, size: int) -> bytes:
        data = source.read(size)
        if not isinstance(data, bytes):
            raise TypeError("Object stream read() must return bytes")
        if len(data) > size:
            raise ValueError("Object stream returned more bytes than requested")
        return data

    def _read_exact_chunk(self, source: ReadableStream, size: int) -> bytes:
        """Read exactly one bounded chunk, tolerating short stream reads."""

        buffer = bytearray(size)
        offset = 0
        while offset < size:
            chunk = self._read_bytes(source, size - offset)
            if not chunk:
                break
            buffer[offset : offset + len(chunk)] = chunk
            offset += len(chunk)
        if offset == size:
            return bytes(buffer)
        return bytes(memoryview(buffer)[:offset])

    def _copy_exact_stream(
        self,
        source: ReadableStream,
        destination: _WritableStream,
        size: int,
    ) -> None:
        remaining = size
        while remaining:
            chunk = self._read_exact_chunk(
                source,
                min(self.chunk_size, remaining),
            )
            if not chunk:
                raise ValueError(
                    f"Object stream ended after {size - remaining} bytes; "
                    f"expected {size}"
                )
            destination.write(chunk)
            remaining -= len(chunk)

        extra = self._read_bytes(source, 1)
        if extra:
            raise ValueError(f"Object stream contains more than declared size {size}")

    def _put_single_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool,
        request_options: Dict[str, Any],
    ) -> int:
        """Validate a single-put source using bounded memory and disk spillover."""

        with SpooledTemporaryFile(max_size=self.chunk_size, mode="w+b") as staged:
            self._copy_exact_stream(source, staged, size)
            staged.seek(0)
            request = dict(request_options)
            request.update(
                {
                    "Bucket": bucket,
                    "Key": key,
                    "Body": staged,
                    "ContentLength": size,
                }
            )
            if not overwrite:
                request["IfNoneMatch"] = "*"
            self._put_object_request(
                request,
                bucket,
                key,
                overwrite,
                body_position=0,
            )
        return size

    def _put_multipart_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool,
        request_options: Dict[str, Any],
    ) -> int:
        upload_id: Optional[str] = None
        completed = False
        try:
            create_request = dict(request_options)
            create_request.update({"Bucket": bucket, "Key": key})
            response = self._start_multipart_upload(create_request, bucket)
            upload_id = response.get("UploadId")
            if not isinstance(upload_id, str) or not upload_id:
                raise RuntimeError("S3 did not return a multipart upload ID")

            parts: list[Dict[str, Any]] = []
            remaining = size
            part_number = 1
            while remaining:
                expected = min(self.chunk_size, remaining)
                chunk = self._read_exact_chunk(source, expected)
                if len(chunk) != expected:
                    raise ValueError(
                        f"Object stream ended after {size - remaining + len(chunk)} "
                        f"bytes; expected {size}"
                    )
                upload_response = self._client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                    ContentLength=len(chunk),
                )
                etag = upload_response.get("ETag")
                if not isinstance(etag, str) or not etag:
                    raise RuntimeError(
                        f"S3 did not return an ETag for multipart part {part_number}"
                    )
                parts.append({"PartNumber": part_number, "ETag": etag})
                remaining -= len(chunk)
                part_number += 1

            extra = self._read_bytes(source, 1)
            if extra:
                raise ValueError(
                    f"Object stream contains more than declared size {size}"
                )

            complete_request: Dict[str, Any] = {
                "Bucket": bucket,
                "Key": key,
                "UploadId": upload_id,
                "MultipartUpload": {"Parts": parts},
            }
            if not overwrite:
                complete_request["IfNoneMatch"] = "*"
            try:
                self._client.complete_multipart_upload(**complete_request)
            except ClientError as error:
                if not overwrite and _is_precondition_failed(error):
                    raise FileExistsError(
                        f"Object already exists: {bucket}/{key}"
                    ) from error
                raise
            completed = True
            return size
        except BaseException as error:
            if not completed and upload_id is not None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except BaseException as cleanup_error:
                    raise MultipartUploadCleanupError(
                        f"Failed to abort multipart upload for {bucket}/{key} "
                        f"after {type(error).__name__}"
                    ) from cleanup_error
            raise

    def _start_multipart_upload(
        self, request: Dict[str, Any], bucket: str
    ) -> Dict[str, Any]:
        try:
            return cast(Dict[str, Any], self._client.create_multipart_upload(**request))
        except ClientError as error:
            if not self.auto_create_bucket or not _is_missing_bucket(error):
                raise

        self._create_bucket(bucket)
        return cast(Dict[str, Any], self._client.create_multipart_upload(**request))

    def same_backend(self, other: StorageDriver) -> bool:
        if not isinstance(other, S3Driver):
            return False

        if self.endpoint_url is not None or other.endpoint_url is not None:
            return self.endpoint_url == other.endpoint_url

        # AWS bucket names are global within a partition.  Regions and
        # credentials do not make bucket/key identity distinct, but the
        # commercial, GovCloud, and China partitions are separate namespaces.
        return self.partition == other.partition

    def _put_with_conditional_retry(
        self,
        request: Dict[str, Any],
        bucket: str,
        key: str,
        overwrite: bool,
        *,
        body_position: Optional[int] = None,
    ) -> None:
        for attempt in range(_MAX_CONDITIONAL_PUT_ATTEMPTS):
            if body_position is not None:
                request["Body"].seek(body_position)
            try:
                self._client.put_object(**request)
                return
            except ClientError as error:
                if overwrite:
                    raise
                if _is_precondition_failed(error):
                    raise FileExistsError(
                        f"Object already exists: {bucket}/{key}"
                    ) from error
                if (
                    not _is_conditional_conflict(error)
                    or attempt == _MAX_CONDITIONAL_PUT_ATTEMPTS - 1
                ):
                    raise

    def _create_bucket(self, bucket: str) -> None:
        request: Dict[str, Any] = {"Bucket": bucket}
        if self.region_name != "us-east-1":
            request["CreateBucketConfiguration"] = {
                "LocationConstraint": self.region_name
            }

        try:
            self._client.create_bucket(**request)
        except ClientError as error:
            # Another writer may have created the bucket between the failed
            # put and this request.  Ownership is required; a globally owned
            # bucket collision must still propagate.
            if _error_code(error) != "BucketAlreadyOwnedByYou":
                raise
