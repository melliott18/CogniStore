"""Public exception hierarchy for the CogniStore Python SDK."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from .models import JobStatus, ValidationIssue


class CogniStoreError(Exception):
    """Base class for every SDK-originated error."""


class APIError(CogniStoreError):
    """A non-success HTTP response from the CogniStore API."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        request_id: str | None = None,
        retryable: bool = False,
        details: Sequence[ValidationIssue] = (),
        retry_after: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retryable = retryable
        self.details = tuple(details)
        self.retry_after = retry_after
        self.headers = httpx.Headers(headers)

    def __str__(self) -> str:
        identity = f" request_id={self.request_id}" if self.request_id else ""
        return f"CogniStore API error {self.status_code} ({self.code}): {self.message}{identity}"


class AuthenticationError(APIError):
    """The configured deployment requires authentication (HTTP 401)."""


class PermissionDeniedError(APIError):
    """The caller is not permitted to perform the operation (HTTP 403)."""


class NotFoundError(APIError):
    """The requested public resource does not exist (HTTP 404)."""


class ConflictError(APIError):
    """The requested operation conflicts with current state (HTTP 409)."""


class PayloadTooLargeError(APIError):
    """The request body exceeds the public API limit (HTTP 413)."""


class RangeNotSatisfiableError(APIError):
    """The requested object byte range cannot be served (HTTP 416)."""


class ValidationError(APIError):
    """The request does not satisfy the API contract (HTTP 422)."""


class ServiceUnavailableError(APIError):
    """A required service is unavailable and may be retryable (HTTP 503)."""


class ServerError(APIError):
    """The server returned an otherwise unmapped 5xx response."""


class TransportError(CogniStoreError):
    """The HTTP exchange failed before a complete response was available."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class RequestTimeoutError(TransportError):
    """A configured connect, read, write, or pool timeout expired."""


class ResponseContractError(CogniStoreError):
    """A success response does not match the supported API v1 contract."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.cause = cause


class PollingTimeoutError(CogniStoreError):
    """An asynchronous job did not reach a terminal state before its deadline."""

    def __init__(self, timeout: float, last_status: JobStatus | None) -> None:
        message = f"CogniStore job did not finish within {timeout:g} seconds"
        super().__init__(message)
        self.timeout = timeout
        self.last_status = last_status


class JobFailedError(CogniStoreError):
    """An asynchronous action reached the terminal ``failed`` state."""

    def __init__(self, job: JobStatus) -> None:
        message = f"CogniStore job {job.job_id} failed"
        if job.error_type:
            message += f" ({job.error_type})"
        super().__init__(message)
        self.job = job
