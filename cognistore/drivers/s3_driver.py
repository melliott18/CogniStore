from __future__ import annotations

from typing import Any, Dict, Generator, Optional
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.session import Session as BotocoreSession

from .storage_driver import DriverCapabilities, StorageDriver


_NOT_FOUND_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NotFound"})
_MISSING_BUCKET_CODES = frozenset({"NoSuchBucket", "NotFound"})
_CONDITIONAL_CONFLICT_CODE = "ConditionalRequestConflict"
_PRECONDITION_FAILED_CODE = "PreconditionFailed"
_MAX_CONDITIONAL_PUT_ATTEMPTS = 3


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
    ) -> None:
        self.endpoint_url = _normalized_endpoint(endpoint_url)
        self.addressing_style = addressing_style
        self.auto_create_bucket = auto_create_bucket
        self.list_page_size = list_page_size

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

        session_options: Dict[str, str] = {}
        if region_name is not None:
            session_options["region_name"] = region_name
        if profile_name is not None:
            session_options["profile_name"] = profile_name
        if access_key is not None:
            session_options["aws_access_key_id"] = access_key
            session_options["aws_secret_access_key"] = secret_key  # type: ignore[assignment]
        if session_token is not None:
            session_options["aws_session_token"] = session_token

        session = boto3.Session(**session_options)
        self.region_name = region_name or session.region_name or "us-east-1"
        self.partition = session.get_partition_for_region(self.region_name)
        client_options: Dict[str, Any] = {
            "region_name": self.region_name,
            "config": Config(
                signature_version="s3v4",
                retries={"total_max_attempts": 4, "mode": "standard"},
                s3={"addressing_style": addressing_style},
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

        try:
            self._put_with_conditional_retry(request, bucket, key, overwrite)
            return
        except ClientError as error:
            if not self.auto_create_bucket or not _is_missing_bucket(error):
                raise

        self._create_bucket(bucket)
        self._put_with_conditional_retry(request, bucket, key, overwrite)

    def get_object(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> bytes:
        request: Dict[str, Any] = {"Bucket": bucket, "Key": key}
        if range is not None:
            request["Range"] = range

        try:
            response = self._client.get_object(**request)
        except ClientError as error:
            if _is_not_found(error):
                raise FileNotFoundError(f"Object not found: {bucket}/{key}") from error
            raise

        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def delete_object(self, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except ClientError as error:
            # DeleteObject is already idempotent for a missing key.  Some
            # compatible servers return 404 when the bucket itself is absent.
            if not _is_not_found(error):
                raise

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
        if "ContentType" in response:
            metadata["content_type"] = response["ContentType"]
        if "Metadata" in response:
            metadata["metadata"] = response["Metadata"]
        return metadata

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
    ) -> None:
        for attempt in range(_MAX_CONDITIONAL_PUT_ATTEMPTS):
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
