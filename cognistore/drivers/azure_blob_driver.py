from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from io import BytesIO
from typing import Any, Dict, Generator, Iterator, NoReturn, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from .storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    DriverCapabilities,
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
    validate_object_generation,
)

_MAX_BLOCK_SIZE = 4000 * 1024 * 1024
_MAX_BLOCKS = 50_000
_BYTE_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class AzureBlobError(RuntimeError):
    """An Azure failure with safe diagnostics and no credential-bearing message.

    The SDK retries transient provider errors before this exception is raised.
    ``status_code`` and ``error_code`` remain available for caller policies.
    """

    def __init__(
        self, operation: str, *, status_code: Optional[int], error_code: Optional[str]
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = status_code is None or status_code in {408, 409, 429} or status_code >= 500
        details = []
        if status_code is not None:
            details.append(f"HTTP {status_code}")
        if error_code is not None:
            details.append(error_code)
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"Azure Blob {operation} failed{suffix}")


def _error_details(error: AzureError) -> tuple[Optional[int], Optional[str]]:
    status = getattr(error, "status_code", None)
    status = status if isinstance(status, int) and 100 <= status <= 599 else None
    code = getattr(error, "error_code", None)
    code = getattr(code, "value", code)
    # Do not interpolate arbitrary provider error text (which can contain a
    # signed URL or connection string) into exceptions used by job logging.
    if not isinstance(code, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", code) is None:
        code = None
    return status, code


def _normalized_endpoint(account_url: str) -> str:
    if not isinstance(account_url, str) or not account_url.strip():
        raise ValueError("Azure account_url must be a non-empty string")
    try:
        parsed = urlsplit(account_url.strip())
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        parsed.port  # Validate malformed ports without exposing the URL.
    except ValueError:
        raise ValueError(
            "Azure account_url must be an HTTP(S) URL without user credentials"
        ) from None
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


class _BlobReader:
    """Consume complete, bounded range responses; retain no open HTTP body."""

    def __init__(
        self,
        download: Callable[[int, int], bytes],
        offset: int,
        length: int,
        chunk_size: int,
    ) -> None:
        self._download = download
        self._offset = offset
        self._remaining = length
        self._chunk_size = chunk_size
        self._buffer = b""
        self._position = 0
        self.closed = False
        # Bind opening to the first conditional GET. Small objects remain
        # readable even if the current blob is replaced after context entry.
        self._fill()

    def _fill(self) -> None:
        self._buffer = b""
        self._position = 0
        if self._remaining:
            length = min(self._remaining, self._chunk_size)
            data = self._download(self._offset, length)
            if not isinstance(data, bytes) or len(data) != length:
                raise OSError("Azure Blob returned an incomplete range response")
            self._buffer = data
            self._offset += length
            self._remaining -= length

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed object reader")
        if not isinstance(size, int):
            raise TypeError("Object reader size must be an integer")
        if size == 0:
            return b""
        output = bytearray()
        while size < 0 or len(output) < size:
            if self._position == len(self._buffer):
                if not self._remaining:
                    break
                self._fill()
            available = len(self._buffer) - self._position
            take = available if size < 0 else min(available, size - len(output))
            output.extend(memoryview(self._buffer)[self._position : self._position + take])
            self._position += take
        return bytes(output)

    def close(self) -> None:
        self.closed = True
        self._buffer = b""
        self._remaining = 0


class AzureBlobDriver(StorageDriver):
    """Azure block-blob backend using sequential transfers and atomic commits.

    Failed uploads leave only uncommitted blocks for Azure's garbage collector.
    There is no safe per-upload abort operation: deleting the blob or committing
    a cleanup block list could destroy an existing or concurrent writer's data.
    Credentials belong to the SDK client and are never exposed in diagnostics.
    """

    capabilities = DriverCapabilities(
        range_reads=True,
        range_writes=False,
        atomic_no_overwrite=True,
        conditional_delete=True,
    )

    def __init__(
        self,
        account_url: Optional[str] = None,
        connection_string: Optional[str] = None,
        credential: Any = None,
        auto_create_container: bool = False,
        list_page_size: Optional[int] = None,
        chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
        client: Any = None,
    ) -> None:
        if connection_string is not None:
            if not isinstance(connection_string, str) or not connection_string.strip():
                raise ValueError("Azure connection_string must be a non-empty string")
            if account_url is not None or credential is not None:
                raise ValueError(
                    "Azure connection_string cannot be combined with account_url or credential"
                )
        if isinstance(credential, str) and not credential.strip():
            raise ValueError("Azure credential must not be empty")
        if not isinstance(auto_create_container, bool):
            raise ValueError("Azure auto_create_container must be a boolean")
        if list_page_size is not None and (
            isinstance(list_page_size, bool)
            or not isinstance(list_page_size, int)
            or not 1 <= list_page_size <= 5000
        ):
            raise ValueError("Azure list_page_size must be an integer from 1 to 5000")
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or not 1 <= chunk_size <= _MAX_BLOCK_SIZE
        ):
            raise ValueError("Azure chunk_size must be an integer from 1 byte through 4000 MiB")
        self.account_url = _normalized_endpoint(account_url) if account_url is not None else None
        self.auto_create_container = auto_create_container
        self.list_page_size = list_page_size
        self.chunk_size = chunk_size
        self._owned_credential: Any = None
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            if account_url is None and connection_string is None:
                raise ValueError("Azure requires account_url or connection_string")
            options: Dict[str, Any] = {
                "max_single_get_size": chunk_size,
                "max_chunk_get_size": chunk_size,
                "max_block_size": chunk_size,
                "logging_enable": False,
                "retry_total": 3,
            }
            try:
                if connection_string is not None:
                    self._client = BlobServiceClient.from_connection_string(
                        connection_string, **options
                    )
                elif account_url is not None:
                    has_sas = "sig" in parse_qs(urlsplit(account_url).query)
                    if credential is None and not has_sas:
                        self._owned_credential = DefaultAzureCredential()
                        credential = self._owned_credential
                    self._client = BlobServiceClient(account_url, credential=credential, **options)
            except (AzureError, ValueError, TypeError):
                if self._owned_credential is not None:
                    self._owned_credential.close()
                raise ValueError(
                    "Could not initialize Azure Blob client; check account and credential configuration"
                ) from None
        if self.account_url is None:
            endpoint = getattr(self._client, "url", None)
            if isinstance(endpoint, str):
                self.account_url = _normalized_endpoint(endpoint)

    def close(self) -> None:
        """Release owned connections and default identity credentials."""
        try:
            if self._owns_client:
                self._owns_client = False
                self._client.close()
        finally:
            if self._owned_credential is not None:
                credential, self._owned_credential = self._owned_credential, None
                credential.close()

    def same_backend(self, other: StorageDriver) -> bool:
        from .rotating import RotatingStorageDriver

        if isinstance(other, RotatingStorageDriver):
            return other.same_backend(self)
        if not isinstance(other, AzureBlobDriver):
            return False
        return self._client is other._client or (
            self.account_url is not None and self.account_url == other.account_url
        )

    @staticmethod
    def _raise_error(
        error: AzureError,
        operation: str,
        bucket: str,
        key: str = "",
        *,
        exclusive: bool = False,
        conditional: bool = False,
    ) -> NoReturn:
        status, code = _error_details(error)
        if status == 404 or code in {"BlobNotFound", "ContainerNotFound", "ResourceNotFound"}:
            raise FileNotFoundError(f"Object not found: {bucket}/{key}") from None
        condition_failed = code in {"ConditionNotMet", "TargetConditionNotMet"} or (
            status == 412 and code is None
        )
        if exclusive and (condition_failed or code == "BlobAlreadyExists"):
            raise FileExistsError(f"Object already exists: {bucket}/{key}") from None
        if conditional and condition_failed:
            raise ObjectGenerationMismatchError(
                f"Object generation changed: {bucket}/{key}"
            ) from None
        if status in {401, 403} or isinstance(error, ClientAuthenticationError):
            raise PermissionError(f"Azure Blob {operation} was not authorized") from None
        raise AzureBlobError(operation, status_code=status, error_code=code) from None

    def _write_request(
        self,
        operation: str,
        bucket: str,
        key: str,
        request: Callable[[], Any],
        *,
        exclusive: bool = False,
    ) -> None:
        try:
            request()
            return
        except AzureError as error:
            if not self.auto_create_container or _error_details(error)[1] != "ContainerNotFound":
                self._raise_error(error, operation, bucket, key, exclusive=exclusive)
        try:
            self._client.get_container_client(bucket).create_container(logging_enable=False)
        except AzureError as error:
            if _error_details(error)[1] != "ContainerAlreadyExists":
                self._raise_error(error, "create container", bucket)
        try:
            request()
        except AzureError as error:
            self._raise_error(error, operation, bucket, key, exclusive=exclusive)

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
            raise NotImplementedError("Azure Blob does not support in-place ranged writes")
        if not isinstance(data, bytes):
            raise TypeError("Object data must be bytes")
        if set(opts) - {"content_type", "metadata"}:
            raise TypeError("Azure put_object supports content_type and metadata options")
        self.put_object_stream(
            bucket, key, BytesIO(data), size=len(data), overwrite=overwrite, metadata=opts
        )

    @staticmethod
    def _stream_options(metadata: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping):
            raise ValueError("Object metadata must be a mapping")
        options: Dict[str, Any] = {}
        content_type = metadata.get("content_type")
        if content_type is not None:
            if not isinstance(content_type, str) or not content_type:
                raise ValueError("Object content_type metadata must be a string")
            options["content_settings"] = ContentSettings(content_type=content_type)
        user_metadata = metadata.get("metadata")
        if user_metadata is not None:
            if not isinstance(user_metadata, Mapping) or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in user_metadata.items()
            ):
                raise ValueError("Object user metadata must contain string values")
            options["metadata"] = dict(user_metadata)
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
        buffer = bytearray(size)
        offset = 0
        while offset < size:
            chunk = self._read_bytes(source, size - offset)
            if not chunk:
                raise ValueError("Object stream ended before its declared size")
            buffer[offset : offset + len(chunk)] = chunk
            offset += len(chunk)
        return bytes(buffer)

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
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Object stream size must be a non-negative integer")
        if not isinstance(overwrite, bool):
            raise ValueError("Object overwrite must be a boolean")
        if size > self.chunk_size * _MAX_BLOCKS:
            raise ValueError(
                "Object requires more than 50,000 blocks; configure a larger Azure chunk_size"
            )
        options = self._stream_options(metadata)
        blob = self._client.get_blob_client(container=bucket, blob=key)
        upload_id = uuid.uuid4().hex
        blocks: list[str] = []
        remaining = size
        while remaining:
            chunk = self._read_exact_chunk(source, min(remaining, self.chunk_size))
            # The SDK base64-encodes these 40-byte IDs. Their size is constant
            # across uploads, and a random namespace prevents cross-upload reuse.
            block_id = f"{upload_id}{len(blocks):08d}"
            self._write_request(
                "stage block",
                bucket,
                key,
                lambda: blob.stage_block(
                    block_id=block_id, data=chunk, length=len(chunk), logging_enable=False
                ),
            )
            blocks.append(block_id)
            remaining -= len(chunk)
        if self._read_bytes(source, 1):
            raise ValueError(f"Object stream contains more than declared size {size}")
        if not overwrite:
            options["match_condition"] = MatchConditions.IfMissing
        # Publication happens exactly once, only after source validation. On
        # failure/cancellation we deliberately leave uncommitted blocks alone:
        # Azure expires them after a week without block writes on this blob.
        self._write_request(
            "commit blocks",
            bucket,
            key,
            lambda: blob.commit_block_list(block_list=blocks, logging_enable=False, **options),
            exclusive=not overwrite,
        )
        return size

    def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
        try:
            props = self._client.get_blob_client(container=bucket, blob=key).get_blob_properties(
                logging_enable=False
            )
        except AzureError as error:
            self._raise_error(error, "stat", bucket, key)
        etag = props.etag
        if not isinstance(etag, str) or not etag:
            raise RuntimeError(f"Azure Blob returned no ETag for {bucket}/{key}")
        result: Dict[str, Any] = {
            "size": int(props.size),
            "mtime": float(props.last_modified.timestamp()),
            "etag": etag,
            "generation": etag,
            "metadata": dict(props.metadata or {}),
        }
        settings = getattr(props, "content_settings", None)
        if settings is not None and settings.content_type is not None:
            result["content_type"] = settings.content_type
        version_id = getattr(props, "version_id", None)
        if version_id:
            result["version_id"] = version_id
        return result

    @staticmethod
    def _range_bounds(range: Optional[str], size: int) -> tuple[int, int]:
        if range is None:
            return 0, size
        match = _BYTE_RANGE.fullmatch(range) if isinstance(range, str) else None
        if match is None or not any(match.groups()):
            raise ValueError("Object range must be a single HTTP byte range")
        first, last = match.groups()
        if not first:
            suffix = int(last)
            if suffix == 0 or size == 0:
                raise ValueError("Object byte range is unsatisfiable")
            length = min(suffix, size)
            return size - length, length
        start = int(first)
        end = int(last) if last else size - 1
        if start >= size or end < start:
            raise ValueError("Object byte range is unsatisfiable")
        return start, min(end, size - 1) - start + 1

    @contextmanager
    def _open_reader(
        self, bucket: str, key: str, range: Optional[str], generation: Optional[str] = None
    ) -> Iterator[ReadableStream]:
        props = self.stat_object(bucket, key)
        if generation is not None and props["generation"] != generation:
            raise ObjectGenerationMismatchError(f"Object generation changed: {bucket}/{key}")
        offset, length = self._range_bounds(range, props["size"])
        blob = self._client.get_blob_client(container=bucket, blob=key)

        def download(start: int, count: int) -> bytes:
            try:
                # A bounded HTTP range avoids the SDK's eager default 32 MiB
                # initial fetch, even with an externally configured client.
                return blob.download_blob(
                    offset=start,
                    length=count,
                    etag=props["etag"],
                    match_condition=MatchConditions.IfNotModified,
                    max_concurrency=1,
                    logging_enable=False,
                ).readall()
            except AzureError as error:
                self._raise_error(error, "read", bucket, key, conditional=True)

        reader = _BlobReader(download, offset, length, self.chunk_size)
        try:
            yield reader
        finally:
            reader.close()

    @contextmanager
    def open_object_reader(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> Iterator[ReadableStream]:
        with self._open_reader(bucket, key, range) as reader:
            yield reader

    @contextmanager
    def open_object_reader_if_generation(
        self, bucket: str, key: str, generation: str, range: Optional[str] = None
    ) -> Iterator[ReadableStream]:
        generation = validate_object_generation(generation)
        with self._open_reader(bucket, key, range, generation) as reader:
            yield reader

    def get_object(self, bucket: str, key: str, range: Optional[str] = None) -> bytes:
        with self.open_object_reader(bucket, key, range) as reader:
            return reader.read()

    def delete_object(self, bucket: str, key: str) -> None:
        try:
            self._client.get_blob_client(container=bucket, blob=key).delete_blob(
                logging_enable=False
            )
        except AzureError as error:
            status, code = _error_details(error)
            if status != 404 and code not in {
                "BlobNotFound",
                "ContainerNotFound",
                "ResourceNotFound",
            }:
                self._raise_error(error, "delete", bucket, key)

    def delete_object_if_generation(self, bucket: str, key: str, generation: str) -> bool:
        generation = validate_object_generation(generation)
        try:
            self._client.get_blob_client(container=bucket, blob=key).delete_blob(
                etag=generation,
                match_condition=MatchConditions.IfNotModified,
                logging_enable=False,
            )
        except AzureError as error:
            status, code = _error_details(error)
            if status == 404 or code in {"BlobNotFound", "ContainerNotFound", "ResourceNotFound"}:
                return False
            if status == 412 or code in {"ConditionNotMet", "TargetConditionNotMet"}:
                # Azure may evaluate If-Match before existence and return 412
                # for an absent blob. A post-failure stat can establish absence
                # without ever deleting a newly created generation.
                try:
                    self.stat_object(bucket, key)
                except FileNotFoundError:
                    return False
            self._raise_error(error, "delete", bucket, key, conditional=True)
        return True

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        options: Dict[str, Any] = {"name_starts_with": prefix, "logging_enable": False}
        if self.list_page_size is not None:
            options["results_per_page"] = self.list_page_size
        try:
            # ItemPaged follows every continuation token; results_per_page is
            # a request size, never a cap on the total number of yielded keys.
            for blob in self._client.get_container_client(bucket).list_blobs(**options):
                yield blob.name
        except AzureError as error:
            status, code = _error_details(error)
            if status != 404 and code not in {"ContainerNotFound", "ResourceNotFound"}:
                self._raise_error(error, "list", bucket)
