from __future__ import annotations

import json

import httpx
import pytest

from cognistore.core.placement_llm import (
    MAX_RESPONSE_BYTES,
    PlacementInference,
    TransientPlacementError,
)
from cognistore.core.placement_llm_http import (
    HTTPPlacementProvider,
    placement_inference_from_env,
)

DECISION = '{"action":"move","dst_tier":"warm","reason":"infrequent access"}'


def provider_for(handler, **kwargs) -> HTTPPlacementProvider:
    return HTTPPlacementProvider(
        "https://placement.example.test/decide",
        model="placement-v1",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_http_adapter_wire_contract_and_secret_header():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer top-secret"
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 0.5
        )
        assert json.loads(request.content) == {
            "model": "placement-v1",
            "prompt": "eligible placement features",
            "schema": {"type": "object"},
        }
        assert b"top-secret" not in request.content
        return httpx.Response(200, content=DECISION)

    provider = provider_for(handler, api_key="top-secret")
    assert provider.complete("eligible placement features", {"type": "object"}, 0.5) == DECISION
    assert len(requests) == 1
    assert provider.provider_id == "cognistore-http-placement"
    assert provider.model == "placement-v1"
    assert provider.version == "1"
    assert provider.model_version == "1"
    assert "top-secret" not in repr(provider)
    assert "placement.example.test" not in repr(provider)


@pytest.mark.parametrize("secret", ["opaque-credential-123", 'opaque-"credential\\123'])
def test_http_adapter_redacts_echoed_configured_secret_in_response_and_audit(secret):
    def handler(request):
        return httpx.Response(
            200,
            json={"action": "move", "dst_tier": "warm", "reason": f"provider echoed {secret}"},
        )

    result = PlacementInference(provider_for(handler, api_key=secret)).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "move"
    assert "[REDACTED]" in result.reason
    assert secret not in result.reason
    assert secret not in json.dumps(result.audit)
    assert json.dumps(secret)[1:-1] not in json.dumps(result.audit)


def test_secret_redaction_cannot_repair_malformed_json_into_a_move():
    def handler(request):
        return httpx.Response(
            200, content='{"action":"move","dst_tier":"warm","reason":"invalid \\x"}'
        )

    result = PlacementInference(provider_for(handler, api_key="\\")).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "stay"
    assert result.audit["response"] is None


def test_http_adapter_redacts_unicode_escaped_ascii_secret_before_audit():
    def handler(request):
        return httpx.Response(
            200, content=r'{"action":"move","dst_tier":"warm","reason":"\u006fpaque-key"}'
        )

    result = PlacementInference(provider_for(handler, api_key="opaque-key")).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "move"
    assert result.reason == "[REDACTED]"
    assert "opaque-key" not in json.dumps(result.audit)


def test_credential_matching_redaction_marker_does_not_survive_redaction():
    def handler(request):
        return httpx.Response(
            200, json={"action": "move", "dst_tier": "warm", "reason": "echo [REDACTED]"}
        )

    result = PlacementInference(provider_for(handler, api_key="[REDACTED]")).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "move"
    assert "[REDACTED]" not in result.reason
    assert "[REDACTED]" not in json.dumps(result.audit)


@pytest.mark.parametrize(
    "body",
    [
        '{"action":"stay","action":"move","dst_tier":"warm","reason":"opaque-key"}',
        '{"action":"move","dst_tier":"warm","reason":"opaque-key","score":NaN}',
        '{"action":"move","dst_tier":"warm","reason":"opaque-key","score":Infinity}',
        '{"action":"move","dst_tier":"warm","reason":"opaque-key","score":1e400}',
    ],
)
def test_secret_redaction_rejects_duplicates_and_nonfinite_json_without_auditing_body(body):
    def handler(request):
        return httpx.Response(200, content=body)

    result = PlacementInference(provider_for(handler, api_key="opaque-key")).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "stay"
    assert result.audit["response"] is None
    assert "opaque-key" not in json.dumps(result.audit)


