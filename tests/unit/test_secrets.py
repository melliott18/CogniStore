from __future__ import annotations

import json
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from cognistore.jobs.retry import FailureCategory, classify_job_error
from cognistore.secrets import (
    KeyReference,
    SecretAccessError,
    SecretConfigurationError,
    SecretReference,
    SecretResolver,
    SecretUnavailableError,
    SecretValue,
)
from cognistore.secrets.config import build_secret_resolver, parse_secret_reference
from cognistore.utils.redaction import REDACTED, redact, redact_text

SENTINEL = "opaque-secret-59-test-credential"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_cache_expiry_rotation_and_outage_fail_closed():
    clock = Clock()
    provider = Mock()
    first = SecretValue(SENTINEL, version="1", ttl_seconds=10)
    second = SecretValue(SENTINEL + "-rotated", version="2")
    provider.fetch.side_effect = [first, RuntimeError(SENTINEL), second]
    resolver = SecretResolver({"test": provider}, cache_ttl_seconds=20, clock=clock)
    ref = SecretReference("test", "storage")
    assert resolver.resolve(ref) is first
    clock.now = 9.999
    assert resolver.resolve(ref) is first
    assert provider.fetch.call_count == 1
    clock.now = 10
    with pytest.raises(SecretUnavailableError) as error:
        resolver.resolve(ref)
    assert error.value.__context__ is None
    assert SENTINEL not in "".join(traceback.format_exception(error.value))
    assert resolver.resolve(ref) is second
    clock.now = 29.999
    assert resolver.resolve(ref) is second
    assert provider.fetch.call_count == 3


def test_fetch_latency_cannot_extend_provider_lease():
    clock = Clock()

    def fetch(ref):
        clock.now += 5
        return SecretValue(SENTINEL, ttl_seconds=5)

    resolver = SecretResolver({"test": Mock(fetch=fetch)}, clock=clock)
    with pytest.raises(SecretUnavailableError, match="expired"):
        resolver.resolve(SecretReference("test", "storage"))


def test_slow_bundle_lookup_cannot_start_an_operation_with_expired_material():
    clock = Clock()

    def fetch(ref):
        if ref.name == "slow":
            clock.now += 2
            return SecretValue(SENTINEL, ttl_seconds=30)
        return SecretValue(SENTINEL, ttl_seconds=1)

    resolver = SecretResolver({"test": Mock(fetch=fetch)}, clock=clock)
    with pytest.raises(SecretUnavailableError, match="bundle expired"):
        resolver.resolve_many([SecretReference("test", "short"), SecretReference("test", "slow")])


def test_cache_capacity_pins_invalidation_and_key_context():
    provider = Mock()
    provider.fetch.side_effect = lambda ref: SecretValue(SENTINEL, version=ref.version)
    key_provider = Mock()
    key_provider.unwrap.side_effect = lambda ref: SecretValue(b"test-key-material-59")
    resolver = SecretResolver({"test": provider}, key_providers={"kms": key_provider}, max_entries=2)
    refs = [SecretReference("test", "storage", version=str(index)) for index in range(3)]
    resolver.resolve_many(refs)
    assert provider.fetch.call_count == 3
    resolver.resolve(refs[1])
    assert provider.fetch.call_count == 3
    resolver.resolve(refs[0])
    assert provider.fetch.call_count == 4
    resolver.invalidate(refs[0])
    resolver.resolve(refs[0])
    assert provider.fetch.call_count == 5
    ref = KeyReference("kms", "key", b"ciphertext", (("b", "2"), ("a", "1")))
    same_ref = KeyReference("kms", "key", b"ciphertext", (("a", "1"), ("b", "2")))
    assert resolver.resolve_key(ref) is resolver.resolve_key(same_ref)
    changed = KeyReference("kms", "key", b"ciphertext", (("a", "different"),))
    resolver.resolve_key(changed)
    assert key_provider.unwrap.call_count == 2
    resolver.invalidate()
    resolver.resolve_key(ref)
    assert key_provider.unwrap.call_count == 3


