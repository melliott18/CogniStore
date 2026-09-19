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
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from starlette._utils import get_route_path
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from cognistore.auth.authorization import (
    AuthorizationError,
    RBACAuthorizer,
    audit_authorization_denial,
    authorization_context,
    authorize_operation,
)
from cognistore.auth.jwt import AuthenticationError, JWTAuthConfig, JWTAuthenticator
from cognistore.auth.principal import principal_context
from cognistore.auth.tenancy import TenantIsolationError, TenantResolver, tenant_context
from cognistore.budget_telemetry import budget_metrics_response
from cognistore.core.legal_holds import LegalHoldError
from cognistore.drivers.observed import access_operation
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from cognistore.jobs.models import QueueSaturatedError
from cognistore.observability import (
    configure_observability,
    metrics_response,
    register_http_routes,
)
from cognistore.ui import register_content_search_ui

from .admin import AdminSession, AdminStorage, JobHistoryPage
from .admin_repairs import (
    RepairListResponse,
    RepairPreviewRequest,
    RepairPreviewResponse,
    RepairStatusResponse,
    RepairSubmitRequest,
)
from .errors import APIError, PayloadTooLargeError, RequestContractError
from .gateway import APIGateway, CogniStoreGateway, UnavailableGateway
from .models import (
    AskRequest,
    AskResponse,
    AuditEventPage,
    AuditEventResource,
    AuditExportResponse,
    AuditVerificationRequest,
    AuditVerificationResponse,
    CatalogObject,
    CatalogObjectPage,
    CatalogScanRequest,
    ErrorBody,
    ErrorEnvelope,
    HealthResponse,
    ImportanceChangeRequest,
    JobStatusResponse,
    LegalHoldList,
    LegalHoldReleaseRequest,
    LegalHoldRequest,
    LegalHoldResource,
    ObjectResource,
    PolicyDecisionPage,
    PolicyDecisionResource,
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    PolicyRunRequest,
    ValidationIssue,
)
from .permissions import (
    ANY_PERMISSION_OPERATIONS,
    AUTHENTICATED_OPERATIONS,
    ENDPOINT_OPERATIONS,
    OPERATION_PERMISSIONS,
    endpoint_operation,
)
from .telemetry import TelemetryMiddleware

