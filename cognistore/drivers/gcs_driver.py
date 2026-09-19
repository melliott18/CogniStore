"""Google Cloud Storage JSON API driver with resumable, bounded-memory transfers."""

from __future__ import annotations

import base64
import errno
import hashlib
import logging
import math
import random
import re
import time
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from io import BytesIO
from tempfile import SpooledTemporaryFile
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlsplit

import google.auth
import httpx
from google.auth.transport.requests import Request as AuthRequest

from cognistore.encryption import (
    ca_bundle,
    require_at_rest,
    require_tls_url,
    require_verified_httpx,
    tls_context,
)

from .storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    DriverCapabilities,
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
    external_encryption_status,
    validate_object_generation,
)

_GCS_ENDPOINT = "https://storage.googleapis.com"
_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]
_UPLOAD_QUANTUM = 256 * 1024
_MAX_OBJECT_SIZE = 5 * 1024**4
_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_PRIVATE_TRANSPORT = ContextVar("cognistore_gcs_private_transport", default=False)


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Resumable session URLs are bearer credentials. httpx INFO logging and
        # httpcore DEBUG response headers would otherwise disclose them. The
        # context is thread/task-local and leaves other HTTP callers untouched.
        return not _PRIVATE_TRANSPORT.get()


for _logger_name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
    "urllib3.connectionpool",
    "google.auth.transport.requests",
):
    logging.getLogger(_logger_name).addFilter(_TransportLogFilter())


@contextmanager
def _private_transport() -> Iterator[None]:
    token = _PRIVATE_TRANSPORT.set(True)
    try:
        yield
    finally:
        _PRIVATE_TRANSPORT.reset(token)


