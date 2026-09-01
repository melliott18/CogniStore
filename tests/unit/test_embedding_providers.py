from __future__ import annotations

import json
import math
import ssl
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import URLError

import pytest

import cognistore.core.embeddings as embeddings_module
from cognistore.core.embeddings import (
    EmbeddingProvider,
    EmbeddingProviderConfigurationError,
    EmbeddingResponseError,
    EmbeddingRetryPolicy,
    EmbeddingSpace,
    EmbeddingVectorValidationError,
    OpenAICompatibleEmbeddingProvider,
    PermanentEmbeddingProviderError,
    SentenceTransformersEmbeddingProvider,
    TransientEmbeddingProviderError,
    embed_with_retry,
    validate_embedding_vectors,
)

MODEL_COMMIT = "a" * 40
CUSTOM_PROVIDER_DIGEST = "sha256:" + "b" * 64


def _space(**changes: object) -> EmbeddingSpace:
    values: dict[str, object] = {
        "provider_implementation": "test-provider",
        "provider_implementation_version": "1.2.3",
        "model": "test/model",
        "model_revision": "commit-0123456789",
        "dimensions": 2,
        "preprocessing": "test-input",
    }
    values.update(changes)
    return EmbeddingSpace(**values)  # type: ignore[arg-type]


def test_embedding_space_is_stable_immutable_and_json_safe() -> None:
    first = _space()
    second = _space()

    assert first.space_id == second.space_id
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert first.to_metadata()["space_id"] == str(first.space_id)
    assert first.to_metadata()["distance_metric"] == "cosine"
    assert first.to_metadata()["normalization"] == "l2"
    with pytest.raises(AttributeError):
        first.model = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_implementation", "other-provider"),
        ("provider_implementation_version", "1.2.4"),
        ("model", "other/model"),
        ("model_revision", "commit-9876543210"),
        ("dimensions", 3),
        ("preprocessing", "other-input"),
        ("preprocessing_version", 2),
    ],
)
def test_embedding_space_identity_includes_every_compatibility_field(
    field: str,
    value: object,
) -> None:
    assert replace(_space(), **{field: value}).space_id != _space().space_id


@pytest.mark.parametrize("revision", ["", "main", "latest", " master "])
def test_embedding_space_rejects_missing_or_mutable_model_revision(
    revision: str,
) -> None:
    with pytest.raises(ValueError, match="model_revision"):
        _space(model_revision=revision)