MAX_OBJECT_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_JSON_BODY_BYTES = 256 * 1024
_JSON_BODY_PATHS = frozenset(
    {
        "/v1/actions/catalog-scans",
        "/v1/actions/policy-runs",
        "/v1/ask",
        "/v1/policies/evaluate",
        "/v1/policies/preview",
        "/v1/catalog/importance",
        "/v1/legal-holds",
        "/v1/audit/verify",
        "/v1/admin/repairs",
        "/v1/admin/repairs/preview",
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
        "description": "Bearer token missing or invalid, or authentication required",
        "headers": {
            "X-Request-ID": _REQUEST_ID_HEADER,
            "WWW-Authenticate": {"schema": {"type": "string"}},
        },
    },
    403: {
        "model": ErrorEnvelope,
        "description": "Operation not permitted by role-based authorization or an additional hook",
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
    """Default authorization extension hook; JWT authentication runs separately."""

    return None


def access_headers(
    request_id: Annotated[
        str | None,
        Header(
            alias="X-Request-ID",
            description=(
                "Request correlation and default access-operation identity. Reusing a valid "
                "identifier deduplicates access signals for the same operation kind and object."
            ),
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            pattern=r"^[A-Za-z0-9._:-]{1,128}$",
            description=(
                "Optional logical access-operation identity shared across retries with different "
                "request IDs. Deduplicates access history only; storage operations still execute."
            ),
        ),
    ] = None,
) -> None:
    """Document the correlation headers parsed by the request middleware."""


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
    response_headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
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
    authentication: JWTAuthConfig | JWTAuthenticator | None = None,
    authorization: RBACAuthorizer | None = None,
    tenancy: TenantResolver | None = None,
    authorization_hook: Callable[..., Any] | None = None,
) -> FastAPI:
    """Create the ASGI application around injected service abstractions."""

    services = gateway or cast(APIGateway, UnavailableGateway())
    authorizer = authorization
    if authorizer is None and isinstance(services, CogniStoreGateway):
        authorizer = services.authorization
    if authorizer is not None and authentication is None:
        raise ValueError("API authorization requires JWT authentication")
    if authorizer is None and authentication is not None:
        authorizer = RBACAuthorizer()
    if isinstance(services, CogniStoreGateway):
        if tenancy is None:
            tenancy = services.tenancy
        elif services.tenancy is not None and services.tenancy is not tenancy:
            raise ValueError("Conflicting tenant policies")
        services.tenancy = tenancy
    if tenancy is not None and authentication is None:
        raise ValueError("API tenant isolation requires JWT authentication")

    def audit_catalog():
        return services.catalog if isinstance(services, CogniStoreGateway) else None

    owns_authenticator = isinstance(authentication, JWTAuthConfig)
    authenticator = (
        JWTAuthenticator(authentication) if isinstance(authentication, JWTAuthConfig)
        else authentication
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            configure_observability()
            await services.startup()
            try:
                yield
            finally:
                await services.shutdown()
        finally:
            if owns_authenticator and authenticator is not None:
                await asyncio.to_thread(authenticator.close)

    app = FastAPI(
        title="CogniStore REST API",
        summary="Versioned object, catalog, Ask, policy, and action operations",
        description=(
            "Stable version 1 REST interface. Deployments can require JWT bearer "
            "authentication with OIDC discovery and explicit role-based permissions. "
            "Authenticated identities without configured role bindings are denied."
        ),
        version="1.0.0",
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.state.gateway = services
    app.state.authenticator = authenticator
    app.state.authorizer = authorizer
    app.state.tenancy = tenancy

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        supplied = request.headers.get("x-request-id")
        if not hasattr(request.state, "request_id"):
            request.state.request_id = (
                supplied if supplied is not None and _REQUEST_ID.fullmatch(supplied) else str(uuid4())
            )
        request.state.principal = None
        request.state.tenant_id = None
        route_path = get_route_path(request.scope)
        is_protected_view = (
            route_path == "/v1/audit" or route_path.startswith("/v1/audit/")
            or route_path == "/v1/admin" or route_path.startswith("/v1/admin/")
            or route_path.rstrip("/") == "/v1/jobs"
        )
        if is_protected_view and authenticator is None:
            await audit_http_denial(request)
            return _error_response(
                request, status_code=401, code="authentication_required",
                message="Operational access requires bearer authentication",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if authenticator is not None and (route_path == "/v1" or route_path.startswith("/v1/")):
            authorization = request.headers.getlist("authorization")
            if not authorization:
                await audit_http_denial(request)
                return _error_response(
                    request,
                    status_code=401,
                    code="authentication_required",
                    message="A bearer access token is required",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                if len(authorization) != 1 or len(authorization[0]) > 16_400:
                    raise AuthenticationError()
                scheme, separator, token = authorization[0].partition(" ")
                if scheme.lower() != "bearer" or not separator or not token or token != token.strip():
                    raise AuthenticationError()
                request.state.principal = await asyncio.to_thread(authenticator.authenticate, token)
            except AuthenticationError:
                await audit_http_denial(request)
                return _error_response(
                    request,
                    status_code=401,
                    code="invalid_token",
                    message="The bearer access token is invalid",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
        if route_path == "/v1" or route_path.startswith("/v1/"):
            operation = endpoint_operation(request.method, route_path)
            try:
                if tenancy is not None:
                    request.state.tenant_id = await asyncio.to_thread(
                        tenancy.resolve, request.state.principal,
                    )
                with tenant_context(request.state.tenant_id):
                    await asyncio.to_thread(
                        authorize_operation,
                        authorizer,
                        OPERATION_PERMISSIONS.get(operation, ()),
                        principal=request.state.principal,
                        operation=operation,
                        boundary="api",
                        catalog=audit_catalog(),
                        correlation_id=request.state.request_id,
                        require_authenticated=operation in AUTHENTICATED_OPERATIONS,
                        require_any=operation in ANY_PERMISSION_OPERATIONS,
                    )
            except (AuthorizationError, TenantIsolationError) as exc:
                if isinstance(exc, TenantIsolationError):
                    await audit_http_denial(request)
                return _error_response(
                    request, status_code=403, code="forbidden", message="Operation not permitted",
                )
        operation_id = request.headers.get("idempotency-key", request.state.request_id)
        if _REQUEST_ID.fullmatch(operation_id) is None:
            return _error_response(
                request,
                status_code=422,
                code="validation_error",
                message="Idempotency-Key must contain 1-128 letters, digits, dots, underscores, colons or hyphens",
            )
        if request.method == "POST" and (
            route_path in _JSON_BODY_PATHS
            or endpoint_operation(request.method, route_path) == "release_legal_hold"
        ):
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
        with tenant_context(request.state.tenant_id), authorization_context(authorizer), principal_context(request.state.principal), access_operation(
            operation_id=operation_id,
            correlation_id=request.state.request_id,
            source="api",
        ):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if route_path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def audit_http_denial(request: Request) -> None:
        operation = endpoint_operation(request.method, get_route_path(request.scope))
        await asyncio.to_thread(
            audit_authorization_denial,
            getattr(request.state, "principal", None),
            OPERATION_PERMISSIONS.get(operation, ()),
            operation=operation,
            boundary="api_result",
            catalog=audit_catalog(),
            correlation_id=_request_id(request),
        )

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        if exc.status_code in (401, 403):
            await audit_http_denial(request)
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            headers=exc.headers,
        )

    @app.exception_handler(AuthorizationError)
    async def authorization_error(request: Request, _exc: AuthorizationError) -> JSONResponse:
        # Hooks may raise this error directly without recording a decision.
        # Also retain the final API denial when a service recheck rejects a
        # request that passed the earlier transport check.
        await audit_http_denial(request)
        return _error_response(
            request, status_code=403, code="forbidden", message="Operation not permitted",
        )

    @app.exception_handler(TenantIsolationError)
    async def tenant_error(request: Request, _exc: TenantIsolationError) -> JSONResponse:
        await audit_http_denial(request)
        return _error_response(
            request, status_code=403, code="forbidden", message="Operation not permitted",
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
        if exc.status_code in (401, 403):
            await audit_http_denial(request)
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

    @app.exception_handler(LegalHoldError)
    async def legal_hold_error(request: Request, exc: LegalHoldError) -> JSONResponse:
        return _error_response(
            request, status_code=409, code="legal_hold", message=str(exc),
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
    # Authentication happens in middleware before any body parsing or custom
    # authorization hook. This dependency documents the bearer scheme in OpenAPI.
    bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", bearerFormat="JWT")
    router = APIRouter(
        prefix="/v1", dependencies=[Depends(bearer), Depends(auth), Depends(access_headers)]
    )
    @app.get(
        "/healthz",
        response_model=HealthResponse,
        operation_id="getHealth",
        tags=["system"],
        responses={200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}}},
    )
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/readyz",
        response_model=HealthResponse,
        operation_id="getReadiness",
        tags=["system"],
        responses={503: _COMMON_ERROR_RESPONSES[503]},
    )
    async def readiness(request: Request) -> HealthResponse | JSONResponse:
        try:
            ready = await asyncio.wait_for(services.check_readiness(), timeout=2.0)
        except Exception:
            ready = False
        if not ready:
            return _error_response(
                request,
                status_code=503,
                code="backend_unavailable",
                message="A required backend is unavailable",
                retryable=True,
                headers={"Retry-After": "1"},
            )
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
        "/legal-holds",
        response_model=LegalHoldResource,
        status_code=status.HTTP_201_CREATED,
        operation_id="placeLegalHold",
        tags=["legal-holds"],
        description=(
            "Place a hold on an exact key, a literal key prefix, or an entire bucket. "
            "Matching conservatively includes case, Unicode, and path aliases. "
            "Requires an authenticated principal with legal_hold_manage. "
            "Held scopes reject all object writes, deletion, and tier movement."
        ),
        responses={
            201: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            **_JSON_ERROR_RESPONSES,
        },
    )
    def place_legal_hold(change: LegalHoldRequest, request: Request) -> LegalHoldResource:
        return services.place_legal_hold(change, correlation_id=_request_id(request))

    @router.get(
        "/legal-holds",
        response_model=LegalHoldList,
        operation_id="listLegalHolds",
        tags=["legal-holds"],
        description=(
            "Inspect retained holds within the current tenant. A key filter includes "
            "all object, prefix, and bucket holds covering that key, including case, "
            "Unicode, and path aliases; key requires bucket."
        ),
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def list_legal_holds(
        bucket: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        key: Annotated[str | None, Query(min_length=1, max_length=8192)] = None,
        active_only: Annotated[bool, Query()] = False,
    ) -> LegalHoldList:
        return services.list_legal_holds(bucket=bucket, key=key, active_only=active_only)

    @router.post(
        "/legal-holds/{hold_id}/release",
        response_model=LegalHoldResource,
        operation_id="releaseLegalHold",
        tags=["legal-holds"],
        description=(
            "Release one hold while retaining its placement and release evidence. "
            "Requires an authenticated principal with legal_hold_release. "
            "Other overlapping active holds continue to protect the object."
        ),
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            409: _CONFLICT_RESPONSE,
            **_JSON_ERROR_RESPONSES,
        },
    )
    def release_legal_hold(
        hold_id: Annotated[str, Path(pattern=_UUID)],
        change: LegalHoldReleaseRequest,
        request: Request,
    ) -> LegalHoldResource:
        return services.release_legal_hold(hold_id, change, correlation_id=_request_id(request))

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
        "/catalog/importance",
        response_model=PolicyEvaluationResponse,
        operation_id="setObjectImportance",
        tags=["catalog"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            **_JSON_ERROR_RESPONSES,
        },
    )
    def set_object_importance(
        change: ImportanceChangeRequest, request: Request
    ) -> PolicyEvaluationResponse:
        return services.set_importance(change, correlation_id=_request_id(request))

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
        "/policies/preview",
        response_model=PolicyDecisionResource,
        operation_id="previewPolicyDecision",
        tags=["policies"],
        description="Evaluate without persisting a decision or executing a move. The diff is a proposal only.",
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            **_JSON_ERROR_RESPONSES,
        },
    )
    def preview_policy(request: PolicyEvaluationRequest) -> PolicyDecisionResource:
        return services.preview_policy(request)

    @router.get(
        "/policy-decisions",
        response_model=PolicyDecisionPage,
        operation_id="listPolicyDecisions",
        tags=["policies"],
        description="Newest retained decisions first. Placement is frozen at evaluation; execution is observed separately.",
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def list_policy_decisions(
        bucket: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        key: Annotated[str | None, Query(min_length=1, max_length=8192)] = None,
        job_id: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        correlation_id: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=16384)] = None,
    ) -> PolicyDecisionPage:
        return services.list_policy_decisions(
            bucket=bucket, key=key, job_id=job_id, correlation_id=correlation_id,
            limit=limit, cursor=cursor,
        )

    @router.get(
        "/policy-decisions/{decision_id}",
        response_model=PolicyDecisionResource,
        operation_id="getPolicyDecision",
        tags=["policies"],
        responses={
            200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
            404: _NOT_FOUND_RESPONSE,
            **_COMMON_ERROR_RESPONSES,
        },
    )
    def get_policy_decision(
        decision_id: Annotated[str, Path(pattern=_UUID)],
    ) -> PolicyDecisionResource:
        return services.get_policy_decision(decision_id)

    @router.get(
        "/audit/events",
        response_model=AuditEventPage,
        operation_id="listAuditEvents",
        tags=["audit"],
        description="List this tenant's retained events, newest first. Every disclosure is audited.",
        responses={200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}}, **_COMMON_ERROR_RESPONSES},
    )
    def list_audit_events(
        bucket: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        key: Annotated[str | None, Query(min_length=1, max_length=8192)] = None,
        job_id: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        correlation_id: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        actor_id: Annotated[str | None, Query(min_length=1, max_length=8192)] = None,
        event_type: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        outcome: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        occurred_after: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        occurred_before: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=16384)] = None,
    ) -> AuditEventPage:
        return services.list_audit_events(
            bucket=bucket, key=key, job_id=job_id, correlation_id=correlation_id,
            actor_id=actor_id, event_type=event_type, outcome=outcome,
            occurred_after=occurred_after, occurred_before=occurred_before,
            limit=limit, cursor=cursor,
        )

    @router.get(
        "/audit/events/{event_id}",
        response_model=AuditEventResource,
        operation_id="getAuditEvent",
        tags=["audit"],
        responses={200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}},
                   404: _NOT_FOUND_RESPONSE, **_COMMON_ERROR_RESPONSES},
    )
    def get_audit_event(event_id: Annotated[str, Path(pattern=_UUID)]) -> AuditEventResource:
        return services.get_audit_event(event_id)

    @router.get(
        "/audit/export",
        response_model=AuditExportResponse,
        operation_id="exportAuditEvents",
        tags=["audit"],
        description=(
            "Export a bounded page of tenant integrity evidence and retained event payloads. "
            "Follow page.next_cursor until complete is true. The checkpoint fixes the export "
            "boundary; subsequent access events belong to a later export. Preserve checkpoints "
            "independently to detect complete history rewrites."
        ),
        responses={200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}}, **_COMMON_ERROR_RESPONSES},
    )
    def export_audit_events(
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        cursor: Annotated[str | None, Query(min_length=1, max_length=16384)] = None,
    ) -> AuditExportResponse:
        return services.export_audit_events(limit=limit, cursor=cursor)

    @router.post(
        "/audit/verify",
        response_model=AuditVerificationResponse,
        operation_id="verifyAuditIntegrity",
        tags=["audit"],
        description="Verify this tenant's retained history against an optional independently saved checkpoint.",
        responses={200: {"headers": {"X-Request-ID": _REQUEST_ID_HEADER}}, **_JSON_ERROR_RESPONSES},
    )
    def verify_audit_integrity(request: AuditVerificationRequest) -> AuditVerificationResponse:
        return services.verify_audit_integrity(request)

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

    @router.get(
        "/admin/session", response_model=AdminSession, operation_id="getAdminSession",
        tags=["administration"], responses=_COMMON_ERROR_RESPONSES,
    )
    def get_admin_session() -> AdminSession:
        return services.get_admin_session()

    @router.get(
        "/admin/storage", response_model=AdminStorage, operation_id="getAdminStorage",
        tags=["administration"], responses=_COMMON_ERROR_RESPONSES,
    )
    async def get_admin_storage() -> AdminStorage:
        return await services.get_admin_storage()

    @router.get(
        "/jobs", response_model=JobHistoryPage, operation_id="listJobs",
        tags=["actions"], responses=_COMMON_ERROR_RESPONSES,
    )
    def list_jobs(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=16_384)] = None,
    ) -> JobHistoryPage:
        return services.list_jobs(limit=limit, cursor=cursor)

    @router.get(
        "/admin/repairs", response_model=RepairListResponse, operation_id="listRepairs",
        tags=["administration"], responses=_COMMON_ERROR_RESPONSES,
    )
    def list_repairs() -> RepairListResponse:
        return services.list_repairs()

    @router.get(
        "/admin/repairs/{repair_id}", response_model=RepairStatusResponse,
        operation_id="getRepair", tags=["administration"],
        responses={404: _NOT_FOUND_RESPONSE, **_COMMON_ERROR_RESPONSES},
    )
    def get_repair(repair_id: Annotated[str, Path(min_length=1, max_length=128)]) -> RepairStatusResponse:
        return services.get_repair(repair_id)

    @router.post(
        "/admin/repairs/preview", response_model=RepairPreviewResponse,
        operation_id="previewRepair", tags=["administration"],
        responses={404: _NOT_FOUND_RESPONSE, 409: _CONFLICT_RESPONSE, **_JSON_ERROR_RESPONSES},
    )
    def preview_repair(request: RepairPreviewRequest) -> RepairPreviewResponse:
        return services.preview_repair(request)

    @router.post(
        "/admin/repairs", response_model=RepairStatusResponse,
        operation_id="submitRepair", tags=["administration"],
        responses={404: _NOT_FOUND_RESPONSE, 409: _CONFLICT_RESPONSE, **_JSON_ERROR_RESPONSES},
    )
    def submit_repair(request: RepairSubmitRequest) -> RepairStatusResponse:
        return services.submit_repair(request)

    # Keep permission requirements visible in the deterministic API contract.
    for route in router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                operation = ENDPOINT_OPERATIONS[(method, route.path)]
                route.openapi_extra = {
                    **(route.openapi_extra or {}),
                    "x-required-permissions": [p.value for p in OPERATION_PERMISSIONS[operation]],
                    "x-permission-mode": "any" if operation in ANY_PERMISSION_OPERATIONS else "all",
                }
    app.include_router(router)
    register_content_search_ui(app)
    register_http_routes(
        getattr(route, "path", "") for route in [*app.routes, *router.routes]
    )

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        if tenancy is not None:
            # Process-wide counters aggregate all tenants. They belong on a
            # private operational exporter, never the shared tenant HTTP surface.
            return Response(status_code=404)
        body, content_type = metrics_response()
        body += budget_metrics_response(getattr(services, "catalog", None))
        return Response(body, headers={"Content-Type": content_type})

    app.add_middleware(TelemetryMiddleware)
    return app
