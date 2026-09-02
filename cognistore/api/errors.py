"""Stable API-domain errors independent of FastAPI and backend exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class APIError(Exception):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    headers: dict[str, str] = field(default_factory=dict)


class ResourceNotFoundError(APIError):
    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            404,
            "resource_not_found",
            f"{resource} was not found: {identifier}",
        )


class ResourceConflictError(APIError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "resource_conflict", message)


class RequestContractError(APIError):
    def __init__(self, message: str, *, code: str = "validation_error") -> None:
        super().__init__(422, code, message)


class RangeNotSatisfiableError(APIError):
    def __init__(self, size: int) -> None:
        super().__init__(
            416,
            "range_not_satisfiable",
            "The requested byte range cannot be satisfied",
            headers={"Content-Range": f"bytes */{size}"},
        )


class PayloadTooLargeError(APIError):
    def __init__(self, maximum_bytes: int) -> None:
        super().__init__(
            413,
            "payload_too_large",
            f"Request bodies must not exceed {maximum_bytes} bytes",
            headers={"Connection": "close"},
        )


class BackendUnavailableError(APIError):
    def __init__(self, message: str = "A required backend is unavailable") -> None:
        super().__init__(
            503,
            "backend_unavailable",
            message,
            retryable=True,
            headers={"Retry-After": "1"},
        )