@pytest.mark.parametrize(
    "field",
    ["normalization_version", "schema_version"],
)
def test_embedding_space_rejects_boolean_versions(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        replace(_space(), **{field: True})


@pytest.mark.parametrize(
    "vectors,count,dimensions,message",
    [
        ([[1.0, 2.0]], 2, 2, "expected 2"),
        ([[1.0, 2.0], [3.0, 4.0]], 1, 2, "expected 1"),
        ([[1.0]], 1, 2, "exactly 2"),
        ([[1.0, 2.0, 3.0]], 1, 2, "exactly 2"),
        ([[0.0, 0.0]], 1, 2, "nonzero norm"),
        ([[math.nan, 1.0]], 1, 2, "finite"),
        ([[math.inf, 1.0]], 1, 2, "finite"),
        ([[True, 1.0]], 1, 2, "numeric"),
        ([["1", 2.0]], 1, 2, "numeric"),
        ([[10**309, 1.0]], 1, 2, "finite"),
        ([[1e154, 1e154]], 1, 2, "finite nonzero norm"),
    ],
)
def test_vector_validation_rejects_bad_cardinality_geometry_and_numbers(
    vectors: object,
    count: int,
    dimensions: int,
    message: str,
) -> None:
    with pytest.raises(EmbeddingVectorValidationError, match=message):
        validate_embedding_vectors(
            vectors,
            expected_count=count,
            dimensions=dimensions,
        )


def test_vector_validation_detaches_iterables_into_float_tuples() -> None:
    vectors = validate_embedding_vectors(
        ([index + 1, index + 2] for index in range(2)),
        expected_count=2,
        dimensions=2,
    )

    assert vectors == ((1.0, 2.0), (2.0, 3.0))


def test_retry_helper_only_retries_transient_failures_with_bounded_delays() -> None:
    calls = 0
    delays: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientEmbeddingProviderError("try again")
        return "done"

    result = embed_with_retry(
        operation,
        policy=EmbeddingRetryPolicy(
            max_attempts=3,
            base_delay_seconds=0.5,
            max_delay_seconds=0.75,
        ),
        sleep=delays.append,
    )

    assert result == "done"
    assert calls == 3
    assert delays == [0.5, 0.75]


def test_retry_helper_does_not_retry_permanent_or_exhausted_failures() -> None:
    permanent_calls = 0

    def permanent() -> None:
        nonlocal permanent_calls
        permanent_calls += 1
        raise EmbeddingResponseError("bad response")

    with pytest.raises(EmbeddingResponseError):
        embed_with_retry(permanent, sleep=lambda _delay: None)
    assert permanent_calls == 1

    transient_calls = 0

    def transient() -> None:
        nonlocal transient_calls
        transient_calls += 1
        raise TransientEmbeddingProviderError("still unavailable")

    with pytest.raises(TransientEmbeddingProviderError):
        embed_with_retry(
            transient,
            policy=EmbeddingRetryPolicy(max_attempts=2),
            sleep=lambda _delay: None,
        )
    assert transient_calls == 2


class _FakeSentenceModel:
    def __init__(self, vectors: object) -> None:
        self.vectors = vectors
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def encode(self, sentences: list[str], **kwargs: object) -> object:
        self.calls.append((sentences, kwargs))
        return self.vectors


def test_sentence_transformers_provider_is_lazy_pinned_and_test_injectable() -> None:
    fake = _FakeSentenceModel([[3.0, 4.0], [0.0, 5.0]])
    factory_calls: list[tuple[str, str]] = []

    def factory(model: str, revision: str) -> _FakeSentenceModel:
        factory_calls.append((model, revision))
        return fake

    provider = SentenceTransformersEmbeddingProvider(
        model="example/model",
        revision=MODEL_COMMIT,
        dimensions=2,
        model_factory=factory,
        provider_implementation_version=CUSTOM_PROVIDER_DIGEST,
    )
    typed_provider: EmbeddingProvider = provider

    assert provider.model_loaded is False
    assert typed_provider.space.model_revision == MODEL_COMMIT
    assert provider.space.dimensions == 2
    vectors = provider.embed_documents(("first", "second"))

    assert factory_calls == [("example/model", MODEL_COMMIT)]
    assert provider.model_loaded is True
    assert vectors == ((0.6, 0.8), (0.0, 1.0))
    assert fake.calls == [
        (
            ["first", "second"],
            {
                "batch_size": 128,
                "convert_to_numpy": True,
                "normalize_embeddings": True,
                "show_progress_bar": False,
            },
        )
    ]


def test_sentence_transformers_query_uses_same_normalized_space() -> None:
    fake = _FakeSentenceModel([[5.0, 12.0]])
    provider = SentenceTransformersEmbeddingProvider(
        model="example/model",
        revision=MODEL_COMMIT,
        dimensions=2,
        model_factory=lambda _model, _revision: fake,
        provider_implementation_version=CUSTOM_PROVIDER_DIGEST,
    )

    assert provider.embed_query("query") == pytest.approx((5 / 13, 12 / 13))


def test_sentence_transformers_validates_batch_and_output_before_returning() -> None:
    provider = SentenceTransformersEmbeddingProvider(
        model="example/model",
        revision=MODEL_COMMIT,
        dimensions=2,
        max_batch_size=1,
        model_factory=lambda _model, _revision: _FakeSentenceModel([[1.0]]),
        provider_implementation_version=CUSTOM_PROVIDER_DIGEST,
    )

    with pytest.raises(ValueError, match="max_batch_size"):
        provider.embed_documents(("one", "two"))
    with pytest.raises(EmbeddingResponseError):
        provider.embed_query("one")


@pytest.mark.parametrize("revision", ["dev", "refs/heads/main", "v1.0.0", "a" * 39])
def test_sentence_transformers_requires_a_full_immutable_commit(revision: str) -> None:
    with pytest.raises(ValueError, match="immutable|commit SHA"):
        SentenceTransformersEmbeddingProvider(
            model="example/model",
            revision=revision,
            dimensions=2,
            model_factory=lambda _model, _revision: _FakeSentenceModel([[1.0, 0.0]]),
        )


def test_sentence_transformers_custom_factory_requires_its_own_version_identity() -> None:
    with pytest.raises(ValueError, match="provider_implementation_version"):
        SentenceTransformersEmbeddingProvider(
            model="example/model",
            revision=MODEL_COMMIT,
            dimensions=2,
            model_factory=lambda _model, _revision: _FakeSentenceModel([[1.0, 0.0]]),
        )


@pytest.mark.parametrize(
    "implementation_version",
    ["adapter-1", "sha256:" + "b" * 63, "sha256:" + "B" * 64],
)
def test_sentence_transformers_custom_factory_requires_content_digest_identity(
    implementation_version: str,
) -> None:
    with pytest.raises(ValueError, match="sha256"):
        SentenceTransformersEmbeddingProvider(
            model="example/model",
            revision=MODEL_COMMIT,
            dimensions=2,
            model_factory=lambda _model, _revision: _FakeSentenceModel([[1.0, 0.0]]),
            provider_implementation_version=implementation_version,
        )


@pytest.mark.parametrize(
    "model",
    ["./local-model", "../local-model", "/models/local", "C:\\models\\local"],
)
def test_sentence_transformers_default_loader_rejects_mutable_local_paths(
    model: str,
) -> None:
    with pytest.raises(ValueError, match="Hub model ID"):
        SentenceTransformersEmbeddingProvider(
            model=model,
            revision=MODEL_COMMIT,
            dimensions=2,
        )


def test_sentence_transformers_default_loader_rejects_hub_shaped_local_directory(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example" / "model").mkdir(parents=True)

    with pytest.raises(ValueError, match="unambiguous Hub model ID"):
        SentenceTransformersEmbeddingProvider(
            model="example/model",
            revision=MODEL_COMMIT,
            dimensions=2,
        )


def test_sentence_transformers_rechecks_local_resolution_before_lazy_load(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    provider = SentenceTransformersEmbeddingProvider(
        model="example/model",
        revision=MODEL_COMMIT,
        dimensions=2,
    )
    (tmp_path / "example" / "model").mkdir(parents=True)

    with pytest.raises(EmbeddingProviderConfigurationError, match="local path"):
        provider.embed_query("query")


def test_sentence_transformers_default_space_versions_material_runtime() -> None:
    provider = SentenceTransformersEmbeddingProvider(
        model="example/model",
        revision=MODEL_COMMIT,
        dimensions=2,
    )

    identity = provider.space.provider_implementation_version
    for package in ("sentence-transformers", "transformers", "tokenizers", "torch", "numpy"):
        assert f"{package}=" in identity


def test_sentence_transformers_default_loader_rejects_identity_override() -> None:
    with pytest.raises(ValueError, match="only be set with a custom model_factory"):
        SentenceTransformersEmbeddingProvider(
            model="example/model",
            revision=MODEL_COMMIT,
            dimensions=2,
            provider_implementation_version="sha256:" + "c" * 64,
        )


def test_sentence_transformers_rejects_runtime_change_before_lazy_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = iter(("runtime-a", "runtime-b"))
    monkeypatch.setattr(
        embeddings_module,
        "_sentence_transformers_runtime_identity",
        lambda: next(identities),
    )
    provider = SentenceTransformersEmbeddingProvider(
        model="example/model",
        revision=MODEL_COMMIT,
        dimensions=2,
    )

    with pytest.raises(EmbeddingProviderConfigurationError, match="runtime changed"):
        provider.embed_query("query")


def test_sentence_transformers_custom_factory_can_name_content_versioned_local_model() -> None:
    provider = SentenceTransformersEmbeddingProvider(
        model="/models/local",
        revision=MODEL_COMMIT,
        dimensions=2,
        model_factory=lambda _model, _revision: _FakeSentenceModel([[1.0, 0.0]]),
        provider_implementation_version=CUSTOM_PROVIDER_DIGEST,
    )

    assert provider.space.model == "/models/local"


Response = tuple[int, object, dict[str, str]]


class _MockEmbeddingServer(ThreadingHTTPServer):
    responses: deque[Response]
    requests: list[dict[str, object]]


class _EmbeddingHandler(BaseHTTPRequestHandler):
    server: _MockEmbeddingServer

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": json.loads(body),
            }
        )
        status, value, configured_headers = self.server.responses.popleft()
        payload = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.send_response(status)
        headers = dict(configured_headers)
        if "Content-Length" not in headers:
            headers["Content-Length"] = str(len(payload))
        for name, header_value in headers.items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


@contextmanager
def _embedding_server(*responses: Response) -> Iterator[_MockEmbeddingServer]:
    server = _MockEmbeddingServer(("127.0.0.1", 0), _EmbeddingHandler)
    server.responses = deque(responses)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _success_response(
    *vectors: list[float],
    model: str = "text-embedding-test",
) -> dict[str, object]:
    return {
        "object": "list",
        "model": model,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
    }


def _openai_provider(
    server: _MockEmbeddingServer, **changes: object
) -> OpenAICompatibleEmbeddingProvider:
    host, port = server.server_address
    values: dict[str, object] = {
        "base_url": f"http://{host}:{port}",
        "api_key": "unit-test-secret",
        "model": "text-embedding-test",
        "deployment_version": "deployment-2026-09-01",
        "dimensions": 2,
    }
    values.update(changes)
    return OpenAICompatibleEmbeddingProvider(**values)  # type: ignore[arg-type]


def test_openai_compatible_provider_posts_bounded_ordered_batch_and_normalizes() -> None:
    response = _success_response([3.0, 4.0], [0.0, 2.0])
    with _embedding_server((200, response, {"Content-Type": "application/json"})) as server:
        provider = _openai_provider(server)

        vectors = provider.embed_documents(("first", "second"))

        assert vectors == ((0.6, 0.8), (0.0, 1.0))
        assert server.requests == [
            {
                "path": "/v1/embeddings",
                "authorization": "Bearer unit-test-secret",
                "content_type": "application/json",
                "body": {
                    "model": "text-embedding-test",
                    "input": ["first", "second"],
                    "encoding_format": "float",
                },
            }
        ]
        metadata = provider.space.to_metadata()
        assert "unit-test-secret" not in repr(provider)
        assert "unit-test-secret" not in json.dumps(metadata)
        assert "base_url" not in metadata


def test_openai_compatible_provider_can_explicitly_request_output_dimensions() -> None:
    response = _success_response([1.0, 0.0])
    with _embedding_server((200, response, {})) as server:
        provider = _openai_provider(server, request_dimensions=True)
        provider_default_dimensions = _openai_provider(server)

        assert provider.embed_query("query") == (1.0, 0.0)
        assert server.requests[0]["body"]["dimensions"] == 2  # type: ignore[index]
        assert provider.space.space_id != provider_default_dimensions.space.space_id


def test_openai_compatible_provider_accepts_exact_endpoint_and_no_api_key() -> None:
    response = _success_response([1.0, 0.0], model="model")
    with _embedding_server((200, response, {}), (200, response, {})) as server:
        host, port = server.server_address
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://{host}:{port}/v1/embeddings",
            api_key=None,
            model="model",
            deployment_version="deploy-1",
            dimensions=2,
        )

        assert provider.embed_query("query") == (1.0, 0.0)
        version_base_provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://{host}:{port}/v1",
            api_key=None,
            model="model",
            deployment_version="deploy-1",
            dimensions=2,
        )
        assert version_base_provider.embed_query("query") == (1.0, 0.0)
        assert [request["path"] for request in server.requests] == [
            "/v1/embeddings",
            "/v1/embeddings",
        ]
        assert server.requests[0]["authorization"] is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"data": []},
        {
            "model": "wrong-model",
            "data": [{"index": 0, "embedding": [1.0, 0.0]}],
        },
        {
            "model": "text-embedding-test",
            "data": [{"index": 1, "embedding": [1.0, 0.0]}],
        },
        {"model": "text-embedding-test", "data": [{"index": 0}]},
        {
            "model": "text-embedding-test",
            "data": [{"index": 0, "embedding": [1.0]}],
        },
        {
            "model": "text-embedding-test",
            "data": [{"index": 0, "embedding": [0.0, 0.0]}],
        },
        {
            "model": "text-embedding-test",
            "data": [{"index": 0, "embedding": [math.nan, 1.0]}],
        },
    ],
)
def test_openai_compatible_provider_rejects_malformed_vectors_without_retry(
    payload: object,
) -> None:
    with _embedding_server((200, payload, {})) as server:
        provider = _openai_provider(server)
        calls = 0

        def embed() -> tuple[float, ...]:
            nonlocal calls
            calls += 1
            return provider.embed_query("query")

        with pytest.raises(EmbeddingResponseError):
            embed_with_retry(embed, sleep=lambda _delay: None)
        assert calls == 1


