from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from cognistore.jobs.models import InvalidJobError
from cognistore.jobs.retry import FailureCategory, RetryPolicy, classify_job_error


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"injected {code}"},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": "request-123",
            },
        },
        "PutObject",
    )


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError("timed out"), FailureCategory.TIMEOUT),
        (
            EndpointConnectionError(endpoint_url="https://storage.invalid"),
            FailureCategory.UNAVAILABLE,
        ),
        (_client_error("SlowDown", 503), FailureCategory.THROTTLED),
        (_client_error("ServiceUnavailable", 503), FailureCategory.UNAVAILABLE),
        (
            _client_error("ConditionalRequestConflict", 409),
            FailureCategory.CONFLICT,
        ),
        (_client_error("AlternateConflict", 409), FailureCategory.CONFLICT),
    ],
)
def test_transient_backend_errors_are_retryable(
    error: BaseException, category: FailureCategory
) -> None:
    classification = classify_job_error(error)

    assert classification.retryable is True
    assert classification.category == category


@pytest.mark.parametrize(
    "error",
    [
        InvalidJobError("malformed payload"),
        ValueError("invalid request"),
        FileNotFoundError("source is missing"),
        PermissionError("access denied"),
        _client_error("MalformedXML", 400),
        _client_error("AccessDenied", 403),
        type("BackendBadRequest", (Exception,), {"status_code": 400})("bad request"),
    ],
)
def test_permanent_contract_and_authorization_errors_are_terminal(
    error: BaseException,
) -> None:
    assert classify_job_error(error).retryable is False


def test_http_status_is_read_from_response_objects() -> None:
    error = RuntimeError("upstream unavailable")
    error.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]

    classification = classify_job_error(error)

    assert classification.retryable is True
    assert classification.category == FailureCategory.UNAVAILABLE


def test_unknown_backend_error_is_a_bounded_retry() -> None:
    classification = classify_job_error(RuntimeError("third-party backend failed"))

    assert classification.retryable is True
    assert classification.category == FailureCategory.UNKNOWN


def test_exponential_backoff_is_jittered_and_capped() -> None:
    policy = RetryPolicy(max_attempts=7, base_delay=2, max_delay=10, jitter=0.25)

    assert policy.delay_for(1, random_value=0) == pytest.approx(1.5)
    assert policy.delay_for(2, random_value=0.5) == pytest.approx(4)
    assert policy.delay_for(3, random_value=1) == pytest.approx(10)
    assert policy.delay_for(30, random_value=1) == pytest.approx(10)


def test_exponential_backoff_handles_extreme_finite_delay_ratios() -> None:
    policy = RetryPolicy(base_delay=5e-324, max_delay=1e308, jitter=0)

    assert policy.delay_for(1, random_value=0.5) == 5e-324
    assert policy.delay_for(10_000, random_value=0.5) == 1e308


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"base_delay": 0},
        {"max_delay": 0.5, "base_delay": 1},
        {"jitter": -0.1},
        {"jitter": 1.1},
    ],
)
def test_retry_policy_rejects_unbounded_or_invalid_configuration(options) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**options)
