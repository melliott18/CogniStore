"""Versioned embedding spaces and provider-neutral embedding contracts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.client import HTTPException, InvalidURL
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
from numbers import Real
from pathlib import Path
from typing import IO, Any, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import NAMESPACE_URL, UUID, uuid5

from cognistore.encryption import require_tls_url, tls_context

EMBEDDING_SPACE_SCHEMA_VERSION = 1
EMBEDDING_PROVIDER_PROTOCOL_VERSION = 1
EMBEDDING_DISTANCE_METRIC = "cosine"
EMBEDDING_NORMALIZATION = "l2"
EMBEDDING_NORMALIZATION_VERSION = 1
SENTENCE_TRANSFORMERS_PROVIDER_IMPLEMENTATION = "cognistore-sentence-transformers"
SENTENCE_TRANSFORMERS_PROVIDER_IMPLEMENTATION_VERSION = 1
OPENAI_COMPATIBLE_PROVIDER_IMPLEMENTATION = "cognistore-openai-compatible"
OPENAI_COMPATIBLE_PROVIDER_IMPLEMENTATION_VERSION = 1
DEFAULT_EMBEDDING_MAX_BATCH_SIZE = 128
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_EMBEDDING_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_EMBEDDING_SPACE_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://cognistore.dev/identities/embedding-spaces",
)
_MUTABLE_REVISION_NAMES = frozenset(
    {
        "current",
        "default",
        "dev",
        "development",
        "head",
        "latest",
        "main",
        "master",
        "prod",
        "production",
        "stable",
        "staging",
        "test",
        "trunk",
    }
)
_SENTENCE_TRANSFORMER_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SENTENCE_TRANSFORMER_MODEL_ID = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_CONTENT_VERSION = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENTENCE_TRANSFORMERS_RUNTIME_PACKAGES = (
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "torch",
    "numpy",
)
_T = TypeVar("_T")
EmbeddingVector = tuple[float, ...]
EmbeddingBatch = tuple[EmbeddingVector, ...]


class EmbeddingProviderError(RuntimeError):
    """Base class for safely classified provider failures."""


class TransientEmbeddingProviderError(EmbeddingProviderError):
    """A provider failure that a bounded retry may safely repeat."""


class PermanentEmbeddingProviderError(EmbeddingProviderError):
    """A provider failure that retrying unchanged input cannot repair."""


class EmbeddingProviderConfigurationError(PermanentEmbeddingProviderError):
    """A provider is unavailable or was configured unsafely."""


class EmbeddingResponseError(PermanentEmbeddingProviderError):
    """A provider returned a malformed or incompatible embedding response."""


class EmbeddingVectorValidationError(ValueError):
    """A vector batch violates its exact cardinality or geometry contract."""


def _required_identity_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string without outer whitespace")
    if "\0" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _immutable_revision(value: object, *, field: str) -> str:
    revision = _required_identity_text(value, field=field)
    folded_revision = revision.casefold()
    if folded_revision in _MUTABLE_REVISION_NAMES or folded_revision.startswith("refs/heads/"):
        raise ValueError(f"{field} must identify an immutable model/deployment version")
    return revision


def _sentence_transformer_revision(value: object) -> str:
    revision = _immutable_revision(value, field="revision")
    if not _SENTENCE_TRANSFORMER_COMMIT.fullmatch(revision):
        raise ValueError("revision must be a full lowercase 40-character model commit SHA")
    return revision


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _positive_finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a positive finite number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return converted


def _identity_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EmbeddingSpace:
    """Immutable compatibility boundary for vectors sharing one search graph."""

    provider_implementation: str
    provider_implementation_version: str
    model: str
    model_revision: str
    dimensions: int
    preprocessing: str
    preprocessing_version: int = 1
    distance_metric: str = EMBEDDING_DISTANCE_METRIC
    normalization: str = EMBEDDING_NORMALIZATION
    normalization_version: int = EMBEDDING_NORMALIZATION_VERSION
    schema_version: int = EMBEDDING_SPACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field in (
            "provider_implementation",
            "provider_implementation_version",
            "model",
            "preprocessing",
        ):
            _required_identity_text(getattr(self, field), field=field)
        _immutable_revision(self.model_revision, field="model_revision")
        _positive_integer(self.dimensions, field="dimensions")
        _positive_integer(self.preprocessing_version, field="preprocessing_version")
        if self.distance_metric != EMBEDDING_DISTANCE_METRIC:
            raise ValueError(f"embedding distance_metric must be {EMBEDDING_DISTANCE_METRIC!r}")
        if self.normalization != EMBEDDING_NORMALIZATION:
            raise ValueError(f"embedding normalization must be {EMBEDDING_NORMALIZATION!r}")
        _positive_integer(
            self.normalization_version,
            field="normalization_version",
        )
        if self.normalization_version != EMBEDDING_NORMALIZATION_VERSION:
            raise ValueError(
                f"unsupported embedding normalization_version: {self.normalization_version!r}"
            )
        _positive_integer(self.schema_version, field="schema_version")
        if self.schema_version != EMBEDDING_SPACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported embedding space schema_version: {self.schema_version!r}")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_protocol_version": EMBEDDING_PROVIDER_PROTOCOL_VERSION,
            "provider_implementation": self.provider_implementation,
            "provider_implementation_version": self.provider_implementation_version,
            "model": self.model,
            "model_revision": self.model_revision,
            "dimensions": self.dimensions,
            "distance_metric": self.distance_metric,
            "normalization": self.normalization,
            "normalization_version": self.normalization_version,
            "preprocessing": self.preprocessing,
            "preprocessing_version": self.preprocessing_version,
        }

    @property
    def fingerprint(self) -> str:
        """Return the canonical lowercase SHA-256 compatibility fingerprint."""

        return _identity_fingerprint(self._identity_payload())

    @property
    def space_id(self) -> UUID:
        """Return the stable UUID for this exact embedding compatibility space."""

        return uuid5(_EMBEDDING_SPACE_NAMESPACE, self.fingerprint)

    def to_metadata(self) -> dict[str, object]:
        """Return a fresh JSON-safe representation with no credentials or URL."""

        return {
            **self._identity_payload(),
            "space_id": str(self.space_id),
            "fingerprint": self.fingerprint,
        }


class EmbeddingProvider(Protocol):
    """Provider-neutral surface used by embedding orchestration."""

    @property
    def space(self) -> EmbeddingSpace: ...

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch: ...

    def embed_query(self, text: str) -> EmbeddingVector: ...


@dataclass(frozen=True)
class EmbeddingRetryPolicy:
    """Bounded deterministic exponential backoff for transient provider work."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0

    def __post_init__(self) -> None:
        _positive_integer(self.max_attempts, field="max_attempts")
        base_delay = _positive_finite_number(
            self.base_delay_seconds,
            field="base_delay_seconds",
        )
        max_delay = _positive_finite_number(
            self.max_delay_seconds,
            field="max_delay_seconds",
        )
        if max_delay < base_delay:
            raise ValueError(
                "max_delay_seconds must be greater than or equal to base_delay_seconds"
            )

    def delay_for(self, failed_attempt: int) -> float:
        """Return the bounded delay after a 1-based failed attempt."""

        _positive_integer(failed_attempt, field="failed_attempt")
        base_delay = float(self.base_delay_seconds)
        max_delay = float(self.max_delay_seconds)
        if base_delay >= max_delay:
            return max_delay
        exponent_to_cap = math.ceil(math.log2(max_delay) - math.log2(base_delay))
        exponent = min(failed_attempt - 1, exponent_to_cap)
        if exponent >= exponent_to_cap:
            return max_delay
        return min(max_delay, math.ldexp(base_delay, exponent))


