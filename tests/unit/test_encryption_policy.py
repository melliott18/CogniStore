"""Production startup rejects insecure or unsubstantiated encryption controls."""
from __future__ import annotations

import json
import ssl

import pytest

from cognistore import encryption


def _config(tmp_path, monkeypatch, **changes):
    entry = {
        "mode": "encrypted-volume", "owner": "owner-secret-sentinel",
        "evidence": "evidence-secret-sentinel", "expires_at": "2099-01-01T00:00:00Z",
        "rotation_days": 90,
    }
    entry.update(changes)
    path = tmp_path / "encryption.json"
    path.write_text(json.dumps({"version": 1, "components": {"runtime": entry}}))
    monkeypatch.setenv("COGNISTORE_ENCRYPTION_CONFIG", str(path))
    return path


def test_production_is_default_and_only_explicit_development_bypasses(monkeypatch):
    monkeypatch.delenv("COGNISTORE_SECURITY_PROFILE")
    assert encryption.production_mode()
    with pytest.raises(encryption.EncryptionConfigurationError, match="TLS"):
        encryption.require_tls_url("http://user:secret@localhost", "service")
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")
    encryption.require_tls_url("http://localhost", "service")
    encryption.require_at_rest("runtime")
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "prod")
    with pytest.raises(encryption.EncryptionConfigurationError, match="profile|PROFILE"):
        encryption.production_mode()


@pytest.mark.parametrize("url", ["http://localhost", "https://", "https://host:bad", "ftp://host"])
def test_endpoint_errors_do_not_echo_endpoints(monkeypatch, url):
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    with pytest.raises(encryption.EncryptionConfigurationError) as exc:
        encryption.require_tls_url(url, "service")
    assert url not in str(exc.value)


def test_tls_context_never_disables_verification(monkeypatch):
    monkeypatch.delenv("COGNISTORE_TLS_CA_FILE", raising=False)
    context = encryption.tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("contents", [None, "", "secret-sentinel-not-a-certificate"])
def test_bad_ca_fails_without_contents(tmp_path, monkeypatch, contents):
    path = tmp_path / "ca.pem"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setenv("COGNISTORE_TLS_CA_FILE", str(path))
    with pytest.raises(encryption.EncryptionConfigurationError) as exc:
        encryption.tls_context()
    assert "secret-sentinel" not in str(exc.value)


