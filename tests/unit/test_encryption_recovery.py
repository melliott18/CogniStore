"""Offline lifecycle drills; deployment volume encryption remains operator evidence."""
from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import ssl
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from cognistore.db import open_catalog
from cognistore.encryption import EncryptionConfigurationError, tls_context


def _server_credentials(directory: Path, name: str) -> tuple[bytes, Path, Path]:
    """Create independent, ephemeral CA/server pairs for a real TLS exchange."""
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} drill CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name).public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    certfile = directory / f"{name}-cert.pem"
    keyfile = directory / f"{name}-key.pem"
    certfile.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    keyfile.chmod(0o600)
    return ca.public_bytes(serialization.Encoding.PEM), certfile, keyfile


def _exchange(certfile: Path, keyfile: Path, *, hostname: str = "localhost") -> bytes:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(certfile, keyfile)
    client_context = tls_context()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                try:
                    with server_context.wrap_socket(connection, server_side=True) as channel:
                        assert channel.recv(4) == b"ping"
                        channel.sendall(b"pong")
                except ssl.SSLError:
                    # Certificate/hostname rejection is asserted at the client.
                    pass

        with ThreadPoolExecutor(max_workers=1) as executor:
            completed = executor.submit(serve)
            try:
                with socket.create_connection(listener.getsockname(), timeout=5) as connection:
                    with client_context.wrap_socket(connection, server_hostname=hostname) as channel:
                        channel.sendall(b"ping")
                        assert channel.recv(4) == b"pong"
                        assert channel.version() in {"TLSv1.2", "TLSv1.3"}
                        certificate = channel.getpeercert(binary_form=True)
                        assert certificate is not None
                        return certificate
            finally:
                completed.result(timeout=5)


def test_tls_certificate_rotation_with_overlapping_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old/new trust overlap avoids downgrades and retiring the CA rejects old peers."""
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    old_ca, old_cert, old_key = _server_credentials(tmp_path, "old")
    new_ca, new_cert, new_key = _server_credentials(tmp_path, "new")
    bundle = tmp_path / "trusted-ca.pem"
    monkeypatch.setenv("COGNISTORE_TLS_CA_FILE", str(bundle))

    bundle.write_bytes(old_ca)
    original = _exchange(old_cert, old_key)
    with pytest.raises(ssl.SSLCertVerificationError):
        _exchange(new_cert, new_key)

    bundle.write_bytes(old_ca + new_ca)
    assert _exchange(old_cert, old_key) == original
    rotated = _exchange(new_cert, new_key)
    assert rotated != original
    with pytest.raises(ssl.SSLCertVerificationError):
        _exchange(new_cert, new_key, hostname="wrong-host.invalid")

    bundle.write_bytes(new_ca)
    assert _exchange(new_cert, new_key) == rotated
    with pytest.raises(ssl.SSLCertVerificationError):
        _exchange(old_cert, old_key)


def test_catalog_restore_requires_current_volume_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore an online snapshot through the DAL and fail closed on stale evidence.

    tmp_path represents an externally approved volume; this test does not claim
    that pytest's temporary directory is physically encrypted.
    """
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    config = tmp_path / "encryption.json"
    monkeypatch.setenv("COGNISTORE_ENCRYPTION_CONFIG", str(config))

    def attest(expiry: datetime) -> None:
        config.write_text(json.dumps({
            "version": 1,
            "components": {"catalog": {
                "mode": "encrypted-volume", "owner": "offline drill",
                "evidence": "synthetic fixture; no physical encryption assertion",
                "expires_at": expiry.isoformat(), "rotation_days": 90,
            }},
        }), encoding="utf-8")

    attest(datetime.now(timezone.utc) + timedelta(hours=1))
    active = tmp_path / "catalog.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    with closing(open_catalog(active)) as catalog:
        catalog.upsert("drill", "original", size=7, tier="hot", metadata={"source": "drill"})
        expected = catalog.get("drill", "original")
        with closing(sqlite3.connect(active)) as source:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        catalog.upsert("drill", "after-backup", size=8, tier="hot")

    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(backup.read_bytes())
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == digest

    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG")
    with pytest.raises(EncryptionConfigurationError, match="catalog"):
        open_catalog(restored, read_only=True)
    monkeypatch.setenv("COGNISTORE_ENCRYPTION_CONFIG", str(config))
    attest(datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(EncryptionConfigurationError, match="catalog"):
        open_catalog(restored, read_only=True)

    attest(datetime.now(timezone.utc) + timedelta(hours=1))
    with closing(open_catalog(restored, read_only=True)) as catalog:
        assert catalog.get("drill", "original") == expected
        assert catalog.get("drill", "after-backup") is None
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == digest
