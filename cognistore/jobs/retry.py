from __future__ import annotations

import errno
import math
import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
    ProxyConnectionError,
    ReadTimeoutError,
)

from cognistore.core.move_jobs import (
    MoveJobConflictError,
    MoveJobFailedError,
    MoveJobLeaseError,
)
from cognistore.core.mover import MoveVerificationError

from .models import InvalidJobError, JobEnvelopeError, QueueSaturatedError


class RetryableJobError(RuntimeError):
    """Explicitly marks a handler failure as safe to retry."""


class TerminalJobError(RuntimeError):
    """Explicitly marks a handler failure as permanent for this job."""


class FailureCategory(str, Enum):
    INVALID = "invalid"
    TIMEOUT = "timeout"
    THROTTLED = "throttled"
    UNAVAILABLE = "unavailable"
    LEASED = "leased"
    INTEGRITY = "integrity"
    CONFLICT = "conflict"
    AUTHORIZATION = "authorization"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorClassification:
    retryable: bool
    category: FailureCategory
    reason: str


_THROTTLING_CODES = frozenset(
    {
        "BandwidthLimitExceeded",
        "EC2ThrottledException",
        "LimitExceededException",
        "PriorRequestNotComplete",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "SlowDown",
        "ThrottledException",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)
_UNAVAILABLE_CODES = frozenset(
    {
        "InternalError",
        "InternalFailure",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "Unavailable",
    }
)
_RETRYABLE_CONFLICT_CODES = frozenset(
    {
        "ConditionalRequestConflict",
        "OperationAborted",
    }
)
_AUTHORIZATION_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidAccessKeyId",
        "InvalidClientTokenId",
        "InvalidToken",
        "NoSuchBucket",
        "NoSuchKey",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EBUSY,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.EINTR,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    }
)
_TERMINAL_BOTOCORE_ERRORS = (
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
)
_RETRYABLE_BOTOCORE_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ProxyConnectionError,
    ReadTimeoutError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with symmetric proportional jitter."""

    max_attempts: int = 7
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool):
            raise ValueError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        for field_name in ("base_delay", "max_delay"):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{field_name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be greater than or equal to base_delay")
        if not isinstance(self.jitter, (int, float)) or isinstance(self.jitter, bool):
            raise ValueError("jitter must be a number")
        if not math.isfinite(self.jitter) or not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between zero and one")

    def delay_for(self, attempt: int, *, random_value: float | None = None) -> float:
        """Return the delay after the given 1-based failed delivery attempt."""

        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if random_value is None:
            random_value = random.random()
        if not isinstance(random_value, (int, float)) or isinstance(random_value, bool):
            raise ValueError("random_value must be a number")
        if not math.isfinite(random_value) or not 0 <= random_value <= 1:
            raise ValueError("random_value must be between zero and one")

        if self.base_delay >= self.max_delay:
            nominal = float(self.max_delay)
        else:
            exponent_to_cap = math.ceil(
                math.log2(self.max_delay) - math.log2(self.base_delay)
            )
            exponent = min(attempt - 1, exponent_to_cap)
            nominal = (
                float(self.max_delay)
                if exponent >= exponent_to_cap
                else min(
                    float(self.max_delay),
                    math.ldexp(float(self.base_delay), exponent),
                )
            )
        multiplier = (1 - self.jitter) + (2 * self.jitter * float(random_value))
        return min(float(self.max_delay), max(0.0, nominal * multiplier))


def _client_error_details(error: ClientError) -> tuple[str, int | None]:
    response = error.response if isinstance(error.response, Mapping) else {}
    error_value = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    code = str(error_value.get("Code", "")) if isinstance(error_value, Mapping) else ""
    status_value = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    status = status_value if isinstance(status_value, int) else None
    return code, status


def _status_from_exception(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        for key in ("status_code", "status"):
            value = getattr(response, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None
    for key in ("status_code", "status", "HTTPStatusCode"):
        value = response.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("HTTPStatusCode")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _classification_for_one(error: BaseException) -> ErrorClassification | None:
    if isinstance(error, QueueSaturatedError) or getattr(
        error, "throughput_saturated", False
    ):
        return ErrorClassification(
            True,
            FailureCategory.THROTTLED,
            "bounded queue is saturated",
        )
    if isinstance(error, RetryableJobError):
        return ErrorClassification(True, FailureCategory.UNKNOWN, "explicit retryable error")
    if isinstance(error, TerminalJobError):
        return ErrorClassification(False, FailureCategory.INVALID, "explicit terminal error")
    if isinstance(error, MoveJobLeaseError):
        return ErrorClassification(True, FailureCategory.LEASED, "move lease is held")
    if isinstance(error, MoveVerificationError):
        return ErrorClassification(False, FailureCategory.INTEGRITY, "integrity verification failed")
    if isinstance(error, MoveJobConflictError):
        return ErrorClassification(False, FailureCategory.CONFLICT, "move idempotency conflict")
    if isinstance(error, MoveJobFailedError):
        return ErrorClassification(False, FailureCategory.INTEGRITY, "move is already terminal")
    if isinstance(error, (InvalidJobError, JobEnvelopeError, TypeError, ValueError)):
        return ErrorClassification(False, FailureCategory.INVALID, "invalid job contract")
    if isinstance(error, (FileNotFoundError, FileExistsError, NotADirectoryError, IsADirectoryError)):
        return ErrorClassification(False, FailureCategory.INVALID, "storage object contract failed")
    if isinstance(error, PermissionError):
        return ErrorClassification(False, FailureCategory.AUTHORIZATION, "storage access denied")
    if isinstance(error, ClientError):
        code, status = _client_error_details(error)
        if code in _THROTTLING_CODES or status == 429:
            return ErrorClassification(True, FailureCategory.THROTTLED, code or "HTTP 429")
        if code in _RETRYABLE_CONFLICT_CODES or status == 409:
            return ErrorClassification(
                True, FailureCategory.CONFLICT, code or "HTTP 409"
            )
        if code in _UNAVAILABLE_CODES or status == 408 or (status is not None and status >= 500):
            return ErrorClassification(True, FailureCategory.UNAVAILABLE, code or f"HTTP {status}")
        if code in _AUTHORIZATION_CODES or status in {401, 403}:
            return ErrorClassification(False, FailureCategory.AUTHORIZATION, code or f"HTTP {status}")
        return ErrorClassification(False, FailureCategory.INVALID, code or f"HTTP {status}")
    if isinstance(error, _TERMINAL_BOTOCORE_ERRORS):
        return ErrorClassification(False, FailureCategory.AUTHORIZATION, "AWS client is misconfigured")
    if isinstance(error, _RETRYABLE_BOTOCORE_ERRORS):
        category = FailureCategory.TIMEOUT if isinstance(
            error, (ConnectTimeoutError, ReadTimeoutError)
        ) else FailureCategory.UNAVAILABLE
        return ErrorClassification(True, category, "backend transport failed")
    if isinstance(error, TimeoutError):
        return ErrorClassification(True, FailureCategory.TIMEOUT, "operation timed out")
    if isinstance(error, ConnectionError):
        return ErrorClassification(True, FailureCategory.UNAVAILABLE, "backend connection failed")
    if isinstance(error, OSError):
        if error.errno is None or error.errno in _TRANSIENT_ERRNOS:
            return ErrorClassification(True, FailureCategory.UNAVAILABLE, "backend I/O unavailable")
        return ErrorClassification(False, FailureCategory.INVALID, f"non-transient errno {error.errno}")
    status = _status_from_exception(error)
    if status == 429:
        return ErrorClassification(True, FailureCategory.THROTTLED, "HTTP 429")
    if status == 409:
        return ErrorClassification(True, FailureCategory.CONFLICT, "HTTP 409")
    if status == 408 or (status is not None and status >= 500):
        return ErrorClassification(True, FailureCategory.UNAVAILABLE, f"HTTP {status}")
    if status in {401, 403}:
        return ErrorClassification(False, FailureCategory.AUTHORIZATION, f"HTTP {status}")
    if status is not None and 400 <= status < 500:
        return ErrorClassification(False, FailureCategory.INVALID, f"HTTP {status}")
    if getattr(error, "retryable", None) is True:
        return ErrorClassification(True, FailureCategory.UNKNOWN, "exception marked retryable")
    if isinstance(error, BotoCoreError):
        return ErrorClassification(True, FailureCategory.UNAVAILABLE, "AWS backend error")
    return None


def classify_job_error(error: BaseException) -> ErrorClassification:
    """Classify an error and its explicit cause chain for worker settlement."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        classification = _classification_for_one(current)
        if classification is not None:
            return classification
        current = current.__cause__ or current.__context__
    # Unknown handler failures remain bounded retries. This preserves availability
    # for third-party backends without allowing a poison job to loop forever.
    return ErrorClassification(True, FailureCategory.UNKNOWN, "unclassified handler error")


def classify_error(error: BaseException) -> ErrorClassification:
    """Compatibility alias for callers that do not use the worker-specific name."""

    return classify_job_error(error)


RandomSource = Callable[[], float]