def test_openai_compatible_provider_uses_indexes_to_restore_request_order() -> None:
    payload = {
        "model": "text-embedding-test",
        "data": [
            {"index": 1, "embedding": [0.0, 2.0]},
            {"index": 0, "embedding": [3.0, 4.0]},
        ],
    }
    with _embedding_server((200, payload, {})) as server:
        provider = _openai_provider(server)

        assert provider.embed_documents(("first", "second")) == (
            (0.6, 0.8),
            (0.0, 1.0),
        )


def test_openai_compatible_provider_wraps_bounded_json_integer_limit_errors() -> None:
    payload = b'{"model":"text-embedding-test","data":' + (b"9" * 5_000) + b"}"
    with _embedding_server((200, payload, {})) as server:
        provider = _openai_provider(server)

        with pytest.raises(EmbeddingResponseError, match="invalid JSON"):
            provider.embed_query("query")


def test_openai_compatible_provider_classifies_transient_and_permanent_http() -> None:
    transient_statuses = (408, 409, 425, 429, 500, 503, 599)
    responses = tuple((status, {"error": "redacted"}, {}) for status in transient_statuses)
    with _embedding_server(*responses, (400, {"error": "bad request"}, {})) as server:
        provider = _openai_provider(server)

        for status in transient_statuses:
            with pytest.raises(TransientEmbeddingProviderError, match=str(status)):
                provider.embed_query("query")
        with pytest.raises(PermanentEmbeddingProviderError, match="400"):
            provider.embed_query("query")