class GCSTransientError(ConnectionError):
    """A transport failure or exhausted transient provider response."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"GCS request temporarily unavailable{suffix}")


class GCSUploadCleanupError(RuntimeError):
    """An upload failed and cancellation could not be confirmed."""


def _close_response(response: httpx.Response) -> None:
    with _private_transport():
        try:
            response.close()
        except httpx.HTTPError:
            raise GCSTransientError() from None
        finally:
            # HTTPX's bound stream points back to the response, whose request
            # retains the upload chunk. Break this cycle after closing rather
            # than retaining many chunks until a cyclic GC pass.
            response.stream = httpx.ByteStream(b"")


@contextmanager
def _response(response: httpx.Response) -> Iterator[httpx.Response]:
    try:
        yield response
    finally:
        _close_response(response)


class _SeekableStream(ReadableStream, Protocol):
    def seek(self, offset: int, /) -> int: ...


class _ResponseReader:
    def __init__(self, response: httpx.Response, chunk_size: int) -> None:
        # Raw iteration preserves stored compressed bytes and HTTP Range offsets.
        # MockTransport may supply an already buffered response.
        self._chunks = iter(
            [response.content]
            if response.is_stream_consumed
            else response.iter_raw(chunk_size=chunk_size)
        )
        self._buffer = bytearray()
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        while not self._eof and (size < 0 or len(self._buffer) < size):
            try:
                with _private_transport():
                    self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._eof = True
            except httpx.HTTPError:
                raise GCSTransientError() from None
        count = len(self._buffer) if size < 0 else min(size, len(self._buffer))
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data


def _endpoint(value: str | None) -> str:
    if value is None:
        return _GCS_ENDPOINT
    if not isinstance(value, str) or not value.strip():
        raise ValueError("GCS emulator_endpoint must be a non-empty URL")
    try:
        parsed = urlsplit(value.strip())
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
        )
        parsed.port  # Validate without ever echoing URL credentials.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("GCS emulator_endpoint must be an HTTP(S) origin without credentials")
    return value.strip().rstrip("/").lower()


def _positive_int(name: str, value: Any, *, minimum: int = 1) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"GCS {name} must be an integer of at least {minimum}")


class GCSDriver(StorageDriver):
    """An authenticated GCS backend, or an explicitly anonymous local emulator.

    Authentication uses Google Application Default Credentials unless a trusted
    credential file is configured. The transport is injectable for offline
    contract tests; injected clients remain owned by their caller.
    """

    capabilities = DriverCapabilities(
        range_reads=True,
        range_writes=False,
        atomic_no_overwrite=True,
        conditional_delete=True,
    )

    def __init__(
        self,
        project: str | None = None,
        credentials_file: str | None = None,
        emulator_endpoint: str | None = None,
        auto_create_bucket: bool = False,
        chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
        list_page_size: int | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        credentials: Any = None,
        kms_key_name: str | None = None,
    ) -> None:
        self.endpoint_url = _endpoint(emulator_endpoint)
        require_tls_url(self.endpoint_url, "GCS")
        self._emulator = emulator_endpoint is not None
        if kms_key_name is not None and (
            not isinstance(kms_key_name, str) or not kms_key_name.strip()
        ):
            raise ValueError("GCS kms_key_name must be a non-empty string")
        self._kms_key_name = kms_key_name
        require_at_rest("storage", mode=None if self._emulator else "provider-managed")
        if client is not None:
            require_verified_httpx(client)
        for name, value in (("project", project), ("credentials_file", credentials_file)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"GCS {name} must be a non-empty string")
        if not isinstance(auto_create_bucket, bool):
            raise ValueError("GCS auto_create_bucket must be a boolean")
        _positive_int("chunk_size", chunk_size)
        if chunk_size % _UPLOAD_QUANTUM:
            raise ValueError("GCS chunk_size must be a multiple of 262144 bytes")
        if list_page_size is not None:
            _positive_int("list_page_size", list_page_size)
            if list_page_size > 1000:
                raise ValueError("GCS list_page_size must not exceed 1000")
        _positive_int("max_retries", max_retries, minimum=0)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("GCS timeout must be a finite positive number")
        if credentials is not None and credentials_file is not None:
            raise ValueError("GCS must use only one credential strategy")
        if emulator_endpoint is not None and (
            credentials_file is not None or credentials is not None
        ):
            raise ValueError("GCS emulator configuration cannot include credentials")

        self._auth_request: Any = None
        self._credentials = credentials
        if emulator_endpoint is None:
            try:
                with _private_transport():
                    self._auth_request = AuthRequest()
                    self._auth_request.session.verify = ca_bundle()
                    self._auth_request.session.trust_env = False
                    if credentials is None:
                        if credentials_file is not None:
                            self._credentials, inferred_project = (
                                google.auth.load_credentials_from_file(
                                    credentials_file,
                                    scopes=_SCOPES,
                                )
                            )
                        else:
                            self._credentials, inferred_project = google.auth.default(
                                scopes=_SCOPES
                            )
                        project = project or inferred_project
            except Exception:
                if self._auth_request is not None:
                    self._auth_request.session.close()
                raise PermissionError("GCS credentials could not be loaded") from None
            try:
                for field in ("_token_uri", "_token_url", "_service_account_impersonation_url"):
                    token_url = getattr(self._credentials, field, None)
                    if isinstance(token_url, str):
                        require_tls_url(token_url, "GCS authentication")
            except BaseException:
                self._auth_request.session.close()
                raise
        if auto_create_bucket and not project:
            raise ValueError("GCS auto_create_bucket requires a project")
        self.project = project
        self.chunk_size = chunk_size
        self.list_page_size = list_page_size
        self.auto_create_bucket = auto_create_bucket
        self.max_retries = max_retries
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            timeout=timeout, verify=tls_context(), trust_env=False
        )

    def encryption_status(self) -> dict[str, Any]:
        if self._emulator:
            return external_encryption_status(key_configured=self._kms_key_name is not None)
        return {
            "source": "provider",
            "mode": "customer-managed" if self._kms_key_name else "provider-default",
            "key_configured": self._kms_key_name is not None,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        if self._auth_request is not None:
            self._auth_request.session.close()

    def same_backend(self, other: StorageDriver) -> bool:
        from .rotating import RotatingStorageDriver

        if isinstance(other, RotatingStorageDriver):
            return other.same_backend(self)
        # GCS bucket names are global; credential/project differences do not
        # distinguish physical storage. Emulator origins do.
        return isinstance(other, GCSDriver) and self.endpoint_url == other.endpoint_url

    def _url(self, bucket: str, key: str | None = None) -> str:
        url = f"{self.endpoint_url}/storage/v1/b/{quote(bucket, safe='')}/o"
        return url if key is None else f"{url}/{quote(key, safe='')}"

    def _send(
        self, method: str, url: str, *, stream: bool = False, **kwargs: Any
    ) -> httpx.Response:
        with _private_transport():
            try:
                request = self._client.build_request(method, url, timeout=self.timeout, **kwargs)
            except (httpx.InvalidURL, ValueError):
                raise OSError(errno.EINVAL, "GCS request could not be constructed") from None
            if self._credentials is not None:
                headers: dict[str, str] = {}
                try:
                    self._credentials.before_request(
                        self._auth_request, method, str(request.url), headers
                    )
                    request.headers.update(headers)
                except Exception:
                    raise PermissionError("GCS credentials could not be refreshed") from None
            try:
                return self._client.send(request, stream=stream, follow_redirects=False)
            except httpx.HTTPError:
                raise GCSTransientError() from None

    def _pause(self, attempt: int) -> None:
        time.sleep(random.uniform(0.5, 1.0) * min(2 ** min(attempt, 5), 30))

    def _request(
        self, method: str, url: str, *, retry: bool = True, **kwargs: Any
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1 if retry else 1):
            try:
                response = self._send(method, url, **kwargs)
            except GCSTransientError:
                if not retry or attempt == self.max_retries:
                    raise
            else:
                if response.status_code not in _TRANSIENT_STATUSES:
                    return response
                status = response.status_code
                _close_response(response)
                if not retry or attempt == self.max_retries:
                    raise GCSTransientError(status)
            self._pause(attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _check(response: httpx.Response, *, exclusive: bool = False) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        # Do not chain provider exceptions or include bodies, object names, URLs
        # or Location headers: all may contain credentials or sensitive data.
        if status == 404:
            raise FileNotFoundError("GCS object or bucket not found")
        if status in {401, 403}:
            raise PermissionError("GCS request denied")
        if status == 412:
            if exclusive:
                raise FileExistsError("GCS destination already exists")
            raise ObjectGenerationMismatchError("GCS object generation changed")
        if status == 416:
            raise ValueError("GCS byte range is not satisfiable")
        if status in _TRANSIENT_STATUSES:
            raise GCSTransientError(status)
        raise OSError(errno.EINVAL, f"GCS request failed (HTTP {status})")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        raise OSError(errno.EIO, "GCS returned invalid metadata") from None

    def get_object(self, bucket: str, key: str, range: str | None = None) -> bytes:
        with self.open_object_reader(bucket, key, range=range) as source:
            return source.read()

    @contextmanager
    def open_object_reader(
        self,
        bucket: str,
        key: str,
        range: str | None = None,
    ) -> Iterator[ReadableStream]:
        with self._open_reader(bucket, key, range=range) as source:
            yield source

    @contextmanager
    def open_object_reader_if_generation(
        self,
        bucket: str,
        key: str,
        generation: str,
        range: str | None = None,
    ) -> Iterator[ReadableStream]:
        generation = validate_object_generation(generation)
        with self._open_reader(bucket, key, range=range, generation=generation) as source:
            yield source

    @contextmanager
    def _open_reader(
        self,
        bucket: str,
        key: str,
        *,
        range: str | None,
        generation: str | None = None,
    ) -> Iterator[ReadableStream]:
        headers = {"Accept-Encoding": "gzip"}
        if range is not None:
            if not isinstance(range, str) or not re.fullmatch(
                r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", range
            ):
                raise ValueError("GCS range must be a single HTTP byte range")
            first, last = range[6:].split("-")
            if (first and last and int(first) > int(last)) or (not first and int(last) == 0):
                raise ValueError("GCS range must contain a valid byte interval")
            headers["Range"] = range
        params = {"alt": "media"}
        if generation is not None:
            params["ifGenerationMatch"] = generation
        # The GET starts before yielding. Reopening/chunking a mutable key later
        # would allow a concurrent replacement to leak into the accepted stream.
        response = self._request(
            "GET", self._url(bucket, key), params=params, headers=headers, stream=True
        )
        try:
            self._check(response)
            if range is not None and response.status_code != 206:
                raise OSError(errno.EIO, "GCS did not honor the requested byte range")
            stored_encoding = response.headers.get("X-Goog-Stored-Content-Encoding")
            if stored_encoding is not None and stored_encoding != response.headers.get(
                "Content-Encoding", "identity"
            ):
                raise OSError(errno.EIO, "GCS returned transcoded content instead of stored bytes")
            yield _ResponseReader(response, self.chunk_size)
        finally:
            with _private_transport():
                _close_response(response)

    def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
        with _response(self._request("GET", self._url(bucket, key))) as response:
            self._check(response)
            data = self._json(response)
        try:
            generation = validate_object_generation(data["generation"])
            raw_size = data["size"]
            if isinstance(raw_size, bool) or not isinstance(raw_size, (str, int)):
                raise ValueError
            size = int(raw_size)
            if size < 0:
                raise ValueError
            return {
                "size": size,
                "mtime": float(
                    datetime.fromisoformat(data["updated"].replace("Z", "+00:00")).timestamp()
                ),
                "generation": generation,
                "content_type": data.get("contentType", "application/octet-stream"),
                "etag": data.get("etag"),
                "metadata": data.get("metadata", {}),
                "md5_hash": data.get("md5Hash"),
                "crc32c": data.get("crc32c"),
            }
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise OSError(errno.EIO, "GCS returned invalid object metadata") from None

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        params: dict[str, Any] = {"prefix": prefix}
        if self.list_page_size is not None:
            params["maxResults"] = self.list_page_size
        seen: set[str] = set()
        while True:
            with _response(self._request("GET", self._url(bucket), params=params)) as response:
                if response.status_code == 404:
                    return
                self._check(response)
                data = self._json(response)
            items = data.get("items", [])
            if not isinstance(items, list):
                raise OSError(errno.EIO, "GCS returned an invalid object listing")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise OSError(errno.EIO, "GCS returned an invalid object listing")
                yield item["name"]
            token = data.get("nextPageToken")
            if not token:
                return
            if not isinstance(token, str) or token in seen:
                raise OSError(errno.EIO, "GCS returned an invalid pagination token")
            seen.add(token)
            params["pageToken"] = token

    def delete_object(self, bucket: str, key: str) -> None:
        # An unconditional delete is not safe to replay after an ambiguous
        # response: a replacement could have appeared at the same key.
        with _response(self._request("DELETE", self._url(bucket, key), retry=False)) as response:
            if response.status_code != 404:
                self._check(response)

    def delete_object_if_generation(self, bucket: str, key: str, generation: str) -> bool:
        generation = validate_object_generation(generation)
        with _response(
            self._request(
                "DELETE", self._url(bucket, key), params={"ifGenerationMatch": generation}
            )
        ) as response:
            if response.status_code == 404:
                return False
            self._check(response)
            return True

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        range: str | None = None,
        overwrite: bool = True,
        **opts: Any,
    ) -> None:
        if range is not None:
            raise NotImplementedError("GCS does not support ranged writes")
        self.put_object_stream(
            bucket, key, BytesIO(data), size=len(data), overwrite=overwrite, metadata=opts
        )

    @staticmethod
    def _metadata(key: str, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
        result: dict[str, Any] = {"name": key, "contentType": "application/octet-stream"}
        if metadata is None:
            return result
        content_type = metadata.get("content_type")
        if content_type is not None:
            if (
                not isinstance(content_type, str)
                or not content_type.strip()
                or any(c in content_type for c in "\r\n")
            ):
                raise ValueError("GCS content_type must be a non-empty single-line string")
            result["contentType"] = content_type
        custom = metadata.get("metadata")
        if custom is not None:
            if not isinstance(custom, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in custom.items()
            ):
                raise ValueError("GCS metadata must map strings to strings")
            result["metadata"] = dict(custom)
        return result

    def _create_bucket(self, bucket: str) -> None:
        with _response(
            self._request(
                "POST",
                f"{self.endpoint_url}/storage/v1/b",
                params={"project": self.project},
                json={"name": bucket},
                retry=False,
            )
        ) as response:
            if response.status_code != 409:
                self._check(response)

    def _start_upload(
        self, bucket: str, metadata: dict[str, Any], size: int, overwrite: bool
    ) -> str:
        url = f"{self.endpoint_url}/upload/storage/v1/b/{quote(bucket, safe='')}/o"
        params = {"uploadType": "resumable"}
        if self._kms_key_name is not None:
            params["kmsKeyName"] = self._kms_key_name
        if not overwrite:
            params["ifGenerationMatch"] = "0"
        kwargs = {
            "params": params,
            "json": metadata,
            "headers": {
                "X-Upload-Content-Type": metadata["contentType"],
                "X-Upload-Content-Length": str(size),
            },
        }
        # Session initiation is not replayed on an ambiguous response: without
        # its Location, a newly created session cannot be explicitly cancelled.
        response = self._request("POST", url, retry=False, **kwargs)
        if response.status_code == 404 and self.auto_create_bucket:
            _close_response(response)
            self._create_bucket(bucket)
            response = self._request("POST", url, retry=False, **kwargs)
        with _response(response):
            self._check(response, exclusive=not overwrite)
            session = response.headers.get("Location", "")
        try:
            parsed = urlsplit(session)
            origin = urlsplit(self.endpoint_url)
            valid = (
                parsed.scheme == origin.scheme
                and parsed.hostname == origin.hostname
                and parsed.port == origin.port
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and bool(parsed.path)
            )
        except ValueError:
            valid = False
        if valid and not self._emulator:
            # Cancellation must target an upload session, never an object URL
            # that would turn cleanup into an unconditional object deletion.
            valid = parsed.path == urlsplit(url).path and bool(
                parse_qs(parsed.query).get("upload_id")
            )
        if not valid:
            raise OSError(errno.EIO, "GCS returned an invalid upload session")
        return session

    def _abort_upload(self, session: str) -> None:
        try:
            with _response(
                self._request("DELETE", session, headers={"Content-Length": "0"})
            ) as response:
                if response.status_code not in {200, 204, 404, 410, 499}:
                    raise GCSUploadCleanupError("GCS upload cancellation could not be confirmed")
        except Exception:
            raise GCSUploadCleanupError("GCS upload cancellation could not be confirmed") from None

    @staticmethod
    def _upload_offset(response: httpx.Response, lower: int, upper: int) -> int:
        value = response.headers.get("Range")
        if value is None:
            offset = 0
        else:
            match = re.fullmatch(r"bytes=0-(\d+)", value)
            if match is None:
                raise OSError(errno.EIO, "GCS returned an invalid upload offset")
            offset = int(match[1]) + 1
        if not lower <= offset <= upper:
            raise OSError(errno.EIO, "GCS returned an inconsistent upload offset")
        return offset

    def _upload(
        self,
        session: str,
        source: _SeekableStream,
        size: int,
        overwrite: bool,
        content_type: str,
    ) -> None:
        offset = 0
        failures = 0
        query = False
        sent_end = 0
        while True:
            if query:
                content = b""
                content_range = f"bytes */{size}"
            else:
                source.seek(offset)
                content = source.read(min(self.chunk_size, size - offset))
                sent_end = offset + len(content)
                content_range = (
                    f"bytes {offset}-{sent_end - 1}/{size}" if content else f"bytes */{size}"
                )
            try:
                response = self._send(
                    "PUT",
                    session,
                    content=content,
                    headers={
                        "Content-Range": content_range,
                        "Content-Type": content_type,
                    },
                )
            except GCSTransientError:
                response = None
            if response is None or response.status_code in _TRANSIENT_STATUSES:
                status = response.status_code if response is not None else None
                if response is not None:
                    _close_response(response)
                if failures >= self.max_retries:
                    raise GCSTransientError(status)
                self._pause(failures)
                failures += 1
                query = True
                continue
            with _response(response):
                if response.status_code in {200, 201}:
                    if sent_end != size:
                        raise OSError(errno.EIO, "GCS upload completed before all bytes were sent")
                    return
                if response.status_code != 308:
                    self._check(response, exclusive=not overwrite)
                    raise OSError(errno.EIO, "GCS returned an invalid upload response")
                acknowledged = self._upload_offset(response, offset, sent_end)
            if acknowledged == offset and (not query or offset == size):
                if failures >= self.max_retries:
                    raise GCSTransientError()
                self._pause(failures)
                failures += 1
            elif acknowledged > offset:
                failures = 0
            offset = acknowledged
            query = offset == size

    def put_object_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        _positive_int("size", size, minimum=0)
        if size > _MAX_OBJECT_SIZE:
            raise ValueError("GCS object size cannot exceed 5 TiB")
        if not isinstance(overwrite, bool):
            raise ValueError("GCS overwrite must be a boolean")
        resource = self._metadata(key, metadata)
        # Validate the *entire* source before publication. A seekable spool also
        # makes arbitrary partially acknowledged offsets safe to replay, even
        # when the caller supplied a non-seekable stream. Large spools use disk.
        if size > self.chunk_size:
            require_at_rest("runtime")
        with SpooledTemporaryFile(max_size=self.chunk_size, mode="w+b") as staged:
            remaining = size
            checksum = hashlib.md5(usedforsecurity=False)
            while remaining:
                requested = min(self.chunk_size, remaining)
                chunk = source.read(requested)
                if not isinstance(chunk, bytes):
                    raise ValueError("GCS source must return bytes")
                if not chunk or len(chunk) > requested:
                    raise ValueError("GCS source length does not match declared size")
                staged.write(chunk)
                checksum.update(chunk)
                remaining -= len(chunk)
            if source.read(1) != b"":
                raise ValueError("GCS source length does not match declared size")
            resource["md5Hash"] = base64.b64encode(checksum.digest()).decode("ascii")
            session = self._start_upload(bucket, resource, size, overwrite)
            try:
                self._upload(session, staged, size, overwrite, resource["contentType"])
            except BaseException:
                self._abort_upload(session)
                raise
        return size