def test_field_invalidation_discards_cached_parent_used_by_storage():
    provider = Mock(fetch=Mock(return_value=SecretValue(SENTINEL)))
    resolver = SecretResolver({"test": provider})
    ref = SecretReference("test", "storage")
    resolver.resolve(ref)
    resolver.invalidate(SecretReference("test", "storage", field="access_key"))
    resolver.resolve(ref)
    assert provider.fetch.call_count == 2


def test_concurrent_refresh_is_single_flight():
    provider = Mock(fetch=Mock(return_value=SecretValue(SENTINEL)))
    resolver = SecretResolver({"test": provider})
    barrier = threading.Barrier(8)

    def resolve(_):
        barrier.wait()
        return resolver.resolve(SecretReference("test", "storage"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(resolve, range(8)))
    assert all(value is results[0] for value in results)
    assert provider.fetch.call_count == 1


def test_close_is_idempotent_and_closes_shared_provider_once():
    provider = Mock()
    resolver = SecretResolver({"first": provider}, key_providers={"second": provider})
    resolver.close()
    resolver.close()
    provider.close.assert_called_once_with()
    with pytest.raises(SecretConfigurationError, match="closed"):
        resolver.resolve(SecretReference("first", "storage"))


def test_provider_cleanup_never_leaks_diagnostics(caplog):
    def close():
        logging.getLogger("test.cleanup.provider").error(SENTINEL)
        raise RuntimeError(SENTINEL)

    resolver = SecretResolver({"test": Mock(close=close)})
    with caplog.at_level(logging.DEBUG):
        resolver.close()
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("failure,expected,category,retryable", [
    (RuntimeError(SENTINEL), SecretUnavailableError, FailureCategory.UNAVAILABLE, True),
    (SecretAccessError(SENTINEL), SecretAccessError, FailureCategory.AUTHORIZATION, False),
    (SecretConfigurationError(SENTINEL), SecretConfigurationError, FailureCategory.INVALID, False),
])
def test_provider_error_details_do_not_reach_worker_or_diagnostics(
    failure, expected, category, retryable, caplog,
):
    def fetch(ref):
        logging.getLogger("test.custom.provider").warning("raw %s", SENTINEL)
        raise failure

    resolver = SecretResolver({"test": Mock(fetch=fetch)})
    with caplog.at_level(logging.DEBUG), pytest.raises(expected) as error:
        resolver.resolve(SecretReference("test", "storage"))
    assert SENTINEL not in caplog.text
    assert SENTINEL not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None
    classification = classify_job_error(error.value)
    assert classification.category == category
    assert classification.retryable is retryable


def test_opaque_values_and_rotated_material_are_redacted_in_audit_and_errors():
    value = SecretValue(json.dumps({"ordinary_field": SENTINEL, "nested": [SENTINEL + "-other"]}))
    assert value.text().startswith("{")
    assert str(value) == REDACTED
    assert SENTINEL not in repr(value)
    assert redact({"detail": value}) == {"detail": REDACTED}
    assert redact({"detail": f"failed {SENTINEL}"}) == {"detail": "failed " + REDACTED}
    assert redact_text(SENTINEL + "-other") == REDACTED
    del value
    assert redact_text(SENTINEL) == REDACTED
    # Operational audit identifiers also pass through the shared redactor.
    from cognistore.core.audit import audit_text_identity
    assert audit_text_identity(SENTINEL).startswith("[REDACTED:sha256:")


def test_binary_keys_and_json_escaped_material_are_redacted():
    import base64

    value = SecretValue(b"\xff\xfe\x13\x42\xab\xef")
    assert value.reveal() == b"\xff\xfe\x13\x42\xab\xef"
    assert redact_text(base64.b64encode(value.reveal()).decode()) == REDACTED
    assert redact_text(value.reveal().hex()) == REDACTED
    assert redact_text(repr(value.reveal())) == REDACTED
    assert redact({"ordinary": value.reveal()}) == {"ordinary": REDACTED}
    with pytest.raises(SecretAccessError) as error:
        value.text()
    assert error.value.__context__ is None
    text = 'opaque-59-"quoted"\nsecret'
    SecretValue(text)
    assert redact_text(json.dumps(text)[1:-1]) == REDACTED
    assert redact_text("ordinary\0text") == "ordinary[NUL]text"
    SecretValue(b"left\0right")
    assert redact_text("diagnostic: left\0right") == "diagnostic: " + REDACTED


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "300"])
def test_invalid_ttls_are_rejected_without_echoing_input(value):
    with pytest.raises(SecretConfigurationError):
        SecretResolver({}, cache_ttl_seconds=value)
    with pytest.raises(SecretAccessError):
        SecretValue(SENTINEL, ttl_seconds=value)