def test_secret_redaction_does_not_shorten_invalid_reason_into_valid_move():
    secret = "opaque-key" * 60

    def handler(request):
        return httpx.Response(200, json={"action": "move", "dst_tier": "warm", "reason": secret})

    result = PlacementInference(provider_for(handler, api_key=secret)).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "stay"
    assert secret not in json.dumps(result.audit)


def test_secret_redaction_cannot_transform_unknown_destination_into_an_allowed_tier():
    def handler(request):
        return httpx.Response(
            200, json={"action": "move", "dst_tier": "opaque-key", "reason": "move object"}
        )

    result = PlacementInference(provider_for(handler, api_key="opaque-key")).evaluate(
        current_tier="hot", size=100, allowed_tiers=["[REDACTED]"]
    )
    assert result.action == "stay"
    assert result.audit["response"] is None
    assert "opaque-key" not in json.dumps(result.audit)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_http_adapter_classifies_retryable_status_without_retrying(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content="remote-sensitive-body")

    with pytest.raises(TransientPlacementError) as error:
        provider_for(handler).complete("prompt", {}, 1.0)
    assert "remote-sensitive-body" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 422, 501])
def test_http_adapter_does_not_retry_or_follow_redirects(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.example.test/secret"},
            content="remote-sensitive-body",
        )

    with pytest.raises(RuntimeError) as error:
        provider_for(handler, api_key="credential").complete("prompt", {}, 1.0)
    assert not isinstance(error.value, TransientPlacementError)
    assert "remote-sensitive-body" not in str(error.value)
    assert "credential" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("exception", [httpx.ConnectError])
def test_http_adapter_sanitizes_retryable_transport_errors(exception):
    def handler(request):
        raise exception("credential remote-sensitive-body", request=request)

    with pytest.raises(TransientPlacementError) as error:
        provider_for(handler).complete("prompt", {}, 1.0)
    assert "credential" not in str(error.value)
    assert "remote-sensitive-body" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("failure", [408, 504, httpx.ReadTimeout, httpx.PoolTimeout])
def test_http_timeout_is_terminal_even_if_next_response_would_move(failure):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, content=DECISION)
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("credential remote-sensitive-body", request=request)

    result = PlacementInference(provider_for(handler), max_attempts=2).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "stay"
    assert result.audit["fallback_reason"] == "llm_provider_timeout"
    assert "credential" not in str(result.audit)
    assert len(requests) == 1


def test_http_adapter_does_not_retry_unclassified_transport_errors():
    def handler(request):
        raise httpx.RemoteProtocolError("private remote data", request=request)

    with pytest.raises(RuntimeError, match="transport failed") as error:
        provider_for(handler).complete("prompt", {}, 1.0)
    assert not isinstance(error.value, TransientPlacementError)


def test_inference_retries_known_http_failure_then_validates_response():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=DECISION)

    result = PlacementInference(provider_for(handler), max_attempts=2).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "move"
    assert result.dst_tier == "warm"
    assert len(requests) == 2


def test_http_vendor_envelope_is_rejected_without_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"message": DECISION})

    result = PlacementInference(provider_for(handler), max_attempts=2).evaluate(
        current_tier="hot", size=100, allowed_tiers=["warm"]
    )
    assert result.action == "stay"
    assert len(requests) == 1


class ChunkStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"x" * MAX_RESPONSE_BYTES
        yield b"x"


@pytest.mark.parametrize("declared", [True, False])
def test_http_response_byte_limit_with_or_without_content_length(declared):
    def handler(request):
        if declared:
            return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        return httpx.Response(200, stream=ChunkStream())

    with pytest.raises(RuntimeError, match="byte limit"):
        provider_for(handler).complete("prompt", {}, 1.0)