def test_openai_compatible_transient_failure_can_retry_to_success() -> None:
    with _embedding_server(
        (429, {"error": "throttled"}, {}),
        (200, _success_response([3.0, 4.0]), {}),
    ) as server:
        provider = _openai_provider(server)

        result = embed_with_retry(
            lambda: provider.embed_query("query"),
            policy=EmbeddingRetryPolicy(max_attempts=2),
            sleep=lambda _delay: None,
        )

        assert result == (0.6, 0.8)
        assert len(server.requests) == 2


def test_openai_compatible_provider_bounds_request_and_response_bytes() -> None:
    with _embedding_server(
        (200, _success_response([1.0, 0.0]), {"Content-Length": "9999"})
    ) as server:
        provider = _openai_provider(server, max_request_bytes=100, max_response_bytes=64)

        with pytest.raises(ValueError, match="request exceeds"):
            provider.embed_query("x" * 200)
        with pytest.raises(EmbeddingResponseError, match="byte limit"):
            provider.embed_query("x")


class _NetworkFailureOpener:
    def open(self, *_args: object, **_kwargs: object) -> Any:
        raise URLError("network unavailable")


class _CertificateFailureOpener:
    def open(self, *_args: object, **_kwargs: object) -> Any:
        raise URLError(ssl.SSLCertVerificationError(1, "untrusted certificate"))