def test_attestation_required_and_safe_status(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG", raising=False)
    with pytest.raises(encryption.EncryptionConfigurationError, match="attestation"):
        encryption.require_at_rest("runtime")
    encryption.require_at_rest("storage", mode="provider-managed")
    _config(tmp_path, monkeypatch)
    encryption.require_at_rest("runtime")
    status = encryption.encryption_status()
    assert status["at_rest"]["runtime"]["current"]
    assert status["at_rest"]["catalog"]["mode"] == "unattested"
    assert "secret-sentinel" not in json.dumps(status)
    assert str(tmp_path) not in json.dumps(status)


@pytest.mark.parametrize("changes", [
    {"expires_at": "2000-01-01T00:00:00Z"}, {"expires_at": "2099-01-01"},
    {"rotation_days": 0}, {"rotation_days": True}, {"mode": "plaintext"},
    {"owner": ""}, {"key": "must-never-appear"}, {"mode": []},
])
def test_invalid_or_expired_attestation_rejected(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    _config(tmp_path, monkeypatch, **changes)
    with pytest.raises(encryption.EncryptionConfigurationError) as exc:
        encryption.require_at_rest("runtime")
    assert "must-never-appear" not in str(exc.value)
    assert "secret-sentinel" not in str(exc.value)


def test_status_exposes_expiry_without_claiming_physical_verification(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, expires_at="2000-01-01T00:00:00Z")
    status = encryption.encryption_status()["at_rest"]["runtime"]
    assert status["source"] == "operator-attestation"
    assert not status["current"]


@pytest.mark.parametrize("raw", [
    '{"version":1,"version":1,"components":{}}', '{"version":true,"components":{}}',
    '{"version":1,"components":{"unknown":{}}}', '[]', 'sentinel-bad-json',
])
def test_bad_document_safe_error(tmp_path, monkeypatch, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw)
    monkeypatch.setenv("COGNISTORE_ENCRYPTION_CONFIG", str(path))
    with pytest.raises(encryption.EncryptionConfigurationError, match="Invalid encryption"):
        encryption.encryption_status()


def test_api_rejects_plaintext_before_loading_drivers(monkeypatch):
    from cognistore.api import server

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_API_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("COGNISTORE_API_TLS_KEYFILE", raising=False)
    monkeypatch.setattr(server, "load_drivers", lambda _: pytest.fail("must reject before clients"))
    with pytest.raises(encryption.EncryptionConfigurationError, match="certificate and key"):
        server.main(["--drivers", "unused.yaml", "--catalog-db", "unused.db"])


def test_health_listener_requires_tls(monkeypatch):
    from cognistore.jobs.health import HealthServer

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_HEALTH_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("COGNISTORE_HEALTH_TLS_KEYFILE", raising=False)
    with pytest.raises(encryption.EncryptionConfigurationError, match="certificate and key"):
        HealthServer(None)


def test_sdk_rejects_plaintext_even_loopback(monkeypatch):
    from cognistore.sdk.client import CogniStoreClient

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    with pytest.raises(encryption.EncryptionConfigurationError, match="TLS"):
        CogniStoreClient("http://127.0.0.1:8080")


def test_otel_plaintext_is_not_silently_ignored(monkeypatch):
    from cognistore.observability import configure_observability

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.setenv("COGNISTORE_OTEL_ENABLED", "true")
    monkeypatch.setenv("COGNISTORE_OTEL_ENDPOINT", "http://localhost:4318/v1/traces")
    with pytest.raises(encryption.EncryptionConfigurationError, match="OTLP"):
        configure_observability()


def test_cli_status_does_not_require_configured_resources(monkeypatch, capsys):
    from cognistore.cli.cognistore_cli import main

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG", raising=False)
    assert main(["--no-config", "encryption-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["profile"] == "production"
    assert not status["at_rest"]["catalog"]["current"]


def test_cli_runtime_requires_attestation(monkeypatch, tmp_path, capsys):
    from cognistore.cli.cognistore_cli import main

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG", raising=False)
    assert main(["--no-config", "--base", str(tmp_path), "ls", "bucket"]) != 0
    assert "attestation" in capsys.readouterr().err


def test_sample_requires_explicit_development(monkeypatch):
    from argparse import Namespace

    from cognistore.samples.content_search.cli import _open_runtime

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    with pytest.raises(ValueError, match="development"):
        _open_runtime(Namespace())


def test_scheduler_requires_runtime_evidence(tmp_path, monkeypatch):
    from cognistore.jobs.scheduler import SQLiteScheduleStore

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG", raising=False)
    with pytest.raises(encryption.EncryptionConfigurationError, match="runtime"):
        SQLiteScheduleStore(tmp_path / "schedule.db")
    assert not (tmp_path / "schedule.db").exists()


def test_cli_status_reports_tier_encryption_without_key_ids(tmp_path, monkeypatch, capsys):
    from cognistore.cli.cognistore_cli import main

    path = tmp_path / "drivers.yaml"
    path.write_text(f"tiers:\n  hot:\n    driver: posix\n    path: {tmp_path / 'hot'}\n")
    assert main(["--no-config", "--drivers", str(path), "encryption-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert "hot" in status["tiers"]
    assert str(tmp_path) not in json.dumps(status)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_telemetry_redirect_cannot_forward_payload_to_plaintext(monkeypatch, status):
    import requests
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    from cognistore import observability

    captured = []
    sent = []
    original = trace_exporter.OTLPSpanExporter

    def exporter(**kwargs):
        result = original(**kwargs)
        # The injected Session is our transport contract; SDK internals change.
        captured.append(kwargs["session"])
        return result

    class Redirect(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            sent.append(request)
            assert request.url.startswith("https://")
            response = requests.Response()
            response.status_code = status
            response.headers["Location"] = "http://plaintext.invalid/trace"
            response.url = request.url
            response.request = request
            response._content = b""
            return response

        def close(self):
            pass

    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.setenv("COGNISTORE_OTEL_ENABLED", "true")
    monkeypatch.setenv("COGNISTORE_OTEL_ENDPOINT", "https://collector.invalid/v1/traces")
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setattr(observability, "_provider", None)
    monkeypatch.setattr(observability, "_tracer", observability._tracer)
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", exporter)
    observability.configure_observability()
    try:
        assert len(captured) == 1
        session = captured[0]
        session.mount("https://", Redirect())
        session.mount("http://", Redirect())
        with pytest.raises(requests.TooManyRedirects):
            session.post("https://collector.invalid/v1/traces", data=b"private payload")
        assert len(sent) == 1
        assert sent[0].body == b"private payload"
    finally:
        observability._provider.shutdown()