@pytest.mark.parametrize(
    ("headers", "body", "error"),
    [
        ({}, b"\xff", "not UTF-8"),
        ({"content-length": "secret"}, b"{}", "invalid content length"),
        ({"content-encoding": "gzip"}, b"compressed", "unsupported content encoding"),
    ],
)
def test_http_adapter_rejects_invalid_framing_without_echoing_body(headers, body, error):
    def handler(request):
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    with pytest.raises(RuntimeError, match=error):
        provider_for(handler).complete("prompt", {}, 1.0)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8080/decide",
        "http://127.0.0.1:8080/decide",
        "http://[::1]:8080/decide",
        "https://placement.example.test/decide",
    ],
)
def test_http_adapter_accepts_secure_or_loopback_endpoint(endpoint):
    assert HTTPPlacementProvider(endpoint, model="model").model == "model"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://placement.example.test/decide",
        "http://192.168.1.1/decide",
        "https://user:password@placement.example.test/decide",
        "https://placement.example.test/decide#secret",
        "https://placement.example.test/decide#",
        "https://placement.example.test:secret/decide",
        "https://placement.example.test:0/decide",
        "https://placement.example.test/\nsecret",
        " https://placement.example.test/decide",
        "file:///secret",
        "https:///secret",
        "",
    ],
)
def test_http_adapter_rejects_unsafe_endpoint_without_echoing_it(endpoint):
    with pytest.raises(ValueError) as error:
        HTTPPlacementProvider(endpoint, model="model")
    assert "password" not in str(error.value)
    assert "secret" not in str(error.value)
    assert "placement.example.test" not in str(error.value)


@pytest.mark.parametrize("key", ["", "key\nsecret", "key secret", "secret\t", "sécret"])
def test_http_adapter_rejects_invalid_secret_header_without_echoing_it(key):
    with pytest.raises(ValueError) as error:
        HTTPPlacementProvider("https://example.test", model="model", api_key=key)
    assert "secret" not in str(error.value)
    assert "sécret" not in str(error.value)


def test_env_factory_without_endpoint_fails_closed():
    inference = placement_inference_from_env({})
    result = inference.evaluate(current_tier="hot", size=100, allowed_tiers=["warm"])
    assert result.action == "stay"


def test_env_factory_builds_configured_provider(monkeypatch):
    captured = {}

    class Inference:
        def __init__(self, provider, **kwargs):
            captured.update(provider=provider, **kwargs)

    monkeypatch.setattr("cognistore.core.placement_llm_http.PlacementInference", Inference)
    placement_inference_from_env(
        {
            "COGNISTORE_LLM_ENDPOINT": "http://localhost:8080/decide",
            "COGNISTORE_LLM_MODEL": "model",
            "COGNISTORE_LLM_MODEL_VERSION": "2026-09-10",
            "COGNISTORE_LLM_API_KEY": "secret",
            "COGNISTORE_LLM_TIMEOUT_SECONDS": "1.5",
            "COGNISTORE_LLM_MAX_ATTEMPTS": "3",
        }
    )
    assert isinstance(captured["provider"], HTTPPlacementProvider)
    assert captured["provider"].model == "model"
    assert captured["provider"].version == "1"
    assert captured["provider"].model_version == "2026-09-10"
    assert captured["timeout_seconds"] == 1.5
    assert captured["max_attempts"] == 3


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("COGNISTORE_LLM_ENDPOINT", "https://user:secret@example.test"),
        ("COGNISTORE_LLM_MODEL", ""),
        ("COGNISTORE_LLM_MODEL_VERSION", "\nsecret"),
        ("COGNISTORE_LLM_API_KEY", "\nsecret"),
        ("COGNISTORE_LLM_TIMEOUT_SECONDS", "secret"),
        ("COGNISTORE_LLM_TIMEOUT_SECONDS", "nan"),
        ("COGNISTORE_LLM_TIMEOUT_SECONDS", "inf"),
        ("COGNISTORE_LLM_TIMEOUT_SECONDS", "0"),
        ("COGNISTORE_LLM_MAX_ATTEMPTS", "secret"),
        ("COGNISTORE_LLM_MAX_ATTEMPTS", "1.5"),
        ("COGNISTORE_LLM_MAX_ATTEMPTS", "0"),
    ],
)
def test_env_factory_rejects_invalid_config_without_exposing_values(key, value):
    config = {
        "COGNISTORE_LLM_ENDPOINT": "https://example.test/decide",
        "COGNISTORE_LLM_MODEL": "model",
        key: value,
    }
    with pytest.raises(ValueError, match="Invalid LLM deployment configuration") as error:
        placement_inference_from_env(config)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