def test_openai_compatible_provider_classifies_network_failures_as_transient() -> None:
    with _embedding_server() as server:
        provider = _openai_provider(server)
        provider._opener = _NetworkFailureOpener()  # type: ignore[assignment]

        with pytest.raises(TransientEmbeddingProviderError, match="network"):
            provider.embed_query("query")


def test_openai_compatible_provider_classifies_tls_trust_failure_as_permanent() -> None:
    with _embedding_server() as server:
        provider = _openai_provider(server)
        provider._opener = _CertificateFailureOpener()  # type: ignore[assignment]

        with pytest.raises(EmbeddingProviderConfigurationError, match="certificate"):
            provider.embed_query("query")


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"base_url": "http://api.example.test"}, "loopback"),
        ({"base_url": "https://key:secret@api.example.test"}, "credentials"),
        ({"base_url": "https://api.example.test?api_key=secret"}, "query"),
        ({"base_url": "not-a-url"}, "absolute HTTP"),
        ({"base_url": "https://api example.test"}, "printable ASCII"),
        ({"base_url": "https://tést.example"}, "printable ASCII"),
        ({"api_key": "key\r\nInjected: yes"}, "safe ASCII"),
        ({"deployment_version": "latest"}, "immutable"),
    ],
)
def test_openai_compatible_provider_rejects_unsafe_url_key_and_version(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "base_url": "https://api.example.test",
        "api_key": "safe-key",
        "model": "model",
        "deployment_version": "deployment-1",
        "dimensions": 2,
    }
    values.update(changes)

    with pytest.raises((EmbeddingProviderConfigurationError, ValueError), match=message):
        OpenAICompatibleEmbeddingProvider(**values)  # type: ignore[arg-type]


def test_explicit_deployment_versions_create_isolated_search_spaces() -> None:
    with _embedding_server() as server:
        first = _openai_provider(server, deployment_version="deployment-1")
        second = _openai_provider(server, deployment_version="deployment-2")

        assert first.space.model == second.space.model
        assert first.space.dimensions == second.space.dimensions
        assert first.space.space_id != second.space.space_id


def test_openai_endpoint_namespaces_are_isolated_without_persisting_urls() -> None:
    with _embedding_server() as first_server, _embedding_server() as second_server:
        first = _openai_provider(first_server)
        second = _openai_provider(second_server)

        assert first.space.model == second.space.model
        assert first.space.model_revision == second.space.model_revision
        assert first.space.space_id != second.space.space_id
        assert "http://" not in first.space.provider_implementation_version
        assert "127.0.0.1" not in first.space.provider_implementation_version