@pytest.mark.parametrize("config", [
    None, [], {"provider": "test"}, {"provider": "test", "name": "n", "plaintext": SENTINEL},
    {"provider": [], "name": "n"}, {"provider": "test", "name": "n", "field": 2},
])
def test_invalid_references_are_safe(config):
    with pytest.raises(SecretConfigurationError) as error:
        parse_secret_reference(config)
    assert SENTINEL not in str(error.value)


@pytest.mark.parametrize("config", [
    [], {"token": SENTINEL}, {"providers": []}, {"providers": {"test": []}},
    {"providers": {"test": {"type": SENTINEL}}},
    {"providers": {"test": {"type": "vault_kv", "url": "https://vault.example.com"}}},
    {"providers": {"test": {"type": "aws_secrets_manager", "secret_key": SENTINEL}}},
    {"cache_ttl_seconds": 0},
])
def test_invalid_provider_config_is_safe(config):
    with pytest.raises(SecretConfigurationError) as error:
        build_secret_resolver(config)
    assert SENTINEL not in str(error.value)


def test_config_constructs_provider_without_exposing_material(monkeypatch):
    provider = Mock(fetch=Mock(return_value=SecretValue(SENTINEL)))
    factory = Mock(return_value=provider)
    monkeypatch.setattr("cognistore.secrets.providers.VaultKVProvider", factory)
    resolver = build_secret_resolver({"providers": {"v": {
        "type": "vault_kv", "url": "https://vault.example.com", "token_file": "/run/token",
    }}})
    factory.assert_called_once_with(address="https://vault.example.com", token_file="/run/token")
    assert resolver.resolve(parse_secret_reference({"provider": "v", "name": "storage"})).text() == SENTINEL
    resolver.close()


def test_bad_key_references_and_unknown_providers():
    for kwargs in ({"ciphertext": "text"}, {"context": (("a", "1"), ("a", "2"))}):
        options = {"provider": "test", "name": "key", "ciphertext": b"blob", **kwargs}
        with pytest.raises(SecretConfigurationError):
            KeyReference(**options)
    resolver = SecretResolver({})
    with pytest.raises(SecretConfigurationError):
        resolver.resolve(SecretReference("missing", "storage"))
    with pytest.raises(SecretConfigurationError):
        resolver.resolve_key(KeyReference("missing", "key", b"blob"))


def test_production_example_contains_only_references_or_workload_identity():
    from pathlib import Path

    import yaml

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "examples/drivers.production.yaml").read_text()
    )
    forbidden = {"access_key", "secret_key", "session_token", "credential", "connection_string"}
    for tier in config["tiers"].values():
        assert not forbidden.intersection(tier)
        for key, value in tier.items():
            if key.endswith("_ref"):
                assert parse_secret_reference(value).provider in config["secrets"]["providers"]
    for declaration in config["secrets"]["providers"].values():
        assert not {"token", "password", "secret", "access_key", "secret_key"}.intersection(declaration)


def test_secret_failure_does_not_reach_spans_metrics_or_structured_logs(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cognistore import observability as telemetry

    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", tracing.get_tracer("secrets-test"))
    provider = Mock(fetch=Mock(side_effect=RuntimeError(SENTINEL)))
    resolver = SecretResolver({"test": provider})
    try:
        with pytest.raises(SecretUnavailableError):
            with telemetry.observe("driver", "get_object", backend="s3"):
                resolver.resolve(SecretReference("test", "storage"))
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert SENTINEL not in spans[0].to_json()
        assert SENTINEL.encode() not in telemetry.metrics_response()[0]
        record = logging.LogRecord("provider", logging.ERROR, __file__, 1, SENTINEL, (), None)
        assert SENTINEL not in telemetry.StructuredLogFormatter().format(record)
    finally:
        tracing.shutdown()
