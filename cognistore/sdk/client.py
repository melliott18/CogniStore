"""Synchronous HTTP client for the stable CogniStore API v1 contract."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType
from typing import Literal, TypeVar, cast
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .errors import (
    APIError,
    AuthenticationError,
    ConflictError,
    JobFailedError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    PollingTimeoutError,
    RangeNotSatisfiableError,
    RequestTimeoutError,
    ResponseContractError,
    ServerError,
    ServiceUnavailableError,
    TransportError,
    ValidationError,
)
from .models import (
    AskRequest,
    AskResponse,
    CatalogObject,
    CatalogObjectPage,
    CatalogScanRequest,
    DeleteObjectResponse,
    ErrorEnvelope,
    HeadObjectResponse,
    HealthResponse,
    JobStatus,
    ObjectDownload,
    ObjectResource,
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    PolicyRunRequest,
    SDKRequest,
)
from .models import ValidationIssue as ValidationIssueModel

DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_MAX_POLL_INTERVAL = 30.0
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed"})
SUPPORTED_OPERATION_IDS = frozenset(
    {
        "ask",
        "deleteObject",
        "evaluatePolicy",
        "getCatalogObject",
        "getHealth",
        "getJobStatus",
        "getObject",
        "headObject",
        "listCatalogObjects",
        "putObject",
        "submitCatalogScan",
        "submitPolicyRun",
    }
)
_HTTP_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)
QueryValue = str | int | float | bool | None


def _package_version() -> str:
    try:
        return version("cognistore")
    except PackageNotFoundError:
        return "0+unknown"


def _positive_finite(value: float, name: str, *, allow_zero: bool = False) -> float:
    converted = float(value)
    minimum_ok = converted >= 0 if allow_zero else converted > 0
    if not minimum_ok or not math.isfinite(converted):
        comparison = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite {comparison} number")
    return converted


def _validated_headers(
    values: Mapping[str, str],
    *,
    source: str,
) -> dict[str, str]:
    headers = {str(name): str(value) for name, value in values.items()}
    for name, value in headers.items():
        if _HTTP_HEADER_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"{source} contains an invalid HTTP header name: {name!r}")
        if value != value.strip(" \t") or any(
            (ord(character) < 32 and character != "\t")
            or ord(character) > 126
            for character in value
        ):
            raise ValueError(f"{source} contains an invalid value for {name!r}")
    try:
        httpx.Headers(headers)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{source} contains an invalid HTTP header") from exc
    return headers


@dataclass(frozen=True)
class ClientConfig:
    """Connection and polling defaults for :class:`CogniStoreClient`."""

    base_url: str
    timeout: float | httpx.Timeout = DEFAULT_HTTP_TIMEOUT
    default_headers: Mapping[str, str] = field(default_factory=dict)
    poll_interval: float = DEFAULT_POLL_INTERVAL
    max_poll_interval: float = DEFAULT_MAX_POLL_INTERVAL

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be a valid absolute HTTP URL")
        if any(
            character.isspace() or ord(character) == 127
            for character in self.base_url
        ):
            raise ValueError("base_url must be a valid absolute HTTP URL")
        if "?" in self.base_url or "#" in self.base_url:
            raise ValueError("base_url must not contain a query string or fragment")
        try:
            parsed = httpx.URL(self.base_url)
            parsed_scheme = parsed.scheme
            parsed_host = parsed.host
            parsed_port = parsed.port
        except (httpx.InvalidURL, TypeError, ValueError) as exc:
            raise ValueError("base_url must be a valid absolute HTTP URL") from exc
        if parsed_scheme not in {"http", "https"} or not parsed_host:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        if parsed_port is not None and not 1 <= parsed_port <= 65_535:
            raise ValueError("base_url contains an invalid port")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        if parsed.username or parsed.password:
            raise ValueError("base_url credentials are not supported; use default_headers")

        if isinstance(self.timeout, bool):
            raise TypeError("timeout must be seconds or an httpx.Timeout")
        if isinstance(self.timeout, (int, float)):
            normalized_timeout: float | httpx.Timeout = _positive_finite(
                float(self.timeout), "timeout"
            )
        elif isinstance(self.timeout, httpx.Timeout):
            normalized_components: dict[str, float | None] = {}
            for component_name in ("connect", "read", "write", "pool"):
                component = getattr(self.timeout, component_name)
                if component is None:
                    normalized_components[component_name] = None
                    continue
                if isinstance(component, bool) or not isinstance(
                    component, (int, float)
                ):
                    raise TypeError(
                        f"timeout.{component_name} must be seconds or None"
                    )
                normalized_components[component_name] = _positive_finite(
                    component,
                    f"timeout.{component_name}",
                )
            normalized_timeout = httpx.Timeout(
                connect=normalized_components["connect"],
                read=normalized_components["read"],
                write=normalized_components["write"],
                pool=normalized_components["pool"],
            )
        else:
            raise TypeError("timeout must be seconds or an httpx.Timeout")

        headers = _validated_headers(self.default_headers, source="default_headers")

        poll_interval = _positive_finite(self.poll_interval, "poll_interval")
        max_poll_interval = _positive_finite(
            self.max_poll_interval, "max_poll_interval"
        )
        if max_poll_interval < poll_interval:
            raise ValueError("max_poll_interval must be greater than or equal to poll_interval")

        object.__setattr__(self, "base_url", str(parsed).rstrip("/"))
        object.__setattr__(self, "timeout", normalized_timeout)
        object.__setattr__(self, "default_headers", MappingProxyType(headers))
        object.__setattr__(self, "poll_interval", poll_interval)
        object.__setattr__(self, "max_poll_interval", max_poll_interval)


class CogniStoreClient:
    """Typed client for all public CogniStore API v1 operations.

    The client owns and closes its internal HTTP connection pool. An injected
    ``http_client`` remains owned by its caller, which is useful for custom
    transports and FastAPI contract tests.
    """

    def __init__(
        self,
        base_url: str | ClientConfig,
        *,
        timeout: float | httpx.Timeout | None = None,
        default_headers: Mapping[str, str] | None = None,
        poll_interval: float | None = None,
        max_poll_interval: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if isinstance(base_url, ClientConfig):
            if any(
                override is not None
                for override in (
                    timeout,
                    default_headers,
                    poll_interval,
                    max_poll_interval,
                )
            ):
                raise TypeError("ClientConfig cannot be combined with configuration overrides")
            config = base_url
        else:
            config = ClientConfig(
                base_url=base_url,
                timeout=timeout if timeout is not None else DEFAULT_HTTP_TIMEOUT,
                default_headers=default_headers or {},
                poll_interval=(
                    poll_interval
                    if poll_interval is not None
                    else DEFAULT_POLL_INTERVAL
                ),
                max_poll_interval=(
                    max_poll_interval
                    if max_poll_interval is not None
                    else DEFAULT_MAX_POLL_INTERVAL
                ),
            )

        self.config = config
        self._http = http_client or httpx.Client(follow_redirects=False)
        self._owns_http_client = http_client is None
        self._closed = False
        self._poll_intervals: dict[UUID, float] = {}

    def __enter__(self) -> CogniStoreClient:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close an internally created connection pool; injected clients stay open."""

        if not self._closed and self._owns_http_client:
            self._http.close()
        self._closed = True

    @staticmethod
    def _quote_segment(value: str) -> str:
        encoded = quote(value, safe="")
        if value in {".", ".."}:
            return encoded.replace(".", "%2E")
        return encoded

    @classmethod
    def _quote_key(cls, value: str) -> str:
        return "/".join(cls._quote_segment(segment) for segment in value.split("/"))

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = httpx.Headers(
            {
                "User-Agent": f"cognistore-python/{_package_version()}",
                **self.config.default_headers,
            }
        )
        if extra is not None:
            headers.update(_validated_headers(extra, source="request headers"))
        return _validated_headers(dict(headers), source="request headers")

    def _send(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        params: Mapping[str, QueryValue] | None = None,
        json: object = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        if self._closed:
            raise TransportError("CogniStoreClient is closed")
        url = self._url(path)
        try:
            response = self._http.request(
                method,
                url,
                params=params,
                json=json,
                content=content,
                headers=self._headers(headers),
                timeout=(
                    self.config.timeout
                    if request_timeout is None
                    else request_timeout
                ),
            )
        except httpx.TimeoutException as exc:
            raise RequestTimeoutError(
                f"CogniStore {method} request timed out",
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"CogniStore {method} request failed before a complete response",
                cause=exc,
            ) from exc

        if response.status_code not in expected_statuses:
            raise self._api_error(response)
        return response

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw_value = response.headers.get("Retry-After")
        if raw_value is None:
            return None
        try:
            value = float(raw_value)
        except ValueError:
            return None
        if value < 0 or not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _error_type(status_code: int) -> type[APIError]:
        mapped: dict[int, type[APIError]] = {
            401: AuthenticationError,
            403: PermissionDeniedError,
            404: NotFoundError,
            409: ConflictError,
            413: PayloadTooLargeError,
            416: RangeNotSatisfiableError,
            422: ValidationError,
            503: ServiceUnavailableError,
        }
        if status_code in mapped:
            return mapped[status_code]
        if status_code >= 500:
            return ServerError
        return APIError

    def _api_error(self, response: httpx.Response) -> APIError:
        request_id = response.headers.get("X-Request-ID")
        code = f"http_{response.status_code}"
        message = response.reason_phrase or "CogniStore API request failed"
        retryable = response.status_code in {429, 502, 503, 504}
        details: tuple[ValidationIssueModel, ...] = ()

        if response.content:
            try:
                envelope = ErrorEnvelope.model_validate_json(response.content)
            except (PydanticValidationError, ValueError):
                envelope = None
            if envelope is not None:
                request_id = request_id or envelope.request_id
                code = envelope.error.code
                message = envelope.error.message
                retryable = envelope.error.retryable
                details = tuple(envelope.error.details)

        error_class = self._error_type(response.status_code)
        return error_class(
            status_code=response.status_code,
            code=code,
            message=message,
            request_id=request_id,
            retryable=retryable,
            details=details,
            retry_after=self._retry_after(response),
            headers=response.headers,
        )

    @staticmethod
    def _parse_model(
        response: httpx.Response,
        model_type: type[ResponseModelT],
    ) -> ResponseModelT:
        try:
            return model_type.model_validate_json(response.content)
        except (PydanticValidationError, ValueError) as exc:
            raise ResponseContractError(
                f"CogniStore response does not match {model_type.__name__} for API v1",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
                cause=exc,
            ) from exc

    @staticmethod
    def _request_json(request: SDKRequest) -> dict[str, object]:
        return request.model_dump(mode="json", exclude_none=True)

    @classmethod
    def _policy_request_json(
        cls,
        request: PolicyEvaluationRequest | PolicyRunRequest,
    ) -> dict[str, object]:
        """Keep feature-free policy calls compatible with older API v1 servers."""

        payload = cls._request_json(request)
        config = payload.get("config")
        if isinstance(config, dict) and not config.get("embedding_rules"):
            config.pop("embedding_rules", None)
        return payload

    @staticmethod
    def _required_header(response: httpx.Response, name: str) -> str:
        value = response.headers.get(name)
        if value is None:
            raise ResponseContractError(
                f"CogniStore response is missing required {name} header",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
            )
        return value

    @classmethod
    def _content_length(cls, response: httpx.Response) -> int:
        raw_value = cls._required_header(response, "Content-Length")
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ResponseContractError(
                "CogniStore response has an invalid Content-Length header",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
                cause=exc,
            ) from exc
        if value < 0:
            raise ResponseContractError(
                "CogniStore response has a negative Content-Length header",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
            )
        return value

    def get_health(self) -> HealthResponse:
        response = self._send("GET", "/healthz", expected_statuses={200})
        return self._parse_model(response, HealthResponse)

    def put_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
    ) -> ObjectResource:
        path = "/v1/objects/{}/{}/{}".format(
            self._quote_segment(tier),
            self._quote_segment(bucket),
            self._quote_key(key),
        )
        response = self._send(
            "PUT",
            path,
            expected_statuses={201},
            params={"overwrite": overwrite},
            content=content,
            headers={"Content-Type": content_type, "Accept": "application/json"},
        )
        return self._parse_model(response, ObjectResource)

    def head_object(self, tier: str, bucket: str, key: str) -> HeadObjectResponse:
        path = "/v1/objects/{}/{}/{}".format(
            self._quote_segment(tier),
            self._quote_segment(bucket),
            self._quote_key(key),
        )
        response = self._send(
            "HEAD",
            path,
            expected_statuses={200},
            headers={"Accept-Encoding": "identity"},
        )
        try:
            return HeadObjectResponse(
                content_length=self._content_length(response),
                content_type=self._required_header(response, "Content-Type"),
                etag=self._required_header(response, "ETag"),
                accept_ranges=cast(
                    Literal["bytes"],
                    self._required_header(response, "Accept-Ranges"),
                ),
                request_id=response.headers.get("X-Request-ID"),
            )
        except PydanticValidationError as exc:
            raise ResponseContractError(
                "CogniStore HEAD response headers do not match API v1",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
                cause=exc,
            ) from exc

    def get_object(
        self,
        tier: str,
        bucket: str,
        key: str,
        *,
        byte_range: str | None = None,
    ) -> ObjectDownload:
        path = "/v1/objects/{}/{}/{}".format(
            self._quote_segment(tier),
            self._quote_segment(bucket),
            self._quote_key(key),
        )
        headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
        if byte_range is not None:
            headers["Range"] = byte_range
        response = self._send(
            "GET",
            path,
            expected_statuses={200, 206},
            headers=headers,
        )
        content_length = self._content_length(response)
        if len(response.content) != content_length:
            raise ResponseContractError(
                "CogniStore object body length does not match Content-Length",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
            )
        content_range = response.headers.get("Content-Range")
        if response.status_code == 206 and content_range is None:
            raise ResponseContractError(
                "CogniStore partial response is missing Content-Range",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
            )
        try:
            return ObjectDownload(
                content=response.content,
                content_length=content_length,
                content_type=self._required_header(response, "Content-Type"),
                etag=self._required_header(response, "ETag"),
                accept_ranges=cast(
                    Literal["bytes"],
                    self._required_header(response, "Accept-Ranges"),
                ),
                content_range=content_range,
                status_code=cast(Literal[200, 206], response.status_code),
                request_id=response.headers.get("X-Request-ID"),
            )
        except PydanticValidationError as exc:
            raise ResponseContractError(
                "CogniStore object response headers do not match API v1",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
                cause=exc,
            ) from exc

    def delete_object(
        self,
        tier: str,
        bucket: str,
        key: str,
    ) -> DeleteObjectResponse:
        path = "/v1/objects/{}/{}/{}".format(
            self._quote_segment(tier),
            self._quote_segment(bucket),
            self._quote_key(key),
        )
        response = self._send("DELETE", path, expected_statuses={204})
        return DeleteObjectResponse(request_id=response.headers.get("X-Request-ID"))

    def list_catalog_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        tier: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CatalogObjectPage:
        params: dict[str, QueryValue] = {
            "bucket": bucket,
            "prefix": prefix,
            "limit": limit,
        }
        if tier is not None:
            params["tier"] = tier
        if cursor is not None:
            params["cursor"] = cursor
        response = self._send(
            "GET",
            "/v1/catalog/objects",
            expected_statuses={200},
            params=params,
            headers={"Accept": "application/json"},
        )
        return self._parse_model(response, CatalogObjectPage)

    def iter_catalog_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        tier: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Iterator[CatalogObject]:
        """Yield catalog objects across opaque keyset pages."""

        next_cursor = cursor
        seen_cursors = {cursor} if cursor is not None else set()
        while True:
            page = self.list_catalog_objects(
                bucket,
                prefix=prefix,
                tier=tier,
                limit=limit,
                cursor=next_cursor,
            )
            yield from page.items
            next_cursor = page.page.next_cursor
            if next_cursor is None:
                return
            if next_cursor in seen_cursors:
                raise ResponseContractError(
                    "CogniStore API returned a repeated catalog cursor"
                )
            seen_cursors.add(next_cursor)

    def get_catalog_object(self, bucket: str, key: str) -> CatalogObject:
        path = "/v1/catalog/objects/{}/{}".format(
            self._quote_segment(bucket),
            self._quote_key(key),
        )
        response = self._send("GET", path, expected_statuses={200})
        return self._parse_model(response, CatalogObject)

    def ask(self, request: AskRequest) -> AskResponse:
        payload = self._request_json(request)
        # Keep a default-constructed newer SDK compatible with older strict v1
        # servers. Explicit mode selection is still serialized normally.
        if "retrieval_mode" not in request.model_fields_set:
            payload.pop("retrieval_mode", None)
        response = self._send(
            "POST",
            "/v1/ask",
            expected_statuses={200},
            json=payload,
            headers={"Accept": "application/json"},
        )
        return self._parse_model(response, AskResponse)

    def evaluate_policy(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResponse:
        response = self._send(
            "POST",
            "/v1/policies/evaluate",
            expected_statuses={200},
            json=self._policy_request_json(request),
            headers={"Accept": "application/json"},
        )
        return self._parse_model(response, PolicyEvaluationResponse)

    def _remember_poll_interval(self, response: httpx.Response, job: JobStatus) -> None:
        retry_after = self._retry_after(response)
        if retry_after is not None:
            self._poll_intervals[job.job_id] = min(
                retry_after,
                self.config.max_poll_interval,
            )

    def submit_catalog_scan(self, request: CatalogScanRequest) -> JobStatus:
        response = self._send(
            "POST",
            "/v1/actions/catalog-scans",
            expected_statuses={202},
            json=self._request_json(request),
            headers={"Accept": "application/json"},
        )
        job = self._parse_model(response, JobStatus)
        self._remember_poll_interval(response, job)
        return job

    def submit_policy_run(self, request: PolicyRunRequest) -> JobStatus:
        response = self._send(
            "POST",
            "/v1/actions/policy-runs",
            expected_statuses={202},
            json=self._policy_request_json(request),
            headers={"Accept": "application/json"},
        )
        job = self._parse_model(response, JobStatus)
        self._remember_poll_interval(response, job)
        return job

    @staticmethod
    def _bounded_timeout(
        configured: float | httpx.Timeout,
        remaining: float,
    ) -> float | httpx.Timeout:
        if isinstance(configured, float):
            return min(configured, remaining)

        def bounded(value: float | None) -> float:
            return remaining if value is None else min(value, remaining)

        return httpx.Timeout(
            connect=bounded(configured.connect),
            read=bounded(configured.read),
            write=bounded(configured.write),
            pool=bounded(configured.pool),
        )

    def _get_job_status(
        self,
        job_id: UUID | str,
        *,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> JobStatus:
        path = f"/v1/jobs/{self._quote_segment(str(job_id))}"
        response = self._send(
            "GET",
            path,
            expected_statuses={200},
            request_timeout=request_timeout,
        )
        return self._parse_model(response, JobStatus)

    def get_job_status(self, job_id: UUID | str) -> JobStatus:
        return self._get_job_status(job_id)

    @staticmethod
    def _terminal_job(job: JobStatus, *, raise_on_failure: bool) -> JobStatus:
        if job.status == "failed" and raise_on_failure:
            raise JobFailedError(job)
        return job

    def wait_for_job(
        self,
        job: UUID | str | JobStatus,
        *,
        timeout: float = 60.0,
        poll_interval: float | None = None,
        raise_on_failure: bool = True,
    ) -> JobStatus:
        """Poll an accepted action until it succeeds, fails, or times out."""

        timeout = _positive_finite(timeout, "timeout", allow_zero=True)
        if poll_interval is not None:
            interval = _positive_finite(poll_interval, "poll_interval")
        elif isinstance(job, JobStatus):
            interval = self._poll_intervals.get(job.job_id, self.config.poll_interval)
        else:
            interval = self.config.poll_interval
        interval = min(interval, self.config.max_poll_interval)

        last_status = job if isinstance(job, JobStatus) else None
        job_id = job.job_id if isinstance(job, JobStatus) else job
        if last_status is not None and last_status.status in TERMINAL_JOB_STATES:
            return self._terminal_job(last_status, raise_on_failure=raise_on_failure)

        started = time.monotonic()
        deadline = started + timeout
        should_wait = last_status is not None
        while True:
            if should_wait:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PollingTimeoutError(timeout, last_status)
                time.sleep(min(interval, remaining))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PollingTimeoutError(timeout, last_status)
            try:
                last_status = self._get_job_status(
                    job_id,
                    request_timeout=self._bounded_timeout(
                        self.config.timeout,
                        remaining,
                    ),
                )
            except RequestTimeoutError as exc:
                if time.monotonic() >= deadline:
                    raise PollingTimeoutError(timeout, last_status) from exc
                raise
            except APIError as exc:
                if not exc.retryable:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PollingTimeoutError(timeout, last_status) from exc
                retry_delay = exc.retry_after if exc.retry_after is not None else interval
                time.sleep(min(retry_delay, self.config.max_poll_interval, remaining))
                should_wait = False
                continue

            if time.monotonic() >= deadline:
                raise PollingTimeoutError(timeout, last_status)
            if last_status.status in TERMINAL_JOB_STATES:
                self._poll_intervals.pop(last_status.job_id, None)
                return self._terminal_job(
                    last_status,
                    raise_on_failure=raise_on_failure,
                )
            should_wait = True
