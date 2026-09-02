"""FastAPI transport for the stable CogniStore version 1 contract."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Annotated, Any, cast
from urllib.parse import quote
from uuid import uuid4

import botocore.exceptions
import nats.errors
import sqlalchemy.exc
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from cognistore.jobs.models import QueueSaturatedError
from cognistore.ui import register_content_search_ui

from .errors import APIError, PayloadTooLargeError, RequestContractError
from .gateway import APIGateway, UnavailableGateway
from .models import (
    AskRequest,
    AskResponse,
    CatalogObject,
    CatalogObjectPage,
    CatalogScanRequest,
    ErrorBody,
    ErrorEnvelope,
    HealthResponse,
    JobStatusResponse,
    ObjectResource,
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    PolicyRunRequest,
    ValidationIssue,
)

MAX_OBJECT_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_JSON_BODY_BYTES = 256 * 1024
_JSON_BODY_PATHS = frozenset(
    {
        "/v1/actions/catalog-scans",
        "/v1/actions/policy-runs",
        "/v1/ask",
        "/v1/policies/evaluate",
    }
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MEDIA_TYPE = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$",
    re.ASCII,
)
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
TierPath = Annotated[str, Path(min_length=1, max_length=256)]
BucketPath = Annotated[str, Path(min_length=1, max_length=1024)]
KeyPath = Annotated[str, Path(min_length=1, max_length=8192)]
_REQUEST_ID_HEADER = {
    "description": "Request correlation identifier",
    "schema": {"type": "string"},
}
_COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {
        "model": ErrorEnvelope,
        "description": "Authentication required by the configured authorization hook",
        "headers": {
            "X-Request-ID": _REQUEST_ID_HEADER,
            "WWW-Authenticate": {"schema": {"type": "string"}},
        },
    },
    403: {
        "model": ErrorEnvelope,
        "description": "Request forbidden by the configured authorization hook",
        "headers": {"X-Request-ID": _REQUEST_ID_HEADER},
    },
    422: {
        "model": ErrorEnvelope,
        "description": "Request validation failed",
        "headers": {"X-Request-ID": _REQUEST_ID_HEADER},
    },
    500: {
        "model": ErrorEnvelope,
        "description": "Backend operation failed",
        "headers": {"X-Request-ID": _REQUEST_ID_HEADER},
    },
    503: {
        "model": ErrorEnvelope,
        "description": "Backend unavailable",
        "headers": {
            "X-Request-ID": _REQUEST_ID_HEADER,
            "Retry-After": {"schema": {"type": "string"}},
        },
    },
}
_NOT_FOUND_RESPONSE = {
    "model": ErrorEnvelope,
    "description": "Resource not found",
    "headers": {"X-Request-ID": _REQUEST_ID_HEADER},
}
_CONFLICT_RESPONSE = {
    "model": ErrorEnvelope,
    "description": "Resource conflict",
    "headers": {"X-Request-ID": _REQUEST_ID_HEADER},
}
_PAYLOAD_TOO_LARGE_RESPONSE = {
    "model": ErrorEnvelope,
    "description": "Request body is too large",
    "headers": {"X-Request-ID": _REQUEST_ID_HEADER},
}
_JSON_ERROR_RESPONSES = {
    413: _PAYLOAD_TOO_LARGE_RESPONSE,
    **_COMMON_ERROR_RESPONSES,
}
_RANGE_RESPONSE = {
    "model": ErrorEnvelope,
    "description": "Byte range not satisfiable",
    "headers": {
        "X-Request-ID": _REQUEST_ID_HEADER,
        "Content-Range": {"schema": {"type": "string"}},
    },
}
_BINARY_CONTENT = {
    "*/*": {
        "schema": {"type": "string", "format": "binary"}
    }
}
_OBJECT_HEADERS = {
    "X-Request-ID": _REQUEST_ID_HEADER,
    "Accept-Ranges": {"schema": {"type": "string", "enum": ["bytes"]}},
    "Content-Disposition": {"schema": {"type": "string", "enum": ["attachment"]}},
    "Content-Length": {"schema": {"type": "integer", "minimum": 0}},
    "Content-Security-Policy": {"schema": {"type": "string"}},
    "ETag": {"schema": {"type": "string"}},
    "X-Content-Type-Options": {"schema": {"type": "string", "enum": ["nosniff"]}},
}
_OBJECT_DOWNLOAD_HEADERS = {
    "Content-Disposition": "attachment",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "X-Content-Type-Options": "nosniff",
}
_PUT_OBJECT_HEADERS = {
    "X-Request-ID": _REQUEST_ID_HEADER,
    "Location": {"schema": {"type": "string"}},
    "ETag": {"schema": {"type": "string"}},
}
_ACTION_HEADERS = {
    "X-Request-ID": _REQUEST_ID_HEADER,
    "Location": {"schema": {"type": "string"}},
    "Retry-After": {"schema": {"type": "string"}},
}


def allow_anonymous() -> None:
    """Default authorization extension hook for the M2 unauthenticated surface."""

    return None


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else str(uuid4())


def _etag(generation: str) -> str:
    """Turn any backend generation into a valid, stable HTTP entity tag."""

    digest = sha256(generation.encode("utf-8")).hexdigest()
    return f'"{digest}"'


def _safe_media_type(value: object) -> str:
    """Keep persisted backend metadata out of the HTTP header grammar."""

    if (
        isinstance(value, str)
        and len(value) <= 255
        and _MEDIA_TYPE.fullmatch(value) is not None
    ):
        return value
    return "application/octet-stream"


def _object_location(tier: str, bucket: str, key: str) -> str:
    return "/v1/objects/{}/{}/{}".format(
        quote(tier, safe=""),
        quote(bucket, safe=""),
        quote(key, safe="/"),
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: list[ValidationIssue] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    envelope = ErrorEnvelope(
        request_id=request_id,
        error=ErrorBody(
            code=code,
            message=message,
            retryable=retryable,
            details=details or [],
        ),
    )
    response_headers = {"X-Request-ID": request_id}
    if headers is not None:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=response_headers,
    )


def create_app(
    gateway: APIGateway | None = None,
    *,
    authorization_hook: Callable[..., Any] | None = None,
) -> FastAPI:
    """Create the ASGI application around injected service abstractions."""

    services = gateway or cast(APIGateway, UnavailableGateway())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await services.startup()
        try:
            yield
        finally:
            await services.shutdown()

    app = FastAPI(
        title="CogniStore REST API",
        summary="Versioned object, catalog, Ask, policy, and action operations",
        description=(
            "Stable version 1 REST interface. Authentication and tenancy are "
            "deployment extension hooks in M2."
        ),
        version="1.0.0",
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.state.gateway = services

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        supplied = request.headers.get("x-request-id")
        request.state.request_id = (
            supplied if supplied is not None and _REQUEST_ID.fullmatch(supplied) else str(uuid4())
        )
        if request.method == "POST" and request.url.path in _JSON_BODY_PATHS:
            raw_content_length = request.headers.get("content-length")
            if raw_content_length is not None:
                try:
                    content_length = int(raw_content_length)
                except ValueError:
                    return _error_response(
                        request,
                        status_code=422,
                        code="validation_error",
                        message="Content-Length must be an integer",
                    )
                if content_length < 0:
                    return _error_response(
                        request,
                        status_code=422,
                        code="validation_error",
                        message="Content-Length must be non-negative",
                    )
                if content_length > MAX_JSON_BODY_BYTES:
                    return _error_response(
                        request,
                        status_code=413,
                        code="payload_too_large",
                        message=(
                            f"Request bodies must not exceed "
                            f"{MAX_JSON_BODY_BYTES} bytes"
                        ),
                        headers={"Connection": "close"},
                    )

            received = 0
            original_receive = request._receive

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > MAX_JSON_BODY_BYTES:
                        raise PayloadTooLargeError(MAX_JSON_BODY_BYTES)
                return message

            request._receive = limited_receive
            try:
                # Read through the bounded receiver here so an oversized
                # chunked body can be returned as the public 413 contract.
                # FastAPI otherwise converts receive-time exceptions raised
                # during JSON parsing into an opaque 400. Successful bodies
                # are cached by Starlette and replayed to the route handler.
                await request.body()
            except PayloadTooLargeError as exc:
                return _error_response(
                    request,
                    status_code=exc.status_code,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                    headers=exc.headers,
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        issues = [
            ValidationIssue(
                location=".".join(str(part) for part in error.get("loc", ())),
                message=str(error.get("msg", "Invalid value")),
                type=str(error.get("type", "validation_error")),
            )
            for error in exc.errors()
        ]
        return _error_response(
            request,
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            details=issues,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code="route_not_found" if exc.status_code == 404 else "http_error",
            message="Route not found" if exc.status_code == 404 else "HTTP request failed",
            headers=(dict(exc.headers) if exc.headers is not None else None),
        )

    @app.exception_handler(FileNotFoundError)
    async def missing_backend_resource(request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(
            request,
            status_code=404,
            code="resource_not_found",
            message="The requested resource was not found",
        )

    @app.exception_handler(FileExistsError)
    async def conflicting_backend_resource(
        request: Request, _exc: FileExistsError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=409,
            code="resource_conflict",
            message="The requested resource already exists",
        )

    @app.exception_handler(ObjectGenerationMismatchError)
    async def object_generation_changed(
        request: Request, _exc: ObjectGenerationMismatchError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=409,
            code="object_generation_changed",
            message="The object changed during the request",
            retryable=True,
        )

    @app.exception_handler(QueueSaturatedError)
    async def queue_saturated(
        request: Request, _exc: QueueSaturatedError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code="queue_saturated",
            message="The asynchronous job queue is at capacity",
            retryable=True,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(OSError)
    @app.exception_handler(ConnectionError)
    @app.exception_handler(TimeoutError)
    @app.exception_handler(botocore.exceptions.ConnectTimeoutError)
    @app.exception_handler(botocore.exceptions.ConnectionClosedError)
    @app.exception_handler(botocore.exceptions.ReadTimeoutError)
    @app.exception_handler(botocore.exceptions.EndpointConnectionError)
    @app.exception_handler(nats.errors.ConnectionClosedError)
    @app.exception_handler(nats.errors.NoServersError)
    @app.exception_handler(nats.errors.TimeoutError)
    @app.exception_handler(sqlalchemy.exc.DisconnectionError)
    @app.exception_handler(sqlalchemy.exc.OperationalError)
    @app.exception_handler(sqlalchemy.exc.TimeoutError)
    async def backend_unavailable(request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code="backend_unavailable",
            message="A required backend is unavailable",
            retryable=True,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(Exception)
    async def backend_failure(request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(
            request,
            status_code=500,
            code="backend_failure",
            message="A backend operation failed",
        )

    auth = authorization_hook or allow_anonymous
    router = APIRouter(prefix="/v1", dependencies=[Depends(auth)])
    @app.get(
        "/healthz",
        response_model=HealthResponse,
        operation_id="getHealth",
        tags=["system"],
        responses={200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}}},
    )
    def health() -> HealthResponse:
        return HealthResponse()

    @router.put(
        "/objects/{tier}/{bucket}/{key:path}",
        response_model=ObjectResource,
        status_code=status.HTTP_201_CREATED,
        operation_id="putObject",
        tags=["objects"],
        responses={
            201: {"headers": _PUT_OBJECT_HEADERS},
            404: _NOT_FOUND_RESPONSE,
            409: _CONFLICT_RESPONSE,
            413: _PAYLOAD_TOO_LARGE_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "*/*": {
                        "schema": {
                            "type": "string",
                            "format": "binary",
                            "maxLength": MAX_OBJECT_UPLOAD_BYTES,
                        }
                    }
                },
            }
        },
    )
    async def put_object(
        request: Request,
        response: Response,
        tier: TierPath,
        bucket: BucketPath,
        key: KeyPath,
        overwrite: Annotated[bool, Query()] = True,
    ) -> ObjectResource:
        content_type = request.headers.get("content-type")
        if content_type is not None:
            content_type = content_type.split(";", 1)[0].strip().lower()
            if len(content_type) > 255 or _MEDIA_TYPE.fullmatch(content_type) is None:
                raise RequestContractError("Content-Type must contain a valid media type")
        raw_content_length = request.headers.get("content-length")
        content_length: int | None = None
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise RequestContractError("Content-Length must be an integer") from exc
            if content_length < 0:
                raise RequestContractError("Content-Length must be non-negative")
        if content_length is not None and content_length > MAX_OBJECT_UPLOAD_BYTES:
            raise PayloadTooLargeError(MAX_OBJECT_UPLOAD_BYTES)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_OBJECT_UPLOAD_BYTES:
                raise PayloadTooLargeError(MAX_OBJECT_UPLOAD_BYTES)
            body.extend(chunk)
        result = await asyncio.to_thread(
            services.put_object,
            tier,
            bucket,
            key,
            bytes(body),
            overwrite=overwrite,
            content_type=content_type,
        )
        response.headers["Location"] = _object_location(tier, bucket, key)
        response.headers["ETag"] = _etag(result.generation)
        return result

    @router.head(
        "/objects/{tier}/{bucket}/{key:path}",
        status_code=status.HTTP_200_OK,
        operation_id="headObject",
        tags=["objects"],
        response_class=Response,
        responses={
            200: {
                "description": "Object metadata headers",
                "headers": _OBJECT_HEADERS,
            },
            404: _NOT_FOUND_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def head_object(tier: TierPath, bucket: BucketPath, key: KeyPath) -> Response:
        resource = services.stat_object(tier, bucket, key)
        return Response(
            status_code=200,
            media_type=_safe_media_type(resource.metadata.get("mime")),
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(resource.size),
                "ETag": _etag(resource.generation),
                **_OBJECT_DOWNLOAD_HEADERS,
            },
        )

    @router.get(
        "/objects/{tier}/{bucket}/{key:path}",
        operation_id="getObject",
        tags=["objects"],
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Complete object bytes",
                "content": _BINARY_CONTENT,
                "headers": _OBJECT_HEADERS,
            },
            206: {
                "description": "One satisfiable byte range",
                "content": _BINARY_CONTENT,
                "headers": {
                    **_OBJECT_HEADERS,
                    "Content-Range": {"schema": {"type": "string"}},
                },
            },
            404: _NOT_FOUND_RESPONSE,
            409: _CONFLICT_RESPONSE,
            416: _RANGE_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def get_object(
        tier: TierPath,
        bucket: BucketPath,
        key: KeyPath,
        byte_range: Annotated[
            str | None,
            Header(alias="Range", max_length=256),
        ] = None,
    ) -> StreamingResponse:
        download = services.open_object(
            tier,
            bucket,
            key,
            byte_range=byte_range,
        )
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": _etag(download.resource.generation),
            "Content-Length": str(download.content_length),
            **_OBJECT_DOWNLOAD_HEADERS,
        }
        if download.content_range is not None:
            headers["Content-Range"] = download.content_range
        try:
            return StreamingResponse(
                download.chunks,
                status_code=206 if download.content_range is not None else 200,
                media_type=_safe_media_type(download.content_type),
                headers=headers,
                background=BackgroundTask(download.close),
            )
        except BaseException:
            download.close()
            raise

    @router.delete(
        "/objects/{tier}/{bucket}/{key:path}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="deleteObject",
        tags=["objects"],
        response_class=Response,
        responses={
            204: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            409: _CONFLICT_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def delete_object(tier: TierPath, bucket: BucketPath, key: KeyPath) -> Response:
        services.delete_object(tier, bucket, key)
        return Response(status_code=204)

    @router.get(
        "/catalog/objects",
        response_model=CatalogObjectPage,
        operation_id="listCatalogObjects",
        tags=["catalog"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def list_catalog_objects(
        bucket: Annotated[str, Query(min_length=1, max_length=1024)],
        prefix: Annotated[str, Query(max_length=8192)] = "",
        tier: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=16_384)] = None,
    ) -> CatalogObjectPage:
        return services.list_catalog_objects(
            bucket=bucket,
            prefix=prefix,
            tier=tier,
            limit=limit,
            cursor=cursor,
        )

    @router.get(
        "/catalog/objects/{bucket}/{key:path}",
        response_model=CatalogObject,
        operation_id="getCatalogObject",
        tags=["catalog"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def get_catalog_object(bucket: BucketPath, key: KeyPath) -> CatalogObject:
        return services.get_catalog_object(bucket, key)

    @router.post(
        "/ask",
        response_model=AskResponse,
        operation_id="ask",
        tags=["search"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            **_JSON_ERROR_RESPONSES,
        },
    )
    def ask(request: AskRequest) -> AskResponse:
        return services.ask(request)

    @router.post(
        "/policies/evaluate",
        response_model=PolicyEvaluationResponse,
        operation_id="evaluatePolicy",
        tags=["policies"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            **_JSON_ERROR_RESPONSES,
        },
    )
    def evaluate_policy(
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResponse:
        return services.evaluate_policy(request)

    @router.post(
        "/actions/catalog-scans",
        response_model=JobStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="submitCatalogScan",
        tags=["actions"],
        responses={
            202: {"headers": _ACTION_HEADERS},
            404: _NOT_FOUND_RESPONSE,
            **_JSON_ERROR_RESPONSES,
        },
    )
    async def submit_catalog_scan(
        request: CatalogScanRequest, response: Response
    ) -> JobStatusResponse:
        result = await services.submit_catalog_scan(request)
        response.headers["Location"] = result.status_url
        response.headers["Retry-After"] = "1"
        return result

    @router.post(
        "/actions/policy-runs",
        response_model=JobStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="submitPolicyRun",
        tags=["actions"],
        responses={202: {"headers": _ACTION_HEADERS}, **_JSON_ERROR_RESPONSES},
    )
    async def submit_policy_run(
        request: PolicyRunRequest, response: Response
    ) -> JobStatusResponse:
        result = await services.submit_policy_run(request)
        response.headers["Location"] = result.status_url
        response.headers["Retry-After"] = "1"
        return result

    @router.get(
        "/jobs/{job_id}",
        response_model=JobStatusResponse,
        operation_id="getJobStatus",
        tags=["actions"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def get_job_status(
        job_id: Annotated[str, Path(pattern=_UUID)],
    ) -> JobStatusResponse:
        return services.get_job(job_id)

    app.include_router(router)
    register_content_search_ui(app)
    return app