def embed_with_retry(
    operation: Callable[[], _T],
    *,
    policy: EmbeddingRetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> _T:
    """Run one idempotent provider operation with bounded transient retries."""

    retry_policy = policy or EmbeddingRetryPolicy()
    if not isinstance(retry_policy, EmbeddingRetryPolicy):
        raise ValueError("policy must be an EmbeddingRetryPolicy")
    for attempt in range(1, retry_policy.max_attempts + 1):
        try:
            return operation()
        except TransientEmbeddingProviderError:
            if attempt >= retry_policy.max_attempts:
                raise
            sleep(retry_policy.delay_for(attempt))
    raise AssertionError("retry loop did not return or raise")


def _bounded_items(value: object, *, limit: int, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise EmbeddingVectorValidationError(f"{field} must be a sequence")
    try:
        iterator = iter(value)  # type: ignore[call-overload]
    except TypeError as exc:
        raise EmbeddingVectorValidationError(f"{field} must be a sequence") from exc
    return tuple(islice(iterator, limit + 1))


def validate_embedding_vectors(
    vectors: object,
    *,
    expected_count: int,
    dimensions: int,
    distance_metric: str = EMBEDDING_DISTANCE_METRIC,
) -> EmbeddingBatch:
    """Validate and detach an exact finite, nonzero vector batch."""

    _nonnegative_count(expected_count, field="expected_count")
    _positive_integer(dimensions, field="dimensions")
    if distance_metric != EMBEDDING_DISTANCE_METRIC:
        raise ValueError(f"embedding distance_metric must be {EMBEDDING_DISTANCE_METRIC!r}")
    rows = _bounded_items(vectors, limit=expected_count, field="vectors")
    if len(rows) != expected_count:
        raise EmbeddingVectorValidationError(
            f"expected {expected_count} embedding vectors, received {len(rows)}"
        )

    validated: list[EmbeddingVector] = []
    for row_index, row in enumerate(rows):
        values = _bounded_items(
            row,
            limit=dimensions,
            field=f"vector {row_index}",
        )
        if len(values) != dimensions:
            raise EmbeddingVectorValidationError(
                f"vector {row_index} must have exactly {dimensions} dimensions"
            )
        converted: list[float] = []
        for column_index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, Real):
                raise EmbeddingVectorValidationError(
                    f"vector {row_index} component {column_index} must be numeric"
                )
            try:
                component = float(value)
            except (OverflowError, ValueError) as exc:
                raise EmbeddingVectorValidationError(
                    f"vector {row_index} component {column_index} must be finite"
                ) from exc
            if not math.isfinite(component):
                raise EmbeddingVectorValidationError(
                    f"vector {row_index} component {column_index} must be finite"
                )
            converted.append(component)
        try:
            squared_norm = math.fsum(component * component for component in converted)
        except OverflowError as exc:
            raise EmbeddingVectorValidationError(
                f"vector {row_index} must have a finite nonzero norm"
            ) from exc
        if not math.isfinite(squared_norm) or squared_norm <= 0:
            raise EmbeddingVectorValidationError(
                f"vector {row_index} must have a finite nonzero norm"
            )
        validated.append(tuple(converted))
    return tuple(validated)


def _nonnegative_count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _normalize_vectors(vectors: EmbeddingBatch) -> EmbeddingBatch:
    normalized: list[EmbeddingVector] = []
    for vector in vectors:
        norm = math.sqrt(math.fsum(component * component for component in vector))
        normalized.append(tuple(component / norm for component in vector))
    return tuple(normalized)


def _validated_texts(
    texts: Sequence[str],
    *,
    max_batch_size: int,
) -> tuple[str, ...]:
    if isinstance(texts, (str, bytes, bytearray)) or not isinstance(texts, Sequence):
        raise TypeError("texts must be a sequence of strings")
    if len(texts) > max_batch_size:
        raise ValueError(f"embedding batch exceeds max_batch_size={max_batch_size}")
    detached = tuple(texts)
    for text in detached:
        if not isinstance(text, str) or not text:
            raise ValueError("embedding inputs must be non-empty strings")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("embedding inputs must be valid Unicode") from exc
    return detached


class _SentenceTransformerModel(Protocol):
    def encode(self, sentences: list[str], **kwargs: object) -> object: ...


SentenceTransformerModelFactory = Callable[[str, str], _SentenceTransformerModel]


def _existing_local_model_path(model: str) -> bool:
    """Fail closed when a nominal Hub ID can be resolved as a local path."""

    try:
        path = Path(model)
        return path.exists() or path.is_symlink()
    except OSError:
        return True


def _sentence_transformers_runtime_identity() -> str:
    return ";".join(
        f"{package}={_package_version(package)}"
        for package in _SENTENCE_TRANSFORMERS_RUNTIME_PACKAGES
    )


class SentenceTransformersEmbeddingProvider:
    """Lazy optional sentence-transformers provider pinned to a model revision."""

    def __init__(
        self,
        *,
        model: str,
        revision: str,
        dimensions: int,
        max_batch_size: int = DEFAULT_EMBEDDING_MAX_BATCH_SIZE,
        model_factory: SentenceTransformerModelFactory | None = None,
        provider_implementation_version: str | None = None,
    ) -> None:
        model = _required_identity_text(model, field="model")
        if model_factory is None:
            _require_hub_tls()
            if not _SENTENCE_TRANSFORMER_MODEL_ID.fullmatch(model) or _existing_local_model_path(
                model
            ):
                raise ValueError(
                    "model must be an unambiguous Hub model ID when using the "
                    "default loader; local model paths require a custom "
                    "model_factory and a content-digest "
                    "provider_implementation_version"
                )
        revision = _sentence_transformer_revision(revision)
        dimensions = _positive_integer(dimensions, field="dimensions")
        self._max_batch_size = _positive_integer(
            max_batch_size,
            field="max_batch_size",
        )
        if model_factory is not None:
            if not isinstance(provider_implementation_version, str) or not (
                _CONTENT_VERSION.fullmatch(provider_implementation_version)
            ):
                raise ValueError(
                    "a custom model_factory requires "
                    "provider_implementation_version='sha256:<64 lowercase hex>', "
                    "covering the adapter, model artifacts, and inference runtime"
                )
        elif provider_implementation_version is not None:
            raise ValueError(
                "provider_implementation_version may only be set with a custom "
                "model_factory; the default loader always versions its complete "
                "inference runtime"
            )
        runtime_identity = _sentence_transformers_runtime_identity()
        implementation_version = (
            f"{SENTENCE_TRANSFORMERS_PROVIDER_IMPLEMENTATION_VERSION};{runtime_identity}"
            if provider_implementation_version is None
            else provider_implementation_version
        )
        self._space = EmbeddingSpace(
            provider_implementation=SENTENCE_TRANSFORMERS_PROVIDER_IMPLEMENTATION,
            provider_implementation_version=implementation_version,
            model=model,
            model_revision=revision,
            dimensions=dimensions,
            preprocessing="sentence-transformers-encode",
            preprocessing_version=1,
        )
        self._model_factory = model_factory
        self._runtime_identity = runtime_identity
        self._model_instance: _SentenceTransformerModel | None = None
        self._model_lock = threading.Lock()

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def model_loaded(self) -> bool:
        """Return whether lazy model construction has already happened."""

        return self._model_instance is not None

    def _model(self) -> _SentenceTransformerModel:
        if self._model_instance is not None:
            return self._model_instance
        with self._model_lock:
            if self._model_instance is None:
                if self._model_factory is None:
                    if _existing_local_model_path(self.space.model):
                        raise EmbeddingProviderConfigurationError(
                            "the configured Hub model ID resolves to a mutable "
                            "local path; construct a new provider with an "
                            "unambiguous model ID"
                        )
                    observed_runtime = _sentence_transformers_runtime_identity()
                    if observed_runtime != self._runtime_identity:
                        raise EmbeddingProviderConfigurationError(
                            "the sentence-transformers inference runtime changed "
                            "after the embedding space was constructed"
                        )
                factory = self._model_factory or _load_sentence_transformer
                try:
                    self._model_instance = factory(
                        self.space.model,
                        self.space.model_revision,
                    )
                except EmbeddingProviderError:
                    raise
                except Exception as exc:
                    raise EmbeddingProviderConfigurationError(
                        "sentence-transformers model could not be loaded"
                    ) from exc
        return self._model_instance

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        detached = _validated_texts(texts, max_batch_size=self._max_batch_size)
        if not detached:
            return ()
        try:
            output = self._model().encode(
                list(detached),
                batch_size=self._max_batch_size,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            vectors = validate_embedding_vectors(
                output,
                expected_count=len(detached),
                dimensions=self.space.dimensions,
            )
        except EmbeddingProviderError:
            raise
        except EmbeddingVectorValidationError as exc:
            raise EmbeddingResponseError(
                "sentence-transformers returned incompatible vectors"
            ) from exc
        except Exception as exc:
            raise PermanentEmbeddingProviderError("sentence-transformers inference failed") from exc
        return _normalize_vectors(vectors)

    def embed_query(self, text: str) -> EmbeddingVector:
        return self.embed_documents((text,))[0]


def _require_hub_tls() -> None:
    try:
        require_tls_url(os.environ.get("HF_ENDPOINT", "https://huggingface.co"), "Model hub")
    except ValueError:
        raise EmbeddingProviderConfigurationError(
            "Production model hub endpoints require verified HTTPS"
        ) from None


def _load_sentence_transformer(model: str, revision: str) -> _SentenceTransformerModel:
    _require_hub_tls()
    if _existing_local_model_path(model):
        raise EmbeddingProviderConfigurationError(
            "the configured Hub model ID resolves to a mutable local path"
        )
    try:
        sentence_transformers = import_module("sentence_transformers")
    except ImportError as exc:
        raise EmbeddingProviderConfigurationError(
            "sentence-transformers is optional; install it to use this provider"
        ) from exc
    SentenceTransformer = sentence_transformers.SentenceTransformer
    return SentenceTransformer(model, revision=revision, device="cpu")


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class OpenAICompatibleEmbeddingProvider:
    """Bounded stdlib client for an OpenAI-compatible ``/v1/embeddings`` API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        deployment_version: str,
        dimensions: int,
        timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        max_batch_size: int = DEFAULT_EMBEDDING_MAX_BATCH_SIZE,
        max_request_bytes: int = DEFAULT_EMBEDDING_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_EMBEDDING_MAX_RESPONSE_BYTES,
        request_dimensions: bool = False,
        provider_implementation_version: str | None = None,
    ) -> None:
        self._endpoint_url = _embedding_endpoint(base_url)
        self._api_key = _validated_api_key(api_key)
        self._timeout_seconds = _positive_finite_number(
            timeout_seconds,
            field="timeout_seconds",
        )
        self._max_batch_size = _positive_integer(
            max_batch_size,
            field="max_batch_size",
        )
        self._max_request_bytes = _positive_integer(
            max_request_bytes,
            field="max_request_bytes",
        )
        self._max_response_bytes = _positive_integer(
            max_response_bytes,
            field="max_response_bytes",
        )
        if not isinstance(request_dimensions, bool):
            raise ValueError("request_dimensions must be a boolean")
        self._request_dimensions = request_dimensions
        implementation_version = (
            str(OPENAI_COMPATIBLE_PROVIDER_IMPLEMENTATION_VERSION)
            if provider_implementation_version is None
            else provider_implementation_version
        )
        endpoint_fingerprint = hashlib.sha256(self._endpoint_url.encode("utf-8")).hexdigest()
        implementation_version = f"{implementation_version};endpoint-sha256={endpoint_fingerprint}"
        self._space = EmbeddingSpace(
            provider_implementation=OPENAI_COMPATIBLE_PROVIDER_IMPLEMENTATION,
            provider_implementation_version=implementation_version,
            model=_required_identity_text(model, field="model"),
            model_revision=_immutable_revision(
                deployment_version,
                field="deployment_version",
            ),
            dimensions=_positive_integer(dimensions, field="dimensions"),
            preprocessing=(
                "openai-compatible-input-explicit-dimensions"
                if request_dimensions
                else "openai-compatible-input-provider-default-dimensions"
            ),
            preprocessing_version=1,
        )
        self._opener = build_opener(_RejectRedirects(), HTTPSHandler(context=tls_context()))

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        detached = _validated_texts(texts, max_batch_size=self._max_batch_size)
        if not detached:
            return ()
        request_payload: dict[str, object] = {
            "model": self.space.model,
            "input": detached,
            "encoding_format": "float",
        }
        if self._request_dimensions:
            request_payload["dimensions"] = self.space.dimensions
        request_body = json.dumps(
            request_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request_body) > self._max_request_bytes:
            raise ValueError(
                f"embedding request exceeds max_request_bytes={self._max_request_bytes}"
            )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(
            self._endpoint_url,
            data=request_body,
            headers=headers,
            method="POST",
        )
        payload = self._send(request)
        try:
            parsed = json.loads(payload)
        except (ValueError, RecursionError) as exc:
            raise EmbeddingResponseError("embedding API returned invalid JSON") from exc
        return self._vectors_from_response(parsed, expected_count=len(detached))

    def embed_query(self, text: str) -> EmbeddingVector:
        return self.embed_documents((text,))[0]

    def _send(self, request: Request) -> bytes:
        try:
            with self._opener.open(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                status = response.getcode()
                if not isinstance(status, int) or status < 200 or status >= 300:
                    _raise_for_http_status(status if isinstance(status, int) else 0)
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise EmbeddingResponseError(
                            "embedding API returned an invalid Content-Length"
                        ) from exc
                    if declared_length < 0 or declared_length > self._max_response_bytes:
                        raise EmbeddingResponseError(
                            "embedding API response exceeds the configured byte limit"
                        )
                payload = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            _raise_for_http_status(exc.code)
        except EmbeddingProviderError:
            raise
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise EmbeddingProviderConfigurationError(
                    "embedding API TLS certificate verification failed"
                ) from exc
            raise TransientEmbeddingProviderError("embedding API network request failed") from exc
        except InvalidURL as exc:
            raise EmbeddingProviderConfigurationError("embedding API URL is invalid") from exc
        except (
            TimeoutError,
            socket.timeout,
            ConnectionError,
            HTTPException,
            OSError,
        ) as exc:
            raise TransientEmbeddingProviderError("embedding API network request failed") from exc
        if len(payload) > self._max_response_bytes:
            raise EmbeddingResponseError("embedding API response exceeds the configured byte limit")
        return payload

    def _vectors_from_response(
        self,
        payload: object,
        *,
        expected_count: int,
    ) -> EmbeddingBatch:
        if not isinstance(payload, dict):
            raise EmbeddingResponseError("embedding API response must be an object")
        response_model = payload.get("model")
        if not isinstance(response_model, str) or response_model != self.space.model:
            raise EmbeddingResponseError(
                "embedding API response model does not match the requested model"
            )
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingResponseError(
                "embedding API response cardinality does not match the request"
            )
        missing = object()
        raw_vectors: list[object] = [missing] * expected_count
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingResponseError(
                    "embedding API response indexes are missing, duplicated, or invalid"
                )
            observed_index = item.get("index")
            if (
                isinstance(observed_index, bool)
                or not isinstance(observed_index, int)
                or observed_index < 0
                or observed_index >= expected_count
                or raw_vectors[observed_index] is not missing
            ):
                raise EmbeddingResponseError(
                    "embedding API response indexes are missing, duplicated, or invalid"
                )
            if "embedding" not in item:
                raise EmbeddingResponseError("embedding API response item is missing its vector")
            raw_vectors[observed_index] = item["embedding"]
        try:
            vectors = validate_embedding_vectors(
                raw_vectors,
                expected_count=expected_count,
                dimensions=self.space.dimensions,
            )
        except EmbeddingVectorValidationError as exc:
            raise EmbeddingResponseError("embedding API returned incompatible vectors") from exc
        return _normalize_vectors(vectors)


def _raise_for_http_status(status: int) -> None:
    if status in {408, 409, 425, 429} or 500 <= status <= 599:
        raise TransientEmbeddingProviderError(f"embedding API transient HTTP status {status}")
    raise PermanentEmbeddingProviderError(f"embedding API permanent HTTP status {status}")


def _validated_api_key(api_key: object) -> str | None:
    if api_key is None:
        return None
    if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
        raise EmbeddingProviderConfigurationError(
            "api_key must be a non-empty string without outer whitespace"
        )
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise EmbeddingProviderConfigurationError(
            "api_key must contain only safe ASCII header characters"
        ) from exc
    if any(ord(character) < 33 or ord(character) > 126 for character in api_key):
        raise EmbeddingProviderConfigurationError(
            "api_key must contain only safe ASCII header characters"
        )
    return api_key


def _embedding_endpoint(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise EmbeddingProviderConfigurationError("base_url must be a non-empty absolute URL")
    if any(ord(character) <= 32 or ord(character) > 126 for character in base_url):
        raise EmbeddingProviderConfigurationError(
            "base_url must contain only printable ASCII URL characters"
        )
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise EmbeddingProviderConfigurationError("base_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EmbeddingProviderConfigurationError("base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise EmbeddingProviderConfigurationError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise EmbeddingProviderConfigurationError(
            "base_url must not contain a query string or fragment"
        )
    try:
        require_tls_url(base_url, "Embedding")
    except ValueError:
        raise EmbeddingProviderConfigurationError(
            "Production embedding endpoints require verified HTTPS"
        ) from None
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise EmbeddingProviderConfigurationError(
            "unencrypted HTTP embedding endpoints are restricted to loopback hosts"
        )
    if port is not None and not 1 <= port <= 65535:
        raise EmbeddingProviderConfigurationError("base_url port is invalid")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = f"{path}/embeddings"
    elif not path.endswith("/v1/embeddings"):
        path = f"{path}/v1/embeddings"
    endpoint = SplitResult(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=path,
        query="",
        fragment="",
    )
    return urlunsplit(endpoint)


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
